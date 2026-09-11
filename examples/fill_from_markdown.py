"""fill_from_markdown.py — use a content .md as input to native_fill.

Reads the cover-page fields from a procurement-system style markdown and
maps them onto slide_01.svg's shape IDs (shape-23 / shape-24 / shape-8),
then drives the MCP server end-to-end via stdin JSON-RPC.

Usage:
    python examples/fill_from_markdown.py [--md <path>] [--check-only]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp_ppt_native_fill import runner  # noqa: E402


DEFAULT_MD = Path(r"D:\Code\tst\native_fill\3山西柏腾科技有限公司采购制度.md")
DEFAULT_TEMPLATE = Path(r"C:\Users\Administrator\Desktop\柏腾ppt模版.pptx")
DEFAULT_WORKSPACE = HERE.parent / "projects" / "boteng_caigou_workspace"
DEFAULT_OUTPUT = HERE.parent / "projects" / "boteng_caigou_out.pptx"


# slide_01.svg shape IDs in the 柏腾 template (cover slide).
COVER_SHAPE_IDS = {
    "title": "shape-23",
    "subtitle": "shape-24",
    "department": "shape-8",
}


def parse_cover_fields(md_text: str) -> dict[str, str]:
    """Extract the four cover-page fields from a procurement-system markdown.

    The expected layout (based on the BT-ZD-MOC-* family of docs):
        line 1: **编号：BT-ZD-****MOC****-001**
        line 3: **山西柏腾科技有限公司**
        line 5: **采购****制度**
        line 7: 工业设备智能化服务商
    """
    # Strip markdown bold markers so "**采购制度**" → "采购制度".
    def _debold(s: str) -> str:
        return s.replace("**", "").strip()

    # The first **…** chunk on the first line is the doc number.
    first_line = md_text.splitlines()[0] if md_text else ""
    m = re.search(r"\*\*(.+?)\*\*", first_line)
    doc_no = _debold(m.group(1)) if m else ""

    # Pull every bold-leading line. The 2nd bold-leading line is the company,
    # the 3rd is the title.
    bold_lines = [
        _debold(line)
        for line in md_text.splitlines()
        if line.startswith("**") and _debold(line)
    ]
    company = bold_lines[1] if len(bold_lines) >= 2 else ""
    title = bold_lines[2] if len(bold_lines) >= 3 else ""

    # Subtitle is the first non-empty non-bold line after the title.
    subtitle = ""
    after_title = False
    for line in md_text.splitlines():
        s = _debold(line)
        if not s:
            continue
        if after_title and not line.startswith("**"):
            subtitle = s
            break
        if s == title:
            after_title = True

    return {
        "doc_no": doc_no,
        "company": company,
        "title": title,
        "subtitle": subtitle,
    }


def build_arguments(template: Path, workspace: Path, output: Path, fields: dict[str, str]) -> dict:
    return {
        "source_pptx": str(template),
        "workspace": str(workspace),
        "output_pptx": str(output),
        "content_mapping": {
            "slide_01.svg": {
                COVER_SHAPE_IDS["title"]: fields["title"],
                COVER_SHAPE_IDS["subtitle"]: fields["subtitle"] or fields["company"],
                COVER_SHAPE_IDS["department"]: fields["doc_no"],
            },
            "slide_05.svg": {
                "shape-2": "THANK YOU",
                "shape-3": "谢谢观看",
            },
        },
        "options": {
            "auto_fix": True,
            "max_fix_iterations": 3,
            "validate_strict": False,
        },
    }


def _run_via_stdin(arguments: dict) -> tuple[dict, str]:
    """Drive the MCP server by piping all JSON-RPC requests through stdin."""
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
    return payload, (proc.stderr or "")[-2000:]


def run(
    md_path: Path = DEFAULT_MD,
    template: Path = DEFAULT_TEMPLATE,
    workspace: Path = DEFAULT_WORKSPACE,
    output: Path = DEFAULT_OUTPUT,
    check_only: bool = False,
) -> int:
    if not md_path.is_file():
        print(f"markdown not found: {md_path}", file=sys.stderr)
        return 1

    fields = parse_cover_fields(md_path.read_text(encoding="utf-8"))
    print("Parsed cover fields:")
    print(json.dumps(fields, ensure_ascii=False, indent=2))

    if check_only:
        skill_dir = runner.resolve_skill_dir()
        print(f"skill_dir = {skill_dir}")
        guard = runner.run_attribution_guard(skill_dir)
        print(f"attribution_guard exit = {guard.exit} ok = {guard.ok}")
        return 0 if guard.ok else 1

    if not template.is_file():
        print(f"template not found: {template}", file=sys.stderr)
        return 1

    workspace.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = build_arguments(template, workspace, output, fields)

    print("\nContent mapping:")
    print(json.dumps(arguments["content_mapping"], ensure_ascii=False, indent=2))

    t0 = time.time()
    payload, stderr_tail = _run_via_stdin(arguments)
    elapsed = time.time() - t0

    print("\nResult envelope:")
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

    if not payload.get("ok") and stderr_tail:
        print("\n--- server stderr (tail) ---", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)

    return 0 if payload.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill PPTX template from markdown content")
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    return run(
        md_path=args.md,
        template=args.template,
        workspace=args.workspace,
        output=args.output,
        check_only=args.check_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())