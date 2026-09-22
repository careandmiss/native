# Phase 25 Plan — SVG Text Positioning Vendor Compatibility

> **Date**: 2026-09-22
> **Status**: Plan mode (no code yet)
> **Trigger**: After running end-to-end vision_layout inference, `svg_to_pptx.py` fails with `SvgNativeConversionError: slide_part01_div.svg: invalid project text property(s): <text> uses unsupported text property 'dominant-baseline'`. Our `add_text_block` helper used `dominant-baseline="middle"` for vertical centering, but ppt-master vendor (`scripts/svg_to_pptx/drawingml/text_properties.py`) lists `dominant-baseline` in `_UNSUPPORTED_TEXT_PROPERTIES` — preflight rejects it.
> **Goal**: Re-engineer `svg_edits.add_text_block` to use ONLY the ppt-master-vendor-allowed text attribute surface, so any vision-injected text block passes `project_text_property_diagnostics` cleanly. Verify by re-running the end-to-end demo with vision enabled (cache hit) and confirming `ok=True stage=done` + non-empty output PPTX.

---

## 1. Vendor text attribute whitelist (verbatim from ppt-master source)

### 1.1 `_TEXT_DIRECT_ATTRIBUTES` (allowed on `<text>`)

Source: `scripts/svg_to_pptx/drawingml/text_properties.py:65-86`

```python
_TEXT_DIRECT_ATTRIBUTES = frozenset({
    'fill', 'fill-opacity', 'filter',
    'font-family', 'font-size', 'font-style', 'font-weight',
    'id', 'letter-spacing', 'opacity',
    'stroke', 'stroke-opacity', 'stroke-width', 'style',
    'text-anchor', 'text-decoration', 'transform',
    'x', 'xml:space', 'y',
})
```

### 1.2 `_TSPAN_DIRECT_ATTRIBUTES` (allowed on `<tspan>`)

Source: `scripts/svg_to_pptx/drawingml/text_properties.py:88-109`

```python
_TSPAN_DIRECT_ATTRIBUTES = frozenset({
    'baseline-shift', 'dx', 'dy',
    'fill', 'fill-opacity', 'font-family', 'font-size',
    'font-style', 'font-weight', 'id', 'letter-spacing',
    'opacity', 'stroke', 'stroke-opacity', 'stroke-width',
    'style', 'text-decoration', 'x', 'xml:space', 'y',
})
```

Note: `tspan` accepts `baseline-shift`, `dx`, `dy` — these are the **correct** vertical-positioning knobs. `text` does not accept `baseline-shift` / `dx` / `dy` directly (vendor would reject), but accepts `transform`.

### 1.3 `_UNSUPPORTED_TEXT_PROPERTIES` (rejected — preflight ERROR)

Source: `scripts/svg_to_pptx/drawingml/text_properties.py:35-46`

```python
_UNSUPPORTED_TEXT_PROPERTIES = frozenset({
    'alignment-baseline', 'direction', 'dominant-baseline',
    'font-kerning', 'font-feature-settings', 'font-size-adjust',
    'font-stretch', 'font-synthesis', 'font-variant',
    'font-variation-settings', 'font', 'hyphens',
    # … 14 entries total
})
```

`dominant-baseline` is in this list. Our current `add_text_block` puts it on `<text>` → ERROR.

### 1.4 How vendor maps SVG `<text x y>` → PPTX text frame

From `scripts/svg_to_pptx/drawingml/converter.py:_require_project_text_properties` (line 490-503):

```python
def _require_project_text_properties(root, svg_path):
    errors = project_text_property_errors(root)
    if not errors: return
    raise SvgNativeConversionError(
        f'{svg_path.name}: invalid project text property(s): ...'
    )
```

→ The preflight runs **before** SVG→PPTX conversion. Any text attribute outside the allowlist aborts the export. There's no "warn and continue" path.

---

## 2. Why current `add_text_block` breaks

Current `svg_edits.py:324-332`:

```python
cx = x + w / 2
cy = y + h / 2
inner = (
    f'<text x="{cx}" y="{cy}" text-anchor="{anchor}" '
    f'dominant-baseline="{alignment_baseline}" '   # ← REJECTED
    f'font-size="{font_size}" font-weight="{font_weight}" '
    f'fill="{fill}" '
    f'data-pptx-edit-name="{shape_id}">'
    f'<tspan xml:space="preserve">{text}</tspan>'
    f"</text>"
)
```

Three problems:

1. `dominant-baseline` is in `_UNSUPPORTED_TEXT_PROPERTIES`. Rejected.
2. `y` is set to `cy` (bounds vertical center). ppt-master maps SVG `<text y>` → PPTX text-frame **top edge**, not baseline. So `y=cy` puts the visual top of the text at bounds center → text appears below center.
3. `_TEXT_DIRECT_ATTRIBUTES` does NOT include `dx`/`dy` on `<text>` (only on `<tspan>`). But the current code puts everything on `<text>`.

---

## 3. Vendor-compliant replacement (designed against allowlist)

### 3.1 Mapping `<text>` → ppt-master allowed attrs

| Goal | Attribute | Allowed on `<text>`? | Notes |
|---|---|---|---|
| Horizontal center | `text-anchor="middle"` | ✓ YES | well-supported |
| Vertical center | `dominant-baseline="middle"` | ✗ NO (unsupported) | must remove |
| Vertical center (alternative) | `y=baseline_y` (no dominant-baseline) | ✓ YES | baseline at `cy + font_size * 0.35` (visual baseline ~70% of cap height) |
| Text styling | `font-family`, `font-size`, `font-weight`, `fill` | ✓ YES | all in allowlist |
| Edit name | `data-pptx-edit-name` | ✓ YES (vendor convention, not validated by allowlist) | |
| Spacing | `letter-spacing`, `word-spacing` (text-only) | ✓ YES | |
| Transform | `transform="translate(...)"` | ✓ YES | |

### 3.2 `_TEXT_DIRECT_ATTRIBUTES` doesn't include `dy` / `baseline-shift` on `<text>`

The ppt-master converter preflight rejects `dy`/`baseline-shift` on `<text>` directly. They must live on `<tspan>` (inside `<text>`). So our centered-text structure becomes:

```xml
<text x="cx" y="baseline_y"
      font-family="…" font-size="…" font-weight="…"
      fill="…" text-anchor="middle"
      data-pptx-edit-name="…">
  <tspan xml:space="preserve">title text</tspan>
</text>
```

Where `baseline_y = y + h / 2 + font_size * 0.35`. The 0.35 empirical factor compensates for the visual gap between baseline and geometric center.

### 3.3 What `dy`/`baseline-shift` on `<tspan>` can additionally do

If we want pixel-precise baseline tuning beyond the 0.35 factor, we can add `<tspan dy="N">` where N is a small px value. But the 0.35 factor is already close enough for most template-v2 sizes (16–60pt). Keep `dy` off `<tspan>` for now.

---

## 4. Implementation Plan

### Phase 25 commit 1 — fix `add_text_block` to vendor compliance

**File**: `src/mcp_ppt_native_fill/svg_edits.py`

**Changes**:

1. Drop `dominant-baseline="middle"` from the inner `<text>` tag.
2. Drop the unused `alignment_baseline` parameter from the public API (or keep for caller-API compat but ignore — pick "keep for API compat" to avoid breaking the `_apply_vision_layouts` call).
3. Compute `baseline_y = y + h / 2 + font_size * 0.35` for vertical centering.
4. Verify the resulting `<text>` carries only attrs from `_TEXT_DIRECT_ATTRIBUTES`.

**Diff sketch**:

```python
def add_text_block(
    svg_path, *, shape_id,
    x, y, w, h, text,
    font_size=48, font_weight="bold", fill="#000000",
    anchor="middle", alignment_baseline="middle",  # kept for compat, ignored
):
    cx = x + w / 2
    # Visual baseline at ~70% of cap height below geometric center.
    # (No 'dominant-baseline' attribute — vendor rejects it.)
    baseline_y = y + h / 2 + font_size * 0.35
    inner = (
        f'<text x="{cx}" y="{baseline_y}" text-anchor="{anchor}" '
        f'font-size="{font_size}" font-weight="{font_weight}" '
        f'fill="{fill}" '
        f'data-pptx-edit-name="{shape_id}">'
        f'<tspan xml:space="preserve">{text}</tspan>'
        f"</text>"
    )
    bounds = f"{x} {y} {w} {h}"
    write_new_content_block(svg_path, group_id=shape_id, bounds=bounds, inner_svg=inner)
```

### Phase 25 commit 2 — verify end-to-end

Run the same demo with `enable_vision_layout=True` and the existing cache hit (no second LLM call) → expect `ok=True stage=done` + non-empty PPTX.

If still failing: inspect vendor stderr for remaining `_UNSUPPORTED_TEXT_PROPERTIES` and patch.

### Phase 25 commit 3 — vendor attribute compliance audit (optional)

Add `tests/test_native_fill.py::TestSvgEditsVendorCompliance`:

```python
def test_add_text_block_uses_only_vendor_allowed_text_attrs(tmp_path):
    """Regression guard: prevent re-adding vendor-rejected attrs."""
    from mcp_ppt_native_fill.svg_edits import add_text_block
    svg = tmp_path / "slide.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="100" height="100"/></svg>'
    )
    add_text_block(svg, shape_id="t1", x=10, y=10, w=80, h=20, text="hi")
    raw = svg.read_text()
    forbidden = ["dominant-baseline", "alignment-baseline", "font-kerning"]
    for f in forbidden:
        assert f not in raw, f"{f!r} appeared in injected <text> — vendor rejects this"
```

---

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| 0.35 visual-baseline factor looks off for huge fonts (>120pt) | low | fallback to `<tspan dy="N">` for finer control if needed |
| New `_TEXT_INLINE_PROPERTIES` allowlist (line 111-126) excludes `dx`/`dy` but accepts `font-family` / `font-size` — vendor checks both direct attrs and inline styles | low | commit 3 audit covers this |
| Vendor version drift (ppt-master v6.3.0 → v6.4) may add/remove attrs | low | pin `_SVG_TEXT_PROPERTIES` snapshot via test fixture |
| E2E demo still fails for other reasons (cache file corruption, etc.) | medium | check `_err.txt` stderr tail |

---

## 6. What this commit does NOT do (out of scope)

- Vision feedback loop (Phase 25 commit 5+ — re-query LLM after first pass)
- LLM content mapping polish (already runs via `llm_planner.plan_content_mapping`)
- Vendor SVG attribute improvements (would require vendor patch)

---

## 7. Open questions

1. **Should we keep `alignment_baseline` parameter** for API compat? Yes — callers might be using it. Mark deprecated in docstring.
2. **Vertical centering factor 0.35** — empirical value. Should we make it tunable via `baseline_offset_factor=0.35` parameter? Probably yes for future-proofing.
3. **Vendor version drift** — when ppt-master updates the allowlist, our tests catch it via `test_add_text_block_uses_only_vendor_allowed_text_attrs`. If a new attr is added (e.g. `text-combine-upright`), tests need updating.

---

**Author**: Claude
**Date**: 2026-09-22
**Status**: Plan mode (no code yet) — awaiting user approval
**Reference**: `docs/PHASE24_VISION_LAYOUT_INFERENCE_PLAN.md` (vision infrastructure upstream)
**Companion**:
- `scripts/svg_to_pptx/drawingml/text_properties.py` (vendor whitelist)
- `scripts/svg_to_pptx/drawingml/converter.py:_require_project_text_properties` (preflight caller)