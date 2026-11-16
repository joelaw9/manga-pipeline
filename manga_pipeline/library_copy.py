"""Library copies (SPEC "Library copies"): a chapter whose season, side label and number the series already has
published is a duplicate (dropped), a better copy (it takes the file: SPEC "Retroactive updates"), or held for a
person (a doubtful one, or a better copy of a busy file). Judged at junk, once the series' baseline has recorded what
the library holds.
"""

import functools
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from manga_pipeline import (
    archive,
    chapter_names,
    config,
    db,
    errors,
    handled,
    images,
    measures,
    names,
    page_store,
    retention,
    rewrite,
    rewrite_covers,
    validation,
    volumes,
    workflow,
)
from manga_pipeline.convert import record_reader
from manga_pipeline.db import sql
from manga_pipeline.metadata import covers

SAME_CHAPTER = 0.15  # page count or strip length within this share: the same chapter (old duplicate_page_tolerance)
NOT_WORSE = 0.95  # page width and bits per pixel at least this many times the published copy's: not worse
WIDER = 1.05  # median page width this many times the published copy's: better
BPP_BETTER = 1.2  # bits per pixel this many times the published copy's: better
JUNK_ROWS = 500  # strip junk (rows per 1000 px of width) differing by more than this: more or less junk
RATINGS = {"censored": 0, None: 1, "uncensored": 2}  # edition ratings (names.rating), higher is better


def judge(ctx: workflow.Context, chapter):
    """At junk's start: None when the series has no confirmed published file of the chapter (its key, and its resolved
    volume: chapter_names.same_chapter), when the chapter already names its file, or when the copy goes on to be judged
    after its junk is decided (after_junk); a copy of a higher edition rating takes its file now (None, or NeedsHuman
    for a busy file). Else the outcome: Dropped (a duplicate: a lower rating, or a page width or bits per pixel below
    NOT_WORSE of the file's) or NeedsHuman (2 files, or not the same chapter)."""
    if chapter["published_file_id"] is not None:
        return None
    found = _copy_of(ctx, chapter)
    if found is None or isinstance(found, workflow.NeedsHuman):
        return found
    published, ours, theirs = found
    rated = RATINGS[names.rating(PurePosixPath(chapter["download_path"]).stem)]
    their_rated = RATINGS[names.rating(published["source_name"])]
    if rated > their_rated:
        return _take(ctx, chapter, published)
    if rated < their_rated or _worse(ours, theirs):
        return _duplicate(ctx, chapter, published)
    return None


def after_junk(ctx: workflow.Context, chapter, edit):
    """Once junk has decided the chapter (edit: its chapter_edit), the rest of the better-copy rule at the same rating
    (SPEC "Library copies"): None when it is no copy (or named its file already), or a better copy, which then takes
    its file (or NeedsHuman for a busy file); else the duplicate's Dropped. Better: neither page width nor bits per
    pixel below NOT_WORSE of the file's, no more junk, and at least 1 of: width WIDER, bits per pixel BPP_BETTER, less
    junk. Bits per pixel and junk count only where the file's are known (a bulk-converted file has no original bits
    per pixel)."""
    if chapter["published_file_id"] is not None:
        return None
    found = _copy_of(ctx, chapter)
    if found is None or isinstance(found, workflow.NeedsHuman):
        return found
    published, ours, theirs = found
    if _worse(ours, theirs):
        return _duplicate(ctx, chapter, published)
    layout = _layout(ctx, chapter["series_id"])
    junk = measures.junk(edit, layout, ours.median_page_width)
    their_junk = published_junk(ctx, published, layout, theirs.median_page_width)
    more = less = False
    if their_junk is not None:
        margin = 0 if layout == "pages" else JUNK_ROWS
        more, less = junk > their_junk + margin, junk < their_junk - margin
    better = (
        ours.median_page_width >= WIDER * theirs.median_page_width
        or _bpp_compared(ours, theirs, lambda a, b: a >= BPP_BETTER * b)
        or less
    )
    if more or not better:
        return _duplicate(ctx, chapter, published)
    return _take(ctx, chapter, published)


def _copy_of(ctx: workflow.Context, chapter):
    """(the published file, our measures, its original measures) when the series has 1 confirmed published file of
    the chapter, still in the library and the same chapter; None when it has none; NeedsHuman otherwise."""
    key = validation.chapter_key(chapter["identity"])
    rows = [
        r
        for r in ctx.conn.execute(
            "SELECT * FROM published_file WHERE series_id = %s AND state = 'confirmed'", (chapter["series_id"],)
        )
        # A published file gone from the library is no copy: publishing replaces its record (SPEC "Publishing"); a
        # file whose queued rewrite renamed it is in the library under its queued name (SPEC "Retroactive updates").
        if validation.chapter_key(r["identity"]) == key
        and any(errors.exists(config.to_local(ctx.cfg, p)) for p in (r["library_path"], r["pending_path"]) if p)
    ]
    if rows:
        ours = functools.cache(lambda: chapter_names.download_volume(ctx.cfg, chapter["download_path"]))
        try:
            rows = [r for r in rows if chapter_names.same_chapter(ours, r["comicinfo_volume"], r["volume_source"])]
        except volumes.VolumeError as e:
            return workflow.NeedsHuman(str(e))
    if not rows:
        return None
    if len(rows) > 1:
        return workflow.NeedsHuman(
            f"the series has {len(rows)} published files of this chapter: {', '.join(r['library_path'] for r in rows)}"
        )
    published = rows[0]
    pages = list(page_store.load(ctx.conn, chapter["id"]).values())
    if not pages:
        msg = f"chapter {chapter['id']} has no page rows: validate stores them"
        raise ValueError(msg)
    ours = _measured(f"chapter {chapter['id']}", measures.summary(pages), measures.bits_per_pixel(pages))
    theirs = _original(ctx, published)
    strip = _layout(ctx, chapter["series_id"]) == "strip"
    measure = "strip_length" if strip else "pages_in"
    mine, its = (ours.strip_length, theirs.strip_length) if strip else (ours.pages_in, theirs.pages_in)
    if abs(mine - its) > SAME_CHAPTER * its:
        return workflow.NeedsHuman(
            f"not the same chapter as the published {published['library_path']}: {measure} {mine} against {its}"
        )
    return published, ours, theirs


def _worse(ours, theirs):
    """A page width, or bits per pixel where the file's is known, below NOT_WORSE of the published file's."""
    return ours.median_page_width < NOT_WORSE * theirs.median_page_width or _bpp_compared(
        ours, theirs, lambda a, b: a < NOT_WORSE * b
    )


def _bpp_compared(ours, theirs, test):
    """test(our bits per pixel, theirs), False where the published file's is not known; ours is always measured."""
    if ours.bpp is None:
        msg = "our copy's bits per pixel is always measured"
        raise ValueError(msg)
    return theirs.bpp is not None and test(ours.bpp, theirs.bpp)


@dataclass(frozen=True)
class Measures:
    """A copy's measurements the check compares."""

    pages_in: int
    median_page_width: int
    strip_length: int
    bpp: float | None  # bits per pixel; None when not known (a bulk file's pages are re-encoded)


def _measured(what, summary, bpp):
    """Measures from a measures.summary of a copy's pages (or a stored published row, which holds the same 3
    measures) and its bits per pixel; ValueError for a summary of no pages."""
    pages_in, width, length = summary["pages_in"], summary["median_page_width"], summary["strip_length"]
    if pages_in is None or width is None or length is None:
        msg = f"{what}: no page measurements"
        raise ValueError(msg)
    return Measures(pages_in, width, length, bpp)


def _layout(ctx: workflow.Context, series_id):
    row = ctx.conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is None:
        msg = f"series {series_id} has no layout (series_conversion): its copies cannot be compared"
        raise ValueError(msg)
    return row["layout"]


def published_junk(ctx: workflow.Context, published, layout, width):
    """The junk removed from the published file's download (measures.junk): stored when the pipeline published it,
    else from the bulk converter's record of its edits; None when unknown (a pipeline file published before it was
    stored, or a record that no longer lists the file)."""
    if published["origin"] != "bulk":
        return published["junk"]
    series = db.one_row(ctx.conn, "SELECT folder FROM series WHERE id = %s", (published["series_id"],))
    try:
        record = record_reader.load(config.to_local(ctx.cfg, series["folder"]))
    except FileNotFoundError as e:
        if errors.dropped_share(e):
            raise
        return None
    except record_reader.RecordError:
        return None
    name = PurePosixPath(published["library_path"]).name
    c = next((c for c in record["chapters"] if c["output"] == name), None)
    if c is None or not isinstance(c.get("edits"), dict):  # an older record keeps no edits: not known
        return None
    edits = c["edits"]
    removed = (edits.get("reshape") or {}).get("removed") or []
    edit = {"drop_pages": edits.get("drops") or [], "strip_removed": removed}
    return measures.junk(edit, layout, width)


def _duplicate(ctx: workflow.Context, chapter, published):
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))

    def record(conn):
        handled.record(conn, chapter["series_id"], chapter["identity"], "duplicate", chapter["content_digest"])

    return workflow.Dropped(f"a duplicate of the published {published['library_path']}", record)


def _original(ctx: workflow.Context, published):
    """The published copy's original measurements: as stored when the pipeline published it, else measured from the
    library file (the bulk converter's, or published before they were stored). Each page is read whole and not
    decoded: an AVIF page gives no size from its first bytes. bpp: the original's bits per pixel, None when not stored
    (a bulk file's pages are re-encoded)."""
    if published["pages_in"] is not None:  # publish stores the 3 measures together
        return _measured(published["library_path"], published, published["bpp"])
    with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
        names = archive.archive_pages(z.namelist())
        sizes = [images.dimensions(z.read(n)) for n in names]
    known = [s for s in sizes if s is not None]
    if not sizes or len(known) < len(sizes):
        msg = f"{published['library_path']}: page sizes cannot be read"
        raise ValueError(msg)
    pages = [{"width": w, "height": h, "phash": None, "blank": True} for w, h in known]
    return _measured(published["library_path"], measures.summary(pages), None)


def _take(ctx: workflow.Context, chapter, published):
    """The better copy names its file (its 1 open chapter) and takes the file's volume cover: the series' stored image
    of that volume, else the old file's own cover page. Returns None (the copy goes on), or NeedsHuman for a busy
    file."""
    volume = rewrite_covers.cover_volume(published["cover_state"])  # an old image too: the copy takes the new one
    if published["cover_state"] is None:  # not read yet (export reads it): the file's own cover page says
        with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
            if any(rewrite_covers.is_cover(n) for n in archive.archive_pages(z.namelist())):
                volume = published["comicinfo_volume"] or ""
    cover = None if volume is None else _cover(ctx, chapter["series_id"], published, volume)
    with db.transaction(ctx.conn):
        workflow.holds_claim(ctx, chapter)
        free = ctx.conn.execute(
            sql.SQL("SELECT 1 FROM published_file p WHERE p.id = %s AND NOT {}").format(
                rewrite.BUSY.format(p=sql.Identifier("p"))
            ),
            (published["id"],),
        ).fetchone()
        if free is None:
            return workflow.NeedsHuman(
                f"a better copy of the published {published['library_path']}, which is busy with a change (status "
                "lists it): retry once its rewrite has finished"
            )
        ctx.conn.execute("UPDATE chapter SET published_file_id = %s WHERE id = %s", (published["id"], chapter["id"]))
        ctx.conn.execute("DELETE FROM volume_cover WHERE chapter_id = %s", (chapter["id"],))
        if cover is not None:
            ctx.conn.execute(
                "INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, %s, %s, %s)",
                (chapter["id"], *cover, volume),
            )
    return None


def _cover(ctx: workflow.Context, series_id, published, volume):
    """(path below paths.covers, sha256) of the cover the copy takes in place of its file's: the series' stored image
    of that volume, else the file's own 000_cover page, stored content-addressed beside the fetched ones."""
    v = names.to_volume(volume)
    row = None
    if v is not None and v == int(v):  # stored images are of whole volumes
        row = ctx.conn.execute(
            "SELECT path, sha256 FROM series_volume_cover WHERE series_id = %s AND volume = %s", (series_id, int(v))
        ).fetchone()
    if row is not None:
        return row["path"], row["sha256"]
    with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
        page = next((n for n in archive.archive_pages(z.namelist()) if rewrite_covers.is_cover(n)), None)
        if page is None:  # the record said it holds one; the file does not: the copy goes without
            return None
        data = z.read(page)
    return covers.store_volume_image(ctx.cfg, data)
