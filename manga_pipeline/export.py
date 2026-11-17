"""Part export (SPEC "Parts", "Publishing"): empties expired retention files, adds ComicInfo.xml, publishes series by
series (each Kavita series only while its files lie in its recorded folder), then does what needs Kavita: ends
kavita_scan waits, confirms, publishes the new series that are ready, writes series fields when no scan is awaited,
and requests at most 1 library scan.
"""

import logging
from datetime import timedelta
from pathlib import PurePosixPath

from manga_pipeline import config, db, errors, kavita_link, retention, rewrite, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.db import sql
from manga_pipeline.metadata import kavita_write
from manga_pipeline.stages import comicinfo, confirm, publish

log = logging.getLogger(__name__)

REQUESTED_AT = "export.kavita_scan_requested_at"  # meta: when the last library scan request succeeded


def run(cfg, conn):
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.context(cfg, conn, "export"), work)


def work(ctx: workflow.Context):
    with workflow.share_errors():
        retention.purge(ctx.cfg)
    # The comparison (SPEC "Retroactive updates"): rewrite chapters for comicinfo and publish below
    rewrite.compare(ctx)
    workflow.drain(ctx, {"comicinfo": _comicinfo})
    for series_id in workflow.series_with_ready(ctx, ("publish",)):
        # A new series Kavita does not hold yet is published by new_series (SPEC "Publishing": it lands whole)
        if not _in_kavita(ctx, series_id) or folder_moved(ctx, series_id):
            continue
        workflow.drain(ctx, {"publish": _publish}, series_id)
    # Every queued rewrite, this run's and what a crash or a reset left: 1 try a run
    rewrite.finish_all(ctx)
    # Kavita unreachable here ends the run: what is left all needs Kavita.
    workflow.end_waits(ctx, "kavita_scan", confirm.wait_over)
    # Confirm before the new series and the request: every chapter a run published before is then indexed or waiting
    # on kavita_scan, so the awaited test sees it.
    workflow.drain(ctx, {"confirm": confirm.run})
    with workflow.share_errors():
        new_series(ctx)
    rewrite.restore_all(ctx)
    # Kavita holds series writes while its scan runs: written by a run when no scan is awaited
    if scan_awaited(ctx) is None:
        with workflow.share_errors():
            kavita_write.run(ctx)
    request_scan(ctx)


# A stage a new series' chapter is still on its way through (SPEC "Publishing": it lands whole)
BEFORE_PUBLISH = workflow.STAGES[: workflow.STAGES.index("publish")]


def new_series(ctx: workflow.Context):
    """Publish each new series Kavita does not hold that is ready, in a run where no scan is awaited: none of its
    chapters ready or claimed before publish, and its batch open (nothing published, or not yet followed by a scan
    request). Every one ready lands under the run's 1 library scan.
    """
    if scan_awaited(ctx) is not None:
        return
    for series_id in workflow.series_with_ready(ctx, ("publish",)):
        series = db.one_row(
            ctx.conn,
            """SELECT s.*, EXISTS (SELECT 1 FROM chapter c WHERE c.series_id = s.id
                    AND c.status IN ('ready', 'claimed') AND c.stage = ANY(%s::text[])) AS on_its_way,
                EXISTS (SELECT 1 FROM published_file p WHERE p.series_id = s.id) AS published
            FROM series s WHERE s.id = %s""",
            (list(BEFORE_PUBLISH), series_id),
        )
        if series["kavita_series_id"] is not None or series["on_its_way"]:
            continue
        if series["published"] and not series["scan_due"]:
            continue  # its batch's scan is requested: the rest waits for its Kavita id
        if _folder_ready(ctx, series):
            workflow.drain(ctx, {"publish": publish.run}, series_id)


def _folder_ready(ctx: workflow.Context, series):
    """Create the series' folder, or check the one there holds only its own files: those it published and those a crash
    left renamed in but unrecorded (a chapter at publish whose staged file is gone and whose file has its staged size);
    anything else, or a folder that cannot be made or read, makes the series needs_human.
    """
    folder = config.to_local(ctx.cfg, series["folder"])
    try:
        if not errors.exists(folder):
            folder.mkdir()
            return True
        names = [p.name for p in folder.iterdir()]
        own = _own_files(ctx, series, folder)
    except OSError as e:
        if e.errno in errors.SHARE_ERRNOS or errors.dropped_share(e):
            raise  # the share: the run's problem (share_errors)
        return _hold(ctx, series, f"folder {series['folder']} cannot be made or read: {e}")
    foreign = sorted(n for n in names if n.lower().endswith(".cbz") and n not in own)
    if not foreign:
        return True
    listed = ", ".join(foreign[:3])
    return _hold(ctx, series, f"folder {series['folder']} holds files the pipeline did not publish: {listed}")


def _own_files(ctx: workflow.Context, series, folder):
    """Names of the files the series published, and of those a crash left renamed in but unrecorded."""
    own = {
        PurePosixPath(r[0]).name
        for r in ctx.conn.execute("SELECT library_path FROM published_file WHERE series_id = %s", (series["id"],))
    }
    for chapter in ctx.conn.execute(
        """SELECT staged_path, staged_size, library_path FROM chapter
        WHERE series_id = %s AND stage = 'publish' AND library_path IS NOT NULL""",
        (series["id"],),
    ):
        target = folder / PurePosixPath(chapter["library_path"]).name
        if (
            not errors.exists(config.to_local(ctx.cfg, chapter["staged_path"]))
            and errors.exists(target)
            and target.stat().st_size == chapter["staged_size"]
        ):
            own.add(target.name)
    return own


def _hold(ctx: workflow.Context, series, reason):
    with db.transaction(ctx.conn):
        workflow.needs_human(ctx.conn, "series", series["id"], reason)
    log.warning("%s: series %s (%s): %s", ctx.part, series["id"], series["name"], reason)
    return False


def _comicinfo(ctx: workflow.Context, chapter):
    return (rewrite.comicinfo_run if chapter["kind"] == "rewrite" else comicinfo.run)(ctx, chapter)


def _publish(ctx: workflow.Context, chapter):
    # a rewrite, or a better copy, queues on its file's row (SPEC "Retroactive updates")
    return (rewrite.publish_run if chapter["published_file_id"] is not None else publish.run)(ctx, chapter)


def _in_kavita(ctx: workflow.Context, series_id):
    row = db.one_row(ctx.conn, "SELECT kavita_series_id FROM series WHERE id = %s", (series_id,))
    return row["kavita_series_id"] is not None


def folder_moved(ctx: workflow.Context, series_id):
    """Whether a Kavita series' files lie elsewhere than its recorded folder (or in no single folder): then the series
    is needs_human, naming both, and nothing is published into it. A new series (no Kavita id) is not checked, nor one
    Kavita no longer holds (confirm reports that, with its fix).
    """
    series = db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (series_id,))
    kid = series["kavita_series_id"]
    if kid is None or ctx.kavita.series(kid) is None:
        return False
    try:
        found = kavita_link.library_folder(ctx.cfg, ctx.kavita.series_folder(kid))
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


def scan_awaited(ctx: workflow.Context):
    """The time of the last library scan request while it is still awaited (less than kavita.rescan_after_minutes ago,
    with chapters still waiting on kavita_scan), else None.
    """
    previous = workflow.get_meta(ctx.conn, REQUESTED_AT)
    if previous is None or not _waiting(ctx):
        return None
    cutoff = workflow.after(workflow.now(ctx.conn), -timedelta(minutes=ctx.cfg["kavita"]["rescan_after_minutes"]))
    return previous if previous > cutoff else None


def _waiting(ctx: workflow.Context):
    """Chapters waiting on kavita_scan, and rewritten files waiting for Kavita to re-read them."""
    return (
        rewrite.waiting(ctx.conn)
        + db.one_row(
            ctx.conn,
            sql.SQL("""SELECT COUNT(*) FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = 'kavita_scan' AND s.status = 'ok' AND {scope}""").format(
                scope=workflow.SCOPE
            ),
            workflow.scope_params(ctx.cfg),
        )[0]
    )


def request_scan(ctx: workflow.Context):
    """At most 1 library scan per run, never while one is awaited (requested less than kavita.rescan_after_minutes
    ago with chapters still waiting on kavita_scan): requested when a publish made a scan due or chapters still wait.
    A repeat request charges 1 attempt to each chapter already waiting at the previous one. A failed request writes
    nothing and is decided again by the next run.
    """
    minutes = ctx.cfg["kavita"]["rescan_after_minutes"]
    previous = workflow.get_meta(ctx.conn, REQUESTED_AT)
    waiting = _waiting(ctx)
    if scan_awaited(ctx) is not None:
        log.info("%s: library scan requested at %s still awaited (%d chapter(s) wait)", ctx.part, previous, waiting)
        return  # ponytail: scan_awaited reads the same 2 values again; 2 cheap queries
    due = db.one_row(ctx.conn, "SELECT EXISTS (SELECT 1 FROM series WHERE scan_due = 1)")[0]
    if not (due or waiting):
        return
    try:
        ctx.kavita.scan_library(ctx.cfg["kavita"]["library_id"])
    except http.HttpError:
        log.exception("%s: library scan request failed", ctx.part)
        return
    requested = workflow.now(ctx.conn)  # 1 stamp: a charged chapter must not count as waiting since after the request
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE series SET scan_due = 0 WHERE scan_due = 1")
        workflow.set_meta(ctx.conn, REQUESTED_AT, requested)
        charged = 0
        if previous is not None:
            reason = f"not indexed {minutes} minutes after the library scan request"
            charged = workflow.charge_waits(ctx, "kavita_scan", reason, previous, requested)
            charged += rewrite.charge_waits(ctx, previous, requested)
    log.info("%s: library scan requested", ctx.part)
    if charged:
        log.warning("%s: %d chapter(s) still not indexed charged 1 attempt", ctx.part, charged)
