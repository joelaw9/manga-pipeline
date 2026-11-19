"""Library copies (SPEC "Library copies"): a chapter the series already has published dropped, swapped in or held.

A chapter whose season, side label and number the series already has published is a duplicate (dropped), a better
copy (it takes the file: SPEC "Retroactive updates"), or held for a person (a doubtful one, or a better copy of a busy
file). Judged at junk, once the series' baseline has recorded what the library holds.
"""

import dataclasses
import functools
import zipfile
from collections.abc import Callable
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
# edition ratings (names.rating), higher is better
RATINGS: dict[str | None, int] = {"censored": 0, None: 1, "uncensored": 2}


def judge(ctx: workflow.Context, chapter: db.Row) -> workflow.Dropped | workflow.NeedsHuman | None:
    """Judge the chapter as a library copy at junk's start; return None or the outcome.

    None when the series has no confirmed published file of the chapter (its key, and its resolved volume:
    chapter_names.same_chapter), when the chapter already names its file, or when the copy goes on to be judged after
    its junk is decided (after_junk); a copy of a higher edition rating takes its file now (None, or NeedsHuman for a
    busy file). Else the outcome: Dropped (a duplicate: a lower rating, or a page width or bits per pixel below
    NOT_WORSE of the file's) or NeedsHuman (2 files, or not the same chapter).
    """
    if chapter["published_file_id"] is not None:
        return None
    found = _copy_of(ctx, chapter)
    if found is None or isinstance(found, workflow.NeedsHuman):
        return found
    published, ours, theirs, layout = found
    return _outcome(ctx, chapter, published, decide(ours, theirs, layout))


def after_junk(
    ctx: workflow.Context, chapter: db.Row, edit: measures.JunkEdit
) -> workflow.Dropped | workflow.NeedsHuman | None:
    """Apply the rest of the better-copy rule at the same rating, once junk has decided the chapter.

    edit: its chapter_edit (SPEC "Library copies"). None when it is no copy (or named its file already), or a better
    copy, which then takes its file (or NeedsHuman for a busy file); else the duplicate's Dropped (decide).
    """
    # read again: judge may have made it name its file earlier in this junk call, after the claimed row was read
    named = db.one_row(ctx.conn, "SELECT published_file_id FROM chapter WHERE id = %s", (chapter["id"],))
    if named["published_file_id"] is not None:
        return None
    found = _copy_of(ctx, chapter)
    if found is None or isinstance(found, workflow.NeedsHuman):
        return found
    published, ours, theirs, layout = found
    ours = dataclasses.replace(ours, junk=measures.junk(edit, layout, ours.measures.median_page_width))
    theirs = dataclasses.replace(theirs, junk=published_junk(ctx, published, layout, theirs.measures.median_page_width))
    return _outcome(ctx, chapter, published, decide(ours, theirs, layout))


def rating(name: str) -> int:
    """Return the edition rating (RATINGS) of a download's or a published file's source name."""
    return RATINGS[names.rating(name)]


@dataclass(frozen=True)
class Measures[B: float | None]:
    """A copy's measurements the check compares: our copy's bits per pixel is always measured (B float)."""

    pages_in: int
    median_page_width: int
    strip_length: int
    bpp: B  # bits per pixel; None when not known (a bulk file's pages are re-encoded)


@dataclass(frozen=True)
class Copy[B: float | None]:
    """A copy as the better-copy rule compares it: its measures, edition rating (rating) and junk removed.

    junk: measures.junk of what its download lost; None when not known (our copy's: not decided yet; a published
    file's: published_junk found none).
    """

    measures: Measures[B]
    rating: int
    junk: float | None


@dataclass(frozen=True)
class Better:
    """Our copy is better: it replaces the published file."""


@dataclass(frozen=True)
class Duplicate:
    """Our copy is not better: a duplicate, dropped; reason says why."""

    reason: str


@dataclass(frozen=True)
class NotSameChapter:
    """Our copy's length is not the published copy's: not the same chapter, a person decides; reason gives both."""

    reason: str


def decide(ours: Copy[float], theirs: Copy[float | None], layout: str) -> Better | Duplicate | NotSameChapter | None:
    """Apply the better-copy rule (SPEC "Library copies") to our copy against the published file's original.

    First what is judged at once (_at_once); then, our junk not known yet, None (decided once it is). Else Better when
    no more junk (strip: by more than JUNK_ROWS; only where theirs is known) and at least 1 of: width WIDER, bits per
    pixel BPP_BETTER, less junk; else a Duplicate. Pure.
    """
    found = _at_once(ours, theirs, layout)
    if found is not None or ours.junk is None:
        return found
    mine, its = ours.measures, theirs.measures
    more = less = False
    if theirs.junk is not None:
        margin = 0 if layout == "pages" else JUNK_ROWS
        more, less = ours.junk > theirs.junk + margin, ours.junk < theirs.junk - margin
    if more:
        return Duplicate(f"more junk removed: {ours.junk:g} against {theirs.junk:g}")
    better = (
        mine.median_page_width >= WIDER * its.median_page_width
        or _bpp_compared(mine, its, lambda a, b: a >= BPP_BETTER * b)
        or less
    )
    return Better() if better else Duplicate("no better in width, bits per pixel or junk")


def _at_once(ours: Copy[float], theirs: Copy[float | None], layout: str) -> Better | Duplicate | NotSameChapter | None:
    """Return what the rule decides before junk, or None.

    In order: not the same chapter (page count, layout pages, or strip length, layout strip, beyond SAME_CHAPTER of
    theirs); a higher edition rating is Better, a lower one a Duplicate; a page width, or bits per pixel where theirs
    is known, below NOT_WORSE of theirs is a Duplicate.
    """
    mine, its = ours.measures, theirs.measures
    strip = layout == "strip"
    measure = "strip_length" if strip else "pages_in"
    length, their_length = (mine.strip_length, its.strip_length) if strip else (mine.pages_in, its.pages_in)
    if abs(length - their_length) > SAME_CHAPTER * their_length:
        return NotSameChapter(f"{measure} {length} against {their_length}")
    if ours.rating != theirs.rating:
        return Better() if ours.rating > theirs.rating else Duplicate("a lower edition rating")
    if mine.median_page_width < NOT_WORSE * its.median_page_width:
        return Duplicate(f"median page width {mine.median_page_width} against {its.median_page_width}")
    if _bpp_compared(mine, its, lambda a, b: a < NOT_WORSE * b):
        return Duplicate(f"bits per pixel {mine.bpp:.2f} against {its.bpp:.2f}")
    return None


def _outcome(
    ctx: workflow.Context, chapter: db.Row, published: db.Row, verdict: Better | Duplicate | NotSameChapter | None
) -> workflow.Dropped | workflow.NeedsHuman | None:
    """Act on decide's verdict: a better copy takes its file, a duplicate is dropped, another chapter is held."""
    match verdict:
        case Better():
            return _take(ctx, chapter, published)
        case Duplicate(reason):
            return _duplicate(ctx, chapter, published, reason)
        case NotSameChapter(reason):
            return workflow.NeedsHuman(f"not the same chapter as the published {published['library_path']}: {reason}")
        case None:
            return None


def _copy_of(
    ctx: workflow.Context, chapter: db.Row
) -> tuple[db.Row, Copy[float], Copy[float | None], str] | workflow.NeedsHuman | None:
    """Return (the published file, our copy, its original, the series' layout) of the chapter's published copy.

    Returned when the series has 1 confirmed published file of the chapter, still in the library; None when it has
    none; NeedsHuman for 2 or more, or a volume that cannot be read. Neither copy's junk is filled in.
    """
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
    ours = measured(f"chapter {chapter['id']}", measures.summary(pages), measures.bits_per_pixel(pages))
    ours_rated = rating(PurePosixPath(chapter["download_path"]).stem)
    theirs = Copy(original(ctx, published), rating(published["source_name"]), None)
    return published, Copy(ours, ours_rated, None), theirs, _layout(ctx, chapter["series_id"])


def _bpp_compared(
    ours: "Measures[float]", theirs: "Measures[float | None]", test: Callable[[float, float], bool]
) -> bool:
    """test(our bits per pixel, theirs), False where the published file's is not known; ours is always measured."""
    return theirs.bpp is not None and test(ours.bpp, theirs.bpp)


def measured[B: float | None](what: str, summary: measures.Summary | db.Row, bpp: B) -> Measures[B]:
    """Build the measures from a measures.summary of a copy's pages and its bits per pixel.

    The summary may instead be a stored published row, which holds the same 3 measures. ValueError for a summary of no
    pages.
    """
    pages_in, width, length = summary["pages_in"], summary["median_page_width"], summary["strip_length"]
    if pages_in is None or width is None or length is None:
        msg = f"{what}: no page measurements"
        raise ValueError(msg)
    return Measures(pages_in, width, length, bpp)


def _layout(ctx: workflow.Context, series_id: int) -> str:
    row = ctx.conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is None:
        msg = f"series {series_id} has no layout (series_conversion): its copies cannot be compared"
        raise ValueError(msg)
    return row["layout"]


def published_junk(ctx: workflow.Context, published: db.Row, layout: str, width: int) -> float | None:
    """Return the junk removed from the published file's download (measures.junk).

    Stored when the pipeline published it, else from the bulk converter's record of its edits; None when unknown (a
    pipeline file published before it was stored, or a record that no longer lists the file).
    """
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
    if c is None or not isinstance(edits := c.get("edits"), dict):  # an older record keeps no edits: not known
        return None
    removed = (edits.get("reshape") or {}).get("removed") or []
    edit: measures.JunkEdit = {"drop_pages": edits.get("drops") or [], "strip_removed": removed}
    return measures.junk(edit, layout, width)


def _duplicate(ctx: workflow.Context, chapter: db.Row, published: db.Row, reason: str) -> workflow.Dropped:
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))

    def record(conn: db.Connection) -> None:
        handled.record(conn, chapter["series_id"], chapter["identity"], "duplicate", chapter["content_digest"])

    return workflow.Dropped(f"a duplicate of the published {published['library_path']}: {reason}", record)


def original(ctx: workflow.Context, published: db.Row) -> Measures[float | None]:
    """Return the published copy's original measurements.

    As stored when the pipeline published it, else measured from the library file (the bulk converter's, or published
    before they were stored), its cover page left out. Each page is read whole and not decoded: an AVIF page gives no
    size from its first bytes. bpp: the original's bits per pixel, None when not stored (a bulk file's pages are
    re-encoded).
    """
    if published["pages_in"] is not None:  # publish stores the 3 measures together
        return measured(published["library_path"], published, published["bpp"])
    with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
        names = [n for n in archive.archive_pages(z.namelist()) if not rewrite_covers.is_cover(n)]
        sizes = [images.dimensions(z.read(n)) for n in names]
    known = [s for s in sizes if s is not None]
    if not sizes or len(known) < len(sizes):
        msg = f"{published['library_path']}: page sizes cannot be read"
        raise ValueError(msg)
    pages: list[measures.SummaryPage] = [{"width": w, "height": h, "phash": None} for w, h in known]
    return measured(published["library_path"], measures.summary(pages), None)


def _take(ctx: workflow.Context, chapter: db.Row, published: db.Row) -> workflow.NeedsHuman | None:
    """Make the better copy name its file (its 1 open chapter) and take the file's volume cover.

    The cover, when the file holds its own volume's: the series' stored image of that volume, else the old file's own
    cover page; another volume's cover gives none. Returns None (the copy goes on), or NeedsHuman for a busy file.
    """
    volume = rewrite_covers.cover_volume(published["cover_state"])  # an old image too: the copy takes the new one
    if volume is not None and names.to_volume(volume) != names.to_volume(published["comicinfo_volume"]):
        volume = None
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


def _cover(ctx: workflow.Context, series_id: int, published: db.Row, volume: str) -> tuple[str, str] | None:
    """Return (path below paths.covers, sha256) of the cover the copy takes in place of its file's.

    The series' stored image of that volume, else the file's own 000_cover page, stored content-addressed beside the
    fetched ones.
    """
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
