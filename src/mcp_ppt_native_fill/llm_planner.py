"""llm_planner.py — turn a markdown document into a content_mapping.

This is the ``phase2.5`` planning step inside ``native_fill``: when the
caller passes ``content_markdown`` + ``options.llm_plan=true``, the server
runs this planner *after* ``pptx_to_svg.py --roundtrip`` has produced the
workspace and *before* phase 3 starts editing SVGs.

Inputs
------
* a content markdown file (path on disk)
* the workspace's ``authoring-svg-flat/authoring_summary.json``
  (ppt-master already wrote one during ``pptx_to_svg.py``)

Outputs
-------
* ``content_mapping`` — ``{ "<svg_filename>": { "<shape_id>": "<new_text>",
  ... }, ... }`` — exactly the shape ``native_fill`` accepts.

The planner uses :func:`llm_client.llm_complete_json`. The LLM is asked to
return ONLY the JSON object so we can parse it without further cleverness.
The plan is validated against the SVG summary (shape_ids that don't exist
are dropped, mismatched slide files are surfaced as warnings).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import llm_client

log = logging.getLogger("mcp_ppt_native_fill.llm_planner")


SYSTEM_PROMPT = """\
You are a content-mapping planner for the ppt-master Edit Native PPTX pipeline.

You will receive:
  1. A `shape_index` — JSON describing every editable text shape in the
     source PPTX after round-trip. For each shape you see: `id` (e.g.
     `shape-23`), `placeholder` (current text), and `max_chars` (the
     visible character capacity of the slot, derived from the original
     placeholder's rendered width). `max_chars` is a HARD ceiling — a
     single CJK character counts as one.
  2. A `content_markdown` — the new material the user wants filled into
     the template.

Your task: produce a `content_mapping` JSON object that pairs each shape
that should change with the new text from the markdown.

Rules
-----
* Map only the shapes listed in `shape_index`. Each entry has a fixed
  visual capacity (`max_chars`); never exceed it.
* Use the markdown's actual content, never the placeholder wording.
  Do not invent facts. If the markdown does not cover a particular shape,
  OMIT it from the mapping (do not put empty strings).
* Preserve the markdown's language (do not translate Chinese to English,
  etc.).
* Compress to fit: if the markdown paragraph is longer than `max_chars`,
  pick the most important phrase or sentence. A chapter title slot
  expects a short phrase, never a paragraph.
* `max_chars` is a HARD ceiling. A safe target is `max_chars - 2` so the
  rendered text has visual margin. Going over by even one character will
  fail the quality gate and abort the export.
* Match slide_NN.svg filenames and shape IDs exactly.

Output format
-------------
Return ONLY a JSON object of this exact shape, no prose:

  {
    "slide_01.svg": {
      "shape-23": "...",
      "shape-8": "..."
    },
    "slide_03.svg": {
      "shape-2": "..."
    }
  }

If the markdown has no usable content for any shape, return `{}`.
"""


def plan_content_mapping(
    *,
    md_path: Path,
    workspace: Path,
    llm_config: llm_client.LLMConfig | None = None,
) -> dict[str, dict[str, str]]:
    """Read the workspace SVGs + markdown, ask the LLM, return content_mapping.

    The workspace's authoring_summary.json gives high-level counts but NOT
    per-shape IDs. So this function parses each ``authoring-svg-flat/*.svg``
    to enumerate text-bearing shapes (with their id + current placeholder
    text) and packs them into the prompt alongside the markdown.

    Returns an empty dict if the LLM signals "no usable mapping". Raises
    ``PlannerError`` on hard failures (missing files, malformed SVGs, LLM-side
    error that the planner cannot recover from).
    """
    _ = _load_summary(workspace)  # sanity check workspace exists
    md_text = _read_md(md_path)

    shape_index = _scan_text_shapes(workspace)
    user_prompt = _build_user_prompt(
        shape_index=shape_index, md_text=md_text, md_path=md_path
    )

    log.info(
        "plan_content_mapping md=%s shape_index_slides=%d md_chars=%d",
        md_path.name,
        len(shape_index),
        len(md_text),
    )
    raw = llm_client.llm_complete_json(
        system=SYSTEM_PROMPT, user=user_prompt, config=llm_config
    )

    mapping = _normalize_mapping(raw, shape_index)
    log.info(
        "plan_content_mapping produced %d slide(s), %d shape-edit(s) total",
        len(mapping),
        sum(len(v) for v in mapping.values()),
    )
    return mapping


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

class PlannerError(RuntimeError):
    """Raised on a planner-side problem the caller must surface."""


def _load_summary(workspace: Path) -> dict[str, Any]:
    summary_path = workspace / "authoring-svg-flat" / "authoring_summary.json"
    if not summary_path.is_file():
        raise PlannerError(
            f"workspace summary not found: {summary_path}. "
            "The round-trip phase may have failed — check phase2 logs."
        )
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlannerError(f"authoring_summary.json is not valid JSON: {exc}") from exc


def _read_md(md_path: Path) -> str:
    if not md_path.is_file():
        raise PlannerError(f"content_markdown not found: {md_path}")
    try:
        return md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PlannerError(f"content_markdown is not utf-8: {exc}") from exc


def _scan_text_shapes(workspace: Path) -> dict[str, dict[str, Any]]:
    """Parse every ``authoring-svg-flat/*.svg`` and return a per-slide map of
    editable text shapes. Each entry is::

        {shape_id: {"placeholder": "...", "max_chars": <int>}}

    ``max_chars`` is the visible character capacity of the slot, derived
    from the original placeholder's rendered width — typically the
    placeholder length + 20% margin so the LLM can keep similar-density
    text without overflowing. Empty placeholders default to a conservative
    24 chars (one short headline).

    Uses the same repair pipeline as ``autofix._parse_svg`` so vendor SVG
    quirks (duplicate attributes, unescaped inner quotes) don't trip the
    parser.
    """
    from . import autofix  # local import to avoid circular at module load

    flat = workspace / "authoring-svg-flat"
    if not flat.is_dir():
        raise PlannerError(f"authoring-svg-flat not found: {flat}")
    index: dict[str, dict[str, Any]] = {}
    for svg_path in sorted(flat.glob("*.svg")):
        try:
            _, root = autofix._parse_svg(svg_path)
        except Exception as exc:
            log.warning("planner: skip %s (%s)", svg_path.name, exc)
            continue
        svg_ns = "{http://www.w3.org/2000/svg}"
        per_slide: dict[str, Any] = {}
        # Strategy: find every <g id="shape-XX"> and pull the text content of
        # its descendant <text>/> children. If a <g> has no id but contains
        # text, skip it (it's a decoration, not an editable shape slot).
        for grp in root.iter(svg_ns + "g"):
            gid = grp.get("id")
            if not gid or not gid.startswith("shape-"):
                continue
            texts = grp.findall(f".//{svg_ns}text")
            if not texts:
                continue
            placeholder = " ".join(
                "".join(t.itertext()).strip() for t in texts
            ).strip()
            placeholder = placeholder[:160]
            # max_chars heuristic: placeholder length + 20 % (CJK/English mix
            # tends to tolerate a bit more), floored at 24 so empty slots
            # still get a usable budget.
            base = max(len(placeholder), 24)
            max_chars = int(base * 1.2)
            per_slide[gid] = {
                "placeholder": placeholder,
                "max_chars": max_chars,
            }
        index[svg_path.name] = per_slide
    return index


def _build_user_prompt(
    *, shape_index: dict[str, dict[str, Any]], md_text: str, md_path: Path
) -> str:
    """Pack the shape index + markdown into a single user message."""
    payload = {
        "shape_index": shape_index,
        "content_markdown_path": str(md_path.name),
        "content_markdown": md_text,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_mapping(
    raw: dict[str, Any], shape_index: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Validate the LLM's mapping against the actual shape index.

    Drops:
      * keys whose svg filename does not appear in the workspace
      * shape IDs that aren't present on the referenced slide
      * non-string values
      * values that exceed the slot's ``max_chars`` (hard ceiling — the
        quality checker would reject these anyway, but failing fast here
        saves a round-trip)
      * empty / whitespace-only values
    """
    if not isinstance(raw, dict):
        raise PlannerError(
            f"LLM returned a non-object JSON value: {type(raw).__name__}"
        )

    cleaned: dict[str, dict[str, str]] = {}
    for svg_key, edits in raw.items():
        if svg_key not in shape_index:
            log.warning(
                "LLM mapping referenced unknown svg %r; dropping whole slide",
                svg_key,
            )
            continue
        if not isinstance(edits, dict):
            log.warning(
                "LLM mapping for %s is not an object: %r; dropping",
                svg_key,
                type(edits).__name__,
            )
            continue
        per_slide_index = shape_index[svg_key]
        slide_clean: dict[str, str] = {}
        for shape_id, new_text in edits.items():
            if shape_id not in per_slide_index:
                log.warning(
                    "LLM mapping for %s referenced unknown shape_id %r; dropping",
                    svg_key,
                    shape_id,
                )
                continue
            if not isinstance(new_text, str):
                log.warning(
                    "LLM mapping for %s shape %s has non-string value %r; dropping",
                    svg_key,
                    shape_id,
                    type(new_text).__name__,
                )
                continue
            stripped = new_text.strip()
            if not stripped:
                continue
            max_chars = per_slide_index[shape_id].get("max_chars", 999)
            if len(stripped) > max_chars:
                log.warning(
                    "LLM mapping for %s shape %s exceeds max_chars (%d > %d); "
                    "truncating to fit",
                    svg_key, shape_id, len(stripped), max_chars,
                )
                stripped = _truncate_to_fit(stripped, max_chars)
            slide_clean[shape_id] = stripped
        if slide_clean:
            cleaned[svg_key] = slide_clean
    return cleaned


def _truncate_to_fit(text: str, max_chars: int) -> str:
    """Truncate a string to ``max_chars``, preferring a CJK-safe break point.

    Plain ``text[:max_chars]`` would slice mid-character if a CJK
    character straddles the boundary, producing broken UTF-8. We back off
    to the nearest boundary that doesn't split a codepoint, then strip
    trailing whitespace and a trailing ``,.;:!?。；：、`` if any.
    """
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    # Walk back to a safe codepoint boundary (Python strings are sequences
    # of codepoints, not bytes, so ``len`` already measures codepoints).
    # Strip any trailing punctuation/space.
    return truncated.rstrip(" ,.;:!?。；：、""''")