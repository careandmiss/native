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


def render_new_block(spec: dict[str, Any]) -> str:
    """Render a ``new_content_block`` spec into raw SVG children.

    Supports four layouts: ``"raw"`` (caller supplied SVG),
    ``"3-column-cards"`` (tile row), ``"flow-steps"`` (numbered
    horizontal flow), and ``"revision-table"`` (header + rows). Anything
    else raises.

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
        bx, by, bw, bh = (float(t) for t in bounds.split())
        gap = 16.0
        card_w = (bw - gap * (n - 1)) / n
        for i, card in enumerate(cards):
            cx = bx + i * (card_w + gap)
            color = card.get("color", "#1D2CAB")
            title = card.get("title", "")
            items = coerce_str_list(card.get("items", []))
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
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" font-size="14" '
                    f'fill="#222">{escape(item)}</text>'
                )
        return "\n".join(parts)
    if layout == "flow-steps":
        steps = payload.get("steps") or []
        if not 2 <= len(steps) <= 5:
            raise ValueError("flow-steps supports 2-5 steps per row")
        bounds = spec.get("bounds") or payload.get("bounds")
        bx, by, bw, bh = (float(t) for t in bounds.split())
        parts: list[str] = []
        n = len(steps)
        gap = 16.0
        step_w = (bw - gap * (n - 1)) / n
        for i, step in enumerate(steps):
            cx = bx + i * (step_w + gap)
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
        rows = payload.get("rows") or []
        if not rows:
            raise ValueError("revision-table requires spec.rows")
        bounds = spec.get("bounds") or payload.get("bounds")
        bx, by, bw, bh = (float(t) for t in bounds.split())
        # Columns: date / status / content / author (4 columns)
        col_w = bw / 4
        row_h = min(28.0, (bh - 32) / max(len(rows), 1))
        parts: list[str] = []
        # Header background.
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{bw:g}" height="28" '
            f'fill="#1D2CAB" fill-opacity="0.08"/>'
        )
        for j, header in enumerate(("日期", "状态", "内容", "修改人")):
            parts.append(
                f'<text x="{bx + j * col_w + 12:g}" y="{by + 19:g}" '
                f'font-size="13" font-weight="bold" fill="#1D2CAB">'
                f'{escape(header)}</text>'
            )
        # Rows.
        for i, row in enumerate(rows):
            ry = by + 32 + i * row_h
            if ry + row_h > by + bh:
                break  # bounds budget exhausted
            for j, key in enumerate(("date", "status", "content", "author")):
                raw_val = row.get(key, "")
                # Bug 10 fix: list values now render as multiple <tspan>
                # lines (one per item) instead of "; "-joined into a
                # single long cell string. The first item stays inline;
                # subsequent items use dy="14" to step down a line within
                # the same <text> element (so the cell stays vertically
                # aligned with its row baseline).
                if isinstance(raw_val, (list, tuple)):
                    cell_items = [str(v) for v in raw_val]
                elif isinstance(raw_val, dict):
                    cell_items = [f"{k}={v}" for k, v in raw_val.items()]
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
                parts.append(
                    f'<text x="{bx + j * col_w + 12:g}" y="{ry + 18:g}" '
                    f'font-size="12" fill="#333">{"".join(tspans)}</text>'
                )
            # Row separator.
            parts.append(
                f'<line x1="{bx:g}" y1="{ry + row_h:g}" x2="{bx + bw:g}" '
                f'y2="{ry + row_h:g}" stroke="#E0E0E0" stroke-width="0.5"/>'
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
        bx, by, bw, bh = (float(t) for t in bounds.split())
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
        bx, by, bw, bh = (float(t) for t in bounds.split())
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
        bx, by, bw, bh = (float(t) for t in bounds.split())
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
        bx, by, bw, bh = (float(t) for t in bounds.split())
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
        bx, by, bw, bh = (float(t) for t in bounds.split())
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
        bx, by, bw, bh = (float(t) for t in bounds.split())
        # Adaptive line height: 30px max, scale down for many items so
        # the whole list fits. 40px top/bottom reserved for the bar +
        # breathing room.
        line_h = min(30.0, (bh - 40.0) / max(1, len(items)))
        parts: list[str] = [
            # Left color bar (6px wide).
            f'<rect x="{bx:g}" y="{by:g}" width="6" height="{bh:g}" '
            f'fill="{escape(color)}"/>'
        ]
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
            display = item[:80] + ("…" if len(item) > 80 else "")
            parts.append(
                f'<text x="{bx + 24:g}" y="{ty:g}" font-size="16" '
                f'fill="#222">{i + 1}. {escape(display)}</text>'
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
        # ppt-master analog: content_caption (256px left rail + 768px
        # right panel with vertical divider at x=360 in ppt-master's
        # 1280px canvas; we anchor to body_bounds so it works on any
        # caller-supplied bounds).
        title = payload.get("title", "")
        eyebrow = payload.get("eyebrow", "")  # optional small label
        body = payload.get("body", "")
        if not isinstance(title, str) or not title:
            raise ValueError("statement-caption requires spec.title")
        if not isinstance(body, str) or not body:
            raise ValueError("statement-caption requires spec.body")

        bounds = spec.get("bounds") or payload.get("bounds")
        bx, by, bw, bh = (float(t) for t in bounds.split())

        # Geometry: 256px left rail + 16px gutter + 768px right panel.
        # ppt-master uses 272 = 256 + 16; we anchor the rail to bx so
        # any caller-supplied body_bounds works (boteng passes
        # "120 130 1060 480" → rail at x=120..376, divider @ x=392).
        RAIL_W = 256.0
        DIV_X = bx + RAIL_W + 16.0
        PANEL_X = DIV_X + 16.0
        TOP = by + 24.0

        parts: list[str] = []
        # 1. Soft tint behind left rail (NOT a card — flat block).
        parts.append(
            f'<rect x="{bx:g}" y="{by:g}" width="{RAIL_W:g}" '
            f'height="{bh:g}" fill="#F4F6F8"/>'
        )
        # 2. Vertical hairline divider (1px, calm gray).
        parts.append(
            f'<line x1="{DIV_X:g}" y1="{by + 20:g}" '
            f'x2="{DIV_X:g}" y2="{by + bh - 20:g}" '
            f'stroke="#D6DCE3" stroke-width="1"/>'
        )
        # 3. Eyebrow (optional small chapter label) — letter-spaced.
        if eyebrow:
            parts.append(
                f'<text x="{bx + 32:g}" y="{TOP + 12:g}" font-size="14" '
                f'font-weight="500" fill="#64748B" letter-spacing="1.2">'
                f'{escape(eyebrow.upper())}</text>'
            )
            title_y = TOP + 56.0
        else:
            title_y = TOP + 32.0
        # 4. Left rail title (32px 700 ink).
        parts.append(
            f'<text x="{bx + 32:g}" y="{title_y:g}" font-size="32" '
            f'font-weight="700" fill="#1E293B">{escape(title)}</text>'
        )
        # 5. Right panel body — CJK-aware wrap, same approach as
        # simple-text. Auto-shrink to 14px if lines don't fit bh.
        from .text_width import chars_that_fit
        inner_w = bx + bw - PANEL_X - 32.0
        font_size = 19.0
        cpl = chars_that_fit(inner_w, font_size)
        if cpl <= 0:
            raise ValueError(
                f"statement-caption bounds too narrow for font_size="
                f"{font_size} (inner_w={inner_w:g})"
            )
        lines = [body[i:i + cpl] for i in range(0, len(body), cpl)]
        usable = max(1.0, (bh - 2 * 32.0) / (font_size * 1.5))
        while len(lines) > usable and font_size > 14.0:
            font_size -= 1.0
            cpl = chars_that_fit(inner_w, font_size)
            if cpl <= 0:
                break
            lines = [body[i:i + cpl] for i in range(0, len(body), cpl)]
            usable = max(1.0, (bh - 2 * 32.0) / (font_size * 1.5))
        for i, line in enumerate(lines):
            ly = TOP + font_size * (i + 1)
            if ly > by + bh - 8:
                break
            parts.append(
                f'<text x="{PANEL_X + 16:g}" y="{ly:g}" '
                f'font-size="{font_size:g}" fill="#1E293B">'
                f'{escape(line)}</text>'
            )
        return "\n".join(parts)
    if layout == "procedural-steps":
        # ppt-master analog: process_timeline (4 evenly-spaced nodes
        # on horizontal axis) + data_story takeaway band. Used when a
        # markdown section synthesizes into N macro phases with a
        # short key-message summary. We anchor the phase row at
        # y=by+80 and the takeaway band at y=by+250 so the layout
        # fits any caller-supplied body_bounds.
        steps = payload.get("steps") or []
        takeaways = payload.get("takeaways") or []
        eyebrow = payload.get("eyebrow", "")
        if not 2 <= len(steps) <= 5:
            raise ValueError(
                f"procedural-steps requires 2-5 steps (got {len(steps)})"
            )
        if not 1 <= len(takeaways) <= 5:
            raise ValueError(
                f"procedural-steps requires 1-5 takeaways "
                f"(got {len(takeaways)})"
            )

        bounds = spec.get("bounds") or payload.get("bounds")
        bx, by, bw, bh = (float(t) for t in bounds.split())

        # Phase row geometry.
        PHASE_COLOR = "#1E293B"  # ink, not the legacy #1D2CAB blue
        phase_y = by + 80.0
        phase_h = 140.0
        margin = 60.0
        n = len(steps)
        span = bw - 2 * margin

        parts: list[str] = []
        # 1. Optional eyebrow (small chapter label).
        if eyebrow:
            parts.append(
                f'<text x="{bx:g}" y="{by + 16:g}" font-size="12" '
                f'font-weight="600" fill="#64748B" letter-spacing="2">'
                f'{escape(eyebrow.upper())}</text>'
            )
        # 2. Phase circles + connectors.
        for i, step in enumerate(steps):
            cx = bx + margin + span * i / max(n - 1, 1)
            cy = phase_y + 28.0
            parts.append(
                f'<circle cx="{cx:g}" cy="{cy:g}" r="28" '
                f'fill="{PHASE_COLOR}"/>'
            )
            parts.append(
                f'<text x="{cx:g}" y="{cy + 6:g}" text-anchor="middle" '
                f'font-size="16" font-weight="700" fill="#FFFFFF">'
                f'{i + 1}</text>'
            )
            # Step label below circle (18px ink).
            parts.append(
                f'<text x="{cx:g}" y="{cy + 56:g}" text-anchor="middle" '
                f'font-size="18" font-weight="600" fill="#1E293B">'
                f'{escape(step.get("label", f"Step {i + 1}"))}</text>'
            )
            # Step detail (14px body, cap 24 chars).
            detail = step.get("detail", "")
            if detail:
                parts.append(
                    f'<text x="{cx:g}" y="{cy + 84:g}" text-anchor="middle" '
                    f'font-size="14" fill="#64748B">'
                    f'{escape(detail[:24])}</text>'
                )
            # Connector to next step.
            if i < n - 1:
                next_cx = bx + margin + span * (i + 1) / max(n - 1, 1)
                parts.append(
                    f'<line x1="{cx + 30:g}" y1="{cy:g}" '
                    f'x2="{next_cx - 30:g}" y2="{cy:g}" '
                    f'stroke="#D6DCE3" stroke-width="2"/>'
                )
        # 3. Takeaway band (bottom panel).
        band_y = phase_y + phase_h + 30.0
        band_h = by + bh - band_y - 16.0
        if band_h < 60.0:
            # bounds too small to fit a takeaway panel — emit just the
            # heading line as a one-line summary so the page still
            # produces a polished result.
            parts.append(
                f'<text x="{bx:g}" y="{band_y:g}" font-size="16" '
                f'font-weight="700" fill="#1E293B">关键要点</text>'
            )
        else:
            parts.append(
                f'<rect x="{bx:g}" y="{band_y:g}" width="{bw:g}" '
                f'height="{band_h:g}" rx="12" fill="#F4F6F8"/>'
            )
            parts.append(
                f'<text x="{bx + 24:g}" y="{band_y + 28:g}" font-size="16" '
                f'font-weight="700" fill="#1E293B">关键要点</text>'
            )
            for i, msg in enumerate(takeaways):
                ty = band_y + 60 + i * 28
                if ty > band_y + band_h - 12:
                    break
                parts.append(
                    f'<text x="{bx + 24:g}" y="{ty:g}" font-size="18" '
                    f'fill="#1E293B">· {escape(msg[:80])}</text>'
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
        bx, by, bw, bh = (float(t) for t in bounds.split())

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
    raise ValueError(f"unsupported new_content_block layout: {layout!r}")
