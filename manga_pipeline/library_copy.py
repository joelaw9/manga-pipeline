"""Library copies (SPEC "Library copies"): a chapter whose season, side label and number the series already has
published is a duplicate (dropped), a better copy (it takes the file: SPEC "Retroactive updates"), or held for a
person (a doubtful one, or a better copy of a busy file). Judged at junk, once the series' baseline has recorded what
the library holds.
"""

import functools
import zipfile
from pathlib import PurePosixPath

from manga_pipeline import (
    archive,
    chapter_names,
    config,
    db,
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
from manga_pipeline.metadata import covers

SAME_CHAPTER = 0.15  # page count or strip length within this share: the same chapter (old duplicate_page_tolerance)
NOT_WORSE = 0.95  # page width and bits per pixel at least this many times the published copy's: not worse
WIDER = 1.05  # median page width this many times the published copy's: better
BPP_BETTER = 1.2  # bits per pixel this many times the published copy's: better
JUNK_ROWS = 500  # strip junk (rows per 1000 px of width) differing by more than this: more or less junk
RATINGS = {"censored": 0, None: 1, "uncensored": 2}  # edition ratings (names.rating), higher is better


def judge(ctx, chapter):
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


def after_junk(ctx, chapter, edit):
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
    junk = measures.junk(edit, layout, ours["median_page_width"])
    their_junk = _junk(ctx, published, layout, theirs["median_page_width"])
    more = less = False
    if their_junk is not None:
        margin = 0 if layout == "pages" else JUNK_ROWS
        more, less = junk > their_junk + margin, junk < their_junk - margin
    better = (
        ours["median_page_width"] >= WIDER * theirs["median_page_width"]
        or (theirs["bpp"] is not None and ours["bpp"] >= BPP_BETTER * theirs["bpp"])
        or less
    )
    if more or not better:
        return _duplicate(ctx, chapter, published)
    return _take(ctx, chapter, published)


def _copy_of(ctx, chapter):
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
        and any(workflow.exists(config.to_local(ctx.cfg, p)) for p in (r["library_path"], r["pending_path"]) if p)
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
        raise ValueError(f"chapter {chapter['id']} has no page rows: validate stores them")
    ours = measures.summary(pages) | {"bpp": measures.bits_per_pixel(pages)}
    theirs = _original(ctx, published)
    measure = "strip_length" if _layout(ctx, chapter["series_id"]) == "strip" else "pages_in"
    if abs(ours[measure] - theirs[measure]) > SAME_CHAPTER * theirs[measure]:
        return workflow.NeedsHuman(
            f"not the same chapter as the published {published['library_path']}: {measure} {ours[measure]} against "
            f"{theirs[measure]}"
        )
    return published, ours, theirs


def _worse(ours, theirs):
    """A page width, or bits per pixel where the file's is known, below NOT_WORSE of the published file's."""
    return ours["median_page_width"] < NOT_WORSE * theirs["median_page_width"] or (
        theirs["bpp"] is not None and ours["bpp"] < NOT_WORSE * theirs["bpp"]
    )


def _layout(ctx, series_id):
    row = ctx.conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is None:
        raise ValueError(f"series {series_id} has no layout (series_conversion): its copies cannot be compared")
    return row["layout"]


def _junk(ctx, published, layout, width):
    """The junk removed from the published file's download (measures.junk): stored when the pipeline published it,
    else from the bulk converter's record of its edits; None when unknown (a pipeline file published before it was
    stored, or a record that no longer lists the file)."""
    if published["origin"] != "bulk":
        return published["junk"]
    series = ctx.conn.execute("SELECT folder FROM series WHERE id = %s", (published["series_id"],)).fetchone()
    try:
        record = record_reader.load(config.to_local(ctx.cfg, series["folder"]))
    except FileNotFoundError as e:
        if workflow.dropped_share(e):
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


def _duplicate(ctx, chapter, published):
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))

    def record(conn):
        handled.record(conn, chapter["series_id"], chapter["identity"], "duplicate", chapter["content_digest"])

    return workflow.Dropped(f"a duplicate of the published {published['library_path']}", record)


def _original(ctx, published):
    """The published copy's original measurements: as stored when the pipeline published it, else measured from the
    library file (the bulk converter's, or published before they were stored). Each page is read whole and not
    decoded: an AVIF page gives no size from its first bytes. bpp: the original's bits per pixel, None when not stored
    (a bulk file's pages are re-encoded)."""
    if published["pages_in"] is not None:
        return {k: published[k] for k in ("pages_in", "median_page_width", "strip_length", "bpp")}
    with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
        names = archive.archive_pages(z.namelist())
        sizes = [images.dimensions(z.read(n)) for n in names]
    if not sizes or None in sizes:
        raise ValueError(f"{published['library_path']}: page sizes cannot be read")
    return measures.summary([{"width": w, "height": h, "phash": None, "blank": True} for w, h in sizes]) | {"bpp": None}


def _take(ctx, chapter, published):
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
            f"SELECT 1 FROM published_file p WHERE p.id = %s AND NOT {rewrite.BUSY.format(p='p')}", (published["id"],)
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


def _cover(ctx, series_id, published, volume):
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
