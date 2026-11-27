"""The junk refill in `status` and its record's backfill (SPEC "Junk decisions", review; schema step 0075).

Chapters carry the record of their first decide after a scan; status reports the last 7 days of them.
"""

from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import db, status

from tests.builders import add_chapter, add_series, adopted
from tests.timing import budget

STEP = Path(__file__).resolve().parent.parent / "manga_pipeline" / "schema" / "0075_junk_refill.sql"


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


def recorded(conn, series_id, identity, waited_on, *, new):
    """Add a chapter whose first decide after its scan waited on `waited_on` candidates a day ago; return its id."""
    chapter_id = add_chapter(conn, series_id, identity, "junk", number=1)
    with db.transaction(conn):
        conn.execute(
            """UPDATE chapter SET junk_decided_at = to_char((now() - interval '1 day') AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'), junk_candidates = %s, junk_new_series = %s WHERE id = %s""",
            (waited_on, new, chapter_id),
        )
    return chapter_id


def section(lines):
    start = lines.index("Junk refill (last 7 days):")
    return lines[start + 1 : start + 6]


@budget(load="db")
def test_status_reports_candidates_per_chapter_for_new_and_known_series(site):
    _cfg, conn = site
    a, b = add_series(conn, "A"), add_series(conn, "B")
    recorded(conn, a, "c1", 0, new=True)
    recorded(conn, a, "c2", 2, new=True)  # waited on 2, then done: still 2
    recorded(conn, b, "c1", 0, new=False)
    recorded(conn, b, "c2", 4, new=False)
    old = recorded(conn, b, "c3", 9, new=False)
    with db.transaction(conn):  # out of the window
        conn.execute("UPDATE chapter SET junk_decided_at = '2000-01-01T00:00:00+00:00' WHERE id = %s", (old,))
    got = section(status.report(conn))
    assert got[0] == "  series with no decision of their own: 2 chapter(s), 2 candidate(s), 1.00 per chapter", (
        "new series: chapters first decided in the window and the candidates they waited on"
    )
    assert got[1] == "  series with decisions: 2 chapter(s), 4 candidate(s), 2.00 per chapter", (
        "known series apart; a chapter decided 8 days ago is out"
    )
    assert got[2] == "  open candidates: 0", "the open candidates"
    assert got[3] == "  decisions Claude made per day: none", "no Claude decision"


@budget(load="db")
def test_status_reports_claude_decisions_per_day_and_junk_needs_human(site):
    _cfg, conn = site
    a = add_series(conn, "A")
    held = add_chapter(conn, a, "c1", "junk", number=1)
    with db.transaction(conn):
        conn.execute("UPDATE chapter SET status = 'needs_human', last_error = 'unsure' WHERE id = %s", (held,))
        conn.execute(
            """INSERT INTO junk_decision (series_key, kind, key, verdict, decider, decided_at, source)
            SELECT 'A', 'block', lpad(to_hex(81985529216486895 + n), 16, '0') || '|1.0', 'junk', 'claude',
                to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'), 'candidate'
            FROM generate_series(1, 3) n"""
        )
    got = section(status.report(conn))
    assert got[1].startswith("  decisions Claude made per day: "), "Claude's decisions per day"
    assert got[1].endswith(" 3"), "3 Claude rows today"
    assert got[2] == "  junk chapters that need a human: 1", "the junk chapters held for a person"


@budget(load="db")
def test_step_0075_backfills_the_chapters_with_a_stored_scan(site):
    _cfg, conn = site
    scanned_at = "2026-01-02T00:00:00+00:00"
    chapters = []
    for name, decided_at in (("A", None), ("B", "2026-01-01T00:00:00+00:00"), ("C", "2026-01-03T00:00:00+00:00")):
        series = add_series(conn, name)
        chapters.append(add_chapter(conn, series, "c1", "junk", number=1))
        with db.transaction(conn):
            conn.execute("UPDATE series SET folder = %s WHERE id = %s", (f"/lib/{name}", series))  # rules key: name
            conn.execute(
                """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates)
                VALUES (%s, %s, 'd', '{}', %s, '[5, 6]')""",
                (chapters[-1], series, scanned_at),
            )
            if decided_at:
                conn.execute(
                    """INSERT INTO junk_decision (series_key, kind, key, verdict, decider, decided_at)
                    VALUES (%s, 'page', 'k', 'keep', 'claude', %s)""",
                    (name, decided_at),
                )
    undecided = add_chapter(conn, add_series(conn, "D"), "c1", "junk", number=1)
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates)
            SELECT id, series_id, 'd', '{}', %s, '[]' FROM chapter WHERE id = %s""",
            (scanned_at, undecided),
        )
        conn.execute("ALTER TABLE chapter DROP COLUMN junk_decided_at, DROP COLUMN junk_candidates")
        conn.execute("ALTER TABLE chapter DROP COLUMN junk_new_series")
        conn.execute(STEP.read_text(encoding="utf-8"))
    got = conn.execute("SELECT junk_decided_at, junk_candidates, junk_new_series FROM chapter ORDER BY id").fetchall()
    assert [tuple(r) for r in got] == [
        (scanned_at, 2, True),
        (scanned_at, 2, False),
        (scanned_at, 2, True),
        (None, None, None),
    ], (
        "a waiting chapter is recorded from its scan, new unless its series decided before the scan;"
        " one scanned and not yet decided is left to its decide"
    )


@budget(load="db")
def test_status_reports_none_when_no_chapter_and_no_decision_is_in_the_window(site):
    _cfg, conn = site
    assert section(status.report(conn))[0] == "  none", "an empty refill section reads none, as the others do"
