"""Preflight checks for SVG destined for ppt-master Edit Native.

The ppt-master roundtrip pipeline (``svg_to_pptx.py --roundtrip``) has
four hard structural requirements that break exports with cryptic
messages. This module surfaces those requirements as deterministic,
machine-checkable rules so an operator can detect a bad SVG before
feeding it to the vendor.

Rules (cross-reference: docs/ppt生成流程.md §6):

  P1. Every ``id`` must be unique within a slide — duplicate ids in
      explicit Layout mode abort the export with
      ``duplicate SVG id(s) are not allowed in explicit Layout mode``.

  P2. Every ``<g data-pptx-object="picture">`` must carry a
      ``data-pptx-shape-id`` and ``data-pptx-shape-scope``. Without it
      the converter allocates a fresh id at runtime, breaking the
      source-ref lookup and surfacing as
      ``Edited round-trip source object did not produce a DrawingML
      shape: <id>``.

  P3. Every ``<g data-pptx-semantic-object="shape">`` must contain at
      most one direct ``<text>`` child. Exceeding that limit triggers
      ``Semantic shape text must be one direct SVG text component``.

  P4. Every ``<g data-pptx-object="group">`` must contain at least two
      visual children. A single-child wrapper gets flattened by the
      converter, losing its identity, which surfaces as
      ``Edited round-trip source object did not produce a DrawingML
      shape: <id>``.

Usage::

    python -m mcp_ppt_native_fill.preflight_check path/to/authoring-svg-flat

The exit code is ``0`` when no rule fires and ``1`` when at least one
violation was found.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

_SVG_NS = "http://www.w3.org/2000/svg"
_NON_VISUAL_TAGS = {"defs", "metadata", "title", "desc", "style"}


@dataclass
class Finding:
    """Single rule violation tied to a slide + element."""

    rule: str
    severity: str  # "error" | "warning"
    svg_file: str
    element_id: str | None
    detail: str


@dataclass
class Report:
    """Aggregate result for one directory of SVG files."""

    svg_files_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["error_count"] = len(self.errors)
        d["warning_count"] = len(self.warnings)
        return d


def _localname(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _check_duplicate_ids(root: ET.Element, file_label: str) -> list[Finding]:
    """P1: every ``id`` unique within a slide."""
    counts: Counter[str] = Counter()
    for elem in root.iter():
        eid = elem.get("id")
        if eid:
            counts[eid] += 1
    return [
        Finding(
            rule="P1.duplicate-id",
            severity="error",
            svg_file=file_label,
            element_id=eid,
            detail=(
                f"<{_localname(eid)} id={eid!r}> appears {n} times in this "
                f"slide; ppt-master's explicit Layout rejects duplicate "
                f"ids."
            ),
        )
        for eid, n in counts.items()
        if n > 1
    ]


def _check_picture_shape_ids(root: ET.Element, file_label: str) -> list[Finding]:
    """P2: every picture has shape-id + shape-scope.

    Only top-level picture carriers (immediate children of the root
    ``<svg>``) are checked. Nested crop-wrapper ``<svg>`` elements
    inside a picture ``<g>`` are part of the carrier's drawing
    geometry and need not carry shape metadata.
    """
    findings: list[Finding] = []
    for top_child in list(root):
        if top_child.get("data-pptx-object") != "picture":
            continue
        eid = top_child.get("id") or "(no-id)"
        shape_id = top_child.get("data-pptx-shape-id")
        shape_scope = top_child.get("data-pptx-shape-scope")
        if not shape_id:
            findings.append(
                Finding(
                    rule="P2.picture-missing-shape-id",
                    severity="error",
                    svg_file=file_label,
                    element_id=eid,
                    detail=(
                        "<g data-pptx-object='picture'> lacks "
                        "data-pptx-shape-id; roundtrip will allocate a "
                        "fresh id and break source-ref lookup."
                    ),
                )
            )
        if not shape_scope:
            findings.append(
                Finding(
                    rule="P2.picture-missing-shape-scope",
                    severity="error",
                    svg_file=file_label,
                    element_id=eid,
                    detail=(
                        "<g data-pptx-object='picture'> lacks "
                        "data-pptx-shape-scope; required pair of "
                        "data-pptx-shape-id."
                    ),
                )
            )
    return findings


def _check_semantic_shape_text_count(
    root: ET.Element, file_label: str
) -> list[Finding]:
    """P3: semantic shapes hold at most one direct text child."""
    findings: list[Finding] = []
    for elem in root.iter():
        if elem.get("data-pptx-semantic-object") != "shape":
            continue
        eid = elem.get("id") or "(no-id)"
        direct_text = sum(
            1
            for child in elem
            if _localname(child.tag) == "text"
        )
        if direct_text > 1:
            findings.append(
                Finding(
                    rule="P3.semantic-shape-multi-text",
                    severity="error",
                    svg_file=file_label,
                    element_id=eid,
                    detail=(
                        f"<g data-pptx-semantic-object='shape'> has "
                        f"{direct_text} direct <text> children; ppt-master "
                        "rejects anything > 1 with 'Semantic shape text "
                        "must be one direct SVG text component'."
                    ),
                )
            )
    return findings


def _check_single_child_groups(root: ET.Element, file_label: str) -> list[Finding]:
    """P4: groups hold at least two visual children."""
    findings: list[Finding] = []
    for elem in root.iter():
        if elem.get("data-pptx-object") != "group":
            continue
        visual = [
            child
            for child in elem
            if _localname(child.tag) not in _NON_VISUAL_TAGS
        ]
        if len(visual) <= 1:
            eid = elem.get("id") or "(no-id)"
            findings.append(
                Finding(
                    rule="P4.single-child-group",
                    severity="warning",
                    svg_file=file_label,
                    element_id=eid,
                    detail=(
                        f"<g data-pptx-object='group'> has "
                        f"{len(visual)} visual child(ren); single-child "
                        "wrappers get flattened by the converter and "
                        "lose their identity. Prefer promoting the "
                        "child or adding a sibling."
                    ),
                )
            )
    return findings


def _scan_one(svg_path: Path) -> list[Finding]:
    label = svg_path.name
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError as exc:
        return [
            Finding(
                rule="parse-error",
                severity="error",
                svg_file=label,
                element_id=None,
                detail=f"could not parse SVG: {exc}",
            )
        ]
    root = tree.getroot()
    findings: list[Finding] = []
    findings.extend(_check_duplicate_ids(root, label))
    findings.extend(_check_picture_shape_ids(root, label))
    findings.extend(_check_semantic_shape_text_count(root, label))
    findings.extend(_check_single_child_groups(root, label))
    return findings


def scan_directory(svg_dir: Path) -> Report:
    """Run all four checks against every ``*.svg`` file in ``svg_dir``."""
    if not svg_dir.is_dir():
        raise FileNotFoundError(f"not a directory: {svg_dir}")
    report = Report()
    for svg_path in sorted(svg_dir.glob("*.svg")):
        report.svg_files_scanned += 1
        report.findings.extend(_scan_one(svg_path))
    return report


def render_human(report: Report) -> str:
    """Format a report for human consumption on stdout."""
    if not report.findings:
        return (
            f"[OK] scanned {report.svg_files_scanned} SVG file(s); "
            "no preflight violations.\n"
        )
    lines = [
        f"[FAIL] scanned {report.svg_files_scanned} SVG file(s); "
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)",
        "",
    ]
    by_rule: dict[str, list[Finding]] = {}
    for finding in report.findings:
        by_rule.setdefault(finding.rule, []).append(finding)
    for rule, items in by_rule.items():
        lines.append(f"== {rule} ({len(items)}) ==")
        for item in items:
            lines.append(
                f"  {item.svg_file}:{item.element_id or '?'}  "
                f"[{item.severity}]  {item.detail}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run preflight checks for SVG files destined for ppt-master "
            "Edit Native roundtrip."
        )
    )
    parser.add_argument(
        "svg_dir",
        type=Path,
        help="Directory containing *.svg authoring files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output",
    )
    args = parser.parse_args(argv)

    try:
        report = scan_directory(args.svg_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_human(report))

    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())