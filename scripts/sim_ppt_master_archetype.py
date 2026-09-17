"""Simulate ppt-master's archetype selection for boteng's 6 content pages.

Reads the actual boteng markdown sections and walks ppt-master's
Composition Patterns to decide which archetype each content page should
use. Compares with what native_fill currently renders.

Run from native_fill repo root:
    PYTHONIOENCODING=utf-8 python -X utf8 scripts/sim_ppt_master_archetype.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MD_PATH = ROOT / "3山西柏腾科技有限公司采购制度.md"


def split_sections(md: str) -> list[dict]:
    """Split boteng markdown into H1 sections (the content pages).

    Boteng uses both `# 前言` and `# **一、****目的**` style headings
    (the second one has inline bold). We strip markdown emphasis and
    normalize the title text to the canonical form.
    """
    sections: list[dict] = []
    cur: dict | None = None
    for line in md.splitlines():
        stripped = line.strip()
        # Match H1 with optional inline bold.
        is_h1 = stripped.startswith("# ") and not stripped.startswith("## ")
        if is_h1:
            if cur:
                sections.append(cur)
            title = stripped[2:].strip()
            # Strip inline markdown emphasis (**...**).
            title = re.sub(r"\*\*(.+?)\*\*", r"\1", title)
            cur = {"title": title, "body_lines": []}
        elif cur is not None:
            cur["body_lines"].append(line)
    if cur:
        sections.append(cur)

    canonical = {
        "前言", "一、目的", "二、适用范围", "三、基本原则",
        "四、工作程序", "附件：",
    }
    out = []
    for s in sections:
        if s["title"] in canonical:
            s["body"] = "\n".join(s["body_lines"]).strip()
            out.append(s)
    return out


def pick_archetype(section: dict) -> tuple[str, str]:
    """Simulate ppt-master's archetype decision (Composition Patterns)."""
    title = section["title"]
    body = section["body"]
    chars = len(body)
    n_h2 = len(re.findall(r"^## ", body, re.MULTILINE))

    # 1. table → table_summary
    if title == "附件：":
        return ("table_summary", "6-column revision table")

    # 2. n_h2 >= 3 → process_timeline (4-step)
    if n_h2 >= 3:
        return ("process_timeline",
                f"4 macro phases by semantic clustering of {n_h2} H2 sub-rules")

    # 3. Long single paragraph (>=80 chars, no H2) → content_caption
    if chars >= 80 and n_h2 == 0:
        return ("content_caption",
                f"left 256px rail + right 816px panel for {chars} chars body")

    # 4. Medium single sentence (20-79 chars, no H2) → hero_statement
    if 20 <= chars <= 79 and n_h2 == 0:
        return ("hero_statement",
                f"1152x528 tinted field + 68px bold headline (1-line claim, {chars} chars)")

    # 5. 1 card with 2-5 short keyword items → kpi_dashboard
    items = [s.strip() for s in re.split(r"[。;,\\n]", body) if 2 <= len(s.strip()) <= 12]
    if 2 <= len(items) <= 5:
        return ("kpi_dashboard", f"{len(items)} KPI tiles (each <=12 chars keyword)")

    # 6. 2 H2 with parallel theses → three_card
    if n_h2 == 2:
        return ("three_card", "3-card synthesis from 2 H2 + synthesized 3rd")

    return ("title_content", "fallback (title + body)")


def main() -> int:
    md = MD_PATH.read_text(encoding="utf-8")
    sections = split_sections(md)
    print("=" * 100)
    print(f"{'#':<4}{'section':<18}{'chars':<8}{'#h2':<6}"
          f"{'archetype':<22}{'why this layout'}")
    print("=" * 100)
    for i, sec in enumerate(sections, 1):
        archetype, reason = pick_archetype(sec)
        chars = len(sec["body"])
        n_h2 = len(re.findall(r"^## ", sec["body"], re.MULTILINE))
        print(f"{i:<4}{sec['title']:<18}{chars:<8}{n_h2:<6}"
              f"{archetype:<22}{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())