"""A new chapter's name next to its series' published chapters (metadata/naming; SPEC "Publishing").

The bulk converter's settled path per rule, its MangaDex aggregate reading, and real bulk records resolved back.
"""

import dataclasses
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from manga_pipeline import names, volumes
from manga_pipeline.metadata import naming
from manga_pipeline.metadata.naming import Row, VolumeError, mangadex_volumes, resolve
from manga_pipeline.names import MAIN, SIDE

from tests.timing import budget

RECORDS = Path(__file__).with_name("fixtures_api") / "records"
OVERRIDE_1_2: volumes.Overrides = {"volumes": {1: "1-2", 2: "3-"}}


def settled(number, volume, source, marker="", *, season=None) -> Row[tuple[str, float, str]]:
    """Return a settled main chapter."""
    return Row(
        key=("p", number, MAIN),
        settled=True,
        kind=MAIN,
        number=float(number),
        season=season,
        src_volume=None,
        volume=volume,
        volume_source=source,
        marker_title=marker,
        title="",
        date=None,
        order=0,
    )


def settled_side(number, volume) -> Row[tuple[str, float, str]]:
    """Return a settled side story, its volume from the side-story rule."""
    return dataclasses.replace(settled(number, volume, "side"), key=("p", number, SIDE), kind=SIDE)


def new(number, src_volume=None, season=None, marker="") -> Row[tuple[str, float, str]]:
    """Return an open main chapter."""
    return Row(
        key=("c", number, MAIN),
        settled=False,
        kind=MAIN,
        number=float(number),
        season=season,
        src_volume=src_volume,
        volume=None,
        volume_source=None,
        marker_title=marker,
        title="",
        date=None,
        order=0,
    )


def new_side(number, *, src_volume=None, title="", date=None, order=0) -> Row[tuple[str, float, str]]:
    """Return an open side story."""
    return dataclasses.replace(
        new(number, src_volume), key=("c", number, SIDE), kind=SIDE, title=title, date=date, order=order
    )


def named(basis, rows, overrides=None, mangadex=None):
    """{(kind, number): Naming} of the open rows."""
    return {(k[2], k[1]): v for k, v in resolve(basis, rows, overrides, mangadex or {}).items()}


def volume_of(basis, rows, number, overrides=None, mangadex=None):
    got = named(basis, rows, overrides, mangadex)[(MAIN, number)]
    return got.volume, got.volume_source


# rule 1: a main volume from the recorded basis only


@budget(load="pure")
@pytest.mark.parametrize(
    ("basis", "row", "overrides", "mangadex", "want"),
    [
        ("filename", new(3, src_volume="2"), None, None, (2, "filename")),
        ("filename", new(3), None, None, (None, "none")),
        ("season tags", new(3, season=2), None, None, (2, "season tags")),
        ("season tags", new(3), None, None, (None, "none")),
        ("mangadex", new(3), None, {"3": "2"}, (2, "mangadex")),
        ("mangadex", new(3), None, {"3": None}, (None, "none")),
        ("override", new(3), OVERRIDE_1_2, None, (2, "override")),
        ("side stories need volumes: main story = Vol 1", new(3), None, None, (1, "default")),
        ("none", new(3, src_volume="2"), OVERRIDE_1_2, {"3": "2"}, (2, "override")),
        ("tapas seasons", new(3, src_volume="2"), None, {"3": "2"}, (None, "none")),
        ("webtoons seasons", new(3, season=2), None, None, (None, "none")),
        (None, new(3, src_volume="2"), None, None, (None, "none")),
    ],
    ids=[
        "filename",
        "filename-none",
        "season-tags",
        "season-tags-none",
        "mangadex",
        "mangadex-none",
        "override",
        "side-default",
        "none",
        "tapas-deferred",
        "webtoons-deferred",
        "no-basis",
    ],
)
def test_a_main_volume_comes_from_the_recorded_basis_only(basis, row, overrides, mangadex, want):
    rows = [settled(1, None, "none"), row]
    assert volume_of(basis, rows, 3, overrides, mangadex) == want, "the basis alone names a new main volume"


@budget(load="pure")
@pytest.mark.parametrize(
    ("markers", "want"),
    [
        ({2: "Chapter 2 (Season 1 Finale)"}, {3: 2, 4: 2}),
        ({2: "Chapter 2 [End of Season 1]", 3: "Chapter 3 S2 END"}, {3: 2, 4: 3}),
        ({3: "Chapter 3 (Season 2 Premiere)"}, {3: 2, 4: 2}),
        ({1: "Chapter 1 (Season Finale)"}, {3: None, 4: None}),
        ({1: "Chapter 1 (Season 1 Finale)", 2: "Chapter 2 (Season 3 Finale)"}, {3: 3, 4: 3}),
    ],
    ids=["closing-marker", "2-ends", "start-marker-only", "no-season-number", "skipped-season"],
)
def test_season_markers_number_volumes_in_season_order(markers, want):
    rows = [settled(n, None, "season markers", marker=markers.get(n, f"Chapter {n}")) for n in (1, 2)]
    rows += [new(n, marker=markers.get(n, f"Chapter {n}")) for n in (3, 4)]
    got = {n: volume_of("season markers", rows, n)[0] for n in (3, 4)}
    assert got == want, "season ends close volumes 1, 2, 3...; a start marker closes the season before it"


@budget(load="pure")
def test_a_season_tag_starts_its_season_at_its_lowest_chapter():
    rows = [settled(1, 1, "season markers"), settled(2, 1, "season markers"), new(3, season=2), new(4, season=2)]
    assert [volume_of("season markers", rows, n)[0] for n in (3, 4)] == [2, 2], "a season tag is a start marker"


@budget(load="pure")
def test_a_season_end_and_the_next_chapter_resolve_together():
    rows = [settled(1, 1, "season markers"), settled(2, 1, "season markers")]
    rows += [new(3, marker="Chapter 3 (Season 1 Finale)"), new(4, marker="Chapter 4")]
    got = named("season markers", rows)
    assert (got[(MAIN, 3)].volume, got[(MAIN, 4)].volume) == (1, 2), "the end marker closes volume 1 at chapter 3"


@budget(load="pure")
@pytest.mark.parametrize(
    ("new_rows", "want"),
    [
        ([new(5)], {5: 2}),
        ([new(5, season=3)], {5: 3}),
        ([new(5, marker="Chapter 5 (Season 2 Finale)"), new(6)], {5: 2, 6: 3}),
    ],
    ids=["mid-season", "next-season-tag", "end-marker-closes-the-open-volume"],
)
def test_recorded_season_volumes_are_season_ends_when_their_markers_are_gone(new_rows, want):
    """User decision 2026-11-15: markers only a download's ComicInfo Title held are not in the record.

    Each recorded volume but the highest ends at its highest chapter, and the highest stays open until a marker closes
    it.
    """
    rows = [settled(n, v, "season markers") for n, v in ((1, 1), (2, 1), (3, 2), (4, 2))] + new_rows
    got = {n: volume_of("season markers", rows, n)[0] for n in want}
    assert got == want


@budget(load="pure")
def test_a_season_tag_after_a_recorded_closed_volume_invents_no_second_end():
    """Review 7c-3: volume 1 recorded closed at 50, 51 recorded in volume 2.

    A season-2 tag at 52 keeps volume 2, since season 1's end is known.
    """
    rows = [settled(n, 1, "season markers") for n in (49, 50)] + [settled(51, 2, "season markers")]
    rows += [new(52, season=2)]
    assert volume_of("season markers", rows, 52)[0] == 2


# rule 2: gap fill (filename and MangaDex bases only)


@budget(load="pure")
def test_a_none_series_with_an_override_file_takes_its_volumes():
    """User 2026-11-15: a `none` series' override file gives a new chapter the override's volume.

    A `none` series whose override file re-volumed its recorded chapters gives a new chapter the override's volume, in
    order with its neighbours; MangaDex never fills it.
    """
    rows = [settled(1, 1, "override"), settled(2, 2, "override"), new(3), new(9)]
    overrides = {"volumes": {1: "1", 2: "2-5"}}
    got = named("none", rows, overrides, {"9": "4"})
    assert (got[(MAIN, 3)].volume, got[(MAIN, 3)].volume_source) == (2, "override")
    assert got[(MAIN, 9)].volume is None, "in no range: no volume; MangaDex is no filler for `none`"


@budget(load="pure")
@pytest.mark.parametrize(
    ("basis", "rows", "overrides", "mangadex", "want"),
    [
        (
            "filename",
            [settled(1, 1, "filename"), settled(2, 1, "filename"), new(3)],
            {"volumes": {1: "1-3"}},
            None,
            (1, "override"),
        ),
        ("filename", [settled(1, 1, "filename"), new(2)], None, {"1": "1", "2": "1"}, (1, "mangadex")),
        ("filename", [settled(5, 2, "filename"), new(6)], None, {"6": "1"}, (None, "none")),
        (
            "filename",
            [settled(1, 1, "filename"), new(2)],
            {"volumes": {2: "2"}},
            {"1": "1", "2": "1"},
            (2, "override"),
        ),
        ("mangadex", [settled(1, 1, "mangadex"), new(2)], {"volumes": {1: "1-2"}}, {"1": "1"}, (1, "override")),
    ],
    ids=[
        "filename-override-fills",
        "filename-mangadex-fills",
        "filename-fill-breaks-order",
        "override-before-mangadex",
        "mangadex-override-fills",
    ],
)
def test_a_gap_fill_fills_only_what_agrees_and_keeps_order(basis, rows, overrides, mangadex, want):
    (got,) = named(basis, rows, overrides, mangadex).values()  # each case's 1 open chapter
    assert (got.volume, got.volume_source) == want, (
        "a filler fills nothing when it clashes with the basis, and no fill breaks the volume order"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("basis", "rows", "overrides", "mangadex", "want"),
    [
        (
            "filename",
            [settled(1, 1, "filename"), settled(2, 1, "filename"), new(3)],
            {"volumes": {2: "1-3"}},
            None,
            (2, "override"),
        ),
        ("mangadex", [settled(1, 1, "mangadex"), new(3)], {"volumes": {2: "1-3"}}, {"1": "1"}, (2, "override")),
        ("mangadex", [settled(1, 1, "mangadex"), new(3)], {"volumes": {3: "3"}}, {"1": "1", "3": "2"}, (3, "override")),
        (
            "season tags",
            [settled(1, 1, "season tags", season=1), new(3, season=1)],
            {"volumes": {2: "1-3"}},
            None,
            (1, "season tags"),
        ),
    ],
    ids=["filename-clash", "mangadex-clash", "beats-mangadex", "season-chapter-ignores-ranges"],
)
def test_an_override_range_gives_a_new_chapter_its_volume_whatever_the_basis(basis, rows, overrides, mangadex, want):
    assert volume_of(basis, rows, 3, overrides, mangadex) == want, (
        'the override file wins over every source for a chapter carrying no season (SPEC "Publishing")'
    )


@budget(load="pure")
def test_a_filler_out_of_order_among_its_own_fills_is_not_used():
    rows = [settled(1, 1, "filename"), new(2), new(3)]
    got = named("filename", rows, None, {"2": "3", "3": "2"})
    assert [got[(MAIN, n)].volume for n in (2, 3)] == [None, None], "fills out of order among themselves: none"


# rule 3: whole numbers; the override file


@budget(load="pure")
@pytest.mark.parametrize(
    ("basis", "row", "mangadex"),
    [("mangadex", new(3), {"3": "2.5"}), ("filename", new(3, src_volume="1.5"), None)],
    ids=["mangadex", "filename"],
)
def test_a_volume_that_is_not_whole_is_a_volume_error(basis, row, mangadex):
    with pytest.raises(VolumeError, match=r"chapter 3: volume (2\.5|1\.5)"):
        resolve(basis, [settled(1, 1, basis), row], None, mangadex or {})


@budget(load="pure")
def test_an_override_basis_without_its_file_is_a_volume_error():
    with pytest.raises(VolumeError, match="override file"):
        resolve("override", [settled(1, 1, "override"), new(2)], None, {})


@budget(load="pure")
def test_a_missing_override_file_on_another_basis_is_no_override():
    rows = [settled(1, 1, "mangadex"), new(2)]
    assert volume_of("mangadex", rows, 2, None, {"2": "2"}) == (2, "mangadex"), "no override file: no override"


# rule 4: side stories


@budget(load="pure")
def test_side_stories_arriving_together_number_after_the_settled_ones():
    rows = [settled(1, 1, "override"), settled(2, 2, "override"), settled_side(1, 3)]
    rows += [new_side(10, order=1), new_side(2, order=2)]  # "Extra 10", "Extra 2": no title
    got = resolve("override", rows, OVERRIDE_1_2, {})
    side = {k[1]: (v.volume, v.number, v.title, v.volume_source) for k, v in got.items()}
    assert side == {2: (3, 2.0, "Side Story 2", "side"), 10: (3, 3.0, "Side Story 3", "side")}, (
        "new side stories take the settled side volume and number on, in number order (2 before 10)"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("main", "want"),
    [([settled(1, 4, "override")], 5), ([settled(1, None, "none")], 2), ([], 2)],
    ids=["after-main", "no-main-volume", "no-main"],
)
def test_a_first_side_story_goes_in_the_volume_after_the_main_story(main, want):
    got = named("none", [*main, new_side(1)])[(SIDE, 1)]
    assert (got.volume, got.number, got.title) == (want, 1.0, "Side Story 1"), "highest main volume + 1, else 2"


@budget(load="pure")
@pytest.mark.parametrize(
    ("title", "want"),
    [
        ("[Side Story]", "Side Story 1"),
        ("[Side Story 3] Lunch", "Side Story 1: Lunch"),
        ("Chapter 3", "Side Story 1"),
        ("Lunch (END)", "Side Story 1: Lunch"),
    ],
    ids=["marker-only", "marker-and-title", "bare-label", "status-marker"],
)
def test_a_side_story_title_drops_the_side_marker_and_a_bare_label(title, want):
    assert named("none", [new_side(1, title=title)])[(SIDE, 1)].title == want, "Side Story <n>[: <title>]"


@budget(load="pure")
def test_side_stories_order_by_source_volume_then_number_then_date():
    rows = [
        new_side(1, src_volume="2", title="b", order=1),
        new_side(5, title="a", date="2024-02-01", order=2),
        new_side(5.5, title="c", date="2024-01-01", order=3),
    ]
    got = sorted((v.number, v.title) for v in resolve("none", rows, None, {}).values())
    assert got == [(1.0, "Side Story 1: a"), (2.0, "Side Story 2: c"), (3.0, "Side Story 3: b")], (
        "no source volume counts as 0; then the number"
    )


# rule 5: the chapter that opens a covered volume


@budget(load="pure")
def test_only_the_first_of_2_new_chapters_of_a_covered_volume_opens_it():
    rows = [settled(1, 1, "override"), settled(2, 1, "override"), new(3), new(4)]
    got = named("override", rows, OVERRIDE_1_2)
    assert [got[(MAIN, n)].opens_covered_volume for n in (3, 4)] == [True, False], "1 cover per volume, its first"


@budget(load="pure")
@pytest.mark.parametrize(
    ("basis", "rows", "overrides", "mangadex", "want"),
    [
        ("mangadex", [settled(1, 1, "mangadex"), new(2)], None, {"1": "1", "2": "2"}, True),
        ("filename", [settled(1, 1, "filename"), new(2, src_volume="2")], None, {"1": "1", "2": "2"}, True),
        ("filename", [settled(1, 1, "filename"), new(2, src_volume="2")], None, None, False),
        ("filename", [settled(1, 1, "filename"), new(2, src_volume="2")], None, {"1": "2", "2": "2"}, False),
        ("override", [settled(3, 2, "override"), new(2.5)], {"volumes": {1: "1-2", 2: "2.5-"}}, None, False),
        ("season tags", [settled(1, 1, "season tags"), new(2, season=2)], None, None, False),
        ("mangadex", [settled(1, 1, "mangadex"), new(1.5)], None, {"1": "1", "1.5": "1"}, False),
    ],
    ids=[
        "mangadex",
        "filename-confirmed",
        "filename-unconfirmed",
        "filename-disagreeing-mangadex",
        "volume-already-published",
        "season-volume",
        "not-first",
    ],
)
def test_a_covered_volume_comes_from_mangadex_or_the_override(basis, rows, overrides, mangadex, want):
    got = [v.opens_covered_volume for v in resolve(basis, rows, overrides, mangadex or {}).values()]
    assert got == [want], "MangaDex, override or a confirmed filename volume, opened by its first new chapter"


# the stored MangaDex aggregate


@budget(load="pure")
@pytest.mark.parametrize(
    ("aggregate", "want"),
    [
        (
            {"volumes": {"1": {"volume": "01", "chapters": {"1": {"chapter": "01"}, "2.50": {"chapter": "2.50"}}}}},
            {"1": "1", "2.5": "1"},
        ),
        ({"volumes": [{"volume": "2", "chapters": [{"chapter": "7"}]}]}, {"7": "2"}),
        (
            {
                "volumes": {
                    "none": {"volume": "none", "chapters": {"3": {"chapter": "3"}}},
                    "2": {"volume": "2", "chapters": {"3": {"chapter": "3"}}},
                }
            },
            {"3": "2"},
        ),
        (
            {
                "volumes": {
                    "2": {"volume": "2", "chapters": {"3": {"chapter": "3"}}},
                    "none": {"volume": "none", "chapters": {"3": {"chapter": "3"}, "4": {"chapter": "4"}}},
                }
            },
            {"3": "2", "4": None},
        ),
        ({"volumes": []}, {}),
        (None, {}),
    ],
    ids=["dicts-normalised", "lists", "real-beats-none-after", "real-beats-none-before", "empty-list", "no-aggregate"],
)
def test_mangadex_volumes_read_the_aggregates_quirks(aggregate, want):
    assert mangadex_volumes(aggregate) == want, "numbers normalised, none is no volume, a real volume beats none"


# round trips: a published name parses back

TITLE = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters=" '!,&"), max_size=30
)
NUMBER = st.decimals(min_value=0, max_value=2000, places=1, allow_nan=False, allow_infinity=False)


@budget(seconds=5, load="pure")
@settings(database=None, derandomize=True, max_examples=300, deadline=None)
@given(number=NUMBER, title=TITLE, volume=st.one_of(st.none(), st.integers(min_value=0, max_value=200)))
def test_a_main_chapter_file_name_parses_back(number, title, volume):
    stem = names.file_name(float(number), title, volume)[: -len(".cbz")]
    item = names.parse(stem)
    assert (item.kind, item.num, names.to_volume(item.volume)) == (MAIN, float(number), volume), (
        f"{stem!r} parses back to its number, volume and kind"
    )


# golden: real bulk records, their last chapters resolved from the rest


def _rows(fixture):
    rows = []
    for k, c in enumerate(fixture["chapters"]):
        stem = Path(c["file"]).stem
        item = names.parse(stem)
        season = int(item.season) if item.season else None
        if c["open"]:
            number = item.num
            if number is None:
                continue  # the resolver's caller leaves out an open chapter without a number (chapter_names)
            title = names.chapter_title(item, "")
            rows.append(
                Row(
                    key=k,
                    settled=False,
                    kind=item.kind,
                    number=number,
                    season=season,
                    src_volume=item.volume,
                    volume=None,
                    volume_source=None,
                    marker_title=stem,
                    title=title,
                    date=None,
                    order=k,
                )
            )
        else:
            volume = names.to_volume(c["volume"])
            rows.append(
                Row(
                    key=k,
                    settled=True,
                    kind=c["kind"],
                    number=float(c["number"]),
                    season=season,
                    src_volume=None,
                    volume=volume,
                    volume_source=c["volume_source"],
                    marker_title=stem,
                    title=c["title"],
                    date=None,
                    order=k,
                )
            )
    return rows


def _opens(fixture, k):
    """Return the record's own cover targets.

    A target: the chapter is its volume's first main chapter and no kept chapter has it.
    """
    c = fixture["chapters"][k]
    volume = names.to_volume(c["volume"])
    if c["kind"] != MAIN or volume not in fixture["covered_volumes"]:
        return False
    same = [x for x in fixture["chapters"] if names.to_volume(x["volume"]) == volume]
    first = min(float(x["number"]) for x in same if x["kind"] == MAIN)
    return float(c["number"]) == first and all(x["open"] for x in same)


@budget(load="fs")
@pytest.mark.parametrize(
    "path", sorted(p for p in RECORDS.glob("*.json") if not p.stem.startswith("platform-")), ids=lambda p: p.stem
)  # platform series: tests/test_platforms.py
def test_a_records_last_chapters_resolve_as_the_bulk_converter_named_them(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    # no MangaDex aggregate is kept offline: the record's own MangaDex-sourced volumes stand in for it
    mangadex = {
        names.fmt_num(float(c["number"])): str(c["volume"])
        for c in fixture["chapters"]
        if c["volume_source"] == naming.MANGADEX
    }
    got = resolve(fixture["basis"], _rows(fixture), fixture["overrides"], mangadex)
    for k, c in enumerate(fixture["chapters"]):
        if c["open"]:
            want = (c["kind"], names.to_volume(c["volume"]), float(c["number"]), c["title"], _opens(fixture, k))
            have = (got[k].kind, got[k].volume, got[k].number, got[k].title, got[k].opens_covered_volume)
            assert have == want, f"{fixture['series']}: {c['file']} named as the record holds it"
