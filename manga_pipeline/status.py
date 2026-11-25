"""The status command (SPEC "CLI", "Needs human"): what is where, what waits and on what, and what needs a human.

Also the series titles changed at the sources, and the Discord reports not sent.

Read-only: it never creates, migrates or writes the database.
"""

from contextlib import closing
from datetime import datetime, timedelta

from manga_pipeline import config, db, kavita_link, notify, pause, rewrite, workflow
from manga_pipeline.db import sql

TITLES_SHOWN_DAYS = 30  # status lists the series titles changed at the sources this many days back


def run(cfg: config.Config) -> None:
    """Print the status report; with no database built yet, say so."""
    if not db.built(cfg):
        print(f"no database {cfg['database']['name']}: no run has built it yet")
        return
    with closing(db.read_only(cfg)) as conn:
        print("\n".join(report(conn)))


def report(conn: db.Connection) -> list[str]:
    """Return the status report as lines, read in 1 read transaction so its sections agree while a part runs."""
    with conn.transaction():  # read_only's transactions are repeatable-read: 1 snapshot
        return _report(conn)


def _pause(conn: db.Connection) -> list[str]:
    """Return the automation's pause line, first in the report, when it is paused (SPEC "Parts"); else none."""
    since = pause.paused(conn)
    return [] if since is None else [f"Automation paused since {since}: state resume ends it"]


def _redownload(row: db.Row) -> str:
    state = workflow.read_redownload(row["redownload"])
    expected = state.get("expected")
    now = (
        f"waiting on {expected['source']} chapter {expected['chapter_id']} since {expected['enqueued_at']}"
        if expected
        else "picking the next source"
    )
    tried = ", ".join(state.get("tried") or []) or "none"
    return f"  chapter {row['id']} ({row['name']} / {row['identity']}): {now}; tried: {tried}"


def _report(conn: db.Connection) -> list[str]:
    return [
        *_pause(conn),
        *_chapters(conn),
        *_runs(conn),
        *_claims(conn),
        *_waits(conn),
        *_bulk_waits(conn),
        *_unattached(conn),
        *_rewrites(conn),
        *_needs_human(conn),
        *_titles(conn),
        *_discord(conn),
        *_redownloads(conn),
        *_notes(conn),
        *_not_taken(conn),
        *_unconfirmed(conn),
    ]


def _chapters(conn: db.Connection) -> list[str]:
    lines = ["Chapters by stage and status:"]
    rows = conn.execute("SELECT stage, status, count(*) AS n FROM chapter GROUP BY stage, status").fetchall()
    rows.sort(key=lambda r: (workflow.STAGES.index(r["stage"]), r["status"]))
    return lines + ([f"  {r['stage']:<10} {r['status']:<12} {r['n']}" for r in rows] or ["  none"])


def _runs(conn: db.Connection) -> list[str]:
    lines = ["Runs (per part and machine; last start, end, result):"]
    runs: dict[str, dict[str, str]] = {}
    for key, value in conn.execute("SELECT key, value FROM meta WHERE key LIKE '%%.last_%%' ORDER BY key"):
        who, what = key.rsplit(".", 1)
        runs.setdefault(who, {})[what] = value
    return lines + (
        [
            f"  {who:<24} {r.get('last_start', '-')}  {r.get('last_end', '-')}  {r.get('last_result', '-')}"
            for who, r in sorted(runs.items())
        ]
        or ["  none"]
    )


def _claims(conn: db.Connection) -> list[str]:
    lines = ["Claims (by owner, oldest first, with their age):"]
    now = datetime.fromisoformat(db.now(conn))
    rows = conn.execute(
        """SELECT c.id, c.claimed_by, c.stage, c.updated_at, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'claimed' ORDER BY c.claimed_by, c.updated_at"""
    ).fetchall()
    return lines + (
        [
            f"  {r['claimed_by']}: chapter {r['id']} ({r['name']}, at {r['stage']}) for "
            f"{(now - datetime.fromisoformat(r['updated_at'])).total_seconds() // 60:.0f} min"
            for r in rows
        ]
        or ["  none"]
    )


def _waits(conn: db.Connection) -> list[str]:
    lines = ["Waits:"]
    rows = conn.execute(
        "SELECT waiting_on, count(*) AS n FROM chapter WHERE status = 'waiting' GROUP BY waiting_on ORDER BY waiting_on"
    ).fetchall()
    return lines + ([f"  {r['waiting_on']:<18} {r['n']}" for r in rows] or ["  none"])


def _bulk_waits(conn: db.Connection) -> list[str]:
    lines = ["Series waiting on bulk_conversion:"]
    rows = conn.execute(
        """SELECT s.id, s.name, count(*) AS n FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = 'bulk_conversion' GROUP BY s.id ORDER BY s.name"""
    ).fetchall()
    return lines + ([f"  {r['name']} (series {r['id']}): {r['n']} chapter(s)" for r in rows] or ["  none"])


def _unattached(conn: db.Connection) -> list[str]:
    lines = ["New series published, waiting for Kavita to list them:"]
    rows = conn.execute(
        sql.SQL("""SELECT s.id, s.comicinfo_series, MIN(h.handled_at) AS since FROM series s
        JOIN handled_chapter h ON h.series_id = s.id AND h.outcome = 'published'
        WHERE {} GROUP BY s.id ORDER BY s.id""").format(kavita_link.UNATTACHED)
    ).fetchall()
    return lines + (
        [f"  {r['comicinfo_series']} (series {r['id']}): published {r['since']}" for r in rows] or ["  none"]
    )


def _rewrites(conn: db.Connection) -> list[str]:
    lines = ["Rewrites (queued, waiting for Kavita, failed):"]
    rows = conn.execute(
        """SELECT id, library_path, pending_path, pending_step, pending_error, pending_attempts, indexed_since
        FROM published_file
        WHERE pending_path IS NOT NULL OR indexed_since IS NOT NULL OR pending_error IS NOT NULL ORDER BY id"""
    ).fetchall()
    open_rewrites = db.one_row(conn, sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(rewrite.OPEN_REWRITES))[0]
    if open_rewrites:
        lines.append(f"  rewrite chapters open: {open_rewrites}")
    unknown = db.one_row(conn, "SELECT count(DISTINCT series_id) FROM published_file WHERE cover_state IS NULL")[0]
    if unknown:  # read by export's next comparison; a series whose files cannot be read stays here, logged
        lines.append(f"  series with published files whose cover is not known yet: {unknown}")
    limit = config.cached()["workflow"]["attempt_limit"]
    given_up = conn.execute(
        """SELECT w.series_id, s.name, w.volume, w.last_error FROM volume_cover_wanted w
        JOIN series s ON s.id = w.series_id WHERE w.attempts >= %s ORDER BY w.series_id, w.volume""",
        (limit,),
    ).fetchall()
    lines += [
        f"  series {r['series_id']} ({r['name']}) volume {r['volume']} cover not fetched: {r['last_error']}"
        for r in given_up
    ]
    if not (rows or open_rewrites or unknown or given_up):
        lines.append("  none")
    return lines + [_rewrite_file(r, limit) for r in rows]


def _rewrite_file(r: db.Row, limit: int) -> str:
    """1 published file's rewrite line: failed (with its fix once given up: limit attempts), queued or waiting."""
    stuck = r["pending_path"] is None or r["pending_attempts"] >= limit
    if r["pending_error"] and not stuck:
        return f"  file {r['id']} ({r['library_path']}): {r['pending_error']} (tried again by export)"
    if r["pending_error"]:
        fix = f"rewrite retry {r['id']}"
        if r["pending_path"] is None or r["pending_step"] < rewrite.STEP_RENAMED:
            fix += f", or rewrite drop {r['id']}"
        return f"  file {r['id']} ({r['library_path']}): {r['pending_error']} -> {fix}"
    if r["pending_path"]:
        return f"  file {r['id']} ({r['library_path']}): queued as {r['pending_path']}"
    return f"  file {r['id']} ({r['library_path']}): waiting for Kavita since {r['indexed_since']}"


def _needs_human(conn: db.Connection) -> list[str]:
    lines = ["Needs human:"]
    series = conn.execute(
        "SELECT id, name, status_reason, origin, admitted_at FROM series WHERE status = 'needs_human' ORDER BY id"
    ).fetchall()
    chapters = conn.execute(
        """SELECT c.id, c.identity, c.stage, c.last_error, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'needs_human' ORDER BY c.id"""
    ).fetchall()
    human = [_human(workflow.human_line("series", r, r["status_reason"])) for r in series]
    human += [_human(workflow.human_line("chapter", r, r["last_error"])) for r in chapters]
    return lines + (human or ["  none"])


def _human(line: tuple[str, str]) -> str:
    text, fix = line
    return f"  {text} -> {fix}" if fix else f"  {text}"


def _titles(conn: db.Connection) -> list[str]:
    lines = [f"Series titles changed at the sources (last {TITLES_SHOWN_DAYS} days, not applied):"]
    since = (datetime.fromisoformat(db.now(conn)) - timedelta(days=TITLES_SHOWN_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT text, queued_at FROM notification WHERE kind = 'title' AND queued_at >= %s ORDER BY id DESC", (since,)
    ).fetchall()
    return lines + ([f"  {r['text']} (seen {r['queued_at']})" for r in rows] or ["  none"])


def _discord(conn: db.Connection) -> list[str]:
    """List the reports not sent to Discord: those queued (Discord off or not reached yet), those Discord refused."""
    lines = ["Discord reports not sent:"]
    queued = db.one_row(
        conn, sql.SQL("SELECT count(*) AS n, min(queued_at) AS oldest FROM notification WHERE {}").format(notify.UNSENT)
    )
    if queued["n"]:
        lines.append(f"  {queued['n']} queued, the oldest since {queued['oldest']} (sent by export and watch)")
    refused = conn.execute("SELECT text, failed FROM notification WHERE failed IS NOT NULL ORDER BY id").fetchall()
    lines += [f"  refused: {r['text']}: {r['failed']}" for r in refused]
    return lines if queued["n"] or refused else [*lines, "  none"]


def _redownloads(conn: db.Connection) -> list[str]:
    lines = ["Re-downloads:"]
    rows = conn.execute(
        """SELECT c.id, c.identity, c.redownload, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status != 'done' AND c.redownload IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    return lines + ([_redownload(r) for r in rows] or ["  none"])


def _notes(conn: db.Connection) -> list[str]:
    lines = ["Chapter notes:"]
    rows = conn.execute(
        """SELECT c.id, c.identity, c.notes, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.notes IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    lines += [
        f"  chapter {r['id']} ({r['name']} / {r['identity']}): {'; '.join(workflow.read_notes(r['notes']))}"
        for r in rows
    ]
    if not rows:
        lines.append("  none")
    return lines


def _not_taken(conn: db.Connection) -> list[str]:
    lines = ["Downloads not taken:"]
    rows = conn.execute("SELECT path, reason, first_seen FROM unmatched_download ORDER BY first_seen, path").fetchall()
    return lines + ([f"  {r['path']}: {r['reason']} (since {r['first_seen']})" for r in rows] or ["  none"])


def _unconfirmed(conn: db.Connection) -> list[str]:
    lines = ["Published files never confirmed:"]
    rows = conn.execute(
        """SELECT p.library_path FROM published_file p WHERE p.state = 'unconfirmed'
            AND NOT EXISTS (SELECT 1 FROM chapter c WHERE c.library_path = p.library_path AND c.status != 'done')
        ORDER BY p.library_path"""
    ).fetchall()
    return lines + ([f"  {r['library_path']}" for r in rows] or ["  none"])
