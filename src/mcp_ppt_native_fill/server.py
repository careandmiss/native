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
    text = json.dumps(payload, ensure_ascii=False, indent=2)
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
