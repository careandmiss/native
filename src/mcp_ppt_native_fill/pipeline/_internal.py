"""Phase 22 commit 6 (2026-09-20): internal phase3/4/5 + helpers.

Extracted from the legacy pipeline.py (now _internal.py).
Internal API only — DO NOT import from outside the pipeline sub-package.

Functions kept (with original line ranges in the legacy module):
  - write_page_plan (62-126)        -- Phase 3 page_plan.json writer
  - _merge_new_blocks + _remove_existing_new_content_group (298-348)
  - phase3_author (747-922)         -- Phase 3 main body
  - _inject_content_chrome + _apply_archetype_meta (925-1149)
  - phase4_quality (1557-1715) + phase5_export (1717-1822)
  - _finalize (2024-2065)

Removed (now in their own PipelineHandler classes):
  - phase2_import -> Phase2ImportHandler (commit 3)
  - llm_plan / realize_plan -> folded into MarkdownExpandHandler (commit 4)
  - run_native_fill / run_with_mapping -> Pipeline.run() + run_with_pipeline (commit 5)
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

from mcp_ppt_native_fill import (
    autofix, block_renderer, io_utils, runner, svg_edits,
    workspace_expand,
)
from mcp_ppt_native_fill.block_renderer import (
    render_new_block as _render_new_block,
)
from mcp_ppt_native_fill.toc_detection import (
    remove_empty_toc_slots as _remove_empty_toc_slots,
)
from mcp_ppt_native_fill.workspace_expand import intent_label_pair

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
    context: dict[str, Any] = field(default_factory=dict)

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)


# === _legacy.py lines 62-126 ===
def write_page_plan(workspace: Path, pages: list[dict]) -> Path:
    """Persist ``page_plan.json`` per master 搂4 schema.

    ``pages`` is a list of ``{"source_slide": int, "svg": str}``. Each svg
    filename must be unique and live under ``authoring-svg-flat/``.
    """
    if not pages:
        raise ValueError("page_plan.pages must not be empty")
    seen_svg: set[str] = set()
    for entry in pages:
        svg = entry.get("svg")
        source_slide_raw = entry.get("source_slide")
        if svg is None:
            if source_slide_raw is None:
                raise ValueError(
                    "page_plan entry has neither 'svg' nor 'source_slide'"
                )
            svg = f"slide_{int(source_slide_raw):02d}.svg"
        if svg in seen_svg:
            raise ValueError(f"duplicate svg filename in page_plan: {svg}")
        seen_svg.add(svg)
        if not (workspace / "authoring-svg-flat" / svg).is_file():
            raise ValueError(
                f"page_plan references missing svg: "
                f"authoring-svg-flat/{svg}"
            )
        if source_slide_raw is not None:
            source_slide = int(source_slide_raw)
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
    """Phase 2: PPTX 鈫?authoring-svg-flat workspace."""
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

# === _legacy.py lines 298-348 ===
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
    wins 鈥?so the LLM can override caller A-path auto-fill instead of
    stacking a second card. Different ``group_id`` 鈫?both kept.
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
    than replaces 鈥?when the caller path already wrote a
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
    tmp.unlink(missing_ok=True)

# === _legacy.py lines 747-922 ===
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
            # filled edits are unaffected. Idempotent 鈥?running again
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
                    "phase3: new_content_block skipped svg=%s shape=%s 鈥?"
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
                    f"{shape_id}: {type(exc).__name__}: {exc} 鈥?"
                    f"prior body group kept"
                )
                continue  # skip writing; existing group stays
            # Render succeeded 鈫?now safe to drop the existing same-id
            # group (idempotency) and any sibling body group.
            # Phase 19 P0-B-2 (2026-09-20): regardless of which
            # shape_id we are about to write, also drop the OTHER
            # body group (content-body 鈫?body_cards). Phase 18
            # cleanup was one-directional (content-body write 鈫?
            # body_cards drop) and let duplicate body groups slip
            # through whenever the LLM produced shape_id="body_cards"
            # on a slide where _fill_missing_content_blocks had
            # already emitted a content-body group. Visible symptom
            # on boteng: slide_part01_content.svg had two `<g
            # id="body_cards">` rendering the same paragraph twice.
            try:
                _remove_existing_new_content_group(svg_path, shape_id)
            except Exception as exc:
                log.warning(
                    "phase3: remove_existing group failed svg=%s shape=%s: %s",
                    svg_name, shape_id, exc,
                )
            sibling_ids = {"content-body", "body_cards"} - {shape_id}
            for sibling_id in sibling_ids:
                try:
                    _remove_existing_new_content_group(svg_path, sibling_id)
                except Exception as exc:
                    log.warning(
                        "phase3: remove sibling body group failed "
                        "svg=%s shape=%s sibling=%s: %s",
                        svg_name, shape_id, sibling_id, exc,
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
                        f"{shape_id}: {type(exc).__name__}: {exc} 鈥?"
                        f"A-path body_cards kept"
                    )
                else:
                    state.errors.append(
                        f"new_content_block failed svg={svg_name} shape="
                        f"{shape_id}: {type(exc).__name__}: {exc}"
                    )

    # Phase 14+ (2026-09-18): back-fill archetype meta fields via
    # relationships_detector before chrome injection so the suppression
    # policy has accurate inputs.
    _apply_archetype_meta(state)

    # Phase 11 (2026-09-17): inject persistent chrome (topbar +
    # footer) on every content slide. We do it here (after the body
    # block has been written) so the chrome SVG fragment is added
    # atomically with the body, and the "PN / 07" page numbering
    # reads correctly in the final PPTX.
    _inject_content_chrome(state)

    state.stage = "authored"
    return state

# === _legacy.py lines 925-1149 ===
def _inject_content_chrome(state: PipelineState) -> None:
    """Phase 11 helper: stamp ppt-master topbar/footer onto content slides.

    Walks every authoring SVG that already had a content-body write,
    and writes the chrome group with the right chapter label, doc
    path, and page number. Silently skips slide_01..05 (cover/TOC)
    because those have their own template chrome and shouldn't get
    an extra topbar/footer.

    Phase 13 (2026-09-17) 鈥?flexibility: when ``state.context[
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
    from .. import chrome as _chrome
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
        # Chrome fully disabled 鈥?keep the template's own chrome intact.
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
    # heading in the same vertical band. shape-17 stays 鈥?it's
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
        # Phase 14+ (2026-09-18): archetype-aware chrome suppression.
        # Hero archetypes (hero_statement / callout-box / statement-caption
        # / hero-number) suppress the topbar so big negative-space type is
        # not fighting a small PART XX label; footer stays for brand
        # consistency. ``archetype`` is set by _derive_default_chrome_plan
        # from the matching page_plan_additions entry's layout.
        archetype = entry.get("archetype") or entry.get("layout", "raw")
        rhythm = entry.get("page_rhythm")
        suppress_t, suppress_f, suppress_sd = _chrome.chrome_suppress_for(
            archetype, rhythm
        )
        # Build the chrome SVG. palette overrides the chrome color
        # constants when supplied.
        inner_parts: list[str] = []
        if enable_topbar and not suppress_t:
            inner_parts.append(_chrome.render_chrome_topbar(
                chapter_label=entry["chapter_label"],
                brand_blue=palette.get("brand_blue", _chrome.BRAND_BLUE),
                gold=palette.get("gold", _chrome.GOLD),
                muted_ink=palette.get("muted_ink", _chrome.MUTED_INK),
            ))
        if enable_footer and not suppress_f:
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


def _apply_archetype_meta(state: PipelineState) -> None:
    """Phase 14+ (2026-09-18): fill missing archetype_meta fields.

    Walks ``state.context["page_plan_pages"]`` and back-fills any of
    the four new fields (``archetype`` / ``relationships_atom`` /
    ``page_rhythm`` / ``reading_mode`` / ``composition_macro``) using
    :func:`relationships_detector.detect` against the entry's title +
    body text. Called after the LLM planner has produced its plan and
    before :func:`_inject_content_chrome` so the chrome suppression
    policy has accurate inputs.

    Behavior:
      * Missing ``archetype`` falls back to ``entry.get("layout",
        "raw")``.
      * Missing ``relationships_atom`` / ``page_rhythm`` come from
        :func:`relationships_detector.detect` with default confidence
        0.5 鈫?atom="none", rhythm="breathing".
      * Missing ``reading_mode`` defaults to ``"balanced"``.
      * Missing ``composition_macro`` defaults to ``None`` (free-form).
    """
    from ..relationships_detector import detect as _rd_detect
    from ..archetype_router import route_archetype as _ar_route
    pages = state.context.get("page_plan_pages") or []
    log.info("phase14 _apply_archetype_meta: %d page(s) in page_plan_pages", len(pages))
    # Phase 17-A (2026-09-18): build a lookup of planner new_blocks
    # so we can see the archetype the LLM actually picked (page_plan_pages
    # entries don't always carry the layout field 鈥?the LLM emits it
    # on new_blocks, not on the page_plan_additions). Without this
    # lookup, every entry looks like archetype=raw and the heuristic
    # over-fires on divider / cover / TOC slides.
    new_blocks_by_svg: dict[str, dict] = {}
    planner_result = state.context.get("planner_result")
    if planner_result is not None:
        for nb in (planner_result.new_blocks or []):
            if not isinstance(nb, dict):
                continue
            svg_name = nb.get("svg", "")
            if svg_name:
                new_blocks_by_svg[svg_name] = nb
    for entry in pages:
        if not isinstance(entry, dict) or not entry.get("svg"):
            continue
        svg_name = entry.get("svg", "")
        # Phase 17-A (2026-09-18): divider slides have no body
        # content (just chapter title); the heuristic would mis-classify
        # them as hero. Skip the override for divider/wrapper/cover
        # slides; only re-route content slides.
        if svg_name.endswith("_div.svg") or "_cover" in svg_name or "_toc" in svg_name:
            entry.setdefault("archetype", entry.get("layout", "raw"))
            entry.setdefault("relationships_atom", "none")
            entry.setdefault("page_rhythm", "breathing")
            entry.setdefault("reading_mode", "presentation")
            entry.setdefault("composition_macro", None)
            continue
        # Resolve the LLM's actual archetype choice from new_blocks
        # (page_plan_pages entries don't always carry ``layout``).
        nb_entry = new_blocks_by_svg.get(svg_name) or {}
        llm_archetype = (
            entry.get("archetype")
            or entry.get("layout")
            or nb_entry.get("layout")
            or nb_entry.get("archetype")
        )
        # run detector only if at least one of the 3 derived fields is missing
        needs_detect = any(
            k not in entry
            for k in ("relationships_atom", "page_rhythm")
        )
        text = (
            (entry.get("title") or "")
            + "\n"
            + (entry.get("body") or "")
        )
        if needs_detect:
            result = _rd_detect(text)
            entry.setdefault("relationships_atom", result["atom"])
            entry.setdefault("page_rhythm", result["suggested_rhythm"])
        # Phase 17-A (2026-09-18): auto-route archetype from content
        # shape when the LLM picked ``raw`` (no archetype) or when the
        # heuristic is high-confidence (>0.85). For hero archetypes we
        # trust the LLM 鈥?semantic choices the heuristic can't reliably
        # infer from text shape alone.
        if llm_archetype in (None, "raw", ""):
            result = _ar_route(text)
            entry["archetype"] = result["archetype"]
            log.info(
                "phase17a: %s archetype=raw 鈫?heuristic %s "
                "(conf=%.2f, %s)",
                entry.get("svg"), result["archetype"],
                result["confidence"], result["reason"],
            )
        else:
            entry["archetype"] = llm_archetype
        entry.setdefault("reading_mode", "balanced")
        entry.setdefault("composition_macro", None)


# === _legacy.py lines 1557-1822 ===
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

    ``strict=False`` treats quality-check WARN/ERROR as advisory 鈥?the
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

    # Pre-repair vendor XML bugs (duplicate attributes, unescaped inner
    # quotes) on every SVG in the workspace so that preflight (and the
    # vendor quality check) don't choke on them. Run this BEFORE the
    # preflight scan so ET.parse sees well-formed XML.
    repaired = autofix.repair_workspace_svgs(authoring_dir)
    if repaired:
        log.info("phase4 pre-repair: fixed %d SVG file(s)", repaired)
    # Restore data-pptx-* attrs that the XML repair (or any earlier edit)
    # may have dropped. The snapshot was taken at the end of phase2.
    restored = autofix.restore_shape_attrs(authoring_dir)
    if restored:
        log.info("phase4 pre-export: restored data-pptx-* attrs on %d svg(s)", restored)

    try:
        from .. import preflight_check as _preflight
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
    # (svg_quality_checker, svg_to_pptx) don't choke on them. This now
    # runs BEFORE the preflight scan (see above) so ET.parse sees
    # well-formed XML.
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

    # Phase 5b 鈥?delivery check.
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

    # Phase 5c 鈥?readback.md.
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
            from .. import render_diff as _render_diff
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

# === _legacy.py lines 2024-2065 ===
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
# fully generic 鈥?no hardcoded boteng shape ids / section names /
# body bounds. All template-specific bits are caller-supplied.
# ---------------------------------------------------------------------------


# === _legacy.py lines 353-440 (_seed_original_roster helper, ===
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
    pages 鈥?otherwise they vanish from the export and the user sees
    "no cover, blank content".

    ``ending_last=True`` (Phase B, Bug 1 fix): when set, identify the
    ``ending`` skeleton and append it AFTER all other originals instead
    of letting ``source_slide`` ordering place it in the middle.
    ``skeleton_kind`` is consulted first (mapping ``"slide_NN.svg" 鈫?
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
    #   1. skeleton_kind[name] == "ending" 鈥?planner already labelled it
    #   2. body text contains THANK YOU / 璋㈣阿 / Q&A 鈥?semantic signal
    #   3. filename matches thank|ending|closing 鈥?defensive filename match
    #   4. last by source_slide 鈥?last-resort fallback
    ending_idx: int | None = None
    if ending_last and candidates:
        for idx, (_, name) in enumerate(candidates):
            if skeleton_kind and skeleton_kind.get(name) == "ending":
                ending_idx = idx
                break
        if ending_idx is None:
            # Bug 08 fix: scan SVG bodies for ending markers (THANK YOU /
            # 璋㈣阿 / Q&A). Templates that append an appendix slide at a
            # higher source_slide than the actual ending would otherwise
            # mis-pick the appendix as ending.
            ending_keywords = ("THANK", "璋㈣阿", "Q&A", "绛旂枒", "鍐嶈")
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

# === _legacy.py lines 1151-1346 (chrome_meta helpers) ===

def _derive_default_chrome_plan(state: PipelineState) -> list[dict]:
    """Build a chrome plan for boteng-like content slides.

    Each entry: {svg, chapter_label, doc_path, page_num, total_pages,
    archetype, relationships_atom, page_rhythm, reading_mode,
    composition_macro}.
    Cover (slide_01) and TOC slides (slide_02..03) are skipped.
    slide_partNN_content.svg get "PART N 路 绗琋绔?XXX" labels.
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
    titles_by_index[1] = "鍓嶈█"
    titles_by_index[2] = "鐩殑"
    titles_by_index[3] = "閫傜敤鑼冨洿"
    titles_by_index[4] = "鍩烘湰鍘熷垯"
    titles_by_index[5] = "宸ヤ綔绋嬪簭"
    titles_by_index[6] = "闄勪欢"
    # Find slide_partNN files in page order (excluding div.svg).
    # Phase 15+ (2026-09-18): also accept _b/_c/_d suffix variants
    # (e.g. slide_part04b_content.svg) so split-content pages get
    # chrome injected. Without the suffix-aware regex, _b/_c/_d slides
    # ship without topbar/footer.
    part_files: list[tuple[int, Path]] = []
    for p in sorted(authoring_dir.glob("slide_part*_content.svg")):
        import re
        m = re.match(r"slide_part(\d+)([a-z]?)_content\.svg", p.name)
        if m:
            part_files.append((int(m.group(1)), p))
    if not part_files:
        return plan
    total = len(part_files)
    # Phase 14+ (2026-09-18): build a lookup of page_plan_additions
    # so we can propagate archetype / relationships_atom / page_rhythm /
    # reading_mode / composition_macro from the planner into each
    # chrome plan entry. _inject_content_chrome then consults
    # chrome_suppress_for to decide whether to render the topbar.
    additions_by_svg: dict[str, dict] = {}
    new_blocks_by_svg: dict[str, dict] = {}
    planner_result = state.context.get("planner_result")
    if planner_result is not None:
        for pea in planner_result.page_plan_additions:
            svg_name = pea.get("svg", "")
            if svg_name:
                additions_by_svg[svg_name] = pea
        # Phase 14+ (2026-09-18): the planner emits the layout per
        # `new_blocks` entry (one per svg). _derive_default_chrome_plan
        # used to only look at page_plan_additions.layout 鈥?which the
        # LLM does not always fill. That meant chrome_suppress_for
        # got archetype="raw" for hero layouts (callout-box /
        # hero_statement / statement-caption) and rendered the topbar
        # when it should have been suppressed. Walk new_blocks once
        # to build a layout lookup mirroring realize_plan (line 547-560).
        for nb in (planner_result.new_blocks or []):
            if not isinstance(nb, dict):
                continue
            svg_name = nb.get("svg", "")
            if svg_name:
                new_blocks_by_svg[svg_name] = nb

    for idx, p in part_files:
        title = titles_by_index.get(idx) or f"绔犺妭 {idx}"
        # Phase 12 (2026-09-17): the literal chapter name "绗琋绔?XXX"
        # would duplicate shape-17 (37px brand-blue chapter title
        # painted by the boteng template's slide_04.svg clone). Drop
        # the Chinese suffix and let the topbar carry a "PART NN 路
        # EN_LABEL" header instead. shape-17 stays the single source
        # of truth for the Chinese chapter name.
        # Phase 19 P0-B-1 (2026-09-20): use section_title-driven intent
        # instead of position-cycled _EN_LABELS.get(idx, "CHAPTER").
        # detect_section_intent + intent_label_pair live in
        # workspace_expand.py:1072 / :1091 (landed in commit 4847a67
        # as P0-B-0). The legacy _EN_LABELS shim is retained (see
        # workspace_expand.py:1110-1130) only for any future import-
        # level callers; nothing else reads it now.
        en_label, _body_label = intent_label_pair(title)
        # Phase 14+: pull archetype meta fields from the planner's
        # matching page_plan_additions entry (if any). Layout
        # resolution order matches realize_plan so chrome suppression
        # sees the same archetype the page actually rendered.
        pea = additions_by_svg.get(p.name) or {}
        pea_edits = pea.get("edits") or {}
        nb_entry = new_blocks_by_svg.get(p.name) or {}
        archetype = (
            pea_edits.get("layout")
            or pea.get("layout")
            or nb_entry.get("layout")
            or nb_entry.get("archetype")
            or "raw"
        )
        plan.append({
            "svg": p.name,
            "section_idx": idx,  # Phase 13: for chrome_meta dict merge
            "chapter_label": f"PART {idx:02d} 路 {en_label}",
            "doc_path": "閲囪喘鍒跺害 / 灞辫タ鏌忚吘绉戞妧鏈夐檺鍏徃",
            "page_num": idx + 1,  # 1-based page (offset by cover/TOC)
            "total_pages": total + 2,  # + cover + TOC
            # Phase 14+: archetype meta fields propagated from planner.
            "archetype": archetype,
            "relationships_atom": pea.get("relationships_atom", "none"),
            "page_rhythm": pea.get("page_rhythm", "dense"),
            "reading_mode": pea.get("reading_mode", "balanced"),
            "composition_macro": pea.get("composition_macro"),
        })
    return plan


def _resolve_chrome_meta(meta, state: PipelineState) -> list[dict]:
    """Resolve a caller-supplied ``chrome_meta`` into a chrome plan.

    Phase 13 (2026-09-17): two accepted forms:

    * ``list[dict]`` 鈥?each entry directly describes a slide:
      ``{svg, chapter_label, doc_path, page_num, total_pages, skip?}``.
    * ``dict`` 鈥?shorthand: ``{"base": {...}, "by_section":
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
                                      "閲囪喘鍒跺害 / 灞辫タ鏌忚吘绉戞妧鏈夐檺鍏徃"),
                "page_num": int(sec_idx) + 1,
                "total_pages": (base.get("total_pages")
                                or len(default_plan) + 2),
            })
        return out
    raise ValueError(
        f"chrome_meta must be list[dict] or dict, got "
        f"{type(meta).__name__}")

