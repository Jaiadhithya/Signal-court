"""Quote-gate normaliser. Applied identically to page text and quotes.

Numbers are never altered: Indian grouping stays, and hyphens next to digits are untouched
because a minus sign changes a number.
"""

from __future__ import annotations

import re
import unicodedata

import ftfy

_QUOTES = {ord(c): "'" for c in "‘’‚‛"}
_QUOTES.update({ord(c): '"' for c in "“”„"})
_DASHES = {ord(c): "-" for c in "‐‑‒–—―−"}
_INVISIBLE = {ord(c): None for c in "­​‌‍⁠﻿"}
_TRANS = {**_QUOTES, **_DASHES, **_INVISIBLE}

_LINE_HYPHEN = re.compile(r"([A-Za-z])-[ \t]*\r?\n[ \t]*([A-Za-z])")
_LETTER_HYPHEN = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")
_RUPEE = re.compile(r"(?:₹|\bRs\.?|\bINR\b|`)\s*(?=\d)", re.IGNORECASE)
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = ftfy.fix_text(t)
    t = t.translate(_TRANS)
    t = _LINE_HYPHEN.sub(r"\1\2", t)
    t = _LETTER_HYPHEN.sub("", t)
    t = _RUPEE.sub("₹", t)
    t = _WS.sub(" ", t).strip()
    return t.casefold()
