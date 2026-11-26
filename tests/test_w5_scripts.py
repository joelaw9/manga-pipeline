"""scripts/mark_bundle_rows.py and scripts/w5_cutover.py (plan phase-10-5, step 2; SPEC "Junk decisions").

The marking of bundle rows imported before the mark, from an old store built here, and the cutover that clears the
unbundled tail-piece candidates chapters wait on.
"""

import json
import sqlite3
from contextlib import closing

import pytest

from manga_pipeline import db
from manga_pipeline.junk import candidates, redecide, store
from scripts import mark_bundle_rows, w5_cutover

from tests.builders import add_chapter, add_series, adopted, config_file, one, running
from tests.timing import budget

AT = "2026-01-01T00:00:00+00:00"
TILES = (
    "1111222233334444",
    "5555666677778888",
    "9999aaaabbbbcccc",
    "ddddeeeeffff0000",
    "0f0f0f0f0f0f0f0f",
    "123456789abcdef0",
    "fedcba9876543210",
    "0123012301230123",
    "a5a5a5a55a5a5a5a",
    "3c3c3c3cc3c3c3c3",
)


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


def old_store(path, rows):
    """Write a bulk-converter junk store at path holding rows (id, series, review_id, origin, tiles, superseded)."""
    with closing(sqlite3.connect(path)) as old:
        old.execute(
            """CREATE TABLE junk (id INTEGER PRIMARY KEY AUTOINCREMENT, series TEXT, decision TEXT NOT NULL,
            origin TEXT NOT NULL, review_id TEXT, tiles TEXT NOT NULL, aspect REAL NOT NULL, chain TEXT,
            credit INTEGER NOT NULL, note TEXT, added TIMESTAMP DEFAULT CURRENT_TIMESTAMP, superseded INTEGER)"""
        )
        old.executemany(
            """INSERT INTO junk (id, series, decision, origin, review_id, tiles, aspect, credit, superseded)
            VALUES (?, ?, 'keep', ?, ?, ?, 0.35, 0, ?)""",
            rows,
        )
        old.commit()
    return path


def imported(conn, series_key, tiles, source):
    """Write an imported piece keep on tiles|0.35 with source; return its id."""
    k = f"{tiles}|{0.35!r}"
    row: store.Decision = {
        "series_key": series_key,
        "kind": "piece",
        "key": k,
        "verdict": "keep",
        "decider": "import",
        "decided_at": AT,
        "source": source,
    }
    with db.transaction(conn):
        store.upsert(conn, [row])
    return one(conn, "SELECT id FROM junk_decision WHERE md5(key) = md5(%s)", (k,))


def marked(conn):
    return {r["source"]: r["bundle"] for r in conn.execute("SELECT source, bundle FROM junk_decision")}


@budget(load="db", seconds=10)
def test_marking_marks_both_source_forms_and_leaves_a_mismatch_and_other_rows_unmarked(site, tmp_path, capsys):
    cfg, conn = site
    path = old_store(
        tmp_path / "junk.sqlite3",
        [
            (11, "S", "piece", "rn01ba7e5", TILES[0], None),
            (12, "S", "piece", "rn01ba7e5", TILES[1], None),
            (13, "Other", "piece", "rn01ba7e5", TILES[2], None),  # row 13 is another series' row
            (14, "S", "piece", "r1aaaaaaa", TILES[3], None),
            (15, "S", "piece", "rn01ba7e5", TILES[4], 99),  # superseded
            (16, "S", "block", "rn01ba7e5", TILES[5], None),  # another origin
            (17, "S", "piece", "rn01ba7e5", TILES[7], None),  # another key than the decision naming it
        ],
    )
    imported(conn, "S", TILES[0], "junk.sqlite3 junk row 11")
    imported(conn, "S", TILES[1], "record junk row 12")
    imported(conn, "S", TILES[2], "junk.sqlite3 junk row 13")
    imported(conn, "S", TILES[3], "junk.sqlite3 junk row 14")
    imported(conn, "S", TILES[4], "record junk row 15")
    imported(conn, "S", TILES[5], "junk.sqlite3 junk row 16")
    imported(conn, "S", TILES[6], "junk.sqlite3 junk row 17")
    args = [str(path), "--config", str(config_file(tmp_path, cfg))]
    assert mark_bundle_rows.main([*args, "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "junk.sqlite3 mismatched: 3" in dry, dry
    assert "marked: 0 (dry run)" in dry, dry
    assert not any(marked(conn).values()), "a dry run writes nothing"
    assert mark_bundle_rows.main(args) == 0
    assert marked(conn) == {
        "junk.sqlite3 junk row 11": True,
        "record junk row 12": True,
        "junk.sqlite3 junk row 13": False,
        "junk.sqlite3 junk row 14": False,
        "record junk row 15": False,
        "junk.sqlite3 junk row 16": False,
        "junk.sqlite3 junk row 17": False,
    }, "an rn row matched by id and series, kind and key is marked; a series, origin or key mismatch is not"
    out = capsys.readouterr().out
    assert "junk.sqlite3 to mark: 1" in out, out
    assert "record to mark: 1" in out, out
    assert "marked: 2" in out, out
    assert mark_bundle_rows.main(args) == 0
    assert "marked: 0" in capsys.readouterr().out, "a rerun marks nothing new"


@budget(load="db", seconds=10)
def test_marking_follows_a_redecide_and_skips_a_key_another_keep_judged(site, tmp_path, capsys):
    cfg, conn = site
    path = old_store(
        tmp_path / "junk.sqlite3",
        [
            (21, "S", "piece", "rn01ba7e5", TILES[8], None),
            (22, "S", "piece", "rn01ba7e5", TILES[9], None),
            (23, "S", "piece", "r1aaaaaaa", TILES[9], None),  # a 1-piece keep on the same key
        ],
    )
    first = imported(conn, "S", TILES[8], "junk.sqlite3 junk row 21")
    redecide.redecide(conn, [first], "junk", AT)
    imported(conn, "S", TILES[9], "junk.sqlite3 junk row 22")
    args = [str(path), "--config", str(config_file(tmp_path, cfg))]
    assert mark_bundle_rows.main([*args, "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "junk.sqlite3 via redecide to mark: 1" in dry, dry
    assert "junk.sqlite3 judged by another keep: 1" in dry, dry
    assert f"judged by another keep: decision {first + 2} (S, {TILES[9]}|{0.35!r})" in dry, dry
    assert mark_bundle_rows.main(args) == 0
    assert marked(conn) == {
        "junk.sqlite3 junk row 21": False,  # retired by the redecide: history
        f"redecided decision {first}": True,
        "junk.sqlite3 junk row 22": False,
    }, "the decision a redecide wrote is traced to its import and marked; a key another keep judged is not"


@budget(load="db", seconds=10)
def test_marking_refuses_a_missing_store_and_a_running_part(site, tmp_path):
    cfg, _ = site
    path = str(config_file(tmp_path, cfg))
    assert mark_bundle_rows.main([str(tmp_path / "none.sqlite3"), "--config", path]) == 2, "no store: refused"
    store_path = old_store(tmp_path / "junk.sqlite3", [])
    with running(cfg, "process"):
        assert mark_bundle_rows.main([str(store_path), "--config", path]) == 3, "a part runs: refused"


def waiting_chapter(conn, series_id, identity, status, ids):
    """Add a junk chapter in status waiting on ids (its scan row recorded); return its id."""
    cid = add_chapter(conn, series_id, identity, "junk", number=int(identity[1:]))
    waiting_on = "junk_decision" if status == "waiting" else None
    with db.transaction(conn):
        conn.execute("UPDATE chapter SET status = %s, waiting_on = %s WHERE id = %s", (status, waiting_on, cid))
        conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates, bundles)
            VALUES (%s, %s, 'd', '{}', %s, %s, %s)""",
            (cid, series_id, AT, json.dumps(ids), json.dumps([{"ids": ids, "size": 2}])),
        )
    return cid


def candidate(conn, series_key, kind, n):
    with db.transaction(conn):
        return one(
            conn,
            """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)
            VALUES (%s, %s, %s, 'r', %s, %s) RETURNING id""",
            (series_key, kind, f"{TILES[n]}|0.5", f"f{n}", AT),
        )


def series_of(conn, name, layout):
    sid = add_series(conn, name)
    with db.transaction(conn):
        conn.execute("UPDATE series SET rules_key = %s WHERE id = %s", (name, sid))
        conn.execute(
            "INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, %s, 'computed')", (sid, layout)
        )
    return sid


@budget(load="db", seconds=10)
def test_the_cutover_clears_strip_chapters_piece_candidates_and_writes_no_status(site, tmp_path, capsys):
    cfg, conn = site
    strip, pages = series_of(conn, "Strip", "strip"), series_of(conn, "Pages", "pages")
    piece, block = candidate(conn, "Strip", "piece", 0), candidate(conn, "Strip", "block", 1)
    band = candidate(conn, "Pages", "band", 2)
    waits = waiting_chapter(conn, strip, "c1", "waiting", [piece, block])
    held = waiting_chapter(conn, strip, "c2", "needs_human", [piece])
    blocks_only = waiting_chapter(conn, strip, "c3", "waiting", [block])
    paged = waiting_chapter(conn, pages, "c1", "waiting", [band])
    before = conn.execute("SELECT id, stage, status, waiting_on, updated_at FROM chapter ORDER BY id").fetchall()
    path = str(config_file(tmp_path, cfg))
    assert w5_cutover.main(["--dry-run", "--config", path]) == 0
    assert "chapters: 2 (waiting 1, needs_human 1) (dry run" in capsys.readouterr().out
    assert w5_cutover.main(["--config", path]) == 0
    scans = {r[0]: (r[1], r[2] == "[]") for r in conn.execute("SELECT chapter_id, candidates, bundles FROM junk_scan")}
    assert scans == {
        waits: ("[]", True),
        held: ("[]", True),
        blocks_only: (f"[{block}]", False),
        paged: (f"[{band}]", False),
    }, "only strip chapters holding a piece candidate are cleared, their bundle records too"
    after = conn.execute("SELECT id, stage, status, waiting_on, updated_at FROM chapter ORDER BY id").fetchall()
    assert after == before, "no chapter status is written: process ends the waits"
    row = conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (piece,)).fetchone()
    assert not candidates.is_open(conn, row), "the old piece candidate closes"
