"""junk.chapter_scan: the junk stage's scan and decision on a CBZ in hand, writing nothing (SPEC "Source review").

A source review's sample is scanned and decided with the series' stored decisions, and the candidates the stage would
offer are listed, while the database keeps no candidate, sighting, layout, page aspect or watermark of it.
"""

import io
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import archive, config, db, names, strip
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, chapter_scan, ocr, store

from tests.builders import add_series, present, site_cfg
from tests.test_junk_stage import strip_cbz
from tests.timing import budget
from tests.world import cbz

WRITTEN = ("junk_seen", "junk_candidate", "junk_watermark", "junk_scan", "series_conversion")
WORDS: ocr.WordLists = {"keywords": ["scans"], "weak": [], "paired": [], "suppress": []}


def extracted(tmp_path, data):
    """Return the CBZ's pages written to tmp_path in archive order, as the stage extracts them."""
    cbz_path = tmp_path / "sample.cbz"
    cbz_path.write_bytes(data)
    work = tmp_path / "pages"
    work.mkdir()
    paths, names, sizes = [], [], []
    for i, (name, page) in enumerate(archive.pages(cbz_path)):
        path = work / f"{i:04d}.png"
        path.write_bytes(page)
        paths.append(path)
        names.append(name)
        sizes.append(Image.open(io.BytesIO(page)).size)
    return chapter_scan.Extracted(paths, names, sizes)


def counts(conn):
    query = sql.SQL("SELECT count(*) AS n FROM {}")
    return {t: db.one_row(conn, query.format(sql.Identifier(t)))["n"] for t in WRITTEN}


@budget(load="db", seconds=5)
@pytest.mark.parametrize("layout", ["pages", "strip"])
def test_a_scan_and_decision_of_a_cbz_in_hand_writes_nothing(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        decisions = store.Decisions(conn, "Series")
        if layout == "strip":
            pages = extracted(tmp_path, strip_cbz("s1"))
            sc = chapter_scan.scan_strip(pages, decisions, "cpu")
        else:
            pages = extracted(tmp_path, cbz(8, seed="s1", credit=True))
            aspects = present(strip.page_aspect([pages.sizes]))  # as the stage computes them for a new series
            sc = chapter_scan.scan_pages(pages, [False] * len(pages.paths), aspects)
        applied = chapter_scan.apply(sc, lambda kind, text: decisions.decide(kind, store.parse_key(kind, text)), [])
        picks = candidates.select(conn, "Series", names.identity(names.parse("Chapter 1")), applied.undecided, WORDS)
        assert applied.problem is None, applied.problem
        assert applied.undecided, "with no decision stored, the chapter's units are undecided"
        assert all(p["unit"] in applied.undecided or p["kind"] == "probe" for p in picks), "offered from undecided"
        assert picks or layout == "strip", "the wider credit page is offered for review by its size"
        assert counts(conn) == dict.fromkeys(WRITTEN, 0), "no candidate, sighting, watermark, scan or layout written"


@budget(load="db", seconds=3)
def test_page_aspects_are_the_stored_ones_else_computed_from_the_chapter_and_never_written(tmp_path):
    cfg = site_cfg(tmp_path)
    sizes = [(1000, 1500)] * 8
    with closing(db.connect(cfg)) as conn:
        series_id = add_series(conn, "Series")
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, 'pages', 'computed')",
                (series_id,),
            )
        assert chapter_scan.page_aspects(conn, series_id, sizes) == ([1.5], False), "computed from the chapter's pages"
        row = db.one_row(conn, "SELECT page_aspect FROM series_conversion WHERE series_id = %s", (series_id,))
        assert row["page_aspect"] is None, "the reader writes nothing: the stage stores what it computed"
        with db.transaction(conn):
            conn.execute("UPDATE series_conversion SET page_aspect = '[1.4]' WHERE series_id = %s", (series_id,))
        assert chapter_scan.page_aspects(conn, series_id, sizes) == ([1.4], True), "stored ones win"


@budget()
def test_the_word_lists_are_config_junk_s_4_lists():
    junk = config.load()["junk"]
    lists = ("keywords", "weak", "paired", "suppress")
    assert set(junk) > set(lists), "config junk holds more than the word lists"
    assert ocr.word_lists(junk) == {k: junk[k] for k in lists}, "only the keyword test's lists"
