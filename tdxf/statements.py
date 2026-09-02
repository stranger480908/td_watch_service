"""
case-file-statement type-code handling.

Codes are six characters. Most are a 2-char category plus 4 positional chars;
TLIT and TNSF are 4-char categories plus 2. Getting this wrong silently drops
whole scoring factors, so it lives in one place with its own tests.

Reference: USPTO Trademark Applications Daily XML DTD element documentation,
CASE FILE STATEMENTS SECTION, notes 1-8.
"""

from __future__ import annotations

import re

# Verified against real data: transliteration is TL0000, a two-char prefix.
# The four-char TLIT form documented for DTD v2.0 does not appear in v2.3.
FOUR_CHAR_KINDS = ()

GOODS_SERVICES = "GS"
PSEUDO_MARK = "PM"
TRANSLATION = "TR"
TRANSLITERATION = "TL"
MARK_DESCRIPTION = "DM"
MARK_OVERFLOW = "MK"
DISCLAIMER = "D0"
DISCLAIMER_PREDEFINED = "D1"
COLORS_CLAIMED = "CC"
COLOR_DESCRIPTION = "CD"
LINING_STIPPLING = "LS"

# GS position 6. Documented as "less goods" handling, not a sequence number.
GS_NORMAL = "1"          # goods text, no less-goods content
GS_ONLY_LESS_GOODS = "2" # statement is ENTIRELY goods that were removed
GS_EMBEDDED_LESS = "3"   # removed goods embedded, wrapped in double parens

# Amendment markers USPTO embeds in goods text after registration.
LESS_GOODS_RE = re.compile(r"\(\(.*?\)\)", re.S)   # ((removed))
DELETED_RE = re.compile(r"\[.*?\]", re.S)          # [deleted]
NEW_WORDING_RE = re.compile(r"\*(.*?)\*", re.S)    # *added* -> added
WS_RE = re.compile(r"\s{2,}")


def kind(type_code: str | None) -> str | None:
    """Category prefix. TLIT/TNSF are four characters, everything else two."""
    if not type_code:
        return None
    if type_code[:4] in FOUR_CHAR_KINDS:
        return type_code[:4]
    return type_code[:2]


def gs_class(type_code: str | None) -> str | None:
    """Prime class from a GS code: positions 3-5. GS0351 -> '035'."""
    if not type_code or kind(type_code) != GOODS_SERVICES or len(type_code) < 5:
        return None
    c = type_code[2:5]
    return c if c.isdigit() or c.strip().isalpha() else None


def gs_flag(type_code: str | None) -> str | None:
    """Position 6 of a GS code."""
    if not type_code or kind(type_code) != GOODS_SERVICES or len(type_code) < 6:
        return None
    return type_code[5]


def is_live_goods(type_code: str | None) -> bool:
    """
    False when the statement is nothing but goods that were removed.

    Scoring against deleted goods produces conflicts on coverage the registrant
    gave up, which is exactly the false positive that gets you unsubscribed.
    """
    return gs_flag(type_code) != GS_ONLY_LESS_GOODS


def clean_goods_text(text: str | None, type_code: str | None = None) -> str:
    """
    Strip amendment markup, keeping only goods currently covered.

    ((...)) and [...] are removed outright. *...* marks wording that was ADDED,
    so the asterisks go but the words stay.
    """
    if not text:
        return ""
    if not is_live_goods(type_code):
        return ""
    out = LESS_GOODS_RE.sub(" ", text)
    out = DELETED_RE.sub(" ", out)
    out = NEW_WORDING_RE.sub(r"\1", out)
    return WS_RE.sub(" ", out).strip()


def join_text_chunks(chunks: list[str]) -> str:
    """
    Join repeated <text> elements.

    The field is documented as 40 positions with overflow into further <text>
    elements. When the leading chunks are all exactly 40 characters the split is
    fixed-width and a space would be inserted mid-word, so concatenate instead.
    """
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]
    if all(len(c) == 40 for c in chunks[:-1]):
        return "".join(chunks)
    return " ".join(chunks)
