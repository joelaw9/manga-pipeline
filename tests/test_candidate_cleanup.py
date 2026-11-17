"""junk review's clean-up of junk candidates (SPEC "Junk decisions"): closed for an hour, then deleted with their
folders; a folder no row names stays a day; a candidate a running review listed is left alone.
"""

import os
import time
from contextlib import closing

import pytest

from manga_pipeline import config, db
from manga_pipeline.junk import candidates

from tests.builders import add_chapter, add_series, adopted, one
from tests.timing import budget

HOUR_AGO_2 = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


def candidate(conn, cfg, folder, key="123456789abcdef0|0.7", closed_at=None):
    """A candidate row and its folder (with an image)."""
    (config.abs_path(cfg["paths"]["candidates"]) / folder).mkdir(parents=True)
    (config.abs_path(cfg["paths"]["candidates"]) / folder / "unit.png").write_bytes(b"png")
    with db.transaction(conn):
        return one(
            conn,
            """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at, closed_at)
            VALUES ('S', 'page', %s, 'r', %s, '2026-01-01T00:00:00+00:00', %s) RETURNING id""",
            (key, folder, closed_at),
        )


def wait_on(conn, chapter_id, cid):
    """The junk chapter chapter_id waits on candidate cid (which is then open)."""
    with db.transaction(conn):
        conn.execute("UPDATE chapter SET status = 'waiting', waiting_on = 'junk_decision' WHERE id = %s", (chapter_id,))
        conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates)
            SELECT id, series_id, 'd', '{}', '2026-01-01T00:00:00+00:00', %s FROM chapter WHERE id = %s""",
            (f"[{cid}]", chapter_id),
        )


def folder(cfg, name):
    return config.abs_path(cfg["paths"]["candidates"]) / name


def row(conn, cid):
    return conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (cid,)).fetchone()


@budget(load="db")
def test_a_closed_candidate_is_stamped_then_deleted_with_its_folder_an_hour_later(site):
    cfg, conn = site
    cid = candidate(conn, cfg, "a-1-1")
    assert candidates.cleanup(cfg, conn) == 0
    assert row(conn, cid)["closed_at"] is not None, "closed 0 min ago: kept"
    assert folder(cfg, "a-1-1").exists(), "closed 0 min ago: kept"
    with db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET closed_at = %s", (HOUR_AGO_2,))
    assert candidates.cleanup(cfg, conn) == 1
    assert row(conn, cid) is None, "closed 2 hours ago: deleted, folder too"
    assert not folder(cfg, "a-1-1").exists(), "closed 2 hours ago: deleted, folder too"


@budget(load="db")
def test_a_reopened_candidate_loses_its_stamp(site):
    cfg, conn = site
    chapter_id = add_chapter(conn, add_series(conn, "S"), "c1", "junk")
    cid = candidate(conn, cfg, "a-1-1", closed_at=HOUR_AGO_2)
    wait_on(conn, chapter_id, cid)
    assert candidates.cleanup(cfg, conn) == 0
    assert row(conn, cid)["closed_at"] is None, "a chapter waits on it again: open, unstamped, kept"


@budget(load="db")
def test_a_candidate_a_running_review_listed_is_never_stamped_or_deleted(site):
    cfg, conn = site
    cid = candidate(conn, cfg, "a-1-1", closed_at=HOUR_AGO_2)
    with db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET review_open = 1 WHERE id = %s", (cid,))
    assert candidates.cleanup(cfg, conn) == 0
    assert row(conn, cid)["closed_at"] == HOUR_AGO_2
    assert folder(cfg, "a-1-1").exists()


@budget(load="db")
def test_a_folder_no_row_names_stays_a_day_and_a_new_rows_folder_survives_its_old_rows_deletion(site):
    cfg, conn = site
    candidate(conn, cfg, "unit-1-1", closed_at=HOUR_AGO_2)  # the old row, closed long ago
    # the same unit's new row, written by another chapter's claim
    candidate(conn, cfg, "unit-2-1", key="0fedcba987654321|0.7")
    fresh, stale = folder(cfg, "unit-3-1"), folder(cfg, "unit-4-1")  # written, rows not yet recorded
    for path in (fresh, stale):
        path.mkdir()
    day_ago = time.time() - 2 * 86400
    os.utime(stale, (day_ago, day_ago))
    candidates.cleanup(cfg, conn)
    assert not folder(cfg, "unit-1-1").exists(), "the deleted row's folder goes"
    assert folder(cfg, "unit-2-1").exists(), "a new row's folder has its own name: never the old row's"
    assert fresh.exists(), "a folder written for a candidate not yet recorded survives"
    assert not stale.exists(), "a folder no row named for a day goes"


@budget(load="db")
def test_joining_a_candidate_cleaned_up_meanwhile_fails_and_writes_nothing(site):
    _cfg, conn = site
    pick = {"kind": "page", "key": "123456789abcdef0|0.7", "reason": "r"}  # its images were never written
    with db.transaction(conn), pytest.raises(candidates.Gone, match="cleaned up meanwhile"):
        candidates.join(conn, "S", [pick], "2026-01-01T00:00:00+00:00")
    assert one(conn, "SELECT count(*) FROM junk_candidate") == 0
