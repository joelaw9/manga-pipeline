"""Source review (SPEC "Source review"): the `source` commands but `sample`, and what they share.

Each command returns the lines it prints (the entry point prints them). `survey`, `measure` and `search` change
nothing; `switch`, `replace` and `retire` change Suwayomi's library and download queue and the series' manga links,
nothing else. Every path is a stored path made local (config.to_local).
"""

import statistics
from collections.abc import Callable, Sequence
from typing import NamedTuple

from manga_pipeline import config, db, library_copy, names, redownload, rewrite, validation, workflow
from manga_pipeline.clients import suwayomi
from manga_pipeline.db import sql
from manga_pipeline.replies import suwayomi as suwayomi_replies

RECENT = 10  # a series' combined measures are over its latest this many confirmed published files
MIN_RUN = 4  # a sample's files come from a run (season, side label) of at least this many published files
SAMPLE_CHAPTERS = 3  # files a sample takes by default
# triggers (SPEC "Source review"): junk above these, a median page width at most WIDTH_MAX, bits per pixel below BPP_MIN
STRIP_JUNK, PAGES_JUNK = 500, 1
WIDTH_MAX = 800
BPP_MIN = 0.8


class FileMeasures(NamedTuple):
    """A published file's measures as Library copies compares them: its original's, the junk its download lost."""

    row: db.Row
    measures: library_copy.Measures[float | None]
    junk: float | None
    rating: int


class Combined(NamedTuple):
    """A series' combined measures: each the mean over the files that have it, None when none has it."""

    width: float | None
    bpp: float | None
    junk: float | None


# ---------------------------------------------------------------- the series and its files


def reviewed(ctx: workflow.Context, series_id: int) -> db.Row:
    """Return the series row of a series that can be reviewed: Kavita holds it, in scope, with confirmed files.

    Refused otherwise (exit 2).
    """
    series = ctx.conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()
    if series is None:
        msg = f"no series {series_id}"
        raise workflow.Refused(msg)
    if series["kavita_series_id"] is None or not published(ctx.conn, series_id):
        msg = f"series {series_id} ({series['name']}) is not in Kavita yet: run once it is published"
        raise workflow.Refused(msg)
    if series["kavita_library_id"] != ctx.cfg["kavita"]["library_id"] or not workflow.row_in_scope(ctx.cfg, series):
        msg = f"series {series_id} ({series['name']}) is not in scope (kavita.library_id, series.only)"
        raise workflow.Refused(msg)
    return series


def published(conn: db.Connection, series_id: int) -> list[db.Row]:
    """Return the series' confirmed published files in chapter order (season, then number)."""
    rows = conn.execute(
        "SELECT * FROM published_file WHERE series_id = %s AND state = 'confirmed' ORDER BY id", (series_id,)
    ).fetchall()
    return sorted(rows, key=_order)


def _order(row: db.Row) -> tuple[float, float]:
    _, season, _, _, _ = names.read_identity(row["identity"])
    return float(season or 0), number(row["identity"]) or 0.0


def number(identity: str) -> float | None:
    """Return the chapter number of an identity (names.identity's JSON), None for a chapter with none."""
    found = names.read_identity(identity)[3]
    return None if found is None else float(found)


def reference(rows: Sequence[db.Row]) -> list[tuple[str, float | None]]:
    """Return the numbering reference of a series (SPEC "Source review"): its published files' (identity, number)."""
    return [(r["identity"], number(r["identity"])) for r in rows]


def key_text(identity: str) -> str:
    """Return a chapter key (season, side label, number) as a person reads it."""
    season, side, found = validation.chapter_key(identity)
    return " ".join(p for p in (f"season {season}" if season else "", side or "", f"#{found or '-'}") if p)


def layout(conn: db.Connection, series_id: int) -> str | None:
    """Return the series' layout (series_conversion), None when it has none yet."""
    row = conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    return None if row is None else row["layout"]


def sample_files(rows: Sequence[db.Row], n: int) -> list[db.Row]:
    """Return the files a sample takes: the latest n of 1 run (same season and side label) of at least MIN_RUN files.

    The run is the latest season's main one, else a side label's. rows: published, in chapter order. Pure.
    """
    runs: dict[tuple[str | None, str | None], list[db.Row]] = {}
    for r in rows:
        if number(r["identity"]) is not None:
            season, side, _ = validation.chapter_key(r["identity"])
            runs.setdefault((season, side), []).append(r)
    full = [(k, run) for k, run in runs.items() if len(run) >= MIN_RUN]
    if not full:
        return []
    _, run = max(full, key=lambda kv: (not kv[0][1], float(kv[0][0] or 0)))
    return run[-n:]


def linked(conn: db.Connection, series_id: int, manga_id: int) -> None:
    """Refuse (exit 2) unless the manga is linked to the series."""
    row = conn.execute("SELECT series_id FROM suwayomi_manga WHERE manga_id = %s", (manga_id,)).fetchone()
    if row is None or row["series_id"] != series_id:
        msg = f"manga {manga_id} is not linked to series {series_id}"
        raise workflow.Refused(msg)


def manga(ctx: workflow.Context, manga_id: int) -> tuple[suwayomi_replies.Manga, suwayomi_replies.Source]:
    """Return Suwayomi's manga and its source; refused (exit 2) when it has none, or its source is not installed."""
    found = ctx.suwayomi.manga(manga_id)
    if found is None:
        msg = f"Suwayomi has no manga {manga_id}"
        raise workflow.Refused(msg)
    if found["source"] is None:
        msg = f"manga {manga_id} ({found['title']}): its source is not installed"
        raise workflow.Refused(msg)
    return found, found["source"]


def chapters(client: suwayomi.Suwayomi, found: suwayomi_replies.Manga) -> list[suwayomi_replies.Chapter]:
    """Return a manga's chapters: Suwayomi's stored list for a library manga, else fetched from its source.

    A library manga is never fetched: a fetch would add a completed series' new chapter before its metadata check
    can see it as new. Any other is stored outside Suwayomi's library by the fetch.
    """
    if found["inLibrary"]:
        return client.chapters(found["id"])
    return client.fetch_manga_and_chapters(found["id"])["chapters"]


def chapter_keys(rows: Sequence[suwayomi_replies.Chapter]) -> set[validation.ChapterKey]:
    """Return the chapter keys a manga's chapters give, each parsed as its download file's name."""
    return {validation.chapter_key(redownload.numbering(r)[0]) for r in rows}


# ---------------------------------------------------------------- measures


def measure_file(ctx: workflow.Context, row: db.Row, series_layout: str | None) -> FileMeasures:
    """Return a published file's measures (library_copy.original) and junk (None without a layout)."""
    m = library_copy.original(ctx, row)
    junk = None if series_layout is None else library_copy.published_junk(ctx, row, series_layout, m.median_page_width)
    return FileMeasures(row, m, junk, library_copy.rating(row["source_name"]))


def combine(files: Sequence[FileMeasures]) -> Combined:
    """Return the combined measures of files. Pure."""

    def mean(values: Sequence[float | None]) -> float | None:
        known = [v for v in values if v is not None]
        return statistics.fmean(known) if known else None

    return Combined(
        mean([f.measures.median_page_width for f in files]),
        mean([f.measures.bpp for f in files]),
        mean([f.junk for f in files]),
    )


def triggers(c: Combined, series_layout: str | None) -> list[str]:
    """Return the triggers combined measures cross (SPEC "Source review"). Pure."""
    out: list[str] = []
    limit = STRIP_JUNK if series_layout == "strip" else PAGES_JUNK
    if c.junk is not None and (c.junk > limit if series_layout == "strip" else c.junk >= limit):
        out.append("junk")
    if c.width is not None and c.width <= WIDTH_MAX:
        out.append("width")
    if c.bpp is not None and c.bpp < BPP_MIN:
        out.append("bits per pixel")
    return out


def _value(v: float | None, fmt: str) -> str:
    return "-" if v is None else format(v, fmt)


def combined_text(c: Combined) -> str:
    """Return combined measures as printed."""
    return f"width {_value(c.width, '.0f')}, bits per pixel {_value(c.bpp, '.2f')}, junk {_value(c.junk, '.4g')}"


def file_text(f: FileMeasures) -> str:
    """Return a published file's line: id, key, source, measures and edition rating."""
    m = f.measures
    return (
        f"file {f.row['id']} {key_text(f.row['identity'])} ({f.row['library_path']}): source {f.row['source'] or '-'}, "
        f"pages {m.pages_in}, width {m.median_page_width}, bits per pixel {_value(m.bpp, '.2f')}, "
        f"junk {_value(f.junk, '.4g')}, rating {f.rating}"
    )


# ---------------------------------------------------------------- survey, measure, search


def survey(ctx: workflow.Context) -> list[str]:
    """Return every series in scope with its combined measures and the triggers it crosses, those crossing most first.

    Reaches no service.
    """
    rows = ctx.conn.execute(
        sql.SQL("""SELECT s.* FROM series s WHERE s.kavita_series_id IS NOT NULL
            AND s.kavita_library_id = %(scope_library)s AND {scope} ORDER BY s.id""").format(scope=workflow.SCOPE),
        workflow.scope_params(ctx.cfg),
    ).fetchall()
    found: list[tuple[int, str]] = []
    for series in rows:
        files = published(ctx.conn, series["id"])[-RECENT:]
        if not files:
            continue
        series_layout = layout(ctx.conn, series["id"])
        c = combine([measure_file(ctx, f, series_layout) for f in files])
        crossed = triggers(c, series_layout)
        text = (
            f"series {series['id']} {series['name']} ({series_layout or 'no layout'}, {len(files)} files): "
            f"{combined_text(c)}; triggers: {', '.join(crossed) or 'none'}"
        )
        found.append((len(crossed), text))
    found.sort(key=lambda t: -t[0])  # stable: by id within the same count
    return [text for _, text in found]


def measure(ctx: workflow.Context, series_id: int) -> list[str]:
    """Return the series' published files with their measures, the combined measures, and its linked manga."""
    series = reviewed(ctx, series_id)
    series_layout = layout(ctx.conn, series_id)
    files = [measure_file(ctx, r, series_layout) for r in published(ctx.conn, series_id)]
    c = combine(files[-RECENT:])
    out = [f"series {series_id} {series['name']} ({series_layout or 'no layout'})", *map(file_text, files)]
    out.append(f"combined (latest {min(RECENT, len(files))}): {combined_text(c)}")
    out.append(f"triggers: {', '.join(triggers(c, series_layout)) or 'none'}")
    for (manga_id,) in ctx.conn.execute(
        "SELECT manga_id FROM suwayomi_manga WHERE series_id = %s ORDER BY manga_id", (series_id,)
    ).fetchall():
        m = ctx.suwayomi.manga(manga_id)
        if m is None:
            out.append(f"manga {manga_id}: Suwayomi has none")
            continue
        source = m["source"]["displayName"] if m["source"] else "source not installed"
        library = "in Suwayomi's library" if m["inLibrary"] else "not in Suwayomi's library"
        out.append(f"manga {manga_id} {m['title']}: {source}, {library}")
    return out


def search(ctx: workflow.Context, series_id: int, source_names: Sequence[str]) -> list[str]:
    """Return what each source's search finds for the series (SPEC "Source review", step 3).

    source_names: the sources to search, in order; none: sources.ranking.
    """
    series = reviewed(ctx, series_id)
    rows = published(ctx.conn, series_id)
    known = _names(ctx.conn, series)
    installed = {s["displayName"]: s["id"] for s in ctx.suwayomi.sources()}
    out: list[str] = []
    for name in source_names or ctx.cfg["sources"]["ranking"]:
        if name not in installed:
            out.append(f"{name}: not installed")
            continue
        try:
            out.append(f"{name}: {_search_source(ctx, installed[name], known, rows)}")
        except suwayomi.SourceUnavailableError as e:
            out.append(f"{name}: the source cannot answer: {str(e).splitlines()[0]}")
    return out


def _names(conn: db.Connection, series: db.Row) -> list[str]:
    """Return the names a series is searched by: its Kavita names, its files' ComicInfo Series, its manga's titles."""
    given: list[str | None] = [series["name"], series["localized_name"], series["original_name"]]
    given += [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT comicinfo_series FROM published_file WHERE series_id = %s ORDER BY 1", (series["id"],)
        )
    ]
    given += [r[0] for r in conn.execute("SELECT title FROM suwayomi_manga WHERE series_id = %s", (series["id"],))]
    return list(dict.fromkeys(n for n in given if n))


def _search_source(ctx: workflow.Context, source_id: str, known: Sequence[str], rows: Sequence[db.Row]) -> str:
    """Return 1 source's line: the manga found, its chapters, coverage, numbering, library and link."""
    hits: dict[int, suwayomi_replies.Manga] = {}
    for query in known:
        for hit in ctx.suwayomi.search_source(source_id, query):
            hits[hit["id"]] = hit
    found = redownload.pick_manga(list(hits.values()), known)
    if found is None:
        return f"no match ({len(hits)} hits)"
    listed = chapters(ctx.suwayomi, found)
    keys = {validation.chapter_key(r["identity"]) for r in rows}
    covered = len(keys & chapter_keys(listed))
    ref = reference(rows)
    picked = sample_files(rows, SAMPLE_CHAPTERS)
    agree = sum(
        redownload.pick_chapter(r["identity"], number(r["identity"]) or 0.0, ref, listed) is not None for r in picked
    )
    link = ctx.conn.execute("SELECT series_id FROM suwayomi_manga WHERE manga_id = %s", (found["id"],)).fetchone()
    return (
        f"manga {found['id']} {found['title']}: {len(listed)} chapters, lists {covered} of {len(keys)} published keys, "
        f"numbering agrees on {agree} of {len(picked)} sample files, "
        f"{'in' if found['inLibrary'] else 'not in'} Suwayomi's library, "
        f"{'linked to series ' + str(link['series_id']) if link else 'linked to no series'}"
    )


# ---------------------------------------------------------------- switch, replace, retire


def switch(ctx: workflow.Context, series_id: int, manga_id: int) -> list[str]:
    """Link the manga to the series and add it to Suwayomi's library; remove nothing (SPEC "Source review", step 5).

    The link is written first: a library manga with no link would be discovered as a new series. A no-op on a manga
    already linked and in the library.
    """
    reviewed(ctx, series_id)
    found, _ = manga(ctx, manga_id)
    row = ctx.conn.execute("SELECT series_id FROM suwayomi_manga WHERE manga_id = %s", (manga_id,)).fetchone()
    if row is not None and row["series_id"] != series_id:
        msg = f"manga {manga_id} is linked to series {row['series_id']}"
        raise workflow.Refused(msg)
    if row is not None and found["inLibrary"]:
        return [f"manga {manga_id} is already linked to series {series_id} and in Suwayomi's library: nothing changed"]
    if row is None:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "INSERT INTO suwayomi_manga (manga_id, source_id, title, series_id) VALUES (%s, %s, %s, %s)",
                (manga_id, str(found["sourceId"]), found["title"], series_id),
            )
    if not found["inLibrary"]:
        ctx.suwayomi.set_in_library(manga_id, in_library=True)
    done = f"manga {manga_id} {found['title']} linked to series {series_id} and in Suwayomi's library"
    return [f"{done}: its new chapters download; source replace queues its copies of published files"]


def replace(ctx: workflow.Context, series_id: int, manga_id: int, file_ids: Sequence[int] | None) -> list[str]:
    """Queue the manga's chapters matching the named published files (None: every one), then start the downloader.

    Skipped and printed: a file not confirmed, one busy with a change (rewrite.BUSY), one whose chapter key an
    unfinished chapter of the series has, one with no match. Refused unless the manga is linked to the series and in
    Suwayomi's library.
    """
    reviewed(ctx, series_id)
    linked(ctx.conn, series_id, manga_id)
    found, _ = manga(ctx, manga_id)
    if not found["inLibrary"]:
        msg = f"manga {manga_id} is not in Suwayomi's library: source switch adds it"
        raise workflow.Refused(msg)
    rows = published(ctx.conn, series_id)
    wanted = rows if file_ids is None else _files(ctx.conn, series_id, file_ids)
    skip = _skipping(ctx.conn, series_id)
    listed = ctx.suwayomi.chapters(manga_id)
    ref = reference(rows)
    out: list[str] = []
    queue: list[int] = []
    for r in wanted:
        head = f"file {r['id']} {key_text(r['identity'])}"
        why = skip(r)
        hit = None if why else redownload.pick_chapter(r["identity"], number(r["identity"]) or 0.0, ref, listed)
        if why or hit is None:
            out.append(f"{head}: skipped: {why or 'no match on the manga'}")
            continue
        queue.append(hit["id"])
        out.append(f"{head}: queued chapter {hit['id']} {hit['name']}")
    if queue:
        ctx.suwayomi.enqueue(queue)
        ctx.suwayomi.start_downloader()
    out.append(f"queued {len(queue)} of {len(wanted)}")
    return out


def _files(conn: db.Connection, series_id: int, file_ids: Sequence[int]) -> list[db.Row]:
    """Return the named published files of the series; refused (exit 2) for an id that is none of them."""
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM published_file WHERE series_id = %s", (series_id,))}
    missing = [i for i in file_ids if i not in rows]
    if missing:
        msg = f"no published file {', '.join(map(str, missing))} of series {series_id}"
        raise workflow.Refused(msg)
    return [rows[i] for i in dict.fromkeys(file_ids)]


def _skipping(conn: db.Connection, series_id: int) -> Callable[[db.Row], str | None]:
    """Return why replace skips a published file, or None: not confirmed, busy, an unfinished chapter of its key."""
    busy = {
        r[0]
        for r in conn.execute(
            sql.SQL("SELECT p.id FROM published_file p WHERE p.series_id = %s AND {}").format(
                rewrite.BUSY.format(p=sql.Identifier("p"))
            ),
            (series_id,),
        )
    }
    unfinished = {
        validation.chapter_key(r[0])
        for r in conn.execute("SELECT identity FROM chapter WHERE series_id = %s AND status <> 'done'", (series_id,))
    }

    def why(row: db.Row) -> str | None:
        if row["state"] != "confirmed":
            return "not confirmed"
        if row["id"] in busy:
            return "busy with a change (status lists it)"
        if validation.chapter_key(row["identity"]) in unfinished:
            return "an unfinished chapter of the series has its chapter key"
        return None

    return why


def retire(ctx: workflow.Context, series_id: int, manga_id: int) -> list[str]:
    """Remove the manga from Suwayomi's library, then delete its link to the series (SPEC "Source review", step 7).

    Refused while the manga has a chapter queued, a CBZ in its download folder, or a chapter an unfinished chapter of
    the series holds or expects. Prints first the series' published chapter keys no other linked manga lists. Out of
    the library first: a library manga with no link would be discovered as a new series.
    """
    reviewed(ctx, series_id)
    linked(ctx.conn, series_id, manga_id)
    found, source = manga(ctx, manga_id)
    listed = ctx.suwayomi.chapters(manga_id)
    _refuse_busy(ctx, series_id, (found, source), listed)
    covered: set[validation.ChapterKey] = set()
    for (other,) in ctx.conn.execute(
        "SELECT manga_id FROM suwayomi_manga WHERE series_id = %s AND manga_id <> %s", (series_id, manga_id)
    ).fetchall():
        covered |= chapter_keys(ctx.suwayomi.chapters(other))
    rows = published(ctx.conn, series_id)
    uncovered = [r for r in rows if validation.chapter_key(r["identity"]) not in covered]
    keys = ", ".join(key_text(r["identity"]) for r in uncovered) or "none"
    out = [f"published keys no other linked manga lists: {keys}"]
    ctx.suwayomi.set_in_library(manga_id, in_library=False)
    with db.transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM suwayomi_manga WHERE manga_id = %s", (manga_id,))
    out.append(f"manga {manga_id} {found['title']} out of Suwayomi's library and unlinked from series {series_id}")
    return out


def _refuse_busy(
    ctx: workflow.Context,
    series_id: int,
    found: tuple[suwayomi_replies.Manga, suwayomi_replies.Source],
    listed: Sequence[suwayomi_replies.Chapter],
) -> None:
    """Refuse (exit 2) while the manga has a chapter queued, a CBZ downloaded, or a chapter a chapter holds."""
    manga_id, title = found[0]["id"], found[0]["title"]
    queued = [e for e in ctx.suwayomi.download_queue()["queue"] if e["chapter"]["mangaId"] == manga_id]
    if queued:
        msg = f"manga {manga_id} has {len(queued)} chapters in Suwayomi's download queue"
        raise workflow.Refused(msg)
    source_folder, manga_folder = suwayomi.download_folders(found[1], title)
    folder = config.abs_path(ctx.cfg["paths"]["downloads"]) / suwayomi.MANGAS_FOLDER / source_folder / manga_folder
    files = sorted(folder.glob("*.cbz")) if folder.is_dir() else []
    if files:
        msg = f"manga {manga_id} has {len(files)} CBZ files in its download folder {folder}"
        raise workflow.Refused(msg)
    ids = [c["id"] for c in listed]
    held = ctx.conn.execute(
        """SELECT id FROM chapter WHERE series_id = %(series)s AND status <> 'done'
            AND (suwayomi_chapter_id = ANY(%(ids)s)
            OR (redownload::jsonb #>> '{expected,chapter_id}')::bigint = ANY(%(ids)s)
            OR (redownload::jsonb #>> '{expected,manga_id}')::bigint = %(manga)s) ORDER BY id""",
        {"series": series_id, "ids": ids, "manga": manga_id},
    ).fetchall()
    if held:
        msg = (
            f"manga {manga_id} has chapters unfinished chapters hold or expect (chapter "
            f"{', '.join(str(r['id']) for r in held)})"
        )
        raise workflow.Refused(msg)
