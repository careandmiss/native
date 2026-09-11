"""llm_fill.py — end-to-end demo of native_fill with the built-in LLM phase.

Drives the stdio MCP server with a single ``tools/call`` that supplies
``content_markdown`` + ``options.llm_plan=true``. The server reads the
markdown, calls the LLM (MiniMax via settings.json fallback), and fills
the template. Output goes to ``projects/llm_<stem>_out.pptx`` (preserved).

Usage:
    python examples/llm_fill.py [--md <path>]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_MD = Path(r"D:\Code\tst\native_fill\3山西柏腾科技有限公司采购制度.md")
DEFAULT_TEMPLATE = Path(r"C:\Users\Administrator\Desktop\柏腾ppt模版.pptx")


def _run_via_stdin(arguments: dict) -> tuple[dict, str]:
    import json as _json
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "native_fill", "arguments": arguments}},
    ]
    stdin_payload = "\n".join(
        _json.dumps(r, ensure_ascii=False) for r in requests
    ) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_ppt_native_fill"],
        input=stdin_payload, cwd=str(HERE.parent),
        env={**__import__("os").environ, "PYTHONPATH": str(SRC)},
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    payload: dict = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if msg.get("id") == 2:
            payload = _json.loads(msg["result"]["content"][0]["text"])
            break
    return payload, (proc.stderr or "")[-2000:]


def run(
    md_path: Path,
    template: Path,
    workspace: Path,
    output: Path,
) -> int:
    if not template.is_file():
        print(f"template not found: {template}", file=sys.stderr)
        return 1
    workspace.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    arguments = {
        "source_pptx": str(template),
        "workspace": str(workspace),
        "output_pptx": str(output),
        "content_mapping": {},
        "content_markdown": str(md_path),
        "options": {"llm_plan": True, "validate_strict": False},
    }

    t0 = time.time()
    payload, stderr_tail = _run_via_stdin(arguments)
    elapsed = time.time() - t0

    summary = {
        "ok": payload.get("ok"),
        "stage": payload.get("stage"),
        "duration_ms": payload.get("duration_ms"),
        "wall_clock_s": round(elapsed, 2),
        "output_pptx": payload.get("output_pptx"),
        "export_summary": payload.get("export_summary"),
        "delivery": payload.get("delivery"),
        "llm_mapping_slides": list(
            (payload.get("llm_content_mapping") or {}).keys()
        ),
        "llm_mapping_count": sum(
            len(v) for v in (payload.get("llm_content_mapping") or {}).values()
        ),
        "warnings_count": len(payload.get("warnings") or []),
        "errors_count": len(payload.get("errors") or []),
        "errors_head": (payload.get("errors") or [])[:3],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not payload.get("ok") and stderr_tail:
        print("\n--- server stderr (tail) ---", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)

    return 0 if payload.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="native_fill with built-in LLM phase")
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--workspace", type=Path,
                        default=HERE.parent / "projects" / "llm_boteng_workspace")
    parser.add_argument("--output", type=Path,
                        default=HERE.parent / "projects" / "llm_boteng_out.pptx")
    args = parser.parse_args()
    return run(args.md, args.template, args.workspace, args.output)


if __name__ == "__main__":
    raise SystemExit(main())