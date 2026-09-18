"""Clone slide_04 of a PPTX into 3 chrome variants for content-adaptive layout.

Goal
----
Take a 5-slide boteng template (``柏腾ppt模版.pptx``) and append three cloned
variants of ``slide_04`` (the lone fillable content canvas) so that the
content pages in the regenerated deck can pull from a source slide whose
chrome decorations match the archetype being rendered:

* slide_06 (content_hero):  strip ellipse accents + chapter title bar +
  dashed connector — only the outer frame + chapter title bar + background
  remain.
* slide_07 (content_wide):  same as hero but keep the dashed connector
  (shape-21).
* slide_08 (content_full):   identical clone — no stripping.

Each clone is appended in sequence so the resulting PPTX has 8 slides total
(the original 5 + 3 appended). The background image reference (``image3.png``),
``shape-3`` outer frame, and ``shape-17`` chapter title bar are preserved
on slides 06 and 07; slide 08 is byte-equivalent to slide 04 (modulo
python-pptx internal ids).

A side-by-side manifest JSON is written next to the output PPTX so the
content-adaptive pipeline (Module 6) can pick the correct source slide
without having to re-derive the route.

CLI
---
::

    python tools/clone_content_template.py \\
        --template "D:/Code/tst/native_fill/柏腾ppt模版.pptx" \\
        --out "D:/Code/tst/native_fill/柏腾ppt模版_v2.pptx"

After running, ``柏腾ppt模版_v2.pptx`` has 8 slides and a sibling manifest
``柏腾ppt模版_v2.manifest.json``.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from pptx import Presentation

# Body rectangle (in EMU) on the 1280x720 (12192000 x 6858000 EMU) canvas.
# Matches ``slide_04``'s fillable region; the manifest embeds it so the
# pipeline can mirror this geometry into the regenerated SVG.
BODY_RECT_EMU = [789940, 976630, 10711180, 5052060]

# Strip policy is matched by ``shape_type`` so it is template-agnostic
# (does not depend on Chinese vs English ``<p:cNvPr name="...">`` text).
# On the boteng template slide_04 the relevant decorations are:
#   * 组合 13  (left ellipse accents,  shape_type=GROUP)   → strip on hero/wide
#   * 文本框 16 (top chapter title text, shape_type=TEXT_BOX) → keep
#   * 组合 17  (right ellipse accents, shape_type=GROUP)   → strip on hero/wide
#   * 直接连接符 20 (dashed connector,  shape_type=LINE)    → strip on hero only
#   * 文本框 21 (outer frame,        shape_type=TEXT_BOX) → keep
#   * 文本框 2  (empty body canvas,  shape_type=TEXT_BOX) → keep
# ``图片 1`` (background picture) is kept on all variants.
#
# Note: ppt-master roundtrip SVGs reference these as shape-14/15/16, shape-17,
# shape-18/19/20, shape-21, shape-3, and content-body (one PPTX shape
# becomes several SVG ids because groups expand). Matching by shape_type
# is the only reliable way to drive stripping from the PPTX side.
from pptx.enum.shapes import MSO_SHAPE_TYPE

# GROUP shapes on slide_04 are the two ellipse decorations; strip them.
# LINE shape is the dashed connector; strip it only on hero.
_STRIP_HERO_TYPES = frozenset({MSO_SHAPE_TYPE.GROUP, MSO_SHAPE_TYPE.LINE})
_STRIP_WIDE_TYPES = frozenset({MSO_SHAPE_TYPE.GROUP})


def _copy_image_rels(src_slide, dst_slide) -> dict[str, str]:
    """Copy IMAGE-type relationships from source slide to cloned slide.

    ``Presentation.add_slide(blank_layout)`` gives the new slide a rels file
    that only contains the slideLayout relationship. But the deep-copied
    shape XML still references image rIds from the source slide (e.g.
    ``r:embed="rId1"`` for the background picture). Without the
    corresponding rels entries, python-pptx serializes the cloned slide
    with those rIds re-pointed at random other relationships — typically
    the slide layout — and ``pptx_to_svg`` then writes
    ``<image href="../images/slideLayout4.xml">`` which the vendor
    ``svg_to_pptx`` rejects as "unsupported file extension".

    Returns:
        A mapping ``{old_rId: new_rId}`` so the caller can rewrite the
        deep-copied shape XML's ``r:embed="..."`` attributes to point at
        the freshly allocated rIds. Empty dict if source had no image
        rels (or all rIds happened to be allocated to the same numbers).
    """
    mapping: dict[str, str] = {}
    IMAGE_RELS_TYPE = (
        "http://schemas.openxmlformats.org/officeDocument/2006/"
        "relationships/image"
    )
    for rId, rel in src_slide.part.rels.items():
        if rel.reltype != IMAGE_RELS_TYPE:
            continue
        new_rId = dst_slide.part.rels.get_or_add(
            IMAGE_RELS_TYPE, rel.target_part
        )
        if new_rId != rId:
            mapping[rId] = new_rId
    return mapping


def _rewrite_picture_embeds(slide, mapping: dict[str, str]) -> int:
    """Rewrite ``r:embed="..."`` / ``r:link="..."`` attributes in shapes.

    Walks every ``<a:blip>`` in the slide's spTree and replaces any rId
    that appears in ``mapping`` with the new rId. Returns the count of
    attributes rewritten.

    Args:
        slide: Newly added ``Slide`` whose shapes we want to keep valid.
        mapping: Output of :func:`_copy_image_rels`.

    Returns:
        Number of rId attributes rewritten.
    """
    if not mapping:
        return 0
    R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    rewritten = 0
    for blip in slide.shapes._spTree.iter(A_BLIP):
        for attr in (f"{R_NS}embed", f"{R_NS}link"):
            old = blip.get(attr)
            if old in mapping:
                blip.set(attr, mapping[old])
                rewritten += 1
    return rewritten


def clone_slide(prs, source_idx: int):
    """Deep-clone ``prs.slides[source_idx]`` and append it to the deck.

    We use the blank layout (index 6) so the clone carries no inherited
    placeholder decorations. Each shape from the source slide is
    ``copy.deepcopy``'d and re-attached to the new ``<p:spTree>`` in
    original order, immediately before the trailing ``<p:extLst>``.

    Image relationships (``rIdN → media/imageN.png``) are copied
    separately so the cloned slide's ``<a:blip r:embed="rId1">`` still
    resolves to a real image rather than re-binding to the slide layout
    (see :func:`_copy_image_rels`).

    Args:
        prs: ``Presentation`` instance (mutated in place).
        source_idx: 0-based index of the source slide (3 → slide_04).

    Returns:
        The new ``Slide`` object appended to ``prs.slides``.
    """
    src = prs.slides[source_idx]
    blank_layout = prs.slide_layouts[min(6, len(prs.slide_layouts) - 1)]
    new_slide = prs.slides.add_slide(blank_layout)
    rId_mapping = _copy_image_rels(src, new_slide)
    print(
        f"[clone] copied {len(rId_mapping)} image relationship(s) from "
        f"source slide (rId mapping: {rId_mapping or 'unchanged'})",
        file=sys.stderr,
    )
    for shp in src.shapes:
        el = copy.deepcopy(shp.element)
        new_slide.shapes._spTree.insert_element_before(el, "p:extLst")
    n_rewritten = _rewrite_picture_embeds(new_slide, rId_mapping)
    print(
        f"[clone] rewrote {n_rewritten} picture embed/link attr(s) "
        f"to use the new rIds",
        file=sys.stderr,
    )
    return new_slide


def strip_shapes_by_type(slide, types_to_strip: frozenset) -> int:
    """Remove top-level shapes whose ``shape_type`` is in ``types_to_strip``.

    Walks the slide's ``<p:spTree>`` children and matches the python-pptx
    ``shape.shape_type`` attribute (e.g. ``MSO_SHAPE_TYPE.GROUP`` for the
    ellipse groups, ``MSO_SHAPE_TYPE.LINE`` for the dashed connector).
    Group shapes (``<p:grpSp>``) are matched as a whole — we do not descend
    into their children. Returns the count of shapes removed (for
    diagnostics).

    Args:
        slide: ``Slide`` object whose ``shapes._spTree`` we mutate.
        types_to_strip: Frozenset of ``MSO_SHAPE_TYPE`` members.
    """
    removed = 0
    # Iterate the live slide.shapes collection (python-pptx already
    # tolerates mutation via its internal cache invalidation).
    for shp in list(slide.shapes):
        if shp.shape_type in types_to_strip:
            sp = shp._element
            sp.getparent().remove(sp)
            removed += 1
    return removed


def main() -> int:
    """CLI entrypoint: clone slide_04 three times, write output + manifest."""
    ap = argparse.ArgumentParser(
        description=(
            "Clone slide_04 of a PPTX into 3 chrome variants for "
            "content-adaptive layout."
        )
    )
    ap.add_argument(
        "--template",
        required=True,
        type=Path,
        help="Path to the source PPTX (e.g. 柏腾ppt模版.pptx).",
    )
    ap.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Path to write the cloned PPTX (e.g. 柏腾ppt模版_v2.pptx).",
    )
    args = ap.parse_args()

    prs = Presentation(args.template)
    n_src = len(prs.slides)
    print(f"[clone] source slide count: {n_src}", file=sys.stderr)
    if n_src < 5:
        print(
            f"[clone] WARNING: source has only {n_src} slides; "
            f"expected 5 so slide_04 may not exist.",
            file=sys.stderr,
        )

    src_idx = 3  # 0-based → slide_04

    # Clone 06 (hero) — strip the heaviest chrome decorations.
    s6 = clone_slide(prs, src_idx)
    n6 = strip_shapes_by_type(s6, _STRIP_HERO_TYPES)
    print(
        f"[clone] slide_06 (hero) stripped {n6} shapes "
        f"(groups + line): 组合 13, 组合 17, 直接连接符 20",
        file=sys.stderr,
    )

    # Clone 07 (wide) — same as hero but keep the dashed connector.
    s7 = clone_slide(prs, src_idx)
    n7 = strip_shapes_by_type(s7, _STRIP_WIDE_TYPES)
    print(
        f"[clone] slide_07 (wide) stripped {n7} shapes "
        f"(groups only): 组合 13, 组合 17",
        file=sys.stderr,
    )

    # Clone 08 (full) — no stripping, identical to slide_04.
    s8 = clone_slide(prs, src_idx)
    print(
        "[clone] slide_08 (full) — unchanged clone "
        "(stripped 0 shapes)",
        file=sys.stderr,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(args.out)
    n_out = len(prs.slides)
    print(
        f"[clone] wrote: {args.out} ({n_out} slides)",
        file=sys.stderr,
    )

    # Side-car manifest the content-adaptive pipeline reads to route each
    # archetype to the right source slide and to clamp the body bbox.
    manifest = {
        "hero": 6,
        "wide": 7,
        "full": 8,
        "body_rect_emu": BODY_RECT_EMU,
    }
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[clone] manifest: {manifest_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())