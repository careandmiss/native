"""mcp_ppt_native_fill — stdlib-only MCP server for ppt-master Edit Native PPTX route.

Wraps the ppt-master v6.3.0 native fill pipeline (5 phases, 7 vendored scripts)
behind one ``native_fill`` MCP tool. Auto-fixes six common authoring pitfalls
per NATIVE_FILL_PIPELINE_GUIDE §6 (gradient / picture / overflow / viewBox /
font / page_plan).

Zero external dependencies. Zero references to mcp_ppt_master or any sibling
package. Only spawns ppt-master scripts as subprocesses.
"""

from __future__ import annotations

from . import autofix, pipeline, runner
from .autofix import (
    fix_nested_picture_data_attrs,
    repair_nested_picture_attrs,
)
from .pipeline import (
    expand_workspace_from_markdown,
    run_with_mapping,
    run_native_fill,
)
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
    "autofix", "pipeline", "runner",
    # autofix public surface
    "fix_nested_picture_data_attrs", "repair_nested_picture_attrs",
    # pipeline public surface
    "expand_workspace_from_markdown", "run_with_mapping", "run_native_fill",
    # runner public surface
    "run_pptx_to_svg", "run_svg_quality_check", "run_svg_to_pptx",
    "resolve_skill_dir",
]
