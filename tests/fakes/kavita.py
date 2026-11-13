"""FakeKavita: the Kavita client with its narrow calls answered from memory, and scans that index a temp library.

It models what the pipeline must cope with (local library `kavita-scanning.md`):
- a scan matches a file to a series by the file's ComicInfo Series (or LocalizedSeries) against the series' names,
  with Kavita's own normalising; a file that matches none of a series' names is not indexed into it, it becomes or
  joins another series;
- a series left with no file is deleted, so a series whose files all carry another name comes back under a new id;
- a scan is only requested: nothing changes until Kavita runs it (here: until the test calls run_scans());
- a series scan covers the series' lowestFolderPath and everything below it, and deletes the series when that
  folder is gone;
- lowestFolderPath is the common parent of the series' files (the library root for a series spanning 2 top folders);
  folderPath is the top folder under the library root, shared by every series below it;
- with `stale_sizes` set, a scan keeps the old size of a file replaced under the same name.

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
        self.files = {}  # Kavita file path: {series, id, chapter, bytes, pages, pagesRead}
        self.stale_sizes = False
        self.writes = []  # every scan request, in order
        self.pending = []  # scan requests Kavita has not run yet
        self._next_id = 1000

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
                "files": [{"id": f["id"], "filePath": path, "bytes": f["bytes"], "pages": f["pages"]}],
            }
            for path, f in files.items()
        ]
        return [{"id": volume_id, "chapters": chapters}]

    def scan_series(self, library_id, series_id):
        self.writes.append(("scan_series", library_id, series_id))
        self.pending.append(("scan_series", library_id, series_id))

    def scan_library(self, library_id):
        self.writes.append(("scan_library", library_id))
        self.pending.append(("scan_library", library_id, None))

    # --- the scan model

    def run_scans(self):
        """Kavita runs the scans requested so far, in order. Until then a request has changed nothing."""
        requests, self.pending = self.pending, []
        for kind, library_id, series_id in requests:
            if kind == "scan_library":
                self._scan(library_id, self.library_folders[library_id])
            elif series_id in self.series_rows:
                self._scan(library_id, self.series_rows[series_id]["lowestFolderPath"])

    def _scan(self, library_id, top):
        """Index again every CBZ in the Kavita folder `top` and below it, each into the series its name matches;
        then delete every series of the library left with no file, and move the others' folders to where their files
        are."""
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
        """Index 1 file into a series. A file the series held before the scan keeps its ids and reading progress,
        and with stale_sizes its old size."""
        local = self.local_path(path)
        try:
            with zipfile.ZipFile(local) as archive:
                pages = sum(name.lower().endswith(IMAGES) for name in archive.namelist())
        except zipfile.BadZipFile:
            return  # Kavita lists it as a media error and indexes nothing
        old = before.get(path)
        kept = old is not None and old["series"] == series_id
        self.files[path] = {
            "series": series_id,
            "id": old["id"] if kept else self._new_id(),
            "chapter": old["chapter"] if kept else self._new_id(),
            "bytes": old["bytes"] if kept and self.stale_sizes else local.stat().st_size,
            "pages": pages,
            "pagesRead": old["pagesRead"] if kept else 0,
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
        raise AssertionError(f"FakeKavita: {local} is under no library folder")

    def _new_id(self):
        self._next_id += 1
        return self._next_id


def _names_in(local, folder_name):
    """(Series, LocalizedSeries) a scan reads from a CBZ: its ComicInfo.xml values, the folder's name for Series when
    the file has none."""
    try:
        with zipfile.ZipFile(local) as archive:
            root = ET.fromstring(archive.read("ComicInfo.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return folder_name, None
    return (root.findtext("Series") or "").strip() or folder_name, (root.findtext("LocalizedSeries") or "").strip()
