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
    # Phase 3: end bulk_conversion waits here.
    workflow.end_waits(ctx, "junk_decision", junk.wait_over)
    workflow.drain(ctx, STAGES)
