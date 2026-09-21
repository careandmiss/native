"""Phase 22 Pipeline Pattern — sub-package for pipeline handlers.

Extracted from the monolithic ``pipeline.py`` (2474 lines, 5 phase
functions inline) into a small set of handlers implementing the
Pipeline Pattern (GoF Behavioral Patterns).

Sub-package layout:
    pipeline/
        __init__.py        — this file (public re-exports)
        context.py         — PipelineContext + PipelineHandler ABC + PipelineError
        _legacy.py         — legacy pipeline.py (commit 5+ will delete)
        orchestrator.py    — Pipeline class (commit 2)
        handlers/
            phase2_import.py        (commit 3)
            markdown_expand.py      (commit 4)
            phase3_author.py        (commit 5)
            phase4_quality.py       (commit 5)
            phase5_export.py        (commit 5)

The public API of ``mcp_ppt_native_fill.pipeline`` is preserved by
re-exporting the legacy module's public names below. New code should
import from the new submodules directly (e.g.
``mcp_ppt_native_fill.pipeline.context.PipelineContext``).
"""
from __future__ import annotations

# New Pipeline Pattern primitives (commit 1).
from .context import (
    PipelineContext,
    PipelineError,
    PipelineHandler,
)

# Legacy re-exports (commits 5-6 will delete). Existing call sites
# that import ``mcp_ppt_native_fill.pipeline.run_with_mapping`` etc.
# continue to work.
from ._legacy import (  # noqa: F401
    PipelineState,
    run_native_fill,
    run_with_mapping,
    _finalize,
    _seed_original_roster,
    write_page_plan,
)

__all__ = [
    # New
    "PipelineContext",
    "PipelineError",
    "PipelineHandler",
    # Legacy (public)
    "PipelineState",
    "run_native_fill",
    "run_with_mapping",
    "_finalize",
]