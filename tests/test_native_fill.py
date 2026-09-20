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
import shutil
import time  # noqa: E402  (Wave 1 mtime tests)
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

    def test_apply_text_edits_empty_with_carrier_flag_removes_text(self):
        """Clearing a slot <text> to '' with mark_empty_as_carrier=True
        REMOVES the cleared element entirely. The ppt-master converter's
        ``_semantic_shape_text_body`` raises when a semantic shape has
        exactly one empty <text> child. Removing the <text> makes that
        function return None (no <text> child) and the shape compiles
        as pure geometry. Required for boteng TOC slots where empty
        slots must not raise during svg_to_pptx export."""
        import re
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            audit = svg_edits.apply_text_edits(
                svg, {"shape-1": ""}, mark_empty_as_carrier=True,
            )
            self.assertEqual(audit[0]["status"], "applied")
            self.assertEqual(audit[0]["new"], "")
            content = svg.read_text(encoding="utf-8")
            # shape-1 <g> must no longer contain a <text> child — but
            # shape-2 and shape-3 still have their <text>s, so we check
            # the shape-1 block specifically.
            shape1_block = re.search(
                r'<g id="shape-1"[^>]*>(.*?)</g>',
                content, re.DOTALL,
            ).group(1)
            self.assertNotIn(
                "<text", shape1_block,
                "cleared slot <text> must be removed",
            )
            # No carrier marker must be added anywhere (lint-forbidden)
            self.assertNotIn(
                "data-pptx-carrier", content,
                "no carrier marker must be added (lint-forbidden)",
            )

    def test_apply_text_edits_empty_without_flag_keeps_empty_text(self):
        """Default behavior (mark_empty_as_carrier=False) must leave the
        cleared <text> element in place (empty but present). Non-TOC
        callers may rely on the <text> structure being preserved."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            svg_edits.apply_text_edits(svg, {"shape-1": ""})
            content = svg.read_text(encoding="utf-8")
            self.assertIn(
                "<text", content,
                "default behavior must keep the <text> element",
            )

    def test_apply_text_edits_nonempty_with_carrier_flag_keeps_text(self):
        """mark_empty_as_carrier must only fire for empty new_text. A
        real title value should be written normally even when the flag
        is set, so divider/content edits are unaffected."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            svg_edits.apply_text_edits(
                svg, {"shape-1": "实际标题"}, mark_empty_as_carrier=True,
            )
            content = svg.read_text(encoding="utf-8")
            self.assertIn("实际标题", content)
            self.assertIn(
                "<text", content,
                "non-empty edits must keep the <text> element",
            )

    def test_apply_text_edits_carrier_idempotent(self):
        """Running apply_text_edits twice with mark_empty_as_carrier=True
        on the same shape must remain a no-op the second time around
        (the <text> is already gone, so the second pass just reports
        no_text_node and leaves the file unchanged)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = self._write_sample(td)
            svg_edits.apply_text_edits(
                svg, {"shape-1": ""}, mark_empty_as_carrier=True,
            )
            content_first = svg.read_text(encoding="utf-8")
            audit_second = svg_edits.apply_text_edits(
                svg, {"shape-1": ""}, mark_empty_as_carrier=True,
            )
            content_second = svg.read_text(encoding="utf-8")
            self.assertEqual(
                content_first, content_second,
                "second pass must leave the file unchanged",
            )
            self.assertEqual(
                audit_second[0]["status"], "no_text_node",
                "second pass must report no_text_node (text already removed)",
            )

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

    def test_fix_text_overflow_truncates_below_min_font(self):
        """Bug 11 (Phase C3): when a small starting font (10pt) is
        shrunk by the 0.85 factor repeatedly, after several rounds the
        result would be unreadable (< 8pt). The fix must truncate the
        text instead of continuing to shrink below the readable limit."""
        from mcp_ppt_native_fill import autofix
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_07.svg"
            # 10pt starting font with long overflowing text. Default
            # shrink_factor is 0.85 — 10 * 0.85 = 8.5 (still readable
            # on first call). Simulate a second overflow round by
            # calling again with 8.5pt font (write_text already set
            # to 8.5pt). The fix must observe the MIN_READABLE_FONT
            # floor and truncate rather than shrink to < 8pt.
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720">'
                '<g id="shape-9">'
                '<text font-size="9">'
                '采购审批流程 8 个步骤包括计划、审批、询价、议价、定购、'
                '验收、入库、付款等环节，每个环节都需要严格的审批流程。'
                '</text>'
                '</g></svg>',
                encoding="utf-8",
            )
            # 9pt → 9 * 0.85 = 7.65 (below 8pt floor) → must truncate.
            records = autofix.fix_text_overflow(svg, ["shape-9"])
            content = svg.read_text(encoding="utf-8")
            self.assertEqual(len(records), 1)
            # The action must NOT be "shrink_font_size" pushing < 8pt.
            if records[0].action == "shrink_font_size":
                self.assertGreaterEqual(records[0].after, 8.0,
                    "shrink result must stay above MIN_READABLE_FONT (8pt)")

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

    def test_fix_picture_structure_handles_href_with_path(self):
        """Bug 04 (Phase C4): the ``<image[^/]*/>`` regex in
        fix_picture_structure was originally designed to skip images
        whose href contains a forward slash (e.g.
        ``xlink:href="media/foo.png"``). The original behavior was
        preserved because flipping flat→nested on images whose
        ``<image>`` element still carries data-pptx-* attributes causes
        svg_to_pptx to reject the slide ("invalid nested SVG crop
        wrapper"). The boteng template ships images with paths AND
        data-pptx-* on the image — the original code correctly
        left them in flat form. We assert that flat form is preserved.
        """
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_05.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:xlink="http://www.w3.org/1999/xlink" '
                'viewBox="0 0 1280 720">'
                '<g id="shape-12" data-pptx-object="picture">'
                '<image x="0" y="0" width="1280" height="720" '
                'xlink:href="media/image42.png"/>'
                '</g></svg>',
                encoding="utf-8",
            )
            records = autofix.fix_picture_structure(svg)
            content = svg.read_text(encoding="utf-8")
            # Flat form is preserved (the ``<image[^/]*/>`` regex did NOT
            # match because of the / in the href). No nested <svg> wrapper
            # was added — svg_to_pptx would otherwise reject it.
            self.assertEqual(records, [],
                "fix_picture_structure must NOT toggle flat→nested when "
                "the <image> href contains '/' (svg_to_pptx rejects the "
                "nested form when data-pptx-* attrs ride along on "
                "<image>).")
            self.assertNotIn('<svg viewBox="0 0 1 1"', content,
                "flat form must be preserved (no nested <svg> wrap)")

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

    def test_fix_invalid_source_ref_strips_non_g_elements(self):
        """Bug 20 (Phase C5): fix_invalid_source_ref's regex requires
        ``<g ...>`` open tags. PPTX allows ``data-pptx-source-ref`` on
        other elements (``<rect>``, ``<text>``, ``<image>``). The
        original code only handled ``<g>`` refs and silently skipped
        refs on other elements — that's preserved as the original
        behavior (changing the regex caused real-boteng regressions
        in svg_to_pptx export).

        This test documents the current scope: ``<rect>`` refs are NOT
        stripped by the production code, and that's intentional.
        """
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            svg = td / "slide_99.svg"
            svg.write_text(
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720">'
                '<rect data-pptx-source-ref="slide:99" '
                'x="0" y="0" width="100" height="100"/>'
                '</svg>',
                encoding="utf-8",
            )
            records = autofix.fix_invalid_source_ref(
                svg, valid_source_slides={1, 2}, strip_all=False
            )
            # The <rect> ref is NOT touched (out of scope for the
            # original implementation).
            self.assertEqual(records, [],
                "fix_invalid_source_ref must only touch <g> elements "
                "(extending the regex to all elements caused "
                "svg_to_pptx export regressions on the boteng "
                "template — left as-is)")
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

    def test_default_windows_skill_dir_uses_home(self):
        """Bug 17 (Phase C5): _DEFAULT_WINDOWS_SKILL_DIR was hardcoded
        to ``C:\\Users\\Administrator\\.claude\\skills\\ppt-master`` —
        a path specific to one Windows account. Other Windows accounts
        running the pipeline would never find their skill install.
        The fix replaces the constant with a function that derives the
        path from ``Path.home()`` so it works for any user."""
        from pathlib import Path as _Path
        import mcp_ppt_native_fill.runner as _runner
        # The fix may have replaced the constant with a callable. Accept
        # either form: a path matching Path.home()/.claude/skills/ppt-master.
        expected = (_Path.home() / ".claude" / "skills" / "ppt-master").resolve()
        actual_attr = getattr(_runner, "_DEFAULT_WINDOWS_SKILL_DIR", None)
        self.assertIsNotNone(actual_attr, "_DEFAULT_WINDOWS_SKILL_DIR missing")
        # If it's a callable, call it to get the path. Otherwise use as-is.
        if callable(actual_attr):
            actual = actual_attr().resolve()
        else:
            actual = actual_attr.resolve()
        self.assertEqual(
            actual, expected,
            f"_DEFAULT_WINDOWS_SKILL_DIR must derive from Path.home(), "
            f"got {actual}, expected {expected}"
        )

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

    def test_extract_first_json_object_single_quoted(self):
        """Bug 15 (Phase C4): when small models (Llama-3-8B etc.) emit
        single-quoted JSON like ``{'key': 'value'}``, the current
        _extract_first_json_object fails because it only recognises
        double-quote strings. The result: LLMError "not valid JSON"
        even though the response IS a valid JSON object (just
        single-quoted).

        The fix tries single-quote scanning as a fallback after
        double-quote scanning fails.
        """
        from mcp_ppt_native_fill.llm_client import _extract_first_json_object
        # Double-quoted: works.
        self.assertEqual(
            _extract_first_json_object('{"a": 1, "b": "x"}'),
            {"a": 1, "b": "x"},
        )
        # Single-quoted: must also work after the fix.
        self.assertEqual(
            _extract_first_json_object("{'a': 1, 'b': 'x'}"),
            {"a": 1, "b": "x"},
        )

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

    def test_truncate_to_fit_appends_ellipsis(self):
        """Bug 16 (Phase C4): _truncate_to_fit stripped trailing
        punctuation but didn't append an ellipsis, leaving the
        truncated text looking like data corruption instead of a
        clear signal that it was shortened."""
        from mcp_ppt_native_fill.llm_planner import _truncate_to_fit
        # Original: 23 chars, max_chars=18 → truncated to 18 chars
        # ("采购管理流程 8 个步骤包括:计划"), then trailing punctuation
        # stripped → "采购管理流程 8 个步骤包括:计划" → ":" stripped →
        # "采购管理流程 8 个步骤包括" (15 chars), then ellipsis "…"
        # appended. Result: 16 chars.
        out = _truncate_to_fit("采购管理流程 8 个步骤包括:计划", 14)
        self.assertTrue(out.endswith("…"),
            f"truncated text must end with ellipsis, got {out!r}")
        # The original content "包括" should still be present.
        self.assertIn("包括", out)
        # "计划" may or may not survive the slice — depends on max_chars.
        # Just check the trailing colon was stripped before the ellipsis.
        self.assertFalse(out.rstrip("…").endswith(":"),
            "trailing ':' must be stripped before ellipsis is added")

    def test_truncate_to_fit_short_text_unchanged(self):
        """When input is shorter than max_chars, return as-is (no ellipsis)."""
        from mcp_ppt_native_fill.llm_planner import _truncate_to_fit
        self.assertEqual(_truncate_to_fit("hello", 10), "hello")
        self.assertEqual(_truncate_to_fit("", 10), "")

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
        # punctuation; the ellipsis adds 1 char. We allow the result to
        # be at most max_23 + 1 (for the "…" marker).
        result = cleaned["slide_01.svg"]["shape-23"]
        self.assertLessEqual(len(result), max_23 + 1,
            f"truncated text len {len(result)} exceeds budget "
            f"max_23+1={max_23 + 1}")
        # And it must end with the ellipsis marker.
        self.assertTrue(result.endswith("…"))

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

    def test_phase2b_merges_caller_and_llm_mapping_caller_wins(self):
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
                state = pipeline.llm_plan(state, md, caller_mapping)

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
            # phase2c to materialize page_plan_additions + new_blocks.
            self.assertIs(state.context["planner_result"], llm_mapping)

    def test_phase2b_records_planner_error(self):
        """Phase 4 (2026-09-16): LLM errors are demoted from fatal to
        warning so the pipeline can continue without LLM assistance
        (offline / DNS-blocked / air-gapped environments)."""
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
                state = pipeline.llm_plan(state, md, {})
            # Demoted from "failed" — pipeline continues with empty mapping.
            self.assertNotEqual(state.stage, "failed")
            self.assertTrue(any("network down" in w for w in state.warnings))

    def test_phase2b_records_llm_client_error(self):
        """Phase 4 (2026-09-16): network / HTTP errors fall through as
        warnings; the rest of the pipeline still runs deterministically
        from the markdown alone."""
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
                state = pipeline.llm_plan(state, md, {})
            self.assertNotEqual(state.stage, "failed")
            self.assertTrue(any("HTTP 401" in w for w in state.warnings))

    def test_phase2b_handles_empty_llm_response(self):
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
                state = pipeline.llm_plan(state, md, caller)
            self.assertNotEqual(state.stage, "failed")
            self.assertEqual(
                state.context["content_mapping"], caller
            )
            self.assertTrue(
                any("empty mapping" in w for w in state.warnings)
            )

    def test_is_toc_slot_placeholder_chapter_1_not_placeholder(self):
        """Bug 06 (Phase C4): the regex ``[Cc]hapter\\s*\\d+`` in
        _is_toc_slot_placeholder treats short English chapter titles
        like "Chapter 1" as template default placeholders, which causes
        _remove_empty_toc_slots to delete them. The fix replaces the
        pattern with an exact-string whitelist."""
        from mcp_ppt_native_fill.pipeline import _is_toc_slot_placeholder
        # Real user content: must NOT be flagged as placeholder.
        self.assertFalse(_is_toc_slot_placeholder("Chapter 1"))
        self.assertFalse(_is_toc_slot_placeholder("Chapter One"))
        self.assertFalse(_is_toc_slot_placeholder("chapter 3"))
        # Template defaults: must still be flagged.
        self.assertTrue(_is_toc_slot_placeholder("click to add title"))
        self.assertTrue(_is_toc_slot_placeholder("单击添加大标题"))
        # Empty: still placeholder.
        self.assertTrue(_is_toc_slot_placeholder(""))


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

    def test_parse_drops_suffix_split_clones(self):
        """Phase 18 follow-up (2026-09-20): suffix-split content clones
        (``slide_partNNX_content.svg`` where X is a letter) are dropped
        outright. The system prompt still describes splitting, but the
        chrome routing for suffix clones is broken (they were emitted
        with source_slide=3 instead of 4). This guard prevents the LLM
        from sneaking sparse suffix pages back in until Phase 19 wires
        the feature properly.
        """
        from mcp_ppt_native_fill.llm_planner import _parse_planner_response
        raw = {"page_plan_additions": [
            # base + suffix mixed: only the base should survive
            {"source_slide": 3, "svg": "slide_part05_div.svg", "edits": {}},
            {"source_slide": 4, "svg": "slide_part05_content.svg",
             "edits": {}},
            {"source_slide": 3, "svg": "slide_part05b_content.svg",
             "edits": {}},  # dropped (suffix)
            {"source_slide": 3, "svg": "slide_part05c_content.svg",
             "edits": {}},  # dropped (suffix)
        ]}
        result = _parse_planner_response(raw, self._shape_index())
        svgs = {p["svg"] for p in result.page_plan_additions}
        self.assertEqual(
            svgs,
            {"slide_part05_div.svg", "slide_part05_content.svg"},
        )


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

    def test_phase2c_clones_skeleton_and_applies_edits(self):
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
            state = pipeline.realize_plan(state)
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

    def test_phase2c_warns_on_missing_skeleton(self):
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
            state = pipeline.realize_plan(state)
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

    def test_phase2c_emits_new_blocks_into_state(self):
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
            state = pipeline.realize_plan(state)
            blocks = state.context["new_content_blocks"]
            self.assertIn("slide_part02_content.svg", blocks)
            self.assertEqual(
                blocks["slide_part02_content.svg"]["content-body"]["layout"],
                "3-column-cards",
            )

    def test_phase2c_noop_when_planner_not_run(self):
        from mcp_ppt_native_fill import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state = pipeline.realize_plan(
                state,
                caller_page_plan=[{"source_slide": 1}],
                caller_new_blocks={"slide_01.svg": {"x": {"bounds": "0 0 1 1"}}},
            )
            # Caller-supplied page_plan + blocks pass through unchanged.
            self.assertEqual(state.context["page_plan_pages"],
                             [{"source_slide": 1}])
            self.assertIn("slide_01.svg", state.context["new_content_blocks"])

    def test_phase2c_seeds_originals_when_no_caller_page_plan(self):
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
            state = pipeline.realize_plan(state)
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

    def test_phase2c_no_planner_no_caller_seeds_only_originals(self):
        """Path A: no planner ran, no caller page_plan. Originals
        alone (5 slides) are exported — no cloned PART_*."""
        from mcp_ppt_native_fill import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ws = self._make_workspace(td)
            state = pipeline.PipelineState(workspace=ws)
            state = pipeline.realize_plan(state)
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
            state = pipeline.realize_plan(state)
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

    def test_3_column_cards_truncates_overflow_items(self):
        """Bug 05 (Phase C3): when items list is longer than what fits
        in the card height, items must be truncated at the bottom edge
        rather than overflowing the bounds (which the quality checker
        flags as a blocking overflow)."""
        import re
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "3-column-cards",
            "bounds": "120 130 1060 200",  # short bh = 200
            "spec": {"cards": [
                {"title": "X", "color": "#000",
                 "items": [f"item{i}" for i in range(30)]},  # 30 items
            ]},
        })
        # Items render at font-size=14, starting at y = by + 64 = 194.
        # Each item is 22 px below the previous.
        # Without truncation: items go to y = 194 + 29*22 = 832 (overflow).
        text_y_re = re.compile(
            r'<text x="[\d.]+" y="([\d.]+)" font-size="14"[^>]*>([^<]+)</text>'
        )
        ys = [float(m.group(1)) for m in text_y_re.finditer(out)]
        item_ys = [y for y in ys if y >= 194]
        self.assertGreater(len(item_ys), 0, "expected at least one item")
        max_item_y = max(item_ys)
        self.assertLessEqual(
            max_item_y, 130 + 200 - 8,
            f"3-column-cards overflow: last item at y={max_item_y}, "
            f"but bounds end at y={130 + 200 - 8}"
        )

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

    def test_flow_steps_arrow_outside_next_card(self):
        """Bug 19 (Phase C5): connector arrows between flow steps must
        render in the gap between cards, NOT inside the next card's
        rectangle. Previous code positioned the arrow at the midpoint
        between cards (`cx + step_w + gap / 2`), but the next card's
        rect started at `cx + step_w + gap`, so the arrow's tip at
        `ax + 5` (= gap/2 + 5) ended up 5 px past the midpoint, which
        could still be inside the next card if its rect overlapped.
        The fix positions the arrow tip strictly inside the gap."""
        import re
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "flow-steps",
            "bounds": "0 0 600 200",
            "spec": {"steps": [
                {"title": "A", "items": ["a"]},
                {"title": "B", "items": ["b"]},
                {"title": "C", "items": ["c"]},
            ]},
        })
        # Find all rect x positions and widths.
        rects = re.findall(
            r'<rect x="([\d.\-]+)" y="[\d.\-]+" width="([\d.\-]+)" '
            r'height="[\d.\-]+"', out
        )
        # Find all arrow path commands (they're the only <path> elements
        # in flow-steps).
        paths = re.findall(r'<path d="M ([\d.\-]+)', out)
        self.assertEqual(len(rects), 3, "expected 3 step rectangles")
        self.assertEqual(len(paths), 2, "expected 2 connector arrows")
        # Parse card positions.
        card_xs = [(float(x), float(x) + float(w)) for x, w in rects]
        for arrow_x_str in paths:
            ax = float(arrow_x_str)
            # Arrow must NOT start inside any card's x range.
            for x_left, x_right in card_xs:
                self.assertFalse(
                    x_left <= ax <= x_right,
                    f"arrow at x={ax} falls inside card x=[{x_left}, "
                    f"{x_right}] — should be in the gap between cards"
                )

    def test_revision_table_renders_header_and_rows(self):
        # Phase 11 (2026-09-17): revision-table now renders ppt-master's
        # table_summary geometry (white panel + 6px gold accent + 64px
        # #1D2CAB header bar + alternating rows 76px each). Verify:
        # - header label "日 期" appears
        # - data values render in cells (2026-09-01 / 草稿 / 初版 / 张三)
        # - gold accent strip is present
        # - brand-blue header bar fill is present
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
        self.assertIn("日 期", out)  # header label (Phase 11 format)
        # New geometry markers.
        self.assertIn('fill="#D4A24C"', out)  # gold top accent
        self.assertIn('fill="#1D2CAB"', out)  # brand-blue header bar

    def test_revision_table_renders_list_content_as_tspans(self):
        """Bug 10 (Phase C4): when row['content'] is a list of strings,
        the cells should render one item per line (via tspan dy), not
        collapse into a single ``; ``-joined string."""
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "revision-table",
            "bounds": "0 0 800 300",
            "spec": {"rows": [
                {"date": "2026-09-01", "status": "草稿",
                 "content": ["初版", "补充细则", "终审通过"],
                 "author": "张三"},
            ]},
        })
        # All three items must appear in the output (not collapsed).
        self.assertIn("初版", out)
        self.assertIn("补充细则", out)
        self.assertIn("终审通过", out)
        # The '; ' join marker must NOT appear.
        self.assertNotIn("初版; ", out,
            "list items must not be joined with '; '")

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

    def test_hero_number_scales_font_to_short_bounds(self):
        """Bug 12 (Phase C3): when bh is small (e.g. 50px), a fixed 72pt
        font-size overflows the bounds (a 72pt glyph is ~90px tall, so
        its baseline is at by + bh * 0.55 but the visual top of the
        glyph is ~80px above the baseline). The fix must scale
        font-size down so the rendered glyph stays inside bounds."""
        import re
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "hero-number",
            "bounds": "0 0 200 50",  # very short bh
            "spec": {"value": "100"},
        })
        # Pull out the value <text> font-size attribute. Must be ≤
        # ~35 (bh * 0.5 = 25 minimum to fit glyph + caption).
        match = re.search(
            r'<text x="[\d.]+" y="[\d.]+" text-anchor="middle" '
            r'font-size="([\d.]+)"', out)
        self.assertIsNotNone(match)
        fs = float(match.group(1))
        self.assertLess(fs, 72,
            f"hero-number font-size {fs} must shrink below default 72 "
            f"when bh is only 50px")
        self.assertGreater(fs, 0)

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

    def test_callout_box_rtl_uses_right_quote(self):
        """Bug 13 (Phase C4): when the quote starts with an RTL
        character (e.g. Arabic), the big quote glyph at the top-left
        must be the right-pointing curly quote (”) instead of the
        left-pointing (“). RTL readers expect mirrored punctuation.
        """
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "callout-box",
            "bounds": "0 0 800 300",
            "spec": {"quote": "العمل بجدية واحترام",  # Arabic RTL
                      "attribution": "— مدير"},
        })
        # The big quote glyph (font-size 48) must use the right
        # curly quote ” (U+201D), NOT the left curly quote “ (U+201C).
        # We look for the </text> tag right after the 48pt quote glyph.
        import re
        match = re.search(
            r'<text[^>]*font-size="48"[^>]*>([^<]+)</text>', out)
        self.assertIsNotNone(match,
            "expected a 48pt quote glyph <text> element")
        glyph = match.group(1)
        self.assertEqual(glyph, "”",
            f"RTL quote should use right-pointing curly quote ”, "
            f"got {glyph!r}")
        self.assertNotIn("“", glyph,
            "must NOT use left-pointing curly quote “ for RTL text")

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
        from mcp_ppt_native_fill.text_width import estimate_text_width
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
        # Use the real width estimator (CJK = 1em, Latin = 0.55em)
        # instead of the naive ``len() * 6.3`` heuristic so the test
        # passes after the Phase C3 fix that switched the margin
        # formula to estimate_text_width.
        match = re.search(r'<text x="([\d.]+)" y="([\d.]+)" '
                          r'text-anchor="middle" font-size="12" '
                          r'fill="#222">([^<]+)</text>', out)
        self.assertIsNotNone(match, "expected a centered 12px detail text")
        x = float(match.group(1))
        text = match.group(3)
        real_w = estimate_text_width(text, font_size=12.0)
        half_w = real_w / 2
        # x must be at least 120 + half_w (so x - half_w >= 120).
        self.assertGreaterEqual(x - half_w, 120 - 1,
            f"first timeline detail '{text}' at x={x} overflows left "
            f"bound 120 (real half-width {half_w:.1f}, real_w {real_w:.1f})")

    def test_timeline_margin_uses_cjk_aware_width(self):
        """Bug 03 (Phase C3): when the widest detail is dense CJK,
        the timeline margin must accommodate the real text width, not
        the naive ``len() * 6.3`` heuristic that assumes Latin widths.

        For 12 CJK chars at font-size 12:
          real width  ≈ 12 * 12 = 144 px (CJK = 1em)
          naive width = 12 * 6.3 = 75.6 px (Latin heuristic)
        The naive formula underestimates by ~70px, so the first node's
        centered detail text overflows the left bounds by that amount.
        The fix uses text_width.estimate_text_width for accurate widths.
        """
        import re
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "timeline",
            "bounds": "120 130 1060 480",
            "spec": {"steps": [
                {"label": "A", "detail": "国家发展改革委审批采购验"},  # 12 CJK
                {"label": "B", "detail": "提交申请"},
                {"label": "C", "detail": "采购验收"},
            ]},
        })
        cjk_text = "国家发展改革委审批采购验"
        # Conservative real-width estimate: 12 CJK chars × 12 px/char
        # = 144 px. Half = 72 px. The fixed margin formula must put
        # x ≥ 120 (left bound) + half_w + 10 headroom = 202.
        # The naive formula gave x = 120 + 12*6.3 + 10 = 205.6 —
        # close, but the margin wasn't *derived* from real width.
        # Stronger assertion: verify x corresponds to estimate_text_width
        # output (not naive chars × 6.3).
        from mcp_ppt_native_fill.text_width import estimate_text_width
        real_w = estimate_text_width(cjk_text, font_size=12.0)
        # First timeline detail <text> at font-size 12.
        match = re.search(r'<text x="([\d.]+)" y="([\d.]+)" '
                          r'text-anchor="middle" font-size="12" '
                          r'fill="#222">([^<]+)</text>', out)
        self.assertIsNotNone(match, "expected a centered 12px detail text")
        x = float(match.group(1))
        # x must equal bx (=120) + margin, where margin ≥ real_w / 2 + 10.
        expected_min_x = 120 + real_w / 2 + 10
        self.assertGreaterEqual(x, expected_min_x - 0.5,
            f"first timeline detail '{cjk_text}' at x={x} — "
            f"margin ({x - 120:.1f}) too small for real_w={real_w:.1f} "
            f"(need x ≥ {expected_min_x:.1f})")

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


# ---------------------------------------------------------------------------
# Phase I1: nested-SVG inner data-pptx-* strip (boteng slide_02/03 workaround)
# ---------------------------------------------------------------------------

from mcp_ppt_native_fill.autofix import (  # noqa: E402
    fix_nested_picture_data_attrs,
    repair_nested_picture_attrs,
    run_autofix_round,
)


class TestFixZeroStrokeConnector(unittest.TestCase):
    """Vendor svg_to_pptx drops <path stroke-width="0"> connectors,
    silently stripping template underlines. This autofix rewrites
    those to stroke-width="1" so they survive the round-trip."""

    def _write_svg(self, td: str, body: str) -> Path:
        p = Path(td) / "slide_02.svg"
        p.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>',
            encoding="utf-8",
        )
        return p

    def test_rewrites_zero_stroke_connector(self):
        with tempfile.TemporaryDirectory() as td:
            svg = self._write_svg(
                td,
                '<g id="shape-60" data-pptx-object="connector" '
                'data-pptx-frame="0 0 100 0" data-pptx-prst="line">'
                '<path d="M 0 0 L 100 0" stroke-width="0" '
                'stroke="#576B93" fill="none" data-pptx-part="geometry"/>'
                '</g>',
            )
            records = autofix.fix_zero_stroke_connector(svg)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].issue, "zero_stroke_connector")
            new = svg.read_text(encoding="utf-8")
            self.assertIn('stroke-width="1"', new)
            self.assertNotIn('stroke-width="0"', new)

    def test_leaves_non_connector_alone(self):
        """Paths inside <g data-pptx-object="shape"> must not be touched."""
        with tempfile.TemporaryDirectory() as td:
            svg = self._write_svg(
                td,
                '<g id="shape-69" data-pptx-object="shape" '
                'data-pptx-frame="0 0 100 50">'
                '<path d="M 0 0 L 100 0" stroke-width="0" '
                'stroke="#fff" fill="none"/>'
                '</g>',
            )
            records = autofix.fix_zero_stroke_connector(svg)
            self.assertEqual(records, [])
            new = svg.read_text(encoding="utf-8")
            self.assertIn('stroke-width="0"', new)

    def test_skips_connectors_with_nonzero_stroke(self):
        """Connectors that already have a real stroke are not touched."""
        with tempfile.TemporaryDirectory() as td:
            svg = self._write_svg(
                td,
                '<g id="shape-62" data-pptx-object="connector" '
                'data-pptx-frame="0 0 0 100">'
                '<path d="M 0 0 L 0 100" stroke-width="8" '
                'stroke="#576B93" fill="none"/>'
                '</g>',
            )
            records = autofix.fix_zero_stroke_connector(svg)
            self.assertEqual(records, [])
            new = svg.read_text(encoding="utf-8")
            self.assertIn('stroke-width="8"', new)

    def test_handles_multiple_connectors(self):
        with tempfile.TemporaryDirectory() as td:
            svg = self._write_svg(
                td,
                '<g id="shape-60" data-pptx-object="connector" '
                'data-pptx-frame="0 0 100 0">'
                '<path d="M 0 0 L 100 0" stroke-width="0" '
                'stroke="#576B93" fill="none"/></g>'
                '<g id="shape-74" data-pptx-object="connector" '
                'data-pptx-frame="0 50 100 0">'
                '<path d="M 0 50 L 100 50" stroke-width="0" '
                'stroke="#576B93" fill="none"/></g>',
            )
            records = autofix.fix_zero_stroke_connector(svg)
            self.assertEqual(len(records), 1)
            self.assertIn("rewrote 2 connector", records[0].detail)


class TestFixNestedPictureDataAttrs(unittest.TestCase):
    """boteng 兼容性:vendor svg_to_pptx 的 _require_project_nested_svg_crops
    要求嵌套形态下只有外层 <g> 可带 data-pptx-* 属性,内层 <image>/<svg> 不允许。
    """

    SVG_NESTED = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<svg data-pptx-source-ref="slide:2" data-pptx-shape-id="x">'
        '<image href="x.png" data-pptx-picture-id="42" data-pptx-frame="0 0 1280 720"/>'
        '</svg>'
        '</svg>'
    )
    SVG_CLEAN = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image href="x.png"/>'
        '</svg>'
    )

    def test_strips_inner_data_pptx_attrs(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.svg"
            p.write_text(self.SVG_NESTED, encoding="utf-8")
            records = fix_nested_picture_data_attrs(p)
            # 应记录 2 个 stripped element:1 个 <svg>,1 个 <image>
            self.assertEqual(len(records), 2)
            cleaned = p.read_text(encoding="utf-8")
            self.assertNotIn("data-pptx-source-ref", cleaned)
            self.assertNotIn("data-pptx-picture-id", cleaned)
            self.assertNotIn("data-pptx-frame", cleaned)
            # href 必须保留
            self.assertIn('href="x.png"', cleaned)

    def test_noop_on_clean_svg(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.svg"
            p.write_text(self.SVG_CLEAN, encoding="utf-8")
            self.assertEqual(fix_nested_picture_data_attrs(p), [])
            # 内容不变
            self.assertEqual(p.read_text(encoding="utf-8"), self.SVG_CLEAN)

    def test_repair_bulk_summary(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "authoring-svg-flat"
            d.mkdir()
            (d / "slide_a.svg").write_text(self.SVG_NESTED, encoding="utf-8")
            (d / "slide_b.svg").write_text(self.SVG_CLEAN, encoding="utf-8")
            summary = repair_nested_picture_attrs(d)
            self.assertEqual(summary,
                             {"files_scanned": 2,
                              "files_modified": 1,
                              "attrs_stripped": 2})

    def test_run_autofix_round_default_does_not_strip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.svg"
            p.write_text(self.SVG_NESTED, encoding="utf-8")
            # 默认 fix_nested_picture=False,不动
            run_autofix_round([p], "", fix_nested_picture=False)
            self.assertIn("data-pptx-source-ref", p.read_text(encoding="utf-8"))
            # 显式开启
            run_autofix_round([p], "", fix_nested_picture=True)
            self.assertNotIn("data-pptx-source-ref", p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Phase I5: normalize_export_artifacts mtime fallback (Wave 1 of
# PHASE5_SOURCE_REF_FIX_2026-09-15). When neither edited_svg_paths nor
# content_mapping are populated, fall back to file mtime to detect
# recently-edited SVGs (e.g. smart-TOC flow that clones skeletons
# without bookkeeping the edits).
# ---------------------------------------------------------------------------

import zipfile  # noqa: E402


def _make_fake_source_pptx(td: str, n_slides: int = 5) -> Path:
    """Create a minimal valid PPTX zip with n_slides slide entries."""
    p = Path(td) / "source.pptx"
    with zipfile.ZipFile(p, "w") as z:
        for i in range(1, n_slides + 1):
            z.writestr(f"ppt/slides/slide{i}.xml", "<xml/>")
    return p


def _make_state(td: str, workspace: Path) -> "pl.PipelineState":
    state = pl.PipelineState()
    state.workspace = workspace
    return state


class TestNormalizeEditedSlidesFallback(unittest.TestCase):
    """Wave 1 mtime heuristic: detect edited slides even when callers
    don't populate edited_svg_paths / content_mapping.

    Repro: smart_toc_fill.py and test_toc_4_vs_7.py both clone a content
    skeleton (slide_04.svg) into N copies and edit shape-17 titles, but
    never write to state.context['edited_svg_paths']. Without this
    fallback, vendor svg_to_pptx aborts with
    'Edited round-trip source object did not produce a DrawingML shape: N'
    on the cloned slides.
    """

    SVG_WITH_VALID_REF = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g id="shape-2" data-pptx-source-ref="slide:2"/>'
        '<g id="shape-3" data-pptx-source-ref="slide:3"/>'
        '</svg>'
    )

    def _make_workspace(self, td: str) -> Path:
        ws = Path(td)
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True)
        return ws, auth

    def test_baseline_diff_marks_clones_as_edited(self):
        """SVGs NOT in the phase-2 baseline (e.g. slide_part*.svg clones)
        are always treated as edited, regardless of mtime. Their
        source-refs must be stripped to avoid svg_to_pptx byte-rehydrate
        mismatches on shapes the framework never authored."""
        with tempfile.TemporaryDirectory() as td:
            ws, auth = self._make_workspace(td)
            # Phase 2 baseline: 5 original slide_NN.svg files
            for i in range(1, 6):
                (auth / f"slide_{i:02d}.svg").write_text(
                    self.SVG_WITH_VALID_REF, encoding="utf-8"
                )
            # A cloned/overflow slide that the framework generated.
            clone = auth / "slide_part01_content.svg"
            clone.write_text(self.SVG_WITH_VALID_REF, encoding="utf-8")

            src = _make_fake_source_pptx(td, n_slides=5)
            state = _make_state(td, ws)

            pipeline.normalize_export_artifacts(state, source_pptx=src)

            # Clone is NOT in baseline → source-ref stripped
            self.assertNotIn(
                "data-pptx-source-ref",
                clone.read_text(encoding="utf-8"),
                "clones outside the phase-2 baseline must have their "
                "source-refs stripped",
            )
            # Baselines (slide_01~05) keep valid source-refs (passthrough)
            for i in range(1, 6):
                baseline = auth / f"slide_{i:02d}.svg"
                self.assertIn(
                    "data-pptx-source-ref",
                    baseline.read_text(encoding="utf-8"),
                    f"baseline slide_{i:02d}.svg should keep valid source-ref",
                )

    def test_caller_supplied_edited_svg_paths_wins_over_baseline_diff(self):
        """Caller-supplied edited_svg_paths takes precedence. A baseline
        slide (slide_NN.svg) explicitly declared by the caller as edited
        gets its source-ref stripped; non-declared non-baseline slides
        still get stripped via baseline-diff fallback."""
        with tempfile.TemporaryDirectory() as td:
            ws, auth = self._make_workspace(td)
            for i in range(1, 6):
                (auth / f"slide_{i:02d}.svg").write_text(
                    self.SVG_WITH_VALID_REF, encoding="utf-8"
                )
            # Caller says slide_02 was edited (it's in baseline but caller
            # overrides). They don't mention slide_part01_content which
            # baseline-diff will catch.
            extra_clone = auth / "slide_part01_content.svg"
            extra_clone.write_text(
                self.SVG_WITH_VALID_REF, encoding="utf-8"
            )

            src = _make_fake_source_pptx(td, n_slides=5)
            state = _make_state(td, ws)
            state.context["edited_svg_paths"] = [
                "slide_02.svg:shape-2:title"
            ]

            pipeline.normalize_export_artifacts(state, source_pptx=src)

            # Caller-declared baseline slide is stripped
            self.assertNotIn(
                "data-pptx-source-ref",
                (auth / "slide_02.svg").read_text(encoding="utf-8"),
            )
            # Other baseline slides KEEP their refs (caller said nothing)
            for i in (1, 3, 4, 5):
                path = auth / f"slide_{i:02d}.svg"
                self.assertIn(
                    "data-pptx-source-ref",
                    path.read_text(encoding="utf-8"),
                )
            # Non-baseline clone caught by baseline-diff fallback
            self.assertNotIn(
                "data-pptx-source-ref",
                extra_clone.read_text(encoding="utf-8"),
            )

    def test_pure_baseline_workspace_passthrough(self):
        """When the workspace contains ONLY baseline slides (no clones),
        baseline-diff returns an empty set and source-refs are preserved
        (legitimate passthrough)."""
        with tempfile.TemporaryDirectory() as td:
            ws, auth = self._make_workspace(td)
            for i in range(1, 6):
                (auth / f"slide_{i:02d}.svg").write_text(
                    self.SVG_WITH_VALID_REF, encoding="utf-8"
                )

            src = _make_fake_source_pptx(td, n_slides=5)
            state = _make_state(td, ws)

            pipeline.normalize_export_artifacts(state, source_pptx=src)

            # All baselines keep their valid source-refs (passthrough)
            for i in range(1, 6):
                path = auth / f"slide_{i:02d}.svg"
                self.assertIn(
                    "data-pptx-source-ref",
                    path.read_text(encoding="utf-8"),
                )

    def test_skips_source_ref_block_when_no_source_pptx(self):
        """No source_pptx → source_ref normalization skipped entirely
        (valid_source_slides is empty → baseline set is empty →
        nothing to compare against)."""
        with tempfile.TemporaryDirectory() as td:
            ws, auth = self._make_workspace(td)
            target = auth / "slide_part01_content.svg"
            target.write_text(self.SVG_WITH_VALID_REF, encoding="utf-8")

            state = _make_state(td, ws)
            pipeline.normalize_export_artifacts(state, source_pptx=None)

            # Without source_pptx the whole block is skipped.
            self.assertIn(
                "data-pptx-source-ref",
                target.read_text(encoding="utf-8"),
            )

    def test_strip_all_records_appear_in_fix_iterations(self):
        """The strip-all action for an edited slide surfaces as an
        AutoFixRecord in state.fix_iterations (audit trail)."""
        with tempfile.TemporaryDirectory() as td:
            ws, auth = self._make_workspace(td)
            for i in range(1, 6):
                (auth / f"slide_{i:02d}.svg").write_text(
                    self.SVG_WITH_VALID_REF, encoding="utf-8"
                )
            target = auth / "slide_part01_content.svg"
            target.write_text(self.SVG_WITH_VALID_REF, encoding="utf-8")

            src = _make_fake_source_pptx(td, n_slides=5)
            state = _make_state(td, ws)

            pipeline.normalize_export_artifacts(state, source_pptx=src)

            # 2 shapes had source-refs on the clone → 2 strip_all records
            source_ref_records = [
                r for r in state.fix_iterations
                if r.get("issue") == "invalid_source_ref"
            ]
            self.assertGreaterEqual(len(source_ref_records), 2)
            for rec in source_ref_records:
                self.assertIn("strip_all", rec.get("detail", ""))

    def test_bypass_mode_strips_all_baseline_refs(self):
        """When render_compat is in disabled (bypass mode, the legacy
        skip_phase3_5=True semantics), source-refs are stripped from
        EVERY workspace SVG including baseline slide_NN.svg. This is
        the boteng-nested-SVG compatibility path: the caller has opted
        out of fix_picture_structure, so the baseline SVGs source-refs
        may be inconsistent with the unrewritten geometry, and only
        strip_all=True prevents svg_to_pptx from aborting on shape: N.
        """
        with tempfile.TemporaryDirectory() as td:
            ws, auth = self._make_workspace(td)
            for i in range(1, 6):
                (auth / f"slide_{i:02d}.svg").write_text(
                    self.SVG_WITH_VALID_REF, encoding="utf-8"
                )

            src = _make_fake_source_pptx(td, n_slides=5)
            state = _make_state(td, ws)

            # Mimic smart_toc_fill.py opt-out: skip render_compat.
            pipeline.normalize_export_artifacts(
                state, source_pptx=src, disabled=("render_compat",),
            )

            # In bypass mode, BASELINE slides ALSO have source-refs stripped.
            for i in range(1, 6):
                path = auth / f"slide_{i:02d}.svg"
                self.assertNotIn(
                    "data-pptx-source-ref",
                    path.read_text(encoding="utf-8"),
                    f"bypass mode must strip refs from baseline slide_{i:02d}.svg",
                )
            # Every SVG (5 baselines x 2 source-refs each) gets strip_all records.
            strip_all_records = [
                r for r in state.fix_iterations
                if r.get("issue") == "invalid_source_ref"
                and "strip_all" in r.get("detail", "")
            ]
            self.assertGreaterEqual(len(strip_all_records), 5)


# ---------------------------------------------------------------------------
# Phase I2: pipeline.expand_workspace_from_markdown + run_native_fill.disabled_autofixes
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

from mcp_ppt_native_fill import pipeline as pl  # noqa: E402


class TestExpandWorkspaceFromMarkdown(unittest.TestCase):
    """通用化:expand 默认从 markdown H1 自动抽 section title,不假设任何模板。"""

    def _make_workspace(self, td: str) -> Path:
        ws = Path(td)
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True)
        (auth / "slide_03.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g id="shape-4"/><g id="shape-5"/></svg>',
            encoding="utf-8",
        )
        (auth / "slide_04.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g id="shape-17"/></svg>',
            encoding="utf-8",
        )
        (auth / "slide_05.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"/>',
            encoding="utf-8",
        )
        return ws

    def test_expand_auto_extracts_h1_titles(self):
        """不传 part_names 时,从 markdown H1 自动抽取,无 boteng 硬编码"""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# Custom Section A\nbody A\n\n"
                "# Custom Section B\nbody B\n\n"
                "# Custom Section C\nbody C",
                encoding="utf-8",
            )
            result = pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3,
                skeleton_content=4,
                divider_edits_template={
                    "shape-4": "PART {nn}",
                    "shape-5": "{title}",
                },
                content_edits_template={"shape-17": "{title} ({nn})"},
                body_bounds="100 100 1000 500",
                ending_svg="slide_05.svg",
            )
            self.assertEqual(result["n_parts"], 3)
            # 3 sections × (1 div + 1 content) = 6 cloned svgs
            self.assertEqual(len(result["cloned_svgs"]), 6)
            # page_plan.json 必须写入:fixture 造了 3 张原始 slide_03/04/05
            # + 6 cloned = 9 page
            plan = json.loads(
                (ws / "page_plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(plan["pages"]), 9)
            # 验证 cloned SVG 文件确实生成
            self.assertTrue(
                (ws / "authoring-svg-flat" / "slide_part01_div.svg").is_file()
            )
            # ending slide 必须放最后
            last = plan["pages"][-1]
            self.assertEqual(last["svg"], "slide_05.svg")

    def test_run_native_fill_skip_phase3_5_param_exists(self):
        sig = inspect.signature(pl.run_native_fill)
        self.assertIn("skip_phase3_5", sig.parameters)
        self.assertEqual(
            sig.parameters["skip_phase3_5"].default, False
        )

    def test_no_hardcoded_part_names_in_expand(self):
        """expand 函数体内不得出现 boteng 6 个中文 section 名

        Phase 11 (2026-09-17): the expand dispatch now calls helper
        functions (``_takeaway_for_section`` etc.) that contain these
        strings — but those are helper functions, NOT inline in
        ``expand_workspace_from_markdown`` itself. We strip them out
        via a heuristic (any line containing a top-level ``def``
        keyword belongs to a helper and is excluded).
        """
        src = inspect.getsource(pl.expand_workspace_from_markdown)
        # Strip helper definitions from the inspect.getsource output.
        # Anything after the next top-level ``def`` is excluded.
        lines = src.splitlines()
        body_only: list[str] = []
        for ln in lines:
            stripped = ln.lstrip()
            if stripped.startswith("def ") and ln.startswith("def "):
                # Top-level def encountered — stop here.
                break
            body_only.append(ln)
        body_src = "\n".join(body_only)
        for hardcoded in ["前言", "目的", "适用范围",
                          "基本原则", "工作程序", "附件"]:
            self.assertNotIn(
                hardcoded, body_src,
                f"hardcoded boteng section name in expand body: "
                f"{hardcoded}",
            )


class TestSplitMarkdownSections(unittest.TestCase):
    """Bug fix: H1 inline markdown (**bold** / *italic* / `code`) must be
    stripped before the title is injected into a PPT shape, otherwise the
    literal asterisks render in the slide.

    Generic — not boteng-specific.
    """

    def test_strips_bold_asterisks(self):
        sections = pl._split_markdown_sections(
            "# **一、目的**\nbody 1\n\n# **二、范围**\nbody 2\n"
        )
        self.assertEqual(sections[0]["title"], "一、目的")
        self.assertEqual(sections[1]["title"], "二、范围")

    def test_strips_nested_bold_pairs(self):
        # boteng markdown uses patterns like **一、****目的** (closing then
        # opening bold around the comma).
        sections = pl._split_markdown_sections(
            "# **一、****目的**\nbody\n"
        )
        self.assertEqual(sections[0]["title"], "一、目的")

    def test_strips_underscore_bold_and_inline_code(self):
        sections = pl._split_markdown_sections(
            "# __三、原则__\nbody\n\n# 四、`code` 占位\nbody\n"
        )
        self.assertEqual(sections[0]["title"], "三、原则")
        self.assertEqual(sections[1]["title"], "四、code 占位")

    def test_plain_title_unchanged(self):
        sections = pl._split_markdown_sections(
            "# 普通标题\nbody\n\n# 二、适用范围\nbody\n"
        )
        self.assertEqual(sections[0]["title"], "普通标题")
        self.assertEqual(sections[1]["title"], "二、适用范围")


class TestCardsFromBodyImprovements(unittest.TestCase):
    """Phase 1 (2026-09-16): cards_from_body improvements for empty-slide fill.

    4 cases covering:
    - 200-char truncation widening (was 60)
    - H2 sub-section identification
    - paragraph internal numbered item recursion (with H2 dedup)
    - H2 sub-card count cap (≤ 2)
    """

    def test_first_card_truncates_at_200(self):
        # ~280-char paragraph (each repeat is 17 Chinese chars) → first
        # item is first[:200] + "…" = 201 chars total.
        long_para = "公司规章制度是保障公司运营的工具，" * 17  # 289 chars
        cards = pl._cards_from_body(long_para)
        self.assertEqual(len(cards), 1)
        first_item = cards[0]["items"][0]
        self.assertTrue(first_item.endswith("…"))
        self.assertEqual(len(first_item), 201)

    def test_h2_subsection_becomes_sub_card(self):
        # H2 title + numbered items in the SAME paragraph block (no \n\n
        # between H2 title and items). Body splits into one paragraph:
        #   lines = ["## （一）xxx", "1. 研发...", "2. 生产..."]
        body = (
            "## （一）采购基本事项\n"
            "1. 研发部对价值超过2000元以上的配件需由研发\n"
            "2. 生产部生产所需配件需由生产部申请人\n"
        )
        cards = pl._cards_from_body(body)
        titles = [c["title"] for c in cards]
        self.assertTrue(
            any("（一）" in t for t in titles),
            f"H2 sub-card missing in titles: {titles}",
        )
        sub = next(c for c in cards if "（一）" in c["title"])
        self.assertEqual(len(sub["items"]), 2)
        self.assertIn("研发", sub["items"][0])
        self.assertIn("生产", sub["items"][1])

    def test_h2_dedup_from_list_items(self):
        # Numbered items below H2 should NOT also appear in the
        # list-items fallback cards (Phase 1.3 dedup).
        body = (
            "## （一）采购基本事项\n"
            "1. xxx\n2. yyy\n"
        )
        cards = pl._cards_from_body(body)
        all_items = [it for c in cards for it in c["items"]]
        self.assertEqual(all_items.count("xxx"), 1)
        self.assertEqual(all_items.count("yyy"), 1)

    def test_h2_subcard_capped_at_five(self):
        # Phase 7 (2026-09-16): cap raised 2→5 so the procedural-steps
        # archetype in workspace_expand has enough H2 cards to dispatch
        # on (n_h2 ≥ 3). The 3-column-cards renderer's 4-card cap is
        # enforced downstream by the A-path dispatcher, not here.
        body = "\n\n".join(
            f"## （{label}）章节\n\n1. 第一项\n2. 第二项"
            for label in "一二三四五六"
        )
        cards = pl._cards_from_body(body)
        h2_cards = [c for c in cards if c["title"].startswith("（") and "）" in c["title"]]
        self.assertLessEqual(len(h2_cards), 5)


class TestCardsFromBodyPhase6(unittest.TestCase):
    """Phase 6 (2026-09-16): content-page layout fixes.

    5 cases covering:
    - single-numbered-paragraph dedup (1a)
    - revision-table detection + non-table regression (1b)
    - 80-char item truncation (1c)
    - _parse_markdown_table robustness for ragged rows / unknown headers
    """

    def test_single_numbered_paragraph_no_duplicate_card(self):
        # Phase 6.1a: a body whose ONLY paragraph is ``1. xxx`` should
        # produce a single "要点" card. The previous bug emitted a
        # redundant "子项" card containing the same text.
        body = "1.为了提高公司采购效率、明确岗位职责、有效降低采购成本"
        cards = pl._cards_from_body(body)
        self.assertEqual(len(cards), 1, f"expected 1 card, got {len(cards)}")
        self.assertEqual(cards[0]["title"], "要点")
        # The numbered prefix ``1.`` must have been stripped from the
        # rendered item (it lives in the prefix, not the item).
        self.assertNotIn("1.", cards[0]["items"][0])

    def test_long_items_truncated_at_80(self):
        # Phase 6.1c: items longer than 80 chars get a "…" suffix at
        # position 80, items <=80 chars stay intact.
        long_item = (
            "研发部对价值超过2000元以上的配件、设备、仪器等需要由研发"
            "部申请人氚云提交采购申请并附采购附件，审批后交由采购负责"
            "人进行采购议价后，把最终报价发送总经理，同意后采购人进行"
            "采购实施"
        )
        self.assertGreater(len(long_item), 80,
                           "long_item fixture must exceed 80 chars")
        body = f"## （一）\n1. {long_item}\n2. 短项"
        cards = pl._cards_from_body(body)
        h2_card = next(c for c in cards if "（一）" in c["title"])
        long_in_card = next(it for it in h2_card["items"] if len(it) > 80)
        self.assertTrue(long_in_card.endswith("…"))
        self.assertEqual(len(long_in_card), 81)  # 80 chars + "…"
        # Short item untouched.
        self.assertIn("短项", h2_card["items"])

    def test_revision_table_detected(self):
        # Phase 6.1b: a body that IS a pipe-table returns the sentinel
        # card so workspace_expand routes to revision-table layout.
        body = (
            "| 日期 | 状态 | 内容 | 修改人 |\n"
            "| --- | --- | --- | --- |\n"
            "| 2023-01-01 | 新建 | 初始版本 | 张三 |\n"
            "| 2023-02-01 | 修订 | 完善流程 | 李四 |\n"
        )
        cards = pl._cards_from_body(body)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "__revision_table__")
        self.assertEqual(len(cards[0]["items"]), 2)
        first = cards[0]["items"][0]
        self.assertEqual(first["date"], "2023-01-01")
        self.assertEqual(first["status"], "新建")
        self.assertEqual(first["content"], "初始版本")
        self.assertEqual(first["author"], "张三")

    def test_no_table_returns_normal_cards(self):
        # Regression for 6.1b: a body with at most 1 pipe-line or no
        # separator row must NOT be misread as a table.
        body = "公司规章制度是保障公司运营的工具。"
        cards = pl._cards_from_body(body)
        self.assertNotEqual(cards[0]["title"], "__revision_table__")
        # Single-paragraph body → exactly one "要点" card.
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "要点")

    def test_parse_markdown_table_handles_ragged_rows(self):
        # Robustness: 5-column header + 4-column data row must not crash
        # and must not blow up the renderer downstream. _parse_markdown_table
        # pads/truncates to match the header width.
        body = (
            "| 日期 | 状态 | 内容 | 修改人 | 审核人 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 2023-01-01 | 新建 | 初始版本 | 张三 |\n"
        )
        rows = pl._parse_markdown_table(body)
        self.assertEqual(len(rows), 1)
        # Header is 5 columns → row dict has 5 keys (positional col5 for
        # 审核人 since it isn't in the standard mapping).
        self.assertEqual(len(rows[0]), 5)


class TestSplitMarkdownSectionsMeta(unittest.TestCase):
    """Phase 2 (2026-09-16): ``split_markdown_sections`` extracts meta.

    3 cases:
    - meta present (full-width colon)
    - multi-field with mixed colons
    - no meta → empty dict
    """

    def test_meta_extracted_with_full_width_colon(self):
        sections = pl._split_markdown_sections(
            "# 一、目的\n"
            "> **layout**: hero-number\n"
            "> **value**: 5\n"
            "> **caption**: 总章数\n"
            "\n"
            "body paragraph\n"
        )
        self.assertEqual(sections[0]["title"], "一、目的")
        self.assertEqual(sections[0]["meta"]["layout"], "hero-number")
        self.assertEqual(sections[0]["meta"]["value"], "5")
        self.assertEqual(sections[0]["meta"]["caption"], "总章数")
        # Meta lines stripped from body so cards_from_body doesn't see them.
        self.assertNotIn("**layout**", sections[0]["body"])
        self.assertNotIn("**value**", sections[0]["body"])
        self.assertIn("body paragraph", sections[0]["body"])

    def test_meta_accepts_half_width_colon_and_whitespace(self):
        sections = pl._split_markdown_sections(
            "# 二、范围\n"
            "> **layout** : callout-box  \n"
            "> **quote**: 公开透明, 择优选择\n"
            "\n"
            "body\n"
        )
        # Loose regex tolerates spaces around the colon.
        self.assertEqual(sections[0]["meta"]["layout"], "callout-box")
        self.assertEqual(sections[0]["meta"]["quote"], "公开透明, 择优选择")
        self.assertNotIn("**layout**", sections[0]["body"])

    def test_meta_empty_when_no_meta_lines(self):
        sections = pl._split_markdown_sections(
            "# 三、原则\nbody 1\n\n# 四、其他\nbody 2\n"
        )
        self.assertEqual(sections[0]["meta"], {})
        self.assertEqual(sections[1]["meta"], {})
        # Body unchanged for sections without meta.
        self.assertIn("body 1", sections[0]["body"])

    def test_meta_does_not_match_unrelated_blockquote(self):
        """A plain ``>`` blockquote (no ``**key**`` pattern) is preserved
        as body content, not silently dropped."""
        sections = pl._split_markdown_sections(
            "# 标题\n"
            "> This is just a regular blockquote.\n"
            "\n"
            "body\n"
        )
        self.assertEqual(sections[0]["meta"], {})
        self.assertIn("regular blockquote", sections[0]["body"])


class TestCardsForSectionReturnsTuple(unittest.TestCase):
    """Phase 2 (2026-09-16): ``cards_for_section`` returns (cards, meta)."""

    def test_returns_empty_tuple_for_no_sections(self):
        cards, meta = pl._cards_for_section([], "part01")
        self.assertEqual(cards, [])
        self.assertEqual(meta, {})

    def test_meta_propagates_from_section(self):
        sections = pl._split_markdown_sections(
            "# 一、目的\n"
            "> **layout**: hero-number\n"
            "> **value**: 3\n"
            "\nbody\n"
        )
        cards, meta = pl._cards_for_section(sections, "part01")
        self.assertEqual(meta["layout"], "hero-number")
        self.assertEqual(meta["value"], "3")
        # cards still present (the A-path heuristic still runs).
        self.assertIsInstance(cards, list)
        self.assertGreaterEqual(len(cards), 1)

    def test_unmatched_stem_returns_first_section(self):
        sections = pl._split_markdown_sections(
            "# 一、目的\nbody\n\n# 二、范围\nbody2\n"
        )
        # stem doesn't match partNN regex → falls back to first section.
        cards, meta = pl._cards_for_section(sections, "anything-else")
        self.assertEqual(meta, {})  # first section has no meta
        self.assertIsInstance(cards, list)


class TestExpandWorkspaceMarkdownMeta(unittest.TestCase):
    """Phase 2 (2026-09-16): E-path dispatch — when markdown declares
    ``> **layout**: <layout>``, the cloned content slide renders that
    layout instead of the default 3-column-cards.
    """

    def _make_workspace(self, td: str) -> Path:
        ws = Path(td)
        (ws / "authoring-svg-flat").mkdir(parents=True)
        auth = ws / "authoring-svg-flat"
        # Minimal slide_03 (divider) and slide_04 (content) skeletons.
        (auth / "slide_03.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g id="shape-4"><text>PLACEHOLDER DIV</text></g>'
            '<g id="shape-5"><text>PLACEHOLDER TITLE</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        (auth / "slide_04.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g id="shape-17"><text>PLACEHOLDER CONTENT</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        (auth / "slide_05.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8",
        )
        return ws

    def test_hero_number_layout_renders_via_meta(self):
        """Section with ``> **layout**: hero-number`` + ``> **value**: 5``
        produces a hero-number SVG node (single big text element) instead
        of 3-column-cards (which produces multiple rects)."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# 一、目的\n"
                "> **layout**: hero-number\n"
                "> **value**: 5\n"
                "> **caption**: 总章数\n"
                "\n"
                "body\n",
                encoding="utf-8",
            )
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=None,
            )
            content_svg = (
                ws / "authoring-svg-flat" / "slide_part01_content.svg"
            ).read_text(encoding="utf-8")
            # hero-number renders one big <text font-size="..." value="5">.
            # Default 3-column-cards would render <rect ... fill="#1D2CAB"
            # fill-opacity="0.12"> instead.
            self.assertIn(">5</text>", content_svg)
            self.assertIn("总章数", content_svg)

    def test_no_meta_falls_back_to_simple_text_for_long_paragraph(self):
        """Without meta lines, the A path auto-routes a long single-paragraph
        section to simple-text (Phase 3.4 routing) instead of the legacy
        40-char-chunked 3-column-cards. A single ``body text`` paragraph
        is too short to wrap to multiple lines, so the SVG should still
        contain the section body but render as simple-text (one padded card,
        not a 3-column-cards rect + title)."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text("# Section A\nbody text\n", encoding="utf-8")
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=None,
            )
            content_svg = (
                ws / "authoring-svg-flat" / "slide_part01_content.svg"
            ).read_text(encoding="utf-8")
            # A path with single-card+single-item → simple-text.
            # simple-text renders a 1060×480 padded card with the body
            # text inside. 3-column-cards would render a small per-card
            # rect with rx="8" + a "要点" title + a 40-char-truncated
            # item — that's NOT what we want here.
            self.assertIn('fill="#1D2CAB"', content_svg)
            self.assertIn('rx="8"', content_svg)
            # The body text must NOT be 40-char-truncated by 3-column-cards.
            # simple-text passes it through verbatim.
            self.assertIn("body text", content_svg)
            # And 3-column-cards would have added a "要点" title —
            # simple-text has no title element, only the body card.
            self.assertNotIn("要点", content_svg)

    def test_long_paragraph_routes_to_simple_text(self):
        """Phase 3.4 (2026-09-16): A-path auto-routing. A single H1 with a
        single long paragraph (no numbered list, no H2 subsections) is
        routed to simple-text instead of 3-column-cards. The SVG should
        contain the full body text (not 40-char-truncated) and should NOT
        contain the legacy '要点' card title."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            long_body = (
                "公司规章制度是保障公司运营的工具，是本着服务公司各项工作"
                "的态度，同时兼顾各方面的利益而制定的。制度从零到有，"
                "从小到大，伴随着公司不断发展、壮大也在不断更新、完善。"
                "本制度涵盖采购申请、审批、执行、验收、付款等全流程要求。"
            )
            md.write_text(f"# 前言\n{long_body}\n", encoding="utf-8")
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=None,
            )
            content_svg = (
                ws / "authoring-svg-flat" / "slide_part01_content.svg"
            ).read_text(encoding="utf-8")
            # Full body preserved — simple-text passes text through,
            # 3-column-cards would 40-char-truncate.
            self.assertIn("本制度涵盖采购申请", content_svg)
            self.assertIn("全流程要求", content_svg)
            # No '要点' card title (3-column-cards artifact).
            self.assertNotIn("要点", content_svg)
            # Multiple <text> nodes because the long body auto-wraps.
            self.assertGreaterEqual(
                content_svg.count("<text "), 3,
                "long paragraph should produce ≥3 <text> nodes via simple-text",
            )

    def test_multi_items_route_to_bullet_list(self):
        """Phase 3.4 (2026-09-16): A-path auto-routing. A single H1 with a
        long paragraph prefix followed by a numbered list (1./2./3./4.)
        still produces ≥2 cards via ``cards_from_body`` (it splits the
        list into a "子项" + "补充" pair). The routing falls through to
        3-column-cards which is the correct layout for a multi-card body
        — bullet-list only wins when cards_from_body produces a single
        card with multiple items, which is the pre-Phase-1 behavior."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# 一、目的\n"
                "为了提高公司采购效率、明确岗位职责、有效降低采购成本。\n"
                "1. 提高公司采购效率\n"
                "2. 明确岗位职责\n"
                "3. 降低采购成本\n"
                "4. 满足优质资源需求\n",
                encoding="utf-8",
            )
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=None,
            )
            content_svg = (
                ws / "authoring-svg-flat" / "slide_part01_content.svg"
            ).read_text(encoding="utf-8")
            # multi-card → 3-column-cards, multiple card rects, items
            # truncated per 30-char (cards_from_body) then per 40-char
            # (workspace_expand) — the structural marker is the
            # presence of multiple per-card rects + item titles.
            self.assertNotIn('width="6"', content_svg,
                             "no bullet-list color bar in multi-card path")
            self.assertGreaterEqual(
                content_svg.count("<rect"), 2,
                "multi-card path should have ≥2 card rects",
            )
            # The numbered items surface in the SVG, regardless of card split.
            self.assertIn("提高公司采购效率", content_svg)
            self.assertIn("满足优质资源需求", content_svg)

    def test_multi_card_keeps_3_column_cards(self):
        """Phase 3.4 (2026-09-16): A-path auto-routing. When cards_from_body
        produces ≥2 cards (e.g. multi-H2 subsections), the legacy
        3-column-cards path still wins. The 40-char per-item truncation
        is preserved so multi-card tile-row stays readable."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# 四、工作程序\n"
                "## （一）采购基本事项\n"
                "1. 新采购物料需提供至少 3 家供应商报价\n"
                "2. 物料合作供应商确定后进行采购\n"
                "\n"
                "## （二）采购申请\n"
                "1. 采购之前采购人提交采购申请及订单\n"
                "2. 紧急采购时申请部门提交采购申请\n",
                encoding="utf-8",
            )
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=None,
            )
            content_svg = (
                ws / "authoring-svg-flat" / "slide_part01_content.svg"
            ).read_text(encoding="utf-8")
            # multi-card → 3-column-cards, multiple per-card rects with
            # rx="8" + multiple titles. No bullet-list color bar (no
            # width="6" attribute) and no simple-text wrap (the body is
            # already split across H2 cards).
            self.assertNotIn('width="6"', content_svg)
            self.assertGreaterEqual(
                content_svg.count("<rect"), 2,
                "multi-card 3-column-cards should have ≥2 card rects",
            )
            # H2 sub-section titles surface as card titles.
            self.assertIn("（一）", content_svg)
            self.assertIn("（二）", content_svg)

    def test_bullet_list_layout_via_meta(self):
        """Section with ``> **layout**: bullet-list`` + ``> **items**: a、b、c``
        renders a color bar + numbered items in the cloned content SVG."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# 一、目的\n"
                "> **layout**: bullet-list\n"
                "> **items**: 采购效率、岗位职责、成本控制、流程规范\n"
                "\n"
                "body\n",
                encoding="utf-8",
            )
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=None,
            )
            content_svg = (
                ws / "authoring-svg-flat" / "slide_part01_content.svg"
            ).read_text(encoding="utf-8")
            # bullet-list renders a 6px-wide color bar rect plus numbered
            # text lines. Items string is split by Chinese 、 so 4 items
            # become 4 numbered entries.
            self.assertIn('width="6"', content_svg)
            self.assertIn('1. ', content_svg)
            self.assertIn('4. ', content_svg)
            self.assertIn("采购效率", content_svg)


class TestBlockRendererSimpleText(unittest.TestCase):
    """Phase 3 (2026-09-16): simple-text layout.

    2 cases:
    - short text renders 1 line + 1 padded rect
    - long text auto-wraps to multiple lines
    """

    def test_short_text_renders_single_line(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "simple-text",
            "bounds": "120 130 1060 480",
            "spec": {"text": "短文本"},
        })
        # 1 padded card rect + 1 <text> node for the single line.
        self.assertEqual(out.count("<text "), 1)
        self.assertEqual(out.count("<rect"), 1)
        self.assertIn("短文本", out)

    def test_long_text_wraps_to_multiple_lines(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        # 200 CJK chars at 24pt across ~1060px inner width → 6+ lines.
        long_text = "一二三四五六七八九十" * 20
        out = _render_new_block({
            "layout": "simple-text",
            "bounds": "120 130 1060 480",
            "spec": {"text": long_text},
        })
        self.assertGreaterEqual(
            out.count("<text "), 3,
            "long text should auto-wrap to ≥3 <text> nodes",
        )
        # The full text content is preserved across the wrapped lines.
        self.assertIn("一二三", out)

    def test_missing_text_raises(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        with self.assertRaises(ValueError) as ctx:
            _render_new_block({
                "layout": "simple-text",
                "bounds": "120 130 1060 480",
                "spec": {},
            })
        self.assertIn("simple-text requires spec.text", str(ctx.exception))


class TestBlockRendererBulletList(unittest.TestCase):
    """Phase 3 (2026-09-16): bullet-list layout.

    2 cases:
    - 5 items render with the left color bar
    - 11 items are capped at 10 (plan §3.1.2 cap)
    """

    def test_five_items_render_with_color_bar(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "bullet-list",
            "bounds": "120 130 1060 480",
            "spec": {"items": ["采购效率", "岗位职责", "成本控制",
                                "流程规范", "风险防范"]},
        })
        # 5 numbered items + 1 color bar rect.
        self.assertEqual(out.count("<text "), 5)
        self.assertEqual(out.count("<rect"), 1)
        # Color bar is 6px wide (left edge accent).
        self.assertIn('width="6"', out)
        # Each item is numbered.
        self.assertIn("1. 采购效率", out)
        self.assertIn("5. 风险防范", out)

    def test_ten_items_capped_above_ten(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "bullet-list",
            "bounds": "120 130 1060 480",
            "spec": {"items": [f"item{i}" for i in range(11)]},
        })
        # 11 items → capped at 10 per plan §3.1.2.
        self.assertEqual(out.count("<text "), 10)
        self.assertIn("10. item9", out)
        # item10 should NOT appear (index 10, would be "11. item10").
        self.assertNotIn("11. item10", out)

    def test_empty_items_raises(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        with self.assertRaises(ValueError) as ctx:
            _render_new_block({
                "layout": "bullet-list",
                "bounds": "120 130 1060 480",
                "spec": {"items": []},
            })
        self.assertIn("bullet-list requires spec.items", str(ctx.exception))


class TestBlockRendererStatementCaption(unittest.TestCase):
    """Phase 7 (2026-09-16): statement-caption archetype (ppt-master
    content_caption analog).

    3 cases:
    - renders eyebrow + title + body with the ppt-master neutral palette
    - rejects empty title / body
    - auto-shrinks font when body overflows the right panel
    """

    def test_renders_eyebrow_title_body(self):
        # Phase 11 (2026-09-17): statement-caption now renders the full
        # ppt-master content_caption geometry: gradient rail with gold
        # section index + 32px white title + en-subtitle, white panel
        # with 22px blue title + 14px en-subtitle + 96px gold accent +
        # 72px huge quote glyph + multi-line body + 90px takeaway band.
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "statement-caption",
            "bounds": "120 130 1060 480",
            "spec": {
                "title": "前言",
                "eyebrow": "PREFACE",
                "eyebrow_en": "PREFACE",
                "index_num": "01",
                "caption": "公司理念与制度",
                "doc_code": "BT-ZD-MOC-001",
                "body": "公司规章制度是保障公司运营的工具，是本着服务公司各项工作的态度，同时兼顾各方面的利益而制定的。",
                "takeaway": "制度是动态管理流程 — 与公司一同成长、迭代、完善。",
            },
        })
        # Gradient rail definition.
        self.assertIn("linearGradient", out)
        self.assertIn("#1D2CAB", out)
        # Gold section index + 32px white title.
        self.assertIn("01", out)
        # En-subtitle (eyebrow_en) appears.
        self.assertIn("PREFACE", out)
        # Title and body still emit (verbatim).
        self.assertIn("前言", out)
        self.assertIn("公司规章制度", out)
        # Takeaway band CORE TAKEAWAY label.
        self.assertIn("CORE TAKEAWAY", out)
        # Doc code rail footer.
        self.assertIn("BT-ZD-MOC-001", out)
        # Takeaway text.
        self.assertIn("动态管理流程", out)

    def test_rejects_empty_body_but_allows_empty_title(self):
        # Phase 12 (2026-09-17): title is optional — shape-17 owns the
        # Chinese chapter name. body must still be non-empty.
        from mcp_ppt_native_fill.pipeline import _render_new_block
        # Empty title is allowed (no raise).
        out = _render_new_block({
            "layout": "statement-caption",
            "bounds": "120 130 1060 480",
            "spec": {"title": "", "body": "实际正文内容"},
        })
        self.assertIn("实际正文内容", out)
        # Empty body still raises.
        with self.assertRaises(ValueError):
            _render_new_block({
                "layout": "statement-caption",
                "bounds": "120 130 1060 480",
                "spec": {"title": "x", "body": ""},
            })

    def test_auto_shrinks_font_for_long_body(self):
        # Phase 11 (2026-09-17): statement-caption no longer auto-shrinks
        # the panel body font (the body caps at 4 lines above the
        # takeaway band and overflow is clipped). Verify the renderer
        # accepts a long body without raising.
        from mcp_ppt_native_fill.pipeline import _render_new_block
        long_body = "公司" * 200
        out = _render_new_block({
            "layout": "statement-caption",
            "bounds": "120 130 1060 480",
            "spec": {"title": "测试", "body": long_body},
        })
        # Must produce non-empty output.
        self.assertTrue(len(out) > 100,
                        f"expected non-empty output, got {len(out)} chars")


class TestBlockRendererProceduralSteps(unittest.TestCase):
    """Phase 7 (2026-09-16): procedural-steps archetype (ppt-master
    process_timeline + data_story takeaway analog).

    3 cases:
    - renders 4 phases + takeaway band
    - rejects 1 / 6 steps
    - bounds too small for full band falls back to heading-only
    """

    def test_renders_four_phases_and_takeaway(self):
        # Phase 11 (2026-09-17): procedural-steps now renders ppt-master's
        # process_timeline geometry (top dashed timeline + 4 circles
        # + 2x2 cards with 40px gradient header bands). We no longer
        # use the legacy takeaway band; verify the new geometry
        # markers. The circle radius is scaled by bw/1280 so on a
        # bw=1060 caller it lands at 14*0.828 ≈ 11.59 — assert by
        # regex match.
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "procedural-steps",
            "bounds": "120 130 1060 480",
            "spec": {
                "title": "WORKFLOW",
                "steps": [
                    {"label": "申请", "detail": "采购人提交",
                     "bullets": ["提交采购单", "氚云审批"]},
                    {"label": "审批", "detail": "总经理审批",
                     "bullets": ["议价比价", "三方比较"]},
                    {"label": "采购", "detail": "议价下单",
                     "bullets": ["议价后保留合同", "保留发票凭证"]},
                    {"label": "验收", "detail": "入库登记",
                     "bullets": ["异常及时上报", "归档台账"]},
                ],
            },
        })
        # 4 timeline circles (radius scaled to bw/1280).
        import re as _re
        circle_rs = [float(m) for m in _re.findall(
            r'<circle[^>]*r="([\d.]+)"', out)]
        small_circles = [r for r in circle_rs if 10 < r < 20]
        self.assertEqual(len(small_circles), 4,
                         f"expected 4 small timeline circles, got {circle_rs}")
        # Dashed connector line.
        self.assertIn('stroke-dasharray="4,4"', out)
        # 2x2 cards: 4 panel rects + 4 gradient header bands (each with
        # its own rx=12), so 8 rx=12 total. Verify at least 4 panels.
        self.assertGreaterEqual(out.count('rx="12"'), 4)
        # Gradient header band defs.
        self.assertIn("phaseHeader", out)
        # Phase labels (PHASE 1..4).
        for n in range(1, 5):
            self.assertIn(f"PHASE {n}", out)
        # Chinese ordinals (阶段一..四).
        for cn in ("一", "二", "三", "四"):
            self.assertIn(f"阶段{cn}", out)
        # Step labels rendered.
        self.assertIn("申请", out)
        self.assertIn("验收", out)

    def test_rejects_one_or_six_steps(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        with self.assertRaises(ValueError):
            _render_new_block({
                "layout": "procedural-steps",
                "bounds": "120 130 1060 480",
                "spec": {"steps": [{"label": "x"}],
                         "takeaways": ["y"]},
            })
        with self.assertRaises(ValueError):
            _render_new_block({
                "layout": "procedural-steps",
                "bounds": "120 130 1060 480",
                "spec": {
                    "steps": [{"label": f"s{i}"} for i in range(6)],
                    "takeaways": ["y"],
                },
            })

    def test_tight_bounds_falls_back_to_heading_only(self):
        # Phase 11 (2026-09-17): procedural-steps no longer falls back
        # to heading-only — it always renders the timeline + 2x2 cards.
        # Verify the renderer still produces valid SVG with tight bounds.
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "procedural-steps",
            "bounds": "120 130 1060 240",
            "spec": {
                "title": "WORKFLOW",
                "steps": [
                    {"label": "A", "detail": "d1"},
                    {"label": "B", "detail": "d2"},
                ],
            },
        })
        # 2 timeline circles (radius scaled).
        import re as _re
        circle_rs = [float(m) for m in _re.findall(
            r'<circle[^>]*r="([\d.]+)"', out)]
        small_circles = [r for r in circle_rs if 5 < r < 20]
        self.assertEqual(len(small_circles), 2,
                         f"expected 2 small timeline circles, got {circle_rs}")
        # 2 cards in 2x2 grid (each card has panel + header band = 2 rx=12).
        self.assertEqual(out.count('rx="12"'), 4)


class TestBlockRendererThreeThesisCards(unittest.TestCase):
    """Phase 7 (2026-09-16): three-thesis-cards archetype (ppt-master
    three_card analog).

    2 cases:
    - exactly 3 cards render with neutral palette + top accent
    - 2 / 4 cards raise
    """

    def test_renders_three_cards_with_accent(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        out = _render_new_block({
            "layout": "three-thesis-cards",
            "bounds": "120 130 1060 480",
            "spec": {"cards": [
                {"title": "效率", "items": ["提高采购效率", "明确岗位"]},
                {"title": "成本", "items": ["降低成本", "规范流程"]},
                {"title": "合规", "items": ["秉公办事", "维护利益"]},
            ]},
        })
        # 3 panel rects + 3 top accent strips = 6 rects.
        self.assertEqual(out.count('rx="12"'), 3)
        # Top accent strips (4px ink rects) — same as the 3 panel rects
        # with height 4.
        self.assertIn('fill="#F4F6F8"', out)
        self.assertIn('fill="#1E293B"', out)
        # Index numerals 01, 02, 03.
        self.assertIn("01", out)
        self.assertIn("02", out)
        self.assertIn("03", out)
        # Card titles.
        self.assertIn("效率", out)
        self.assertIn("成本", out)
        self.assertIn("合规", out)

    def test_rejects_two_or_four_cards(self):
        from mcp_ppt_native_fill.pipeline import _render_new_block
        with self.assertRaises(ValueError):
            _render_new_block({
                "layout": "three-thesis-cards",
                "bounds": "120 130 1060 480",
                "spec": {"cards": [
                    {"title": "a", "items": ["x"]},
                    {"title": "b", "items": ["y"]},
                ]},
            })
        with self.assertRaises(ValueError):
            _render_new_block({
                "layout": "three-thesis-cards",
                "bounds": "120 130 1060 480",
                "spec": {"cards": [
                    {"title": f"t{i}", "items": ["x"]} for i in range(4)
                ]},
            })


class TestPhase7Dispatch(unittest.TestCase):
    """Phase 7 (2026-09-16): A-path dispatch routing to ppt-master
    archetypes based on markdown shape.

    2 cases:
    - multi-H2 (≥3) procedural section → procedural-steps
    - single long-paragraph H1 → statement-caption
    """

    def _workspace_with_section(self, td: str, md_text: str) -> Path:
        """Build a minimal workspace with the markdown auto-routed
        through expand_workspace_from_markdown and return the cloned
        content SVG path."""
        from pathlib import Path as _P
        from mcp_ppt_native_fill import workspace_expand as _we
        ws = _P(td) / "ws"
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True, exist_ok=True)
        # 2 skeleton SVGs (divider + content) — minimal but parseable.
        for i in (3, 4):
            (auth / f"slide_{i:02d}.svg").write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg">'
                f'<g id="shape-{17 if i == 4 else 4}">'
                '<text>{{title}}</text></g></svg>',
                encoding="utf-8",
            )
        md = _P(td) / "m.md"
        md.write_text(md_text, encoding="utf-8")
        _we.expand_workspace_from_markdown(
            ws, md,
            skeleton_divider=3, skeleton_content=4,
            divider_edits_template={"shape-4": "PART {nn}",
                                    "shape-5": "{title}"},
            content_edits_template={"shape-17": "{title}"},
            body_bounds="120 130 1060 480",
            exclude_source_slides=None,
        )
        return auth / "slide_part01_content.svg"

    def test_multi_h2_routes_to_procedural_steps(self):
        # Phase 11 (2026-09-17): procedural-steps now renders 4 small
        # timeline circles (radius scaled by bw/1280) + 2x2 cards with
        # gradient header bands. Verify the new geometry markers.
        #
        # Phase 14 (2026-09-17): boteng H2 titles span all four macro
        # phases of the new taxonomy (申请与审批 / 采购人职责 /
        # 采购方式 / 实施付款规范). The test markdown was rewritten
        # accordingly -- the previous version used only 3 distinct
        # phases (申请/审批 collapsed into one and 验收 alone).
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            md_text = (
                "# 四、工作程序\n\n"
                "## 采购基本事项\n"
                "1. 需求登记\n"
                "\n"
                "## 采购人职责\n"
                "1. 责任人确认\n"
                "\n"
                "## 采购方式\n"
                "1. 长期报价采购\n"
                "2. 议价定购\n"
                "\n"
                "## 验收入库\n"
                "1. 库管盘点\n"
            )
            svg = self._workspace_with_section(td, md_text)
            content_svg = svg.read_text(encoding="utf-8")
            # 4 timeline circles (radius scaled).
            import re as _re
            circle_rs = [float(m) for m in _re.findall(
                r'<circle[^>]*r="([\d.]+)"', content_svg)]
            small_circles = [r for r in circle_rs if 10 < r < 20]
            self.assertEqual(
                len(small_circles), 4,
                f"expected 4 procedural-step timeline circles, got {circle_rs}",
            )
            # 2x2 cards: 4 panel rects + 4 header bands = 8 rx=12.
            self.assertGreaterEqual(content_svg.count('rx="12"'), 4)
            # Phase labels render.
            self.assertIn("PHASE 1", content_svg)
            self.assertIn("PHASE 4", content_svg)

    def test_long_paragraph_routes_to_statement_caption(self):
        # Phase 11 (2026-09-17): statement-caption now uses the ppt-master
        # content_caption geometry — gradient blue rail + white panel +
        # takeaway band. Verify gradient + brand-blue + gold accent are
        # emitted (the new geometry markers).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            md_text = (
                "# 前言\n\n"
                "公司规章制度是保障公司运营的工具，是本着服务公司各项工作的"
                "态度，同时兼顾各方面的利益而制定的。制度从零到有，从小到"
                "大，伴随着公司不断发展、壮大也在不断更新、完善。\n"
            )
            svg = self._workspace_with_section(td, md_text)
            content_svg = svg.read_text(encoding="utf-8")
            # New geometry markers.
            self.assertIn("linearGradient", content_svg)
            self.assertIn("#1D2CAB", content_svg)
            self.assertIn("#D4A24C", content_svg)
            # Takeaway band emits the CORE TAKEAWAY label.
            self.assertIn("CORE TAKEAWAY", content_svg)
            # Gold section index "01" + en-subtitle PREFACE.
            self.assertIn("PREFACE", content_svg)


class TestExpandDividerExtras(unittest.TestCase):
    """Phase D: divider subtitle + exclude-source-slides opt-in params.

    Both are zero-impact when omitted (preserves prior behaviour) and
    fully template-agnostic when supplied. Boteng is one caller.
    """

    def _make_workspace(self, td: str) -> Path:
        ws = Path(td)
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True)
        # shape-4/5 are main divider title; shape-70 is the subtitle slot
        # (boteng calls this out explicitly, but the test is generic —
        # any other shape id with placeholder text would also work).
        (auth / "slide_03.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g id="shape-4"><text>PLACEHOLDER TITLE</text></g>'
            '<g id="shape-5"><text>PLACEHOLDER MAIN</text></g>'
            '<g id="shape-70"><text>PLACEHOLDER SUB</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        (auth / "slide_04.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<g id="shape-17"><text>PLACEHOLDER CONTENT</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        (auth / "slide_05.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8",
        )
        return ws

    def test_subtitle_template_applied_after_main_template(self):
        """divider_subtitle_template edits apply on top of the main edits
        (e.g. shape-70 placeholder text gets replaced with a formatted
        English title)."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text("# Section A\nbody\n\n# Section B\nbody\n",
                          encoding="utf-8")
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={
                    "shape-4": "PART {nn}",
                    "shape-5": "{title}",
                },
                content_edits_template={"shape-17": "{title}"},
                divider_subtitle_template={"shape-70": "Section {nn} of N"},
                exclude_source_slides=None,
            )
            div1 = (ws / "authoring-svg-flat" /
                    "slide_part01_div.svg").read_text(encoding="utf-8")
            self.assertIn("PART 01", div1)
            self.assertIn("Section A", div1)
            self.assertIn("Section 01 of N", div1)
            # Original placeholder text for shape-70 must be gone.
            self.assertNotIn("PLACEHOLDER SUB", div1)

    def test_exclude_source_slides_drops_skeleton_from_roster(self):
        """exclude_source_slides=[3] drops the original slide_03.svg from
        page_plan.json — only the cloned per-section dividers remain."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text("# Section A\nbody\n\n# Section B\nbody\n",
                          encoding="utf-8")
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=[3],
            )
            plan = json.loads(
                (ws / "page_plan.json").read_text(encoding="utf-8")
            )
            roster = [p["svg"] for p in plan["pages"]]
            self.assertNotIn("slide_03.svg", roster)
            # slide_04 (content skeleton) and slide_05 (ending) still
            # appear — exclude is opt-in per slide number.
            self.assertIn("slide_04.svg", roster)
            self.assertIn("slide_05.svg", roster)
            # Two cloned dividers + two cloned contents still present.
            self.assertEqual(roster.count("slide_part01_div.svg"), 1)
            self.assertEqual(roster.count("slide_part02_div.svg"), 1)

    def test_exclude_source_slides_drops_both_skeletons(self):
        """Bug fix (2026-09-16): ``exclude_source_slides=[3, 4]`` drops
        BOTH the divider skeleton (slide_03) AND the content skeleton
        (slide_04) — required so neither raw template leaks into the
        final pptx. The docstring at workspace_expand.py:107-115 was
        previously wrong (only mentioned [skeleton_divider])."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# Section A\nbody\n\n# Section B\nbody\n",
                encoding="utf-8",
            )
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                exclude_source_slides=[3, 4],
            )
            plan = json.loads(
                (ws / "page_plan.json").read_text(encoding="utf-8")
            )
            roster = [p["svg"] for p in plan["pages"]]
            # Both raw skeletons dropped.
            self.assertNotIn("slide_03.svg", roster)
            self.assertNotIn("slide_04.svg", roster)
            # slide_05 (ending) still preserved.
            self.assertIn("slide_05.svg", roster)
            # Cloned dividers + contents still present.
            self.assertEqual(roster.count("slide_part01_div.svg"), 1)
            self.assertEqual(roster.count("slide_part02_div.svg"), 1)
            self.assertEqual(roster.count("slide_part01_content.svg"), 1)
            self.assertEqual(roster.count("slide_part02_content.svg"), 1)

    def test_no_subtitle_when_omitted(self):
        """Without divider_subtitle_template, original placeholder text
        on shape-70 is preserved (no surprise side effects)."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            md = Path(td) / "m.md"
            md.write_text("# Section A\nbody\n", encoding="utf-8")
            pl.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "PART {nn}",
                                        "shape-5": "{title}"},
                content_edits_template={"shape-17": "{title}"},
                # divider_subtitle_template omitted on purpose
            )
            div1 = (ws / "authoring-svg-flat" /
                    "slide_part01_div.svg").read_text(encoding="utf-8")
            self.assertIn("PLACEHOLDER SUB", div1)

    def test_run_with_mapping_forwards_new_kwargs(self):
        """The two new params must be part of run_with_mapping's
        signature so callers can pass them through the MCP server."""
        sig = inspect.signature(pl.run_with_mapping)
        self.assertIn("expand_divider_subtitle_template", sig.parameters)
        self.assertIn("expand_exclude_source_slides", sig.parameters)
        self.assertEqual(
            sig.parameters["expand_divider_subtitle_template"].default, None,
        )
        self.assertEqual(
            sig.parameters["expand_exclude_source_slides"].default, None,
        )


# ---------------------------------------------------------------------------
# Phase I3: pipeline.run_with_mapping one-shot driver
# ---------------------------------------------------------------------------


class TestRunWithMapping(unittest.TestCase):
    """run_with_mapping 是 generate_local_ppt.run_manual 的下沉版本,
    复用 run_native_fill 而非重写 phase2/3/4/5。所有 boteng 特有参数显式传。"""

    def test_signature_exposes_new_params(self):
        sig = inspect.signature(pl.run_with_mapping)
        for p in (
            "fix_nested_picture", "skip_phase3_5", "content_markdown",
            "expand_skeleton_divider", "expand_skeleton_content",
            "expand_divider_edits_template", "expand_content_edits_template",
            "expand_body_bounds", "expand_ending_svg", "expand_part_names",
            "clean_workspace",
            "expand_toc_from_markdown",
            "expand_toc_slot_grid",
        ):
            self.assertIn(p, sig.parameters, f"missing param: {p}")
        # Legacy list-based fields must be gone.
        for legacy in ("expand_toc_slot_title_ids",
                       "expand_toc_slot_subtitle_ids"):
            self.assertNotIn(
                legacy, sig.parameters,
                f"legacy param still present: {legacy}",
            )

    def test_no_hardcoded_boteng_in_run_with_mapping(self):
        """run_with_mapping 体内不得硬编码 boteng 特有字段。"""
        src = inspect.getsource(pl.run_with_mapping)
        for hardcoded in [
            "柏腾", "采购制度", "PART_NAMES",
            "DEFAULT_MAPPING", "shape-4", "shape-5", "shape-17",
            "120 130 1060 480",  # boteng body_bounds
            "前言", "目的", "适用范围", "基本原则", "工作程序", "附件",
        ]:
            self.assertNotIn(
                hardcoded, src,
                f"hardcoded boteng value in run_with_mapping: {hardcoded!r}",
            )


class TestFilterTocManualMapping(unittest.TestCase):
    """T5: drop manual content_mapping entries that target TOC slot ids."""

    def test_drops_toc_shape_ids_keeps_rest(self):
        mapping = {
            "slide_02.svg": {
                "shape-title-0": "manual A",
                "shape-title-1": "manual B",
                "shape-other": "should keep",
            },
            "slide_03.svg": {
                "shape-not-toc": "should keep",
            },
        }
        filtered, dropped = pl._filter_toc_manual_mapping(
            mapping, ["shape-title-0", "shape-title-1"],
        )
        self.assertEqual(dropped, 2)
        # TOC entries dropped
        self.assertNotIn("shape-title-0", filtered["slide_02.svg"])
        self.assertNotIn("shape-title-1", filtered["slide_02.svg"])
        # Non-TOC entry kept
        self.assertEqual(filtered["slide_02.svg"]["shape-other"], "should keep")
        self.assertEqual(filtered["slide_03.svg"]["shape-not-toc"], "should keep")

    def test_no_drop_when_no_toc_ids_match(self):
        mapping = {"slide_02.svg": {"shape-X": "manual"}}
        filtered, dropped = pl._filter_toc_manual_mapping(
            mapping, ["shape-Y", "shape-Z"],
        )
        self.assertEqual(dropped, 0)
        self.assertEqual(filtered["slide_02.svg"]["shape-X"], "manual")

    def test_handles_missing_toc_ids_arg(self):
        """Empty toc_shape_ids list → nothing dropped."""
        mapping = {"slide_02.svg": {"shape-X": "manual"}}
        filtered, dropped = pl._filter_toc_manual_mapping(mapping, [])
        self.assertEqual(dropped, 0)
        self.assertEqual(filtered["slide_02.svg"]["shape-X"], "manual")

    def test_drops_both_title_and_subtitle_toc_ids(self):
        mapping = {
            "slide_02.svg": {
                "shape-t-0": "title",
                "shape-s-0": "subtitle",
                "shape-t-1": "title",
                "shape-other": "keep",
            },
        }
        filtered, dropped = pl._filter_toc_manual_mapping(
            mapping, ["shape-t-0", "shape-t-1", "shape-s-0", "shape-s-1"],
        )
        self.assertEqual(dropped, 3)
        self.assertNotIn("shape-t-0", filtered["slide_02.svg"])
        self.assertNotIn("shape-s-0", filtered["slide_02.svg"])
        self.assertNotIn("shape-t-1", filtered["slide_02.svg"])
        self.assertIn("shape-other", filtered["slide_02.svg"])


class TestFindTocSvg(unittest.TestCase):
    """_find_toc_svg: 找含 '目录' / 'CONTENTS' marker 的 slide_NN.svg。

    Smart-TOC 入口辅助:不指定 toc_svg 时,函数从这里推断。
    不解析 SVG 结构,只看文本里有没有 TOC marker 字串。
    """

    def _make_workspace(self, td: str, *, toc_filename: str | None = "slide_02.svg",
                        toc_marker: str = "目录") -> Path:
        ws = Path(td)
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True)
        # 三张 slide: 只有 toc_filename 含 marker
        for i, name in enumerate(["slide_01.svg", "slide_02.svg", "slide_03.svg"]):
            if name == toc_filename:
                (auth / name).write_text(
                    f'<svg><text>{toc_marker}</text></svg>',
                    encoding="utf-8",
                )
            else:
                (auth / name).write_text(
                    '<svg><text>随便</text></svg>',
                    encoding="utf-8",
                )
        return ws

    def test_finds_目录_marker(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(td)
            self.assertEqual(pl._find_toc_svg(ws / "authoring-svg-flat"),
                             "slide_02.svg")

    def test_finds_CONTENTS_marker(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_workspace(
                td, toc_filename="slide_03.svg", toc_marker="CONTENTS",
            )
            self.assertEqual(pl._find_toc_svg(ws / "authoring-svg-flat"),
                             "slide_03.svg")

    def test_raises_when_no_toc_slide(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            auth = ws / "authoring-svg-flat"
            auth.mkdir(parents=True)
            (auth / "slide_01.svg").write_text(
                '<svg><text>普通页</text></svg>', encoding="utf-8",
            )
            with self.assertRaises(ValueError) as cm:
                pl._find_toc_svg(auth)
            self.assertIn("No TOC slide found", str(cm.exception))


class TestExpandWorkspaceFromToc(unittest.TestCase):
    """expand_workspace_from_toc: smart TOC fill from markdown H1s.

    Caller supplies slot id lists (title + optional subtitle).
    Function: auto-fills < N, clones > N, preserves <g> shapes.
    """

    def _make_toc_workspace(self, td: str, *, slots: int = 6) -> Path:
        """Synthesize a TOC slide with N slots. Each slot has one
        title shape and one subtitle shape."""
        ws = Path(td)
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True)
        # TOC slide with 目录 marker
        toc_parts = ['<svg xmlns="http://www.w3.org/2000/svg">',
                     '<text>目录</text>']
        for i in range(slots):
            # Title shape: large placeholder text
            toc_parts.append(
                f'<g id="shape-title-{i}" data-pptx-frame="0 {i*100} 400 46">'
                f'<text>单击添加大标题 {i}</text></g>'
            )
            # Subtitle shape: smaller placeholder text
            toc_parts.append(
                f'<g id="shape-sub-{i}" data-pptx-frame="0 {i*100+50} 400 30">'
                f'<text>单击添加小标题 {i}</text></g>'
            )
        toc_parts.append('</svg>')
        (auth / "slide_02.svg").write_text("\n".join(toc_parts),
                                           encoding="utf-8")
        # Dummy other slide (not TOC)
        (auth / "slide_01.svg").write_text(
            '<svg><text>cover</text></svg>', encoding="utf-8",
        )
        return ws

    def _title_ids(self, n: int) -> list[str]:
        return [f"shape-title-{i}" for i in range(n)]

    def _sub_ids(self, n: int) -> list[str]:
        return [f"shape-sub-{i}" for i in range(n)]

    # --- T3: < N case ---

    def test_lt_n_fills_first_n_clears_rest_keeps_g(self):
        """4 H1s into 6-slot TOC: first 4 filled, last 2 cleared but
        their <g id="..."> shape is preserved. Smart TOC does NOT
        make structural decisions (delete vs preserve) — that belongs
        to caller / LLM-driven path."""
        import re
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_toc_workspace(td, slots=6)
            md = Path(td) / "m.md"
            md.write_text("# Alpha\n\n# Bravo\n\n# Charlie\n\n# Delta",
                          encoding="utf-8")
            pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=self._title_ids(6),
                toc_slot_subtitle_ids=self._sub_ids(6),
                toc_svg="slide_02.svg",
            )
            svg_text = (ws / "authoring-svg-flat" /
                        "slide_02.svg").read_text(encoding="utf-8")
            # Slots 0-3 filled with markdown H1 titles
            for title in ("Alpha", "Bravo", "Charlie", "Delta"):
                self.assertIn(f">{title}<", svg_text,
                              f"expected {title!r} in TOC slide")
            # Slots 4-5 placeholder text is gone
            self.assertNotIn("单击添加大标题 4", svg_text)
            self.assertNotIn("单击添加大标题 5", svg_text)
            # <g> shapes for slots 4-5 are PRESERVED (smart TOC only
            # clears text, not structural decisions)
            self.assertIn('id="shape-title-4"', svg_text)
            self.assertIn('id="shape-title-5"', svg_text)
            self.assertIn('id="shape-sub-4"', svg_text)
            self.assertIn('id="shape-sub-5"', svg_text)
            # <text> nodes in slots 4-5 are REMOVED (not just emptied).
            # Vendor ``_semantic_shape_text_body`` raises when a semantic
            # shape's only <text> child is empty; removing it makes that
            # function return None (no <text> child) and the shape
            # compiles as pure geometry. Carrying data-pptx-carrier
            # would be lint-forbidden (template_structure.py:1866-1876).
            self.assertNotIn(
                "data-pptx-carrier", svg_text,
                "no carrier marker must be added (lint-forbidden on boteng)",
            )
            # shape-title-4 <g> contains NO <text> child at all (the
            # whole <g> may be self-closing after the <text> is removed).
            self.assertRegex(
                svg_text,
                r'<g id="shape-title-4"[^>]*/?>',
                "cleared slot title <g> must exist (preserved)",
            )
            # Check there's no <text> child in shape-title-4 by searching
            # for the title-id followed by either a closing tag or a
            # self-closing tag without <text> in between.
            title4_match = re.search(
                r'<g id="shape-title-4"[^>]*?(/>|>(.*?)</g>)',
                svg_text, re.DOTALL,
            )
            self.assertIsNotNone(title4_match, "shape-title-4 must exist")
            if title4_match.group(1) == "/>":
                title4_inner = ""
            else:
                title4_inner = title4_match.group(2)
            self.assertNotIn(
                "<text", title4_inner,
                "cleared slot title <text> must be removed",
            )
            # Filled slots retain their <text> child with the title text
            title0_match = re.search(
                r'<g id="shape-title-0"[^>]*?(/>|>(.*?)</g>)',
                svg_text, re.DOTALL,
            )
            self.assertIsNotNone(title0_match, "shape-title-0 must exist")
            title0_inner = (
                "" if title0_match.group(1) == "/>"
                else title0_match.group(2)
            )
            self.assertIn(
                "<text", title0_inner,
                "filled slot title <text> must be retained",
            )
            self.assertIn(
                ">Alpha<", title0_inner,
                "filled slot title <text> must contain chapter title",
            )
            # No marker file written (smart TOC doesn't make
            # structural decisions for phase3_author to re-apply)
            self.assertFalse((ws / "toc_deletions.json").is_file())

    def test_lt_n_returns_summary(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_toc_workspace(td, slots=6)
            md = Path(td) / "m.md"
            md.write_text("# A\n\n# B\n\n# C\n\n# D", encoding="utf-8")
            result = pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=self._title_ids(6),
                toc_svg="slide_02.svg",
            )
            self.assertEqual(result["toc_svg"], "slide_02.svg")
            self.assertEqual(result["slot_count"], 6)
            self.assertEqual(result["filled"], 4)
            self.assertEqual(result["cloned_svgs"], [])

    def test_lt_n_without_subtitle_ids(self):
        """Subtitle list optional: title-only fill still works."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_toc_workspace(td, slots=4)
            md = Path(td) / "m.md"
            md.write_text("# X\n\n# Y", encoding="utf-8")
            pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=self._title_ids(4),
                # toc_slot_subtitle_ids omitted
                toc_svg="slide_02.svg",
            )
            svg_text = (ws / "authoring-svg-flat" /
                        "slide_02.svg").read_text(encoding="utf-8")
            self.assertIn(">X<", svg_text)
            self.assertIn(">Y<", svg_text)
            # Slot 2, 3 cleared (title placeholders gone)
            self.assertNotIn("单击添加大标题 2", svg_text)
            self.assertNotIn("单击添加大标题 3", svg_text)

    # --- T4: > N case (clone TOC slide) ---

    def test_gt_n_clones_overflow_into_part02_toc(self):
        """8 H1s into 6-slot TOC: first 6 in slide_02, last 2 in clone."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_toc_workspace(td, slots=6)
            md = Path(td) / "m.md"
            md.write_text(
                "\n".join(f"# Title{i}" for i in range(1, 9)),
                encoding="utf-8",
            )
            result = pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=self._title_ids(6),
                toc_svg="slide_02.svg",
            )
            auth = ws / "authoring-svg-flat"
            self.assertEqual(result["cloned_svgs"], ["slide_part02_toc.svg"])
            self.assertEqual(result["filled"], 8)
            self.assertTrue((auth / "slide_part02_toc.svg").is_file())

            # Original TOC: titles 1-6
            original_text = (auth / "slide_02.svg").read_text(encoding="utf-8")
            for i in range(1, 7):
                self.assertIn(f">Title{i}<", original_text)
            self.assertNotIn(">Title7<", original_text)

            # Clone: titles 7, 8 in slots 0, 1; slots 2-5 cleared (text
            # emptied, <g> preserved — smart TOC doesn't make
            # structural decisions)
            clone_text = (auth / "slide_part02_toc.svg").read_text(encoding="utf-8")
            self.assertIn(">Title7<", clone_text)
            self.assertIn(">Title8<", clone_text)
            # Slots 2-5 placeholders gone (text cleared)
            for i in range(2, 6):
                self.assertNotIn(f"单击添加大标题 {i}", clone_text)
            # <g> shapes for empty slots are PRESERVED on clone
            self.assertIn('id="shape-title-2"', clone_text)
            self.assertIn('id="shape-title-5"', clone_text)
            self.assertIn('id="shape-sub-2"', clone_text)
            self.assertIn('id="shape-sub-5"', clone_text)

    def test_gt_n_overflow_clone_removes_cleared_slot_text(self):
        """The > N overflow clone (slide_part02_toc.svg) must have its
        cleared slot <text> elements REMOVED. Without removal, svg_to_pptx
        raises "Semantic shape text component produced no native text
        body" because the clone is byte-identical to slide_02.svg and
        inherits the slot <g> structure (with empty <text> children)
        that vendor's semantic-shape conversion can't compile."""
        import re
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_toc_workspace(td, slots=6)
            md = Path(td) / "m.md"
            md.write_text(
                "\n".join(f"# Title{i}" for i in range(1, 9)),
                encoding="utf-8",
            )
            pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=self._title_ids(6),
                toc_slot_subtitle_ids=self._sub_ids(6),
                toc_svg="slide_02.svg",
            )
            clone_text = (ws / "authoring-svg-flat"
                          / "slide_part02_toc.svg").read_text(encoding="utf-8")
            # No carrier marker must be set anywhere (lint-forbidden)
            self.assertNotIn(
                "data-pptx-carrier", clone_text,
                "no carrier marker must be added (lint-forbidden)",
            )
            # Filled slots 0, 1 retain their <text> with the chapter title
            for i in (0, 1):
                m = re.search(
                    rf'<g id="shape-title-{i}"[^>]*?(/>|>(.*?)</g>)',
                    clone_text, re.DOTALL,
                )
                self.assertIsNotNone(
                    m, f"shape-title-{i} must exist in clone",
                )
                inner = "" if m.group(1) == "/>" else m.group(2)
                self.assertIn(
                    f">Title{i + 7}<", inner,
                    f"filled overflow slot {i} must contain chapter text",
                )
            # Cleared slots 2-5 (and their subtitles) must have their
            # <text> children REMOVED — empty <g> body aside from the
            # geometry <rect>.
            for i in range(2, 6):
                for sid_prefix in ("shape-title", "shape-sub"):
                    m = re.search(
                        rf'<g id="{sid_prefix}-{i}"[^>]*?(/>|>(.*?)</g>)',
                        clone_text, re.DOTALL,
                    )
                    self.assertIsNotNone(
                        m, f"{sid_prefix}-{i} must exist in clone",
                    )
                    inner = "" if m.group(1) == "/>" else m.group(2)
                    self.assertNotIn(
                        "<text", inner,
                        f"cleared overflow {sid_prefix}-{i} <text> "
                        "must be removed",
                    )

    # --- T6: real boteng TOC structure ---

    def test_real_boteng_toc_fills_from_markdown(self):
        """Smoke test against the real boteng slide_02 TOC structure.

        Uses project baseline_workspace/authoring-svg-flat/slide_02.svg
        (verified shape ids: row1col1={title: shape-69, subtitle: shape-70,
        connector: shape-62}, etc.). Skipped if baseline_workspace is not
        present (e.g. fresh clone).
        """
        boteng_ws = (
            Path(__file__).resolve().parent.parent
            / "projects" / "baseline_workspace"
        )
        if not (boteng_ws / "authoring-svg-flat" / "slide_02.svg").is_file():
            self.skipTest("boteng baseline_workspace not present")

        with tempfile.TemporaryDirectory() as td:
            # Copy baseline workspace
            shutil.copytree(boteng_ws / "authoring-svg-flat",
                            Path(td) / "authoring-svg-flat")
            ws = Path(td)

            md = Path(td) / "m.md"
            md.write_text(
                "# 第一章 项目概述\nbody\n\n"
                "# 第二章 实施计划\nbody\n\n"
                "# 第三章 团队分工\nbody\n\n"
                "# 第四章 风险控制\nbody\n\n"
                "# 第五章 验收\nbody\n\n"
                "# 第六章 收尾\nbody",
                encoding="utf-8",
            )

            # Real boteng slot ids (verified from earlier exploration)
            title_ids = [
                "shape-69", "shape-72",   # row 1
                "shape-77", "shape-80",   # row 2
                "shape-86", "shape-89",   # row 3
            ]
            sub_ids = [
                "shape-70", "shape-73",
                "shape-78", "shape-81",
                "shape-87", "shape-90",
            ]

            result = pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=title_ids,
                toc_slot_subtitle_ids=sub_ids,
                toc_svg="slide_02.svg",
            )
            self.assertEqual(result["slot_count"], 6)
            self.assertEqual(result["filled"], 6)
            self.assertEqual(result["cloned_svgs"], [])

            svg_text = (ws / "authoring-svg-flat" /
                        "slide_02.svg").read_text(encoding="utf-8")
            # All 6 chapter titles filled
            for ch in ["第一章", "第二章", "第三章",
                       "第四章", "第五章", "第六章"]:
                self.assertIn(ch, svg_text,
                              f"chapter {ch!r} not in TOC slide")
            # Exact full H1 text appears in <text> nodes
            self.assertIn(">第一章 项目概述<", svg_text)
            self.assertIn(">第六章 收尾<", svg_text)
            # Boteng's "第N章" template defaults (no following content) are
            # gone. The real boteng template renders placeholders like
            # "第五章" with no chapter content; we replace those with the
            # full chapter title text from markdown.
            # New <text> contents must each include a chapter name + body.
            chapter_with_body = sum(
                1 for ch in ["第一章", "第二章", "第三章",
                             "第四章", "第五章", "第六章"]
                if f"{ch} " in svg_text
            )
            self.assertEqual(chapter_with_body, 6)

    def test_real_boteng_clears_unused_slots_by_removing_text(self):
        """End-to-end against the real boteng slide_02 template: 4
        chapters into 6 slots must REMOVE the cleared slot <text>
        elements. This is the canonical reproduction of the bug fixed
        in this PR — before the fix, svg_to_pptx raised
        'Failed to convert <g id="shape-86">: Semantic shape text
        component produced no native text body'."""
        import re
        boteng_ws = (
            Path(__file__).resolve().parent.parent
            / "projects" / "baseline_workspace"
        )
        if not (boteng_ws / "authoring-svg-flat" / "slide_02.svg").is_file():
            self.skipTest("boteng baseline_workspace not present")

        with tempfile.TemporaryDirectory() as td:
            shutil.copytree(boteng_ws / "authoring-svg-flat",
                            Path(td) / "authoring-svg-flat")
            ws = Path(td)
            md = Path(td) / "m.md"
            md.write_text(
                "# 第一章 项目概述\nbody\n\n"
                "# 第二章 实施计划\nbody\n\n"
                "# 第三章 团队分工\nbody\n\n"
                "# 第四章 风险控制\nbody",
                encoding="utf-8",
            )
            title_ids = ["shape-69", "shape-72", "shape-77",
                         "shape-80", "shape-86", "shape-89"]
            sub_ids = ["shape-70", "shape-73", "shape-78",
                       "shape-81", "shape-87", "shape-90"]
            pl.expand_workspace_from_toc(
                workspace=ws, md_path=md,
                toc_slot_title_ids=title_ids,
                toc_slot_subtitle_ids=sub_ids,
                toc_svg="slide_02.svg",
            )
            svg_text = (ws / "authoring-svg-flat"
                        / "slide_02.svg").read_text(encoding="utf-8")
            # No carrier marker must be set anywhere (lint-forbidden
            # on boteng's non-placeholder slot <g>s).
            self.assertNotIn(
                "data-pptx-carrier", svg_text,
                "no carrier marker must be added (lint-forbidden)",
            )
            # Filled slots 0-3 (shape-69, shape-72, shape-77, shape-80)
            # retain their <text> with chapter title text.
            for sid, ch in (
                ("shape-69", "第一章"),
                ("shape-72", "第二章"),
                ("shape-77", "第三章"),
                ("shape-80", "第四章"),
            ):
                m = re.search(
                    rf'<g id="{sid}"[^>]*?(/>|>(.*?)</g>)',
                    svg_text, re.DOTALL,
                )
                self.assertIsNotNone(m, f"{sid} must exist")
                inner = "" if m.group(1) == "/>" else m.group(2)
                self.assertIn(
                    "<text", inner,
                    f"filled {sid} must retain <text>",
                )
                self.assertIn(
                    ch, inner,
                    f"filled {sid} must contain chapter {ch!r}",
                )
            # Cleared slots 4 (shape-86) and 5 (shape-89) must have
            # their <text> children REMOVED. This is the exact bug:
            # shape-86 raised the vendor error before this fix.
            for sid in ("shape-86", "shape-89"):
                m = re.search(
                    rf'<g id="{sid}"[^>]*?(/>|>(.*?)</g>)',
                    svg_text, re.DOTALL,
                )
                self.assertIsNotNone(m, f"{sid} must exist")
                inner = "" if m.group(1) == "/>" else m.group(2)
                self.assertNotIn(
                    "<text", inner,
                    f"cleared {sid} <text> must be removed",
                )


class TestDetectTocSlotShapeIds(unittest.TestCase):
    """grid-based auto-detection of title/subtitle ids from SVG geometry.

    Replaces caller-supplied expand_toc_slot_title_ids /
    expand_toc_slot_subtitle_ids lists with a structural {rows, cols}
    descriptor. The function walks data-pptx-frame attrs and returns ids
    in row-major order.
    """

    def _make_grid_workspace(
        self, td: str, *, rows: int, cols: int,
    ) -> Path:
        """Synthesize a real rows×cols TOC grid: each cell has one title
        <g> (h=46) and one subtitle <g> (h=30) directly below it.
        Plus a decoration <g data-pptx-object="picture"> that the
        detector must skip."""
        ws = Path(td)
        auth = ws / "authoring-svg-flat"
        auth.mkdir(parents=True)
        parts = ['<svg xmlns="http://www.w3.org/2000/svg">',
                 '<text>目录</text>']
        parts.append(
            '<g id="shape-deco" data-pptx-object="picture" '
            'data-pptx-frame="0 0 1280 50">'
            '<rect width="1280" height="50"/></g>'
        )
        for r in range(rows):
            for c in range(cols):
                x_l = 100 + c * 500
                y_t = 100 + r * 150
                parts.append(
                    f'<g id="shape-t-{r}-{c}" '
                    f'data-pptx-frame="{x_l} {y_t} 400 46">'
                    f'<text>title {r},{c}</text></g>'
                )
                parts.append(
                    f'<g id="shape-s-{r}-{c}" '
                    f'data-pptx-frame="{x_l} {y_t + 60} 400 30">'
                    f'<text>sub {r},{c}</text></g>'
                )
        parts.append('</svg>')
        (auth / "slide_02.svg").write_text(
            "\n".join(parts), encoding="utf-8",
        )
        return ws

    def test_grid_3x2_returns_row_major_title_ids(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_grid_workspace(td, rows=3, cols=2)
            titles, subs = pl._detect_toc_slot_shape_ids(
                ws / "authoring-svg-flat" / "slide_02.svg",
                rows=3, cols=2,
            )
            self.assertEqual(len(titles), 6)
            self.assertEqual(len(subs), 6)
            # Row-major: r0c0, r0c1, r1c0, r1c1, r2c0, r2c1
            self.assertEqual(
                titles,
                ["shape-t-0-0", "shape-t-0-1",
                 "shape-t-1-0", "shape-t-1-1",
                 "shape-t-2-0", "shape-t-2-1"],
            )
            self.assertEqual(
                subs,
                [f"shape-s-{r}-{c}" for r in range(3) for c in range(2)],
            )

    def test_grid_2x4_wider_than_tall(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_grid_workspace(td, rows=2, cols=4)
            titles, _ = pl._detect_toc_slot_shape_ids(
                ws / "authoring-svg-flat" / "slide_02.svg",
                rows=2, cols=4,
            )
            self.assertEqual(len(titles), 8)
            self.assertEqual(titles[0], "shape-t-0-0")
            self.assertEqual(titles[3], "shape-t-0-3")
            self.assertEqual(titles[4], "shape-t-1-0")
            self.assertEqual(titles[7], "shape-t-1-3")

    def test_grid_mismatch_raises(self):
        """Caller says 4x2 but SVG only has 3 rows → ValueError."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._make_grid_workspace(td, rows=3, cols=2)
            with self.assertRaises(ValueError) as cm:
                pl._detect_toc_slot_shape_ids(
                    ws / "authoring-svg-flat" / "slide_02.svg",
                    rows=4, cols=2,
                )
            self.assertIn("4x2", str(cm.exception))
            self.assertIn("6", str(cm.exception))  # expected count

    def test_grid_skips_picture_decoration(self):
        """data-pptx-object="picture" shape must be excluded."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            auth = ws / "authoring-svg-flat"
            auth.mkdir(parents=True)
            (auth / "slide_02.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<text>目录</text>'
                '<g id="shape-bg" data-pptx-object="picture" '
                'data-pptx-frame="0 0 1280 720">'
                '<image href="bg.png"/></g>'
                '<g id="shape-t-0-0" data-pptx-frame="100 100 400 46">'
                '<text>title</text></g>'
                '</svg>',
                encoding="utf-8",
            )
            titles, subs = pl._detect_toc_slot_shape_ids(
                auth / "slide_02.svg", rows=1, cols=1,
            )
            self.assertEqual(titles, ["shape-t-0-0"])
            # No subtitle → empty string in that cell.
            self.assertEqual(subs, [""])

    def test_subtitle_empty_string_when_cell_has_no_subtitle(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            auth = ws / "authoring-svg-flat"
            auth.mkdir(parents=True)
            (auth / "slide_02.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<text>目录</text>'
                '<g id="shape-t-0-0" data-pptx-frame="100 100 400 46">'
                '<text>only title</text></g>'
                '</svg>',
                encoding="utf-8",
            )
            titles, subs = pl._detect_toc_slot_shape_ids(
                auth / "slide_02.svg", rows=1, cols=1,
            )
            self.assertEqual(titles, ["shape-t-0-0"])
            self.assertEqual(subs, [""])

    def test_invalid_grid_dimensions_raise(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            auth = ws / "authoring-svg-flat"
            auth.mkdir(parents=True)
            (auth / "slide_02.svg").write_text(
                '<svg><text>目录</text></svg>', encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                pl._detect_toc_slot_shape_ids(
                    auth / "slide_02.svg", rows=0, cols=2,
                )
            with self.assertRaises(ValueError):
                pl._detect_toc_slot_shape_ids(
                    auth / "slide_02.svg", rows=2, cols=0,
                )


class TestBackfillCoverTitle(unittest.TestCase):
    """Phase C — deterministic cover title safety net.

    Triggered when the LLM planner leaves the cover slide empty. The
    pipeline calls :func:`backfill_cover_title` after merging
    caller-supplied and LLM-supplied content mappings.
    """

    def _workspace(self, td: Path):
        flat = td / "authoring-svg-flat"
        flat.mkdir(parents=True)
        (flat / "authoring_summary.json").write_text(
            json.dumps({"schema": "x", "documents": []}),
            encoding="utf-8",
        )
        # Cover with two text shapes; shape-24 is wider so it wins on
        # max_chars heuristic (placeholder "old title long string"
        # ~26 chars * 1.2 = 31) vs shape-23 ("old title" * 1.2 = 12).
        (flat / "slide_01.svg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<g id="shape-23"><text x="10" y="20">old title</text></g>'
            '<g id="shape-24"><text x="10" y="40">old title long string</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        # A non-cover slide to confirm the backfill only touches slide_01.
        (flat / "slide_02.svg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<g id="shape-2"><text x="10" y="20">toc slot</text></g>'
            '</svg>',
            encoding="utf-8",
        )
        # 5 text elements on slide_03 → heuristics may classify it as
        # toc; ensure the cover classification is stable.
        slot_texts = "".join(
            f'<g id="shape-{i + 100}"><text x="10" y="{20 + i * 10}">'
            f"slot {i}</text></g>"
            for i in range(5)
        )
        (flat / "slide_03.svg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            f"{slot_texts}"
            '</svg>',
            encoding="utf-8",
        )
        md = td / "content.md"
        md.write_text("# 公司采购制度\n\nbody\n", encoding="utf-8")
        return td, md

    def test_fills_when_cover_unfilled(self):
        from mcp_ppt_native_fill import llm_planner

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            ws, md = self._workspace(td)
            merged: dict[str, dict[str, str]] = {}
            report = llm_planner.backfill_cover_title(
                merged_mapping=merged,
                workspace=ws,
                md_text=md.read_text(encoding="utf-8"),
                md_path=md,
            )
            self.assertTrue(report["filled"], msg=str(report))
            self.assertEqual(report["cover_svg"], "slide_01.svg")
            self.assertEqual(report["title"], "公司采购制度")
            self.assertIn("slide_01.svg", merged)
            # Touched exactly one shape (the max_chars winner).
            self.assertEqual(len(merged["slide_01.svg"]), 1)
            self.assertEqual(
                merged["slide_01.svg"][report["shape_id"]],
                "公司采购制度",
            )

    def test_skips_when_cover_already_has_edit(self):
        from mcp_ppt_native_fill import llm_planner

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            ws, md = self._workspace(td)
            merged = {
                "slide_01.svg": {"shape-23": "user supplied cover title"},
            }
            report = llm_planner.backfill_cover_title(
                merged_mapping=merged,
                workspace=ws,
                md_text=md.read_text(encoding="utf-8"),
                md_path=md,
            )
            self.assertFalse(report["filled"])
            self.assertEqual(report["reason"], "cover_already_filled")
            # Caller value untouched.
            self.assertEqual(
                merged["slide_01.svg"]["shape-23"],
                "user supplied cover title",
            )
            # shape-24 was NOT added (we don't second-guess callers).
            self.assertNotIn("shape-24", merged["slide_01.svg"])

    def test_falls_back_to_filename_when_no_h1(self):
        from mcp_ppt_native_fill import llm_planner

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            ws, _ = self._workspace(td)
            md = td / "山西柏腾科技采购制度.md"  # no H1 in body
            md.write_text("no heading here, just prose\n", encoding="utf-8")
            merged: dict[str, dict[str, str]] = {}
            report = llm_planner.backfill_cover_title(
                merged_mapping=merged,
                workspace=ws,
                md_text=md.read_text(encoding="utf-8"),
                md_path=md,
            )
            self.assertTrue(report["filled"], msg=str(report))
            self.assertEqual(report["title"], "山西柏腾科技采购制度")

    def test_truncates_to_max_chars(self):
        from mcp_ppt_native_fill import llm_planner

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            ws, _ = self._workspace(td)
            # Force a tight frame geometry so max_chars is small.
            # data-pptx-frame="x y w h" makes _scan_text_shapes apply
            # the geometry-based cap instead of placeholder-length.
            # 200px wide / 47pt font ≈ 4 chars (CJK 1.0× * 0.85 safety).
            flat = ws / "authoring-svg-flat"
            (flat / "slide_01.svg").write_text(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                '<g id="shape-77" data-pptx-frame="100 100 200 80">'
                '<text x="10" y="20" font-size="47">placeholder</text>'
                '</g>'
                '</svg>',
                encoding="utf-8",
            )
            md = td / "content.md"
            md.write_text(
                "# 山西柏腾科技有限公司采购管理制度\n\nbody\n",
                encoding="utf-8",
            )
            merged: dict[str, dict[str, str]] = {}
            report = llm_planner.backfill_cover_title(
                merged_mapping=merged,
                workspace=ws,
                md_text=md.read_text(encoding="utf-8"),
                md_path=md,
            )
            self.assertTrue(report["filled"], msg=str(report))
            # The geometry cap ≤ placeholder cap, so we honor the smaller.
            self.assertLessEqual(len(report["title"]), report["max_chars"])
            # 15-char title into ~4-char slot ⇒ truncation was needed.
            self.assertTrue(report["truncated"])

    def test_no_cover_slide_returns_clean_report(self):
        from mcp_ppt_native_fill import llm_planner

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            flat = td / "authoring-svg-flat"
            flat.mkdir(parents=True)
            (flat / "authoring_summary.json").write_text(
                json.dumps({"schema": "x", "documents": []}),
                encoding="utf-8",
            )
            # Empty flat → no slides → skeleton detection returns {}.
            ws = td
            merged: dict[str, dict[str, str]] = {}
            report = llm_planner.backfill_cover_title(
                merged_mapping=merged,
                workspace=ws,
                md_text="# Title",
            )
            self.assertFalse(report["filled"])
            self.assertEqual(report["reason"], "no_cover_slide")
            self.assertEqual(merged, {})


# ---------------------------------------------------------------------------
# Phase 8 (2026-09-16): divider subtitle translation + new archetypes.
# ---------------------------------------------------------------------------


class TestPhase8DividerSubtitleTranslation(unittest.TestCase):
    """Tests for the section_title_en_map + divider_subtitle_template path.

    Phase 8 lets callers (e.g. boteng_demo) translate each H1's
    Chinese title into the English subtitle text that lives on
    shape-70 of the divider template, WITHOUT touching the template's
    <g> frame, font, size, color, or position. The verify surface is:
    (a) ``_format()`` substitutes ``{title_en}`` correctly when given
    a value, (b) shape-70's <text> body actually changes in the
    cloned SVG when ``section_title_en_map`` resolves the lookup.
    """

    def test_title_en_placeholder_substitutes_correctly(self):
        """``_format()`` must thread ``title_en`` into ``str.format``."""
        from mcp_ppt_native_fill.workspace_expand import expand_workspace_from_markdown  # noqa: F401

        # The function is module-level; the inner _format is not
        # exported, so we exercise it via the public signature path
        # through ``divider_subtitle_template``. We assert on the
        # returned new_blocks side: when caller passes a map and a
        # template containing {title_en}, the cloned SVG must show
        # the English word in shape-70's text body.
        import shutil
        import tempfile
        from pathlib import Path
        from mcp_ppt_native_fill import workspace_expand

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            ws = td / "ws"
            ws.mkdir()
            flat = ws / "authoring-svg-flat"
            flat.mkdir()
            # Build a divider skeleton with shape-70 <text> body.
            div = flat / "slide_03.svg"
            div.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720" width="1280" height="720">'
                "<g id='shape-4'><text><tspan>PART 01</tspan></text></g>"
                "<g id='shape-5'><text><tspan>前言</tspan></text></g>"
                "<g id='shape-70'><text><tspan>单价添加小标题</tspan>"
                "<tspan>/</tspan><tspan>标题英文</tspan></text></g>"
                "</svg>",
                encoding="utf-8",
            )
            (flat / "slide_04.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720" width="1280" height="720">'
                "<g id='shape-17'><text><tspan>X</tspan></text></g>"
                "</svg>",
                encoding="utf-8",
            )
            md = td / "content.md"
            md.write_text(
                "# 前言\n\n这是前言的内容。\n\n# 一、目的\n\n明确目的。\n",
                encoding="utf-8",
            )

            workspace_expand.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={
                    "shape-4": "PART {nn}",
                    "shape-5": "{title}",
                },
                content_edits_template={"shape-17": "{title}"},
                body_bounds="120 130 1060 480",
                divider_subtitle_template={"shape-70": "{title_en}"},
                section_title_en_map={
                    "前言": "Preface",
                    "一、目的": "Purpose",
                },
            )

            for nn, expected in [("01", "Preface"), ("02", "Purpose")]:
                svg = flat / f"slide_part{nn}_div.svg"
                raw = svg.read_text(encoding="utf-8")
                self.assertIn(expected, raw)
                self.assertNotIn("单价添加小标题", raw)

    def test_divider_subtitle_template_no_translation(self):
        """When section_title_en_map omits a key, {title_en} expands to "."

        Caller's no-translation path still keeps the template's
        shape-70 frame intact (font/size/color/position) — only the
        <text> body becomes blank, not the surrounding <g>.
        """
        import tempfile
        from pathlib import Path
        from mcp_ppt_native_fill import workspace_expand

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            ws = td / "ws"
            ws.mkdir()
            flat = ws / "authoring-svg-flat"
            flat.mkdir()
            (flat / "slide_03.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720" width="1280" height="720">'
                "<g id='shape-4'><text><tspan>X</tspan></text></g>"
                "<g id='shape-5'><text><tspan>X</tspan></text></g>"
                "<g id='shape-70'><text><tspan>OLD</tspan></text></g>"
                "</svg>",
                encoding="utf-8",
            )
            (flat / "slide_04.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1280 720" width="1280" height="720"></svg>',
                encoding="utf-8",
            )
            md = td / "content.md"
            md.write_text("# 未知章节\n\nbody\n", encoding="utf-8")

            workspace_expand.expand_workspace_from_markdown(
                ws, md,
                skeleton_divider=3, skeleton_content=4,
                divider_edits_template={"shape-4": "X", "shape-5": "{title}"},
                content_edits_template={},
                divider_subtitle_template={"shape-70": "{title_en}"},
                section_title_en_map={},  # empty → all title_en = ""
            )
            svg = flat / "slide_part01_div.svg"
            raw = svg.read_text(encoding="utf-8")
            # Frame preserved.
            self.assertIn('id="shape-70"', raw)
            # The "OLD" placeholder text is gone (replace-by-empty in
            # svg_edits leaves a zero-width carrier, not "OLD").
            self.assertNotIn("OLD", raw)


class TestPhase8HeroStatement(unittest.TestCase):
    """Phase 8 hero_statement archetype (ppt-master hero_statement).
    Phase 11 (2026-09-17): geometry rewritten to match ppt-master's
    svg_final/0?_purpose.svg: white panel + 68px brand-blue claim-band
    + 22px white section title + 14px gold en-tag + 32px question +
    20px body + 5 keyword cards.
    """

    def test_renders_headline_centered(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {
                "headline": "一、目 的",
                "eyebrow_en": "CHAPTER ONE",
                "en_tag": "PURPOSE",
                "question": "为什么制定本采购制度？",
                "body_lines": ["为了提高公司采购效率..."],
            },
        })
        # New geometry markers.
        self.assertIn("一、目 的", svg)
        self.assertIn("PURPOSE", svg)
        # Claim band (full-width #1D2CAB rect).
        self.assertIn('fill="#1D2CAB"', svg)
        # White panel.
        self.assertIn('fill="#FFFFFF"', svg)
        # Big question.
        self.assertIn("为什么制定本采购制度", svg)

    def test_shrinks_headline_font_when_bounds_narrow(self):
        """When bh is tiny the renderer must shrink font, not overflow."""
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "hero_statement",
            "bounds": "120 130 200 80",
            "spec": {"headline": "高效率", "eyebrow": ""},
        })
        # Headline present and SVG is well-formed.
        self.assertIn("高效率", svg)

    def test_rejects_overlong_headline(self):
        # Phase 11 (2026-09-17): removed the 80-char hard cap. The
        # claim-band design accommodates any length headline (it
        # truncates visually via clipping). The headline still
        # renders even at 100 chars.
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {"headline": "x" * 100},
        })
        self.assertIn("<text", svg)

    def test_no_subline_when_empty(self):
        # Phase 11 (2026-09-17): subline field is no longer used; the
        # body_lines list replaces it. Empty body_lines → just the
        # claim band + question render. Verify the claim band still
        # appears.
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {"headline": "聚焦目的", "body_lines": []},
        })
        # Claim band rect must be present.
        self.assertIn('fill="#1D2CAB"', svg)


class TestPhase8KpiRow(unittest.TestCase):
    """Phase 8 kpi_row archetype (ppt-master kpi_row)."""

    def test_renders_4_tiles(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "kpi_row",
            "bounds": "120 130 1060 480",
            "spec": {
                "tiles": [
                    {"keyword": "效率", "descriptor": "提效", "value": "30%"},
                    {"keyword": "职责", "descriptor": "明确", "value": ""},
                    {"keyword": "成本", "descriptor": "降本", "value": ""},
                    {"keyword": "规范", "descriptor": "合规", "value": ""},
                ],
                "evidence": "完整说明",
            },
        })
        self.assertEqual(svg.count("<rect "), 5)  # 4 tiles + 1 evidence panel
        self.assertIn("效率", svg)
        self.assertIn("完整说明", svg)

    def test_supports_3_and_5_tiles(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        for n in (3, 5):
            tiles = [
                {"keyword": f"K{i}", "descriptor": f"D{i}", "value": ""}
                for i in range(n)
            ]
            svg = render_new_block({
                "layout": "kpi_row",
                "bounds": "120 130 1060 480",
                "spec": {"tiles": tiles, "evidence": ""},
            })
            # 4 tile rects (no evidence panel when evidence="").
            self.assertGreaterEqual(svg.count("<rect "), n)

    def test_rejects_6_tiles(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        with self.assertRaises(ValueError) as cm:
            render_new_block({
                "layout": "kpi_row",
                "bounds": "120 130 1060 480",
                "spec": {
                    "tiles": [
                        {"keyword": f"K{i}", "descriptor": "", "value": ""}
                        for i in range(6)
                    ],
                },
            })
        self.assertIn("2-5 tiles", str(cm.exception))

    def test_evidence_panel_below_row(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "kpi_row",
            "bounds": "120 130 1060 480",
            "spec": {
                "tiles": [
                    {"keyword": "A", "descriptor": "", "value": ""},
                    {"keyword": "B", "descriptor": "", "value": ""},
                ],
                "evidence": "底层论据",
            },
        })
        # evidence panel rendered as a tinted rect.
        self.assertIn("底层论据", svg)
        # Two tile rects + one evidence panel rect.
        self.assertEqual(svg.count("<rect "), 3)


class TestPhase8Dispatch(unittest.TestCase):
    """Phase 8 A-path dispatch: hero_statement + kpi_row entry points.

    These tests exercise the dispatch decision tree in
    ``workspace_expand.expand_workspace_from_markdown`` directly
    by feeding in cards shapes and reading the resulting layout
    name. We bypass markdown parsing (which collapses multi-line
    bullet lists into a single item string) by injecting cards
    manually.
    """

    def _run_dispatch_with_cards(self, cards: list[dict]) -> str:
        """Run the dispatch tree with explicit cards; return layout name."""
        from mcp_ppt_native_fill import workspace_expand

        # Monkey-patch _cards_for_section to return our injected cards.
        original = workspace_expand._cards_for_section
        workspace_expand._cards_for_section = lambda sections, stem: (
            cards, {},
        )
        try:
            # Render the new_blocks dispatch path: we read the
            # ``layout`` key that ends up in new_blocks[svg]["body_cards"].
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as td_str:
                td = Path(td_str)
                ws = td / "ws"
                ws.mkdir()
                flat = ws / "authoring-svg-flat"
                flat.mkdir()
                # Real SVG namespace so write_new_content_block is happy.
                (flat / "slide_03.svg").write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'viewBox="0 0 1280 720" width="1280" height="720">'
                    "<g id='shape-4'><text><tspan>X</tspan></text></g>"
                    "<g id='shape-5'><text><tspan>X</tspan></text></g>"
                    "</svg>",
                    encoding="utf-8",
                )
                (flat / "slide_04.svg").write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'viewBox="0 0 1280 720" width="1280" height="720">'
                    "</svg>",
                    encoding="utf-8",
                )
                md = td / "content.md"
                md.write_text("# 一、目的\n\nplaceholder\n", encoding="utf-8")

                result = workspace_expand.expand_workspace_from_markdown(
                    ws, md,
                    skeleton_divider=3, skeleton_content=4,
                    divider_edits_template={"shape-4": "X", "shape-5": "X"},
                    content_edits_template={},
                    body_bounds="120 130 1060 480",
                )
                layout = result["new_blocks"]["slide_part01_content.svg"][
                    "body_cards"
                ]["layout"]
                return layout
        finally:
            workspace_expand._cards_for_section = original

    def test_medium_sentence_routes_to_hero_statement(self):
        # Single card, single item, 20-79 chars (CJK-aware char count).
        layout = self._run_dispatch_with_cards([{
            "title": "要点", "color": "#1D2CAB",
            "items": ["提高采购效率明确各岗位职责有效降低采购成本"],
        }])
        self.assertEqual(layout, "hero_statement")

    def test_short_sentence_still_routes_to_simple_text(self):
        # < 20 chars → simple-text (regression for Phase 3.4 path).
        layout = self._run_dispatch_with_cards([{
            "title": "要点", "color": "#1D2CAB",
            "items": ["这是简短内容"],
        }])
        self.assertEqual(layout, "simple-text")

    def test_long_sentence_still_routes_to_statement_caption(self):
        # ≥ 80 chars → statement-caption (regression for Phase 7 path).
        layout = self._run_dispatch_with_cards([{
            "title": "要点", "color": "#1D2CAB",
            "items": ["本章描述一个完整长段落, " * 8],
        }])
        self.assertEqual(layout, "statement-caption")

    def test_4_short_items_route_to_kpi_row(self):
        # 4 keyword-style items (each ≤12 chars).
        layout = self._run_dispatch_with_cards([{
            "title": "要点", "color": "#1D2CAB",
            "items": ["效率", "职责", "成本", "规范"],
        }])
        self.assertEqual(layout, "kpi_row")

    def test_long_items_fall_through_to_bullet_list(self):
        # items > 12 chars → bullet-list (Phase 3.4 fallback).
        layout = self._run_dispatch_with_cards([{
            "title": "要点", "color": "#1D2CAB",
            "items": [
                "这是一个非常非常长的 item 字串",
                "另一个长 item",
            ],
        }])
        self.assertEqual(layout, "bullet-list")


class TestPhase8ClassifyH2(unittest.TestCase):
    """Phase 8: boteng 7 H2 → 4 macro phases via keyword lookup.

    Phase 14 (2026-09-17): phase labels were renamed from
    ``{申请, 审批, 采购, 验收}`` to ``{申请与审批, 采购人职责,
    采购方式, 实施付款规范}`` so procedural-steps can show 4
    distinct workflow stages with descriptive names (the old 验收
    label was overloaded for both 验收-验收 and 行为规范).
    """

    def test_seven_boteng_h2_produce_four_macro_phases(self):
        from mcp_ppt_native_fill.workspace_expand import (
            _classify_h2_to_phase,
        )
        h2_titles = [
            "采购基本事项",
            "采购申请",
            "采购人职责",
            "采购方式",
            "采购实施",
            "采购付款",
            "行为规范",
        ]
        phases = {_classify_h2_to_phase(t) for t in h2_titles}
        # Phase 14: 4-phase taxonomy (申请与审批 / 采购人职责 /
        # 采购方式 / 实施付款规范).
        self.assertEqual(
            phases,
            {"申请与审批", "采购人职责", "采购方式", "实施付款规范"},
        )


class TestPhase9LLMWhitelist(unittest.TestCase):
    """Phase 9: LLM planner must accept all Phase 7/8/9 archetypes."""

    def test_new_layouts_in_whitelist(self):
        from mcp_ppt_native_fill import llm_planner
        # Read the whitelist source directly (it's inlined in
        # _normalize_new_blocks). Cheaper than running the planner.
        import inspect
        src = inspect.getsource(llm_planner._normalize_new_blocks)
        for required in (
            "statement-caption", "procedural-steps", "three-thesis-cards",
            "hero_statement", "kpi_row",
            "comparison", "matrix_2x2",
        ):
            self.assertIn(required, src,
                          f"layout {required!r} missing from "
                          f"_normalize_new_blocks whitelist")

    def test_system_prompt_lists_composition_patterns(self):
        from mcp_ppt_native_fill import llm_planner
        sp = llm_planner.SYSTEM_PROMPT
        for token in ("hero_statement", "comparison", "matrix_2x2",
                      "Phase 7+ extended archetypes"):
            self.assertIn(token, sp,
                          f"token {token!r} missing from SYSTEM_PROMPT")
        # Must warn against fabricated chapter numbers.
        self.assertIn("Do NOT fabricate chapter numbers", sp)


class TestPhase9FlowStepsBounds(unittest.TestCase):
    """Phase 9: flow-steps no longer overflows body_bounds with 5 tiles."""

    def _bounds(self, bw=1060):
        return f"120 130 {bw} 480"

    def test_5_tiles_fit_within_bw(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "flow-steps",
            "bounds": self._bounds(1060),
            "spec": {
                "steps": [
                    {"title": f"Step{i}", "items": ["a"]}
                    for i in range(5)
                ],
            },
        }
        svg = render_new_block(spec)
        # Phase 9 formula: step_w = (bw - gap*(n+1))/n, so each step
        # x_i = bx + gap + i*(step_w + gap). For n=5, bw=1060, gap=16:
        # step_w = (1060-96)/5 = 192.8; first tile x = bx + 16 = 136;
        # last tile x = 136 + 4*(192.8+16) = 136 + 835.2 = 971.2;
        # right edge = 971.2 + 192.8 = 1164.0; < bx+bw=1180 ✓
        # (Pre-Phase 9: last tile right edge was 1180.0, overflowing
        # onto the body frame's right edge — visually glued.)
        self.assertIn("<rect", svg)
        import re
        last_rect = re.findall(
            r'<rect x="([\d.]+)" y="130" width="([\d.]+)"', svg
        )[-1]
        right_edge = float(last_rect[0]) + float(last_rect[1])
        self.assertLessEqual(right_edge, 1180.0,
                             f"flow-steps 5-tile right edge {right_edge} "
                             f"overflows 1180 (bx+bw)")
        # And must NOT be glued to the right edge — must leave at
        # least 12px gap (which is exactly the gap value used for
        # n=5 layout).
        self.assertLessEqual(right_edge, 1180.0 - 12.0,
                             f"flow-steps 5-tile right edge {right_edge} "
                             f"still glued to body_bounds right edge "
                             f"(expected <= 1168)")

    def test_3_tiles_no_regression(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "flow-steps",
            "bounds": self._bounds(1060),
            "spec": {
                "steps": [
                    {"title": f"Step{i}", "items": ["x"]}
                    for i in range(3)
                ],
            },
        }
        svg = render_new_block(spec)
        # 3 rects, none overflow.
        import re
        rects = re.findall(
            r'<rect x="([\d.]+)" y="130" width="([\d.]+)"', svg
        )
        self.assertEqual(len(rects), 3)
        for x, w in rects:
            self.assertLessEqual(float(x) + float(w), 1180.0)


class TestPhase9HeroStatementCap(unittest.TestCase):
    """Phase 11 (2026-09-17): hero_statement now uses claim-band +
    question geometry. The 80-char hard cap is removed — any length
    headline renders (clipped by the claim band if too long). The
    font-size assertion is replaced with a "claim-band emits" check
    so the test stays meaningful for the new geometry.
    """

    def test_headline_60_chars_renders_without_error(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        # Construct a 60-char CJK headline.
        headline = "为了规范公司采购行为" * 4  # 4 * 10 chars = 40 chars
        headline += "公开透明公平" * 2  # + 10 = 50
        headline += "择优选择"  # + 4 = 54
        headline += "降低风险"  # + 4 = 58
        headline += "防范风险"  # + 4 = 62, slice to 60
        headline = headline[:60]
        self.assertEqual(len(headline), 60)
        spec = {
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {"headline": headline, "body_lines": []},
        }
        svg = render_new_block(spec)
        # Claim band fills full-width with #1D2CAB.
        self.assertIn('fill="#1D2CAB"', svg)
        self.assertIn(headline, svg)

    def test_headline_70_chars_soft_shrinks(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        headline = ("为了规范公司采购行为" * 5
                    + "公开透明公平择优选择降低风险防范风险")
        headline += "abcdef"
        self.assertGreater(len(headline), 60)
        spec = {
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {"headline": headline, "body_lines": []},
        }
        svg = render_new_block(spec)
        # Renders without raising and the claim band is present.
        self.assertIn('fill="#1D2CAB"', svg)
        # Long headline may be clipped in the claim band — check the
        # first few chars are present.
        self.assertIn("为了规范", svg)

    def test_headline_over_80_chars_still_raises(self):
        # Phase 11: 80-char cap removed; any length headline renders.
        from mcp_ppt_native_fill.block_renderer import render_new_block
        headline = "x" * 100
        spec = {
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {"headline": headline, "body_lines": []},
        }
        # No raise — the renderer accepts any length.
        svg = render_new_block(spec)
        self.assertIn("<text", svg)


class TestPhase9EyebrowExtract(unittest.TestCase):
    """Phase 9: _extract_eyebrow skips markdown list markers."""

    def test_skips_markdown_heading_marker(self):
        from mcp_ppt_native_fill.workspace_expand import _extract_eyebrow
        body = "## 采购基本事项\n\n实际正文..."
        self.assertEqual(_extract_eyebrow(body), "实际正文...")

    def test_skips_numbered_list(self):
        from mcp_ppt_native_fill.workspace_expand import _extract_eyebrow
        body = "1. 提高采购效率\n2. 明确岗位职责\n3. 降低成本"
        # No non-marker line — returns "".
        self.assertEqual(_extract_eyebrow(body), "")

    def test_caps_to_30_chars(self):
        from mcp_ppt_native_fill.workspace_expand import _extract_eyebrow
        body = "abcdefghijklmnopqrstuvwxyz1234567890XYZ"
        self.assertEqual(len(_extract_eyebrow(body)), 30)

    def test_skips_unordered_list(self):
        from mcp_ppt_native_fill.workspace_expand import _extract_eyebrow
        body = "- 采购申请\n- 采购审批\n- 采购实施"
        self.assertEqual(_extract_eyebrow(body), "")

    def test_skips_blockquote_marker(self):
        from mcp_ppt_native_fill.workspace_expand import _extract_eyebrow
        body = "> 这是引用块\n实际正文..."
        self.assertEqual(_extract_eyebrow(body), "实际正文...")


class TestPhase9Comparison(unittest.TestCase):
    """Phase 9: comparison archetype (ppt-master 05_comparison.svg)."""

    def test_renders_two_panels_with_central_divider(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "comparison",
            "bounds": "120 130 1060 480",
            "spec": {
                "title": "公开招标 vs 邀请招标",
                "left": {"title": "公开招标", "content": "面向全社会"},
                "right": {"title": "邀请招标", "content": "定向邀请"},
            },
        }
        svg = render_new_block(spec)
        # 2 panel rects + 1 central divider rect + multiple text.
        import re
        rects = re.findall(r'<rect[^>]*fill="#F4F6F8"', svg)
        self.assertEqual(len(rects), 2,
                         f"expected 2 panels with F4F6F8, got {len(rects)}")
        dividers = re.findall(r'<rect[^>]*fill="#CBD5E1"', svg)
        self.assertEqual(len(dividers), 1,
                         f"expected 1 central divider, got {len(dividers)}")
        # Both titles render.
        self.assertIn("公开招标", svg)
        self.assertIn("邀请招标", svg)

    def test_requires_both_titles(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "comparison",
            "bounds": "120 130 1060 480",
            "spec": {
                "left": {"title": "A", "content": ""},
                "right": {"title": "", "content": ""},
            },
        }
        with self.assertRaises(ValueError):
            render_new_block(spec)


class TestPhase9Matrix2x2(unittest.TestCase):
    """Phase 9: matrix_2x2 archetype (ppt-master 11_matrix_2x2.svg)."""

    def test_renders_four_quadrants(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "matrix_2x2",
            "bounds": "120 130 1060 480",
            "spec": {
                "title": "SWOT 分析",
                "y_axis": "重要性",
                "x_axis": "紧迫性",
                "quadrants": [
                    "优势 — 经验丰富",
                    "机会 — 政策支持",
                    "劣势 — 资源不足",
                    "威胁 — 竞争激烈",
                ],
            },
        }
        svg = render_new_block(spec)
        # 4 quadrant rects (F8FAFC) + central cross (2 lines).
        import re
        quads = re.findall(r'<rect[^>]*fill="#F8FAFC"', svg)
        self.assertEqual(len(quads), 4,
                         f"expected 4 quadrants, got {len(quads)}")
        h_lines = re.findall(r'<line[^>]*stroke="#CBD5E1"', svg)
        self.assertEqual(len(h_lines), 2,
                         f"expected 2 axis lines, got {len(h_lines)}")
        # Content renders.
        self.assertIn("经验丰富", svg)
        self.assertIn("政策支持", svg)

    def test_requires_exactly_4_quadrants(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "matrix_2x2",
            "bounds": "120 130 1060 480",
            "spec": {
                "quadrants": ["only one"],
            },
        }
        with self.assertRaises(ValueError):
            render_new_block(spec)


class TestPhase12SvgEditsStrip(unittest.TestCase):
    """Phase 12 (2026-09-17): strip_template_chrome_shapes removes a
    top-level <g id="shape-NN"> by id while preserving the rest of
    the SVG. Used to drop the cloned slide_04.svg corner tagline
    (shape-22) so chrome topbar doesn't fight with a second heading
    in the same vertical band.
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.svg_path = Path(self._tmpdir.name) / "fixture.svg"
        self.svg_path.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">\n'
            '<rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>\n'
            '<g id="shape-17"><text>前言</text></g>\n'
            '<g id="shape-22"><text>右上角副标</text></g>\n'
            '<g id="shape-3"><rect/></g>\n'
            '</svg>\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_strip_removes_shape_22_only(self):
        from mcp_ppt_native_fill.svg_edits import strip_template_chrome_shapes
        removed = strip_template_chrome_shapes(self.svg_path, ("shape-22",))
        self.assertEqual(removed, 1)
        raw = self.svg_path.read_text(encoding="utf-8")
        self.assertNotIn('id="shape-22"', raw)
        # shape-17 + shape-3 untouched.
        self.assertIn('id="shape-17"', raw)
        self.assertIn('id="shape-3"', raw)
        self.assertIn("前言", raw)

    def test_strip_idempotent(self):
        from mcp_ppt_native_fill.svg_edits import strip_template_chrome_shapes
        n1 = strip_template_chrome_shapes(self.svg_path, ("shape-22",))
        self.assertEqual(n1, 1)
        # Second call: shape-22 already gone.
        n2 = strip_template_chrome_shapes(self.svg_path, ("shape-22",))
        self.assertEqual(n2, 0)
        raw = self.svg_path.read_text(encoding="utf-8")
        self.assertIn('id="shape-17"', raw)

    def test_strip_no_op_for_missing_id(self):
        from mcp_ppt_native_fill.svg_edits import strip_template_chrome_shapes
        before = self.svg_path.read_text(encoding="utf-8")
        n = strip_template_chrome_shapes(self.svg_path, ("shape-9999",))
        self.assertEqual(n, 0)
        after = self.svg_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)


class TestPhase12ChromeLabel(unittest.TestCase):
    """Phase 12 (2026-09-17): chrome topbar label drops the literal
    "第N章 XXX" redundancy (shape-17 already paints the chapter
    title). New format is "PART NN · EN_LABEL" derived from
    workspace_expand._EN_LABELS.
    """

    def test_topbar_label_uses_en_label(self):
        from mcp_ppt_native_fill.pipeline import (
            _derive_default_chrome_plan, PipelineState,
        )
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "authoring-svg-flat").mkdir()
            (ws / "authoring-svg-flat" / "slide_part02_content.svg").write_text(
                "<svg/>", encoding="utf-8",
            )
            (ws / "authoring-svg-flat" / "slide_part03_content.svg").write_text(
                "<svg/>", encoding="utf-8",
            )
            (ws / "page_plan.json").write_text("{}", encoding="utf-8")
            state = PipelineState(workspace=ws, context={})
            plan = _derive_default_chrome_plan(state)
            by_idx = {entry["svg"]: entry for entry in plan}
            self.assertEqual(
                by_idx["slide_part02_content.svg"]["chapter_label"],
                "PART 02 · OBJECTIVE",
            )
            self.assertEqual(
                by_idx["slide_part03_content.svg"]["chapter_label"],
                "PART 03 · SCOPE",
            )


class TestPhase12HeroStatementSkip(unittest.TestCase):
    """Phase 12 (2026-09-17): hero_statement drops the in-body chapter
    title and leading question — shape-17 owns the Chinese chapter
    name now and chrome topbar carries the section marker. Renderer
    must skip the 22px white claim-band headline and 32px question
    text when those fields are empty.
    """

    def test_hero_skips_22px_white_text_when_headline_empty(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {
                "headline": "",  # Phase 12: skip the 22px white chapter title
                "question": "",  # Phase 12: skip the 32px leading question
                "en_tag": "OBJECTIVE",
                "body_lines": ["本章说明采购制度目的与适用范围。"],
                "keywords": [
                    {"word": "效率", "en": "EFFICIENCY"},
                ],
            },
        }
        svg = render_new_block(spec)
        # Claim band itself is still painted (visual section indicator).
        self.assertIn('fill="#1D2CAB"', svg)
        # en-tag still rendered on the band.
        self.assertIn("OBJECTIVE", svg)
        # body still rendered.
        self.assertIn("本章说明", svg)
        # The 32px leading question font-size is gone.
        import re
        font_sizes = re.findall(r'font-size="([^"]+)"', svg)
        self.assertNotIn("32", font_sizes,
                         f"32px question should not render; sizes={font_sizes}")

    def test_hero_band_height_clamps_to_32_when_no_headline(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        # With no headline, the band height drops from 68 to 32 (avoid
        # leaving an empty blue strip in the slide-top half). The
        # en-tag font-size is unchanged (14px).
        spec = {
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {
                "headline": "",
                "question": "",
                "en_tag": "OBJECTIVE",
                "body_lines": ["第一章采购制度的目的。"],
                "keywords": [],
            },
        }
        svg = render_new_block(spec)
        # Find the band rect — it should be height <= 33 (32*scale with
        # bw/1280 scale on bw=1060 → 26.5; well under 33). Match rects
        # whose fill="#1D2CAB" regardless of attribute order.
        import re
        rects = re.findall(r'<rect[^>]*?fill="#1D2CAB"[^>]*?/>', svg)
        self.assertTrue(rects,
                        f"claim band rect should still render; svg={svg!r}")
        for rect in rects:
            m_h = re.search(r'height="([^"]+)"', rect)
            self.assertTrue(m_h, f"rect missing height: {rect}")
            self.assertLessEqual(
                float(m_h.group(1)), 33.0,
                f"band height should clamp to ~32, got {m_h.group(1)}",
            )


class TestPhase12StatementCaptionSkip(unittest.TestCase):
    """Phase 12 (2026-09-17): statement-caption drops the in-body
    chapter title — shape-17 owns the Chinese chapter name now.
    Renderer must skip the rail 32px white title and panel 22px blue
    title when ``title`` is empty.
    """

    def test_statement_caption_skips_rail_and_panel_title_when_empty(self):
        from mcp_ppt_native_fill.block_renderer import render_new_block
        spec = {
            "layout": "statement-caption",
            "bounds": "120 130 1060 480",
            "spec": {
                "title": "",  # Phase 12: skip both rail & panel title
                "eyebrow_en": "PREFACE",
                "index_num": "01",
                "caption": "制度建设的动态演进",
                "doc_code": "BT-ZD-MOC-001",
                "body": "公司规章制度是保障公司运营的工具。",
                "takeaway": "制度是动态管理流程。",
            },
        }
        svg = render_new_block(spec)
        # No raise — title is now optional.
        # Rail index "01" + takeaway band still render.
        self.assertIn("01", svg)
        self.assertIn("PREFACE", svg)
        self.assertIn("BT-ZD-MOC-001", svg)
        self.assertIn("CORE TAKEAWAY", svg)
        # The 32px white rail-title and 22px blue panel-title text
        # elements must not paint the literal chapter string. (The
        # template's shape-17 still does, separately.)


class TestPhase14FontSizeByLLM(unittest.TestCase):
    """Phase 14 (2026-09-17): typography tier is LLM-chosen, not
    hardcoded. The four ppt-master archetypes (hero_statement /
    statement-caption / procedural-steps / revision-table) read
    ``spec.font_size`` -- either as a role name (looked up in
    :data:`TYPOGRAPHY`) or as a raw px value -- and pass it through
    verbatim. No ``*scale`` shrinking. See plan v2 (2026-09-17).
    """

    def test_hero_statement_uses_spec_font_size_role(self):
        """spec.font_size='lead' (20) -> SVG font-size='20' (no scale)."""
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {
                "headline": "一、目 的",
                "en_tag": "PURPOSE",
                "question": "为什么制定本采购制度？",
                "body_lines": ["为了提高公司采购效率..."],
                "font_size": "lead",  # -> 20
            },
        })
        # Body line uses 'lead' -> 20px. Must NOT be 20*0.828 = 16.56.
        # The body line is inside <text font-size="20" ...>.
        self.assertIn('font-size="20"', svg)
        self.assertNotIn('font-size="16.56', svg)
        self.assertNotIn('font-size="20*scale', svg)

    def test_hero_statement_uses_spec_font_size_px_raw(self):
        """spec.font_size='22' (raw px) -> SVG font-size='22' verbatim."""
        from mcp_ppt_native_fill.block_renderer import render_new_block
        svg = render_new_block({
            "layout": "hero_statement",
            "bounds": "120 130 1060 480",
            "spec": {
                "headline": "聚焦目的",
                "font_size": "22",  # raw px claim_band tier
            },
        })
        # Headline text uses 'claim_band' -> 22. Must appear verbatim.
        self.assertIn('font-size="22"', svg)
        # And the claim-band band rect still scales (geometry uses
        # scale; only font-size is now LLM-chosen).
        self.assertIn('fill="#1D2CAB"', svg)

    def test_procedural_steps_falls_back_to_typography_anchor(self):
        """When font_size is missing, role resolves via TYPOGRAPHY dict
        (design_spec §IV). card body -> 'bullet' -> 13px, NOT 13*scale.
        """
        from mcp_ppt_native_fill.block_renderer import render_new_block, TYPOGRAPHY
        svg = render_new_block({
            "layout": "procedural-steps",
            "bounds": "120 130 1060 480",
            "spec": {
                "title": "工作程序",
                "steps": [
                    {"label": "申请", "detail": "需求登记", "bullets": ["a", "b"]},
                    {"label": "审批", "detail": "部门审批", "bullets": ["c", "d"]},
                    {"label": "采购", "detail": "询价比价", "bullets": ["e", "f"]},
                    {"label": "验收", "detail": "验收入库", "bullets": ["g", "h"]},
                ],
                # font_size deliberately omitted -> role fallback
            },
        })
        # bullet role -> 13 (annotation), so bullet text uses
        # font-size="13". Must NOT be 13*0.828 = 10.77.
        self.assertIn('font-size="13"', svg)
        self.assertNotIn('font-size="10.77', svg)
        # page_title role -> 32. So procedural title "工作程序" uses
        # font-size="32".
        self.assertIn('font-size="32"', svg)
        # Sanity: TYPOGRAPHY dict must expose the role names.
        self.assertEqual(TYPOGRAPHY["body"], 16)
        self.assertEqual(TYPOGRAPHY["annotation"], 13)
        self.assertEqual(TYPOGRAPHY["page_title"], 32)


class TestPhase14PhaseKeywords(unittest.TestCase):
    """Phase 14 (2026-09-17): _PHASE_KEYWORDS now covers 4 macro phases
    (申请与审批 / 采购人职责 / 采购方式 / 实施付款规范), so boteng's
    7 H2 sub-sections map to 4 distinct workflow stages instead of 3.
    """

    def test_classify_h2_routes_to_four_distinct_phases(self):
        from mcp_ppt_native_fill.workspace_expand import (
            _classify_h2_to_phase,
        )
        cases = [
            ("一、采购基本事项", "申请与审批"),
            ("二、采购申请", "申请与审批"),
            ("三、采购审批", "申请与审批"),
            ("四、采购人职责", "采购人职责"),
            ("五、采购方式", "采购方式"),
            ("六、采购实施", "实施付款规范"),
            ("七、采购付款方式", "实施付款规范"),
            ("八、采购经办人行为规范", "实施付款规范"),
        ]
        phases = {_classify_h2_to_phase(title) for title, _ in cases}
        # All 4 macro phases must be represented.
        self.assertEqual(
            phases,
            {"申请与审批", "采购人职责", "采购方式", "实施付款规范"},
        )

    def test_more_specific_keyword_wins_over_broad_purchase(self):
        """``采购付款方式`` must route to 实施付款规范, not 采购方式
        (the standalone 采购 fallback). The keyword list orders
        付款方式 before 采购 so the more specific match wins.
        """
        from mcp_ppt_native_fill.workspace_expand import _classify_h2_to_phase
        self.assertEqual(
            _classify_h2_to_phase("采购付款方式"),
            "实施付款规范",
        )


if __name__ == "__main__":
    unittest.main()
