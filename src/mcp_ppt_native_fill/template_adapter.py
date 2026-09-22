"""Phase 21 (2026-09-20): Template Adapter.

Goal: enable the pipeline to accept ANY pptx template without the
caller having to specify boteng-specific shape IDs like ``shape-4``
(PART NN label), ``shape-5`` (Chinese title), or ``shape-70`` (EN
subtitle).

Approach (inspired by C:\\Users\\Administrator\\.claude\\skills\\ppt-master
\\scripts\\template_text_slots.py which classifies SVG text slots by
their ``data-pptx-placeholder`` attribute):

1. **inspect_template(pptx_path)** walks the PPTX slides and classifies
   each into one of {cover, toc, divider, content, ending} using
   pure-shape heuristics (text counts, body rect dimensions, keyword
   presence). Picks the first match for each role.

2. **find_text_slots(pptx_path)** finds editable text slots per slide,
   exposing ``selector`` (CSS-like path), ``role`` (placeholder name),
   and ``editable`` flag. The pipeline uses these to auto-build
   ``expand_divider_edits_template`` / ``expand_content_edits_template``
   without caller input.

3. **ensure_ascii_path(pptx_path)** copies non-ASCII filenames to a
   deterministic ASCII path in the system temp dir, avoiding the
   PowerShell stdio encoding corruption that fails Chinese filename
   subprocess calls.

Why no portable shape ID mapping (shape-4, shape-5, etc.)?
Different templates use entirely different shape IDs. template_v2
has ``shape-2``, ``shape-5``, ``shape-12`` (vendor assigns by walk
order); boteng has ``shape-4``, ``shape-5``, ``shape-70`` (vendor
assigns by author intent). No portable mapping exists. Better to let
``archetype_router.route_archetype`` (Phase 20) + LLM pick the body
layout, and let ``template_text_slots`` find the actual placeholder
text slots in each template.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

from pptx import Presentation
from pptx.util import Emu


SlideKind = Literal["cover", "toc", "divider", "content", "ending", "unknown"]


@dataclass
class TextSlot:
    """One editable text placeholder in a template slide.
    
    Mirrors the role/selector model from
    ppt-master/scripts/template_text_slots.py:TemplateTextSlot, but
    discovered from PPTX shape names + geometry instead of from
    vendor-converted SVG attributes (so we don't require a prior
    pptx_to_svg conversion step).
    """
    slide_idx: int                    # 1-based
    shape_name: str                   # pptx shape.name (e.g. "标题 1" / "shape-5")
    role: str                         # "title" / "subtitle" / "part_label" / "body" / "footer" / "text"
    x: float                           # inches
    y: float
    w: float
    h: float
    current_text: str = ""            # placeholder text in template
    editable: bool = True


@dataclass
class TemplateProfile:
    """Auto-detected profile of a PPTX template. All slide indices
    are 1-based (matching python-pptx's ``slides`` indexing)."""
    
    # Required: which slides serve each role.
    cover_slide: int = 1
    toc_slide: int | None = None
    divider_skeleton: int | None = None
    content_skeleton: int | None = None
    ending_slide: int | None = None
    
    # Auto-detected body geometry.
    toc_grid: dict[str, int] = field(default_factory=dict)
    body_bounds: str = "120 130 1060 480"
    
    # Per-slide editable text slots (Phase 21 P0-B will consume these
    # to auto-build expand_divider_edits_template / etc.). Keyed by
    # 1-based slide index.
    text_slots: dict[int, list[TextSlot]] = field(default_factory=dict)
    
    # Cache key (hash of pptx path) for downstream translation cache.
    template_hash: str = ""
    
    def to_dict(self) -> dict:
        return {
            **asdict(self),
            # TextSlot list needs manual serialization (dataclass → dict).
            "text_slots": {
                idx: [asdict(s) for s in slots]
                for idx, slots in self.text_slots.items()
            },
        }


# Heuristic keywords. Each item: (substring, slide_kind).
_TOC_KEYWORDS = ("CONTENTS", "目录", "目錄", "Table of Contents")
_ENDING_KEYWORDS = ("THANK YOU", "THANK\u00a0YOU", "感谢", "謝謝", "谢谢")

# Role inference keywords for text slots (matched against shape name + text).
_TITLE_KEYWORDS = ("标题", "標題", "title", "TITLE", "headline", "Headline")
_PART_LABEL_KEYWORDS = ("PART ", "PART\u00a0", "PART\n", "part-label")
_SUBTITLE_KEYWORDS = ("副标题", "副標題", "subtitle", "Subtitle", "英文", "english")
_FOOTER_KEYWORDS = ("footer", "Footer", "页脚", "頁腳")
_BODY_KEYWORDS = ("body", "Body", "正文", "内容", "內容")


def _emu_to_inches(emu: int | None) -> float:
    if emu is None:
        return 0.0
    return emu / Emu(914400)


def _shape_texts(shape) -> list[str]:
    if not shape.has_text_frame:
        return []
    return [p.text for p in shape.text_frame.paragraphs if p.text.strip()]


def _slide_text_blob(slide) -> str:
    parts: list[str] = []
    for sh in slide.shapes:
        parts.extend(_shape_texts(sh))
    return " ".join(parts).upper()


def _classify_slide(slide, slide_idx: int | None = None,
                    n_slides: int | None = None) -> SlideKind:
    """Classify one slide into cover/toc/divider/content/ending.

    Heuristic priority (first match wins):
      1. TOC: contains "CONTENTS" / "目录" keyword OR has 4+ small
         card-shape rectangles arranged in a grid.
      2. Ending: contains "THANK YOU" / "感谢" keyword AND few texts.
      3. Content: has a big body rectangle (≥ 7×4 inches below top).
      4. Divider: 2-4 large text shapes, no big body rect.
      5. Position-based fallback (Phase 23+ commit 5): if no rule
         applies (text_count=0 — graphic-only template like
         template_v2), infer from slide position in the deck:
           slide 1         -> cover
           slide 2         -> toc
           middle slides   -> divider
           last 1-2 slides -> ending
      6. Cover: default.
    """
    blob = _slide_text_blob(slide)
    text_count = sum(1 for sh in slide.shapes if _shape_texts(sh))

    # Rule 1: TOC.
    if any(kw.upper() in blob for kw in _TOC_KEYWORDS):
        return "toc"
    small_rects = [
        sh for sh in slide.shapes
        if 0.5 <= _emu_to_inches(sh.width) <= 4.0
        and 0.5 <= _emu_to_inches(sh.height) <= 4.0
    ]
    if len(small_rects) >= 4:
        return "toc"
    
    # Rule 2: Ending. Text keyword (THANK YOU / 感谢) OR last slide of
    # the deck (closing slide position heuristic for templates that
    # don't follow the conventional "thank you" closing text).
    is_last_slide = slide_idx is not None and n_slides is not None \
        and slide_idx == n_slides
    if any(kw.upper() in blob for kw in _ENDING_KEYWORDS) and text_count <= 3:
        return "ending"
    if is_last_slide and text_count <= 3:
        return "ending"
    
    # Rule 3: Content (has big body rect).
    has_big_body = any(
        _emu_to_inches(sh.width) >= 7.0
        and _emu_to_inches(sh.height) >= 4.0
        and _emu_to_inches(sh.top) >= 1.0
        for sh in slide.shapes
    )
    if has_big_body:
        return "content"
    
    # Rule 4: Divider (few text shapes, no big body).
    if 2 <= text_count <= 4:
        return "divider"

    # Rule 5: Position-based fallback for graphic-only templates
    # (text_count == 0, all rules above failed). Phase 23+ commit 5:
    # template_v2.pptx has decorative slides (slides 3/4/5) with only
    # picture/shape geometry and no text shapes — the previous
    # fallback "cover" was wrong because all 5 slides then looked
    # like cover. Use the slide's position in the deck as a
    # structural hint:
    #   n_slides == 5 → slide 3=divider, 4=content, 5=ending
    #   n_slides >= 4 → last slide = ending, middle = divider
    if text_count == 0 and slide_idx is not None and n_slides is not None:
        if slide_idx == 1:
            return "cover"
        if slide_idx == 2 and n_slides >= 3:
            return "toc"
        if slide_idx == n_slides:
            return "ending"
        # 5-slide templates: middle two are divider + content.
        if n_slides == 5:
            return "divider" if slide_idx == 3 else "content"
        # Larger templates: middle slides alternate divider/content,
        # but we can't disambiguate without more signal — default to
        # divider (the most common middle ground-page role).
        return "divider"

    return "cover"


def _detect_toc_grid(slide) -> dict[str, int]:
    """Detect TOC card grid (rows × cols) from slide shape positions."""
    cards = []
    for sh in slide.shapes:
        w = _emu_to_inches(sh.width)
        h = _emu_to_inches(sh.height)
        x = _emu_to_inches(sh.left)
        y = _emu_to_inches(sh.top)
        if 1.5 <= w <= 4.5 and 0.5 <= h <= 3.5 and 0.05 <= x <= 12.5 and 0.5 <= y <= 6.5:
            cards.append((x, y, w, h))
    if len(cards) < 2:
        return {"rows": 1, "cols": max(1, len(cards))}
    sorted_by_y = sorted(cards, key=lambda c: c[1])
    rows = 1
    last_y = sorted_by_y[0][1]
    for _, y, _, _ in sorted_by_y[1:]:
        if abs(y - last_y) > 0.3:
            rows += 1
            last_y = y
    cols = max(1, (len(cards) + rows - 1) // rows)
    return {"rows": rows, "cols": cols}


def _detect_body_bounds(slide) -> str:
    """Detect the largest non-edge rectangle on a content slide.
    Returns ``"x y w h"`` in SVG px (96 px/inch).
    """
    best = None
    best_area = 0.0
    for sh in slide.shapes:
        x = _emu_to_inches(sh.left)
        y = _emu_to_inches(sh.top)
        w = _emu_to_inches(sh.width)
        h = _emu_to_inches(sh.height)
        if w >= 12.0 and h >= 7.0:
            continue
        if y < 1.0 or y + h > 6.5:
            continue
        area = w * h
        if area > best_area:
            best_area = area
            best = (x, y, w, h)
    if best is None:
        return "120 130 1060 480"
    x, y, w, h = best
    return f"{int(x*96)} {int(y*96)} {int(w*96)} {int(h*96)}"


def _infer_slot_role(shape, current_text: str) -> str:
    """Infer a text slot's semantic role. Combines four signals:

      1. Placeholder type (authoritative when available)
      2. Shape name keywords (legacy heuristic, robust for boteng)
      3. Geometry (y ratio vs slide height, body area)
      4. Text content heuristics ("PART " label vs long body text)

    PPTX placeholder types (PP_PLACEHOLDER enum):
      TITLE / CENTER_TITLE → "title"
      SUBTITLE               → "subtitle"
      BODY                   → "body"

    Geometry fallback:
      y_ratio < 0.15 (top of slide)    → "title"
      y_ratio > 0.85 (bottom)         → "footer"
      body area > 1.5 sq inches       → "body"
    """
    # 1. Placeholder type (authoritative)
    if getattr(shape, "is_placeholder", False):
        try:
            from pptx.enum.shapes import PP_PLACEHOLDER  # type: ignore
            ph_type = shape.placeholder_format.type
            if ph_type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
                return "title"
            if ph_type == PP_PLACEHOLDER.SUBTITLE:
                return "subtitle"
            if ph_type == PP_PLACEHOLDER.BODY:
                return "body"
        except Exception:
            pass  # not all templates use real placeholders

    # 2. Shape-name keyword heuristic (legacy)
    name_lower = (shape.name or "").lower()
    if any(kw.lower() in name_lower for kw in _TITLE_KEYWORDS):
        return "title"
    if any(kw.lower() in name_lower for kw in _PART_LABEL_KEYWORDS):
        return "part_label"
    if any(kw.lower() in name_lower for kw in _SUBTITLE_KEYWORDS):
        return "subtitle"
    if any(kw.lower() in name_lower for kw in _FOOTER_KEYWORDS):
        return "footer"
    if any(kw.lower() in name_lower for kw in _BODY_KEYWORDS):
        return "body"

    # 3. Geometry heuristic (no name keyword hit)
    try:
        slide_height = shape.part.slide.part_height / 914400  # inches
    except Exception:
        slide_height = 7.5
    y_in = _emu_to_inches(getattr(shape, "top", 0) or 0)
    y_ratio = y_in / slide_height if slide_height else 0

    if "PART " in current_text or "PART\n" in current_text:
        return "part_label"

    if y_ratio < 0.15 and current_text:
        return "title"
    if y_ratio > 0.85:
        return "footer"

    w = _emu_to_inches(getattr(shape, "width", 0) or 0)
    h = _emu_to_inches(getattr(shape, "height", 0) or 0)
    if w * h > 1.5 and 0.2 < y_ratio < 0.7:
        return "body"

    # 4. Text-content heuristic (legacy)
    if current_text and len(current_text) <= 30 and not any(
        c in current_text for c in "。.，,！!？?；;"
    ):
        return "title"
    return "text"


def _find_text_slots(slide, slide_idx: int) -> list[TextSlot]:
    """Walk every shape on a slide and emit TextSlot records for the
    editable text shapes. Inspired by ppt-master/template_text_slots
    but operates on PPTX-native shape names rather than svg attributes.
    """
    slots: list[TextSlot] = []
    for sh in slide.shapes:
        if not sh.has_text_frame:
            continue
        texts = _shape_texts(sh)
        if not texts:
            continue
        current_text = " ".join(texts)
        role = _infer_slot_role(sh, current_text)
        slots.append(TextSlot(
            slide_idx=slide_idx,
            shape_name=sh.name or "",
            role=role,
            x=_emu_to_inches(sh.left),
            y=_emu_to_inches(sh.top),
            w=_emu_to_inches(sh.width),
            h=_emu_to_inches(sh.height),
            current_text=current_text,
            editable=True,
        ))
    return slots


def _template_hash(pptx_path: Path) -> str:
    return hashlib.md5(str(pptx_path.resolve()).encode("utf-8")).hexdigest()[:8]


def inspect_template(pptx_path: Path) -> TemplateProfile:
    """Walk the PPTX and produce a TemplateProfile.
    
    1. Open the PPTX, classify each slide.
    2. Pick first match per role (cover/toc/divider/content/ending).
    3. Detect TOC grid + body bounds.
    4. Collect editable text slots per slide.
    
    Returns:
        ``TemplateProfile`` with cover_slide=1 by default and the
        remaining fields filled from the first classification match.
    """
    p = Presentation(str(pptx_path))
    profile = TemplateProfile(template_hash=_template_hash(pptx_path))
    n_slides = len(p.slides)

    for i, slide in enumerate(p.slides, 1):
        kind = _classify_slide(slide, slide_idx=i, n_slides=n_slides)
        profile.text_slots[i] = _find_text_slots(slide, i)
        if kind == "cover" and profile.divider_skeleton is None and profile.content_skeleton is None:
            profile.cover_slide = i
        elif kind == "toc" and profile.toc_slide is None:
            profile.toc_slide = i
            profile.toc_grid = _detect_toc_grid(slide)
        elif kind == "divider" and profile.divider_skeleton is None:
            profile.divider_skeleton = i
        elif kind == "content" and profile.content_skeleton is None:
            profile.content_skeleton = i
            profile.body_bounds = _detect_body_bounds(slide)
        elif kind == "ending" and profile.ending_slide is None:
            profile.ending_slide = i
    
    return profile


def ensure_ascii_path(pptx_path: Path) -> Path:
    """Copy non-ASCII pptx files to a deterministic ASCII path in
    ``%TEMP%\\mcp_ppt_ascii_templates\\`` to dodge PowerShell stdio
    encoding issues when the path appears in subprocess arguments.
    Idempotent (no-op if target already exists).
    """
    if pptx_path.name.isascii():
        return pptx_path
    digest = hashlib.md5(str(pptx_path.resolve()).encode("utf-8")).hexdigest()[:8]
    tmp_dir = Path(tempfile.gettempdir()) / "mcp_ppt_ascii_templates"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ascii_path = tmp_dir / f"{digest}_{pptx_path.stem}.pptx"
    if not ascii_path.exists():
        shutil.copy2(pptx_path, ascii_path)
    return ascii_path


__all__ = (
    "TemplateProfile",
    "TextSlot",
    "inspect_template",
    "ensure_ascii_path",
    "SlideKind",
)
