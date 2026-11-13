"""Stage publish (SPEC "Chapter stages", "Publishing"): renames the staged CBZ into the series folder, never over a
library file, and records the published file, the handled chapter and the series' due scan with the rename's
outcome. The scan itself is requested by `export` after the series' publishes.
"""

import os
from pathlib import Path

from manga_pipeline import workflow


def run(ctx, chapter):
    staged, target = Path(chapter["staged_path"]), Path(chapter["library_path"])
    if not target.parent.is_dir():
        raise FileNotFoundError(f"series folder {target.parent} does not exist")
    if staged.exists():
        if target.exists():
            return workflow.NeedsHuman(f"{target} is already in the library; a library file is never overwritten")
        # ponytail: check-then-replace; only another writer in the same folder in between could lose a file.
        os.replace(staged, target)
    elif not (target.exists() and target.stat().st_size == chapter["staged_size"]):
        raise FileNotFoundError(f"neither the staged {staged} nor the published {target} (of the staged size) exists")
    # else: the rename happened before a crash; carry on

    def record(conn):
        # A row already at the target describes a file that is gone (the target was free): the new file replaces it.
        published_id = conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, source, state, size, comicinfo_series,
                comicinfo_volume, comicinfo_number, comicinfo_title, comicinfo_date, origin)
            VALUES (?, ?, ?, ?, 'unconfirmed', ?, ?, ?, ?, ?, ?, 'pipeline')
            ON CONFLICT (library_path) DO UPDATE SET series_id = excluded.series_id, identity = excluded.identity,
                source = excluded.source, parse_fields = NULL, state = excluded.state, size = excluded.size,
                median_page_width = NULL, strip_length = NULL, page_phashes = NULL, pages_in = NULL,
                pages_out = NULL, comicinfo_series = excluded.comicinfo_series,
                comicinfo_volume = excluded.comicinfo_volume, comicinfo_number = excluded.comicinfo_number,
                comicinfo_title = excluded.comicinfo_title, comicinfo_date = excluded.comicinfo_date,
                origin = excluded.origin
            RETURNING id""",
            (
                str(target),
                chapter["series_id"],
                chapter["identity"],
                chapter["source"],
                chapter["staged_size"],
                chapter["comicinfo_series"],
                chapter["comicinfo_volume"],
                chapter["comicinfo_number"],
                chapter["comicinfo_title"],
                chapter["comicinfo_date"],
            ),
        ).fetchone()[0]
        conn.execute("UPDATE chapter SET published_file_id = ? WHERE id = ?", (published_id, chapter["id"]))
        conn.execute(
            """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
            VALUES (?, ?, 'published', ?, ?)""",
            (chapter["series_id"], chapter["identity"], chapter["content_digest"], workflow.now()),
        )
        conn.execute("UPDATE series SET scan_due = 1 WHERE id = ?", (chapter["series_id"],))

    return workflow.Done(record)
