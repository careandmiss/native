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
       a. If handler.name in ctx.skip_handlers → skip.
       b. If handler.skip(ctx) returns True → skip.
       c. Run handler.run(ctx). On success, continue.
       d. On PipelineError: mark state=failed, call
          handler.on_failure(ctx, exc), break.
       e. If ctx.stop_after == handler.name → break (debug hook).
    2. Return the (possibly mutated) context.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from .context import PipelineContext, PipelineError, PipelineHandler

log = logging.getLogger("mcp_ppt_native_fill.pipeline.orchestrator")


class Pipeline:
    """Runs a list of PipelineHandlers in sequence.

    Default handler chain is empty; the actual handlers
    (Phase2Import, MarkdownExpand, Phase3Author, Phase4Quality,
    Phase5Export) are added in commit 5. Until then, callers must
    pass an explicit ``handlers`` list to construct a working
    Pipeline.

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

    DEFAULT_HANDLERS: ClassVar[list[type[PipelineHandler]]] = []

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


__all__ = ["Pipeline"]