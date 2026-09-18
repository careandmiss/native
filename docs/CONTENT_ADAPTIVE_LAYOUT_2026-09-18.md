# Content-Adaptive Layout — Borrow ppt-master's Design Philosophy into mcp_ppt_native_fill

## Context

After Round-2 fixes (commit `4f484f7`) shipped, the regenerated PPTX (`3山西柏腾科技有限公司采购制度_20260918_092702.pptx`, 14 slides) has correct font sizing, no THANK YOU appendix pollution, and no text overflow. But the user observes:

> "内容页面的排版我怎么感觉像是固定排版 不管什么文档都是那样的排版呢?"

Root cause analysis (from 3 parallel Explore agents):

1. **All content pages clone `source_slide: 4`** in `page_plan.json` → identical chrome (top ellipse accents `shape-14/15/16` + `shape-18/19/20`, top chapter title bar `shape-17`, dashed connector `shape-21`, outer frame `shape-3`, injected topbar + footer).
2. **Body archetype DOES vary** (comparison / callout-box / procedural-steps / 3-column-cards), but the fixed chrome dominates visually so the variation is masked.
3. **Two consecutive 3-card pages** (`slide_part05/part06`) reinforce the "looks the same" perception.
4. **Template has only 1 fillable content canvas** (slide_04, body rect 789940,976630,10711180,5052060 EMU). Chrome is inlined per-slide (not in master/layout).
5. **`workspace_expand.expand_workspace_from_markdown`** accepts a single `skeleton_content: int`; per-page skeleton picker does not exist.
6. **`chrome.py`** is archetype-blind — `render_chrome_topbar` / `render_chrome_footer` use fixed geometry regardless of what archetype is rendered underneath.
7. **`block_renderer.render_new_block`** uses uniform body bounds (clip area `(83, 110, 1203, 569)`) for all 17 archetypes.

Intended outcome: each content page earns its composition from its content (page job + Relationships atom + page rhythm), per ppt-master §2.2 nine-step decision chain. Chrome varies by archetype (hero archetypes suppress topbar; dense archetypes keep it). Body bounds vary by archetype. Three distinct source-slide variants replace the single `source_slide: 4`, so different archetypes physically clone different template slides with different chrome.

**User-confirmed scope** (2026-09-18): Layer 1 + Layer 2 (no Layer 3 this iteration); hero archetypes (`hero_statement`, `callout-box`, `statement-caption`, `hero-number`) suppress topbar but keep footer.

---

## Implementation

### Module 1 — `archetype_meta.py` (NEW, ~180 LOC)

Single source of truth: archetype → metadata.

```python
# src/mcp_ppt_native_fill/archetype_meta.py
from typing import TypedDict

class ArchetypeMeta(TypedDict):
    rhythm: str                   # "anchor" | "dense" | "breathing"
    reading_mode: str            # "text" | "balanced" | "presentation"
    body_bounds: tuple[float, float, float, float]  # (x, y, w, h) on 1280x720
    chrome_overrides: dict       # {suppress_topbar, suppress_footer, suppress_section_divider}
    source_slide_hint: int       # template slide index (Layer 2 routing)
    recipe: str | None           # Layer 3 recipe name (None for this iteration)

ARCHETYPE_META: dict[str, ArchetypeMeta] = {
    "hero_statement":   {"rhythm": "breathing",  "reading_mode": "presentation", "body_bounds": (110, 140, 1060, 460), "chrome_overrides": {"suppress_topbar": True,  "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 6, "recipe": None},
    "hero-number":      {"rhythm": "anchor",     "reading_mode": "presentation", "body_bounds": (200, 180, 880, 380),  "chrome_overrides": {"suppress_topbar": True,  "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 6, "recipe": None},
    "callout-box":      {"rhythm": "breathing",  "reading_mode": "presentation", "body_bounds": (160, 200, 960, 280),  "chrome_overrides": {"suppress_topbar": True,  "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 6, "recipe": None},
    "statement-caption":{"rhythm": "breathing",  "reading_mode": "presentation", "body_bounds": (160, 200, 960, 280),  "chrome_overrides": {"suppress_topbar": True,  "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 6, "recipe": None},
    "kpi_row":          {"rhythm": "dense",      "reading_mode": "balanced",     "body_bounds": (64, 150, 1152, 360),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 7, "recipe": None},
    "revision-table":   {"rhythm": "dense",      "reading_mode": "text",         "body_bounds": (64, 130, 1152, 460),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 7, "recipe": None},
    "timeline":         {"rhythm": "anchor",     "reading_mode": "presentation", "body_bounds": (64, 150, 1152, 420),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 7, "recipe": None},
    "comparison":       {"rhythm": "dense",      "reading_mode": "balanced",     "body_bounds": (84, 130, 1112, 440),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "two-column-compare":{"rhythm":"dense",      "reading_mode": "balanced",     "body_bounds": (84, 130, 1112, 440),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "procedural-steps": {"rhythm": "anchor",     "reading_mode": "balanced",     "body_bounds": (84, 140, 1112, 460),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "flow-steps":       {"rhythm": "anchor",     "reading_mode": "balanced",     "body_bounds": (84, 140, 1112, 460),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 7, "recipe": None},
    "matrix_2x2":       {"rhythm": "dense",      "reading_mode": "balanced",     "body_bounds": (160, 150, 960, 400),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "3-column-cards":   {"rhythm": "dense",      "reading_mode": "balanced",     "body_bounds": (96, 140, 1088, 460),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "three-thesis-cards":{"rhythm":"dense",      "reading_mode": "balanced",     "body_bounds": (96, 140, 1088, 460),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "bullet-list":      {"rhythm": "dense",      "reading_mode": "text",         "body_bounds": (84, 130, 1112, 470),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "simple-text":      {"rhythm": "breathing",  "reading_mode": "text",         "body_bounds": (84, 130, 1112, 470),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
    "raw":              {"rhythm": "dense",      "reading_mode": "balanced",     "body_bounds": (83, 110, 1203, 569),  "chrome_overrides": {"suppress_topbar": False, "suppress_footer": False, "suppress_section_divider": False}, "source_slide_hint": 8, "recipe": None},
}
DEFAULT_META = ARCHETYPE_META["raw"]
```

### Module 2 — `relationships_detector.py` (NEW, ~120 LOC)

Pure heuristic that reads markdown section text and emits `{atom, confidence, suggested_rhythm}`.

```python
# src/mcp_ppt_native_fill/relationships_detector.py
from typing import TypedDict

class AtomResult(TypedDict):
    atom: str                    # "order" | "link" | "parent" | "membership" | "contrast" | "overlap" | "none"
    confidence: float            # 0..1
    suggested_rhythm: str        # "anchor" | "dense" | "breathing"

_ORDER_RE       = r"(?:^\s*\d+[\.\)、])|(?:步骤\s*\d+)|(?:Step\s*\d+)"
_CONTRAST_RE    = r"(?:对比|区别|不同于|反之|vs\.?|V\.S\.|新制度|旧制度|新方法|旧方法)"
_PARALLEL_RE    = r"(?:属于|归类|分类|category)"  # weak membership signal
_NESTED_RE      = r"(?:^\s*\d+\.\d+[\.\)、])"     # nested numbered list → parent
_ARROW_RE       = r"(?:→|->|=>|流程)"           # link signal
_PEER_HEADINGS  = 3                             # 3+ peer h3 → membership

def detect(section_text: str) -> AtomResult:
    """Heuristic: pick one of 7 atoms from section text shape."""
    # ordered list at top → order (0.9)
    # nested list → parent (0.85)
    # contrast keyword → contrast (0.85)
    # arrow / flow keyword → link (0.7)
    # 3+ peer h3 → membership (0.8)
    # otherwise → none (0.5)
    ...
```

### Module 3 — `tools/clone_content_template.py` (NEW, ~250 LOC)

CLI: `python tools/clone_content_template.py --template 柏腾ppt模版.pptx --out 柏腾ppt模版_v2.pptx`

Clones `slide_04` three times with different chrome decorations:

| Output slide | Source | Stripped shapes | Kept shapes |
|---|---|---|---|
| `slide_06_content_hero` | clone of slide_04 | shape-14/15/16, shape-18/19/20, shape-21 | shape-3, shape-17 |
| `slide_07_content_wide` | clone of slide_04 | shape-14/15/16, shape-18/19/20 | shape-3, shape-17, shape-21 |
| `slide_08_content_full` | clone of slide_04 (unchanged) | none | all |

Stripping pattern uses `python-pptx` to walk the slide's `shapes` tree by `name` and call `.element.getparent().remove(...)`. Background image (`image3.png`) is preserved. Manifest JSON: `{"hero": 6, "wide": 7, "full": 8, "body_rect_emu": [789940, 976630, 10711180, 5052060]}`.

### Module 4 — `chrome.py` additions

```python
# src/mcp_ppt_native_fill/chrome.py — additions

def chrome_suppress_for(archetype: str, rhythm: str | None = None) -> tuple[bool, bool, bool]:
    """Pure-Python: (suppress_topbar, suppress_footer, suppress_section_divider) by archetype."""
    from .archetype_meta import ARCHETYPE_META, DEFAULT_META
    meta = ARCHETYPE_META.get(archetype, DEFAULT_META)
    o = meta["chrome_overrides"]
    return (o["suppress_topbar"], o["suppress_footer"], o["suppress_section_divider"])
```

### Module 5 — `block_renderer.py` updates

1. Add `from . import archetype_meta` at module top.
2. In `render_new_block` dispatcher (line 217+), after `layout = spec.get("layout", "raw")`, add:
   ```python
   meta = archetype_meta.ARCHETYPE_META.get(layout, archetype_meta.DEFAULT_META)
   body_bounds = spec.get("body_bounds") or list(meta["body_bounds"])
   ```
3. Pass `body_bounds` to each of the 17 archetype render calls. Most already accept `(x, y, w, h)` — replace hardcoded defaults with the parameter. `spec["body_bounds"]` from the LLM still wins (backward compat).
4. In `statement-caption` (line 909+) and `hero_statement` (line 1341+), `body_y0 = by + (body_bounds[1] - default_body_y0)` recompute so the dynamic line-cap uses the new bounds.

### Module 6 — `pipeline.py` updates

1. **`realize_plan` (line 443)** — accept new parameter:
   ```python
   def realize_plan(
       state: PipelineState,
       *,
       caller_page_plan: list[dict] | None = None,
       caller_new_blocks: dict[str, dict[str, dict[str, Any]]] | None = None,
       content_skeleton_pool: list[int] | None = None,   # NEW (Layer 2)
   ) -> PipelineState:
   ```
   When `content_skeleton_pool` is provided and an entry lacks explicit `source_slide`, route via `archetype_meta.ARCHETYPE_META[layout]["source_slide_hint"]`. Validate the hint against the pool; fallback to `content_skeleton_pool[0]`.

2. **`_derive_default_chrome_plan` (line 968)** — extend each entry dict with `archetype`, `relationships_atom`, `page_rhythm`, `reading_mode` fields, looked up from the matching `page_plan_additions` entry (which the LLM now fills).

4. **`_inject_content_chrome` (line 850)** — wrap each chrome write in archetype-aware suppression:
   ```python
   for entry in plan:
       if entry.get("skip"):
           continue
       archetype = entry.get("archetype", "raw")
       suppress_t, suppress_f, suppress_sd = _chrome.chrome_suppress_for(archetype, entry.get("page_rhythm"))
       ...
       if enable_topbar and not suppress_t:
           inner_parts.append(_chrome.render_chrome_topbar(...))
       if enable_footer and not suppress_f:
           inner_parts.append(_chrome.render_chrome_footer(...))
   ```

5. **New helper `_apply_archetype_meta(state)`** (~40 LOC) — walks `state.context["page_plan_pages"]`; for any entry missing `archetype` / `relationships_atom` / `page_rhythm` / `reading_mode`, fills from `relationships_detector.detect(entry.get("title", "") + " " + entry.get("body", ""))`. Called after `_apply_llm_plan` and before `_inject_content_chrome`.

### Module 7 — `workspace_expand.py` updates

`expand_workspace_from_markdown` (line 42): replace `skeleton_content: int` with `content_skeleton_pool: list[int] = [4]`. Default `[4]` preserves backward compat (one pool entry, behave as before).

Inside the H1 expansion loop (line 219+), call `relationships_detector.detect(section_text)` and embed `atom` + `suggested_rhythm` into the synthesized `page_plan.json` entry.

When emitting a content SVG path that doesn't yet exist, pick `source_slide` from `archetype_meta.ARCHETYPE_META[heuristic_archetype]["source_slide_hint"]`.

### Module 8 — `llm_planner.py` updates

`SYSTEM_PROMPT` lines 99-138 (Composition Patterns table) — add two columns: `Relationships atom` and `Rhythm`. Example row:

```
| Hero statement | 1 quoted idea / metric | hero_statement | presentation | overlap | breathing |
```

Add a new section after line 138: "Atom, rhythm, and reading mode" (~25 lines):
- "For every page_plan_additions entry, also emit `relationships_atom` (one of: order/link/parent/membership/contrast/overlap/none) and `page_rhythm` (one of: anchor/dense/breathing)."
- "Pick the atom that best describes how the page's content units relate to each other — not how they relate to the slide above."
- "Pick the rhythm: `anchor` for declarative/structural pages (1 takeaway), `dense` for data/comparison pages (4+ items), `breathing` for single-thought / quote pages."

Update the JSON schema example block (around line 200+) to include the 4 new fields:
```json
{
  "svg": "slide_part05_content.svg",
  "source_slide": 8,
  "layout": "3-column-cards",
  "title": "...",
  "relationships_atom": "membership",
  "page_rhythm": "dense",
  "composition_macro": "triad",
  "reading_mode": "balanced"
}
```

Update `_normalize_new_blocks` (line 1044+) — accept the 4 new optional fields, defaulting to `"none"` / `"dense"` / `null` / `"balanced"` respectively.

---

## Critical Files

- `src/mcp_ppt_native_fill/archetype_meta.py` — NEW: archetype → metadata dict
- `src/mcp_ppt_native_fill/relationships_detector.py` — NEW: heuristic atom detector
- `tools/clone_content_template.py` — NEW: template cloner (Layer 2)
- `src/mcp_ppt_native_fill/chrome.py` — add `chrome_suppress_for`
- `src/mcp_ppt_native_fill/block_renderer.py` — read `archetype_meta.body_bounds`, propagate to 17 archetypes
- `src/mcp_ppt_native_fill/pipeline.py` — `realize_plan` accepts `content_skeleton_pool`; `_inject_content_chrome` archetype-aware; `_apply_archetype_meta` new helper; `_derive_default_chrome_plan` propagates 4 new fields
- `src/mcp_ppt_native_fill/workspace_expand.py` — `content_skeleton_pool: list[int] = [4]`; heuristic detector call
- `src/mcp_ppt_native_fill/llm_planner.py` — SYSTEM_PROMPT atom+rhythm guidance; `_normalize_new_blocks` accepts 4 new fields
- `docs/CONTENT_ADAPTIVE_LAYOUT_2026-09-18.md` — NEW: reference doc (copy of this plan file)

(Existing functions reused: `chrome.render_chrome_topbar`/`render_chrome_footer`, `svg_edits.write_new_content_block`, `svg_edits.strip_template_chrome_shapes`, `_derive_default_chrome_plan`, `_seed_original_roster`, `_resolve_chrome_meta`.)

---

## Verification

### End-to-end checklist

1. **Template cloning**:
   ```bash
   python tools/clone_content_template.py \
     --template "D:/Code/tst/native_fill/柏腾ppt模版.pptx" \
     --out "D:/Code/tst/native_fill/柏腾ppt模版_v2.pptx"
   ```
   Verify `柏腾ppt模版_v2.pptx` has 8 slides total, with slide_06 stripped of ellipse accents, slide_07 stripped of ellipse accents only, slide_08 identical to slide_04. Manifest JSON written to `柏腾ppt模版_v2.manifest.json`.

2. **Pipeline re-run**:
   ```python
   PYTHONPATH=src python -c "
   from pathlib import Path
   from mcp_ppt_native_fill import generate_pptx
   print(generate_pptx(
       md_path=Path(r'D:/Code/tst/native_fill/3山西柏腾科技有限公司采购制度.md'),
       template_pptx_path=Path(r'D:/Code/tst/native_fill/柏腾ppt模版_v2.pptx'),
       skill_dir=Path(r'C:/Users/Administrator/.claude/skills/ppt-master'),
   ))
   "
   ```

3. **SVG inspection** — open `authoring-svg-flat/slide_part02..07_content.svg` and verify:
   - `slide_part03_content.svg` (callout-box) → NO `id="slide-topbar"` element
   - `slide_part04_content.svg` (procedural-steps) → HAS topbar
   - `slide_part05_content.svg` (3-column-cards) → HAS topbar
   - `slide_part06_content.svg` (3-column-cards) → HAS topbar (but body bbox different from slide_part05)
   - `data-pptx-bounds` on `content-body` group varies across slides (≥3 distinct bounds tuples)

4. **page_plan.json inspection** — confirm `relationships_atom`, `page_rhythm`, `composition_macro`, `reading_mode` fields populated for all content pages. Confirm `source_slide` varies: hero archetypes → 6, table/timeline → 7, default → 3.

6. **Backward compat** — run with `content_skeleton_pool=[3]` (Layer 2 disabled, single pool entry); output must match current Round-2 baseline.

7. **Tests**:
   - `test_archetype_meta.py`: every archetype has a meta entry; body bounds inside `(0, 0, 1280, 720)`.
   - `test_relationships_detector.py`: 12 fixtures covering each atom + each confidence threshold.
   - `test_chrome_suppress_for.py`: each archetype + rhythm yields expected booleans.
   - `test_pipeline_realize_plan.py`: with `content_skeleton_pool=[6,7,8]`, hero layouts route to 6, table layouts to 7, default to 8.

### Visual acceptance

14 slides → ≥5 distinct visual signatures (chrome_signature × body_bounds × archetype). The user's complaint dissolves because no two adjacent slides look identical — slide_part03 (no topbar, big quote) and slide_part05 (topbar, 3 colored cards) are now visually distinguishable at a glance.

---

## Execution Plan (Multi-Agent)

Phase 1 (parallel, 3 agents) — independent foundations:
- **Agent 1**: write `archetype_meta.py` + `relationships_detector.py` (~300 LOC, no other files touched)
- **Agent 2**: write `chrome_suppress_for` in `chrome.py` (~30 LOC) + new `tools/clone_content_template.py` script (~250 LOC)
- **Agent 3**: extend `llm_planner.py` SYSTEM_PROMPT + `_normalize_new_blocks` (~80 LOC)

Phase 2 (parallel, depends on Phase 1) — integration:
- **Agent 4**: update `block_renderer.py` (read archetype_meta, propagate body_bounds to 17 archetypes)
- **Agent 5**: update `workspace_expand.py` (content_skeleton_pool + heuristic detector)
- **Agent 6**: update `pipeline.py` (realize_plan content_skeleton_pool + chrome suppression + _apply_archetype_meta helper + _derive_default_chrome_plan 4 new fields)

Phase 3 (sequential, depends on Phase 2):
- **Verify Agent 1**: run end-to-end pipeline; assert SVG inspection + page_plan.json fields + source_slide variation; if any check fails, loop-until-dry with targeted fixes.

Phase 4:
- **Commit Agent**: bundle all changes with comprehensive commit message; push to `origin/integration/sink-generate-local-ppt-2026-09-14`.

Phase 5:
- **Doc Agent**: write `docs/CONTENT_ADAPTIVE_LAYOUT_2026-09-18.md` (copy of this plan file, formatted as reference doc); include before/after SVG snippets.

---

## Out of Scope

- Layer 3 (page-level recipes + carrier-receipt review gate) — deferred to next sprint.
- Whitelist of composition_macro values (free-form string from LLM; validation deferred).
- Full ppt-master typography table import (uses existing 8-tier TYPOGRAPHY).
- Per-section divider chrome variation (e.g., divider with eyebrow text).
- Multi-language Relationships detector (English-only heuristics; CJK coverage via ordered-list patterns).
- Replacing the current `_ALLOW_PPT_MASTER_ARCHETYPES` flag with auto-detection.

---

## Open Questions Resolved

- **Layer scope**: Layer 1 + Layer 2 ✅ (user confirmed 2026-09-18)
- **Hero topbar suppression**: hero_statement / callout-batch / statement-caption / hero-number → suppress topbar; keep footer ✅ (user confirmed 2026-09-18)
- **composition_macro**: free-form string from LLM (no validation)
- **Footer suppression**: not in this iteration; all keep footer
- **Layer 3 (recipes + carrier-receipt)**: deferred