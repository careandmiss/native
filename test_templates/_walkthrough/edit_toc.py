"""Edit slide_02.svg TOC slots — fill all 6 with content from the
boteng MD.

This script edits the round-tripped TOC slide by replacing the
placeholder text inside 12 specific shape groups (6 slots × 2 lines
each: title + subtitle). It writes the file back in place so the
subsequent svg_quality_checker and svg_to_pptx steps see the edits.
"""

import re
from pathlib import Path

WS = Path(__file__).resolve().parent

# 6 TOC entries — title, subtitle (per row, left then right column).
TOC_ENTRIES = [
    # row 1
    ("前言", "Preface"),
    ("一、目的", "Purpose"),
    # row 2
    ("二、适用范围", "Scope"),
    ("三、基本原则", "Core Principles"),
    # row 3
    ("四、工作程序", "Procedure"),
    ("附件：", "Annex"),
]

# Map from row index (0..2) and column (L|R) to the two shape ids:
# (accent, title, subtitle).
SLOT_SHAPES = {
    (0, "L"): ("shape-62", "shape-69", "shape-70"),
    (0, "R"): ("shape-71", "shape-72", "shape-73"),
    (1, "L"): ("shape-76", "shape-77", "shape-78"),
    (1, "R"): ("shape-79", "shape-80", "shape-81"),
    (2, "L"): ("shape-85", "shape-86", "shape-87"),
    (2, "R"): ("shape-88", "shape-89", "shape-90"),
}


def replace_tspan_in_shape(data: str, shape_id: str, new_text: str) -> str:
    """Replace the first <tspan> inside the <g id="shape_id" ...> block.

    Keeps every other attribute (font-size, fill, position, etc.) so
    we don't accidentally alter the layout. If the shape's text has
    no <tspan>, falls back to its <text>.
    """
    pat = rf'(<g id="{shape_id}"[^>]*>)(.*?)(</g>)'
    m = re.search(pat, data, re.DOTALL)
    if not m:
        print(f"  WARN: shape {shape_id} not found")
        return data
    head, body, tail = m.group(1), m.group(2), m.group(3)
    if "<tspan" in body:
        new_body = re.sub(
            r"(<tspan[^>]*>)[^<]*(</tspan>)",
            rf"\g<1>{new_text}\g<2>",
            body, count=1,
        )
    elif "<text" in body:
        new_body = re.sub(
            r"(<text[^>]*>)[^<]*(</text>)",
            rf"\g<1>{new_text}\g<2>",
            body, count=1,
        )
    else:
        print(f"  WARN: shape {shape_id} has no <text>/<tspan>")
        return data
    return data.replace(m.group(0), head + new_body + tail)


def main() -> None:
    slide_path = WS / "authoring-svg-flat" / "slide_02.svg"
    data = slide_path.read_text(encoding="utf-8")
    print(f"editing {slide_path.name}")
    for (row, col), (accent_id, title_id, sub_id) in SLOT_SHAPES.items():
        title, subtitle = TOC_ENTRIES[row * 2 + (0 if col == "L" else 1)]
        print(f"  row{row+1} {col}  title_id={title_id}  -> {title!r}")
        data = replace_tspan_in_shape(data, title_id, title)
        data = replace_tspan_in_shape(data, sub_id, subtitle)
    slide_path.write_text(data, encoding="utf-8")
    print("saved")


if __name__ == "__main__":
    main()
