"""Phase 22 commit 2 (2026-09-20): Pipeline orchestrator.

Provides the :class:`Pipeline` class that runs a list of
:class:`PipelineHandler` instances in order. Replaces the legacy
``pipeline.run_native_fill`` (a 400-line function that mixed
phase 1-5 logic) with a small declarative orchestrator.

Design patterns:
    - **Pipeline Pattern** (GoF "Chain of Responsibility" cousin):
      handlers run sequentially, each transforming a shared
      ``PipelineContext``.
    - **Factory Method**: ``Pipeline.DEFAULT_HANDLERS`` is a
      registry-style factory listing the canonical handler chain.
    - **Chain of Responsibility** (degenerate): two skip mechanisms
      (``skip_handlers`` set + ``handler.skip()`` method) make the
      chain semi-skippable.

Lifecycle (called by ``Pipeline.run``):
    1. For each handler in order:
       a. If handler.name in ctx.skip_handlers 鈫?skip.
       b. If handler.skip(ctx) returns True 鈫?skip.
       c. Run handler.run(ctx). On success, continue.
       d. On PipelineError: mark state=failed, call
          handler.on_failure(ctx, exc), break.
       e. If ctx.stop_after == handler.name 鈫?break (debug hook).
    2. Return the (possibly mutated) context.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from .context import PipelineContext, PipelineError, PipelineHandler
from .handlers import (
    MarkdownExpandHandler,
    Phase2ImportHandler,
    Phase3AuthorHandler,
    Phase4QualityHandler,
    Phase5ExportHandler,
)

log = logging.getLogger("mcp_ppt_native_fill.pipeline.orchestrator")


class Pipeline:
    """Runs a list of PipelineHandlers in sequence.

    Default handler chain is the canonical phase 1-5 sequence
    (registered in commit 5). Callers may override with a custom
    list for testing or partial pipelines.

    Example::

        pipeline = Pipeline([
            Phase2ImportHandler(),
            MarkdownExpandHandler(),
            Phase3AuthorHandler(),
        ])
        result_ctx = pipeline.run(ctx)
        if result_ctx.state.stage == "failed":
            ...
    """

    DEFAULT_HANDLERS: ClassVar[list[type[PipelineHandler]]] = [
        Phase2ImportHandler,
        MarkdownExpandHandler,
        Phase3AuthorHandler,
        Phase4QualityHandler,
        Phase5ExportHandler,
    ]

    def __init__(self, handlers: list[PipelineHandler] | None = None):
        if handlers is None:
            handlers = [h() for h in self.DEFAULT_HANDLERS]
        # If a caller passed a class (not an instance), instantiate
        # it. The common case is ``Pipeline([A, B, C])`` where A, B,
        # C are already instances.
        self.handlers: list[PipelineHandler] = [
            h() if isinstance(h, type) else h for h in handlers
        ]

    def add_handler(self, handler: PipelineHandler, position: int | None = None) -> None:
        """Append (or insert at ``position``) a handler.

        Useful for callers who want to inject a custom handler
        without subclassing Pipeline.
        """
        if position is None:
            self.handlers.append(handler)
        else:
            self.handlers.insert(position, handler)

    def run(self, ctx: PipelineContext) -> PipelineContext:
        """Run the handler chain. See module docstring for lifecycle."""
        for h in self.handlers:
            if h.name in ctx.skip_handlers:
                log.info("pipeline: skipping %s (skip_handlers)", h.name)
                continue
            if h.skip(ctx):
                log.info("pipeline: skipping %s (skip()=True)", h.name)
                continue
            log.info("pipeline: running %s", h.name)
            try:
                ctx = h.run(ctx)
            except PipelineError as exc:
                log.warning("pipeline: %s failed: %s", h.name, exc)
                ctx.state.stage = "failed"
                ctx.state.errors.append(f"{h.name}: {exc}")
                try:
                    h.on_failure(ctx, exc)
                except Exception as cleanup_exc:  # noqa: BLE001
                    log.warning(
                        "pipeline: %s.on_failure raised: %s",
                        h.name, cleanup_exc,
                    )
                break
            if ctx.stop_after == h.name:
                log.info("pipeline: stopped after %s per stop_after", h.name)
                break
        return ctx


__all__ = ["Pipeline", "run_with_pipeline"]


def run_with_pipeline(
    *,
    skill_dir,
    source_pptx,
    workspace,
    output_pptx,
    content_mapping,
    new_content_blocks=None,
    page_plan=None,
    content_markdown=None,
    enable_llm_planner=False,
    inheritance_mode="both",
    skip_phase3_5=False,
    disabled_autofixes=(),
    auto_fix=True,
    max_fix_iterations=3,
    validate_strict=True,
    quality_strict=False,
    preflight_strict=None,
    render_previews=False,
    llm_layout_hints=None,
    enable_chrome_topbar=True,
    enable_chrome_footer=True,
    enable_ppt_master_archetypes=True,
    expand_skeleton_divider=None,
    expand_skeleton_content=None,
    expand_divider_edits_template=None,
    expand_content_edits_template=None,
    expand_body_bounds="0 0 1280 720",
    expand_ending_svg=None,
    expand_part_names=None,
    expand_divider_subtitle_template=None,
    expand_section_title_en_map=None,
    expand_exclude_source_slides=None,
    expand_toc_from_markdown=False,
    expand_toc_slot_grid=None,
    fix_nested_picture=False,
    clean_workspace=False,
) -> dict[str, Any]:
    """Top-level Pipeline entry point. Construct PipelineContext
    from input kwargs, run the default handler chain, return the
    final response dict.

    This is the Phase 22 Pipeline Pattern replacement for the
    legacy ``pipeline.run_with_mapping`` (a 400-line function that
    manually inlined phases 1-5). It runs vendor exactly once per
    call (Phase2ImportHandler is the only handler that spawns
    vendor), eliminating the Phase 21 ``.publish-{hash}/`` file-lock
    bug.

    server.py will switch to this entry point at commit 6
    (``run_with_pipeline`` replaces ``run_with_mapping`` in the
    ``_execute_native_fill`` dispatch).
    """
    from .context import PipelineContext
    from ..pipeline import PipelineState  # parent package

    state = PipelineState()
    options = {
        "inheritance_mode": inheritance_mode,
        "enable_llm_planner": enable_llm_planner,
        "skip_phase3_5": skip_phase3_5,
        "disabled_autofixes": disabled_autofixes,
        "auto_fix": auto_fix,
        "max_fix_iterations": max_fix_iterations,
        "validate_strict": validate_strict,
        "quality_strict": quality_strict,
        "preflight_strict": preflight_strict,
        "render_previews": render_previews,
        "llm_layout_hints": llm_layout_hints,
        "enable_chrome_topbar": enable_chrome_topbar,
        "enable_chrome_footer": enable_chrome_footer,
        "enable_ppt_master_archetypes": enable_ppt_master_archetypes,
        "fix_nested_picture": fix_nested_picture,
        "clean_workspace": clean_workspace,
        "content_markdown": str(content_markdown) if content_markdown else None,
        "expand_skeleton_divider": expand_skeleton_divider,
        "expand_skeleton_content": expand_skeleton_content,
        "expand_divider_edits_template": expand_divider_edits_template,
        "expand_content_edits_template": expand_content_edits_template,
        "expand_body_bounds": expand_body_bounds,
        "expand_ending_svg": expand_ending_svg,
        "expand_part_names": expand_part_names,
        "expand_divider_subtitle_template": expand_divider_subtitle_template,
        "expand_section_title_en_map": expand_section_title_en_map,
        "expand_exclude_source_slides": expand_exclude_source_slides,
        "expand_toc_from_markdown": expand_toc_from_markdown,
        "expand_toc_slot_grid": expand_toc_slot_grid,
        "new_content_blocks": new_content_blocks,
    }
    ctx = PipelineContext(
        state=state,
        source_pptx=source_pptx,
        workspace=workspace,
        output_pptx=output_pptx,
        skill_dir=skill_dir,
        options=options,
    )
    pipeline = Pipeline()
    ctx = pipeline.run(ctx)

    # Map ctx 鈫?response dict (matches the legacy _finalize shape).
    from . import _internal  # late import to avoid cycle
    return _internal._finalize(ctx.state)
