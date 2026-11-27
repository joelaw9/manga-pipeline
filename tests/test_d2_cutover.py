"""scripts/d2_cutover.py (plan phase-10-6, step 3; SPEC "Junk decisions", Candidates)."""

import json
from contextlib import closing

import pytest

from manga_pipeline import db, intake, process
from manga_pipeline.junk import ocr
from scripts import d2_cutover

from tests.builders import adopted, config_file, run_part, running
from tests.test_junk_stage import KEYWORD_TEXT, Site, open_candidates, strip_cbz
from tests.test_w5_scripts import candidate, series_of, waiting_chapter
from tests.timing import budget

FIRST = "a first chapter of the series"  # the reason the first-3 rule gave


def without_text(value):
    """Return a stored scan with every OCR text left out: its blocks have no reason but the first-3 rule's."""
    if isinstance(value, dict):
        return {k: without_text(v) for k, v in value.items() if k != "text"}
    if isinstance(value, list):
        return [without_text(v) for v in value]
    return value


@budget(load="pool", seconds=30)
def test_a_chapter_waiting_only_by_the_first_3_rule_is_decided_again_and_its_wait_ends(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    site = Site(tmp_path, monkeypatch)
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_cbz("d2"))
    run_part(site.cfg, intake)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "it waits on its blocks"
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):  # as a chapter the old rule made wait
        conn.execute("UPDATE junk_scan SET scan = %s", (json.dumps(without_text(json.loads(scan))),))
        conn.execute("UPDATE junk_candidate SET reason = %s", (FIRST,))
    refill = site.rows("SELECT junk_decided_at, junk_candidates, junk_new_series FROM chapter")
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "without the cutover it waits on for good"
    assert d2_cutover.main(["--config", str(config_file(tmp_path, site.cfg))]) == 0
    assert "chapters waiting on junk candidates: 1 cleared" in capsys.readouterr().out
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "decided again with no first-3 rule: no candidate, the chapter goes on"
    assert open_candidates(site) == [], "no candidate is left open"
    assert site.rows("SELECT junk_decided_at, junk_candidates, junk_new_series FROM chapter") == refill, (
        "the refill keeps the first decide's record"
    )


@pytest.fixture
def adopted_site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


@budget(load="db", seconds=10)
def test_the_cutover_clears_only_waiting_chapters_writes_no_status_and_dry_run_writes_nothing(
    adopted_site, tmp_path, capsys
):
    cfg, conn = adopted_site
    strip = series_of(conn, "Strip", "strip")
    block, other = candidate(conn, "Strip", "block", 1), candidate(conn, "Strip", "block", 2)
    waits = waiting_chapter(conn, strip, "c1", "waiting", [block])
    held = waiting_chapter(conn, strip, "c2", "needs_human", [other])
    scans = "SELECT chapter_id, candidates, bundles FROM junk_scan ORDER BY chapter_id"
    chapters = "SELECT id, stage, status, waiting_on, updated_at FROM chapter ORDER BY id"
    before_scans, before = conn.execute(scans).fetchall(), conn.execute(chapters).fetchall()
    path = str(config_file(tmp_path, cfg))
    assert d2_cutover.main(["--dry-run", "--config", path]) == 0
    assert "chapters waiting on junk candidates: 1 (dry run: nothing cleared)" in capsys.readouterr().out
    assert conn.execute(scans).fetchall() == before_scans, "a dry run writes nothing"
    assert d2_cutover.main(["--config", path]) == 0
    got = {r[0]: (r[1], r[2]) for r in conn.execute(scans)}
    assert got[waits] == ("[]", "[]"), "the waiting chapter's candidates and bundles are cleared"
    assert got[held] == (f"[{other}]", json.dumps([{"ids": [other], "size": 2}])), "a needs_human chapter is untouched"
    assert conn.execute(chapters).fetchall() == before, "no chapter status is written: process ends the wait"


@budget(load="db", seconds=10)
def test_the_cutover_is_refused_while_a_part_runs(adopted_site, tmp_path):
    cfg, conn = adopted_site
    waits = waiting_chapter(
        conn, series_of(conn, "Strip", "strip"), "c1", "waiting", [candidate(conn, "Strip", "block", 1)]
    )
    with running(cfg, "process"):
        assert d2_cutover.main(["--config", str(config_file(tmp_path, cfg))]) == 3, "a part runs: refused"
    got = conn.execute("SELECT candidates FROM junk_scan WHERE chapter_id = %s", (waits,)).fetchone()
    assert got[0] != "[]", "nothing is cleared"
