"""Structured markdown E-path demo.

Generates a pptx that exercises BOTH paths through expand_workspace_from_markdown:
  - simple markdown (no meta) → 3-column-cards via cards_from_body (A path)
  - structured markdown with meta → caller-declared layout via dispatch (E path)

The markdown intentionally mixes styles so the diff between paths is visible:
  - 前言     : simple-text  (single padded card, auto-wrap)
  - 一、目的 : hero-number  (big KPI value)
  - 二、范围 : bullet-list  (left color bar + numbered items)
  - 三、原则 : callout-box  (quote + attribution)
  - 四、流程 : 3-column-cards (no meta → A path, fallback)
  - 五、总结 : two-column-compare (left vs right)
  - 六、时间线: timeline (horizontal axis nodes)

Output: projects/structured_demo_out.pptx
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
DEFAULT_OUTPUT = HERE.parent / "projects" / "structured_demo_out.pptx"
DEFAULT_WORKSPACE = HERE.parent / "projects" / "structured_demo_workspace"

TOC_TOP = {"rows": 3, "cols": 2}
DIVIDER_EDITS = {"shape-4": "PART {nn}", "shape-5": "{title}"}
# Phase 8 (2026-09-16): translate H1 titles to English subtitles for
# the boteng divider template's shape-70 element (font / size / color
# / position preserved by the template).
SECTION_TITLE_EN = {
    "前言": "Preface",
    "一、目的": "Purpose",
    "二、适用范围": "Application Scope",
    "三、基本原则": "Basic Principles",
    "四、工作程序": "Working Procedure",
    "附件：": "Appendix",
}
DIVIDER_SUBTITLE = {"shape-70": "{title_en}"}
CONTENT_EDITS = {"shape-17": "{title}"}
BODY_BOUNDS = "120 130 1060 480"

# Mixed markdown: some sections structured (E-path), some plain (A-path).
# Note: two-column-compare and timeline require nested dict/list spec
# shapes (left/right as objects, steps as list of dicts) which markdown
# string values can't represent. They're reached via direct API calls,
# not through the Phase 2 E-path dispatch. The 4 layouts shown here
# all accept flat string values and exercise the dispatch fully.
MD = """# 前言

> **layout**: simple-text
> **text**: 为了规范公司采购行为, 降低采购成本, 提高采购质量, 公开透明, 公平竞争, 择优选择, 强化监督, 防范风险。

# 一、目的

> **layout**: hero-number
> **value**: 3
> **caption**: 大目标

为了提高公司采购效率。

# 二、范围

> **layout**: bullet-list
> **items**: 设备采购、物料采购、服务采购、研发采购、生产采购

适用于公司所有采购活动。

# 三、原则

> **layout**: callout-box
> **quote**: 公开透明 公平竞争 择优选择
> **attribution**: 公司采购部

公开透明、 公平竞争、 择优选择。

# 四、流程

需求申请 → 采购审批 → 供应商选择 → 合同签订 → 验收入库 → 付款结算。
"""


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


def main() -> int:
    if not DEFAULT_TEMPLATE.is_file():
        print(f"template not found: {DEFAULT_TEMPLATE}", file=sys.stderr)
        return 1
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    arguments = {
        "source_pptx": str(DEFAULT_TEMPLATE),
        "workspace": str(DEFAULT_WORKSPACE),
        "output_pptx": str(DEFAULT_OUTPUT),
        # Pass empty content_mapping — let the LLM (via llm_planner)
        # decide all per-shape edits (cover title, TOC slots, etc.).
        # The previous version hardcoded the cover title here, which
        # defeats the purpose of testing the full LLM pipeline.
        "content_mapping": {},
        "content_markdown": "",  # set below
        "options": {
            "validate_strict": False,
            # LLM-driven planning: when both content_markdown AND
            # llm_plan=true are set, pipeline calls
            # llm_planner.plan_content_mapping() which makes real
            # Anthropic / OpenAI calls. The resulting content_mapping
            # is merged with caller (caller wins on conflict).
            "llm_plan": True,
            "expand_toc_from_markdown": True,
            "expand_toc_slot_grid": TOC_TOP,
            "expand_skeleton_divider": 3,
            "expand_skeleton_content": 4,
            "expand_divider_edits_template": DIVIDER_EDITS,
            "expand_divider_subtitle_template": DIVIDER_SUBTITLE,
            "expand_section_title_en_map": SECTION_TITLE_EN,
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
    # Pass markdown via a temp file so MCP server can read it.
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="structured_demo_"))
    md_path = td / "content.md"
    md_path.write_text(MD, encoding="utf-8")
    arguments["content_markdown"] = str(md_path)

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
        "errors_head": (payload.get("errors") or [])[:3],
        "warnings_count": len(payload.get("warnings") or []),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not payload.get("ok") and stderr_tail:
        print("\n--- server stderr (tail) ---", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)

    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())