"""clients.kavita: the token and the 401 rule, the reads, the scan requests, path mapping and a series' folder.

SPEC "Services".
"""

import logging
from pathlib import Path

import pytest

from manga_pipeline import config
from manga_pipeline.clients import http, kavita
from manga_pipeline.errors import Unreachable

from tests.builders import kavita_series
from tests.fakes.http import CannedService, Each, reply
from tests.timing import budget

AUTH = "/api/Plugin/authenticate"
ROOT = "/kavita/library"  # the library folder, as Kavita sees it
LIBRARIES = [{"id": 4, "name": "Manga", "folders": [ROOT]}, {"id": 5, "name": "Books", "folders": ["/books/"]}]


def failure(status, body=b""):
    return http.HttpError(status, "http://kavita.invalid/x", body)


def client(answers=None, path_map=None):
    """Return a client whose requests are answered from `answers`.

    authenticate answers a token unless `answers` says more.
    """
    service = CannedService(
        "kavita", {**(answers or {}), **({} if AUTH in (answers or {}) else {AUTH: {"token": "t1"}})}
    )
    return kavita.Kavita(
        "http://kavita.invalid/", "secret-key", path_map or {"/kavita": "/mnt/share"}, service
    ), service


def volumes(*paths, read=0):
    """Return a volumes reply: 1 volume, 1 chapter per file path."""
    chapters = [
        {
            "id": 100 + i,
            "volumeId": 7,
            "pages": 10,
            "pagesRead": read,
            "lastModifiedUtc": f"2026-11-15T21:24:02.{i:07d}",
            "files": [{"id": 200 + i, "filePath": p, "bytes": 1000 + i, "pages": 10}],
        }
        for i, p in enumerate(paths)
    ]
    return [{"id": 7, "chapters": chapters}]


# --- token


@budget(load="pure")
def test_first_call_authenticates_as_the_plugin_and_sends_the_token():
    k, service = client({"/api/Library/libraries": LIBRARIES})
    assert k.libraries() == LIBRARIES
    assert service.sent == [
        ("POST", f"{AUTH}?apiKey=secret-key&pluginName=manga-pipeline", None),
        ("GET", "/api/Library/libraries", None),
    ]
    assert service.headers == {"Authorization": "Bearer t1"}


@budget(load="pure")
def test_token_is_kept_for_the_run():
    k, service = client({"/api/Library/libraries": LIBRARIES})
    k.libraries()
    k.libraries()
    assert [path for _, path, _ in service.sent].count("/api/Library/libraries") == 2
    assert len(service.sent) == 3, "SPEC 'Services': the token is kept for the run"


@budget(load="pure")
def test_a_401_gets_1_new_token_and_1_more_try():
    k, service = client(
        {AUTH: Each([{"token": "old"}, {"token": "new"}]), "/api/Library/libraries": Each([failure(401), LIBRARIES])}
    )
    assert k.libraries() == LIBRARIES
    assert [method for method, _, _ in service.sent] == ["POST", "GET", "POST", "GET"]
    assert service.headers == {"Authorization": "Bearer new"}


@budget(load="pure")
def test_a_second_401_makes_kavita_unreachable_with_1_warning(caplog):
    k, service = client({"/api/Library/libraries": Each([failure(401), failure(401)]), "/api/Series/5": {"id": 5}})
    with caplog.at_level(logging.WARNING, logger="manga_pipeline"), pytest.raises(Unreachable) as e:
        k.libraries()
    assert (e.value.service, e.value.reason) == ("kavita", "HTTP 401 after re-authentication")
    sent = len(service.sent)
    with pytest.raises(Unreachable):
        k.series(5)
    assert len(service.sent) == sent, "SPEC 'Services': Kavita is not asked again in the run"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1, "SPEC 'Services': 1 warning"
    assert "secret-key" not in caplog.text


@budget(load="pure")
def test_a_refused_api_key_makes_kavita_unreachable():
    k, service = client({AUTH: failure(401)})
    with pytest.raises(Unreachable, match="API key was refused"):
        k.libraries()
    assert len(service.sent) == 1


@budget(load="pure")
def test_other_failures_pass_through():
    k, _ = client({"/api/Library/libraries": failure(500, b"boom")})
    with pytest.raises(http.HttpError) as e:
        k.libraries()
    assert e.value.status == 500


@budget(load="pure")
def test_an_unreachable_kavita_is_passed_on():
    k, _ = client({"/api/Library/libraries": Unreachable("kavita", "HTTP 503")})
    with pytest.raises(Unreachable, match="HTTP 503"):
        k.libraries()


# --- reads


@budget(load="pure")
def test_series_in_library_follows_pages(monkeypatch):
    monkeypatch.setattr(kavita, "PAGE_SIZE", 2)
    pages = Each([[{"id": 1}, {"id": 2}], [{"id": 3}]])
    k, service = client({"/api/Series/all-v2": pages})
    assert k.series_in_library(4) == [{"id": 1}, {"id": 2}, {"id": 3}]
    body = {
        "statements": [{"comparison": 0, "field": 19, "value": "4"}],
        "combination": 1,
        "limitTo": 0,
        "sortOptions": {"sortField": 1, "isAscending": True},
    }
    assert service.sent[1:] == [
        ("POST", "/api/Series/all-v2?PageNumber=1&PageSize=2", body),
        ("POST", "/api/Series/all-v2?PageNumber=2&PageSize=2", body),
    ]


@budget(load="pure")
def test_series_in_library_stops_after_an_empty_page(monkeypatch):
    monkeypatch.setattr(kavita, "PAGE_SIZE", 2)
    k, _service = client({"/api/Series/all-v2": Each([[{"id": 1}, {"id": 2}], []])})
    assert k.series_in_library(4) == [{"id": 1}, {"id": 2}]


@budget(load="pure")
def test_series_is_its_dto():
    k, service = client({"/api/Series/12": {"id": 12, "name": "S"}})
    assert k.series(12) == {"id": 12, "name": "S"}
    assert service.sent[-1] == ("GET", "/api/Series/12", None)


@budget(load="pure")
@pytest.mark.parametrize("answer", [failure(404), b"", reply(b"  ")], ids=["404", "empty", "blank"])
def test_series_kavita_no_longer_holds_is_none(answer):
    k, _ = client({"/api/Series/12": answer})
    assert k.series(12) is None, "SPEC 'Services': a 404 or an empty reply means the series is gone"


@budget(load="pure")
def test_series_passes_other_failures_on():
    k, _ = client({"/api/Series/12": failure(500)})
    with pytest.raises(http.HttpError):
        k.series(12)


@budget(load="pure")
def test_a_file_reply_without_its_size_is_an_error_not_a_file_of_unknown_size():
    reply_without_bytes = volumes(f"{ROOT}/S/Ch.1.cbz")
    del reply_without_bytes[0]["chapters"][0]["files"][0]["bytes"]
    k, _ = client({"/api/Series/volumes": reply_without_bytes})
    with pytest.raises(KeyError, match="bytes"):
        k.series_files(12)


@budget(load="pure")
def test_series_files_carry_local_paths_sizes_and_ids():
    k, service = client({"/api/Series/volumes": volumes(f"{ROOT}/S/Ch.1.cbz", f"{ROOT}/S/Ch.2.cbz", read=3)})
    assert k.series_files(12) == [
        {
            "path": Path("/mnt/share/library/S/Ch.1.cbz"),
            "bytes": 1000,
            "pages": 10,
            "file_id": 200,
            "chapter_id": 100,
            "volume_id": 7,
            "pages_read": 3,
            "modified": "2026-11-15T21:24:02.0000000",
        },
        {
            "path": Path("/mnt/share/library/S/Ch.2.cbz"),
            "bytes": 1001,
            "pages": 10,
            "file_id": 201,
            "chapter_id": 101,
            "volume_id": 7,
            "pages_read": 3,
            "modified": "2026-11-15T21:24:02.0000001",
        },
    ]
    assert service.sent[-1] == ("GET", "/api/Series/volumes?seriesId=12", None)


# --- path mapping


@budget(load="pure")
@pytest.mark.parametrize(
    ("path_map", "path", "want"),
    [
        ({"/kavita": "/mnt/share"}, "/kavita/library/S/c1.cbz", "/mnt/share/library/S/c1.cbz"),
        ({"/kavita": "/mnt/share"}, "/kavita", "/mnt/share"),
        ({"/kavita/": "/mnt/share"}, "/kavita/a", "/mnt/share/a"),
        ({"/kavita": "/mnt/a", "/kavita/library": "/mnt/b"}, "/kavita/library/S", "/mnt/b/S"),
        ({"/kavita/library": "/mnt/b", "/kavita": "/mnt/a"}, "/kavita/other/S", "/mnt/a/other/S"),
        ({"/shared/manga": "/shared/manga"}, "/shared/manga/x", "/shared/manga/x"),
    ],
    ids=["prefix", "the-prefix-itself", "trailing-slash", "longest-wins", "shorter-for-the-rest", "identity"],
)
def test_local_path_maps_the_longest_prefix(path_map, path, want):
    k, _ = client(path_map=path_map)
    assert k.local_path(path) == Path(want)


@budget(load="pure")
@pytest.mark.parametrize(
    "path", ["/kavita2/S/c1.cbz", "/Kavita/S/c1.cbz", "/other/S"], ids=["boundary", "case", "none"]
)
def test_path_under_no_prefix_is_an_error(path):
    k, _ = client(path_map={"/kavita": "/mnt/share"})
    with pytest.raises(ValueError, match="under no prefix"):
        k.local_path(path)


# --- series folder


@budget(load="pure")
def test_series_folder_is_the_folder_of_its_files_not_folder_path():
    k, service = client(
        {
            "/api/Series/volumes": volumes(f"{ROOT}/Local/S/c1.cbz", f"{ROOT}/Local/S/c2.cbz"),
            "/api/Library/libraries": LIBRARIES,
        }
    )
    assert k.series_folder(12) == Path("/mnt/share/library/Local/S")
    assert ("GET", "/api/Series/12", None) not in service.sent, "the series DTO (folderPath) is never asked"


@budget(load="pure")
@pytest.mark.parametrize(
    ("paths", "problem"),
    [
        ([], "0 folders"),
        ([f"{ROOT}/A/c1.cbz", f"{ROOT}/B/c2.cbz"], "2 folders"),
        ([f"{ROOT}/c1.cbz"], "directly in the library folder"),
        (["/books/c1.cbz"], "directly in the library folder"),
    ],
    ids=["no-files", "2-folders", "library-root", "another-library-root"],
)
def test_series_without_a_single_folder_below_a_library_has_no_usable_folder(paths, problem):
    k, _ = client({"/api/Series/volumes": volumes(*paths), "/api/Library/libraries": LIBRARIES}, {"/": "/mnt"})
    with pytest.raises(kavita.FolderError, match=problem):
        k.series_folder(12)


@budget(load="pure")
def test_library_folders_are_read_once_per_client():
    k, service = client({"/api/Series/volumes": volumes(f"{ROOT}/S/c1.cbz"), "/api/Library/libraries": LIBRARIES})
    k.series_folder(1)
    k.series_folder(2)
    assert [path for _, path, _ in service.sent].count("/api/Library/libraries") == 1


# --- scans


@budget(load="pure")
def test_scan_library_posts_an_unforced_library_scan():
    k, service = client({"/api/Library/scan": b""})
    k.scan_library(4)
    assert service.sent[-1] == ("POST", "/api/Library/scan?libraryId=4&force=false", None)


@budget(load="pure")
def test_from_config_reads_the_kavita_keys():
    cfg = config.cached()
    k = kavita.from_config(cfg)
    assert k.base == "http://kavita.invalid"
    assert k.local_path("/kavita/library") == Path(cfg["paths"]["kavita_path_map"]["/kavita"], "library")


@budget()
def test_update_series_resends_the_series_whole_with_the_changes():
    kv, service = client({"/api/Series/update": {}})
    series: kavita.Series = {**kavita_series(7, "N"), "coverImageLocked": True, "aniListId": 1, "libraryId": 3}
    kv.update_series(series, aniListId=2)
    method, path, body = service.sent[-1]
    assert (method, path) == ("POST", "/api/Series/update")
    assert body == {
        **{name: series[name] for name in kavita.UPDATE_SERIES_FIELDS},
        "id": 7,
        "aniListId": 2,
    }, "every field Kavita reads is resent (an omitted one is reset); fields it does not read are not"
    assert body["coverImageLocked"] is True, "a locked cover stays locked"
    assert "libraryId" not in body, "a field the update does not read is not sent"


@budget()
def test_an_upload_kavita_does_not_serve_is_a_cover_error():
    kv, _ = client({"/api/Upload/series": {}, "/api/Series/7": {"id": 7, "coverImage": "v01.png"}})
    with pytest.raises(kavita.CoverError):
        kv.upload_cover(7, b"img")
