"""native_fill_demo.py — end-to-end demo of the native_fill MCP tool.

Spins up the stdio MCP server in-process, sends a single ``tools/call`` for
``native_fill`` against 柏腾ppt模版.pptx, and prints the result envelope.

Usage:
    python examples/native_fill_demo.py [--check-only]

Exits 0 on pipeline success, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp_ppt_native_fill import runner, server


DEFAULT_TEMPLATE = Path(r"C:\Users\Administrator\Desktop\柏腾ppt模版.pptx")
DEFAULT_WORKSPACE = HERE.parent / "projects" / "native_fill_demo_workspace"
DEFAULT_OUTPUT = HERE.parent / "projects" / "native_fill_demo_out.pptx"


def _send_request(proc_stdin, proc_stdout, request: dict) -> dict:
    """Write one JSON-RPC request and read the matching response."""
    proc_stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc_stdin.flush()
    line = proc_stdout.readline()
    return json.loads(line)


def _run_via_stdin(arguments: dict) -> tuple[dict, str]:
    """Drive the MCP server by piping all JSON-RPC requests through stdin.

    Using a single stdin write avoids the interactive-Popen buffering problem
    on Windows where buffered server-side writes can deadlock the test client.
    Returns ``(payload, server_stderr_tail)``.
    """
    import subprocess

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "native_fill", "arguments": arguments},
        },
    ]
    stdin_payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "mcp_ppt_native_fill.server"],
        input=stdin_payload,
        cwd=str(HERE.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )

    # Parse responses: skip non-JSON noise (e.g. logging lines if any leaked)
    # and find the matching id=2 response.
    payload: dict = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2:
            text = msg["result"]["content"][0]["text"]
            payload = json.loads(text)
            break

    stderr_tail = (proc.stderr or "")[-2000:]
    return payload, stderr_tail


def _build_demo_args(template: Path, workspace: Path, output: Path) -> dict:
    """Build a ``native_fill`` arguments payload for the 柏腾 demo.

    The shape_ids here are placeholders — they match the layout described in
    docs/NATIVE_FILL_PIPELINE_GUIDE.md §9 walkthrough. Real shape_ids depend
    on the source PPTX content. The demo's primary goal is exercising the
    end-to-end pipeline, not verifying the exact text replacement.
    """
    return {
        "source_pptx": str(template),
        "workspace": str(workspace),
        "output_pptx": str(output),
        "content_mapping": {
            "slide_01.svg": {
                "shape-23": "合同管理制度",
                "shape-24": "工业设备智能化服务商",
                "shape-8": "部门：商务部",
            },
            "slide_05.svg": {
                "shape-2": "THANK YOU",
                "shape-3": "谢谢观看",
            },
        },
        "options": {
            "auto_fix": True,
            "max_fix_iterations": 3,
            "validate_strict": False,  # demo: don't abort on advisory
        },
    }


def run_demo(
    template: Path = DEFAULT_TEMPLATE,
    workspace: Path = DEFAULT_WORKSPACE,
    output: Path = DEFAULT_OUTPUT,
    check_only: bool = False,
) -> int:
    if check_only:
        # Verify we can resolve skill_dir + attribution_guard passes.
        skill_dir = runner.resolve_skill_dir()
        print(f"skill_dir = {skill_dir}")
        guard = runner.run_attribution_guard(skill_dir)
        print(f"attribution_guard exit = {guard.exit} ok = {guard.ok}")
        return 0 if guard.ok else 1

    if not template.is_file():
        print(f"template not found: {template}", file=sys.stderr)
        print("Pass --check-only to skip e2e.", file=sys.stderr)
        return 1

    if not workspace.is_dir():
        workspace.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    arguments = _build_demo_args(template, workspace, output)

    t0 = time.time()
    payload, stderr_tail = _run_via_stdin(arguments)
    elapsed = time.time() - t0

    # Render the result envelope.
    print(
        json.dumps(
            {
                "ok": payload.get("ok"),
                "stage": payload.get("stage"),
                "duration_ms": payload.get("duration_ms"),
                "wall_clock_s": round(elapsed, 2),
                "export_summary": payload.get("export_summary"),
                "delivery": payload.get("delivery"),
                "fix_iterations_count": len(payload.get("fix_iterations") or []),
                "warnings_count": len(payload.get("warnings") or []),
                "errors_count": len(payload.get("errors") or []),
                "errors_head": (payload.get("errors") or [])[:3],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # If pipeline failed, surface server stderr (logs) so the user can see
    # what vendor complained about.
    if not payload.get("ok") and stderr_tail:
        print("--- server stderr (tail) ---", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)

    return 0 if payload.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="native_fill MCP demo")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only verify SKILL_DIR resolution + attribution_guard.",
    )
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return run_demo(
        template=args.template,
        workspace=args.workspace,
        output=args.output,
        check_only=args.check_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
