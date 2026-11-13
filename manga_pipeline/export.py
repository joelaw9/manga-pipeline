"""Part export (SPEC "Parts", "Publishing"): empties expired retention files, adds ComicInfo.xml, publishes series by
series, then does what needs Kavita: ends kavita_scan waits, requests the rescans that are due and 1 scan per series
published into, and confirms."""

import logging
from contextlib import closing
from datetime import UTC, datetime, timedelta

from manga_pipeline import db, retention, workflow
from manga_pipeline.clients import http
from manga_pipeline.stages import comicinfo, confirm, publish

log = logging.getLogger(__name__)


def run(cfg):
    with closing(db.connect(cfg)) as conn:
        workflow.run_part(workflow.Context(cfg, conn, "export"), work)


def work(ctx):
    with workflow.share_errors():
        retention.purge(ctx.cfg)
    workflow.drain(ctx, {"comicinfo": comicinfo.run})
    for series_id in workflow.series_with_ready(ctx, ("publish",)):
        workflow.drain(ctx, {"publish": publish.run}, series_id)
    # Kavita unreachable here ends the run: what is left all needs Kavita.
    workflow.end_waits(ctx, "kavita_scan", confirm.wait_over)
    rescan(ctx)
    request_scans(ctx)
    workflow.drain(ctx, {"confirm": confirm.run})


def request_scans(ctx):
    """1 series scan for each in-scope, ok series a publish made due. A failed request leaves it due, free, for the
    next run."""
    rows = ctx.conn.execute(
        f"SELECT s.* FROM series s WHERE s.scan_due = 1 AND s.status = 'ok' AND {workflow.SCOPE} ORDER BY s.id"
    ).fetchall()
    for series in rows:
        _scan(ctx, series)


def rescan(ctx):
    """A series whose chapters still wait on kavita_scan kavita.rescan_after_minutes after its last request gets
    1 more request, and each of those chapters 1 attempt. A series still due its first request is request_scans'."""
    minutes = ctx.cfg["kavita"]["rescan_after_minutes"]
    cutoff = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    rows = ctx.conn.execute(
        f"""SELECT s.* FROM series s WHERE s.status = 'ok' AND s.scan_due = 0 AND {workflow.SCOPE}
            AND (s.scan_requested_at IS NULL OR s.scan_requested_at <= ?)
            AND EXISTS (SELECT 1 FROM chapter c WHERE c.series_id = s.id AND c.status = 'waiting'
                AND c.waiting_on = 'kavita_scan')
        ORDER BY s.id""",
        (cutoff,),
    ).fetchall()
    for series in rows:
        if not _scan(ctx, series):
            continue
        charged = workflow.charge_waits(
            ctx, series["id"], "kavita_scan", f"not indexed {minutes} minutes after the series scan request"
        )
        log.warning("%s: series %s rescanned; %d waiting chapter(s) charged 1 attempt", ctx.part, series["id"], charged)


def _scan(ctx, series):
    """Request the series' scan and record it. Returns whether it was requested; a failed request is logged and left
    for the next run."""
    try:
        ctx.kavita.scan_series(series["kavita_library_id"], series["kavita_series_id"])
    except http.HttpError as e:
        log.error("%s: series scan request for series %s failed: %s", ctx.part, series["id"], e)
        return False
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE series SET scan_requested_at = ?, scan_due = 0 WHERE id = ?", (workflow.now(), series["id"])
        )
    log.info("%s: series scan requested for series %s", ctx.part, series["id"])
    return True
