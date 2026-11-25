"""Which published files' volumes change (SPEC "Retroactive updates").

Only a fill of a published file holding no volume, or its override range's volume; a source moving or dropping a
volume changes nothing and is listed.
"""

import dataclasses
import json

import pytest

from manga_pipeline import names
from manga_pipeline.metadata import naming, rewrite_plan
from manga_pipeline.metadata.naming import Row

from tests.test_naming_resolver import RECORDS
from tests.timing import budget

MAIN, SIDE = names.MAIN, names.SIDE


def pub(file_id, number, volume, source, kind=MAIN) -> Row[naming.RowKey]:
    return Row(
        key=("published", file_id),
        settled=True,
        kind=kind,
        number=float(number),
        season=None,
        src_volume=None,
        volume=volume,
        volume_source=source,
        marker_title="",
        title="",
        date=None,
        order=file_id,
    )


def new(key, number, src_volume=None) -> Row[naming.RowKey]:
    return Row(
        key=key,
        settled=False,
        kind=MAIN,
        number=float(number),
        season=None,
        src_volume=src_volume,
        volume=None,
        volume_source=None,
        marker_title="",
        title="",
        date=None,
        order=1000 + key,
    )


@budget(load="fs")
@pytest.mark.parametrize(
    "path", sorted(p for p in RECORDS.glob("*.json") if not p.stem.startswith("platform-")), ids=lambda p: p.stem
)
def test_a_bulk_record_with_its_own_inputs_needs_no_rewrite(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    mangadex = {
        names.fmt_num(float(c["number"])): str(c["volume"])
        for c in fixture["chapters"]
        if c["volume_source"] == naming.MANGADEX
    }
    rows = [
        pub(k, c["number"], names.to_volume(c["volume"]), c["volume_source"], c["kind"])
        for k, c in enumerate(fixture["chapters"])
    ]
    got = rewrite_plan.changes(fixture["basis"], rows, fixture["overrides"], mangadex)
    assert got == {}, f"{fixture['series']}: the record's own volumes stand"


@budget(load="pure")
def test_a_published_file_holding_no_volume_is_filled():
    rows = [pub(1, 1, 1, "filename"), pub(2, 2, None, "none"), pub(3, 3, 1, "filename")]
    got = rewrite_plan.changes("filename", rows, None, {"1": "1", "2": "1", "3": "1"})
    assert got == {2: (1, "mangadex")}, "MangaDex fills the gap between 2 volume-1 neighbours"


@budget(load="pure")
def test_an_override_range_moves_a_published_volume():
    rows = [pub(1, 1, 1, "mangadex"), pub(2, 2, 1, "mangadex"), pub(3, 3, 1, "mangadex")]
    got = rewrite_plan.changes("mangadex", rows, {"volumes": {2: "3-"}}, {"1": "1", "2": "1", "3": "1"})
    assert got == {3: (2, "override")}, "the override file wins over the source"


@budget(load="pure")
def test_volume_starts_move_a_published_part_chapter_into_its_volume():
    rows = [pub(1, 4, 1, "mangadex"), pub(2, 4.5, None, "none"), pub(3, 5, 2, "mangadex")]
    got = rewrite_plan.changes("mangadex", rows, {"volume_starts": {1: 0, 2: 5}}, {"4": "1", "5": "2"})
    assert got == {2: (1, "override")}, "4.5 lies between volume 1's start and volume 2's: volume 1"


@budget(load="pure")
def test_an_override_range_does_not_reach_a_season_chapter():
    rows = [dataclasses.replace(pub(n, n, 1, "season tags"), season=1) for n in (1, 2)]
    assert rewrite_plan.changes("season tags", rows, {"volumes": {2: "2"}}, {}) == {}, "a season's numbers restart"


@budget(load="pure")
@pytest.mark.parametrize("now", [{"1": "1", "2": "2"}, {"1": "1"}], ids=["moved", "dropped"])
def test_a_source_moving_or_dropping_a_published_volume_changes_nothing(now):
    rows = [pub(1, 1, 1, "mangadex"), pub(2, 2, 1, "mangadex")]
    assert rewrite_plan.changes("mangadex", rows, None, now) == {}, "7e never moves or removes a volume"
    want = {2: (1, names.to_volume(now.get("2")))}
    assert rewrite_plan.differences("mangadex", rows, None, now) == want, "naming diff lists it"


@budget(load="pure")
def test_a_filename_volume_is_not_listed_as_dropped():
    rows = [pub(1, 1, 1, "filename"), pub(2, 2, 1, "filename")]
    assert rewrite_plan.differences("filename", rows, None, {}) == {}, "no input reads a file's own volume again"


@budget(load="pure")
def test_a_fractional_fill_is_skipped_without_an_error():
    rows = [pub(1, 1, 1, "filename"), pub(2, 2, None, "none"), pub(3, 3, 2, "filename")]
    assert rewrite_plan.changes("filename", rows, None, {"2": "1.5"}) == {}, "ComicInfo Volume is whole"


@budget(load="pure")
def test_open_chapters_and_side_stories_are_not_rewritten():
    rows = [pub(1, 1, None, "none"), pub(2, 1, None, "side", kind=SIDE), new(9, 2, src_volume="1")]
    got = rewrite_plan.changes("filename", rows, {"volumes": {1: "1-2"}}, {})
    assert got == {1: (1, "override")}, "only published main chapters change"
