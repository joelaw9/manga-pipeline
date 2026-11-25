"""Source ids (SPEC "Source ids"): 1 form per source, read from each origin, judged for agreement.

And the search acceptance rules.
"""

from typing import TYPE_CHECKING

import pytest

from manga_pipeline.metadata import ids, search

from tests.fakes.sources import fake_sources
from tests.timing import budget

if TYPE_CHECKING:
    from manga_pipeline.metadata.records import SeriesRecords
    from manga_pipeline.replies.sources import AniListHit, AniListTitle, MangaBakaSeries, MangaDexManga
    from manga_pipeline.replies.suwayomi import Manga

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
    record_mb: MangaBakaSeries = {"id": 9, "source": {"manga_updates": {"id": "04gtb06"}}}
    record_md: MangaDexManga = {"id": MD, "attributes": {"links": {"mu": "4gtb06"}}}
    manga: Manga = {
        "id": 1,
        "title": "X",
        "status": "ONGOING",
        "url": "",
        "description": None,
        "inLibrary": True,
        "sourceId": "1",
        "source": {"id": "1", "name": "X", "lang": "en", "displayName": "X (EN)"},
        "trackRecords": {"nodes": [{"trackerId": 7, "remoteId": number}]},
    }
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
    title: AniListTitle = {"romaji": None, "english": "Omniscient Reader", "native": None}
    hits: list[AniListHit] = [
        {"id": 1, "format": "NOVEL", "title": title, "synonyms": None},
        {"id": 2, "format": "MANGA", "title": title, "synonyms": None},
    ]
    al.results["Omniscient Reader"] = hits
    assert search.anilist(al, ["Omniscient Reader"]) == "2", "the novel is skipped"
    hits.append({"id": 3, "format": "MANGA", "title": None, "synonyms": ["omniscient reader"]})
    assert search.anilist(al, ["Omniscient Reader"]) is None, "2 manga share the name: none is taken"


@budget()
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("anilist.co/manga/105398", [("anilist", "105398")]),
        ("Read it on [AniList](https://anilist.co/manga/105398/Solo-Leveling).", [("anilist", "105398")]),
        ("**https://www.anilist.co/manga/105398**<br>", [("anilist", "105398")]),
        ("(https://www.mangaupdates.com/series/04gtb06)", [("mangaupdates", str(int("04gtb06", 36)))]),
        ("mangaupdates.com/series.html%3fid=04gtb06.", [("mangaupdates", str(int("04gtb06", 36)))]),
        ("https://www.mangaupdates.com/series.html?id=151025", []),  # all digits: a retired legacy id
        (f"MangaDex: https://mangadex.org/title/{MD.upper()}/solo.", [("mangadex", MD)]),
        ("https://myanimelist.net/manga/121496 https://mangabaka.org/2034", []),  # never searched: not read
        (
            f"https://mangadex.org/title/{MD} then https://anilist.co/manga/1",
            [("mangadex", MD), ("anilist", "1")],
        ),
        (None, []),
    ],
)
def test_a_description_link_gives_its_id_whatever_text_surrounds_it(text, expected):
    assert ids.description_ids(text) == expected, "the id ends at the first character outside its form"


@budget()
def test_variants_strip_up_to_2_trailing_suffixes_and_keep_1_name_per_key():
    found = search.variants(["Title (Official) [Webtoon]", "title  official webtoon", "(Only)", "A [B] [C] [D]"])
    assert found == [
        "Title (Official) [Webtoon]",
        "(Only)",
        "A [B] [C] [D]",
        "Title (Official)",
        "Title",
        "A [B] [C]",
        "A [B]",
    ], "own names first, then each with up to 2 suffixes removed; a name only of a suffix stays whole"
    fullwidth = "Hero" + chr(0xFF08) + "Official" + chr(0xFF09)
    assert search.variants([fullwidth, "Saga 〘Official〙"]) == [fullwidth, "Saga 〘Official〙", "Hero", "Saga"]


@budget()
def test_lookup_names_add_mangabaka_titles_else_mangaupdates_titles():
    records: SeriesRecords = {
        "mangabaka": {"id": 1, "title": "Main", "secondary_titles": {"en": [{"title": "Other"}]}},
        "mangaupdates": {"title": "MU Title", "associated": [{"title": "MU Other"}]},
        "mangadex": None,
        "anilist": None,
    }
    assert search.lookup_names(["Name (Official)"], records) == ["Name (Official)", "Main", "Name", "Other"], (
        "own names, MangaBaka's main title, the variants, then its other titles"
    )
    records["mangabaka"] = None
    assert search.lookup_names(["Name"], records) == ["Name", "MU Title", "MU Other"], "MangaUpdates' without MangaBaka"


@budget()
def test_a_type_suffix_keeps_only_mangaupdates_results_of_that_type():
    clients = fake_sources()
    mu = clients.mangaupdates
    mu.results["Solo (Manhwa)"] = mu.results["Solo"] = [
        {"record": {"series_id": 1, "title": "Solo", "type": "Manga"}, "hit_title": "Solo"},
        {"record": {"series_id": 2, "title": "Solo", "type": "Manhwa"}, "hit_title": "Solo"},
    ]
    assert search.mangaupdates(mu, ["Solo (Manhwa)", "Solo"]) == str(2), "only the Manhwa result is taken"
    assert search.mangaupdates(mu, ["Solo [Webtoon]", "Solo"]) == str(2), "Webtoon allows Manhwa and Manhua"


@budget()
def test_mangaupdates_without_a_type_suffix_takes_every_comic_type():
    clients = fake_sources()
    mu = clients.mangaupdates
    mu.results["Solo"] = [{"record": {"series_id": 2, "title": "Solo", "type": "Manhwa"}, "hit_title": "Solo"}]
    assert search.mangaupdates(mu, ["Solo"]) == "2", "no suffix: no type limit"


@budget()
def test_a_webtoon_suffix_beside_another_type_suffix_allows_both():
    clients = fake_sources()
    mu = clients.mangaupdates
    mu.results["Foo (Webtoon)"] = [{"record": {"series_id": 1, "title": "Foo", "type": "Manga"}, "hit_title": "Foo"}]
    names = search.variants(["Foo (Webtoon)", "Foo (Manga)"])
    assert search.mangaupdates(mu, names) == "1", "Manga kept beside Manhwa and Manhua"


@budget()
def test_a_type_suffix_also_limits_mangabakas_title_search():
    clients = fake_sources()
    mb = clients.mangabaka
    mb.results["Bastard"] = [{"id": 1, "title": "Bastard!!", "type": "manga"}]
    assert search.mangabaka(mb, search.variants(["Bastard (Manhwa)"]), {}) is None, "the manga is not the manhwa"
    assert search.mangabaka(mb, ["Bastard"], {}) == "1", "without a suffix: taken"
