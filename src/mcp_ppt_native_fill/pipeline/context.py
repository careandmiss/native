"""Pipeline Pattern — context, base handler, and error class.

This module defines the three primitives the Pipeline Pattern needs:

- :class:`PipelineContext`: mutable state bag passed between handlers.
- :class:`PipelineHandler`: abstract base class every phase must inherit.
- :class:`PipelineError`: raised by handlers to short-circuit the pipeline.

Design pattern: Pipeline Pattern (a GoF behavioral pattern).
See ``docs/PIPELINE_PATTERN_DESIGN_2026-09-20.md`` for the
design-mode deep-dive.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

# PipelineState is still in pipeline.py (commit 6 will move it to
# pipeline/state.py). We use TYPE_CHECKING so the import is only
# resolved during type-checking tools (mypy), not at runtime —
# avoiding the circular import
#   pipeline/__init__.py  → pipeline/context.py  → ..pipeline  → __init__
if TYPE_CHECKING:
    from ..pipeline import PipelineState


class PipelineError(RuntimeError):
    """Raised by a PipelineHandler.run() when its phase fails. The
    orchestrator catches this, marks ``ctx.state.stage = "failed"``,
    appends to ``ctx.state.errors``, and stops the chain.

    Distinct from generic exceptions so handlers can signal "I failed
    cleanly, no need to crash the whole pipeline" without relying on
    string matching.
    """


@dataclass
class PipelineContext:
    """Mutable state bag passed between PipelineHandlers.

    Replaces the implicit global state in the legacy
    ``pipeline.run_native_fill`` (a 400-line function that took 40+
    keyword-only arguments). Now handlers read/write context fields
    instead of relying on closure-captured locals.

    Lifecycle:
        1. caller constructs PipelineContext once before run()
        2. Pipeline.run() iterates handlers; each may call ctx.get/set
        3. Pipeline.run() returns the final ctx
        4. caller maps ctx to a JSON response via _ctx_to_response()

    Why a single dataclass (vs Protocol or TypedDict)?
        - dataclass + field(default_factory=dict) gives ergonomic
          ctx.get() / ctx.set() helpers without subclassing dict
        - one stop for input args (source_pptx), state (PipelineState),
          inter-handler outputs (handler_outputs), and lifecycle
          controls (skip_handlers, stop_after)
        - dataclass repr makes debug logging trivial
    """

    # ---- core pipeline state (existing dataclass) ----
    state: PipelineState

    # ---- input parameters (one-shot, set before run()) ----
    source_pptx: Path
    workspace: Path
    output_pptx: Path
    skill_dir: Path
    options: dict[str, Any] = field(default_factory=dict)

    # ---- inter-handler outputs (handler N sets, handler N+1 reads) ----
    handler_outputs: dict[str, Any] = field(default_factory=dict)

    # ---- lifecycle control ----
    skip_handlers: set[str] = field(default_factory=set)
    stop_after: str | None = None  # handler.name to stop AFTER

    # ---- convenience API ----
    def get(self, key: str, default: Any = None) -> Any:
        return self.handler_outputs.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.handler_outputs[key] = value

    def has(self, key: str) -> bool:
        return key in self.handler_outputs


class PipelineHandler(ABC):
    """Abstract base class for every pipeline phase."""

    name: ClassVar[str]

    @abstractmethod
    def run(self, ctx: PipelineContext) -> PipelineContext:
        raise NotImplementedError

    def skip(self, ctx: PipelineContext) -> bool:
        return False

    def on_failure(self, ctx: PipelineContext, exc: PipelineError) -> None:
        return None


__all__ = [
    "PipelineContext",
    "PipelineError",
    "PipelineHandler",
]
