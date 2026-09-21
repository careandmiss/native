"""Phase 20 (2026-09-20): Rule-based layout selection for content slides.

Decision tree that classifies a markdown section body into a layout
archetype without invoking the LLM planner. The LLM is the fallback
for cases the rules cannot decide.

Rule priority: first match wins. Order matters — short-body /
empty-body rules fire before shape-detection rules so a 40-char
section with a stray "1." in it still resolves to merge-to-divider
rather than procedural-steps.

Rule catalog (see ``TestLayoutRule`` for the full positive / negative
matrix):

  Rule 0  empty body               → "simple-text"  (P0-B-3 fallback fills)
  Rule 1  short body (< 60 chars,  → "merge-to-divider"
           < 2 newlines)
  Rule 2  numbered ordered list    → "procedural-steps" if > 5 items
                                    else "bullet-list"
  Rule 3  markdown pipe table      → "revision-table"
  Rule 4  long single paragraph    → "simple-text"
           (> 200 chars, < 3 lines)
  Rule 5  KPI pattern              → "kpi_row"
  Rule 6  contrast words           → "two-column-compare"
  Rule 7  arrow flow               → "flow-steps"
  default                          → None (LLM-driven fallback)

The regex constants are deliberately shared with
``archetype_router.py:71-84`` so rule and heuristic cannot drift.
"""
from __future__ import annotations

from typing import TypedDict

# Shared with archetype_router. Importing keeps the regex identical;
# if a heuristic regex is updated there, layout_rules picks it up
# without needing a parallel edit.
from .archetype_router import (
    _ARROW_RE,
    _CONTRAST_RE,
    _KPI_RE,
    _ORDER_RE,
    _PIPE_TABLE_RE,
)

# Rule thresholds. Picked to be conservative — the worst case for
# these thresholds is "rule picks merge-to-divider when caller
# wanted a full slide", which loses at most ~3 lines of text in
# the divider footer (acceptable). The opposite case (rule picks
# a full layout when caller wanted to merge) wastes a slide,
# which is worse.
SHORT_BODY_CHARS = 60
SHORT_BODY_LINES = 2
LONG_PARAGRAPH_CHARS = 200
LONG_PARAGRAPH_LINES = 3
LIST_PROCEDURAL_THRESHOLD = 5


class RuleResult(TypedDict, total=False):
    """Return shape for ``select_layout_for_section``.

    Fields:
        layout: One of the 16 ``VALID_ARCHETYPES`` names, or
            ``"merge-to-divider"`` (signal: caller must skip the
            content-slide clone and inline the body into the
            divider footer).
        confidence: Match confidence. 1.0 for an explicit rule hit
            with no ambiguity; lower values reserved for future
            fuzzy rules.
        rule: Name of the rule that fired (e.g. ``"short-body"``).
            ``""`` when ``layout is None``.
    """

    layout: str | None
    confidence: float
    rule: str


def select_layout_for_section(section: dict | str) -> RuleResult:
    """Rule-based layout selection.

    Args:
        section: Either a dict with ``"body"`` key (typical pipeline
            caller) or a raw body string (test-only convenience).

    Returns:
        ``RuleResult`` with ``layout`` set to one of:
          - ``"merge-to-divider"`` — caller should NOT generate a
            separate content slide; instead inline the body into
            the divider footer.
          - A name from ``archetype_router.VALID_ARCHETYPES`` —
            caller should use this layout for the content slide.
          - ``None`` — no rule matched; caller should fall back to
            LLM-driven layout selection via ``archetype_router``.

    Rules are evaluated in priority order; the first hit returns.
    """
    if isinstance(section, str):
        body = section
    else:
        body = (section.get("body") if section else "") or ""

    body = body.strip()

    # Rule 0: empty body → simple-text placeholder (P0-B-3 fallback
    # synthesizes a 1-card placeholder so the slide is not blank).
    if not body:
        return {"layout": "simple-text", "confidence": 1.0,
                "rule": "empty-body"}

    # Rule 1: very short body → merge into divider, no content SVG.
    # This is the central Phase 20 design choice: stop generating
    # whole slides for one-line sections.
    # ``<=`` (not ``<``) so a 1-newline body (2 lines total, e.g.
    # "前言：\n公司商务行为管理。") still resolves to merge-to-divider.
    if (len(body) < SHORT_BODY_CHARS
            and body.count("\n") + 1 <= SHORT_BODY_LINES):
        return {"layout": "merge-to-divider", "confidence": 1.0,
                "rule": "short-body"}

    # Rule 2: numbered ordered list. Length thresholds choose
    # procedural-steps (long flow) vs bullet-list (short list).
    if _ORDER_RE.search(body):
        items = len(_ORDER_RE.findall(body))
        if items > LIST_PROCEDURAL_THRESHOLD:
            return {"layout": "procedural-steps", "confidence": 0.95,
                    "rule": "numbered-list-long"}
        return {"layout": "bullet-list", "confidence": 0.95,
                "rule": "numbered-list-short"}

    # Rule 3: markdown pipe table (any line starts+ends with |).
    if _PIPE_TABLE_RE.search(body):
        return {"layout": "revision-table", "confidence": 0.95,
                "rule": "pipe-table"}

    # Rule 4: long single paragraph — no list, no table, but too
    # much text for hero_statement; render as flowing body text.
    if (len(body) > LONG_PARAGRAPH_CHARS
            and body.count("\n") + 1 < LONG_PARAGRAPH_LINES):
        return {"layout": "simple-text", "confidence": 0.9,
                "rule": "long-paragraph"}

    # Rule 5: KPI pattern — single-line number+unit rows.
    if _KPI_RE.search(body):
        return {"layout": "kpi_row", "confidence": 0.9,
                "rule": "kpi-pattern"}

    # Rule 6: contrast words → two-column-compare.
    if _CONTRAST_RE.search(body):
        return {"layout": "two-column-compare", "confidence": 0.9,
                "rule": "contrast-words"}

    # Rule 7: arrow / flow → flow-steps (only when multi-line).
    if _ARROW_RE.search(body) and body.count("\n") >= 3:
        return {"layout": "flow-steps", "confidence": 0.85,
                "rule": "arrow-flow"}

    # No rule matched — caller falls back to LLM-driven selection.
    return {"layout": None, "confidence": 0.0, "rule": ""}
