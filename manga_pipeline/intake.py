"""Part intake (SPEC "Parts"): discovers new downloads, then per series validates its chapters."""

from contextlib import closing

from manga_pipeline import db, workflow
from manga_pipeline.stages import discover, validate

STAGES = {"discover": discover.run, "validate": validate.run}


def run(cfg):
    with closing(db.connect(cfg)) as conn:
        workflow.run_part(workflow.Context(cfg, conn, "intake"), work)


def work(ctx):
    # Phase 4: end suwayomi_download waits here.
    with workflow.share_errors():
        discover.scan(ctx)
    for series_id in workflow.series_with_ready(ctx, tuple(STAGES)):
        # Phase 7: a new series' full metadata retrieval, and a metadata check when one is due, go here.
        workflow.drain(ctx, STAGES, series_id)
