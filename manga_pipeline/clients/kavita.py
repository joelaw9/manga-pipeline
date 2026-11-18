"""Kavita API client (SPEC "Services").

The token, the reads, the library scan request, path mapping and a series' folder.

Endpoints as verified against Kavita v0.9.1.4 by the bulk converter's client (optimizer/kavita.py), which this is
ported from. 1 object per run: it keeps the token, the library folders and whether Kavita was found unreachable.
"""

import base64
import json as jsonlib
import logging
import urllib.parse
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import TypedDict

from manga_pipeline import config
from manga_pipeline.clients import http
from manga_pipeline.errors import Unreachable
from manga_pipeline.replies.kavita import Library, Series, SeriesMetadata, Volume

log = logging.getLogger(__name__)
PLUGIN = "manga-pipeline"
PAGE_SIZE = 1000
LIBRARY_FILTER_FIELD = 19  # all-v2 filter statement field: the series' library
SORT_BY_NAME = 1  # all-v2 sortOptions.sortField


# Fields /api/Series/update reads (Kavita 0.9.1); see Kavita.update_series.
UPDATE_SERIES_FIELDS = (
    "name",
    "nameLocked",
    "sortName",
    "sortNameLocked",
    "localizedName",
    "localizedNameLocked",
    "coverImageLocked",
    "aniListId",
    "malId",
    "mangaBakaId",
    "cbrId",
    "comicVineId",
    "hardcoverId",
    "metronId",
    "metadataProviderOverride",
)


# Kavita's replies are typed in replies/kavita.py; the token reply and the client's own file listing are here. Each
# reply is read by its reader at the end of this module, unchecked.


class Authenticated(TypedDict):
    """The reply of POST /api/Plugin/authenticate: the token."""

    token: str


class SeriesFile(TypedDict):
    """A file Kavita holds for a series (Kavita.series_files), with its chapter's and volume's ids and progress."""

    path: Path
    bytes: int
    pages: int
    file_id: int
    chapter_id: int
    volume_id: int
    pages_read: int
    modified: str


class CoverError(Exception):
    """Kavita does not serve a cover it accepted."""


class ReplyError(Exception):
    """A Kavita reply lacks a field the client must resend."""


class FolderError(Exception):
    """A series has no usable folder (SPEC "Services")."""


class Kavita:
    """A run's Kavita client: it keeps the token, the library folders and the path map.

    Its http.Service remembers whether Kavita was found unreachable.
    """

    def __init__(
        self, url: str, api_key: str | None, path_map: Mapping[str, str], service: http.Service | None = None
    ) -> None:
        """Store the server's URL, the API key and the path map (longest Kavita prefix first); nothing is asked yet."""
        self.base = url.rstrip("/")
        self._api_key = api_key
        # Longest Kavita prefix first, so the most specific entry wins.
        self._path_map = sorted(((k.rstrip("/"), v) for k, v in path_map.items()), key=lambda kv: -len(kv[0]))
        self._service = service or http.Service("kavita")
        self._token: str | None = None
        self._library_folders: set[str] | None = None

    # --- reads

    def libraries(self) -> list[Library]:
        """GET /api/Library/libraries: every library with its id, name and folders (Kavita paths)."""
        return self._call("GET", "/api/Library/libraries").parse(_libraries)

    def series_in_library(self, library_id: int) -> list[Series]:
        """Return every series DTO of 1 library (POST /api/Series/all-v2).

        Filtered to the library and sorted by name (pages of an unsorted list can repeat or miss a series).
        """
        body: dict[str, object] = {
            "statements": [{"comparison": 0, "field": LIBRARY_FILTER_FIELD, "value": str(library_id)}],
            "combination": 1,
            "limitTo": 0,
            "sortOptions": {"sortField": SORT_BY_NAME, "isAscending": True},
        }
        out: list[Series] = []
        page = 1
        while True:
            batch = self._call("POST", f"/api/Series/all-v2?PageNumber={page}&PageSize={PAGE_SIZE}", body).parse(
                _series_page
            )
            out += batch
            if len(batch) < PAGE_SIZE:
                return out
            page += 1

    def series(self, series_id: int) -> Series | None:
        """GET /api/Series/{id}: the series DTO, or None when Kavita no longer holds it.

        None for a 404, or an empty reply for a series deleted since it was listed.
        """
        try:
            reply = self._call("GET", f"/api/Series/{int(series_id)}")
        except http.HttpError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                return None
            raise
        return reply.parse(_series) if reply.body.strip() else None

    def metadata(self, series_id: int) -> SeriesMetadata:
        """GET /api/Series/metadata: the series' metadata (web links, summary, genres, tags, people, locks)."""
        return self._call("GET", f"/api/Series/metadata?seriesId={int(series_id)}").parse(_metadata)

    def update_metadata(self, metadata: SeriesMetadata) -> None:
        """POST /api/Series/metadata: Kavita applies the whole series metadata as sent (read it fresh first)."""
        self._call("POST", "/api/Series/metadata", {"seriesMetadata": metadata})

    def update_series(self, series: Series, **changes: int) -> None:
        """POST /api/Series/update: the series as just read, resent whole with the changes.

        Kavita applies every field it is sent and resets those left out (an omitted aniListId becomes 0, an omitted
        coverImageLocked unlocks the cover). ReplyError when the series lacks one of those fields.
        """
        missing = [name for name in UPDATE_SERIES_FIELDS if name not in series]
        if missing:
            msg = f"Kavita's reply for series {series['id']} lacks {', '.join(missing)}: the update would reset it"
            raise ReplyError(msg)
        body: dict[str, object] = {name: series.get(name) for name in UPDATE_SERIES_FIELDS}
        body.update(changes, id=series["id"])
        self._call("POST", "/api/Series/update", body)

    def upload_cover(self, series_id: int, image: bytes) -> None:
        """POST /api/Upload/series, then check Kavita serves it.

        Kavita has answered 200 while failing to write the file. CoverError when Kavita does not serve it.
        """
        self._call("POST", "/api/Upload/series", {"id": int(series_id), "url": base64.b64encode(image).decode()})
        if not self.has_uploaded_cover(series_id):
            msg = f"Kavita accepted the cover of series {series_id} but does not serve it"
            raise CoverError(msg)

    def has_uploaded_cover(self, series_id: int) -> bool:
        """Return whether the series' cover is one uploaded through the API.

        Kavita names those series<id>.<ext> (a scan names its own after a volume), and serves its image.
        """
        series = self.series(series_id) or {}
        if not (series.get("coverImage") or "").startswith(f"series{int(series_id)}."):
            return False
        try:
            return bool(self._call("GET", f"/api/Image/series-cover?seriesId={int(series_id)}").body)
        except http.HttpError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                return False
            raise

    def volumes(self, series_id: int) -> list[Volume]:
        """GET /api/Series/volumes: the series' volumes, each with its chapters, each with its files (Kavita paths)."""
        return self._call("GET", f"/api/Series/volumes?seriesId={int(series_id)}").parse(_volumes)

    def series_files(self, series_id: int) -> list[SeriesFile]:
        """Return every file Kavita holds for the series.

        Each with its local path, bytes, pages, file id, chapter id, volume id, the chapter's pagesRead (of the API
        key's user) and its lastModifiedUtc (moved by every scan that re-reads the file: local library
        kavita-scanning.md).
        """
        return [
            {
                "path": self.local_path(file["filePath"]),
                "bytes": file["bytes"],
                "pages": file["pages"],
                "file_id": file["id"],
                "chapter_id": chapter["id"],
                "volume_id": volume["id"],
                "pages_read": chapter["pagesRead"],
                "modified": chapter["lastModifiedUtc"],
            }
            for volume in self.volumes(series_id)
            for chapter in volume["chapters"]
            for file in chapter["files"]
        ]

    def save_progress(self, series_id: int, volume_id: int, chapter_id: int, page: int) -> None:
        """Record that the API key's user has read `page` pages of the chapter (POST /api/reader/progress).

        Kavita caps it at the chapter's page count.
        """
        series = self.series(series_id)
        if series is None:
            msg = f"Kavita series {series_id} is gone: progress not saved"
            raise ValueError(msg)
        body: dict[str, object] = {
            "volumeId": volume_id,
            "chapterId": chapter_id,
            "pageNum": page,
            "seriesId": series_id,
            "libraryId": series["libraryId"],
            "bookScrollId": None,
        }
        self._call("POST", "/api/reader/progress", json=body)

    def mark_read(self, series_id: int, chapter_ids: Iterable[int]) -> None:
        """Record that the API key's user has read the chapters whole (POST /api/reader/mark-multiple-read)."""
        body: dict[str, object] = {
            "seriesId": series_id,
            "volumeIds": [],
            "chapterIds": list(chapter_ids),
            "generateReadingSession": False,
        }
        self._call("POST", "/api/reader/mark-multiple-read", json=body)

    def refresh_covers(self, library_id: int, series_id: int) -> None:
        """Have Kavita regenerate the series' chapter and volume cover images (POST /api/Series/refresh-metadata).

        Forced: a library scan keeps a re-read chapter's old one. The series' locked cover stays.
        """
        body: dict[str, object] = {"libraryId": library_id, "seriesId": series_id, "forceUpdate": True}
        self._call("POST", "/api/Series/refresh-metadata", json=body)

    def series_folder(self, series_id: int) -> Path:
        """Return the single folder holding the series' files, as a local path.

        FolderError when its files lie in no folder, in 2 or more, or directly in one of a library's own folders. Never
        Kavita's folderPath: for a series under a shared top folder that is the top folder.
        """
        folders = sorted(
            {
                file["filePath"].rpartition("/")[0]
                for volume in self.volumes(series_id)
                for chapter in volume["chapters"]
                for file in chapter["files"]
            }
        )
        if len(folders) != 1:
            found = ", ".join(folders) or "none"
            msg = f"Kavita series {series_id} has its files in {len(folders)} folders ({found}), not 1"
            raise FolderError(msg)
        if self._library_folders is None:
            self._library_folders = {f.rstrip("/") for library in self.libraries() for f in library["folders"]}
        if folders[0].rstrip("/") in self._library_folders:
            msg = f"Kavita series {series_id} has its files directly in the library folder {folders[0]}"
            raise FolderError(msg)
        return self.local_path(folders[0])

    def local_path(self, kavita_path: str) -> Path:
        """Return a path as Kavita reports it as a local path.

        The longest prefix of paths.kavita_path_map that ends on a path boundary is replaced; case counts. ValueError
        for a path under no prefix.
        """
        for prefix, local in self._path_map:
            if kavita_path == prefix or kavita_path.startswith(prefix + "/"):
                return Path(local, *kavita_path[len(prefix) :].split("/"))
        msg = f"Kavita path {kavita_path} is under no prefix of paths.kavita_path_map"
        raise ValueError(msg)

    # --- scans: requested, never awaited

    def scan_library(self, library_id: int) -> None:
        """POST /api/Library/scan (not forced): Kavita's own library scan.

        It matches files to series by their ComicInfo Series against each series' names and keeps their ids. The
        pipeline's only scan: a series scan deletes a series whose files no longer parse back to it, and any scan
        requested while one runs does not work. (POST /api/Library/scan-folder is never used: it rewrites a series'
        folderPath to the top folder.)
        """
        self._call("POST", f"/api/Library/scan?libraryId={int(library_id)}&force=false")

    # --- transport

    def _call(self, method: str, path: str, json: Mapping[str, object] | None = None) -> http.Response:
        """1 authenticated call. A 401 gets 1 new token and 1 more try; a second 401 makes Kavita unreachable."""
        for _ in (1, 2):
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            try:
                return self._service.request(method, self.base + path, headers=headers, json=json)
            except http.HttpError as e:
                if e.status != HTTPStatus.UNAUTHORIZED:
                    raise
                self._token = None
        msg = "HTTP 401 after re-authentication"
        raise self._refused(msg)

    def _get_token(self) -> str:
        if self._token is None:
            query = urllib.parse.urlencode({"apiKey": self._api_key, "pluginName": PLUGIN})
            try:
                reply = self._service.request("POST", f"{self.base}/api/Plugin/authenticate?{query}")
            except http.HttpError as e:
                if e.status != HTTPStatus.UNAUTHORIZED:
                    raise
                msg = "HTTP 401 from authenticate: the API key was refused"
                raise self._refused(msg) from None
            self._token = reply.parse(_authenticated)["token"]
        return self._token

    def _refused(self, reason: str) -> Unreachable:
        log.warning("kavita: %s; Kavita is not asked again in this run", reason)  # Phase 9 adds the notification
        return self._service.mark_unreachable(reason)


def from_config(cfg: config.Config) -> Kavita:
    """Build the Kavita client from the kavita.url, kavita.api_key and paths.kavita_path_map settings."""
    return Kavita(cfg["kavita"]["url"], cfg["kavita"]["api_key"], cfg["paths"]["kavita_path_map"])


# --- readers of Kavita's replies: each takes the reply's JSON as the shape Kavita sends, unchecked (an approved
# unchecked boundary: CLAUDE.md, "Code standards")


def _authenticated(body: bytes) -> Authenticated:
    """Read POST /api/Plugin/authenticate's reply; unchecked."""
    return jsonlib.loads(body)


def _libraries(body: bytes) -> list[Library]:
    """Read GET /api/Library/libraries' reply; unchecked."""
    return jsonlib.loads(body)


def _series_page(body: bytes) -> list[Series]:
    """Read a page of POST /api/Series/all-v2's reply; unchecked."""
    return jsonlib.loads(body)


def _series(body: bytes) -> Series:
    """Read GET /api/Series/{id}'s reply; unchecked."""
    return jsonlib.loads(body)


def _metadata(body: bytes) -> SeriesMetadata:
    """Read GET /api/Series/metadata's reply; unchecked."""
    return jsonlib.loads(body)


def _volumes(body: bytes) -> list[Volume]:
    """Read GET /api/Series/volumes' reply; unchecked."""
    return jsonlib.loads(body)
