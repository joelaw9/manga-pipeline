"""Part process (SPEC "Parts"): junk, then convert, picking by priority after every chapter; a series' ready junk
stage chapters are all scanned before any is decided, and all run before any of it converts (workflow.claim). Closed
junk candidates are cleaned up first (SPEC "Junk decisions")."""

from contextlib import closing

from manga_pipeline import db, workflow
from manga_pipeline.junk import candidates
from manga_pipeline.stages import convert, junk

STAGES = {"junk": junk.run, "convert": convert.run}


def run(cfg):
    with closing(db.connect(cfg)) as conn:
        workflow.run_part(workflow.Context(cfg, conn, "process"), work)


def work(ctx):
    workflow.end_waits(ctx, "bulk_conversion", junk.bulk_conversion_over)
    candidates.cleanup(ctx.cfg, ctx.conn)
    workflow.end_waits(ctx, "junk_decision", junk.junk_decision_over)
    workflow.drain(ctx, STAGES)
