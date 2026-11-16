"""FakeKavita: the Kavita client with its narrow calls answered from memory, and scans that index a temp library.

It models what the pipeline must cope with (local library `kavita-scanning.md`):
- a scan matches a file to a series by the file's ComicInfo Series (or LocalizedSeries) against the series' names,
  with Kavita's own normalising; a file that matches none of a series' names is not indexed into it, it becomes or
  joins another series;
- a series left with no file is deleted, so a series whose files all carry another name comes back under a new id;
- a scan is only requested: nothing changes until Kavita runs it (here: until the test calls run_scans());
- lowestFolderPath is the common parent of the series' files (the library root for a series spanning 2 top folders);
  folderPath is the top folder under the library root, shared by every series below it;
- with `stale_sizes` set, a scan keeps the old size of a file replaced under the same name;
- a file replaced under the same name keeps its chapter, file id and reading progress only while its ComicInfo Volume
  is unchanged; a changed volume, like a new name, gets a new chapter with no progress; every file a scan re-reads
  (new, or modified since) gets a later chapter lastModifiedUtc (measured 2026-11-15: local library
  kavita-scanning.md);
- reading progress is written for the API key's user: save_progress, mark_read;
- refresh_covers is recorded in writes (cover images are not modelled).

Simpler than Kavita: every file is its own chapter in 1 volume per series, and a file with no ComicInfo Series takes
its folder's name (Kavita parses the file name first).
"""

import copy
import os
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from manga_pipeline.clients import kavita
from manga_pipeline.clients.kavita_match import normalize, series_names

from tests.fakes.http import NoNetwork

IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")


class FakeKavita(kavita.Kavita):
    def __init__(self, libraries, path_map):
        """libraries: {library id: its folder as Kavita sees it}; path_map: Kavita prefix to local prefix, so each
        library folder is a real (temp) folder."""
        super().__init__("http://kavita.invalid", "fake-key", path_map, NoNetwork("kavita"))
        self.library_folders = {library_id: folder.rstrip("/") for library_id, folder in libraries.items()}
        self.series_rows = {}  # series id: DTO
        self.metadata_rows = {}  # series id: series metadata (webLinks, ...)
        self.covers = {}  # series id: the cover image uploaded
        self.files = {}  # Kavita file path: {series, id, chapter, bytes, pages, pagesRead}
        self.stale_sizes = False
        self.writes = []  # every write call (scan requests, metadata, series, cover), in order
        self.pending = []  # scan requests Kavita has not run yet
        self._next_id = 1000
        self._clock = 0  # Kavita's own clock: lastModifiedUtc values

    # --- building state

    def add_series(self, name, folder, library_id=None, localized="", original=None):
        """A series Kavita already holds, with every CBZ now in <library folder>/<folder> indexed into it, whatever
        the files say. folder may be nested ("Local/Name"). Returns the series id."""
        library_id = next(iter(self.library_folders)) if library_id is None else library_id
        series_id = self._add(library_id, name, f"{self.library_folders[library_id]}/{folder}", localized, original)
        for path in self._cbz_in(self.series_rows[series_id]["lowestFolderPath"]):
            self._index(series_id, path, {})
        return series_id

    # --- the client's calls

    def libraries(self):
        return [{"id": i, "name": f"Library {i}", "folders": [f]} for i, f in self.library_folders.items()]

    def series_in_library(self, library_id):
        return copy.deepcopy([s for s in self.series_rows.values() if s["libraryId"] == library_id])

    def series(self, series_id):
        return copy.deepcopy(self.series_rows.get(series_id))

    def metadata(self, series_id):
        return copy.deepcopy(self.metadata_rows.setdefault(series_id, {"seriesId": series_id, "webLinks": ""}))

    def update_metadata(self, metadata):
        """As Kavita: the whole metadata replaced; a list entry's id 0 gets an id."""
        metadata = copy.deepcopy(metadata)
        for value in metadata.values():
            for entry in value if isinstance(value, list) else []:
                if isinstance(entry, dict) and entry.get("id") == 0:
                    entry["id"] = self._new_id()
        self.writes.append(("metadata", metadata["seriesId"]))
        self.metadata_rows[metadata["seriesId"]] = metadata

    def update_series(self, series, **changes):
        self.writes.append(("series", series["id"], dict(changes)))
        self.series_rows[series["id"]].update(changes)

    def upload_cover(self, series_id, image):
        self.writes.append(("cover", series_id))
        self.series_rows[series_id]["coverImage"] = f"series{series_id}.png"
        self.covers[series_id] = image

    def has_uploaded_cover(self, series_id):
        return self.series_rows[series_id].get("coverImage", "").startswith(f"series{series_id}.")

    def volumes(self, series_id):
        files = {path: f for path, f in sorted(self.files.items()) if f["series"] == series_id}
        if not files:
            return []
        volume_id = series_id * 10
        chapters = [
            {
                "id": f["chapter"],
                "volumeId": volume_id,
                "pages": f["pages"],
                "pagesRead": f["pagesRead"],
                "lastModifiedUtc": f["modified"],
                "files": [{"id": f["id"], "filePath": path, "bytes": f["bytes"], "pages": f["pages"]}],
            }
            for path, f in files.items()
        ]
        return [{"id": volume_id, "chapters": chapters}]

    def scan_library(self, library_id):
        self.writes.append(("scan_library", library_id))
        self.pending.append(library_id)

    def save_progress(self, _series_id, _volume_id, chapter_id, page):
        self.writes.append(("progress", chapter_id, page))
        file = next(f for f in self.files.values() if f["chapter"] == chapter_id)
        file["pagesRead"] = min(page, file["pages"])

    def mark_read(self, _series_id, chapter_ids):
        self.writes.append(("read", tuple(chapter_ids)))
        for file in self.files.values():
            if file["chapter"] in chapter_ids:
                file["pagesRead"] = file["pages"]

    def refresh_covers(self, library_id, series_id):
        self.writes.append(("refresh_covers", library_id, series_id))

    # --- the scan model

    def run_scans(self):
        """Kavita runs the library scans requested so far, in order. Until then a request has changed nothing."""
        requests, self.pending = self.pending, []
        for library_id in requests:
            self._scan(library_id)

    def _scan(self, library_id):
        """Index again every CBZ in the library, each into the series its name matches;
        then delete every series of the library left with no file, and move the others' folders to where their files
        are."""
        top = self.library_folders[library_id]
        on_disk = [self._kavita_path(folder) for folder, _, _ in os.walk(self.local_path(top))]
        indexed = [p.rpartition("/")[0] for p in self.files if p.startswith(top + "/")]
        for folder in sorted({*on_disk, *indexed}):
            before = {path: self.files.pop(path) for path in [*self.files] if path.rpartition("/")[0] == folder}
            for path in self._cbz_in(folder):
                name, localized = _names_in(self.local_path(path), folder.rpartition("/")[2])
                series_id = self._series_named(library_id, name, localized)
                if series_id is None:
                    series_id = self._add(library_id, name, folder, localized or "", None)
                self._index(series_id, path, before)
        folders = {}
        for path, file in self.files.items():
            folders.setdefault(file["series"], set()).add(path.rpartition("/")[0])
        for series_id in [i for i, s in self.series_rows.items() if s["libraryId"] == library_id]:
            if series_id not in folders:
                del self.series_rows[series_id]
            else:
                lowest = posixpath.commonpath(sorted(folders[series_id]))
                self.series_rows[series_id].update(self._folder_fields(library_id, lowest))

    def _series_named(self, library_id, name, localized):
        wanted = {normalize(n) for n in (name, localized) if n}
        for series_id, row in self.series_rows.items():
            if row["libraryId"] == library_id and wanted & {normalize(n) for n in series_names(row)}:
                return series_id
        return None

    def _index(self, series_id, path, before):
        """Index 1 file into a series. A file the series held before the scan under the same volume keeps its ids and
        reading progress, and with stale_sizes its old size; a re-read file's chapter gets a later lastModifiedUtc."""
        local = self.local_path(path)
        try:
            with zipfile.ZipFile(local) as archive:
                pages = sum(name.lower().endswith(IMAGES) for name in archive.namelist())
        except zipfile.BadZipFile:
            return  # Kavita lists it as a media error and indexes nothing
        old = before.get(path)
        volume = _volume_in(local)
        kept = old if old is not None and old["series"] == series_id and old["volume"] == volume else None
        mtime = local.stat().st_mtime_ns
        reread = kept is None or kept["mtime"] != mtime
        self._clock += reread
        self.files[path] = {
            "series": series_id,
            "id": kept["id"] if kept is not None else self._new_id(),
            "chapter": kept["chapter"] if kept is not None else self._new_id(),
            "bytes": kept["bytes"] if kept is not None and self.stale_sizes else local.stat().st_size,
            "pages": pages,
            "pagesRead": kept["pagesRead"] if kept is not None else 0,
            "volume": volume,
            "mtime": mtime,
            "modified": self._stamp() if reread or kept is None else kept["modified"],
        }

    def _add(self, library_id, name, folder, localized, original):
        series_id = self._new_id()
        self.series_rows[series_id] = {
            "id": series_id,
            "name": name,
            "localizedName": localized,
            "originalName": original or name,
            "sortName": name,
            "libraryId": library_id,
            **self._folder_fields(library_id, folder),
        }
        return series_id

    def _folder_fields(self, library_id, lowest):
        """folderPath and lowestFolderPath of a series whose files' common parent is `lowest`."""
        root = self.library_folders[library_id]
        top = lowest[len(root) + 1 :].split("/")[0]
        return {"folderPath": f"{root}/{top}" if top else root, "lowestFolderPath": lowest}

    def _cbz_in(self, folder):
        """Kavita paths of the CBZ files directly in a Kavita folder, sorted; none when the folder is missing."""
        local = self.local_path(folder)
        if not local.is_dir():
            return []
        return sorted(f"{folder}/{e.name}" for e in os.scandir(local) if e.is_file() and e.name.endswith(".cbz"))

    def _kavita_path(self, local):
        """A local folder as Kavita sees it (the reverse of local_path, for folders under a library root)."""
        for root in self.library_folders.values():
            local_root = self.local_path(root)
            if os.path.commonpath([local_root, local]) == str(local_root):
                rest = os.path.relpath(local, local_root).replace(os.sep, "/")
                return root if rest == "." else f"{root}/{rest}"
        msg = f"FakeKavita: {local} is under no library folder"
        raise AssertionError(msg)

    def _stamp(self):
        """Kavita's lastModifiedUtc now: 7 fractional digits, as Kavita writes it; 1 second a re-read."""
        c = self._clock
        return f"2026-01-01T{c // 3600:02d}:{c // 60 % 60:02d}:{c % 60:02d}.0000000"

    def _new_id(self):
        self._next_id += 1
        return self._next_id


def _volume_in(local):
    """The ComicInfo Volume a scan reads from a CBZ, or None."""
    try:
        with zipfile.ZipFile(local) as archive:
            return (ET.fromstring(archive.read("ComicInfo.xml")).findtext("Volume") or "").strip() or None
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return None


def _names_in(local, folder_name):
    """(Series, LocalizedSeries) a scan reads from a CBZ: its ComicInfo.xml values, the folder's name for Series when
    the file has none."""
    try:
        with zipfile.ZipFile(local) as archive:
            root = ET.fromstring(archive.read("ComicInfo.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return folder_name, None
    return (root.findtext("Series") or "").strip() or folder_name, (root.findtext("LocalizedSeries") or "").strip()
