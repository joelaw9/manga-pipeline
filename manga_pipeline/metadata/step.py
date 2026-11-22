"""`intake`'s metadata step (SPEC "Metadata checks"): sync, link, baseline, check, admit.

Bring the series rows in step with the library, link library manga that have no downloads, run the due checks, each
series alone, then take once a day the baseline of the library's series that have none (SPEC "State"), then admit the
new series whose check completed (metadata/admit.py). The step never stops intake's chapter work:
a service that cannot be reached skips the step, and a check that fails is logged and stays due. Only a failing share or
database ends the run.
"""

import logging
from collections.abc import Sequence
from datetime import timedelta

from manga_pipeline import adoption, baseline, db, errors, identify, incidents, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.db import sql
from manga_pipeline.metadata import admit, check, covers, library, sources
from manga_pipeline.replies import kavita as kavita_replies

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


def run(ctx: workflow.Context, clients: sources.Sources) -> None:
    """Run the metadata step: sync and link the library, wanted covers, the due checks, the baseline pass, admission.

    A service failure while syncing or linking skips the rest; a baseline pass, a cover fetch or a check that does not
    complete is logged and tried again next run. A database or share failure in a check raises; any other check
    failure is that series' (logged).
    """
    kavita_series = identify.KavitaSeries(ctx)
    try:
        listed = kavita_series.rows()
        incidents.clear_key_refused(ctx.conn)  # Kavita answered: a key incident already sent is over
        library.sync(ctx, listed)
        identify.sweep(ctx, ctx.suwayomi.library_manga(), kavita_series)
    except CHECK_FAILURES as e:
        log.warning("%s: metadata step skipped: %s", ctx.part, e)
        return
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
    try:  # after the checks: a series whose check completed now is adopted now
        baseline_pass(ctx, kavita_series.rows())
    except CHECK_FAILURES as e:
        if isinstance(e, workflow.Unreachable) and e.service == workflow.SHARE:
            raise  # the share: the run's problem, as everywhere in intake
        log.warning("%s: baseline pass not completed: %s", ctx.part, e)
    admit.run(ctx, kavita_series.rows())


def baseline_pass(ctx: workflow.Context, listed_series: Sequence[kavita_replies.Series]) -> None:
    """Take, once a day, the baseline of the library's series that have none.

    Those are every in-scope ok series Kavita holds (listed_series: the step's listing) that has none (or lacks its
    naming fields): from its record when its folder holds one, else adopted once nothing holds its adoption back
    (adoption.waiting), so a series no download reaches has published files to compare. A series whose folder Kavita
    gives no usable form is, when it is adoptable (no baseline, a completed check, no chapter at discover), adopted
    through baseline.take all the same, which holds it (needs_human); else it is left as it is, as is one that is not
    adopted yet and holds no record; a record that does not parse is logged (validate holds the series' downloads).
    """
    conn = ctx.conn
    last = workflow.get_meta(conn, BASELINE_PASS_AT)
    now = workflow.now(conn)
    if last is not None and last > workflow.after(now, -timedelta(days=1)):
        return
    rows = conn.execute(
        sql.SQL("""SELECT s.* FROM series s
            WHERE s.status = 'ok' AND s.origin = 'kavita' AND s.kavita_series_id = ANY(%(listed)s)
            AND {scope} ORDER BY s.id""").format(scope=workflow.SCOPE),
        {**workflow.scope_params(ctx.cfg), "listed": [s["id"] for s in listed_series]},
    ).fetchall()
    taken = 0
    for listed in rows:
        if not baseline.needed(conn, listed):
            continue
        if listed["baseline_at"] is None and adoption.waiting(conn, listed["id"]) is None:
            series = listed  # take reads its folder (once), then takes its record or adopts it
        else:  # not adoptable: only a record is taken
            series = _with_folder(ctx, listed)
            if series is None or not baseline.has_record(ctx.cfg, series["folder"]):
                continue
        if _take(ctx, series):
            taken += 1
    with db.transaction(conn):
        workflow.set_meta(conn, BASELINE_PASS_AT, now)
    if taken:
        log.info("%s: baseline pass: %d series", ctx.part, taken)


def _with_folder(ctx: workflow.Context, series: db.Row) -> db.Row | None:
    """Return the series row with its folder stored (baseline.with_folder).

    None when Kavita gives no usable one, or replies with an error: this series' (logged).
    """
    try:
        found = baseline.with_folder(ctx, series)
    except http.HttpError as e:
        log.info("%s: series %s (%s): folder not read: %s", ctx.part, series["id"], series["name"], e)
        return None
    if isinstance(found, adoption.Problem):  # held only where it is adopted or a download needs the folder
        log.info("%s: series %s (%s): folder not read: %s", ctx.part, series["id"], series["name"], found.text)
        return None
    return found


def _take(ctx: workflow.Context, series: db.Row) -> bool:
    """Take the series' baseline; return whether it was taken (why not logged).

    The database, the share or a service down raises: the run's problem (the pass is tried again); any other failure
    is this series' (logged).
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
