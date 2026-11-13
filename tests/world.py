"""A test world for the passthrough: an adopted site, a FakeSuwayomi over its downloads, a FakeKavita over its library,
and builders for bulk-converted series, Suwayomi manga and complete downloads."""

import hashlib
import io
import json
import zipfile
import zlib
from contextlib import closing

import numpy as np
from PIL import Image

from manga_pipeline import archive, config, db, workflow
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.junk import review

from tests.builders import adopted
from tests.fakes.kavita import FakeKavita
from tests.fakes.suwayomi import FakeSuwayomi
from tests.fakes.suwayomi import page as fake_page

LIBRARY_ID = 4  # tests/test_config.yaml kavita.library_id


def credit_page(seed=""):
    """A noisy page wider than fake pages, but no spread (PNG bytes): in a page-format chapter it is a junk candidate
    by its size, so the chapter waits on junk_decision until the candidate is decided."""
    rng = np.random.default_rng(zlib.crc32(f"credit {seed}".encode()))
    out = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (24, 28, 3), dtype=np.uint8)).save(out, "PNG")
    return out.getvalue()


def cbz(pages=3, comicinfo=None, seed="", credit=False):
    """A small CBZ: `pages` small page images whose colour carries seed (other seed = other content), then a credit
    page when `credit` (credit_page: the same in every chapter), and a ComicInfo.xml when given (bytes or str)."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        for n in range(1, pages + 1):
            z.writestr(f"{n:03}.png", fake_page(n, seed))
        if credit:
            z.writestr(f"{pages + 1:03}.png", credit_page())
        if comicinfo is not None:
            z.writestr("ComicInfo.xml", comicinfo)
    return data.getvalue()


def comicinfo(**fields):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f'<?xml version="1.0" encoding="utf-8"?><ComicInfo>{body}</ComicInfo>'


# A record's calibration as the bulk converter writes it: the colour group probed (fixed at q75), the gray group left
# unprobed (no gray page sampled), so a series of colour test pages converts with no calibration probes.
CALIBRATION = {
    "groups": {
        "gray": {"q": 80, "n": 0, "why": "too few sample pages (0)", "probes": [], "excluded": []},
        "colour": {
            "q": 75,
            "n": 40,
            "why": "q70 fails: mean 84.1 < 85",
            "probes": [{"q": 75, "n": 40, "counted": 40, "mean": 86.0, "min": 81.0}],
            "excluded": [],
        },
    }
}


def bulk_record(series_name, kavita_id, chapters, layout="pages", basis="none"):
    """A valid finished .optimizer.json: chapters = [(output, source file, number, source bytes)]; basis: its
    series.naming.basis (where a new chapter's volume comes from)."""
    sources, rows = [], []
    for output, file, number, data in chapters:
        digest = hashlib.sha256("\n".join(archive.sha1(b) for _, b in _pages_of(data)).encode()).hexdigest()
        sources.append(
            {
                "file": file,
                "sha256": hashlib.sha256(data).hexdigest(),
                "pages_digest": digest,
                "fate": "chapter",
                "output": output,
            }
        )
        rows.append(
            {
                "output": output,
                "file": file,
                "sources": [file],
                "volume": None,
                "number": number,
                "title": "",
                "pages_out": len(_pages_of(data)),
            }
        )
    return {
        "schema": 4, "stages": ["structure", "image", "naming"], "kavita": {"series_id": kavita_id},
        "folders": [{"name": series_name, "from_run": "r1"}], "runs": [{"run_id": "r1", "written": "2026-11-01"}],
        "errors": [], "flags": [],
        "series": {"layout": {"layout": layout, "page_aspect": [1.4]}, "naming": {"series_name": series_name,
                   "restart": False, "basis": basis}, "calibration": CALIBRATION, "covers": {},
                   "decisions": {"checks": [], "templates": {}, "frames": {},
                                 "rows": {"junk": [], "frames": [], "marks": []}}},
        "sources": sources, "chapters": rows,
    }  # fmt: skip


def _pages_of(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [(n, z.read(n)) for n in archive.archive_pages(z.namelist())]


class World:
    def __init__(self, tmp_path, **changes):
        self.cfg = adopted(tmp_path, **changes)
        self.share = tmp_path / "share"
        self.library = config.abs_path(self.cfg["paths"]["library"])
        self.mangas = config.abs_path(self.cfg["paths"]["downloads"]) / suwayomi_client.MANGAS_FOLDER
        self.suwayomi = FakeSuwayomi(self.mangas)
        self.kavita = FakeKavita({LIBRARY_ID: "/kavita/library"}, self.cfg["paths"]["kavita_path_map"])
        self.source = self.suwayomi.add_source("Src")
        self.kavita_specs = []  # [(id, name, folder, add_series keywords)]: rebuilds the FakeKavita in another process

    def context(self, conn, part):
        return workflow.Context(self.cfg, conn, part, suwayomi=self.suwayomi, kavita=self.kavita)

    def run(self, part_module):
        """Run a part (intake, process or export module) as its run() does, with the world's fakes."""
        with closing(db.connect(self.cfg)) as conn:
            workflow.run_part(self.context(conn, part_module.__name__.rsplit(".", 1)[1]), part_module.work)

    def kavita_series(
        self,
        name,
        folder=None,
        record=True,
        bulk=(("Ch.1.cbz", "Grp_Chapter 1", "1"),),
        basis="none",
        layout="pages",
        **names,
    ):
        """A series Kavita holds in <library>/<folder or name> with bulk-converted chapters [(output, source file
        stem, number)] and, with record, its .optimizer.json. Returns the Kavita series id."""
        folder = folder or name
        path = self.library / folder
        path.mkdir(parents=True, exist_ok=True)
        chapters = []
        for output, stem, number in bulk:
            data = cbz(seed=stem)
            (path / output).write_bytes(data)
            chapters.append((output, f"{stem}.cbz", number, data))
        series_id = self.kavita.add_series(name, folder, LIBRARY_ID, **names)
        self.kavita_specs.append((series_id, name, folder, names))
        if record:
            self.write_record(name, series_id, folder, chapters, basis, layout)
        return series_id

    def write_record(self, name, series_id, folder, chapters, basis="none", layout="pages"):
        (self.library / folder / ".optimizer.json").write_text(
            json.dumps(bulk_record(name, series_id, chapters, layout=layout, basis=basis)), encoding="utf-8"
        )

    def manga(self, title):
        return self.suwayomi.add_manga(title, self.source)

    def download(self, manga_id, name, scanlator=None, data=None, flagged=True):
        """A chapter of the manga whose CBZ is in the downloads folder, flagged downloaded unless flagged=False.
        Returns (Suwayomi chapter id, path)."""
        chapter_id = self.suwayomi.add_chapter(manga_id, name, scanlator)
        path = self.suwayomi.path(chapter_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cbz(seed=name) if data is None else data)
        self.suwayomi.chapter_rows[chapter_id]["isDownloaded"] = flagged
        return chapter_id, path

    def rows(self, sql, *params):
        with closing(db.connect(self.cfg)) as conn:
            return [tuple(r) for r in conn.execute(sql, params)]

    def dump(self, path):
        """The world as JSON for tests/world_run.py: Suwayomi's rows and the Kavita series to rebuild."""
        s = self.suwayomi
        rows = {
            "sources": s.source_rows,
            "mangas": list(s.manga_rows.values()),
            "chapters": list(s.chapter_rows.values()),
        }
        world = {"mangas_root": str(self.mangas), "suwayomi": rows, "kavita": self.kavita_specs,
                 "path_map": self.cfg["paths"]["kavita_path_map"]}  # fmt: skip
        path.write_text(json.dumps(world), encoding="utf-8")
        return path


def fakes_from(path):
    """(FakeSuwayomi, FakeKavita) of a dumped world. The FakeKavita indexes every CBZ now in each series' folder: a
    process started later sees the scans of the runs before it as done."""
    world = json.loads(path.read_text(encoding="utf-8"))
    suwayomi = FakeSuwayomi(world["mangas_root"])
    suwayomi.source_rows = world["suwayomi"]["sources"]
    suwayomi.manga_rows = {m["id"]: m for m in world["suwayomi"]["mangas"]}
    suwayomi.chapter_rows = {c["id"]: c for c in world["suwayomi"]["chapters"]}
    kavita = FakeKavita({LIBRARY_ID: "/kavita/library"}, world["path_map"])
    for series_id, name, folder, names in world["kavita"]:
        kavita._next_id = series_id - 1  # the same series id as in the test's process
        kavita.add_series(name, folder, LIBRARY_ID, **names)
    return suwayomi, kavita


def decide_junk(cfg, verdict="keep"):
    """A person's verdict on every open junk candidate (junk decide); the next process run frees their chapters."""
    with closing(db.connect(cfg)) as conn:
        rows = review.listing(conn, cfg, everything=True)
        for r in rows:
            review.decide(conn, r["id"], verdict, "human", workflow.now())
    return len(rows)
