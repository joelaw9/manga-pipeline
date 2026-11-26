"""Split chapters' joins (SPEC "Retroactive updates", Split chapters).

`queue` (each `export`, after the comparison) gives every run (metadata/parts.py) of an `ok` in-scope series with a
Kavita id a join chapter: a `rewrite` chapter naming the run's first file, `join_parts` its other files, starting at
comicinfo, under the rewrite cap. `comicinfo_run` builds the joined file in staging; `rewrite.publish_run` queues it,
`rewrite.finish` replaces the first file and removes the others. `published` reads published files as the rule takes
them (the census uses it too).
"""

import logging
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from manga_pipeline import archive, comicinfo, config, db, names, rewrite, rewrite_covers, validation, volumes, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import parts
from manga_pipeline.stages import convert

log = logging.getLogger(__name__)

# The series a join may take: ok, in scope, Kavita holds them, and with a confirmed pipeline file numbered as a part
SERIES = sql.SQL("""SELECT s.* FROM series s WHERE s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND {scope}
    AND EXISTS (SELECT 1 FROM published_file p WHERE p.series_id = s.id AND p.origin = 'pipeline'
        AND p.state = 'confirmed' AND (p.identity::jsonb ->> 3) LIKE '%%.%%')
    ORDER BY s.id""").format(scope=workflow.SCOPE)


def published(conn: db.Connection, series_ids: Sequence[int] | None) -> dict[int, list[parts.File]]:
    """Return the series' published files as the run rule takes them ({series id: files}; None: every series)."""
    out: dict[int, list[parts.File]] = {}
    for r in conn.execute(
        """SELECT id, series_id, identity, comicinfo_volume, origin, state, joined_parts FROM published_file
        WHERE %(ids)s::bigint[] IS NULL OR series_id = ANY(%(ids)s::bigint[]) ORDER BY id""",
        {"ids": None if series_ids is None else list(series_ids)},
    ):
        _, season, side, number, _ = names.read_identity(r["identity"])
        out.setdefault(r["series_id"], []).append(
            parts.File(
                id=r["id"],
                season=season,
                side=side,
                number=number,
                volume=r["comicinfo_volume"],
                origin=r["origin"],
                confirmed=r["state"] == "confirmed",
                joined=r["joined_parts"],
            )
        )
    return out


def queue(ctx: workflow.Context) -> None:
    """Queue a join chapter for every run of the series a join may take, while the rewrite cap has room.

    A series whose override file says join_parts: false has none; one whose override file cannot be read is skipped
    (logged; the comparison holds it).
    """
    conn, cap = ctx.conn, ctx.cfg["export"]["rewrites_open"]
    overrides = config.abs_path(ctx.cfg["paths"]["naming_overrides"])
    made = 0
    for series in conn.execute(SERIES, workflow.scope_params(ctx.cfg)).fetchall():
        runs = parts.runs(published(conn, [series["id"]]).get(series["id"], []))
        if not runs:
            continue
        try:
            with workflow.share_errors():
                if not volumes.join_parts(overrides, series["rules_key"]):
                    continue
        except volumes.OverrideError as e:
            log.warning("%s: series %s: no join: %s", ctx.part, series["id"], e)
            continue
        for run in runs:
            open_ = db.one_row(conn, sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(rewrite.OPEN_REWRITES))[0]
            if open_ >= cap:
                if made:
                    log.info("%s: %d join(s) queued; the rewrite cap is full", ctx.part, made)
                return
            made += _queue_run(ctx, series, run)
    if made:
        log.info("%s: %d join(s) queued", ctx.part, made)


def _queue_run(ctx: workflow.Context, series: db.Row, run: parts.Run) -> int:
    """Insert 1 run's join chapter in 1 transaction once it is checked again; return 1 when inserted, else 0.

    Again: the run still stands, no file of it is busy, and no download of the series that is not done is at discover
    (its key not settled) or has the key of N or of a part.
    """
    conn = ctx.conn
    with db.transaction(conn):
        again = parts.runs(published(conn, [series["id"]]).get(series["id"], []))
        if run not in again:
            return 0
        rows = conn.execute(
            sql.SQL("""SELECT p.*, {busy} AS busy FROM published_file p WHERE p.id = ANY(%s)""").format(
                busy=rewrite.BUSY.format(p=sql.Identifier("p"))
            ),
            (list(run.members),),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        files = [by_id[i] for i in run.members]
        if any(f["busy"] for f in files) or _download_open(conn, series["id"], run, files):
            return 0
        first, title = files[0], next((f["comicinfo_title"] for f in files if f["comicinfo_title"]), None)
        whole = str(run.whole)
        target = PurePosixPath(first["library_path"]).parent / names.file_name(
            float(whole), title or "", names.to_volume(first["comicinfo_volume"])
        )
        now = workflow.now(conn)
        made = conn.execute(
            """INSERT INTO chapter (series_id, kind, identity, source, stage, status, published_file_id, join_parts,
                library_path, comicinfo_series, comicinfo_volume, comicinfo_number, comicinfo_title, comicinfo_date,
                naming_kind, naming_volume_source, discovered_at, updated_at)
            VALUES (%s, 'rewrite', %s, %s, 'comicinfo', 'ready', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (published_file_id) WHERE published_file_id IS NOT NULL AND status != 'done' DO NOTHING
            RETURNING id""",
            (
                series["id"],
                names.renumbered(first["identity"], whole),
                first["source"],
                first["id"],
                list(run.members[1:]),
                str(target),
                series["comicinfo_series"],
                first["comicinfo_volume"],
                whole,
                title,
                first["comicinfo_date"],
                first["kind"],
                first["volume_source"],
                now,
                now,
            ),
        ).fetchone()
    if made is None:
        return 0
    log.info(
        "%s: series %s: join of %s queued as %s",
        ctx.part,
        series["id"],
        ", ".join(f["library_path"] for f in files),
        target,
    )
    return 1


def _download_open(conn: db.Connection, series_id: int, run: parts.Run, files: Sequence[db.Row]) -> bool:
    """Return whether a download of the series that is not done is at discover or has the key of N or of a file."""
    keys = {validation.chapter_key(f["identity"]) for f in files}
    keys.add((run.season, "", str(run.whole)))
    for r in conn.execute(
        """SELECT stage, identity FROM chapter WHERE series_id = %s AND kind = 'download' AND status <> 'done'""",
        (series_id,),
    ):
        if r["stage"] == "discover" or (r["identity"] and validation.chapter_key(r["identity"]) in keys):
            return True
    return False


def comicinfo_run(ctx: workflow.Context, chapter: db.Row) -> workflow.Done | workflow.NeedsHuman:
    """Build the joined file in staging: the files' pages byte for byte in run order, numbered anew, and its ComicInfo.

    A leading cover page of the first file keeps its name; a volume cover page of a later file is left out, with its
    Page entry. ComicInfo.xml: the first file's own fields (rewrite.file_fields), Number N, Title the first title of the
    run else "Chapter N", PageCount, and every file's Page entries re-indexed (none when a file has none). A file gone
    or unreadable, or holding no page, holds the join for a person.
    """
    conn = ctx.conn
    series = db.one_row(conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    ids = [chapter["published_file_id"], *chapter["join_parts"]]
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM published_file WHERE id = ANY(%s)", (ids,))}
    if len(rows) != len(ids):
        return workflow.NeedsHuman(f"a file of the join is gone (published files {ids})")
    built = _read(ctx.cfg, [rows[i] for i in ids])
    if isinstance(built, str):
        return workflow.NeedsHuman(built)
    first_xml, pages, entries = built
    try:
        fields = rewrite.file_fields(ctx, series, first_xml, chapter["comicinfo_volume"])
    except volumes.VolumeError as e:
        return workflow.NeedsHuman(str(e))
    whole = chapter["comicinfo_number"]
    fields["Number"] = whole
    fields["Title"] = chapter["comicinfo_title"] or f"Chapter {whole}"
    fields["PageCount"] = len(pages)
    xml = comicinfo.build(fields, entries)
    staged = config.abs_path(ctx.cfg["paths"]["staging"]) / convert.staged_name(conn, chapter)
    workflow.holds_claim(ctx, chapter)
    try:
        size = archive.write(staged, [*((n, d) for n, d, _ in pages), (archive.COMICINFO, xml)])
        _check(staged, pages)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise

    def record(c: db.Connection) -> None:
        c.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s WHERE id = %s",
            (config.to_stored(ctx.cfg, staged), size, chapter["id"]),
        )

    return workflow.Done(record)


type _Page = tuple[str, bytes, int]  # (name in the joined file, bytes, CRC)


def _read(cfg: config.Config, files: Sequence[db.Row]) -> tuple[bytes, list[_Page], list[dict[str, str]]] | str:
    """Return the first file's ComicInfo.xml, the joined pages and their Page entries; else why the join cannot be."""
    taken: list[tuple[int, int, str, bytes, int]] = []  # (file, its page index, name, bytes, CRC)
    xmls: list[bytes | None] = []
    for n, f in enumerate(files):
        path = config.to_local(cfg, f["library_path"])
        try:
            with workflow.share_errors(), zipfile.ZipFile(path) as z:
                infos = {i.filename: i for i in z.infolist()}
                page_names = archive.archive_pages(infos)
                xmls.append(archive.read_comicinfo(path))
                for i, name in enumerate(page_names):
                    if n and rewrite_covers.is_cover(name):
                        continue  # a later file's volume cover
                    taken.append((n, i, name, z.read(infos[name]), infos[name].CRC))
        except archive.UNREADABLE as e:
            return f"{f['library_path']} cannot be read: {e}"
        if not page_names:
            return f"{f['library_path']} holds no page"
    first_xml = xmls[0]
    if first_xml is None:
        return f"{files[0]['library_path']} holds no ComicInfo.xml"
    pages = _numbered(taken)
    entries = _entries(taken, [comicinfo.read_pages(x) if x else None for x in xmls])
    return first_xml, pages, entries


def _numbered(taken: Sequence[tuple[int, int, str, bytes, int]]) -> list[_Page]:
    """Return the pages named anew in order: 1, 2, … (zero-padded).

    A leading cover page of the first file keeps its name.
    """
    width = len(str(len(taken)))
    out: list[_Page] = []
    k = 0
    for pos, (n, _, name, data, crc) in enumerate(taken):
        if pos == 0 and n == 0 and rewrite_covers.is_cover(name):
            out.append((name, data, crc))
            continue
        k += 1
        out.append((f"{k:0{width}d}{Path(name).suffix.lower()}", data, crc))
    return out


def _entries(
    taken: Sequence[tuple[int, int, str, bytes, int]], per_file: Sequence[list[dict[str, str]] | None]
) -> list[dict[str, str]]:
    """Return the Page entries of the joined pages, re-indexed; none when a file has none."""
    if any(not e for e in per_file):
        return []
    by_image = [{e.get("Image"): e for e in entries or []} for entries in per_file]
    out: list[dict[str, str]] = []
    for new, (n, i, _, _, _) in enumerate(taken):
        entry = by_image[n].get(str(i))
        if entry is None:
            return []  # entries that do not name every page: none rather than wrong ones
        out.append({**entry, "Image": str(new)})
    return out


def _check(staged: Path, pages: Sequence[_Page]) -> None:
    """Every page's CRC in the joined file is its source's."""
    with zipfile.ZipFile(staged) as z:
        got = {i.filename: i.CRC for i in z.infolist()}
    if any(got.get(n) != crc for n, _, crc in pages):
        msg = f"{staged}: a joined page's CRC differs from its file's"
        raise RuntimeError(msg)
