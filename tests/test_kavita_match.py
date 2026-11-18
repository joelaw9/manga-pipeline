"""Tests of clients.kavita_match: `normalize` (Kavita's own rule) and `series_names` (SPEC "Services")."""

import pytest

from manga_pipeline.clients.kavita_match import normalize, series_names

from tests.builders import kavita_series
from tests.timing import budget

# normalize (Kavita v0.9.1.4 StringExtensions.ToNormalized: keep [\p{L}0-9+!\uff0a\uff01\uff0b], then lower-case)


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "Gal Maid & Villainess: Only Milady's Happy End Will Win!",
            "Gal Maid  Villainess: Only Miladys Happy End Will Win!",
        ),
        ("Swordmaster\u2019s Youngest Son", "Swordmaster's Youngest Son"),
        ("SOLO LEVELING", "solo leveling"),
        ("Cafe\u0301", "Cafe"),
    ],
    ids=["punctuation-spaces", "curly-apostrophe", "case", "combining-mark-dropped"],
)
def test_normalize_same_key(a, b):
    """Names differing only in spaces, punctuation, case or a combining mark get the same key.

    Kavita drops all of them. The punctuation: straight / curly apostrophes, &, :.
    """
    assert normalize(a) == normalize(b)


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Master Villainess the Invincible!", "Master Villainess the Invincible"),  # sample library
        ("Heavenly Demon Reborn!", "Heavenly Demon Reborn\uff01"),  # ASCII vs fullwidth "!"
        (
            "\uff33\uff4f\uff4c\uff4f \uff2c\uff45\uff56\uff45\uff4c\uff49\uff4e\uff47",
            "Solo Leveling",
        ),  # full-width; no NFKC folding
        ("Caf\u00e9", "Cafe\u0301"),  # precomposed letter kept, mark dropped
        ("Kill Me Now \uff12", "Kill Me Now 2"),
    ],  # only ASCII digits kept
    ids=[
        "exclamation-kept",
        "fullwidth-exclamation",
        "fullwidth-letters",
        "precomposed-vs-decomposed",
        "non-ascii-digit",
    ],
)
def test_normalize_different_key(a, b):
    """Kavita keeps "!", fullwidth forms as they are, and precomposed letters.

    "!" as in I Failed to Oust the Villain! and Trash Belongs in the Trash Can!. A ComicInfo Series differing there
    parses to another Kavita series.
    """
    assert normalize(a) != normalize(b)


@budget(load="pure")
@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("Kill Me Now 2", "killmenow2"),
        ("Trash Belongs in the Trash Can!", "trashbelongsinthetrashcan!"),
        ("A+B\uff0bC\uff0aD\uff01", "a+b\uff0bc\uff0ad\uff01"),
        ("Straße", "straße"),
        (None, ""),
        ("", ""),
        ("İstanbul", "istanbul"),
        ("ΟΔΟΣ", "οδοσ"),
        ("A\U0001d400B", "ab"),
    ],
    ids=[
        "name",
        "exclamation",
        "plus-and-fullwidth",
        "sharp-s",
        "none",
        "empty",
        "dotted-capital-i",
        "greek-sigma-per-char",
        "astral-letter-dropped",
    ],
)
def test_normalize_value(name, want):
    r"""The key is letters, ASCII digits, + ! and full-width * ! +, lower-cased (not case-folded: ß stays).

    A missing name is the empty key. Like .NET, each character is lowered on its own ("İ" gives "i", a final capital
    sigma U+03C3, never the context-sensitive "ς"), and a letter above U+FFFF (a UTF-16 surrogate pair, not \p{L} to
    .NET) is dropped.
    """
    assert normalize(name) == want


# series_names


@budget(load="pure")
def test_series_names_in_key_order_without_blanks():
    """name, localizedName, originalName in that order; empty and missing names are left out."""
    assert series_names({**kavita_series(1, "Liz"), "localizedName": "", "originalName": "Rize"}) == ["Liz", "Rize"]
    assert series_names({**kavita_series(1, "Liz"), "localizedName": None, "originalName": None}) == ["Liz"]


@budget(load="pure")
def test_series_names_of_no_series_is_empty():
    """A missing DTO has no names."""
    assert series_names(None) == []
