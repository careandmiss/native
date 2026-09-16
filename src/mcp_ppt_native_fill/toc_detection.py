"""TOC slot detection + cleanup helpers.

Pure helpers for detecting, filtering, and cleaning up TOC slide
contents. Extracted from pipeline.py for module-size reduction;
imports nothing from pipeline.py so the dependency graph stays
acyclic.

Public API (used by ``pipeline.py`` and ``workspace_expand.py``):
    * :func:`find_toc_svg` — locate the TOC slide by 目录/CONTENTS marker.
    * :func:`filter_toc_manual_mapping` — drop manual mapping entries
      that target TOC slot shape ids.
    * :func:`is_toc_slot_placeholder` — detect unfilled template
      default strings.
    * :func:`detect_toc_slot_shape_ids` — auto-locate title/subtitle
      shape ids for an N×M grid from SVG geometry.
    * :func:`remove_empty_toc_slots` — strip unfilled TOC slot <g>s.
    * :func:`fill_missing_content_blocks` — synthesize default 3-col
      cards for cloned content slides the LLM forgot.
    * Markdown helpers :func:`strip_inline_markdown`,
      :func:`split_markdown_sections`, :func:`cards_for_section`,
      :func:`cards_from_body`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .pipeline import PipelineState

log = logging.getLogger(__name__)


# Default TOC placeholder phrases that the LLM did NOT replace. When a
# TOC slot's text still equals one of these (after whitespace
# normalisation), the slot is "unfilled" and its <g> elements (accent
# bar + title + subtitle) are removed so the rendered slide does not
# show template default text in empty rows.
#
# Bug 06 fix: entries are stored in whitespace-normalised form (no
# spaces) to match ``"".join(text.split())`` in :func:`is_toc_slot_placeholder`.
# The previous set had "click to add title" (with spaces) which never
# matched the normalised input.
TOC_PLACEHOLDER_PHRASES: frozenset[str] = frozenset({
    "单击添加大标题", "点击添加大标题", "点击添加标题",
    "clicktoaddtitle", "clickheretoaddtitle",
    "单击添加",
    "",  # genuinely empty
})


def find_toc_svg(authoring_dir: Path) -> str:
    """Return the filename of the slide that contains the TOC marker.

    Looks for any ``slide_*.svg`` in ``authoring_dir`` whose body
    contains the Chinese "目录" or English "CONTENTS" string. Used as
    the default value for ``toc_svg`` in
    :func:`workspace_expand.expand_workspace_from_toc` when the caller
    doesn't supply one explicitly.

    Mirrors the same scan logic used by :func:`remove_empty_toc_slots`
    (lines below) but returns just the filename rather than the list
    of candidate paths.

    Raises
    ------
    ValueError
        If no slide in ``authoring_dir`` contains a TOC marker. Caller
        should pass ``toc_svg=`` explicitly in that case.
    """
    candidates: list[Path] = []
    for path in sorted(authoring_dir.glob("slide_*.svg")):
        try:
            data = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "目录" in data or "CONTENTS" in data.upper():
            candidates.append(path)
    if not candidates:
        raise ValueError(
            f"No TOC slide found in {authoring_dir}: no slide contains "
            "'目录' or 'CONTENTS'. Pass toc_svg= explicitly."
        )
    return candidates[0].name


def filter_toc_manual_mapping(
    content_mapping: dict[str, dict[str, str]],
    toc_shape_ids: list[str],
) -> tuple[dict[str, dict[str, str]], int]:
    """Drop manual mapping entries that target TOC slot shape ids.

    When ``expand_toc_from_markdown`` is on, the auto-fill from
    :func:`workspace_expand.expand_workspace_from_toc` would overwrite
    any manual TOC entries anyway — but dropping them up front makes
    the intent explicit and surfaces the conflict in audit logs
    instead of silently overwriting.

    Parameters
    ----------
    content_mapping:
        ``{svg_filename: {shape_id: new_text}}`` — the caller's
        manual mapping. Mutated by replacing per-svg dicts (not the
        outer container) to keep the caller's reference intact.
    toc_shape_ids:
        Union of title + subtitle shape ids supplied by the caller
        for smart TOC fill.

    Returns
    -------
    ``(filtered_mapping, total_dropped)``. The returned mapping is a
    shallow copy with per-svg dicts replaced as needed.
    """
    toc_set = set(toc_shape_ids)
    total_dropped = 0
    filtered: dict[str, dict[str, str]] = {}
    for svg_name, edits in content_mapping.items():
        kept = {k: v for k, v in edits.items() if k not in toc_set}
        dropped = len(edits) - len(kept)
        if dropped:
            log.info(
                "expand_toc_from_markdown: dropped %d manual TOC entries "
                "in %s (shape_ids: %s)",
                dropped, svg_name,
                [k for k in edits if k not in kept],
            )
            total_dropped += dropped
        filtered[svg_name] = kept
    return filtered, total_dropped


def is_toc_slot_placeholder(text: str) -> bool:
    """True if the slot text is still template default (unfilled by LLM).

    Matched patterns:
      * The original template default strings (Chinese: "单击添加大标题",
        "点击添加大标题" etc.; English: "click to add title").
      * Subtitle defaults (boteng: "单击添加小标题/标题英文 单价..." style).
      * Boteng's default row labels: "第五章" / "第六章" — these are
        template placeholders the LLM should have replaced with a real
        chapter name. If still present, the row is unused.
      * Empty string.

    Bug 06 fix: previously matched ``[Cc]hapter\\s*\\d+`` as a placeholder,
    which wrongly flagged short English chapter titles like "Chapter 1"
    as unfilled template defaults. Replaced with a strict whitelist.
    """
    t = "".join(text.split())  # strip whitespace
    if t in TOC_PLACEHOLDER_PHRASES:
        return True
    # "单击添加小标题/标题英文" style boteng subtitle
    if "添加" in t and "标题" in t:
        return True
    if t.startswith("添加"):
        return True
    # Boteng's default row label "第N章" (e.g. "第五章", "第六章").
    # The real chapter title in boteng is "第N章 / <chapter-name>" with
    # content text, so a bare "第N章" with no associated content is
    # the template default we want to remove.
    if re.fullmatch(r"第[一二三四五六七八九十百千]+章", t):
        return True
    return False


def detect_toc_slot_shape_ids(
    toc_svg_path: Path,
    *,
    rows: int,
    cols: int,
    subtitle_offset: float | None = None,
) -> tuple[list[str], list[str]]:
    """Auto-detect title + subtitle shape-* ids for an N×M TOC grid.

    Walks the TOC slide's ``<g id="shape-*" data-pptx-frame="x y w h">``
    elements, clusters them by y-coordinate into rows and by
    x-coordinate into columns, and returns the title/subtitle shape ids
    in row-major fill order (r1c1, r1c2, r2c1, r2c2, ...).

    Caller declares the grid dimensions (``rows``, ``cols``); the
    function never assumes a specific template layout. Title shapes are
    identified by frame height ∈ [35, 60] (matches
    :func:`remove_empty_toc_slots`'s heuristic so detection agrees
    with the LLM-cleanup path). Subtitle per cell is the nearest shape
    (by y) within ``±50 px`` of ``title_y + subtitle_offset`` and inside
    a ``±50 px`` x-band of ``title_x``. If no subtitle is found the
    cell's subtitle id is the empty string.

    Parameters
    ----------
    toc_svg_path:
        Path to the TOC slide's SVG. Must already exist (post
        phase2_import) and contain ``<g id="shape-*" data-pptx-frame=…>``
        elements.
    rows, cols:
        Grid dimensions declared by the caller. ``rows * cols`` is the
        total slot count.
    subtitle_offset:
        Optional y-offset (px) below the title within each cell where
        the subtitle sits. Default ``30`` (matches the synthetic test
        fixture); boteng's ``~52 px`` works without override because
        the x-band + nearest-y pick resolves it.

    Returns
    -------
    ``(title_ids, subtitle_ids)`` — both lists have length
    ``rows * cols`` in row-major order. ``subtitle_ids[i]`` is the id
    of the subtitle shape inside the same cell as ``title_ids[i]``,
    or ``""`` when no subtitle was found.

    Raises
    ------
    ValueError
        * ``rows < 1`` or ``cols < 1``.
        * SVG has fewer than ``rows * cols`` title candidates.
        * A row anchor has fewer than ``cols`` titles.
    """
    if rows < 1 or cols < 1:
        raise ValueError(f"rows={rows}, cols={cols} must be >= 1")

    raw = toc_svg_path.read_text(encoding="utf-8")
    # Match any shape-* id (digits, hyphens, underscores). The
    # ``shape-`` prefix is the ppt-master convention; the suffix can be
    # anything (``69`` for boteng, ``title-0`` for synthetic fixtures).
    shape_re = re.compile(
        r'<g id="(shape-[^"]+)"[^>]*data-pptx-frame="([^"]+)"[^>]*>(.*?)</g>',
        re.DOTALL,
    )
    # gid -> (x_l, y_top, x_r, y_bot, h)
    parsed: dict[str, tuple[float, float, float, float, float]] = {}
    for m in shape_re.finditer(raw):
        gid = m.group(1)
        parts = m.group(2).split()
        if len(parts) < 4:
            continue
        try:
            x, y, w, h = (float(p) for p in parts[:4])
        except ValueError:
            continue
        body = m.group(3)
        if "<image" in body:
            continue
        if 'data-pptx-object="picture"' in m.group(0):
            continue
        parsed[gid] = (x, y, x + w, y + h, h)

    if not parsed:
        raise ValueError(
            f"no shape-* <g> with frame in {toc_svg_path}"
        )

    # Title candidates: frame height ∈ [35, 60] (matches
    # remove_empty_toc_slots heuristic so detection agrees with the
    # LLM-driven cleanup path).
    title_candidates: list[tuple[str, float, float, float]] = [
        (g, xl, yt, h) for g, (xl, yt, xr, yb, h) in parsed.items()
        if 35.0 <= h <= 60.0
    ]
    if len(title_candidates) < rows * cols:
        raise ValueError(
            f"grid {rows}x{cols} needs {rows * cols} title shapes "
            f"(height 35-60), found {len(title_candidates)} in "
            f"{toc_svg_path}. Adjust the grid spec or check the SVG."
        )

    # Cluster title candidates by y_top with ±5 px tolerance to
    # produce row anchors. Same dedup rule as remove_empty_toc_slots.
    title_candidates.sort(key=lambda t: t[2])
    row_anchors: list[float] = []
    for _g, _xl, y, _h in title_candidates:
        if row_anchors and abs(row_anchors[-1] - y) < 5.0:
            continue
        row_anchors.append(y)
        if len(row_anchors) >= rows:
            break
    if len(row_anchors) < rows:
        raise ValueError(
            f"expected {rows} distinct row anchors, found "
            f"{len(row_anchors)} in {toc_svg_path}"
        )

    # For each row anchor, take `cols` leftmost titles by x_left.
    title_ids: list[str] = []
    title_xs: list[float] = []
    title_ys: list[float] = []
    for anchor in row_anchors[:rows]:
        row_titles = sorted(
            [(g, xl, yt, h) for g, xl, yt, h in title_candidates
             if abs(yt - anchor) < 30.0],
            key=lambda t: t[1],
        )[:cols]
        if len(row_titles) < cols:
            raise ValueError(
                f"row at y={anchor} has only {len(row_titles)} title "
                f"shape(s); grid wants {cols}"
            )
        for gid, xl, yt, _h in row_titles:
            title_ids.append(gid)
            title_xs.append(xl)
            title_ys.append(yt)

    # Subtitle per cell: nearest shape (by y) within x-band ±50 px
    # of title_x, y close to title_y + subtitle_offset. Skip if the
    # nearest is already a title.
    subtitle_ids: list[str] = []
    target_offset = subtitle_offset if subtitle_offset is not None else 30.0
    title_set = set(title_ids)
    for tx, ty in zip(title_xs, title_ys):
        best: str = ""
        best_dist = float("inf")
        target_y = ty + target_offset
        for gid, (xl, yt, _xr, _yb, _h) in parsed.items():
            if gid in title_set:
                continue
            if abs(xl - tx) > 50.0:
                continue
            d = abs(yt - target_y)
            if d < best_dist:
                best_dist = d
                best = gid
        subtitle_ids.append(best)

    return title_ids, subtitle_ids


def remove_empty_toc_slots(
    authoring_dir: Path,
    state: PipelineState,
) -> None:
    """Remove unfilled TOC slot <g> elements from the TOC slide.

    The TOC layout in the boteng template is a 3×2 grid of slots; each
    slot has 3 stacked shapes (accent bar + title + subtitle) per
    column. When the LLM fills fewer slots than the template
    provides, the unfilled slots keep the template's default
    placeholder text ("点击添加大标题" etc.) and look like an
    unfinished page.

    Strategy: cluster all shape-* `<g>`s into horizontal *bands* by
    y-coordinate (the slot row spans y in [row_y, row_y + ~80]).
    A band is "empty" iff both its left-column title and its
    right-column title still contain a known placeholder phrase.
    We then strip the band's entire set of shapes (accent bar +
    title + subtitle for both columns + any decoration between).

    Background shapes (page background rect, decorative diamonds,
    separator lines, the page-level <g id="shape-61" container)
    are kept because they have either no ``data-pptx-frame`` or a
    frame outside the row band ranges. Our matcher only ever picks
    up slot shapes.
    """
    # Slide_02 is the boteng TOC; future templates may differ. Detect
    # by presence of "目录" / "CONTENTS" text in the SVG body.
    candidate_slides: list[Path] = []
    for path in authoring_dir.glob("slide_*.svg"):
        try:
            data = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "目录" in data or "CONTENTS" in data.upper():
            candidate_slides.append(path)
    if not candidate_slides:
        return

    for toc_svg in candidate_slides:
        original = toc_svg.read_text(encoding="utf-8")
        # Find every shape-* with a frame; record (gid, y_top, y_bottom,
        # x_left, x_right, body).
        shape_re = re.compile(
            r'<g id="(shape-\d+)"[^>]*data-pptx-frame="([^"]+)"[^>]*>(.*?)</g>',
            re.DOTALL,
        )
        items: list[tuple[str, float, float, float, float, str]] = []
        for m in shape_re.finditer(original):
            gid = m.group(1)
            parts = m.group(2).split()
            if len(parts) < 4:
                continue
            try:
                x, y, w, h = (float(p) for p in parts[:4])
            except ValueError:
                continue
            # Safety: never remove a shape whose body is a <image>
            # (page background) or data-pptx-object="picture". These
            # are decoration, not editable TOC text slots.
            body = m.group(3)
            if "<image" in body or 'data-pptx-object="picture"' in m.group(0):
                continue
            items.append((gid, y, y + h, x, x + w, body))
        if not items:
            continue

        # Extract first text inside each shape's body for the title-text
        # check below.
        def _text(body: str) -> str:
            return "".join(
                (t[0] or t[1] or "")
                for t in re.findall(
                    r'<text[^>]*>([^<]*)</text>|<tspan[^>]*>([^<]*)</tspan>',
                    body,
                )
            ).strip()

        # Identify TOC rows by their TITLE shapes: a title shape has
        # a tall frame (height ~40-50 px, the title text size) and is
        # usually the largest in its y-band. We pick the y_top of
        # each title shape, then group shapes whose y_top is within
        # 90 px of that title (covers accent above + subtitle below).
        title_y_values: list[float] = []
        for gid, y_top, y_bot, x_l, x_r, body in items:
            h = y_bot - y_top
            txt = _text(body)
            if 35 <= h <= 60 and txt and not is_toc_slot_placeholder(txt):
                # likely a title
                title_y_values.append(y_top)
        # Deduplicate close y values (left + right col title of same row).
        title_y_values.sort()
        row_anchors: list[float] = []
        for y in title_y_values:
            if row_anchors and abs(row_anchors[-1] - y) < 5.0:
                continue
            row_anchors.append(y)

        if not row_anchors:
            continue

        # A row's full y-extent is derived from adjacent anchor positions.
        # Bug 07 fix: was hardcoded ``anchor - 30, anchor + 90`` (matched
        # boteng's ~90px row height). For templates with compact rows
        # (~30px), the 90px band swallowed the next row's decoration;
        # for templates with loose rows (~140px), the band missed the
        # current row's subtitle. Compute row extent from the gap to the
        # next (or previous) anchor when available.
        empty_rows: list[tuple[float, float]] = []  # (y_top, y_bot)
        for i, anchor in enumerate(row_anchors):
            prev_anchor = row_anchors[i - 1] if i > 0 else None
            next_anchor = (
                row_anchors[i + 1] if i + 1 < len(row_anchors) else None
            )
            if prev_anchor is not None and next_anchor is not None:
                # Middle row: split the gap evenly, cap at 90.
                row_h = min(90.0, (next_anchor - prev_anchor) / 2)
            elif prev_anchor is not None:
                row_h = min(90.0, (anchor - prev_anchor))
            elif next_anchor is not None:
                row_h = min(90.0, (next_anchor - anchor))
            else:
                row_h = 90.0  # single-row TOC fallback
            row_top = anchor - 20
            row_bot = anchor + row_h
            row_shapes = [
                it for it in items
                if row_top <= it[1] <= row_bot
            ]
            # Collect the texts of the row's TITLE shapes (heights
            # 35-60, with text). If ANY title has real (non-placeholder)
            # text, the row is filled. If all titles are placeholder
            # or there are no titles in the row, the row is empty.
            title_texts: list[str] = []
            for _gid, y_top, y_bot, _x, _r, body in row_shapes:
                h = y_bot - y_top
                txt = _text(body)
                if 35 <= h <= 60 and txt:
                    title_texts.append(txt)
            if not title_texts:
                # No title in this y-band — this is decoration
                # (separators, page background), skip.
                continue
            if all(is_toc_slot_placeholder(t) for t in title_texts):
                empty_rows.append((row_top, row_bot))
        if not empty_rows:
            continue

        # Collect every shape whose y_top is in any empty row's range.
        gids_to_remove: set[str] = set()
        for row_top, row_bot in empty_rows:
            for gid, y_top, _, _, _, _ in items:
                if row_top <= y_top <= row_bot:
                    gids_to_remove.add(gid)

        patched = original
        for gid in sorted(gids_to_remove):
            # Find the outer <g id="gid" ...> and balance nested
            # </g> so we do not stop at a child's closer.
            m = re.search(rf'<g id="{re.escape(gid)}"[^>]*>', patched)
            if not m:
                continue
            depth = 0
            i = m.start()
            end = None
            for j in range(i, len(patched)):
                if patched.startswith("<g", j):
                    depth += 1
                elif patched.startswith("</g>", j):
                    depth -= 1
                    if depth == 0:
                        end = j + 4
                        break
            if end is None:
                continue
            patched = patched[:i] + patched[end:]

        if patched != original:
            toc_svg.write_text(patched, encoding="utf-8")
            n_rows = len(empty_rows)
            n_shapes_removed = len(gids_to_remove)
            log.info(
                "phase3: TOC cleanup — removed %d empty row(s) / %d shape(s) "
                "from %s",
                n_rows, n_shapes_removed, toc_svg.name,
            )
            state.warnings.append(
                f"toc cleanup: removed {n_shapes_removed} unfilled shape(s) "
                f"from {toc_svg.name}"
            )


# ---------------------------------------------------------------------------
# Markdown parsing helpers (used by _fill_missing_content_blocks below and
# by workspace_expand for the default fallback cards).
# ---------------------------------------------------------------------------


def strip_inline_markdown(text: str) -> str:
    """Strip common markdown inline markers from a heading line.

    Removes ``**bold**`` / ``__bold__`` / ``*italic*`` / ``_italic_``
    / `` `code` `` so the title injected into PPT shapes doesn't show
    the literal asterisks. Generic — no template-specific assumptions.
    """
    # Order matters: strip bold (** **) before italic (* *) to avoid
    # eating an outer ** by mistake.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


# Phase 2 (2026-09-16): loose meta extraction. Matches blockquote lines
# like ``> **layout**: hero-number`` or ``> **要点**：采购效率、岗位职责``.
# Accepts BOTH full-width (：) and half-width (:) colons; tolerates spaces
# around the colon and at the end of the value. Captures any field name
# (no whitelist) so the markdown author has freedom. Callers in
# ``workspace_expand`` only consult a few known keys (``layout``,
# ``items``, ``caption``, etc.) — unknown keys are kept in ``meta`` for
# forward compatibility but ignored at render time.
_META_RE = re.compile(
    r"^>\s*\*\*([^*]+?)\*\*\s*[：:]\s*(.+?)\s*$",
    re.MULTILINE,
)


def _extract_section_meta(body: str) -> tuple[dict[str, str], str]:
    """Pull ``> **key**: value`` lines out of ``body``.

    Returns ``(meta, body_without_meta_lines)``. Order in ``meta`` follows
    first appearance in ``body`` (dict preserves insertion order in
    Python 3.7+). Multi-value keys are NOT supported — each meta line
    yields one entry, last one wins on key collision.
    """
    meta: dict[str, str] = {}
    for m in _META_RE.finditer(body):
        key = m.group(1).strip()
        value = m.group(2).strip()
        # Strip leading "**" wrappers inside value too (defensive).
        value = value.replace("**", "").strip()
        if key:
            meta[key] = value
    # Strip the meta lines so downstream ``cards_from_body`` doesn't
    # re-process them as ordinary blockquote text.
    cleaned = _META_RE.sub("", body)
    # Collapse any blank lines introduced by the strip so split("\n\n")
    # in cards_from_body still works as before.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return meta, cleaned


def split_markdown_sections(md_text: str) -> list[dict[str, Any]]:
    """Split a Chinese procurement policy markdown into
    ``[{title, body, meta}]`` sections keyed on `# 一、` / `# 二、` etc.

    H1 inline markdown (``**bold**`` / ``*italic*`` / `` `code` ``)
    is stripped — otherwise those markers leak into the PPT shape
    text and render as literal asterisks.

    Phase 2 (2026-09-16): also extracts ``> **key**: value`` meta lines
    into the ``meta`` dict. See :func:`_extract_section_meta` for the
    syntax. Sections without any meta lines get ``meta={}``.
    """
    sections: list[dict[str, Any]] = []
    head_re = re.compile(r"^#\s+(.+)$", re.MULTILINE)
    matches = list(head_re.finditer(md_text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        body = md_text[start:end].strip()
        meta, body = _extract_section_meta(body)
        title = strip_inline_markdown(m.group(1).strip())
        sections.append({"title": title, "body": body, "meta": meta})
    return sections


def cards_for_section(
    sections: list[dict[str, Any]],
    stem: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull 2-3 cards from the section matching ``stem`` (e.g. ``part02``).

    Returns ``(cards, meta)`` so callers can decide whether to render the
    heuristic cards (``A path``) or honor the caller's structured markdown
    (``E path`` — see ``workspace_expand.py`` for the dispatch logic).
    ``meta`` is the section's extracted meta dict (``{}`` when none).

    Mapping heuristic:
      - stem ``partNN`` → H1 with leading CN numeral ``一/二/三/...`` whose
        ordinal matches ``NN`` (so part02 → 二、目的) or just picks the
        ``NN``-th section if there are that many.
    cn_numerals = "一二三四五六七八九十"
    """
    if not sections:
        return [], {}
    m = re.match(r"part(\d+)$", stem)
    section = None
    if m:
        idx = int(m.group(1)) - 1  # part02 → section[1]
        if 0 <= idx < len(sections):
            section = sections[idx]
    if section is None:
        section = sections[0]
    meta = section.get("meta") or {}
    cards = cards_from_body(section["body"]) or [
        {"title": section["title"][:10], "color": "#1D2CAB",
         "items": [section["body"][:60] + ("…" if len(section["body"]) > 60 else "")]}
    ]
    return cards, meta


def cards_from_body(body: str) -> list[dict[str, Any]]:
    """Convert a markdown body into 2-3 cards: one ``要点`` card from
    the first paragraph, then split any ``1. xxx / 2. yyy`` numbered
    list into additional cards. Truncate each item to a sane length.

    Phase 1.1 (2026-09-16): widened the first-card truncation from 60 to
    200 characters so single-paragraph chapters (e.g. ``前言`` ~140 chars)
    keep their full context in the rendered SVG. The downstream
    ``3-column-cards`` layout was already truncating again to 40 chars per
    item inside ``workspace_expand.py`` (see ``cards_for_section`` loop
    there), so widening this limit does not break visual balance.
    """
    if not body:
        return []
    cards: list[dict[str, Any]] = []
    colors = ["#1D2CAB", "#EE822F", "#75BD42"]

    # First card: first paragraph (≥ 1 line, ≤ 200 chars after Phase 1.1).
    # Phase 1.2/1.3 (2026-09-16): paragraphs whose first line is an ``##``
    # heading are H2 sub-sections, owned by the sub-cards block below.
    # They are excluded from the "first card" / "list items" extraction so
    # their content isn't double-counted.
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    h2_re = re.compile(r"^##\s+(.+)$")
    h2_paragraph_idxs: set[int] = set()
    for i, p in enumerate(paragraphs):
        first_line = p.splitlines()[0].strip() if p.splitlines() else ""
        if h2_re.match(first_line):
            h2_paragraph_idxs.add(i)
    non_h2_paragraphs = [
        p for i, p in enumerate(paragraphs) if i not in h2_paragraph_idxs
    ]
    if non_h2_paragraphs:
        first = non_h2_paragraphs[0]
        # Strip leading "# " / leading numbered list prefix.
        first = re.sub(r"^#\s+", "", first)
        first = re.sub(r"^[\d一二三四五六七八九十]+[、.]\s*", "", first)
        cards.append({
            "title": "要点",
            "color": colors[0],
            "items": [first[:200] + ("…" if len(first) > 200 else "")],
        })

    # Phase 1.2 (2026-09-16): H2 sub-section identification. Slides whose
    # body contains ``## （一）xxx`` style subsections get one extra card
    # per H2, with the H2 title as the card title and the numbered list
    # items below it as the card items. This fills the body for slides
    # that have NO top-level numbered list but DO have H2 subsections.
    list_re = re.compile(r"^([\d]+)[、.]\s*(.+)$")
    sub_cards: list[dict[str, Any]] = []
    for i in h2_paragraph_idxs:
        p = paragraphs[i]
        lines = p.splitlines()
        if not lines:
            continue
        h2m = h2_re.match(lines[0].strip())
        if not h2m:
            continue
        sub_title = h2m.group(1).strip()
        # Strip bold markers (``**xxx**``) so the card title doesn't show asterisks.
        sub_title = sub_title.replace("**", "").strip()
        items: list[str] = []
        for line in lines[1:]:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lm = list_re.match(line)
            if lm:
                txt = lm.group(2).strip()
                items.append(txt[:30] + ("…" if len(txt) > 30 else ""))
        if items:
            # Color cycles with current card count so duplicates don't share color.
            color = colors[(len(cards) + len(sub_cards)) % 3]
            sub_cards.append({
                "title": sub_title[:12],   # Phase 1.2 widened from earlier truncation
                "color": color,
                "items": items[:4],
            })

    # Append at most 2 H2 sub-cards (renderer supports 1-4 cards per row).
    cards.extend(sub_cards[:2])

    # Additional cards: numbered list ``1. xxx`` items.
    # Phase 1.3 (2026-09-16): also scan paragraphs[0] (non-H2 path) so numbered
    # items embedded in the first paragraph are captured. H2 paragraphs
    # are already excluded (Phase 1.2 owns their items).
    list_items: list[str] = []
    for p in non_h2_paragraphs:
        for line in p.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            lm = list_re.match(line)
            if lm:
                list_items.append(lm.group(2).strip())
    if list_items:
        # Group into 2 cards (cap at 4 items per card).
        half = max(1, (len(list_items) + 1) // 2)
        cards.append({
            "title": "子项",
            "color": colors[1],
            "items": [it[:30] + ("…" if len(it) > 30 else "")
                      for it in list_items[:half]],
        })
        if len(list_items) > half:
            cards.append({
                "title": "补充",
                "color": colors[2],
                "items": [it[:30] + ("…" if len(it) > 30 else "")
                          for it in list_items[half:half + 4]],
            })

    # Trim to 4 cards max (Phase 1.2: renderer ``3-column-cards`` supports
    # 1-4 cards; lifted cap from 3 so H2 sub-cards + numbered list cards
    # can coexist on the same slide, e.g. ``四、工作程序``).
    return cards[:4]


def fill_missing_content_blocks(
    *,
    cloned_svgs: list[str],
    final_new_blocks: dict[str, dict[str, dict[str, Any]]],
    state: PipelineState,
) -> None:
    """Synthesize a default 3-column-cards new_block per cloned
    ``*_content.svg`` that doesn't yet have one.

    The cloned content skeleton (slide_04) only ships a title bar
    (shape-17) and a corner tagline (shape-22). The body rectangle
    (shape-3, 1124×530) is empty by design — the LLM is supposed to
    populate it via ``new_blocks``. When the LLM forgets, this
    fallback pulls paragraphs from the source markdown (if loaded) or
    just emits a placeholder card so the page is not blank.

    Bounds match the body rectangle of slide_04: ``x=120, y=130,
    w=1060, h=480`` — leaves a comfortable margin inside the body.
    """
    if not cloned_svgs:
        return

    md_text: str = ""
    md_path = state.context.get("content_markdown")
    if isinstance(md_path, Path) and md_path.is_file():
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except OSError:
            md_text = ""

    # Parse out the markdown into H1 / paragraph sections so the
    # fallback can pick the section whose title matches the cloned
    # content SVG's stem (slide_part02_content.svg → part02 → "PART 02").
    sections = split_markdown_sections(md_text) if md_text else []

    bounds = "120 130 1060 480"
    fallback_count = 0
    for svg_name in cloned_svgs:
        existing = final_new_blocks.get(svg_name) or {}
        if existing:
            continue
        stem = svg_name.replace("slide_", "").replace(".svg", "")
        # Try to find a matching section by stem number (e.g. part02 → section 2)
        cards, meta = cards_for_section(sections, stem)
        if not cards:
            cards = [{"title": "本节要点", "color": "#1D2CAB",
                      "items": ["(待补充)"]}]
            fallback_count += 1
        final_new_blocks.setdefault(svg_name, {})["content-body"] = {
            "bounds": bounds,
            "layout": "3-column-cards",
            "spec": {"cards": cards},
        }
        log.info(
            "phase2c: synthesized default 3-column-cards for %s (%d cards)",
            svg_name, len(cards),
        )
    if fallback_count:
        log.warning(
            "phase2c: %d cloned content slide(s) had no matching markdown "
            "section; emitted placeholder cards",
            fallback_count,
        )
