"""The status command (SPEC "CLI", "Needs human"): what is where, what waits and on what, and what needs a human.
Read-only: it never creates, migrates or writes the database."""

import json
from contextlib import closing

from manga_pipeline import config, db, workflow


def run(cfg):
    path = config.abs_path(cfg["database"]["path"])
    if not path.is_file():
        print(f"no database at {path}: no run has built it yet")
        return
    with closing(db.read_only(cfg)) as conn:
        print("\n".join(report(conn)))


def report(conn):
    """The status report as lines, read in 1 read transaction so its sections agree while a part runs."""
    conn.execute("BEGIN")
    try:
        return _report(conn)
    finally:
        conn.execute("COMMIT")


def _settle(row):
    """The other commands that settle a needs_human chapter at its stage (SPEC "Bad downloads")."""
    hint = ""
    if row["stage"] == "validate":
        hint += f", chapter use {row['id']} SUWAYOMI_CHAPTER_ID"
    if row["stage"] in workflow.DROPPABLE:
        hint += f", chapter drop {row['id']}"
    return hint


def _redownload(row):
    state = json.loads(row["redownload"])
    expected = state.get("expected")
    now = (
        f"waiting on {expected['source']} chapter {expected['chapter_id']} since {expected['enqueued_at']}"
        if expected
        else "picking the next source"
    )
    tried = ", ".join(state.get("tried") or []) or "none"
    return f"  chapter {row['id']} ({row['name']} / {row['identity']}): {now}; tried: {tried}"


def _report(conn):
    lines = ["Chapters by stage and status:"]
    rows = conn.execute("SELECT stage, status, count(*) AS n FROM chapter GROUP BY stage, status").fetchall()
    rows.sort(key=lambda r: (workflow.STAGES.index(r["stage"]), r["status"]))
    lines += [f"  {r['stage']:<10} {r['status']:<12} {r['n']}" for r in rows] or ["  none"]

    lines.append("Waits:")
    rows = conn.execute(
        "SELECT waiting_on, count(*) AS n FROM chapter WHERE status = 'waiting' GROUP BY waiting_on ORDER BY waiting_on"
    ).fetchall()
    lines += [f"  {r['waiting_on']:<18} {r['n']}" for r in rows] or ["  none"]

    lines.append("Series waiting on bulk_conversion:")
    rows = conn.execute(
        """SELECT s.id, s.name, count(*) AS n FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = 'bulk_conversion' GROUP BY s.id ORDER BY s.name"""
    ).fetchall()
    lines += [f"  {r['name']} (series {r['id']}): {r['n']} chapter(s)" for r in rows] or ["  none"]

    lines.append("Needs human:")
    series = conn.execute(
        "SELECT id, name, status_reason FROM series WHERE status = 'needs_human' ORDER BY id"
    ).fetchall()
    chapters = conn.execute(
        """SELECT c.id, c.identity, c.stage, c.last_error, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'needs_human' ORDER BY c.id"""
    ).fetchall()
    human = [f"  series {r['id']} ({r['name']}): {r['status_reason']} -> retry --series {r['id']}" for r in series]
    human += [
        f"  chapter {r['id']} ({r['name']} / {r['identity']}, at {r['stage']}): {r['last_error']} -> retry {r['id']}"
        + _settle(r)
        for r in chapters
    ]
    lines += human or ["  none"]

    lines.append("Re-downloads:")
    rows = conn.execute(
        """SELECT c.id, c.identity, c.redownload, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status != 'done' AND c.redownload IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    lines += [_redownload(r) for r in rows] or ["  none"]

    lines.append("Chapter notes:")
    rows = conn.execute(
        """SELECT c.id, c.identity, c.notes, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.notes IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    lines += [f"  chapter {r['id']} ({r['name']} / {r['identity']}): {'; '.join(json.loads(r['notes']))}" for r in rows]
    if not rows:
        lines.append("  none")

    lines.append("Downloads not taken:")
    rows = conn.execute("SELECT path, reason, first_seen FROM unmatched_download ORDER BY first_seen, path").fetchall()
    lines += [f"  {r['path']}: {r['reason']} (since {r['first_seen']})" for r in rows] or ["  none"]

    lines.append("Published files never confirmed:")
    rows = conn.execute(
        """SELECT p.library_path FROM published_file p WHERE p.state = 'unconfirmed'
            AND NOT EXISTS (SELECT 1 FROM chapter c WHERE c.library_path = p.library_path AND c.status != 'done')
        ORDER BY p.library_path"""
    ).fetchall()
    return lines + ([f"  {r['library_path']}" for r in rows] or ["  none"])
