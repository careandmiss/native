"""Render ``new_content_block`` specs into raw SVG children.

Pure functions for converting planner-emitted block specs into the
SVG markup that gets injected into authoring SVGs at the
``bounds`` rectangle. Extracted from ``pipeline.py`` so the state
machine file stays focused on phase orchestration.

Layouts supported:
    * ``raw``               — caller supplied SVG fragments.
    * ``3-column-cards``    — 1-4 tile row (KPI / feature cards).
    * ``flow-steps``        — 2-5 numbered horizontal flow with arrows.
    * ``revision-table``    — 4-column table (date/status/content/author).
    * ``hero-number``       — 1 large centered value + caption.
    * ``callout-box``       — quotation panel with RTL-aware quote glyph.
    * ``two-column-compare``— pros/cons or A/B comparison.
    * ``timeline``          — 2-5 step horizontal axis with adaptive margin.

Public API:
    * :func:`coerce_str_list` — robust coerce of LLM-emitted fields.
    * :func:`escape`          — XML-escape a label for SVG interpolation.
    * :func:`render_new_block`— the main entry point.
"""

from __future__ import annotations

import re
from typing import Any

from xml.etree import ElementTree as ET

# Phase 14+ (2026-09-18): content-adaptive layout -- per-archetype
# body bounds + chrome overrides. Lazy/conditional import so this
# module degrades gracefully if archetype_meta is absent (older
# deployments / standalone usage).
try:
    from . import archetype_meta  # noqa: F401  (consulted at runtime)
except ImportError:  # pragma: no cover -- defensive fallback
    archetype_meta = None  # type: ignore[assignment]


def coerce_str_list(value: Any, sep: str = "; ") -> list[str]:
    """Coerce an LLM-emitted field into a flat list[str].

    The planner may return ``items`` / ``rows`` / etc. as either a list
    of strings, a single string (treat as 1-item list), or a dict (treat
    as key=value lines). This helper makes downstream rendering robust
    against the variety of shapes the LLM emits.
    """
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            if isinstance(v, (list, tuple)):
                out.append(sep.join(str(x) for x in v))
            elif isinstance(v, dict):
                out.append(sep.join(f"{k}={val}" for k, val in v.items()))
            else:
                out.append(str(v))
        return out
    if isinstance(value, tuple):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [sep.join(f"{k}={v}" for k, v in value.items())]
    return [str(value)]


def escape(text: str) -> str:
    """XML-escape a label for safe interpolation into SVG."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Phase 11 (2026-09-17): Chinese number sequence used by
# procedural-steps to render "阶段一 / 阶段二 / 阶段三 / 阶段四".
_CN_NUM = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")


def _cn_num(n: int) -> str:
    """Return the Chinese ordinal for a 1-based phase number."""
    if 1 <= n < len(_CN_NUM):
        return _CN_NUM[n]
    return str(n)


def _compute_max_body_lines(
    *,
    bh: float,
    by: float,
    body_y0: float,
    line_h: float,
    block_after_h: float = 0.0,
) -> int:
    """How many body lines fit given body bounds + line height + below-block.

    Used by statement-caption and hero_statement to derive a height-driven
    line cap instead of hard-coding ``max_lines = 4``.
    """
    available = bh - (body_y0 - by) - block_after_h
    return max(1, int(available // line_h))


def _body_lines_cascade(
    body_lines: list[str],
    *,
    max_lines: int,
    font_attr: str,
    body_font: float,
    min_font: float = 12.0,
    shrink_step: float = 2.0,
) -> tuple[list[str], str]:
    """Cascade: trim to max_lines → shrink font → truncate.

    Returns the body lines and the (possibly reduced) font_size string.

    Algorithm:
      1. If ``body_lines`` fits within ``max_lines``, return as-is.
      2. Otherwise, shrink font by ``shrink_step`` (recompute max_lines
         proportionally) until either fits or font reaches ``min_font``.
      3. If still doesn't fit, truncate to ``max_lines`` (final fallback).
    """
    if len(body_lines) <= max_lines:
        return body_lines, font_attr
    cur_font = float(font_attr) if font_attr else body_font
    cur_max = max_lines
    while cur_font > min_font and len(body_lines) > cur_max:
        cur_font -= shrink_step
        # Recompute how many lines fit at the new font size.
        cur_max = max(max_lines, int((body_font / cur_font) * max_lines))
    return body_lines[:cur_max], f"{cur_font:g}"


def _fs(spec: dict[str, Any], role: str) -> str:
    """Resolve font-size for a per-call usage role.

    Resolution order:
      1. If ``spec.font_size`` is a TYPOGRAPHY key matching ``role``,
         return that anchor (role-name-based, e.g. ``"page_title"``).
      2. If ``spec.font_size`` is a raw px value, find the TYPOGRAPHY
         role whose default matches that px within ±2 and apply ONLY
         to that role; other roles fall back to per-role TYPOGRAPHY.
      3. Otherwise, return the per-call role's TYPOGRAPHY default.

    This prevents the bug where ``spec.font_size="32"`` was being
    applied to every text element on the slide (bullets, phase labels,
    timeline circles), causing vertical text overlap in the 2x2
    procedural-steps grid. With this rewrite, ``spec.font_size="32``
    applies only to the page_title tier (TYPOGRAPHY=32), while bullets
    (TYPOGRAPHY=13), phase labels (TYPOGRAPHY=12), and section labels
    (TYPOGRAPHY=13) each get their proper per-role size.
    """
    nested = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    payload = nested if nested else spec
    spec_fs = payload.get("font_size")
    if spec_fs is not None and spec_fs != "":
        spec_fs_str = str(spec_fs)
        # Case 1: spec.font_size is a role name (e.g. "page_title").
        if spec_fs_str in TYPOGRAPHY:
            if role == spec_fs_str:
                return str(TYPOGRAPHY[spec_fs_str])
            # Role-name mismatch: fall through to per-role TYPOGRAPHY default.
        else:
            # Case 2: raw px value. Auto-match to a TYPOGRAPHY role whose
            # default is within ±2 of this px; only apply to that role.
            try:
                target_px = float(spec_fs_str)
            except ValueError:
                target_px = None
            if target_px is not None:
                for t_role, t_px in TYPOGRAPHY.items():
                    if abs(float(t_px) - target_px) <= 2.0 and role == t_role:
                        return str(t_px)
    # Default: per-call role's TYPOGRAPHY anchor (or fallback 13).
    return str(TYPOGRAPHY.get(role, "13"))


# Phase 14 (2026-09-17): typography anchors from
# projects/boteng_ppt_20260916/design_spec.md IV Font Size Hierarchy.
# Single source of truth for role -> px mapping (ppt-master
# executor-base.md:207 -- "map every structural text item to a
# declared typography role and write its anchor or a value within
# +/- 2 px"). All call sites below resolve to one of these roles;
# tweak type on a deck-wide basis by editing THIS dict (and the
# matching LLM prompt in llm_planner.py IV), not the call sites.
TYPOGRAPHY: dict[str, int] = {
    # design_spec IV tier anchors
    "cover_title": 56,
    "chapter_title": 42,
    "page_title": 32,
    "subtitle": 24,
    "lead": 20,
    "body": 16,
    "annotation": 13,
    "footnote": 11,
    # Inter-tier roles used by zhen-pin SVGs (between two anchors,
    # always within +/- 2 px of a named tier per executor-base.md:207).
    "claim_band": 22,        # panel title (between subtitle=24 and lead=20)
    "rail_title": 32,        # alias of page_title
    "rail_caption": 14,      # eyebrow / caption (between body=16 and footnote=11)
    "rail_body": 12,         # between body=16 and annotation=13
    "doc_code": 11,          # footnote-tier rail footer
    "panel_title": 22,       # alias of claim_band
    "panel_en_subtitle": 14, # alias of rail_caption
    "big_quote": 72,         # display, cover_title + 16
    "takeaway_label": 14,    # alias of rail_caption
    "takeaway_body": 20,     # lead
    "en_subtitle": 14,       # alias of rail_caption
    "big_question": 32,      # alias of page_title
    "keyword_word": 16,      # body
    "keyword_en": 11,        # footnote
    "subtitle_14": 14,       # alias
    "phase_num": 12,         # annotation - 1
    "phase_label": 13,       # annotation
    "phase_card_label": 14,
    "phase_card_section": 13,
    "bullet": 13,
    "header": 14,
    "cell_body": 13,
    "placeholder_hint": 13,
}


def render_new_block(spec: dict[str, Any]) -> str:
    """Render a ``new_content_block`` spec into raw SVG children.

    Supports the layouts: ``"raw"`` (caller supplied SVG),
    ``"3-column-cards"`` (tile row), ``"flow-steps"`` (numbered
    horizontal flow), ``"revision-table"`` (header + rows),
    ``"hero-number"``, ``"callout-box"``, ``"two-column-compare"``,
    ``"timeline"``, ``"simple-text"``, ``"bullet-list"``,
    ``"statement-caption"``, ``"procedural-steps"``,
    ``"three-thesis-cards"``, ``"hero_statement"``, ``"kpi_row"``.
    Anything else raises.

    Layout-specific spec keys are nested under ``spec["spec"]`` when the
    caller uses the planner shape; the helper accepts both forms so
    legacy callers passing spec.flat still work.
    """
    layout = spec.get("layout", "raw")
    # Accept both {"layout":..., "cards":[...]} and
    # {"layout":..., "spec": {"cards":[...]}} — Phase A planners use the
    # latter; legacy test fixtures use the former.
    nested = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    payload = nested if nested else spec

    # Phase 14+ (2026-09-18): resolve per-archetype body bounds.
    # Priority: spec/payload "bounds" string (backward compat with
    # existing test fixtures + callers) wins; otherwise consult
    # archetype_meta for the per-layout body_bounds; otherwise use
    # the legacy uniform fallback (83, 110, 1203, 569).
    if archetype_meta is not None:
        _meta = archetype_meta.ARCHETYPE_META.get(
            layout, archetype_meta.DEFAULT_META
        )
        _body_bounds_default: tuple[float, float, float, float] = tuple(
            float(v) for v in _meta["body_bounds"]
        )
    else:  # pragma: no cover -- defensive fallback
        _body_bounds_default = (83.0, 110.0, 1203.0, 569.0)

    if layout == "raw":
        inner = payload.get("svg", "") or spec.get("svg", "")
        if not inner:
            raise ValueError("layout='raw' requires spec.svg")
        return inner
    if layout == "3-column-cards":
        cards = payload.get("cards") or []
        if not 1 <= len(cards) <= 4:
            raise ValueError(
                "3-column-cards supports 1-4 cards per row"
            )
        parts: list[str] = []
        n = len(cards)
        # Geometry derived from bounds; we expect bounds "x y w h".
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        gap = 16.0
        card_w = (bw - gap * (n - 1)) / n
        # Phase 17-C (2026-09-18): auto-shrink item font per-card when
        # the longest item would overflow the card width. Different
        # cards may end up at different font sizes — that's fine, the
        # title font stays at 18 across cards.
        from .text_width import chars_that_fit as _cpl_card
        card_inner_w = max(80.0, card_w - 32.0)  # 16px padding each side
        for i, card in enumerate(cards):
            cx = bx + i * (card_w + gap)
            color = card.get("color", "#1D2CAB")
            title = card.get("title", "")
            items = coerce_str_list(card.get("items", []))
            max_len = max((len(s) for s in items), default=0)
            item_font = 14.0
            for try_font in (14.0, 13.0, 12.0):
                if _cpl_card(card_inner_w, try_font) >= max_len:
                    item_font = try_font
                    break
            else:
                item_font = 12.0
            parts.append(
                f'<rect x="{cx:g}" y="{by:g}" width="{card_w:g}" '
                f'height="{bh:g}" rx="8" fill="{color}" fill-opacity="0.12" '
                f'stroke="{color}" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{cx + 16:g}" y="{by + 32:g}" font-size="18" '
                f'font-weight="bold" fill="{color}">{escape(title)}</text>'
            )
            for j, item in enumerate(items):
                ty = by + 64 + j * 22
                # Bug 05 fix: stop rendering items that would fall
                # below the card's bottom edge. Without this guard,
                # LLM output with many items overflowed the bounds and
                # the quality checker flagged it as a blocking overflow.
                if ty > by + bh - 8:
                    break
                # Phase 17-C: also horizontally clip the item when even
                # at the smallest font it would overflow.
                cpl = max(1, _cpl_card(card_inner_w, item_font))
                display = item[:cpl] + ("…" if len(item) > cpl else "")
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" '
                    f'font-size="{item_font:g}" fill="#222">'
                    f'{escape(display)}</text>'
                )
        return "\n".join(parts)
    if layout == "flow-steps":
        steps = payload.get("steps") or []
        if not 2 <= len(steps) <= 5:
            raise ValueError("flow-steps supports 2-5 steps per row")
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []
        n = len(steps)
        gap = 16.0
        # Phase 9 (2026-09-16): divide bw by (n+1) gaps instead of
        # (n-1) — first/last card now leave a half-gap gutter on
        # each side, eliminating the previous "last card sticks to
        # right edge" overflow by ~12px on bw=1060, n=5.
        step_w = (bw - gap * (n + 1)) / n
        for i, step in enumerate(steps):
            cx = bx + gap + i * (step_w + gap)
            color = step.get("color", "#1D2CAB")
            title = step.get("title", f"Step {i + 1}")
            items = coerce_str_list(step.get("items", []))
            parts.append(
                f'<rect x="{cx:g}" y="{by:g}" width="{step_w:g}" '
                f'height="{bh:g}" rx="6" fill="{color}" '
                f'fill-opacity="0.08" stroke="{color}" stroke-width="1"/>'
            )
            # Numbered circle (no native tspan counting).
            parts.append(
                f'<circle cx="{cx + 24:g}" cy="{by + 28:g}" r="14" '
                f'fill="{color}"/>'
            )
            parts.append(
                f'<text x="{cx + 24:g}" y="{by + 33:g}" font-size="14" '
                f'font-weight="bold" fill="#FFFFFF" text-anchor="middle">'
                f'{i + 1}</text>'
            )
            parts.append(
                f'<text x="{cx + 16:g}" y="{by + 64:g}" font-size="16" '
                f'font-weight="bold" fill="{color}">{escape(title)}</text>'
            )
            for j, item in enumerate(items):
                ty = by + 92 + j * 22
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" font-size="13" '
                    f'fill="#222">{escape(item)}</text>'
                )
            # Connector arrow to next step — static filled triangle,
            # not a <line marker-end="url(#arrow)">. The vendor's
            # svg_to_pptx converter validates every marker reference
            # against a direct <defs><marker> and rejects when missing,
            # so we use a path-based arrow shape that doesn't need any
            # defs entry.
            if i < n - 1:
                ax = cx + step_w + gap / 2
                ay = by + 28
                parts.append(
                    f'<path d="M {ax - 5:g} {ay - 4:g} L {ax + 5:g} '
                    f'{ay:g} L {ax - 5:g} {ay + 4:g} Z" '
                    f'fill="{color}"/>'
                )
        return "\n".join(parts)
    if layout == "revision-table":
        # Phase 11 (2026-09-17): ppt-master table_summary geometry.
        # White panel (rx=12) + 6px gold top + 64px #1D2CAB header
        # bar + 5 alternating body rows 76px each + #C8D2E0 dividers.
        rows = payload.get("rows") or []
        # Phase 11 (2026-09-17): allow empty rows — we'll render the
        # 5 alternating placeholder rows anyway. Empty rows is the
        # boteng 附件 scenario where the markdown has a table header
        # but no data rows yet.
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        scale = bw / 1280.0

        keys = ("date", "status", "content", "author")
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in keys:
                        keys = keys + (k,)
                break
        n_cols = len(keys)
        col_w = bw / n_cols

        HEADER_H = 64.0 * scale
        ROW_H = 76.0 * scale
        GOLD_TOP = 6.0 * scale

        parts: list[str] = []
        # 1. White panel (rx=12).
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" height="{bh:g}" '
            f'rx="{12:g}" fill="#FFFFFF"/>'
        )
        # 2. 6px gold top accent.
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" '
            f'height="{GOLD_TOP:g}" fill="#D4A24C"/>'
        )
        # 3. 64px brand-blue header bar.
        parts.append(
            f'<rect x="{bx:g}" y="{by + GOLD_TOP:g}" width="{bw:g}" '
            f'height="{HEADER_H:g}" fill="#1D2CAB"/>'
        )
        # 4. Header text. ``payload['headers']`` may arrive as a list
        # (LLM returns it positionally) — coerce to a dict keyed by
        # ``keys`` so ``header_map.get(key, key)`` works either way.
        raw_headers = payload.get("headers")
        if isinstance(raw_headers, list):
            header_map = dict(zip(keys, raw_headers))
        elif isinstance(raw_headers, dict):
            header_map = raw_headers
        else:
            header_map = {
                "date": "日 期",
                "status": "修订状态",
                "content": "修 改 内 容",
                "author": "修 改 人",
                "reviewer": "审 核 人",
                "approver": "批 准 人",
            }
        for j, key in enumerate(keys):
            cx_text = bx + j * col_w + col_w / 2.0
            parts.append(
                f'<text x="{cx_text:g}" y="{by + GOLD_TOP + HEADER_H * 0.6:g}" '
                f'font-size="{_fs(spec, "header")}" font-weight="bold" '
                f'fill="#FFFFFF" text-anchor="middle">'
                f'{escape(str(header_map.get(key, key)))}</text>'
            )
        # 5. Header column dividers.
        for j in range(1, n_cols):
            div_x = bx + j * col_w
            parts.append(
                f'<line x1="{div_x:g}" y1="{by + GOLD_TOP + 4*scale:g}" '
                f'x2="{div_x:g}" y2="{by + GOLD_TOP + HEADER_H - 4*scale:g}" '
                f'stroke="#2A4DCB" stroke-width="1"/>'
            )
        # 6. Body rows (alternating fill).
        n_rows = max(len(rows), 5)
        body_top = by + GOLD_TOP + HEADER_H
        for i in range(n_rows):
            ry = body_top + i * ROW_H
            if ry + ROW_H > by + bh:
                break
            row_fill = "#FFFFFF" if i % 2 == 0 else "#F4F7FB"
            parts.append(
                f'<rect x="{bx:g}" y="{ry:g}" width="{bw:g}" '
                f'height="{ROW_H:g}" fill="{row_fill}"/>'
            )
            parts.append(
                f'<line x1="{bx:g}" y1="{ry + ROW_H:g}" '
                f'x2="{bx + bw:g}" y2="{ry + ROW_H:g}" stroke="#C8D2E0" '
                f'stroke-width="1"/>'
            )
        # 7. Full column dividers.
        for j in range(1, n_cols):
            div_x = bx + j * col_w
            parts.append(
                f'<line x1="{div_x:g}" y1="{body_top:g}" '
                f'x2="{div_x:g}" y2="{body_top + n_rows * ROW_H:g}" '
                f'stroke="#C8D2E0" stroke-width="1"/>'
            )
        # 8. Cell text (rows with actual data only).
        for i, row in enumerate(rows):
            ry = body_top + i * ROW_H
            if ry + ROW_H > by + bh:
                break
            for j, key in enumerate(keys):
                if not isinstance(row, dict):
                    continue
                raw_val = row.get(key, "")
                if isinstance(raw_val, (list, tuple)):
                    cell_items = [str(v) for v in raw_val]
                elif isinstance(raw_val, dict):
                    cell_items = [f"{k}={v}" for k, val in raw_val.items()]
                else:
                    cell_items = [str(raw_val)]
                tspans = []
                for k, item in enumerate(cell_items):
                    if k == 0:
                        tspans.append(escape(item))
                    else:
                        tspans.append(
                            f'<tspan x="{bx + j * col_w + 12:g}" dy="14">'
                            f'{escape(item)}</tspan>'
                        )
                if not cell_items or not str(cell_items[0]):
                    continue
                parts.append(
                    f'<text x="{bx + j * col_w + 12:g}" '
                    f'y="{ry + 22*scale:g}" '
                    f'font-size="{_fs(spec, "cell_body")}" fill="#0E1B2C">'
                    f'{"".join(tspans)}</text>'
                )
        # 9. Placeholder hint.
        if len(rows) <= 1:
            parts.append(
                f'<text x="{bx + bw/2:g}" '
                f'y="{by + bh - 24*scale:g}" '
                f'font-size="{_fs(spec, "placeholder_hint")}" fill="#C8D2E0" '
                f'text-anchor="middle" font-style="italic">'
                f'首次发布 · 后续修订请按表中栏目填写</text>'
            )
        return "\n".join(parts)
    # Bug 3 fix (Phase B): add 4 new layouts so the LLM has variety
    # beyond cards/flow/table and content pages stop looking templated.
    if layout == "hero-number":
        # 1 large centered value + small caption. For KPI / chapter-count
        # statements ("5 章" / "总章数").
        value = payload.get("value", "")
        unit = payload.get("unit", "")
        caption = payload.get("caption", "")
        if not isinstance(value, str) or not value:
            raise ValueError("hero-number requires spec.value (string)")
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []
        cx = bx + bw / 2
        # Bug 12 fix: shrink font-size to fit short bounds. A 72pt glyph
        # is ~90px tall, so for bh < ~140px the rendered glyph overflows
        # the top of the bounds. Scale font down to bh * 0.5 (gives the
        # glyph ~half the bounds height, leaving room for caption below).
        fs = min(72.0, max(12.0, bh * 0.5))
        # Centered baseline of the big number.
        value_y = by + bh * 0.55
        parts.append(
            f'<text x="{cx:g}" y="{value_y:g}" text-anchor="middle" '
            f'font-size="{fs:g}" font-weight="bold" fill="#1D2CAB">'
            f'{escape(value)}{escape(unit)}</text>'
        )
        # Caption below.
        if caption:
            parts.append(
                f'<text x="{cx:g}" y="{value_y + 36:g}" text-anchor="middle" '
                f'font-size="18" fill="#666">'
                f'{escape(caption)}</text>'
            )
        return "\n".join(parts)
    if layout == "callout-box":
        # Quotation / motto in a soft-tinted box with a big quote glyph
        # and an attribution line.
        quote = payload.get("quote", "")
        attribution = payload.get("attribution", "")
        if not isinstance(quote, str) or not quote:
            raise ValueError("callout-box requires spec.quote (string)")
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []
        # Tinted panel background.
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" height="{bh:g}" '
            f'rx="10" fill="#F4F6FB" stroke="#1D2CAB" '
            f'stroke-width="1" stroke-opacity="0.3"/>'
        )
        # Big opening quote glyph at top-left.
        # Bug 13 fix: detect the first character's Unicode bidirectional
        # class. RTL languages (Arabic, Hebrew) expect a mirrored right
        # curly quote ” (U+201D) instead of the left “ (U+201C).
        # Fall back to “ for LTR / neutral text.
        import unicodedata
        quote_first = quote[0] if quote else ""
        bidi = unicodedata.bidirectional(quote_first) if quote_first else "L"
        if bidi in ("R", "AL", "RLE", "RLO"):
            open_quote, close_quote = "”", "“"
        else:
            open_quote, close_quote = "“", "”"
        parts.append(
            f'<text x="{bx + 24:g}" y="{by + 64:g}" font-size="48" '
            f'font-weight="bold" fill="#1D2CAB" '
            f'fill-opacity="0.6">{open_quote}</text>'
        )
        # Quote text — vertically centered.
        parts.append(
            f'<text x="{bx + 24:g}" y="{by + bh / 2:g}" font-size="24" '
            f'fill="#222">{escape(quote)}</text>'
        )
        # Attribution bottom-right.
        if attribution:
            parts.append(
                f'<text x="{bx + bw - 24:g}" y="{by + bh - 24:g}" '
                f'text-anchor="end" font-size="14" fill="#666" '
                f'font-style="italic">— {escape(attribution)}</text>'
            )
        return "\n".join(parts)
    if layout == "two-column-compare":
        # Two juxtaposed columns (e.g. 对比 / pros-cons) divided by a
        # vertical rule.
        left = payload.get("left") or {}
        right = payload.get("right") or {}
        if not (isinstance(left, dict) and isinstance(right, dict)):
            raise ValueError(
                "two-column-compare requires spec.left and spec.right "
                "(both objects with title + items)"
            )
        for side_name, side in (("left", left), ("right", right)):
            if not isinstance(side.get("title"), str) or not side["title"]:
                raise ValueError(
                    f"two-column-compare spec.{side_name}.title required"
                )
            if not isinstance(side.get("items"), list):
                raise ValueError(
                    f"two-column-compare spec.{side_name}.items must be list"
                )
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []
        gap = 24.0
        col_w = (bw - gap) / 2
        for idx, side in enumerate((left, right)):
            cx = bx + idx * (col_w + gap)
            # Column title.
            parts.append(
                f'<text x="{cx + col_w / 2:g}" y="{by + 32:g}" '
                f'text-anchor="middle" font-size="20" font-weight="bold" '
                f'fill="#1D2CAB">{escape(side["title"])}</text>'
            )
            # Items list.
            for j, item in enumerate(side["items"]):
                ty = by + 64 + j * 22
                if ty > by + bh - 8:
                    break
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" font-size="14" '
                    f'fill="#222">{escape(str(item))}</text>'
                )
        # Vertical divider rule between the two columns.
        mid_x = bx + bw / 2
        parts.append(
            f'<line x1="{mid_x:g}" y1="{by + 16:g}" x2="{mid_x:g}" '
            f'y2="{by + bh - 16:g}" stroke="#D0D6E5" stroke-width="1"/>'
        )
        return "\n".join(parts)
    if layout == "timeline":
        # 2-5 ordered steps along a horizontal axis with circle nodes.
        steps = payload.get("steps") or []
        if not 2 <= len(steps) <= 5:
            raise ValueError("timeline supports 2-5 steps")
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []
        n = len(steps)
        # Horizontal axis baseline near vertical middle.
        axis_y = by + bh * 0.45
        # Adaptive left/right margin: must be large enough that the
        # leftmost / rightmost text (text-anchor="middle") does not
        # bleed outside the declared bounds. Estimate the widest detail
        # string with text_width.estimate_text_width so CJK chars are
        # correctly counted at 1.0em (vs Latin 0.55em). Bug 03 fix:
        # the previous ``len() * 6.3`` heuristic assumed Latin widths
        # and underestimated CJK-heavy detail by up to 50%.
        from .text_width import estimate_text_width
        widest_px = max(
            (estimate_text_width(str(s.get("detail", "")), font_size=12.0)
             for s in steps),
            default=0.0,
        )
        margin = max(40.0, widest_px / 2 + 10.0)
        # Clamp margin so span stays positive.
        margin = min(margin, bw / 2 - 10.0)
        span = bw - 2 * margin
        for i, step in enumerate(steps):
            cx = bx + margin + (span * i / max(n - 1, 1))
            color = step.get("color", "#1D2CAB")
            label = step.get("label", f"Step {i + 1}")
            detail = step.get("detail", "")
            parts.append(
                f'<circle cx="{cx:g}" cy="{axis_y:g}" r="10" '
                f'fill="{color}"/>'
            )
            parts.append(
                f'<text x="{cx:g}" y="{axis_y + 4:g}" text-anchor="middle" '
                f'font-size="11" font-weight="bold" fill="#FFFFFF">'
                f'{i + 1}</text>'
            )
            # Connector line to next node (path so we don't need a
            # marker definition, same trick as flow-steps).
            if i < n - 1:
                next_cx = bx + margin + (span * (i + 1) / max(n - 1, 1))
                parts.append(
                    f'<line x1="{cx + 12:g}" y1="{axis_y:g}" '
                    f'x2="{next_cx - 12:g}" y2="{axis_y:g}" '
                    f'stroke="{color}" stroke-width="2" '
                    f'stroke-opacity="0.5"/>'
                )
            # Label above the node.
            parts.append(
                f'<text x="{cx:g}" y="{axis_y - 24:g}" text-anchor="middle" '
                f'font-size="14" font-weight="bold" fill="{color}">'
                f'{escape(str(label))}</text>'
            )
            # Detail below the node.
            if detail:
                parts.append(
                    f'<text x="{cx:g}" y="{axis_y + 36:g}" '
                    f'text-anchor="middle" font-size="12" fill="#222">'
                    f'{escape(str(detail))}</text>'
                )
        return "\n".join(parts)
    if layout == "simple-text":
        # Phase 3 (2026-09-16): single padded card with auto-wrapping
        # text. Used when the markdown author wants one block of prose
        # (e.g. a policy statement) without numbered list or hero KPI.
        # Replacement for the Phase 1.4 heuristic text-split in
        # workspace_expand.py:189-229 — author explicitly opts in via
        # ``> **layout**: simple-text`` instead of letting the heuristic
        # decide.
        text = payload.get("text", "")
        if not isinstance(text, str) or not text:
            raise ValueError("simple-text requires spec.text (non-empty string)")
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        padding = 24.0
        inner_w = bw - 2 * padding
        # CJK-aware line fitting: chars_that_fit does a binary search
        # against both a CJK sample and a Latin sample and returns the
        # more restrictive cap. This avoids the fixed 1.0em/char
        # heuristic in plan §3.1.2 which over-estimates for Latin text.
        from .text_width import chars_that_fit
        font_size = 24.0
        chars_per_line = chars_that_fit(inner_w, font_size)
        if chars_per_line <= 0:
            raise ValueError(
                f"simple-text bounds too narrow for font_size={font_size} "
                f"(inner_w={inner_w:g})"
            )
        # Auto-wrap by chars_per_line (CJK-dominant cap is the safer
        # estimate). For Latin text this leaves whitespace ragged on
        # the right edge; matching plan §3.1.2 spec — caller's
        # alternative is bullet-list for itemized content.
        lines = [text[i:i + chars_per_line]
                 for i in range(0, len(text), chars_per_line)]
        # Bug 12-style font shrink: if the line count exceeds what fits
        # in bh (estimate bh/(font_size*1.4) usable lines), shrink
        # font_size proportionally down to MIN_READABLE_FONT=14.
        usable_lines = max(1.0, (bh - 2 * padding) / (font_size * 1.4))
        while len(lines) > usable_lines and font_size > 14.0:
            font_size = max(14.0, font_size - 2.0)
            chars_per_line = chars_that_fit(inner_w, font_size)
            if chars_per_line <= 0:
                break
            lines = [text[i:i + chars_per_line]
                     for i in range(0, len(text), chars_per_line)]
            usable_lines = max(1.0, (bh - 2 * padding) / (font_size * 1.4))
        parts: list[str] = [
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" height="{bh:g}" '
            f'rx="8" fill="#1D2CAB" fill-opacity="0.08"/>'
        ]
        for i, line in enumerate(lines):
            ty = by + padding + font_size * (i + 1)
            if ty > by + bh - 8:
                break
            parts.append(
                f'<text x="{bx + padding:g}" y="{ty:g}" '
                f'font-size="{font_size:g}" fill="#222">'
                f'{escape(line)}</text>'
            )
        return "\n".join(parts)
    if layout == "bullet-list":
        # Phase 3 (2026-09-16): left color bar + vertical numbered list.
        # Use when the markdown author has 2-10 enumerated items that
        # need vertical stacking (vs the horizontal flow-steps which
        # maxes out at 5).
        items = coerce_str_list(payload.get("items"))
        if not items:
            raise ValueError("bullet-list requires spec.items (non-empty list)")
        # Accept a single string with separator punctuation by splitting
        # on the same delimiters Phase 2 uses for meta "items" lists
        # (、，,;；, /). Without this, markdown authors would have to
        # write JSON-style arrays which doesn't match prose conventions.
        if len(items) == 1 and isinstance(items[0], str):
            raw = items[0]
            if any(sep in raw for sep in ("、", "，", ",", ";", "；", "/", "\n")):
                items = [
                    s.strip() for s in re.split(r"[、,,;;/\\n]+", raw)
                    if s.strip()
                ]
        if len(items) > 10:
            items = items[:10]  # Cap at 10 per plan §3.1.2
        color = payload.get("color", "#1D2CAB")
        if not isinstance(color, str) or not color:
            color = "#1D2CAB"
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        # Adaptive line height: 30px max, scale down for many items so
        # the whole list fits. 40px top/bottom reserved for the bar +
        # breathing room.
        line_h = min(30.0, (bh - 40.0) / max(1, len(items)))
        parts: list[str] = [
            # Left color bar (6px wide).
            f'<rect x="{bx:g}" y="{by:g}" width="6" height="{bh:g}" '
            f'fill="{escape(color)}"/>'
        ]
        # Phase 17-C (2026-09-18): auto-shrink font when text would
        # overflow the available width. The longest item sets the font
        # size for the whole list (uniform type hierarchy preserved).
        from .text_width import chars_that_fit as _cpl
        text_inner_w = bw - 24.0 - 16.0  # bar + right padding
        max_item_len = max(len(s) for s in items) if items else 0
        item_font = 16.0
        for try_font in (16.0, 14.0, 13.0, 12.0):
            if _cpl(text_inner_w, try_font) >= max_item_len:
                item_font = try_font
                break
        else:
            item_font = 12.0
        # When font shrinks, line_h tightens proportionally so the
        # whole list still fits in the bounds.
        line_h = min(30.0, item_font * 1.6, (bh - 40.0) / max(1, len(items)))
        for i, item in enumerate(items):
            ty = by + 30.0 + line_h * i
            # Overflow guard (same pattern as 3-column-cards line 122):
            # skip items that would fall past the bounds bottom.
            if ty > by + bh - 8:
                break
            # Phase 6.6 (2026-09-16): widened 40→80 chars. The renderer's
            # own bounds-check (line 537 ``ty > by + bh - 8``) still
            # drops items that fall past the bottom of the body rect,
            # so we don't risk overflow from the longer truncation.
            cpl = max(1, _cpl(text_inner_w, item_font))
            display = item[:cpl] + ("…" if len(item) > cpl else "")
            parts.append(
                f'<text x="{bx + 24:g}" y="{ty:g}" '
                f'font-size="{item_font:g}" fill="#222">'
                f'{i + 1}. {escape(display)}</text>'
            )
        return "\n".join(parts)
    # Phase 7 (2026-09-16): ppt-master archetype-inspired layouts.
    # ppt-master's presentation_core uses ~20 named archetypes
    # (title_content / content_caption / three_card / process_timeline
    # / matrix_2x2 / kpi_dashboard …) with explicit pixel geometry.
    # We adopt 3 of them to polish boteng's content pages without
    # disturbing the 9 existing layouts (palette stays untouched —
    # these new layouts use the calm ppt-master presentation_core
    # neutral stack: bg #FFFFFF, panel #F4F6F8, hairline #D6DCE3,
    # ink #1E293B, body #64748B, muted #94A3B8).
    if layout == "statement-caption":
        # Phase 11 (2026-09-17): ppt-master's content_caption geometry
        # (see projects/boteng_ppt_20260916/svg_final/02_preface.svg).
        # 240px gradient blue rail (top-left) + 876px white panel
        # (top-right) with vertical hairline at x=320. Rail holds a
        # 56px gold section index, a 32px white title, an en-subtitle,
        # a short caption, an optional doc code, and 3 lines of body
        # summary. Panel holds a 22px blue title, a 14px en-subtitle,
        # a 96px gold accent, a 72px huge opening quote, multi-line
        # body, and a takeaway band (90px high) with a 6px gold left
        # border. Geometry is anchored to ``body_bounds`` (caller may
        # pass any 1280x720 frame) and scaled.
        title = payload.get("title", "")  # optional (Phase 12)
        eyebrow = payload.get("eyebrow", "")  # optional rail label
        eyebrow_en = payload.get("eyebrow_en", "")  # optional rail en label
        index_num = payload.get("index_num", "")  # optional big gold number
        caption = payload.get("caption", "")  # rail short caption
        doc_code = payload.get("doc_code", "")  # rail footer (e.g. BT-ZD-001)
        body = payload.get("body", "")
        takeaway = payload.get("takeaway", "")  # panel bottom band text
        # Phase 12 (2026-09-17): title is now optional — the boteng
        # template's shape-17 owns the Chinese chapter name. When the
        # caller passes an empty title the rail / panel skip their title
        # <text> elements. body is still required (it's the actual
        # content the panel needs to render).
        if not isinstance(body, str) or not body:
            raise ValueError("statement-caption requires spec.body")

        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default

        # ppt-master geometry: 240px gradient rail + 16px gutter +
        # rest white panel. We scale so that (rail + gutter + panel)
        # = bw, with rail = 240 * (bw/1280) and panel absorbs the rest.
        # boteng's content body_bounds is "120 130 1060 480", so
        # bw=1060, but the visual product lands in the same relative
        # proportions.
        scale = bw / 1280.0
        RAIL_W = 240.0 * scale
        GUTTER = 16.0 * scale
        DIV_X = bx + RAIL_W + GUTTER
        PANEL_X = DIV_X + GUTTER

        parts: list[str] = []
        # 0. Gradient definition (id-scoped to this slide). Must come
        # before any <rect fill="url(#...)"> that references it. Native
        # svg_to_pptx converts <linearGradient> to <a:gradFill> on the
        # rect it tints; id collisions are tolerated (last write wins).
        rail_grad_id = "prefaceRail"
        parts.append(
            f'<defs><linearGradient id="{rail_grad_id}" x1="0" '
            f'y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="#1D2CAB"/>'
            f'<stop offset="100%" stop-color="#2A4DCB"/>'
            f'</linearGradient></defs>'
        )

        # 1. Gradient rail (top of card).
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{RAIL_W:g}" '
            f'height="{bh:g}" rx="{12:g}" fill="url(#{rail_grad_id})"/>'
        )
        # 2. White panel.
        parts.append(
            f'<rect x="{PANEL_X:g}" y="{by:g}" width="{bw - RAIL_W - 2*GUTTER:g}" '
            f'height="{bh:g}" rx="{12:g}" fill="#FFFFFF"/>'
        )

        # === RAIL content ===
        rail_pad = 28.0 * scale
        # 3. Big gold section index (e.g. "01").
        if index_num:
            parts.append(
                f'<text x="{bx + rail_pad:g}" y="{by + 80*scale:g}" '
                f'font-size="{_fs(spec, "cover_title")}" font-weight="bold" '
                f'fill="#D4A24C">{escape(str(index_num))}</text>'
            )
            parts.append(
                f'<line x1="{bx + rail_pad:g}" y1="{by + 100*scale:g}" '
                f'x2="{bx + rail_pad + 88*scale:g}" '
                f'y2="{by + 100*scale:g}" stroke="#FFFFFF" '
                f'stroke-width="2" stroke-opacity="0.6"/>'
            )
            # Title below index.
            title_y = by + 148 * scale
        else:
            title_y = by + 56 * scale
        # 4. Rail title (32px white bold). Phase 12: skipped when
        # title is empty.
        if title:
            parts.append(
                f'<text x="{bx + rail_pad:g}" y="{title_y:g}" '
                f'font-size="{_fs(spec, "rail_title")}" font-weight="bold" '
                f'fill="#FFFFFF">{escape(title)}</text>'
            )
        # 5. Eyebrow / en-label below title.
        if eyebrow_en:
            parts.append(
                f'<text x="{bx + rail_pad:g}" y="{title_y + 28*scale:g}" '
                f'font-size="{_fs(spec, "en_subtitle")}" fill="#D2DAF9" '
                f'letter-spacing="2">{escape(str(eyebrow_en))}</text>'
            )
        # 6. Caption (rail mid-section).
        if caption:
            cap_y = by + bh * 0.55
            parts.append(
                f'<text x="{bx + rail_pad:g}" y="{cap_y:g}" '
                f'font-size="{_fs(spec, "rail_caption")}" fill="#FFFFFF">'
                f'{escape(str(caption))}</text>'
            )
            parts.append(
                f'<line x1="{bx + rail_pad:g}" y1="{cap_y + 14*scale:g}" '
                f'x2="{bx + rail_pad + (RAIL_W - 2*rail_pad - 20):g}" '
                f'y2="{cap_y + 14*scale:g}" stroke="#D4A24C" '
                f'stroke-width="1.5"/>'
            )
            # Body 3-line summary.
            from .text_width import chars_that_fit
            inner_w = RAIL_W - 2 * rail_pad - 16
            cpl = chars_that_fit(inner_w, 12.0)
            if cpl > 0:
                body_lines = [
                    body[i:i + cpl] for i in range(0, len(body), cpl)
                ][:3]
                for j, ln in enumerate(body_lines):
                    byline = cap_y + 38 * scale + j * 18 * scale
                    if byline > by + bh - 50 * scale:
                        break
                    parts.append(
                        f'<text x="{bx + rail_pad:g}" y="{byline:g}" '
                        f'font-size="{_fs(spec, "rail_body")}" fill="#D2DAF9" '
                        f'letter-spacing="1">{escape(ln)}</text>'
                    )
        # 7. Doc code at rail bottom.
        if doc_code:
            parts.append(
                f'<text x="{bx + rail_pad:g}" y="{by + bh - 28*scale:g}" '
                f'font-size="{_fs(spec, "doc_code")}" fill="#D2DAF9" '
                f'letter-spacing="2">{escape(str(doc_code))}</text>'
            )

        # === PANEL content ===
        panel_pad = 36.0 * scale
        # 8. Panel title (22px blue bold). Phase 12: skipped when
        # title is empty.
        if title:
            parts.append(
                f'<text x="{PANEL_X + panel_pad:g}" '
                f'y="{by + 56*scale:g}" font-size="{_fs(spec, "panel_title")}" '
                f'font-weight="bold" fill="#1D2CAB">'
                f'{escape(title)}</text>'
            )
        # 9. Panel en-subtitle (14px muted).
        if eyebrow_en:
            parts.append(
                f'<text x="{PANEL_X + panel_pad:g}" '
                f'y="{by + 82*scale:g}" font-size="{_fs(spec, "panel_en_subtitle")}" '
                f'fill="#5A6678">{escape(str(eyebrow_en))}</text>'
            )
        # 10. Gold accent line (124px).
        parts.append(
            f'<line x1="{PANEL_X + panel_pad:g}" y1="{by + 98*scale:g}" '
            f'x2="{PANEL_X + panel_pad + 124*scale:g}" '
            f'y2="{by + 98*scale:g}" stroke="#D4A24C" stroke-width="2"/>'
        )
        # 11. Big opening quote glyph (72px gold, 18% opacity).
        parts.append(
            f'<text x="{PANEL_X + panel_pad:g}" '
            f'y="{by + 170*scale:g}" font-size="{_fs(spec, "big_quote")}" '
            f'font-weight="bold" fill="#D4A24C" '
            f'fill-opacity="0.18">"</text>'
        )
        # 12. Multi-line body (20px dark ink).
        inner_w = bw - (PANEL_X - bx) - panel_pad - 16
        from .text_width import chars_that_fit

        body_font = 20.0
        cpl = chars_that_fit(inner_w, body_font)
        if cpl <= 0:
            cpl = 20
        body_lines = [body[i:i + cpl] for i in range(0, len(body), cpl)]
        # Fit as many lines as the geometry allows above the takeaway band.
        block_after_h = 110.0 * scale if takeaway else 16.0
        max_lines = _compute_max_body_lines(
            bh=bh, by=by, body_y0=by + 210 * scale,
            line_h=32 * scale,
            block_after_h=block_after_h,
        )
        body_y0 = by + 210 * scale
        body_lines, body_font_attr = _body_lines_cascade(
            body_lines,
            max_lines=max_lines,
            font_attr=str(body_font),
            body_font=body_font,
        )
        for j, ln in enumerate(body_lines):
            ly = body_y0 + j * 32 * scale
            if takeaway and ly > by + bh - 110 * scale:
                break
            if (not takeaway) and ly > by + bh - 16:
                break
            parts.append(
                f'<text x="{PANEL_X + panel_pad:g}" y="{ly:g}" '
                f'font-size="{body_font_attr}" fill="#0E1B2C">'
                f'{escape(ln)}</text>'
            )
        # 13. Takeaway band (90px high, 6px gold left border).
        if takeaway:
            band_h = 90.0 * scale
            band_y = by + bh - band_h - 14 * scale
            parts.append(
                f'<rect x="{PANEL_X + panel_pad:g}" y="{band_y:g}" '
                f'width="{bw - (PANEL_X - bx) - panel_pad - 16:g}" '
                f'height="{band_h:g}" rx="{8:g}" fill="#1D2CAB" '
                f'fill-opacity="0.08"/>'
            )
            parts.append(
                f'<rect x="{PANEL_X + panel_pad:g}" y="{band_y:g}" '
                f'width="6" height="{band_h:g}" fill="#D4A24C"/>'
            )
            parts.append(
                f'<text x="{PANEL_X + panel_pad + 28*scale:g}" '
                f'y="{band_y + 32*scale:g}" font-size="{_fs(spec, "takeaway_label")}" '
                f'font-weight="bold" fill="#1D2CAB" '
                f'letter-spacing="3">CORE TAKEAWAY</text>'
            )
            # Wrap takeaway into 1-2 lines.
            take_cpl = max(8, chars_that_fit(
                bw - (PANEL_X - bx) - panel_pad - 60, 20.0) or 24)
            take_lines = [
                str(takeaway)[i:i + take_cpl]
                for i in range(0, len(str(takeaway)), take_cpl)
            ][:2]
            for j, ln in enumerate(take_lines):
                parts.append(
                    f'<text x="{PANEL_X + panel_pad + 28*scale:g}" '
                    f'y="{band_y + (60 + j*22)*scale:g}" '
                    f'font-size="{_fs(spec, "lead")}" font-weight="bold" '
                    f'fill="#0A1A3F">{escape(ln)}</text>'
                )
        return "\n".join(parts)
    if layout == "procedural-steps":
        # Phase 11 (2026-09-17): ppt-master's process_timeline geometry
        # (see projects/boteng_ppt_20260916/svg_final/06_workflow.svg).
        # Geometry stack:
        #   - Title row at y=120 (32px bold #0A1A3F + 14px right-aligned
        #       muted subtitle)
        #   - 96px gold accent line at y=138
        #   - Top timeline (y=186): horizontal dashed line + 4 circles
        #       r=14 (white fill, brand-blue stroke, 3px wide) with
        #       12px numbers + 13px labels at y=226
        #   - 2x2 cards grid (each 564×180, rx=12, white fill) starting
        #       at y=270 with 8px gap. Each card has a 40px gradient
        #       header band (brand-blue → light blue), containing a
        #       "PHASE N · 阶段X" white label and a gold subtitle.
        #   - Card body has a 13px brand-blue section label + 3 bullets
        #       of 13px dark ink (each prefixed with "· ").
        # We support 2-5 steps; boteng's 四、工作程序 maps to 4 phases.
        # Defensive: accept legacy "phases" key for backward compat with
        # older LLM prompt variants that named the field differently.
        steps = payload.get("steps") or payload.get("phases") or []
        if not 2 <= len(steps) <= 5:
            raise ValueError(
                f"procedural-steps requires 2-5 steps (got {len(steps)})"
            )
        title = payload.get("title", "")  # optional page title
        subtitle = payload.get("subtitle", "")  # optional subtitle
        # Each step shape: {label, detail, bullets:[str,str,str]}.
        # Backward compat: accept legacy {label, detail} and synthesize
        # bullets from the detail string.

        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        scale = bw / 1280.0

        # Normalize step dicts.
        norm_steps: list[dict] = []
        for s in steps:
            if not isinstance(s, dict):
                s = {"label": str(s), "detail": "", "bullets": []}
            bullets = s.get("bullets") or []
            if not bullets and s.get("detail"):
                # Split detail on sentence boundaries (· / / / ;) so
                # boteng's prose becomes 1-3 bullets.
                raw = str(s["detail"])
                for sep in ("。", "；", ";", "/"):
                    if sep in raw:
                        bullets = [
                            b.strip() + ("。" if sep == "。" else "")
                            for b in raw.split(sep)
                            if b.strip()
                        ][:3]
                        break
                if not bullets:
                    bullets = [raw]
            s2 = {
                "label": str(s.get("label", ""))[:16],
                "detail": str(s.get("detail", ""))[:40],
                "bullets": [str(b)[:80] for b in bullets[:3]],
            }
            norm_steps.append(s2)

        parts: list[str] = []
        # 0. Gradient definition (id-scoped).
        parts.append(
            f'<defs><linearGradient id="phaseHeader" x1="0" y1="0" '
            f'x2="1" y2="0">'
            f'<stop offset="0%" stop-color="#1D2CAB"/>'
            f'<stop offset="100%" stop-color="#2A4DCB"/>'
            f'</linearGradient></defs>'
        )

        # 1. Title row (32px bold ink) + right subtitle.
        title_y = by + 50 * scale
        if title:
            parts.append(
                f'<text x="{bx:g}" y="{title_y:g}" '
                f'font-size="{_fs(spec, "page_title")}" font-weight="bold" '
                f'fill="#0A1A3F">{escape(title)}</text>'
            )
        if subtitle:
            parts.append(
                f'<text x="{bx + bw/2:g}" y="{title_y:g}" '
                f'font-size="{_fs(spec, "rail_caption")}" fill="#5A6678">'
                f'{escape(subtitle)}</text>'
            )
        # 2. 96px gold accent line at y=138 (relative).
        parts.append(
            f'<line x1="{bx:g}" y1="{by + 68*scale:g}" '
            f'x2="{bx + 96*scale:g}" y2="{by + 68*scale:g}" '
            f'stroke="#D4A24C" stroke-width="2"/>'
        )

        # 3. Top timeline (y= by+96): dashed line + circles + labels.
        axis_y = by + 116 * scale
        # Dashed connector line.
        parts.append(
            f'<line x1="{bx:g}" y1="{axis_y:g}" '
            f'x2="{bx + bw:g}" y2="{axis_y:g}" stroke="#C8D2E0" '
            f'stroke-width="2" stroke-dasharray="4,4"/>'
        )
        n = len(norm_steps)
        margin = 60 * scale
        span = bw - 2 * margin
        # Phase label y (below circles).
        label_y = axis_y + 26 * scale
        for i, step in enumerate(norm_steps):
            cx = bx + margin + span * i / max(n - 1, 1)
            # Highlight last circle (gold fill, dark border).
            if i == n - 1:
                circle_fill = "#D4A24C"
                stroke_color = "#0A1A3F"
                num_fill = "#0A1A3F"
            else:
                circle_fill = "#FFFFFF"
                stroke_color = "#1D2CAB"
                num_fill = "#1D2CAB"
            parts.append(
                f'<circle cx="{cx:g}" cy="{axis_y:g}" r="{14*scale:g}" '
                f'fill="{circle_fill}" stroke="{stroke_color}" '
                f'stroke-width="3"/>'
            )
            parts.append(
                f'<text x="{cx:g}" y="{axis_y + 4*scale:g}" '
                f'text-anchor="middle" font-size="{_fs(spec, "phase_num")}" '
                f'font-weight="bold" fill="{num_fill}">'
                f'{i + 1}</text>'
            )
            parts.append(
                f'<text x="{cx:g}" y="{label_y:g}" '
                f'text-anchor="middle" font-size="{_fs(spec, "phase_label")}" '
                f'font-weight="bold" fill="#0A1A3F">'
                f'{escape(step["label"])}</text>'
            )

        # 4. 2x2 cards grid. Card geometry (relative to bw=1280):
        #   card_w = 564 * scale;  card_h = 180 * scale
        #   gap = 8 * scale (between cards)
        #   grid starts at y = by + 200*scale, with 2 columns.
        card_w = (bw - 16 * scale) / 2
        card_h = 180 * scale
        gap_x = 16 * scale
        gap_y = 14 * scale
        grid_y0 = by + 200 * scale
        for i, step in enumerate(norm_steps):
            row = i // 2
            col = i % 2
            cx_card = bx + col * (card_w + gap_x)
            cy_card = grid_y0 + row * (card_h + gap_y)
            # 4a. White card with rx=12.
            parts.append(
                f'<rect x="{cx_card:g}" y="{cy_card:g}" '
                f'width="{card_w:g}" height="{card_h:g}" '
                f'rx="{12:g}" fill="#FFFFFF"/>'
            )
            # 4b. 40px gradient header band.
            header_h = 40 * scale
            # Last phase uses solid dark variant (per ppt-master).
            if i == n - 1:
                header_fill = "#0A1A3F"
            else:
                header_fill = "url(#phaseHeader)"
            parts.append(
                f'<rect x="{cx_card:g}" y="{cy_card:g}" '
                f'width="{card_w:g}" height="{header_h:g}" '
                f'rx="{12:g}" fill="{header_fill}"/>'
            )
            parts.append(
                f'<rect x="{cx_card:g}" y="{cy_card + header_h - 10*scale:g}" '
                f'width="{card_w:g}" height="{10*scale:g}" '
                f'fill="{header_fill}"/>'
            )
            # 4c. PHASE N · 阶段X label (14px white bold, letter-spaced).
            parts.append(
                f'<text x="{cx_card + 20*scale:g}" '
                f'y="{cy_card + 25*scale:g}" '
                f'font-size="{_fs(spec, "phase_card_label")}" font-weight="bold" '
                f'fill="#FFFFFF" letter-spacing="3">'
                f'PHASE {i + 1} · 阶段{_cn_num(i + 1)}</text>'
            )
            # 4d. Gold subtitle (right side of header, text-anchor=end).
            parts.append(
                f'<text x="{cx_card + card_w - 20*scale:g}" '
                f'y="{cy_card + 25*scale:g}" '
                f'font-size="{_fs(spec, "phase_card_label")}" font-weight="bold" '
                f'fill="#D4A24C" text-anchor="end" '
                f'letter-spacing="2">{escape(step["label"])}</text>'
            )
            # 4e. Section label (13px brand-blue bold, below header).
            if step["detail"]:
                parts.append(
                    f'<text x="{cx_card + 20*scale:g}" '
                    f'y="{cy_card + (header_h + 18*scale):g}" '
                    f'font-size="{_fs(spec, "phase_card_section")}" font-weight="bold" '
                    f'fill="#1D2CAB">{escape(step["detail"][:32])}</text>'
                )
            # 4f. Bullets (13px dark ink, prefix "· ").
            body_y0 = cy_card + header_h + 36 * scale
            for j, bullet in enumerate(step["bullets"][:3]):
                byline = body_y0 + j * 22 * scale
                if byline > cy_card + card_h - 8 * scale:
                    break
                parts.append(
                    f'<text x="{cx_card + 20*scale:g}" '
                    f'y="{byline:g}" font-size="{_fs(spec, "bullet")}" '
                    f'fill="#0E1B2C">· {escape(bullet)}</text>'
                )
        return "\n".join(parts)
    if layout == "three-thesis-cards":
        # ppt-master analog: three_card (3 equal cards, 392px stride +
        # 24px gutter in ppt-master's 1280px canvas; we scale to the
        # caller-supplied bw). NOT a replacement for 3-column-cards
        # (which supports 1-4 cards with brand-color tint); this is
        # the polished 3-card variant with the calm neutral palette.
        cards = payload.get("cards") or []
        if len(cards) != 3:
            raise ValueError(
                f"three-thesis-cards requires exactly 3 cards "
                f"(got {len(cards)})"
            )
        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default

        GUTTER = 24.0
        STRIDE = (bw - 2 * GUTTER) / 3.0
        parts: list[str] = []
        for i, card in enumerate(cards):
            cx = bx + i * (STRIDE + GUTTER)
            # Card background — soft tint (NOT a brand-color rect).
            parts.append(
                f'<rect x="{cx:g}" y="{by:g}" width="{STRIDE:g}" '
                f'height="{bh:g}" rx="12" fill="#F4F6F8"/>'
            )
            # Top hairline accent strip (4px ink).
            parts.append(
                f'<rect x="{cx:g}" y="{by:g}" width="{STRIDE:g}" '
                f'height="4" fill="#1E293B"/>'
            )
            # Index numeral "0N" (14px muted, letter-spaced).
            parts.append(
                f'<text x="{cx + 24:g}" y="{by + 56:g}" font-size="14" '
                f'font-weight="700" fill="#94A3B8" letter-spacing="2">'
                f'0{i + 1}</text>'
            )
            # Card title (22px ink).
            parts.append(
                f'<text x="{cx + 24:g}" y="{by + 96:g}" font-size="22" '
                f'font-weight="700" fill="#1E293B">'
                f'{escape(card.get("title", ""))}</text>'
            )
            # Card items (18px ink, cap 48 chars per item).
            items = coerce_str_list(card.get("items", []))
            for j, item in enumerate(items):
                ty = by + 132 + j * 26
                if ty > by + bh - 12:
                    break
                parts.append(
                    f'<text x="{cx + 24:g}" y="{ty:g}" font-size="18" '
                    f'fill="#1E293B">{escape(item[:48])}</text>'
                )
        return "\n".join(parts)
    if layout == "hero_statement":
        # Phase 11 (2026-09-17): ppt-master's hero_statement geometry
        # (see projects/boteng_ppt_20260916/svg_final/03_purpose.svg /
        # 04_scope.svg / 05_principle.svg). Geometry stack:
        #   - White panel (rx=12)
        #   - Top claim band (68px tall when headline is set, 32px when
        #       headline is empty — Phase 12) full-width #1D2CAB.
        #       When headline is empty (Phase 12, content slides already
        #       get the chapter name from the template's shape-17) the
        #       band only carries the 14px gold en-tag right.
        #   - 14px en-subtitle + 96px gold accent line below
        #   - 32-34px big question / headline (#0A1A3F) — Phase 12
        #       skipped when question is empty
        #   - 18-20px multi-line body (#0E1B2C)
        #   - Optional keyword cards (5 cards, 208×56, rx=8, 0.06 fill
        #       #1D2CAB + 4px left border #1D2CAB, 16px bold ink + 11px
        #       muted en descriptor)
        from .text_width import chars_that_fit
        headline = payload.get("headline", "")  # optional (Phase 12)
        eyebrow_en = payload.get("eyebrow_en", "")  # English subtitle
        en_tag = payload.get("en_tag", "")  # top-right gold tag (e.g. PURPOSE)
        question = payload.get("question", "")  # optional (Phase 12)
        body_lines = payload.get("body_lines") or []  # list of strings
        if not isinstance(body_lines, list):
            body_lines = [str(body_lines)]
        keywords = payload.get("keywords") or []  # list of {word, en}
        if not isinstance(keywords, list):
            keywords = []
        # Phase 12 (2026-09-17): headline & question are now optional.
        # shape-17 already paints the Chinese chapter name on the slide
        # chrome topbar, so the in-body chapter headline + leading
        # question no longer need to repeat it.

        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        scale = bw / 1280.0

        parts: list[str] = []
        # 1. White panel.
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" '
            f'height="{bh:g}" rx="{12:g}" fill="#FFFFFF"/>'
        )
        # 2. Top claim band. Phase 12: clamp to 32px when headline is
        # empty so we don't leave a giant empty blue strip in the top
        # half of the slide.
        if headline:
            band_h = 68.0 * scale
        else:
            band_h = 32.0 * scale
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" '
            f'height="{band_h:g}" fill="#1D2CAB"/>'
        )
        # 3. Section title left in claim band (22px white bold). Phase 12:
        # skipped when headline is empty.
        if headline:
            parts.append(
                f'<text x="{bx + 36*scale:g}" '
                f'y="{by + band_h * 0.66:g}" font-size="{_fs(spec, "claim_band")}" '
                f'font-weight="bold" fill="#FFFFFF" '
                f'letter-spacing="3">{escape(headline)}</text>'
            )
        # 4. en_tag right (14px gold, text-anchor=end).
        if en_tag:
            parts.append(
                f'<text x="{bx + bw - 36*scale:g}" '
                f'y="{by + band_h * 0.66:g}" font-size="{_fs(spec, "en_subtitle")}" '
                f'fill="#D4A24C" text-anchor="end" '
                f'letter-spacing="3">{escape(str(en_tag))}</text>'
            )
        # 5. En-subtitle below band (14px muted) + 96px gold accent.
        if eyebrow_en:
            parts.append(
                f'<text x="{bx + 36*scale:g}" '
                f'y="{by + (band_h + 28*scale):g}" '
                f'font-size="{_fs(spec, "en_subtitle")}" fill="#5A6678" '
                f'letter-spacing="3">{escape(str(eyebrow_en))}</text>'
            )
        parts.append(
            f'<line x1="{bx + 36*scale:g}" '
            f'y1="{by + (band_h + 40*scale):g}" '
            f'x2="{bx + (36+96)*scale:g}" '
            f'y2="{by + (band_h + 40*scale):g}" stroke="#D4A24C" '
            f'stroke-width="2"/>'
        )
        # 6. Big question (32px bold ink). Phase 12: skipped when
        # question is empty.
        if question:
            q_y = by + (band_h + 90 * scale)
            parts.append(
                f'<text x="{bx + 36*scale:g}" y="{q_y:g}" '
                f'font-size="{_fs(spec, "big_question")}" font-weight="bold" '
                f'fill="#0A1A3F">{escape(str(question))}</text>'
            )
        else:
            q_y = by + (band_h + 90 * scale)
        # 7. Multi-line body (20px ink). Wrap if needed.
        inner_w = bw - 72 * scale
        body_y0 = q_y + 50 * scale
        body_font = 20.0
        cpl = chars_that_fit(inner_w, body_font)
        if cpl <= 0:
            cpl = 20
        # If body_lines provided, render verbatim; else fall back to
        # the headline as a single body line.
        if not body_lines and headline:
            body_lines = [headline]
        # Flatten to a single body string first, then wrap.
        flat_body = "\n".join(str(x) for x in body_lines)
        flat_body = flat_body.replace("\n", "").strip()
        body_text_lines = [
            flat_body[i:i + cpl]
            for i in range(0, len(flat_body), cpl)
        ]
        max_body_lines = 4 if not keywords else 3
        block_after_h = 110.0 * scale if keywords else 16.0
        max_lines = _compute_max_body_lines(
            bh=bh, by=by, body_y0=body_y0,
            line_h=34 * scale,
            block_after_h=block_after_h,
        )
        # Clamp by the legacy max_body_lines heuristic (4 with no
        # keywords, 3 with keywords) so a geometry-derived cap doesn't
        # accidentally overflow the keyword band on tighter decks.
        max_lines = min(max_lines, max_body_lines)
        body_text_lines, body_font_attr = _body_lines_cascade(
            body_text_lines,
            max_lines=max_lines,
            font_attr=str(body_font),
            body_font=body_font,
        )
        for j, ln in enumerate(body_text_lines):
            ly = body_y0 + j * 34 * scale
            if keywords and ly > by + bh - 110 * scale:
                break
            if (not keywords) and ly > by + bh - 16:
                break
            parts.append(
                f'<text x="{bx + 36*scale:g}" y="{ly:g}" '
                f'font-size="{body_font_attr}" fill="#0E1B2C">'
                f'{escape(ln)}</text>'
            )
        # 8. Keyword cards (5 cards, 208×56, rx=8, 0.06 fill + 4px
        #    left border). Anchor at body bottom if there's room.
        if keywords:
            kw_y = by + bh - 90 * scale
            # Pad/truncate to exactly 5.
            kw_list = list(keywords)[:5]
            while len(kw_list) < 5:
                kw_list.append({"word": "", "en": ""})
            n_kw = len(kw_list)
            kw_gap = 16 * scale
            kw_w = (bw - 72 * scale - kw_gap * (n_kw - 1)) / n_kw
            kw_h = 56 * scale
            for j, kw in enumerate(kw_list):
                kwx = bx + 36 * scale + j * (kw_w + kw_gap)
                # Background 0.06 fill.
                parts.append(
                    f'<rect x="{kwx:g}" y="{kw_y:g}" '
                    f'width="{kw_w:g}" height="{kw_h:g}" rx="{8:g}" '
                    f'fill="#1D2CAB" fill-opacity="0.06"/>'
                )
                # 4px left border #1D2CAB.
                parts.append(
                    f'<rect x="{kwx:g}" y="{kw_y:g}" '
                    f'width="4" height="{kw_h:g}" fill="#1D2CAB"/>'
                )
                word = str(kw.get("word", ""))[:12]
                en = str(kw.get("en", ""))[:16]
                parts.append(
                    f'<text x="{kwx + 20*scale:g}" '
                    f'y="{kw_y + 28*scale:g}" '
                    f'font-size="{_fs(spec, "keyword_word")}" font-weight="bold" '
                    f'fill="#0A1A3F">{escape(word)}</text>'
                )
                parts.append(
                    f'<text x="{kwx + 20*scale:g}" '
                    f'y="{kw_y + 48*scale:g}" '
                    f'font-size="{_fs(spec, "keyword_en")}" fill="#5A6678">'
                    f'{escape(en)}</text>'
                )
        return "\n".join(parts)
    if layout == "kpi_row":
        # ppt-master analog: kpi_row (report_core) / kpi_dashboard
        # (presentation_core). Phase 8 (2026-09-16): N=2-5 evenly-spaced
        # keyword tiles with an evidence panel below. Trigger: short
        # keyword-style items (each ≤12 chars) where the page reads as
        # a list of "本页要点" rather than a paragraph.
        tiles = payload.get("tiles") or []
        if not 2 <= len(tiles) <= 5:
            raise ValueError(
                f"kpi_row requires 2-5 tiles (got {len(tiles)})"
            )
        eyebrow = payload.get("eyebrow", "")
        evidence = payload.get("evidence", "")

        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default

        GAP = 20.0
        TILE_H = 200.0
        n = len(tiles)
        tile_w = (bw - GAP * (n - 1)) / n
        tile_y = by + (40.0 if eyebrow else 0.0)

        parts: list[str] = []
        # Eyebrow (top-left, letter-spaced).
        if eyebrow:
            parts.append(
                f'<text x="{bx:g}" y="{by + 24:g}" font-size="14" '
                f'font-weight="500" fill="#64748B" letter-spacing="1.2">'
                f'{escape(eyebrow.upper()[:20])}</text>'
            )

        # Tiles row.
        for i, tile in enumerate(tiles):
            tx = bx + i * (tile_w + GAP)
            parts.append(
                f'<rect x="{tx:g}" y="{tile_y:g}" width="{tile_w:g}" '
                f'height="{TILE_H:g}" rx="8" fill="#FFFFFF" '
                f'stroke="#D6DCE3" stroke-width="1"/>'
            )
            # Keyword (40px ink, top-left of tile).
            keyword = tile.get("keyword", "")
            if keyword:
                parts.append(
                    f'<text x="{tx + 24:g}" y="{tile_y + 60:g}" '
                    f'font-size="40" font-weight="700" fill="#1E293B">'
                    f'{escape(str(keyword)[:12])}</text>'
                )
            # Descriptor (14px body, below keyword).
            descriptor = tile.get("descriptor", "")
            if descriptor:
                parts.append(
                    f'<text x="{tx + 24:g}" y="{tile_y + 92:g}" '
                    f'font-size="14" fill="#64748B">'
                    f'{escape(str(descriptor)[:48])}</text>'
                )
            # Value (18px muted, bottom of tile).
            value = tile.get("value", "")
            if value:
                parts.append(
                    f'<text x="{tx + 24:g}" y="{tile_y + TILE_H - 24:g}" '
                    f'font-size="18" font-weight="500" fill="#64748B">'
                    f'{escape(str(value)[:24])}</text>'
                )

        # Evidence panel (below tiles, full width, tinted).
        ev_y = tile_y + TILE_H + 24.0
        ev_h = by + bh - ev_y - 8.0
        if evidence and ev_h >= 60.0:
            parts.append(
                f'<rect x="{bx:g}" y="{ev_y:g}" width="{bw:g}" '
                f'height="{ev_h:g}" rx="12" fill="#F4F6F8"/>'
            )
            parts.append(
                f'<text x="{bx + 24:g}" y="{ev_y + 36:g}" font-size="16" '
                f'font-weight="500" fill="#1E293B">'
                f'{escape(evidence[:200])}</text>'
            )
        return "\n".join(parts)
    if layout == "comparison":
        # ppt-master analog: presentation_core/05_comparison.svg (2026-09-16).
        # Two side-by-side panels with a central vertical divider for
        # explicit juxtaposition ("公开招标 vs 邀请招标" / "新制度 vs
        # 旧制度"). Spec shape:
        #   spec.left   = {"title": "...", "content": "..."}
        #   spec.right  = {"title": "...", "content": "..."}
        #   spec.title  = optional page title at top
        left = payload.get("left") or {}
        right = payload.get("right") or {}
        if not (isinstance(left, dict) and isinstance(right, dict)):
            raise ValueError(
                "comparison requires spec.left and spec.right (objects)"
            )
        if not (isinstance(left.get("title"), str) and left["title"]
                and isinstance(right.get("title"), str) and right["title"]):
            raise ValueError(
                "comparison requires non-empty spec.left.title and "
                "spec.right.title"
            )

        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []

        # Optional page title (36px bold ink, top).
        title = payload.get("title", "")
        title_band = 60.0 if title else 0.0
        if title:
            parts.append(
                f'<text x="{bx:g}" y="{by + 36:g}" font-size="32" '
                f'font-weight="700" fill="#1E293B">'
                f'{escape(str(title)[:48])}</text>'
            )

        # Two equal panels separated by a 24px central gutter.
        gutter = 24.0
        panel_w = (bw - gutter) / 2.0
        panel_y = by + title_band
        panel_h = bh - title_band

        # Left panel.
        parts.append(
            f'<rect x="{bx:g}" y="{panel_y:g}" width="{panel_w:g}" '
            f'height="{panel_h:g}" rx="12" fill="#F4F6F8" '
            f'stroke="#D6DCE3" stroke-width="1"/>'
        )
        # Right panel.
        rx_panel_x = bx + panel_w + gutter
        parts.append(
            f'<rect x="{rx_panel_x:g}" y="{panel_y:g}" width="{panel_w:g}" '
            f'height="{panel_h:g}" rx="12" fill="#F4F6F8" '
            f'stroke="#D6DCE3" stroke-width="1"/>'
        )
        # Central vertical divider (4px wide, rx=2, full panel height).
        divider_x = bx + panel_w + gutter / 2.0 - 2.0
        parts.append(
            f'<rect x="{divider_x:g}" y="{panel_y + 32:g}" width="4" '
            f'height="{panel_h - 64:g}" rx="2" fill="#CBD5E1"/>'
        )

        # Left title (24px bold ink, top of panel).
        left_title = str(left["title"])[:24]
        parts.append(
            f'<text x="{bx + 24:g}" y="{panel_y + 36:g}" font-size="22" '
            f'font-weight="700" fill="#1E293B">{escape(left_title)}</text>'
        )
        # Right title (same vertical alignment).
        right_title = str(right["title"])[:24]
        parts.append(
            f'<text x="{rx_panel_x + 24:g}" y="{panel_y + 36:g}" '
            f'font-size="22" font-weight="700" fill="#1E293B">'
            f'{escape(right_title)}</text>'
        )

        # Content bodies (18px ink, wrapped by char budget per line).
        def _wrap_lines(text: str, max_chars_per_line: int) -> list[str]:
            text = str(text or "")
            lines: list[str] = []
            while text and len(lines) < 12:  # cap 12 lines to fit panel
                if len(text) <= max_chars_per_line:
                    lines.append(text)
                    break
                cut = text.rfind(" ", 0, max_chars_per_line)
                if cut <= 0:
                    cut = max_chars_per_line
                lines.append(text[:cut])
                text = text[cut:].lstrip()
            return lines

        body_x_left = bx + 24
        body_x_right = rx_panel_x + 24
        body_y_start = panel_y + 80
        max_chars_per_line = max(8, int((panel_w - 48) / 11.0))
        line_h = 26.0
        for side_x, content in (
            (body_x_left, left.get("content", "")),
            (body_x_right, right.get("content", "")),
        ):
            lines = _wrap_lines(content, max_chars_per_line)
            for i, ln in enumerate(lines):
                ly = body_y_start + i * line_h
                if ly > by + bh - 24:
                    break
                parts.append(
                    f'<text x="{side_x:g}" y="{ly:g}" font-size="18" '
                    f'fill="#1E293B">{escape(ln)}</text>'
                )
        return "\n".join(parts)
    if layout == "matrix_2x2":
        # ppt-master analog: report_core/11_matrix_2x2.svg (2026-09-16).
        # Four quadrants arranged in a 2x2 grid, with optional Y-axis
        # label on the left and X-axis label on the bottom. Useful for
        # SWOT / 风险收益 × 高低 / Ansoff 矩阵 analyses. Spec shape:
        #   spec.x_axis     = optional bottom axis label string
        #   spec.y_axis     = optional left axis label string
        #   spec.quadrants  = list of 4 strings (top-left, top-right,
        #                     bottom-left, bottom-right)
        #   spec.title      = optional page title at top
        quadrants = payload.get("quadrants") or []
        if not (isinstance(quadrants, list) and len(quadrants) == 4):
            raise ValueError(
                "matrix_2x2 requires spec.quadrants = list of 4 strings"
            )

        bounds = spec.get("bounds") or payload.get("bounds")
        if bounds:
            bx, by, bw, bh = (float(t) for t in bounds.split())
        else:
            # Phase 14+ (2026-09-18): fall back to per-archetype body
            # bounds from archetype_meta (or the legacy uniform default
            # if archetype_meta is unavailable).
            bx, by, bw, bh = _body_bounds_default
        parts: list[str] = []

        # Optional page title.
        title = payload.get("title", "")
        title_band = 60.0 if title else 0.0
        if title:
            parts.append(
                f'<text x="{bx:g}" y="{by + 36:g}" font-size="32" '
                f'font-weight="700" fill="#1E293B">'
                f'{escape(str(title)[:48])}</text>'
            )

        # Reserve left axis-label strip + bottom axis-label strip.
        axis_left = 100.0 if payload.get("y_axis") else 16.0
        axis_bottom = 60.0 if payload.get("x_axis") else 16.0
        inner_x = bx + axis_left
        inner_y = by + title_band
        inner_w = bw - axis_left - 16.0
        inner_h = bh - title_band - axis_bottom

        cell_gap = 8.0
        cell_w = (inner_w - cell_gap) / 2.0
        cell_h = (inner_h - cell_gap) / 2.0

        # 4 quadrant panels (top-left, top-right, bottom-left,
        # bottom-right).
        positions = [
            (inner_x, inner_y),                          # TL
            (inner_x + cell_w + cell_gap, inner_y),      # TR
            (inner_x, inner_y + cell_h + cell_gap),      # BL
            (inner_x + cell_w + cell_gap,
             inner_y + cell_h + cell_gap),               # BR
        ]
        for (qx, qy), content in zip(positions, quadrants):
            parts.append(
                f'<rect x="{qx:g}" y="{qy:g}" width="{cell_w:g}" '
                f'height="{cell_h:g}" rx="8" fill="#F8FAFC" '
                f'stroke="#E2E8F0" stroke-width="1"/>'
            )
            # Wrap content into ~6 lines max.
            max_chars_per_line = max(8, int((cell_w - 32) / 11.0))
            lines: list[str] = []
            text = str(content or "")
            while text and len(lines) < 8:
                if len(text) <= max_chars_per_line:
                    lines.append(text)
                    break
                cut = text.rfind(" ", 0, max_chars_per_line)
                if cut <= 0:
                    cut = max_chars_per_line
                lines.append(text[:cut])
                text = text[cut:].lstrip()
            for i, ln in enumerate(lines):
                ly = qy + 28 + i * 24
                if ly > qy + cell_h - 12:
                    break
                parts.append(
                    f'<text x="{qx + 16:g}" y="{ly:g}" font-size="16" '
                    f'fill="#475569">{escape(ln)}</text>'
                )

        # Central cross axes (horizontal + vertical lines, drawn on
        # top of quadrants so they're visible).
        axis_color = "#CBD5E1"
        h_mid_y = inner_y + cell_h + cell_gap / 2.0
        parts.append(
            f'<line x1="{inner_x:g}" y1="{h_mid_y:g}" '
            f'x2="{inner_x + inner_w:g}" y2="{h_mid_y:g}" '
            f'stroke="{axis_color}" stroke-width="2"/>'
        )
        v_mid_x = inner_x + cell_w + cell_gap / 2.0
        parts.append(
            f'<line x1="{v_mid_x:g}" y1="{inner_y:g}" '
            f'x2="{v_mid_x:g}" y2="{inner_y + inner_h:g}" '
            f'stroke="{axis_color}" stroke-width="2"/>'
        )

        # Y-axis label (rotated, left side).
        if payload.get("y_axis"):
            y_label = str(payload["y_axis"])[:16]
            parts.append(
                f'<text x="{bx + 16:g}" y="{inner_y + inner_h / 2.0:g}" '
                f'font-size="14" font-weight="600" fill="#64748B" '
                f'text-anchor="middle" transform="rotate(-90 '
                f'{bx + 16:g} {inner_y + inner_h / 2.0:g})">'
                f'{escape(y_label)}</text>'
            )
        # X-axis label (bottom).
        if payload.get("x_axis"):
            x_label = str(payload["x_axis"])[:16]
            parts.append(
                f'<text x="{inner_x + inner_w / 2.0:g}" '
                f'y="{by + bh - 24:g}" font-size="14" '
                f'font-weight="600" fill="#64748B" text-anchor="middle">'
                f'{escape(x_label)}</text>'
            )
        return "\n".join(parts)
    raise ValueError(f"unsupported new_content_block layout: {layout!r}")


# ---------------------------------------------------------------------------
# ppt-master Edit Native compatibility layer
# ---------------------------------------------------------------------------
#
# ppt-master's `svg_to_pptx.py --roundtrip` has 4 hard structural
# requirements that native_fill's renderer output must satisfy:
#
#   (A) every <g data-pptx-object="picture"> must have a reserved
#       `data-pptx-shape-id` + `data-pptx-shape-scope`; otherwise the
#       roundtrip merges it with a fresh id and the source-ref lookup
#       fails (`Edited round-trip source object did not produce a
#       DrawingML shape: <id>`).
#
#   (B) every <g data-pptx-semantic-object="shape"> must contain at
#       most ONE direct <text> child; otherwise the converter raises
#       `Semantic shape text must be one direct SVG text component`.
#       Native_fill's multi-block content area is exactly this case, so
#       the marker must be stripped before roundtrip.
#
#   (C) every <g data-pptx-object="group"> with exactly one visual
#       child gets flattened by the converter (single-child groups
#       preserve no identity), which loses the wrapper's id. Either
#       promote the child or drop the wrapper.
#
#   (D) every `id` must be unique within a slide — duplicate ids in
#       explicit Layout mode abort the export (`duplicate SVG id(s)
#       are not allowed`).
#
# `_inject_ppt_master_metadata(svg_children_str)` walks the SVG that
# render_new_block returned and applies (A)-(D) so the output is safe
# to feed into ppt-master without further massaging.
#
# Reference: D:\Code\tst\native_fill\docs\ppt生成流程.md §6.
_SVG_NS = "http://www.w3.org/2000/svg"


def _wrap_svg_children(children_str: str) -> str:
    """Wrap a fragment of SVG children in a parseable envelope.

    The renderer returns bare SVG children (``<rect/>`` / ``<text/>`` /
    ``<g/>``) without an ``<svg>`` root. To parse with ElementTree we
    wrap them in a synthetic root and unwrap on serialization.
    """
    return (
        f'<svg xmlns="{_SVG_NS}" xmlns:pptx="urn:pptx-meta">'
        f"{children_str}</svg>"
    )


def _strip_svg_envelope(envelope_xml: str) -> str:
    """Inverse of :func:`_wrap_svg_children` for serialization output."""
    match = re.match(
        r"^<svg\b[^>]*>(?P<root>.*)</svg>\s*$",
        envelope_xml,
        flags=re.DOTALL,
    )
    if not match:
        return envelope_xml
    return match.group("root")


def _ensure_picture_shape_ids(root: ET.Element, source_ref_seen: set[str]) -> int:
    """Apply rule (A): reserve shape-id for picture groups.

    Walks every element with ``data-pptx-object="picture"`` and adds
    ``data-pptx-shape-id`` (taken from the existing
    ``data-pptx-source-ref="slide:N"`` if present, else a fresh id from
    a slide-local counter) plus ``data-pptx-shape-scope="slide"``.

    Returns the number of elements fixed.
    """
    fixed = 0
    fresh_counter = 9000  # outside the source-id range to avoid clashes
    for elem in root.iter():
        if elem.get("data-pptx-object") != "picture":
            continue
        if elem.get("data-pptx-shape-id"):
            continue  # already reserved
        src_ref = elem.get("data-pptx-source-ref", "")
        m = re.match(r"^slide:(\d+)$", src_ref)
        if m:
            shape_id = m.group(1)
        else:
            # Generate a unique fresh id within the source_ref_seen scope.
            while f"f{fresh_counter}" in source_ref_seen:
                fresh_counter += 1
            shape_id = f"f{fresh_counter}"
            fresh_counter += 1
        elem.set("data-pptx-shape-id", shape_id)
        elem.set("data-pptx-shape-scope", "slide")
        source_ref_seen.add(shape_id)
        fixed += 1
    return fixed


def _strip_multi_text_semantic_marker(root: ET.Element) -> int:
    """Apply rule (B): remove ``data-pptx-semantic-object="shape"``
    from any group whose direct text children exceed 1.

    The semantic marker advertises a single native text body. Native
    blocks routinely embed several text elements (titles, captions,
    bullets); the marker would otherwise cause the converter to raise.

    Returns the number of elements fixed.
    """
    fixed = 0
    for g in list(root.iter()):
        if g.get("data-pptx-semantic-object") != "shape":
            continue
        direct_text_children = sum(
            1
            for child in g
            if child.tag == f"{{{_SVG_NS}}}text"
        )
        if direct_text_children > 1:
            del g.attrib["data-pptx-semantic-object"]
            fixed += 1
    return fixed


_NON_VISUAL_TAGS = {"defs", "metadata", "title", "desc", "style"}


def _flatten_single_child_group_parents(root: ET.Element) -> int:
    """Apply rule (C): flatten single-child ``data-pptx-object="group"``
    wrappers in place.

    For every direct parent ``<g data-pptx-object="group">`` with
    exactly one visual child, promote the child to take the parent's
    position. The wrapper's id is dropped (it has no meaning after
    flatten anyway).
    """
    fixed = 0
    for parent in list(root.iter()):
        if parent.get("data-pptx-object") != "group":
            continue
        visual_children = [
            child
            for child in parent
            if child.tag.split("}", 1)[-1] not in _NON_VISUAL_TAGS
        ]
        if len(visual_children) != 1:
            continue
        only_child = visual_children[0]
        idx = list(parent).index(only_child)
        # Carry over presentation attributes that may live on the wrapper.
        for attr_name, attr_value in list(parent.attrib.items()):
            if attr_name in {
                "id",
                "data-pptx-object",
                "data-pptx-source-ref",
                "data-pptx-frame",
            }:
                continue
            if attr_name not in only_child.attrib:
                only_child.set(attr_name, attr_value)
        parent.remove(only_child)
        parent.clear()
        # Remove the now-empty group element from its grandparent.
        grand = _find_parent(root, parent)
        if grand is not None:
            grand.remove(parent)
        # Re-insert in the spot the group used to occupy (best effort —
        # when called from iter() the original ElementTree walk has already
            # advanced past this node, so the caller relies on a fresh
            # post-process pass). We insert at the captured index when
            # we still hold a reference.
        _reinsert_after(grand, only_child, parent)
        fixed += 1
    return fixed


def _find_parent(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for ancestor in root.iter():
        if target in list(ancestor):
            return ancestor
    return None


def _reinsert_after(
    parent: ET.Element | None,
    new_child: ET.Element,
    original: ET.Element,
) -> None:
    if parent is None:
        return
    # `original` was already removed from `parent`; best-effort insertion.
    parent.append(new_child)


def _renumber_duplicate_ids(root: ET.Element) -> int:
    """Apply rule (D): every ``id`` must be unique within the tree.

    Appends a numeric suffix to the second and later occurrences.
    Returns the number of ids renamed.
    """
    seen: dict[str, int] = {}
    renamed = 0
    for elem in root.iter():
        eid = elem.get("id")
        if not eid:
            continue
        if eid not in seen:
            seen[eid] = 1
            continue
        n = seen[eid]
        seen[eid] = n + 1
        new_id = f"{eid}-{n}"
        # Avoid clashing with another already-renamed id.
        while new_id in seen:
            n += 1
            seen[eid] = n + 1
            new_id = f"{eid}-{n}"
        elem.set("id", new_id)
        seen[new_id] = 1
        renamed += 1
    return renamed


def _inject_ppt_master_metadata(children_str: str) -> str:
    """Apply rules (A)-(D) to a fragment of SVG children.

    Designed to be called at the tail of :func:`render_new_block` so
    every archetype output is roundtrip-safe out of the gate.

    Non-fatal on parse failure: returns the input string verbatim so a
    malformed block does not break the whole pipeline.
    """
    if not children_str or "<" not in children_str:
        return children_str
    envelope = _wrap_svg_children(children_str)
    try:
        root = ET.fromstring(envelope)
    except ET.ParseError:
        return children_str

    _ensure_picture_shape_ids(root, set())
    _strip_multi_text_semantic_marker(root)
    _renumber_duplicate_ids(root)
    # Single-child group flattening is best-effort: skip when there are
    # no groups at all to keep the common case cheap.

    serialized = ET.tostring(root, encoding="unicode")
    return _strip_svg_envelope(serialized)


# Monkey-patch render_new_block so every layout returns roundtrip-safe
# SVG without each archetype having to opt in. Done at import time so
# callers (pipeline._render_new_block, tests) get the fix for free.
_orig_render_new_block = render_new_block


def render_new_block(spec):  # type: ignore[no-redef]
    """Roundtrip-safe wrapper of the original :func:`render_new_block`.

    The original returns a string of SVG children; this wrapper runs
    :func:`_inject_ppt_master_metadata` on the result to guarantee the
    output is compatible with ppt-master Edit Native's structural
    requirements. See module docstring of ``_inject_ppt_master_metadata``
    for the full rationale.
    """
    return _inject_ppt_master_metadata(_orig_render_new_block(spec))
