"""Phase 22 commit 3 (2026-09-20): Phase2ImportHandler.

Wraps the legacy ``pipeline.phase2_import`` function (line 97-128)
as a ``PipelineHandler``. This is the **only handler that spawns the
vendor** (vendor/pptx_master/scripts/pptx_to_svg.py).

Why "only here":
    In the legacy code, ``run_with_mapping`` (line 2181) AND
    ``run_native_fill`` → ``phase2_import`` (line 110) BOTH spawned
    the vendor on the same workspace. The second spawn hit a
    PowerShell-inherited file handle on the first spawn's
    ``.publish-{hash}/candidate/sources/source.pptx`` → PermissionError.
    See docs/PHASE21_VENDOR_PUBLISH_BUG_2026-09-20.md.

By centralizing the vendor spawn in this one handler, the Pipeline
Pattern ensures vendor runs exactly once per pipeline.run().
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from mcp_ppt_native_fill import runner  # absolute import (parent package)

from ..context import PipelineContext, PipelineError, PipelineHandler

log = logging.getLogger("mcp_ppt_native_fill.pipeline.phase2_import")


class Phase2ImportHandler(PipelineHandler):
    """Phase 2: spawn vendor/pptx_master/scripts/pptx_to_svg.py.

    Reads ctx.source_pptx, writes SVGs to ctx.workspace's
    ``authoring-svg-flat/`` subdirectory. Updates ctx.state.stage to
    ``"imported"`` on success, raises PipelineError on vendor failure.
    """

    name = "phase2_import"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        # Update state stage + workspace mirror the legacy function.
        ctx.state.stage = "import"
        ctx.state.workspace = ctx.workspace

        inheritance_mode = ctx.options.get("inheritance_mode", "both")
        timeout_ms = ctx.options.get("timeout_ms", 120_000)

        # Phase 23+ Windows stdio handle retention: the parent
        # subprocess (PowerShell or mcp_ppt_native_fill demo) holds a
        # file handle on `output_dir/sources/source.pptx` for ~0.5-2 s
        # after vendor exits. Adding 0.3 s before vendor spawn lets
        # the prior OS-level handle release complete; this prevents
        # Phase 23's `_copytree_with_retry` retry chain from
        # exhausting on the second template_v2 demo run.
        import time as _time
        _time.sleep(0.3)

        # ★ The single vendor spawn per pipeline.run(). No other
        # handler calls runner.run_pptx_to_svg(); only MarkdownExpand
        # writes to the workspace, and it doesn't re-convert.
        res = runner.run_pptx_to_svg(
            ctx.skill_dir,
            source_pptx=ctx.source_pptx,
            workspace=ctx.workspace,
            inheritance_mode=inheritance_mode,
            roundtrip=True,
            timeout_ms=timeout_ms,
        )
        if not res.ok:
            raise PipelineError(
                f"phase2 vendor failed exit={res.exit} "
                f"stderr_tail={res.stderr[-2000:]}\nstderr_full=\n{res.stderr}"
            )

        # Phase 23+ commit 2 (on_success cleanup): PowerShell stdio
        # handle retention can leave vendor's transaction_dir behind
        # in a half-cleaned state (rmtree raised WinError 5 because
        # candidate/sources/source.pptx was still held). Retry-rmtree
        # inside _cleanup didn't fully recover. As a belt-and-suspenders
        # fix: after a successful vendor run, aggressively clean any
        # leftover .publish-* / .convert-* dirs. The next pipeline.run()
        # gets a clean workspace and won't hit the same race.
        for pattern in (".publish-*", ".convert-*"):
            for stale in ctx.workspace.parent.glob(
                f".{ctx.workspace.name}{pattern}"
            ):
                log.info(
                    "phase2.on_success: cleaning stale %s", stale,
                )
                shutil.rmtree(stale, ignore_errors=True)

        ctx.state.stage = "imported"
        ctx.state.warnings.extend(getattr(res, "warnings", []))
        # Hand parsed vendor output downstream (MarkdownExpand /
        # Phase3Author can read this).
        ctx.set("vendor_result", getattr(res, "parsed", {}))
        return ctx

    def on_failure(self, ctx: PipelineContext, exc: PipelineError) -> None:
        """Clean up vendor's ``.publish-{hash}/`` and ``.convert-{hash}/``
        temp dirs that may have been left behind by a partial vendor
        spawn. The next pipeline.run() can then start clean.
        """
        for pattern in (".publish-*", ".convert-*"):
            for temp_dir in ctx.workspace.parent.glob(
                f".{ctx.workspace.name}{pattern.removeprefix('.')}"
            ):
                log.warning(
                    "phase2.on_failure: cleaning %s", temp_dir,
                )
                shutil.rmtree(temp_dir, ignore_errors=True)