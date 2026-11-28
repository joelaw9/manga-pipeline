"""scripts/d8_cutover.py (plan phase-10-8, D4; SPEC "Junk decisions", Candidates)."""

from contextlib import closing

import pytest

from manga_pipeline import db, intake, process
from manga_pipeline.junk import candidates, ocr
from scripts import d8_cutover

from tests.builders import adopted, config_file, present, run_part, running
from tests.test_junk_stage import KEYWORD_TEXT, Site, open_candidates, strip_cbz
from tests.test_w5_scripts import candidate, series_of, waiting_chapter
from tests.timing import budget


@budget(load="pool", seconds=30)
def test_a_deleted_candidates_chapter_is_decided_again_and_opens_it_with_the_new_images(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    site = Site(tmp_path, monkeypatch)
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_cbz("d8"))
    run_part(site.cfg, intake)
    run_part(site.cfg, process)
    before = {c["id"] for c in open_candidates(site)}
    assert before, "it waits on its candidates"
    assert d8_cutover.main(["--config", str(config_file(tmp_path, site.cfg))]) == 0
    assert f"open unreviewed candidates: {len(before)} deleted" in capsys.readouterr().out
    assert site.rows("SELECT id FROM junk_candidate") == [], "deleted"
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "decided again: it waits on them again"
    after = open_candidates(site)
    assert len(after) == len(before), "each opened again"
    assert not before & {c["id"] for c in after}, "as new candidates"
    for c in after:
        info = candidates.read_info(candidates.folder(site.cfg, c["folder"]))
        assert "position" in present(info), "with the new images: a position"


@pytest.fixture
def adopted_site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


@budget(load="db", seconds=10)
def test_the_cutover_deletes_only_open_unreviewed_candidates_no_held_chapter_lists_and_dry_run_writes_nothing(
    adopted_site, tmp_path, capsys
):
    cfg, conn = adopted_site
    strip, stuck = series_of(conn, "Strip", "strip"), series_of(conn, "Stuck", "strip")
    fresh, reviewed, unsure, shared = (candidate(conn, "Strip", "block", n) for n in range(4))
    by_held, by_held_series = candidate(conn, "Strip", "block", 4), candidate(conn, "Stuck", "block", 5)
    closed = candidate(conn, "Strip", "block", 6)  # no chapter waits on it: not open
    waiting_chapter(conn, strip, "c1", "waiting", [fresh, reviewed, unsure, shared])
    waiting_chapter(conn, strip, "c2", "needs_human", [by_held, shared])
    waiting_chapter(conn, stuck, "c1", "waiting", [by_held_series])
    with db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET reviews = 1 WHERE id = %s", (reviewed,))
        conn.execute(  # as a killed review leaves it: its verdict recorded, its review not counted
            "UPDATE junk_candidate SET verdict = 'unsure' WHERE id = %s", (unsure,)
        )
        conn.execute("UPDATE series SET status = 'needs_human', status_reason = 'x' WHERE id = %s", (stuck,))
    rows = "SELECT id FROM junk_candidate ORDER BY id"
    scans = "SELECT chapter_id, candidates, bundles FROM junk_scan ORDER BY chapter_id"
    chapters = "SELECT id, stage, status, waiting_on, updated_at FROM chapter ORDER BY id"
    before_rows, before_scans, before = (conn.execute(q).fetchall() for q in (rows, scans, chapters))
    path = str(config_file(tmp_path, cfg))
    assert d8_cutover.main(["--dry-run", "--config", path]) == 0
    assert "open unreviewed candidates: 1 (dry run: nothing deleted)" in capsys.readouterr().out
    assert conn.execute(rows).fetchall() == before_rows, "a dry run writes nothing"
    assert d8_cutover.main(["--config", path]) == 0
    assert "open unreviewed candidates: 1 deleted" in capsys.readouterr().out
    left = {r[0] for r in conn.execute(rows)}
    assert left == {reviewed, unsure, shared, by_held, by_held_series, closed}, (
        "only the open unreviewed one no held chapter lists goes"
    )
    assert conn.execute(scans).fetchall() == before_scans, "no chapter's list is cleared"
    assert conn.execute(chapters).fetchall() == before, "no chapter status is written: process ends the wait"
    assert d8_cutover.main(["--config", path]) == 0
    assert "open unreviewed candidates: 0 deleted" in capsys.readouterr().out, "a second run finds none left"


@budget(load="db", seconds=10)
def test_the_cutover_is_refused_while_a_part_runs(adopted_site, tmp_path):
    cfg, conn = adopted_site
    fresh = candidate(conn, "Strip", "block", 1)
    waiting_chapter(conn, series_of(conn, "Strip", "strip"), "c1", "waiting", [fresh])
    with running(cfg, "process"):
        assert d8_cutover.main(["--config", str(config_file(tmp_path, cfg))]) == 3, "a part runs: refused"
    assert [r[0] for r in conn.execute("SELECT id FROM junk_candidate")] == [fresh], "nothing is deleted"
