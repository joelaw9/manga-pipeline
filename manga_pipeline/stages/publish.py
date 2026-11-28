"""Stage publish (SPEC "Chapter stages", "Publishing"): renames the staged CBZ into the series folder.

It never renames over a library file, and records the published file, the handled chapter and the series' due scan
with the rename's outcome. The scan itself is `export`'s 1 library scan, after all its publishes.
"""

import json
from pathlib import PurePosixPath
from typing import TypedDict

from manga_pipeline import config, db, errors, measures, page_store, rewrite_covers, workflow
from manga_pipeline.convert import plan
from manga_pipeline.db import sql
from manga_pipeline.stages import convert


class FileValues(TypedDict):
    """The published_file columns publish records of a chapter's download (row_values)."""

    series_id: int
    identity: str
    source: str | None
    size: int
    median_page_width: int | None
    strip_length: int | None
    page_phashes: str | None  # JSON text
    pages_in: int | None
    bpp: float | None
    junk: float | None
    pages_out: None
    parse_fields: None
    comicinfo_series: str
    comicinfo_volume: str | None
    comicinfo_number: str
    comicinfo_title: str | None
    comicinfo_date: str | None
    origin: str
    source_name: str
    kind: str
    volume_source: str
    cover_state: str


ROW_VALUES = tuple(FileValues.__annotations__)  # row_values' columns, in order


def row_values(conn: db.Connection, chapter: db.Row) -> FileValues:
    """Return {published_file column: value} of a chapter's download as publish records it.

    A better copy's are applied to its file's row (SPEC "Retroactive updates"). The values: its identity, source, names
    and the original measurements a later copy of the chapter is judged against (SPEC "Library copies": none for a
    chapter with no page rows; its lengths, page count and strip length, leave a 000_cover page out).
    """
    pages = list(page_store.load(conn, chapter["id"]).values())
    m = measures.summary(pages)
    lengths = measures.summary(rewrite_covers.content_pages(pages))
    cover = convert.read_result(chapter["convert_result"])["cover"]
    edit = conn.execute("SELECT edit FROM chapter_edit WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    layout = conn.execute(
        "SELECT layout FROM series_conversion WHERE series_id = %s", (chapter["series_id"],)
    ).fetchone()
    junk = None
    if (width := m["median_page_width"]) is not None and edit is not None and layout is not None:
        junk = measures.junk(plan.check_edit(edit["edit"]), layout["layout"], width)
    return {
        "series_id": chapter["series_id"],
        "identity": chapter["identity"],
        "source": chapter["source"],
        "size": chapter["staged_size"],
        "median_page_width": m["median_page_width"],
        "strip_length": lengths["strip_length"],
        "page_phashes": None if m["page_phashes"] is None else json.dumps(m["page_phashes"]),
        "pages_in": lengths["pages_in"],
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


def queued(conn: db.Connection, series_id: int, stored_path: str, file_id: int | None = None) -> bool:
    """Return whether another file of the series has a rewrite or a better copy queued to stored_path.

    file_id: the file renamed, whose own queued path does not count (None: a new file).
    """
    return (
        conn.execute(
            "SELECT 1 FROM published_file WHERE series_id = %s AND pending_path = %s AND id IS DISTINCT FROM %s",
            (series_id, stored_path, file_id),
        ).fetchone()
        is not None
    )


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Rename the staged CBZ into the series' folder as it is now, never over a library file, and record the result.

    A target already in the library or queued for another file (queued), or one left at another size by a lost
    staged file, is needs_human; a rename done before a crash carries on. The record: the published file
    (unconfirmed), the handled chapter, the series' due scan.
    """
    # The series' folder as it is now (a person may have linked it again since comicinfo named the file): the name
    # comicinfo settled, in that folder.
    folder = db.one_row(ctx.conn, "SELECT folder FROM series WHERE id = %s", (chapter["series_id"],))[0]
    staged = config.to_local(ctx.cfg, chapter["staged_path"])
    target = config.to_local(ctx.cfg, folder) / PurePosixPath(chapter["library_path"]).name
    library_path = config.to_stored(ctx.cfg, target)
    if not errors.exists(target.parent):
        msg = f"series folder {target.parent} does not exist"
        raise FileNotFoundError(msg)
    if queued(ctx.conn, chapter["series_id"], library_path):
        return workflow.NeedsHuman(f"{target} is queued for another file; a library file is never overwritten")
    if errors.exists(staged):
        if errors.exists(target):
            return workflow.NeedsHuman(f"{target} is already in the library; a library file is never overwritten")
        workflow.holds_claim(ctx, chapter)
        # ponytail: check-then-replace; only another writer in the same folder in between could lose a file.
        staged.replace(target)
    elif not errors.exists(target):
        msg = f"neither the staged {staged} nor the published {target} exists"
        raise FileNotFoundError(msg)
    elif target.stat().st_size != chapter["staged_size"]:
        return workflow.NeedsHuman(f"{target} is already in the library at another size; the staged file is gone")
    # else: the rename happened before a crash; carry on

    def record(conn: db.Connection) -> None:
        values: dict[str, object] = {"library_path": library_path, "state": "unconfirmed", **row_values(conn, chapter)}
        # a reused row's are a gone file's
        values |= {"original": None, "previous": None, "previous_values": None, "pending_previous": None}
        values["unedited"] = None if chapter["naming_unedited"] is None else json.dumps(chapter["naming_unedited"])
        # A row already at the target describes a file that is gone (the target was free): the new file replaces it.
        conn.execute(
            sql.SQL("""INSERT INTO published_file ({columns}) VALUES ({places})
            ON CONFLICT (library_path) DO UPDATE SET
                {updates}""").format(
                columns=sql.SQL(", ").join(map(sql.Identifier, values)),
                places=sql.SQL(", ").join([sql.Placeholder()] * len(values)),
                updates=sql.SQL(", ").join(
                    sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c)) for c in values if c != "library_path"
                ),
            ),
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
