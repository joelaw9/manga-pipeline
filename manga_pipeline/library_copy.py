"""Library copies (SPEC "Library copies"): a chapter the series already has published dropped, swapped in or held.

A chapter whose season, side label and number the series already has published is a duplicate (dropped), a better
copy (it takes the file: SPEC "Retroactive updates"), or held for a person (a doubtful one, or a better copy of a busy
file). Judged at junk, once the series' baseline has recorded what the library holds: at junk's start only what no
junk can change (judge), the rest once its junk is decided, on the lengths after junk (after_junk).
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
    """Judge the chapter as a library copy at junk's start, before its scan; return None or the outcome.

    Only what no junk can change is decided here: NeedsHuman for 2 or more confirmed published files of the chapter
    (its key, and its resolved volume: chapter_names.same_chapter) or a volume that cannot be read; Dropped for a
    download holding the published file's own pages (their digest). None otherwise: no copy, a chapter that already
    names its file, or a copy after_junk judges once its junk is decided.
    """
    if chapter["published_file_id"] is not None:
        return None
    published = _file_of(ctx, chapter)
    if published is None or isinstance(published, workflow.NeedsHuman):
        return published
    # only a file measured from the library gives its pages' digest (a stored original's file was re-encoded)
    path = config.to_local(ctx.cfg, published["library_path"])
    if published["pages_in"] is None and archive.pages_digest(path) == chapter["content_digest"]:
        return _duplicate(ctx, chapter, published, "the same pages")
    return None


def after_junk(
    ctx: workflow.Context, chapter: db.Row, edit: measures.JunkEdit
) -> workflow.Dropped | workflow.NeedsHuman | None:
    """Apply the better-copy rule once junk has decided the chapter (SPEC "Library copies").

    edit: its chapter_edit. None when it is no copy (or names its file), or a better copy, which then takes its file;
    else the outcome: a duplicate's Dropped, or NeedsHuman (2 files, a volume that cannot be read, not the same
    chapter, or a better copy of a busy file).
    """
    if chapter["published_file_id"] is not None:
        return None
    found = _copy_of(ctx, chapter, edit)
    if found is None or isinstance(found, workflow.NeedsHuman):
        return found
    published, ours, theirs, layout = found
    return _outcome(ctx, chapter, published, decide(ours, theirs, layout))


def earlier_copy(conn: db.Connection, chapter: db.Row) -> bool:
    """Return whether another download of the chapter's series that is not done has its chapter key and is ahead of it.

    Ahead: past junk (convert to confirm); or at junk, naming no published file, and before it by (discovery time, id).
    A chapter at junk that names its file is never ahead and never waits. A download at discover or validate is never
    ahead, and a rewrite chapter is no download. Reads only.
    """
    if chapter["published_file_id"] is not None:
        return False
    key = validation.chapter_key(chapter["identity"])
    ours = (chapter["discovered_at"], chapter["id"])
    rows = conn.execute(
        """SELECT id, identity, stage, published_file_id, discovered_at FROM chapter
        WHERE series_id = %s AND id <> %s AND kind = 'download' AND status <> 'done'
            AND stage IN ('junk', 'convert', 'comicinfo', 'publish', 'confirm')""",
        (chapter["series_id"], chapter["id"]),
    )
    return any(
        validation.chapter_key(r["identity"]) == key
        and (r["stage"] != "junk" or (r["published_file_id"] is None and (r["discovered_at"], r["id"]) < ours))
        for r in rows
    )


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
class PublishedCopy(Copy[float | None]):
    """A published file as the better-copy rule compares a copy with it (published_copy).

    length: what the same-chapter test compares our length with (a page count, layout pages, or a strip length, layout
    strip); junk_out: whether our length is compared less our junk.
    """

    length: float
    junk_out: bool


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


def decide(ours: Copy[float], theirs: PublishedCopy, layout: str) -> Better | Duplicate | NotSameChapter:
    """Apply the better-copy rule (SPEC "Library copies") to our copy, its junk decided, against the published file.

    In order: not the same chapter (our length, less our junk where theirs.junk_out, beyond SAME_CHAPTER of
    theirs.length); a higher edition rating is Better, a lower one a Duplicate; a page width, or bits per pixel where
    theirs is known, below NOT_WORSE of theirs is a Duplicate. Else Better when no more junk (strip: by more than
    JUNK_ROWS; only where theirs is known) and at least 1 of: width WIDER, bits per pixel BPP_BETTER, less junk; else
    a Duplicate. Pure; ValueError when our junk is not known.
    """
    if ours.junk is None:
        msg = "our copy's junk is not decided: the better-copy rule judges a copy after its junk"
        raise ValueError(msg)
    mine, its = ours.measures, theirs.measures
    strip = layout == "strip"
    length: float = mine.strip_length if strip else mine.pages_in
    if theirs.junk_out:
        length -= ours.junk
    if abs(length - theirs.length) > SAME_CHAPTER * theirs.length:
        measure = ("strip_length" if strip else "pages_in") + (" after junk" if theirs.junk_out else "")
        return NotSameChapter(f"{measure} {length:.0f} against {theirs.length:.0f}")
    if ours.rating != theirs.rating:
        return Better() if ours.rating > theirs.rating else Duplicate("a lower edition rating")
    if mine.median_page_width < NOT_WORSE * its.median_page_width:
        return Duplicate(f"median page width {mine.median_page_width} against {its.median_page_width}")
    if _bpp_compared(mine, its, lambda a, b: a < NOT_WORSE * b):
        return Duplicate(f"bits per pixel {mine.bpp:.2f} against {its.bpp:.2f}")
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


def _outcome(
    ctx: workflow.Context, chapter: db.Row, published: db.Row, verdict: Better | Duplicate | NotSameChapter
) -> workflow.Dropped | workflow.NeedsHuman | None:
    """Act on decide's verdict: a better copy takes its file, a duplicate is dropped, another chapter is held."""
    match verdict:
        case Better():
            return _take(ctx, chapter, published)
        case Duplicate(reason):
            return _duplicate(ctx, chapter, published, reason)
        case NotSameChapter(reason):
            return workflow.NeedsHuman(f"not the same chapter as the published {published['library_path']}: {reason}")


def _copy_of(
    ctx: workflow.Context, chapter: db.Row, edit: measures.JunkEdit
) -> tuple[db.Row, Copy[float], PublishedCopy, str] | workflow.NeedsHuman | None:
    """Return (the published file, our copy, the published copy, the series' layout) of the chapter's copy (_file_of).

    Our copy: its page rows' measures, its lengths leaving a 000_cover page out (rewrite_covers.content_pages; width
    and bits per pixel over every page), and the junk edit removes (measures.junk). None or NeedsHuman as _file_of
    gives them.
    """
    published = _file_of(ctx, chapter)
    if published is None or isinstance(published, workflow.NeedsHuman):
        return published
    pages = list(page_store.load(ctx.conn, chapter["id"]).values())
    if not pages:
        msg = f"chapter {chapter['id']} has no page rows: validate stores them"
        raise ValueError(msg)
    layout = _layout(ctx, chapter["series_id"])
    what, bpp = f"chapter {chapter['id']}", measures.bits_per_pixel(pages)
    lengths = measured(what, measures.summary(rewrite_covers.content_pages(pages)), bpp)
    ours = dataclasses.replace(  # as publish.row_values stores them: only the lengths leave a 000_cover page out
        measured(what, measures.summary(pages), bpp), pages_in=lengths.pages_in, strip_length=lengths.strip_length
    )
    junk = measures.junk(edit, layout, ours.median_page_width)
    ours_copy = Copy(ours, rating(PurePosixPath(chapter["download_path"]).stem), junk)
    return published, ours_copy, published_copy(ctx, published, layout), layout


def _file_of(ctx: workflow.Context, chapter: db.Row) -> db.Row | workflow.NeedsHuman | None:
    """Return the published file of the chapter's copy.

    Returned when the series has 1 confirmed published file of the chapter, still in the library; None when it has
    none; NeedsHuman for 2 or more, or a volume that cannot be read.
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
    return rows[0]


def published_copy(ctx: workflow.Context, published: db.Row, layout: str) -> PublishedCopy:
    """Return a published file as the better-copy rule compares a copy with it (SPEC "Library copies").

    Its original's measures (original), its edition rating, the junk its download lost (published_junk), and the
    length the same-chapter test compares (a 000_cover page never counted):
    - a stored original whose junk is known: less its junk, against ours less ours;
    - a stored original whose junk is not known: as stored, against ours with its junk in;
    - measured from the library file of a bulk or pipeline file (its junk removed already): as measured, against ours
      less ours;
    - measured from an adopted file (origin library; its junk not known): as measured, against ours with its junk in.
    """
    m = original(ctx, published)
    junk = published_junk(ctx, published, layout, m.median_page_width)
    length = m.strip_length if layout == "strip" else m.pages_in
    if published["pages_in"] is not None:  # a stored original
        if junk is not None:
            return PublishedCopy(m, rating(published["source_name"]), junk, length - junk, junk_out=True)
        return PublishedCopy(m, rating(published["source_name"]), junk, length, junk_out=False)
    return PublishedCopy(m, rating(published["source_name"]), junk, length, junk_out=published["origin"] != "library")


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

    As stored when the pipeline published it, else measured from the library file (the bulk converter's, an adopted
    one, or published before they were stored), its cover page left out. Each page is read whole and not decoded: an
    AVIF page gives no size from its first bytes. bpp: the original's bits per pixel; None when not stored, except for
    an adopted file (origin library), whose pages are its download's (a bulk file's pages are re-encoded).
    """
    if published["pages_in"] is not None:  # publish stores the 3 measures together
        return measured(published["library_path"], published, published["bpp"])
    sizes: list[tuple[int, int] | None] = []
    lengths: list[int] = []
    with zipfile.ZipFile(config.to_local(ctx.cfg, published["library_path"])) as z:
        for n in archive.archive_pages(z.namelist()):  # 1 page in memory at a time
            data = z.read(n)
            if not rewrite_covers.is_cover(n):
                sizes.append(images.dimensions(data))
                lengths.append(len(data))
    known = [s for s in sizes if s is not None]
    if not sizes or len(known) < len(sizes):
        msg = f"{published['library_path']}: page sizes cannot be read"
        raise ValueError(msg)
    pages: list[measures.SummaryPage] = [{"width": w, "height": h, "phash": None} for w, h in known]
    bpp = None
    if published["origin"] == "library":
        bpp = measures.bits_per_pixel(
            [{"width": w, "height": h, "bytes": n} for (w, h), n in zip(known, lengths, strict=True)]
        )
    return measured(published["library_path"], measures.summary(pages), bpp)


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
