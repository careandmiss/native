"""Section dispatcher — auto-split long sections into multiple pages.

Phase 17-D (2026-09-18). When a markdown section is denser than a
single archetype's natural page capacity, this module splits it into
N pages with a deliberate archetype rhythm:

    Page 1: hero intro (hero_statement / callout-box) — sets up the
            chapter with a single-sentence claim or quotation.
    Page 2..N-1: enumeration (3-column-cards / procedural-steps /
                bullet-list) — carries the dense content.
    Page N: takeaway (callout-box / hero_statement / statement-caption)
            — closes the section with the chapter's distilled
            conclusion.

The dispatcher is pure: takes a section's text and returns a list of
:class:`SplitPage` dicts. Each entry has ``archetype``, ``spec``,
``index`` (1-based), ``total``, and ``reason`` (for logging). The
caller (Phase 17 wiring in pipeline / llm_planner) is responsible for
emitting the matching ``page_plan_additions`` + ``new_blocks`` entries.

Thresholds (per archetype):
    hero_statement / callout-box / hero-number: 1 item (no split)
    statement-caption: 1 paragraph (no split)
    3-column-cards: 3-4 cards / page
    procedural-steps: 3-5 steps / page
    bullet-list: 6-8 items / page
    revision-table: 5-6 rows / page
    comparison: 2 sides (no split)

If the section's content fits in one page (no split needed), the
dispatcher returns a single-element list with the router's pick
(``archetype_router.route_archetype``) so callers don't have to
special-case the "1 page" case.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .archetype_router import route_archetype


# Maximum items per page per archetype. A section with more items is
# split so each page stays within the natural capacity of its layout.
_MAX_PER_PAGE: dict[str, int] = {
    "3-column-cards": 4,       # 1-4 cards / row
    "procedural-steps": 5,     # N=3-5 macro steps
    "flow-steps": 5,           # same
    "bullet-list": 6,          # bullet-list caps at ~6-8 for readability
    "kpi_row": 5,              # 2-5 tiles
    "timeline": 3,
    "revision-table": 6,
    "matrix_2x2": 4,
    "three-thesis-cards": 3,
    "two-column-compare": 2,   # 2 sides, no split
    "comparison": 2,           # 2 sides, no split
    # Hero archetypes: no split (1 page only).
    "hero_statement": 1,
    "hero-number": 1,
    "callout-box": 1,
    "statement-caption": 1,
    "simple-text": 1,
}


@dataclass
class SplitPage:
    """One page of a (possibly split) section."""

    archetype: str
    spec: dict[str, Any]
    index: int  # 1-based within the section
    total: int  # total pages for this section
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "archetype": self.archetype,
            "spec": self.spec,
            "index": self.index,
            "total": self.total,
            "reason": self.reason,
        }


def _split_spec_by_count(
    archetype: str, spec: dict[str, Any], max_per_page: int,
) -> list[dict[str, Any]]:
    """Split a spec into N sub-specs by content count.

    Honors the per-archetype ``max_per_page`` threshold. Returns 1+
    sub-specs that each fit on one page. The split strategy:

    * 3-column-cards: group items per card into pages by CARD count.
    * procedural-steps / flow-steps: N steps → ceil(N / max_per_page).
    * bullet-list: M items → ceil(M / max_per_page) pages.
    * revision-table: M rows → ceil(M / max_per_page) pages.
    * comparison / two-column-compare / three-thesis-cards: no split
      (return the spec verbatim).
    * Others: no split.
    """
    if max_per_page <= 0:
        return [spec]

    if archetype == "3-column-cards":
        cards = spec.get("cards") or []
        if len(cards) <= max_per_page:
            return [spec]
        out = []
        for i in range(0, len(cards), max_per_page):
            chunk = cards[i:i + max_per_page]
            out.append({**spec, "cards": chunk})
        return out

    if archetype in ("procedural-steps", "flow-steps"):
        steps = spec.get("steps") or []
        if len(steps) <= max_per_page:
            return [spec]
        out = []
        for i in range(0, len(steps), max_per_page):
            chunk = steps[i:i + max_per_page]
            out.append({**spec, "steps": chunk})
        return out

    if archetype == "bullet-list":
        items = spec.get("items") or []
        if len(items) <= max_per_page:
            return [spec]
        out = []
        for i in range(0, len(items), max_per_page):
            chunk = items[i:i + max_per_page]
            out.append({**spec, "items": chunk})
        return out

    if archetype == "kpi_row":
        tiles = spec.get("tiles") or []
        if len(tiles) <= max_per_page:
            return [spec]
        out = []
        for i in range(0, len(tiles), max_per_page):
            chunk = tiles[i:i + max_per_page]
            out.append({**spec, "tiles": chunk})
        return out

    if archetype == "revision-table":
        rows = spec.get("rows") or []
        if len(rows) <= max_per_page:
            return [spec]
        out = []
        for i in range(0, len(rows), max_per_page):
            chunk = rows[i:i + max_per_page]
            out.append({**spec, "rows": chunk})
        return out

    # Hero archetypes / non-splittable: return as-is.
    return [spec]


def _split_text_evenly(text: str, n: int) -> list[str]:
    """Split ``text`` into ``n`` roughly equal sub-sections by paragraph.

    Used when the router picks a hero archetype but the section has
    more content than fits in one hero page. We re-split by paragraph
    so each split page has a chunk that can be re-routed as its own
    hero intro / takeaway.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return [text] * n
    if len(paragraphs) <= n:
        return paragraphs + [""] * (n - len(paragraphs))
    # Distribute paragraphs as evenly as possible.
    out: list[str] = []
    base, extra = divmod(len(paragraphs), n)
    cursor = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        chunk = paragraphs[cursor:cursor + size]
        cursor += size
        out.append("\n\n".join(chunk))
    return out


def dispatch_section(
    section_text: str,
    *,
    base_spec: dict[str, Any] | None = None,
    section_title: str = "",
) -> list[SplitPage]:
    """Plan N pages for one section's content.

    Args:
        section_text: Raw markdown body of the section.
        base_spec: Optional spec the LLM emitted (cards / steps / etc.).
            When the section needs splitting, the dispatcher splits
            this spec by content count.
        section_title: Optional title (used for hero intro / takeaway
            headlined when kind).

    Returns:
        List of :class:`SplitPage`. Always >=1 element.
    """
    base_spec = base_spec or {}
    router_result = route_archetype(section_text)
    base_archetype = router_result["archetype"]
    max_per_page = _MAX_PER_PAGE.get(base_archetype, 1)

    # If the spec needs splitting, do it now.
    sub_specs = _split_spec_by_count(base_archetype, base_spec, max_per_page)
    if len(sub_specs) == 1:
        # No split needed; trust the router.
        return [
            SplitPage(
                archetype=base_archetype,
                spec=sub_specs[0],
                index=1,
                total=1,
                reason=router_result["reason"],
            )
        ]

    # Multi-page: emit N pages, each carrying a sub-spec. Page 1 uses
    # the router-picked archetype; subsequent pages use the same
    # archetype (visual rhythm: same layout, different content). The
    # caller (workspace_expand / llm_planner) decides whether to add
    # a hero intro / takeaway wrap.
    pages: list[SplitPage] = []
    for i, sub in enumerate(sub_specs):
        pages.append(
            SplitPage(
                archetype=base_archetype,
                spec=sub,
                index=i + 1,
                total=len(sub_specs),
                reason=f"split page {i + 1}/{len(sub_specs)} (max_per_page={max_per_page})",
            )
        )
    return pages


__all__: tuple[str, ...] = (
    "SplitPage",
    "dispatch_section",
    "_MAX_PER_PAGE",
)