"""Phase 22 Pipeline Pattern — sub-package.

Layout (Layered Architecture):

  pipeline/
    __init__.py        — this file (public re-exports)
    context.py         — PipelineContext + PipelineHandler ABC + PipelineError
    orchestrator.py    — Pipeline class + run_with_pipeline entry
    handlers/          — thin PipelineHandler subclasses (~80 lines each)
        phase2_import.py      — only handler that spawns vendor
        markdown_expand.py    — markdown → page_plan + Phase 21 inspect
        phase3_author.py      — wraps _internal.phase3_author
        phase4_quality.py     — wraps _internal.phase4_quality
        phase5_export.py      — wraps _internal.phase5_export
    _internal.py        — phase3/4/5 implementations + helpers
                          (Façade — hides vendor complexity)

Design patterns applied here:

  - Pipeline Pattern (GoF "Chain of Responsibility" cousin):
    handlers run sequentially via Pipeline.run(); each mutates
    PipelineContext.

  - Adapter Pattern: each handler adapts the legacy free-function
    API (in _internal.py) to the PipelineHandler.run(ctx) interface.
    Handler files stay < 100 lines — just lifecycle wiring.

  - Façade Pattern: _internal.py hides the 700+ lines of phase3
    vendor coordination behind phase3_author(state, ...) — handlers
    don't need to know about block_renderer / archetype_router /
    chrome / autofix / edit_summary.

  - Template Method: PipelineHandler.run() is the template;
    handlers fill in by overriding it. Pipeline.run() also
    implements the template lifecycle (skip → run → on_failure →
    stop_after).

  - Strategy Pattern (commit 5+): MarkdownExpandHandler picks
    between CallerSuppliedAdapter and InspectedAdapter based on
    which options are None.

Public re-exports keep the historical ``mcp_ppt_native_fill.pipeline``
import surface so demos and tests don't break.
"""
from __future__ import annotations

# Layered Architecture imports (commit 1-5).
from .context import (
    PipelineContext,
    PipelineError,
    PipelineHandler,
)
from .orchestrator import Pipeline, run_with_pipeline

# PipelineState lives in _internal.py (was originally in pipeline.py,
# commit 0). Re-export so legacy callers keep working.
from ._internal import (
    PipelineState,
    _finalize,
    _merge_new_blocks,
    _seed_original_roster,
    write_page_plan,
)

__all__ = [
    # Layer 1: Pipeline Pattern primitives
    "Pipeline",
    "PipelineContext",
    "PipelineError",
    "PipelineHandler",
    "run_with_pipeline",
    # Internal helpers re-exported so workspace_expand.py and other
    # modules outside the sub-package can still import them.
    "PipelineState",
    "_finalize",
    "_merge_new_blocks",
    "_seed_original_roster",
    "write_page_plan",
]