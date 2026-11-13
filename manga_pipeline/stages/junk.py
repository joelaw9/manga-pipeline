"""Stage junk (SPEC "Chapter stages", "State", "Bad downloads"): first the series' baseline (a series Kavita holds
waits on bulk_conversion until its record parses), then the put-aside check that needed it; junk removal itself is a
stub (Phase 5).

Stub wait, for tests: while a marker file <download stem>.wait sits next to the download, the chapter waits on
junk_decision; `process` ends the wait once the marker is gone (wait_over).
"""

from pathlib import Path

from manga_pipeline import baseline, handled, library_copy, workflow


def run(ctx, chapter):
    series = ctx.conn.execute("SELECT * FROM series WHERE id = ?", (chapter["series_id"],)).fetchone()
    if baseline.needed(series):
        why = baseline.take(ctx, series)
        if why:
            return workflow.Waiting("bulk_conversion", {"why": why})
    why = handled.put_aside(ctx, chapter, chapter["content_digest"])
    if why:
        return workflow.Dropped(why)
    outcome = library_copy.judge(ctx, chapter)
    if outcome:
        return outcome
    marker = _marker(chapter)
    if marker.exists():
        return workflow.Waiting("junk_decision", {"marker": str(marker)})
    return workflow.Done()


def bulk_conversion_over(ctx, chapter):
    """The bulk_conversion wait ends once the series has a baseline or its record parses."""
    series = ctx.conn.execute("SELECT * FROM series WHERE id = ?", (chapter["series_id"],)).fetchone()
    return not baseline.needed(series) or baseline.record_problem(series) is None


def wait_over(ctx, chapter):
    return not _marker(chapter).exists()


def _marker(chapter):
    return Path(chapter["download_path"]).with_suffix(".wait")
