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
            self.assertIn('fill="#FFFFFF"', content)

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
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = pipeline.PipelineState(workspace=td)
            state.workspace = td

            caller_mapping = {
                "slide_01.svg": {"shape-23": "caller title"},  # conflict
            }
            llm_mapping = {
                "slide_01.svg": {
                    "shape-23": "llm title",
                    "shape-24": "llm subtitle",  # new entry
                },
            }
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
                return_value={},
            ):
                state = pipeline.phase2_5_llm_plan(state, md, caller)
            self.assertNotEqual(state.stage, "failed")
            self.assertEqual(
                state.context["content_mapping"], caller
            )
            self.assertTrue(
                any("empty mapping" in w for w in state.warnings)
            )


if __name__ == "__main__":
    unittest.main()
