# mcp-ppt-native-fill — Design Notes

> Standalone design document for the `D:\Code\tst\native_fill\` MCP project.
> For the pipeline itself see
> [`NATIVE_FILL_PIPELINE_GUIDE.md`](NATIVE_FILL_PIPELINE_GUIDE.md).

## 1. Goals

- **One tool call** wraps ppt-master v6.3.0's 5-phase Edit Native PPTX
  round-trip pipeline (`pptx_to_svg → edit SVG → svg_quality_checker →
  svg_to_pptx → pptx_delivery_check → source_to_md`).
- **Auto-fixes** the six common authoring pitfalls from pipeline guide §6
  (gradient, picture structure, text overflow, viewBox, font stack,
  page_plan validation) without the LLM having to learn SVG quirks.
- **Stdlib-only** — no `mcp[cli]`, no `fastmcp`, no third-party deps. Drop
  the folder into any Python ≥ 3.10 environment.
- **Independent** — zero references to `mcp_ppt_master` (a sibling project
  at `D:\Code\tst\mcp_ppt_server\` that wraps the Generate PPTX route).

## 2. The 7 vendored scripts

| Script | Phase | Purpose |
|---|---|---|
| `attribution_guard.py` | 1 | Fail-closed skill integrity check (exit 0 / 78) |
| `pptx_to_svg.py` | 2 | Source PPTX → round-trip workspace |
| `svg_authoring_view.py` | 3-helper | Refresh `authoring_summary.json` |
| `svg_quality_checker.py` | 4 | Round-trip quality gate (exit 0/1) |
| `svg_to_pptx.py` | 5 | Workspace → PPTX (byte-for-byte source preservation) |
| `pptx_delivery_check.py` | 5b | Structural integrity of output PPTX |
| `source_to_md.py` | 5c | Human-readable readback |

`template_text_slots.py` is a library (no CLI) and is intentionally
**not** imported — `svg_edits.py` operates on SVG XML directly.

All seven are spawned as subprocesses from `runner.py`. SKILL_DIR is
resolved per the 5-step priority documented in SKILL.md.

## 3. State machine

```
INIT → IMPORTED → PLANNED → AUTHORED → QUALITY_PASSED → EXPORTED → VALIDATED → DONE
```

| Transition | Driver | On failure |
|---|---|---|
| INIT → IMPORTED | `pptx_to_svg.py --roundtrip` | abort, return `stage="failed"` |
| IMPORTED → PLANNED → AUTHORED | write `page_plan.json` + `svg_edits.apply_text_edits` + `write_new_content_block` | abort on validation errors |
| AUTHORED → QUALITY_PASSED | refresh summary + `svg_quality_checker --roundtrip` | enter auto-fix loop up to `max_fix_iterations` |
| QUALITY_PASSED → EXPORTED | `svg_to_pptx --roundtrip` | abort, surface stderr tail |
| EXPORTED → VALIDATED | `pptx_delivery_check` + `source_to_md` | abort if `validate_strict` and `status=failed` |
| VALIDATED → DONE | aggregate result envelope | — |

## 4. Auto-fix catalogue

Each fix function in `autofix.py` is pure and returns an
`AutoFixRecord(slide, issue, action, before, after, detail)`. They never
crash the pipeline — failures are reported via `warnings[]`.

| Issue | Trigger | Action |
|---|---|---|
| `text_overflow` | `svg_quality_checker` reports `overflow horizontal N%` on a shape | shrink `font-size` ×0.85 on that shape's `<text>` |
| `viewbox_missing` | quality check ERROR or export abort citing root viewBox | restore `<svg viewBox="0 0 W H" width=W height=H>` (W,H passed in by caller) |
| `gradient_unexportable` | export reports `Edited round-trip source object did not produce a DrawingML shape` on a gradient id | strip `<defs><linearGradient>...</linearGradient></defs>`, rewrite `fill="url(#x)"` → `fill="#FFFFFF"` |
| `picture_structure` | same, on `<image>` or nested `<svg><image/></svg>` | toggle between nested and flat forms (no rule to predict which passes — try both) |
| `unsafe_font` | quality check WARN with `Font stack exports non-PPT-safe typeface(s)` | rewrite `font-family="…思源黑体 CN…"` → `font-family="微软雅黑", sans-serif` |
| `page_plan` | export abort citing unknown/duplicated svg | validated at write time in `pipeline.write_page_plan` |

The auto-fix loop iterates between QUALITY and EXPORT phases. After
`max_fix_iterations` rounds with no resolution, the pipeline aborts with
the last quality-check error surfaced via `errors[]`.

## 5. SVG editing rules (preserved by `svg_edits.py`)

- Use `xml.etree.ElementTree`. Never use `lxml` or other XML libraries.
- When replacing text, **clear all `<tspan>` children** of the target
  `<text>` element (their `x`/`dy` attributes would otherwise pin the new
  content to the old baseline).
- **Preserve every `data-pptx-*` attribute** on the parent `<g>` (and any
  decorative children). The vendor `svg_to_pptx` uses these to rehydrate
  the original DrawingML byte-for-byte.
- File writes go through `write_utf8_atomic` to avoid partial writes when
  the process is interrupted mid-call.

## 6. Why this is independent from `mcp_ppt_master`

`D:\Code\tst\mcp_ppt_server\` (Generate PPTX route) and
`D:\Code\tst\native_fill\` (Edit Native PPTX route) are sibling projects
with **different design constraints**:

| Aspect | `mcp_ppt_master` (Generate) | `mcp_ppt_native_fill` (Edit Native) |
|---|---|---|
| Source | Zero / image / PDF | Existing PPTX template |
| Output design | Free SVG redraw | Must byte-for-byte preserve source design |
| Critical invariant | SVG schema compliance | `data-pptx-*` attribute preservation |
| Auto-fix surface | none (LLM-authored) | 6-class auto-fix (master §6) |

The two never share code because the cost of accidental coupling (e.g.
one importing the other's runner) outweighs the ~150 lines of protocol
duplication.

## 7. Out of scope

- LLM-driven content generation (this MCP only accepts a pre-planned
  `content_mapping`).
- Old ppt-master versions (v6.1, v6.2). Tested against v6.3.0 only.
- Create Template route.
- Multi-user / remote workspace.
- FastMCP / `mcp[cli]` PyPI deps.
