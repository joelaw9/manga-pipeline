"""The split-chapter run rule (SPEC "Retroactive updates", Split chapters)."""

import itertools
from dataclasses import replace

import pytest

from manga_pipeline.metadata.parts import File, Run, held, runs

from tests.timing import budget

_ids = itertools.count(1)


BASE = File(id=0, season=None, side="", number=None, volume=None, origin="pipeline", confirmed=True, joined=None)


def f(number, fid=None, **fields):
    """Return a published file of the rule's shape: BASE numbered `number`, with fields (season, side, volume…)."""
    return replace(BASE, id=fid or next(_ids), number=number, **fields)


def numbers(*nums, **kw):
    return [f(n, fid=i, **kw) for i, n in enumerate(nums, 1)]


@pytest.mark.parametrize("k", [2, 3, 4, 5, 6])
@budget(load="pure")
def test_an_exact_run_joins(k):
    files = numbers(*(f"12.{j}" for j in range(1, k + 1)))
    assert runs(files) == [Run(None, 12, tuple(range(1, k + 1)), k)], "N.1 … N.k with k ≥ 2 is a run"


@pytest.mark.parametrize(
    ("nums", "expected"),
    [
        (("12.1", "12.2", "12.5"), (1, 2)),
        (("12.1", "12.2", "12.3", "12.5"), (1, 2, 3)),
        (("12.1", "12.2", "12.3", "12.4", "12.5"), (1, 2, 3, 4, 5)),
    ],
)
@budget(load="pure")
def test_a_point_5_is_an_extra_beside_2_or_3_parts_and_a_part_after_4(nums, expected):
    (run,) = runs(numbers(*nums))
    assert run.members == expected, "N.5 beside 2-3 parts stays its own chapter; after N.4 it is part 5"


@pytest.mark.parametrize(
    ("nums", "why"),
    [
        (("12.1", "12.2", "12.5", "12.6"), "other parts beyond the run"),
        (("12.1", "12.3"), "a gap"),
        (("12.2", "12.3"), "not from .1"),
        (("12.1",), "a lone .1"),
        (("12", "12.1", "12.2"), "beside a whole N"),
        (("12.01", "12.02"), "x.01 is no part"),
        (("12.1", "12.1", "12.2"), "2 files of 1 part"),
        (("12.1", "12.2", "12.4"), "a part beyond with a gap"),
    ],
)
@budget(load="pure")
def test_numbers_outside_the_rule_stay_as_they_are(nums, why):
    assert runs(numbers(*nums)) == [], why


@budget(load="pure")
def test_an_unconfirmed_whole_n_blocks_the_run():
    files = [*numbers("12.1", "12.2"), f("12", confirmed=False, fid=9)]
    assert runs(files) == [], "a whole N just published, not confirmed yet, is still a whole N"


@budget(load="pure")
def test_a_joined_n_counts_as_its_parts_and_takes_the_next():
    files = [f("12", joined=2, fid=7), f("12.3", fid=8)]
    assert runs(files) == [Run(None, 12, (7, 8), 3)], "the joined file first, then the new part"
    assert runs([f("12", joined=2, fid=7)]) == [], "a joined file alone has nothing to join"
    assert runs([f("12", joined=2, fid=7), f("12.5", fid=8)]) == [], ".5 beside 2 parts held is an extra"


@budget(load="pure")
def test_a_run_needs_1_volume_and_1_season():
    assert runs([f("12.1", volume="2"), f("12.2", volume="3")]) == [], "2 volumes: no run"
    assert runs([f("12.1", volume="2"), f("12.2", volume="2.0")]) != [], "the same volume written 2 ways"
    two = [f("3.1", season="1"), f("3.2", season="2")]
    assert runs(two) == [], "parts of 2 seasons are 2 chapters"
    one = [f("3.1", season="2", fid=1), f("3.2", season="2", fid=2)]
    assert runs(one) == [Run("2", 3, (1, 2), 2)]


@budget(load="pure")
def test_side_chapters_are_never_parts():
    assert runs(numbers("4.1", "4.2", side="side story")) == []


@pytest.mark.parametrize(
    ("member", "why"),
    [
        ({"origin": "library"}, "an adopted part joins once converted"),
        ({"origin": "bulk"}, "a bulk file never joins"),
        ({"confirmed": False}, "a part not confirmed yet"),
    ],
)
@budget(load="pure")
def test_every_member_is_a_confirmed_pipeline_file(member, why):
    files = [f("12.1", fid=1), f("12.2", fid=2, **member)]
    assert runs(files) == [], why
    if "origin" in member:
        assert runs(files, any_origin=True) == [Run(None, 12, (1, 2), 2)], "the census counts it"


@budget(load="pure")
def test_runs_of_a_series_come_by_season_then_number():
    files = [*numbers("20.1", "20.2"), f("3.1", fid=11), f("3.2", fid=12)]
    assert [r.whole for r in runs(files)] == [3, 20]


@budget(load="pure")
def test_held_names_the_parts_a_joined_file_holds():
    assert held(None, "12", 3) == {(None, "", "12.1"), (None, "", "12.2"), (None, "", "12.3")}
    assert held("2", "5", None) == set(), "a file that is no joined file holds no parts"
