"""Pipeline handlers sub-package.

Each module exports one ``PipelineHandler`` subclass:

  - phase2_import.py  — Phase2ImportHandler (vendor PPTX → SVGs)
  - markdown_expand.py — MarkdownExpandHandler (markdown → page_plan)
  - phase3_author.py  — Phase3AuthorHandler (chrome + archetype + body_cards)
  - phase4_quality.py — Phase4QualityHandler (P2 picture advisory)
  - phase5_export.py  — Phase5ExportHandler (SVGs → PPTX)

Each handler is registered in :data:`Pipeline.DEFAULT_HANDLERS` in
``orchestrator.py`` at commit 5.
"""
from __future__ import annotations

# Re-export every concrete handler so callers can write
# ``from mcp_ppt_native_fill.pipeline.handlers import Phase2ImportHandler``
# rather than reaching into the submodule.
from .markdown_expand import MarkdownExpandHandler
from .phase2_import import Phase2ImportHandler

__all__ = [
    "MarkdownExpandHandler",
    "Phase2ImportHandler",
]