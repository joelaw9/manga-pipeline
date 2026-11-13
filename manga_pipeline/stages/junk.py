"""Stage junk (SPEC "Chapter stages"). Stub: removes nothing. Phase 5 replaces it.

Stub wait, for tests: while a marker file <download stem>.wait sits next to the download, the chapter waits on
junk_decision; `process` ends the wait once the marker is gone (wait_over).
"""

from pathlib import Path

from manga_pipeline import workflow


def run(ctx, chapter):
    marker = _marker(chapter)
    if marker.exists():
        return workflow.Waiting("junk_decision", {"marker": str(marker)})
    return workflow.Done()


def wait_over(ctx, chapter):
    return not _marker(chapter).exists()


def _marker(chapter):
    return Path(chapter["download_path"]).with_suffix(".wait")
