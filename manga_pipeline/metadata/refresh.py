"""A completed series' chapter list refresh (SPEC "Suwayomi"): Suwayomi's updater skips completed series, so a due
check asks Suwayomi to fetch the list again, and queues only the chapters that refresh added and the series lacks.
"""

import logging

from manga_pipeline import names, validation, workflow
from manga_pipeline.clients import suwayomi as suwayomi_client

log = logging.getLogger(__name__)
COMPLETED = "COMPLETED"


def completed(ctx: workflow.Context, series_id, manga_rows):
    held = _held_keys(ctx.conn, series_id)
    for manga in manga_rows:
        if manga["status"] != COMPLETED:
            continue
        before = {c["id"] for c in ctx.suwayomi.chapters(manga["id"])}
        try:
            after = ctx.suwayomi.fetch_manga_and_chapters(manga["id"])["chapters"]
        except suwayomi_client.SourceUnavailableError as e:
            log.warning("series %s: chapter refresh of Suwayomi manga %s: %s", series_id, manga["id"], e)
            continue
        # Queued at once, manga by manga: the next refresh counts these chapters as listed before it.
        # ponytail: a failure between the fetch and the enqueue loses them; persist the added ids if that bites.
        queued = []
        for chapter in after:
            if chapter["id"] not in before and key(chapter) not in held:
                held.add(key(chapter))  # 1 copy per chapter key, whichever manga or scanlator lists it
                queued.append(chapter["id"])
        if queued:
            ctx.suwayomi.enqueue(queued)
            ctx.suwayomi.start_downloader()
            log.info(
                "series %s: refresh of completed manga %s queued %d new chapter(s)", series_id, manga["id"], len(queued)
            )


def key(chapter):
    """(season, side label, number) of a Suwayomi chapter, as its download's name gives it."""
    item = names.parse(suwayomi_client.chapter_file(chapter["scanlator"], chapter["name"])[: -len(".cbz")])
    return validation.chapter_key(names.identity(item))


def _held_keys(conn, series_id):
    """The chapter keys of the series' published files, unfinished chapters and handled chapters."""
    rows = conn.execute(
        """SELECT identity FROM published_file WHERE series_id = %(id)s
        UNION ALL SELECT identity FROM chapter WHERE series_id = %(id)s AND identity IS NOT NULL
        UNION ALL SELECT identity FROM handled_chapter WHERE series_id = %(id)s""",
        {"id": series_id},
    )
    return {validation.chapter_key(row[0]) for row in rows}
