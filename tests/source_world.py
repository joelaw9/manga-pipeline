"""A test world for the source review (SPEC "Source review"): a series Kavita holds and a candidate manga.

World's fakes stand in for the clients the commands build; `main` runs the CLI and returns (exit code, stdout lines).
"""

import io
import zipfile
from contextlib import closing
from typing import NamedTuple

from PIL import Image

from manga_pipeline import cli, db, intake
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import sources, step

from tests.builders import config_file
from tests.world import World

SERIES = "Alpha"
FILES = 5  # the series' bulk files, Ch.1 to Ch.5, each 3 pages 16 px wide
SOURCE = "Better"  # the candidate's source; its display name is "Better (EN)"


def make_world(tmp_path, monkeypatch):
    """Return a World whose fakes the CLI's clients are, and whose series Alpha Kavita holds with FILES files."""
    w = World(tmp_path)
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: w.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: w.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: w.sources)
    library(w)
    return w


def library(w, name=SERIES, files=FILES):
    """Add a series Kavita holds with bulk files Ch.1 to Ch.<files>, recorded by intake's baseline; return its id."""
    bulk = tuple((f"Ch.{n}.cbz", f"Grp_Chapter {n}", str(n)) for n in range(1, files + 1))
    w.kavita_series(name, bulk=bulk)
    w.kavita.scan_library(w.cfg["kavita"]["library_id"])
    w.kavita.run_scans()
    w.rows("DELETE FROM meta WHERE key = %s RETURNING key", step.BASELINE_PASS_AT)  # a pass a day: this one takes it
    w.run(intake)
    ((series_id,),) = w.rows("SELECT id FROM series WHERE name = %s", name)
    return series_id


def candidate(w, numbers=range(1, FILES + 1), *, in_library=False, names=None):
    """Add the candidate manga on source SOURCE with chapters "Chapter <n>"; return (manga id, {number: chapter id}).

    names: {number: chapter name} for chapters named otherwise.
    """
    source = next((i for i, s in w.suwayomi.source_rows.items() if s["name"] == SOURCE), None)
    source = source or w.suwayomi.add_source(SOURCE)
    manga = w.suwayomi.add_manga(SERIES, source, in_library=in_library)
    chapters = {
        n: w.suwayomi.add_chapter(manga, (names or {}).get(n, f"Chapter {n}"), scanlator=SOURCE) for n in numbers
    }
    return manga, chapters


def link(w, manga_id, series_id=1):
    """Link a Suwayomi manga to a series, as identification does."""
    m = w.suwayomi.manga_rows[manga_id]
    with closing(db.connect(w.cfg)) as conn, db.transaction(conn):
        conn.execute(
            "INSERT INTO suwayomi_manga (manga_id, source_id, title, series_id) VALUES (%s, %s, %s, %s)",
            (manga_id, m["sourceId"], m["title"], series_id),
        )


def pages(width=16, n=3, seed=""):
    """Return n PNG pages `width` px wide, 1.5 times as tall, each its own colour."""
    out = []
    for i in range(1, n + 1):
        data = io.BytesIO()
        Image.new("RGB", (width, width * 3 // 2), (40 + i * 30, 90, 200 - i * 20 + len(seed))).save(data, "PNG")
        out.append(data.getvalue())
    return out


def zipped(images):
    """Return a CBZ of the page images."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for i, image in enumerate(images, 1):
            z.writestr(f"{i:03}.png", image)
    return data.getvalue()


class Ran(NamedTuple):
    """A CLI run: its exit code, the lines it printed and its stderr."""

    code: int
    lines: list[str]
    err: str


def main(w, capsys, *argv, cfg=None):
    """Run the CLI on the world's config (or cfg); return what it gave (Ran)."""
    capsys.readouterr()
    code = cli.main(["--config", str(config_file(w.share.parent, cfg or w.cfg)), *argv])
    out = capsys.readouterr()
    return Ran(code, out.out.splitlines(), out.err)
