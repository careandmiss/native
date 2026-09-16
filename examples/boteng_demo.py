"""端到端：山西柏腾采购制度.md + 柏腾ppt模版.pptx.

策略：和 structured_demo 一样，全部交给 expand_workspace_from_markdown
统一处理。开 llm_plan=True 让 cover title / TOC 由 llm_planner 兜底
（最近 backfill 改动保证了 cover 不漏）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_TEMPLATE = Path(r"D:\Code\tst\native_fill\柏腾ppt模版.pptx")
DEFAULT_MD = Path(r"D:\Code\tst\native_fill\3山西柏腾科技有限公司采购制度.md")
DEFAULT_OUTPUT = HERE.parent / "projects" / "boteng_采购制度_out.pptx"
DEFAULT_WORKSPACE = HERE.parent / "projects" / "boteng_采购制度_workspace"

TOC_TOP = {"rows": 3, "cols": 2}
DIVIDER_EDITS = {"shape-4": "PART {nn}", "shape-5": "{title}"}
CONTENT_EDITS = {"shape-17": "{title}"}
BODY_BOUNDS = "120 130 1060 480"


def _run_via_stdin(arguments: dict) -> tuple[dict, str]:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "native_fill", "arguments": arguments}},
    ]
    stdin_payload = "\n".join(
        json.dumps(r, ensure_ascii=False) for r in requests
    ) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_ppt_native_fill"],
        input=stdin_payload, cwd=str(HERE.parent),
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True, text=True, encoding="utf-8", timeout=900,
    )
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
            payload = json.loads(msg["result"]["content"][0]["text"])
            break
    return payload, (proc.stderr or "")[-3000:]


def main() -> int:
    if not DEFAULT_TEMPLATE.is_file():
        print(f"template not found: {DEFAULT_TEMPLATE}", file=sys.stderr); return 1
    if not DEFAULT_MD.is_file():
        print(f"markdown not found: {DEFAULT_MD}", file=sys.stderr); return 1
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    arguments = {
        "source_pptx": str(DEFAULT_TEMPLATE),
        "workspace": str(DEFAULT_WORKSPACE),
        "output_pptx": str(DEFAULT_OUTPUT),
        "content_mapping": {},
        "content_markdown": str(DEFAULT_MD),
        "options": {
            "validate_strict": False,
            "llm_plan": True,
            "expand_toc_from_markdown": True,
            "expand_toc_slot_grid": TOC_TOP,
            "expand_skeleton_divider": 3,
            "expand_skeleton_content": 4,
            "expand_divider_edits_template": DIVIDER_EDITS,
            "expand_content_edits_template": CONTENT_EDITS,
            "expand_body_bounds": BODY_BOUNDS,
            "expand_ending_svg": "slide_05.svg",
            "expand_exclude_source_slides": [3, 4],
            "fix_nested_picture": True,
            "skip_phase3_5": False,
            "disabled_autofixes": ["render_compat"],
            "clean_workspace": True,
        },
    }

    t0 = time.time()
    payload, stderr_tail = _run_via_stdin(arguments)
    elapsed = time.time() - t0

    summary = {
        "ok": payload.get("ok"),
        "stage": payload.get("stage"),
        "wall_clock_s": round(elapsed, 2),
        "output_pptx": payload.get("output_pptx"),
        "output_exists": DEFAULT_OUTPUT.is_file(),
        "output_size_bytes": DEFAULT_OUTPUT.stat().st_size
        if DEFAULT_OUTPUT.is_file() else 0,
        "toc_summary": payload.get("toc_summary"),
        "expansions_n_parts": (payload.get("expansions") or {}).get("n_parts"),
        "expansions_cloned_svgs": (payload.get("expansions") or {}).get("cloned_svgs"),
        "errors_count": len(payload.get("errors") or []),
        "errors_head": (payload.get("errors") or [])[:5],
        "warnings_count": len(payload.get("warnings") or []),
        "warnings_head": (payload.get("warnings") or [])[:5],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not payload.get("ok") and stderr_tail:
        print("\n--- server stderr (tail) ---", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)

    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())