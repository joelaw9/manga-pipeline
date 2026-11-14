"""Stage publish (SPEC "Chapter stages", "Publishing"): renames the staged CBZ into the series folder, never over a
library file, and records the published file, the handled chapter and the series' due scan with the rename's
outcome. The scan itself is `export`'s 1 library scan, after all its publishes.
"""

import json
import os
from pathlib import Path

from manga_pipeline import measures, page_store, workflow


def run(ctx, chapter):
    staged, target = Path(chapter["staged_path"]), Path(chapter["library_path"])
    if not target.parent.is_dir():
        raise FileNotFoundError(f"series folder {target.parent} does not exist")
    if staged.exists():
        if target.exists():
            return workflow.NeedsHuman(f"{target} is already in the library; a library file is never overwritten")
        # ponytail: check-then-replace; only another writer in the same folder in between could lose a file.
        os.replace(staged, target)
    elif not target.exists():
        raise FileNotFoundError(f"neither the staged {staged} nor the published {target} exists")
    elif target.stat().st_size != chapter["staged_size"]:
        return workflow.NeedsHuman(f"{target} is already in the library at another size; the staged file is gone")
    # else: the rename happened before a crash; carry on

    def record(conn):
        # The download's original measurements, which a later copy of the chapter is judged against (SPEC "Library
        # copies"); none for a chapter that has no page rows.
        m = measures.summary(list(page_store.load(conn, chapter["id"]).values()))
        phashes = None if m["page_phashes"] is None else json.dumps(m["page_phashes"])
        # A row already at the target describes a file that is gone (the target was free): the new file replaces it.
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, source, state, size, median_page_width,
                strip_length, page_phashes, pages_in, comicinfo_series, comicinfo_volume, comicinfo_number,
                comicinfo_title, comicinfo_date, origin)
            VALUES (%s, %s, %s, %s, 'unconfirmed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pipeline')
            ON CONFLICT (library_path) DO UPDATE SET series_id = excluded.series_id, identity = excluded.identity,
                source = excluded.source, parse_fields = NULL, state = excluded.state, size = excluded.size,
                median_page_width = excluded.median_page_width, strip_length = excluded.strip_length,
                page_phashes = excluded.page_phashes, pages_in = excluded.pages_in,
                pages_out = NULL, comicinfo_series = excluded.comicinfo_series,
                comicinfo_volume = excluded.comicinfo_volume, comicinfo_number = excluded.comicinfo_number,
                comicinfo_title = excluded.comicinfo_title, comicinfo_date = excluded.comicinfo_date,
                origin = excluded.origin""",
            (
                str(target),
                chapter["series_id"],
                chapter["identity"],
                chapter["source"],
                chapter["staged_size"],
                m["median_page_width"],
                m["strip_length"],
                phashes,
                m["pages_in"],
                chapter["comicinfo_series"],
                chapter["comicinfo_volume"],
                chapter["comicinfo_number"],
                chapter["comicinfo_title"],
                chapter["comicinfo_date"],
            ),
        )
        conn.execute(
            """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
            VALUES (%s, %s, 'published', %s, %s)""",
            (chapter["series_id"], chapter["identity"], chapter["content_digest"], workflow.now()),
        )
        conn.execute("UPDATE series SET scan_due = 1 WHERE id = %s", (chapter["series_id"],))

    return workflow.Done(record)
