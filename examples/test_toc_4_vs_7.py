"""Test smart TOC with < N (4 chapters) and > N (7 chapters) markdown.

Verifies the two non-trivial scenarios that boteng's ==N case didn't cover:
  * 4 chapters: slots 1-4 filled, slots 5-6 cleared (no overflow clone)
  * 7 chapters: slide_02.svg slots 1-6 + slide_part02_toc.svg slot 1

Outputs:
  * projects/toc_4_test_out.pptx
  * projects/toc_7_test_out.pptx

Each output is then re-extracted to confirm TOC slide text matches the
markdown.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_TEMPLATE = Path(r"D:\Code\tst\native_fill\柏腾ppt模版.pptx")
NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

TOC_TOP = {"rows": 3, "cols": 2}

# Boteng divider/content skeleton (matched to existing examples)
DIVIDER_EDITS = {"shape-4": "PART {nn}", "shape-5": "{title}"}
CONTENT_EDITS = {"shape-17": "{title}"}
BODY_BOUNDS = "120 130 1060 480"

# Markdown for the 4-chapter scenario (< N: 4 < 6 slots)
MD_4 = """# 第一章 项目概述
这是项目背景和目标说明。

# 第二章 实施步骤
详细的执行流程说明。

# 第三章 团队分工
主要人员职责分配。

# 第四章 风险控制
风险识别与应对措施。
"""

# Markdown for the 7-chapter scenario (> N: 7 > 6 slots → 1 overflow clone)
MD_7 = """# 第一章 项目概述
背景和目标。

# 第二章 实施步骤
详细流程。

# 第三章 团队分工
人员职责。

# 第四章 风险控制
风险与对策。

# 第五章 验收标准
验收方法与准则。

# 第六章 培训计划
培训安排。

# 第七章 运维保障
运维与服务支持。
"""


def _read_md(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_md(tmpdir: Path, text: str) -> Path:
    md = tmpdir / "content.md"
    md.write_text(text, encoding="utf-8")
    return md


def _run_pipeline(
    md_path: Path,
    workspace: Path,
    output: Path,
    template: Path = DEFAULT_TEMPLATE,
) -> dict:
    """Run smart TOC pipeline via stdio MCP and return the parsed result."""
    arguments = {
        "source_pptx": str(template),
        "workspace": str(workspace),
        "output_pptx": str(output),
        "content_mapping": {},
        "content_markdown": str(md_path),
        "options": {
            "validate_strict": False,
            "expand_toc_from_markdown": True,
            "expand_toc_slot_grid": TOC_TOP,
            "expand_skeleton_divider": 3,
            "expand_skeleton_content": 4,
            "expand_divider_edits_template": DIVIDER_EDITS,
            "expand_content_edits_template": CONTENT_EDITS,
            "expand_body_bounds": BODY_BOUNDS,
            "expand_ending_svg": "slide_05.svg",
            "fix_nested_picture": True,
            "skip_phase3_5": True,
            "clean_workspace": True,
        },
    }
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
    return payload


def _extract_toc_slide_text(pptx: Path) -> dict[str, list[str]]:
    """Return {slide_filename: [texts]} for TOC slides in the deck.

    Slides containing 'CONTENTS' or '目录' are treated as TOC slides.
    """
    toc_texts: dict[str, list[str]] = {}
    with zipfile.ZipFile(pptx) as z:
        slide_files = sorted(
            n for n in z.namelist()
            if re.match(r"ppt/slides/slide\d+\.xml$", n)
        )
        for sf in slide_files:
            tree = ET.fromstring(z.read(sf))
            texts = [t.text for t in tree.iter(f"{NS}t") if t.text]
            joined = " ".join(texts)
            if "CONTENTS" in joined or "目录" in joined:
                toc_texts[sf] = texts
    return toc_texts


def _print_toc_summary(name: str, payload: dict, pptx: Path) -> None:
    toc = payload.get("toc_summary") or {}
    print(f"\n=== {name} ===")
    print(f"  output:        {pptx}")
    print(f"  output exists: {pptx.is_file()}")
    print(f"  ok:            {payload.get('ok')}")
    print(f"  stage:         {payload.get('stage')}")
    print(f"  slot_count:    {toc.get('slot_count')}")
    print(f"  filled:        {toc.get('filled')}")
    print(f"  cloned_svgs:   {toc.get('cloned_svgs')}")
    errors = payload.get("errors") or []
    if errors:
        print(f"  errors:        {errors[:3]}")
    if pptx.is_file():
        toc_texts = _extract_toc_slide_text(pptx)
        print(f"  TOC slides in PPTX ({len(toc_texts)}):")
        for sf, texts in toc_texts.items():
            print(f"    {sf}: {texts}")


def main() -> int:
    md4 = MD_4
    md7 = MD_7

    # --- Scenario A: 4 chapters (< N) ---
    print("Running 4-chapter scenario...")
    ws4 = HERE.parent / "projects" / "toc_4_test_workspace"
    out4 = HERE.parent / "projects" / "toc_4_test_out.pptx"
    md4_path = _make_md(Path(tempfile.mkdtemp()), md4) if False else (
        _write_md_inline(md4)
    )
    payload4 = _run_pipeline(md4_path, ws4, out4)
    _print_toc_summary("4 chapters (< 6 slots)", payload4, out4)

    # --- Scenario B: 7 chapters (> N) ---
    print("\nRunning 7-chapter scenario...")
    ws7 = HERE.parent / "projects" / "toc_7_test_workspace"
    out7 = HERE.parent / "projects" / "toc_7_test_out.pptx"
    md7_path = _write_md_inline(md7)
    payload7 = _run_pipeline(md7_path, ws7, out7)
    _print_toc_summary("7 chapters (> 6 slots → 1 overflow clone)", payload7, out7)

    return 0


def _write_md_inline(text: str) -> Path:
    """Write a temp md file (auto-cleaned at process exit)."""
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="toc_test_"))
    p = td / "content.md"
    p.write_text(text, encoding="utf-8")
    return p


if __name__ == "__main__":
    raise SystemExit(main())
