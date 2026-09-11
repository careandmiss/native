"""autofix.py — the six native fill auto-fixes from NATIVE_FILL_PIPELINE_GUIDE §6.

Each fix function is a pure transformation that returns an
:class:`AutoFixRecord` describing what it did. The pipeline layer decides
when to invoke them and aggregates ``fix_iterations`` for audit.

Public surface
--------------
* :func:`fix_text_overflow`        — shrink font-size / shorten text (auto, 3 rounds)
* :func:`fix_viewbox_missing`      — restore ``<svg viewBox="0 0 W H">``
* :func:`fix_gradient_unexportable` — strip ``<defs><linearGradient>``, fill -> solid
* :func:`fix_picture_structure`    — toggle nested-svg / direct-image
* :func:`fix_unsafe_font`          — replace non-PPT-safe font stacks
* :func:`run_autofix_round`        — orchestrate one auto-fix attempt

No LLM, no network, no external deps. ElementTree + regex only.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import io_utils
from .svg_edits import SVG_NS, _local, _qname

log = logging.getLogger("mcp_ppt_native_fill.autofix")

# Font stacks that PowerPoint cannot render — they live on the host only.
# ppt-master's quality checker emits WARN for these; replacing them avoids
# downstream visual drift in PowerPoint.
_PPT_UNSAFE_FONT_HINTS = (
    "思源黑体 CN",
    "Source Han Sans",
    "Source Han Serif",
    "Noto Sans CJK",
    "Noto Serif CJK",
    "PingFang SC",
    "Hiragino Sans",
)
_PPT_SAFE_REPLACEMENT = '"微软雅黑", sans-serif'


# ---------------------------------------------------------------------------
# AutoFixRecord + helpers.
# ---------------------------------------------------------------------------

@dataclass
class AutoFixRecord:
    slide: str  # slide_NN.svg filename
    issue: str  # short error tag
    action: str  # what we did
    before: Any = None
    after: Any = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide": self.slide,
            "issue": self.issue,
            "action": self.action,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
        }


def _parse_svg(svg_path: Path) -> tuple[ET.ElementTree, ET.Element]:
    raw = io_utils.read_utf8(svg_path)
    # Order matters: escape unescaped inner quotes FIRST so the dedup
    # regex below can read full attribute values. The dedup regex
    # (`[\w:.-]+="[^"]*"`) cannot span bare `"` characters, so on raw
    # vendor SVGs (e.g. `font-family=""微软雅黑", sans-serif"`) it would
    # truncate the value at the first inner `"` and silently drop the
    # rest of the attribute region. After `_escape_inner_attr_quotes` the
    # only remaining `"` chars are valid value delimiters, so dedup is
    # safe.
    repaired = _escape_inner_attr_quotes(raw)
    repaired = _dedupe_duplicate_attrs(repaired)
    if repaired != raw:
        # Persist the repaired version so downstream vendor tools
        # (svg_quality_checker, svg_to_pptx) don't choke on the same
        # vendor bugs we just worked around in Python. Any autofix
        # function that mutates the tree and writes it back via
        # ``_write_svg`` will overwrite this intermediate write, so this
        # only persists when no autofix mutates the file.
        io_utils.write_utf8_atomic(svg_path, repaired)
    root = ET.fromstring(repaired)
    return ET.ElementTree(root), root


# Match a single attribute name="value" inside a tag's attribute region.
# Vendored SVGs never contain '>' or '"' inside attribute values, so a
# straightforward regex is safe.
_DUPE_ATTR_NAME_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')
# Match an element *open* tag only (not DOCTYPE, CDATA, processing
# instructions, or close tags). Element names start with an ASCII letter.
# This excludes:
#   <!DOCTYPE …>     (DTD)
#   <!-- … -->       (comment)
#   <![CDATA[ … ]]>  (CDATA section)
#   <?xml … ?>       (processing instruction)
#   </text>          (close tag)
_DUPE_TAG_RE = re.compile(
    r'<([a-zA-Z][\w:.-]*)(\s+[^>]*?)?(\s*/)?>',
    re.DOTALL,
)


def _dedupe_duplicate_attrs(svg_text: str) -> str:
    """For each element open tag, keep only the first occurrence of each
    attribute name. ``xml.etree.ElementTree`` is strict about duplicate
    attributes and would raise ``ParseError: duplicate attribute`` on
    perfectly valid SVG (e.g. ``<text xml:space="preserve" xml:space="preserve">``
    emitted by ppt-master's ``pptx_to_svg.py``). This repair makes the parser
    forgiving without changing element structure.

    The first occurrence wins because PowerPoint tolerates any value, and the
    vendor's first emission is always the most meaningful one.
    """
    def fix(m: re.Match) -> str:
        name = m.group(1)
        attrs = m.group(2) or ""
        slash = m.group(3) or ""
        if not attrs.strip():
            return m.group(0)
        seen: dict[str, str] = {}
        for am in _DUPE_ATTR_NAME_RE.finditer(attrs):
            seen.setdefault(am.group(1), am.group(0))
        rebuilt_attrs = " " + " ".join(seen.values())
        return f"<{name}{rebuilt_attrs}{slash}>"

    return _DUPE_TAG_RE.sub(fix, svg_text)


def _escape_inner_attr_quotes(svg_text: str) -> str:
    """Repair vendor SVGs where ``pptx_to_svg.py`` emitted literal ``"``
    characters inside attribute values instead of ``&quot;``.

    The most common occurrence is ``font-family=""微软雅黑", sans-serif"``:
    the vendor's source PPTX has the CSS convention ``"微软雅黑"`` (literal
    quotes around the Chinese family name) but the exporter writes those
    quotes raw, producing an attribute value with an empty leading ``""``
    followed by quoted text — which ``xml.etree.ElementTree`` rejects as
    "not well-formed (invalid token)".

    Strategy: track tag / attribute-value state via a tiny state machine.
    When a value-opener ``"`` is followed by another ``"`` before any other
    char (the ``""`` empty-value signature), the second ``"`` is the
    vendor-bug inner quote — escape it as ``&quot;``. Subsequent bare ``"``
    chars inside the value are escaped the same way unless they are
    followed by whitespace and then an attribute-name character /
    tag-close / self-close / EOF (i.e. the value actually ends there).
    """
    out: list[str] = []
    i = 0
    n = len(svg_text)
    in_tag = False
    in_attr_value = False
    just_opened = False  # True only for the very next char after value-open
    while i < n:
        ch = svg_text[i]
        if not in_tag:
            # CDATA / comment / PI are passed through verbatim — XML treats
            # their contents as opaque text, never as attribute values.
            if ch == '<' and svg_text.startswith('<![CDATA[', i):
                j = svg_text.find(']]>', i + 9)
                end = n if j < 0 else j + 3
                out.append(svg_text[i:end])
                i = end
                continue
            if ch == '<' and svg_text.startswith('<!--', i):
                j = svg_text.find('-->', i + 4)
                end = n if j < 0 else j + 3
                out.append(svg_text[i:end])
                i = end
                continue
            if ch == '<' and i + 1 < n and svg_text[i + 1] == '?':
                j = svg_text.find('?>', i + 2)
                end = n if j < 0 else j + 2
                out.append(svg_text[i:end])
                i = end
                continue
            if ch == '<':
                in_tag = True
                in_attr_value = False
                just_opened = False
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        # in_tag == True
        if in_attr_value:
            if just_opened:
                # The very next char after a value-opener. If it's another
                # '"' that's the vendor bug ``""value`` (empty value opener
                # + start of inner quoted content) — escape it as &quot;.
                # Otherwise it's normal content; fall through.
                if ch == '"':
                    out.append('&quot;')
                    just_opened = False
                    i += 1
                    continue
                just_opened = False
                # fall through to the normal in_attr_value branch
            if ch == '\\' and i + 1 < n and svg_text[i + 1] == '"':
                # Pass through \" escape verbatim
                out.append(svg_text[i:i + 2])
                i += 2
                continue
            if ch == '"':
                # Decide: actual close-of-value vs stray inner quote.
                # Look ahead past whitespace. If the next non-whitespace
                # char is an attribute-name start (alpha, _, :) or a
                # tag-close / self-close / EOF, the '"' closes the value.
                # Otherwise it's a stray inner quote — escape it.
                k = i + 1
                while k < n and svg_text[k] in ' \t\r\n':
                    k += 1
                next_ch = svg_text[k] if k < n else ''
                if next_ch in ('', '>', '/', '=') or next_ch.isalpha() or next_ch in '_:':
                    in_attr_value = False
                    out.append(ch)
                else:
                    out.append('&quot;')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        # in_tag but not in_attr_value
        if ch == '>':
            in_tag = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_attr_value = True
            just_opened = True
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _write_svg(svg_path: Path, root: ET.Element) -> None:
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp = svg_path.with_suffix(svg_path.suffix + ".tmp")
    tmp.write_bytes(xml_bytes)
    tmp.replace(svg_path)


# ---------------------------------------------------------------------------
# Fix 1: text overflow frame.
# ---------------------------------------------------------------------------

_OVERFLOW_HINT_RE = re.compile(
    r"(?P<shape>shape-\d+).*?overflow horizontal (?P<pct>\d+(?:\.\d+)?)%",
)


def detect_overflow_shapes(quality_stdout: str) -> dict[str, list[float]]:
    """Parse svg_quality_checker --roundtrip output for overflow hints."""
    out: dict[str, list[float]] = {}
    for line in quality_stdout.splitlines():
        m = _OVERFLOW_HINT_RE.search(line)
        if not m:
            continue
        shape = m.group("shape")
        pct = float(m.group("pct"))
        out.setdefault(shape, []).append(pct)
    return out


def fix_text_overflow(
    svg_path: Path,
    shape_ids: Iterable[str],
    *,
    shrink_factor: float = 0.85,
) -> list[AutoFixRecord]:
    """Shrink ``font-size`` on the listed shapes by ``shrink_factor``.

    Returns one record per shape actually shrunk. Skips shapes whose
    font-size cannot be parsed as a unitless positive number (per master §5.5).
    """
    records: list[AutoFixRecord] = []
    if not shape_ids:
        return records

    tree, root = _parse_svg(svg_path)
    targets = set(shape_ids)
    for g in root.iter(_qname("g")):
        gid = g.get("id")
        if not gid or gid not in targets:
            continue
        for text in g.iter(_qname("text")):
            fs_raw = text.get("font-size")
            if not fs_raw:
                continue
            try:
                old_fs = float(fs_raw.rstrip("px"))
            except ValueError:
                continue
            new_fs = round(old_fs * shrink_factor, 2)
            text.set("font-size", f"{new_fs:g}")
            records.append(
                AutoFixRecord(
                    slide=svg_path.name,
                    issue="text_overflow",
                    action="shrink_font_size",
                    before=old_fs,
                    after=new_fs,
                    detail=f"shape={gid} factor={shrink_factor}",
                )
            )
            break  # one fix per shape group is enough

    if records:
        _write_svg(svg_path, root)
    return records


# ---------------------------------------------------------------------------
# Fix 2: missing viewBox.
# ---------------------------------------------------------------------------

_VIEWBOX_RE = re.compile(r"^0\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?$")


def fix_viewbox_missing(
    svg_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
) -> AutoFixRecord | None:
    """Restore ``<svg viewBox="0 0 W H">`` when missing or malformed.

    When sibling SVGs in the same authoring directory expose a viewBox we
    could prefer that, but the per-slide call signature stays simple: callers
    pass width/height explicitly to avoid hidden cross-file coupling.
    """
    tree, root = _parse_svg(svg_path)
    if root.tag != _qname("svg"):
        return None

    existing = root.get("viewBox")
    if existing and _VIEWBOX_RE.match(existing):
        return None

    new_vb = f"0 0 {int(width)} {int(height)}"
    root.set("viewBox", new_vb)
    if not root.get("width"):
        root.set("width", str(int(width)))
    if not root.get("height"):
        root.set("height", str(int(height)))
    _write_svg(svg_path, root)
    return AutoFixRecord(
        slide=svg_path.name,
        issue="viewbox_missing",
        action="restore_viewbox",
        before=existing,
        after=new_vb,
    )


# ---------------------------------------------------------------------------
# Fix 3: gradient unexportable.
# ---------------------------------------------------------------------------

_URL_REF_RE = re.compile(r"url\(#([^)]+)\)")


def fix_gradient_unexportable(svg_path: Path) -> list[AutoFixRecord]:
    """Strip ``<defs><linearGradient>...</linearGradient></defs>`` blocks
    and rewrite ``fill="url(#x)"`` / ``stroke="url(#x)"`` to solid white.

    Per master §6.1, DrawingML's gradient stop requirements (monotonic,
    positions in [0,1]) reject most authoring SVGs. White is a safe default
    because slide backgrounds are usually light; consumers can re-tint via
    slide redesign.

    Returns one record per gradient defs block removed.
    """
    tree, root = _parse_svg(svg_path)
    svg_ns = SVG_NS
    records: list[AutoFixRecord] = []

    defs = root.find(_qname("defs"))
    if defs is None:
        return records

    gradient_ids: set[str] = set()
    for tag in ("linearGradient", "radialGradient"):
        for grad in list(defs.findall(_qname(tag))):
            gid = grad.get("id")
            if gid:
                gradient_ids.add(gid)
            defs.remove(grad)

    if defs.find(_qname("linearGradient")) is None and defs.find(_qname("radialGradient")) is None:
        # defs is empty — drop it entirely
        root.remove(defs)

    if not gradient_ids:
        return records

    # Rewrite fill / stroke references to a solid color. Use white, which is
    # also what master §6.1 suggests.
    rewritten = 0
    for elem in root.iter():
        for attr in ("fill", "stroke"):
            val = elem.get(attr)
            if not val:
                continue
            m = _URL_REF_RE.search(val)
            if m and m.group(1) in gradient_ids:
                elem.set(attr, "#FFFFFF")
                rewritten += 1

    if rewritten or gradient_ids:
        _write_svg(svg_path, root)
        records.append(
            AutoFixRecord(
                slide=svg_path.name,
                issue="gradient_unexportable",
                action="strip_linearGradient",
                before=sorted(gradient_ids),
                after="#FFFFFF (solid)",
                detail=f"rewritten {rewritten} url() references",
            )
        )
    return records


# ---------------------------------------------------------------------------
# Fix 4: picture structure toggle.
# ---------------------------------------------------------------------------

def fix_picture_structure(svg_path: Path) -> list[AutoFixRecord]:
    """Toggle nested-svg picture wrappers between two equivalent forms.

    Per master §6.2 the two structures (``<g><svg viewBox=...><image/></svg></g>``
    vs ``<g><image/></g>``) behave inconsistently across slides, and there
    is no rule to predict which one vendor accepts. The native_fill MCP
    therefore flips whichever form is currently present to the other, hoping
    one of the two passes.
    """
    raw = io_utils.read_utf8(svg_path)
    # Detect "nested svg picture": a <g> containing <svg><image/></svg>
    nested = re.search(
        r"<g[^>]*data-pptx-object=\"picture\"[^>]*>\s*"
        r"<svg[^>]*viewBox=\"[^\"]+\"[^>]*>\s*"
        r"<image[^>]*/>\s*"
        r"</svg>\s*"
        r"</g>",
        raw,
        re.DOTALL,
    )
    flat = re.search(
        r"<g[^>]*data-pptx-object=\"picture\"[^>]*>\s*<image[^>]*/>\s*</g>",
        raw,
        re.DOTALL,
    )

    if nested and not flat:
        # Strip the inner <svg viewBox="...">...</svg> wrapper.
        new_raw, n = re.subn(
            r"(<g[^>]*data-pptx-object=\"picture\"[^>]*>)\s*"
            r"<svg[^>]*viewBox=\"[^\"]+\"[^>]*>\s*"
            r"(<image[^>]*/>)\s*"
            r"</svg>\s*"
            r"(</g>)",
            r"\1\2\3",
            raw,
            count=1,
        )
        if n:
            io_utils.write_utf8_atomic(svg_path, new_raw)
            return [
                AutoFixRecord(
                    slide=svg_path.name,
                    issue="picture_structure",
                    action="nested_to_flat",
                    before="<g><svg viewBox=...><image/></svg></g>",
                    after="<g><image/></g>",
                )
            ]
    elif flat and not nested:
        # Wrap the <image/> in a synthetic <svg viewBox="0 0 1 1"> matching
        # ppt-master's authoring convention. Only attempt if we can find the
        # matching </g>.
        m = re.search(
            r"(<g[^>]*data-pptx-object=\"picture\"[^>]*>)\s*"
            r"(<image[^/]*/>)\s*"
            r"(</g>)",
            raw,
            re.DOTALL,
        )
        if m:
            new_block = (
                f"{m.group(1)}\n"
                f'<svg viewBox="0 0 1 1" preserveAspectRatio="none">'
                f"{m.group(2)}</svg>\n"
                f"{m.group(3)}"
            )
            new_raw = raw[: m.start()] + new_block + raw[m.end():]
            io_utils.write_utf8_atomic(svg_path, new_raw)
            return [
                AutoFixRecord(
                    slide=svg_path.name,
                    issue="picture_structure",
                    action="flat_to_nested",
                    before="<g><image/></g>",
                    after='<g><svg viewBox="0 0 1 1"><image/></svg></g>',
                )
            ]

    return []


# ---------------------------------------------------------------------------
# Fix 5: unsafe font stack.
# ---------------------------------------------------------------------------

def fix_unsafe_font(svg_path: Path) -> list[AutoFixRecord]:
    """Rewrite ``font-family="思源黑体 CN ..."`` → ``font-family="微软雅黑", sans-serif``.

    Per master §5/§6.5 the PowerPoint-on-Windows font catalog does not have
    the Source Han family, so PPT silently substitutes. Use the safer stack
    up front so what the author wrote is what PowerPoint renders.
    """
    raw = io_utils.read_utf8(svg_path)
    new_raw = raw
    rewritten = 0
    for hint in _PPT_UNSAFE_FONT_HINTS:
        # Match any font-family value containing the hint (quoted or not).
        pattern = re.compile(
            r'font-family\s*=\s*"([^"]*?' + re.escape(hint) + r'[^"]*)"'
        )
        new_raw, n = pattern.subn(
            f'font-family="{_PPT_SAFE_REPLACEMENT}"',
            new_raw,
        )
        rewritten += n
        pattern = re.compile(
            r"font-family\s*=\s*'([^']*?" + re.escape(hint) + r"[^']*)'"
        )
        new_raw, n = pattern.subn(
            f"font-family=\"{_PPT_SAFE_REPLACEMENT}\"",
            new_raw,
        )
        rewritten += n

    if rewritten == 0:
        return []

    io_utils.write_utf8_atomic(svg_path, new_raw)
    return [
        AutoFixRecord(
            slide=svg_path.name,
            issue="unsafe_font",
            action="replace_font_family",
            before="non-PPT-safe font stack",
            after=_PPT_SAFE_REPLACEMENT,
            detail=f"replaced {rewritten} font-family attributes",
        )
    ]


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------

def run_autofix_round(
    slide_files: list[Path],
    quality_stdout: str,
    *,
    fix_overflow: bool = True,
    fix_viewbox: bool = True,
    fix_gradient: bool = True,
    fix_picture: bool = True,
    fix_font: bool = True,
) -> list[AutoFixRecord]:
    """Run one auto-fix pass over the slide set.

    Detection is purely local (no LLM). The caller is responsible for capping
    total rounds (master §7.9 suggests 3).
    """
    overflow_by_slide = _detect_overflow_by_slide(quality_stdout, slide_files)
    records: list[AutoFixRecord] = []

    for svg_path in slide_files:
        if fix_gradient:
            records.extend(fix_gradient_unexportable(svg_path))
        if fix_picture:
            records.extend(fix_picture_structure(svg_path))
        if fix_font:
            records.extend(fix_unsafe_font(svg_path))
        if fix_viewbox:
            rec = fix_viewbox_missing(svg_path)
            if rec is not None:
                records.append(rec)
        if fix_overflow:
            shape_ids = overflow_by_slide.get(svg_path.name, [])
            records.extend(fix_text_overflow(svg_path, shape_ids))

    return records


def _detect_overflow_by_slide(
    quality_stdout: str,
    slide_files: Iterable[Path],
) -> dict[str, list[str]]:
    """Map slide filename → list of overflowing shape ids.

    The quality checker emits overflow lines like::

        [ERROR] slide_02.svg - Failed
           [ERROR] <text> (x=..., text='...') exceeds owning frame <g id="shape-N"> ...
              overflow horizontal 45.8%; ...

    The slide filename is on the line *above* the overflow line, not on
    the overflow line itself. We scan with a 1-line lookbehind.
    """
    by_slide: dict[str, list[str]] = {}
    prev_slide: str | None = None
    slide_files_list = list(slide_files)
    only_slide = (
        slide_files_list[0].name if len(slide_files_list) == 1 else None
    )
    for line in quality_stdout.splitlines():
        # Header line: "[ERROR] slide_NN.svg - Failed" or
        # "[WARN]  slide_NN.svg - Passed (with warnings)"
        m_header = re.search(r"\b(slide[^\s]*\.svg)\b", line)
        if m_header and ("Failed" in line or "Passed" in line or "ERROR" in line or "WARN" in line):
            prev_slide = m_header.group(1)
        if "overflow horizontal" not in line:
            continue
        m_overflow = _OVERFLOW_HINT_RE.search(line)
        if not m_overflow:
            continue
        shape = m_overflow.group("shape")
        slide_name = prev_slide
        if not slide_name:
            if only_slide:
                slide_name = only_slide
            else:
                log.warning(
                    "overflow on line without preceding slide header: %s",
                    line[:80],
                )
                continue
        by_slide.setdefault(slide_name, []).append(shape)
    return by_slide


# ---------------------------------------------------------------------------
# Bulk workspace XML repair.
# ---------------------------------------------------------------------------

def repair_workspace_svgs(authoring_dir: Path) -> int:
    """Walk ``authoring_dir`` and persist the in-memory XML repairs
    (escape inner attribute quotes, dedupe duplicate attributes) for every
    SVG that needs them. Returns the number of files actually repaired.

    Call this once, right after ``pptx_to_svg.py`` produces the round-trip
    workspace and *before* any vendor tool (``svg_quality_checker``,
    ``svg_to_pptx``) tries to parse the SVGs from disk. The vendor's parser
    is just as strict as ``xml.etree.ElementTree`` and rejects the same
    well-formed-but-malformed patterns (``font-family=""X"`` etc.).
    """
    repaired = 0
    for svg_path in sorted(authoring_dir.glob("*.svg")):
        try:
            raw = io_utils.read_utf8(svg_path)
        except OSError:
            continue
        after_escape = _escape_inner_attr_quotes(raw)
        after_dedupe = _dedupe_duplicate_attrs(after_escape)
        if after_dedupe != raw:
            io_utils.write_utf8_atomic(svg_path, after_dedupe)
            repaired += 1
    return repaired


# ---------------------------------------------------------------------------
# data-pptx-* attribute snapshot / restore.
# ---------------------------------------------------------------------------

SHAPE_ATTRS_SNAPSHOT = "shape_attrs_snapshot.json"

# data-pptx-* attrs that are *safe to reapply* verbatim during restore.
# data-pptx-source-ref is INTENTIONALLY excluded — it's only meaningful
# if it points at a real source slide, and the autofix loop may have
# stripped it for that very reason.
_PPTX_SNAPSHOT_PREFIXES = (
    "data-pptx-object",
    "data-pptx-frame",
    "data-pptx-prst",
    "data-pptx-semantic-object",
    "data-pptx-bounds",
    "data-pptx-pattern",
    "data-pptx-fg",
    "data-pptx-bg",
    "data-pptx-replace-with",
    "data-pptx-x",
    "data-pptx-y",
    "data-pptx-width",
    "data-pptx-height",
    "data-pptx-authority",
    "data-pptx-text-model",
)


def snapshot_shape_attrs(authoring_dir: Path) -> int:
    """Walk every ``<g id="shape-NN">`` and persist the ``data-pptx-*``
    attribute set to ``shape_attrs_snapshot.json`` at the workspace root.

    Called once after ``pptx_to_svg --roundtrip`` (i.e. phase2 is done
    and the SVGs are clean). The snapshot is later used by
    :func:`restore_shape_attrs` to recover shapes whose attributes were
    accidentally dropped during text edits — without this, svg_to_pptx
    raises ``Edited round-trip source object did not produce a
    DrawingML shape``.

    Returns the number of shapes captured.
    """
    import json as _json

    index: dict[str, dict[str, dict[str, str]]] = {}
    for svg_path in sorted(authoring_dir.glob("*.svg")):
        try:
            _, root = _parse_svg(svg_path)
        except Exception as exc:
            log.warning("snapshot: skip %s (%s)", svg_path.name, exc)
            continue
        svg_ns = "{http://www.w3.org/2000/svg}"
        per_slide: dict[str, dict[str, str]] = {}
        for g in root.iter(svg_ns + "g"):
            gid = g.get("id")
            if not gid or not gid.startswith("shape-"):
                continue
            captured = {
                k: v
                for k, v in g.attrib.items()
                if any(k.startswith(p) for p in _PPTX_SNAPSHOT_PREFIXES)
            }
            if captured:
                per_slide[gid] = captured
        if per_slide:
            index[svg_path.name] = per_slide
    snapshot_path = authoring_dir.parent / SHAPE_ATTRS_SNAPSHOT
    io_utils.write_utf8_atomic(
        snapshot_path, _json.dumps(index, ensure_ascii=False, indent=2)
    )
    return sum(len(v) for v in index.values())


def fix_invalid_source_ref(
    svg_path: Path,
    valid_source_slides: set[int],
    *,
    strip_all: bool = False,
) -> list[AutoFixRecord]:
    """Strip ``data-pptx-source-ref="slide:N"`` from shapes whose N is not
    a real slide in the source PPTX — or, with ``strip_all=True``, from
    every shape on the SVG regardless of N.

    Per master §3, ``data-pptx-source-ref`` is the byte-for-byte
    rehydration key: svg_to_pptx looks up slide N in the source
    ``ppt/slides/slideN.xml`` and, if the shape's geometry / paint
    matches, restores the original DrawingML bytes. If the source PPTX
    has fewer slides than N — common in templated decks where the
    vendor's index overflows — svg_to_pptx aborts with
    ``Edited round-trip source object did not produce a DrawingML
    shape: N``.

    A passthrough page never hits this code path (it restores the whole
    slide byte-for-byte), but a rebuilt page does. The fix: drop the
    bad source-ref on the shape so svg_to_pptx treats it as a fresh
    authored DrawingML element instead of trying to rehydrate.

    ``strip_all=True`` is used when ANY shape on the slide has been
    edited: even if every source-ref points at a real source slide,
    once a slide is rebuilt svg_to_pptx re-checks every shape's
    rehydration against the new SVG geometry, and one mismatch aborts
    the whole slide export. Stripping all refs forces svg_to_pptx to
    render the entire slide from the SVG instead.

    Returns one record per shape whose ref was stripped.
    """
    raw = io_utils.read_utf8(svg_path)
    new_raw = raw
    records: list[AutoFixRecord] = []
    # Match a <g ... data-pptx-source-ref="slide:N" ...> opening tag, where
    # N is the integer we want to validate against valid_source_slides.
    pattern = re.compile(
        r'(<g\b[^>]*?\bdata-pptx-source-ref="slide:)(\d+)("[^>]*>)',
        re.DOTALL,
    )

    def _fix(m: re.Match) -> str:
        prefix, n_str, suffix = m.group(1), m.group(2), m.group(3)
        try:
            n = int(n_str)
        except ValueError:
            return m.group(0)
        if not strip_all and n in valid_source_slides:
            return m.group(0)
        if strip_all:
            detail = "strip_all: slide was edited; force fresh render"
        else:
            detail = f"source PPTX has no slide{n}"
        records.append(
            AutoFixRecord(
                slide=svg_path.name,
                issue="invalid_source_ref",
                action="strip_data_pptx_source_ref",
                before=f"slide:{n}",
                after="(removed; vendor will treat as fresh authored shape)",
                detail=detail,
            )
        )
        # Drop the attribute by replacing the whole `data-pptx-source-ref="slide:N"`
        # segment with empty string in BOTH the prefix and suffix copies.
        return re.sub(
            r'\s*data-pptx-source-ref="slide:\d+"',
            "",
            prefix + n_str + suffix,
            count=1,
        )

    new_raw = pattern.sub(_fix, raw)
    if records:
        io_utils.write_utf8_atomic(svg_path, new_raw)
    return records


def restore_shape_attrs(
    authoring_dir: Path,
    shape_filter: set[tuple[str, str]] | None = None,
) -> int:
    """For each shape in the workspace, re-apply the snapshot's
    ``data-pptx-*`` attributes if they're missing or have been changed.

    ``shape_filter`` is an optional set of ``(svg_filename, shape_id)``
    tuples to limit the restoration; pass ``None`` (the default) to
    process every shape.

    Returns the number of shape groups whose attributes were repaired.
    """
    import json as _json

    snapshot_path = authoring_dir.parent / SHAPE_ATTRS_SNAPSHOT
    if not snapshot_path.is_file():
        log.info("restore_shape_attrs: no snapshot at %s; skipping", snapshot_path)
        return 0
    try:
        snapshot = _json.loads(io_utils.read_utf8(snapshot_path))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("restore_shape_attrs: bad snapshot (%s); skipping", exc)
        return 0

    fixed = 0
    for svg_path in sorted(authoring_dir.glob("*.svg")):
        per_slide = snapshot.get(svg_path.name)
        if not per_slide:
            continue
        try:
            tree, root = _parse_svg(svg_path)
        except Exception as exc:
            log.warning("restore: parse fail %s (%s); skipping", svg_path.name, exc)
            continue
        svg_ns = "{http://www.w3.org/2000/svg}"
        changed = False
        for g in root.iter(svg_ns + "g"):
            gid = g.get("id")
            if not gid or gid not in per_slide:
                continue
            if shape_filter is not None and (svg_path.name, gid) not in shape_filter:
                continue
            expected = per_slide[gid]
            for k, v in expected.items():
                if g.get(k) != v:
                    g.set(k, v)
                    changed = True
        if changed:
            _write_svg(svg_path, root)
            fixed += 1
    if fixed:
        log.info("restore_shape_attrs: repaired %d svg file(s)", fixed)
    return fixed
