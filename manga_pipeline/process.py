"""Part process (SPEC "Parts"): junk, then convert, picking by priority after every chapter; a series' ready junk
stage chapters are all scanned before any is decided, and all run before any of it converts (workflow.claim). First
it removes what a killed run of it left: chapter scratch folders on this machine, and staged files no chapter names
(SPEC "Conversion"). junk review cleans up the junk candidates (SPEC "Junk decisions").
"""

import logging
import re
import shutil
import time

from manga_pipeline import config, errors, workflow
from manga_pipeline.stages import convert, junk

log = logging.getLogger(__name__)
STAGES = {"junk": junk.run, "convert": convert.run}
STAGED_KEEP_S = 24 * 3600  # a staged file no chapter names is deleted once this old


def run(cfg, conn):
    """conn: the command's connection, holding its run lock (cli.main)."""
    workflow.run_part(workflow.Context(cfg, conn, "process"), work)


def work(ctx):
    with workflow.share_errors():
        clean_scratch(ctx.cfg)
        clean_staging(ctx)
    workflow.end_waits(ctx, "junk_decision", junk.junk_decision_over)
    workflow.drain(ctx, STAGES)


def clean_scratch(cfg):
    """Remove the chapter folders (<id>-<count>) in this machine's scratch folder and nothing else there (junk review
    keeps its own folder in it). Not with lock.enabled false: another run may be using them."""
    if not cfg["lock"]["enabled"]:
        return
    root = config.abs_path(cfg["paths"]["scratch"])
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir() and re.fullmatch(workflow.CLAIM_NAME, entry.name):
            shutil.rmtree(entry)


def clean_staging(ctx):
    """Delete a staged .cbz no chapter names, or a .cbz.part (a killed archive write's), once it is a day old (a run
    whose claim was freed may have left one); the owner file and anything else in the staging folder are kept. A file
    another machine's process start deleted first is skipped."""
    root = config.abs_path(ctx.cfg["paths"]["staging"])
    named = {row[0] for row in ctx.conn.execute("SELECT staged_path FROM chapter WHERE staged_path IS NOT NULL")}
    # a queued rewrite's staged file belongs to its published file's row, whatever became of its chapter
    named |= {
        row[0] for row in ctx.conn.execute("SELECT pending_staged FROM published_file WHERE pending_staged IS NOT NULL")
    }
    cutoff = time.time() - STAGED_KEEP_S
    for entry in [*root.glob("*.cbz"), *root.glob("*.cbz.part")]:
        if config.to_stored(ctx.cfg, entry) in named:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except FileNotFoundError as e:
            if errors.dropped_share(e):
                raise
            continue
        log.info("process: deleted %s: staged by a run whose claim was freed", entry)
