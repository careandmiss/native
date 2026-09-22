# Phase 24 Plan — Vision Layout Inference (MCP-built-in LLM)

> **Date**: 2026-09-22
> **Status**: Plan mode (no code yet)
> **Trigger**: User said "我们可以mcp内置llm" + earlier "不能硬编码". We have `llm_client.py` (Anthropic + OpenAI-compatible adapters) and `llm_planner.py` (already uses it). Now we extend the stack with **vision** capability so the MCP server can auto-infer per-template placeholder layouts from rendered PPTX/SVG slides — no hardcoding, no caller-supplied config.
> **Goal**: When `_auto_fill_template_options` runs, optionally call a vision-capable LLM with rendered slide PNGs → receive structured layout JSON → use it to drive `expand_workspace_from_markdown`'s text-block injection. Cached per-pptx-hash so cost is one-shot per template.

---

## 1. Current State — What Already Exists

| Component | File | Status |
|---|---|---|
| `LLMConfig`, `OpenAICompatibleClient`, `AnthropicClient` | `src/mcp_ppt_native_fill/llm_client.py` | ✅ Text-only, no vision |
| `llm_complete_json(system, user, config)` | `llm_client.py:276` | ✅ Public entry, returns parsed JSON dict |
| `_anthropic_complete` / `_openai_compat_complete` | `llm_client.py:333, 362` | ✅ Build text-only messages |
| `plan_content_mapping` (LLM-driven content routing) | `src/mcp_ppt_native_fill/llm_planner.py:366` | ✅ Uses `llm_complete_json` for text LLM |
| `inspect_template` (no vision, text + PPTX-metadata only) | `src/mcp_ppt_native_fill/template_adapter.py:373` | ✅ Position-based + text-shape inference |
| `_auto_fill_template_options` (no vision) | `src/mcp_ppt_native_fill/pipeline/handlers/markdown_expand.py` | ❌ Hardcoded fallback for boteng |
| Template V2 demo layout config | caller-side | ❌ Required (Phase 24 commit 2) |
| `docs/PHASE24_TEMPLATE_LAYOUT_CONFIG_PLAN.md` | caller-supplied layout plan | ⚠️ Earlier draft — superseded by THIS document |

### What I missed in earlier analysis

1. **`llm_client.py` is text-only** — to support vision we must extend
   `_anthropic_complete` and `_openai_compat_complete` to accept image
   attachments (base64-encoded PNG).
2. **No `data-pptx-placeholder` attribute convention exists in vendor SVGs** —
   the vendor (`vendor/pptx_master/scripts/pptx_to_svg/converter.py`) doesn't
   emit `data-pptx-placeholder="title"` markers. We have to add them OR rely
   on vision-only inference.
3. **Vision capability depends on model** — `claude-sonnet-5-20250929`
   supports vision; `gpt-5-mini` supports vision; but local models like
   `llama-3-8b` do NOT. We need capability detection.
4. **Vision latency + cost** — vision tokens cost ~1000× text tokens.
   Caching is critical.
5. **No PNG render path exists** — vendor produces SVG; we need to render
   SVG → PNG (cairosvg is in repo, vendor svg_to_pptx may also do this).
6. **No structured output schema** — current `plan_content_mapping`
   returns free-form LLM JSON; we need a strict schema for layout JSON
   with coordinate validation (vendor rejects `%` units, must be absolute).
7. **No confidence threshold** — vision may hallucinate positions; need
   a way to reject low-confidence inferences and fall back.

---

## 2. Design — Three-Layer Stack

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3: pipeline integration                                  │
│  markdown_expand._auto_fill_template_options:                  │
│    1. Look up cached layout (vision_layout_cache/<hash>.json)   │
│    2. If miss + vision enabled → call vision_layout.infer()   │
│    3. Pass layout to expand_workspace_from_markdown(           │
│         divider_layouts=, content_layouts=, ...)              │
│  Falls back to text-shape auto-fill (no vision needed) if       │
│  vision fails or is disabled.                                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2: vision_layout module                                  │
│  vision_layout.infer_layout(pptx_path) -> LayoutProfile:        │
│    1. Render each slide to PNG (via cairosvg)                  │
│    2. Build vision prompt: "for each slide, identify text       │
│       placeholder rectangles + their semantic role + canvas-   │
│       coordinate bounds"                                        │
│    3. Call llm_client.llm_complete_vision_json(...)            │
│    4. Parse response, validate coordinates, cache to disk      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: llm_client vision extension                           │
│  Add llm_complete_vision_json(*, system, user, images, config) │
│  - OpenAI: messages content = [text, image_url base64]         │
│  - Anthropic: messages content = [text, image base64 source]    │
│  Same response_format=json_object + parsed-JSON return path    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Layer 1 — `llm_client.py` vision extension

### 3.1 New public function

```python
def llm_complete_vision_json(
    *,
    system: str,
    user: str,
    images: list[tuple[bytes, str]],  # (png_bytes, media_type)
    config: LLMConfig | None = None,
    max_image_dim: int = 1568,        # Anthropic's recommended max
) -> dict[str, Any]:
    """Vision-capable variant of llm_complete_json. Sends text + images.
    
    ``images`` is a list of (bytes, media_type) tuples. Each image is
    resized to fit max_image_dim on the longest side before encoding
    (Anthropic recommends <= 1568 px to keep token cost low).
    """
```

### 3.2 Anthropic extension

```python
def _anthropic_complete_vision(*, system, user, images, config) -> str:
    content_blocks = [{"type": "text", "text": user}]
    for png_bytes, media_type in images:
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,  # "image/png"
                "data": b64,
            },
        })
    body = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    # ... same HTTP transport ...
```

### 3.3 OpenAI extension

```python
def _openai_compat_complete_vision(*, system, user, images, config) -> str:
    content_blocks = [{"type": "text", "text": user}]
    for png_bytes, media_type in images:
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        })
    body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content_blocks},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": config.max_tokens,
    }
    # ... same HTTP transport ...
```

### 3.4 Capability detection

```python
def _is_vision_capable(model: str) -> bool:
    """Heuristic: assume vision-capable unless the model name suggests
    otherwise (e.g. local llama-3-8b, mistral-7b)."""
    name = model.lower()
    if "vision" in name or "-v-" in name:
        return True
    # OpenAI gpt-4o, gpt-5, gpt-4-turbo are vision-capable
    if any(x in name for x in ("gpt-4o", "gpt-5", "gpt-4-turbo")):
        return True
    # Anthropic sonnet / opus / haiku (3.5+, 4+) are vision-capable
    if "claude" in name and ("haiku" in name or "sonnet" in name or "opus" in name):
        return True
    # Local models without explicit vision marker → assume not
    return False
```

### 3.5 Image resize helper

```python
def _resize_png(png_bytes: bytes, max_dim: int = 1568) -> bytes:
    """Resize PNG to fit max_dim on longest side. Uses stdlib only —
    uses vendored cairosvg (already a project dep) to decode + cairosvg
    PNG re-encode via PIL if available, else PIL fallback."""
```

(We have `cairosvg` as a dep — already used in `_render.py` I wrote earlier.)

---

## 4. Layer 2 — `vision_layout.py` module

### 4.1 New file: `src/mcp_ppt_native_fill/vision_layout.py`

```python
@dataclass(frozen=True)
class PlaceholderLayout:
    """One text-block placeholder inferred from vision."""
    role: str              # "title" | "subtitle" | "part" | "body" | ...
    text: str              # template like "{title}" or "{doc_title}"
    x: float               # absolute SVG canvas coord
    y: float
    w: float
    h: float
    font_size: int = 32
    font_weight: str = "normal"  # "normal" | "bold"
    confidence: float = 1.0     # 0..1, from LLM self-reported


@dataclass(frozen=True)
class SlideLayout:
    """Vision-inferred layout for one slide."""
    slide_index: int
    kind: str              # "cover" | "toc" | "divider" | "content" | "ending"
    placeholders: list[PlaceholderLayout]
    raw_response: dict    # full LLM response (for debugging)


@dataclass(frozen=True)
class TemplateLayoutProfile:
    """All slides' inferred layouts for one template."""
    schema_version: str = "1.0"
    template_hash: str
    canvas: tuple[int, int]  # (width, height) in SVG canvas units
    slides: list[SlideLayout]
    model_used: str
    inferred_at: float   # unix timestamp
    confidence_avg: float
```

### 4.2 Public function

```python
def infer_template_layout(
    pptx_path: Path,
    *,
    config: LLMConfig | None = None,
    enable_cache: bool = True,
    cache_dir: Path | None = None,
    max_image_dim: int = 1568,
) -> TemplateLayoutProfile:
    """Render each PPTX slide to PNG, send to vision LLM, return inferred
    layout. Caches result keyed by pptx-hash."""
```

Implementation steps:

1. `pptx_path` → run `pptx_to_svg` vendor → write SVG per slide
2. Render each SVG → PNG via cairosvg (resize to `max_image_dim`)
3. Build vision prompt:
   ```
   System: You are a PPT template layout analyzer. For each slide image,
   identify the visual structure of text placeholder rectangles and their
   semantic role. Output a JSON dict mapping slide_index to a list of
   {role, text_template, x, y, w, h, font_size, font_weight, confidence}.
   Coordinates must be absolute SVG canvas units (no %, no px suffix).
   Role values: "title", "subtitle", "part", "body", "footer", "header".
   text_template values: "{doc_title}", "{title}", "{doc_subtitle}",
   "{title_en}", "PART {nn}", "{body}", "{section_NN_title}", "{closing}".
   Only return roles you can confidently identify; if a slide has no
   text placeholders, return an empty placeholders list.
   User: Analyze these template slides (slide_01.png .. slide_NN.png):
   ```
4. Call `llm_client.llm_complete_vision_json(system, user, images)`
5. Parse JSON; validate coords are in canvas bounds + non-negative; clamp
6. Cache to `<cache_dir>/<pptx_hash>.json` with timestamp + model
7. Return `TemplateLayoutProfile`

### 4.3 Caching

```python
CACHE_VERSION = "1"

def _cache_key(pptx_path: Path) -> str:
    """Stable hash of pptx contents + size + mtime."""
    stat = pptx_path.stat()
    digest = hashlib.sha256()
    digest.update(str(pptx_path.resolve()).encode("utf-8"))
    with pptx_path.open("rb") as f:
        digest.update(f.read())  # full file hash
    return digest.hexdigest()[:16]


def _cache_path(pptx_path: Path, cache_dir: Path) -> Path:
    return cache_dir / f"{_cache_key(pptx_path)}-{CACHE_VERSION}.json"


def _load_cached(pptx_path: Path, cache_dir: Path) -> TemplateLayoutProfile | None:
    p = _cache_path(pptx_path, cache_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Validate schema_version + model — invalidate if different
        if data.get("schema_version") != CACHE_VERSION:
            return None
        return _deserialize_layout(data)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
```

---

## 5. Layer 3 — pipeline integration

### 5.1 `markdown_expand.py:_auto_fill_template_options`

After `inspect_template` produces `profile`, add vision-based layout:

```python
def _auto_fill_template_options(self, ctx, opts):
    # ... existing skeleton/toc/ending auto-fill ...

    # === Phase 24 vision-based layout inference ===
    injects: dict = {}
    vision_layout = self._maybe_infer_vision_layout(ctx, opts)
    if vision_layout is not None:
        for slide in vision_layout.slides:
            if slide.kind == "cover" and slide.placeholders:
                # Pass cover layouts; _apply_cover_layouts runs in workspace_expand
                injects["cover_layouts"] = [
                    _ph_to_dict(p) for p in slide.placeholders
                ]
            elif slide.kind == "divider":
                injects["divider_layouts"] = [
                    _ph_to_dict(p) for p in slide.placeholders
                ]
            elif slide.kind == "content":
                injects["content_layouts"] = [...]
            elif slide.kind == "ending":
                injects["ending_layouts"] = [...]
            elif slide.kind == "toc":
                # TOC has a card grid; pass list
                injects["toc_layouts"] = [...]
    return opts, injects
```

### 5.2 New helper: `_maybe_infer_vision_layout`

```python
def _maybe_infer_vision_layout(self, ctx, opts) -> TemplateLayoutProfile | None:
    """Run vision layout inference if enabled + vision-capable model configured.

    Caller opts:
      - enable_vision_layout (default: True if LLM configured + vision-capable)
      - vision_layout_model (default: cfg.model)
      - vision_layout_cache_dir (default: ~/.mcp_ppt_vision_cache)
    """
    from .vision_layout import infer_template_layout, _is_vision_capable
    cfg = LLMConfig.from_env()
    if not _is_vision_capable(opts.get("vision_layout_model") or cfg.model):
        return None
    pptx = ensure_ascii_path(ctx.source_pptx)
    return infer_template_layout(pptx, config=cfg, cache_dir=...)
```

### 5.3 `workspace_expand.py` extension

Replace current hardcoded `divider_inject_title` etc. with generic `_inject_<role>` keys:

```python
def expand_workspace_from_markdown(
    workspace, md_path, *,
    skeleton_divider, skeleton_content,
    divider_edits_template=None,
    content_edits_template=None,
    # === NEW: vision-inferred layouts ===
    cover_layouts: list[dict] | None = None,
    toc_layouts: list[dict] | None = None,
    divider_layouts: list[dict] | None = None,
    content_layouts: list[dict] | None = None,
    ending_layouts: list[dict] | None = None,
    body_bounds="0 0 1280 720",
    ...
):
    # Existing edits_template path (boteng compat) still works.
    # New *_layouts paths use svg_edits.add_text_block for inline
    # text-block injection — for templates without placeholder shapes.
    if cover_layouts:
        _apply_layouts(workspace / "authoring-svg-flat" / "slide_01.svg", cover_layouts, doc_title, ...)
    if divider_layouts:
        # Apply to each cloned divider slide
        for divider_svg in cloned_dividers:
            _apply_layouts(divider_svg, divider_layouts, section_title, ...)
```

Helper:
```python
def _apply_layouts(svg_path, layouts, **format_vars):
    for layout in layouts:
        text = layout["text"].format(**format_vars)
        svg_edits.add_text_block(
            svg_path,
            shape_id=layout.get("shape_id", f"_injected_{layout['role']}"),
            x=layout["x"], y=layout["y"],
            w=layout["w"], h=layout["h"],
            text=text,
            font_size=layout.get("font_size", 32),
            font_weight=layout.get("font_weight", "normal"),
        )
```

---

## 6. Output Schema (LLM must produce)

```json
{
  "schema_version": "1.0",
  "template_hash": "abc123def456",
  "canvas": {"width": 1280, "height": 720},
  "slides": [
    {
      "index": 1,
      "kind": "cover",
      "placeholders": [
        {"role": "title", "text": "{doc_title}", "x": 0.5, "y": 1.7,
         "w": 8.8, "h": 0.85, "font_size": 36, "font_weight": "bold",
         "confidence": 0.95},
        {"role": "subtitle", "text": "{doc_subtitle}", "x": 0.5, "y": 2.7,
         "w": 12.0, "h": 0.5, "font_size": 18, "font_weight": "normal",
         "confidence": 0.85}
      ]
    },
    {
      "index": 3,
      "kind": "divider",
      "placeholders": [
        {"role": "title", "text": "{title}", "x": 5.0, "y": 3.0,
         "w": 7.5, "h": 1.2, "font_size": 60, "font_weight": "bold",
         "confidence": 0.92},
        {"role": "part", "text": "PART {nn}", "x": 2.0, "y": 3.0,
         "w": 0.8, "h": 1.6, "font_size": 96, "font_weight": "bold",
         "confidence": 0.95}
      ]
    }
  ],
  "model_used": "claude-sonnet-5-20250929",
  "confidence_avg": 0.91
}
```

**Validation rules** (post-parse):
- Coordinates must be in canvas bounds (0 ≤ x, y, x+w ≤ width, y+h ≤ height)
- No `%` units, no `px` suffix — pure unitless numbers
- font_size ∈ [8, 200]
- confidence ∈ [0, 1]
- If validation fails, retry once with stricter prompt; if still fails, return
  empty placeholders for that slide and log warning

---

## 7. Failure Modes & Fallback Strategy

| Failure | Detection | Fallback |
|---|---|---|
| LLM not configured | `LLMConfig.from_env()` returns None | Skip vision; use text-shape auto-fill only |
| Model not vision-capable | `_is_vision_capable(model) == False` | Skip vision; log "model X is text-only" |
| Network timeout / 5xx | `LLMError` raised | Retry once after 2s; on 2nd failure, log + skip vision |
| Vision returns invalid JSON | `json.JSONDecodeError` | Retry once with stricter prompt; on 2nd failure, log + skip vision |
| Coordinates out of canvas bounds | post-validate | Clamp to canvas bounds; log warning |
| Confidence < 0.5 for any slide | post-validate | Drop that slide's placeholders; keep the rest |
| Cache file corrupt | `json.JSONDecodeError` on read | Delete cache file; re-infer |
| SVG render fails (cairosvg) | `cairosvg.Error` | Skip that slide's inference; proceed |
| Pipeline already past `_auto_fill_template_options` | `inspect_template` returned profile with no slide_kind | Use position-based fallback (current `_classify_slide` Rule 5) |

Vision is **always opt-in fallback-safe**. Worst case = current text-only auto-fill behavior.

---

## 8. Demo Control (caller opts)

Existing demos pass these:
```python
options = {
    "enable_llm_planner": True,        # existing
    # === NEW ===
    "enable_vision_layout": True,      # default True if LLM configured
    "vision_layout_model": None,       # default: use MCP_LLM_MODEL env
    "vision_layout_cache_dir": None,   # default: ~/.mcp_ppt_native_fill/vision_cache
    "vision_layout_max_image_dim": 1568,
    "vision_layout_min_confidence": 0.5,
}
```

Demos can disable vision explicitly:
```python
options = {"enable_vision_layout": False, ...}
```

Demos can force a specific model:
```python
options = {"vision_layout_model": "claude-sonnet-5-20250929", ...}
```

---

## 9. Phased Implementation

### Phase 24 commit 1 — `llm_client.py` vision support

Files: `src/mcp_ppt_native_fill/llm_client.py`
- Add `llm_complete_vision_json` + `_anthropic_complete_vision` +
  `_openai_compat_complete_vision`
- Add `_is_vision_capable` capability detection
- Add `_resize_png` (uses vendored cairosvg)
- Backward compat: text-only `llm_complete_json` unchanged

### Phase 24 commit 2 — `vision_layout.py` module + caching

Files: `src/mcp_ppt_native_fill/vision_layout.py` (new)
- `PlaceholderLayout`, `SlideLayout`, `TemplateLayoutProfile` dataclasses
- `infer_template_layout(pptx_path, ...)` public function
- `render_slide_pngs(pptx_path)` — vendor-pptx_to_svg + cairosvg→PNG
- `build_vision_prompt()` — system + user prompt
- `parse_and_validate_layout()` — JSON parse + coordinate validation
- Cache load/save helpers

### Phase 24 commit 3 — pipeline integration

Files:
- `src/mcp_ppt_native_fill/pipeline/handlers/markdown_expand.py`:
  - Add `_maybe_infer_vision_layout` method
  - Pass `injects["cover_layouts"]` / `divider_layouts` / etc. through to
    `expand_workspace_from_markdown`
- `src/mcp_ppt_native_fill/workspace_expand.py`:
  - Add `cover_layouts`, `toc_layouts`, `divider_layouts`, `content_layouts`,
    `ending_layouts` params to `expand_workspace_from_markdown`
  - Implement `_apply_layouts` helper
- `src/mcp_ppt_native_fill/svg_edits.py`:
  - Keep `add_text_block` (already supports absolute coords from Phase 23+ fix)

### Phase 24 commit 4 — demo + tests

Files:
- `tests/test_native_fill.py`: add `TestVisionLayout` test class
  - Mock LLM client responses
  - Verify cache hit / miss / invalidation
  - Verify coord validation (rejection of out-of-bounds)
- `examples/template_v2_demo.py` (or update boteng_auto_template2_v2md.py):
  - Add `enable_vision_layout: True` opt
  - Verify template_v2 fills cover/toc/divider/content/ending via vision

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Vision LLM cost ($) | Per-template cache miss = ~$0.05 with claude-sonnet-5 | Cache hits = $0; opt-out via `enable_vision_layout=False` |
| Vision LLM latency | +2-5s per template first-run | Cache; only one inference per template |
| Vision hallucination | Wrong coords → ugly PPTX | Confidence threshold (drop <0.5); coord validation (clamp); user can override via `expand_*_layouts` caller opts |
| Local LLM not vision | User runs `llama.cpp llama-3-8b` → vision silently fails | `_is_vision_capable` check + clear error log |
| `cairosvg` not available | PNG render fails | Fallback to PIL if available; else skip that slide |
| Cache file growth | Many templates → many cache files | LRU eviction at cache_dir level (max 100 entries) |
| Vendor coordinate format | Vendor rejects `%` in svg → pptx | Vision prompt EXPLICITLY requires unitless; validate post-parse |

---

## 11. Migration Impact

**Backward compat**: zero — vision is opt-in fallback. Existing demos that
don't configure LLM continue working as before (text-shape auto-fill only).

**Existing callsites that need attention**:
- `inspect_template`: no change (still text + position based)
- `_auto_fill_template_options`: now returns `(opts, injects)` tuple (already
  done in Phase 23+ commit 6; just add vision path)
- `expand_workspace_from_markdown`: signature adds optional `*_layouts` kwargs
- Tests: vision path is gated; existing tests unchanged

---

## 12. Open Questions

1. **Confidence threshold** — drop slide entirely if avg confidence < 0.5,
   or per-placeholder? → **per-placeholder** (drop only bad ones)
2. **Cache eviction** — LRU at 100 entries, or unbounded? → **100 entries
   with LRU**, configurable via `vision_layout_cache_max_entries`
3. **Multiple models** — let caller override per-call (`vision_layout_model`
   param)? → **yes**, fallback to `MCP_LLM_MODEL` env
4. **TOC layout** — 4 cards (1×4 grid) or 6 cards (2×3)? LLM must guess → trust
   LLM, fall back to current 4-card grid heuristic if invalid
5. **PNG resolution** — 1568 px max dim (Anthropic's recommendation);
   higher = more tokens but better fidelity. Make configurable.
6. **Multi-language templates** — vision must read Chinese/Japanese/Korean
   text. Most modern VLMs handle this. No special handling needed.

---

## 13. Out of Scope (Phase 25+)

- **Phase 25**: Auto-detect `data-pptx-placeholder` attributes from vendor
  SVG output (modify vendor to emit these markers) so vision-only fallback
  isn't needed.
- **Phase 26**: Per-page LLM call (one vision call per slide) for higher
  fidelity on long templates.
- **Phase 27**: Layout refinement loop — vision call → check output → re-call
  with corrections if low confidence.

---

**Author**: Claude
**Date**: 2026-09-22
**Status**: Plan mode (no code yet) — awaiting user approval
**Companion docs**:
- `docs/PHASE24_TEMPLATE_LAYOUT_CONFIG_PLAN.md` — earlier caller-driven plan (superseded by this vision-driven plan)
- `docs/PHASE23_PLUS_VENDOR_REFACTOR_PLAN_2026-09-20.md` — vendor refactor
- `docs/PIPELINE_PATTERN_DESIGN_2026-09-20.md` — Pipeline Pattern design