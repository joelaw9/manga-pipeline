"""The metadata sources' clients and title searches on canned replies (SPEC "Source ids", "Metadata checks").

Each client gets a CannedService through its `service=` argument; the replies are shaped as the local library's
`manga-metadata/` notes describe them.
"""

import pytest

from manga_pipeline.clients import http
from manga_pipeline.metadata import search, sources

from tests.fakes.http import CannedService, Each
from tests.timing import budget

MU_ID = int("04gtb06", 36)  # a MangaUpdates id whose base-36 form starts with 0
MD_ID = "5f6a3e2c-1b4d-4c8e-9a7f-2d3b4c5e6f70"
OTHER_MD_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def gone(path):
    return http.HttpError(404, f"https://source.invalid{path}", b'{"errors":[{"message":"Not Found.","status":404}]}')


def mangaupdates_result(series_id, title, hit_title=None, kind="Manga"):
    return {"record": {"series_id": series_id, "title": title, "type": kind}, "hit_title": hit_title or title}


def mangadex_record(md_id, mu_link):
    return {"id": md_id, "type": "manga", "attributes": {"links": {"mu": mu_link}}}


@budget(load="pure")
def test_anilist_media_is_none_when_anilist_answers_404():
    service = CannedService("anilist", {"": gone("")})  # AniList's endpoint is the host itself: path ""
    assert sources.AniList(service).media(12345) is None, "an AniList 404 for a Media id means AniList no longer has it"


@budget(load="pure")
def test_anilist_graphql_errors_raise_source_error():
    reply = {"data": None, "errors": [{"message": "Too Many Requests.", "status": 429}]}
    service = CannedService("anilist", {"": reply})
    with pytest.raises(sources.SourceError, match="Too Many Requests"):
        sources.AniList(service).media(12345)


@budget(load="pure")
def test_mangabaka_series_is_none_when_mangabaka_answers_404():
    service = CannedService("mangabaka", {"/v1/series/77": gone("/v1/series/77")})
    assert sources.MangaBaka(service).series("77") is None, "a MangaBaka 404 for a series id means it is gone"


@budget(load="pure")
@pytest.mark.parametrize(
    ("states", "want"),
    [
        (["merged", "active", "deleted"], 2),
        (["active", "active"], None),
        (["merged", "deleted"], None),
    ],
)
def test_mangabaka_by_source_takes_the_1_active_series(states, want):
    series = [{"id": i + 1, "state": state, "title": f"T{i}"} for i, state in enumerate(states)]
    service = CannedService("mangabaka", {"/v1/source/anilist/9": {"data": {"series": series}}})
    found = sources.MangaBaka(service).by_source("anilist", "9")
    assert (found["id"] if found else None) == want, "by_source keeps only active series and gives none for several"


@budget(load="pure")
def test_mangabaka_lookup_by_mangaupdates_id_asks_with_7_base36_digits():
    record = {"id": 31, "state": "active", "type": "manga", "title": "Yona"}
    service = CannedService("mangabaka", {"/v1/source/manga-updates/": {"data": {"series": [record]}}})
    found = search.mangabaka(sources.MangaBaka(service), ["Yona"], {"mangaupdates": str(MU_ID)})
    assert service.sent == [("GET", "/v1/source/manga-updates/04gtb06", None)], (
        "MangaBaka is asked with the MangaUpdates id's zero-padded 7-digit base-36 form"
    )
    assert found == "31", "the active series MangaBaka holds for the MangaUpdates id is the series' MangaBaka id"


@budget(load="pure")
def test_mangadex_covers_reads_every_page():
    first = [{"attributes": {"volume": str(v), "locale": "ja", "fileName": f"{v}.jpg"}} for v in range(100)]
    second = [{"attributes": {"volume": str(v), "locale": "ja", "fileName": f"{v}.jpg"}} for v in range(100, 150)]
    pages = Each([{"data": first, "total": 150}, {"data": second, "total": 150}])
    service = CannedService("mangadex", {"/cover?": pages})
    covers = sources.MangaDex(service).covers(MD_ID)
    assert [c["fileName"] for c in covers] == [f"{v}.jpg" for v in range(150)], "covers holds every page's covers"
    offsets = [path.rsplit("offset=", 1)[1] for _, path, _ in service.sent]
    assert offsets == ["0", "100"], "each page is asked from the offset the pages before it reached"


@budget(load="pure")
def test_mangaupdates_search_posts_the_name_and_page_size():
    results = [mangaupdates_result(1, "Yona of the Dawn")]
    service = CannedService("mangaupdates", {"/v1/series/search": {"results": results}})
    found = sources.MangaUpdates(service).search("Yona of the Dawn")
    assert service.sent == [("POST", "/v1/series/search", {"search": "Yona of the Dawn", "perpage": 10})], (
        "the search body is the name and a page of SEARCH_RESULTS"
    )
    assert found == results, "search returns the reply's results"


@budget(load="pure")
@pytest.mark.parametrize(
    "hit",
    [mangaupdates_result(2, "Yona of the Dawn"), mangaupdates_result(2, "Akatsuki no Yona", "Yona of the Dawn")],
    ids=["title", "hit_title"],
)
def test_search_mangaupdates_takes_the_1_exact_hit(hit):
    results = [mangaupdates_result(1, "Yona: Dawn Travels"), hit]
    service = CannedService("mangaupdates", {"/v1/series/search": {"results": results}})
    found = search.mangaupdates(sources.MangaUpdates(service), ["Yona of the Dawn"])
    assert found == "2", "exactly 1 result whose title or hit title is the name is the series"


@budget(load="pure")
def test_search_mangaupdates_tries_the_next_name_when_several_series_share_one():
    shared = {"results": [mangaupdates_result(1, "Leviathan"), mangaupdates_result(2, "Leviathan")]}
    unique = {"results": [mangaupdates_result(2, "Leviathan (Lee Hyunseok)")]}
    service = CannedService("mangaupdates", {"/v1/series/search": Each([shared, unique])})
    found = search.mangaupdates(sources.MangaUpdates(service), ["Leviathan", "Leviathan (Lee Hyunseok)"])
    assert found == "2", "a name several series hold gives no id; the next name's 1 exact hit does"


@budget(load="pure")
def test_search_mangadex_takes_the_entry_whose_mangaupdates_link_is_the_series_id():
    records = [mangadex_record(OTHER_MD_ID, "zzzzzzz"), mangadex_record(MD_ID, "04gtb06")]
    service = CannedService("mangadex", {"/manga?": {"data": records}})
    found = search.mangadex(sources.MangaDex(service), ["Yona"], {"mangaupdates": str(MU_ID)})
    assert found == MD_ID, "the MangaDex entry is the one whose links.mu is the series' MangaUpdates id"


@budget(load="pure")
def test_search_mangadex_without_a_mangaupdates_id_asks_nothing():
    service = CannedService("mangadex")
    assert search.mangadex(sources.MangaDex(service), ["Yona"], {}) is None, "no MangaUpdates id, no MangaDex id"
    assert service.sent == [], "MangaDex is not searched without a MangaUpdates id to confirm a hit"


@budget(load="pure")
def test_mangadex_chapter_list_reads_every_page_keeps_untitled_entries_and_skips_unnumbered_ones():
    first = [{"attributes": {"publishAt": "x", "chapter": "01", "title": "Start"}}] * 499 + [
        {"attributes": {"publishAt": "x", "chapter": "Extra", "title": "Omake"}}
    ]
    second = [{"attributes": {"publishAt": "x", "chapter": "17.50", "title": None}}]
    service = CannedService(
        "mangadex", {f"/manga/{MD_ID}/feed?": Each([{"data": first, "total": 501}, {"data": second, "total": 501}])}
    )
    got = sources.MangaDex(service).chapter_list(MD_ID)
    assert got[0] == ["1", "Start"], "the number as fmt_num gives it"
    assert got[-1] == ["17.5", ""], "an untitled entry is kept: the parts rule counts every number"
    assert len(got) == 500, "a chapter that is no number is left out"
    sent = [path for _, path, _ in service.sent]
    assert all("translatedLanguage%5B%5D=en" in p and "order%5BcreatedAt%5D=asc" in p for p in sent)
    assert [p.rsplit("offset=", 1)[1].split("&")[0] for p in sent] == ["0", "500"]
