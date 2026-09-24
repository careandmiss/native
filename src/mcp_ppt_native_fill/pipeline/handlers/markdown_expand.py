"""Phase 22 commit 4 (2026-09-20): MarkdownExpandHandler.

Wraps the legacy ``pipeline.run_with_mapping`` markdown-expansion
section (lines 2266-2382) as a PipelineHandler.

Behavior:
    - skip() returns True when ``options["content_markdown"]`` is
      absent (the optional markdown → page_plan path is off).
    - When caller supplies edits_template / section_title_en_map,
      those win (preserve boteng_demo compat).
    - When caller omits them, Phase 21 inspect_template auto-fills
      the skeleton indices + TOC grid (Strategy Pattern).
    - After expanding, smart TOC fill is attempted if
      ``expand_toc_from_markdown`` is True and resolved_toc has
      title_ids.

Inputs read:
    - options["content_markdown"]  — Path to markdown file
    - options["expand_*"]  — TOC grid / divider edits / content
      edits / section title en / etc.
    - options["expand_toc_from_markdown"] — bool

Outputs written:
    - ctx.handler_outputs["expansions"] — page_plan_path + cloned_svgs
    - ctx.handler_outputs["content_mapping"] — merged TOC edits
    - ctx.handler_outputs["new_blocks"] — from expand_workspace_from_markdown
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp_ppt_native_fill import workspace_expand  # parent package
from mcp_ppt_native_fill.pipeline._internal import _merge_new_blocks  # internal helper

from ..context import PipelineContext, PipelineError, PipelineHandler

log = logging.getLogger("mcp_ppt_native_fill.pipeline.markdown_expand")


class MarkdownExpandHandler(PipelineHandler):
    """Phase 2.5: expand workspace from markdown → page_plan.

    Runs between Phase2Import (vendor SVGs ready) and Phase3Author
    (which consumes the page_plan). Skips when no content_markdown
    is supplied (the legacy "no markdown" path keeps using the
    template's original roster).
    """

    name = "markdown_expand"

    def skip(self, ctx: PipelineContext) -> bool:
        """Skip when caller didn't supply a content_markdown path.

        We check only for None here (cheap, no I/O). The actual
        is_file() check happens inside run() — the Pipeline
        orchestrator trusts ``skip()`` to make a routing decision,
        but a bad path is the handler's own problem.
        """
        return ctx.options.get("content_markdown") is None

    def run(self, ctx: PipelineContext) -> PipelineContext:
        opts = ctx.options
        content_markdown = Path(opts["content_markdown"])

        # 1. Phase 21 inspect_template auto-fill. Caller-supplied
        #    edits_template still wins (preserve boteng_demo compat).
        opts, injects = self._auto_fill_template_options(ctx, opts)

        # Phase 24 commit 3 (vision-inferred layouts). The auto-fill
        # helper returned any vision-inferred placeholder lists in
        # ``injects`` (cover_layouts, toc_layouts, divider_layouts,
        # content_layouts, ending_layouts). Forward them to
        # expand_workspace_from_markdown; that function injects text
        # blocks at the LLM-inferred positions for graphic-only templates
        # where the boteng default shape-4/5/17 path is a no-op.
        try:
            expansions = workspace_expand.expand_workspace_from_markdown(
                ctx.workspace, content_markdown,
                skeleton_divider=opts.get("expand_skeleton_divider"),
                skeleton_content=opts.get("expand_skeleton_content"),
                divider_edits_template=opts.get("expand_divider_edits_template"),
                content_edits_template=opts.get("expand_content_edits_template"),
                divider_inject_title=injects.get("divider_inject_title"),
                divider_inject_part=injects.get("divider_inject_part"),
                content_inject_title=injects.get("content_inject_title"),
                cover_layouts=injects.get("cover_layouts"),
                toc_layouts=injects.get("toc_layouts"),
                divider_layouts=injects.get("divider_layouts"),
                content_layouts=injects.get("content_layouts"),
                ending_layouts=injects.get("ending_layouts"),
                body_bounds=opts.get("expand_body_bounds", "0 0 1280 720"),
                ending_svg=opts.get("expand_ending_svg"),
                part_names=opts.get("expand_part_names"),
                divider_subtitle_template=opts.get("expand_divider_subtitle_template"),
                section_title_en_map=opts.get("expand_section_title_en_map"),
                exclude_source_slides=opts.get("expand_exclude_source_slides"),
            )
        except Exception as exc:
            raise PipelineError(
                f"markdown_expand: expand_workspace_from_markdown "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc

        ctx.set("expansions", expansions)

        # 3. Smart TOC fill (opt-in). The legacy code runs this AFTER
        #    the markdown expansion so auto-fill wins over any leftover
        #    placeholders. Same ordering here.
        toc_summary = self._smart_toc_fill(ctx, expansions)
        ctx.set("toc_summary", toc_summary)

        # 4. Forward the effective page_plan (markdown-expanded if
        #    available, else the caller-supplied original) so downstream
        #    handlers can read it.
        effective_page_plan = self._effective_page_plan(expansions)
        ctx.set("page_plan", effective_page_plan)

        # 5. Merge caller-supplied new_blocks with the LLM's
        #    caller-side blocks (same group_id means the LLM can
        #    override the caller slot instead of stacking a second).
        ctx.set(
            "new_blocks",
            _merge_new_blocks(
                expansions.get("new_blocks", {}),
                opts.get("new_content_blocks"),
            ),
        )

        # 6. Merge TOC edits into content_mapping so phase3_author's
        #    re-apply (idempotent) restores the TOC fill AFTER
        #    run_native_fill's phase2_import overwrites the SVG.
        if toc_summary.get("cloned_svgs"):
            content_mapping = dict(opts.get("content_mapping") or {})
            toc_svg = toc_summary["toc_svg"]
            toc_edits = workspace_expand.build_toc_phase3_edits(
                ctx.workspace, content_markdown,
                toc_slot_title_ids=self._toc_title_ids(opts),
                toc_slot_subtitle_ids=self._toc_subtitle_ids(opts),
            )
            if toc_edits:
                existing = content_mapping.get(toc_svg, {})
                content_mapping[toc_svg] = {**existing, **toc_edits}
                ctx.set("content_mapping", content_mapping)
        # Surface the (possibly TOC-augmented) content_mapping so
        # downstream handlers (Phase3Author) can read it. Without
        # this, MarkdownExpandHandler's TOC edits never reach
        # phase3_author.
        if "content_mapping" not in ctx.handler_outputs:
            ctx.set("content_mapping",
                    dict(opts.get("content_mapping") or {}))

        ctx.state.stage = "plan_realize"
        return ctx

    # ---- helpers ----

    def _auto_fill_template_options(
        self, ctx: PipelineContext, opts: dict,
    ) -> tuple[dict, dict]:
        """Phase 21 P0-B integration: when caller omits edits_template
        / skeleton indices / ending_svg / TOC grid, inspect_template
        fills them in. Caller-supplied values always win.

        Returns ``(opts, injects)`` — the second dict carries the
        caller-supplied template-specific placeholder layouts
        (Phase 23+ commit 6), passed directly to
        :func:`expand_workspace_from_markdown` as kwargs. Keys:
        ``divider_inject_title`` / ``divider_inject_part`` /
        ``content_inject_title`` — each is a dict with ``shape_id``,
        ``x``, ``y``, ``w``, ``h``, ``font_size``. The caller is
        expected to derive these from vision over the template PNGs
        (or by trial-and-error in their demo config).
        """
        if (
            opts.get("expand_divider_edits_template") is None
            or opts.get("expand_content_edits_template") is None
        ):
            try:
                from mcp_ppt_native_fill import template_adapter
                pptx_for_inspect = template_adapter.ensure_ascii_path(
                    ctx.source_pptx,
                )
                profile = template_adapter.inspect_template(
                    pptx_for_inspect,
                )
                # Phase 23+ commit 6: graphic-only templates (e.g.
                # template_v2 slide_03/04 with no text shapes) need
                # text-block INJECTION at the divider/content
                # skeleton position — we capture these here and pass
                # them directly to expand_workspace_from_markdown
                # rather than via opts (server.py / run_with_pipeline
                # don't yet propagate these kwargs).
                injects: dict = {}  # always init so the trailing
                                     # except branch can still mutate it
                if (
                    opts.get("expand_skeleton_divider") is None
                    and profile.divider_skeleton
                ):
                    opts["expand_skeleton_divider"] = (
                        profile.divider_skeleton
                    )
                if (
                    opts.get("expand_skeleton_content") is None
                    and profile.content_skeleton
                ):
                    opts["expand_skeleton_content"] = (
                        profile.content_skeleton
                    )
                if (
                    opts.get("expand_ending_svg") is None
                    and profile.ending_slide is not None
                ):
                    opts["expand_ending_svg"] = (
                        f"slide_{profile.ending_slide:02d}.svg"
                    )
                if (
                    opts.get("expand_toc_slot_grid") is None
                    and profile.toc_grid
                ):
                    opts["expand_toc_slot_grid"] = profile.toc_grid
                if opts.get("expand_divider_edits_template") is None:
                    opts["expand_divider_edits_template"] = {
                        "shape-4": "PART {nn}",
                        "shape-5": "{title}",
                    }
                if opts.get("expand_content_edits_template") is None:
                    opts["expand_content_edits_template"] = {
                        "shape-17": "{title}",
                    }
                # Phase 23+ commit 6 (2026-09-20): template-specific
                # placeholder positions are NOT inferred here. Each
                # template has different visual layouts and we don't
                # want to hardcode coordinates (that would couple the
                # pipeline to one specific template).
                #
                # Caller (typically after running vision over the
                # template PNGs) passes a ``expand_divider_layouts`` /
                # ``expand_content_layouts`` / ``expand_cover_layouts`` /
                # ``expand_toc_layouts`` / ``expand_ending_layouts``
                # dict to opt-in. See examples/ for the schema.
                #
                # We still auto-fill cover/toc/ending/divider/content
                # edits_template using the existing text shapes (when
                # available — boteng-style templates) via the *edits
                # paths above. The layouts path is only used when the
                # template has no usable text shapes (graphic-only
                # templates like template_v2).
                if (
                    opts.get("expand_body_bounds", "0 0 1280 720")
                    == "0 0 1280 720"
                    and profile.body_bounds
                ):
                    opts["expand_body_bounds"] = profile.body_bounds
                log.info(
                    "phase21: auto-inspected template %s",
                    pptx_for_inspect.name,
                )
            except Exception as exc:
                log.warning(
                    "phase21: inspect_template failed for %s: %s: %s; "
                    "falling back to caller options",
                    ctx.source_pptx, type(exc).__name__, exc,
                )
        # Pull any caller-supplied template-specific layouts from
        # opts. These go into the second tuple element so they're
        # passed directly to expand_workspace_from_markdown (they're
        # not opts that need to round-trip through server.py).
        for key in (
            "divider_inject_title", "divider_inject_part",
            "content_inject_title",
        ):
            val = opts.get("expand_" + key)
            if val:
                injects[key] = val

        # Phase 24 commit 3 (vision-inferred layouts). When caller
        # opts request it AND an LLM is configured + vision-capable,
        # call vision_layout.infer_template_layout once per template
        # (cached). On any failure (no LLM / not vision-capable / network
        # error / malformed JSON) we fall back to the text-shape-only
        # auto-fill already done above — no caller action required.
        if opts.get("enable_vision_layout", True):
            try:
                from mcp_ppt_native_fill.vision_layout import (
                    infer_template_layout,
                )
                # Phase 27 commit 1: use inspect_template's rule-based
                # slide classification as the authoritative kind map.
                # The LLM frequently misclassifies content slides as
                # toc / divider (vision alone sees similar visual
                # layouts); inspect_template is structural and knows
                # which slide_index is cover / toc / divider / content
                # / ending. We rebuild that map here and pass it into
                # vision_layout so its parsed SlideLayout.kind field
                # reflects the rule-based truth rather than the LLM's
                # hallucinated kind.
                template_kind_map: dict[int, str] = {}
                try:
                    inspect_profile = template_adapter.inspect_template(
                        template_adapter.ensure_ascii_path(ctx.source_pptx),
                    )
                    if inspect_profile.cover_slide is not None:
                        template_kind_map[inspect_profile.cover_slide] = "cover"
                    if inspect_profile.toc_slide is not None:
                        template_kind_map[inspect_profile.toc_slide] = "toc"
                    if inspect_profile.divider_skeleton is not None:
                        template_kind_map[
                            inspect_profile.divider_skeleton
                        ] = "divider"
                    if inspect_profile.content_skeleton is not None:
                        template_kind_map[
                            inspect_profile.content_skeleton
                        ] = "content"
                    if inspect_profile.ending_slide is not None:
                        template_kind_map[inspect_profile.ending_slide] = "ending"
                except Exception:  # noqa: BLE001
                    # If inspect_template fails we still let vision_layout
                    # run with its LLM-derived kind — best-effort.
                    template_kind_map = None

                profile_layout = infer_template_layout(
                    ctx.source_pptx,
                    cache_dir=Path(
                        opts.get("vision_layout_cache_dir")
                        or (Path.home() / ".mcp_ppt_native_fill" / "vision_cache")
                    ),
                    template_kind_map=template_kind_map,
                    min_confidence=float(
                        opts.get("vision_layout_min_confidence", 0.5)
                    ),
                    max_image_dim=int(
                        opts.get("vision_layout_max_image_dim", 1568)
                    ),
                    dry_run=False,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "phase24: vision_layout inference failed (%s: %s); "
                    "falling back to text-shape auto-fill",
                    type(exc).__name__, exc,
                )
                profile_layout = None

            if profile_layout is not None:
                log.info(
                    "phase24: vision_layout inferred %d slide(s) (avg conf %.2f)",
                    len(profile_layout.slides),
                    profile_layout.confidence_avg,
                )
                for slide_layout in profile_layout.slides:
                    layouts_serialized = [
                        {
                            "role": ph.role,
                            "text": ph.text,
                            "x": ph.x, "y": ph.y,
                            "w": ph.w, "h": ph.h,
                            "font_size": ph.font_size,
                            "font_weight": ph.font_weight,
                            "shape_id": (
                                f"_vision_{ph.role}"
                            ),
                        }
                        for ph in slide_layout.placeholders
                    ]
                    key = f"{slide_layout.kind}_layouts"
                    if key in (
                        "cover_layouts", "toc_layouts",
                        "divider_layouts", "content_layouts",
                        "ending_layouts",
                    ):
                        injects[key] = layouts_serialized
        return opts, injects

    def _smart_toc_fill(
        self, ctx: PipelineContext, expansions: dict,
    ) -> dict:
        """Auto-fill TOC slide from markdown H1s (opt-in)."""
        opts = ctx.options
        if not opts.get("expand_toc_from_markdown"):
            return {"toc_svg": None, "slot_count": 0, "filled": 0,
                    "cloned_svgs": []}
        resolved_toc = opts.get("phase_resolved_toc") or {}
        title_ids = resolved_toc.get("title_ids") or []
        if not title_ids:
            return {"toc_svg": None, "slot_count": 0, "filled": 0,
                    "cloned_svgs": []}
        subtitle_ids = (
            [sid for sid in resolved_toc.get("subtitle_ids", []) if sid]
            or None
        )
        try:
            toc_summary = workspace_expand.expand_workspace_from_toc(
                ctx.workspace, Path(opts["content_markdown"]),
                toc_slot_title_ids=title_ids,
                toc_slot_subtitle_ids=subtitle_ids,
                toc_svg=resolved_toc.get("toc_svg"),
            )
            return toc_summary
        except Exception as exc:
            log.warning(
                "markdown_expand: smart TOC fill failed: %s: %s",
                type(exc).__name__, exc,
            )
            return {"toc_svg": None, "slot_count": 0, "filled": 0,
                    "cloned_svgs": []}

    def _effective_page_plan(self, expansions: dict) -> list | None:
        """If markdown expansion produced a page_plan.json, load it
        so downstream handlers use the expanded plan."""
        plan_path = expansions.get("page_plan_path")
        if not plan_path or not plan_path.is_file():
            return None
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            pages = payload.get("pages")
            return pages if isinstance(pages, list) else None
        except (json.JSONDecodeError, OSError):
            return None

    def _toc_title_ids(self, opts: dict) -> list[str]:
        return (opts.get("phase_resolved_toc") or {}).get("title_ids") or []

    def _toc_subtitle_ids(self, opts: dict) -> list[str] | None:
        ids = (opts.get("phase_resolved_toc") or {}).get("subtitle_ids") or []
        return [sid for sid in ids if sid] or None