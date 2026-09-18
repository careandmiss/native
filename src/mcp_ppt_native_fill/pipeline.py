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

from . import autofix, io_utils, runner, svg_edits, workspace_expand

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
    # the dataclass schema every release. ``llm_plan`` writes
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


def llm_plan(
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
    ``state.context["planner_result"]`` for phase2c to materialize.
    """
    state.stage = "llm_plan"
    workspace = state.workspace
    assert workspace is not None

    from . import llm_planner  # late import to avoid pulling HTTP deps

    try:
        # Phase 9 (2026-09-16): propagate caller-supplied
        # ``expand_layout_hints`` into the LLM planner so it can bias
        # toward new archetypes (hero_statement / kpi_row /
        # procedural-steps / etc.) instead of falling back to the
        # legacy callout-box / hero-number.
        layout_hints = (state.context or {}).get("llm_layout_hints")
        planner_result = llm_planner.plan_content_mapping(
            md_path=content_markdown,
            workspace=workspace,
            layout_hints=layout_hints,
        )
    except llm_planner.PlannerError as exc:
        # Phase 4 (2026-09-16): planner-side errors are recoverable.
        # Log as warning, fall through with empty content_mapping, let
        # the deterministic A-path / E-path dispatch + cover title
        # backfill produce a usable PPT without LLM assistance.
        state.warnings.append(f"llm_plan: planner error: {exc}")
        planner_result = llm_planner.PlannerResult(content_mapping={})
    except Exception as exc:  # network / LLMError etc.
        # Phase 4 (2026-09-16): transient network / DNS / provider
        # outages must not abort the whole pipeline. Demote to warning
        # so callers without network access (offline demos, air-gapped
        # build envs) still get a deterministic, content-aware PPT
        # generated from the markdown alone.
        state.warnings.append(f"llm_plan: {type(exc).__name__}: {exc}")
        planner_result = llm_planner.PlannerResult(content_mapping={})

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

    # Cover title backfill (Phase C, 2026-09-16). Deterministic
    # safety net: when neither the caller nor the LLM wrote to the
    # cover slide (probabilistic), we fill it from the markdown's H1
    # / filename. Runs unconditionally so empty-mapping fallbacks also
    # get the cover title. Only writes when the cover slide has zero
    # shape edits; if either side already filled it, we leave it alone.
    try:
        backfill = llm_planner.backfill_cover_title(
            merged_mapping=merged,
            workspace=workspace,
            md_text=content_markdown.read_text(encoding="utf-8"),
            md_path=content_markdown,
        )
        if backfill.get("filled"):
            state.warnings.append(
                f"llm_plan: cover title backfill wrote "
                f"{backfill['cover_svg']}:{backfill['shape_id']}="
                f"{backfill['title']!r} "
                f"(max_chars={backfill['max_chars']}, "
                f"truncated={backfill['truncated']})"
            )
        elif backfill.get("reason") not in (
            None, "cover_already_filled", "no_cover_slide",
        ):
            log.info(
                "llm_plan: cover title backfill skipped: %s",
                backfill["reason"],
            )
    except Exception as exc:
        # Backfill is best-effort; never fail the pipeline on this.
        log.warning("llm_plan: cover title backfill raised: %s", exc)

    state.context["content_mapping"] = merged
    state.context["planner_result"] = planner_result
    state.stage = "imported"
    return state


# TOC detection / cleanup / placeholder helpers moved to
# ``mcp_ppt_native_fill.toc_detection``. Imported below so existing
# call sites in pipeline.py keep their underscore-prefixed aliases.
from .toc_detection import (  # noqa: F401  (re-export for back-compat)
    TOC_PLACEHOLDER_PHRASES as _TOC_PLACEHOLDER_PHRASES,
    cards_for_section as _cards_for_section,
    cards_from_body as _cards_from_body,
    detect_toc_slot_shape_ids as _detect_toc_slot_shape_ids,
    _parse_markdown_table as _parse_markdown_table,
    fill_missing_content_blocks as _fill_missing_content_blocks,
    filter_toc_manual_mapping as _filter_toc_manual_mapping,
    find_toc_svg as _find_toc_svg,
    is_toc_slot_placeholder as _is_toc_slot_placeholder,
    remove_empty_toc_slots as _remove_empty_toc_slots,
    split_markdown_sections as _split_markdown_sections,
    strip_inline_markdown as _strip_inline_markdown,
)
# Block rendering (new_content_block spec → SVG) moved to
# ``mcp_ppt_native_fill.block_renderer``.
from .block_renderer import (  # noqa: F401  (re-export for back-compat)
    coerce_str_list as _coerce_str_list,
    escape as _escape,
    render_new_block as _render_new_block,
)
# Workspace expansion (cloning + smart TOC fill) moved to
# ``mcp_ppt_native_fill.workspace_expand``. Call sites in pipeline
# reference the module directly (``workspace_expand.<fn>``) to avoid
# recursion through the back-compat aliases.
# The two no-op / legacy stubs plus the public entry points are
# re-exported so external callers (tests, third-party code) keep
# working through ``pipeline.<name>``.
from .workspace_expand import (  # noqa: F401  (re-export for back-compat)
    apply_toc_deletion_marker as _apply_toc_deletion_marker,
    build_toc_phase3_edits as _build_toc_phase3_edits,
    build_toc_slot_edits as _build_toc_slot_edits,
    expand_workspace_from_markdown as expand_workspace_from_markdown,
    expand_workspace_from_toc as expand_workspace_from_toc,
    strip_toc_slot_g_elements as _strip_toc_slot_g_elements,
    toc_clone_basename as _toc_clone_basename,
    toc_deletion_marker_path as _toc_deletion_marker_path,
    toc_slide_number as _toc_slide_number,
)
from .workspace_expand import _EN_LABELS  # noqa: F401  (Phase 12 chrome topbar)


def _merge_new_blocks(
    caller_blocks: dict[str, dict[str, Any]],
    planner_blocks: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Merge caller-supplied ``new_blocks`` (from
    :func:`workspace_expand.expand_workspace_from_markdown`) and the
    planner/LLM-supplied ``new_blocks`` (from the Phase B planner).

    Phase 6.2b/6.4 (2026-09-16) unification: both sides use the same
    SVG-write path (:func:`svg_edits.write_new_content_block` inside
    :func:`realize_plan`). When caller and planner emit the SAME
    ``group_id`` (key under per-svg dict) for the same svg, planner
    wins — so the LLM can override caller A-path auto-fill instead of
    stacking a second card. Different ``group_id`` → both kept.
    """
    merged: dict[str, dict[str, Any]] = {}
    for svg_name, blocks in (caller_blocks or {}).items():
        merged[svg_name] = dict(blocks)
    for svg_name, blocks in (planner_blocks or {}).items():
        per_svg = merged.setdefault(svg_name, {})
        # Planner wins on per-group_id collision.
        per_svg.update(blocks)
    return merged


def _remove_existing_new_content_group(svg_path: Path, group_id: str) -> None:
    """Remove any existing top-level ``<g id="<group_id>">`` from an SVG.

    Phase 6.2b (2026-09-16): ``write_new_content_block`` appends rather
    than replaces — when the caller path already wrote a
    ``body_cards`` group, a later LLM re-write would produce a
    duplicate ``<g id="body_cards">`` and svg_to_pptx rejects the
    duplicate. Drop the existing same-id group before writing.
    Idempotent: no-op when no matching group exists.

    Uses regex on the raw text (no ElementTree) so it doesn't depend
    on svg_edits' private namespace helpers. The pattern is
    deliberately conservative: it only matches a ``<g>`` whose first
    attribute is ``id="<group_id>"`` so it never trims unrelated
    nested groups.
    """
    import re as _re
    raw = svg_path.read_text(encoding="utf-8")
    pattern = _re.compile(
        r'<g\s+id="' + _re.escape(group_id) + r'"[^>]*>.*?</g>',
        _re.DOTALL,
    )
    new_raw, n = pattern.subn("", raw, count=1)
    if n == 0:
        return
    tmp = svg_path.with_suffix(svg_path.suffix + ".tmp")
    tmp.write_text(new_raw, encoding="utf-8")
    tmp.replace(svg_path)


def _seed_original_roster(
    authoring_dir: Path,
    *,
    exclude: frozenset[str] = frozenset(),
    skeleton_kind: dict[str, str] | None = None,
    ending_last: bool = False,
) -> list[dict[str, Any]]:
    """Build a page_plan from the original ``slide_NN.svg`` skeletons.

    Used by ``realize_plan`` when no caller-supplied
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


def realize_plan(
    state: PipelineState,
    *,
    caller_page_plan: list[dict] | None = None,
    caller_new_blocks: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> PipelineState:
    """Phase 2c: materialize LLM planner output (Phase A expansion).

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

    # Phase 13 (2026-09-17): gate ppt-master archetype geometry per
    # caller request, and propagate the per-deck palette override.
    # Module globals in block_renderer are read inside render_new_block
    # dispatch. Defaults preserve Phase 12 behavior.
    opts = state.context.get("phase13_options") if isinstance(
        state.context, dict) else None
    if isinstance(opts, dict):
        from . import block_renderer as _br_mod
        _br_mod._ALLOW_PPT_MASTER_ARCHETYPES = bool(
            opts.get("enable_ppt_master_archetypes", True))
        _br_mod._PALETTE_OVERRIDE = opts.get("palette") or {}
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
            # mark_empty_as_carrier=True so re-cleared TOC slot <text>
            # elements (post-phase2_import overwrite) survive vendor
            # convert_text compilation. Only fires for new_text == "";
            # filled edits are unaffected. Idempotent — running again
            # just sets the same attr.
            audit = svg_edits.apply_text_edits(
                svg_path, edits, mark_empty_as_carrier=True,
            )
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
            # Belt-and-suspenders: reject new_blocks targeting the ending
            # skeleton. The LLM's correct path for trailing sections is
            # page_plan_additions, not new_blocks. The _normalize_new_blocks
            # guard already drops these, but defense-in-depth here protects
            # against future code paths that bypass the normalizer.
            _svg_skeleton_kind = (state.context.get("skeleton_kind") or {}).get(svg_name)
            if _svg_skeleton_kind == "ending":
                state.errors.append(
                    f"new_content_block skipped: svg={svg_name} has skeleton_kind="
                    f"ending (reserved for THANK YOU / chrome); use "
                    f"page_plan_additions for trailing sections"
                )
                log.warning(
                    "phase3: new_content_block skipped svg=%s shape=%s — "
                    "ending skeleton reserved for closing chrome",
                    svg_name, shape_id,
                )
                continue
            bounds = spec.get("bounds")
            if not bounds:
                state.warnings.append(
                    f"new_content_block svg={svg_name} shape={shape_id} "
                    f"missing bounds; skipping (would WARN at quality gate)"
                )
                continue
            # Phase 11+ (2026-09-17): dry-run render FIRST so we can
            # detect ValueError before deleting any existing body
            # group. If the LLM's render raises, we leave the
            # caller's ``body_cards`` in place so the slide isn't
            # blank. This corrects an earlier idempotency guard that
            # removed the existing same-id group before the render
            # attempt, which destroyed body_cards on failure paths.
            try:
                inner_svg = _render_new_block(spec)
            except ValueError as exc:
                # On render failure, preserve whatever group is
                # already there. body_cards (A-path) and content-body
                # (LLM override) are both kept; only the warning is
                # surfaced so the user knows the LLM's spec was bad.
                log.warning(
                    "phase3: render failed for %s shape=%s (%s); "
                    "leaving prior body group in place",
                    svg_name, shape_id, exc,
                )
                state.warnings.append(
                    f"LLM override render failed svg={svg_name} shape="
                    f"{shape_id}: {type(exc).__name__}: {exc} — "
                    f"prior body group kept"
                )
                continue  # skip writing; existing group stays
            # Render succeeded → now safe to drop the existing same-id
            # group (idempotency) and any body_cards anti-stack.
            try:
                _remove_existing_new_content_group(svg_path, shape_id)
            except Exception as exc:
                log.warning(
                    "phase3: remove_existing group failed svg=%s shape=%s: %s",
                    svg_name, shape_id, exc,
                )
            if shape_id == "content-body":
                try:
                    _remove_existing_new_content_group(
                        svg_path, "body_cards"
                    )
                except Exception as exc:
                    log.warning(
                        "phase3: remove caller body_cards failed "
                        "svg=%s: %s",
                        svg_name, exc,
                    )
            try:
                svg_edits.write_new_content_block(
                    svg_path,
                    group_id=shape_id,
                    bounds=bounds,
                    inner_svg=inner_svg,
                )
            except ValueError as exc:
                if shape_id == "content-body":
                    log.warning(
                        "phase3: LLM override write failed for %s (%s); "
                        "A-path body_cards preserved",
                        svg_name, exc,
                    )
                    state.warnings.append(
                        f"LLM override write failed svg={svg_name} shape="
                        f"{shape_id}: {type(exc).__name__}: {exc} — "
                        f"A-path body_cards kept"
                    )
                else:
                    state.errors.append(
                        f"new_content_block failed svg={svg_name} shape="
                        f"{shape_id}: {type(exc).__name__}: {exc}"
                    )

    # Phase 11 (2026-09-17): inject persistent chrome (topbar +
    # footer) on every content slide. We do it here (after the body
    # block has been written) so the chrome SVG fragment is added
    # atomically with the body, and the "PN / 07" page numbering
    # reads correctly in the final PPTX.
    _inject_content_chrome(state)

    state.stage = "authored"
    return state


def _inject_content_chrome(state: PipelineState) -> None:
    """Phase 11 helper: stamp ppt-master topbar/footer onto content slides.

    Walks every authoring SVG that already had a content-body write,
    and writes the chrome group with the right chapter label, doc
    path, and page number. Silently skips slide_01..05 (cover/TOC)
    because those have their own template chrome and shouldn't get
    an extra topbar/footer.

    Phase 13 (2026-09-17) — flexibility: when ``state.context[
    "phase13_options"]["enable_chrome_topbar"]`` is False, no
    topbar is injected; same for footer. When both are False, the
    function is a no-op (so templates that already have their own
    chrome keep their design intact). When the caller passes
    ``chrome_meta`` (list or dict) it overrides the default plan.
    The ``palette`` option propagates to ``render_chrome_topbar`` /
    ``render_chrome_footer`` for per-deck color customization.
    """
    if state.workspace is None:
        return
    from . import chrome as _chrome
    authoring_dir = state.workspace / "authoring-svg-flat"
    if not authoring_dir.is_dir():
        return
    # Phase 13 (2026-09-17): per-deck chrome/archetype toggles.
    opts = state.context.get("phase13_options") if isinstance(
        state.context, dict) else None
    if not isinstance(opts, dict):
        opts = {}
    enable_topbar = bool(opts.get("enable_chrome_topbar", True))
    enable_footer = bool(opts.get("enable_chrome_footer", True))
    if not (enable_topbar or enable_footer):
        # Chrome fully disabled — keep the template's own chrome intact.
        return
    palette = opts.get("palette") or {}
    plan = state.context.get("phase11_chrome_plan") if isinstance(
        state.context, dict) else None
    if plan is None:
        # Phase 11 default: derive per-slide chrome from the slide
        # filename. slide_partNN_* slides get the chapter label
        # derived from NN; cover/TOC slides get nothing.
        plan = _derive_default_chrome_plan(state)
        if isinstance(state.context, dict):
            state.context["phase11_chrome_plan"] = plan
    # Phase 13 (2026-09-17): if the caller supplied a chrome_meta
    # override, use it instead of (or merged with) the derived plan.
    chrome_meta_override = opts.get("chrome_meta")
    if chrome_meta_override is not None:
        plan = _resolve_chrome_meta(chrome_meta_override, state)
        if isinstance(state.context, dict):
            state.context["phase11_chrome_plan"] = plan
    # Phase 12 (2026-09-17): strip the template's corner tagline
    # (shape-22). The LLM previously wrote it; we now drop the
    # whole shape so chrome topbar doesn't fight with a second
    # heading in the same vertical band. shape-17 stays — it's
    # the only place the Chinese chapter name appears.
    #
    # Phase 13: only strip when the topbar is enabled. If the caller
    # has disabled the topbar, keep the template's own corner tagline.
    if enable_topbar:
        for entry in plan:
            if entry.get("skip"):
                continue
            svg_path = authoring_dir / entry["svg"]
            if not svg_path.is_file():
                continue
            try:
                removed = svg_edits.strip_template_chrome_shapes(
                    svg_path, shape_ids=("shape-22",))
                if removed:
                    log.info(
                        "phase12: stripped %d chrome shapes from %s",
                        removed, entry["svg"])
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "phase12: strip failed svg=%s: %s",
                    entry["svg"], exc)
    for entry in plan:
        if entry.get("skip"):
            continue
        svg_path = authoring_dir / entry["svg"]
        if not svg_path.is_file():
            continue
        # Build the chrome SVG. palette overrides the chrome color
        # constants when supplied.
        inner_parts: list[str] = []
        if enable_topbar:
            inner_parts.append(_chrome.render_chrome_topbar(
                chapter_label=entry["chapter_label"],
                brand_blue=palette.get("brand_blue", _chrome.BRAND_BLUE),
                gold=palette.get("gold", _chrome.GOLD),
                muted_ink=palette.get("muted_ink", _chrome.MUTED_INK),
            ))
        if enable_footer:
            inner_parts.append(_chrome.render_chrome_footer(
                doc_path=entry.get("doc_path",
                                   _chrome.chrome_meta_defaults()["doc_path"]),
                page_num=entry["page_num"],
                total_pages=entry["total_pages"],
                muted_ink=palette.get("muted_ink", _chrome.MUTED_INK),
            ))
        inner = "\n".join(inner_parts)
        if not inner:
            continue
        try:
            svg_edits.write_new_content_block(
                svg_path,
                group_id="page-chrome",
                bounds="0 0 1280 720",
                inner_svg=inner,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "phase11: chrome injection failed svg=%s: %s",
                entry["svg"], exc,
            )


def _derive_default_chrome_plan(state: PipelineState) -> list[dict]:
    """Build a chrome plan for boteng-like content slides.

    Each entry: {svg, chapter_label, doc_path, page_num, total_pages}.
    Cover (slide_01) and TOC slides (slide_02..03) are skipped.
    slide_partNN_content.svg get "PART N · 第N章 XXX" labels.
    """
    plan: list[dict] = []
    workspace = state.workspace
    if workspace is None:
        return plan
    authoring_dir = workspace / "authoring-svg-flat"
    if not authoring_dir.is_dir():
        return plan
    page_plan_path = workspace / "page_plan.json"
    page_plan: dict = {}
    if page_plan_path.is_file():
        try:
            import json
            page_plan = json.loads(page_plan_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            page_plan = {}
    pages = page_plan.get("pages") or []
    # Build a section_title lookup from the markdown if available.
    titles_by_index: dict[int, str] = {}
    titles_by_index[1] = "前言"
    titles_by_index[2] = "目的"
    titles_by_index[3] = "适用范围"
    titles_by_index[4] = "基本原则"
    titles_by_index[5] = "工作程序"
    titles_by_index[6] = "附件"
    # Find slide_partNN files in page order (excluding div.svg).
    part_files: list[tuple[int, Path]] = []
    for p in sorted(authoring_dir.glob("slide_part*_content.svg")):
        import re
        m = re.match(r"slide_part(\d+)_content\.svg", p.name)
        if m:
            part_files.append((int(m.group(1)), p))
    if not part_files:
        return plan
    total = len(part_files)
    for idx, p in part_files:
        title = titles_by_index.get(idx) or f"章节 {idx}"
        # Phase 12 (2026-09-17): the literal chapter name "第N章 XXX"
        # would duplicate shape-17 (37px brand-blue chapter title
        # painted by the boteng template's slide_04.svg clone). Drop
        # the Chinese suffix and let the topbar carry a "PART NN ·
        # EN_LABEL" header instead. shape-17 stays the single source
        # of truth for the Chinese chapter name.
        en_label = _EN_LABELS.get(idx, "CHAPTER")
        plan.append({
            "svg": p.name,
            "section_idx": idx,  # Phase 13: for chrome_meta dict merge
            "chapter_label": f"PART {idx:02d} · {en_label}",
            "doc_path": "采购制度 / 山西柏腾科技有限公司",
            "page_num": idx + 1,  # 1-based page (offset by cover/TOC)
            "total_pages": total + 2,  # + cover + TOC
        })
    return plan


def _resolve_chrome_meta(meta, state: PipelineState) -> list[dict]:
    """Resolve a caller-supplied ``chrome_meta`` into a chrome plan.

    Phase 13 (2026-09-17): two accepted forms:

    * ``list[dict]`` — each entry directly describes a slide:
      ``{svg, chapter_label, doc_path, page_num, total_pages, skip?}``.
    * ``dict`` — shorthand: ``{"base": {...}, "by_section":
      {section_idx: chapter_label, ...}}`` merged with the default
      plan produced by :func:`_derive_default_chrome_plan`.

    The dict form supports partial overrides: missing fields fall
    back to the default plan; ``by_section`` overrides only the
    ``chapter_label`` of matching sections; ``base`` (a dict) shallow-
    overrides the corresponding keys on every entry.

    Returns
    -------
    list[dict]
        Same shape as ``_derive_default_chrome_plan``.
    """
    if isinstance(meta, list):
        # List form: pass through, but backfill section_idx when caller
        # didn't supply it (tested by key, not relied on).
        out: list[dict] = []
        for entry in meta:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"chrome_meta list entries must be dict, got "
                    f"{type(entry).__name__}: {entry!r}")
            out.append(entry)
        return out
    if isinstance(meta, dict):
        # Dict form: merge with the default plan.
        base = {k: v for k, v in meta.items() if k != "by_section"}
        by_section = meta.get("by_section") or {}
        if not isinstance(by_section, dict):
            raise ValueError(
                f"chrome_meta.by_section must be dict, got "
                f"{type(by_section).__name__}")
        default_plan = _derive_default_chrome_plan(state)
        out = []
        for entry in default_plan:
            sec_idx = entry.get("section_idx")
            new_entry = {**entry, **base}
            if sec_idx is not None and sec_idx in by_section:
                new_entry["chapter_label"] = by_section[sec_idx]
            out.append(new_entry)
        # Allow extra entries in by_section that don't match any
        # default-plan section (caller-only slides, e.g. a closing).
        for sec_idx, label in by_section.items():
            try:
                idx_int = int(sec_idx)
            except (TypeError, ValueError):
                continue
            if any(e.get("section_idx") == idx_int for e in out):
                continue
            # Synthesize a minimal entry for the missing section.
            out.append({
                "svg": f"slide_part{int(sec_idx):02d}_content.svg",
                "section_idx": int(sec_idx),
                "chapter_label": label,
                "doc_path": base.get("doc_path",
                                      "采购制度 / 山西柏腾科技有限公司"),
                "page_num": int(sec_idx) + 1,
                "total_pages": (base.get("total_pages")
                                or len(default_plan) + 2),
            })
        return out
    raise ValueError(
        f"chrome_meta must be list[dict] or dict, got "
        f"{type(meta).__name__}")


def normalize_export_artifacts(
    state: PipelineState, *, source_pptx: Path | None = None,
    disabled: tuple[str, ...] = (),
) -> PipelineState:
    """Run pre-emptive fixes that don't depend on a quality-check failure.

    These run unconditionally after phase3 (edits) and before phase4
    (quality check). They handle authoring patterns svg_to_pptx refuses
    silently: picture-structure variants, gradient defs, non-PPT-safe
    font stacks, connector zero-stroke drops, invalid
    ``data-pptx-source-ref`` values. Without this step, an edited page
    whose only "edited" change is text can still fail phase5 export
    with ``Edited round-trip source object did not produce a DrawingML
    shape`` because the underlying picture/gradient/font/source-ref
    structure was never normalized.

    Three independent fix groups, each opt-out via the ``disabled``
    tuple:
      * ``render_compat``        — gradient / picture / font rewrites
      * ``connector_preserve``   — zero-stroke connector path rewriting
      * ``source_ref``           — invalid ``data-pptx-source-ref`` strip
    """
    workspace = state.workspace
    assert workspace is not None
    authoring_dir = workspace / "authoring-svg-flat"
    slide_files = sorted(authoring_dir.glob("slide_*.svg"))
    slide_files.extend(
        sorted(p for p in authoring_dir.glob("*.svg") if p not in slide_files)
    )

    # Wave 1 mtime heuristic: stamp function entry. Any SVG modified
    # within the last 0.5s before this point is overwhelmingly likely
    # to have just been written by phase2c (skeleton clone+edit) and
    # therefore must be treated as EDITED for source-ref stripping.
    # Falls back gracefully when callers (smart TOC, expand-from-md)
    # don't populate edited_svg_paths / content_mapping.
    phase_start = time.time()

    records: list[dict] = []

    if "render_compat" not in disabled:
        records.extend(
            _apply_render_compat_fixes(slide_files)
        )

    if "connector_preserve" not in disabled:
        records.extend(
            _apply_connector_preservation(slide_files)
        )

    valid_source_slides: set[int] = set()
    if "source_ref" not in disabled:
        if source_pptx is not None and source_pptx.is_file():
            valid_source_slides = _count_pptx_slides(source_pptx)
        if valid_source_slides:
            # Determine which slides need ALL of their
            # data-pptx-source-ref attrs stripped. svg_to_pptx tries to
            # byte-rehydrate every shape on a slide; if ANY ref points
            # at a source slide whose cNvPr.id doesn't actually exist
            # there (a known vendor round-trip bug for boteng slide_04
            # shape-2 → source slide 2 where id=2 is absent), the export
            # aborts with "Edited round-trip source object did not
            # produce a DrawingML shape: N".
            #
            # Two opt-in modes determine the strip_all set:
            #
            # A) Normal mode (render_compat ran in this function):
            #    edited_slides = edited_svg_paths ∪ content_mapping
            #    keys ∪ baseline-diff. The render_compat pass rewrote
            #    picture_structure / gradient / font, making the
            #    baseline slides' source-refs consistent. Only the
            #    framework-authored SVGs (clones, overflow, …) need
            #    refs stripped.
            #
            # B) Bypass mode (render_compat in `disabled`):
            #    The caller opted out of the picture_structure rewrite
            #    (boteng nested-SVG compatibility). The baseline
            #    SVGs' source-refs may be inconsistent with their
            #    rewritten geometry, so we MUST strip refs from EVERY
            #    SVG — the same semantics as the legacy `skip_phase3_5`
            #    bypass path. This matches the pre-refactor behavior
            #    where smart_toc_fill.py + skip_phase3_5=True worked.
            baseline = {f"slide_{n:02d}.svg" for n in valid_source_slides}
            baseline_diff = {
                f.name for f in slide_files if f.name not in baseline
            }
            if "render_compat" in disabled:
                # Bypass mode: caller has explicitly opted out of the
                # full autofix pipeline. Treat every workspace SVG as
                # needing source-ref stripping so svg_to_pptx falls
                # back to fresh render rather than byte-rehydration.
                edited_slides: set[str] = {f.name for f in slide_files}
                log.debug(
                    "normalize_export_artifacts: render_compat disabled "
                    "→ strip_all=True on %d slide(s)",
                    len(edited_slides),
                )
            else:
                # Normal mode: union of explicit edits and baseline diff.
                edited_slides = set()
                for e in state.context.get("edited_svg_paths", []):
                    edited_slides.add(e.split(":", 1)[0].strip())
                cm = state.context.get("content_mapping", {}) or {}
                edited_slides |= set(cm.keys())
                edited_slides |= baseline_diff
            records.extend(
                _apply_source_ref_normalization(
                    slide_files, valid_source_slides, edited_slides,
                )
            )

    state.fix_iterations.extend(records)
    if records:
        log.info("normalize_export_artifacts: %d action(s) applied", len(records))
    return state


def _apply_render_compat_fixes(slide_files: list[Path]) -> list[dict]:
    """SVG render-compat fixes: gradient / picture_structure / unsafe_font.

    Grouped because all three target the same SVG fidelity issue
    (vendor svg_to_pptx dropping / re-encoding certain constructs).
    """
    records: list[dict] = []
    for svg_path in slide_files:
        for fn in (
            autofix.fix_gradient_unexportable,
            autofix.fix_picture_structure,
            autofix.fix_unsafe_font,
        ):
            try:
                for rec in fn(svg_path):
                    records.append(rec.to_dict())
            except Exception as exc:
                log.warning(
                    "render_compat %s on %s failed: %s",
                    fn.__name__, svg_path.name, exc,
                )
    return records


def _apply_connector_preservation(slide_files: list[Path]) -> list[dict]:
    """Rewrite ``stroke-width=0`` on connector paths to ``1``.

    Vendor svg_to_pptx drops zero-stroke paths entirely, losing
    connector underlines. Isolated so callers can opt out without
    disabling other render-compat fixes.
    """
    records: list[dict] = []
    for svg_path in slide_files:
        try:
            for rec in autofix.fix_zero_stroke_connector(svg_path):
                records.append(rec.to_dict())
        except Exception as exc:
            log.warning(
                "connector_preserve fix_zero_stroke_connector on %s failed: %s",
                svg_path.name, exc,
            )
    return records


def _apply_source_ref_normalization(
    slide_files: list[Path],
    valid_source_slides: set[int],
    edited_slides: set[str],
) -> list[dict]:
    """Strip invalid ``data-pptx-source-ref`` attrs per edit state.

    For UNEDITED slides: only strip refs that point at non-existent
    source slides. Refs that point at real slides must be preserved
    so svg_to_pptx can passthrough the byte from source.

    For EDITED slides: strip EVERY data-pptx-source-ref. svg_to_pptx
    will render the whole slide fresh from SVG instead of trying to
    byte-rehydrate per element, which is what causes the "shape: N"
    export abort when ANY shape on the slide has changed but other
    refs still claim rehydration is possible.
    """
    records: list[dict] = []
    for svg_path in slide_files:
        try:
            strip_all = svg_path.name in edited_slides
            for rec in autofix.fix_invalid_source_ref(
                svg_path, valid_source_slides, strip_all=strip_all,
            ):
                records.append(rec.to_dict())
        except Exception as exc:
            log.warning(
                "source_ref fix_invalid_source_ref on %s failed: %s",
                svg_path.name, exc,
            )
    return records


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
    preflight_strict: bool | None = None,
) -> PipelineState:
    """Phase 4: refresh summary + quality check + optional auto-fix loop.

    ``strict=False`` treats quality-check WARN/ERROR as advisory — the
    pipeline records them in ``state.warnings`` / ``state.errors`` but
    advances to phase 5 anyway. Required for templates whose vendor
    quality checker emits WARN for non-blocking issues that don't
    actually block svg_to_pptx (e.g. boteng's "non-PPT-safe font" WARN).

    ``preflight_strict`` controls the PR-12 offline lint gate (P1-P4
    hard constraints from ppt-master Edit Native). Default ``None``
    inherits from ``strict``: callers using ``quality_strict=False``
    (boteng-style) also get advisory preflight, callers using
    ``quality_strict=True`` get fail-fast on P1-P3 violations.
    """
    state.stage = "quality"
    workspace = state.workspace
    assert workspace is not None
    authoring_dir = workspace / "authoring-svg-flat"

    # PR-12 (2026-09-17): offline lint against the 4 ppt-master Edit
    # Native hard constraints BEFORE the vendor quality check runs.
    # Catches duplicate-ids / missing picture shape-id / semantic
    # multi-text / single-child wrappers with a clear message instead
    # of letting vendor svg_to_pptx abort with cryptic errors.
    if preflight_strict is None:
        preflight_strict = strict
    try:
        from . import preflight_check as _preflight
        preflight_report = _preflight.scan_directory(authoring_dir)
    except FileNotFoundError:
        log.warning("phase4 preflight: authoring-svg-flat not found")
        preflight_report = None

    if preflight_report is not None:
        if preflight_report.errors:
            # Cap the message at 10 findings so a single bad template
            # doesn't produce a wall-of-text state.errors entry.
            detail_lines = [
                f"  {f.svg_file}:{f.element_id or '?'} "
                f"[{f.rule}] {f.detail}"
                for f in preflight_report.errors[:10]
            ]
            preflight_msg = (
                f"phase4 preflight: {len(preflight_report.errors)} "
                f"violation(s) in {preflight_report.svg_files_scanned} "
                f"SVG(s):\n" + "\n".join(detail_lines)
            )
            if preflight_strict:
                state.stage = "failed"
                state.errors.append(preflight_msg)
                log.error(preflight_msg)
                return state
            log.warning(preflight_msg)
            state.warnings.append(preflight_msg)
        for w in preflight_report.warnings:
            msg = (
                f"phase4 preflight warning {w.svg_file}:"
                f"{w.element_id or '?'} [{w.rule}] {w.detail}"
            )
            state.warnings.append(msg)
            log.warning(msg)

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
    render_previews: bool = False,
) -> PipelineState:
    """Phase 5: svg_to_pptx + delivery_check + source_to_md + optional
    PR-13 cairosvg preview rendering."""
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

    # PR-13 (2026-09-17): render authoring SVGs to PNG previews under
    # validation/diff/ for visual sanity checks. Opt-in: cheap (cairosvg
    # is fast), but some callers might prefer not to write PNGs to disk.
    if render_previews:
        try:
            from . import render_diff as _render_diff
            preview_report = _render_diff.render_svg_previews(workspace)
            state.last_delivery = state.last_delivery or {}
            state.last_delivery["preview_dir"] = preview_report.output_dir
            state.last_delivery["preview_pngs_written"] = (
                preview_report.pngs_written
            )
            state.last_delivery["preview_failure_count"] = (
                len(preview_report.failures)
            )
        except FileNotFoundError:
            log.warning("phase5 render_previews: workspace missing")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "phase5 render_previews failed: %s: %s",
                type(exc).__name__, exc,
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
    enable_llm_planner: bool = False,
    skip_phase3_5: bool = False,
    disabled_autofixes: tuple[str, ...] = (),
    quality_strict: bool = True,
    # PR-13 (2026-09-17): produce PNG previews of authoring SVGs at
    # validation/diff/ via cairosvg. Default False to avoid writing
    # PNGs that the caller didn't ask for; :func:`entry.generate_pptx`
    # flips it on by default.
    render_previews: bool = False,
    # PR-12 (2026-09-17): preflight (P1-P4 hard constraint) strictness.
    # None inherits from ``quality_strict``. False is the boteng default
    # (``vendor QC`` is advisory) — preflight violations become warnings.
    preflight_strict: bool | None = None,
    # Phase 9 (2026-09-16): layout hints forwarded to the LLM planner.
    llm_layout_hints: dict[str, Any] | None = None,
    # Phase 13 (2026-09-17): chrome/archetype flexibility options.
    # Defaults preserve Phase 12 behavior (chrome on, ppt-master
    # archetypes on, no new archetypes).
    enable_chrome_topbar: bool = True,
    enable_chrome_footer: bool = True,
    enable_ppt_master_archetypes: bool = True,
    enable_section_divider: bool = False,
    enable_closing_archetype: bool = False,
    chrome_meta: list[dict] | dict | None = None,
    palette: dict | None = None,
    archetype_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """End-to-end native_fill pipeline.

    Always returns a dict suitable as an MCP tool result. ``ok`` is True
    only when the final stage reaches ``done`` and validate_strict succeeded.

    ``skip_phase3_5`` (default False, deprecated) is equivalent to
    ``disabled_autofixes=('render_compat', 'connector_preserve',
    'source_ref')``. ``disabled_autofixes`` is the canonical per-fix
    opt-out (e.g. ``('render_compat',)`` to skip only the
    picture_structure rewrite). Set True / pass a non-empty tuple only
    for source templates whose nested picture form is mis-rehydrated
    (e.g. boteng slide_02/03). For normal templates the fixups protect
    against svg_to_pptx rejections.
    """
    if skip_phase3_5:
        # Deprecated alias. Merge into the canonical disabled tuple so
        # the downstream phase sees a single source of truth.
        merged: tuple[str, ...] = tuple(
            sorted(set(disabled_autofixes) | {
                "render_compat", "connector_preserve", "source_ref",
            })
        )
        disabled_autofixes = merged
    state = PipelineState(skill_dir=skill_dir)

    # Phase 13 (2026-09-17): record the chrome/archetype flexibility
    # options on state.context so downstream phases (_inject_content_chrome
    # + block_renderer dispatch) can read them. Defaults preserve Phase 12
    # behavior, so callers that don't pass new options see no change.
    state.context["phase13_options"] = {
        "enable_chrome_topbar": enable_chrome_topbar,
        "enable_chrome_footer": enable_chrome_footer,
        "enable_ppt_master_archetypes": enable_ppt_master_archetypes,
        "enable_section_divider": enable_section_divider,
        "enable_closing_archetype": enable_closing_archetype,
        "chrome_meta": chrome_meta,
        "palette": palette,
        "archetype_override": archetype_override,
    }

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

    # Phase 2b — optional LLM-driven content planning. Runs only when the
    # caller passed both `content_markdown` and `llm_plan=true`. The
    # LLM-derived mapping is merged into the caller-supplied
    # ``content_mapping`` (caller entries win on conflict).
    if content_markdown is not None and enable_llm_planner:
        # Phase 9 (2026-09-16): stash caller-supplied layout hints so
        # llm_plan → plan_content_mapping can bias toward new
        # archetypes.
        if llm_layout_hints:
            state.context["llm_layout_hints"] = llm_layout_hints
        state = llm_plan(state, content_markdown, content_mapping)
        if state.stage == "failed":
            return _finalize(state)
        # phase3_author reads the (now possibly merged) mapping from state.
        merged_mapping = state.context.get("content_mapping", content_mapping)
    else:
        merged_mapping = content_mapping

    # Phase 2c — materialize LLM planner output (Phase A expansion):
    # clone skeleton SVGs for page_plan_additions, register new_blocks.
    # Falls through as a no-op when the planner did not run.
    state = realize_plan(
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

    # Pre-export artifact normalization. Runs before quality-check so
    # the autofix loop in phase4 only has to deal with overflow /
    # viewBox / page_plan issues that depend on rendered metrics.
    #
    # Per-fix opt-out via ``disabled_autofixes`` (canonical) or the
    # deprecated ``skip_phase3_5=True`` boolean (merged into
    # disabled_autofixes above).
    if disabled_autofixes:
        log.info(
            "normalize_export_artifacts running with disabled=%s",
            list(disabled_autofixes),
        )
    state = normalize_export_artifacts(
        state, source_pptx=source_pptx, disabled=disabled_autofixes,
    )
    if state.stage == "failed":
        return _finalize(state)

    # Phase 4 (with auto-fix loop)
    state = phase4_quality(
        state,
        auto_fix=auto_fix,
        max_fix_iterations=max_fix_iterations,
        strict=quality_strict,
        preflight_strict=preflight_strict,
    )
    if state.stage == "failed":
        return _finalize(state)

    # Phase 5
    state = phase5_export(
        state,
        output_pptx=output_pptx,
        validate_strict=validate_strict,
        render_previews=render_previews,
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
    expand_section_title_en_map: dict[str, str] | None = None,
    # Phase 9 (2026-09-16): propagate layout_hints into the LLM
    # planner so it can prefer new archetypes (hero_statement /
    # kpi_row / procedural-steps / etc.).
    llm_layout_hints: dict[str, Any] | None = None,
    expand_exclude_source_slides: list[int] | None = None,
    # Workaround toggles (opt-in)
    fix_nested_picture: bool = False,
    skip_phase3_5: bool = False,
    disabled_autofixes: tuple[str, ...] = (),
    enable_llm_planner: bool = False,
    # Smart TOC fill (opt-in; auto-detects TOC slide from 目录/CONTENTS)
    expand_toc_from_markdown: bool = False,
    expand_toc_slot_grid: dict[str, Any] | None = None,
    # run_native_fill params
    auto_fix: bool = True,
    max_fix_iterations: int = 3,
    validate_strict: bool = True,
    quality_strict: bool = False,
    # PR-12 (2026-09-17): forwarded to run_native_fill. None means
    # inherit from quality_strict (i.e. advisory for boteng).
    preflight_strict: bool | None = None,
    # PR-13 (2026-09-17): forwarded to run_native_fill. Default False
    # here (low-level wrapper); :func:`entry.generate_pptx` flips on.
    render_previews: bool = False,
    clean_workspace: bool = False,
    # Phase 13 (2026-09-17): chrome/archetype flexibility options.
    # Forwarded verbatim to ``run_native_fill``; defaults preserve
    # Phase 12 behavior.
    enable_chrome_topbar: bool = True,
    enable_chrome_footer: bool = True,
    enable_ppt_master_archetypes: bool = True,
    enable_section_divider: bool = False,
    enable_closing_archetype: bool = False,
    chrome_meta: list[dict] | dict | None = None,
    palette: dict | None = None,
    archetype_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One-stop driver for the "manual mapping + markdown section cloning" workflow.

    Sequence:
      1. ``runner.run_pptx_to_svg`` → authoring-svg-flat/*.svg
      2. apply ``content_mapping`` text edits
      3. optional ``autofix.repair_nested_picture_attrs`` (boteng workaround)
      4. optional ``expand_workspace_from_markdown`` (per-section cloning)
      5. delegate the rest (page-plan re-apply, autofix loop, quality
         check, svg_to_pptx) to ``run_native_fill``

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
        Deprecated alias. Equivalent to passing
        ``disabled_autofixes=["render_compat"]``. Required for boteng
        slide_02/03; pairs with ``skip_phase3_5``.
    skip_phase3_5:
        Deprecated alias. Equivalent to bypassing the autofix loop
        entirely (use only when both ``fix_nested_picture=True`` AND a
        separate ``disabled_autofixes`` opt-in would be redundant).
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
        # mark_empty_as_carrier=True so re-cleared TOC slot <text>
        # elements (post-phase2_import overwrite) survive convert_text
        # compilation. The flag is no-op on filled edits — only fires
        # for new_text == "". Idempotent with the earlier
        # expand_workspace_from_toc pass which already marked the
        # cleared slots.
        audit = svg_edits.apply_text_edits(p, edits,
                                           mark_empty_as_carrier=True)
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
        expansions = workspace_expand.expand_workspace_from_markdown(
            workspace, content_markdown,
            skeleton_divider=expand_skeleton_divider,
            skeleton_content=expand_skeleton_content,
            divider_edits_template=expand_divider_edits_template,
            content_edits_template=expand_content_edits_template,
            body_bounds=expand_body_bounds,
            ending_svg=expand_ending_svg,
            part_names=expand_part_names,
            divider_subtitle_template=expand_divider_subtitle_template,
            section_title_en_map=expand_section_title_en_map,
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
        toc_summary = workspace_expand.expand_workspace_from_toc(
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
        toc_edits_for_phase3 = workspace_expand.build_toc_phase3_edits(
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
        # Phase 6.2b/6.4 (2026-09-16): merge caller-supplied
        # ``new_blocks`` (from expand_workspace_from_markdown's auto
        # routing) into the new_content_blocks the LLM also writes
        # into. Same ``body_cards`` group_id means the LLM can
        # override the caller slot instead of stacking a second card.
        new_content_blocks=_merge_new_blocks(
            expansions.get("new_blocks", {}),
            new_content_blocks,
        ),
        content_markdown=content_markdown,
        skill_dir=skill_dir,
        auto_fix=auto_fix,
        max_fix_iterations=max_fix_iterations,
        validate_strict=validate_strict,
        skip_phase3_5=skip_phase3_5,
        disabled_autofixes=disabled_autofixes,
        quality_strict=quality_strict,
        preflight_strict=preflight_strict,
        render_previews=render_previews,
        enable_llm_planner=enable_llm_planner,
        llm_layout_hints=llm_layout_hints,
        # Phase 13 (2026-09-17): forward chrome/archetype flexibility.
        enable_chrome_topbar=enable_chrome_topbar,
        enable_chrome_footer=enable_chrome_footer,
        enable_ppt_master_archetypes=enable_ppt_master_archetypes,
        enable_section_divider=enable_section_divider,
        enable_closing_archetype=enable_closing_archetype,
        chrome_meta=chrome_meta,
        palette=palette,
        archetype_override=archetype_override,
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



# ---------------------------------------------------------------------------# Caller supplies the slot shape-id lists (title + optional subtitle,
# row-major fill order). Function: auto-detects the TOC slide, fills
# < N slots and clears the rest, clones overflow into
# slide_partNN_toc.svg, and updates page_plan.json so clones follow the
# original TOC. Fully generic — no per-template shape ids, no font-size
# heuristics, no SVG parsing beyond the 目录/CONTENTS marker detection
# in :func:`_find_toc_svg`.
# ---------------------------------------------------------------------------


