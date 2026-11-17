"""`intake`'s metadata step (SPEC "Metadata checks"): bring the series rows in step with the library, link library
manga that have no downloads, take once a day the baseline of the library's record series (SPEC "Retroactive updates"),
then run the due checks, each series alone, then admit the new series whose check completed (metadata/admit.py).
The step never stops intake's chapter work:
a service that cannot be reached skips the step, and a check that fails is logged and stays due. Only a failing share or
database ends the run.
"""

import logging
from datetime import timedelta

from manga_pipeline import baseline, config, db, errors, identify, kavita_link, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.convert import record_reader
from manga_pipeline.db import sql
from manga_pipeline.metadata import admit, check, covers, library, sources

log = logging.getLogger(__name__)
# What a service's answer can raise: the check simply did not complete.
CHECK_FAILURES = (
    workflow.Unreachable,
    http.HttpError,
    http.HttpTimeoutError,
    sources.SourceError,
    suwayomi_client.GraphQLError,
)
BASELINE_PASS_AT = "intake.baseline_pass_at"  # meta: when the last baseline pass completed


def run(ctx: workflow.Context, clients):
    kavita_series = identify.KavitaSeries(ctx)
    try:
        library.sync(ctx, kavita_series.rows())
        identify.sweep(ctx, ctx.suwayomi.library_manga(), kavita_series)
    except CHECK_FAILURES as e:
        log.warning("%s: metadata step skipped: %s", ctx.part, e)
        return
    try:
        baseline_pass(ctx)
    except CHECK_FAILURES as e:
        log.warning("%s: baseline pass not completed: %s", ctx.part, e)
    try:
        covers.fetch_wanted(ctx)
    except CHECK_FAILURES as e:  # MangaDex down or failing: tried again next run
        log.warning("%s: wanted volume covers not fetched: %s", ctx.part, e)
    covers.clean(ctx)
    for series_id in check.due(ctx):
        try:
            check.run(ctx, clients, series_id)
        except CHECK_FAILURES as e:
            log.warning("series %s: metadata check not completed: %s", series_id, e)
        except Exception as e:
            if isinstance(e, db.Error) or (
                isinstance(e, OSError) and (errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS)
            ):
                raise  # the database or the share: the run's problem
            # A reply of an unexpected shape, a file this machine cannot write: this series' problem, not the run's
            log.exception("series %s: metadata check failed", series_id)
    admit.run(ctx, kavita_series.rows())


def baseline_pass(ctx: workflow.Context):
    """Once a day: take the baseline of every in-scope ok series Kavita holds that has none (or lacks its naming
    fields) and whose folder holds a bulk-converter record, so a series no download reaches has published files to
    compare. A series whose folder Kavita gives no usable form, or that holds no record, is left as it is; a record
    that does not parse is logged (validate holds the series' downloads, as before).
    """
    conn = ctx.conn
    last = workflow.get_meta(conn, BASELINE_PASS_AT)
    now = workflow.now(conn)
    if last is not None and last > workflow.after(now, -timedelta(days=1)):
        return
    rows = conn.execute(
        sql.SQL("""SELECT s.* FROM series s
            WHERE s.status = 'ok' AND s.origin = 'kavita' AND s.kavita_series_id IS NOT NULL
            AND {scope} ORDER BY s.id""").format(scope=workflow.SCOPE),
        workflow.scope_params(ctx.cfg),
    ).fetchall()
    taken = 0
    for listed in rows:
        if not baseline.needed(conn, listed):
            continue
        series = listed if listed["folder"] is not None else _with_folder(ctx, listed)
        if series is None or not errors.exists(config.to_local(ctx.cfg, series["folder"]) / record_reader.RECORD_NAME):
            continue
        if _take(ctx, series):
            taken += 1
    with db.transaction(conn):
        workflow.set_meta(conn, BASELINE_PASS_AT, now)
    if taken:
        log.info("%s: baseline pass: %d series", ctx.part, taken)


def _with_folder(ctx: workflow.Context, series):
    """The series row once its folder, as Kavita gives it, is stored; None when Kavita gives no usable one (logged)."""
    conn = ctx.conn
    try:
        folder = kavita_link.library_folder(ctx.cfg, ctx.kavita.series_folder(series["kavita_series_id"]))
    except (kavita_client.FolderError, config.PathError, ValueError, http.HttpError) as e:
        # held only where a download needs the folder (SPEC "Metadata checks"); an error reply: this series'
        log.info("%s: series %s (%s): folder not read: %s", ctx.part, series["id"], series["name"], e)
        return None
    with db.transaction(conn):
        conn.execute("UPDATE series SET folder = %s WHERE id = %s AND folder IS NULL", (folder, series["id"]))
    return db.one_row(conn, "SELECT * FROM series WHERE id = %s", (series["id"],))


def _take(ctx: workflow.Context, series):
    """Take the series' baseline; whether it was taken (why not logged). The database, the share or a service down
    raises: the run's problem (the pass is tried again); any other failure is this series' (logged).
    """
    try:
        why = baseline.take(ctx, series)
    except Exception as e:
        if isinstance(e, (db.Error, workflow.Unreachable, http.HttpTimeoutError)) or (
            isinstance(e, OSError) and (errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS)
        ):
            raise  # the database, the share or a service down: the run's problem (the pass is tried again)
        log.exception("%s: series %s (%s): baseline not taken", ctx.part, series["id"], series["name"])
        return False
    if why:
        log.info("%s: series %s (%s): no baseline: %s", ctx.part, series["id"], series["name"], why)
        return False
    return True
