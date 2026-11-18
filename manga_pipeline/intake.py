"""Part intake (SPEC "Parts"): discovers new downloads, then works each series' chapters.

Per series, it takes the chapters through discover and validate.
"""

from manga_pipeline import config, db, discovery, redownload, workflow
from manga_pipeline.metadata import check, step
from manga_pipeline.stages import discover, validate

STAGES = {"discover": discover.run, "validate": validate.run}


def run(cfg: config.Config, conn: db.Connection) -> None:
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.context(cfg, conn, "intake"), work)


def work(ctx: workflow.Context) -> None:
    """Run 1 intake pass: discover new downloads, run the metadata step, then drain each ready series' chapters.

    The waits for a re-download, a bulk conversion and a metadata check are ended on the way; the chapters go through
    discover and validate.
    """
    # Before discovery, so a re-download that ended is judged by validate, not listed as a new download.
    workflow.end_waits(ctx, "suwayomi_download", redownload.wait_over)
    with workflow.share_errors():
        discovery.scan(ctx)
        step.run(ctx, ctx.sources)
        workflow.end_waits(ctx, "bulk_conversion", validate.bulk_conversion_over)
    workflow.end_waits(ctx, "metadata_check", check.completed)
    for series_id in workflow.series_with_ready(ctx, tuple(STAGES)):
        workflow.drain(ctx, STAGES, series_id)
