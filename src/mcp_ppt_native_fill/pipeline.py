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

import logging
import re
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
    the configured LLM to derive ``content_mapping`` from the markdown
    + workspace summary. Caller-supplied entries WIN on conflict — the
    LLM only fills gaps where the caller didn't provide text.
    """
    state.stage = "llm_plan"
    workspace = state.workspace
    assert workspace is not None

    from . import llm_planner  # late import to avoid pulling HTTP deps

    try:
        llm_mapping = llm_planner.plan_content_mapping(
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

    if not llm_mapping:
        state.warnings.append(
            "llm_plan: model returned an empty mapping; "
            "falling back to caller-supplied content_mapping"
        )
        merged = dict(caller_mapping)
    else:
        merged: dict[str, dict[str, str]] = {}
        # Iterate the union of keys to preserve caller-only and LLM-only entries.
        for svg in set(caller_mapping) | set(llm_mapping):
            cm = caller_mapping.get(svg) or {}
            lm = llm_mapping.get(svg) or {}
            merged[svg] = {**lm, **cm}  # caller wins on key collision

        log.info(
            "llm_plan: caller slides=%d, llm slides=%d, merged slides=%d",
            len(caller_mapping),
            len(llm_mapping),
            len(merged),
        )
        state.warnings.append(
            f"llm_plan: derived {sum(len(v) for v in llm_mapping.values())} "
            f"shape edit(s) from {content_markdown.name}"
        )

    state.context["content_mapping"] = merged
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
) -> PipelineState:
    """Phase 4: refresh summary + quality check + optional auto-fix loop."""
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

    authoring_dir = workspace / "authoring-svg-flat"
    slide_files = sorted(authoring_dir.glob("slide_*.svg"))
    # also include any non-conforming clones (e.g. slide_part02_div.svg)
    slide_files.extend(sorted(p for p in authoring_dir.glob("*.svg") if p not in slide_files))

    if state.page_plan_pages if hasattr(state, "page_plan_pages") else None:
        # Already validated in phase3; refresh summary regardless.
        pass

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
) -> dict[str, Any]:
    """End-to-end native_fill pipeline.

    Always returns a dict suitable as an MCP tool result. ``ok`` is True
    only when the final stage reaches ``done`` and validate_strict succeeded.
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

    # Phase 3
    state = phase3_author(state, page_plan, merged_mapping, new_content_blocks)
    if state.stage == "failed":
        return _finalize(state)

    # Phase 3.5 — pre-emptive picture/gradient/font/source-ref normalization.
    # Runs before quality-check so the autofix loop in phase4 only has to
    # deal with overflow / viewBox / page_plan issues that depend on
    # rendered metrics.
    state = phase3_5_pre_export_fixes(state, source_pptx=source_pptx)
    if state.stage == "failed":
        return _finalize(state)

    # Phase 4 (with auto-fix loop)
    state = phase4_quality(
        state,
        auto_fix=auto_fix,
        max_fix_iterations=max_fix_iterations,
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

def _render_new_block(spec: dict[str, Any]) -> str:
    """Render a ``new_content_block`` spec into raw SVG children.

    Supports two layouts: ``"raw"`` (caller supplied SVG) and
    ``"3-column-cards"`` (renders a tile row per spec). Anything else raises.
    """
    layout = spec.get("layout", "raw")
    if layout == "raw":
        inner = spec.get("svg", "")
        if not inner:
            raise ValueError("layout='raw' requires spec.svg")
        return inner
    if layout == "3-column-cards":
        cards = spec.get("cards") or []
        if not 1 <= len(cards) <= 4:
            raise ValueError(
                "3-column-cards supports 1-4 cards per row"
            )
        parts: list[str] = []
        n = len(cards)
        # Geometry derived from bounds; we expect bounds "x y w h".
        bx, by, bw, bh = (float(t) for t in spec["bounds"].split())
        gap = 16.0
        card_w = (bw - gap * (n - 1)) / n
        for i, card in enumerate(cards):
            cx = bx + i * (card_w + gap)
            color = card.get("color", "#1D2CAB")
            title = card.get("title", "")
            items = card.get("items", [])
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
                parts.append(
                    f'<text x="{cx + 16:g}" y="{ty:g}" font-size="14" '
                    f'fill="#222">{_escape(item)}</text>'
                )
        return "\n".join(parts)
    raise ValueError(f"unsupported new_content_block layout: {layout!r}")


def _escape(text: str) -> str:
    """XML-escape a label for safe interpolation into SVG."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
