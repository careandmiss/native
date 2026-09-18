"""Heuristic Relationships-atom detector for markdown section text.

Borrowed from ppt-master's Relationships layer (see plan §2.2 step 4). The
detector inspects a markdown section's textual shape -- not its meaning -- and
returns the single atom whose rule matches first. The seven possible atoms
are:

* ``order``       -- ordered/numbered list (``1. foo``, ``步骤 1``, ``Step 2``)
* ``parent``      -- nested numbered list (``1.1 foo``, ``1.2.3 foo``)
* ``contrast``    -- comparison keywords (``对比``, ``区别``, ``vs.``, ``新制度``,
  ``旧制度``, ``新方法``, ``旧方法``, ``反之``, ``不同于``)
* ``link``        -- arrow / flow keywords (``→``, ``->``, ``=>``, ``流程``)
* ``membership``  -- categorisation keywords (``属于``, ``归类``, ``分类``,
  ``category``) plus 3+ list items
* ``overlap``     -- (reserved; not emitted by this heuristic detector)
* ``none``        -- default when no rule matches

Rules fire in priority order -- the first match wins, so more specific patterns
(parent/order) shadow weaker ones (link/contrast). Each emitted atom carries
a ``confidence`` score in ``[0, 1]``.

In addition to the atom, the detector emits a ``suggested_rhythm``:

* ``breathing`` -- 1 or fewer non-empty lines (one thought)
* ``dense``     -- ``len(text) > 800`` or 6+ list items (information-rich)
* ``anchor``    -- the default for ordinary structured content

The detector is pure: no global state, no I/O, every call returns a fresh
``AtomResult`` dict so unit tests can compare values directly.
"""

from __future__ import annotations

import re
from typing import TypedDict


class AtomResult(TypedDict):
    """Detector output.

    Attributes:
        atom: One of ``"order"``, ``"link"``, ``"parent"``, ``"membership"``,
            ``"contrast"``, ``"overlap"``, ``"none"``.
        confidence: Match confidence in ``[0, 1]``.
        suggested_rhythm: One of ``"anchor"``, ``"dense"``, ``"breathing"``.
    """

    atom: str
    confidence: float
    suggested_rhythm: str


# Regex rules. Anchored patterns use re.MULTILINE so ^ matches the start of
# any line in the section, not just the start of the whole string.
_ORDER_RE = re.compile(
    r"(?:^\s*\d+[\.\)、])|(?:步骤\s*\d+)|(?:Step\s*\d+)",
    re.MULTILINE,
)
_NESTED_RE = re.compile(
    r"^\s*\d+\.\d+[\.\)、]",
    re.MULTILINE,
)
_CONTRAST_RE = re.compile(
    r"(?:对比|区别|不同于|反之|vs\.?|V\.S\.|新制度|旧制度|新方法|旧方法)"
)
_ARROW_RE = re.compile(
    r"(?:→|->|=>|流程)"
)
_PARALLEL_RE = re.compile(
    r"(?:属于|归类|分类|category)"
)
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[\.\)、])\s+",
    re.MULTILINE,
)


def _non_empty_line_count(text: str) -> int:
    """Count non-empty, non-whitespace lines in the section."""
    return sum(1 for line in text.splitlines() if line.strip())


def _list_item_count(text: str) -> int:
    """Count markdown list items (ordered or unordered) at line start."""
    return len(_LIST_ITEM_RE.findall(text))


def _suggested_rhythm(section_text: str) -> str:
    """Pick a rhythm hint from the section's shape.

    * ``breathing`` if the section has 1 or fewer non-empty lines.
    * ``dense`` if the section is longer than 800 chars or has 6+ list items.
    * ``anchor`` otherwise.
    """
    if _non_empty_line_count(section_text) <= 1:
        return "breathing"
    if len(section_text) > 800 or _list_item_count(section_text) >= 6:
        return "dense"
    return "anchor"


def detect(section_text: str) -> AtomResult:
    """Detect the Relationships atom that best describes ``section_text``.

    Rules fire in priority order; the first matching rule wins:

    1. ``_ORDER_RE`` matches a numbered list header or ``步骤 N``/``Step N`` ->
       ``atom="order"``, ``confidence=0.9``.
    2. ``_NESTED_RE`` matches a nested numbered list header -> ``atom="parent"``,
       ``confidence=0.85``.
    3. ``_CONTRAST_RE`` matches a comparison keyword -> ``atom="contrast"``,
       ``confidence=0.85``.
    4. ``_ARROW_RE`` matches an arrow or flow keyword -> ``atom="link"``,
       ``confidence=0.7``.
    5. ``_PARALLEL_RE`` matches a categorisation keyword AND the section has
       3+ non-empty lines -> ``atom="membership"``, ``confidence=0.8``.
    6. Otherwise -> ``atom="none"``, ``confidence=0.5``.

    Every return is a fresh dict (no shared mutable state).
    """
    text = section_text or ""

    if _ORDER_RE.search(text):
        atom: str = "order"
        confidence: float = 0.9
    elif _NESTED_RE.search(text):
        atom = "parent"
        confidence = 0.85
    elif _CONTRAST_RE.search(text):
        atom = "contrast"
        confidence = 0.85
    elif _ARROW_RE.search(text):
        atom = "link"
        confidence = 0.7
    elif _PARALLEL_RE.search(text) and _non_empty_line_count(text) >= 3:
        atom = "membership"
        confidence = 0.8
    else:
        atom = "none"
        confidence = 0.5

    return {
        "atom": atom,
        "confidence": confidence,
        "suggested_rhythm": _suggested_rhythm(text),
    }