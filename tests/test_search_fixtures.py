"""Title search recall (SPEC "Source ids") on replies recorded from the real sources, 1 case per rule of A7.

Each fixture in tests/fixtures_api/search/ holds real replies (date and rule inside); each test also shows the case
missed or wrongly taken without its rule.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from manga_pipeline.metadata import ids, search

from tests.fakes.sources import fake_sources
from tests.timing import budget

if TYPE_CHECKING:
    from manga_pipeline.metadata.records import SeriesRecords

FIXTURES = Path(__file__).with_name("fixtures_api") / "search"


def recorded(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@budget()
def test_k10_a_description_gives_the_series_anilist_and_mangaupdates_ids():
    case = recorded("description")
    found = [pair for text in case["descriptions"] for pair in ids.description_ids(text)]
    assert found == [("anilist", "182118"), ("mangaupdates", "37653239020")], "The Song of the Medicine Love You Sing"


@budget()
def test_k29_a_manhwa_suffix_keeps_mangaupdates_from_taking_a_manga_of_the_same_name():
    case = recorded("type-hint")
    clients = fake_sources()
    clients.mangaupdates.results.update(case["mangaupdates_search"])
    assert search.mangaupdates(clients.mangaupdates, ["Bastard"]) == "44676133193", "without a suffix: Bastard!!"
    assert search.mangaupdates(clients.mangaupdates, ["Bastard (Manhwa)", "Bastard"]) is None, (
        "the only exact hit is the Japanese manga: not taken for a manhwa"
    )


@budget()
def test_k30_a_trailing_suffix_removed_finds_the_series():
    case = recorded("suffix-variant")
    clients = fake_sources()
    clients.mangabaka.results.update(case["mangabaka_search"])
    assert search.mangabaka(clients.mangabaka, ["Solo Leveling (Official)"], {}) is None, "the name as it is: no hit"
    names = search.variants(["Solo Leveling (Official)"])
    assert search.mangabaka(clients.mangabaka, names, {}) == "3397", "searched as Solo Leveling"


@budget()
def test_k31_mangadex_is_found_by_a_mangabaka_title_the_series_names_lack():
    case = recorded("mangabaka-title")
    clients = fake_sources()
    clients.mangadex.results.update(case["mangadex_search"])
    records: SeriesRecords = {"mangabaka": case["mangabaka"], "mangaupdates": None, "mangadex": None, "anilist": None}
    assert search.mangadex(clients.mangadex, case["names"], case["agreed"]) is None, "by its own name: not found"
    found = search.mangadex(clients.mangadex, search.lookup_names(case["names"], records), case["agreed"])
    assert found == "7c9d6ef4-7a17-46b3-b2e7-870b474f2ab2", "Vengeance of the Fallen Crown, by its Japanese title"
