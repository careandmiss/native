"""server.py — stdio JSON-RPC MCP server for mcp_ppt_native_fill.

Implements the MCP 2024-11-05 protocol over line-delimited JSON-RPC 2.0
on stdin/stdout. Exposes one tool: ``native_fill``.

Logs go to stderr (stdout is the JSON-RPC transport channel).

Run via:
    python -m mcp_ppt_native_fill.server
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from . import pipeline, runner

# ---------------------------------------------------------------------------
# Logging — stderr only.
# ---------------------------------------------------------------------------

try:
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
except (AttributeError, OSError):
    if hasattr(sys.stdin, "buffer"):
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

logging.basicConfig(
    level=os.environ.get("MCP_NATIVE_FILL_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp_ppt_native_fill.server")


# ---------------------------------------------------------------------------
# Server identity.
# ---------------------------------------------------------------------------

SERVER_INFO = {
    "name": "mcp-ppt-native-fill",
    "version": "0.1.0",
}

SERVER_CAPABILITIES = {
    "tools": {"listChanged": False},
}

NEGOTIATED_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05"}


# ---------------------------------------------------------------------------
# Tool descriptor.
# ---------------------------------------------------------------------------

TOOL_NATIVE_FILL: dict[str, Any] = {
    "name": "native_fill",
    "description": (
        "Fill an existing PPTX template with new content while preserving its "
        "original design byte-for-byte. Wraps ppt-master v6.3.0's Edit Native "
        "PPTX round-trip pipeline (Phase 2-5) plus six auto-fixes from "
        "NATIVE_FILL_PIPELINE_GUIDE §6 (gradient, picture structure, text "
        "overflow, viewBox, font stack, page_plan validation).\n\n"
        "Use when:\n"
        "  - The user provides a source PPTX template + content (markdown / "
        "structured spec) and wants the original design preserved.\n"
        "  - The user asks for '按模板生成', '套模板', 'fill the template', "
        "'replace the text but keep the design'.\n\n"
        "Do NOT use when:\n"
        "  - No source PPTX is provided (use Generate PPTX instead).\n"
        "  - The user wants the design redrawn / beautified (use Generate PPTX "
        "beautify profile).\n"
        "  - The input is an image or PDF to reconstruct (use Image-to-PPTX).\n\n"
        "Required inputs: source_pptx, workspace, content_mapping, output_pptx. "
        "page_plan and new_content_blocks are optional."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_pptx": {
                "type": "string",
                "description": "Absolute path to the source .pptx template.",
            },
            "workspace": {
                "type": "string",
                "description": (
                    "Absolute path for the round-trip workspace directory. "
                    "Created if it does not exist."
                ),
            },
            "output_pptx": {
                "type": "string",
                "description": "Absolute path for the final .pptx.",
            },
            "page_plan": {
                "type": "array",
                "description": (
                    "Optional page plan. Each entry: "
                    "{\"source_slide\": <1-based int>, \"svg\": <filename>}. "
                    "Omit to use the source roster as-is."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "source_slide": {"type": "integer", "minimum": 1},
                        "svg": {"type": "string"},
                    },
                    "required": ["source_slide"],
                },
            },
            "content_mapping": {
                "type": "object",
                "description": (
                    "Per-svg text edits. Keys are svg filenames (under "
                    "authoring-svg-flat/), values are "
                    "{shape_id: new_text} maps. If `content_markdown` is "
                    "passed with `options.llm_plan=true`, the LLM-generated "
                    "mapping is merged INTO this dict (caller-supplied "
                    "entries win on conflict)."
                ),
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "content_markdown": {
                "type": "string",
                "description": (
                    "Optional absolute path to a content .md file. When "
                    "combined with `options.llm_plan=true`, the server "
                    "calls the configured LLM to plan content_mapping from "
                    "the markdown + workspace summary before phase3 runs."
                ),
            },
            "new_content_blocks": {
                "type": "object",
                "description": (
                    "Optional new authored blocks. Keys are svg filenames, "
                    "values are {shape_id: {bounds, layout, ...}}."
                ),
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": {"type": "object"},
                },
            },
            "options": {
                "type": "object",
                "description": "Behavior knobs.",
                "properties": {
                    "auto_fix": {"type": "boolean", "default": True},
                    "max_fix_iterations": {"type": "integer", "minimum": 0, "maximum": 10, "default": 3},
                    "validate_strict": {"type": "boolean", "default": True},
                    "llm_plan": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true AND `content_markdown` is set, run an "
                            "internal LLM planning pass to derive "
                            "content_mapping from the markdown + workspace "
                            "summary. Requires MCP_LLM_API_KEY in the server "
                            "environment."
                        ),
                    },
                    "skill_dir": {
                        "type": "string",
                        "description": (
                            "Override PPT_MASTER_SKILL_DIR. Defaults to the "
                            "platform-standard skill install location."
                        ),
                    },
                    "inheritance_mode": {
                        "type": "string",
                        "enum": ["both", "layered", "flat"],
                        "default": "both",
                    },
                    "skip_phase3_5": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Skip the phase3.5 pre-export fixup pass. "
                            "Enable only when vendor svg_to_pptx rejects "
                            "nested-picture rehydration (e.g. boteng "
                            "slide_02/03). Default False preserves full "
                            "pipeline safety."
                        ),
                    },
                    "fix_nested_picture": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Strip inner data-pptx-* attrs from <image>/<svg> "
                            "nested inside authoring-svg-flat SVGs before "
                            "phase4/5. Required for templates whose nested "
                            "SVG would otherwise be mis-rehydrated by "
                            "svg_to_pptx. Pairs with skip_phase3_5=True for "
                            "the boteng regression."
                        ),
                    },
                    "expand_skeleton_divider": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Source slide N used as the divider template "
                            "when cloning per-markdown-section pages. "
                            "Required (along with the other expand_* "
                            "params) to enable markdown expansion."
                        ),
                    },
                    "expand_skeleton_content": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Source slide N used as the content template "
                            "when cloning per-markdown-section pages."
                        ),
                    },
                    "expand_divider_edits_template": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Shape-id → text-format template for cloned "
                            "divider pages. Each value supports {nn}, {n}, "
                            "{title} placeholders. e.g. boteng: "
                            "{\"shape-4\": \"PART {nn}\", \"shape-5\": \"{title}\"}"
                        ),
                    },
                    "expand_content_edits_template": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Shape-id → text-format template for cloned "
                            "content pages. Same placeholder rules as "
                            "expand_divider_edits_template."
                        ),
                    },
                    "expand_body_bounds": {
                        "type": "string",
                        "default": "0 0 1280 720",
                        "description": (
                            "Bounds 'x y w h' for the auto-inserted content "
                            "cards block. boteng callers pass "
                            "\"120 130 1060 480\"."
                        ),
                    },
                    "expand_ending_svg": {
                        "type": "string",
                        "description": (
                            "Optional filename (e.g. 'slide_05.svg') to "
                            "force-move to the end of page_plan."
                        ),
                    },
                    "expand_part_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional ordered section titles. Default "
                            "extracts all H1 from content_markdown."
                        ),
                    },
                    "expand_divider_subtitle_template": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Optional second-pass text edits applied to "
                            "each cloned divider AFTER "
                            "expand_divider_edits_template. Same "
                            "{shape_id: text_format} shape; supports "
                            "{nn}/{n}/{title} placeholders. Use for "
                            "shapes the main title template doesn't "
                            "cover (e.g. English subtitle shape). "
                            "Caller picks which shape ids to target — "
                            "this server makes no template assumption."
                        ),
                    },
                    "expand_exclude_source_slides": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "description": (
                            "Optional list of 1-based slide numbers to "
                            "drop from page_plan.json's original roster. "
                            "Use when a skeleton slide is also a "
                            "design sample that cloning consumed — "
                            "e.g. boteng callers pass [skeleton_divider] "
                            "to drop slide_03 so the original 'PART 01 "
                            "/ <template title>' doesn't appear in the "
                            "deck alongside cloned per-section dividers."
                        ),
                    },
                    "clean_workspace": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, wipe workspace + output_pptx "
                            "before running. Used by manual boteng runs."
                        ),
                    },
                    "expand_toc_from_markdown": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, auto-fill the TOC slide from the "
                            "markdown H1s (after manual content_mapping "
                            "edits). TOC slide is auto-detected (the "
                            "first slide containing '目录' or "
                            "'CONTENTS'). Caller must supply "
                            "expand_toc_slot_title_ids. Manual mapping "
                            "entries that target TOC slot shape ids "
                            "are silently dropped to avoid stomping the "
                            "auto-fill."
                        ),
                    },
                    "expand_toc_slot_title_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Required when expand_toc_from_markdown is "
                            "true: ordered list of shape-* ids that "
                            "receive the chapter titles in row-major "
                            "fill order. Caller picks the ids for "
                            "their template (e.g. boteng: "
                            "['shape-69','shape-72','shape-77',"
                            "'shape-80','shape-86','shape-89'])."
                        ),
                    },
                    "expand_toc_slot_subtitle_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional parallel list of shape-* ids for "
                            "subtitle text. Same length as "
                            "expand_toc_slot_title_ids. If given, "
                            "subtitles mirror the title text (caller "
                            "can post-edit). If omitted, subtitle "
                            "slots are left untouched."
                        ),
                    },
                },
            },
        },
        "required": ["source_pptx", "workspace", "output_pptx", "content_mapping"],
    },
}


# ---------------------------------------------------------------------------
# Tool execution.
# ---------------------------------------------------------------------------

def _execute_native_fill(arguments: dict) -> dict:
    """Dispatch the native_fill tool call. Returns the pipeline result dict."""
    source_pptx = Path(arguments["source_pptx"])
    workspace = Path(arguments["workspace"])
    output_pptx = Path(arguments["output_pptx"])

    options = arguments.get("options") or {}
    auto_fix = bool(options.get("auto_fix", True))
    max_fix_iterations = int(options.get("max_fix_iterations", 3))
    validate_strict = bool(options.get("validate_strict", True))
    inheritance_mode = options.get("inheritance_mode", "both")
    llm_plan = bool(options.get("llm_plan", False))
    skip_phase3_5 = bool(options.get("skip_phase3_5", False))
    fix_nested_picture = bool(options.get("fix_nested_picture", False))
    clean_workspace = bool(options.get("clean_workspace", False))

    expand_skeleton_divider = options.get("expand_skeleton_divider")
    expand_skeleton_content = options.get("expand_skeleton_content")
    expand_divider_edits_template = options.get("expand_divider_edits_template")
    expand_content_edits_template = options.get("expand_content_edits_template")
    expand_body_bounds = options.get("expand_body_bounds", "0 0 1280 720")
    expand_ending_svg = options.get("expand_ending_svg")
    expand_part_names = options.get("expand_part_names")
    expand_divider_subtitle_template = options.get(
        "expand_divider_subtitle_template"
    )
    expand_exclude_source_slides = options.get("expand_exclude_source_slides")

    # Smart TOC fill (opt-in)
    expand_toc_from_markdown = bool(
        options.get("expand_toc_from_markdown", False)
    )
    expand_toc_slot_title_ids = options.get("expand_toc_slot_title_ids")
    expand_toc_slot_subtitle_ids = options.get("expand_toc_slot_subtitle_ids")

    content_markdown_raw = arguments.get("content_markdown")
    content_markdown: Path | None = (
        Path(content_markdown_raw) if content_markdown_raw else None
    )

    try:
        skill_dir = runner.resolve_skill_dir(options.get("skill_dir"))
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "stage": "init",
            "error": f"skill_dir resolution failed: {exc}",
            "hint": (
                "Pass options.skill_dir explicitly, or set PPT_MASTER_SKILL_DIR, "
                "or install ppt-master to ~/.claude/skills/ppt-master/."
            ),
        }
    log.info("skill_dir=%s", skill_dir)

    if llm_plan and not content_markdown:
        return {
            "ok": False,
            "stage": "init",
            "error": "options.llm_plan=true requires content_markdown path",
            "hint": (
                "Set arguments.content_markdown to an absolute .md file path, "
                "or unset options.llm_plan."
            ),
        }

    # If caller asked for markdown expansion OR smart TOC fill, route
    # through run_with_mapping (the one-shot driver that handles
    # pptx_to_svg, edits, strip, expansion, and delegates the rest to
    # run_native_fill).
    needs_expand = (
        expand_skeleton_divider is not None
        and expand_skeleton_content is not None
        and expand_divider_edits_template is not None
        and expand_content_edits_template is not None
    )
    needs_toc_expand = (
        expand_toc_from_markdown
        and expand_toc_slot_title_ids is not None
    )
    if needs_expand or needs_toc_expand:
        if not content_markdown:
            return {
                "ok": False,
                "stage": "init",
                "error": "expand_* options require content_markdown path",
                "hint": (
                    "Set arguments.content_markdown to an absolute .md "
                    "file path when using expand_skeleton_divider / "
                    "expand_skeleton_content / expand_divider_edits_template "
                    "/ expand_content_edits_template."
                ),
            }
        return pipeline.run_with_mapping(
            skill_dir=skill_dir,
            source_pptx=source_pptx,
            workspace=workspace,
            output_pptx=output_pptx,
            content_mapping=arguments.get("content_mapping") or {},
            content_markdown=content_markdown,
            page_plan=arguments.get("page_plan"),
            new_content_blocks=arguments.get("new_content_blocks"),
            expand_skeleton_divider=expand_skeleton_divider,
            expand_skeleton_content=expand_skeleton_content,
            expand_divider_edits_template=expand_divider_edits_template,
            expand_content_edits_template=expand_content_edits_template,
            expand_body_bounds=expand_body_bounds,
            expand_ending_svg=expand_ending_svg,
            expand_part_names=expand_part_names,
            expand_divider_subtitle_template=expand_divider_subtitle_template,
            expand_exclude_source_slides=expand_exclude_source_slides,
            expand_toc_from_markdown=expand_toc_from_markdown,
            expand_toc_slot_title_ids=expand_toc_slot_title_ids,
            expand_toc_slot_subtitle_ids=expand_toc_slot_subtitle_ids,
            fix_nested_picture=fix_nested_picture,
            skip_phase3_5=skip_phase3_5,
            auto_fix=auto_fix,
            max_fix_iterations=max_fix_iterations,
            validate_strict=validate_strict,
            clean_workspace=clean_workspace,
        )

    return pipeline.run_native_fill(
        source_pptx=source_pptx,
        workspace=workspace,
        output_pptx=output_pptx,
        page_plan=arguments.get("page_plan"),
        content_mapping=arguments.get("content_mapping") or {},
        new_content_blocks=arguments.get("new_content_blocks"),
        content_markdown=content_markdown,
        llm_plan=llm_plan,
        skill_dir=skill_dir,
        auto_fix=auto_fix,
        max_fix_iterations=max_fix_iterations,
        validate_strict=validate_strict,
        inheritance_mode=inheritance_mode,
        skip_phase3_5=skip_phase3_5,
    )


def _execute_tool(name: str, arguments: dict) -> dict:
    if name == "native_fill":
        return _execute_native_fill(arguments)
    return {
        "ok": False,
        "stage": "init",
        "error": f"unknown tool: {name}",
        "available_tools": [TOOL_NATIVE_FILL["name"]],
    }


def _build_tool_result(payload: dict) -> dict:
    """Wrap a tool payload as an MCP ``CallToolResult``."""
    # Defensively serialise any pathlib.Path that leaked into the
    # result dict (e.g. from a stage summary that captured a Path).
    # json.dumps's default= handler keeps the structure intact while
    # turning Path into its str form.
    text = json.dumps(
        payload, ensure_ascii=False, indent=2, default=str,
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": not bool(payload.get("ok", False)),
    }


# ---------------------------------------------------------------------------
# JSON-RPC dispatch.
# ---------------------------------------------------------------------------

def _jsonrpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _dispatch(request: dict) -> dict | None:
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        client_version = params.get("protocolVersion")
        chosen = (
            client_version
            if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else NEGOTIATED_PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": chosen,
                "serverInfo": SERVER_INFO,
                "capabilities": SERVER_CAPABILITIES,
            },
        }

    if method == "notifications/initialized":
        return None  # no response for notifications

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [TOOL_NATIVE_FILL]},
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not tool_name:
            return _jsonrpc_error(req_id, -32602, "params.name is required")
        try:
            payload = _execute_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": _build_tool_result(payload),
            }
        except Exception as exc:
            log.exception("tool execution failed")
            err_payload = {
                "ok": False,
                "stage": "init",
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": "unhandled_exception",
                "tool": tool_name,
                "trace_tail": traceback.format_exc()[-1000:],
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": _build_tool_result(err_payload),
            }

    return _jsonrpc_error(req_id, -32601, f"method not found: {method}")


# ---------------------------------------------------------------------------
# stdio loop.
# ---------------------------------------------------------------------------

def _serve_stdio(stdin=sys.stdin, stdout=sys.stdout) -> None:
    log.info("mcp-ppt-native-fill server starting (pid=%d)", os.getpid())
    try:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed JSON: %s", exc)
                continue
            response = _dispatch(request)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        log.info("mcp-ppt-native-fill server shutting down")
    finally:
        log.info("mcp-ppt-native-fill server stopped")


def serve_stdio() -> None:
    _serve_stdio()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. With no args, serves stdio. With args, utility commands."""
    import argparse

    parser = argparse.ArgumentParser(prog="mcp_ppt_native_fill.server")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Serve over stdio (default).")
    sub.add_parser("tools-list", help="Print tools/list payload and exit.")
    sub.add_parser("ping", help="Print ping response and exit.")

    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        serve_stdio()
        return 0
    if args.command == "tools-list":
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "result": {"tools": [TOOL_NATIVE_FILL]},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "ping":
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "ping",
                    "result": {},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
