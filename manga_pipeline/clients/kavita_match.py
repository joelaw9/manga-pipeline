"""Kavita's own series-name normalising and the names a Kavita series DTO matches on (SPEC "Services").

Copied from the bulk converter (optimizer/kavita_match.py).
"""

import unicodedata

from manga_pipeline.clients.kavita import Series

SERIES_NAME_KEYS = ("name", "localizedName", "originalName")  # the names a Kavita series DTO matches on
NORMALIZE_KEEP = frozenset("0123456789+!\uff0a\uff01\uff0b")  # kept by normalize besides letters (Kavita's class)
BMP_LAST = 0xFFFF  # the last code point of the Basic Multilingual Plane (1 C# char)


def series_names(series: Series | None) -> list[str]:
    """Return a Kavita series DTO's non-empty names (SERIES_NAME_KEYS order)."""
    if series is None:
        return []
    return [name for k in SERIES_NAME_KEYS if (name := series.get(k))]


def normalize(name: str | None) -> str:
    r"""Return Kavita's series key of a name, exactly.

    Kavita v0.9.1.4 Kavita.Common/Extensions/StringExtensions.cs `ToNormalized`, which Parser.Normalize calls: keep
    Unicode letters (category L*), ASCII digits, + ! and full-width * ! +, drop everything else (spaces, punctuation,
    combining marks, other digits), then lower-case. No Unicode folding (NFKC, casefold). As .NET does: a letter above
    U+FFFF is a UTF-16 surrogate pair, not \p{L}, so it is dropped; each character is lowered on its own (never the
    whole string: Python lowers a final capital sigma to "ς" by context), and what lowering adds is filtered again ("İ"
    lowers to "i" + a combining dot: "i").
    """
    return "".join(low for c in name or "" if _kept(c) for low in c.lower() if _kept(low))


def _kept(c: str) -> bool:
    """Return whether Kavita's normalize keeps c: a BMP letter, an ASCII digit, ASCII + ! and full-width * ! +."""
    return c in NORMALIZE_KEEP or (ord(c) <= BMP_LAST and unicodedata.category(c).startswith("L"))
