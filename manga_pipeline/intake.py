"""Part intake (SPEC "Parts"): discovers new downloads, then per series takes its chapters through discover and
validate."""

from manga_pipeline import discovery, redownload, workflow
from manga_pipeline.metadata import check, sources, step
from manga_pipeline.stages import discover, validate

STAGES = {"discover": discover.run, "validate": validate.run}


def run(cfg, conn):
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.Context(cfg, conn, "intake"), work)


def work(ctx):
    if ctx.sources is None:  # the run's 1 client of each metadata source: the check and validate's volume covers
        object.__setattr__(ctx, "sources", sources.new_sources())
    # Before discovery, so a re-download that ended is judged by validate, not listed as a new download.
    workflow.end_waits(ctx, "suwayomi_download", redownload.wait_over)
    with workflow.share_errors():
        discovery.scan(ctx)
        step.run(ctx, ctx.sources)
        workflow.end_waits(ctx, "bulk_conversion", validate.bulk_conversion_over)
    workflow.end_waits(ctx, "metadata_check", check.completed)
    for series_id in workflow.series_with_ready(ctx, tuple(STAGES)):
        workflow.drain(ctx, STAGES, series_id)
