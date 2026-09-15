"""pipeline.py — orchestrate the native fill state machine.

State machine (plan §5):

    INIT → IMPORTED → PLANNED → AUTHORED → QUALITY_PASSED → EXPORTED → VALIDATED → DONE
                                                                       ↘ FAILED

Phases:
  - Phase 2 (IMPORTED)        — pptx_to_svg.py --roundtrip
  - Phase 3 (PLANNED→AUTHORED) — write page_plan.json + apply content_mapping + new_blocks
  - Phase 4 (QUALITY_PASSED)  — svg_authoring_view refresh + svg_quality_checker --roundtrip
  - Phase 5 (EXPORTED→DONE)   — svg_to_pptx + pptx_delivery_check + source_to_md

Auto-fix loop runs between QUALITY and EXPORT: if Phase 4 returns exit 1 we
re-try up to ``max_fix_iterations`` times (default 3) before giving up.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import autofix, io_utils, runner, svg_edits

log = logging.getLogger("mcp_ppt_native_fill.pipeline")

PAGE_PLAN_SCHEMA = "ppt-master.roundtrip-page-plan.v1"


@dataclass
class PipelineState:
    stage: str = "init"
    workspace: Path | None = None
    output_pptx: Path | None = None
    skill_dir: Path | None = None
    fix_iterations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_export_receipt: dict | None = None
    last_delivery: dict | None = None
    last_quality_stdout: str = ""
    # Free-form scratchpad for phases to pass values without changing
    # the dataclass schema every release. ``phase2_5_llm_plan`` writes
    # ``content_mapping`` here for ``phase3_author`` to read.
    context: dict[str, Any] = field(default_factory=dict)

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)


# ---------------------------------------------------------------------------
# page_plan.json helpers.
# ---------------------------------------------------------------------------

def write_page_plan(workspace: Path, pages: list[dict]) -> Path:
    """Persist ``page_plan.json`` per master §4 schema.

    ``pages`` is a list of ``{"source_slide": int, "svg": str}``. Each svg
    filename must be unique and live under ``authoring-svg-flat/``.
    """
    if not pages:
        raise ValueError("page_plan.pages must not be empty")
    seen_svg: set[str] = set()
    for entry in pages:
        svg = entry.get("svg") or f"slide_{int(entry['source_slide']):02d}.svg"
        if svg in seen_svg:
            raise ValueError(f"duplicate svg filename in page_plan: {svg}")
        seen_svg.add(svg)
        if not (workspace / "authoring-svg-flat" / svg).is_file():
            raise ValueError(
                f"page_plan references missing svg: "
                f"authoring-svg-flat/{svg}"
            )
        source_slide = int(entry["source_slide"])
        if source_slide < 1:
            raise ValueError(
                f"page_plan.source_slide must be >= 1, got {source_slide}"
            )

    payload = {"schema": PAGE_PLAN_SCHEMA, "pages": pages}
    path = workspace / "page_plan.json"
    io_utils.write_json_atomic(path, payload)
    return path


# ---------------------------------------------------------------------------
# Per-phase drivers.
# ---------------------------------------------------------------------------

def phase2_import(
    state: PipelineState,
    source_pptx: Path,
    workspace: Path,
    *,
    inheritance_mode: str = "both",
    timeout_ms: int = 120_000,
) -> PipelineState:
    """Phase 2: PPTX → authoring-svg-flat workspace."""
    state.stage = "import"
    state.workspace = workspace
    state.output_pptx  # type: ignore[misc]

    res = runner.run_pptx_to_svg(
        state.skill_dir,  # type: ignore[arg-type]
        source_pptx=source_pptx,
        workspace=workspace,
        inheritance_mode=inheritance_mode,
        roundtrip=True,
        timeout_ms=timeout_ms,
    )
    if not res.ok:
        state.stage = "failed"
        state.errors.append(
            f"phase2 pptx_to_svg failed exit={res.exit} "
            f"stderr_tail={res.stderr[-400:]}"
        )
        return state

    state.stage = "imported"
    state.warnings.extend(res.warnings)
    return state


def phase2_5_llm_plan(
    state: PipelineState,
    content_markdown: Path,
    caller_mapping: dict[str, dict[str, str]],
) -> PipelineState:
    """Optional LLM-driven content planning phase.

    Runs after phase2 (workspace exists) and before phase3 (edits). Asks
    the configured LLM to derive a PlannerResult from the markdown + SVG
    shape_index + skeleton_index. Caller-supplied entries WIN on conflict
    in the content_mapping merge. The full PlannerResult (with
    page_plan_additions / new_blocks / skeleton_kind) is stashed in
    ``state.context["planner_result"]`` for phase2_6 to materialize.
    """
    state.stage = "llm_plan"
    workspace = state.workspace
    assert workspace is not None

    from . import llm_planner  # late import to avoid pulling HTTP deps

    try:
        planner_result = llm_planner.plan_content_mapping(
            md_path=content_markdown,
            workspace=workspace,
        )
    except llm_planner.PlannerError as exc:
        state.stage = "failed"
        state.errors.append(f"llm_plan: {exc}")
        return state
    except Exception as exc:  # network / LLMError etc.
        state.stage = "failed"
        state.errors.append(f"llm_plan: {type(exc).__name__}: {exc}")
        return state

    # Legacy path: PlannerResult's dict-like shim makes the merge logic
    # work without caring about the new fields.
    llm_mapping = planner_result.content_mapping
    if not llm_mapping:
        state.warnings.append(
            "llm_plan: model returned an empty mapping; "
            "falling back to caller-supplied content_mapping"
        )
        merged = dict(caller_mapping)
    else:
        merged: dict[str, dict[str, str]] = {}
        for svg in set(caller_mapping) | set(llm_mapping):
            cm = caller_mapping.get(svg) or {}
            lm = llm_mapping.get(svg) or {}
            merged[svg] = {**lm, **cm}  # caller wins on key collision

        log.info(
            "llm_plan: caller slides=%d, llm slides=%d, merged slides=%d, "
            "page_plan_additions=%d, new_blocks=%d",
            len(caller_mapping),
            len(llm_mapping),
            len(merged),
            len(planner_result.page_plan_additions),
            len(planner_result.new_blocks),
        )
        state.warnings.append(
            f"llm_plan: derived {sum(len(v) for v in llm_mapping.values())} "
            f"shape edit(s) + {len(planner_result.page_plan_additions)} "
            f"page addition(s) + {len(planner_result.new_blocks)} "
            f"new block(s) from {content_markdown.name}"
        )

    state.context["content_mapping"] = merged
    state.context["planner_result"] = planner_result
    state.stage = "imported"
    return state


# Default TOC placeholder phrases that the LLM did NOT replace. When a
# TOC slot's text still equals one of these (after whitespace
# normalisation), the slot is "unfilled" and its <g> elements (accent
# bar + title + subtitle) are removed so the rendered slide does not
# show template default text in empty rows.
#
# Bug 06 fix: entries are stored in whitespace-normalised form (no
# spaces) to match ``"".join(text.split())`` in _is_toc_slot_placeholder.
# The previous set had "click to add title" (with spaces) which never
# matched the normalised input.
_TOC_PLACEHOLDER_PHRASES: frozenset[str] = frozenset({
    "单击添加大标题", "点击添加大标题", "点击添加标题",
    "clicktoaddtitle", "clickheretoaddtitle",
    "单击添加",
    "",  # genuinely empty
})


def _find_toc_svg(authoring_dir: Path) -> str:
    """Return the filename of the slide that contains the TOC marker.

    Looks for any ``slide_*.svg`` in ``authoring_dir`` whose body
    contains the Chinese "目录" or English "CONTENTS" string. Used as
    the default value for ``toc_svg`` in
    :func:`expand_workspace_from_toc` when the caller doesn't supply
    one explicitly.

    Mirrors the same scan logic used by :func:`_remove_empty_toc_slots`
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


def _filter_toc_manual_mapping(
    content_mapping: dict[str, dict[str, str]],
    toc_shape_ids: list[str],
) -> tuple[dict[str, dict[str, str]], int]:
    """Drop manual mapping entries that target TOC slot shape ids.

    When ``expand_toc_from_markdown`` is on, the auto-fill from
    :func:`expand_workspace_from_toc` would overwrite any manual TOC
    entries anyway — but dropping them up front makes the intent
    explicit and surfaces the conflict in audit logs instead of
    silently overwriting.

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


def _is_toc_slot_placeholder(text: str) -> bool:
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
    if t in _TOC_PLACEHOLDER_PHRASES:
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


def _detect_toc_slot_shape_ids(
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
    :func:`_remove_empty_toc_slots`'s heuristic so detection agrees
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
    # _remove_empty_toc_slots heuristic so detection agrees with the
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
    # produce row anchors. Same dedup rule as _remove_empty_toc_slots.
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


def _remove_empty_toc_slots(
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
            if 35 <= h <= 60 and txt and not _is_toc_slot_placeholder(txt):
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
            if all(_is_toc_slot_placeholder(t) for t in title_texts):
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


def _fill_missing_content_blocks(
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
    sections = _split_markdown_sections(md_text) if md_text else []

    bounds = "120 130 1060 480"
    fallback_count = 0
    for svg_name in cloned_svgs:
        existing = final_new_blocks.get(svg_name) or {}
        if existing:
            continue
        stem = svg_name.replace("slide_", "").replace(".svg", "")
        # Try to find a matching section by stem number (e.g. part02 → section 2)
        cards = _cards_for_section(sections, stem)
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
            "phase2.6: synthesized default 3-column-cards for %s (%d cards)",
            svg_name, len(cards),
        )
    if fallback_count:
        log.warning(
            "phase2.6: %d cloned content slide(s) had no matching markdown "
            "section; emitted placeholder cards",
            fallback_count,
        )


def _strip_inline_markdown(text: str) -> str:
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


def _split_markdown_sections(md_text: str) -> list[dict[str, str]]:
    """Split a Chinese procurement policy markdown into
    ``[{title, body}]`` sections keyed on `# 一、` / `# 二、` etc.

    H1 inline markdown (``**bold**`` / ``*italic*`` / `` `code` ``)
    is stripped — otherwise those markers leak into the PPT shape
    text and render as literal asterisks.
    """
    sections: list[dict[str, str]] = []
    head_re = re.compile(r"^#\s+(.+)$", re.MULTILINE)
    matches = list(head_re.finditer(md_text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        body = md_text[start:end].strip()
        title = _strip_inline_markdown(m.group(1).strip())
        sections.append({"title": title, "body": body})
    return sections


def _cards_for_section(
    sections: list[dict[str, str]],
    stem: str,
) -> list[dict[str, Any]]:
    """Pull 2-3 cards from the section matching ``stem`` (e.g. ``part02``).

    Mapping heuristic:
      - stem ``partNN`` → H1 with leading CN numeral ``一/二/三/...`` whose
        ordinal matches ``NN`` (so part02 → 二、目的) or just picks the
        ``NN``-th section if there are that many.
    cn_numerals = "一二三四五六七八九十"
    """
    if not sections:
        return []
    m = re.match(r"part(\d+)$", stem)
    if not m:
        return _cards_from_body(sections[0]["body"]) if sections else []
    idx = int(m.group(1)) - 1  # part02 → section[1]
    if idx < 0 or idx >= len(sections):
        return []
    section = sections[idx]
    return _cards_from_body(section["body"]) or [
        {"title": section["title"][:10], "color": "#1D2CAB",
         "items": [section["body"][:60] + ("…" if len(section["body"]) > 60 else "")]}
    ]


def _cards_from_body(body: str) -> list[dict[str, Any]]:
    """Convert a markdown body into 2-3 cards: one ``要点`` card from
    the first paragraph, then split any ``1. xxx / 2. yyy`` numbered
    list into additional cards. Truncate each item to a sane length."""
    if not body:
        return []
    cards: list[dict[str, Any]] = []
    colors = ["#1D2CAB", "#EE822F", "#75BD42"]

    # First card: first paragraph (≥ 1 line, ≤ 60 chars).
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    if paragraphs:
        first = paragraphs[0]
        # Strip leading "# " / leading numbered list prefix.
        first = re.sub(r"^#\s+", "", first)
        first = re.sub(r"^[\d一二三四五六七八九十]+[、.]\s*", "", first)
        cards.append({
            "title": "要点",
            "color": colors[0],
            "items": [first[:60] + ("…" if len(first) > 60 else "")],
        })

    # Additional cards: numbered list ``1. xxx`` items.
    list_re = re.compile(r"^([\d]+)[、.]\s*(.+)$")
    list_items: list[str] = []
    for p in paragraphs[1:]:
        for line in p.splitlines():
            line = line.strip()
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

    # Trim to 3 cards max (renderer supports 1-4, but 3 keeps visual balance).
    return cards[:3]


def _seed_original_roster(
    authoring_dir: Path,
    *,
    exclude: frozenset[str] = frozenset(),
    skeleton_kind: dict[str, str] | None = None,
    ending_last: bool = False,
) -> list[dict[str, Any]]:
    """Build a page_plan from the original ``slide_NN.svg`` skeletons.

    Used by ``phase2_6_realize_planner_output`` when no caller-supplied
    ``page_plan`` was given AND/OR the planner only emitted
    ``page_plan_additions``. Returns one entry per
    ``slide_NN.svg`` ordered by ``NN`` (cover first), skipping any
    names in ``exclude`` (the LLM's cloned PART_* files).

    The pipeline must register the original cover/toc/divider/ending
    pages — otherwise they vanish from the export and the user sees
    "no cover, blank content".

    ``ending_last=True`` (Phase B, Bug 1 fix): when set, identify the
    ``ending`` skeleton and append it AFTER all other originals instead
    of letting ``source_slide`` ordering place it in the middle.
    ``skeleton_kind`` is consulted first (mapping ``"slide_NN.svg" →
    "ending"|...``); if not provided or no ending entry is found, fall
    back to the slide with the highest source_slide (the boteng
    template's slide_05.svg is always the closing slide).
    """
    roster: list[dict[str, Any]] = []
    if not authoring_dir.is_dir():
        return roster
    pattern = re.compile(r"^slide_(\d+)\.svg$")
    candidates: list[tuple[int, str]] = []
    for path in authoring_dir.iterdir():
        if not path.is_file():
            continue
        m = pattern.match(path.name)
        if not m:
            continue
        if path.name in exclude:
            continue
        candidates.append((int(m.group(1)), path.name))
    candidates.sort(key=lambda item: item[0])

    # Identify the ending slide (pop it out so we can re-append at the
    # end). Three strategies, in priority order:
    #   1. skeleton_kind[name] == "ending" — planner already labelled it
    #   2. body text contains THANK YOU / 谢谢 / Q&A — semantic signal
    #   3. filename matches thank|ending|closing — defensive filename match
    #   4. last by source_slide — last-resort fallback
    ending_idx: int | None = None
    if ending_last and candidates:
        for idx, (_, name) in enumerate(candidates):
            if skeleton_kind and skeleton_kind.get(name) == "ending":
                ending_idx = idx
                break
        if ending_idx is None:
            # Bug 08 fix: scan SVG bodies for ending markers (THANK YOU /
            # 谢谢 / Q&A). Templates that append an appendix slide at a
            # higher source_slide than the actual ending would otherwise
            # mis-pick the appendix as ending.
            ending_keywords = ("THANK", "谢谢", "Q&A", "答疑", "再见")
            for idx, (_, name) in enumerate(candidates):
                try:
                    body = (authoring_dir / name).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                if any(kw in body for kw in ending_keywords):
                    ending_idx = idx
                    break
            if ending_idx is None:
                # Defensive filename match before resorting to position.
                for idx, (_, name) in enumerate(candidates):
                    if re.search(r"(thank|ending|closing|back_cover)",
                                 name, re.I):
                        ending_idx = idx
                        break
            if ending_idx is None:
                ending_idx = len(candidates) - 1  # fallback: highest NN

    ending_entry: dict[str, Any] | None = None
    if ending_idx is not None:
        source_slide, name = candidates.pop(ending_idx)
        ending_entry = {"source_slide": source_slide, "svg": name}

    for source_slide, name in candidates:
        roster.append({"source_slide": source_slide, "svg": name})

    if ending_entry is not None:
        roster.append(ending_entry)
    return roster


def phase2_6_realize_planner_output(
    state: PipelineState,
    *,
    caller_page_plan: list[dict] | None = None,
    caller_new_blocks: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> PipelineState:
    """Phase 2.6: materialize LLM planner output (Phase A expansion).

    For each ``page_plan_addition`` the planner returned:
      1. Copy the skeleton SVG (per ``source_slide``) to the new filename.
      2. Apply text edits to the copy via ``svg_edits.apply_text_edits``.
      3. Register the copy in the final ``page_plan_pages`` list with
         ``source_slide`` pointing at the skeleton.

    For each ``new_block`` the planner returned:
      1. Append to the working ``new_content_blocks`` dict (caller keys
         win on collision).

    Errors are non-fatal — collected as warnings so the pipeline can
    still complete (export will surface the missing page later if any).
    """
    state.stage = "plan_realize"
    workspace = state.workspace
    assert workspace is not None
    authoring_dir = workspace / "authoring-svg-flat"
    planner_result = state.context.get("planner_result")
    if planner_result is None:
        # No planner ran (caller-only path) — nothing to realize.
        # If the caller did not supply a page_plan, still seed with the
        # original workspace roster (cover/toc/divider/content/ending)
        # so the export at least mirrors the source PPTX.
        if caller_page_plan:
            seeded: list[dict[str, Any]] = list(caller_page_plan)
        else:
            seeded = _seed_original_roster(authoring_dir)
        state.context.setdefault("page_plan_pages", seeded)
        state.context.setdefault(
            "new_content_blocks",
            {k: dict(v) for k, v in (caller_new_blocks or {}).items()},
        )
        state.stage = "imported"
        return state

    # Resolve which source slide each skeleton comes from. We trust the
    # planner's source_slide field, but fall back to the skeleton's own
    # filename ordering (slide_NN.svg → N).
    import shutil

    final_pages: list[dict[str, Any]] = list(caller_page_plan or [])

    # Bug #1 fix: when the caller did not supply a page_plan, the planner's
    # ``page_plan_additions`` is the *only* source of pages. That drops the
    # original cover/toc/divider/ending slides from the export (the user
    # reports "no cover page"). Re-seed ``final_pages`` with every
    # ``slide_NN.svg`` already present in the workspace that is not a
    # newly-cloned PART_* file, preserving the original ordering by
    # ``source_slide``.
    if not caller_page_plan:
        seeded = _seed_original_roster(
            authoring_dir,
            exclude=frozenset(
                e.get("svg", "") for e in planner_result.page_plan_additions
            ),
            skeleton_kind=planner_result.skeleton_kind,
            ending_last=True,
        )
        final_pages = list(seeded)
        log.info(
            "phase2.6: seeded %d original slide(s) into page_plan "
            "(cover/toc/divider/ending; ending moved to last)",
            len(seeded),
        )

    seen_svgs: set[str] = {p.get("svg", "") for p in final_pages if p.get("svg")}
    new_clones: list[Path] = []

    for entry in planner_result.page_plan_additions:
        new_svg_name = entry.get("svg")
        source_slide = int(entry.get("source_slide", 0))
        edits = entry.get("edits") or {}
        if not new_svg_name or source_slide < 1:
            state.warnings.append(
                f"phase2.6: skipping malformed page_plan_addition: {entry!r}"
            )
            continue
        if new_svg_name in seen_svgs:
            state.warnings.append(
                f"phase2.6: page_plan_additions references already-used "
                f"svg {new_svg_name!r}; skipping"
            )
            continue
        skeleton_path = authoring_dir / f"slide_{source_slide:02d}.svg"
        new_path = authoring_dir / new_svg_name
        if not skeleton_path.is_file():
            state.warnings.append(
                f"phase2.6: skeleton slide_{source_slide:02d}.svg missing "
                f"for {new_svg_name!r}; skipping"
            )
            continue
        try:
            shutil.copy2(skeleton_path, new_path)
        except OSError as exc:
            state.warnings.append(
                f"phase2.6: copy {skeleton_path.name} → {new_svg_name} "
                f"failed: {exc}"
            )
            continue
        if edits:
            try:
                audit = svg_edits.apply_text_edits(new_path, edits)
                applied = sum(
                    1 for a in audit if a.get("status") == "applied"
                )
                log.info(
                    "phase2.6: cloned %s from slide_%02d.svg, "
                    "applied %d/%d edit(s)",
                    new_svg_name, source_slide, applied, len(edits),
                )
            except (ValueError, FileNotFoundError) as exc:
                state.warnings.append(
                    f"phase2.6: edits on {new_svg_name} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        new_clones.append(new_path)
        final_pages.append({
            "source_slide": source_slide,
            "svg": new_svg_name,
        })
        seen_svgs.add(new_svg_name)
        # Also fold the new page's edits into the merged content_mapping
        # so phase3 sees them.
        merged_cm = state.context.setdefault("content_mapping", {})
        merged_cm[new_svg_name] = edits

    # new_blocks: planner's structural blocks override caller-supplied
    # blocks on key collision (svg filename).
    final_new_blocks: dict[str, dict[str, dict[str, Any]]] = {
        k: dict(v) for k, v in (caller_new_blocks or {}).items()
    }
    for block in planner_result.new_blocks:
        svg = block.get("svg")
        block_id = block.get("id")
        if not svg or not block_id:
            continue
        per_svg = final_new_blocks.setdefault(svg, {})
        # Keep first block-id collision losing; the LLM should not emit
        # two blocks with the same id on the same svg.
        per_svg[block_id] = {
            "bounds": block["bounds"],
            "layout": block["layout"],
            **({"spec": block["spec"]} if block.get("spec") else {}),
        }

    # Bug #2 fix (safety net): for every cloned content SVG that the LLM
    # forgot to give a ``new_blocks`` entry, synthesize a default
    # 3-column-cards block from the markdown text. Without this, the cloned
    # content page renders as a giant blank rectangle (shape-3 in slide_04
    # skeleton, 1124×530) because the skeleton only ships a title bar.
    _fill_missing_content_blocks(
        cloned_svgs=[
            entry.get("svg", "")
            for entry in planner_result.page_plan_additions
            if entry.get("svg", "").endswith("_content.svg")
        ],
        final_new_blocks=final_new_blocks,
        state=state,
    )

    # Bug 1 fix (Phase B): the ending skeleton (e.g. THANK YOU) must be
    # the LAST page in the deck, not just the last original. Source-order
    # sorting placed it at slide 5 of a 13-page deck because cloned
    # PART_* content pages take source_slide 3/4, jumping over slide_05.
    # Strategy: after all clones are appended, locate the ending entry
    # (skeleton_kind says "ending") and move it to the tail.
    if planner_result.skeleton_kind:
        ending_svgs = {
            svg for svg, kind in planner_result.skeleton_kind.items()
            if kind == "ending"
        }
        ending_idx: int | None = None
        for idx, page in enumerate(final_pages):
            if page.get("svg") in ending_svgs:
                ending_idx = idx
                break  # take the first (there should be exactly one)
        if ending_idx is not None and ending_idx != len(final_pages) - 1:
            ending_entry = final_pages.pop(ending_idx)
            final_pages.append(ending_entry)
            log.info(
                "phase2.6: moved ending slide %s to position %d "
                "(was at %d)",
                ending_entry.get("svg"),
                len(final_pages),
                ending_idx + 1,
            )

    state.context["page_plan_pages"] = final_pages
    state.context["new_content_blocks"] = final_new_blocks
    state.context["skeleton_kind"] = planner_result.skeleton_kind

    # Pre-repair vendor XML quirks on freshly-cloned SVGs so phase3 edits
    # don't trip on duplicate-attribute / unescaped-quote bugs the original
    # skeletons had. Originals are already repaired by phase2 import (or
    # phase4 entry); we re-repair everything here because (a) it's cheap
    # and idempotent, and (b) it lets new_content_blocks apply to any
    # SVG in the workspace without the caller having to think about it.
    if new_clones or planner_result.new_blocks:
        try:
            repaired = autofix.repair_workspace_svgs(authoring_dir)
            if repaired:
                log.info(
                    "phase2.6: pre-repaired %d SVG file(s) "
                    "(new clones / blocks)",
                    repaired,
                )
        except Exception as exc:
            log.warning(
                "phase2.6: clone repair failed: %s: %s",
                type(exc).__name__, exc,
            )

    log.info(
        "phase2.6: realized %d page addition(s) + %d new block(s); "
        "total page_plan_pages=%d",
        len(planner_result.page_plan_additions),
        len(planner_result.new_blocks),
        len(final_pages),
    )
    state.stage = "imported"
    return state


def phase3_author(
    state: PipelineState,
    page_plan_pages: list[dict] | None,
    content_mapping: dict[str, dict[str, str]],
    new_content_blocks: dict[str, dict[str, dict[str, Any]]] | None,
) -> PipelineState:
    """Phase 3: write page_plan.json + apply per-slide edits + new blocks."""
    state.stage = "plan"
    workspace = state.workspace
    assert workspace is not None

    authoring_dir = workspace / "authoring-svg-flat"

    if page_plan_pages:
        write_page_plan(workspace, page_plan_pages)

    state.stage = "author"
    for svg_name, edits in content_mapping.items():
        svg_path = authoring_dir / svg_name
        if not svg_path.is_file():
            state.warnings.append(
                f"content_mapping: svg not found authoring-svg-flat/{svg_name}; "
                f"skipping {len(edits)} edits"
            )
            continue
        try:
            audit = svg_edits.apply_text_edits(svg_path, edits)
            for entry in audit:
                if entry.get("status") != "applied":
                    state.warnings.append(
                        f"edit skipped svg={svg_name} shape={entry.get('shape_id')} "
                        f"reason={entry.get('status')}"
                    )
        except (ValueError, FileNotFoundError) as exc:
            state.errors.append(
                f"edit failed svg={svg_name}: {type(exc).__name__}: {exc}"
            )

    # Bug fix (Phase B+, post-Phase-A): remove unfilled TOC slot <g>
    # elements so the TOC slide does not display template default
    # placeholder text in empty rows. See _remove_empty_toc_slots.
    _remove_empty_toc_slots(authoring_dir, state)

    for svg_name, blocks in (new_content_blocks or {}).items():
        svg_path = authoring_dir / svg_name
        if not svg_path.is_file():
            state.warnings.append(
                f"new_content_blocks: svg not found authoring-svg-flat/{svg_name}"
            )
            continue
        for shape_id, spec in blocks.items():
            bounds = spec.get("bounds")
            if not bounds:
                state.warnings.append(
                    f"new_content_block svg={svg_name} shape={shape_id} "
                    f"missing bounds; skipping (would WARN at quality gate)"
                )
                continue
            try:
                svg_edits.write_new_content_block(
                    svg_path,
                    group_id=shape_id,
                    bounds=bounds,
                    inner_svg=_render_new_block(spec),
                )
            except ValueError as exc:
                state.errors.append(
                    f"new_content_block failed svg={svg_name} shape={shape_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

    state.stage = "authored"
    return state


def phase3_5_pre_export_fixes(
    state: PipelineState, *, source_pptx: Path | None = None
) -> PipelineState:
    """Run pre-emptive fixes that don't depend on a quality-check failure.

    These run unconditionally after phase3 (edits) and before phase4
    (quality check). They handle authoring patterns svg_to_pptx refuses
    silently: picture-structure variants, gradient defs, non-PPT-safe
    font stacks, invalid ``data-pptx-source-ref`` values. Without this
    step, an edited page whose only "edited" change is text can still
    fail phase5 export with ``Edited round-trip source object did not
    produce a DrawingML shape`` because the underlying
    picture/gradient/font/source-ref structure was never normalized.
    """
    workspace = state.workspace
    assert workspace is not None
    authoring_dir = workspace / "authoring-svg-flat"
    slide_files = sorted(authoring_dir.glob("slide_*.svg"))
    slide_files.extend(
        sorted(p for p in authoring_dir.glob("*.svg") if p not in slide_files)
    )

    valid_source_slides: set[int] = set()
    if source_pptx is not None and source_pptx.is_file():
        valid_source_slides = _count_pptx_slides(source_pptx)

    # Determine which slides were edited in phase3 — those need ALL of
    # their data-pptx-source-ref attrs stripped, otherwise svg_to_pptx
    # tries to byte-rehydrate other shapes on the slide and aborts with
    # ``Edited round-trip source object did not produce a DrawingML
    # shape``. Passthrough slides keep their source-refs intact.
    edited_slides = {
        e.split(":", 1)[0].strip()
        for e in state.context.get("edited_svg_paths", [])
    }
    if not edited_slides:
        # Fall back: scan content_mapping keys directly.
        cm = state.context.get("content_mapping", {}) or {}
        edited_slides = set(cm.keys())

    records: list[dict] = []
    for svg_path in slide_files:
        for fn in (
            autofix.fix_gradient_unexportable,
            autofix.fix_picture_structure,
            autofix.fix_unsafe_font,
            autofix.fix_zero_stroke_connector,
        ):
            try:
                for rec in fn(svg_path):
                    records.append(rec.to_dict())
            except Exception as exc:
                log.warning("phase3.5 %s on %s failed: %s", fn.__name__, svg_path.name, exc)
        if valid_source_slides:
            try:
                # For UNEDITED slides: only strip refs that point at
                # non-existent source slides. Refs that point at real
                # slides must be preserved so svg_to_pptx can passthrough
                # the byte from source.
                #
                # For EDITED slides: strip EVERY data-pptx-source-ref.
                # svg_to_pptx will render the whole slide fresh from SVG
                # instead of trying to byte-rehydrate per element, which
                # is what causes the "shape: N" export abort when ANY
                # shape on the slide has changed but other refs still
                # claim rehydration is possible.
                strip_all = svg_path.name in edited_slides
                for rec in autofix.fix_invalid_source_ref(
                    svg_path, valid_source_slides, strip_all=strip_all
                ):
                    records.append(rec.to_dict())
            except Exception as exc:
                log.warning("phase3.5 fix_invalid_source_ref on %s failed: %s", svg_path.name, exc)
    state.fix_iterations.extend(records)
    if records:
        log.info("phase3.5 pre-export fixes: %d action(s) applied", len(records))
    return state


def _count_pptx_slides(source_pptx: Path) -> set[int]:
    """Return the set of 1-based slide numbers present in source PPTX."""
    import zipfile
    try:
        with zipfile.ZipFile(source_pptx) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return set()
    slides: set[int] = set()
    for name in names:
        m = re.match(r"ppt/slides/slide(\d+)\.xml$", name)
        if m:
            slides.add(int(m.group(1)))
    return slides


def phase4_quality(
    state: PipelineState,
    *,
    auto_fix: bool,
    max_fix_iterations: int,
    timeout_ms: int = 60_000,
    strict: bool = True,
) -> PipelineState:
    """Phase 4: refresh summary + quality check + optional auto-fix loop.

    ``strict=False`` treats quality-check WARN/ERROR as advisory — the
    pipeline records them in ``state.warnings`` / ``state.errors`` but
    advances to phase 5 anyway. Required for templates whose vendor
    quality checker emits WARN for non-blocking issues that don't
    actually block svg_to_pptx (e.g. boteng's "non-PPT-safe font" WARN).
    """
    state.stage = "quality"
    workspace = state.workspace
    assert workspace is not None
    authoring_dir = workspace / "authoring-svg-flat"

    # Pre-repair vendor XML bugs (duplicate attributes, unescaped inner
    # quotes) on every SVG in the workspace so that vendor tools
    # (svg_quality_checker, svg_to_pptx) don't choke on them.
    repaired = autofix.repair_workspace_svgs(authoring_dir)
    if repaired:
        log.info("phase4 pre-repair: fixed %d SVG file(s)", repaired)
    # Restore data-pptx-* attrs that the XML repair (or any earlier edit)
    # may have dropped. The snapshot was taken at the end of phase2.
    restored = autofix.restore_shape_attrs(authoring_dir)
    if restored:
        log.info("phase4 pre-export: restored data-pptx-* attrs on %d svg(s)", restored)

    # Bug 18 fix: removed redundant re-assignment of authoring_dir
    # (it was already bound to workspace / "authoring-svg-flat" above).
    slide_files = sorted(authoring_dir.glob("slide_*.svg"))
    # also include any non-conforming clones (e.g. slide_part02_div.svg)
    slide_files.extend(sorted(p for p in authoring_dir.glob("*.svg") if p not in slide_files))

    # Bug 09 fix: removed dead-code block that referenced
    # ``state.page_plan_pages`` (which never existed on PipelineState;
    # page_plan data lives in ``state.context["page_plan_pages"]``).
    # The block was unreachable (``hasattr`` was always False) and did
    # nothing useful (just ``pass``). Validation already happened in
    # phase3_author; phase4 only needs to refresh the summary.

    runner.run_svg_authoring_view_refresh(
        state.skill_dir,  # type: ignore[arg-type]
        authoring_dir,
        timeout_ms=30_000,
    )

    iterations = 0
    while True:
        qc = runner.run_svg_quality_check(
            state.skill_dir,  # type: ignore[arg-type]
            workspace,
            timeout_ms=timeout_ms,
        )
        state.last_quality_stdout = qc.stdout + "\n" + qc.stderr
        if qc.ok:
            state.stage = "quality_passed"
            return state

        # Advisory mode: report QC issues but advance to phase 5.
        if not strict:
            log.warning(
                "phase4 quality_check exit=%d (advisory, strict=False); "
                "proceeding to phase 5",
                qc.exit,
            )
            state.warnings.append(
                f"phase4 quality_check exit={qc.exit} (advisory; "
                "proceeding to phase 5)"
            )
            state.stage = "quality_advisory"
            return state

        if not auto_fix or iterations >= max_fix_iterations:
            state.stage = "failed"
            state.errors.append(
                f"phase4 quality_check failed exit={qc.exit} after "
                f"{iterations} fix iteration(s); last stderr_tail="
                f"{qc.stderr[-400:]}"
            )
            return state

        iterations += 1
        records = autofix.run_autofix_round(
            slide_files,
            qc.stdout + "\n" + qc.stderr,
        )
        for rec in records:
            state.fix_iterations.append(rec.to_dict())
        if not records:
            state.stage = "failed"
            state.errors.append(
                f"phase4 quality_check failed exit={qc.exit} and no auto-fix "
                f"rule matched; aborting"
            )
            return state


def phase5_export(
    state: PipelineState,
    output_pptx: Path,
    *,
    validate_strict: bool = True,
    timeout_ms: int = 240_000,
) -> PipelineState:
    """Phase 5: svg_to_pptx + delivery_check + source_to_md."""
    state.stage = "export"
    state.output_pptx = output_pptx
    workspace = state.workspace
    assert workspace is not None

    # Final restore of data-pptx-* attrs right before svg_to_pptx. The
    # quality-check loop already calls this on entry, but the autofix
    # loop may have mutated shapes since then.
    authoring_dir = workspace / "authoring-svg-flat"
    restored = autofix.restore_shape_attrs(authoring_dir)
    if restored:
        log.info("phase5 pre-export: restored data-pptx-* on %d svg(s)", restored)

    res = runner.run_svg_to_pptx(
        state.skill_dir,  # type: ignore[arg-type]
        workspace,
        output_pptx,
        timeout_ms=timeout_ms,
    )
    if not res.ok:
        state.stage = "failed"
        state.errors.append(
            f"phase5 svg_to_pptx failed exit={res.exit} "
            f"stderr_tail={res.stderr[-600:]}"
        )
        return state

    state.last_export_receipt = (res.parsed or {}).get("export_summary")
    state.stage = "exported"

    # Phase 5b — delivery check.
    state.stage = "validate"
    validation_dir = workspace / "validation"
    io_utils.ensure_dir(validation_dir)
    delivery_report = validation_dir / f"{output_pptx.stem}.delivery.json"
    dc = runner.run_pptx_delivery_check(
        state.skill_dir,  # type: ignore[arg-type]
        output_pptx,
        timeout_ms=60_000,
    )
    state.last_delivery = dc.parsed or {}
    if dc.stdout.strip():
        io_utils.write_utf8_atomic(delivery_report, dc.stdout)

    if validate_strict:
        status = (dc.parsed or {}).get("status")
        if status == "failed":
            state.stage = "failed"
            state.errors.append(
                f"delivery_check status=failed (validate_strict=True): "
                f"{(dc.parsed or {}).get('errors', [])[:3]}"
            )
            return state

    # Phase 5c — readback.md.
    readback_md = validation_dir / "readback.md"
    rm = runner.run_source_to_md(
        state.skill_dir,  # type: ignore[arg-type]
        output_pptx,
        readback_md,
        timeout_ms=60_000,
    )
    state.last_delivery = state.last_delivery or {}
    state.last_delivery["readback_md"] = (
        str(readback_md) if rm.ok and readback_md.is_file() else ""
    )

    state.stage = "done"
    return state


# ---------------------------------------------------------------------------
# Top-level orchestration.
# ---------------------------------------------------------------------------

def run_native_fill(
    *,
    source_pptx: Path,
    workspace: Path,
    output_pptx: Path,
    page_plan: list[dict] | None,
    content_mapping: dict[str, dict[str, str]],
    new_content_blocks: dict[str, dict[str, dict[str, Any]]] | None,
    skill_dir: Path,
    auto_fix: bool = True,
    max_fix_iterations: int = 3,
    validate_strict: bool = True,
    inheritance_mode: str = "both",
    content_markdown: Path | None = None,
    llm_plan: bool = False,
    skip_phase3_5: bool = False,
    quality_strict: bool = True,
) -> dict[str, Any]:
    """End-to-end native_fill pipeline.

    Always returns a dict suitable as an MCP tool result. ``ok`` is True
    only when the final stage reaches ``done`` and validate_strict succeeded.

    ``skip_phase3_5`` (default False) bypasses
    :func:`phase3_5_pre_export_fixes`. Set True only for source templates
    whose nested picture form is mis-rehydrated by phase3.5's
    ``fix_picture_structure`` (e.g. boteng slide_02/03). For normal
    templates the fixup protects against svg_to_pptx rejections.
    """
    state = PipelineState(skill_dir=skill_dir)

    guard = runner.run_attribution_guard(skill_dir)
    if not guard.ok:
        return {
            "ok": False,
            "stage": "init",
            "error": (
                f"attribution_guard failed exit={guard.exit} "
                f"stderr_tail={guard.stderr[-400:]}"
            ),
            "duration_ms": state.elapsed_ms(),
        }

    # Phase 2
    state = phase2_import(
        state,
        source_pptx=source_pptx,
        workspace=workspace,
        inheritance_mode=inheritance_mode,
    )
    if state.stage == "failed":
        return _finalize(state)

    # Snapshot data-pptx-* attrs on every shape BEFORE any editing. Phase 5
    # (svg_to_pptx) refuses shapes whose attributes were dropped during
    # text edits, so we capture the clean baseline here and restore in the
    # autofix loop + right before export.
    assert state.workspace is not None
    captured = autofix.snapshot_shape_attrs(
        state.workspace / "authoring-svg-flat"
    )
    log.info("phase2: snapshotted data-pptx-* attrs on %d shape(s)", captured)

    # Phase 2.5 — optional LLM-driven content planning. Runs only when the
    # caller passed both `content_markdown` and `llm_plan=true`. The
    # LLM-derived mapping is merged into the caller-supplied
    # ``content_mapping`` (caller entries win on conflict).
    if content_markdown is not None and llm_plan:
        state = phase2_5_llm_plan(state, content_markdown, content_mapping)
        if state.stage == "failed":
            return _finalize(state)
        # phase3_author reads the (now possibly merged) mapping from state.
        merged_mapping = state.context.get("content_mapping", content_mapping)
    else:
        merged_mapping = content_mapping

    # Phase 2.6 — materialize LLM planner output (Phase A expansion):
    # clone skeleton SVGs for page_plan_additions, register new_blocks.
    # Falls through as a no-op when the planner did not run.
    state = phase2_6_realize_planner_output(
        state,
        caller_page_plan=page_plan,
        caller_new_blocks=new_content_blocks,
    )
    if state.stage == "failed":
        return _finalize(state)
    final_pages = state.context.get("page_plan_pages", page_plan)
    final_blocks = state.context.get("new_content_blocks", new_content_blocks)

    # Phase 3
    state = phase3_author(state, final_pages, merged_mapping, final_blocks)
    if state.stage == "failed":
        return _finalize(state)

    # Phase 3.5 — pre-emptive picture/gradient/font/source-ref normalization.
    # Runs before quality-check so the autofix loop in phase4 only has to
    # deal with overflow / viewBox / page_plan issues that depend on
    # rendered metrics.
    # Bypass when caller sets skip_phase3_5=True (boteng nested-SVG regression).
    if skip_phase3_5:
        log.info(
            "phase3.5 SKIPPED by skip_phase3_5=True "
            "(boteng-style nested-SVG compatibility); "
            "running source-ref fixup only"
        )
        # Even when skipping phase3.5 we still need to strip invalid
        # ``data-pptx-source-ref`` attributes — the boteng round-trip
        # emits refs like ``slide:22`` for a 5-slide deck which
        # svg_to_pptx cannot resolve. We run JUST the source-ref fix
        # (no picture_structure rewrite, no gradient/font rewrite).
        # Also run fix_zero_stroke_connector: vendor svg_to_pptx drops
        # <path stroke-width="0"> elements, which silently strips
        # template underlines (e.g. boteng TOC header bar). Safe —
        # only touches connector paths.
        try:
            valid = set(range(1, 100))  # best-effort permissive set
            for svg_path in sorted(
                (state.workspace / "authoring-svg-flat").glob("*.svg")
            ):
                for rec in autofix.fix_invalid_source_ref(
                    svg_path, valid, strip_all=True,
                ):
                    state.context.setdefault(
                        "fix_iterations", []
                    ).append(rec.to_dict())
                for rec in autofix.fix_zero_stroke_connector(svg_path):
                    state.context.setdefault(
                        "fix_iterations", []
                    ).append(rec.to_dict())
        except Exception as exc:
            log.warning(
                "phase3.5-bypass source-ref fixup failed: %s", exc,
            )
    else:
        state = phase3_5_pre_export_fixes(state, source_pptx=source_pptx)
        if state.stage == "failed":
            return _finalize(state)

    # Phase 4 (with auto-fix loop)
    state = phase4_quality(
        state,
        auto_fix=auto_fix,
        max_fix_iterations=max_fix_iterations,
        strict=quality_strict,
    )
    if state.stage == "failed":
        return _finalize(state)

    # Phase 5
    state = phase5_export(
        state,
        output_pptx=output_pptx,
        validate_strict=validate_strict,
    )
    return _finalize(state)


def _finalize(state: PipelineState) -> dict[str, Any]:
    """Render the state as an MCP-friendly JSON dict."""
    payload: dict[str, Any] = {
        "ok": state.stage == "done",
        "stage": state.stage,
        "workspace": str(state.workspace) if state.workspace else None,
        "output_pptx": str(state.output_pptx) if state.output_pptx else None,
        "duration_ms": state.elapsed_ms(),
        "fix_iterations": state.fix_iterations,
        "warnings": state.warnings,
        "errors": state.errors,
    }
    # If the LLM phase ran, surface the merged mapping so callers can audit
    # which edits came from where.
    if "content_mapping" in state.context:
        payload["llm_content_mapping"] = state.context["content_mapping"]
    if state.last_export_receipt is not None:
        payload["export_summary"] = state.last_export_receipt
    if state.last_delivery is not None:
        payload["delivery"] = {
            "status": state.last_delivery.get("status"),
            "zip_integrity": state.last_delivery.get("zip_integrity"),
            "slides_count": state.last_delivery.get("slides_count"),
            "advisories_count": len(state.last_delivery.get("advisories") or []),
        }
        payload["readback_md"] = state.last_delivery.get("readback_md", "")
    return payload


# ---------------------------------------------------------------------------
# Tiny helpers for new_content_blocks "layout" presets.
# ---------------------------------------------------------------------------

def _coerce_str_list(value: Any, sep: str = "; ") -> list[str]:
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


def _render_new_block(spec: dict[str, Any]) -> str:
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
            items = _coerce_str_list(card.get("items", []))
            parts.append(
                f'<rect x="{cx:g}" y="{by:g}" width="{card_w:g}" '
                f'height="{bh:g}" rx="8" fill="{color}" fill-opacity="0.12" '
                f'stroke="{color}" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{cx + 16:g}" y="{by + 32:g}" font-size="18" '
                f'font-weight="bold" fill="{color}">{_escape(title)}</text>'
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
                    f'fill="#222">{_escape(item)}</text>'
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
            items = _coerce_str_list(step.get("items", []))
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
                f'font-weight="bold" fill="{color}">{_escape(title)}</text>'
            )
            for j, item in enumerate(items):
                ty = by + 92 + j * 22
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" font-size="13" '
                    f'fill="#222">{_escape(item)}</text>'
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
                f'{_escape(header)}</text>'
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
                        tspans.append(_escape(item))
                    else:
                        tspans.append(
                            f'<tspan x="{bx + j * col_w + 12:g}" dy="14">'
                            f'{_escape(item)}</tspan>'
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
            f'{_escape(value)}{_escape(unit)}</text>'
        )
        # Caption below.
        if caption:
            parts.append(
                f'<text x="{cx:g}" y="{value_y + 36:g}" text-anchor="middle" '
                f'font-size="18" fill="#666">'
                f'{_escape(caption)}</text>'
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
            f'fill="#222">{_escape(quote)}</text>'
        )
        # Attribution bottom-right.
        if attribution:
            parts.append(
                f'<text x="{bx + bw - 24:g}" y="{by + bh - 24:g}" '
                f'text-anchor="end" font-size="14" fill="#666" '
                f'font-style="italic">— {_escape(attribution)}</text>'
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
                f'fill="#1D2CAB">{_escape(side["title"])}</text>'
            )
            # Items list.
            for j, item in enumerate(side["items"]):
                ty = by + 64 + j * 22
                if ty > by + bh - 8:
                    break
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" font-size="14" '
                    f'fill="#222">{_escape(str(item))}</text>'
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
                f'{_escape(str(label))}</text>'
            )
            # Detail below the node.
            if detail:
                parts.append(
                    f'<text x="{cx:g}" y="{axis_y + 36:g}" '
                    f'text-anchor="middle" font-size="12" fill="#222">'
                    f'{_escape(str(detail))}</text>'
                )
        return "\n".join(parts)
    raise ValueError(f"unsupported new_content_block layout: {layout!r}")


# ---------------------------------------------------------------------------
# run_with_mapping: one-shot driver that delegates phase2/3/4/5 to
# run_native_fill. Vendored from generate_local_ppt.run_manual but
# fully generic — no hardcoded boteng shape ids / section names /
# body bounds. All template-specific bits are caller-supplied.
# ---------------------------------------------------------------------------


def run_with_mapping(
    *,
    skill_dir: Path,
    source_pptx: Path,
    workspace: Path,
    output_pptx: Path,
    content_mapping: dict[str, dict[str, str]],
    content_markdown: Path | None = None,
    page_plan: list[dict] | None = None,
    new_content_blocks: dict | None = None,
    # Markdown expansion (all generic, no boteng hardcoding)
    expand_skeleton_divider: int | None = None,
    expand_skeleton_content: int | None = None,
    expand_divider_edits_template: dict[str, str] | None = None,
    expand_content_edits_template: dict[str, str] | None = None,
    expand_body_bounds: str = "0 0 1280 720",
    expand_ending_svg: str | None = None,
    expand_part_names: list[str] | None = None,
    expand_divider_subtitle_template: dict[str, str] | None = None,
    expand_exclude_source_slides: list[int] | None = None,
    # Workaround toggles (opt-in)
    fix_nested_picture: bool = False,
    skip_phase3_5: bool = False,
    # Smart TOC fill (opt-in; auto-detects TOC slide from 目录/CONTENTS)
    expand_toc_from_markdown: bool = False,
    expand_toc_slot_grid: dict[str, Any] | None = None,
    # run_native_fill params
    auto_fix: bool = True,
    max_fix_iterations: int = 3,
    validate_strict: bool = True,
    quality_strict: bool = False,
    clean_workspace: bool = False,
) -> dict[str, Any]:
    """One-stop driver for the "manual mapping + markdown section cloning" workflow.

    Sequence:
      1. ``runner.run_pptx_to_svg`` → authoring-svg-flat/*.svg
      2. apply ``content_mapping`` text edits
      3. optional ``autofix.repair_nested_picture_attrs`` (boteng workaround)
      4. optional ``expand_workspace_from_markdown`` (per-section cloning)
      5. delegate the rest (phase 3 re-apply, phase 3.5/4/5) to
         ``run_native_fill``

    Generic: makes no assumption about template shape ids, body bounds,
    section names, or ending slide. Boteng is one caller among many.

    Parameters
    ----------
    skill_dir / source_pptx / workspace / output_pptx:
        Standard native_fill inputs. All paths are ``.resolve()``-ed
        defensively because ``runner._run`` hard-requires cwd=scripts_dir.
    content_mapping:
        ``{svg_filename: {shape_id: new_text}}`` for the original
        5-slide skeleton. Caller-supplied; boteng ships its own mapping
        in ``tests/_fixtures/boteng_mapping.py`` (or inline in callers).
    content_markdown:
        Optional markdown to expand. When given along with the four
        ``expand_skeleton_*`` / ``expand_*_template`` params, every
        markdown H1 gets a divider + content clone.
    expand_*:
        Forwarded verbatim to :func:`expand_workspace_from_markdown`.
        All default-safe; ``body_bounds`` defaults to full canvas.
    fix_nested_picture:
        Opt-in: strip inner ``data-pptx-*`` attrs from nested
        ``<image>`` / ``<svg>``. Required for boteng slide_02/03.
    skip_phase3_5:
        Opt-in: bypass phase3.5's picture_structure rewrite. Required
        for boteng (pairs with ``fix_nested_picture=True``).
    clean_workspace:
        When True, wipe ``workspace`` and ``output_pptx`` before run.
    """
    skill_dir = Path(skill_dir).resolve()
    source_pptx = Path(source_pptx).resolve()
    workspace = Path(workspace).resolve()
    output_pptx = Path(output_pptx).resolve()
    if content_markdown is not None:
        content_markdown = Path(content_markdown).resolve()

    if clean_workspace and workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    if output_pptx.exists():
        output_pptx.unlink()

    auth = workspace / "authoring-svg-flat"
    auth.mkdir(parents=True, exist_ok=True)

    # Phase 2: pptx_to_svg round-trip
    r2 = runner.run_pptx_to_svg(skill_dir, source_pptx, workspace,
                                roundtrip=True)
    if not r2.ok:
        return {
            "ok": False, "stage": "phase2",
            "stderr": r2.stderr[-600:],
        }

    # Smart TOC pre-filter: when expand_toc_from_markdown is on, drop
    # any manual content_mapping entries that target TOC slot shape ids.
    # Otherwise those manual entries would stomp the auto-generated TOC.
    toc_dropped_count = 0
    resolved_toc: dict[str, list[str]] = {
        "title_ids": [], "subtitle_ids": [], "toc_svg": None,
    }
    if expand_toc_slot_grid is not None:
        toc_svg_explicit = expand_toc_slot_grid.get("toc_svg")
        toc_svg_name: str | None = None
        if toc_svg_explicit and (auth / toc_svg_explicit).is_file():
            toc_svg_name = toc_svg_explicit
        else:
            try:
                toc_svg_name = _find_toc_svg(auth)
            except ValueError:
                toc_svg_name = None
        if toc_svg_name:
            try:
                detected_titles, detected_subs = _detect_toc_slot_shape_ids(
                    auth / toc_svg_name,
                    rows=int(expand_toc_slot_grid["rows"]),
                    cols=int(expand_toc_slot_grid["cols"]),
                    subtitle_offset=expand_toc_slot_grid.get("subtitle_offset"),
                )
            except (ValueError, OSError) as exc:
                log.warning(
                    "smart TOC grid detection failed for %s: %s: %s",
                    toc_svg_name, type(exc).__name__, exc,
                )
            else:
                resolved_toc = {
                    "title_ids": detected_titles,
                    "subtitle_ids": detected_subs,
                    "toc_svg": toc_svg_name,
                }
                if expand_toc_from_markdown and (
                    detected_titles or detected_subs
                ):
                    content_mapping, toc_dropped_count = (
                        _filter_toc_manual_mapping(
                            content_mapping,
                            list(detected_titles) + list(detected_subs),
                        )
                    )

    # Phase 3 (pre): apply text edits to the original skeleton SVGs
    edit_summary: list[dict[str, Any]] = []
    for fn, edits in content_mapping.items():
        p = auth / fn
        if not p.is_file():
            edit_summary.append({
                "svg": fn, "status": "missing_svg",
                "applied": 0, "total": len(edits),
            })
            continue
        audit = svg_edits.apply_text_edits(p, edits)
        ok = sum(1 for r in audit if r.get("status") == "applied")
        edit_summary.append({
            "svg": fn, "status": "applied",
            "applied": ok, "total": len(edits),
        })

    # Nested-SVG inner data-pptx-* strip (opt-in, boteng-style)
    strip_report: dict[str, int] = {
        "files_scanned": 0, "files_modified": 0, "attrs_stripped": 0,
    }
    if fix_nested_picture:
        strip_report = autofix.repair_nested_picture_attrs(auth)

    # Markdown → page_plan expansion (only when caller supplied the
    # four required template params)
    expansions: dict[str, Any] = {"cloned_svgs": [], "n_parts": 0}
    if (
        content_markdown is not None
        and content_markdown.is_file()
        and expand_skeleton_divider is not None
        and expand_skeleton_content is not None
        and expand_divider_edits_template is not None
        and expand_content_edits_template is not None
    ):
        expansions = expand_workspace_from_markdown(
            workspace, content_markdown,
            skeleton_divider=expand_skeleton_divider,
            skeleton_content=expand_skeleton_content,
            divider_edits_template=expand_divider_edits_template,
            content_edits_template=expand_content_edits_template,
            body_bounds=expand_body_bounds,
            ending_svg=expand_ending_svg,
            part_names=expand_part_names,
            divider_subtitle_template=expand_divider_subtitle_template,
            exclude_source_slides=expand_exclude_source_slides,
        )

    # Smart TOC fill (opt-in): auto-fill TOC slide from markdown H1s.
    # Runs AFTER content_mapping so the auto-fill wins over any
    # leftover placeholders. We already pre-filtered manual TOC entries
    # above so they don't stomp the auto-generated values.
    toc_summary: dict[str, Any] = {
        "toc_svg": None, "slot_count": 0, "filled": 0,
        "cloned_svgs": [],
    }
    if (
        expand_toc_from_markdown
        and content_markdown is not None
        and content_markdown.is_file()
        and resolved_toc["title_ids"]
    ):
        toc_subtitle_ids = [
            sid for sid in resolved_toc["subtitle_ids"] if sid
        ] or None
        toc_summary = expand_workspace_from_toc(
            workspace, content_markdown,
            toc_slot_title_ids=resolved_toc["title_ids"],
            toc_slot_subtitle_ids=toc_subtitle_ids,
            toc_svg=resolved_toc["toc_svg"],
        )
        # Also merge the TOC fill into content_mapping for the TOC slide
        # so phase3_author's re-apply (idempotent) restores the fill
        # AFTER run_native_fill's phase2_import overwrites the SVG.
        # Without this merge the TOC reverts to template defaults.
        toc_svg_name = toc_summary["toc_svg"]
        toc_edits_for_phase3 = _build_toc_phase3_edits(
            workspace, content_markdown,
            toc_slot_title_ids=resolved_toc["title_ids"],
            toc_slot_subtitle_ids=toc_subtitle_ids,
        )
        if toc_edits_for_phase3:
            existing = content_mapping.get(toc_svg_name, {})
            merged = {**existing, **toc_edits_for_phase3}
            content_mapping = {
                **content_mapping, toc_svg_name: merged,
            }

    # Delegate phase 3 re-apply / 3.5 / 4 / 5 to run_native_fill.
    # run_native_fill will re-apply text edits (idempotent), optionally
    # skip phase 3.5, and run quality_check + svg_to_pptx.
    #
    # If we just expanded the workspace from markdown, forward the
    # resulting page_plan.json so phase3_author's write_page_plan
    # doesn't clobber it with the original-roster-only version.
    effective_page_plan = page_plan
    if expansions.get("page_plan_path"):
        plan_json = expansions["page_plan_path"]
        if plan_json.is_file():
            try:
                payload = json.loads(
                    plan_json.read_text(encoding="utf-8")
                )
                if isinstance(payload.get("pages"), list):
                    effective_page_plan = payload["pages"]
            except (json.JSONDecodeError, OSError) as exc:
                log.warning(
                    "run_with_mapping: could not parse expanded page_plan: %s",
                    exc,
                )
    result = run_native_fill(
        source_pptx=source_pptx,
        workspace=workspace,
        output_pptx=output_pptx,
        page_plan=effective_page_plan,
        content_mapping=content_mapping,
        new_content_blocks=new_content_blocks,
        content_markdown=content_markdown,
        skill_dir=skill_dir,
        auto_fix=auto_fix,
        max_fix_iterations=max_fix_iterations,
        validate_strict=validate_strict,
        skip_phase3_5=skip_phase3_5,
        quality_strict=quality_strict,
    )
    # Augment result with the pre-delegation work that the caller
    # asked about (edit_summary, strip_report, expansions, toc_summary).
    result["edit_summary"] = edit_summary
    result["strip_report"] = strip_report
    result["expansions"] = expansions
    result["toc_summary"] = toc_summary
    result["toc_dropped_entries"] = toc_dropped_count
    return result


# ---------------------------------------------------------------------------
# Markdown → page_plan expansion (no-LLM).
#
# Vendored from generate_local_ppt.expand_workspace_from_markdown and
# made fully generic — no hardcoded boteng section names, shape ids,
# or body bounds. All template-specific bits are caller-supplied.
# ---------------------------------------------------------------------------


def expand_workspace_from_markdown(
    workspace: Path,
    md_path: Path,
    *,
    skeleton_divider: int,
    skeleton_content: int,
    divider_edits_template: dict[str, str],
    content_edits_template: dict[str, str],
    body_bounds: str = "0 0 1280 720",
    layout: str = "3-column-cards",
    ending_svg: str | None = None,
    part_names: list[str] | None = None,
    divider_subtitle_template: dict[str, str] | None = None,
    exclude_source_slides: list[int] | None = None,
) -> dict[str, Any]:
    """Clone skeleton slides for each markdown H1 section.

    For every H1 in ``md_path`` we clone ``slide_<skeleton_divider>.svg``
    into ``slide_partNN_div.svg`` and ``slide_<skeleton_content>.svg``
    into ``slide_partNN_content.svg``, then apply template-supplied text
    edits and embed an auto-generated new_block of cards pulled from
    the section body. Finally we re-seal ``page_plan.json`` with the
    original roster prepended and (optionally) ``ending_svg`` moved
    to the last position.

    Parameters
    ----------
    workspace:
        Authoring workspace root. ``authoring-svg-flat/`` must exist
        under it and contain ``slide_<NN>.svg`` skeletons.
    md_path:
        Markdown file. H1s (``^#\\s+(.+)$``) define sections.
    skeleton_divider / skeleton_content:
        Slide numbers (1-based) of the divider / content skeleton SVGs.
        Required — caller must supply whatever fits their template.
    divider_edits_template / content_edits_template:
        ``{shape_id: text_format}`` maps. Each ``text_format`` supports
        ``{nn}`` (zero-padded section index), ``{n}`` (1-based integer),
        and ``{title}`` (H1 title text). e.g. boteng::
            {"shape-4": "PART {nn}", "shape-5": "{title}"}
    body_bounds:
        ``"x y w h"`` for the embedded cards block. Default is the
        full 1280×720 canvas; boteng callers pass ``"120 130 1060 480"``.
    layout:
        Layout name passed to :func:`_render_new_block`. Default
        ``"3-column-cards"``.
    ending_svg:
        If given, force-move this svg filename to the end of the
        generated page_plan (so the deck ends with the ending slide).
    part_names:
        Optional ordered list of section titles. Default ``None`` →
        auto-extract every H1 from the markdown. Caller may supply a
        subset to cap cloning (e.g. take only the first 4 H1s).
    divider_subtitle_template:
        Optional second-pass text edits applied to each cloned divider
        AFTER ``divider_edits_template``. Same shape-id → text-format
        format (``{nn}`` / ``{n}`` / ``{title}`` placeholders). Use
        this for shapes the main title template doesn't cover — e.g.
        boteng's English subtitle shape::

            divider_subtitle_template={"shape-70": "{title_en}"}

        Caller is responsible for picking the right shape ids and
        supplying the value data; this function makes no assumption
        about which shape ids exist on the template.
    exclude_source_slides:
        Optional list of 1-based slide numbers to drop from the
        original roster in ``page_plan.json``. The skeleton slide used
        for cloning (``skeleton_divider`` / ``skeleton_content``) is
        usually a design sample whose on-deck counterpart would
        duplicate the cloned per-section pages — boteng callers pass
        ``[skeleton_divider]`` so the divider sample is cloned but
        not also emitted as a standalone page. Default ``None``
        preserves every original.

    Returns
    -------
    ``{"cloned_svgs": [..], "page_plan_path": Path, "n_parts": int}``.

    Fully generic: this function makes no assumption about template
    shape ids, body bounds, section names, or layout choice. The boteng
    scenario is one of many callers.
    """
    md_text = md_path.read_text(encoding="utf-8")
    sections = _split_markdown_sections(md_text)
    if part_names is None:
        part_names = [s["title"] for s in sections]
    n_parts = min(len(part_names), len(sections))

    auth = workspace / "authoring-svg-flat"
    cloned: list[str] = []

    def _format(template_dict: dict[str, str], *, nn: str, n: int,
                title: str) -> dict[str, str]:
        return {
            k: v.format(nn=nn, n=n, title=title)
            for k, v in template_dict.items()
        }

    for i, title in enumerate(part_names[:n_parts], start=1):
        nn = f"{i:02d}"

        # 1) divider clone
        div_svg_name = f"slide_part{nn}_div.svg"
        div_skeleton = auth / f"slide_{skeleton_divider:02d}.svg"
        if div_skeleton.is_file():
            shutil.copy2(div_skeleton, auth / div_svg_name)
            div_edits = _format(divider_edits_template,
                                nn=nn, n=i, title=title)
            svg_edits.apply_text_edits(auth / div_svg_name, div_edits)
            # Optional subtitle second-pass (e.g. English subtitle shape).
            if divider_subtitle_template:
                sub_edits = _format(divider_subtitle_template,
                                    nn=nn, n=i, title=title)
                svg_edits.apply_text_edits(auth / div_svg_name, sub_edits)
            cloned.append(div_svg_name)
        else:
            log.warning(
                "expand: divider skeleton %s missing; skipping %s",
                div_skeleton.name, div_svg_name,
            )

        # 2) content clone + new_block
        cont_svg_name = f"slide_part{nn}_content.svg"
        cont_skeleton = auth / f"slide_{skeleton_content:02d}.svg"
        if cont_skeleton.is_file():
            shutil.copy2(cont_skeleton, auth / cont_svg_name)
            cont_edits = _format(content_edits_template,
                                 nn=nn, n=i, title=title)
            svg_edits.apply_text_edits(auth / cont_svg_name, cont_edits)
            # Embed auto-generated cards block
            stem = f"part{nn}"
            cards = _cards_for_section(sections, stem)
            for c in cards:
                c["items"] = [
                    it[:40] + ("…" if len(it) > 40 else "")
                    for it in c["items"]
                ]
            if not cards:
                cards = [{
                    "title": "要点",
                    "color": "#1D2CAB",
                    "items": ["(待补充)"],
                }]
            spec = {
                "layout": layout,
                "spec": {"cards": cards},
                "bounds": body_bounds,
            }
            inner = _render_new_block(spec)
            svg_edits.write_new_content_block(
                auth / cont_svg_name,
                group_id="body_cards",
                bounds=body_bounds,
                inner_svg=inner,
            )
            cloned.append(cont_svg_name)
        else:
            log.warning(
                "expand: content skeleton %s missing; skipping %s",
                cont_skeleton.name, cont_svg_name,
            )

    # Re-seal page_plan.json with original roster + cloned, ending last.
    # Caller may opt to drop skeleton source slides from the roster —
    # those are design samples consumed by cloning and would
    # duplicate the cloned per-section pages if left in.
    exclude_filenames: frozenset[str] = frozenset(
        f"slide_{n:02d}.svg" for n in (exclude_source_slides or [])
    )
    additions_dicts = [
        {
            "source_slide": skeleton_divider if n.endswith("_div.svg")
            else skeleton_content,
            "svg": n,
        }
        for n in cloned
    ]
    original_roster = _seed_original_roster(
        auth, exclude=exclude_filenames,
    )
    pages = original_roster + additions_dicts
    if ending_svg:
        idx = next(
            (i for i, p in enumerate(pages)
             if p.get("svg") == ending_svg),
            None,
        )
        if idx is not None and idx != len(pages) - 1:
            pages.append(pages.pop(idx))
    plan_path = write_page_plan(workspace, pages)
    return {
        "cloned_svgs": cloned,
        "page_plan_path": plan_path,
        "n_parts": n_parts,
    }


def _escape(text: str) -> str:
    """XML-escape a label for safe interpolation into SVG."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# expand_workspace_from_toc: smart TOC fill from markdown H1s.
#
# Caller supplies the slot shape-id lists (title + optional subtitle,
# row-major fill order). Function: auto-detects the TOC slide, fills
# < N slots and clears the rest, clones overflow into
# slide_partNN_toc.svg, and updates page_plan.json so clones follow the
# original TOC. Fully generic — no per-template shape ids, no font-size
# heuristics, no SVG parsing beyond the 目录/CONTENTS marker detection
# in :func:`_find_toc_svg`.
# ---------------------------------------------------------------------------


def _build_toc_slot_edits(
    titles_for_slots: list[str],
    slot_indices: list[int],
    title_ids: list[str],
    sub_ids: list[str] | None,
) -> dict[str, str]:
    """Build an edits dict for a batch of TOC slots.

    For slot ``slot_indices[i]`` we set:
      * ``title_ids[slot_indices[i]]`` -> ``titles_for_slots[i]``
        (or "" if i out of range -> clear the text).
      * ``sub_ids[slot_indices[i]]`` -> same as title when subtitles
        are provided (caller can post-edit if they want different
        subtitle text per slot).

    Out-of-range titles_for_slots entries mean "clear this slot's text".
    """
    edits: dict[str, str] = {}
    for i, slot_idx in enumerate(slot_indices):
        text = titles_for_slots[i] if i < len(titles_for_slots) else ""
        edits[title_ids[slot_idx]] = text
        if sub_ids is not None:
            edits[sub_ids[slot_idx]] = text
    return edits


def _toc_clone_basename(batch_idx: int) -> str:
    """Map batch index to clone SVG filename. batch_idx is 1-based."""
    return f"slide_part{batch_idx:02d}_toc.svg"


def _build_toc_phase3_edits(
    workspace: Path,
    md_path: Path,
    *,
    toc_slot_title_ids: list[str],
    toc_slot_subtitle_ids: list[str] | None,
) -> dict[str, str]:
    """Compute fill+clear edits for the original TOC slide only.

    Used by :func:`run_with_mapping` to merge TOC edits into
    ``content_mapping`` so phase3_author's re-apply (idempotent)
    restores the fill AFTER run_native_fill's phase2_import overwrites
    the SVG. Overflow clones are NOT included — they live outside
    the source pptx and aren't regenerated by phase2_import.
    """
    auth = workspace / "authoring-svg-flat"
    toc_svg = _find_toc_svg(auth)

    sections = _split_markdown_sections(md_path.read_text(encoding="utf-8"))
    titles = [s["title"] for s in sections]
    slot_count = len(toc_slot_title_ids)
    n = min(len(titles), slot_count)

    filled_titles = titles[:n] + [""] * (slot_count - n)
    return _build_toc_slot_edits(
        filled_titles, list(range(slot_count)),
        toc_slot_title_ids, toc_slot_subtitle_ids,
    )


def _toc_deletion_marker_path(workspace: Path) -> Path:
    """Unused — kept as a no-op stub for backwards compatibility with
    any external callers that referenced it. smart TOC no longer
    performs structural deletions; that decision belongs to the
    caller or the LLM-driven content_mapping path.
    """
    raise NotImplementedError(
        "smart TOC deletion marker removed: structural decisions "
        "(delete vs preserve slot shape) belong to caller/LLM"
    )


def _apply_toc_deletion_marker(
    authoring_dir: Path,
    state: PipelineState,
) -> None:
    """No-op. Smart TOC fill preserves slot shapes; caller/LLM decides
    whether to delete the cleared slots.
    """
    return None


def _strip_toc_slot_g_elements(
    svg_path: Path,
    slot_indices: list[int],
    title_ids: list[str],
    sub_ids: list[str] | None,
) -> None:
    """Unused — kept as a no-op stub for backwards compatibility.
    Smart TOC fill preserves slot shapes; structural deletion is
    caller/LLM's responsibility (see ``_remove_empty_toc_slots`` for
    the LLM-driven cleanup).
    """
    raise NotImplementedError(
        "smart TOC slot <g> deletion removed: structural decisions "
        "belong to caller/LLM"
    )


def expand_workspace_from_toc(
    workspace: Path,
    md_path: Path,
    *,
    toc_slot_title_ids: list[str],
    toc_slot_subtitle_ids: list[str] | None = None,
    toc_svg: str | None = None,
    part_names: list[str] | None = None,
) -> dict[str, Any]:
    """Fill the TOC slide from markdown H1s using caller-supplied slot ids.

    Parameters
    ----------
    workspace:
        Authoring workspace root. Must contain ``authoring-svg-flat/``.
    md_path:
        Markdown file; H1s define section titles to fill into the TOC.
    toc_slot_title_ids:
        Ordered list of shape-* ids that receive the chapter titles, in
        row-major fill order. Required.
    toc_slot_subtitle_ids:
        Optional parallel list of shape-* ids for subtitles. If given,
        must have the same length as ``toc_slot_title_ids``. If omitted,
        subtitle slots are left untouched.
    toc_svg:
        Optional explicit TOC SVG filename. Auto-detected (first slide
        containing "目录" / "CONTENTS") if None.
    part_names:
        Optional explicit list of section titles. Defaults to all H1s
        in the markdown.

    Behaviour
    ---------
    * N <= slots_total: edit ``toc_svg`` in place. Fill first N slots;
      clear remaining slots' title text (and subtitle text if subtitle
      ids were supplied). Slot ``<g>`` shapes are preserved — only
      ``<text>`` nodes are emptied. Structural cleanup (delete empty
      slot shapes) is the caller's responsibility; the LLM-driven
      path handles it via :func:`_remove_empty_toc_slots`.
    * N > slots_total: fill first batch in ``toc_svg``; for each
      subsequent batch of ``slots_total`` items, clone ``toc_svg`` as
      ``slide_partNN_toc.svg`` and fill. page_plan.json is updated so
      clones appear after the original TOC slide.

    Returns
    -------
    dict with keys: ``toc_svg`` (str), ``slot_count`` (int),
    ``filled`` (int), ``cloned_svgs`` (list[str]).
    """
    auth = workspace / "authoring-svg-flat"
    if toc_svg is None:
        toc_svg = _find_toc_svg(auth)

    sections = _split_markdown_sections(md_path.read_text(encoding="utf-8"))
    titles = part_names if part_names is not None else [s["title"] for s in sections]
    n_total = len(titles)
    slot_count = len(toc_slot_title_ids)
    if toc_slot_subtitle_ids is not None and len(toc_slot_subtitle_ids) != slot_count:
        raise ValueError(
            f"toc_slot_subtitle_ids length {len(toc_slot_subtitle_ids)} "
            f"!= toc_slot_title_ids length {slot_count}"
        )

    # --- < N / == N case: edit toc_svg in place ---
    fill_n = min(n_total, slot_count)
    all_indices = list(range(slot_count))
    edits = _build_toc_slot_edits(
        titles[:fill_n] + [""] * (slot_count - fill_n),
        all_indices,
        toc_slot_title_ids,
        toc_slot_subtitle_ids,
    )
    svg_edits.apply_text_edits(auth / toc_svg, edits)

    # --- > N case: clone per overflow batch ---
    cloned: list[str] = []
    if n_total > slot_count:
        for batch_idx, batch_start in enumerate(
            range(slot_count, n_total, slot_count), start=2
        ):
            clone_name = _toc_clone_basename(batch_idx)
            shutil.copy2(auth / toc_svg, auth / clone_name)
            batch_titles = titles[batch_start:batch_start + slot_count]
            clone_fill = len(batch_titles)
            clone_edits = _build_toc_slot_edits(
                batch_titles + [""] * (slot_count - clone_fill),
                all_indices,
                toc_slot_title_ids,
                toc_slot_subtitle_ids,
            )
            svg_edits.apply_text_edits(auth / clone_name, clone_edits)
            cloned.append(clone_name)

    # --- Update page_plan.json: clones follow the original TOC ---
    if cloned:
        plan_path = workspace / "page_plan.json"
        if plan_path.is_file():
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            pages = payload.get("pages")
            if isinstance(pages, list):
                toc_source = _toc_slide_number(toc_svg)
                # Find TOC slide index in roster
                toc_idx = next(
                    (i for i, p in enumerate(pages)
                     if p.get("svg") == toc_svg),
                    len(pages),
                )
                # Build clone entries; insert right after original TOC
                clone_entries = [
                    {"source_slide": toc_source, "svg": c} for c in cloned
                ]
                pages = (
                    pages[:toc_idx + 1]
                    + clone_entries
                    + [p for p in pages[toc_idx + 1:]
                       if p.get("svg") not in set(cloned)]
                )
                payload["pages"] = pages
                plan_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    return {
        "toc_svg": toc_svg,
        "slot_count": slot_count,
        "filled": n_total,
        "cloned_svgs": cloned,
    }


def _toc_slide_number(toc_svg_filename: str) -> int:
    """Extract 1-based slide number from ``slide_NN.svg`` filename."""
    m = re.search(r"slide_(\d+)\.svg$", toc_svg_filename)
    return int(m.group(1)) if m else 0

