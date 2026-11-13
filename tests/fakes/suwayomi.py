"""FakeSuwayomi: the Suwayomi client with its narrow calls answered from memory and a temp downloads folder.

It models what the pipeline must cope with (local library `suwayomi-downloads.md`): a CBZ appears under its final
name before the chapter is flagged downloaded; the flag changes only through the API, so a file removed from the
folder directly stays flagged; enqueueing a flagged chapter whose file is gone downloads it again; a finished
download leaves the queue, a failed one stays in it as ERROR; a chapter new on the source is listed only once the
manga's chapter list is fetched.
"""

import copy
import io
import zipfile
import zlib
from pathlib import Path

from PIL import Image

from manga_pipeline.clients import suwayomi

from tests.fakes.http import NoNetwork


def page(number, seed=""):
    """A small real page image (PNG bytes, 16 x 24) of 1 colour, set by its number and seed: no junk unit (it has no
    signature), not white, and other number or seed = other bytes."""
    shade = 40 + (zlib.crc32(f"{number} {seed}".encode()) % 160)
    out = io.BytesIO()
    Image.new("RGB", (16, 24), (shade, shade, 255 - shade)).save(out, "PNG")
    return out.getvalue()


def cbz(pages=3, comicinfo=None):
    """The bytes of a small CBZ: `pages` small page images (page) and, when given, a ComicInfo.xml."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for number in range(1, pages + 1):
            archive.writestr(f"{number:03}.png", page(number))
        if comicinfo is not None:
            archive.writestr("ComicInfo.xml", comicinfo)
    return data.getvalue()


class FakeSuwayomi(suwayomi.Suwayomi):
    def __init__(self, mangas_root):
        """mangas_root: the folder that holds the source folders (Suwayomi's <downloads>/mangas)."""
        super().__init__("http://suwayomi.invalid", NoNetwork("suwayomi"))
        self.mangas_root = Path(mangas_root)
        self.source_rows, self.manga_rows, self.chapter_rows = {}, {}, {}
        self.unlisted = {}  # chapter rows the source has and Suwayomi has not listed yet
        self.queue = []  # [{state, tries, progress, chapter id}], in queue order
        self.downloader = "STOPPED"
        self.unavailable = set()  # ids of the sources whose site is down
        self.writes = []  # every write call, in order
        self._next_id = 100

    # --- building state

    def add_source(self, name, lang="en"):
        source_id = str(self._new_id())
        display = f"{name} ({lang.upper()})"
        self.source_rows[source_id] = {"id": source_id, "name": name, "lang": lang, "displayName": display}
        return source_id

    def add_manga(self, title, source_id, in_library=True, status="ONGOING"):
        manga_id = self._new_id()
        self.manga_rows[manga_id] = {
            "id": manga_id,
            "title": title,
            "status": status,
            "url": f"/manga/{manga_id}",
            "inLibrary": in_library,
            "sourceId": source_id,
        }
        return manga_id

    def add_chapter(self, manga_id, name, scanlator=None, number=None, listed=True):
        """A chapter of the manga, not downloaded. number defaults to its place in the manga. listed=False: the
        source has it, and Suwayomi lists it only after fetch_manga_and_chapters."""
        chapter_id = self._new_id()
        rows = [*self.chapter_rows.values(), *self.unlisted.values()]
        order = 1 + sum(c["mangaId"] == manga_id for c in rows)
        (self.chapter_rows if listed else self.unlisted)[chapter_id] = {
            "id": chapter_id,
            "mangaId": manga_id,
            "name": name,
            "scanlator": scanlator,
            "chapterNumber": float(order if number is None else number),
            "sourceOrder": order,
            "isDownloaded": False,
            "pageCount": -1,
            "uploadDate": "1700000000000",
            "url": f"/chapter/{chapter_id}",
            "realUrl": f"https://source.invalid/chapter/{chapter_id}",
        }
        return chapter_id

    def path(self, chapter_id):
        """Where Suwayomi writes the chapter's CBZ: built by the client's own 3 name helpers."""
        chapter = self.chapter_rows[chapter_id]
        manga = self.manga_rows[chapter["mangaId"]]
        return (
            self.mangas_root
            / suwayomi.source_folder(self.source_rows[manga["sourceId"]]["displayName"])
            / suwayomi.manga_folder(manga["title"])
            / suwayomi.chapter_file(chapter["scanlator"], chapter["name"])
        )

    # --- what Suwayomi's downloader does between 2 runs of the pipeline

    def begin_download(self, chapter_id, data=b"PK half-written"):
        """The downloader is writing the chapter: the CBZ is visible, incomplete, and the chapter is not flagged."""
        self._entry(chapter_id)["state"] = "DOWNLOADING"
        self._write(chapter_id, data)

    def finish_downloads(self, data=None):
        """Every queued or downloading chapter is written (data, or a small CBZ), flagged and leaves the queue."""
        for entry in [e for e in self.queue if e["state"] in ("QUEUED", "DOWNLOADING")]:
            self._write(entry["chapter"], cbz() if data is None else data)
            self.chapter_rows[entry["chapter"]]["isDownloaded"] = True
            self.queue.remove(entry)

    def fail_download(self, chapter_id, tries=3):
        """The download failed: it stays in the queue as ERROR and nothing is flagged."""
        self._entry(chapter_id).update(state="ERROR", tries=tries)

    # --- the client's calls

    def ping(self):
        pass

    def library_size(self):
        return sum(m["inLibrary"] for m in self.manga_rows.values())

    def library_manga(self):
        for manga_id in sorted(self.manga_rows):
            if self.manga_rows[manga_id]["inLibrary"]:
                yield self._manga(manga_id)

    def manga(self, manga_id):
        return self._manga(manga_id) if manga_id in self.manga_rows else None

    def chapters(self, manga_id):
        rows = [c for c in self.chapter_rows.values() if c["mangaId"] == manga_id]
        return copy.deepcopy(sorted(rows, key=lambda c: c["sourceOrder"]))

    def chapter(self, chapter_id):
        row = self.chapter_rows.get(chapter_id)
        return copy.deepcopy(row)

    def download_queue(self):
        queue = [
            {
                "state": e["state"],
                "tries": e["tries"],
                "progress": e["progress"],
                "position": position,
                "chapter": {"id": e["chapter"], "mangaId": self.chapter_rows[e["chapter"]]["mangaId"]},
            }
            for position, e in enumerate(self.queue)
        ]
        return {"state": self.downloader, "queue": queue}

    def sources(self):
        return copy.deepcopy(list(self.source_rows.values()))

    def enqueue(self, chapter_ids):
        """As Suwayomi: a chapter already queued is skipped; so is one flagged with its file in place; one flagged
        whose file is gone is queued again."""
        self.writes.append(("enqueue", list(chapter_ids)))
        for chapter_id in chapter_ids:
            queued = any(e["chapter"] == chapter_id for e in self.queue)
            in_place = self.chapter_rows[chapter_id]["isDownloaded"] and self.path(chapter_id).exists()
            if not queued and not in_place:
                self.queue.append({"state": "QUEUED", "tries": 0, "progress": 0.0, "chapter": chapter_id})

    def delete_downloaded(self, chapter_ids):
        """As Suwayomi: the file goes (a missing one is no error), emptied folders go, the flag is cleared."""
        self.writes.append(("delete_downloaded", list(chapter_ids)))
        for chapter_id in chapter_ids:
            path = self.path(chapter_id)
            path.unlink(missing_ok=True)
            for folder in (path.parent, path.parent.parent):
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            self.chapter_rows[chapter_id]["isDownloaded"] = False

    def dequeue(self, chapter_ids):
        """As Suwayomi: the chapters leave the queue; one not queued is no error."""
        self.writes.append(("dequeue", list(chapter_ids)))
        self.queue = [e for e in self.queue if e["chapter"] not in chapter_ids]

    def start_downloader(self):
        self.writes.append(("start_downloader",))
        self.downloader = "STARTED"

    def search_source(self, source_id, title):
        """The source's manga whose title contains `title`, whatever the case."""
        self.writes.append(("search_source", str(source_id), title))
        self._check_source(str(source_id))
        rows = self.manga_rows.values()
        hits = [m["id"] for m in rows if m["sourceId"] == str(source_id) and title.casefold() in m["title"].casefold()]
        return [self._manga(manga_id) for manga_id in sorted(hits)]

    def fetch_manga_and_chapters(self, manga_id):
        self.writes.append(("fetch_manga_and_chapters", manga_id))
        self._check_source(self.manga_rows[manga_id]["sourceId"])
        for chapter_id in [i for i, c in self.unlisted.items() if c["mangaId"] == manga_id]:
            self.chapter_rows[chapter_id] = self.unlisted.pop(chapter_id)
        return {"manga": self._manga(manga_id), "chapters": self.chapters(manga_id)}

    # --- internals

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def _manga(self, manga_id):
        row = self.manga_rows[manga_id]
        return copy.deepcopy({**row, "source": self.source_rows.get(row["sourceId"])})

    def _check_source(self, source_id):
        if source_id in self.unavailable:
            raise suwayomi.SourceUnavailable(f"No route to host (source {source_id})")

    def _entry(self, chapter_id):
        for entry in self.queue:
            if entry["chapter"] == chapter_id:
                return entry
        raise AssertionError(f"FakeSuwayomi: chapter {chapter_id} is not in the download queue")

    def _write(self, chapter_id, data):
        path = self.path(chapter_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
