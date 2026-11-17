"""Kavita API client (SPEC "Services").

The token, the reads, the library scan request, path mapping and a series' folder.

Endpoints as verified against Kavita v0.9.1.4 by the bulk converter's client (optimizer/kavita.py), which this is
ported from. 1 object per run: it keeps the token, the library folders and whether Kavita was found unreachable.
"""

import base64
import logging
import urllib.parse
from http import HTTPStatus
from pathlib import Path

from manga_pipeline.clients import http

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


class CoverError(Exception):
    """Kavita does not serve a cover it accepted."""


class FolderError(Exception):
    """A series has no usable folder (SPEC "Services")."""


class Kavita:
    """A run's Kavita client: it keeps the token, the library folders and the path map.

    Its http.Service remembers whether Kavita was found unreachable.
    """

    def __init__(self, url, api_key, path_map, service=None):
        """Store the server's URL, the API key and the path map (longest Kavita prefix first); nothing is asked yet."""
        self.base = url.rstrip("/")
        self._api_key = api_key
        # Longest Kavita prefix first, so the most specific entry wins.
        self._path_map = sorted(((k.rstrip("/"), v) for k, v in path_map.items()), key=lambda kv: -len(kv[0]))
        self._service = service or http.Service("kavita")
        self._token = None
        self._library_folders = None

    # --- reads

    def libraries(self):
        """GET /api/Library/libraries: every library with its id, name and folders (Kavita paths)."""
        return self._call("GET", "/api/Library/libraries").json()

    def series_in_library(self, library_id):
        """Return every series DTO of 1 library (POST /api/Series/all-v2).

        Filtered to the library and sorted by name (pages of an unsorted list can repeat or miss a series).
        """
        body = {
            "statements": [{"comparison": 0, "field": LIBRARY_FILTER_FIELD, "value": str(library_id)}],
            "combination": 1,
            "limitTo": 0,
            "sortOptions": {"sortField": SORT_BY_NAME, "isAscending": True},
        }
        out, page = [], 1
        while True:
            batch = self._call("POST", f"/api/Series/all-v2?PageNumber={page}&PageSize={PAGE_SIZE}", body).json()
            out += batch
            if len(batch) < PAGE_SIZE:
                return out
            page += 1

    def series(self, series_id):
        """GET /api/Series/{id}: the series DTO, or None when Kavita no longer holds it.

        None for a 404, or an empty reply for a series deleted since it was listed.
        """
        try:
            reply = self._call("GET", f"/api/Series/{int(series_id)}")
        except http.HttpError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                return None
            raise
        return reply.json() if reply.body.strip() else None

    def metadata(self, series_id):
        """GET /api/Series/metadata: the series' metadata (web links, summary, genres, tags, people, locks)."""
        return self._call("GET", f"/api/Series/metadata?seriesId={int(series_id)}").json()

    def update_metadata(self, metadata):
        """POST /api/Series/metadata: Kavita applies the whole series metadata as sent (read it fresh first)."""
        self._call("POST", "/api/Series/metadata", {"seriesMetadata": metadata})

    def update_series(self, series, **changes):
        """POST /api/Series/update: the series as just read, resent whole with the changes.

        Kavita applies every field it is sent and resets those left out (an omitted aniListId becomes 0, an omitted
        coverImageLocked unlocks the cover).
        """
        body = {name: series[name] for name in UPDATE_SERIES_FIELDS if name in series}
        body.update(changes, id=series["id"])
        self._call("POST", "/api/Series/update", body)

    def upload_cover(self, series_id, image):
        """POST /api/Upload/series, then check Kavita serves it.

        Kavita has answered 200 while failing to write the file. CoverError when Kavita does not serve it.
        """
        self._call("POST", "/api/Upload/series", {"id": int(series_id), "url": base64.b64encode(image).decode()})
        if not self.has_uploaded_cover(series_id):
            msg = f"Kavita accepted the cover of series {series_id} but does not serve it"
            raise CoverError(msg)

    def has_uploaded_cover(self, series_id):
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

    def volumes(self, series_id):
        """GET /api/Series/volumes: the series' volumes, each with its chapters, each with its files (Kavita paths)."""
        return self._call("GET", f"/api/Series/volumes?seriesId={int(series_id)}").json()

    def series_files(self, series_id):
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

    def save_progress(self, series_id, volume_id, chapter_id, page):
        """Record that the API key's user has read `page` pages of the chapter (POST /api/reader/progress).

        Kavita caps it at the chapter's page count.
        """
        series = self.series(series_id)
        if series is None:
            msg = f"Kavita series {series_id} is gone: progress not saved"
            raise ValueError(msg)
        body = {
            "volumeId": volume_id,
            "chapterId": chapter_id,
            "pageNum": page,
            "seriesId": series_id,
            "libraryId": series["libraryId"],
            "bookScrollId": None,
        }
        self._call("POST", "/api/reader/progress", json=body)

    def mark_read(self, series_id, chapter_ids):
        """Record that the API key's user has read the chapters whole (POST /api/reader/mark-multiple-read)."""
        body = {
            "seriesId": series_id,
            "volumeIds": [],
            "chapterIds": list(chapter_ids),
            "generateReadingSession": False,
        }
        self._call("POST", "/api/reader/mark-multiple-read", json=body)

    def refresh_covers(self, library_id, series_id):
        """Have Kavita regenerate the series' chapter and volume cover images (POST /api/Series/refresh-metadata).

        Forced: a library scan keeps a re-read chapter's old one. The series' locked cover stays.
        """
        body = {"libraryId": library_id, "seriesId": series_id, "forceUpdate": True}
        self._call("POST", "/api/Series/refresh-metadata", json=body)

    def series_folder(self, series_id):
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

    def local_path(self, kavita_path):
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

    def scan_library(self, library_id):
        """POST /api/Library/scan (not forced): Kavita's own library scan.

        It matches files to series by their ComicInfo Series against each series' names and keeps their ids. The
        pipeline's only scan: a series scan deletes a series whose files no longer parse back to it, and any scan
        requested while one runs does not work. (POST /api/Library/scan-folder is never used: it rewrites a series'
        folderPath to the top folder.)
        """
        self._call("POST", f"/api/Library/scan?libraryId={int(library_id)}&force=false")

    # --- transport

    def _call(self, method, path, json=None):
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

    def _get_token(self):
        if self._token is None:
            query = urllib.parse.urlencode({"apiKey": self._api_key, "pluginName": PLUGIN})
            try:
                reply = self._service.request("POST", f"{self.base}/api/Plugin/authenticate?{query}")
            except http.HttpError as e:
                if e.status != HTTPStatus.UNAUTHORIZED:
                    raise
                msg = "HTTP 401 from authenticate: the API key was refused"
                raise self._refused(msg) from None
            self._token = reply.json()["token"]
        return self._token

    def _refused(self, reason):
        log.warning("kavita: %s; Kavita is not asked again in this run", reason)  # Phase 8 adds the notification
        return self._service.mark_unreachable(reason)


def from_config(cfg):
    """Build the Kavita client from the kavita.url, kavita.api_key and paths.kavita_path_map settings."""
    return Kavita(cfg["kavita"]["url"], cfg["kavita"]["api_key"], cfg["paths"]["kavita_path_map"])
