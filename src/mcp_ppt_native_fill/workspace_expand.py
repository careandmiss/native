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
    section_title_en_map: dict[str, str] | None = None,
    exclude_source_slides: list[int] | None = None,
) -> dict[str, Any]:
    """Clone skeleton slides for each markdown H1 section.

    For every H1 in ``md_path`` we clone ``slide_<skeleton_divider>.svg``
    into ``slide_partNN_div.svg`` and ``slide_<skeleton_content>.svg``
    into ``slide_partNN_content.svg``, then apply template-supplied text
    edits and accumulate a new-block spec (one per content slide) under
    the ``new_blocks`` return key. The caller (:func:`pipeline.run_with_mapping`)
    feeds those specs into ``realize_plan`` via ``new_content_blocks``
    so the LLM and caller paths share one SVG-write pipeline and the
    same ``group_id`` collision semantics.

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
        format (``{nn}`` / ``{n}`` / ``{title}`` / ``{title_en}``
        placeholders). Use this for shapes the main title template
        doesn't cover — e.g. boteng's English subtitle shape::

            divider_subtitle_template={"shape-70": "{title_en}"}

        Caller is responsible for picking the right shape ids and
        supplying the value data; this function makes no assumption
        about which shape ids exist on the template.
    section_title_en_map:
        Optional ``{section_title: english_title}`` lookup used to
        resolve the ``{title_en}`` placeholder in
        ``divider_subtitle_template``. Phase 8 (2026-09-16):
        lets callers (e.g. boteng_demo) translate each H1's Chinese
        title into the English subtitle that lives on shape-70 of the
        divider template, without breaking the template's original
        visual structure (font / size / color / position). When a
        section title is not in the map, ``{title_en}`` expands to
        the empty string (graceful degradation, never raises).
    exclude_source_slides:
        Optional list of 1-based slide numbers to drop from the
        original roster in ``page_plan.json``. The skeleton slides
        used for cloning (``skeleton_divider`` / ``skeleton_content``)
        are design samples whose on-deck counterparts would duplicate
        the cloned per-section pages — boteng callers pass
        ``[skeleton_divider, skeleton_content]`` so both samples are
        cloned but neither appears as a standalone page in the final
        deck. Default ``None`` preserves every original.

    Returns
    -------
    ``{"cloned_svgs": [..], "page_plan_path": Path, "n_parts": int,
    "new_blocks": {svg_name: {"body_cards": spec}}}``.

    The ``new_blocks`` dict is the Phase 6.2b/6.4 (2026-09-16) addition:
    previously this function wrote its auto-generated cards block
    directly into the cloned SVG via ``write_new_content_block``. That
    caused double-layer stack when the LLM also injected a
    ``content-body`` block (different ``group_id``) into the same SVG.
    By returning the spec and letting ``realize_plan`` route everything
    through one ``write_new_content_block`` call site, the LLM can
    override the same ``body_cards`` key (no double-layer) or add
    separate blocks for orthogonal content (no collision).

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
    # Phase 6.2b/6.4 (2026-09-16): accumulate caller-side new_blocks here
    # instead of writing directly to SVG. ``realize_plan`` consumes this
    # dict via the same ``new_content_blocks`` channel the LLM uses,
    # which gives the LLM one chance to override the same ``group_id``.
    new_blocks: dict[str, dict[str, Any]] = {}

    def _format(template_dict: dict[str, str], *, nn: str, n: int,
                title: str, title_en: str = "") -> dict[str, str]:
        # Phase 8 (2026-09-16): ``title_en`` placeholder for divider
        # subtitle shapes (e.g. boteng shape-70). When the template
        # contains ``{title_en}`` and the caller supplied a section
        # title map, the lookup result is substituted; otherwise it
        # expands to empty string.
        return {
            k: v.format(nn=nn, n=n, title=title, title_en=title_en)
            for k, v in template_dict.items()
        }

    for i, title in enumerate(part_names[:n_parts], start=1):
        nn = f"{i:02d}"

        # 1) divider clone
        div_svg_name = f"slide_part{nn}_div.svg"
        div_skeleton = auth / f"slide_{skeleton_divider:02d}.svg"
        if div_skeleton.is_file():
            shutil.copy2(div_skeleton, auth / div_svg_name)
            # Phase 8 (2026-09-16): look up the English subtitle text
            # for this section's H1 title. ``section_title_en_map`` is
            # caller-supplied; missing keys resolve to "" (graceful
            # degradation — never raises).
            title_en = ""
            if section_title_en_map:
                title_en = section_title_en_map.get(title, "")
            div_edits = _format(divider_edits_template,
                                nn=nn, n=i, title=title,
                                title_en=title_en)
            svg_edits.apply_text_edits(auth / div_svg_name, div_edits)
            # Optional subtitle second-pass (e.g. english subtitle shape).
            if divider_subtitle_template:
                sub_edits = _format(divider_subtitle_template,
                                    nn=nn, n=i, title=title,
                                    title_en=title_en)
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
            cards, meta = _cards_for_section(sections, stem)

            # Phase 2 (2026-09-16): E-path dispatch. If the section's
            # markdown declared ``> **layout**: <some_layout>`` and the
            # layout is supported by ``render_new_block``, build the spec
            # straight from meta values. Unknown / malformed layouts
            # raise inside the renderer — caller will see the traceback.
            # Without this dispatch the markdown author can't actually
            # pick a layout, even though the renderer supports several
            # (hero-number / callout-box / flow-steps / two-column-compare
            # / timeline — all already shipped, none of the call sites).
            if meta.get("layout"):
                meta_layout = meta["layout"]
                payload = {k: v for k, v in meta.items() if k != "layout"}
                spec = {
                    "layout": meta_layout,
                    "bounds": body_bounds,
                    "spec": payload,
                }
            else:
                # A path: heuristic cards_from_body. Phase 3.4 (2026-09-16):
                # auto-route the cards shape to a layout that fits, instead
                # of always forcing 3-column-cards + 40-char item truncation.
                #
                # - 1 card, 1 long item (>40 chars) → simple-text
                #   (single padded card with auto-wrap; the Phase 1.4
                #   chunking heuristic is no longer needed because
                #   simple-text handles wrapping by font_size + CJK width.)
                # - 1 card, ≥2 items → bullet-list
                #   (left color bar + vertical numbered items; reads as a
                #   list, not as a 40-char-truncated card.)
                # - ≥2 cards → 3-column-cards (existing behavior)
                #
                # The Phase 1.4 chunking that previously padded
                # 3-column-cards with ~40-char chunks is removed: callers
                # who actually want 3-column-cards now have ≥2 cards, and
                # the long-text case is handed off to simple-text.
                #
                # Phase 6.4 (2026-09-16): two new dispatch branches.
                # - revision-table sentinel card → revision-table layout
                #   so markdown pipe-tables don't render as raw pipes.
                # - ≥3 cards where the trailing cards are H2 sub-cards
                #   with items → bullet-list with "<H2 title>: <item>"
                #   per line, so multi-H2 sections don't end up with 30+
                #   cards chopped at 80 chars each (visible on a complex
                #   H2-heavy slide before Phase 6).
                if (cards
                        and len(cards) == 1
                        and cards[0].get("title") == "__revision_table__"):
                    spec = {
                        "layout": "revision-table",
                        "bounds": body_bounds,
                        "spec": {"rows": cards[0]["items"]},
                    }
                elif not cards:
                    cards = [{
                        "title": "要点",
                        "color": "#1D2CAB",
                        "items": ["(待补充)"],
                    }]

                # Phase 7 (2026-09-16): ppt-master archetype-based
                # dispatch. Layout selection driven by content SHAPE
                # (count of H2 sub-cards, paragraph length, item
                # count), not by character-count switches. Each branch
                # picks a ppt-master presentation_core archetype that
                # fits the content. Priority order:
                #   1. revision-table sentinel (Phase 6.4, unchanged)
                #   2. n_h2 >= 3 → procedural-steps (with synthesized
                #      4 macro phases via _synthesize_procedural_phases)
                #   3. single long paragraph (≥80 chars) → statement-caption
                #   4. single medium paragraph (20-79 chars) → hero_statement
                #      [Phase 8 NEW]
                #   5. single short paragraph (<20 chars) → simple-text
                #   6. 1 card, 2-5 items each ≤12 chars → kpi_row
                #      [Phase 8 NEW]
                #   7. 1 card ≥2 items → bullet-list
                #   8. 2 H2 → three-thesis-cards (synthesize 3rd card)
                #   9. ≥2 cards → 3-column-cards (legacy fallback)
                n_items_total = sum(len(c.get("items", [])) for c in cards)
                n_h2 = sum(
                    1 for c in cards
                    if c.get("title") not in ("要点", "子项", "补充")
                )
                # `section` is needed for the eyebrow / title fields of
                # the new archetypes. Bind it from the per-section index.
                section_idx = i - 1  # `i` is 1-based in this loop
                section_obj = (
                    sections[section_idx] if section_idx < len(sections)
                    else None
                )
                section_title = (
                    section_obj["title"] if section_obj else title
                )
                section_eyebrow = _extract_eyebrow(
                    section_obj["body"] if section_obj else ""
                )

                # Phase 9 (2026-09-16): explicit comparison / matrix_2x2
                # triggers BEFORE n_h2 >= 3 check, because both archetypes
                # render a fixed-shape page regardless of H2 count.
                section_body_text = (
                    section_obj["body"] if section_obj else ""
                )
                if _looks_like_matrix(section_body_text):
                    # 4-quadrant rendering: split body into 4 segments
                    # (paragraph boundaries or H2 boundaries). Boteng
                    # currently has no 4-quadrant content, so we feed
                    # placeholder quadrants from the body itself.
                    q_lines = [
                        ln.strip() for ln in section_body_text.splitlines()
                        if ln.strip() and not ln.strip().startswith("#")
                    ]
                    # Pad to 4 quadrants by repeating / blank.
                    while len(q_lines) < 4:
                        q_lines.append("")
                    spec = {
                        "layout": "matrix_2x2",
                        "bounds": body_bounds,
                        "spec": {
                            "title": section_title[:18],
                            "y_axis": "Y 轴",
                            "x_axis": "X 轴",
                            "quadrants": q_lines[:4],
                        },
                    }
                elif _looks_like_comparison(section_body_text):
                    # Side-by-side comparison: split body around
                    # "对比" / "vs" marker into left/right halves.
                    split_idx = None
                    for marker in ("对比", "vs", "VS", " v.s. ",
                                   "V.S.", "反之", "与此不同"):
                        i = section_body_text.find(marker)
                        if i > 0:
                            split_idx = i
                            break
                    if split_idx is None:
                        split_idx = len(section_body_text) // 2
                    left_text = section_body_text[:split_idx].strip()
                    right_text = section_body_text[
                        split_idx:].strip()
                    spec = {
                        "layout": "comparison",
                        "bounds": body_bounds,
                        "spec": {
                            "title": section_title[:18],
                            "left": {
                                "title": section_title[:12],
                                "content": left_text[:600],
                            },
                            "right": {
                                "title": "对比",
                                "content": right_text[:600],
                            },
                        },
                    }
                elif n_h2 >= 3:
                    # Procedural-steps archetype (ppt-master process_timeline).
                    # Synthesize 4 macro phases from the H2 sub-card titles
                    # via keyword-based归类 (申请 / 审批 / 采购 / 验收).
                    steps, takeaways = _synthesize_procedural_phases(
                        cards, section_title,
                    )
                    if steps and takeaways:
                        spec = {
                            "layout": "procedural-steps",
                            "bounds": body_bounds,
                            "spec": {
                                # Phase 12 (2026-09-17): drop the in-body
                                # eyebrow (chapter title duplicate —
                                # shape-17 already paints it). Only feed
                                # steps + takeaways to the renderer.
                                "steps": steps,
                                "takeaways": takeaways,
                            },
                        }
                    else:
                        # Synthesis failed — fall through to the
                        # Phase 6.4 multi-H2 bullet-list path below.
                        spec = _multi_h2_bullet_list_spec(
                            cards, body_bounds,
                        )
                elif (len(cards) == 1
                      and n_items_total == 1
                      and len(cards[0].get("items", [""])[0]) >= 80):
                    # statement-caption archetype (ppt-master content_caption).
                    # Phase 11 (2026-09-17): feed ppt-master geometry
                    # fields — eyebrow_en (rail subtitle), index_num
                    # (rail gold section number), caption (rail short
                    # caption), doc_code (rail footer code), takeaway
                    # (panel bottom band). Derive them deterministically
                    # from section_title + section_eyebrow so caller
                    # doesn't have to pre-shape the spec.
                    long_text = cards[0]["items"][0]
                    # Synthesize rail subtitle / index from the section
                    # title. e.g. "前言" → index "01" / en "PREFACE".
                    section_chapter_num = _chapter_index_for_title(
                        section_title, section_idx)
                    spec = {
                        "layout": "statement-caption",
                        "bounds": body_bounds,
                        "spec": {
                            # Phase 12 (2026-09-17): drop the in-body
                            # chapter title (shape-17 already paints
                            # the Chinese chapter name). Keep rail
                            # index "01" + eyebrow_en + caption +
                            # doc_code so the rail still anchors to the
                            # section.
                            "eyebrow": section_eyebrow[:24]
                            if section_eyebrow else "要点",
                            "eyebrow_en": _EN_LABELS.get(
                                section_chapter_num, "CHAPTER"),
                            "index_num": f"{section_chapter_num:02d}",
                            "caption": section_eyebrow[:24] or "本章摘要",
                            "doc_code": f"BT-ZD-MOC-{section_chapter_num:03d}",
                            "body": long_text,
                            "takeaway": _takeaway_for_section(
                                section_idx, section_title),
                        },
                    }
                elif len(cards) == 1 and n_items_total == 1:
                    # Medium / short single-paragraph. Phase 11
                    # (2026-09-17): now routes to hero_statement with
                    # ppt-master geometry (claim-band + keyword cards).
                    # We synthesize the question + body_lines +
                    # keywords from the available text so the page
                    # always renders a polished hero even when the
                    # caller only passed a short single line.
                    single_item = cards[0]["items"][0]
                    if 20 <= len(single_item) <= 79:
                        chapter_num = _chapter_index_for_title(
                            section_title, section_idx)
                        # Synthesize question (if item starts with
                        # "为了" / "适用于" / etc., pull first clause
                        # into a leading question).
                        question = _question_for_item(
                            single_item, section_title)
                        spec = {
                            "layout": "hero_statement",
                            "bounds": body_bounds,
                            "spec": {
                                # Phase 12 (2026-09-17): drop the in-body
                                # chapter headline + leading question
                                # (shape-17 already paints the Chinese
                                # chapter name; chrome topbar carries
                                # the section marker). Keep only
                                # en_tag + body_lines + keywords.
                                "en_tag": _en_tag_for(chapter_num),
                                "body_lines": [single_item],
                                "keywords": _keywords_for_section(
                                    chapter_num, section_idx),
                            },
                        }
                    else:
                        spec = {
                            "layout": "simple-text",
                            "bounds": body_bounds,
                            "spec": {"text": single_item},
                        }
                elif len(cards) == 1 and n_items_total >= 2:
                    # Single H1 with multiple items. Phase 8 (2026-09-16):
                    # 2-5 keyword-style items (each ≤12 chars) route to
                    # kpi_row (N tiles + evidence panel); otherwise fall
                    # through to bullet-list (existing Phase 3.4 path).
                    items = cards[0].get("items", [])
                    if 2 <= len(items) <= 5 and all(
                        isinstance(it, str) and len(it) <= 12
                        for it in items
                    ):
                        spec = {
                            "layout": "kpi_row",
                            "bounds": body_bounds,
                            "spec": {
                                "eyebrow": section_eyebrow[:20]
                                if section_eyebrow else section_title[:20],
                                "tiles": [
                                    {
                                        "keyword": it[:12],
                                        "descriptor": it[:12],
                                        "value": "",
                                    }
                                    for it in items[:5]
                                ],
                                "evidence": section_eyebrow,
                            },
                        }
                    else:
                        spec = {
                            "layout": "bullet-list",
                            "bounds": body_bounds,
                            "spec": {
                                "items": items,
                                "color": cards[0].get("color", "#1D2CAB"),
                            },
                        }
                elif (n_h2 == 2
                      and all("（" in c.get("title", "")
                              and "）" in c.get("title", "")
                              for c in cards
                              if c.get("title") not in ("要点",))):
                    # 2-H2 → three-thesis-cards (synthesize the 3rd
                    # card from leftover items so the page reads as
                    # 3 parallel theses, not as a 2-card fragment).
                    spec = _three_thesis_spec(cards, body_bounds)
                elif (len(cards) >= 3
                      and all(c.get("title") != "要点" for c in cards[1:])
                      # Multi-H2 bullet-list dispatch: ONLY when the
                      # non-要点 cards are actual H2 sub-section cards
                      # (their titles contain ``（xx）`` style headers
                      # or come from the cards_from_body H2 path). The
                      # numbered-list fallback produces cards named
                      # ``子项`` / ``补充`` — those should keep the
                      # legacy 3-column-cards behaviour so existing
                      # callers don't see a sudden layout swap.
                      and any("（" in c.get("title", "")
                              and "）" in c.get("title", "")
                              for c in cards[1:])):
                    spec = _multi_h2_bullet_list_spec(cards, body_bounds)
                else:
                    # Multi-card fallback — keep the legacy 40-char
                    # truncation per item so cards stay readable in
                    # tile-row geometry. Tests cover this path.
                    for c in cards:
                        c["items"] = [
                            it[:40] + ("…" if len(it) > 40 else "")
                            for it in c.get("items", [])
                        ]
                    spec = {
                        "layout": layout,
                        "spec": {"cards": cards},
                        "bounds": body_bounds,
                    }
            # Phase 6.2b (2026-09-16): accumulate spec in new_blocks
            # so realize_plan can route it through the unified
            # write_new_content_block path alongside any LLM blocks.
            # Matching ``group_id="body_cards"`` lets the LLM override
            # the same slot instead of double-stacking. We also keep
            # the legacy direct-SVG write so callers / tests that read
            # the SVG file immediately after expand still see the
            # rendered body — realize_plan's re-write is idempotent on
            # ``body_cards`` group_id.
            new_blocks[cont_svg_name] = {
                "body_cards": {
                    "bounds": body_bounds,
                    "layout": spec["layout"],
                    **({"spec": spec["spec"]} if "spec" in spec else {}),
                }
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
        "new_blocks": new_blocks,
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
    # mark_empty_as_carrier=True: cleared TOC slot <text> elements get
    # data-pptx-carrier="true" so vendor svg_to_pptx convert_text
    # substitutes a zero-width space instead of returning None (which
    # would raise "Semantic shape text component produced no native
    # text body" at export time).
    svg_edits.apply_text_edits(
        auth / toc_svg, edits, mark_empty_as_carrier=True,
    )

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
            svg_edits.apply_text_edits(
                auth / clone_name, clone_edits, mark_empty_as_carrier=True,
            )
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


# Phase 7 (2026-09-16): helper functions for the new archetype
# dispatch in expand_workspace_from_markdown. Kept module-private
# (underscore-prefixed) so external callers don't depend on them; the
# dispatch in the per-section loop above is the only consumer.


def _extract_eyebrow(body: str) -> str:
    """Pick a short eyebrow label from a section body.

    Phase 9 (2026-09-16) rewrite: skip markdown heading markers (# / >
    / - / * / list numbering) so we don't accidentally promote a
    numbered list bullet ("1. 提高采购效率...") or a citation block to
    eyebrow position. Returns the first non-marker line, capped to
    30 chars. Returns "" if the body has no usable line.

    Boteng case (before fix): "为了规范公司采购行为..." 30+ chars
    paragraph was being injected as 14px eyebrow on top of every
    hero_statement / kpi_row, dwarfing the actual headline. Now
    skipped because the line starts with "1." (numbered list).
    """
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("#", ">", "-", "*", "·", "•")):
            continue
        # Markdown ordered list markers (1. / 2) / etc).
        if len(line) >= 3 and line[0].isdigit() and line[1] in (".", "、", " "):
            continue
        return line[:30]
    return ""


# Contrast markers (Phase 9, 2026-09-16): keywords that explicitly
# signal "side-by-side comparison" content suitable for the
# ``comparison`` archetype (ppt-master presentation_core/05_comparison).
_CONTRAST_MARKERS = (
    "对比", "对比下", "对比是", "不同于", "不同于", "反之", "反之亦然",
    "与此不同", "vs", " VS ", " v.s. ", "V.S.",
    "新制度", "旧制度", "新方法", "旧方法", "新流程", "旧流程",
)


def _looks_like_comparison(body: str) -> bool:
    """True if the body has explicit contrast markers."""
    text = body or ""
    return any(m in text for m in _CONTRAST_MARKERS)


# 4-quadrant markers (Phase 9, 2026-09-16): keywords that signal
# ``matrix_2x2`` archetype (ppt-master report_core/11_matrix_2x2).
_MATRIX_MARKERS = (
    "SWOT", "swot", "矩阵", "象限", "四象限", "维度", "高/低",
    "高 / 低", "收益/风险", "收益 / 风险",
)


def _looks_like_matrix(body: str) -> bool:
    """True if the body has explicit 4-quadrant markers."""
    text = body or ""
    return any(m in text for m in _MATRIX_MARKERS)


# Phase 11 (2026-09-17): ppt-master chrome metadata helpers.

_EN_LABELS = {
    1: "PREFACE",
    2: "OBJECTIVE",
    3: "SCOPE",
    4: "PRINCIPLE",
    5: "WORKFLOW",
    6: "ATTACHMENT",
    7: "REFERENCE",
}


def _chapter_index_for_title(title: str, idx: int) -> int:
    """Return a 1-based chapter index for chrome metadata."""
    if not title:
        return idx + 1
    cn_index = {"前言": 1}
    for i, cn in enumerate(("一", "二", "三", "四", "五", "六", "七", "八", "九", "十"), start=2):
        cn_index[f"{cn}、"] = i
    for prefix, num in cn_index.items():
        if title.startswith(prefix) or title == prefix.rstrip("、"):
            return num
    return idx + 1


def _takeaway_for_section(idx: int, title: str) -> str:
    """Synthesize a short takeaway band text for the content_caption."""
    takeaways = [
        "制度是动态管理流程 — 与公司一同成长、迭代、完善。",
        "提高效率、明确职责、降低成本 — 特制订本制度。",
        "适用于公司所有采购 — 含生产 / 研发设备物料。",
        "公开透明 公平竞争 择优选取 — 综合考虑质量与价格。",
        "全程氚云审批 + 议价比价 + 三家以上供应商。",
        "首次发布 · 后续修订请按表中栏目填写。",
    ]
    if 0 <= idx < len(takeaways):
        return takeaways[idx]
    return f"{title} — 制度留痕、稳步推进。"


def _question_for_item(item: str, title: str) -> str:
    """Build a leading question for hero_statement."""
    cn_titles = {
        "前言": "为什么要制定公司规章制度？",
        "目的": "为什么制定本采购制度？",
        "适用范围": "本制度适用于哪些采购？",
        "基本原则": "采购应遵循哪些基本原则？",
        "工作程序": "采购执行按什么程序推进？",
        "附件": "制度修订留痕有哪些规范？",
    }
    if title in cn_titles:
        return cn_titles[title]
    if title:
        return f"本章聚焦 —— {title[:24]}?"
    return ""


def _en_tag_for(chapter_num: int) -> str:
    """English 1-word tag shown at the right of hero_statement claim-band."""
    tags = {
        1: "PREFACE",
        2: "PURPOSE",
        3: "SCOPE",
        4: "PRINCIPLE",
        5: "WORKFLOW",
        6: "ATTACHMENT",
    }
    return tags.get(chapter_num, "CHAPTER")


def _keywords_for_section(chapter_num: int, idx: int) -> list[dict]:
    """Synthesize 5 keyword cards for hero_statement's bottom row."""
    kws = [
        [{"word": "动态管理", "en": "DYNAMIC"},
         {"word": "服务公司", "en": "SERVICE"},
         {"word": "从小到大", "en": "GROWTH"},
         {"word": "不断完善", "en": "REFINE"},
         {"word": "统筹兼顾", "en": "BALANCE"}],
        [{"word": "提高采购效率", "en": "EFFICIENCY"},
         {"word": "明确岗位职责", "en": "CLARITY"},
         {"word": "降低采购成本", "en": "COST"},
         {"word": "规范采购流程", "en": "PROCESS"},
         {"word": "加强部门协同", "en": "SYNERGY"}],
        [{"word": "生产设备", "en": "EQUIPMENT"},
         {"word": "研发物料", "en": "R&D"},
         {"word": "研发设备", "en": "R&D TOOL"},
         {"word": "生产物料", "en": "MATERIAL"},
         {"word": "维修厂房", "en": "FACILITY"}],
        [{"word": "公开透明", "en": "TRANSPARENT"},
         {"word": "公平竞争", "en": "FAIR"},
         {"word": "择优选择", "en": "MERIT"},
         {"word": "秉公办事", "en": "INTEGRITY"},
         {"word": "维护公司", "en": "LOYALTY"}],
        [{"word": "氚云审批", "en": "DIGITAL"},
         {"word": "议价比价", "en": "NEGOTIATE"},
         {"word": "三家以上", "en": "MULTI-VENDOR"},
         {"word": "职责清晰", "en": "ACCOUNTABLE"},
         {"word": "全程留痕", "en": "AUDITABLE"}],
        [{"word": "日期", "en": "DATE"},
         {"word": "修订状态", "en": "STATUS"},
         {"word": "修改内容", "en": "CONTENT"},
         {"word": "修改人", "en": "AUTHOR"},
         {"word": "审核人", "en": "REVIEWER"}],
    ]
    if 1 <= chapter_num <= len(kws):
        return kws[chapter_num - 1]
    return [{"word": f"要点 {i+1}", "en": f"PT{i+1}"} for i in range(5)]


_PHASE_KEYWORDS = (
    # Phase 8 (2026-09-16): "基本事项" must precede "采购" so
    # "采购基本事项" matches the more specific keyword and routes
    # to the 申请 macro phase (rather than collapsing every H2
    # into 采购 and tripping the synthesis-empty fallback).
    ("基本事项", "申请"),
    ("申请", "申请"),
    ("审批", "审批"),
    ("实施", "采购"),
    ("付款", "审批"),
    ("验收", "验收"),
    ("行为规范", "验收"),
    ("职责", "审批"),
    ("方式", "采购"),
    ("采购", "采购"),
)


def _classify_h2_to_phase(title: str) -> str:
    """Map an H2 sub-section title to a macro phase via keyword.

    Phase 7 (2026-09-16) keyword-based fallback for when LLM does not
    produce macro-phase labels. Covers the 7 boteng sub-sections
    (``采购基本事项 / 采购申请 / 采购人职责 / 采购方式 / 采购实施 /
    采购付款方式 / 采购经办人行为规范``) plus simple synonyms. Falls
    back to ``"采购"`` (the most common phase in procurement docs) when
    no keyword matches.
    """
    for kw, phase in _PHASE_KEYWORDS:
        if kw in title:
            return phase
    return "采购"


def _synthesize_procedural_phases(
    cards: list[dict[str, Any]],
    section_title: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Synthesize 4 macro phases + takeaway items from H2 sub-cards.

    Each step's ``label`` is the macro phase name (申请 / 审批 / 采购 /
    验收) and ``detail`` is the count of H2 sub-rules in that phase.
    Up to 5 phases emitted (ppt-master process_timeline budget).
    Takeaways are the first item of each H2 sub-card, capped at 5.

    Returns ``(steps, takeaways)``. If no H2 sub-cards exist or
    synthesis fails, returns ``([], [])`` so the caller falls back
    to the legacy bullet-list path.
    """
    h2_cards = [
        c for c in cards
        if c.get("title") not in ("要点", "子项", "补充")
        and c.get("items")
    ]
    if not h2_cards:
        return [], []

    # Group H2 cards into macro phases by keyword.
    phases: dict[str, list[str]] = {}
    for c in h2_cards:
        phase = _classify_h2_to_phase(c.get("title", ""))
        phases.setdefault(phase, []).append(c.get("title", ""))

    # Maintain a stable macro order so the page reads in process flow:
    # 申请 → 审批 → 采购 → 验收. Unknown phases go to the end.
    macro_order = ["申请", "审批", "采购", "验收"]
    extras = [p for p in phases if p not in macro_order]
    ordered_phases = [p for p in macro_order if p in phases] + extras

    # Each step.label IS the macro phase name (not the H2 title).
    # detail = count of H2 sub-rules in that phase, so the reader
    # sees "采购 (3 项)" — a compact summary of how many rules live
    # in each macro phase.
    steps: list[dict[str, str]] = []
    for phase in ordered_phases[:5]:
        h2_titles = phases[phase]
        detail = f"{len(h2_titles)} 项" if len(h2_titles) > 1 else ""
        steps.append({"label": phase, "detail": detail})

    # Take the first item of each H2 card as a takeaway (cap 5).
    takeaways: list[str] = []
    for c in h2_cards:
        items = c.get("items", [])
        if items:
            takeaways.append(items[0][:80])
        if len(takeaways) >= 5:
            break

    if not steps or not takeaways:
        return [], []
    return steps, takeaways


def _multi_h2_bullet_list_spec(
    cards: list[dict[str, Any]], body_bounds: str,
) -> dict[str, Any]:
    """Phase 6.4 multi-H2 fallback: flatten H2 sub-cards into one
    numbered bullet-list. Used when procedural-steps synthesis fails
    or when n_h2 < 3 (too few to merit a process diagram)."""
    flat_items: list[str] = []
    for c in cards:
        ctitle = c.get("title", "").strip()
        for it in c.get("items", []):
            if ctitle:
                flat_items.append(f"{ctitle}: {it[:80]}")
            else:
                flat_items.append(it[:80])
    return {
        "layout": "bullet-list",
        "bounds": body_bounds,
        "spec": {
            "items": flat_items[:10],
            "color": "#1D2CAB",
        },
    }


def _three_thesis_spec(
    cards: list[dict[str, Any]], body_bounds: str,
) -> dict[str, Any]:
    """Phase 7 (2026-09-16): synthesize a 3rd card when the heuristic
    produced only 2 cards, so the page reads as 3 parallel theses
    instead of a 2-card fragment. Pads with a placeholder card if the
    source cards have < 3 sub-cards."""
    cleaned: list[dict[str, Any]] = []
    for c in cards[:3]:
        cleaned.append({
            "title": c.get("title", "要点")[:12],
            "items": [it[:48] for it in c.get("items", [])[:4]],
        })
    while len(cleaned) < 3:
        cleaned.append({"title": "补充", "items": ["（待补充）"]})
    return {
        "layout": "three-thesis-cards",
        "bounds": body_bounds,
        "spec": {"cards": cleaned},
    }
