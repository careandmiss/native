"""text_width.py — per-character width estimator, forked from ppt-master.

The functions in this file are a minimal copy of ppt-master's
``svg_to_pptx/drawingml/utils.py:3537-3595`` (the ``_estimate_character_width``
+ ``estimate_text_width`` core) with three intentional simplifications:

  1. **No grapheme clusters**: ppt-master's full estimator groups
     combining marks, ZWJ, regional-indicator pairs into clusters for
     emoji / virama accuracy. We skip that — our use case (max_chars
     fitting per shape) is single-line CJK / Latin / mixed and the
     extra complexity is not worth it.
  2. **No ``is_serif_run`` headroom table**: ppt-master's
     ``_TEXT_WIDTH_HEADROOM_BASE = 1.06`` / ``_TEXT_WIDTH_HEADROOM_CAPS =
     1.12`` per-family constants are calibrated against LibreOffice
     renders. We expose a single ``HEADROOM = 1.10`` constant callers
     can override.
  3. **No ``estimate_single_line_text_frame_width`` helper**: we only
     need the no-run-dict version. Callers wrap into a fake ``[run]``
     if they want the headroom path.

What this file **does** keep:
  - CJK character width ≈ ``font_size`` (matches the value used by
    ppt-master's quality checker — verified empirically: 13 CJK at
    47.49pt = 704.4 px matches the round-trip ``svg_quality_checker.py
    --roundtrip`` reported 704 px).
  - Latin digit width 0.55 em, space 0.30 em, m/W 0.75 em, i/l 0.30 em.
  - Bold latin +5 % bump.

Calibration caveat: ppt-master's exact numbers were tuned against
Calibri / 思源黑体 CN stacks. ``微软雅黑`` (used in the boteng cover
title) renders ~3 % wider than 思源黑体 CN at the same point size;
``宋体`` renders ~10 % narrower. The HEADROOM constant absorbs most
of this; if a particular template consistently overflows, bump
HEADROOM per-template via ``text_width.HEADROOM = 1.15`` after
``template_inspect`` runs.

Stdlib-only — no ppt-master import at runtime.
"""

from __future__ import annotations

import unicodedata


HEADROOM = 1.06  # Per-run safety multiplier (matches ppt-master sans default)


# Textbox padding per side, in pixels. ppt-master adds this inside
# `drawingml_text_frame_width_emu` to cover the small slack that
# renderers (LibreOffice in particular) add before measuring width.
_TEXTBOX_PADDING_MIN_PX = 0.5
_TEXTBOX_PADDING_MAX_PX = 2.0
_TEXTBOX_PADDING_RATIO = 0.04


def _textbox_padding_px(font_size: float) -> float:
    """Per-side slack, matching ppt-master ``_textbox_padding``."""
    return max(
        _TEXTBOX_PADDING_MIN_PX,
        min(_TEXTBOX_PADDING_MAX_PX, font_size * _TEXTBOX_PADDING_RATIO),
    )


def is_cjk_char(ch: str) -> bool:
    """Return whether one character uses the CJK East Asian width model.

    Forked from ppt-master ``is_cjk_char``. CJK glyphs render ~1em wide
    in CJK-aware fonts; Latin glyphs render at varied widths.
    """
    cp = ord(ch)
    return (
        _is_han_char(ch)
        or _is_hiragana_char(ch)
        or _is_katakana_char(ch)
        or _is_hangul_char(ch)
        or 0x2E80 <= cp <= 0x2FFF
        or 0x3000 <= cp <= 0x303F
        or 0x3100 <= cp <= 0x312F
        or 0x31A0 <= cp <= 0x31BF
        or 0x31C0 <= cp <= 0x31EF
        or 0xFF00 <= cp <= 0xFFEF
    )


def _is_han_char(ch: str) -> bool:
    return 0x4E00 <= ord(ch) <= 0x9FFF


def _is_hiragana_char(ch: str) -> bool:
    return 0x3040 <= ord(ch) <= 0x309F


def _is_katakana_char(ch: str) -> bool:
    return 0x30A0 <= ord(ch) <= 0x30FF


def _is_hangul_char(ch: str) -> bool:
    cp = ord(ch)
    return (0xAC00 <= cp <= 0xD7A3) or (0x1100 <= cp <= 0x11FF) or (0x3130 <= cp <= 0x318F)


def _estimate_character_width(ch: str, font_size: float) -> float:
    """Per-character width in SVG pixels.

    Forked from ppt-master ``_estimate_character_width``. Half-width
    CJK punctuation (``0xFF00-0xFFEF`` + ``unicodedata.east_asian_width
    == 'H'``) renders 0.5 em; full-width CJK renders 1.0 em.
    """
    if (
        0xFF00 <= ord(ch) <= 0xFFEF
        and unicodedata.east_asian_width(ch) == 'H'
    ):
        return font_size * 0.5
    if is_cjk_char(ch):
        return font_size
    if ch == ' ':
        return font_size * 0.3
    if ch in 'mMwWOQ%':
        return font_size * 0.75
    if ch in 'iIlj!|':
        return font_size * 0.3
    if ch.isdigit():
        return font_size * 0.55
    return font_size * 0.55


def estimate_text_width(text: str, font_size: float,
                         font_weight: str = '400',
                         headroom: float | None = None) -> float:
    """Estimate the rendered width of a single line of text in SVG px.

    ``headroom`` defaults to :data:`HEADROOM` (1.10). Pass 1.0 to
    disable. Bold latin (font_weight in ``{'bold', '600', '700',
    '800', '900'}``) gets a 5 % bump on each latin grapheme (CJK
    widths stay fixed because CJK fonts already render bold
    glyphs the same width).
    """
    if not text:
        return 0.0
    bold = font_weight in ('bold', '600', '700', '800', '900')
    h = HEADROOM if headroom is None else headroom
    total = 0.0
    for ch in text:
        w = _estimate_character_width(ch, font_size)
        if bold and not is_cjk_char(ch):
            w *= 1.05
        total += w
    # ppt-master's measure_text also adds per-side textbox padding
    # (typically 0.5-2 px each side). Without it our estimate is ~5 %
    # below ppt-master's; with it we're within 1 px of the
    # quality_checker reported rendered width.
    total += _textbox_padding_px(font_size) * 2
    return total * h


# Binary-search sample texts. The shape's actual rendered language is
# unknown up front, so we measure both worst cases and pick the
# tighter cap. A shape that fits 8 CJK chars at 47pt may still fit 14
# Latin chars at the same font size; we use the more restrictive cap.
_SAMPLE_CJK = "一二三四五六七八九十" * 4  # 40 chars, all 1.0em
_SAMPLE_LATIN = ("Lorem ipsum dolor sit amet, consectetur adipiscing "
                "elit, sed do eiusmod tempor incididunt ut labore et "
                "dolore magna aliqua.")  # ~120 chars, mix of widths


def chars_that_fit(frame_width: float, font_size: float,
                   font_weight: str = '400',
                   sample_cjk: str | None = None,
                   sample_latin: str | None = None,
                   headroom: float | None = None) -> int:
    """Return the maximum number of characters that fit in ``frame_width``.

    Binary-searches two sample texts (CJK-dominant and Latin-dominant)
    and returns ``min(cjk_cap, latin_cap)`` — the cap that fits BOTH
    language extremes, so neither style overflows.

    Pass ``sample_cjk`` / ``sample_latin`` to override the sample
    (useful when you know the shape holds English titles, etc.).
    """
    cjk_sample = sample_cjk if sample_cjk is not None else _SAMPLE_CJK
    lat_sample = sample_latin if sample_latin is not None else _SAMPLE_LATIN
    cjk_cap = _binary_search(cjk_sample, frame_width, font_size,
                              font_weight, headroom)
    lat_cap = _binary_search(lat_sample, frame_width, font_size,
                              font_weight, headroom)
    return min(cjk_cap, lat_cap)


def _binary_search(sample: str, target_width: float, font_size: float,
                   font_weight: str, headroom: float | None) -> int:
    """Find the longest prefix of ``sample`` whose width ≤ ``target_width``."""
    if not sample:
        return 0
    lo, hi = 0, len(sample)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        w = estimate_text_width(sample[:mid], font_size, font_weight,
                                 headroom=headroom)
        if w <= target_width:
            lo = mid
        else:
            hi = mid - 1
    return lo
