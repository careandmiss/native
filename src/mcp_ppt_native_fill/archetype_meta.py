"""Single source of truth: archetype -> metadata.

This module provides ``ARCHETYPE_META``, a dictionary keyed by archetype name that
drives how each archetype is rendered in the PPT content pages. Every entry
encodes four independent drivers that together describe how a slide's content
should be composed and framed:

1. ``rhythm`` -- the page rhythm (``"anchor"`` for a single declarative takeaway,
   ``"dense"`` for many data/comparison items, ``"breathing"`` for one thought
   that needs room around it). Influences font-size tier and whitespace budget.

2. ``reading_mode`` -- the reader's attention profile (``"presentation"`` for
   auditoriums, ``"balanced"`` for mixed screens, ``"text"`` for docs/screen
   reading). Drives line-length and density tuning.

3. ``body_bounds`` -- a ``(x, y, w, h)`` tuple on the 1280x720 SVG canvas that
   the block renderer clips to. Different archetypes get different bounds so
   the visual weight matches the content (hero archetypes get a tighter, more
   centered frame; dense archetypes get a wider frame).

4. ``chrome_overrides`` -- a dict of three booleans
   (``suppress_topbar``, ``suppress_footer``, ``suppress_section_divider``)
   that the chrome layer consults before injecting decorations. Hero
   archetypes (single-quote / single-number) suppress the topbar to give the
   statement room to breathe; everything else keeps the standard chrome.

In addition, ``source_slide_hint`` is the Layer-2 routing value: each archetype
maps to one of three cloned template slides (6=hero, 7=wide, 8=full) so the
content physically comes from a template variant with matching chrome. The
``recipe`` field is reserved for the (deferred) Layer-3 recipe name; all
entries are ``None`` in this iteration.

``DEFAULT_META`` points at the ``"raw"`` archetype's metadata. It is the safe
fallback for any unknown archetype name encountered by callers (block
renderer, chrome layer, pipeline).
"""

from __future__ import annotations

from typing import TypedDict


class ArchetypeMeta(TypedDict):
    """Metadata describing how to render a single archetype.

    Attributes:
        rhythm: Page rhythm hint -- ``"anchor"`` | ``"dense"`` | ``"breathing"``.
        reading_mode: Reader profile -- ``"text"`` | ``"balanced"`` |
            ``"presentation"``.
        body_bounds: ``(x, y, w, h)`` body clip rectangle on the 1280x720 SVG
            canvas (top-left origin).
        chrome_overrides: Mapping with ``suppress_topbar``, ``suppress_footer``,
            ``suppress_section_divider`` boolean keys.
        source_slide_hint: Index of the template slide to clone for this
            archetype (Layer-2 routing). Hero -> 6, wide -> 7, full -> 8.
        recipe: Layer-3 recipe name, or ``None`` (deferred).
    """

    rhythm: str
    reading_mode: str
    body_bounds: tuple[float, float, float, float]
    chrome_overrides: dict
    source_slide_hint: int
    recipe: str | None


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