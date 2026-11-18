"""clients.suwayomi (SPEC "Services").

GraphQL errors, paging, the reads, the writes' requests, the source-call rules and the download file names. Replies
under tests/fixtures_api were recorded from a real Suwayomi v2.4.2366 by reads only; the replies of the calls that
write are written by hand from its schema.
"""

import copy
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from manga_pipeline import config
from manga_pipeline.clients import http, suwayomi
from manga_pipeline.errors import Unreachable

from tests.builders import present
from tests.fakes.http import CannedService, Clock, Each, Reply, Urlopen
from tests.timing import budget

FIXTURES = Path(__file__).with_name("fixtures_api")
GRAPHQL = "/api/graphql"
PATH_ERROR = {"errors": [{"message": "No route to host", "path": ["fetchSourceManga"]}], "data": None}
PING = {"data": {"__typename": "Query"}}
MANGA = {"id": 9, "title": "T", "status": "ONGOING", "url": "/t", "inLibrary": False, "sourceId": "7", "source": None}
CHAPTER = {"id": 90, "mangaId": 9, "name": "Ch.1", "scanlator": None, "chapterNumber": 1.0, "sourceOrder": 1}


def recorded(name):
    return json.loads((FIXTURES / f"suwayomi_{name}.json").read_text(encoding="utf-8"))


def client(*answers):
    """Return a client answered by `answers`, 1 per request in order."""
    service = CannedService("suwayomi", {GRAPHQL: Each(answers)})
    return suwayomi.Suwayomi("http://suwayomi.invalid/", service), service


def sent_query(service, index=-1):
    method, path, body = service.sent[index]
    assert (method, path) == ("POST", GRAPHQL)
    return body["query"], body["variables"]


# --- GraphQL


@budget(load="pure")
def test_graphql_returns_the_data():
    s, service = client({"data": {"a": 1}})
    assert s.graphql("{ a }", json.loads, {"x": 2}) == {"a": 1}
    assert service.sent == [("POST", GRAPHQL, {"query": "{ a }", "variables": {"x": 2}})]


@budget(load="fs")
@pytest.mark.parametrize("name", ["error_validation", "error_syntax", "error_execution"])
def test_a_reply_with_errors_is_a_failure_though_its_status_is_200(name):
    s, _ = client(recorded(name))
    with pytest.raises(suwayomi.GraphQLError) as e:
        s.graphql("{ anything }", json.loads)
    assert e.value.errors == recorded(name)["errors"], "SPEC 'Services': HTTP 200 with errors is a failure"
    assert recorded(name)["errors"][0]["message"] in str(e.value)


@budget(load="pure")
def test_a_timed_out_query_is_sent_again(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(http, "time", clock)
    urlopen = Urlopen(TimeoutError("timed out"), Reply({"data": {"mangas": {"totalCount": 758}}}))
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    assert suwayomi.Suwayomi("http://suwayomi.invalid").library_size() == 758, "SPEC 'Services': a timeout is retried"
    assert len(urlopen.requests) == 2
    assert clock.sleeps == [2]
    assert urlopen.requests[0].full_url == "http://suwayomi.invalid/api/graphql"


@budget(load="pure")
def test_an_unreachable_suwayomi_is_passed_on():
    s, _ = client(Unreachable("suwayomi", "HTTP 503"))
    with pytest.raises(Unreachable, match="HTTP 503"):
        s.sources()


# --- reads


@budget(load="fs")
def test_library_size():
    s, _ = client(recorded("library_size"))
    assert s.library_size() == 758


@budget(load="fs")
def test_library_manga_follows_pages_until_the_last(monkeypatch):
    monkeypatch.setattr(suwayomi, "PAGE_SIZE", 3)
    last = copy.deepcopy(recorded("library_page_2"))
    last["data"]["mangas"]["pageInfo"]["hasNextPage"] = False
    s, service = client(recorded("library_page_1"), last)
    got = list(s.library_manga())
    assert [m["id"] for m in got] == [23, 31093, 31094, 31148, 31149, 31151], "SPEC 'Services': read page by page"
    assert present(got[0]["source"])["displayName"] == "MangaDex (EN)"
    assert got[0]["title"].startswith("I, Who Have")
    assert [sent_query(service, i)[1] for i in (0, 1)] == [{"first": 3, "after": 0}, {"first": 3, "after": 31094}]
    query = sent_query(service)[0]
    assert "inLibrary: true" in query
    assert "order: [{by: ID}]" in query
    assert "id: {greaterThan: $after}" in query


@budget(load="fs")
def test_library_manga_asks_for_the_next_page_only_when_it_is_read():
    s, service = client(recorded("library_page_1"))
    assert next(s.library_manga())["id"] == 23
    assert len(service.sent) == 1


@budget(load="pure")
def test_library_manga_of_an_empty_library_is_empty():
    s, _ = client({"data": {"mangas": {"nodes": [], "pageInfo": {"hasNextPage": False}}}})
    assert list(s.library_manga()) == []


@budget(load="fs")
def test_manga_by_id():
    s, service = client(recorded("manga"))
    assert present(s.manga(23))["title"].startswith("I, Who Have")
    assert sent_query(service)[1] == {"id": 23}


@budget(load="fs")
def test_manga_suwayomi_does_not_hold_is_none():
    s, _ = client(recorded("manga_absent"))
    assert s.manga(999999999) is None, "SPEC 'Services': an absent manga is not an error"


@budget(load="fs")
def test_chapters_of_a_manga():
    s, service = client(recorded("chapters"))
    got = s.chapters(23)
    assert [(c["id"], c["name"], c["scanlator"], c["isDownloaded"]) for c in got[:2]] == [
        (788, "Ch.0 - Fading Strength", "Uploaded by RedRhetoric", True),
        (787, "Ch.0", "FireScans", True),
    ]
    assert set(got[0]) == set(suwayomi.CHAPTER.split()), "every field asked for is in the reply"
    query, variables = sent_query(service)
    assert variables == {"id": 23}
    assert "mangaId: $id" in query


@budget(load="fs")
def test_download_queue():
    s, _ = client(recorded("download_queue"))
    got = s.download_queue()
    assert got["state"] == "STOPPED"
    assert got["queue"][0] == {
        "state": "ERROR",
        "tries": 3,
        "progress": 0.0,
        "position": 0,
        "chapter": {"id": 541053, "mangaId": 106422},
    }


@budget(load="fs")
def test_sources():
    s, _ = client(recorded("sources"))
    assert s.sources()[0] == {
        "id": "0",
        "name": "Local source",
        "lang": "localsourcelang",
        "displayName": "Local source",
    }


# --- writes (replies written by hand from the schema; not run against a real server)


@budget(load="pure")
def test_enqueue_sends_the_chapter_ids():
    s, service = client({"data": {"enqueueChapterDownloads": {"downloadStatus": {"state": "STOPPED"}}}})
    s.enqueue((5, 6))
    query, variables = sent_query(service)
    assert variables == {"ids": [5, 6]}
    assert "enqueueChapterDownloads(input: {ids: $ids})" in query


@budget(load="pure")
def test_delete_downloaded_sends_the_chapter_ids():
    s, service = client({"data": {"deleteDownloadedChapters": {"chapters": [{"id": 5}]}}})
    s.delete_downloaded([5])
    query, variables = sent_query(service)
    assert variables == {"ids": [5]}
    assert "deleteDownloadedChapters(input: {ids: $ids})" in query


@budget(load="pure")
def test_start_downloader():
    s, service = client({"data": {"startDownloader": {"downloadStatus": {"state": "STARTED"}}}})
    s.start_downloader()
    assert "startDownloader(input: {})" in sent_query(service)[0]


@budget(load="pure")
def test_dequeue_sends_the_chapter_ids():
    s, service = client({"data": {"dequeueChapterDownloads": {"downloadStatus": {"state": "STOPPED"}}}})
    s.dequeue([5])
    query, variables = sent_query(service)
    assert variables == {"ids": [5]}
    assert "dequeueChapterDownloads(input: {ids: $ids})" in query


@budget(load="pure")
def test_chapter_by_id_and_an_absent_one():
    s, service = client({"data": {"chapters": {"nodes": [CHAPTER]}}}, {"data": {"chapters": {"nodes": []}}})
    assert s.chapter(90) == CHAPTER, "a chapter is read by its id"
    assert sent_query(service)[1] == {"id": 90}
    assert s.chapter(91) is None, "an id Suwayomi does not hold reads as None"


@budget(load="pure")
def test_a_write_that_fails_is_a_failure():
    s, _ = client({"errors": [{"message": "boom", "path": ["enqueueChapterDownloads"]}], "data": None})
    with pytest.raises(suwayomi.GraphQLError, match="boom"):
        s.enqueue([5])


# --- calls that reach a download source


@budget(load="pure")
def test_search_source_returns_its_hits_without_retrying_a_timeout():
    s, service = client({"data": {"fetchSourceManga": {"mangas": [MANGA]}}})
    assert s.search_source("7", "T") == [MANGA]
    query, variables = sent_query(service)
    assert variables == {"source": "7", "query": "T"}
    assert "type: SEARCH, page: 1" in query
    assert service.options == [(suwayomi.SOURCE_TIMEOUT, False)], "SPEC 'Services': its timeout is not retried"


@budget(load="pure")
def test_fetch_manga_and_chapters_returns_both():
    s, service = client({"data": {"fetchMangaAndChapters": {"manga": MANGA, "chapters": [CHAPTER]}}})
    assert s.fetch_manga_and_chapters(9) == {"manga": MANGA, "chapters": [CHAPTER]}
    query, variables = sent_query(service)
    assert variables == {"id": 9}
    assert "fetchManga: false, fetchChapters: true" in query
    assert service.options == [(suwayomi.SOURCE_TIMEOUT, False)]


@budget(load="pure")
@pytest.mark.parametrize("call", [lambda s: s.search_source("7", "T"), lambda s: s.fetch_manga_and_chapters(9)])
def test_an_error_raised_while_the_query_ran_means_the_source_is_unavailable(call):
    s, _ = client(PATH_ERROR)
    with pytest.raises(suwayomi.SourceUnavailableError, match="No route to host"):
        call(s)


@budget(load="fs")
@pytest.mark.parametrize("name", ["error_validation", "error_syntax"])
def test_a_wrong_query_to_a_source_call_is_a_failure(name):
    s, _ = client(recorded(name))
    with pytest.raises(suwayomi.GraphQLError):
        s.search_source("7", "T")


@budget(load="pure")
def test_errors_with_and_without_a_path_are_a_failure():
    s, _ = client({"errors": [{"message": "a", "path": ["x"]}, {"message": "b"}], "data": None})
    with pytest.raises(suwayomi.GraphQLError):
        s.search_source("7", "T")


@budget(load="pure")
def test_a_source_call_that_times_out_while_suwayomi_answers_means_the_source_is_unavailable():
    s, service = client(http.HttpTimeoutError("POST http://suwayomi.invalid/api/graphql timed out after 120 s"), PING)
    with pytest.raises(suwayomi.SourceUnavailableError, match="timed out"):
        s.search_source("7", "T")
    assert sent_query(service)[0] == "{ __typename }", "SPEC 'Services': Suwayomi is asked a trivial query"
    assert service.options[-1] == (60, True), "the ping is retried like any request"


@budget(load="pure")
def test_a_source_call_that_times_out_while_suwayomi_does_not_answer_is_unreachable():
    s, _ = client(http.HttpTimeoutError("timed out"), Unreachable("suwayomi", "timed out after 60 s"))
    with pytest.raises(Unreachable) as e:
        s.fetch_manga_and_chapters(9)
    assert e.value.service == "suwayomi"


@budget(load="pure")
def test_from_config_reads_the_url():
    assert suwayomi.from_config(config.cached()).base == "http://suwayomi.invalid"


# --- download file names


@budget(load="pure")
@pytest.mark.parametrize(
    ("name", "want"),
    [
        (" .a:b*c?d<e>f|g\\h/i. ", "a_b_c_d_e_f_g_h_i"),
        (" ...   . ", "(invalid)"),
        ("", "(invalid)"),
        ("a" * 300, "a" * 240),
        ("a你" * 128, "a你" * 60),
        ('He said "hi"\x7f\x00\x1f', "He said _hi____"),
        ("Re:Zero", "Re_Zero"),
        ("name. ", "name"),
        ("a . b", "a . b"),
        ("\tname", "_name"),
        ("x" * 239 + ". tail", "x" * 239 + "."),
        ("x" * 239 + "\U0001f600" + "y", "x" * 239),
        ("é" * 121, "é" * 120),
        ("\u200b", "\u200b"),
    ],
    ids=[
        "suwayomi-test-1",
        "suwayomi-test-2",
        "empty",
        "suwayomi-test-3",
        "suwayomi-test-4",
        "quotes-and-controls",
        "colon",
        "trailing-dot-space",
        "inner-kept",
        "leading-tab-replaced",
        "no-trim-after-the-cut",
        "no-room-for-4-bytes",
        "2-byte-characters",
        "zero-width-space",
    ],
)
def test_valid_filename_is_suwayomis_rule(name, want):
    assert suwayomi.valid_filename(name) == want, "SPEC 'Services': Suwayomi's own file-name rule"


@budget(load="pure")
def test_folders_and_chapter_file():
    assert suwayomi.source_folder("MangaDex (EN)") == "MangaDex (EN)"
    assert suwayomi.manga_folder("Re:Zero - Starting Life?") == "Re_Zero - Starting Life_"
    assert (
        suwayomi.chapter_file("Easy Going Scans", "Vol.1 Ch.0 - Prologue")
        == "Easy Going Scans_Vol.1 Ch.0 - Prologue.cbz"
    )
    assert suwayomi.chapter_file(None, "Chapter 5: End") == "Chapter 5_ End.cbz"
    assert suwayomi.chapter_file("", "Ch.1") == "_Ch.1.cbz", "an empty scanlator still counts as one"
    assert suwayomi.chapter_file("\u200b", "Ch.1") == "\u200b_Ch.1.cbz"
    assert suwayomi.chapter_file(None, "...") == "(invalid).cbz"
    assert suwayomi.chapter_file(None, "a" * 300) == "a" * 240 + ".cbz", "the extension follows the cut"


@budget(seconds=5, load="pure")
@settings(database=None, derandomize=True, max_examples=500, deadline=None)
@given(st.text())
def test_valid_filename_always_gives_a_name_suwayomi_would_write(name):
    got = suwayomi.valid_filename(name)
    assert got, "never empty"
    assert not any(ord(c) < 0x20 or c in suwayomi.REPLACED for c in got), "no character Suwayomi replaces"
    assert len(got.encode("utf-16-le", "surrogatepass")) // 2 <= 240, "at most 240 UTF-16 units"
    assert len(got.encode("utf-8", "surrogatepass")) <= 240, "at most 240 UTF-8 bytes"
    if len(name.encode("utf-8", "surrogatepass")) <= 240:
        assert suwayomi.valid_filename(got) == got, "a name that needs no cut is changed once, not twice"
