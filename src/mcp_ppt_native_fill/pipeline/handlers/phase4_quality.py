"""Phase 22 commit 5 (2026-09-20): Phase4QualityHandler (stub).

Full implementation deferred to commit 6. Stub delegates to legacy.
"""
from __future__ import annotations

import logging

from mcp_ppt_native_fill.pipeline import _legacy

from ..context import PipelineContext, PipelineHandler

log = logging.getLogger("mcp_ppt_native_fill.pipeline.phase4_quality")


class Phase4QualityHandler(PipelineHandler):
    """Phase 4: P2 picture advisory + quality gate.

    Stub: delegates to legacy. Commit 6 will inline.

    legacy.phase4_quality reads ``state.skill_dir`` to refresh the
    SVG authoring view; we must populate it before calling, since
    PipelineContext.skill_dir lives outside PipelineState.
    """
    name = "phase4_quality"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        # Phase4 reads skill_dir off the state; mirror ctx.skill_dir
        # onto state so the legacy function sees the vendor dir.
        ctx.state.skill_dir = ctx.skill_dir
        _legacy.phase4_quality(
            ctx.state,
            auto_fix=ctx.options.get("auto_fix", True),
            max_fix_iterations=ctx.options.get("max_fix_iterations", 3),
            strict=ctx.options.get("quality_strict", False),
            preflight_strict=ctx.options.get("preflight_strict"),
        )
        return ctx