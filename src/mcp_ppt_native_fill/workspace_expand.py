"""Workspace expansion: clone divider/content skeletons + smart TOC fill.

Extracted from ``pipeline.py`` for module-size reduction. Two entry
points:

  * :func:`expand_workspace_from_markdown` — clone per-H1-section
    divider/content pages, write a new ``page_plan.json``.
  * :func:`expand_workspace_from_toc` — smart TOC fill from markdown
    H1s (fill N slots, clear the rest, clone overflow batches).

Both are pure helpers: they read the workspace, write SVGs, and (for
TOC fill) update ``page_plan.json`` so clones follow the original.

Dependency graph:
    workspace_expand.py
        ├─→ toc_detection.py (find_toc_svg, _TOC helpers,
        │                       split_markdown_sections, cards_for_section)
        ├─→ block_renderer.py (render_new_block)
        └─→ pipeline.py       (write_page_plan, _seed_original_roster)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from . import svg_edits
from .block_renderer import render_new_block
from .toc_detection import (
    cards_for_section as _cards_for_section,
    find_toc_svg as _find_toc_svg,
    split_markdown_sections as _split_markdown_sections,
)

log = logging.getLogger(__name__)


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
        Layout name passed to :func:`render_new_block`. Default
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
    # Imported here to avoid a top-level pipeline import cycle.
    from .pipeline import _seed_original_roster, write_page_plan

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
            # Optional subtitle second-pass (e.g. english subtitle shape).
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
            inner = render_new_block(spec)
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


def build_toc_slot_edits(
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


def toc_clone_basename(batch_idx: int) -> str:
    """Map batch index to clone SVG filename. batch_idx is 1-based."""
    return f"slide_part{batch_idx:02d}_toc.svg"


def build_toc_phase3_edits(
    workspace: Path,
    md_path: Path,
    *,
    toc_slot_title_ids: list[str],
    toc_slot_subtitle_ids: list[str] | None,
) -> dict[str, str]:
    """Compute fill+clear edits for the original TOC slide only.

    Used by :func:`pipeline.run_with_mapping` to merge TOC edits into
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
    return build_toc_slot_edits(
        filled_titles, list(range(slot_count)),
        toc_slot_title_ids, toc_slot_subtitle_ids,
    )


def toc_deletion_marker_path(workspace: Path) -> Path:
    """Unused — kept as a no-op stub for backwards compatibility with
    any external callers that referenced it. smart TOC no longer
    performs structural deletions; that decision belongs to the
    caller or the LLM-driven content_mapping path.
    """
    raise NotImplementedError(
        "smart TOC deletion marker removed: structural decisions "
        "(delete vs preserve slot shape) belong to caller/LLM"
    )


def apply_toc_deletion_marker(
    authoring_dir: Path,
    state: Any,  # PipelineState; kept as Any to avoid circular import
) -> None:
    """No-op. Smart TOC fill preserves slot shapes; caller/LLM decides
    whether to delete the cleared slots.
    """
    return None


def strip_toc_slot_g_elements(
    svg_path: Path,
    slot_indices: list[int],
    title_ids: list[str],
    sub_ids: list[str] | None,
) -> None:
    """Unused — kept as a no-op stub for backwards compatibility.
    Smart TOC fill preserves slot shapes; structural deletion is
    caller/LLM's responsibility (see ``remove_empty_toc_slots`` for
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
      path handles it via :func:`remove_empty_toc_slots`.
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
    edits = build_toc_slot_edits(
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
            clone_name = toc_clone_basename(batch_idx)
            shutil.copy2(auth / toc_svg, auth / clone_name)
            batch_titles = titles[batch_start:batch_start + slot_count]
            clone_fill = len(batch_titles)
            clone_edits = build_toc_slot_edits(
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
                toc_source = toc_slide_number(toc_svg)
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


def toc_slide_number(toc_svg_filename: str) -> int:
    """Extract 1-based slide number from ``slide_NN.svg`` filename."""
    m = re.search(r"slide_(\d+)\.svg$", toc_svg_filename)
    return int(m.group(1)) if m else 0
