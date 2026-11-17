"""Chapter names (names; SPEC "Bad downloads", "Publishing").

The library's file-name shapes (parse_name), the helper rules (number parts, ratings, titles, side labels, ComicInfo
dates), a chapter's identity and its published file name. The parse tests are ported from the bulk converter's
tests/test_structure.py.
"""

import json

import pytest

from manga_pipeline import names
from manga_pipeline.clients.suwayomi import valid_filename
from manga_pipeline.names import MAIN, NON_CHAPTER, SIDE, UNNUMBERED, parse_name

from tests.timing import budget

# the bulk converter's structure.non_chapter_names: whole titles after the number
NAMES = ["Notice", "Break Notice", "Hiatus Notice", "Announcement", "Hiatus", "Author's Note"]


def p(stem):
    return parse_name(stem, None, NAMES)


# classification, by parse_name


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "want"),
    [
        (
            "Byleth_Chapter 1 _ 5 Seconds Before a Witch Falls in Love",
            (MAIN, "1", None, "Byleth", "5 Seconds Before a Witch Falls in Love"),
        ),
        ("Easy Going Scans_Vol.1 Ch.17.5", (MAIN, "17.5", "1", "Easy Going Scans", "")),
        ("_dumbcat__Chapter 45_ End of Season 1", (MAIN, "45", None, "dumbcat", "End of Season 1")),
        ("chigusa_Chapter 4 _ Where Is She_", (MAIN, "4", None, "chigusa", "Where Is She")),
        ("Loïc-sama scans_Vol.1 Ch.4", (MAIN, "4", "1", "Loïc-sama scans", "")),
        ("Chapter 107 - Special Test Specimen", (MAIN, "107", None, "", "Special Test Specimen")),
        ("Chap 83", (MAIN, "83", None, "", "")),
        ("Unknown_REM 141.5", (MAIN, "141.5", None, "Unknown", "")),
        ("hua_Episode 56", (MAIN, "56", None, "hua", "")),
    ],
    ids=[
        "byleth-title",
        "vol-decimal",
        "dumbcat-prefix",
        "trailing-underscore",
        "accented-group",
        "dash-title",
        "chap",
        "rem",
        "episode",
    ],
)
def test_filename_shapes_in_the_library(stem, want):
    it = p(stem)
    assert (it.kind, it.number, it.volume, it.group, it.title) == want, "(kind, number, volume, group, title)"


@budget(load="pure")
@pytest.mark.parametrize(
    "stem",
    [
        "mei_Bonus Episode 3 [END]",
        "rishe_Special Episode 1 _ He's Just a Puppy",
        "rishe_Epilogue 4 [END]",
        "Easy Going Scans_Vol.2.5 Ch.56 - [Side Story] Chapter 1",
    ],
    ids=["bonus-episode", "special-episode", "epilogue-end", "side-story-tag"],
)
def test_side_stories_by_label_or_leading_side_tag(stem):
    assert p(stem).kind == SIDE, "a side label before the number, or a side tag opening the title, is a side story"


@budget(load="pure")
def test_main_label_with_a_side_word_in_the_title_stays_main():
    assert p("Byleth_Chapter 4 _ Bonus Chapter").kind == MAIN, "the label decides, not words in the title"


@budget(load="pure")
def test_end_marker_is_not_a_side_tag():
    it = parse_name("Oto_Chapter 163 [Epilogue End] _ Peaceful Days and Afterword", {"Number": "163"}, NAMES)
    assert (it.kind, it.number) == (MAIN, "163"), "`[Epilogue End]` is an end marker on a main chapter"


@budget(load="pure")
@pytest.mark.parametrize(
    "stem",
    ["Notice", "Jujucat_Notice", "rishe_Author's Note", "Chapter 90.5 - Break Notice"],
    ids=["notice", "group-notice", "authors-note", "numbered-break-notice"],
)
def test_non_chapters_by_title_only(stem):
    assert p(stem).kind == NON_CHAPTER, "an allowlisted whole title (after any label / number) is a non-chapter"


@budget(load="pure")
def test_notice_inside_a_real_chapter_title_is_not_a_non_chapter():
    assert p("Chapter 12 - A Notice From the Duke").kind == MAIN, "the allowlist matches the whole title only"


@budget(load="pure")
def test_unnumbered_story_item():
    it = p("rishe_〖Main Story Finale〗 _ A Love To Satisfy Regrets")
    assert (it.kind, it.number) == (UNNUMBERED, None), "no number anywhere is unnumbered"


@budget(load="pure")
def test_number_from_comicinfo_when_filename_has_none():
    assert parse_name("Prologue Special", {"Number": "0"}, NAMES).number == "0", (
        "`Prologue Special` with ComicInfo Number 0 is chapter 0"
    )


@budget(load="pure")
def test_comicinfo_number_numbers_an_unnumbered_name():
    it = parse_name("MangaK_Chapte 58", {"Number": "59"}, NAMES)
    assert (it.kind, it.number, it.title, it.notes) == (MAIN, "59", "Chapte 58", ["number from ComicInfo"]), (
        "numbered by ComicInfo, the name kept as the title"
    )


@budget(load="pure")
def test_side_tag_on_a_name_numbered_by_comicinfo_is_a_side_story():
    assert parse_name("rishe_[Side Story] The Ball", {"Number": "2"}, NAMES).kind == SIDE, (
        "a side tag opening the title marks a side story also when ComicInfo gives the number"
    )


@budget(load="pure")
def test_comicinfo_number_that_is_no_number_is_ignored():
    it = parse_name("rishe_Teaser", {"Number": "Teaser"}, NAMES)
    assert (it.kind, it.number) == (UNNUMBERED, None), "a ComicInfo Number that is not a number numbers nothing"


@budget(load="pure")
@pytest.mark.parametrize("number", ["-1", "1e3", "inf", "nan"], ids=["minus-one", "exponent", "inf", "nan"])
def test_comicinfo_number_is_digits_with_an_optional_decimal(number):
    """Tachiyomi / Suwayomi store -1 for a number they could not parse; float() would take "1e3", "inf", "nan"."""
    it = parse_name("rishe_Teaser", {"Number": number}, NAMES)
    assert (it.kind, it.number) == (UNNUMBERED, None), "not a chapter number"


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "num", "want"),
    [
        ("ComicDom_Vol. 7, Ch. 56", "56", (MAIN, "56", "7", None, "")),
        (
            "KS Group_Vol. 10, Ch. 80_ Slave, Return to Khalitz Kingdom",
            "80",
            (MAIN, "80", "10", None, "Slave, Return to Khalitz Kingdom"),
        ),
        ("Official_S1 - Chapter 1", "1", (MAIN, "1", None, "1", "")),
        ("ryu_S1 Episode 29", "29", (MAIN, "29", None, "1", "")),
        ("_Queen_B__S2 Episode 66", "66", (MAIN, "66", None, "2", "")),
        ("The Hours Between_(S2) Episode 100", "100", (MAIN, "100", None, "2", "")),
        ("FloMio_(S2) Ep. 102 - Revealing Truth (1)", "102", (MAIN, "102", None, "2", "Revealing Truth (1)")),
        ("[Season 2] Chapter 2", "2", (MAIN, "2", None, "2", "")),
        ("Unknown_# 10", "10", (MAIN, "10", None, None, "")),
        ("Official_# 126.5", "126.5", (MAIN, "126.5", None, None, "")),
        ("hua_64. A Conversation with Darkness", "64", (MAIN, "64", None, None, "A Conversation with Darkness")),
        ("Official_Story 1", "1", (MAIN, "1", None, None, "")),
        ("Unknown_Day 1", "1", (MAIN, "1", None, None, "")),
        ("Official_Page 1", "1", (MAIN, "1", None, None, "")),
        ("Unknown_Volume 1", "1", (MAIN, "1", "1", None, "")),
        ("The Hours Between_Spin-off #1", "1", (SIDE, "1", None, None, "")),
        ("MangaK_Chapte 58", "59", (MAIN, "59", None, None, "Chapte 58")),
        (
            "Vikija_Season 1 Finale _ A Priceless Gift",
            "",
            (UNNUMBERED, None, None, None, "Season 1 Finale _ A Priceless Gift"),
        ),
        ("rishe_Teaser", "", (UNNUMBERED, None, None, None, "Teaser")),
    ],
    ids=[
        "vol-comma-ch",
        "vol-comma-ch-title",
        "s1-dash",
        "s1-episode",
        "queen-b-s2",
        "paren-s2",
        "paren-s2-ep-title",
        "bracket-season",
        "hash",
        "hash-decimal",
        "num-dot-title",
        "story",
        "day",
        "page",
        "volume-only",
        "spin-off",
        "comicinfo-numbers-it",
        "season-finale",
        "teaser",
    ],
)
def test_name_shapes_of_the_full_library(stem, num, want):
    it = parse_name(stem, {"Number": num}, NAMES)
    assert (it.kind, it.number, it.volume, it.season, it.title) == want, "(kind, number, volume, season, title)"


@budget(load="pure")
def test_label_word_without_a_number_is_a_title():
    assert p("Story of a Knight").title == "Story of a Knight", "Story / Day / Page / # are labels only before a number"


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "num", "want"),
    [("hua_Episode 15-1", "15.1", "15.1"), ("Chapter 172-5", "172.5", "172.5"), ("Chapter 45-1", "45.2", "45.1")],
    ids=["bokja-moon", "webtoon-character", "filename-wins-over-comicinfo"],
)
def test_hyphen_parts_are_decimal_parts(stem, num, want):
    assert parse_name(stem, {"Number": num}, NAMES).number == want, "a hyphen part is the decimal part"


@budget(load="pure")
def test_a_decimal_with_a_hyphen_part_has_no_number():
    it = p("Vol.2 Ch.045.5-1")
    assert it.number is None, "a decimal with a hyphen part has no single reading"


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "prefix"),
    [
        ("Easy Going Scans_Vol.1 Ch.3.1", "Easy Going Scans_Vol.1 Ch."),
        ("Easy Going Scans_Vol.3 Ch.3.1", "Easy Going Scans_Vol.3 Ch."),
        ("Chapter 10-1", "Chapter "),
        ("Chapter 100-1", "Chapter "),
        ("Ch.045-1", "Ch."),
        ("Level 3 Hero_Ch.3.1", "Level 3 Hero_Ch."),
        ("S1 - Chapter 3.1", "S1 - Chapter "),
        ("  Chapter 3.1 ", "  Chapter "),
        ("64.1. Title 64.1", ""),
        ("The Beginning (1)", None),
    ],
    ids=[
        "vol-ch-dot",
        "vol-equals-chapter",
        "two-digits",
        "three-digits",
        "ch-dot-padded",
        "number-in-group",
        "season-tag",
        "spaces",
        "num-dot-title",
        "no-number",
    ],
)
def test_number_start_is_where_the_parsed_number_starts(stem, prefix):
    start = names.number_start(stem)
    assert (stem[:start] if start is not None else None) == prefix, "the index where parse_name read the number"


# helper rules


@budget(load="pure")
@pytest.mark.parametrize(
    ("number", "want"),
    [("3.10", (3, "10")), ("3.1", (3, "1")), ("3", (3, "")), ("3.0", (3, "")), (" 17.5 ", (17, "5")), (None, None)],
    ids=["3.10", "3.1", "plain", "point-zero", "spaces", "none"],
)
def test_number_key_keeps_parts_apart(number, want):
    assert names.number_key(number) == want, "3.1 and 3.10 are different parts; 3 and 3.0 are one chapter"


@budget(load="pure")
@pytest.mark.parametrize(
    ("n", "want"),
    [
        (1.0, "1"),
        ("01", "1"),
        (17.5, "17.5"),
        ("2.5", "2.5"),
        (None, None),
        (1084.125, "1084.125"),
        ("1084.125", "1084.125"),
        (1e16, "10000000000000000"),
    ],
    ids=[
        "float-whole",
        "zero-padded",
        "decimal",
        "text-decimal",
        "none",
        "seven-digits",
        "text-seven-digits",
        "large-whole",
    ],
)
def test_fmt_num(n, want):
    assert names.fmt_num(n) == want, "numbers as text without trailing .0 or leading zeros, every digit kept"


@budget(load="pure")
def test_item_num_is_the_float_number():
    assert (p("Chapter 17.5").num, p("rishe_Teaser").num) == (17.5, None), "Item.num: a float, None when unnumbered"


@budget(load="pure")
@pytest.mark.parametrize(
    ("title", "want"),
    [
        ("SP2 [R19]", "uncensored"),
        ("[R18]", "uncensored"),
        ("[🔞]", "uncensored"),
        ("[R19 version]", "uncensored"),
        ("SP2 [R15]", "censored"),
        ("(R12)", "censored"),
        ("Reunion [END]", None),
        ("", None),
        (None, None),
    ],
    ids=["r19", "r18", "adult-emoji", "r19-version", "r15", "r12-parens", "untagged", "empty", "none"],
)
def test_rating_tags(title, want):
    assert names.rating(title) == want, "[R19] / [R18] / [🔞] uncensored, [R15] / [R12] censored, untagged None"


@budget(load="pure")
@pytest.mark.parametrize(
    ("title", "want"),
    [
        ("Reunion [END]", "reunion"),
        ("SP2 [R15]", "sp2 rated_r15"),
        ("Chapter 99 [🔞]", "chapter 99 rated_adult"),
        ("Where Is She\u2019s Ring?", "where is she's ring"),
    ],
    ids=["end-marker-dropped", "rating-kept", "adult-kept", "curly-apostrophe"],
)
def test_normalize_title(title, want):
    assert names.normalize_title(title) == want, "lowercase words; bracketed markers dropped, rating tags kept"


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ("", "", True),
        ("", "Reunion", False),
        ("Reunion [END]", "Reunion", True),
        ("SP2 [R15]", "SP2 [R19]", False),
        ("the duke returns home", "the duke returns again", True),
        ("a perfect couple", "a perfect pair", False),
    ],
    ids=["both-untitled", "one-untitled", "end-marker-ignored", "rating-tags-differ", "overlap-60pct", "overlap-50pct"],
)
def test_similar_titles(a, b, want):
    assert names.similar_titles(a, b) is want, "both untitled, equal, or token overlap >= 60%; rating tags count"


@budget(load="pure")
@pytest.mark.parametrize(
    ("ci", "want"),
    [
        ({"Year": "2021", "Month": "Mar"}, "2021-03-01"),
        ({"Year": "2024", "Month": "9", "Day": "23"}, "2024-09-23"),
        ({"Year": "2024", "Month": "September", "Day": ""}, "2024-09-01"),
        ({"Year": "2024", "Month": "", "Day": ""}, "2024-01-01"),
        ({"Year": "", "Month": "9"}, None),
        ({}, None),
        (None, None),
        ({"Year": "2024", "Month": "Sometime"}, "2024-01-01"),
        ({"Year": "2024", "Month": "13", "Day": "0"}, "2024-01-01"),
        ({"Year": "-5", "Month": "3"}, None),
    ],
    ids=[
        "month-name",
        "suwayomi",
        "long-month-name",
        "year-only",
        "no-year",
        "empty",
        "none",
        "unreadable-month",
        "out-of-range-not-clamped",
        "negative-year",
    ],
)
def test_ci_date(ci, want):
    assert names.ci_date(ci) == want, (
        "Month may be a name; a part that is no integer in range counts as absent, never clamped; no Year is None"
    )


@budget(load="pure")
def test_ci_date_parts_names_the_unusable_values():
    assert names.ci_date_parts({"Year": "-5", "Month": "Mar", "Day": "32"}) == (
        {"Year": None, "Month": 3, "Day": None},
        [("Year", "-5"), ("Day", "32")],
    ), "the usable parts as integers, and the values left out"


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "want"),
    [
        ("Emile's Lovers_Special Episode 1", "special episode"),
        ("Spin off 2", "spin-off"),
        ("Easy Going Scans_Vol.2.5 Ch.56 - [Side Story] Chapter 1", "side story"),
        ("Chapter 3", ""),
    ],
    ids=["own-label", "spin-off-spelling", "side-tag", "main"],
)
def test_side_label(stem, want):
    assert names.side_label(p(stem)) == want, "its own label (spin off = spin-off), or its side tag's"


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "want"),
    [
        ("Easy Going Scans_Vol.1 Ch.1", ("Easy Going Scans", "Vol.1 Ch.1")),
        ("_dumbcat__Chapter 45", ("_dumbcat_", "Chapter 45")),
        ("Chapter 45", ("", "Chapter 45")),
    ],
    ids=["group", "underscored-group", "no-group"],
)
def test_split_group(stem, want):
    assert names.split_group(stem) == want, "the scanlator prefix before the first '_' that opens a chapter name"


# identity


def identity(stem, ci_number=""):
    return names.identity(names.parse(stem, ci_number))


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Official_S2 Episode 5", "Official_S1 Episode 5"),
        ("rishe_Side Story 1", "rishe_Chapter 1"),
        ("rishe_Chapter 1", "chigusa_Chapter 1"),
        ("rishe_Chapter 1 _ The Ball", "rishe_Chapter 1 _ The Duel"),
    ],
    ids=["season", "side-label", "scanlator", "title"],
)
def test_identity_tells_chapters_apart(a, b):
    assert identity(a) != identity(b), "season, side label, scanlator and title each tell 2 chapters apart"


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Byleth_Chapter 1 _ Title", "Byleth_Chapter 1 : Title"),
        ("Chapter 01", "Chapter 1"),
        ("rishe_Chapter 2 _ Where Is She_", "Rishe_Chapter 2 _ where is she?"),
    ],
    ids=["valid-filename-colon", "zero-padded", "case-and-punctuation"],
)
def test_identity_agrees_across_spellings(a, b):
    assert identity(a) == identity(b), "':' as '_', '01' as '1', case and punctuation give 1 identity"


@budget(load="pure")
def test_a_number_with_2_readings_keeps_its_digits_in_the_identity():
    assert '"26.10"' in identity("Chapter 26.10"), "as written, not 26.1"
    assert '"26.10"' in identity("Chapter 026-10"), "as written, not 26.1"
    assert identity("Chapter 26.10") != identity("Chapter 26.1"), "never the identity of the shorter decimal"
    assert '"3"' in identity("Chapter 3.0"), "numbers with 1 reading as before"
    assert '"26.5"' in identity("Chapter 26.5"), "numbers with 1 reading as before"
    assert names.two_readings("26.10"), "a decimal part ending in 0"
    assert names.two_readings("17.50"), "a decimal part ending in 0"
    assert [names.two_readings(n) for n in ("26.1", "3.0", "10", "100.5", None)] == [None] * 5


@budget(load="pure")
def test_identity_of_the_valid_filename_form():
    assert identity("Byleth_Chapter 1 _ Title") == identity(valid_filename("Byleth_Chapter 1 : Title")), (
        "a chapter name and its Suwayomi file name have 1 identity"
    )


@budget(load="pure")
def test_identity_is_json_of_its_parts():
    assert json.loads(identity("(S2) Episode 05 - Side Story [END]", "")) == ["", "2", "", "5", "sidestoryend"], (
        "[scanlator key, season, side label, number, title key]"
    )


@budget(load="pure")
def test_identity_takes_the_comicinfo_number():
    assert identity("rishe_Teaser", "3") == identity("rishe_Chapter 3 _ Teaser"), (
        "a name without a number is numbered by its ComicInfo Number"
    )


# file_name and chapter_title: the bulk converter's naming (SPEC "Publishing")


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "ci_title", "volume", "want"),
    [
        ("rishe_Chapter 01", None, None, "Ch.1.cbz"),
        ("rishe_Chapter 01", None, 2, "Vol.2 Ch.1.cbz"),
        ("Chapter 4 - Where Is She_", "Chapter 4 : Where Is She?", None, "Ch.4 - Where Is She_.cbz"),
        ("Chapter 7 - x", 'Side Story 1: A/B "C" <D>|*', 3, "Vol.3 Ch.7 - Side Story 1 - A_B _C_ _D___.cbz"),
        ("Vol.1 Ch.17.5 - Extra", None, 1.5, "Vol.1.5 Ch.17.5 - Extra.cbz"),
        ("Chapter 50 - Dawn (End of Season 1)", None, 1, "Vol.1 Ch.50 - Dawn.cbz"),
        ("Chapter 9 - ...", None, None, "Ch.9.cbz"),
        ("Chapter 2", "Chapter 2", None, "Ch.2.cbz"),
        ("Chapter 2", "Episode 7 Begins", None, "Ch.2 - Episode 7 Begins.cbz"),
    ],
    ids=[
        "no-title",
        "volume",
        "comicinfo-title",
        "illegal-chars",
        "decimals",
        "status-marker",
        "dots-only",
        "bare-label",
        "other-number-label",
    ],
)
def test_file_name(stem, ci_title, volume, want):
    item = names.parse(stem)
    got = names.file_name(item.number, names.chapter_title(item, ci_title), volume)
    assert got == want, "the bulk converter's name"


@budget(load="pure")
def test_a_long_title_is_capped_at_80_characters():
    item = names.parse("Chapter 1")
    got = names.file_name(item.number, "x" * 100)
    assert got == "Ch.1 - " + "x" * 79 + "….cbz"


@budget(load="pure")
@pytest.mark.parametrize(
    ("value", "want"),
    [("2", 2), ("02", 2), ("2.0", 2), ("2.5", 2.5), ("-1", None), ("IV", None), (None, None), ("", None)],
)
def test_to_volume(value, want):
    assert names.to_volume(value) == want


@budget(load="pure")
def test_file_name_needs_a_number():
    with pytest.raises(ValueError, match="needs a chapter number"):
        names.file_name(names.parse("rishe_Teaser").number, "")
