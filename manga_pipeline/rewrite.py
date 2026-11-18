"""Retroactive rewrites of published files (SPEC "Retroactive updates"): compare, comicinfo, publish, finish, restore.

`export`'s comparison, a rewrite chapter's comicinfo and publish (which only queues the rewrite on its file's row; its
convert, for a cover, is rewrite_covers'), `finish` (the file acts, recorded step by step on the row), and the progress
restore once Kavita has re-read the file.

A rewrite never re-encodes pages, and the library never lacks the chapter's file. Only `export` (1 at a time
fleet-wide: its run lock) acts on a queued row.
"""

import hashlib
import json
import logging
import os
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from manga_pipeline import (
    archive,
    baseline,
    chapter_names,
    comicinfo,
    config,
    db,
    errors,
    handled,
    names,
    retention,
    rewrite_covers,
    volumes,
    workflow,
)
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.db import sql
from manga_pipeline.metadata import covers as cover_images
from manga_pipeline.metadata import naming, platforms, rewrite_plan
from manga_pipeline.stages import comicinfo as comicinfo_stage
from manga_pipeline.stages import convert, publish

log = logging.getLogger(__name__)

# The modules whose code decides a published file's volume: a change to them compares every series again.
_CODE_MODULES = ("metadata/naming.py", "chapter_names.py", "names.py", "volumes.py", "metadata/platforms.py",
                 "metadata/episodes.py", "metadata/rewrite_plan.py", "comicinfo.py")  # fmt: skip
CODE = hashlib.sha256(b"".join((Path(__file__).parent / m).read_bytes() for m in _CODE_MODULES)).hexdigest()
# Elements the comicinfo stage writes from the series (new on a rewrite); every other element of the old file is its
# own chapter's and is kept, Notes, PageCount and BlackAndWhite included (SPEC "Retroactive updates").
SERIES_LEVEL = (comicinfo_stage.OWN - {"Notes", "PageCount", "BlackAndWhite"}) | {"Series", "LocalizedSeries", "Volume"}
# Columns of a queued rewrite on its published_file row, cleared together.
PENDING = ("pending_path", "pending_staged", "pending_size", "pending_volume", "pending_volume_source", "pending_step",
           "pending_cover", "pending_copy", "pending_title", "pending_unedited")  # fmt: skip
PROGRESS = ("carry_pages_read", "carry_old_pages", "carry_new_pages", "carry_page_shift", "carry_kavita_modified")
CARRY = (*PROGRESS, "carry_cover")
# Open rewrite chapters and better copies (both name their file): the cap export.rewrites_open counts them.
OPEN_REWRITES = sql.SQL("published_file_id IS NOT NULL AND status NOT IN ('done', 'needs_human')")
# A published file busy with a change, {p} its alias (fill it with sql.Identifier(alias)): an open chapter names it (a
# rewrite or a copy), a queued rewrite, a wait for Kavita, a cover refresh due, or a failed rewrite. No new rewrite or
# copy takes it meanwhile.
BUSY = sql.SQL("""({p}.pending_path IS NOT NULL OR {p}.indexed_since IS NOT NULL OR {p}.pending_error IS NOT NULL
    OR {p}.carry_cover IS TRUE OR EXISTS (
        SELECT 1 FROM chapter b WHERE b.published_file_id = {p}.id AND b.status <> 'done'))""")
# finish's file acts, each recorded in published_file.pending_step once done
STEP_DATED, STEP_RETAINED, STEP_RENAMED, STEP_OLD_REMOVED = 1, 2, 3, 4


def _set_null(columns: Iterable[str]) -> sql.Composed:
    """`column = NULL, ...` of columns, an SQL SET list."""
    return sql.SQL(", ").join(sql.SQL("{} = NULL").format(sql.Identifier(c)) for c in columns)


# --- the comparison


def compare(ctx: workflow.Context) -> None:
    """Compare every in-scope ok series with a Kavita id and its baseline taken whose naming inputs changed.

    Queue a rewrite chapter for each published file whose volume or cover should change, while fewer than
    export.rewrites_open are open, and record the cover images wanted. A series compared whole stores the hash it was
    compared with; one cut short, or whose files' covers could not be read, is compared again next run.
    """
    conn, cap = ctx.conn, ctx.cfg["export"]["rewrites_open"]
    candidates = conn.execute(
        sql.SQL("""SELECT s.* FROM series s WHERE s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND {scope}
        ORDER BY s.id""").format(scope=workflow.SCOPE),
        workflow.scope_params(ctx.cfg),
    ).fetchall()
    for series in candidates:
        if baseline.needed(conn, series):
            continue
        room = cap - db.one_row(conn, sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(OPEN_REWRITES))[0]
        if room <= 0:
            log.info("%s: %d rewrites open (export.rewrites_open): the comparison waits", ctx.part, cap)
            return
        if not rewrite_covers.fill_states(ctx, series):  # outside the lock: it may read many files once
            continue
        _compare_series(ctx, series, room)


def _compare_series(ctx: workflow.Context, series: db.Row, room: int) -> None:
    """Compare 1 series whose naming inputs changed since its stored hash.

    Record the cover images wanted and queue up to room rewrite chapters; store the hash once every change is queued. 1
    transaction (the database-wide write lock): the hash and the comparison read the same inputs once.
    """
    conn = ctx.conn
    # ponytail: it reads the series' open downloads' ComicInfo.xml under the lock, only when the hash changed
    with db.transaction(conn):
        try:
            digest = _hash(ctx, series)
            if digest == _stored_hash(conn, series["id"]):
                return
            todo, wants, _ = _plan(ctx, series)
        except volumes.OverrideError as e:  # the series' own file: a person must fix it, as at comicinfo
            workflow.needs_human(conn, "series", series["id"], f"{e} (comparing its published files)")
            log.warning("%s: series %s (%s): %s", ctx.part, series["id"], series["name"], e)
            return
        except volumes.VolumeError as e:  # a download's volume: its chapter is held at comicinfo; compared again
            log.warning("%s: series %s (%s): not compared: %s", ctx.part, series["id"], series["name"], e)
            return
        for v in sorted(wants):
            conn.execute(
                "INSERT INTO volume_cover_wanted (series_id, volume) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (series["id"], int(v)),  # a wanted volume is whole (_plan)
            )
        made = _queue(ctx, series, todo, room)
        if made >= room and len(todo) > room:
            return  # cut short: compared again next run
        conn.execute(
            """INSERT INTO series_naming (series_id, naming_hash, compared_at) VALUES (%s, %s, %s)
            ON CONFLICT (series_id) DO UPDATE SET naming_hash = excluded.naming_hash,
                compared_at = excluded.compared_at""",
            (series["id"], digest, workflow.now(conn)),
        )
    if made:
        log.info("%s: series %s (%s): %d rewrite(s) queued", ctx.part, series["id"], series["name"], made)


def _queue(ctx: workflow.Context, series: db.Row, todo: dict[int, "Change"], room: int) -> int:
    """Queue a rewrite chapter for todo's files ({file id: Change}) in order until room are made; how many were."""
    made = 0
    for file_id, change in todo.items():
        if made >= room:
            break
        made += _queue_chapter(ctx, series, file_id, change)
    return made


type CoverChange = tuple[Literal["drop"]] | tuple[Literal["insert"], str, db.Row]
"""A file's cover change: ("drop",), or ("insert", volume, its series_volume_cover row)."""


class Change(NamedTuple):
    """The change a rewrite chapter makes to 1 published file (_plan).

    Its volume and volume source, its cover (None, ("insert", volume, its series_volume_cover row) or ("drop",)), its
    title ("" = untitled) and the unedited values it keeps afterwards (rewrite_plan.edit_changes).
    """

    volume: float | None
    source: str | None
    cover: CoverChange | None
    title: str
    unedited: naming.Unedited | None


def _plan(ctx: workflow.Context, series: db.Row) -> tuple[dict[int, Change], set[str], bool]:
    """Return (todo, wants, unknown) of 1 series.

    todo: {published file id: Change} of the files whose volume, cover or (by a hand edit) title should change, in id
    order; wants: the volumes whose cover image intake should fetch (that file's cover is then left for later); unknown:
    some file's cover is not known yet (covers are then left out). VolumeError as naming.resolve raises it.
    """
    conn, sid = ctx.conn, series["id"]
    rows, overrides, mangadex, platform = chapter_names.inputs(ctx, series)
    basis = series["naming_basis"]
    files = {r["id"]: r for r in conn.execute("SELECT * FROM published_file WHERE series_id = %s", (sid,))}
    edits, unedited = chapter_names.edits(conn, sid), {i: f["unedited"] for i, f in files.items()}
    held = rewrite_plan.held(rows, edits, unedited)
    edited = rewrite_plan.edit_changes(rows, edits, unedited)
    moved: dict[int, tuple[float | None, str | None]] = {
        i: v for i, v in rewrite_plan.changes(basis, rows, overrides, mangadex, platform).items() if i not in held
    }
    moved.update({i: (v, source) for i, (v, source, _, _) in edited.items() if i in held})
    states = {i: f["cover_state"] for i, f in files.items()}
    unknown = any(s is None for s in states.values())
    cover: dict[int, CoverChange] = {}
    wants: set[str] = set()
    if not unknown:
        taken = {
            r[0]
            for r in conn.execute(
                """SELECT v.volume FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id
                WHERE c.series_id = %s AND c.status <> 'done'""",
                (sid,),
            )
        }
        images: dict[str, db.Row] = {
            names.fmt_num(r["volume"]): r
            for r in conn.execute("SELECT volume, path, sha256 FROM series_volume_cover WHERE series_id = %s", (sid,))
        }
        given_up: set[str] = {
            names.fmt_num(r[0])
            for r in conn.execute(
                "SELECT volume FROM volume_cover_wanted WHERE series_id = %s AND attempts >= %s",
                (sid, ctx.cfg["workflow"]["attempt_limit"]),
            )
        }
        records = {
            r["kind"]: r["payload"]
            for r in conn.execute(
                "SELECT kind, payload FROM fetched WHERE series_id = %s AND source = 'mangadex' AND kind = ANY(%s)",
                (sid, ["manga", "covers"]),
            )
        }
        shas = {v: r["sha256"] for v, r in images.items()}
        now = rewrite_plan.moved_rows(rows, moved)
        covered = naming.covered_volumes(basis, now, (overrides or {}).get("volumes") or {}, mangadex)
        for fid, act in rewrite_plan.cover_changes(now, covered, states=states, taken=taken, images=shas).items():
            if act[0] == "drop":
                cover[fid] = ("drop",)
            elif (v := act[1]) in images:
                cover[fid] = ("insert", v, images[v])
            else:
                number = names.to_volume(v)
                if number is None:  # cover_changes names volumes by fmt_num: always a number
                    msg = f"cover change for {v!r}: not a volume number"
                    raise ValueError(msg)
                if (
                    v not in given_up
                    and number == int(number)
                    and records.get("manga")
                    and cover_images.volume_choice(records["manga"], records.get("covers") or [], number)
                ):
                    wants.add(v)
                if rewrite_covers.cover_volume(states[fid]) is not None:  # another volume's cover: dropped meanwhile
                    cover[fid] = ("drop",)
    todo: dict[int, Change] = {}
    for fid in sorted(set(moved) | set(cover) | set(edited)):
        current = (names.to_volume(files[fid]["comicinfo_volume"]), files[fid]["volume_source"])
        title, kept = edited.get(fid, (None, None, files[fid]["comicinfo_title"] or "", files[fid]["unedited"]))[2:]
        volume, source = moved.get(fid, current)
        todo[fid] = Change(volume, source, cover.get(fid), title, kept)
    return todo, wants, unknown


def _stored_hash(conn: db.Connection, series_id: int) -> str | None:
    row = conn.execute("SELECT naming_hash FROM series_naming WHERE series_id = %s", (series_id,)).fetchone()
    return row[0] if row else None


def _hash(ctx: workflow.Context, series: db.Row) -> str:
    """Return the hash of the series' naming inputs. VolumeError for a malformed override file.

    The inputs: its basis, override file, hand edits, stored MangaDex aggregate, cover list and platform list, its
    stored cover images, the naming code, its published files' rows, its open downloads and the covers they took, and
    its open rewrites.
    """
    conn, sid = ctx.conn, series["id"]
    overrides = volumes.override_file(config.abs_path(ctx.cfg["paths"]["naming_overrides"]), series["rules_key"])
    fetched = conn.execute(
        """SELECT source, kind, payload_hash FROM fetched WHERE series_id = %s
            AND ((source = 'mangadex' AND kind IN ('aggregate', 'covers', 'manga')) OR kind = %s)
        ORDER BY source, kind""",
        (sid, platforms.KIND),
    ).fetchall()
    files = conn.execute(
        """SELECT id, library_path, state, comicinfo_volume, comicinfo_number, comicinfo_title, kind, volume_source,
            source_name, pending_path, indexed_since, pending_error, cover_state, pending_cover, carry_cover, unedited
        FROM published_file
        WHERE series_id = %s
        ORDER BY id""",
        (sid,),
    ).fetchall()
    downloads = conn.execute(  # the rows chapter_names reads: claims coming and going change nothing
        """SELECT id, download_path, naming_kind, comicinfo_number, comicinfo_volume FROM chapter
        WHERE series_id = %s AND kind = 'download' AND stage != 'discover' AND published_file_id IS NULL
            AND (status = ANY(%s) OR (status = 'needs_human' AND naming_kind IS NOT NULL)) ORDER BY id""",
        (sid, list(chapter_names.OPEN)),
    ).fetchall()
    rewrites = conn.execute(  # a file skipped for its open rewrite is compared again once that chapter ends
        "SELECT id FROM chapter WHERE series_id = %s AND published_file_id IS NOT NULL AND status <> 'done'"
        " ORDER BY id",
        (sid,),
    ).fetchall()
    images = conn.execute(
        "SELECT volume, sha256 FROM series_volume_cover WHERE series_id = %s ORDER BY volume", (sid,)
    ).fetchall()
    taken = conn.execute(
        """SELECT v.volume FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id
        WHERE c.series_id = %s AND c.status <> 'done' ORDER BY v.volume""",
        (sid,),
    ).fetchall()
    inputs: list[object] = [series["naming_basis"], overrides, [tuple(r) for r in fetched], CODE]
    inputs += [[tuple(r) for r in files], [tuple(r) for r in downloads], [tuple(r) for r in rewrites]]
    edits = conn.execute(
        "SELECT season, side, number, title, volume FROM naming_edit WHERE series_id = %s ORDER BY side, season,"
        " number",
        (sid,),
    ).fetchall()
    inputs += [[tuple(r) for r in images], [tuple(r) for r in taken], [tuple(r) for r in edits]]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def _queue_chapter(ctx: workflow.Context, series: db.Row, file_id: int, change: Change) -> int:
    """Make 1 rewrite chapter for a confirmed published file with no open chapter and nothing queued or awaited.

    It starts at convert when its cover changes (an insert's cover handed over as a volume_cover row), else at
    comicinfo; it carries the file's title and unedited values afterwards (title "" = untitled). Returns 1 when made,
    else 0.
    """
    conn = ctx.conn
    row = conn.execute(
        sql.SQL("SELECT * FROM published_file p WHERE p.id = %s AND p.state = 'confirmed' AND NOT {}").format(
            BUSY.format(p=sql.Identifier("p"))
        ),
        (file_id,),
    ).fetchone()
    if row is None:
        return 0
    target = _target(row, change.volume, change.title)
    now = workflow.now(conn)
    made = conn.execute(
        """INSERT INTO chapter (series_id, kind, identity, source, stage, status, published_file_id, library_path,
            comicinfo_series, comicinfo_volume, comicinfo_number, comicinfo_title, comicinfo_date, naming_kind,
            naming_volume_source, drop_cover, naming_unedited, discovered_at, updated_at)
        VALUES (%s, 'rewrite', %s, %s, %s, 'ready', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (published_file_id) WHERE published_file_id IS NOT NULL AND status != 'done' DO NOTHING
        RETURNING id""",
        (
            series["id"],
            row["identity"],
            row["source"],
            "comicinfo" if change.cover is None else "convert",
            file_id,
            target,
            series["comicinfo_series"],
            None if change.volume is None else names.fmt_num(change.volume),
            row["comicinfo_number"],
            change.title or None,
            row["comicinfo_date"],
            row["kind"],
            change.source,
            change.cover == ("drop",),
            None if change.unedited is None else json.dumps(change.unedited),
            now,
            now,
        ),
    ).fetchone()
    if made is None:
        return 0
    if change.cover is not None and change.cover[0] == "insert":
        conn.execute(
            "INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, %s, %s, %s)",
            (made["id"], change.cover[2]["path"], change.cover[2]["sha256"], change.cover[1]),
        )
    log.info(
        "%s: %s: volume %s -> %s (%s)%s%s%s", ctx.part, row["library_path"], row["comicinfo_volume"],
        None if change.volume is None else names.fmt_num(change.volume), change.source,
        f", title {change.title!r}" if change.title != (row["comicinfo_title"] or "") else "",
        f", renamed {target}" if target != row["library_path"] else "", _cover_text(change.cover),
    )  # fmt: skip
    return 1


def _cover_text(cover: CoverChange | None) -> str:
    return (
        "" if cover is None else f", insert volume {cover[1]}'s cover" if cover[0] == "insert" else ", drop its cover"
    )


def _target(row: db.Row, volume: float | None, title: str) -> str:
    """Return the stored path the rewritten file takes (D7.8).

    A pipeline file is named for its new volume and title; a bulk file keeps its name.
    """
    if row["origin"] != "pipeline":
        return row["library_path"]
    name = names.file_name(float(row["comicinfo_number"]), title, volume)
    return str(PurePosixPath(row["library_path"]).parent / name)


# --- the rewrite chapter's stages


def comicinfo_run(ctx: workflow.Context, chapter: db.Row) -> workflow.Done | workflow.NeedsHuman:
    """Copy the library file to staging with a new ComicInfo.xml; every other member copied byte for byte.

    The new ComicInfo.xml holds the series-level fields now, the new volume, the old file's own fields and pages.
    """
    conn = ctx.conn
    series = db.one_row(conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    file = db.one_row(conn, "SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],))
    # the cover changed at convert
    converted = chapter["convert_result"] and rewrite_covers.read_result(chapter["convert_result"])
    source = config.to_local(ctx.cfg, file["library_path"])  # unchanged until finish: a rerun reads the same
    old = archive.read_comicinfo(source)
    if old is None:
        return workflow.NeedsHuman(f"{source} holds no ComicInfo.xml")
    kept = {k: v for k, v in comicinfo.read_all(old).items() if k not in SERIES_LEVEL and comicinfo.field_ok(k, v)}
    try:
        group = volumes.series_group(config.abs_path(ctx.cfg["paths"]["naming_overrides"]), series["rules_key"])
    except volumes.VolumeError as e:
        return workflow.NeedsHuman(str(e))
    elements = comicinfo_stage.series_fields(ctx, series, None, None, group)
    old_links = (comicinfo.read_all(old).get("Web") or "").split()
    links = (elements["Web"] or "").split()
    fields: dict[str, str | int | None] = {**elements, **kept}
    fields["Web"] = " ".join([*links, *(u for u in old_links if u not in links)])
    fields["Series"] = series["comicinfo_series"]
    if series["localized_name"]:
        fields["LocalizedSeries"] = series["localized_name"]
    fields.pop("Volume", None)
    if chapter["comicinfo_volume"] is not None:
        fields["Volume"] = chapter["comicinfo_volume"]
    if (chapter["comicinfo_title"] or "") != (file["comicinfo_title"] or ""):  # a hand edit, or its removal
        fields["Title"] = chapter["comicinfo_title"] or f"Chapter {names.fmt_num(float(file['comicinfo_number']))}"
    pages = comicinfo.read_pages(old)
    if converted:
        pages = rewrite_covers.shift_pages(pages, converted)
        fields["PageCount"] = page_count(config.to_local(ctx.cfg, chapter["staged_path"]))
    xml = comicinfo.build(fields, pages)
    workflow.holds_claim(ctx, chapter)
    if converted:  # convert's staged file, its ComicInfo.xml replaced in place
        staged = config.to_local(ctx.cfg, chapter["staged_path"])
        size = archive.replace_member(staged, archive.COMICINFO, xml)
    else:
        staged = config.abs_path(ctx.cfg["paths"]["staging"]) / convert.staged_name(conn, chapter)
        size = copy_replacing(source, staged, archive.COMICINFO, xml)

    def record(c: db.Connection) -> None:
        c.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s WHERE id = %s",
            (config.to_stored(ctx.cfg, staged), size, chapter["id"]),
        )

    return workflow.Done(record)


def page_count(cbz: Path) -> int:
    """How many pages a CBZ holds (its member list only)."""
    with zipfile.ZipFile(cbz) as z:
        return len(archive.archive_pages(z.namelist()))


def copy_replacing(source: Path, dest: Path, name: str, data: bytes) -> int:
    """Write dest: source's members in order, member `name` (compared without case) replaced by data.

    data is stored last when the source had none; every other member's bytes copied unchanged, checked by CRC against
    the source. Returns dest's size.
    """
    with zipfile.ZipFile(source) as z:
        members = [(i.filename, z.read(i), i.CRC) for i in z.infolist() if i.filename.lower() != name.lower()]
    size = archive.write(dest, [*((n, d) for n, d, _ in members), (name, data)])
    with zipfile.ZipFile(dest) as z:
        got = {i.filename: i.CRC for i in z.infolist()}
    if any(got.get(n) != crc for n, _, crc in members):
        dest.unlink()
        msg = f"{dest}: a copied member's CRC differs from {source}'s"
        raise RuntimeError(msg)
    return size


def publish_run(ctx: workflow.Context, chapter: db.Row) -> workflow.Done | workflow.NeedsHuman:
    """Queue the rewrite, or a better copy's replacement, on its file's row with the API key's user's progress.

    The chapter is then done (SPEC "Retroactive updates"). A copy also queues its values (publish.row_values), puts its
    download aside the retention way and is recorded as a handled chapter `published`. A publish run again finds its
    rewrite queued or finished and is done.
    """
    conn = ctx.conn
    file = db.one_row(conn, "SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],))
    if file["pending_path"] == chapter["library_path"] or file["indexed_since"] is not None:
        return workflow.Done()  # queued, or finished, by an earlier run of this stage
    series = db.one_row(conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    old = config.to_local(ctx.cfg, file["library_path"])
    seen = [f for f in ctx.kavita.series_files(series["kavita_series_id"]) if f["path"] == old]
    if not seen:  # a confirmed file Kavita lost: tried again by a later run, its attempts counted
        msg = f"Kavita does not list {old} in series {series['kavita_series_id']}"
        raise RuntimeError(msg)
    staged = config.to_local(ctx.cfg, chapter["staged_path"])
    target = config.to_local(ctx.cfg, chapter["library_path"])
    new_pages = page_count(staged)
    converted = chapter["convert_result"] and rewrite_covers.read_result(chapter["convert_result"])
    clash = conn.execute(
        "SELECT id FROM published_file WHERE library_path = %s AND id <> %s", (chapter["library_path"], file["id"])
    ).fetchone()
    if clash or (target != old and errors.exists(target)):
        return workflow.NeedsHuman(f"{target} is already in the library; a library file is never overwritten")
    copy = chapter["kind"] == "download"  # a better copy (SPEC "Library copies")
    read, old_pages = seen[0]["pages_read"], page_count(old)
    if copy:
        values = publish.row_values(conn, chapter)
        cover, shift = values["cover_state"], copy_shift(read, old_pages, new_pages)
        workflow.holds_claim(ctx, chapter)
        retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))
    else:
        cover, shift = converted["cover_state"] if converted else None, new_pages - old_pages

    def record(c: db.Connection) -> None:
        now = cover and rewrite_covers.current_state(c, chapter["id"], chapter["series_id"], cover)
        c.execute(
            """UPDATE published_file SET pending_path = %s, pending_staged = %s, pending_size = %s,
                pending_volume = %s, pending_volume_source = %s, pending_cover = %s, pending_step = 0,
                pending_attempts = 0, carry_pages_read = %s, carry_old_pages = %s, carry_new_pages = %s,
                carry_page_shift = %s, carry_kavita_modified = %s, carry_cover = %s, pending_copy = %s,
                pending_title = %s, pending_unedited = %s
            WHERE id = %s AND pending_path IS NULL""",
            (
                chapter["library_path"],
                chapter["staged_path"],
                chapter["staged_size"],
                chapter["comicinfo_volume"],
                chapter["naming_volume_source"],
                now,
                read,
                seen[0]["pages"],
                new_pages,
                shift,
                seen[0]["modified"],
                copy or (bool(converted) and converted["cover_state"] != file["cover_state"]),  # a copy's page 1 is new
                json.dumps({"values": values, "digest": chapter["content_digest"]}) if copy else None,
                chapter["comicinfo_title"],
                _json(file["unedited"] if copy else chapter["naming_unedited"]),  # a copy keeps the file's edits
                file["id"],
            ),
        )
        if copy:
            handled.record(c, chapter["series_id"], chapter["identity"], "published", chapter["content_digest"])

    return workflow.Done(record)


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value)


# --- finishing queued rewrites


def copy_shift(read: int, old_pages: int, new_pages: int) -> int:
    """Return the page shift a better copy's progress takes.

    The pages read scaled to its page count, as a shift from the read page (the restore clamps it).
    """
    return round(read * new_pages / old_pages) - read if old_pages else 0


def finish_all(ctx: workflow.Context) -> None:
    """Finish every queued rewrite that has failed fewer than workflow.attempt_limit times.

    A row whose act fails records its error and 1 attempt and resumes at its next step on the next run; the others go
    on (a share error ends the run). At the limit it waits for `rewrite retry` (or, before its rename, `rewrite drop`).
    """
    rows = ctx.conn.execute(
        "SELECT * FROM published_file WHERE pending_path IS NOT NULL AND pending_attempts < %s ORDER BY id",
        (ctx.cfg["workflow"]["attempt_limit"],),
    ).fetchall()
    for row in rows:
        try:
            with workflow.share_errors():
                finish(ctx, row)
        except workflow.Unreachable:
            raise
        except Exception as e:
            with db.transaction(ctx.conn):
                ctx.conn.execute(
                    """UPDATE published_file SET pending_error = %s, pending_attempts = pending_attempts + 1
                    WHERE id = %s""",
                    (f"{type(e).__name__}: {e}", row["id"]),
                )
            log.warning("%s: rewrite of %s failed: %s", ctx.part, row["library_path"], e)


def finish(ctx: workflow.Context, row: db.Row) -> None:
    """Do the file acts of 1 queued rewrite, each recorded on the row once done (pending_step).

    So a retry starts at the next: 1 the staged file's time set to now; 2 the old file hard-linked into retention; 3 the
    staged file renamed over the target; 4 a renamed file's old name removed; 5 the row takes the new path, size and
    volume.
    """
    cfg, conn = ctx.cfg, ctx.conn
    staged = config.to_local(cfg, row["pending_staged"])
    target = config.to_local(cfg, row["pending_path"])
    old = config.to_local(cfg, row["library_path"])
    step = row["pending_step"]
    if step < STEP_RENAMED and not errors.exists(staged):
        if not _renamed(row, target, old):
            _clear(conn, row["id"])
            log.warning("%s: rewrite of %s dropped: its staged file is gone", ctx.part, row["library_path"])
            return
        step = record_step(conn, row["id"], STEP_RENAMED)  # renamed in before a crash recorded it
    if step < STEP_DATED:
        os.utime(staged)  # the rename keeps it: Kavita skips a file it judges unchanged by its date
        step = record_step(conn, row["id"], STEP_DATED)
    if step < STEP_RETAINED:
        retention.put_library(cfg, old)
        step = record_step(conn, row["id"], STEP_RETAINED)
    if step < STEP_RENAMED:
        if target != old and errors.exists(target):
            msg = f"{target} is already in the library; a library file is never overwritten"
            raise FileExistsError(msg)
        staged.replace(target)
        step = record_step(conn, row["id"], STEP_RENAMED)
    if step < STEP_OLD_REMOVED:
        if target != old and errors.exists(old):
            old.unlink()
        step = record_step(conn, row["id"], STEP_OLD_REMOVED)
    _settle(conn, row)
    log.info("%s: rewrote %s as %s", ctx.part, row["library_path"], row["pending_path"])


def _renamed(row: db.Row, target: Path, old: Path) -> bool:
    """Return whether a queued rewrite whose staged file is gone was renamed over its target before a crash recorded it.

    It was when a new name holds a file of the queued size, or the same name holds the rewritten file (is_new).
    """
    if target != old:
        return errors.exists(target) and target.stat().st_size == row["pending_size"]
    return is_new(target, row)


def _settle(conn: db.Connection, row: db.Row) -> None:
    """Do finish's last act: the row takes the new path, size, volume, title and cover (a better copy's values too).

    Its pending fields are cleared; the series is due a scan.
    """
    with db.transaction(conn):
        if row["pending_copy"]:  # a better copy: the row describes it now (SPEC "Retroactive updates")
            values = {k: v for k, v in row["pending_copy"]["values"].items() if k not in ("size", "cover_state")}
            conn.execute(
                sql.SQL("UPDATE published_file SET {} WHERE id = %s").format(
                    sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in values)
                ),
                (*values.values(), row["id"]),
            )
        conn.execute(
            sql.SQL("""UPDATE published_file SET library_path = pending_path, size = pending_size,
                comicinfo_volume = pending_volume, volume_source = pending_volume_source,
                comicinfo_title = pending_title, unedited = pending_unedited,
                cover_state = coalesce(pending_cover, cover_state),
                {}, pending_error = NULL, pending_attempts = 0,
                indexed_since = %s, wait_charges = 0
            WHERE id = %s""").format(_set_null(PENDING)),
            (workflow.now(conn), row["id"]),
        )
        conn.execute("UPDATE series SET scan_due = 1 WHERE id = %s", (row["series_id"],))


def is_new(path: Path, row: db.Row) -> bool:
    """Return whether a same-name target already holds the rewritten file (a crash after the rename).

    It does when its size and its ComicInfo Volume are the queued ones, and its Title too when the rewrite changes it.
    """
    if not errors.exists(path) or path.stat().st_size != row["pending_size"]:
        return False
    xml = archive.read_comicinfo(path)
    if xml is None:
        return False
    fields = comicinfo.read_all(xml)
    if (row["pending_title"] or "") != (row["comicinfo_title"] or ""):
        want = row["pending_title"] or f"Chapter {names.fmt_num(float(row['comicinfo_number']))}"
        if fields.get("Title") != want:
            return False
    return (fields.get("Volume") or None) == row["pending_volume"]


def record_step(conn: db.Connection, file_id: int, step: int) -> int:
    """Record finish's act `step` done on the file (`pending_step`) and return it."""
    with db.transaction(conn):
        conn.execute("UPDATE published_file SET pending_step = %s WHERE id = %s", (step, file_id))
    return step


def _give_up_copy(conn: db.Connection, row: db.Row) -> None:
    """Give up a better copy's queued replacement (its staged file gone, or `rewrite drop`).

    The copy's handled record says dropped, not published; its download stays in retention. Inside the caller's
    transaction.
    """
    if row["pending_copy"] is None:
        return
    values = row["pending_copy"]["values"]
    conn.execute(
        """UPDATE handled_chapter SET outcome = 'dropped' WHERE id = (
            SELECT id FROM handled_chapter WHERE series_id = %s AND identity = %s AND content_digest IS NOT DISTINCT
                FROM %s AND outcome = 'published' ORDER BY id DESC LIMIT 1)""",
        (values["series_id"], values["identity"], row["pending_copy"]["digest"]),
    )


def _clear(conn: db.Connection, file_id: int) -> None:
    with db.transaction(conn):
        _give_up_copy(conn, db.one_row(conn, "SELECT * FROM published_file WHERE id = %s", (file_id,)))
        conn.execute(
            sql.SQL("""UPDATE published_file SET {}, pending_error = NULL,
                pending_attempts = 0 WHERE id = %s""").format(_set_null((*PENDING, *CARRY))),
            (file_id,),
        )


def drop(conn: db.Connection, file_id: int, limit: int) -> db.Row:
    """`rewrite drop FILE_ID`: clear a stuck rewrite that has not renamed its file in, with its progress carry.

    Stuck: its attempts at workflow.attempt_limit, or its progress wait failed. The caller removes the staged file it
    names; the next comparison of its series starts again. workflow.Refused otherwise: a rewrite export is still working
    on, or one past its rename (`rewrite retry` finishes it).
    """
    row = conn.execute("SELECT * FROM published_file WHERE id = %s", (file_id,)).fetchone()
    if row is None or not (
        (row["pending_path"] is not None and row["pending_attempts"] >= limit)
        or (row["indexed_since"] is not None and row["pending_error"] is not None)
    ):
        msg = f"published file {file_id} has no stuck rewrite"
        raise workflow.Refused(msg)
    if row["pending_path"] is not None and row["pending_step"] >= STEP_RENAMED:
        msg = f"published file {file_id}'s rewrite has renamed its file in: rewrite retry {file_id} finishes it"
        raise workflow.Refused(msg)
    with db.transaction(conn):
        _give_up_copy(conn, row)
        conn.execute(
            sql.SQL("""UPDATE published_file SET {},
                pending_error = NULL, pending_attempts = 0, indexed_since = NULL, wait_charges = 0
                WHERE id = %s""").format(_set_null((*PENDING, *CARRY))),
            (file_id,),
        )
        conn.execute("DELETE FROM series_naming WHERE series_id = %s", (row["series_id"],))
    return row


def retry(conn: db.Connection, file_id: int) -> db.Row:
    """`rewrite retry FILE_ID`: a failed rewrite tries again from its next step, or its progress wait starts again.

    workflow.Refused when the row holds no error.
    """
    row = conn.execute("SELECT * FROM published_file WHERE id = %s", (file_id,)).fetchone()
    if row is None or row["pending_error"] is None:
        msg = f"published file {file_id} has no failed rewrite"
        raise workflow.Refused(msg)
    with db.transaction(conn):
        conn.execute(
            """UPDATE published_file SET pending_error = NULL, pending_attempts = 0, wait_charges = 0,
                indexed_since = CASE WHEN indexed_since IS NULL THEN NULL ELSE %s END WHERE id = %s""",
            (workflow.now(conn), file_id),
        )
    return row


# --- the progress restore


def restore_all(ctx: workflow.Context) -> None:
    """Restore progress for every rewritten file Kavita has re-read and end its wait; then refresh the covers.

    Re-read: listed at its size, its chapter modified after the snapshot. The API key's user's progress is restored
    where Kavita lost it.
    """
    rows = ctx.conn.execute(
        """SELECT p.*, s.kavita_series_id AS kid FROM published_file p JOIN series s ON s.id = p.series_id
        WHERE p.indexed_since IS NOT NULL AND p.pending_error IS NULL AND s.kavita_series_id IS NOT NULL
        ORDER BY p.id"""
    ).fetchall()
    listed: dict[int, dict[Path, kavita_client.SeriesFile]] = {}
    for row in rows:
        if row["kid"] not in listed:
            try:
                listed[row["kid"]] = {f["path"]: f for f in ctx.kavita.series_files(row["kid"])}
            except workflow.Unreachable:
                raise
            except Exception as e:  # e.g. a series Kavita no longer holds: its rows wait; the run goes on
                log.warning("%s: Kavita series %s: files not read: %s", ctx.part, row["kid"], e)
                listed[row["kid"]] = {}
        seen = listed[row["kid"]].get(config.to_local(ctx.cfg, row["library_path"]))
        if seen is None or seen["bytes"] != row["size"] or not _later(seen["modified"], row["carry_kavita_modified"]):
            continue
        try:
            _restore(ctx, row, seen)
        except workflow.Unreachable:
            raise
        except Exception as e:  # this row's problem: logged, tried again next run; the run goes on
            log.warning("%s: %s: progress not restored: %s", ctx.part, row["library_path"], e)
            continue
        with db.transaction(ctx.conn):  # carry_cover stays: refresh_covers ends it
            ctx.conn.execute(
                sql.SQL("""UPDATE published_file SET {}, indexed_since = NULL,
                    wait_charges = 0 WHERE id = %s""").format(_set_null(PROGRESS)),
                (row["id"],),
            )
    refresh_covers(ctx)


def refresh_covers(ctx: workflow.Context) -> None:
    """Ask Kavita to regenerate the cover images of each series holding a re-read file whose cover a rewrite changed.

    A library scan keeps a re-read chapter's old one. 1 request per series per run; a request that fails is logged and
    asked again by the next run.
    """
    rows = ctx.conn.execute(
        """SELECT s.kavita_series_id AS kid, array_agg(p.id) AS ids FROM published_file p
        JOIN series s ON s.id = p.series_id
        WHERE p.carry_cover AND p.indexed_since IS NULL AND p.pending_path IS NULL AND s.kavita_series_id IS NOT NULL
        GROUP BY s.kavita_series_id ORDER BY s.kavita_series_id"""
    ).fetchall()
    for row in rows:
        try:
            ctx.kavita.refresh_covers(ctx.cfg["kavita"]["library_id"], row["kid"])
        except workflow.Unreachable:
            raise
        except Exception as e:  # this series' problem: asked again next run; the run goes on
            log.warning("%s: Kavita series %s: cover images not refreshed: %s", ctx.part, row["kid"], e)
            continue
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE published_file SET carry_cover = NULL WHERE id = ANY(%s)", (list(row["ids"]),))
        log.info("%s: Kavita series %s: cover images refreshed (%d file(s))", ctx.part, row["kid"], len(row["ids"]))


def _later(now: str, before: str | None) -> bool:
    return before is None or datetime.fromisoformat(now) > datetime.fromisoformat(before)


def _restore(ctx: workflow.Context, row: db.Row, seen: kavita_client.SeriesFile) -> None:
    """Write the carried progress, only when higher than what Kavita now holds.

    Unread stays unread; fully read is marked read; else the pages read, shifted by an added or dropped cover, at least
    1, at most the new page count (written lower too when Kavita still holds exactly the snapshot's page: a same-name
    file keeps it unshifted).
    """
    read, old_pages = row["carry_pages_read"] or 0, row["carry_old_pages"] or 0
    if read <= 0:
        return
    if read >= old_pages:
        if seen["pages_read"] < seen["pages"]:
            ctx.kavita.mark_read(row["kid"], [seen["chapter_id"]])
            log.info("%s: %s: marked read again", ctx.part, row["library_path"])
        return
    want = min(max(read + (row["carry_page_shift"] or 0), 1), row["carry_new_pages"] or seen["pages"])
    if want > seen["pages_read"] or (want != read and seen["pages_read"] == read):
        ctx.kavita.save_progress(row["kid"], seen["volume_id"], seen["chapter_id"], want)
        log.info("%s: %s: progress %d page(s) restored", ctx.part, row["library_path"], want)


def charge_waits(ctx: workflow.Context, waited_since: str, at: str) -> int:
    """Add 1 charge to each rewritten file still not re-read whose wait began at or before waited_since.

    A charge is a repeated library scan request; at workflow.attempt_limit charges its row records the error. Inside
    the caller's transaction; returns how many were charged.
    """
    limit = ctx.cfg["workflow"]["attempt_limit"]
    rows = ctx.conn.execute(
        """UPDATE published_file SET wait_charges = wait_charges + 1
        WHERE indexed_since IS NOT NULL AND pending_error IS NULL AND indexed_since <= %s RETURNING id, wait_charges""",
        (waited_since,),
    ).fetchall()
    for row in rows:
        if row["wait_charges"] >= limit:
            ctx.conn.execute(
                "UPDATE published_file SET pending_error = %s WHERE id = %s",
                (f"not re-read by Kavita after {limit} library scan requests (at {at})", row["id"]),
            )
    return len(rows)


def waiting(conn: db.Connection) -> int:
    """How many rewritten files wait for Kavita to re-read them."""
    return db.one_row(
        conn, "SELECT count(*) FROM published_file WHERE indexed_since IS NOT NULL AND pending_error IS NULL"
    )[0]


# --- naming diff


type Diff = tuple[
    list[tuple[str, str | None, str | None, str, str | None, str | None]],
    list[str],
    bool,
    list[tuple[str, str | None, str | None]],
    list[tuple[str, str | None, float | None]],
]
"""diff's (queued, wants, unknown, apart, edits)."""


def diff(ctx: workflow.Context, series: db.Row) -> Diff:
    """Return what the comparison of 1 series would do and the differences it leaves; reads no library file.

    (rewrites the comparison would queue: [(path, old volume, new volume, new path, cover, new title or None)], cover
    images it would want: [volume], whether some file's cover is not known yet (covers then left out), differences not
    applied: [(path, stored volume, sources' volume)], the series' hand edits: [(chapter, title, volume)]). VolumeError
    for a malformed override file.
    """
    rows, overrides, mangadex, platform = chapter_names.inputs(ctx, series)
    basis = series["naming_basis"]
    todo, wants, unknown = _plan(ctx, series)
    files = {r["id"]: r for r in ctx.conn.execute("SELECT * FROM published_file WHERE series_id = %s", (series["id"],))}
    busy = {
        r[0]
        for r in ctx.conn.execute(
            sql.SQL("SELECT p.id FROM published_file p WHERE p.series_id = %s AND {}").format(
                BUSY.format(p=sql.Identifier("p"))
            ),
            (series["id"],),
        )
    }
    # what the comparison would queue now: confirmed files with nothing open, queued or awaited
    queued = [
        (
            files[i]["library_path"],
            files[i]["comicinfo_volume"],
            None if v is None else names.fmt_num(v),
            _target(files[i], v, title),
            _cover_text(cover).removeprefix(", ") or None,
            title if title != (files[i]["comicinfo_title"] or "") else None,
        )
        for i, (v, _, cover, title, _) in todo.items()
        if files[i]["state"] == "confirmed"
        and i not in busy
        and not (
            files[i]["pending_path"]
            or files[i]["indexed_since"]
            or files[i]["pending_error"]
            or files[i]["carry_cover"]
        )
    ]
    edits = chapter_names.edits(ctx.conn, series["id"])
    held = rewrite_plan.held(rows, edits, {i: f["unedited"] for i, f in files.items()})
    apart = [
        (files[i]["library_path"], names.fmt_num(a), None if b is None else names.fmt_num(b))
        for i, (a, b) in sorted(rewrite_plan.differences(basis, rows, overrides, mangadex, platform).items())
        if i not in held
    ]
    order = sorted(edits, key=lambda k: (k[0], k[1] or 0, k[2]))
    listed = [(naming.edit_token(*key), edits[key].title, edits[key].volume) for key in order]
    return queued, sorted(wants), unknown, apart, listed
