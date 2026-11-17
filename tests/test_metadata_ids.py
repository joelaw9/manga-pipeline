"""Source ids (SPEC "Source ids"): 1 form per source, read from each origin, judged for agreement; and the search
acceptance rules.
"""

import pytest

from manga_pipeline.metadata import ids, search

from tests.fakes.sources import fake_sources
from tests.timing import budget

MD = "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"


@budget()
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://anilist.co/manga/105398/Solo-Leveling", ("anilist", "105398")),
        ("https://www.mangaupdates.com/series/04gtb06/solo-leveling", ("mangaupdates", str(int("04gtb06", 36)))),
        ("https://www.mangaupdates.com/series.html?id=151025", None),
        ("https://www.mangaupdates.com/series/151025", None),
        ("https://www.mangaupdates.com/authors/4kfx6u7/oh-yeong", None),  # a MangaUpdates page that is no series'
        (f"https://mangadex.org/title/{MD.upper()}/solo", ("mangadex", MD)),
        ("https://myanimelist.net/manga/121496/Na_Honjaman_Level_Up", ("myanimelist", "121496")),
        ("https://mangabaka.org/2034", ("mangabaka", "2034")),
        ("https://mangabaka.dev/series/2034", ("mangabaka", "2034")),
        ("https://mangabaka.org/manga/2034/solo-leveling", ("mangabaka", "2034")),
        ("https://mangabaka.org/2034/solo-leveling", ("mangabaka", "2034")),
        ("https://anilist.co/manga/²", None),
        ("https://example.com/manga/1", None),
    ],
)
def test_a_link_gives_its_sources_id_in_its_form(url, expected):
    assert ids.link_id(url) == expected, "an all-digit MangaUpdates link is a retired legacy id: ignored"


@budget()
def test_every_origin_of_a_mangaupdates_id_compares_equal():
    number = str(int("04gtb06", 36))
    record_mb = {"id": 9, "source": {"manga_updates": {"id": "04gtb06"}}}
    record_md = {"id": MD, "attributes": {"links": {"mu": "4gtb06"}}}
    manga = {"source": {"name": "X"}, "url": "", "trackRecords": {"nodes": [{"trackerId": 7, "remoteId": number}]}}
    found = {
        ("mangabaka",): ids.mangabaka_ids(record_mb),
        ("mangadex",): ids.mangadex_ids(record_md),
        ("suwayomi",): ids.suwayomi_ids(manga),
    }
    assert {v for pairs in found.values() for s, v in pairs if s == "mangaupdates"} == {number}, (
        "base-36 text (padded or not) and the tracker's number are 1 id"
    )


@budget()
def test_disagreeing_ids_name_each_origin_and_a_human_id_wins():
    rows = [("anilist", "kavita", "1"), ("anilist", "suwayomi", "2"), ("mangadex", "kavita", MD)]
    agreed, problem = ids.resolve(rows)
    assert problem == "anilist ids disagree: 1 (kavita), 2 (suwayomi)", "the problem names every id and its origin"
    agreed, problem = ids.resolve([*rows, ("anilist", "human", "2")])
    assert (agreed, problem) == ({"anilist": "2", "mangadex": MD}, None), "a person's id is the source's id"


@budget()
def test_a_mangabaka_search_takes_the_1_exact_title_else_a_close_top_result_alone():
    clients = fake_sources()
    mb = clients.mangabaka
    mb.results["Solo Leveling"] = [{"id": 5, "title": "Solo Leveling"}, {"id": 6, "title": "Solo Leveling: Ragnarok"}]
    assert search.mangabaka(mb, ["Solo Leveling"], {}) == "5"
    mb.results["The Beginning After"] = [{"id": 7, "title": "The Beginning After the End"}, {"id": 8, "title": "TBATE"}]
    assert search.mangabaka(mb, ["The Beginning After"], {}) == "7", "a long title cut off still matches"
    mb.results["Tower"] = [{"id": 9, "title": "Towers"}, {"id": 10, "title": "Towerr"}]
    assert search.mangabaka(mb, ["Tower"], {}) is None, "2 close results: none is taken"


@budget()
def test_mangabaka_is_searched_by_title_only_without_ids_or_when_they_lead_to_a_novel():
    clients = fake_sources()
    mb = clients.mangabaka
    mb.results["Solo Leveling"] = [{"id": 5, "title": "Solo Leveling"}]
    assert search.mangabaka(mb, ["Solo Leveling"], {"anilist": "1"}) is None, "an AniList id MangaBaka lacks: no search"
    mb.records[("anilist", "1")] = {"id": 4, "type": "novel"}
    assert search.mangabaka(mb, ["Solo Leveling"], {"anilist": "1"}) == "5", "it led to the novel: searched"


@budget()
def test_a_search_never_takes_a_novel_or_a_name_2_series_share():
    clients = fake_sources()
    al = clients.anilist
    al.results["Omniscient Reader"] = [
        {"id": 1, "format": "NOVEL", "title": {"english": "Omniscient Reader"}},
        {"id": 2, "format": "MANGA", "title": {"english": "Omniscient Reader"}},
    ]
    assert search.anilist(al, ["Omniscient Reader"]) == "2", "the novel is skipped"
    al.results["Omniscient Reader"].append({"id": 3, "format": "MANGA", "synonyms": ["omniscient reader"]})
    assert search.anilist(al, ["Omniscient Reader"]) is None, "2 manga share the name: none is taken"
