"""The status command (SPEC "CLI", "Needs human"): what is where, what waits and on what, and what needs a human.
Read-only: it never creates, migrates or writes the database.
"""

import json
from contextlib import closing
from datetime import datetime

from manga_pipeline import config, db, kavita_link, rewrite, workflow
from manga_pipeline.db import sql


def run(cfg):
    if not db.built(cfg):
        print(f"no database {cfg['database']['name']}: no run has built it yet")
        return
    with closing(db.read_only(cfg)) as conn:
        print("\n".join(report(conn)))


def report(conn):
    """The status report as lines, read in 1 read transaction so its sections agree while a part runs."""
    with conn.transaction():  # read_only's transactions are repeatable-read: 1 snapshot
        return _report(conn)


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
    return [
        *_chapters(conn),
        *_runs(conn),
        *_claims(conn),
        *_waits(conn),
        *_bulk_waits(conn),
        *_unattached(conn),
        *_rewrites(conn),
        *_needs_human(conn),
        *_redownloads(conn),
        *_notes(conn),
        *_not_taken(conn),
        *_unconfirmed(conn),
    ]


def _chapters(conn):
    lines = ["Chapters by stage and status:"]
    rows = conn.execute("SELECT stage, status, count(*) AS n FROM chapter GROUP BY stage, status").fetchall()
    rows.sort(key=lambda r: (workflow.STAGES.index(r["stage"]), r["status"]))
    return lines + ([f"  {r['stage']:<10} {r['status']:<12} {r['n']}" for r in rows] or ["  none"])


def _runs(conn):
    lines = ["Runs (per part and machine; last start, end, result):"]
    runs = {}
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


def _claims(conn):
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


def _waits(conn):
    lines = ["Waits:"]
    rows = conn.execute(
        "SELECT waiting_on, count(*) AS n FROM chapter WHERE status = 'waiting' GROUP BY waiting_on ORDER BY waiting_on"
    ).fetchall()
    return lines + ([f"  {r['waiting_on']:<18} {r['n']}" for r in rows] or ["  none"])


def _bulk_waits(conn):
    lines = ["Series waiting on bulk_conversion:"]
    rows = conn.execute(
        """SELECT s.id, s.name, count(*) AS n FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = 'bulk_conversion' GROUP BY s.id ORDER BY s.name"""
    ).fetchall()
    return lines + ([f"  {r['name']} (series {r['id']}): {r['n']} chapter(s)" for r in rows] or ["  none"])


def _unattached(conn):
    lines = ["New series published, waiting for Kavita to list them:"]
    rows = conn.execute(
        sql.SQL("""SELECT s.id, s.comicinfo_series, MIN(h.handled_at) AS since FROM series s
        JOIN handled_chapter h ON h.series_id = s.id AND h.outcome = 'published'
        WHERE {} GROUP BY s.id ORDER BY s.id""").format(kavita_link.UNATTACHED)
    ).fetchall()
    return lines + (
        [f"  {r['comicinfo_series']} (series {r['id']}): published {r['since']}" for r in rows] or ["  none"]
    )


def _rewrites(conn):
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


def _rewrite_file(r, limit):
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


def _needs_human(conn):
    lines = ["Needs human:"]
    series = conn.execute(
        "SELECT id, name, status_reason, origin, admitted_at FROM series WHERE status = 'needs_human' ORDER BY id"
    ).fetchall()
    chapters = conn.execute(
        """SELECT c.id, c.identity, c.stage, c.last_error, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'needs_human' ORDER BY c.id"""
    ).fetchall()
    human = [
        f"  series {r['id']} ({r['name']}): {r['status_reason']}"
        + (
            ("" if "series new" in r["status_reason"] else f" -> series link or series new {r['id']}")
            if r["origin"] == "new" and r["admitted_at"] is None
            else f" -> retry --series {r['id']}"
        )
        for r in series
    ]
    human += [
        f"  chapter {r['id']} ({r['name']} / {r['identity']}, at {r['stage']}): {r['last_error']} -> retry {r['id']}"
        + _settle(r)
        for r in chapters
    ]
    return lines + (human or ["  none"])


def _redownloads(conn):
    lines = ["Re-downloads:"]
    rows = conn.execute(
        """SELECT c.id, c.identity, c.redownload, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status != 'done' AND c.redownload IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    return lines + ([_redownload(r) for r in rows] or ["  none"])


def _notes(conn):
    lines = ["Chapter notes:"]
    rows = conn.execute(
        """SELECT c.id, c.identity, c.notes, s.name FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.notes IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    lines += [f"  chapter {r['id']} ({r['name']} / {r['identity']}): {'; '.join(json.loads(r['notes']))}" for r in rows]
    if not rows:
        lines.append("  none")
    return lines


def _not_taken(conn):
    lines = ["Downloads not taken:"]
    rows = conn.execute("SELECT path, reason, first_seen FROM unmatched_download ORDER BY first_seen, path").fetchall()
    return lines + ([f"  {r['path']}: {r['reason']} (since {r['first_seen']})" for r in rows] or ["  none"])


def _unconfirmed(conn):
    lines = ["Published files never confirmed:"]
    rows = conn.execute(
        """SELECT p.library_path FROM published_file p WHERE p.state = 'unconfirmed'
            AND NOT EXISTS (SELECT 1 FROM chapter c WHERE c.library_path = p.library_path AND c.status != 'done')
        ORDER BY p.library_path"""
    ).fetchall()
    return lines + ([f"  {r['library_path']}" for r in rows] or ["  none"])
