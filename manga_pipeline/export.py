"""Part export (SPEC "Parts"): comicinfo for the ready chapters, then publish series by series, then confirm."""

from contextlib import closing

from manga_pipeline import db, workflow
from manga_pipeline.stages import comicinfo, confirm, publish


def run(cfg):
    with closing(db.connect(cfg)) as conn:
        workflow.run_part(workflow.Context(cfg, conn, "export"), work)


def work(ctx):
    # Phase 7: end kavita_scan waits here (the chapters move on to confirm).
    workflow.drain(ctx, {"comicinfo": comicinfo.run})
    for series_id in workflow.series_with_ready(ctx, ("publish",)):
        workflow.drain(ctx, {"publish": publish.run}, series_id)
        # Phase 7: 1 library scan for the series goes here.
    workflow.drain(ctx, {"confirm": confirm.run})
