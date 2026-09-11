"""svg_edits.py — surgical SVG text edits by shape id.

The ppt-master native fill pipeline stores per-shape identity via the
``data-pptx-source-ref`` attribute on top-level ``<g id="shape-NN">``
containers. Each ``<text>`` element inside the shape carries the visual text.
Editing text in place preserves ``data-pptx-*`` so vendor ``svg_to_pptx
--roundtrip`` can rehydrate the original DrawingML byte-for-byte.

This module intentionally avoids the ppt-master ``template_text_slots`` library
(no CLI, depends on private slide_roster import path). ElementTree-based
in-place edits are enough for the native fill MCP.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from . import io_utils

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)  # keep default-namespace prefix clean


def _local(tag: str) -> str:
    """Strip {ns} from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _qname(tag: str) -> str:
    """Build a Clark-notation qualified name."""
    return f"{{{SVG_NS}}}{tag}"


def find_shape_groups(root: ET.Element, shape_ids: Iterable[str] | None = None) -> dict[str, ET.Element]:
    """Index top-level <g id="..."> elements keyed by id.

    If ``shape_ids`` is given, only matching ids are returned. Missing ids
    silently skipped — callers should warn separately.
    """
    wanted = set(shape_ids) if shape_ids is not None else None
    found: dict[str, ET.Element] = {}
    # Iterate direct children of <svg> first (authoring-svg-flat layout), then
    # descend one level for nested groups.
    for parent in (root, *list(root)):
        for g in parent.iter(_qname("g")):
            gid = g.get("id")
            if not gid:
                continue
            if wanted is None or gid in wanted:
                found.setdefault(gid, g)
    return found


def find_text_in_group(group: ET.Element, shape_id: str) -> ET.Element | None:
    """Return the first <text> inside the shape group, or None."""
    for elem in group.iter(_qname("text")):
        return elem
    # Some authoring SVGs split text across <tspan> siblings — return the
    # tspan's parent text element when present.
    for tspan in group.iter(_qname("tspan")):
        return tspan  # type: ignore[return-value]
    return None


def apply_text_edits(
    svg_path: Path,
    edits: dict[str, str],
    *,
    preserve_whitespace: bool = True,
) -> list[dict]:
    """Replace the visible text inside each <text> referenced by edits.

    Parameters
    ----------
    svg_path:
        Path to an authoring-svg-flat slide_NN.svg.
    edits:
        ``{shape_id: new_text}`` map. Unknown shape_ids are skipped and
        reported via the returned list as ``{"shape_id": ..., "status":
        "not_found"}``.
    preserve_whitespace:
        If True, set ``xml:space="preserve"`` on the text element to keep
        leading/trailing spaces (this is the ppt-master default).

    Returns
    -------
    list of ``{shape_id, status, old?, new?}`` records for audit.
    """
    svg_path = Path(svg_path)
    if not svg_path.is_file():
        raise FileNotFoundError(f"svg not found: {svg_path}")

    raw = io_utils.read_utf8(svg_path)
    try:
        tree = ET.ElementTree(ET.fromstring(raw))
    except ET.ParseError as exc:
        raise ValueError(f"svg is not well-formed XML: {svg_path}: {exc}") from exc

    root = tree.getroot()
    groups = find_shape_groups(root, edits.keys())
    audit: list[dict] = []

    for shape_id, new_text in edits.items():
        g = groups.get(shape_id)
        if g is None:
            audit.append({"shape_id": shape_id, "status": "not_found"})
            continue

        text_elem = find_text_in_group(g, shape_id)
        if text_elem is None:
            audit.append({"shape_id": shape_id, "status": "no_text_node"})
            continue

        # Capture pre-state for audit. itertext concatenates <text> and all
        # <tspan> descendants.
        old_text = "".join(text_elem.itertext()).strip()

        # Replace text. Strip <tspan> children because their dy/x overrides
        # would otherwise pin the new content to the old baseline.
        text_elem.text = new_text
        text_elem.tail = None
        for tspan in list(text_elem.findall(_qname("tspan"))):
            text_elem.remove(tspan)

        if preserve_whitespace:
            text_elem.set("xml:space", "preserve")
            # ElementTree writes the bare attribute; "xml:space" is allowed in
            # SVG/XML even when not declared in our tiny schema.

        audit.append(
            {
                "shape_id": shape_id,
                "status": "applied",
                "old": old_text,
                "new": new_text,
            }
        )

    # Write back atomically. UTF-8, XML declaration, default namespace.
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp_path = svg_path.with_suffix(svg_path.suffix + ".tmp")
    tmp_path.write_bytes(xml_bytes)
    tmp_path.replace(svg_path)
    return audit


def clone_slide(
    source_svg: Path,
    dest_svg: Path,
    *,
    rewire_to_slide: int,
) -> Path:
    """Copy an authoring SVG and rewrite ``data-pptx-source-ref`` values.

    When a page plan reuses a source slide (e.g. ``slide_03`` becomes both
    ``slide_03`` and ``slide_part02_div``), every ``data-pptx-source-ref``
    in the cloned SVG must point to the same logical slide (the source
    raster stays the same). The native pipeline uses ``rewire_to_slide``
    only when the plan registers a *different* source slide; for true
    duplication we keep the refs intact and let page_plan.json bind the
    clone to the original source_slide.

    This helper currently just copies the file (source refs already encode
    slide N), so callers must update page_plan.json to register the copy
    under the desired source_slide.
    """
    import shutil

    dest_svg = Path(dest_svg)
    io_utils.ensure_dir(dest_svg.parent)
    shutil.copy2(source_svg, dest_svg)
    return dest_svg.resolve()


def write_new_content_block(
    svg_path: Path,
    *,
    group_id: str,
    bounds: str,
    inner_svg: str,
) -> None:
    """Append a new authored ``<g id="..." data-pptx-bounds="...">`` block.

    Used for ``new_content_blocks`` in the native_fill tool input. The block
    is appended inside the existing ``<svg>`` root, just before ``</svg>``.
    ``bounds`` must follow the ``"x y width height"`` format.
    """
    if not re.fullmatch(r"-?\d+(\.\d+)?\s+-?\d+(\.\d+)?\s+\d+(\.\d+)?\s+\d+(\.\d+)?", bounds):
        raise ValueError(
            f"bounds must be 'x y width height' (unitless decimal): {bounds!r}"
        )

    raw = io_utils.read_utf8(svg_path)
    try:
        tree = ET.ElementTree(ET.fromstring(raw))
    except ET.ParseError as exc:
        raise ValueError(f"svg is not well-formed XML: {svg_path}: {exc}") from exc

    root = tree.getroot()
    if root.tag != _qname("svg"):
        raise ValueError(f"unexpected root tag in {svg_path}: {root.tag}")

    # Wrap inner_svg in a synthetic root to parse, then re-parent children.
    wrapper = (
        f'<wrap xmlns="{SVG_NS}">'
        f'<g id="{group_id}" data-pptx-bounds="{bounds}">{inner_svg}</g>'
        f"</wrap>"
    )
    parsed = ET.fromstring(wrapper)
    new_g = parsed.find(_qname("g"))
    if new_g is None:
        raise ValueError("inner_svg produced no <g> element")

    # Re-tag children into the SVG namespace (already are, but defensive).
    root.append(new_g)

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp = svg_path.with_suffix(svg_path.suffix + ".tmp")
    tmp.write_bytes(xml_bytes)
    tmp.replace(svg_path)
