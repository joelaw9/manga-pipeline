"""Kavita API client (SPEC "Services"): the token, the reads, the library scan request, path mapping and a series'
folder.

Endpoints as verified against Kavita v0.9.1.4 by the bulk converter's client (optimizer/kavita.py), which this is
ported from. 1 object per run: it keeps the token, the library folders and whether Kavita was found unreachable.
"""

import logging
import urllib.parse
from pathlib import Path

from manga_pipeline.clients import http

log = logging.getLogger(__name__)
PLUGIN = "manga-pipeline"
PAGE_SIZE = 1000
LIBRARY_FILTER_FIELD = 19  # all-v2 filter statement field: the series' library
SORT_BY_NAME = 1  # all-v2 sortOptions.sortField


class FolderError(Exception):
    """A series has no usable folder (SPEC "Services")."""


class Kavita:
    def __init__(self, url, api_key, path_map, service=None):
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
        """POST /api/Series/all-v2, filtered to 1 library and sorted by name (pages of an unsorted list can repeat
        or miss a series): every series DTO of it."""
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
        """GET /api/Series/{id}: the series DTO, or None when Kavita no longer holds it (404, or an empty reply for a
        series deleted since it was listed)."""
        try:
            reply = self._call("GET", f"/api/Series/{int(series_id)}")
        except http.HttpError as e:
            if e.status == 404:
                return None
            raise
        return reply.json() if reply.body.strip() else None

    def metadata(self, series_id):
        """GET /api/Series/metadata: the series' metadata (web links, summary, genres, tags, people, locks)."""
        return self._call("GET", f"/api/Series/metadata?seriesId={int(series_id)}").json()

    def volumes(self, series_id):
        """GET /api/Series/volumes: the series' volumes, each with its chapters, each with its files (Kavita paths)."""
        return self._call("GET", f"/api/Series/volumes?seriesId={int(series_id)}").json()

    def series_files(self, series_id):
        """Every file Kavita holds for the series: its local path, bytes, pages, file id, chapter id, volume id and
        the chapter's pagesRead (of the API key's user)."""
        return [
            {
                "path": self.local_path(file["filePath"]),
                "bytes": file["bytes"],
                "pages": file["pages"],
                "file_id": file["id"],
                "chapter_id": chapter["id"],
                "volume_id": volume["id"],
                "pages_read": chapter["pagesRead"],
            }
            for volume in self.volumes(series_id)
            for chapter in volume["chapters"]
            for file in chapter["files"]
        ]

    def series_folder(self, series_id):
        """The single folder holding the series' files, as a local path. FolderError when its files lie in no folder,
        in 2 or more, or directly in one of a library's own folders. Never Kavita's folderPath: for a series under a
        shared top folder that is the top folder."""
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
            raise FolderError(f"Kavita series {series_id} has its files in {len(folders)} folders ({found}), not 1")
        if self._library_folders is None:
            self._library_folders = {f.rstrip("/") for library in self.libraries() for f in library["folders"]}
        if folders[0].rstrip("/") in self._library_folders:
            raise FolderError(f"Kavita series {series_id} has its files directly in the library folder {folders[0]}")
        return self.local_path(folders[0])

    def local_path(self, kavita_path):
        """A path as Kavita reports it, as a local path: the longest prefix of paths.kavita_path_map that ends on a
        path boundary is replaced; case counts. ValueError for a path under no prefix."""
        for prefix, local in self._path_map:
            if kavita_path == prefix or kavita_path.startswith(prefix + "/"):
                return Path(local, *kavita_path[len(prefix) :].split("/"))
        raise ValueError(f"Kavita path {kavita_path} is under no prefix of paths.kavita_path_map")

    # --- scans: requested, never awaited

    def scan_library(self, library_id):
        """POST /api/Library/scan (not forced): Kavita's own library scan, which matches files to series by their
        ComicInfo Series against each series' names and keeps their ids. The pipeline's only scan: a series scan
        deletes a series whose files no longer parse back to it, and any scan requested while one runs does not work.
        (POST /api/Library/scan-folder is never used: it rewrites a series' folderPath to the top folder.)"""
        self._call("POST", f"/api/Library/scan?libraryId={int(library_id)}&force=false")

    # --- transport

    def _call(self, method, path, json=None):
        """1 authenticated call. A 401 gets 1 new token and 1 more try; a second 401 makes Kavita unreachable."""
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            try:
                return self._service.request(method, self.base + path, headers=headers, json=json)
            except http.HttpError as e:
                if e.status != 401:
                    raise
                self._token = None
                if attempt == 2:
                    raise self._refused("HTTP 401 after re-authentication") from None

    def _get_token(self):
        if self._token is None:
            query = urllib.parse.urlencode({"apiKey": self._api_key, "pluginName": PLUGIN})
            try:
                reply = self._service.request("POST", f"{self.base}/api/Plugin/authenticate?{query}")
            except http.HttpError as e:
                if e.status != 401:
                    raise
                raise self._refused("HTTP 401 from authenticate: the API key was refused") from None
            self._token = reply.json()["token"]
        return self._token

    def _refused(self, reason):
        log.warning("kavita: %s; Kavita is not asked again in this run", reason)  # Phase 8 adds the notification
        return self._service.mark_unreachable(reason)


def from_config(cfg):
    return Kavita(cfg["kavita"]["url"], cfg["kavita"]["api_key"], cfg["paths"]["kavita_path_map"])
