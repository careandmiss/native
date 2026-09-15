"""smart_toc_fill.py — end-to-end demo of the smart TOC fill feature.

Drives the stdio MCP server with a single tools/call that uses the
manual-mapping path PLUS expand_toc_from_markdown=True. The TOC slide
gets auto-filled from the markdown H1s; divider + content slides are
cloned per section via the existing expand_*_template params.

Output: D:/Code/tst/native_fill/projects/smart_toc_boteng_out.pptx

Usage:
    python examples/smart_toc_fill.py
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
DEFAULT_OUTPUT = HERE.parent / "projects" / "smart_toc_boteng_out.pptx"
DEFAULT_WORKSPACE = HERE.parent / "projects" / "smart_toc_boteng_workspace"

# Boteng slide_02 TOC layout — 3 rows × 2 cols. Caller only declares
# grid dimensions; the function auto-locates title + subtitle shape ids
# from the SVG geometry.
TOC_TOP = {"rows": 3, "cols": 2}

# Divider / content skeleton (boteng: slide_03 = divider, slide_04 = content)
DIVIDER_EDITS = {
    "shape-4": "PART {nn}",
    "shape-5": "{title}",
}
DIVIDER_SUBTITLE = {
    "shape-70": "PART {nn} / {title}",
}
CONTENT_EDITS = {
    "shape-17": "{title}",
}
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
        capture_output=True, text=True, encoding="utf-8", timeout=600,
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
    return payload, (proc.stderr or "")[-2000:]


def run(template: Path, md_path: Path, workspace: Path, output: Path) -> int:
    if not template.is_file():
        print(f"template not found: {template}", file=sys.stderr)
        return 1
    workspace.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    arguments = {
        "source_pptx": str(template),
        "workspace": str(workspace),
        "output_pptx": str(output),
        "content_mapping": {},  # empty — smart TOC does the TOC work
        "content_markdown": str(md_path),
        "options": {
            "validate_strict": False,
            # Smart TOC fill — caller declares grid only; shape ids are
            # auto-detected from SVG geometry.
            "expand_toc_from_markdown": True,
            "expand_toc_slot_grid": TOC_TOP,
            # Divider / content cloning (existing)
            "expand_skeleton_divider": 3,
            "expand_skeleton_content": 4,
            "expand_divider_edits_template": DIVIDER_EDITS,
            "expand_content_edits_template": CONTENT_EDITS,
            "expand_body_bounds": BODY_BOUNDS,
            "expand_ending_svg": "slide_05.svg",
            # Boteng compatibility workarounds
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
        "duration_ms": payload.get("duration_ms"),
        "wall_clock_s": round(elapsed, 2),
        "output_pptx": payload.get("output_pptx"),
        "output_exists": output.is_file(),
        "output_size_bytes": output.stat().st_size if output.is_file() else 0,
        "edit_summary": payload.get("edit_summary"),
        "toc_summary": payload.get("toc_summary"),
        "toc_dropped_entries": payload.get("toc_dropped_entries"),
        "expansions_n_parts": (payload.get("expansions") or {}).get("n_parts"),
        "expansions_cloned_svgs": (payload.get("expansions") or {}).get("cloned_svgs"),
        "errors_count": len(payload.get("errors") or []),
        "errors_head": (payload.get("errors") or [])[:3],
        "warnings_count": len(payload.get("warnings") or []),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not payload.get("ok") and stderr_tail:
        print("\n--- server stderr (tail) ---", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)

    return 0 if payload.get("ok") else 1


def main() -> int:
    return run(DEFAULT_TEMPLATE, DEFAULT_MD,
               DEFAULT_WORKSPACE, DEFAULT_OUTPUT)


if __name__ == "__main__":
    raise SystemExit(main())
