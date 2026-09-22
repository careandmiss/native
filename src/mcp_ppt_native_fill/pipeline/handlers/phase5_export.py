"""Phase 22 commit 5 (2026-09-20): Phase5ExportHandler (stub).

Full implementation deferred to commit 6. Stub delegates to legacy.
"""
from __future__ import annotations

import logging

from mcp_ppt_native_fill.pipeline import _internal

from ..context import PipelineContext, PipelineHandler

log = logging.getLogger("mcp_ppt_native_fill.pipeline.phase5_export")


class Phase5ExportHandler(PipelineHandler):
    """Phase 5: SVG 鈫?PPTX export via vendor/svg_to_pptx."""

    name = "phase5_export"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        _internal.phase5_export(
            ctx.state,
            output_pptx=ctx.output_pptx,
            validate_strict=ctx.options.get("validate_strict", True),
            render_previews=ctx.options.get("render_previews", False),
        )
        return ctx
