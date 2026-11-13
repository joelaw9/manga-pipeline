"""The status command (SPEC "CLI", "Needs human"): what is where, what waits and on what, and what needs a human.
Read-only: it never creates, migrates or writes the database."""

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
        for r in chapters
    ]
    lines += human or ["  none"]

    lines.append("Downloads not taken:")
    rows = conn.execute("SELECT path, reason, first_seen FROM unmatched_download ORDER BY first_seen, path").fetchall()
    lines += [f"  {r['path']}: {r['reason']} (since {r['first_seen']})" for r in rows] or ["  none"]

    lines.append("Published files never confirmed:")
    rows = conn.execute(
        """SELECT p.library_path FROM published_file p WHERE p.state = 'unconfirmed'
            AND NOT EXISTS (SELECT 1 FROM chapter c WHERE c.published_file_id = p.id AND c.status != 'done')
        ORDER BY p.library_path"""
    ).fetchall()
    return lines + ([f"  {r['library_path']}" for r in rows] or ["  none"])
