"""vision_layout.py — vision-LLM-driven template placeholder inference.

Phase 24 commit 2 (2026-09-22). When ``llm_client.llm_complete_vision_json``
is available, this module renders each PPTX slide to a PNG, sends the
PNGs to a vision-capable LLM, and parses back a structured layout
profile. Cached per-pptx-hash so cost is one-shot per template.

Why
---
The MCP pipeline's ``_auto_fill_template_options`` used to hardcode
boteng-specific shape IDs (shape-4 = PART {nn}, shape-5 = {title}).
Templates like ``template_v2.pptx`` with no text shapes on divider/content
slides were unable to be filled automatically. With vision inference, we
ask the LLM to look at the rendered slides and tell us where the
placeholder rectangles are.

This module is intentionally thin — the heavy lifting lives in:
  - ``llm_client.llm_complete_vision_json`` (HTTP + vision protocol)
  - ``cairosvg`` (SVG → PNG)
  - python-pptx-via-vendor (PPTX → SVG via ``vendor/pptx_master``)

The output schema is intentionally a single dict (TemplateLayoutProfile)
so callers can introspect it without depending on this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

log = logging.getLogger("mcp_ppt_native_fill.vision_layout")

# Bumped whenever the schema or prompt changes; old cache entries are
# invalidated automatically by ``_cache_path``.
CACHE_VERSION = "1"


@dataclass(frozen=True)
class PlaceholderLayout:
    """One text-block placeholder inferred from vision."""
    role: str               # "title" | "subtitle" | "part" | "body" | "footer" | "header"
    text: str               # template string e.g. "{title}", "PART {nn}"
    x: float                # absolute SVG canvas coord (no "%", no "px")
    y: float
    w: float
    h: float
    font_size: int = 32
    font_weight: str = "normal"  # "normal" | "bold"
    confidence: float = 1.0     # 0..1, from LLM self-report


@dataclass(frozen=True)
class SlideLayout:
    """Vision-inferred layout for one slide."""
    slide_index: int
    kind: str              # "cover" | "toc" | "divider" | "content" | "ending"
    placeholders: tuple[PlaceholderLayout, ...]
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TemplateLayoutProfile:
    """All slides' inferred layouts for one template."""
    schema_version: str = CACHE_VERSION
    template_hash: str = ""
    canvas_width: int = 1280
    canvas_height: int = 720
    slides: tuple[SlideLayout, ...] = ()
    model_used: str = ""
    inferred_at: float = 0.0       # unix timestamp
    confidence_avg: float = 0.0
    cache_hit: bool = False


# --- Vision prompt ------------------------------------------------------------

_VISION_SYSTEM_PROMPT = """\
You are a PPT template layout analyzer. The user will provide 1 to 10
PNG screenshots of a slide template. Each PNG is one slide (slide_01.png,
slide_02.png, ...). Your job is to identify the visual structure of
text placeholder rectangles and their semantic role on each slide.

For each slide, output a list of placeholder entries. Each entry must
be a JSON object with these EXACT keys:

  role          — one of "title", "subtitle", "part", "body",
                  "footer", "header", "decoration".
                  Use the most-specific semantic role you can identify
                  from the slide's visual structure (titles at the top,
                  body in the middle, footers/headers at the edges,
                  PART-label numbers near chapter dividers, etc.).
  text          — template string with placeholders the pipeline
                  substitutes at run time. Use "{doc_title}" for cover
                  titles, "{doc_subtitle}" for cover subtitles,
                  "{title}" for divider/content titles,
                  "{title_en}" for English subtitles,
                  "PART {nn}" for divider PART labels,
                  "{section_NN_title}" for TOC card titles (NN = 1-based
                  card index), "{body}" for content bodies,
                  "{closing}" for ending closing text. Use literal text
                  only when no template substitution applies.
  x, y, w, h    — bounding box in SVG canvas coordinates. MUST be
                  unitless decimal numbers in the SAME coordinate space
                  as the PNG image. NEVER use "%" units, "px" suffix, or
                  negative values.
  font_size     — estimated visible font size in pt. If unsure, use 24.
  font_weight   — "normal" or "bold".
  confidence    — your self-reported confidence in this inference, 0..1.

Return a single JSON object matching this schema (no prose, no markdown):

{
  "canvas_width": <integer>,
  "canvas_height": <integer>,
  "slides": [
    {"index": <1-based slide index>,
     "kind": "cover|toc|divider|content|ending",
     "placeholders": [ ... ]},
    ...
  ]
}

Rules:
- Only return placeholders you can confidently identify from the image.
- If a slide has NO text placeholders (pure decoration / picture-only),
  return an empty placeholders list for that slide.
- Coordinates must lie INSIDE the canvas (0 ≤ x, y; x+w ≤ canvas_width;
  y+h ≤ canvas_height). The pipeline clamps if you go slightly over.
- Use ABSOLUTE coordinates (no %, no px suffix). The pipeline does not
  understand relative units.
- For TOC slides with N card slots, emit one placeholder per card with
  role "section_card" and text "{section_NN_title}" where NN is 1-based.
"""


def _build_user_prompt(num_slides: int) -> str:
    return (
        f"Analyze these {num_slides} slide PNGs. Each PNG is rendered "
        f"from one slide of a PPT template. Output the structured layout "
        f"JSON described in the system prompt — nothing else."
    )


# --- Cache --------------------------------------------------------------------

def _pptx_content_hash(pptx_path: Path) -> str:
    """Stable hash of pptx file contents (used as cache key)."""
    digest = hashlib.sha256()
    digest.update(str(pptx_path.resolve()).encode("utf-8"))
    with pptx_path.open("rb") as f:
        # Read in 1 MiB chunks to avoid loading 100 MB+ PPTX into memory.
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def default_cache_dir() -> Path:
    """Default per-user cache directory."""
    base = Path(
        os.environ.get(
            "MCP_PPT_VISION_CACHE_DIR",
            str(Path.home() / ".mcp_ppt_native_fill" / "vision_cache"),
        )
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_path(pptx_path: Path, cache_dir: Path) -> Path:
    digest = _pptx_content_hash(pptx_path)
    return cache_dir / f"{digest}-{CACHE_VERSION}.json"


def _load_cached(
    pptx_path: Path,
    cache_dir: Path,
    template_kind_map: dict[int, str] | None = None,
) -> TemplateLayoutProfile | None:
    """Load and return a cached ``TemplateLayoutProfile``.

    ``template_kind_map`` (Phase 27 commit 1) is applied at load time
    so cached results that were originally produced with a hallucinated
    LLM kind (e.g. ``slide_04`` classified as ``toc``) get re-routed to
    the rule-based truth on every cache hit. Without this override, a
    stale cache would keep feeding the wrong SVG even after the pipeline
    is fixed.
    """
    p = _cache_path(pptx_path, cache_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        profile = _deserialize(data)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("vision_layout cache invalid (%s): %s", p, exc)
        return None
    # Re-apply the rule-based kind map so cached slide kinds always
    # reflect the rule-based truth, not the LLM's hallucinated kind.
    if template_kind_map and profile.slides:
        from dataclasses import replace as _replace
        new_slides = tuple(
            _replace(s, kind=template_kind_map.get(s.slide_index, s.kind))
            for s in profile.slides
        )
        profile = _replace(profile, slides=new_slides)
    return profile


def _save_cached(profile: TemplateLayoutProfile, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # profile.template_hash is the SHA256[:16] of the pptx contents —
    # match the same naming scheme as _cache_path / _load_cached.
    p = cache_dir / f"{profile.template_hash}-{CACHE_VERSION}.json"
    payload = _serialize(profile)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _cache_filename_for_hash(content_hash: str) -> str:
    return f"{content_hash}-{CACHE_VERSION}.json"


# --- Render PNGs --------------------------------------------------------------

def render_slide_pngs(
    pptx_path: Path,
    *,
    out_dir: Path | None = None,
    max_dim: int = 1568,
    timeout_s: int = 300,
) -> list[tuple[int, Path, int, int]]:
    """Convert pptx → SVG (via vendor) → PNG (via vendored cairosvg).

    Returns a list of ``(slide_index, png_path, width, height)``. PNGs are
    written to ``out_dir`` (auto-created if None). Each PNG is the rendered
    full slide at the pptx's native canvas size (resized down so the
    longest side ≤ ``max_dim`` to keep vision-token cost bounded).
    """
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix=".vision-layout-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    svg_dir = out_dir / "svg"
    png_dir = out_dir / "png"
    svg_dir.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)

    # Resolve vendor pptx_to_svg.py relative to THIS module's path so we
    # work no matter where the caller invokes us from. The module lives
    # at ``src/mcp_ppt_native_fill/vision_layout.py`` so the project
    # root is 3 ``.parent`` calls away.
    _here = Path(__file__).resolve()
    project_root = _here.parent.parent.parent
    vendor_pptx_to_svg = (
        project_root / "vendor" / "pptx_master" / "scripts" / "pptx_to_svg.py"
    )
    if not vendor_pptx_to_svg.exists():
        raise FileNotFoundError(
            f"vendor pptx_to_svg not found at {vendor_pptx_to_svg} "
            f"(project_root resolved to {project_root})"
        )

    # Vendor invocation uses cwd = scripts dir so relative imports work.
    subprocess.run(
        [
            sys.executable, "-u",
            str(vendor_pptx_to_svg),
            str(pptx_path), "-o", str(svg_dir),
            "--inheritance-mode", "both", "--roundtrip",
        ],
        check=True,
        cwd=str(vendor_pptx_to_svg.parent),
        timeout=timeout_s,
    )

    # Find authored svgs; vendored flat directory has slide_01..N.
    svg_files = sorted(svg_dir.rglob("slide_*.svg"))
    if not svg_files:
        raise RuntimeError(
            f"vendor produced no slide_*.svg in {svg_dir}"
        )

    results: list[tuple[int, Path, int, int]] = []
    for svg_path in svg_files:
        slide_index = _slide_index_from_name(svg_path.name)
        if slide_index is None:
            continue
        png_path = png_dir / f"{svg_path.stem}.png"
        _cairosvg_render(svg_path, png_path, max_dim)
        w, h = _png_dimensions(png_path)
        results.append((slide_index, png_path, w, h))
    results.sort(key=lambda r: r[0])
    return results


def _slide_index_from_name(name: str) -> int | None:
    import re
    m = re.match(r"slide_(\d+)", name)
    return int(m.group(1)) if m else None


def _cairosvg_render(svg_path: Path, png_path: Path, max_dim: int) -> None:
    try:
        import cairosvg
    except ImportError as exc:
        raise RuntimeError("cairosvg is required for vision_layout.render_slide_pngs") from exc
    cairosvg.svg2png(
        url=str(svg_path), write_to=str(png_path),
        output_width=max_dim,
    )


def _png_dimensions(png_path: Path) -> tuple[int, int]:
    """Return (width, height) in pixels of a PNG. Uses stdlib only
    (no PIL) by reading the IHDR chunk header."""
    try:
        with png_path.open("rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return 0, 0
            # IHDR chunk: 4-byte length + 4-byte type + 4-byte width + 4-byte height
            length_bytes = f.read(4)
            chunk_type = f.read(4)
            if chunk_type != b"IHDR":
                return 0, 0
            width = int.from_bytes(f.read(4), "big")
            height = int.from_bytes(f.read(4), "big")
            return width, height
    except OSError:
        return 0, 0


# --- LLM call + parse ---------------------------------------------------------

def _build_images_payload(png_results: list[tuple[int, Path, int, int]]) -> list[tuple[bytes, str]]:
    return [
        (png_path.read_bytes(), "image/png")
        for _idx, png_path, _w, _h in png_results
    ]


def _parse_response(
    raw: dict[str, Any],
    expected_canvas_width: int,
    expected_canvas_height: int,
    min_confidence: float = 0.5,
    template_kind_map: dict[int, str] | None = None,
) -> TemplateLayoutProfile | None:
    """Parse + validate the LLM JSON dict into TemplateLayoutProfile.

    Drops placeholders with confidence < min_confidence and clamps
    out-of-canvas coordinates. Returns None if the response is malformed
    beyond recovery.

    ``template_kind_map`` (Phase 27 commit 1): authoritative slide
    classification from ``inspect_template`` (rule-based, doesn't
    hallucinate). When provided, override the LLM's ``kind`` field
    for each slide. The LLM frequently misclassifies content slides
    as ``toc`` or vice versa because it's inferring from pixels; the
    rule-based inspector knows the template's structural skeleton and
    is the source of truth for routing vision-injected placeholders.
    """
    if not isinstance(raw, dict):
        return None
    cw = int(raw.get("canvas_width") or expected_canvas_width)
    ch = int(raw.get("canvas_height") or expected_canvas_height)
    slides_raw = raw.get("slides") or []
    if not isinstance(slides_raw, list):
        return None

    slide_layouts: list[SlideLayout] = []
    for slide_raw in slides_raw:
        if not isinstance(slide_raw, dict):
            continue
        idx = int(slide_raw.get("index") or 0)
        # Override the LLM's kind with the rule-based classifier's
        # truth when available. Otherwise keep the LLM's classification.
        if template_kind_map and idx in template_kind_map:
            kind = template_kind_map[idx]
        else:
            kind = str(slide_raw.get("kind", ""))
        kind = str(slide_raw.get("kind") or "")
        phs_raw = slide_raw.get("placeholders") or []
        phs: list[PlaceholderLayout] = []
        if isinstance(phs_raw, list):
            for ph in phs_raw:
                if not isinstance(ph, dict):
                    continue
                try:
                    confidence = float(ph.get("confidence", 1.0))
                except (TypeError, ValueError):
                    confidence = 1.0
                if confidence < min_confidence:
                    continue
                try:
                    x = float(ph["x"]); y = float(ph["y"])
                    w = float(ph["w"]); h = float(ph["h"])
                except (KeyError, TypeError, ValueError):
                    continue
                # Clamp to canvas.
                x = max(0.0, min(x, cw - w))
                y = max(0.0, min(y, ch - h))
                w = max(0.1, min(w, cw - x))
                h = max(0.1, min(h, ch - y))
                try:
                    font_size = int(ph.get("font_size", 32))
                except (TypeError, ValueError):
                    font_size = 32
                font_size = max(8, min(200, font_size))
                phs.append(PlaceholderLayout(
                    role=str(ph.get("role", "body")),
                    text=str(ph.get("text", "")),
                    x=x, y=y, w=w, h=h,
                    font_size=font_size,
                    font_weight=str(ph.get("font_weight", "normal")),
                    confidence=confidence,
                ))
        slide_layouts.append(SlideLayout(
            slide_index=idx, kind=kind,
            placeholders=tuple(phs), raw_response=slide_raw,
        ))
    slide_layouts.sort(key=lambda s: s.slide_index)

    if slide_layouts:
        avg = sum(
            ph.confidence
            for s in slide_layouts for ph in s.placeholders
        ) / max(1, sum(len(s.placeholders) for s in slide_layouts))
    else:
        avg = 0.0

    return TemplateLayoutProfile(
        canvas_width=cw, canvas_height=ch,
        slides=tuple(slide_layouts),
        inferred_at=time.time(), confidence_avg=avg,
    )


def _serialize(profile: TemplateLayoutProfile) -> dict[str, Any]:
    return {
        "schema_version": profile.schema_version,
        "template_hash": profile.template_hash,
        "canvas_width": profile.canvas_width,
        "canvas_height": profile.canvas_height,
        "model_used": profile.model_used,
        "inferred_at": profile.inferred_at,
        "confidence_avg": profile.confidence_avg,
        "slides": [
            {
                "index": s.slide_index,
                "kind": s.kind,
                "placeholders": [
                    {**asdict(p), "confidence": p.confidence}
                    for p in s.placeholders
                ],
            }
            for s in profile.slides
        ],
    }


def _deserialize(data: dict[str, Any]) -> TemplateLayoutProfile:
    """Inverse of :func:`_serialize`. Raises on malformed data."""
    if data.get("schema_version") != CACHE_VERSION:
        raise ValueError(
            f"schema_version mismatch: expected {CACHE_VERSION!r}, "
            f"got {data.get('schema_version')!r}"
        )
    cw = int(data["canvas_width"])
    ch = int(data["canvas_height"])
    slides_data = data.get("slides") or []
    slide_layouts: list[SlideLayout] = []
    for s in slides_data:
        phs_raw = s.get("placeholders") or []
        phs: list[PlaceholderLayout] = []
        for ph in phs_raw:
            phs.append(PlaceholderLayout(
                role=str(ph["role"]),
                text=str(ph["text"]),
                x=float(ph["x"]), y=float(ph["y"]),
                w=float(ph["w"]), h=float(ph["h"]),
                font_size=int(ph.get("font_size", 32)),
                font_weight=str(ph.get("font_weight", "normal")),
                confidence=float(ph.get("confidence", 1.0)),
            ))
        slide_layouts.append(SlideLayout(
            slide_index=int(s["index"]),
            kind=str(s.get("kind", "")),
            placeholders=tuple(phs),
            raw_response=s,
        ))
    return TemplateLayoutProfile(
        schema_version=data["schema_version"],
        template_hash=data.get("template_hash", ""),
        canvas_width=cw, canvas_height=ch,
        slides=tuple(slide_layouts),
        model_used=data.get("model_used", ""),
        inferred_at=float(data.get("inferred_at", 0.0)),
        confidence_avg=float(data.get("confidence_avg", 0.0)),
        cache_hit=True,
    )


# --- Public entry point -------------------------------------------------------

def infer_template_layout(
    pptx_path: Path,
    *,
    config: Any | None = None,           # llm_client.LLMConfig (avoid hard import)
    enable_cache: bool = True,
    cache_dir: Path | None = None,
    max_image_dim: int = 1568,
    min_confidence: float = 0.5,
    render_dir: Path | None = None,
    cleanup_render: bool = True,
    dry_run: bool = False,
    template_kind_map: dict[int, str] | None = None,
) -> TemplateLayoutProfile | None:
    """End-to-end vision layout inference for one PPTX template.

    1. Compute pptx-hash; check cache.
    2. Render each slide to PNG via vendor + cairosvg.
    3. Call ``llm_complete_vision_json`` with system + user prompts +
       inline image attachments.
    4. Parse + validate the JSON response (drop low-confidence, clamp
       out-of-canvas coordinates).
    5. Cache to ``cache_dir`` (default ``~/.mcp_ppt_native_fill/vision_cache``).
    6. Cleanup intermediate render dir unless ``cleanup_render=False``.

    Returns ``None`` when:
      - LLM is not configured / not vision-capable
      - network / vision call fails
      - cache + dry_run mode active (no real inference)

    Caller is expected to fall back to text-shape auto-fill when this
    returns None.
    """
    from .llm_client import (  # local import to avoid hard dep at module import
        llm_complete_vision_json, _is_vision_capable, LLMError,
    )

    pptx_path = Path(pptx_path)
    if not pptx_path.is_file():
        log.warning("vision_layout: pptx not found: %s", pptx_path)
        return None

    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    pptx_hash = _pptx_content_hash(pptx_path)
    profile_template_hash = pptx_hash

    if enable_cache:
        cached = _load_cached(
            pptx_path, cache_dir,
            template_kind_map=template_kind_map,
        )
        if cached is not None:
            log.info(
                "vision_layout cache hit for %s (hash=%s, model=%s)",
                pptx_path.name, pptx_hash, cached.model_used,
            )
            # Re-stamp the hash so callers can re-save under the same key.
            from dataclasses import replace
            return replace(cached, template_hash=profile_template_hash, cache_hit=True)

    if config is None:
        from .llm_client import LLMConfig
        try:
            config = LLMConfig.from_env()
        except ValueError as exc:
            log.warning("vision_layout: LLM not configured: %s", exc)
            return None

    if not _is_vision_capable(config.model):
        log.warning(
            "vision_layout: model %s is not vision-capable; "
            "skipping layout inference", config.model,
        )
        return None

    if dry_run:
        log.info("vision_layout: dry_run=True; not calling vision LLM")
        return None

    # --- Render slides ---
    try:
        png_results = render_slide_pngs(
            pptx_path, out_dir=render_dir, max_dim=max_image_dim,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("vision_layout: render failed (%s): %s", type(exc).__name__, exc)
        return None

    if not png_results:
        return None

    images = _build_images_payload(png_results)

    system = _VISION_SYSTEM_PROMPT
    user = _build_user_prompt(len(png_results))

    # --- Call LLM ---
    try:
        raw = llm_complete_vision_json(
            system=system, user=user, images=images, config=config,
            max_image_dim=max_image_dim,
        )
    except LLMError as exc:
        log.warning(
            "vision_layout: LLM call failed (%s); skipping",
            type(exc).__name__, exc,
        )
        if cleanup_render and render_dir is not None:
            shutil.rmtree(render_dir, ignore_errors=True)
        return None

    # --- Parse + validate ---
    cw, ch = (png_results[0][2], png_results[0][3])
    profile = _parse_response(
        raw, cw, ch, min_confidence=min_confidence,
        template_kind_map=template_kind_map,
    )
    if profile is None:
        log.warning("vision_layout: response could not be parsed; skipping")
        if cleanup_render and render_dir is not None:
            shutil.rmtree(render_dir, ignore_errors=True)
        return None

    # Attach the pptx hash + model name; cache.
    from dataclasses import replace as _replace
    profile = _replace(
        profile,
        template_hash=profile_template_hash,
        model_used=config.model,
    )

    if enable_cache:
        try:
            _save_cached(profile, cache_dir)
        except OSError as exc:
            log.warning("vision_layout: cache save failed: %s", exc)

    if cleanup_render and render_dir is not None:
        shutil.rmtree(render_dir, ignore_errors=True)

    return profile


# Late-imported modules used by render_slide_pngs + cleanup_render.
import shutil  # noqa: E402
import tempfile  # noqa: E402