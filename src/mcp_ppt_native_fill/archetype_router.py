"""Content-shape -> archetype auto-router.

Phase 17-A (2026-09-18): drives the "intelligent layout" stack. Given a
section's textual shape (paragraphs / list items / sub-headings / char
count), pick the archetype that best carries the content. Pure
heuristic — no LLM round-trip — so the 17 archetypes in
``archetype_meta.ARCHETYPE_META`` actually get used (the LLM is biased
toward 4-6 favorites and produces repetitive decks).

The router accepts an optional ``llm_choice`` argument and uses it
unless the heuristic is high-confidence (>0.85). When the LLM picks
``"raw"`` (no archetype), the heuristic always wins. This keeps LLM
flexibility while preventing degenerate layouts (``raw``, hero on
dense paragraphs, etc.).

Returns a :class:`RouteResult` tuple — never raises. Every archetype
name is one of the keys in ``ARCHETYPE_META`` so downstream consumers
can rely on it.

Rules (priority order, first match wins):

    1. 1 short claim (≤80 chars, 1 paragraph) + LLM picked hero →
       keep ``llm_choice`` (hero archetypes are unambiguous).
    2. Numeric KPI / 短数字 (``3`` + ``个目标``) → ``hero-number``.
    3. Long single quotation (>80 chars) + (一)(二)(三) sub-headings →
       ``statement-caption`` (1 page) or split into ``hero_statement``
       intro + ``procedural-steps`` enumeration.
    4. 5+ ordered list items / (一)(二)(三) sub-headings →
       ``procedural-steps``.
    5. Contrast keywords (新/旧 / 对比 / 区别 / vs.) →
       ``comparison`` (two-panel juxtaposition).
    6. Arrow / flow keywords (→ / 流程) →
       ``procedural-steps`` (sequence matters).
    7. 4+ peer items (unordered bullets) →
       ``3-column-cards`` (1-4 cards per row).
    8. 5+ bullet items (text-heavy) →
       ``bullet-list``.
    9. 1 long paragraph (≥80 chars, 1 paragraph) →
       ``statement-caption``.
   10. Long markdown pipe-table →
       ``revision-table``.
   11. Otherwise → ``bullet-list`` (safe fallback).

The heuristic never returns ``"raw"``; the renderer drops raw blocks
without text content, so emitting ``raw`` here is a guaranteed blank
canvas.
"""
from __future__ import annotations

import re
from typing import TypedDict

# Recognise the legacy 9 + Phase 7+ + Phase 16 archetypes as valid
# candidates. The router may only emit names that exist in
# ``archetype_meta.ARCHETYPE_META``; this frozenset is the whitelist.
VALID_ARCHETYPES = frozenset({
    "3-column-cards", "flow-steps", "revision-table",
    "hero-number", "callout-box", "two-column-compare", "timeline",
    "statement-caption", "procedural-steps", "three-thesis-cards",
    "hero_statement", "kpi_row", "comparison", "matrix_2x2",
    "simple-text", "bullet-list",
})

# Hero archetypes — the LLM is trusted for these because "1 short
# claim" / "1 short quote" are semantic choices the heuristic cannot
# reliably infer from text shape alone.
HERO_ARCHETYPES = frozenset({
    "hero_statement", "hero-number", "callout-box", "statement-caption",
})

# CJK sub-heading markers — (一)/(二) etc.
_SUBHEAD_RE = re.compile(r"[（(]\s*[一二三四五六七八九十]+\s*[)）]")
_ORDER_RE = re.compile(r"(?:^\s*\d+[\.\)、])|(?:步骤\s*\d+)|(?:Step\s*\d+)", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+])\s+", re.MULTILINE)
_CONTRAST_RE = re.compile(r"(?:对比|区别|不同于|反之|vs\.?|V\.S\.|新制度|旧制度|新方法|旧方法|新旧)")
_ARROW_RE = re.compile(r"(?:→|->|=>|(?:^\s*(?:[-*+]|\d+[\.\)、])\s*流程))", re.MULTILINE)
_PIPE_TABLE_RE = re.compile(r"^\|.+\|$", re.MULTILINE)
# KPI: a number alone on a line, OR a number followed by a single CJK
# unit character. Captures "5" / "200万" / "10个". Does NOT match
# "5 附件" or "10 项目" — those are section heads, not KPIs.
_KPI_RE = re.compile(
    r"^\s*\d+(\.\d+)?\s*[%万亿元台个件条人名次种项款章节份]?\s*$",
    re.MULTILINE,
)


class RouteResult(TypedDict):
    """Auto-routing decision.

    Attributes:
        archetype: One of the 17 archetype names in ``ARCHETYPE_META``.
        confidence: Match confidence in ``[0.5, 1.0]``. Values >=0.85
            override the LLM's choice.
        reason: One-line explanation logged for debugging.
        split: Suggested split count. ``1`` for single page; ``>=2``
            when the section is dense enough to warrant multiple
            content pages (Phase 17-D dispatcher uses this).
    """

    archetype: str
    confidence: float
    reason: str
    split: int


def _line_metrics(text: str) -> dict[str, int]:
    """Compute section shape metrics.

    Returns dict with: ``chars``, ``lines`` (non-empty), ``paragraphs``
    (consecutive non-empty lines grouped), ``bullets``, ``ordered``,
    ``subheads``.

    Paragraph detection: blank lines OR short standalone lines (each
    short line is its own paragraph unless the section explicitly
    groups them with blank lines). This matches how Chinese regulatory
    text reads — each "line" is typically a complete sentence or list
    item, not a wrapped continuation of a longer paragraph.
    """
    text = text or ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            # Blank line — close current paragraph.
            if current:
                paragraphs.append(current)
                current = []
            continue
        if current and all(len(p) < 60 for p in current + [s]):
            # All current lines + this one are short — treat each as
            # its own paragraph (typical of bullet-list / itemized
            # content in Chinese prose).
            paragraphs.append(current)
            current = [s]
        else:
            current.append(s)
    if current:
        paragraphs.append(current)
    return {
        "chars": len(text),
        "lines": len(lines),
        "paragraphs": len(paragraphs),
        "bullets": len(_BULLET_RE.findall(text)),
        "ordered": len(_ORDER_RE.findall(text)),
        "subheads": len(_SUBHEAD_RE.findall(text)),
    }


def _is_long_quotation(text: str) -> bool:
    """Detect single long quotation (>80 chars, single paragraph)."""
    m = _line_metrics(text)
    return m["chars"] >= 80 and m["paragraphs"] == 1


def _is_short_claim(text: str) -> bool:
    """Detect single short claim (≤80 chars, ≤1 paragraph)."""
    m = _line_metrics(text)
    return m["chars"] <= 80 and m["paragraphs"] <= 1


def _looks_like_kpi(text: str) -> bool:
    """Detect KPI-style lines (a number with a unit caption).

    E.g. "5 大目标", "200 万", "10 项". Conservative: only fires
    when the first line is a numeric token.
    """
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    return bool(_KPI_RE.match(first_line))


def _looks_like_pipe_table(text: str) -> bool:
    """Detect a markdown pipe table (>=3 lines starting/ending with |)."""
    matches = _PIPE_TABLE_RE.findall(text)
    return len(matches) >= 3


def route_archetype(
    section_text: str,
    *,
    llm_choice: str | None = None,
) -> RouteResult:
    """Pick the best archetype for ``section_text``.

    Args:
        section_text: The raw markdown body of one section (already
            stripped of the heading line). May be empty.
        llm_choice: Optional archetype the LLM picked in its
            ``new_blocks`` entry. Used when the heuristic is not
            high-confidence (<0.85). When ``llm_choice == "raw"`` or
            is unknown, the heuristic always wins.

    Returns:
        :class:`RouteResult` with the picked archetype, confidence,
        reason, and split hint.
    """
    m = _line_metrics(section_text)

    # Rule 1: 1 short claim + LLM picked hero → keep LLM (don't second-
    # guess semantic hero choices).
    if _is_short_claim(section_text) and llm_choice in HERO_ARCHETYPES:
        return {
            "archetype": llm_choice,
            "confidence": 0.95,
            "reason": "short claim + LLM hero choice; trusting LLM",
            "split": 1,
        }

    # Rule 2: numeric KPI line → hero-number. Fires BEFORE Rule 1b so
    # "5" / "200 万" / "10个" don't get mis-classified as hero_statement.
    if _looks_like_kpi(section_text):
        return {
            "archetype": "hero-number",
            "confidence": 0.85,
            "reason": "first line is a numeric KPI token",
            "split": 1,
        }

    # Rule 1b: 1 short claim (≤80 chars, ≤1 paragraph) with no list
    # structure → hero_statement. Single-sentence claims look right on
    # the hero big-question layout. We only fire this when no list
    # signal exists (no ordered / no bullets / no subheads).
    if (
        _is_short_claim(section_text)
        and m["bullets"] == 0
        and m["ordered"] == 0
        and m["subheads"] == 0
    ):
        return {
            "archetype": "hero_statement",
            "confidence": 0.8,
            "reason": "short non-list claim; hero_statement big-question",
            "split": 1,
        }

    # Rule 3: long single quotation → statement-caption (always; the
    # rail summary + panel body design is purpose-built for quotes).
    if _is_long_quotation(section_text):
        return {
            "archetype": "statement-caption",
            "confidence": 0.9,
            "reason": "long single quotation; statement-captrail rail+panel fits",
            "split": 1,
        }

    # Rule 4: 5+ ordered items OR (一)(二)(三) sub-headings →
    # procedural-steps (numbered circles + takeaway band).
    if m["subheads"] >= 3 or m["ordered"] >= 5:
        split = max(1, (m["ordered"] + 4) // 5) if m["ordered"] > 5 else 1
        if m["subheads"] >= 3:
            split = max(split, (m["subheads"] + 1) // 2)
        return {
            "archetype": "procedural-steps",
            "confidence": 0.9,
            "reason": (
                f"{m['ordered']} ordered items / {m['subheads']} sub-heads; "
                "procedural-steps (N=3-5) handles ordered sequence"
            ),
            "split": split,
        }

    # Rule 5: contrast → two-panel juxtaposition.
    if _CONTRAST_RE.search(section_text) and m["paragraphs"] >= 2:
        return {
            "archetype": "comparison",
            "confidence": 0.9,
            "reason": "contrast keyword + 2+ paragraphs; comparison panels",
            "split": 1,
        }

    # Rule 6: arrow / flow keywords → procedural-steps.
    if _ARROW_RE.search(section_text):
        return {
            "archetype": "procedural-steps",
            "confidence": 0.8,
            "reason": "arrow / flow keyword; procedural-steps captures sequence",
            "split": 1,
        }

    # Rule 7: pipe table → revision-table.
    if _looks_like_pipe_table(section_text):
        return {
            "archetype": "revision-table",
            "confidence": 0.9,
            "reason": "markdown pipe table; revision-table compresses without losing content",
            "split": 1,
        }

    # Rule 8: 3-4 peer items → 3-column-cards (the natural archetype
    # for "3 things in same category").
    if 3 <= m["bullets"] <= 4 or 3 <= m["paragraphs"] <= 4:
        return {
            "archetype": "3-column-cards",
            "confidence": 0.85,
            "reason": f"{m['bullets'] or m['paragraphs']} peer items; 3-column-cards",
            "split": 1,
        }

    # Rule 9: 5+ bullet items → bullet-list (text-heavy).
    if m["bullets"] >= 5 or (m["lines"] >= 5 and m["paragraphs"] == 1):
        return {
            "archetype": "bullet-list",
            "confidence": 0.8,
            "reason": (
                f"{m['bullets']} bullets / {m['lines']} lines; "
                "bullet-list handles text-heavy prose"
            ),
            "split": max(1, (m["bullets"] + 7) // 8),
        }

    # Rule 10: 1 long paragraph (>=80 chars) → statement-caption.
    if m["chars"] >= 80 and m["paragraphs"] == 1:
        return {
            "archetype": "statement-caption",
            "confidence": 0.75,
            "reason": "1 long paragraph; statement-caption rail+panel",
            "split": 1,
        }

    # Rule 11: many bullets with mid-density (2 bullets) → 3-column-cards.
    if m["bullets"] == 2:
        return {
            "archetype": "3-column-cards",
            "confidence": 0.65,
            "reason": "2 bullets; 3-column-cards (2 cards)",
            "split": 1,
        }

    # Fallback: bullet-list (never raw, never hero).
    return {
        "archetype": "bullet-list",
        "confidence": 0.55,
        "reason": "no rule matched; bullet-list is the safe prose fallback",
        "split": 1,
    }


def override_llm_choice(
    section_text: str,
    llm_choice: str | None,
) -> tuple[str, str]:
    """Decide whether to override the LLM's archetype choice.

    Returns (final_archetype, decision_source) where decision_source is
    one of: ``"heuristic_high_confidence"``, ``"heuristic_force"``
    (LLM picked raw / invalid), ``"llm_choice"``.

    Override conditions:
    - ``llm_choice is None`` or ``llm_choice == "raw"`` → heuristic wins
    - ``llm_choice not in VALID_ARCHETYPES`` → heuristic wins
    - heuristic confidence >= 0.85 → heuristic wins
    - else → LLM choice wins
    """
    result = route_archetype(section_text, llm_choice=llm_choice)
    if llm_choice is None or llm_choice == "raw":
        return result["archetype"], "heuristic_force"
    if llm_choice not in VALID_ARCHETYPES:
        return result["archetype"], "heuristic_force"
    if result["confidence"] >= 0.85:
        return result["archetype"], "heuristic_high_confidence"
    return llm_choice, "llm_choice"


__all__: tuple[str, ...] = (
    "RouteResult",
    "VALID_ARCHETYPES",
    "HERO_ARCHETYPES",
    "route_archetype",
    "override_llm_choice",
)