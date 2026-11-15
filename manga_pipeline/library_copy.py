"""Library copies (SPEC "Library copies"): a chapter whose season, side label and number the series already has
published is a duplicate (dropped), or held for a person (a better copy, a doubtful one). Judged at junk, once the
series' baseline has recorded what the library holds. Reads the share and the database only.
"""

import functools
import zipfile

from manga_pipeline import (
    archive,
    chapter_names,
    config,
    handled,
    images,
    measures,
    page_store,
    retention,
    validation,
    volumes,
    workflow,
)

SAME_CHAPTER = 0.15  # page count or strip length within this share: the same chapter (old duplicate_page_tolerance)
WIDER = 1.05  # median page width this many times the published copy's: better
SAME_WIDTH = 0.95  # at least this, from a higher-ranked source: better


def judge(ctx, chapter):
    """None when the series has no confirmed published file of the chapter (its key, and its resolved volume:
    chapter_names.same_chapter); else the outcome: Dropped (a duplicate, put aside the retention way and recorded) or
    NeedsHuman."""
    key = validation.chapter_key(chapter["identity"])
    rows = [
        r
        for r in ctx.conn.execute(
            "SELECT * FROM published_file WHERE series_id = %s AND state = 'confirmed'", (chapter["series_id"],)
        )
        # A published file gone from the library is no copy: publishing replaces its record (SPEC "Publishing").
        if validation.chapter_key(r["identity"]) == key and workflow.exists(config.to_local(ctx.cfg, r["library_path"]))
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
    ours = measures.summary(pages)
    theirs = _original(ctx, published)
    row = ctx.conn.execute(
        "SELECT layout FROM series_conversion WHERE series_id = %s", (chapter["series_id"],)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"series {chapter['series_id']} has no layout (series_conversion): its copies cannot be compared"
        )
    layout = row["layout"]
    measure = "strip_length" if layout == "strip" else "pages_in"
    if abs(ours[measure] - theirs[measure]) > SAME_CHAPTER * theirs[measure]:
        return workflow.NeedsHuman(
            f"not the same chapter as the published {published['library_path']}: {measure} {ours[measure]} against "
            f"{theirs[measure]}"
        )
    width, their_width = ours["median_page_width"], theirs["median_page_width"]
    ranks = _rank(ctx, chapter["source"]), _rank(ctx, published["source"], published["origin"])
    if width >= WIDER * their_width or (width >= SAME_WIDTH * their_width and ranks[0] < ranks[1]):
        return workflow.NeedsHuman(
            f"a better copy than the published {published['library_path']} (page width {width} against "
            f"{their_width}, source {chapter['source'] or 'hand-placed'} against "
            f"{published['source'] or ('hand-placed' if published['origin'] == 'pipeline' else published['origin'])}); "
            "replacing a published file comes with Phase 7e"
        )
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))

    def record(conn):
        handled.record(conn, chapter["series_id"], chapter["identity"], "duplicate", chapter["content_digest"])

    return workflow.Dropped(f"a duplicate of the published {published['library_path']}", record)


def _original(ctx, published):
    """The published copy's original measurements: as stored when the pipeline published it, else measured from the
    library file (the bulk converter's, or published before they were stored). Each page is read whole and not
    decoded: an AVIF page gives no size from its first bytes."""
    if published["pages_in"] is not None:
        return {k: published[k] for k in ("pages_in", "median_page_width", "strip_length")}
    with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
        names = archive.archive_pages(z.namelist())
        sizes = [images.dimensions(z.read(n)) for n in names]
    if not sizes or None in sizes:
        raise ValueError(f"{published['library_path']}: page sizes cannot be read")
    return measures.summary([{"width": w, "height": h, "phash": None, "blank": True} for w, h in sizes])


def _rank(ctx, source, origin="pipeline"):
    """A source's rank, lower is better: sources.ranking in order, then other sources (a source recorded by its id
    included), then the bulk converter."""
    ranking = ctx.cfg["sources"]["ranking"]
    if origin == "bulk":
        return len(ranking) + 1
    return ranking.index(source) if source in ranking else len(ranking)
