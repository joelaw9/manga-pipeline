"""Stage validate (SPEC "Chapter stages"). Stub: accepts every chapter. Phase 4 replaces it."""

from manga_pipeline import workflow


def run(ctx, chapter):
    return workflow.Done()
