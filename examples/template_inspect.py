#!/usr/bin/env python3
"""Profile each .pptx in test_templates/ — palette, font rhythm, slide count.

Output: prints one block per template, and writes a JSON profile to
``test_templates/profiles.json`` for later use by ``multi_template_test.py``.

Run:
    python examples/template_inspect.py [dir]

Default dir = ``<repo>/test_templates``.

This is the **first step** of building a cross-template validation set for
Phase C. Use it to verify the palette / font / slide-count variety is
real, before we trust any template-agnostic claim.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from statistics import median, quantiles

if sys.version_info >= (3, 11):
    from statistics import quantiles as _q
else:  # pragma: no cover
    _q = quantiles


def _read_xml(z: zipfile.ZipFile, name: str) -> str:
    try:
        return z.read(name).decode("utf-8", errors="replace")
    except KeyError:
        return ""


def _slide_texts(z: zipfile.ZipFile) -> list[list[str]]:
    out: list[list[str]] = []
    slide_re = re.compile(r"ppt/slides/slide\d+\.xml$")
    for name in sorted(z.namelist()):
        if not slide_re.match(name):
            continue
        data = _read_xml(z, name)
        out.append(re.findall(r"<a:t>([^<]+)</a:t>", data))
    return out


def _collect_srgb_fills(z: zipfile.ZipFile) -> Counter:
    c: Counter = Counter()
    slide_re = re.compile(r"ppt/slides/slide\d+\.xml$")
    for name in z.namelist():
        if not slide_re.match(name):
            continue
        data = _read_xml(z, name)
        for m in re.findall(r"<a:srgbClr val=\"([0-9A-Fa-f]{6})\"", data):
            c[m.upper()] += 1
    return c


def _collect_scheme_refs(z: zipfile.ZipFile) -> Counter:
    c: Counter = Counter()
    slide_re = re.compile(r"ppt/slides/slide\d+\.xml$")
    for name in z.namelist():
        if not slide_re.match(name):
            continue
        data = _read_xml(z, name)
        for m in re.findall(r"<a:schemeClr val=\"([a-zA-Z0-9]+)\"", data):
            c[m] += 1
    return c


def _theme_palette(z: zipfile.ZipFile) -> dict[str, str]:
    """Read theme1.xml and resolve accent1..6, dk1/2, lt1/2 → hex.

    Each ``<a:NAME>`` slot in ``clrScheme`` is followed (after optional
    whitespace) by either an ``<a:srgbClr val="..."/>`` or an
    ``<a:sysClr ... lastClr="..."/>``. We split the theme at slot
    boundaries and pull the first hex inside each.
    """
    palette: dict[str, str] = {}
    theme = _read_xml(z, "ppt/theme/theme1.xml")
    if not theme:
        return palette
    slot_re = re.compile(
        r"<a:(dk1|lt1|dk2|lt2|accent[1-6]|hlink|folHlink)>"
        r"(.*?)</a:\1>",
        re.DOTALL,
    )
    color_re = re.compile(
        r"<a:srgbClr val=\"([0-9A-Fa-f]{6})\"|"
        r"<a:sysClr[^>]*lastClr=\"([0-9A-Fa-f]{6})\""
    )
    for slot_match in slot_re.finditer(theme):
        slot = slot_match.group(1)
        body = slot_match.group(2)
        color_match = color_re.search(body)
        if not color_match:
            continue
        hex_str = (color_match.group(1) or color_match.group(2) or "").upper()
        if hex_str:
            palette[slot] = hex_str
    return palette


def _collect_fonts_pt(z: zipfile.ZipFile) -> list[float]:
    """Inline sz="2400" (hundredths of a point) → 24.0 pt."""
    out: list[float] = []
    slide_re = re.compile(r"ppt/slides/slide\d+\.xml$")
    for name in z.namelist():
        if not slide_re.match(name):
            continue
        data = _read_xml(z, name)
        for m in re.findall(r"sz=\"(\d+)\"", data):
            out.append(int(m) / 100.0)
    return out


def _hls_saturation_and_lightness(hex_str: str) -> tuple[float, float]:
    """Return (saturation, lightness) in 0..1; skip the hue channel."""
    import colorsys
    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    _, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    return saturation, lightness


def _saturation(hex_str: str) -> float:
    """HSL saturation; 0 = gray, 1 = fully saturated."""
    s, _ = _hls_saturation_and_lightness(hex_str)
    return s


def _is_neutral(hex_str: str) -> bool:
    """Truly neutral: pure gray, black, white, or pale tint.

    Three filters, all required to fail:
      1. HLS saturation < 0.30 → pure gray
      2. Lightness < 0.10 → near-black
      3. Lightness > 0.85 → pale tint (``#D2DAF9`` math-sat 0.77
         but L=0.90 — visually a near-white tint, not a brand color)

    Brand colors typically have lightness 0.15..0.85. ``#1D2CAB``
    (boteng brand, L=0.39, s=0.71) passes; ``#002060`` (L=0.19,
    s=1.0) passes; ``#D2DAF9`` is filtered out.
    """
    s, lightness = _hls_saturation_and_lightness(hex_str)
    if s < 0.30:
        return True
    if lightness < 0.10:
        return True
    if lightness > 0.85:
        return True
    return False


def _dominant_brand(hex_counts: Counter, theme_palette: dict[str, str]) -> dict[str, str]:
    """Pick the most-saturated, most-frequent non-neutral color as brand.

    Falls back to ``accent1`` from theme palette when no slide-level
    saturated color reaches ``count >= 2``. Sorts by ``saturation DESC,
    count DESC`` so a strongly saturated but rare color (e.g. the brand
    used on a single cover bar) beats a frequent muted text color.
    """
    saturated = [
        (c, n) for c, n in hex_counts.items()
        if not _is_neutral(c) and n >= 1
    ]
    if saturated:
        saturated.sort(key=lambda cn: (_saturation(cn[0]), cn[1]), reverse=True)
        primary = saturated[0][0]
        secondary = saturated[1][0] if len(saturated) > 1 else ""
        return {"primary": primary, "secondary": secondary}
    # fallback to theme accent1
    return {"primary": theme_palette.get("accent1", ""), "secondary": ""}


def profile_template(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        slide_texts = _slide_texts(z)
        srgb = _collect_srgb_fills(z)
        scheme = _collect_scheme_refs(z)
        theme_palette = _theme_palette(z)
        fonts = _collect_fonts_pt(z)
    brand = _dominant_brand(srgb, theme_palette)
    return {
        "path": str(path),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "slide_count": len(slide_texts),
        "texts_per_slide": [len(t) for t in slide_texts],
        "srgb_top10": [
            {"hex": f"#{c}", "count": n}
            for c, n in srgb.most_common(10)
        ],
        "scheme_refs": dict(scheme),
        "theme_palette": {
            k: f"#{v}" for k, v in theme_palette.items() if v
        },
        "brand_inferred": brand,
        "font_pt": {
            "count": len(fonts),
            "min": min(fonts) if fonts else None,
            "median": median(fonts) if fonts else None,
            "q25": _q(fonts, n=4)[0] if len(fonts) >= 4 else None,
            "q75": _q(fonts, n=4)[2] if len(fonts) >= 4 else None,
            "max": max(fonts) if fonts else None,
        },
    }


def print_profile(p: dict) -> None:
    print(f"=== {p['name']} ({p['size_bytes'] // 1024} KB) ===")
    print(f"  slides: {p['slide_count']}, texts/slide: {p['texts_per_slide']}")
    srgb_str = " ".join(
        f"{item['hex']}x{item['count']}" for item in p["srgb_top10"][:5]
    )
    print(f"  srgb top5: {srgb_str}")
    print(f"  scheme refs: {p['scheme_refs']}")
    theme = " ".join(f"{k}={v}" for k, v in list(p["theme_palette"].items())[:6])
    print(f"  theme palette: {theme}")
    print(f"  brand (inferred): {p['brand_inferred']}")
    f = p["font_pt"]
    if f["count"]:
        q25 = f"{f['q25']:.0f}" if f["q25"] is not None else "n/a"
        q75 = f"{f['q75']:.0f}" if f["q75"] is not None else "n/a"
        print(
            f"  font pt: min={f['min']:.0f} med={f['median']:.0f} "
            f"q25={q25} q75={q75} max={f['max']:.0f} "
            f"(n={f['count']})"
        )
    else:
        print(f"  font pt: none inline (uses theme defaults)")
    print()


def main() -> int:
    dir_ = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "test_templates"
    )
    if not dir_.is_dir():
        print(f"ERROR: directory not found: {dir_}", file=sys.stderr)
        return 1
    profiles = []
    for p in sorted(dir_.glob("*.pptx")):
        try:
            profile = profile_template(p)
            profiles.append(profile)
            print_profile(profile)
        except Exception as exc:
            print(f"FAILED on {p.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    out_path = dir_ / "profiles.json"
    out_path.write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"profile summary written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
