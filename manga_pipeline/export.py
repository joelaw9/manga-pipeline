"""Part export (SPEC "Parts", "Publishing"): empties expired retention files, ends metadata_check waits, adds
ComicInfo.xml, publishes series by series (each Kavita series only while its files lie in its recorded folder), then
does what needs Kavita: ends kavita_scan waits, confirms, writes series fields when no scan is awaited, and requests at
most 1 library scan."""

import logging
from datetime import timedelta

from manga_pipeline import config, db, identify, retention, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.metadata import kavita_write
from manga_pipeline.stages import comicinfo, confirm, publish

log = logging.getLogger(__name__)

REQUESTED_AT = "export.kavita_scan_requested_at"  # meta: when the last library scan request succeeded


def run(cfg, conn):
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.Context(cfg, conn, "export"), work)


def work(ctx):
    with workflow.share_errors():
        retention.purge(ctx.cfg)
    workflow.end_waits(ctx, "metadata_check", comicinfo.checked)
    workflow.drain(ctx, {"comicinfo": comicinfo.run})
    for series_id in workflow.series_with_ready(ctx, ("publish",)):
        if folder_moved(ctx, series_id):
            continue
        workflow.drain(ctx, {"publish": publish.run}, series_id)
    # Kavita unreachable here ends the run: what is left all needs Kavita.
    workflow.end_waits(ctx, "kavita_scan", confirm.wait_over)
    # Confirm before the request: every chapter this run published then waits on kavita_scan, so the awaited test
    # sees it.
    workflow.drain(ctx, {"confirm": confirm.run})
    # Kavita holds series writes while its scan runs: written by a run when no scan is awaited
    if scan_awaited(ctx) is None:
        with workflow.share_errors():
            kavita_write.run(ctx)
    request_scan(ctx)


def folder_moved(ctx, series_id):
    """Whether a Kavita series' files lie elsewhere than its recorded folder (or in no single folder): then the series
    is needs_human, naming both, and nothing is published into it. A new series (no Kavita id) is not checked, nor one
    Kavita no longer holds (confirm reports that, with its fix)."""
    series = ctx.conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()
    kid = series["kavita_series_id"]
    if kid is None or ctx.kavita.series(kid) is None:
        return False
    try:
        found = identify.library_folder(ctx.cfg, ctx.kavita.series_folder(kid))
    except (kavita_client.FolderError, config.PathError, ValueError) as e:  # ValueError: a path under no map prefix
        reason = f"its folder is {series['folder']}, but {e}"
    else:
        if found == series["folder"]:
            return False
        reason = f"its folder is {series['folder']}, but Kavita holds its files in {found}"
    reason += ": nothing is published into it (series link, then retry --series)"
    with db.transaction(ctx.conn):
        workflow.needs_human(ctx.conn, "series", series_id, reason)
    log.warning("%s: series %s (%s): %s", ctx.part, series_id, series["name"], reason)
    return True


def scan_awaited(ctx):
    """The time of the last library scan request while it is still awaited (less than kavita.rescan_after_minutes ago,
    with chapters still waiting on kavita_scan), else None."""
    previous = workflow.get_meta(ctx.conn, REQUESTED_AT)
    if previous is None or not _waiting(ctx):
        return None
    cutoff = workflow.after(workflow.now(ctx.conn), -timedelta(minutes=ctx.cfg["kavita"]["rescan_after_minutes"]))
    return previous if previous > cutoff else None


def _waiting(ctx):
    return ctx.conn.execute(
        f"""SELECT COUNT(*) FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = 'kavita_scan' AND s.status = 'ok' AND {workflow.SCOPE}""",
        workflow.scope_params(ctx.cfg),
    ).fetchone()[0]


def request_scan(ctx):
    """At most 1 library scan per run, never while one is awaited (requested less than kavita.rescan_after_minutes
    ago with chapters still waiting on kavita_scan): requested when a publish made a scan due or chapters still wait.
    A repeat request charges 1 attempt to each chapter already waiting at the previous one. A failed request writes
    nothing and is decided again by the next run."""
    minutes = ctx.cfg["kavita"]["rescan_after_minutes"]
    previous = workflow.get_meta(ctx.conn, REQUESTED_AT)
    waiting = _waiting(ctx)
    if scan_awaited(ctx) is not None:
        log.info("%s: library scan requested at %s still awaited (%d chapter(s) wait)", ctx.part, previous, waiting)
        return  # ponytail: scan_awaited reads the same 2 values again; 2 cheap queries
    due = ctx.conn.execute("SELECT EXISTS (SELECT 1 FROM series WHERE scan_due = 1)").fetchone()[0]
    if not (due or waiting):
        return
    try:
        ctx.kavita.scan_library(ctx.cfg["kavita"]["library_id"])
    except http.HttpError as e:
        log.error("%s: library scan request failed: %s", ctx.part, e)
        return
    requested = workflow.now(ctx.conn)  # 1 stamp: a charged chapter must not count as waiting since after the request
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE series SET scan_due = 0 WHERE scan_due = 1")
        workflow.set_meta(ctx.conn, REQUESTED_AT, requested)
        charged = 0
        if previous is not None:
            reason = f"not indexed {minutes} minutes after the library scan request"
            charged = workflow.charge_waits(ctx, "kavita_scan", reason, previous, requested)
    log.info("%s: library scan requested", ctx.part)
    if charged:
        log.warning("%s: %d chapter(s) still not indexed charged 1 attempt", ctx.part, charged)
