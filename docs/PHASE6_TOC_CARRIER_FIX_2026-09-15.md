# Phase 6 — TOC Slot Empty-`<text>` Removal Fix

**Date:** 2026-09-15
**Branch:** `integration/sink-generate-local-ppt-2026-09-14`
**Status:** Implemented, all 166 tests pass, both E2E scenarios produce valid pptx.

## Problem

After pushing the Phase 5 source-ref fix (commit `413bb33`), running
`examples/test_toc_4_vs_7.py` failed with the same vendor error in both
scenarios:

```
SvgNativeConversionError: slide_02.svg:
  Failed to convert <g id="shape-86">:
  Semantic shape text component produced no native text body

SvgNativeConversionError: slide_part02_toc.svg:
  Failed to convert <g id="shape-72">:
  Semantic shape text component produced no native text body
```

### Root cause

`workspace_expand.expand_workspace_from_toc` clears unused TOC slot
`<text>` content when `N < slot_count` (4-chapter → slots 5/6 cleared;
7-chapter overflow clone → slots 2-6 cleared). After clearing, the
slot `<g>` exists with an **empty `<text>` self-closing element**.

Vendor `_semantic_shape_text_body` (`converter.py:744-792`) requires
exactly one `<text>` child per semantic shape and calls
`convert_text` on it. When the text is empty, `convert_text`
(`elements.py:3233-3239`) returns `None` and the function raises
`SvgNativeConversionError: Semantic shape text component produced no
native text body`.

### Why the obvious fix (carrier marker) fails

The canonical ppt-master escape hatch is `data-pptx-carrier="true"` on
the `<text>` element — `convert_text` then substitutes `'​'`
(zero-width space) and compiles a real `p:txBody`. BUT:

* **Placement lint** (`template_structure.py:1866-1876`) rejects any
  `data-pptx-*` attribute on a descendant of a non-placeholder `<g>`.
  Boteng's slot `<g>` shapes carry `data-pptx-semantic-object="shape"`
  but NOT `data-pptx-placeholder` — so the carrier marker on the inner
  `<text>` is lint-forbidden.

* Trying to convert the slot `<g>` into a placeholder carrier
  (`data-pptx-placeholder="title"`, `data-pptx-binding="carrier"`)
  ALSO fails: the placeholder requires exactly one visual child with
  the carrier marker (line 1835), but boteng's slot `<g>` contains
  both a geometry `<path>` AND a `<text>` (2 visual children).

## Approach

**Remove the empty `<text>` element entirely** when clearing a TOC
slot. With no `<text>` child, `_semantic_shape_text_body` returns
`None` (no text to convert), and `_convert_semantic_shape`
(`converter.py:849`) compiles the shape as pure geometry:

```python
text_body = _semantic_shape_text_body(shape, ctx)
return _append_shape_text(geometry, text_body) if text_body else geometry
```

The slot `<g>` shape and its geometry `<path>` are preserved; only
the empty `<text>` is dropped.

### Why this design (vs alternatives)

| Option | Why not |
|--------|---------|
| Add `data-pptx-carrier="true"` on cleared `<text>` | Lint-forbidden on boteng's non-placeholder slot `<g>` (`template_structure.py:1866-1876`) |
| Convert slot `<g>` to placeholder carrier | Placeholder requires exactly 1 visual child with carrier marker; boteng has 2 (geometry + text) |
| Add `data-pptx-placeholder="title"` AND carrier on `<text>` | Same as above — fails the "exactly 1 visual child" check |
| Re-export boteng template with pre-applied carrier markers | Out of our control; doesn't fix future templates |
| Delete the cleared `<text>` entirely | **Selected.** Compiles cleanly as pure geometry; idempotent; no template mutation needed. |

## Implementation

### 1. `src/mcp_ppt_native_fill/svg_edits.py`

Added `mark_empty_as_carrier: bool = False` kwarg to
`apply_text_edits`. When `True` and `new_text == ""`, the cleared
`<text>` element is **removed** from its parent `<g>` instead of being
left empty. The kwarg name is kept for grep-stability with downstream
autofixes and earlier Wave 1 patches; effective behaviour is now
"remove the empty `<text>`", documented inline.

```python
if new_text == "" and mark_empty_as_carrier:
    g.remove(text_elem)
    audit.append({
        "shape_id": shape_id,
        "status": "applied",
        "old": old_text,
        "new": "",
        "carrier_removed": True,
    })
    continue
```

### 2. `src/mcp_ppt_native_fill/workspace_expand.py`

Two call sites pass `mark_empty_as_carrier=True`:

* Line 420 (in-place TOC edit, `< N / == N` branch)
* Line 440 (overflow clone edit, `> N` branch)

All other `apply_text_edits` callers (divider edits, content edits,
`pipeline.py` phase3 re-apply) keep the default `False`. Phase 3
re-apply also receives the flag because phase2_import overwrites the
workspace SVG between expand and re-apply.

### 3. `src/mcp_ppt_native_fill/pipeline.py`

`phase3_author` (line 597) passes `mark_empty_as_carrier=True` to
`svg_edits.apply_text_edits`. This is the re-apply inside
`run_native_fill` that runs AFTER `phase2_import` overwrites the
workspace — without the flag here, the cleared `<text>` is re-created
as empty (without the `<text>`-removal pass) and the vendor error
re-appears.

## Critical files

| File | Change |
|------|--------|
| `src/mcp_ppt_native_fill/svg_edits.py` | Added kwarg + empty `<text>` removal logic |
| `src/mcp_ppt_native_fill/workspace_expand.py` | Pass flag at 2 TOC edit sites |
| `src/mcp_ppt_native_fill/pipeline.py` | Pass flag in `phase3_author` (line 597) |
| `tests/test_native_fill.py` | Updated 4 unit tests + added `import re` to 4 tests |
| `examples/test_toc_4_vs_7.py` | Unchanged (the bug repro) |

## Verification

### Unit tests

```
$ python -m unittest tests.test_native_fill
Ran 166 tests in 0.308s
OK
```

Test updates:

* `test_apply_text_edits_empty_with_carrier_flag_removes_text` — asserts
  cleared `<text>` is gone from shape-1, no carrier marker set
* `test_apply_text_edits_empty_without_flag_keeps_empty_text` — default
  behavior keeps the `<text>` (just emptied)
* `test_apply_text_edits_nonempty_with_carrier_flag_keeps_text` —
  non-empty edits unaffected
* `test_apply_text_edits_carrier_idempotent` — second pass reports
  `no_text_node` (the `<text>` is already gone)

Test rewrites:

* `test_lt_n_fills_first_n_clears_rest_keeps_g` — asserts cleared slot
  `<g>` is self-closing (no `<text>` child); filled slot retains its
  `<text>` with chapter title
* `test_gt_n_overflow_clone_removes_cleared_slot_text` — mirror of
  the above for the overflow clone
* `test_real_boteng_clears_unused_slots_by_removing_text` — end-to-end
  against the real boteng `slide_02.svg`: cleared slots shape-86,
  shape-89 have NO `<text>` child

### End-to-end (both scenarios produce valid pptx)

```
$ python examples/test_toc_4_vs_7.py
=== 4 chapters (< 6 slots) ===
  output exists: True
  ok:            True
  stage:         done
  slot_count:    6
  filled:        4
  cloned_svgs:   []

=== 7 chapters (> 6 slots → 1 overflow clone) ===
  output exists: True
  ok:            True
  stage:         done
  slot_count:    6
  filled:        7
  cloned_svgs:   ['slide_part02_toc.svg']
```

The 4-chapter scenario fills slots 0-3 (chapter titles), slots 4-5
have their `<text>` removed (slot `<g>` + geometry preserved).
The 7-chapter scenario fills slots 0-5 in slide_02 and slot 0 in
slide_part02_toc.svg; slots 1-5 in the clone have their `<text>`
removed.

### Regression check

```
$ rm -f projects/smart_toc_boteng_out.pptx
$ python examples/smart_toc_fill.py
  "ok": true,
  "stage": "done",
  "errors_count": 0
```

Smart TOC (6/6 filled, no slots cleared) is unaffected: the empty
`<text>` removal only fires when `mark_empty_as_carrier=True` AND
`new_text == ""`. Smart-TOC's 6/6 fill passes real text, so no slot
is cleared, so no `<text>` is removed.

## Reuse / existing utilities

* `svg_edits.apply_text_edits` (`src/mcp_ppt_native_fill/svg_edits.py:68`) — extension point
* `build_toc_slot_edits` (`src/mcp_ppt_native_fill/workspace_expand.py:243`) — already produces empty strings for cleared slots; removal pass piggybacks on the same data
* `_make_toc_workspace` test fixture (`tests/test_native_fill.py:3602`) — synthetic slot workspace for unit tests
* `shutil.copytree` from `projects/baseline_workspace` — for the real-boteng integration test

No new modules needed. No public API additions. The change is fully
backward-compatible (new kwarg defaults to `False`).

## Out of scope

* **Re-exporting the boteng template** with pre-applied carrier markers — out of our control (user-provided template), and the framework fix handles it.
* **Other vendor errors surfaced by `svg_to_pptx`** (e.g., connector zero-stroke on non-TOC shapes) — already handled by `disabled_autofixes=["render_compat"]` opt-in (Phase 5).
* **Structural cleanup of empty TOC slot `<g>` shapes** — `remove_empty_toc_slots` in `toc_detection.py:328` is the LLM-driven path; smart-TOC explicitly preserves `<g>` shapes. Out of scope for this fix.
* **Generic semantic-shape empty-text handling** — this fix is specific to the smart-TOC fill path. Non-TOC edits leave empty `<text>` alone (default flag is `False`).

## Risk assessment

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Removing `<text>` makes filled slots lose visual line wrapping | None | `<text>` is only removed when `new_text == ""` AND `mark_empty_as_carrier=True` (opt-in, smart-TOC only) |
| `<text>` removal affects non-TOC callers | None | Flag is opt-in; default `False`; only 2 TOC call sites pass `True` |
| Phase3 re-apply re-creates empty `<text>` | None | `phase3_author` now passes `mark_empty_as_carrier=True`; empty `<text>` is removed (not just emptied) |
| Smart-TOC full-fill (6/6) regresses | Very low | Verified via `examples/smart_toc_fill.py` — `ok=true`, `errors_count=0` |
| Lint forbids the now-absent `<text>` removal | None | The fix removes structure, it doesn't add it; lint only fires on present attributes |
