"""Auto-fit body bounds to actual content size.

Phase 17-B (2026-09-18). When a content slide has far less content than
its archetype_meta body_bounds allows, half the page is empty. When it
has more content than fits, text overflows the frame. The optimizer
collapses the bounds in both dimensions to fit the actual content,
keeping the original aspect ratio centered.

Pure functions; no I/O. Three exports:

* :func:`compute_optimal_bounds` — pick a ``(x, y, w, h)`` tuple that
  snug-fits the spec's content. Returns the same shape as
  ``archetype_meta.ARCHETYPE_META[*]["body_bounds"]``.

* :func:`compute_optimal_font_size` — pick a font size in [min_font,
  base_font] such that the text fits within the container width /
  height. Wraps the existing :func:`block_renderer._body_lines_cascade`
  in a content-size-aware interface.

* :func:`bounds_to_str` — format the tuple as the ``"x y w h"`` string
  consumed by ``svg_edits.write_new_content_block`` / ``block_renderer``.

The optimizer is conservative: when in doubt, it returns the
archetype_meta default. We never make bounds larger than the default
(negative space is part of the archetype design — hero archetypes
should keep their tight, centered frames).
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .archetype_meta import ARCHETYPE_META, DEFAULT_META
from .text_width import chars_that_fit


# Approximate CJK line height at a given font size. Empirical: a CJK
# glyph is ~1.0-1.2x font size tall; with 1.15 line-height we get the
# line height used by the renderer.
_LINE_HEIGHT_RATIO = 1.45

# Item row height (used by 3-column-cards / bullet-list) at the
# renderer's default body font size (16px).
_ITEM_ROW_HEIGHT = 24.0
_ITEM_PADDING_TOP = 8.0
_ITEM_PADDING_BOTTOM = 14.0

# Card padding for 3-column-cards (top + bottom + heading row).
_CARD_PADDING = 56.0

# Slide-canvas anchor for centering. body_bounds is on the 1280x720
# canvas; we keep x/y aligned to the archetype defaults but allow w/h
# to shrink.
_DEFAULT_CONTAINER: tuple[float, float, float, float] = (
    64.0, 110.0, 1152.0, 569.0,
)


def _spec_text_length(spec: dict[str, Any]) -> int:
    """Total character count of all prose in a spec (rough).

    Used to estimate the rendered height for hero archetypes and
    statement-caption (where the only meaningful input is the body).
    """
    if not isinstance(spec, dict):
        return 0
    n = 0
    for k in ("body", "text", "headline", "subline", "quote"):
        v = spec.get(k)
        if isinstance(v, str):
            n += len(v)
    cards = spec.get("cards") or []
    for c in cards:
        if isinstance(c, dict):
            t = c.get("title")
            if isinstance(t, str):
                n += len(t)
            for it in (c.get("items") or []):
                if isinstance(it, str):
                    n += len(it)
    steps = spec.get("steps") or []
    for s in steps:
        if isinstance(s, dict):
            for k in ("label", "detail", "title"):
                v = s.get(k)
                if isinstance(v, str):
                    n += len(v)
    return n


def _spec_max_items_per_card(spec: dict[str, Any]) -> int:
    """Largest item count across all cards (for 3-column-cards)."""
    if not isinstance(spec, dict):
        return 0
    cards = spec.get("cards") or []
    if not cards:
        return 0
    max_items = 0
    for c in cards:
        if not isinstance(c, dict):
            continue
        items = c.get("items") or []
        max_items = max(max_items, len(items))
    return max_items


def _spec_card_count(spec: dict[str, Any]) -> int:
    if not isinstance(spec, dict):
        return 0
    cards = spec.get("cards") or []
    return len(cards)


def _spec_step_count(spec: dict[str, Any]) -> int:
    if not isinstance(spec, dict):
        return 0
    steps = spec.get("steps") or []
    return len(steps)


def _spec_bullet_count(spec: dict[str, Any]) -> int:
    if not isinstance(spec, dict):
        return 0
    items = spec.get("items") or []
    return len(items)


def _align_origin(
    default_x: float, default_y: float,
    new_w: float, new_h: float,
    default_w: float, default_h: float,
) -> tuple[float, float]:
    """Keep the archetype's anchor x/y, but shrink w/h.

    When the new content is shorter, we keep the top-left origin so
    the slide reads top-down; the extra space below the body is empty
    but consistent with hero-style "breathing room" above the content.

    When the new content is wider/taller than default, we cap at the
    default (we never grow bounds).
    """
    return default_x, default_y


def compute_optimal_bounds(
    layout: str,
    spec: dict[str, Any] | None = None,
    *,
    container: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Pick body bounds that snug-fit ``spec`` for ``layout``.

    Returns ``(x, y, w, h)`` on the 1280x720 canvas.

    Strategy per layout:

    * 3-column-cards: shrink height to ``_CARD_PADDING + max_items *
      _ITEM_ROW_HEIGHT + 8`` (the largest card's rendered height),
      capped at the archetype default. Width is unchanged.
    * procedural-steps / flow-steps: shrink height to ``n_steps * 90 +
      40``, capped at default.
    * bullet-list: shrink height to ``_ITEM_PADDING_TOP + n_items *
      _ITEM_ROW_HEIGHT + _ITEM_PADDING_BOTTOM``, capped at default.
    * kpi_row: shrink height to ``n_tiles * 110 + 80``, capped.
    * hero_statement / callout-box / hero-number / statement-caption:
      KEEP defaults (negative space is the design).
    * revision-table / two-column-compare / comparison / matrix_2x2 /
      three-thesis-cards: KEEP defaults (these layouts are dense by
      nature; shrinking looks wrong).
    """
    spec = spec or {}
    if container is not None:
        cx, cy, cw, ch = container
        default = (cx, cy, cw, ch)
    else:
        meta = ARCHETYPE_META.get(layout, DEFAULT_META)
        default = tuple(meta["body_bounds"])  # type: ignore[assignment]

    dx, dy, dw, dh = default

    if layout == "3-column-cards":
        max_items = _spec_max_items_per_card(spec)
        n_cards = _spec_card_count(spec)
        if max_items > 0 and n_cards > 0:
            needed = _CARD_PADDING + max_items * _ITEM_ROW_HEIGHT + 12
            if needed < dh:
                # Vertical-center within the default bounds.
                return dx, dy + (dh - needed) / 2, dw, needed
        return default

    if layout in ("procedural-steps", "flow-steps"):
        n_steps = _spec_step_count(spec)
        if n_steps >= 3:
            needed = n_steps * 92.0 + 40.0
            if needed < dh:
                return dx, dy + (dh - needed) / 2, dw, needed
        return default

    if layout == "bullet-list":
        n_items = _spec_bullet_count(spec)
        if n_items > 0:
            needed = _ITEM_PADDING_TOP + n_items * _ITEM_ROW_HEIGHT + _ITEM_PADDING_BOTTOM
            if needed < dh:
                return dx, dy + (dh - needed) / 2, dw, needed
        return default

    if layout == "kpi_row":
        tiles = spec.get("tiles") or [] if isinstance(spec, dict) else []
        n = len(tiles)
        if n >= 2:
            needed = n * 110.0 + 80.0
            if needed < dh:
                return dx, dy + (dh - needed) / 2, dw, needed
        return default

    # Hero archetypes + dense layouts: keep defaults (negative space).
    return default


def compute_optimal_font_size(
    text: str,
    *,
    container_w: float,
    container_h: float,
    base_font: float = 16.0,
    min_font: float = 12.0,
    shrink_step: float = 1.0,
    line_h_ratio: float = _LINE_HEIGHT_RATIO,
) -> float:
    """Pick the largest font in ``[min_font, base_font]`` that fits.

    Algorithm:
      1. Compute chars_per_line at ``base_font`` (CJK-aware via
         :func:`text_width.chars_that_fit`).
      2. Estimate lines_needed = ceil(len(text) / chars_per_line).
      3. lines_h = lines_needed * base_font * line_h_ratio.
      4. While ``lines_h > container_h`` AND font > min_font: shrink
         font by ``shrink_step`` and recompute (chars_per_line grows
         proportionally so lines_needed falls).
      5. Return the smallest font that fits, or ``min_font`` if even
         the minimum cannot fit (caller should fall back to overflow
         truncation).
    """
    if not text:
        return base_font
    font = float(base_font)
    n_chars = len(text)
    while font > min_font:
        cpl = chars_that_fit(container_w, font)
        if cpl <= 0:
            cpl = max(1, n_chars)
        lines = (n_chars + cpl - 1) // cpl  # ceil
        line_h = font * line_h_ratio
        if lines * line_h <= container_h:
            return font
        font -= shrink_step
    return min_font


def bounds_to_str(bounds: tuple[float, float, float, float]) -> str:
    """Format ``(x, y, w, h)`` as ``"x y w h"`` for SVG attributes."""
    x, y, w, h = bounds
    return f"{x:g} {y:g} {w:g} {h:g}"


__all__: tuple[str, ...] = (
    "compute_optimal_bounds",
    "compute_optimal_font_size",
    "bounds_to_str",
    "DEFAULT_CONTAINER",
)