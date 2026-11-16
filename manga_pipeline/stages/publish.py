"""Stage publish (SPEC "Chapter stages", "Publishing"): renames the staged CBZ into the series folder, never over a
library file, and records the published file, the handled chapter and the series' due scan with the rename's
outcome. The scan itself is `export`'s 1 library scan, after all its publishes.
"""

import json
from pathlib import PurePosixPath

from manga_pipeline import config, measures, page_store, rewrite_covers, workflow


def row_values(conn, chapter):
    """{published_file column: value} of a chapter's download as publish records it (a better copy's are applied to
    its file's row: SPEC "Retroactive updates"): its identity, source, names and the original measurements a later copy
    of the chapter is judged against (SPEC "Library copies": none for a chapter with no page rows)."""
    pages = list(page_store.load(conn, chapter["id"]).values())
    m = measures.summary(pages)
    cover = json.loads(chapter["convert_result"])["cover"]
    edit = conn.execute("SELECT edit FROM chapter_edit WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    layout = conn.execute(
        "SELECT layout FROM series_conversion WHERE series_id = %s", (chapter["series_id"],)
    ).fetchone()
    junk = None
    if pages and edit is not None and layout is not None:
        junk = measures.junk(json.loads(edit["edit"]), layout["layout"], m["median_page_width"])
    return {
        "series_id": chapter["series_id"],
        "identity": chapter["identity"],
        "source": chapter["source"],
        "size": chapter["staged_size"],
        "median_page_width": m["median_page_width"],
        "strip_length": m["strip_length"],
        "page_phashes": None if m["page_phashes"] is None else json.dumps(m["page_phashes"]),
        "pages_in": m["pages_in"],
        "bpp": measures.bits_per_pixel(pages) if pages else None,
        "junk": junk,
        "pages_out": None,
        "parse_fields": None,
        "comicinfo_series": chapter["comicinfo_series"],
        "comicinfo_volume": chapter["comicinfo_volume"],
        "comicinfo_number": chapter["comicinfo_number"],
        "comicinfo_title": chapter["comicinfo_title"],
        "comicinfo_date": chapter["comicinfo_date"],
        "origin": "pipeline",
        "source_name": PurePosixPath(chapter["download_path"]).stem,
        "kind": chapter["naming_kind"],
        "volume_source": chapter["naming_volume_source"],
        # the volume cover convert inserted; stale when MangaDex has a new image of that volume since
        "cover_state": rewrite_covers.current_state(
            conn, chapter["id"], chapter["series_id"], f"volume:{cover['volume']}" if cover else "none"
        ),
    }


def run(ctx, chapter):
    # The series' folder as it is now (a person may have linked it again since comicinfo named the file): the name
    # comicinfo settled, in that folder.
    folder = ctx.conn.execute("SELECT folder FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()[0]
    staged = config.to_local(ctx.cfg, chapter["staged_path"])
    target = config.to_local(ctx.cfg, folder) / PurePosixPath(chapter["library_path"]).name
    library_path = config.to_stored(ctx.cfg, target)
    if not workflow.exists(target.parent):
        msg = f"series folder {target.parent} does not exist"
        raise FileNotFoundError(msg)
    if workflow.exists(staged):
        if workflow.exists(target):
            return workflow.NeedsHuman(f"{target} is already in the library; a library file is never overwritten")
        workflow.holds_claim(ctx, chapter)
        # ponytail: check-then-replace; only another writer in the same folder in between could lose a file.
        staged.replace(target)
    elif not workflow.exists(target):
        msg = f"neither the staged {staged} nor the published {target} exists"
        raise FileNotFoundError(msg)
    elif target.stat().st_size != chapter["staged_size"]:
        return workflow.NeedsHuman(f"{target} is already in the library at another size; the staged file is gone")
    # else: the rename happened before a crash; carry on

    def record(conn):
        values = {"library_path": library_path, "state": "unconfirmed", **row_values(conn, chapter)}
        values["unedited"] = None if chapter["naming_unedited"] is None else json.dumps(chapter["naming_unedited"])
        # A row already at the target describes a file that is gone (the target was free): the new file replaces it.
        conn.execute(
            f"""INSERT INTO published_file ({", ".join(values)}) VALUES ({", ".join(["%s"] * len(values))})
            ON CONFLICT (library_path) DO UPDATE SET
                {", ".join(f"{c} = excluded.{c}" for c in values if c != "library_path")}""",
            tuple(values.values()),
        )
        conn.execute(
            """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
            VALUES (%s, %s, 'published', %s, %s)""",
            (chapter["series_id"], chapter["identity"], chapter["content_digest"], workflow.now(conn)),
        )
        conn.execute("UPDATE series SET scan_due = 1 WHERE id = %s", (chapter["series_id"],))
        conn.execute("UPDATE chapter SET library_path = %s WHERE id = %s", (library_path, chapter["id"]))

    return workflow.Done(record)
