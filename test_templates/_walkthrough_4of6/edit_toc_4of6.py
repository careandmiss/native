"""Edit slide_02.svg TOC slots — fill ONLY 4 of 6 (top 2 rows),
leaving row 3 empty. Mimics what our LLM does with generic_content.md
(4 chapters, 6 TOC slots).
"""

import re
from pathlib import Path

WS = Path(__file__).resolve().parent

# 4 TOC entries — only top 2 rows, both columns.
TOC_ENTRIES = [
    ("前言", "Preface"),
    ("一、目的", "Purpose"),
    ("二、适用范围", "Scope"),
    ("三、基本原则", "Core Principles"),
]

SLOT_SHAPES = {
    (0, "L"): ("shape-62", "shape-69", "shape-70"),
    (0, "R"): ("shape-71", "shape-72", "shape-73"),
    (1, "L"): ("shape-76", "shape-77", "shape-78"),
    (1, "R"): ("shape-79", "shape-80", "shape-81"),
    # row 2 (y=557) left empty on purpose
}


def replace_tspan_in_shape(data: str, shape_id: str, new_text: str) -> str:
    pat = rf'(<g id="{shape_id}"[^>]*>)(.*?)(</g>)'
    m = re.search(pat, data, re.DOTALL)
    if not m:
        return data
    head, body, tail = m.group(1), m.group(2), m.group(3)
    if "<tspan" in body:
        new_body = re.sub(
            r"(<tspan[^>]*>)[^<]*(</tspan>)",
            rf"\g<1>{new_text}\g<2>", body, count=1,
        )
    elif "<text" in body:
        new_body = re.sub(
            r"(<text[^>]*>)[^<]*(</text>)",
            rf"\g<1>{new_text}\g<2>", body, count=1,
        )
    else:
        return data
    return data.replace(m.group(0), head + new_body + tail)


def main() -> None:
    slide_path = WS / "authoring-svg-flat" / "slide_02.svg"
    data = slide_path.read_text(encoding="utf-8")
    print(f"editing {slide_path.name} — 4 of 6 TOC slots")
    for (row, col), (accent_id, title_id, sub_id) in SLOT_SHAPES.items():
        title, subtitle = TOC_ENTRIES[row * 2 + (0 if col == "L" else 1)]
        print(f"  row{row+1} {col}  title_id={title_id}")
        data = replace_tspan_in_shape(data, title_id, title)
        data = replace_tspan_in_shape(data, sub_id, subtitle)
    slide_path.write_text(data, encoding="utf-8")
    print("saved (row 3 left empty)")


if __name__ == "__main__":
    main()
