"""Notice titles (SPEC "Chapter stages": discover and junk): a download whose title is a notice or hiatus page.

Notice titles are validate.non_chapter_names. An unnumbered one is dropped at discover. A numbered one is judged once
at junk, where the series has its baseline and the chapter its page rows, by the bulk converter's rule (its
optimizer/structure.py `settle_non_chapters`): kept as a chapter with a note when it has an end marker, at least the
series' median page count, or no known median; else dropped as a notice. A chapter carrying the note is not judged
again.
"""

import json
import statistics
from pathlib import Path

from manga_pipeline import archive, comicinfo, config, db, handled, names, page_store, retention, validation, workflow
from manga_pipeline.db import sql

KEPT = "a numbered notice title kept as a chapter"


def title(cfg: config.Config, download: Path, number: str) -> names.Item:
    """Parse a download's name against validate.non_chapter_names: kind NON_CHAPTER for a notice title.

    Its ComicInfo Number numbers it when the name has none.
    """
    ci = {"Number": number} if number else None
    return names.parse_name(Path(download).stem, ci, cfg["validate"]["non_chapter_names"])


def keep(pages: int, title_text: str, median: float | None) -> str | None:
    """Return why a numbered notice title is a chapter (the bulk converter's rule), or None: it is a notice.

    median: the series' median page count, None when unknown (a drop needs a known one).
    """
    why: list[str] = []
    if names.END_MARKER.search(title_text):
        why.append("end marker")
    if median is None:
        why.append("no median page count known for the series")
    elif pages >= median:
        why.append(f"{pages} pages >= series median {median:g}")
    return ", ".join(why) or None


def median(conn: db.Connection, series_id: int) -> float | None:
    """Return the series' median page count like with like, or None with fewer than validation.MIN_PUBLISHED files.

    In a page-layout series over every published file's source page count (its output's for a bulk-converter file); in
    a strip series over the files the pipeline published and the adopted ones (a bulk-converter strip file holds re-cut
    pages; an adopted file holds its download's).
    """
    row = conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is None and not conn.execute("SELECT 1 FROM published_file WHERE series_id = %s", (series_id,)).fetchone():
        return None  # a new series before its layout is measured: nothing published to compare with
    if row is None:
        msg = f"series {series_id} has no layout (series_conversion): its page counts cannot be compared"
        raise ValueError(msg)
    strip = row["layout"] == "strip"
    count = sql.SQL(
        "CASE WHEN origin = 'library' THEN pages_out ELSE pages_in END" if strip else "coalesce(pages_in, pages_out)"
    )
    origin = sql.SQL("AND origin IN ('pipeline', 'library')") if strip else sql.SQL("")
    counts: list[int] = [
        r[0]
        for r in conn.execute(
            sql.SQL("SELECT {count} FROM published_file WHERE series_id = %s AND {count} IS NOT NULL {origin}").format(
                count=count, origin=origin
            ),
            (series_id,),
        )
    ]
    return statistics.median(counts) if len(counts) >= validation.MIN_PUBLISHED else None


def judge(ctx: workflow.Context, chapter: db.Row) -> workflow.Dropped | None:
    """Judge a chapter's notice title at junk: return None to go on, Dropped for a notice.

    None is no notice title, or one judged a chapter: its note is written now, in its own transaction.
    """
    notes = workflow.read_notes(chapter["notes"] or "[]")
    if any(n.startswith(KEPT) for n in notes):
        return None
    download = config.to_local(ctx.cfg, chapter["download_path"])
    item = title(ctx.cfg, download, comicinfo.download_number(download))
    if item.kind != names.NON_CHAPTER:
        return None
    pages = len(page_store.load(ctx.conn, chapter["id"]))
    if not pages:
        msg = f"chapter {chapter['id']} has no page rows: validate stores them"
        raise ValueError(msg)
    why = keep(pages, item.title, median(ctx.conn, chapter["series_id"]))
    if why is None:
        return drop(ctx, chapter, download)
    note = f"{KEPT} (title {item.title!r}, number {item.number}): {why}"
    with db.transaction(ctx.conn):
        workflow.holds_claim(ctx, chapter)
        ctx.conn.execute("UPDATE chapter SET notes = %s WHERE id = %s", (json.dumps([*notes, note]), chapter["id"]))
    return None


def drop(ctx: workflow.Context, chapter: db.Row, download: Path) -> workflow.Dropped:
    """Drop a notice: remove its download the retention way; the outcome records it as a dropped handled chapter."""
    digest = archive.digest_or_none(download)
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, download)

    def record(conn: db.Connection) -> None:
        handled.record(conn, chapter["series_id"], chapter["identity"], "dropped", digest)

    return workflow.Dropped(f"{download.name}: a notice, not a chapter (validate.non_chapter_names)", record)
