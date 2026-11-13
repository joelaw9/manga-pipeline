"""Part process (SPEC "Parts"): junk, then convert, picking by priority after every chapter; a series' ready junk
stage chapters all run before any of it converts (workflow.claim)."""

from contextlib import closing

from manga_pipeline import db, workflow
from manga_pipeline.stages import convert, junk

STAGES = {"junk": junk.run, "convert": convert.run}


def run(cfg):
    with closing(db.connect(cfg)) as conn:
        workflow.run_part(workflow.Context(cfg, conn, "process"), work)


def work(ctx):
    workflow.end_waits(ctx, "bulk_conversion", junk.bulk_conversion_over)
    workflow.end_waits(ctx, "junk_decision", junk.wait_over)
    workflow.drain(ctx, STAGES)
