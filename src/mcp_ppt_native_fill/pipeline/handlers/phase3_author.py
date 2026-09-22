"""Phase 22 commit 5 (2026-09-20): Phase3AuthorHandler (stub).

Full implementation deferred to commit 6. For now this handler
delegates to the legacy ``pipeline._legacy.phase3_author`` function
so the Pipeline Pattern can be wired end-to-end without rewriting
all 800+ lines of phase 3 logic.
"""
from __future__ import annotations

import logging

from mcp_ppt_native_fill import autofix
from mcp_ppt_native_fill.pipeline import _internal

from ..context import PipelineContext, PipelineHandler

log = logging.getLogger("mcp_ppt_native_fill.pipeline.phase3_author")


class Phase3AuthorHandler(PipelineHandler):
    """Phase 3: SVG generation (chrome + archetype + body_cards).

    Stub: delegates to legacy function. Commit 6 will inline the
    ~700 lines of phase3_author body into this class.
    """

    name = "phase3_author"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        # Nested-SVG inner data-pptx-* strip (legacy line 2259-2264):
        # boteng templates have nested-SVG picture shapes whose data-pptx-*
        # attributes need to be stripped before vendor round-trip. This
        # was inlined into _legacy.run_with_mapping; commit 6 will
        # move it to its own pre-Phase3 handler.
        if ctx.options.get("fix_nested_picture"):
            auth = ctx.workspace / "authoring-svg-flat"
            autofix.repair_nested_picture_attrs(auth)

        page_plan_pages = ctx.get("page_plan")
        new_content_blocks = ctx.get("new_blocks")
        content_mapping = ctx.get("content_mapping") or {}
        _internal.phase3_author(
            ctx.state,
            page_plan_pages=page_plan_pages,
            content_mapping=content_mapping,
            new_content_blocks=new_content_blocks,
        )
        return ctx
