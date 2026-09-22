# Phase 26 Plan — Vision Verification + Vendor Compliance Tests

> **Date**: 2026-09-22
> **Status**: Plan mode (no code yet — awaiting user approval)
> **Trigger**: User asked "你分析应该怎么办" after Phase 25 commit 1 (`55c3649`) made end-to-end vision inference work for `template_v2.pptx`. We now have a 5.86 MB PPTX in `projects/柏腾ppt模版2_ws_20260922_133858_out.pptx`, but we never **visually verified** that cover/toc/divider/content actually show the user's content. We also never proved the approach **generalizes** to other templates.
> **Goal**: Verify (visually + structurally) that vision-injected text actually lands on the right slides, and lock in vendor attribute compliance with a regression test so Phase 25 fixes don't slip.

---

## 1. Current state (where Phase 25 left us)

| Asset | Status |
|---|---|
| `_direct_test_final/_direct_testA/` workspace | ✅ vision_layout filled 8 entries (`analysis`, `animations.json`, `authoring-svg-flat`, `authoring-svg-flat_vector_asset_inventory.json`, `icons`, `images`, `sources`, `validation`) |
| `slide_part01_div.svg` | ✅ contains `_vision_title_01` with text "一、工作手册的原则" and `_vision_part_01` with "PART 01" |
| `柏腾ppt模版2_ws_20260922_133858_out.pptx` | ✅ 5.86 MB, `ok=True stage=done` |
| Cover slide (`slide_01.svg`) | ⚠️ NOT verified to contain `{doc_title}` placeholder text |
| TOC slide (`slide_02.svg`) | ⚠️ NOT verified to contain `{section_NN_title}` cards |
| Other 6 content slides | ⚠️ Vision inferred `_vision_body_01` etc. with **empty tspan** — placeholder text missing |

Two visual verification questions:
1. **Did cover and TOC get filled?** The vision_layout pipeline only adds text via `_apply_vision_layouts` for divider/content/ending (workspace_expand.py:299-360). Cover/TOC aren't passed through `_apply_vision_layouts`. So they remain at **vendor-default text** (placeholders like `文本框 X`).
2. **Why are content slides empty?** `_vision_body_01` has `<tspan xml:space="preserve"></tspan>` — the LLM emitted `text: "{body}"` which `_apply_vision_layouts` doesn't have a value for in `fmt_vars`, so `SafeDict.__missing__` returns the literal `{body}` — but then `add_text_block` writes it as text content. The body placeholder needs a real value.

---

## 2. Phase 26 commit 1 — Visual verification

### 2.1 What to render

Render the generated PPTX to PNGs and inspect:

```bash
# Convert .pptx to PDF via vendor (using LibreOffice / vendor's svg_to_pptx preview path)
# Then convert PDF to PNG via cairosvg / pdftoppm
```

Simpler: convert the SVG clones directly to PNG (since we already have `analysis/roundtrip-svg/layered/` + `authoring-svg-flat/`).

```python
# scripts/verify_vision_layouts.py
import cairosvg
from pathlib import Path
WS = Path(r"D:\Code\DSH\native_fill\projects\柏腾ppt模版2_ws_20260922_133858")
af = WS / "authoring-svg-flat"
for svg in sorted(af.glob("slide_part*.svg")) + [af / "slide_01.svg", af / "slide_02.svg", af / "slide_05.svg"]:
    png = svg.with_suffix(".png")
    cairosvg.svg2png(url=str(svg), write_to=str(png), output_width=1280)
    print(f"  {svg.name} → {png.name}")
```

### 2.2 Inspection points (use `read_image` tool to look at each PNG)

For each slide image, verify:

| Slide | Expected content (vision-injected) | What to look for |
|---|---|---|
| `slide_01.svg` (cover) | Vendor-default text | Verify cover renders the template's default `PPT[模板]XXXXXX` style — NOT the LLM-inferred `{doc_title}` placeholder (vision doesn't inject here) |
| `slide_02.svg` (TOC) | Vendor-default 4 cards (01/02/03/04) with "目录章节" placeholder text | Verify TOC cards render with their default placeholder |
| `slide_05.svg` (ending) | Vendor-default ending slide | Verify ending renders |
| `slide_part01_div.svg` | `PART 01` (大) + `一、工作手册的原则` (标题) | ✅ Already verified |
| `slide_part01_content.svg` | `一、工作手册的原则` title + body placeholder | Vision inferred `_vision_body_01` (empty tspan) — body placeholder needs real content |
| `slide_part02_div.svg` | `PART 02` + `二、XX` | Should work via divider_layouts |
| `slide_part03..07_div.svg` | Each `PART NN` + section title | Should work via divider_layouts |

### 2.3 Verdict

After visual inspection, decide:
- **A1**: Cover/TOC need vision injection too → extend `_apply_vision_layouts` to also process `cover_layouts` + `toc_layouts` (currently only divider/content). Possibly add to `workspace_expand.py`.
- **A2**: Content body needs real text content — extract from markdown section body and pass via new `body_texts` param.
- **A3**: All looks good — declare Phase 26 done and move to Phase 27 (generalization).

---

## 3. Phase 26 commit 2 — vendor attribute compliance test

Add regression test so Phase 25 fixes (no `dominant-baseline`) don't slip back:

### 3.1 Test file

`tests/test_native_fill.py`:

```python
class TestSvgEditsVendorCompliance(unittest.TestCase):
    """Phase 26 commit 2: guard against regression on vendor text
    attribute whitelist. ppt-master's svg_to_pptx preflight rejects
    any text attribute outside its closed allowlist
    (scripts/svg_to_pptx/drawingml/text_properties.py:65-86 for
    _TEXT_DIRECT_ATTRIBUTES and line 35-46 for _UNSUPPORTED_TEXT_PROPERTIES).

    Forbidden attrs include: alignment-baseline, direction,
    dominant-baseline, font-kerning, font-feature-settings,
    font-size-adjust, font-stretch, font-synthesis, font-variant,
    font-variation-settings, font, hyphens, etc.
    """

    FORBIDDEN_TEXT_ATTRS = frozenset({
        "alignment-baseline", "direction", "dominant-baseline",
        "font-kerning", "font-feature-settings", "font-size-adjust",
        "font-stretch", "font-synthesis", "font-variant",
        "font-variation-settings", "font", "hyphens",
    })

    def _build_minimal_svg(self, tmp_path: Path) -> Path:
        svg = tmp_path / "slide.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0" y="0" width="100" height="100"/></svg>'
        )
        return svg

    def test_add_text_block_no_forbidden_text_attrs(self):
        from mcp_ppt_native_fill.svg_edits import add_text_block
        import tempfile, re
        with tempfile.TemporaryDirectory() as td:
            svg = self._build_minimal_svg(Path(td))
            add_text_block(
                svg, shape_id="t1", x=10, y=10, w=80, h=20,
                text="hi", font_size=18,
            )
            raw = svg.read_text(encoding="utf-8")
        for f in self.FORBIDDEN_TEXT_ATTRS:
            self.assertNotIn(
                f, raw,
                f"{f!r} appeared in injected <text> — vendor rejects this",
            )

    def test_add_text_block_uses_only_allowed_text_attrs(self):
        """Positive check: every text attribute must be in the vendor
        whitelist (_TEXT_DIRECT_ATTRIBUTES ∪ _TEXT_DECLARATION_PROPERTIES
        ∪ style/data-*)."""
        from mcp_ppt_native_fill.svg_edits import add_text_block
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svg = self._build_minimal_svg(Path(td))
            add_text_block(
                svg, shape_id="t1", x=10, y=10, w=80, h=20,
                text="hi",
            )
            raw = svg.read_text(encoding="utf-8")
        # Extract every attribute on every <text> element
        text_attrs = re.findall(r'<text\s+([^/>]+?)(?:/?>)', raw)
        attr_set: set[str] = set()
        for attrs in text_attrs:
            for m in re.findall(r'(\w[\w-]*)\s*=', attrs):
                attr_set.add(m)
        # style and data-* are explicitly allowed (vendor treats them
        # separately from the closed allowlist).
        allowed = frozenset({
            "fill", "fill-opacity", "filter", "font-family", "font-size",
            "font-style", "font-weight", "id", "letter-spacing",
            "opacity", "stroke", "stroke-opacity", "stroke-width",
            "style", "text-anchor", "text-decoration", "transform",
            "x", "xml:space", "y",
            # + data-* namespace (data-pptx-edit-name etc.)
        })
        unexpected = attr_set - allowed - frozenset(
            m for m in attr_set if m.startswith("data-")
        )
        self.assertEqual(
            unexpected, set(),
            f"unexpected text attributes: {unexpected}",
        )

    def test_add_text_block_baseline_offset_centers_vertically(self):
        """baseline_y = y + h/2 + font_size*0.35 — verify the math
        so vision-injected text is visually centered in bounds."""
        from mcp_ppt_native_fill.svg_edits import add_text_block
        import tempfile, re
        with tempfile.TemporaryDirectory() as td:
            svg = self._build_minimal_svg(Path(td))
            add_text_block(
                svg, shape_id="t1",
                x=10, y=20, w=80, h=40, text="hi", font_size=24,
            )
            raw = svg.read_text(encoding="utf-8")
        m = re.search(r'<text\s+x="([\d.]+)"\s+y="([\d.]+)"', raw)
        self.assertIsNotNone(m)
        x, y = float(m.group(1)), float(m.group(2))
        # cy = 20 + 40/2 = 40, baseline_y = 40 + 24*0.35 = 48.4
        self.assertAlmostEqual(x, 50.0)  # cx = 10 + 80/2
        self.assertAlmostEqual(y, 48.4)
```

### 3.2 Test placement

Add as a new class `TestSvgEditsVendorCompliance` in `tests/test_native_fill.py`. Run via `python -m unittest tests.test_native_fill.TestSvgEditsVendorCompliance -v`.

---

## 4. Phase 26 commit 3 — Cover/TOC vision injection (if visual verification confirms gap)

If visual verification (§2.3 A1) confirms cover/TOC lack text:

### 4.1 Cover

`workspace_expand.py` doesn't currently apply any edits to cover (`slide_01.svg`). Two options:

- **Option C1**: Extend `_apply_vision_layouts` to also handle cover via a new `cover_layouts` param. The expanded `text` could be `{doc_title}` / `{doc_subtitle}` — need a `doc_title` mapping from markdown's first H1.
- **Option C2**: Use boteng-style `apply_text_edits` path when template has actual text shapes — vision inferred role → text-shape binding (no inject needed).

### 4.2 TOC

TOC slide has 4 cards (01/02/03/04). Vision inferred `card_NN_title` placeholders. We need to:
- Pass `body` mapping from markdown section body content to `_apply_vision_layouts`
- For each TOC card_NN_title placeholder, substitute `{section_NN_title}` with the actual H1 of section NN

### 4.3 Implementation sketch

```python
# workspace_expand.py — extend _apply_vision_layouts to accept body_texts:
def _apply_vision_layouts(
    svg_path, layouts, *, nn, title, title_en="", body=""
):
    fmt_vars = {..., "body": body, ...}
    # existing logic

# markdown_expand.py — pass body from sections
expansions = workspace_expand.expand_workspace_from_markdown(
    ...,
    cover_layouts=injects.get("cover_layouts"),
    toc_layouts=injects.get("toc_layouts"),
    divider_layouts=injects.get("divider_layouts"),
    content_layouts=injects.get("content_layouts"),
    ending_layouts=injects.get("ending_layouts"),
)

# Inside expand_workspace_from_markdown, after cloning cover (slide_01.svg):
if cover_layouts:
    _apply_vision_layouts(auth / "slide_01.svg", cover_layouts,
                           nn=1, title=doc_title, title_en="")
```

---

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Visual verification reveals cover/TOC still empty | medium | Phase 26 commit 3 fix |
| Vendor attr compliance test catches a future regression in add_text_block | low | CI runs the test on every commit |
| Cover/TOC injection touches too many SVGs (cover doesn't clone per section) | medium | pass `nn=1` (cover/toc are singletons) |
| Vision LLM hallucinates cover/TOC positions and creates ugly output | low | confidence threshold (already in `vision_layout_min_confidence`) |
| `safe_dict` accepts `{section_NN_title}` literally (not substituted) — content mapping should populate it | medium | Phase 26 commit 3 expansion |

---

## 6. Out of scope (Phase 27+)

- Phase 27: vendor SVG `data-pptx-placeholder` attribute emission (avoid needing vision at all)
- Phase 28: vision feedback loop (rerun LLM after first pass with P2 violations)
- Phase 29: LLM orchestration polish (vision → content mapping → polish full chain)

---

## 7. Open questions

1. **Should `_apply_vision_layouts` accept cover/TOC?** Currently only divider/content/ending. Phase 26 commit 3 may extend.
2. **Body content extraction** — from markdown section body? Or just title? Currently `_apply_vision_layouts` receives only `(nn, title, title_en)` from `expand_workspace_from_markdown`. Adding body requires plumbing markdown section body through.
3. **Cover/TOC vision injection** — apply to slide_01.svg (cover) and slide_02.svg (toc) directly, not as clones. The `_apply_vision_layouts` clones happen inside the per-section loops; cover/toc are pre-clones.

---

**Author**: Claude
**Date**: 2026-09-22
**Status**: Plan mode (no code yet) — awaiting user approval
**Reference**: 
- `docs/PHASE24_VISION_LAYOUT_INFERENCE_PLAN.md` (upstream — vision infrastructure)
- `docs/PHASE25_TEXT_POSITIONING_VENDOR_COMPAT_PLAN.md` (just-shipped — vendor text attr whitelist)
- `ppt-master: scripts/svg_to_pptx/drawingml/text_properties.py` (vendor whitelist source of truth)