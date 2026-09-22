"""entry.py — one-shot wrapper: markdown + template → PPTX.

Provides :func:`generate_pptx` for callers who don't want to thread
``content_mapping`` / ``expand_*`` / ``enable_llm_planner`` themselves.
Defaults turn on every automation native_fill has:

  * LLM planner is mandatory (the markdown is the source of truth)
  * Smart TOC fill is auto-enabled with a 2×4 default grid
  * Chrome topbar + footer are auto-injected
  * ``workspace`` / ``output_pptx`` are auto-derived from the inputs

For low-level control (custom content mapping, deterministic / offline
runs, fixed TOC grid), call :func:`pipeline.run_with_mapping` directly.

PR-11 of the end-to-end protocol plan
(``docs/端到端协议计划书.md`` §4, §8).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .pipeline import run_with_pipeline

log = logging.getLogger(__name__)

# Default smart-TOC grid: 2 rows × 4 columns = 8 slots. Matches boteng's
# slide_02.svg layout. Override via ``hints={"toc_grid": {"rows": …,
# "cols": …, "subtitle_offset": …}}`` for templates with a different
# TOC geometry. If your template has more than 8 H1 sections,
# ``expand_workspace_from_toc`` will auto-clone a ``slide_partNN_toc.svg``
# for overflow — but the FIRST page must fit in this default.
DEFAULT_TOC_GRID: dict[str, dict[str, Any]] = {
    "rows": 2,
    "cols": 4,
    "subtitle_offset": 30,
}


def _default_workspace(md_path: Path, template_pptx_path: Path) -> Path:
    """Auto-derive a workspace directory from md + template paths.

    Layout::

        <template_dir>/workspace/<md_stem>_auto/

    The workspace lives next to the template so relative asset paths
    inside the template keep resolving during roundtrip.
    """
    md_stem = md_path.stem
    return (template_pptx_path.parent / "workspace" /
            f"{md_stem}_auto")


def _default_output_pptx(md_path: Path, workspace: Path) -> Path:
    """Auto-derive an output PPTX path with a timestamp suffix.

    Layout::

        <workspace_parent>/<md_stem>_<YYYYMMDD_HHMMSS>.pptx
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_stem = md_path.stem
    return workspace.parent / f"{md_stem}_{timestamp}.pptx"


def generate_pptx(
    *,
    md_path: Path,
    template_pptx_path: Path,
    skill_dir: Path,
    workspace: Path | None = None,
    output_pptx: Path | None = None,
    hints: dict[str, Any] | None = None,
    clean_workspace: bool = True,
    timeout_ms: int = 240_000,
) -> dict[str, Any]:
    """One-shot wrapper: markdown + template → PPTX.

    Parameters
    ----------
    md_path:
        Source markdown. H1 / H2 headings drive page structure.
    template_pptx_path:
        Source PPTX template. Native chrome / Master / shapes are
        preserved by ``ppt-master Edit Native PPTX`` roundtrip.
    skill_dir:
        ppt-master skill root (must contain
        ``scripts/pptx_to_svg.py`` and ``scripts/svg_to_pptx.py``).
    workspace:
        Authoring workspace root. Auto-derived to
        ``<template_dir>/workspace/<md_stem>_auto/`` if not given.
    output_pptx:
        Final PPTX. Auto-derived to
        ``<workspace_parent>/<md_stem>_<YYYYMMDD_HHMMSS>.pptx`` if
        not given.
    hints:
        Optional dict of feature toggles. Supported keys:

        * ``palette``              (dict)
        * ``chrome_meta``          (dict or list[dict])
        * ``archetype_override``   (dict[str, str])
        * ``llm_layout_hints``     (dict)
        * ``validate_strict``      (bool, default True)
        * ``quality_strict``       (bool, default False)
        * ``fix_nested_picture``   (bool, default False — boteng-only)
        * ``skip_phase3_5``        (bool, default False — deprecated)
        * ``disabled_autofixes``   (tuple[str, ...])
        * ``enable_chrome_topbar`` (bool, default True)
        * ``enable_chrome_footer`` (bool, default True)
        * ``render_previews``      (bool, default True — produce
          ``validation/diff/slide_NN.png`` via cairosvg; PR-13)
        * ``toc_grid``             (dict — override the default 2×4 grid;
          keys: ``rows``, ``cols``, ``subtitle_offset``)
        * ``preflight_strict``     (bool, default None — inherit from
          ``quality_strict``; PR-12 gate for P1-P4 hard constraints)
        * ``max_fix_iterations``   (int, default 3)

    clean_workspace:
        Wipe workspace before run. Default True (one-shot semantics
        expects a fresh start). Set False to resume an existing
        workspace.
    timeout_ms:
        Per-stage timeout. Default 240 s (covers LLM call + roundtrip
        + autofix loop + svg_to_pptx).

    Returns
    -------
    dict
        Same shape as :func:`pipeline.run_with_mapping`:

        ``{ok, stage, output_pptx, duration_ms, llm_content_mapping,
        export_summary, delivery, warnings, errors, ...}``

    Raises
    ------
    FileNotFoundError
        When ``md_path`` or ``template_pptx_path`` does not exist.

    Notes
    -----
    Requires the configured LLM provider to be reachable. If the LLM
    call fails, :func:`pipeline.llm_plan` swallows the error and
    continues with caller-supplied ``content_mapping`` (here, an empty
    dict) — the pipeline will still export a PPTX, but pages will be
    near-empty. For offline / deterministic runs, call
    :func:`pipeline.run_with_mapping` directly with
    ``enable_llm_planner=False`` and a hand-written ``content_mapping``.
    """
    md_path = Path(md_path).resolve()
    template_pptx_path = Path(template_pptx_path).resolve()
    skill_dir = Path(skill_dir).resolve()

    if workspace is None:
        workspace = _default_workspace(md_path, template_pptx_path)
    workspace = Path(workspace).resolve()

    if output_pptx is None:
        output_pptx = _default_output_pptx(md_path, workspace)
    output_pptx = Path(output_pptx).resolve()

    if not md_path.is_file():
        raise FileNotFoundError(f"md_path not found: {md_path}")
    if not template_pptx_path.is_file():
        raise FileNotFoundError(
            f"template_pptx_path not found: {template_pptx_path}"
        )

    hints = dict(hints or {})

    # Extract supported hint overrides with sensible defaults.
    palette = hints.get("palette")
    chrome_meta = hints.get("chrome_meta")
    archetype_override = hints.get("archetype_override")
    llm_layout_hints = hints.get("llm_layout_hints")
    validate_strict = bool(hints.get("validate_strict", True))
    quality_strict = bool(hints.get("quality_strict", False))
    fix_nested_picture = bool(hints.get("fix_nested_picture", False))
    skip_phase3_5 = bool(hints.get("skip_phase3_5", False))
    disabled_autofixes = tuple(hints.get("disabled_autofixes", ()))
    enable_chrome_topbar = bool(hints.get("enable_chrome_topbar", True))
    enable_chrome_footer = bool(hints.get("enable_chrome_footer", True))
    max_fix_iterations = int(hints.get("max_fix_iterations", 3))
    preflight_strict = hints.get("preflight_strict")
    render_previews = bool(hints.get("render_previews", True))
    # Phase 14+ (2026-09-18): per-archetype content skeleton pool. When
    # the caller provides this (e.g. from a v2 template cloned via
    # ``tools/clone_content_template.py``), ``archetype_meta`` routes
    # each page_plan_additions entry to the right source slide.
    content_skeleton_pool = hints.get("content_skeleton_pool")

    toc_grid_input = hints.get("toc_grid")
    if toc_grid_input is None:
        toc_grid: dict[str, Any] = dict(DEFAULT_TOC_GRID)
    elif isinstance(toc_grid_input, dict):
        toc_grid = {"rows": 2, "cols": 4, "subtitle_offset": 30}
        toc_grid.update(toc_grid_input)
    else:
        raise TypeError(
            f"hints['toc_grid'] must be a dict or None, got "
            f"{type(toc_grid_input).__name__}"
        )

    log.info(
        "generate_pptx: md=%s template=%s workspace=%s output=%s "
        "(enable_llm_planner=True, smart_toc=%dx%d)",
        md_path.name, template_pptx_path.name, workspace, output_pptx,
        toc_grid["rows"], toc_grid["cols"],
    )

    return run_with_pipeline(
        skill_dir=skill_dir,
        source_pptx=template_pptx_path,
        workspace=workspace,
        output_pptx=output_pptx,
        content_mapping={},
        content_markdown=md_path,
        page_plan=None,
        new_content_blocks=None,
        # expand_* no-ops when LLM planner is on — the planner produces
        # page_plan_additions + new_blocks directly. We pass None so
        # the deterministic expand_workspace_from_markdown branch in
        # run_with_mapping is skipped.
        expand_skeleton_divider=None,
        expand_skeleton_content=None,
        expand_divider_edits_template=None,
        expand_content_edits_template=None,
        expand_body_bounds="0 0 1280 720",
        expand_ending_svg=None,
        expand_part_names=None,
        expand_divider_subtitle_template=None,
        expand_section_title_en_map=None,
        llm_layout_hints=llm_layout_hints,
        expand_exclude_source_slides=None,
        fix_nested_picture=fix_nested_picture,
        skip_phase3_5=skip_phase3_5,
        disabled_autofixes=disabled_autofixes,
        # Mandatory for one-shot: LLM planner fills content_mapping,
        # page_plan_additions, new_blocks, and skeleton_kind.
        enable_llm_planner=True,
        # Auto smart-TOC fill (overridable via hints["toc_grid"]).
        expand_toc_from_markdown=True,
        expand_toc_slot_grid=toc_grid,
        auto_fix=True,
        max_fix_iterations=max_fix_iterations,
        validate_strict=validate_strict,
        quality_strict=quality_strict,
        preflight_strict=preflight_strict,
        render_previews=render_previews,
        clean_workspace=clean_workspace,
        content_skeleton_pool=content_skeleton_pool,
        enable_chrome_topbar=enable_chrome_topbar,
        enable_chrome_footer=enable_chrome_footer,
        enable_ppt_master_archetypes=True,
        enable_section_divider=False,
        enable_closing_archetype=False,
        chrome_meta=chrome_meta,
        palette=palette,
        archetype_override=archetype_override,
    )


__all__ = ["generate_pptx", "DEFAULT_TOC_GRID"]