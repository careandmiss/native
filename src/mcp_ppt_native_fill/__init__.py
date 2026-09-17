"""mcp_ppt_native_fill — stdlib-only MCP server for ppt-master Edit Native PPTX route.

Wraps the ppt-master v6.3.0 native fill pipeline (5 phases, 7 vendored scripts)
behind one ``native_fill`` MCP tool. Auto-fixes six common authoring pitfalls
per NATIVE_FILL_PIPELINE_GUIDE §6 (gradient / picture / overflow / viewBox /
font / page_plan).

Zero external dependencies. Zero references to mcp_ppt_master or any sibling
package. Only spawns ppt-master scripts as subprocesses.
"""

from __future__ import annotations

from . import autofix, pipeline, runner, workspace_expand
from .autofix import (
    fix_nested_picture_data_attrs,
    repair_nested_picture_attrs,
)
from .pipeline import (
    run_with_mapping,
    run_native_fill,
)
from .entry import (
    DEFAULT_TOC_GRID,
    generate_pptx,
)
from .render_diff import (
    RenderReport,
    render_human as render_diff_human,
    render_svg_previews,
)
from .workspace_expand import expand_workspace_from_markdown
from .runner import (
    run_pptx_to_svg,
    run_svg_quality_check,
    run_svg_to_pptx,
    resolve_skill_dir,
)

__version__ = "0.1.0"
__all__ = [
    "__version__",
    # submodules
    "autofix", "pipeline", "runner", "workspace_expand",
    # autofix public surface
    "fix_nested_picture_data_attrs", "repair_nested_picture_attrs",
    # pipeline public surface
    "expand_workspace_from_markdown", "run_with_mapping", "run_native_fill",
    # entry public surface (PR-11 one-shot wrapper)
    "generate_pptx", "DEFAULT_TOC_GRID",
    # render_diff public surface (PR-13 cairosvg previews)
    "render_svg_previews", "RenderReport", "render_diff_human",
    # runner public surface
    "run_pptx_to_svg", "run_svg_quality_check", "run_svg_to_pptx",
    "resolve_skill_dir",
]
