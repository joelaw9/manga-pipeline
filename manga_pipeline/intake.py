"""Part intake (SPEC "Parts"): discovers new downloads, then per series takes its chapters through discover and
validate."""

from manga_pipeline import discovery, redownload, workflow
from manga_pipeline.stages import discover, validate

STAGES = {"discover": discover.run, "validate": validate.run}


def run(cfg, conn):
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.Context(cfg, conn, "intake"), work)


def work(ctx):
    # Before discovery, so a re-download that ended is judged by validate, not listed as a new download.
    workflow.end_waits(ctx, "suwayomi_download", redownload.wait_over)
    with workflow.share_errors():
        discovery.scan(ctx)
    for series_id in workflow.series_with_ready(ctx, tuple(STAGES)):
        # Phase 7: a new series' full metadata retrieval, and a metadata check when one is due, go here.
        workflow.drain(ctx, STAGES, series_id)
