"""scripts/inspect_pptx.py — 解压 pptx、统计 text_runs、计算 SVG bbox 覆盖率。

用法:
    python scripts/inspect_pptx.py <pptx_path>
    python scripts/inspect_pptx.py <pptx_path> --svg <svg_path>
    python scripts/inspect_pptx.py <pptx_path> --workspace <workspace_dir>
    python scripts/inspect_pptx.py <pptx_path> --workspace <workspace_dir> --slides 6,8,14

设计要点:
- text_run 计数复用 examples/test_toc_4_vs_7.py:156 的 ElementTree namespace 模式
  (NS = "http://schemas.openxmlformats.org/drawingml/2006/main"),比 regex 更准、支持带属性
- 覆盖率 = <rect> + <path> 的 bbox 面积 / body_bounds 面积(body_bounds = "120 130 1060 480")
- workspace 模式自动从 page_plan.json 找 content SVG 与 slide 映射
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# 复用 examples/test_toc_4_vs_7.py:156 的 namespace 常量
NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
BODY_BOUNDS = "120 130 1060 480"


def count_text_runs(xml_text: str) -> int:
    """数 <a:t> 节点数(支持带属性版本,如 <a:t xml:space="preserve">)。"""
    root = ET.fromstring(xml_text)
    return sum(1 for _ in root.iter(f"{{{NS}}}t"))


def slide_text_preview(xml_text: str, limit: int = 30) -> str:
    """返回拼接后的 <a:t> 内容预览(去 namespace 后只看 text 字段)。"""
    root = ET.fromstring(xml_text)
    parts = [t.text or "" for t in root.iter(f"{{{NS}}}t")]
    joined = " ".join(parts).strip()
    return (joined[:limit] + "…") if len(joined) > limit else joined


def body_cards_stats(svg_text: str, body_bounds: str = BODY_BOUNDS) -> dict:
    """统计 body_cards 组内的 rect 覆盖面积 + text 节点数。

    返回 ``{"coverage": float (0.0-1.0), "text_nodes": int}``。

    关键洞察:bbox coverage = 100% 不等于内容密集——一个全屏半透明 rect
    就是 100%,但里面可能只有 1 个 text 节点。所以必须同时看 text_nodes
    才能反映真正的"内容密度"。Phase 1/3 应同时跟踪两个指标。
    """
    bx, by, bw, bh = (float(t) for t in body_bounds.split())
    total = bw * bh
    root = ET.fromstring(svg_text)
    svg_ns = "{http://www.w3.org/2000/svg}"
    filled = 0.0
    text_count = 0
    for g in root.iter(f"{svg_ns}g"):
        if g.get("id") != "body_cards":
            continue
        for elem in g.iter():
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag in ("rect", "path"):
                x = elem.get("x")
                y = elem.get("y")
                w = elem.get("width")
                h = elem.get("height")
                if not all([x, y, w, h]):
                    continue
                try:
                    filled += float(w) * float(h)
                except ValueError:
                    continue
            elif tag == "text":
                text_count += 1
        break  # 只数第一个 body_cards
    return {"coverage": filled / total, "text_nodes": text_count}


# Backward-compat alias (旧代码引用 coverage() 的地方保留)
def coverage(svg_text: str, body_bounds: str = BODY_BOUNDS) -> float:
    """DEPRECATED: 用 ``body_cards_stats`` 同时获取 coverage + text_nodes。

    仅保留向后兼容——返回的只是 coverage,不含 text_nodes。
    """
    return body_cards_stats(svg_text, body_bounds)["coverage"]


def inspect_pptx(pptx_path: Path) -> list[dict]:
    """返回 [{slide_idx, slide_xml, text_runs, preview}, ...]"""
    out: list[dict] = []
    with zipfile.ZipFile(pptx_path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda s: int(re.search(r"slide(\d+)", s).group(1)),
        )
        for s in slides:
            idx = int(re.search(r"slide(\d+)", s).group(1))
            xml = z.read(s).decode("utf-8", errors="replace")
            out.append(
                {
                    "slide_idx": idx,
                    "slide_xml": s,
                    "text_runs": count_text_runs(xml),
                    "preview": slide_text_preview(xml),
                }
            )
    return out


def resolve_content_slides(
    workspace: Path | None, targets: list[int] | None
) -> list[dict]:
    """从 page_plan.json 找 content SVG 与 pptx 演示顺序的映射。

    返回的 ``pptx_idx`` 是该 SVG 在 pptx 文件中的演示顺序(1-indexed,即等于
    plan.pages 列表中的 index + 1)。``s  ``source_slide`` 是模板源 slide 编号
    (一个模板源可能被多个 partNN_content.svg 复用,如 slide 4 同时是 part01
    和 part02 的源)。

    ``targets`` 按 ``pptx_idx`` 过滤(不是 source_slide)——这符合用户自然预期:
    "slide 6 in the output PPTX"。
    """
    if not workspace or not (workspace / "page_plan.json").is_file():
        return []
    plan = json.loads((workspace / "page_plan.json").read_text(encoding="utf-8"))
    pairs: list[dict] = []
    for i, page in enumerate(plan.get("pages", [])):
        svg = page.get("svg", "")
        if svg.endswith("_content.svg"):
            pairs.append(
                {
                    "pptx_idx": i + 1,
                    "source_slide": page["source_slide"],
                    "svg": svg,
                }
            )
    if targets:
        pairs = [p for p in pairs if p["pptx_idx"] in targets]
    return pairs


def main() -> int:
    p = argparse.ArgumentParser(
        description="解压 pptx、统计 text_runs、计算 SVG bbox 覆盖率"
    )
    p.add_argument("pptx_path", type=Path)
    p.add_argument("--svg", type=Path, help="单个 SVG 文件算覆盖率")
    p.add_argument(
        "--workspace",
        type=Path,
        help="workspace 目录(含 page_plan.json + authoring-svg-flat/)",
    )
    p.add_argument(
        "--slides",
        type=str,
        help="逗号分隔的 source_slide 列表,如 '6,8,14'",
    )
    p.add_argument("--body-bounds", default=BODY_BOUNDS)
    args = p.parse_args()

    info = inspect_pptx(args.pptx_path)
    print(f"=== {args.pptx_path.name} — {len(info)} slides ===")
    for s in info:
        print(
            f"  slide{s['slide_idx']:2d}: text_runs={s['text_runs']:3d}  "
            f"preview={s['preview']!r}"
        )

    # Workspace 模式:自动找 content SVG + 算覆盖率
    targets = [int(x) for x in args.slides.split(",")] if args.slides else None
    if args.workspace:
        svg_root = args.workspace / "authoring-svg-flat"
        pairs = resolve_content_slides(args.workspace, targets)
        if pairs:
            print(
                f"\n=== Body cards stats (body_bounds={args.body_bounds}) ===\n"
                f"  {'pptx':<6} {'svg':<35} {'coverage':<10} {'text_nodes'}"
            )
            for pair in pairs:
                svg_path = svg_root / pair["svg"]
                if not svg_path.is_file():
                    print(f"  {pair['svg']}: NOT FOUND")
                    continue
                stats = body_cards_stats(
                    svg_path.read_text(encoding="utf-8"), args.body_bounds
                )
                print(
                    f"  {pair['pptx_idx']:<6} {pair['svg']:<35} "
                    f"{stats['coverage'] * 100:>5.1f}%     {stats['text_nodes']}"
                )

    # 单 SVG 模式
    if args.svg and args.svg.is_file():
        cov = coverage(args.svg.read_text(encoding="utf-8"), args.body_bounds)
        print(f"\n{args.svg.name}: coverage={cov * 100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())