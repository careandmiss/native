"""llm_planner.py — turn a markdown document into a PlannerResult.

This is the ``phase2.5`` planning step inside ``native_fill``: when the
caller passes ``content_markdown`` + ``options.llm_plan=true``, the server
runs this planner *after* ``pptx_to_svg.py --roundtrip`` has produced the
workspace and *before* phase 3 starts editing SVGs.

Inputs
------
* a content markdown file (path on disk)
* the workspace's ``authoring-svg-flat/authoring_summary.json``
  (ppt-master already wrote one during ``pptx_to_svg.py``)

Outputs (PlannerResult, Phase A)
-------------------------------
* ``content_mapping``  — ``{ "<svg>": { "<shape_id>": "<new_text>" }}``
* ``page_plan_additions`` — new pages the LLM wants to add by cloning
  skeleton SVGs. Each is
  ``{"source_slide": int, "svg": "<new-filename>.svg", "edits": {<shape_id>:<text>}}``
* ``new_blocks`` — structured content blocks for existing slides.
  Each entry is
  ``{"svg": "<existing.svg>", "id": "<block-id>", "bounds": "x y w h",
     "layout": "3-column-cards"|"flow-steps"|"revision-table"|"raw",
     "spec": {...layout-specific...}}``
* ``skeleton_kind`` — ``{"slide_NN.svg": "cover"|"toc"|"divider"|"content"|"ending"}``

The planner uses :func:`llm_client.llm_complete_json`. The LLM is asked to
return ONLY the JSON object so we can parse it without further cleverness.
The plan is validated against the SVG summary (shape_ids that don't exist
are dropped, mismatched slide files are surfaced as warnings).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import llm_client

log = logging.getLogger("mcp_ppt_native_fill.llm_planner")


SYSTEM_PROMPT = """\
You are a content-mapping planner for the ppt-master Edit Native PPTX pipeline.

You will receive:
  1. A `shape_index` — JSON describing every editable text shape in the
     source PPTX after round-trip. For each shape you see: `id` (e.g.
     `shape-23`), `placeholder` (current text), `font_size` (px), `frame`
     (`[x, y, w, h]` of the owning slot), and `max_chars` (the visible
     character capacity derived from frame width / font-size, NOT from
     the old placeholder length). `max_chars` is a HARD ceiling.
  2. A `skeleton_index` — the role of each slide in the template:
       * `cover`  — slide_01 style: one big title + a few small labels
       * `toc`    — slide_02 style: list of section headings
       * `divider` — slide_03 style: PART NN + chapter title (large font)
       * `content` — slide_04 style: section title + large body area
       * `ending` — slide_05 style: closing page (THANK YOU etc.)
     `divider_id` and `content_id` tell you which skeleton to clone
     when adding new pages for additional H1 sections.
  3. A `content_markdown` — the new material the user wants filled into
     the template.

Your task: produce a JSON plan with FOUR top-level keys (see "Output
format" below). The plan may need to ADD new pages and STRUCTURED blocks
beyond simple text replacement when the markdown has more content than
existing slots can hold.

Skeleton detection — when markdown H1 count > (existing non-cover, non-ending slides),
you MUST clone the divider + content skeletons to host the overflow. Each new H1
(except the first one, which usually uses existing content slide) becomes:
  - one new `<stem>_partNN_div.svg` (copy of `divider_id`'s SVG), and
  - one new `<stem>_partNN_content.svg` (copy of `content_id`'s SVG).
These new SVGs are registered in `page_plan_additions` with their text edits.
If H1 count <= existing content slides, you MAY keep all edits in existing slots.

Rules
-----
* `max_chars` is a HARD ceiling; safe target is `max_chars - 2`.
* Match slide filenames and shape IDs exactly (case-sensitive).
* Use markdown content verbatim — no invented facts, no language translation.
* When overflowing a slot, prefer `new_blocks` (3-column-cards / flow-steps /
  revision-table) over cramming into the existing slot.
* For new pages cloned from a skeleton, only fill the skeleton shape IDs that
  actually exist in the skeleton (text title + subtitle). Body content goes
  into a `new_blocks` entry on the cloned page, not into existing skeleton
  shapes (those are designed for short headers, not paragraphs).
* **MANDATORY for cloned content pages**: every `slide_partNN_content.svg`
  in `page_plan_additions` MUST have a matching entry in `new_blocks`
  (layout = 3-column-cards by default, with 2-3 cards from the section's
  paragraphs). If you forget, the slide renders as a giant blank rectangle
  on the body area. Do not emit a content clone without a `new_blocks`
  body.

Output format (return ONLY this JSON object)
--------------------------------------------
{
  "content_mapping": {
    "slide_01.svg": {"shape-23": "...", "shape-24": "..."},
    "slide_03.svg": {"shape-4": "...", "shape-5": "..."}
  },
  "page_plan_additions": [
    {
      "source_slide": <divider_id 1-based>,
      "svg": "slide_part02_div.svg",
      "edits": {"shape-<big_title_id>": "PART 02", "shape-<sub_id>": "章节名"}
    },
    {
      "source_slide": <content_id 1-based>,
      "svg": "slide_part02_content.svg",
      "edits": {"shape-<page_title_id>": "二、章节名"}
    }
  ],
  "new_blocks": [
    {
      "svg": "slide_part02_content.svg",
      "id": "content-body",
      "bounds": "120 130 1060 480",
      "layout": "3-column-cards",
      "spec": {
        "cards": [
          {"title": "...", "color": "#1D2CAB", "items": ["...", "..."]},
          {"title": "...", "color": "#EE822F", "items": ["...", "..."]},
          {"title": "...", "color": "#75BD42", "items": ["...", "..."]}
        ]
      }
    }
  ],
  "skeleton_kind": {
    "slide_01.svg": "cover",
    "slide_02.svg": "toc",
    "slide_03.svg": "divider",
    "slide_04.svg": "content",
    "slide_05.svg": "ending",
    "slide_part02_div.svg": "divider",
    "slide_part02_content.svg": "content"
  }
}

Available layouts for `new_blocks`:
- `3-column-cards` (1-4 cards; spec.cards = [{title, color, items}])
- `flow-steps` (2-5 ordered steps; spec.steps = [{title, items}])
- `revision-table` (header + rows; spec.rows = [{date, status, content, author}])
- `raw` (caller supplied SVG; spec.svg = "<svg>...</svg>")

If the markdown has no usable content for any shape, return `{}` for
content_mapping and page_plan_additions / new_blocks. Never invent facts.
"""


def plan_content_mapping(
    *,
    md_path: Path,
    workspace: Path,
    llm_config: llm_client.LLMConfig | None = None,
) -> PlannerResult:
    """Read the workspace SVGs + markdown, ask the LLM, return PlannerResult.

    Returns a ``PlannerResult`` whose ``content_mapping`` is the legacy
    dict-like shape (so callers that pre-Phase-A did
    ``mapping[svg][shape] = text`` keep working). The result also carries
    ``page_plan_additions`` / ``new_blocks`` / ``skeleton_kind`` for the
    Phase-A expansion pipeline to consume.

    Raises ``PlannerError`` on hard failures (missing files, malformed
    SVGs, LLM-side error that the planner cannot recover from).
    """
    _ = _load_summary(workspace)  # sanity check workspace exists
    md_text = _read_md(md_path)

    shape_index = _scan_text_shapes(workspace)
    skeleton_index = _detect_skeleton_kind(workspace)
    user_prompt = _build_user_prompt(
        shape_index=shape_index,
        skeleton_index=skeleton_index,
        md_text=md_text,
        md_path=md_path,
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

    result = _parse_planner_response(raw, shape_index)
    log.info(
        "plan_content_mapping produced mapping=%d slide(s)/%d edit(s), "
        "page_plan_additions=%d, new_blocks=%d",
        len(result.content_mapping),
        sum(len(v) for v in result.content_mapping.values()),
        len(result.page_plan_additions),
        len(result.new_blocks),
    )
    return result


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

class PlannerError(RuntimeError):
    """Raised on a planner-side problem the caller must surface."""


@dataclass
class PlannerResult:
    """Phase-2.5 planner output.

    Backward-compatible: behaves like the legacy ``content_mapping`` dict
    when used as a mapping (so existing tests / call-sites that iterated
    ``result[svg][shape]`` keep working), while carrying the full Phase-A
    payload (page_plan_additions, new_blocks, skeleton_kind).
    """

    content_mapping: dict[str, dict[str, str]] = field(default_factory=dict)
    page_plan_additions: list[dict[str, Any]] = field(default_factory=list)
    new_blocks: list[dict[str, Any]] = field(default_factory=list)
    skeleton_kind: dict[str, str] = field(default_factory=dict)

    # -- dict-like shim for backward compatibility ----------------------------
    def __getitem__(self, k: str) -> dict[str, str]:
        return self.content_mapping[k]

    def __iter__(self):
        return iter(self.content_mapping)

    def __len__(self) -> int:
        return len(self.content_mapping)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PlannerResult):
            return self.content_mapping == other.content_mapping
        if isinstance(other, dict):
            return self.content_mapping == other
        return NotImplemented

    def __bool__(self) -> bool:
        return bool(self.content_mapping)

    def __contains__(self, k: str) -> bool:
        return k in self.content_mapping

    def keys(self):
        return self.content_mapping.keys()

    def values(self):
        return self.content_mapping.values()

    def items(self):
        return self.content_mapping.items()

    def get(self, k: str, default=None):
        return self.content_mapping.get(k, default)


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
    *,
    shape_index: dict[str, dict[str, Any]],
    skeleton_index: dict[str, Any],
    md_text: str,
    md_path: Path,
) -> str:
    """Pack the shape index + skeleton index + markdown into one user msg."""
    payload = {
        "shape_index": shape_index,
        "skeleton_index": skeleton_index,
        "content_markdown_path": str(md_path.name),
        "content_markdown": md_text,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Skeleton detection.
# ---------------------------------------------------------------------------

# Heuristic table — kept here so it's testable in isolation. Phase B will
# swap the placeholder-length heuristic for a real font-size-based one;
# Phase A just needs a working baseline.
_SKELETON_RULES: list[tuple[str, dict[str, Any]]] = [
    # (kind, predicate params) — order matters; first match wins.
    ("ending", {"max_text_elements": 6, "min_text_chars_avg": 8,
                "first_id_starts_with": "shape-7"}),  # shape-7 "THANK YOU" in boteng
    ("cover",  {"text_element_range": (4, 8)}),       # 封面: 4-8 个槽
    ("toc",    {"text_element_range": (10, 16)}),     # 目录: 10-16 个并列槽
    ("divider",{"text_element_range": (1, 4),
                "min_font_size": 50}),                  # divider: 1-4 槽 + 大字号
    ("content",{"text_element_range": (1, 4),
                "max_font_size": 40}),                  # content: 小字号正文
]


def _detect_skeleton_kind(workspace: Path) -> dict[str, Any]:
    """Classify each slide's skeleton role + report divider/content ids.

    Returns a dict::

        {
          "skeleton_kind": {"slide_NN.svg": "cover|toc|divider|content|ending", ...},
          "divider_id": <1-based source_slide or None>,
          "content_id": <1-based source_slide or None>,
        }

    Heuristic (Phase A):
      * ending — last slide, low text count, presence of "THANK" placeholder
      * cover  — first slide, moderate text count, large title slot
      * toc    — slide with the most text_elements (catalog/目录)
      * divider — slide with ≥ 1 large-font (>50pt) title and few text elements
      * content — other slides with small-font body area

    Phase B will replace font-size thresholds with font-size × frame geometry;
    for now this works well enough on the boteng template.
    """
    from . import autofix  # local import to avoid circular at module load

    flat = workspace / "authoring-svg-flat"
    if not flat.is_dir():
        raise PlannerError(f"authoring-svg-flat not found: {flat}")
    svg_files = sorted(flat.glob("slide_*.svg"))
    if not svg_files:
        return {"skeleton_kind": {}, "divider_id": None, "content_id": None}

    kind: dict[str, str] = {}
    per_slide_stats: list[tuple[Path, int, float]] = []  # (path, n_text, max_fs)
    for svg_path in svg_files:
        try:
            _, root = autofix._parse_svg(svg_path)
        except Exception as exc:
            log.warning("skeleton_detect: skip %s (%s)", svg_path.name, exc)
            continue
        svg_ns = "{http://www.w3.org/2000/svg}"
        text_count = 0
        max_font_size = 0.0
        for t in root.iter(svg_ns + "text"):
            text_count += 1
            fs = float(t.get("font-size") or 0)
            if fs > max_font_size:
                max_font_size = fs
        per_slide_stats.append((svg_path, text_count, max_font_size))

    if not per_slide_stats:
        return {"skeleton_kind": {}, "divider_id": None, "content_id": None}

    # last slide → ending (highest priority).
    last_path, _, _ = per_slide_stats[-1]
    kind[last_path.name] = "ending"

    # first slide → cover.
    first_path, first_n, first_fs = per_slide_stats[0]
    if first_path.name not in kind:
        kind[first_path.name] = "cover"

    # toc = slide with most text_elements (excluding cover/ending we already
    # classified).
    candidates = [s for s in per_slide_stats if s[0].name not in kind]
    if candidates:
        toc_path, toc_n, _ = max(candidates, key=lambda s: s[1])
        kind[toc_path.name] = "toc"
        remaining = [s for s in candidates if s[0].name not in kind]
    else:
        remaining = []

    # divider = first remaining with max_font_size >= 50 (large title).
    divider_id = None
    for path, n, fs in remaining:
        if fs >= 50:
            kind[path.name] = "divider"
            divider_id = _source_slide_from_filename(path.name)
            remaining = [s for s in remaining if s[0].name not in kind]
            break

    # everything else → content.
    content_id = None
    for path, n, fs in remaining:
        if path.name not in kind:
            kind[path.name] = "content"
            if content_id is None:
                content_id = _source_slide_from_filename(path.name)

    return {
        "skeleton_kind": kind,
        "divider_id": divider_id,
        "content_id": content_id,
    }


def _source_slide_from_filename(name: str) -> int | None:
    """Extract 1-based source_slide index from ``slide_NN.svg``."""
    import re
    m = re.fullmatch(r"slide_(\d+)\.svg", name)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Planner response parser.
# ---------------------------------------------------------------------------

def _parse_planner_response(
    raw: dict[str, Any], shape_index: dict[str, dict[str, Any]]
) -> PlannerResult:
    """Validate the LLM response and return a PlannerResult.

    Accepts both the legacy single-dict shape (``{"slide_NN.svg": {...}}``)
    and the Phase-A four-key shape (content_mapping + page_plan_additions +
    new_blocks + skeleton_kind). Anything else is a hard PlannerError.
    """
    if not isinstance(raw, dict):
        raise PlannerError(
            f"LLM returned a non-object JSON value: {type(raw).__name__}"
        )

    # Legacy callers (Phase A's pre-deployed tests) pass just the
    # content_mapping dict. Auto-promote to PlannerResult.
    if "content_mapping" not in raw and "page_plan_additions" not in raw \
            and "new_blocks" not in raw:
        cm = _normalize_mapping(raw, shape_index)
        return PlannerResult(content_mapping=cm)

    cm_raw = raw.get("content_mapping") or {}
    if not isinstance(cm_raw, dict):
        raise PlannerError(
            f"content_mapping must be an object, got {type(cm_raw).__name__}"
        )
    cm = _normalize_mapping(cm_raw, shape_index)

    additions = _normalize_page_plan_additions(
        raw.get("page_plan_additions") or [], shape_index
    )
    blocks = _normalize_new_blocks(raw.get("new_blocks") or [])
    skel = raw.get("skeleton_kind") or {}
    if not isinstance(skel, dict):
        skel = {}
    skeleton_kind = {str(k): str(v) for k, v in skel.items()}

    return PlannerResult(
        content_mapping=cm,
        page_plan_additions=additions,
        new_blocks=blocks,
        skeleton_kind=skeleton_kind,
    )


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


def _normalize_page_plan_additions(
    raw: Any, shape_index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate the LLM's page_plan_additions.

    Drops entries that are not objects, miss source_slide, miss svg, or
    whose edits reference unknown shape_ids. Valid edits are clamped to
    the per-shape ``max_chars`` budget (Phase A: same heuristic as
    content_mapping; Phase B will replace with the geometry-based one).
    """
    if not isinstance(raw, list):
        log.warning(
            "page_plan_additions is not a list: %r; dropping",
            type(raw).__name__,
        )
        return []

    # Build a union of shape_ids across all slides so the LLM can target
    # shapes inherited from the skeleton — the cloned SVG retains the
    # skeleton's shape-IDs.
    all_shape_ids: dict[str, dict[str, Any]] = {}
    for slide_index in shape_index.values():
        all_shape_ids.update(slide_index)

    cleaned: list[dict[str, Any]] = []
    seen_svgs: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning(
                "page_plan_additions entry is not object: %r; dropping",
                type(entry).__name__,
            )
            continue
        svg = entry.get("svg")
        source_slide = entry.get("source_slide")
        if not isinstance(svg, str) or not svg:
            log.warning("page_plan_additions entry missing svg; dropping")
            continue
        if not isinstance(source_slide, int) or source_slide < 1:
            log.warning(
                "page_plan_additions entry %s missing valid source_slide; "
                "dropping", svg,
            )
            continue
        if svg in seen_svgs:
            log.warning(
                "page_plan_additions references duplicate svg %r; dropping",
                svg,
            )
            continue
        edits_raw = entry.get("edits") or {}
        if not isinstance(edits_raw, dict):
            log.warning(
                "page_plan_additions[%s].edits is not object; dropping edits",
                svg,
            )
            edits_raw = {}
        edits: dict[str, str] = {}
        for shape_id, new_text in edits_raw.items():
            if shape_id not in all_shape_ids:
                log.warning(
                    "page_plan_additions[%s] references unknown shape %r; "
                    "dropping",
                    svg, shape_id,
                )
                continue
            if not isinstance(new_text, str):
                continue
            stripped = new_text.strip()
            if not stripped:
                continue
            max_chars = all_shape_ids[shape_id].get("max_chars", 999)
            if len(stripped) > max_chars:
                log.warning(
                    "page_plan_additions[%s] shape %s exceeds max_chars "
                    "(%d > %d); truncating",
                    svg, shape_id, len(stripped), max_chars,
                )
                stripped = _truncate_to_fit(stripped, max_chars)
            edits[shape_id] = stripped
        cleaned.append(
            {
                "source_slide": source_slide,
                "svg": svg,
                "edits": edits,
            }
        )
        seen_svgs.add(svg)
    return cleaned


def _normalize_new_blocks(raw: Any) -> list[dict[str, Any]]:
    """Validate the LLM's new_blocks list.

    Each block must have svg + bounds + layout + spec. Supported layouts
    match those supported by ``pipeline._render_new_block``; unsupported
    layouts are dropped with a warning. ``raw`` layout gets minimal
    validation; structural layouts get per-layout spec validation.
    """
    if not isinstance(raw, list):
        log.warning("new_blocks is not a list: %r; dropping", type(raw).__name__)
        return []
    cleaned: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("new_block entry is not object; dropping")
            continue
        svg = entry.get("svg")
        bounds = entry.get("bounds")
        layout = entry.get("layout")
        spec = entry.get("spec") or {}
        if not isinstance(svg, str) or not svg:
            log.warning("new_block missing svg; dropping")
            continue
        if not isinstance(bounds, str) or not bounds:
            log.warning("new_block %s missing bounds; dropping", svg)
            continue
        if layout not in {"3-column-cards", "flow-steps",
                          "revision-table", "raw"}:
            log.warning(
                "new_block %s uses unsupported layout %r; dropping",
                svg, layout,
            )
            continue
        if not isinstance(spec, dict):
            log.warning("new_block %s spec is not object; dropping", svg)
            continue
        # Per-layout validation.
        if layout == "3-column-cards":
            cards = spec.get("cards") or []
            if not 1 <= len(cards) <= 4:
                log.warning(
                    "3-column-cards requires 1-4 cards, got %d; dropping",
                    len(cards),
                )
                continue
        elif layout == "flow-steps":
            steps = spec.get("steps") or []
            if not 2 <= len(steps) <= 5:
                log.warning(
                    "flow-steps requires 2-5 steps, got %d; dropping",
                    len(steps),
                )
                continue
        elif layout == "revision-table":
            rows = spec.get("rows") or []
            if not rows:
                log.warning("revision-table requires rows; dropping")
                continue
        elif layout == "raw":
            if not isinstance(spec.get("svg"), str) or not spec["svg"]:
                log.warning("raw new_block requires spec.svg; dropping")
                continue
        block_id = entry.get("id") or f"new-block-{len(cleaned) + 1}"
        cleaned.append({
            "svg": svg,
            "id": str(block_id),
            "bounds": bounds,
            "layout": layout,
            "spec": spec,
        })
    return cleaned