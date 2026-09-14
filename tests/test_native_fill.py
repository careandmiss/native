"""Unit tests for mcp_ppt_native_fill.

Coverage:
- io_utils: atomic write/read round-trip, JSON read/write, BOM strip
- svg_edits: shape-id targeting, tspan clearing, data-pptx-* preservation
- autofix: 5 fix functions (overflow / viewBox / gradient / picture / font)
- runner: SKILL_DIR 5-step priority resolution, export-receipt regex
- pipeline: page_plan validation
- server: stdio JSON-RPC dispatch happy path + error path

These tests do NOT spawn ppt-master subprocesses (that would require the
real skill install); subprocess behaviour is exercised by examples/.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

# Make src/ importable regardless of where pytest is invoked from.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp_ppt_native_fill import (  # noqa: E402
    autofix,
    io_utils,
    pipeline,
    runner,
    server,
    svg_edits,
)


# ---------------------------------------------------------------------------
# io_utils
# ---------------------------------------------------------------------------

class IoUtilsTests(unittest.TestCase):
    def test_atomic_write_then_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "file.txt"
            io_utils.write_utf8_atomic(p, "hello\n世界")
            self.assertEqual(io_utils.read_utf8(p), "hello\n世界")
            self.assertTrue(p.is_file())

    def test_atomic_write_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            io_utils.write_utf8_atomic(p, "v1")
            io_utils.write_utf8_atomic(p, "v2")
            self.assertEqual(io_utils.read_utf8(p), "v2")

    def test_bom_strip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            p.write_bytes(b"\xef\xbb\xbfhello")
            self.assertEqual(io_utils.read_utf8(p), "hello")

    def test_json_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.json"
            payload = {"中文": [1, 2, 3], "nested": {"k": "v"}}
            io_utils.write_json_atomic(p, payload)
            self.assertEqual(io_utils.read_json(p), payload)


# ---------------------------------------------------------------------------
# svg_edits
# ---------------------------------------------------------------------------

SAMPLE_SVG = """<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <g id="shape-1" data-pptx-source-ref="slide:1" data-pptx-object="shape">
    <rect x="100" y="100" width="500" height="60" fill="#1D2CAB"/>
    <text x="120" y="140" font-size="32" fill="#FFFFFF" data-pptx-frame="100 100 500 60">原标题</text>
  </g>
  <g id="shape-2" data-pptx-source-ref="slide:1" data-pptx-object="shape">
    <text x="120" y="240" font-size="18" fill="#222">
      <tspan x="120" dy="0">多行第一行</tspan>
      <tspan x="120" dy="22">多行第二行</tspan>
    </text>
  </g>
  <g id="shape-3" data-pptx-source-ref="slide:1" data-pptx-object="group">
    <text x="100" y="300" font-size="14">部门：XXX 汇报人：XXX</text>
  </g>
</svg>
"""


class SvgEditsTests(unittest.TestCase):
    def _write_sample(self, td: Path) -> Path:
        p = td / "slide_01.svg"
        p.write_text(SAMPLE_SVG, encoding="utf-8")
        return p

    def test_apply_text_edits_replaces_text(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            audit = svg_edits.apply_text_edits(
                svg,
                {"shape-1": "新标题"},
            )
            self.assertEqual(audit[0]["status"], "applied")
            self.assertEqual(audit[0]["old"], "原标题")
            self.assertEqual(audit[0]["new"], "新标题")
            content = svg.read_text(encoding="utf-8")
            self.assertIn("新标题", content)
            self.assertNotIn("原标题", content)

    def test_apply_text_edits_clears_tspans(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            svg_edits.apply_text_edits(svg, {"shape-2": "单行"})
            content = svg.read_text(encoding="utf-8")
            self.assertIn("单行", content)
            self.assertNotIn("多行第一行", content)
            self.assertNotIn("<tspan", content)

    def test_apply_text_edits_preserves_data_pptx_attrs(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            svg_edits.apply_text_edits(svg, {"shape-1": "X", "shape-3": "Y"})
            content = svg.read_text(encoding="utf-8")
            self.assertIn('data-pptx-source-ref="slide:1"', content)
            self.assertIn('data-pptx-object="shape"', content)
            self.assertIn('data-pptx-object="group"', content)
            self.assertIn('data-pptx-frame="100 100 500 60"', content)

    def test_apply_text_edits_does_not_duplicate_xml_space(self):
        # Bug: apply_text_edits called with preserve_whitespace=True on a
        # <text> that already has ``xml:space="preserve"`` used to set()
        # the bare key, producing a *duplicate* ``xml:space`` attribute
        # which the strict downstream svg_to_pptx parser rejects with
        # ``duplicate attribute: line N, column M``. Verify the attribute
        # appears exactly once after the edit.
        sample = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'viewBox="0 0 1280 720">'
            '<g id="shape-1">'
            '<text x="10" y="20" xml:space="preserve">old</text></g>'
            '</svg>'
        )
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            p = td / "slide.svg"
            p.write_text(sample, encoding="utf-8")
            svg_edits.apply_text_edits(p, {"shape-1": "new"},
                                       preserve_whitespace=True)
            text = p.read_text(encoding="utf-8")
            self.assertEqual(text.count('xml:space="preserve"'), 1,
                             "xml:space must not be duplicated")

    def test_unknown_shape_id_reported(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            audit = svg_edits.apply_text_edits(svg, {"shape-99": "missing"})
            self.assertEqual(audit[0]["status"], "not_found")

    def test_no_text_node_reported(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_02.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-X"><rect x="0" y="0" width="100" height="50"/></g></svg>',
                encoding="utf-8",
            )
            audit = svg_edits.apply_text_edits(svg, {"shape-X": "ignored"})
            self.assertEqual(audit[0]["status"], "no_text_node")

    def test_write_new_content_block_appends(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            svg_edits.write_new_content_block(
                svg,
                group_id="new-block",
                bounds="100 500 600 100",
                inner_svg='<rect x="100" y="500" width="600" height="100" fill="#ABC"/>',
            )
            content = svg.read_text(encoding="utf-8")
            self.assertIn('id="new-block"', content)
            self.assertIn('data-pptx-bounds="100 500 600 100"', content)
            self.assertIn("fill=\"#ABC\"", content)

    def test_write_new_content_block_rejects_bad_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            with self.assertRaises(ValueError):
                svg_edits.write_new_content_block(
                    svg,
                    group_id="bad",
                    bounds="100 500",  # only two values
                    inner_svg="<rect/>",
                )


# ---------------------------------------------------------------------------
# autofix
# ---------------------------------------------------------------------------

class AutofixTests(unittest.TestCase):
    def test_dedupe_duplicate_attrs_drops_repeats(self):
        from mcp_ppt_native_fill.autofix import _dedupe_duplicate_attrs
        svg = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<text xml:space="preserve" xml:space="preserve" font-size="12">hi</text>'
            '</svg>'
        )
        out = _dedupe_duplicate_attrs(svg)
        # Only one xml:space survives; xml.etree can now parse it
        ET.fromstring(out)
        self.assertEqual(out.count('xml:space="preserve"'), 1)

    def test_dedupe_duplicate_attrs_preserves_doctype_and_cdata(self):
        from mcp_ppt_native_fill.autofix import _dedupe_duplicate_attrs
        svg = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g><!-- <xml:space="a"/> --></g>'
            '<text><![CDATA[<xml:space="b"/>]]></text>'
            '</svg>'
        )
        out = _dedupe_duplicate_attrs(svg)
        # The DOCTYPE / comment / CDATA payloads must be untouched — if our
        # regex had matched them as tags, ElementTree would reject the result.
        ET.fromstring(out)
        self.assertIn('<![CDATA[<xml:space="b"/>]]>', out)
        self.assertIn('<!-- <xml:space="a"/> -->', out)

    def test_dedupe_duplicate_attrs_preserves_self_closing(self):
        from mcp_ppt_native_fill.autofix import _dedupe_duplicate_attrs
        svg = '<svg><path d="M0 0L1 1" data-x="a" data-x="b"/></svg>'
        out = _dedupe_duplicate_attrs(svg)
        ET.fromstring(out)
        # Self-closing slash and the single surviving attribute both intact.
        self.assertIn('data-x="a"', out)
        self.assertIn('/>', out)
        self.assertEqual(out.count('data-x='), 1)

    def test_parse_svg_handles_vendor_duplicate_attributes(self):
        # Regression: ppt-master's pptx_to_svg.py occasionally emits the same
        # attribute twice on one element (e.g. xml:space="preserve"). The
        # strict ElementTree parser would raise; _parse_svg must repair it.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            svg.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<text xml:space="preserve" xml:space="preserve" font-size="12">x</text>'
                '</svg>',
                encoding="utf-8",
            )
            tree, root = autofix._parse_svg(svg)
            # The svg root survived and the text element is reachable
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            text = root.find("{http://www.w3.org/2000/svg}text")
            self.assertIsNotNone(text)
            self.assertEqual(text.text, "x")

    def test_escape_inner_attr_quotes_handles_vendor_font_family(self):
        # Regression: ppt-master emits font-family values with literal " "
        # around Chinese font names, producing
        # ``font-family=""微软雅黑", sans-serif"``. ElementTree rejects this
        # as not well-formed. _escape_inner_attr_quotes must rewrite the
        # inner quotes to &quot; so the value becomes the intended
        # ``"微软雅黑", sans-serif``.
        from mcp_ppt_native_fill.autofix import _escape_inner_attr_quotes
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<text font-family=""微软雅黑", sans-serif">x</text>'
            '</svg>'
        )
        repaired = _escape_inner_attr_quotes(svg)
        ET.fromstring(repaired)
        # The original literal inner quotes are gone (replaced with &quot;)
        self.assertNotIn('""微软雅黑"', repaired)
        # The value is now correctly escaped
        self.assertIn('&quot;微软雅黑&quot;, sans-serif', repaired)

    def test_escape_inner_attr_quotes_preserves_cdata_comments_pi(self):
        from mcp_ppt_native_fill.autofix import _escape_inner_attr_quotes
        svg = (
            '<?xml version="1.0"?>'
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<!-- <font family="a"b> -->'
            '<text><![CDATA[font "x" <foo/>]]></text>'
            '</svg>'
        )
        repaired = _escape_inner_attr_quotes(svg)
        ET.fromstring(repaired)
        # Comment content and CDATA content are never touched
        self.assertIn('<!-- <font family="a"b> -->', repaired)
        self.assertIn('<![CDATA[font "x" <foo/>]]>', repaired)

    def test_parse_svg_handles_vendor_font_family_bug(self):
        # End-to-end: a real vendor-emitted SVG (Chinese font family with
        # bare inner quotes) must parse via autofix._parse_svg.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<text font-family=""微软雅黑", sans-serif" font-size="12">x</text>'
                '</svg>',
                encoding="utf-8",
            )
            tree, root = autofix._parse_svg(svg)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            text = root.find("{http://www.w3.org/2000/svg}text")
            self.assertIsNotNone(text)
            self.assertEqual(text.text, "x")

    def test_parse_svg_persists_repair_for_vendor_tools(self):
        # The repair is not only useful in-memory — vendor's
        # svg_quality_checker and svg_to_pptx also parse the SVGs from
        # disk, so the repaired content must be written back.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            original = (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<text font-family=""微软雅黑", sans-serif">x</text>'
                '</svg>'
            )
            svg.write_text(original, encoding="utf-8")
            autofix._parse_svg(svg)
            on_disk = svg.read_text(encoding="utf-8")
            # The literal unescaped inner quotes are gone on disk.
            self.assertNotIn('""微软雅黑"', on_disk)
            # And the file is now valid XML.
            ET.fromstring(on_disk)

    def test_parse_svg_does_not_overwrite_clean_files(self):
        # A clean SVG should pass through _parse_svg byte-identical.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            original = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<text font-family="微软雅黑, sans-serif">x</text>'
                '</svg>'
            )
            svg.write_text(original, encoding="utf-8")
            autofix._parse_svg(svg)
            self.assertEqual(svg.read_text(encoding="utf-8"), original)

    def _make_overflow_svg(self, td: Path, shape_id: str = "shape-3") -> Path:
        svg = (
            '<?xml version="1.0"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            f'<g id="{shape_id}" data-pptx-source-ref="slide:1">'
            '<text x="100" y="100" font-size="40">部门：商务部 实施日期：2024-01-01</text>'
            '</g></svg>'
        )
        p = td / "slide_01.svg"
        p.write_text(svg, encoding="utf-8")
        return p

    def test_fix_text_overflow_shrinks(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._make_overflow_svg(td)
            records = autofix.fix_text_overflow(svg, ["shape-3"], shrink_factor=0.85)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].issue, "text_overflow")
            self.assertAlmostEqual(records[0].after, 40 * 0.85, places=2)
            # file now reflects new font-size
            self.assertIn('font-size="34"', svg.read_text(encoding="utf-8"))

    def test_detect_overflow_shapes(self):
        stdout = (
            "slide_01.svg: shape-3 exceeds owning frame ... "
            "overflow horizontal 27.6%\n"
            "shape-7 also overflow horizontal 1.7%\n"
        )
        found = autofix.detect_overflow_shapes(stdout)
        self.assertIn("shape-3", found)
        self.assertIn("shape-7", found)
        self.assertAlmostEqual(found["shape-3"][0], 27.6)

    def test_fix_viewbox_missing_restores(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg">'  # no viewBox
                '<rect/></svg>',
                encoding="utf-8",
            )
            rec = autofix.fix_viewbox_missing(svg, width=1280, height=720)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.after, "0 0 1280 720")
            self.assertIn('viewBox="0 0 1280 720"', svg.read_text(encoding="utf-8"))

    def test_fix_viewbox_missing_noop_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080">'
                '</svg>',
                encoding="utf-8",
            )
            self.assertIsNone(autofix.fix_viewbox_missing(svg))

    def test_fix_gradient_strips_and_rewrites(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_02.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<defs><linearGradient id="ggrad1" x1="0" y1="0.5" x2="1" y2="0.5">'
                '<stop offset="0" stop-color="#FFF" stop-opacity="0"/>'
                '<stop offset="1" stop-color="#FFF"/></linearGradient></defs>'
                '<rect x="0" y="0" width="600" height="100" fill="url(#ggrad1)"/>'
                '</svg>',
                encoding="utf-8",
            )
            records = autofix.fix_gradient_unexportable(svg)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].action, "strip_linearGradient")
            content = svg.read_text(encoding="utf-8")
            self.assertNotIn("<linearGradient", content)
            self.assertIn('fill="none"', content)

    def test_fix_unsafe_font(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<text x="0" y="50" font-size="32" '
                'font-family="&quot;思源黑体 CN Regular&quot;, sans-serif">x</text>'
                '</svg>',
                encoding="utf-8",
            )
            records = autofix.fix_unsafe_font(svg)
            self.assertEqual(len(records), 1)
            content = svg.read_text(encoding="utf-8")
            self.assertIn("微软雅黑", content)
            self.assertNotIn("思源黑体", content)

    def test_fix_picture_structure_nested_to_flat(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_03.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-8" data-pptx-object="picture">'
                '<svg viewBox="0 0 1 1"><image x="0" y="0" width="1" height="1"/></svg>'
                '</g></svg>',
                encoding="utf-8",
            )
            records = autofix.fix_picture_structure(svg)
            self.assertTrue(len(records) >= 1)
            content = svg.read_text(encoding="utf-8")
            # data-pptx-object="picture" must be PRESERVED on the <g>; vendor
            # needs it to rehydrate the original DrawingML.
            self.assertIn('data-pptx-object="picture"', content)
            # The nested <svg viewBox=...> wrapper around <image/> is gone.
            self.assertNotIn("<svg viewBox=", content)
            # Verify it now matches the flat pattern
            self.assertRegex(
                content,
                r'data-pptx-object="picture"[^>]*>\s*<image[^>]*/>\s*</g>',
            )

    def test_fix_picture_structure_flat_to_nested(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_04.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-9" data-pptx-object="picture">'
                '<image x="0" y="0" width="1280" height="720" href="x.png"/>'
                '</g></svg>',
                encoding="utf-8",
            )
            records = autofix.fix_picture_structure(svg)
            self.assertTrue(len(records) >= 1)
            content = svg.read_text(encoding="utf-8")
            self.assertIn("<svg viewBox=", content)

    def test_fix_gradient_clears_solid_white_on_1920x1080(self):
        """Bug 01: a medium-sized solid-white panel on 1920x1080 canvas must
        be PROTECTED (not cleared) — the bug is that the hardcoded
        ``fw < 0.6 * 1280 and fh < 0.6 * 720`` threshold treats an 800×500
        panel as "full canvas" on a 1920×1080 template, clearing content
        card backgrounds that the template designer intended to keep white.

        On 1280×720 (the original boteng target), 800×500 IS a large panel
        that legitimately covers the slide background image → cleared.
        On 1920×1080, the same 800×500 is just a content card → must stay.

        The fix is to read canvas dimensions from the SVG root and use a
        relative threshold (0.6 × canvas_w / canvas_h).
        """
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_01.svg"
            # 1920×1080 canvas with a 800×500 medium-size white card
            # placed at top-left (legitimate content card on this canvas).
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'width="1920" height="1080" viewBox="0 0 1920 1080">'
                '<defs>'
                '<linearGradient id="g1">'
                '<stop offset="0" stop-color="#fff"/>'
                '</linearGradient>'
                '</defs>'
                '<g id="shape-1" data-pptx-frame="100 100 800 500" fill="#FFFFFF">'
                '<rect x="100" y="100" width="800" height="500"/>'
                '</g>'
                '</svg>',
                encoding="utf-8",
            )
            records = autofix.fix_gradient_unexportable(svg)
            content = svg.read_text(encoding="utf-8")
            # 800×500 on 1920×1080 is small (0.6×1920=1152, 0.6×1080=648)
            # → must be protected from the solid-white rewrite.
            self.assertIn('fill="#FFFFFF"', content)
            # The gradient defs must still be stripped (independent logic).
            self.assertNotIn("<linearGradient", content)


# ---------------------------------------------------------------------------
# runner: SKILL_DIR resolution and export-receipt regex.
# ---------------------------------------------------------------------------

class RunnerTests(unittest.TestCase):
    def test_export_receipt_regex(self):
        line = (
            "Round-trip export summary: output_pages=11 "
            "passthrough=0 cloned_passthrough=0 patched=0 rebuilt=11"
        )
        m = runner._EXPORT_RECEIPT_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "11")
        self.assertEqual(m.group(5), "11")

    def test_resolve_skill_dir_arg_wins(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scripts = td / "scripts"
            scripts.mkdir()
            (scripts / "attribution_guard.py").write_text("# fake")
            resolved = runner.resolve_skill_dir(skill_dir=td)
            self.assertEqual(resolved, td.resolve())

    def test_resolve_skill_dir_env_var(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scripts = td / "scripts"
            scripts.mkdir()
            (scripts / "attribution_guard.py").write_text("# fake")
            with mock.patch.dict(os.environ, {"PPT_MASTER_SKILL_DIR": str(td)}):
                resolved = runner.resolve_skill_dir()
            self.assertEqual(resolved, td.resolve())

    def test_resolve_skill_dir_invalid_arg_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                runner.resolve_skill_dir(skill_dir=td)

    def test_resolve_skill_dir_cwd_probe(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scripts = td / "ppt-master" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "attribution_guard.py").write_text("# fake")
            old_cwd = Path.cwd()
            old_default_posix = runner._DEFAULT_POSIX_SKILL_DIR
            old_default_win = runner._DEFAULT_WINDOWS_SKILL_DIR
            try:
                os.chdir(td)
                # Point both default-platform candidates to a non-existent path
                # so the resolver must fall through to the cwd probe.
                runner._DEFAULT_POSIX_SKILL_DIR = td / "no-such-posix"
                runner._DEFAULT_WINDOWS_SKILL_DIR = td / "no-such-windows"
                with mock.patch.dict(
                    os.environ, {"PPT_MASTER_SKILL_DIR": ""}, clear=False
                ):
                    resolved = runner.resolve_skill_dir()
            finally:
                os.chdir(old_cwd)
                runner._DEFAULT_POSIX_SKILL_DIR = old_default_posix
                runner._DEFAULT_WINDOWS_SKILL_DIR = old_default_win
            self.assertEqual(resolved, (td / "ppt-master").resolve())

    def test_parse_quality_summary(self):
        stdout = (
            "Checking ...\n"
            "slide_01.svg: shape-3 exceeds owning frame ... overflow horizontal 27.6%\n"
            "Final: 1 errors, 4 warnings\n"
        )
        parsed = runner._parse_quality_summary(stdout, "")
        self.assertEqual(parsed["errors"], 1)
        self.assertEqual(parsed["warnings"], 4)
        self.assertEqual(parsed["overflow_count"], 1)


# ---------------------------------------------------------------------------
# pipeline: page_plan validation.
# ---------------------------------------------------------------------------

class PipelinePagePlanTests(unittest.TestCase):
    def _workspace(self, td: Path) -> Path:
        asvg = td / "authoring-svg-flat"
        asvg.mkdir(parents=True)
        (asvg / "slide_01.svg").write_text("<svg/>")
        (asvg / "slide_03.svg").write_text("<svg/>")
        return td

    def test_write_page_plan_ok(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._workspace(Path(td))
            path = pipeline.write_page_plan(
                td,
                [
                    {"source_slide": 1, "svg": "slide_01.svg"},
                    {"source_slide": 3, "svg": "slide_03.svg"},
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], pipeline.PAGE_PLAN_SCHEMA)
            self.assertEqual(len(payload["pages"]), 2)

    def test_write_page_plan_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._workspace(Path(td))
            with self.assertRaises(ValueError):
                pipeline.write_page_plan(
                    td,
                    [
                        {"source_slide": 1, "svg": "slide_01.svg"},
                        {"source_slide": 1, "svg": "slide_01.svg"},
                    ],
                )

    def test_write_page_plan_rejects_missing_svg(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._workspace(Path(td))
            with self.assertRaises(ValueError):
                pipeline.write_page_plan(
                    td,
                    [{"source_slide": 1, "svg": "slide_99.svg"}],
                )

    def test_write_page_plan_rejects_invalid_source_slide(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._workspace(Path(td))
            with self.assertRaises(ValueError):
                pipeline.write_page_plan(
                    td,
                    [{"source_slide": 0, "svg": "slide_01.svg"}],
                )


# ---------------------------------------------------------------------------
# server: stdio JSON-RPC dispatch.
# ---------------------------------------------------------------------------

class ServerTests(unittest.TestCase):
    def test_dispatch_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        resp = server._dispatch(req)
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "mcp-ppt-native-fill")

    def test_dispatch_tools_list(self):
        resp = server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["native_fill"])

    def test_dispatch_ping(self):
        resp = server._dispatch({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_dispatch_unknown_method(self):
        resp = server._dispatch({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_dispatch_unknown_tool(self):
        resp = server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "missing", "arguments": {}},
            }
        )
        self.assertIn("result", resp)
        self.assertTrue(resp["result"]["isError"])

    def test_dispatch_native_fill_unknown_skill_dir(self):
        resp = server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "native_fill",
                    "arguments": {
                        "source_pptx": "C:/x.pptx",
                        "workspace": "C:/w",
                        "output_pptx": "C:/o.pptx",
                        "content_mapping": {},
                        "options": {"skill_dir": "C:/no-such-path-xyz"},
                    },
                },
            }
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertIn("skill_dir resolution failed", payload["error"])

    def test_dispatch_notifications_initialized_returns_none(self):
        resp = server._dispatch(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertIsNone(resp)


# ---------------------------------------------------------------------------
# LLM client + planner tests (stdlib urllib is monkey-patched so no network).
# ---------------------------------------------------------------------------

class LLMClientTests(unittest.TestCase):
    def _fake_http(self, monkey, *, json_body: dict, http_error=None):
        """Install a fake urlopen that returns the given JSON or raises."""
        class _Resp:
            def __init__(self, body):
                self._body = body.encode("utf-8")

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, timeout=0):
            if http_error is not None:
                raise http_error
            return _Resp(json.dumps(json_body))

        monkey.setattr(llm_client.urllib.request, "urlopen", _urlopen)

    def test_anthropic_complete_parses_text_block_via_unittest_mock(self):
        from unittest.mock import patch
        from mcp_ppt_native_fill import llm_client
        import os

        env = {
            "MCP_LLM_PROVIDER": "anthropic",
            "MCP_LLM_API_KEY": "sk-test",
            "MCP_LLM_BASE_URL": "https://api.example.com",
            "MCP_LLM_MODEL": "claude-test",
        }

        fake_response = {
            "id": "msg_x",
            "content": [{"type": "text", "text": '{"hello": "world"}'}],
        }

        captured = {}
        def fake_post(url, body, headers, timeout_s):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = body
            return fake_response

        with patch.object(llm_client, "_http_post_json", side_effect=fake_post):
            cfg = llm_client.LLMConfig(
                provider="anthropic",
                api_key="sk-test",
                base_url="https://api.example.com",
                model="claude-test",
                max_tokens=1024,
                timeout_s=30,
            )
            out = llm_client.llm_complete_json(
                system="you are a planner",
                user="hello",
                config=cfg,
            )
        self.assertEqual(out, {"hello": "world"})
        self.assertEqual(captured["url"], "https://api.example.com/v1/messages")
        self.assertEqual(captured["headers"]["x-api-key"], "sk-test")
        self.assertEqual(captured["body"]["system"], "you are a planner")
        self.assertEqual(captured["body"]["messages"][0]["role"], "user")

    def test_openai_compat_complete_uses_json_response_format(self):
        from unittest.mock import patch
        from mcp_ppt_native_fill import llm_client

        fake_response = {
            "choices": [
                {"message": {"content": '{"k": 1}'}}
            ]
        }
        captured = {}
        def fake_post(url, body, headers, timeout_s):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = headers
            return fake_response

        with patch.object(llm_client, "_http_post_json", side_effect=fake_post):
            cfg = llm_client.LLMConfig(
                provider="openai_compatible",
                api_key="sk-test",
                base_url="https://api.openai.com",
                model="gpt-test",
                max_tokens=2048,
                timeout_s=30,
            )
            out = llm_client.llm_complete_json(system="x", user="y", config=cfg)
        self.assertEqual(out, {"k": 1})
        self.assertEqual(captured["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(
            captured["body"]["response_format"], {"type": "json_object"}
        )
        self.assertEqual(captured["headers"]["authorization"], "Bearer sk-test")

    def test_openai_compat_appends_v1_when_missing(self):
        from unittest.mock import patch
        from mcp_ppt_native_fill import llm_client

        captured = {}
        def fake_post(url, body, headers, timeout_s):
            captured["url"] = url
            return {"choices": [{"message": {"content": "{}"}}]}

        with patch.object(llm_client, "_http_post_json", side_effect=fake_post):
            cfg = llm_client.LLMConfig(
                provider="openai_compatible",
                api_key="k",
                base_url="http://localhost:11434",
                model="m",
                max_tokens=1024,
                timeout_s=30,
            )
            llm_client.llm_complete_json(system="s", user="u", config=cfg)
        # Ollama-style base gets /v1 appended, then /chat/completions
        self.assertEqual(captured["url"], "http://localhost:11434/v1/chat/completions")

    def test_strip_code_fence_removes_json_wrapper(self):
        from mcp_ppt_native_fill.llm_client import _strip_code_fence
        self.assertEqual(
            _strip_code_fence("```json\n{\"a\": 1}\n```"),
            '{"a": 1}',
        )
        self.assertEqual(
            _strip_code_fence('{"a": 1}'),
            '{"a": 1}',
        )

    def test_llm_complete_json_raises_on_invalid_json(self):
        from unittest.mock import patch
        from mcp_ppt_native_fill import llm_client
        from mcp_ppt_native_fill.llm_client import LLMError

        def fake_post(url, body, headers, timeout_s):
            return {"choices": [{"message": {"content": "not json"}}]}

        with patch.object(llm_client, "_http_post_json", side_effect=fake_post):
            cfg = llm_client.LLMConfig(
                provider="openai_compatible",
                api_key="k", base_url="https://x", model="m",
                max_tokens=10, timeout_s=10,
            )
            with self.assertRaises(LLMError) as cm:
                llm_client.llm_complete_json(system="s", user="u", config=cfg)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_config_from_env_validates_provider(self):
        from mcp_ppt_native_fill import llm_client
        import os
        old = os.environ.get("MCP_LLM_PROVIDER")
        os.environ["MCP_LLM_PROVIDER"] = "bogus"
        try:
            with self.assertRaises(ValueError):
                llm_client.LLMConfig.from_env()
        finally:
            if old is None:
                os.environ.pop("MCP_LLM_PROVIDER", None)
            else:
                os.environ["MCP_LLM_PROVIDER"] = old

    def test_config_requires_api_key(self):
        from mcp_ppt_native_fill import llm_client
        import os
        old_key = os.environ.get("MCP_LLM_API_KEY")
        old_path = os.environ.get("MCP_LLM_SETTINGS_JSON")
        os.environ.pop("MCP_LLM_API_KEY", None)
        # Point at a guaranteed-non-existent file so the settings.json
        # fallback cannot satisfy the requirement — this keeps the test
        # honest in environments where the real ~/.claude/settings.json
        # does contain an auth token.
        os.environ["MCP_LLM_SETTINGS_JSON"] = "/nonexistent/settings.json"
        try:
            with self.assertRaises(ValueError):
                llm_client.LLMConfig.from_env()
        finally:
            os.environ.pop("MCP_LLM_API_KEY", None)
            os.environ.pop("MCP_LLM_SETTINGS_JSON", None)
            if old_key is not None:
                os.environ["MCP_LLM_API_KEY"] = old_key
            if old_path is not None:
                os.environ["MCP_LLM_SETTINGS_JSON"] = old_path


class LLMPlannerTests(unittest.TestCase):
    def _make_workspace(self, td: Path):
        flat = td / "authoring-svg-flat"
        flat.mkdir(parents=True)
        # summary is sanity-checked but not consulted for shape IDs anymore
        # — the planner parses the SVGs themselves.
        (flat / "authoring_summary.json").write_text(
            json.dumps({"schema": "x", "documents": []}),
            encoding="utf-8",
        )
        # Minimal but valid SVG fixtures with shape IDs the LLM can target.
        (flat / "slide_01.svg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<g id="shape-23"><text x="10" y="20">old title</text></g>'
            '<g id="shape-24"><text x="10" y="40">old subtitle</text></g>'
            '<g id="shape-99"><text x="10" y="60">decor only</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        (flat / "slide_02.svg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<g id="shape-2"><text x="10" y="20"></text></g>'
            '</svg>',
            encoding="utf-8",
        )
        md = td / "content.md"
        md.write_text("# Title\n\nbody\n", encoding="utf-8")
        return td, flat, md

    def test_planner_drops_unknown_svg_and_empty_text(self):
        from mcp_ppt_native_fill import llm_planner
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            workspace, _, md = self._make_workspace(td)

            llm_response = {
                "slide_01.svg": {
                    "shape-23": "采购制度",
                    "shape-99": "drop me, only decoration",  # unknown id in index
                },
                "slide_99.svg": {  # unknown slide entirely
                    "shape-x": "y",
                },
                "slide_02.svg": {
                    "shape-2": "   ",  # empty after strip, drop
                },
            }

            with patch.object(llm_planner.llm_client, "llm_complete_json", return_value=llm_response):
                mapping = llm_planner.plan_content_mapping(
                    md_path=md, workspace=workspace
                )
        # slide_99.svg dropped (unknown), shape-2 empty text dropped,
        # shape-99 IS in the shape_index (the planner scans the SVG and
        # sees <g id="shape-99"><text>decor only</text></g>) so its edit
        # passes validation. shape-23 keeps its mapping.
        self.assertEqual(
            mapping,
            {
                "slide_01.svg": {
                    "shape-23": "采购制度",
                    "shape-99": "drop me, only decoration",
                },
            },
        )

    def test_planner_raises_on_missing_workspace(self):
        from mcp_ppt_native_fill import llm_planner
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "content.md").write_text("x", encoding="utf-8")
            with self.assertRaises(llm_planner.PlannerError):
                llm_planner.plan_content_mapping(
                    md_path=td / "content.md",
                    workspace=td,
                )

    def test_planner_raises_on_missing_md(self):
        from mcp_ppt_native_fill import llm_planner
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "authoring-svg-flat").mkdir()
            (td / "authoring-svg-flat" / "authoring_summary.json").write_text(
                "{}", encoding="utf-8"
            )
            with self.assertRaises(llm_planner.PlannerError):
                llm_planner.plan_content_mapping(
                    md_path=td / "nope.md",
                    workspace=td,
                )

    def test_scan_text_shapes_extracts_per_shape_placeholder(self):
        from mcp_ppt_native_fill.llm_planner import _scan_text_shapes
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws, _, _ = self._make_workspace(td)
            index = _scan_text_shapes(ws)
        # Each shape entry is now {placeholder, max_chars}; we only assert
        # on placeholders and max_chars ≥ 24 (the floor for empty slots).
        self.assertEqual(
            index["slide_01.svg"]["shape-23"]["placeholder"], "old title",
        )
        self.assertEqual(
            index["slide_01.svg"]["shape-24"]["placeholder"], "old subtitle",
        )
        self.assertEqual(
            index["slide_01.svg"]["shape-99"]["placeholder"], "decor only",
        )
        self.assertEqual(index["slide_02.svg"]["shape-2"]["placeholder"], "")
        for slide in index.values():
            for entry in slide.values():
                self.assertGreaterEqual(entry["max_chars"], 24)

    def test_normalize_truncates_overlong_text(self):
        from mcp_ppt_native_fill.llm_planner import _normalize_mapping, _scan_text_shapes
        from mcp_ppt_native_fill.llm_planner import PlannerError
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws, _, _ = self._make_workspace(td)
            idx = _scan_text_shapes(ws)
            # shape-23 placeholder is "old title" (9 chars) → max ~11
            max_23 = idx["slide_01.svg"]["shape-23"]["max_chars"]
            overlong = "x" * (max_23 + 50)
            cleaned = _normalize_mapping(
                {"slide_01.svg": {"shape-23": overlong}},
                idx,
            )
        # Overlong text was truncated to fit. The trailing strip removes
        # punctuation; we just check it's within budget.
        self.assertLessEqual(len(cleaned["slide_01.svg"]["shape-23"]), max_23)

    def test_toc_slots_get_tight_max_chars(self):
        """Bug 2 fix (Phase B): TOC slide shapes get a tight per-font-size
        cap (8 chars for ≥24pt, 6 chars for <24pt) so the LLM doesn't
        write long chapter headings and leave a large blank gap under
        each item."""
        from mcp_ppt_native_fill.llm_planner import _scan_text_shapes
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            (flat / "authoring_summary.json").write_text(
                json.dumps({"schema": "x", "documents": []}),
                encoding="utf-8",
            )
            # Cover (4 slots, generic), TOC (6 slots, tight), content (1).
            (flat / "slide_01.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-1"><text x="10" y="20" font-size="32">c1</text></g>'
                '<g id="shape-2"><text x="10" y="40" font-size="32">c2</text></g>'
                '<g id="shape-3"><text x="10" y="60" font-size="32">c3</text></g>'
                '<g id="shape-4"><text x="10" y="80" font-size="32">c4</text></g>'
                '</svg>', encoding="utf-8")
            # TOC: 3 big-font (32pt) + 3 small-font (16pt). The heuristic
            # would give ~28-29 chars each; the Bug 2 fix must clamp
            # them to 8 / 6.
            (flat / "slide_02.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-10"><text x="10" y="20" font-size="32">t1</text></g>'
                '<g id="shape-11"><text x="10" y="40" font-size="32">t2</text></g>'
                '<g id="shape-12"><text x="10" y="60" font-size="32">t3</text></g>'
                '<g id="shape-13"><text x="10" y="80" font-size="16">d1</text></g>'
                '<g id="shape-14"><text x="10" y="100" font-size="16">d2</text></g>'
                '<g id="shape-15"><text x="10" y="120" font-size="16">d3</text></g>'
                '</svg>', encoding="utf-8")
            # Content slide.
            (flat / "slide_03.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-20"><text x="10" y="20" font-size="16">body</text></g>'
                '</svg>', encoding="utf-8")
            idx = _scan_text_shapes(td)
            # Cover keeps the generic 24-floor heuristic.
            self.assertGreaterEqual(
                idx["slide_01.svg"]["shape-1"]["max_chars"], 24)
            # TOC big-font slots capped at 8.
            for sid in ("shape-10", "shape-11", "shape-12"):
                self.assertLessEqual(
                    idx["slide_02.svg"][sid]["max_chars"], 8,
                    f"{sid} (TOC 32pt) must cap at 8 chars")
            # TOC small-font slots capped at 6.
            for sid in ("shape-13", "shape-14", "shape-15"):
                self.assertLessEqual(
                    idx["slide_02.svg"][sid]["max_chars"], 6,
                    f"{sid} (TOC 16pt) must cap at 6 chars")
            # Non-TOC slides keep the heuristic (no data-pptx-frame
            # in this fixture → fallback to placeholder heuristic).
            self.assertGreaterEqual(
                idx["slide_03.svg"]["shape-20"]["max_chars"], 24)

    def test_toc_slots_get_tight_max_chars_post_phase_c1(self):
        """Phase C1 regression: TOC cap is still tight after switching
        to ppt-master's per-character estimator. The 8/6 chars cap
        survives because it is set explicitly on the toc branch and
        min(heuristic, cap) preserves it.
        """
        from mcp_ppt_native_fill.llm_planner import _scan_text_shapes
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            (flat / "authoring_summary.json").write_text(
                json.dumps({"schema": "x", "documents": []}),
                encoding="utf-8",
            )
            # Cover slide so _detect_skeleton_kind doesn't classify
            # slide_02 as ending. Cover needs 4-8 text shapes per
            # _SKELETON_RULES (text_element_range (4, 8)).
            (flat / "slide_01.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-1"><text x="10" y="20" font-size="32">c1</text></g>'
                '<g id="shape-2"><text x="10" y="40" font-size="32">c2</text></g>'
                '<g id="shape-3"><text x="10" y="60" font-size="32">c3</text></g>'
                '<g id="shape-4"><text x="10" y="80" font-size="32">c4</text></g>'
                '<g id="shape-5"><text x="10" y="100" font-size="32">c5</text></g>'
                '</svg>', encoding="utf-8",
            )
            # TOC slide: 6 slots with explicit data-pptx-frame so the
            # new geometry-based estimator also runs. Cap should still
            # be 8 / 6 because min(heuristic, cap) wins.
            (flat / "slide_02.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-10" data-pptx-frame="120 200 400 40">'
                '<text x="130" y="220" font-size="32">title</text></g>'
                '<g id="shape-11" data-pptx-frame="120 280 400 40">'
                '<text x="130" y="300" font-size="32">title</text></g>'
                '<g id="shape-13" data-pptx-frame="120 360 400 30">'
                '<text x="130" y="380" font-size="16">sub</text></g>'
                '<g id="shape-14" data-pptx-frame="120 410 400 30">'
                '<text x="130" y="430" font-size="16">sub</text></g>'
                '</svg>', encoding="utf-8",
            )
            # Ending slide so _detect_skeleton_kind doesn't classify
            # slide_02 as the last slide. Ending needs < 8 text shapes
            # per _SKELETON_RULES.
            (flat / "slide_03.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-20"><text x="10" y="20" font-size="48">'
                'THANK YOU</text></g>'
                '</svg>', encoding="utf-8",
            )
            idx = _scan_text_shapes(td)
            # 8 / 6 caps still win over geometry estimate.
            for sid in ("shape-10", "shape-11"):
                self.assertLessEqual(
                    idx["slide_02.svg"][sid]["max_chars"], 8,
                    f"{sid} TOC 32pt cap must stay at 8 chars "
                    f"(got {idx['slide_02.svg'][sid]['max_chars']})")
            for sid in ("shape-13", "shape-14"):
                self.assertLessEqual(
                    idx["slide_02.svg"][sid]["max_chars"], 6,
                    f"{sid} TOC 16pt cap must stay at 6 chars "
                    f"(got {idx['slide_02.svg'][sid]['max_chars']})")

    def test_geometry_based_max_chars_uses_frame_and_font_size(self):
        """Phase B+ fix: non-TOC shapes use frame geometry + font-size
        to compute max_chars instead of the placeholder-length heuristic.
        The boteng cover title (47pt in 681px frame) fits ~14 chars;
        the heuristic would have given 28 and let the LLM overflow."""
        from mcp_ppt_native_fill.llm_planner import _scan_text_shapes
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            (flat / "authoring_summary.json").write_text(
                json.dumps({"schema": "x", "documents": []}),
                encoding="utf-8",
            )
            # Boteng-style cover: shape-23 has data-pptx-frame and a
            # large font-size (47pt). Without the geometry fix the
            # heuristic gives max_chars = 28; with the fix, it should
            # be ~12 (681 / 47 * 0.85 = 12.3).
            (flat / "slide_01.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-23" data-pptx-frame="53.2 163.6 680.93 197">'
                '<text x="62.8" y="234.13" font-size="47.49">'
                '山西柏腾科技有限公司采购制度'
                '</text></g>'
                '</svg>', encoding="utf-8")
            # Content title with smaller font: 23pt in 427px frame
            # fits ~15 chars (427 / 23 * 0.85 = 15.8). The LLM text
            # "一、目的 · 二、适用范围 · 三、基本原则" (16 chars) overflows
            # by 6.5% per the quality checker — the cap of 15 forces
            # it to drop one segment.
            (flat / "slide_04.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-17" data-pptx-frame="104.13 25 426.67 56.93">'
                '<text x="113.73" y="61.53" font-size="22.92">'
                '一、目的 · 二、适用范围 · 三、基本原则'
                '</text></g>'
                '</svg>', encoding="utf-8")
            idx = _scan_text_shapes(td)
            # Cover title: ppt-master estimator + binary search of CJK +
            # latin samples caps at ~13 chars. Previously 0.85 heuristic
            # gave 12 (under-counting).
            cover_cap = idx["slide_01.svg"]["shape-23"]["max_chars"]
            self.assertLessEqual(
                cover_cap, 14,
                f"cover title (47pt in 681px) must cap tight, got {cover_cap}")
            # Content title: ppt-master estimator caps at ~17 chars
            # (mixed CJK + spaces + middle dots).
            content_cap = idx["slide_04.svg"]["shape-17"]["max_chars"]
            self.assertLessEqual(
                content_cap, 18,
                f"content title (23pt in 427px) must cap tight, got {content_cap}")
            # Both must be tighter than the placeholder heuristic would
            # have given (heuristic = max(placeholder_len, 24) * 1.2 = 28
            # for these shapes).
            self.assertLess(cover_cap, 28)
            self.assertLess(content_cap, 28)

    def test_geometry_max_chars_falls_back_to_heuristic(self):
        """Phase B+ fix: when ``data-pptx-frame`` is missing on a shape,
        fall back to the placeholder-length heuristic (existing
        behaviour)."""
        from mcp_ppt_native_fill.llm_planner import _scan_text_shapes
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            (flat / "authoring_summary.json").write_text(
                json.dumps({"schema": "x", "documents": []}),
                encoding="utf-8",
            )
            # No data-pptx-frame attribute → heuristic fallback.
            (flat / "slide_01.svg").write_text(
                '<?xml version="1.0"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-23">'
                '<text x="10" y="20" font-size="48">山西柏腾</text>'
                '</g></svg>', encoding="utf-8")
            idx = _scan_text_shapes(td)
            cap = idx["slide_01.svg"]["shape-23"]["max_chars"]
            # Heuristic: placeholder_len=4, base=max(4,24)=24, * 1.2 = 28.
            self.assertEqual(cap, 28)


class ServerLLMInputsTests(unittest.TestCase):
    """Verify the tool descriptor and dispatcher accept the new LLM inputs."""

    def test_tool_descriptor_includes_content_markdown(self):
        self.assertIn("content_markdown", server.TOOL_NATIVE_FILL["inputSchema"]["properties"])
        self.assertIn("llm_plan", server.TOOL_NATIVE_FILL["inputSchema"]["properties"]["options"]["properties"])

    def test_execute_native_fill_rejects_llm_without_md(self):
        arguments = {
            "source_pptx": "C:/nope.pptx",
            "workspace": "C:/nope_ws",
            "output_pptx": "C:/nope_out.pptx",
            "content_mapping": {},
            "options": {"llm_plan": True},  # no content_markdown
        }
        result = server._execute_native_fill(arguments)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "init")
        self.assertIn("requires content_markdown", result["error"])


class PipelineLLMPhaseTests(unittest.TestCase):
    """Test the optional LLM phase integrated into run_native_fill."""

    def test_phase2_5_merges_caller_and_llm_mapping_caller_wins(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = pipeline.PipelineState(workspace=td)
            state.workspace = td

            caller_mapping = {
                "slide_01.svg": {"shape-23": "caller title"},  # conflict
            }
            llm_mapping = PlannerResult(content_mapping={
                "slide_01.svg": {
                    "shape-23": "llm title",
                    "shape-24": "llm subtitle",  # new entry
                },
            })
            md = td / "content.md"
            md.write_text("x", encoding="utf-8")

            with patch(
                "mcp_ppt_native_fill.llm_planner.plan_content_mapping",
                return_value=llm_mapping,
            ):
                state = pipeline.phase2_5_llm_plan(state, md, caller_mapping)

            self.assertNotEqual(state.stage, "failed")
            self.assertEqual(
                state.context["content_mapping"],
                {
                    "slide_01.svg": {
                        "shape-23": "caller title",   # caller wins
                        "shape-24": "llm subtitle",   # LLM-only
                    },
                },
            )
            # Phase-A: full PlannerResult must also be stashed in state for
            # phase2_6 to materialize page_plan_additions + new_blocks.
            self.assertIs(state.context["planner_result"], llm_mapping)

    def test_phase2_5_records_planner_error(self):
        from mcp_ppt_native_fill import pipeline, llm_planner
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = pipeline.PipelineState(workspace=td)
            state.workspace = td
            md = td / "content.md"
            md.write_text("x", encoding="utf-8")

            with patch(
                "mcp_ppt_native_fill.llm_planner.plan_content_mapping",
                side_effect=llm_planner.PlannerError("network down"),
            ):
                state = pipeline.phase2_5_llm_plan(state, md, {})
            self.assertEqual(state.stage, "failed")
            self.assertIn("network down", state.errors[0])

    def test_phase2_5_records_llm_client_error(self):
        from mcp_ppt_native_fill import pipeline, llm_client
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = pipeline.PipelineState(workspace=td)
            state.workspace = td
            md = td / "content.md"
            md.write_text("x", encoding="utf-8")

            with patch(
                "mcp_ppt_native_fill.llm_planner.plan_content_mapping",
                side_effect=llm_client.LLMError("HTTP 401"),
            ):
                state = pipeline.phase2_5_llm_plan(state, md, {})
            self.assertEqual(state.stage, "failed")
            self.assertIn("HTTP 401", state.errors[0])

    def test_phase2_5_handles_empty_llm_response(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = pipeline.PipelineState(workspace=td)
            state.workspace = td
            md = td / "content.md"
            md.write_text("x", encoding="utf-8")
            caller = {"slide_01.svg": {"shape-23": "manual"}}

            with patch(
                "mcp_ppt_native_fill.llm_planner.plan_content_mapping",
                return_value=PlannerResult(),
            ):
                state = pipeline.phase2_5_llm_plan(state, md, caller)
            self.assertNotEqual(state.stage, "failed")
            self.assertEqual(
                state.context["content_mapping"], caller
            )
            self.assertTrue(
                any("empty mapping" in w for w in state.warnings)
            )


class SkeletonDetectionTests(unittest.TestCase):
    """Verify _detect_skeleton_kind classifies each slide by its structure."""

    def _make_workspace(self, td: Path):
        flat = td / "authoring-svg-flat"
        flat.mkdir(parents=True)
        # 5 slides matching the boteng template shape:
        # slide_01 cover (mixed font sizes, few text elements)
        # slide_02 toc (most text elements)
        # slide_03 divider (one big 80pt title)
        # slide_04 content (small font body)
        # slide_05 ending (THANK YOU etc.)
        fixtures = {
            "slide_01.svg": [
                ("shape-23", 56, "山西柏腾"),  # title
                ("shape-24", 37, "采购制度"),  # subtitle
                ("shape-30", 21, "工业设备智能化服务商"),
                ("shape-25", 21, "高效协同"),
                ("shape-8",  27, "制定人"),
                ("shape-9",  21, "2026.XX.XX"),
            ],
            "slide_02.svg": [
                (f"shape-{60 + i}", 32 if i % 2 == 0 else 16,
                 f"项目 {i}")
                for i in range(13)
            ],
            "slide_03.svg": [
                ("shape-4", 58, "PART 01"),
                ("shape-5", 80, "前言"),
                ("shape-70", 21, "公司规章制度是保障公司运营的工具"),
                ("shape-25", 18, "高效协同"),
            ],
            "slide_04.svg": [
                ("shape-17", 37, "采购制度内容"),
                ("shape-22", 16, "高效协同 开放坦诚"),
            ],
            "slide_05.svg": [
                ("shape-7",  106, "THANK  YOU"),
                ("shape-9",  58, "感谢您的聆听"),
                ("shape-11", 23, "山西柏腾"),
                ("shape-15", 23, "山西柏腾"),
                ("shape-16", 32, "采购部"),
                ("shape-25", 18, "高效协同"),
            ],
        }
        for name, shapes in fixtures.items():
            body = "".join(
                f'<g id="{sid}"><text x="10" y="20" '
                f'font-size="{fs}">{text}</text></g>'
                for sid, fs, text in shapes
            )
            (flat / name).write_text(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720">'
                f'{body}</svg>',
                encoding="utf-8",
            )
        (flat / "authoring_summary.json").write_text(
            json.dumps({"schema": "x", "documents": []}),
            encoding="utf-8",
        )
        return td

    def test_skeleton_detection_boteng_shape(self):
        from mcp_ppt_native_fill.llm_planner import _detect_skeleton_kind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            r = _detect_skeleton_kind(ws)
        # First slide → cover, last → ending, max-text → toc, big-font → divider, rest → content.
        self.assertEqual(r["skeleton_kind"]["slide_01.svg"], "cover")
        self.assertEqual(r["skeleton_kind"]["slide_02.svg"], "toc")
        self.assertEqual(r["skeleton_kind"]["slide_03.svg"], "divider")
        self.assertEqual(r["skeleton_kind"]["slide_04.svg"], "content")
        self.assertEqual(r["skeleton_kind"]["slide_05.svg"], "ending")
        self.assertEqual(r["divider_id"], 3)
        self.assertEqual(r["content_id"], 4)

    def test_skeleton_detect_38pt_divider_via_keyword(self):
        """Bug 02 (part 2): a slide whose title is only 38pt but its body
        contains a Chinese "第N章"-style marker must still classify as
        divider. The previous ``fs >= 50`` hardcoded threshold wrongly
        missed 38pt divider titles (common in templates that economize on
        type size for non-cover pages).

        The fix is to use semantic signals (chapter marker / "第N章" /
        "PART N") in addition to the font-size heuristic.
        """
        from mcp_ppt_native_fill.llm_planner import _detect_skeleton_kind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            (flat / "authoring_summary.json").write_text(
                '{"schema": "x", "documents": []}', encoding="utf-8"
            )
            # 5 slides: cover / toc / 38pt-divider / content / ending
            fixtures = {
                "slide_01.svg": [("shape-1", 56, "山西柏腾")],
                "slide_02.svg": [("shape-1", 24, "目录")],
                # 38pt divider with explicit "第N章" marker in body
                "slide_03.svg": [
                    ("shape-1", 38, "第二章 招标范围"),
                    ("shape-2", 24, "本章要点"),
                ],
                "slide_04.svg": [("shape-1", 20, "正文内容")],
                "slide_05.svg": [
                    ("shape-1", 60, "THANK YOU"),
                    ("shape-2", 30, "感谢您的聆听"),
                ],
            }
            for name, shapes in fixtures.items():
                body = "".join(
                    f'<g id="{sid}"><text x="10" y="20" '
                    f'font-size="{fs}">{text}</text></g>'
                    for sid, fs, text in shapes
                )
                (flat / name).write_text(
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'viewBox="0 0 1280 720">'
                    f'{body}</svg>',
                    encoding="utf-8",
                )
            r = _detect_skeleton_kind(td)
        # 38pt + "第N章" keyword must classify as divider (was failing).
        self.assertEqual(r["skeleton_kind"]["slide_03.svg"], "divider")
        self.assertEqual(r["divider_id"], 3)


class PlannerResultTests(unittest.TestCase):
    """Phase-A planner response parsing + dict-like shim."""

    def _shape_index(self):
        return {
            "slide_01.svg": {
                "shape-23": {"placeholder": "title", "max_chars": 60},
                "shape-24": {"placeholder": "subtitle", "max_chars": 60},
            },
            "slide_03.svg": {
                "shape-4": {"placeholder": "PART NN", "max_chars": 20},
                "shape-5": {"placeholder": "前言", "max_chars": 30},
            },
        }

    def test_parse_legacy_dict_promotes_to_planner_result(self):
        from mcp_ppt_native_fill.llm_planner import _parse_planner_response
        raw = {"slide_01.svg": {"shape-23": "新标题"}}
        result = _parse_planner_response(raw, self._shape_index())
        self.assertEqual(
            result.content_mapping, {"slide_01.svg": {"shape-23": "新标题"}}
        )
        # Dict-like shim lets the result compare equal to a plain dict.
        self.assertEqual(result, {"slide_01.svg": {"shape-23": "新标题"}})
        self.assertEqual(result["slide_01.svg"]["shape-23"], "新标题")
        self.assertTrue(bool(result))
        self.assertEqual(len(result), 1)

    def test_parse_phase_a_four_key_shape(self):
        from mcp_ppt_native_fill.llm_planner import _parse_planner_response
        raw = {
            "content_mapping": {"slide_01.svg": {"shape-23": "A"}},
            "page_plan_additions": [
                {"source_slide": 3, "svg": "slide_part02_div.svg",
                 "edits": {"shape-4": "PART 02", "shape-5": "范围"}},
                {"source_slide": 4, "svg": "slide_part02_content.svg",
                 "edits": {"shape-17": "二、范围"}},
            ],
            "new_blocks": [
                {"svg": "slide_part02_content.svg",
                 "id": "content-body",
                 "bounds": "120 130 1060 480",
                 "layout": "3-column-cards",
                 "spec": {"cards": [
                     {"title": "基础", "color": "#1D2CAB",
                      "items": ["a", "b"]},
                     {"title": "销售", "color": "#EE822F",
                      "items": ["c", "d"]},
                     {"title": "采购", "color": "#75BD42",
                      "items": ["e", "f"]},
                 ]}},
            ],
            "skeleton_kind": {"slide_01.svg": "cover"},
        }
        result = _parse_planner_response(raw, self._shape_index())
        self.assertEqual(len(result.page_plan_additions), 2)
        self.assertEqual(len(result.new_blocks), 1)
        self.assertEqual(result.skeleton_kind["slide_01.svg"], "cover")
        # content_mapping still validated against shape_index.
        self.assertEqual(result["slide_01.svg"]["shape-23"], "A")

    def test_parse_drops_unsupported_layout(self):
        from mcp_ppt_native_fill.llm_planner import _parse_planner_response
        raw = {"new_blocks": [
            {"svg": "slide_x.svg", "id": "b", "bounds": "0 0 100 100",
             "layout": "totally-bogus", "spec": {}},
        ]}
        result = _parse_planner_response(raw, self._shape_index())
        self.assertEqual(result.new_blocks, [])

    def test_parse_drops_oversized_3_column_cards(self):
        from mcp_ppt_native_fill.llm_planner import _parse_planner_response
        raw = {"new_blocks": [
            {"svg": "slide_x.svg", "id": "b", "bounds": "0 0 100 100",
             "layout": "3-column-cards",
             "spec": {"cards": [
                 {"title": f"c{i}"} for i in range(6)
             ]}},
        ]}
        result = _parse_planner_response(raw, self._shape_index())
        self.assertEqual(result.new_blocks, [])

    def test_parse_drops_duplicate_svg_in_page_plan(self):
        from mcp_ppt_native_fill.llm_planner import _parse_planner_response
        raw = {"page_plan_additions": [
            {"source_slide": 3, "svg": "slide_x.svg", "edits": {}},
            {"source_slide": 3, "svg": "slide_x.svg", "edits": {}},
        ]}
        result = _parse_planner_response(raw, self._shape_index())
        self.assertEqual(len(result.page_plan_additions), 1)


class PipelinePhase26Tests(unittest.TestCase):
    """Phase-2.6 materialize LLM planner output (Phase A expansion)."""

    def _make_workspace(self, td: Path):
        flat = td / "authoring-svg-flat"
        flat.mkdir(parents=True)
        # 5 minimal slides — only slide_01 / slide_03 / slide_04 used here.
        for n in (1, 2, 3, 4, 5):
            (flat / f"slide_{n:02d}.svg").write_text(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720">'
                f'<g id="shape-{n}"><text x="10" y="20">orig-{n}</text></g>'
                '</svg>',
                encoding="utf-8",
            )
        return td

    def test_phase2_6_clones_skeleton_and_applies_edits(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state.context["planner_result"] = PlannerResult(
                content_mapping={},
                page_plan_additions=[
                    {"source_slide": 3,
                     "svg": "slide_part02_div.svg",
                     "edits": {"shape-3": "PART 02"}},
                ],
                new_blocks=[],
                skeleton_kind={"slide_03.svg": "divider",
                               "slide_part02_div.svg": "divider"},
            )
            state = pipeline.phase2_6_realize_planner_output(state)
            self.assertNotEqual(state.stage, "failed")
            clone = ws / "authoring-svg-flat" / "slide_part02_div.svg"
            self.assertTrue(clone.is_file(),
                            "phase2.6 must clone the skeleton SVG")
            # Edit applied.
            text = clone.read_text(encoding="utf-8")
            self.assertIn("PART 02", text)
            # page_plan_pages: originals (slide_01..05) seeded first,
            # then the planner's page_plan_additions appended.
            pages = state.context["page_plan_pages"]
            seeded_svgs = [p["svg"] for p in pages
                           if p["svg"].startswith("slide_") and
                           not p["svg"].startswith("slide_part")]
            self.assertEqual(
                seeded_svgs,
                ["slide_01.svg", "slide_02.svg",
                 "slide_03.svg", "slide_04.svg", "slide_05.svg"],
            )
            self.assertIn(
                {"source_slide": 3, "svg": "slide_part02_div.svg"},
                pages,
            )
            # content_mapping picks up the new page's edits.
            self.assertEqual(
                state.context["content_mapping"]["slide_part02_div.svg"],
                {"shape-3": "PART 02"},
            )

    def test_phase2_6_warns_on_missing_skeleton(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state.context["planner_result"] = PlannerResult(
                page_plan_additions=[
                    {"source_slide": 99,
                     "svg": "slide_nope.svg",
                     "edits": {}},
                ],
            )
            state = pipeline.phase2_6_realize_planner_output(state)
            self.assertTrue(
                any("missing" in w for w in state.warnings),
                "missing skeleton must surface as a warning",
            )
            # Originals still seeded (5 slides) even though the clone
            # was rejected.
            seeded_svgs = [p["svg"] for p in state.context["page_plan_pages"]]
            self.assertEqual(seeded_svgs,
                             ["slide_01.svg", "slide_02.svg",
                              "slide_03.svg", "slide_04.svg",
                              "slide_05.svg"])

    def test_phase2_6_emits_new_blocks_into_state(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state.context["planner_result"] = PlannerResult(
                new_blocks=[
                    {"svg": "slide_part02_content.svg",
                     "id": "content-body",
                     "bounds": "120 130 1060 480",
                     "layout": "3-column-cards",
                     "spec": {"cards": [
                         {"title": "A", "items": ["a"]},
                         {"title": "B", "items": ["b"]},
                         {"title": "C", "items": ["c"]},
                     ]}},
                ],
            )
            state = pipeline.phase2_6_realize_planner_output(state)
            blocks = state.context["new_content_blocks"]
            self.assertIn("slide_part02_content.svg", blocks)
            self.assertEqual(
                blocks["slide_part02_content.svg"]["content-body"]["layout"],
                "3-column-cards",
            )

    def test_phase2_6_noop_when_planner_not_run(self):
        from mcp_ppt_native_fill import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state = pipeline.phase2_6_realize_planner_output(
                state,
                caller_page_plan=[{"source_slide": 1}],
                caller_new_blocks={"slide_01.svg": {"x": {"bounds": "0 0 1 1"}}},
            )
            # Caller-supplied page_plan + blocks pass through unchanged.
            self.assertEqual(state.context["page_plan_pages"],
                             [{"source_slide": 1}])
            self.assertIn("slide_01.svg", state.context["new_content_blocks"])

    def test_phase2_6_seeds_originals_when_no_caller_page_plan(self):
        """Bug #1 fix: when caller passes no page_plan AND planner
        returned page_plan_additions, originals (cover/toc/divider/
        ending) must be seeded into page_plan_pages. Otherwise they
        vanish from the export and the user sees 'no cover'.

        The ending skeleton (slide_05.svg) must be the LAST page in
        the deck — not at slide 5 of N — so the THANK YOU page no
        longer appears in the middle (user feedback 2026-09-13)."""
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state.context["planner_result"] = PlannerResult(
                content_mapping={},
                page_plan_additions=[
                    {"source_slide": 3,
                     "svg": "slide_part02_div.svg",
                     "edits": {"shape-3": "PART 02"}},
                    {"source_slide": 4,
                     "svg": "slide_part02_content.svg",
                     "edits": {"shape-4": "body"}},
                ],
                new_blocks=[],
                skeleton_kind={"slide_01.svg": "cover",
                                "slide_02.svg": "toc",
                                "slide_03.svg": "divider",
                                "slide_04.svg": "content",
                                "slide_05.svg": "ending"},
            )
            state = pipeline.phase2_6_realize_planner_output(state)
            pages = state.context["page_plan_pages"]
            svgs = [p["svg"] for p in pages]
            # Bug 1: ending slide is the LAST page of the deck.
            self.assertEqual(svgs[-1], "slide_05.svg",
                             f"ending slide must be last; got order {svgs}")
            # Cover/toc/divider/content originals stay in source order.
            self.assertEqual(svgs[:4],
                             ["slide_01.svg", "slide_02.svg",
                              "slide_03.svg", "slide_04.svg"])
            # Cloned PART_* sit between content and ending.
            self.assertIn("slide_part02_div.svg", svgs)
            self.assertIn("slide_part02_content.svg", svgs)
            self.assertLess(svgs.index("slide_part02_div.svg"),
                            svgs.index("slide_05.svg"))
            # Originals keep their source_slide.
            self.assertEqual(pages[0], {"source_slide": 1,
                                        "svg": "slide_01.svg"})

    def test_phase2_6_no_planner_no_caller_seeds_only_originals(self):
        """Path A: no planner ran, no caller page_plan. Originals
        alone (5 slides) are exported — no cloned PART_*."""
        from mcp_ppt_native_fill import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state = pipeline.phase2_6_realize_planner_output(state)
            svgs = [p["svg"] for p in state.context["page_plan_pages"]]
            self.assertEqual(svgs,
                             ["slide_01.svg", "slide_02.svg",
                              "slide_03.svg", "slide_04.svg",
                              "slide_05.svg"])

    def test_ending_slide_lands_last_after_clones(self):
        """Bug 1 fix (Phase B): the ending skeleton (e.g. THANK YOU)
        must be the LAST page even when the planner adds cloned
        PART_* content pages in between. Without this, the closing
        slide appears in slide 5 / 13 of a 13-page deck because the
        source-order sort places slide_NN.svg by NN — and the LLM's
        clones take source_slide 3/4, jumping over slide_05."""
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.llm_planner import PlannerResult
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state.context["planner_result"] = PlannerResult(
                content_mapping={},
                page_plan_additions=[
                    {"source_slide": 3,
                     "svg": "slide_part02_div.svg",
                     "edits": {"shape-3": "PART 02"}},
                    {"source_slide": 4,
                     "svg": "slide_part02_content.svg",
                     "edits": {"shape-4": "body"}},
                ],
                new_blocks=[],
                # Planner correctly labels slide_05.svg as "ending".
                skeleton_kind={"slide_01.svg": "cover",
                                "slide_02.svg": "toc",
                                "slide_03.svg": "divider",
                                "slide_04.svg": "content",
                                "slide_05.svg": "ending"},
            )
            state = pipeline.phase2_6_realize_planner_output(state)
            pages = state.context["page_plan_pages"]
            svgs = [p["svg"] for p in pages]
            # The ending slide must be the LAST page.
            self.assertEqual(svgs[-1], "slide_05.svg",
                             f"ending slide must be last; got order {svgs}")
            # The cloned PART_* content lives BEFORE the ending slide.
            self.assertLess(svgs.index("slide_part02_content.svg"),
                            svgs.index("slide_05.svg"))
            self.assertLess(svgs.index("slide_part02_div.svg"),
                            svgs.index("slide_05.svg"))

    def test_seed_original_roster_moves_ending_last(self):
        """Bug 1 fix: _seed_original_roster(ending_last=True) moves the
        ending skeleton to the tail. Falls back to highest source_slide
        when skeleton_kind has no "ending" entry."""
        from mcp_ppt_native_fill import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            flat = ws / "authoring-svg-flat"
            # Without skeleton_kind: highest source_slide (=5) wins.
            roster = pipeline._seed_original_roster(flat, ending_last=True)
            svgs = [p["svg"] for p in roster]
            self.assertEqual(svgs[-1], "slide_05.svg")
            self.assertEqual(svgs[:-1],
                             ["slide_01.svg", "slide_02.svg",
                              "slide_03.svg", "slide_04.svg"])
            # With skeleton_kind pointing at a non-last slide as ending:
            # planner-authoritative.
            roster2 = pipeline._seed_original_roster(
                flat,
                skeleton_kind={"slide_03.svg": "ending"},
                ending_last=True,
            )
            svgs2 = [p["svg"] for p in roster2]
            self.assertEqual(svgs2[-1], "slide_03.svg")
            self.assertNotIn("slide_03.svg", svgs2[:-1])

    def test_phase4_quality_has_no_dead_code(self):
        """Bug 09: phase4_quality contained a dead-code block that
        referenced ``hasattr(state, 'page_plan_pages')`` — but
        PipelineState never defined that attribute (the data lives in
        ``state.context['page_plan_pages']``), so the block was unreachable
        and ``pass`` did nothing.

        Asserting on inspect.getsource() is a lightweight way to prevent
        the dead code from sneaking back in via a future refactor.
        """
        import inspect
        from mcp_ppt_native_fill import pipeline
        src = inspect.getsource(pipeline.phase4_quality)
        self.assertNotIn('hasattr(state, "page_plan_pages")', src)
        # The confused ternary ``if X if hasattr(...) else None`` pattern
        # must also be gone.
        self.assertNotIn("if hasattr(", src)

    def test_seed_original_roster_fallback_finds_thank_keyword(self):
        """Bug 08: when skeleton_kind is None and the last slide is NOT
        the ending (e.g. an appendix is appended at slide_10), the
        fallback must find the actual ending by scanning SVG bodies for
        THANK YOU / 谢谢 / Q&A keywords — not blindly pick the
        highest source_slide."""
        from mcp_ppt_native_fill import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            # 5 originals + 1 appendix. Without skeleton_kind and without
            # skeleton detection, the naive fallback picks slide_10 (highest
            # source_slide), but slide_05 has "THANK YOU" → must be picked.
            for n in range(1, 6):
                (flat / f"slide_{n:02d}.svg").write_text(
                    f"<svg><text>item {n}</text></svg>", encoding="utf-8"
                )
            (flat / "slide_05.svg").write_text(
                "<svg><text>THANK YOU</text></svg>", encoding="utf-8"
            )
            # An appendix added later (e.g. slide_10) — this is the highest
            # source_slide and would win under the old buggy fallback.
            (flat / "slide_10.svg").write_text(
                "<svg><text>附录：参考文献</text></svg>", encoding="utf-8"
            )
            roster = pipeline._seed_original_roster(flat, ending_last=True)
            svgs = [p["svg"] for p in roster]
            # slide_05 (THANK YOU) must be the ending, NOT slide_10.
            self.assertEqual(svgs[-1], "slide_05.svg")
            self.assertNotEqual(svgs[-1], "slide_10.svg")


class ContentBlockFallbackTests(unittest.TestCase):
    """Bug #2 fix: cloned content pages that the LLM forgot to fill
    must get a synthesized 3-column-cards body block, otherwise the
    page renders as a giant blank rectangle."""

    def _make_md(self, td: Path) -> Path:
        md = td / "doc.md"
        md.write_text(
            "# 一、目的\n\n"
            "为了规范公司采购行为, 降低采购成本, 提高采购质量。\n\n"
            "1. 采购原则\n2. 采购范围\n3. 采购职责\n\n"
            "# 二、适用范围\n\n"
            "适用于公司所有采购活动。\n\n"
            "# 三、基本原则\n\n"
            "公开透明、 公平竞争、 择优选择。\n",
            encoding="utf-8",
        )
        return md

    def test_fallback_synthesizes_three_cards_from_markdown(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.pipeline import PipelineState
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            md = self._make_md(td)
            state = PipelineState(workspace=td)
            state.context["content_markdown"] = md
            final_blocks: dict = {}
            pipeline._fill_missing_content_blocks(
                cloned_svgs=["slide_part02_content.svg",
                             "slide_part03_content.svg"],
                final_new_blocks=final_blocks,
                state=state,
            )
            # Both cloned content SVGs now have a content-body block.
            self.assertIn("slide_part02_content.svg", final_blocks)
            self.assertIn("slide_part03_content.svg", final_blocks)
            for svg in ("slide_part02_content.svg",
                        "slide_part03_content.svg"):
                block = final_blocks[svg]["content-body"]
                self.assertEqual(block["layout"], "3-column-cards")
                self.assertEqual(block["bounds"], "120 130 1060 480")
                # 1-3 cards per spec.
                self.assertGreaterEqual(len(block["spec"]["cards"]), 1)
                self.assertLessEqual(len(block["spec"]["cards"]), 3)

    def test_fallback_skips_svg_that_already_has_blocks(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.pipeline import PipelineState
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            md = self._make_md(td)
            state = PipelineState(workspace=td)
            state.context["content_markdown"] = md
            final_blocks = {
                "slide_part02_content.svg": {
                    "user-block": {"layout": "raw",
                                   "bounds": "0 0 100 100"},
                },
            }
            pipeline._fill_missing_content_blocks(
                cloned_svgs=["slide_part02_content.svg"],
                final_new_blocks=final_blocks,
                state=state,
            )
            # Pre-existing block preserved; no overwrite / append.
            self.assertEqual(list(final_blocks["slide_part02_content.svg"]
                                  .keys()),
                             ["user-block"])

    def test_fallback_emits_placeholder_when_markdown_missing(self):
        from mcp_ppt_native_fill import pipeline
        from mcp_ppt_native_fill.pipeline import PipelineState
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = PipelineState(workspace=td)
            # No content_markdown set at all.
            final_blocks: dict = {}
            pipeline._fill_missing_content_blocks(
                cloned_svgs=["slide_part99_content.svg"],
                final_new_blocks=final_blocks,
                state=state,
            )
            self.assertIn("slide_part99_content.svg", final_blocks)
            block = final_blocks["slide_part99_content.svg"]["content-body"]
            self.assertEqual(block["layout"], "3-column-cards")
            # At least 1 card emitted (the placeholder).
            self.assertGreaterEqual(len(block["spec"]["cards"]), 1)


class NewBlockLayoutTests(unittest.TestCase):
    """Test the three new block layouts (3-column-cards, flow-steps, revision-table)."""

    def test_3_column_cards_renders_with_spec_payload(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "3-column-cards",
            "bounds": "0 0 600 200",
            "spec": {"cards": [
                {"title": "A", "color": "#1D2CAB", "items": ["a1", "a2"]},
                {"title": "B", "color": "#EE822F", "items": ["b1"]},
                {"title": "C", "color": "#75BD42", "items": ["c1", "c2"]},
            ]},
        })
        self.assertIn("<rect", out)
        self.assertIn("A", out)
        self.assertIn("B", out)
        self.assertIn("C", out)
        self.assertIn("#1D2CAB", out)
        self.assertIn("#EE822F", out)

    def test_flow_steps_renders_with_arrows(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "flow-steps",
            "bounds": "0 0 600 200",
            "spec": {"steps": [
                {"title": "申请", "items": ["提交"]},
                {"title": "审批", "items": ["初审", "复审"]},
                {"title": "采购", "items": ["下单", "到货"]},
            ]},
        })
        # 3 step rects + 2 connector arrow paths (we use <path>, not
        # <line marker-end="url(#arrow)">, because svg_to_pptx validates
        # every marker reference against <defs>).
        self.assertEqual(out.count("<rect"), 3)
        self.assertEqual(out.count("<path"), 2)
        self.assertIn("申请", out)
        self.assertIn("审批", out)

    def test_revision_table_renders_header_and_rows(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "revision-table",
            "bounds": "0 0 800 200",
            "spec": {"rows": [
                {"date": "2026-09-01", "status": "草稿",
                 "content": "初版", "author": "张三"},
                {"date": "2026-09-05", "status": "发布",
                 "content": "审批通过", "author": "李四"},
            ]},
        })
        self.assertIn("2026-09-01", out)
        self.assertIn("草稿", out)
        self.assertIn("张三", out)
        self.assertIn("日期", out)  # header label

    def test_unsupported_layout_raises(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        with self.assertRaises(ValueError):
            _render_new_block({"layout": "made-up", "bounds": "0 0 1 1"})

    def test_hero_number_renders_centered_value_and_caption(self):
        """Bug 3 fix: hero-number renders a big centered value with
        optional unit + caption (for KPI / chapter-count statements)."""
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "hero-number",
            "bounds": "0 0 800 400",
            "spec": {"value": "5", "unit": "章",
                      "caption": "制度总章数"},
        })
        self.assertIn('font-size="72"', out)
        self.assertIn("5", out)
        self.assertIn("章", out)
        self.assertIn("制度总章数", out)
        self.assertIn("text-anchor=\"middle\"", out)

    def test_hero_number_rejects_missing_value(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        with self.assertRaises(ValueError):
            _render_new_block({
                "layout": "hero-number",
                "bounds": "0 0 800 400",
                "spec": {"caption": "no value"},
            })

    def test_callout_box_renders_quote_and_attribution(self):
        """Bug 3 fix: callout-box renders a tinted panel with a big
        quote glyph and an italic attribution."""
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "callout-box",
            "bounds": "0 0 800 300",
            "spec": {"quote": "秉公办事、维护公司利益",
                      "attribution": "采购基本原则"},
        })
        # Tinted panel background.
        self.assertIn('fill="#F4F6FB"', out)
        # Quote text rendered.
        self.assertIn("秉公办事", out)
        # Attribution with em-dash.
        self.assertIn("采购基本原则", out)
        self.assertIn("—", out)

    def test_two_column_compare_renders_two_columns_with_divider(self):
        """Bug 3 fix: two-column-compare renders two titled columns
        of items separated by a vertical rule."""
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "two-column-compare",
            "bounds": "0 0 800 300",
            "spec": {"left": {"title": "公开招标",
                                "items": ["范围广", "成本高"]},
                       "right": {"title": "邀请招标",
                                 "items": ["范围窄", "成本低"]}},
        })
        self.assertIn("公开招标", out)
        self.assertIn("邀请招标", out)
        self.assertIn("范围广", out)
        self.assertIn("成本低", out)
        # Vertical divider rule between columns.
        self.assertIn('<line ', out)

    def test_timeline_renders_nodes_and_connectors(self):
        """Bug 3 fix: timeline renders numbered circle nodes along a
        horizontal axis with labels above and details below."""
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "timeline",
            "bounds": "0 0 1000 300",
            "spec": {"steps": [
                {"label": "申请", "detail": "提交采购单"},
                {"label": "审批", "detail": "部门 + 财务"},
                {"label": "采购", "detail": "询价比价"},
                {"label": "验收入库", "detail": "质检登账"},
            ]},
        })
        # 4 nodes = 4 circles + 3 connector lines.
        self.assertEqual(out.count("<circle"), 4)
        self.assertEqual(out.count("<line"), 3)
        self.assertIn("申请", out)
        self.assertIn("验收入库", out)
        self.assertIn("提交采购单", out)

    def test_timeline_keeps_first_node_inside_bounds(self):
        """Bug 3 fix: timeline margin must be large enough that the
        leftmost node's centered detail text does not bleed outside
        the declared bounds (the quality checker rejects this as a
        blocking overflow)."""
        import re
        from mcp_ppt_native_fill.pipeline import _render_new_block
        # bounds 120 130 1060 480 → x range [120, 1180]. With 5 nodes
        # and ~20-char detail text, naive margin=40 overflows by ~70px.
        out = _render_new_block({
            "layout": "timeline",
            "bounds": "120 130 1060 480",
            "spec": {"steps": [
                {"label": "L1",
                 "detail": "至少3家比价 · 氚云申请审批 · 索票索据"},
                {"label": "L2", "detail": "提交申请单"},
                {"label": "L3", "detail": "建档询价"},
                {"label": "L4", "detail": "议价定购"},
                {"label": "L5", "detail": "交期控制"},
            ]},
        })
        # Pull out the first detail <text> x coordinate and check it
        # + estimated half-width stays inside [120, 1180].
        # The first node's detail text uses font-size=12 with
        # text-anchor="middle"; estimate width ~6.3 px per mixed char.
        # We assert no <text> x < 120 - half_width for any detail line.
        match = re.search(r'<text x="([\d.]+)" y="([\d.]+)" '
                          r'text-anchor="middle" font-size="12" '
                          r'fill="#222">([^<]+)</text>', out)
        self.assertIsNotNone(match, "expected a centered 12px detail text")
        x = float(match.group(1))
        text = match.group(3)
        half_w = len(text) * 6.3
        self.assertGreaterEqual(x - half_w, 120 - 5,
            f"first timeline detail '{text}' at x={x} overflows left "
            f"bound 120 (estimated half-width {half_w:.1f})")

    def test_normalize_accepts_new_layouts(self):
        """Bug 3 fix: _normalize_new_blocks accepts all 4 new layouts
        with minimal valid specs."""
        from mcp_ppt_native_fill.llm_planner import _normalize_new_blocks
        raw = [
            {"svg": "s1.svg", "id": "h", "bounds": "0 0 100 100",
             "layout": "hero-number",
             "spec": {"value": "5", "caption": "总章数"}},
            {"svg": "s2.svg", "id": "c", "bounds": "0 0 100 100",
             "layout": "callout-box",
             "spec": {"quote": "秉公办事"}},
            {"svg": "s3.svg", "id": "tcc", "bounds": "0 0 100 100",
             "layout": "two-column-compare",
             "spec": {"left": {"title": "A", "items": ["a1"]},
                       "right": {"title": "B", "items": ["b1"]}}},
            {"svg": "s4.svg", "id": "tl", "bounds": "0 0 100 100",
             "layout": "timeline",
             "spec": {"steps": [
                 {"label": "L1", "detail": "d1"},
                 {"label": "L2", "detail": "d2"},
                 {"label": "L3", "detail": "d3"},
             ]}},
        ]
        cleaned = _normalize_new_blocks(raw)
        self.assertEqual(len(cleaned), 4)
        seen = {c["layout"] for c in cleaned}
        self.assertEqual(seen, {"hero-number", "callout-box",
                                 "two-column-compare", "timeline"})

    def test_normalize_drops_invalid_new_layouts(self):
        """Bug 3 fix: malformed new layouts surface as warnings and
        drop silently rather than crash the planner."""
        from mcp_ppt_native_fill.llm_planner import _normalize_new_blocks
        raw = [
            # hero-number without value
            {"svg": "s1.svg", "id": "h", "bounds": "0 0 100 100",
             "layout": "hero-number", "spec": {"caption": "no value"}},
            # callout-box without quote
            {"svg": "s2.svg", "id": "c", "bounds": "0 0 100 100",
             "layout": "callout-box", "spec": {}},
            # two-column-compare with only left side
            {"svg": "s3.svg", "id": "tcc", "bounds": "0 0 100 100",
             "layout": "two-column-compare",
             "spec": {"left": {"title": "A", "items": ["a1"]}}},
            # timeline with too few steps
            {"svg": "s4.svg", "id": "tl", "bounds": "0 0 100 100",
             "layout": "timeline",
             "spec": {"steps": [{"label": "L1"}]}},
        ]
        cleaned = _normalize_new_blocks(raw)
        self.assertEqual(cleaned, [])


if __name__ == "__main__":
    unittest.main()
