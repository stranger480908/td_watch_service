"""
Phonetic blocking keys.

Blocking is candidate generation: cheaply narrowing millions of possible pairs
to a set small enough to score properly. Two independent keys, OR'd:

  trigram   pg_trgm similarity on the mark text. Good at typos, insertions,
            and shared substrings.
  phonetic  Double Metaphone. Good at spellings that diverge on the page but
            converge in the ear.

Neither is sufficient alone, which is the whole reason both exist:

  NOOVANA / NUVANA   trigram 0.36 (caught)   dmetaphone NFN = NFN (caught)
  SOLEIL  / SOLAY    trigram 0.22 (missed)   dmetaphone SLL vs SL (missed)

The phonetic key is also an equality join rather than a similarity scan, so it
is far cheaper per candidate at gazette-day volume.

Marks are multi-word, so we key per token and match on ANY shared token code.
"NUVANA LABS" and "NUVANA" must block together.
"""

from __future__ import annotations

import re

from metaphone import doublemetaphone

TOKEN_RE = re.compile(r"[A-Za-z0-9']+")

# Tokens that carry no distinguishing weight. Blocking on these would pull in
# thousands of unrelated marks per gazette day.
STOPWORDS = frozenset({
    "THE", "AND", "OF", "A", "AN", "FOR", "BY", "WITH", "INC", "LLC", "LTD",
    "CORP", "CO", "COMPANY", "GROUP", "BRANDS", "INTERNATIONAL",
})

MIN_TOKEN_LEN = 2


def tokens(text: str | None) -> list[str]:
    if not text:
        return []
    out = []
    for t in TOKEN_RE.findall(text.upper()):
        if len(t) >= MIN_TOKEN_LEN and t not in STOPWORDS and not t.isdigit():
            out.append(t)
    return out


def codes(text: str | None) -> list[str]:
    """
    Distinct Double Metaphone codes for every meaningful token.

    Both the primary and alternate code are kept. The alternate exists for
    names with more than one plausible pronunciation, which is precisely the
    case that matters for trademarks.
    """
    seen: list[str] = []
    for tok in tokens(text):
        for c in doublemetaphone(tok):
            if c and c not in seen:
                seen.append(c)
    return seen


def blocking_rows(serial_number: str, sources: dict[str, list[str]]) -> list[tuple]:
    """
    (serial_number, code, source) rows for the mark_phonetic table.

    sources maps a label to the strings it contributes, e.g.
    {"mark": ["NUVAHNA"], "pseudo_mark": ["NUVANA"], "translation": [...]}.
    Pseudo marks matter here: USPTO already computed the alternate spelling,
    so indexing its phonetic code costs nothing and widens recall for free.
    """
    rows, seen = [], set()
    for source, texts in sources.items():
        for text in texts:
            for c in codes(text):
                if (c, source) not in seen:
                    seen.add((c, source))
                    rows.append((serial_number, c, source))
    return rows
