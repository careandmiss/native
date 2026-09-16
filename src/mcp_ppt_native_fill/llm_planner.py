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
import re
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

Composition Patterns (Phase B, Bug 3) — pick layout by content shape,
NOT by template slot. The default of "always 3-column-cards" makes
cloned content pages look identical and rigid. Vary the layout based
on what the section is actually saying:

  * 1 big claim / single KPI / total (e.g. "5 章", "200 万") → ``hero-number``
  * 1 motto / quote / center thesis (e.g. "秉公办事、维护公司利益") → ``callout-box``
  * 2 juxtaposed alternatives / contrast (e.g. "公开招标 vs 邀请招标") → ``two-column-compare``
  * 5+ ordered steps in time (e.g. "申请 → 审批 → 采购 → 验收入库") → ``timeline`` (or ``flow-steps`` if 2-4 steps)
  * 4+ parallel peer items (≤3 columns of comparable things) → ``3-column-cards``
  * version / revision history (date / status / author) → ``revision-table``
  * dense paragraphs with no clear structure → ``3-column-cards`` (default fallback)

Phase 7+ extended archetypes (ppt-master presentation_core / report_core
inspired, 2026-09-16). Use these when they fit better than the legacy
nine:

  * single short sentence (20-79 chars) / chapter intro claim → ``hero_statement``
    (68px centered headline on a tinted panel with optional subline)
  * single long paragraph (≥80 chars) / chapter manifesto → ``statement-caption``
    (256px left rail + right content panel)
  * 1 card with 2-5 keyword-style items (each ≤12 chars) → ``kpi_row``
    (N tiles + evidence panel below)
  * 3+ ordered H2 sub-sections synthesizing macro phases
    (e.g. 7 procedural steps → 4 macro phases: 申请/审批/采购/验收) →
    ``procedural-steps`` (numbered circles + takeaway band)
  * 2 H2 sub-sections → ``three-thesis-cards``
  * explicit contrast / side-by-side comparison (新 vs 旧, A vs B) →
    ``comparison`` (two large panels with central divider)
  * 4 quadrants / SWOT / 矩阵 / 象限 → ``matrix_2x2`` (four panels)

IMPORTANT: Do NOT fabricate chapter numbers like "1目的" / "2适用范围".
When rendering chapter intros, use the FULL title verbatim as the
``hero_statement`` headline (e.g. "为了规范公司采购行为..."), NOT a
synthesized chapter number + keyword concatenation. ``hero-number``
(layout: hero-number) is reserved for ACTUAL numeric KPIs (e.g. "5"
with caption "大目标") — not for chapter intros.

At least ONE cloned content page per deck should use a non-default
layout (``hero-number``, ``callout-box``, ``two-column-compare``, or
``timeline``) so the deck doesn't look templated.

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
- `hero-number` (centered KPI; spec.value, spec.unit?, spec.caption?)
- `callout-box` (tinted quote panel; spec.quote, spec.attribution?)
- `two-column-compare` (juxtaposition; spec.left={title, items}, spec.right={title, items})
- `timeline` (2-5 ordered nodes on a horizontal axis; spec.steps=[{label, detail, color?}])
- `raw` (caller supplied SVG; spec.svg = "<svg>...</svg>")
- Phase 7+:
  - `statement-caption` (long single paragraph; spec.body=..., spec.title?=..., spec.eyebrow?=...)
  - `procedural-steps` (N=3-5 macro phases; spec.phases=[{label, detail}], spec.takeaway?=...)
  - `three-thesis-cards` (exactly 2 H2-driven cards; spec.cards=[{title, items}])
  - `hero_statement` (single short claim; spec.headline, spec.subline?, spec.eyebrow?)
  - `kpi_row` (N=2-5 tiles; spec.tiles=[{keyword, descriptor, value?}], spec.evidence?)
- Phase 9:
  - `comparison` (two side-by-side panels; spec.left={title, content}, spec.right={title, content})
  - `matrix_2x2` (four quadrants; spec.x_axis, spec.y_axis, spec.quadrants=[t,r,b,l])

If the markdown has no usable content for any shape, return `{}` for
content_mapping and page_plan_additions / new_blocks. Never invent facts.
"""


def plan_content_mapping(
    *,
    md_path: Path,
    workspace: Path,
    llm_config: llm_client.LLMConfig | None = None,
    layout_hints: dict[str, Any] | None = None,
) -> PlannerResult:
    """Read the workspace SVGs + markdown, ask the LLM, return PlannerResult.

    Returns a ``PlannerResult`` whose ``content_mapping`` is the legacy
    dict-like shape (so callers that pre-Phase-A did
    ``mapping[svg][shape] = text`` keep working). The result also carries
    ``page_plan_additions`` / ``new_blocks`` / ``skeleton_kind`` for the
    Phase-A expansion pipeline to consume.

    ``layout_hints`` (Phase 9, 2026-09-16): caller-supplied dict of
    preferences propagated to the user prompt. Currently supported
    keys:
      * ``force_archetype`` (bool): when true, the LLM must pick one of
        the layouts in the SYSTEM_PROMPT whitelist, not invent custom
        SVG fragments.
      * ``prefer_new`` (bool): when true, the LLM must prefer the
        Phase 7+ archetypes (statement-caption / procedural-steps /
        three-thesis-cards / hero_statement / kpi_row / comparison /
        matrix_2x2) over legacy 9 where either fits.
      * ``no_fabricate_chapter_numbers`` (bool): when true, the LLM
        must NOT synthesize chapter numbers like "1目的" — render the
        full verbatim title.

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
        layout_hints=layout_hints,
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


def _geometry_based_max_chars(grp: Any, text: Any) -> int | None:
    """Compute ``max_chars`` from the shape's frame geometry + font-size.

    Uses ``mcp_ppt_native_fill.text_width.chars_that_fit`` which is a
    fork of ppt-master's ``svg_to_pptx/drawingml/utils.py`` per-character
    width estimator. We binary-search a CJK and a Latin sample and
    return the tighter cap so neither language overflows.

    Returns ``None`` to signal the caller should fall back to the
    placeholder-length heuristic when frame or font-size info is
    missing.
    """
    from mcp_ppt_native_fill.text_width import chars_that_fit
    frame_attr = grp.get("data-pptx-frame")
    if not frame_attr:
        return None
    parts = frame_attr.split()
    if len(parts) < 4:
        return None
    try:
        _, _, w, _ = (float(p) for p in parts[:4])
    except ValueError:
        return None
    if w <= 0:
        return None
    fs_attr = text.get("font-size")
    if not fs_attr:
        return None
    try:
        fs = float(fs_attr)
    except ValueError:
        return None
    if fs <= 0:
        return None
    # font_weight: bold bumps latin 5 %, ignored by CJK (CJK fonts
    # already render bold glyphs the same width).
    weight = text.get("font-weight") or "400"
    return chars_that_fit(w, fs, font_weight=str(weight))


def _scan_text_shapes(workspace: Path) -> dict[str, dict[str, Any]]:
    """Parse every ``authoring-svg-flat/*.svg`` and return a per-slide map of
    editable text shapes. Each entry is::

        {shape_id: {"placeholder": "...", "max_chars": <int>}}

    ``max_chars`` is the visible character capacity of the slot, derived
    from the original placeholder's rendered width — typically the
    placeholder length + 20% margin so the LLM can keep similar-density
    text without overflowing. Empty placeholders default to a conservative
    24 chars (one short headline).

    TOC slides get a tighter per-font-size cap (Phase B, Bug 2): the
    generic placeholder-length heuristic gives 28-30 chars which lets
    the LLM write long chapter headings and leave a large blank gap
    under each item. TOC chapter title slots are typically 24-32pt and
    the rendered frame fits ~8 CJK chars comfortably; subtitle slots
    are 14-18pt and fit ~6 chars. We honor the tighter of the two so
    the LLM keeps titles short.

    Non-TOC slides use a frame-geometry-aware cap (Phase B+, fix #2):
    when the shape's ``data-pptx-frame`` attribute is parseable, we
    compute ``frame_width / font_size`` (CJK chars render ~1.0× the
    font-size; mixed Latin chars ~0.55×) and apply a 0.85 safety
    factor so the LLM's text always fits. This catches the boteng
    template's narrow cover title (47pt in a 681px frame ≈ 14 chars)
    and section title (23pt in a 427px frame ≈ 16 chars) without
    forcing us to hardcode per-template magic numbers. The
    placeholder-length heuristic is kept as a fallback when frame
    info is missing.

    Uses the same repair pipeline as ``autofix._parse_svg`` so vendor SVG
    quirks (duplicate attributes, unescaped inner quotes) don't trip the
    parser.
    """
    from . import autofix  # local import to avoid circular at module load
    import re  # local — only used for the TOC eligibility check below

    flat = workspace / "authoring-svg-flat"
    if not flat.is_dir():
        raise PlannerError(f"authoring-svg-flat not found: {flat}")
    # Pre-compute the per-slide skeleton role once. The boteng template
    # has 10-12 TOC slots on slide_02; we want all of them tightened
    # without changing the schema (still {shape_id: {placeholder,
    # max_chars}}).
    skeleton_kind = _detect_skeleton_kind(workspace).get("skeleton_kind", {})
    index: dict[str, dict[str, Any]] = {}
    for svg_path in sorted(flat.glob("*.svg")):
        try:
            _, root = autofix._parse_svg(svg_path)
        except Exception as exc:
            log.warning("planner: skip %s (%s)", svg_path.name, exc)
            continue
        svg_ns = "{http://www.w3.org/2000/svg}"
        # Restrict the Bug 2 tight cap to ORIGINAL slide_NN.svg files
        # only. Cloned slides (slide_partNN_*.svg) inherit the slide_04
        # (content) skeleton's shape IDs, but _detect_skeleton_kind may
        # misclassify them as "toc" if the previous run's LLM edits left
        # lots of text on the page. The cap is meant for the catalog
        # page, not content clones.
        is_original_skeleton = bool(
            re.fullmatch(r"slide_\d+\.svg", svg_path.name)
        )
        is_toc_slide = (
            is_original_skeleton
            and skeleton_kind.get(svg_path.name) == "toc"
        )
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
            heuristic = int(base * 1.2)
            # Bug 2 fix (Phase B): on TOC slides, override the generic
            # heuristic with a font-size-aware cap so chapter titles
            # stay short. Big-font slots (title, ≥24pt) → 8 chars;
            # small-font slots (subtitle, <24pt) → 6 chars. The min()
            # preserves the original heuristic for unusually small frames
            # (so we never loosen the cap).
            if is_toc_slide and texts:
                fs = float(texts[0].get("font-size") or 16)
                cap = 8 if fs >= 24 else 6
                max_chars = min(heuristic, cap)
            else:
                # Phase B+ fix #2 (frame-geometry): prefer frame_width /
                # font_size over the placeholder-length heuristic when
                # both are parseable. CJK glyphs render ~1.0× font-size
                # in the boteng template's "微软雅黑" / "思源黑体 CN"
                # stacks; Latin glyphs ~0.55×. 0.85 safety factor leaves
                # 15% headroom for letter-spacing / hinting artifacts.
                geom_cap = _geometry_based_max_chars(grp, texts[0])
                max_chars = (
                    min(heuristic, geom_cap) if geom_cap is not None
                    else heuristic
                )
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
    layout_hints: dict[str, Any] | None = None,
) -> str:
    """Pack the shape index + skeleton index + markdown into one user msg.

    Phase 9 (2026-09-16): if ``layout_hints`` is provided, include it
    as a top-level key in the JSON payload so the LLM sees caller
    preferences (force_archetype / prefer_new / no_fabricate_chapter_numbers).
    """
    payload: dict[str, Any] = {
        "shape_index": shape_index,
        "skeleton_index": skeleton_index,
        "content_markdown_path": str(md_path.name),
        "content_markdown": md_text,
    }
    if layout_hints:
        payload["layout_hints"] = layout_hints
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
      * divider — slide with ≥ 1 large-font (>38pt) title and few text elements
      * content — other slides with small-font body area

    Bug 02 fix (Phase C1): the divider threshold was 50pt (matched boteng
    only). Templates using 38-49pt divider titles were mis-classified as
    content. Lowered threshold to 38pt AND added a semantic fallback: any
    slide whose body text matches "第N章" / "PART N" patterns is classified
    as divider regardless of font size. This catches templates that
    economize on type size for non-cover pages.

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

    # Bug 02 (Phase C1): per-slide semantic flags.
    # ``has_chapter_marker`` — body text matches "第N章" / "PART N" patterns.
    # Used as fallback divider signal when font-size heuristic is too strict.
    chapter_re = re.compile(r"(第[一-鿿]{1,4}章|PART\s*[0-9一二三四五六七八九十]+)")
    part_stats: list[tuple[Path, int, float, bool]] = []  # (path, n_text, max_fs, has_chapter)
    for svg_path in svg_files:
        try:
            _, root = autofix._parse_svg(svg_path)
        except Exception as exc:
            log.warning("skeleton_detect: skip %s (%s)", svg_path.name, exc)
            continue
        svg_ns = "{http://www.w3.org/2000/svg}"
        text_count = 0
        max_font_size = 0.0
        body_text_chunks: list[str] = []
        for t in root.iter(svg_ns + "text"):
            text_count += 1
            fs = float(t.get("font-size") or 0)
            if fs > max_font_size:
                max_font_size = fs
            body_text_chunks.append("".join(t.itertext()))
        body_text = " ".join(body_text_chunks)
        has_chapter = bool(chapter_re.search(body_text))
        part_stats.append((svg_path, text_count, max_font_size, has_chapter))

    if not part_stats:
        return {"skeleton_kind": {}, "divider_id": None, "content_id": None}

    kind: dict[str, str] = {}
    # last slide → ending (highest priority).
    last_path = part_stats[-1][0]
    kind[last_path.name] = "ending"

    # first slide → cover.
    first_path = part_stats[0][0]
    if first_path.name not in kind:
        kind[first_path.name] = "cover"

    # toc = slide with most text_elements (excluding cover/ending we already
    # classified, AND slides with chapter markers — those are dividers, not toc).
    candidates = [s for s in part_stats if s[0].name not in kind and not s[3]]
    remaining_all = [s for s in part_stats if s[0].name not in kind]
    if candidates:
        toc_path, toc_n, _, _ = max(candidates, key=lambda s: s[1])
        kind[toc_path.name] = "toc"
        remaining = [s for s in remaining_all if s[0].name not in kind]
    else:
        remaining = remaining_all

    # Bug 02 fix: divider = first remaining with max_font_size >= 38
    # (was 50; lowered for 38-49pt divider titles) OR body contains
    # a chapter marker (semantic fallback for templates that use small
    # fonts for divider titles).
    divider_id = None
    for path, n, fs, has_chapter in remaining:
        if fs >= 38 or has_chapter:
            kind[path.name] = "divider"
            divider_id = _source_slide_from_filename(path.name)
            remaining = [s for s in remaining if s[0].name not in kind]
            break

    # everything else → content.
    content_id = None
    for path, n, fs, _ in remaining:
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

    Bug 16 fix: if the result was actually truncated (i.e. shorter
    than the input), append an ellipsis "…" so callers can tell that
    the content was shortened — without the marker, truncated text
    looks like data corruption.
    """
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    # Walk back to a safe codepoint boundary (Python strings are sequences
    # of codepoints, not bytes, so ``len`` already measures codepoints).
    # Strip any trailing punctuation/space.
    cleaned = truncated.rstrip(" ,.;:!?。；：、""''")
    # Only append the ellipsis if we actually cut content off.
    if len(cleaned) < len(text):
        cleaned = cleaned + "…"
    return cleaned


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
                          "revision-table", "raw",
                          "hero-number", "callout-box",
                          "two-column-compare", "timeline",
                          # Phase 7 (2026-09-16)
                          "statement-caption", "procedural-steps", "three-thesis-cards",
                          # Phase 8 (2026-09-16)
                          "hero_statement", "kpi_row",
                          # Phase 9 (2026-09-16)
                          "comparison", "matrix_2x2"}:
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
        elif layout == "hero-number":
            if not isinstance(spec.get("value"), str) or not spec["value"]:
                log.warning(
                    "hero-number requires spec.value (string); dropping"
                )
                continue
        elif layout == "callout-box":
            if not isinstance(spec.get("quote"), str) or not spec["quote"]:
                log.warning(
                    "callout-box requires spec.quote (string); dropping"
                )
                continue
        elif layout == "two-column-compare":
            left = spec.get("left")
            right = spec.get("right")
            if not (isinstance(left, dict) and isinstance(right, dict)):
                log.warning(
                    "two-column-compare requires spec.left and spec.right "
                    "(objects); dropping"
                )
                continue
            compare_ok = True
            for side_name, side in (("left", left), ("right", right)):
                if not isinstance(side.get("title"), str) or not side["title"]:
                    log.warning(
                        "two-column-compare spec.%s.title required; dropping",
                        side_name,
                    )
                    compare_ok = False
                    break
                if not isinstance(side.get("items"), list):
                    log.warning(
                        "two-column-compare spec.%s.items must be list; "
                        "dropping", side_name,
                    )
                    compare_ok = False
                    break
            if not compare_ok:
                continue
        elif layout == "timeline":
            steps = spec.get("steps") or []
            if not 2 <= len(steps) <= 5:
                log.warning(
                    "timeline requires 2-5 steps, got %d; dropping",
                    len(steps),
                )
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


# ---------------------------------------------------------------------------
# Cover title backfill (Phase C, 2026-09-16).
#
# The LLM planner is probabilistic. When the cover slide
# (``skeleton_kind["..._cover"]``) has no entry in the merged
# ``content_mapping``, the deck ships with the template's placeholder
# text on slide 1 — a clearly broken result. The cover title slot is
# deterministic enough to fill without the LLM: every cover has a
# single large title shape (max ``max_chars``), and the doc title comes
# from the markdown H1 / filename. This helper fills that gap.
#
# Idempotent and additive: it only writes when the cover slide has
# ZERO shape edits in ``merged_mapping``. Caller-supplied edits and
# any LLM edits are preserved.
# ---------------------------------------------------------------------------


def _doc_title_from_markdown(md_text: str, md_path: Path | None) -> str:
    """Best-effort doc title for the cover slide.

    Order:
      1. First H1 in the markdown (``# 标题``).
      2. Filename stem with separators (``_`` / ``-``) replaced by space.
      3. Empty string (caller should skip the backfill).
    """
    head_re = re.compile(r"^#\s+(.+)$", re.MULTILINE)
    m = head_re.search(md_text)
    if m:
        title = m.group(1).strip()
        # strip inline markdown emphasis (same as toc_detection.split_markdown_sections)
        title = re.sub(r"\*\*(.+?)\*\*", r"\1", title)
        title = re.sub(r"\*(.+?)\*", r"\1", title)
        title = re.sub(r"`(.+?)`", r"\1", title)
        if title:
            return title
    if md_path is not None:
        stem = md_path.stem
        stem = re.sub(r"[_\-]+", " ", stem).strip()
        if stem:
            return stem
    return ""


def _cover_svg_name(skeleton_kind: dict[str, str]) -> str | None:
    """Return the SVG filename marked as ``"cover"`` by skeleton detection.

    Returns ``None`` when no cover is classified (the template doesn't
    have a cover slide, or skeleton detection failed).
    """
    for name, role in skeleton_kind.items():
        if role == "cover":
            return name
    return None


def _pick_cover_title_shape(
    cover_shapes: dict[str, dict[str, Any]],
) -> tuple[str, int] | None:
    """Choose the cover slide's primary title shape (max ``max_chars``).

    Returns ``(shape_id, max_chars)`` or ``None`` if no shapes exist.
    Tie-break: larger frame area wins (rough — uses
    ``max_chars`` * 100 as a proxy since we don't expose frame w/h here).
    """
    if not cover_shapes:
        return None
    best_id = max(
        cover_shapes,
        key=lambda sid: cover_shapes[sid].get("max_chars", 0),
    )
    best = cover_shapes[best_id]
    return best_id, int(best.get("max_chars", 0))


def backfill_cover_title(
    *,
    merged_mapping: dict[str, dict[str, str]],
    workspace: Path,
    md_text: str,
    md_path: Path | None = None,
) -> dict[str, Any]:
    """Deterministically fill the cover slide title if it's empty.

    Only triggers when:
      * the workspace has a cover slide per skeleton detection, AND
      * the cover slide has **no** shape edits in ``merged_mapping``
        (LLM already wrote to it ⇒ we leave it alone; caller wins
        already), AND
      * we can derive a non-empty doc title from the markdown.

    Returns a small report dict so callers can log / surface it::

        {"filled": bool, "cover_svg": str|None, "shape_id": str|None,
         "title": str|None, "max_chars": int|None, "truncated": bool,
         "reason": str|None}
    """
    empty = {
        "filled": False, "cover_svg": None, "shape_id": None,
        "title": None, "max_chars": None, "truncated": False,
        "reason": None,
    }
    skeleton_index = _detect_skeleton_kind(workspace)
    skeleton_kind = (skeleton_index or {}).get("skeleton_kind") or {}
    cover_svg = _cover_svg_name(skeleton_kind)
    if not cover_svg:
        return {**empty, "reason": "no_cover_slide"}
    # LLM / caller already wrote to this slide → leave it alone.
    if merged_mapping.get(cover_svg):
        return {**empty, "reason": "cover_already_filled"}

    title = _doc_title_from_markdown(md_text, md_path)
    if not title:
        return {**empty, "reason": "no_doc_title"}

    shape_index = _scan_text_shapes(workspace)
    cover_shapes = shape_index.get(cover_svg) or {}
    picked = _pick_cover_title_shape(cover_shapes)
    if picked is None:
        return {**empty, "reason": "cover_no_text_shapes"}
    shape_id, max_chars = picked

    # Honor the same truncation ceiling the LLM planner enforces:
    # ``max_chars - 2`` is the safe target (see SYSTEM_PROMPT Rules).
    safe = max_chars - 2 if max_chars > 2 else max_chars
    truncated = False
    if len(title) > safe and safe > 0:
        title = title[:safe]
        truncated = True

    merged_mapping.setdefault(cover_svg, {})[shape_id] = title
    return {
        "filled": True, "cover_svg": cover_svg, "shape_id": shape_id,
        "title": title, "max_chars": max_chars, "truncated": truncated,
        "reason": None,
    }