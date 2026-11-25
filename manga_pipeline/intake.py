"""Part intake (SPEC "Parts"): discovers new downloads, queues adopted files' conversions, then works the chapters.

It takes the chapters through discover and validate, picked by priority as process and export pick them: every
download before any conversion (SPEC "State").
"""

from manga_pipeline import config, conversions, db, discovery, incidents, redownload, workflow
from manga_pipeline.metadata import check, step
from manga_pipeline.stages import discover, validate

STAGES = {"discover": discover.run, "validate": validate.run}


def run(cfg: config.Config, conn: db.Connection) -> None:
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.context(cfg, conn, "intake"), work)


def work(ctx: workflow.Context) -> None:
    """Run 1 intake pass: discover new downloads, run the metadata step, queue conversions, then drain the chapters.

    The waits for a re-download, a bulk conversion and a metadata check are ended on the way; the link folders no open
    chapter uses are removed before the queue; the chapters go through discover and validate by priority. Kavita
    refusing the API key anywhere in the run queues the key incident at its end (SPEC "Needs human").
    """
    try:
        # Before discovery, so a re-download that ended is judged by validate, not listed as a new download.
        workflow.end_waits(ctx, "suwayomi_download", redownload.wait_over)
        with workflow.share_errors():
            discovery.scan(ctx)
            step.run(ctx, ctx.sources)
            workflow.end_waits(ctx, "bulk_conversion", validate.bulk_conversion_over)
        workflow.end_waits(ctx, "metadata_check", check.completed)
        conversions.clean_links(ctx.cfg, ctx.conn)
        conversions.queue(ctx.cfg, ctx.conn)
        workflow.drain(ctx, STAGES)
    finally:
        if ctx.kavita.refused is not None:
            incidents.queue_key_refused(ctx.conn)
