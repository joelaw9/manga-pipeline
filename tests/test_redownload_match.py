"""redownload.pick_manga and pick_chapter (SPEC "Bad downloads").

Which search hit is the series and which chapter of another source is ours, over hand-built Suwayomi replies.
"""

from manga_pipeline import names, redownload
from manga_pipeline.replies import suwayomi

from tests.builders import present
from tests.timing import budget


def hit(manga_id, title) -> suwayomi.Manga:
    """Return a search hit: a manga of the source, not in the library."""
    return {
        "id": manga_id,
        "title": title,
        "status": "ONGOING",
        "url": f"/manga/{manga_id}",
        "inLibrary": False,
        "sourceId": "1",
        "source": None,
        "trackRecords": {"nodes": []},
    }


def rows(names_, scanlator=None, start=1):
    """Suwayomi chapter rows named names_ ([name] or [(scanlator, name)])."""
    out = []
    for i, n in enumerate(names_, start):
        group, name = n if isinstance(n, tuple) else (scanlator, n)
        out.append({"id": i, "name": name, "scanlator": group})
    return out


def reference(chapters):
    """Return the numbering reference pick_chapter takes of Suwayomi chapter rows."""
    return [redownload.numbering(row) for row in chapters]


def chapter_of(stem):
    """Return the identity and number pick_chapter takes of our chapter, named stem."""
    item = names.parse(stem)
    return names.identity(item), float(present(item.number))


# --- the series


@budget(load="pure")
def test_the_hit_whose_title_equals_a_known_name_is_the_series():
    hits = [hit(1, "Solo Leveling: Ragnarok"), hit(2, "SOLO LEVELING"), hit(3, "Solo Levelling")]
    assert present(redownload.pick_manga(hits, ["Solo Leveling"]))["id"] == 2, "letters and digits, lower-cased, equal"


@budget(load="pure")
def test_two_exact_hits_are_not_confident():
    assert redownload.pick_manga([hit(1, "Solo Leveling"), hit(2, "solo-leveling")], ["Solo Leveling"]) is None


@budget(load="pure")
def test_a_lone_near_title_is_the_series_when_every_other_hit_is_far_below():
    hits = [hit(1, "The Beginning After the End."), hit(2, "Beginning")]
    assert redownload.pick_manga(hits, ["The Beginning After The End (Official)"]) is None, "ratio below 90"
    hits = [hit(1, "Omniscient Reader Viewpoint"), hit(2, "Omniscient Reader")]
    assert present(redownload.pick_manga(hits, ["Omniscient Reader's Viewpoint"]))["id"] == 1, "ratio 98, runner-up 76"
    close = [hit(1, "Omniscient Reader Viewpoint"), hit(2, "Omniscient Readers Viewpoin")]
    assert redownload.pick_manga(close, ["Omniscient Reader's Viewpoint"]) is None, "a runner-up within 10 is unsure"


@budget(load="pure")
def test_no_hit_no_series():
    assert redownload.pick_manga([], ["Solo Leveling"]) is None


# --- the chapter


ORIGINAL = rows([f"Chapter {n}" for n in range(1, 21)])


@budget(load="pure")
def test_the_chapter_with_our_number_is_ours_when_the_numbering_agrees():
    other = rows([f"Ch. {n}" for n in range(1, 21)], start=100)
    found = present(redownload.pick_chapter(*chapter_of("Chapter 12"), reference(ORIGINAL), other))
    assert found["name"] == "Ch. 12", "the same season, side label and number, whatever the label or title"


@budget(load="pure")
def test_a_chapter_numbered_with_2_readings_is_never_picked_for_the_shorter_decimal():
    ours = rows([f"Chapter {n}" for n in range(1, 21)] + ["Chapter 12.1"])
    other = rows([f"Ch. {n}" for n in range(1, 21)] + ["Ch. 12.10"], start=100)
    assert redownload.pick_chapter(*chapter_of("Chapter 12.1"), reference(ours), other) is None, (
        "12.10 is not chapter 12.1"
    )
    with_ours = rows([f"Ch. {n}" for n in range(1, 21)] + ["Ch. 12.10", "Ch. 12.1"], start=100)
    assert (
        present(redownload.pick_chapter(*chapter_of("Chapter 12.1"), reference(ours), with_ours))["name"] == "Ch. 12.1"
    )


@budget(load="pure")
def test_numbering_that_does_not_agree_is_not_confident():
    other = rows([f"Chapter {n}" for n in range(30, 50)], start=100)  # another numbering (absolute vs per season)
    other += rows(["Chapter 12"], start=500)
    assert redownload.pick_chapter(*chapter_of("Chapter 12"), reference(ORIGINAL), other) is None


@budget(load="pure")
def test_neighbours_with_unlike_titles_disagree():
    ours = rows([f"Chapter {n} - Title {n}" for n in range(1, 21)])
    shifted = rows([f"Chapter {n} - Title {n + 1}" for n in range(1, 21)], start=100)  # off by 1 in content
    assert redownload.pick_chapter(*chapter_of("Chapter 12 - Title 12"), reference(ours), shifted) is None
    same = rows([f"Chapter {n} - Title {n}" for n in range(1, 21)], start=100)
    assert present(redownload.pick_chapter(*chapter_of("Chapter 12 - Title 12"), reference(ours), same))["id"] == 111


@budget(load="pure")
def test_fewer_than_3_neighbours_are_not_enough():
    ours = rows(["Chapter 1", "Chapter 2", "Chapter 3"])
    other = rows(["Chapter 1", "Chapter 2", "Chapter 3"], start=100)
    assert redownload.pick_chapter(*chapter_of("Chapter 2"), reference(ours), other) is None, (
        "only 2 other chapters to compare"
    )


@budget(load="pure")
def test_another_season_with_the_same_number_is_not_ours():
    ours = rows([f"S2 Chapter {n}" for n in range(1, 21)])
    other = rows([f"S1 Chapter {n}" for n in range(1, 21)], start=100)
    other += rows([f"S2 Chapter {n}" for n in range(1, 21)], start=200)
    found = present(redownload.pick_chapter(*chapter_of("S2 Chapter 5"), reference(ours), other))
    assert found["name"] == "S2 Chapter 5"


@budget(load="pure")
def test_an_aggregator_row_per_group_picks_our_scanlator_or_none():
    groups = [(g, f"Chapter {n}") for n in range(1, 21) for g in ("Official", "Fan Group")]
    other = rows(groups, start=100)
    found = present(
        redownload.pick_chapter(
            *chapter_of("Fan Group_Chapter 12"),
            reference(rows([("Fan Group", f"Chapter {n}") for n in range(1, 21)])),
            other,
        )
    )
    assert (found["scanlator"], found["name"]) == ("Fan Group", "Chapter 12")
    assert redownload.pick_chapter(*chapter_of("Other_Chapter 12"), reference(ORIGINAL), other) is None, (
        "2 groups, neither ours"
    )
