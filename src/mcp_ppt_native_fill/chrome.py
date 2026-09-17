"""Persistent page chrome helpers.

Phase 11 (2026-09-17): Every content slide now carries a small "page
chrome" — a topbar (chapter / section label + accent line) and a footer
(doc path + slide number). This is the structural change we adopted from
``ppt-master``'s real generated SVG (see
``docs/PHASE11_NATIVE_FILL_SVG_REFORM_2026-09-17.md``).

Pure functions; no shell rendering — the pipeline injects the resulting
SVG fragments into the slide authoring SVG at the appropriate bounds.
"""
from __future__ import annotations

from typing import Iterable


# Phase 11 palette. Deep-blue base matches the boteng template brand
# (#1D2CAB); gold accent (#D4A24C) is reused verbatim from ppt-master.
BRAND_BLUE = "#1D2CAB"
BRAND_BLUE_LIGHT = "#2A4DCB"
GOLD = "#D4A24C"
MUTED_INK = "#5A6678"
LIGHT_RAIL_BG = "#F4F7FB"

# Phase 11 chrome geometry — chosen to match ppt-master's
# svg_final/*.svg exactly so that the visual product lands in the same
# spot on the 1280x720 canvas.
TOPBAR_TOP = 32.0
TOPBAR_LABEL_Y = 44.0
TOPBAR_ACCENT_X1 = 64.0
TOPBAR_ACCENT_X2 = 160.0
TOPBAR_ACCENT_Y = 56.0

FOOTER_Y = 700.0
FOOTER_HEIGHT = 30.0


def render_chrome_topbar(
    *,
    chapter_label: str,
    brand_blue: str = BRAND_BLUE,
    gold: str = GOLD,
    muted_ink: str = MUTED_INK,
    light_bg: str = LIGHT_RAIL_BG,
    topbar_top: float = TOPBAR_TOP,
    label_y: float = TOPBAR_LABEL_Y,
    accent_x1: float = TOPBAR_ACCENT_X1,
    accent_x2: float = TOPBAR_ACCENT_X2,
    accent_y: float = TOPBAR_ACCENT_Y,
    letter_spacing: float = 4.0,
) -> str:
    """Render the persistent topbar at the slide top.

    Geometry matches ppt-master's
    ``projects/boteng_ppt_20260916/svg_final/0?_content.svg`` lines
    6-9: 12px bold uppercase letter-spaced chapter label on a 96px
    gold accent line at y=56.

    Args:
        chapter_label: e.g. ``"PART 1 · 第一章 总则"`` or ``"PREFACE · 前 言"``.
            Caller is responsible for any "PART X ·" prefix; we render
            the string verbatim (uppercase prefix is up to the caller).
        brand_blue: topbar label color (defaults to brand blue).
        gold: accent line color.
        muted_ink: fallback muted ink.
        light_bg: background fill color for the bar region.
        topbar_top / label_y / accent_x1 / accent_x2 / accent_y /
            letter_spacing: layout knobs (defaults match ppt-master).
    """
    return (
        f'<g id="slide-topbar" data-pptx-role="decoration" '
        f'data-pptx-bounds="64 {topbar_top:g} 1216 36">'
        f'<text x="64" y="{label_y:g}" font-size="12" font-weight="bold" '
        f'fill="{muted_ink}" letter-spacing="{letter_spacing:g}">'
        f'{_esc(chapter_label)}</text>'
        f'<line x1="{accent_x1:g}" y1="{accent_y:g}" '
        f'x2="{accent_x2:g}" y2="{accent_y:g}" stroke="{gold}" '
        f'stroke-width="2"/>'
        f'</g>'
    )


def render_chrome_footer(
    *,
    doc_path: str,
    page_num: int,
    total_pages: int,
    muted_ink: str = MUTED_INK,
    footer_y: float = FOOTER_Y,
    font_size: float = 11.0,
) -> str:
    """Render the persistent footer at the slide bottom.

    Geometry matches ppt-master's footer at line 38-41 of every
    ``svg_final/*.svg``: left text ``"采购制度 / 山西柏腾科技有限公司"``
    right text ``"PN / 07"``.
    """
    left = _esc(doc_path)
    right = f"P{page_num:02d} / {total_pages:02d}"
    return (
        f'<g id="slide-footer" data-pptx-role="decoration" '
        f'data-pptx-bounds="64 680 1216 30" fill="{muted_ink}" '
        f'font-size="{font_size:g}">'
        f'<text x="64" y="{footer_y:g}">{left}</text>'
        f'<text x="1216" y="{footer_y:g}" text-anchor="end">{right}</text>'
        f'</g>'
    )


def _esc(text: str) -> str:
    """XML-escape a chrome label.

    Kept private — block_renderer.escape is the public version. We
    import nothing from block_renderer to avoid a cycle.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__: Iterable[str] = (
    "render_chrome_topbar",
    "render_chrome_footer",
    "BRAND_BLUE",
    "BRAND_BLUE_LIGHT",
    "GOLD",
    "MUTED_INK",
    "LIGHT_RAIL_BG",
)