"""Retroactive rewrites of published files (SPEC "Retroactive updates"): `export`'s comparison, a rewrite chapter's
comicinfo and publish (which only queues the rewrite on its file's row; its convert, for a cover, is
rewrite_covers'), `finish` (the file acts, recorded step by step
on the row), and the progress restore once Kavita has re-read the file.

A rewrite never re-encodes pages, and the library never lacks the chapter's file. Only `export` (1 at a time
fleet-wide: its run lock) acts on a queued row.
"""

import hashlib
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from manga_pipeline import (
    archive,
    baseline,
    chapter_names,
    comicinfo,
    config,
    db,
    handled,
    names,
    retention,
    rewrite_covers,
    volumes,
    workflow,
)
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
OPEN_REWRITES = "published_file_id IS NOT NULL AND status NOT IN ('done', 'needs_human')"
# A published file busy with a change, {p} its alias: an open chapter names it (a rewrite or a copy), a queued
# rewrite, a wait for Kavita, a cover refresh due, or a failed rewrite. No new rewrite or copy takes it meanwhile.
BUSY = """({p}.pending_path IS NOT NULL OR {p}.indexed_since IS NOT NULL OR {p}.pending_error IS NOT NULL
    OR {p}.carry_cover IS TRUE OR EXISTS (
        SELECT 1 FROM chapter b WHERE b.published_file_id = {p}.id AND b.status <> 'done'))"""


# --- the comparison


def compare(ctx):
    """Compare every in-scope ok series with a Kavita id and its baseline taken whose naming inputs changed: queue a
    rewrite chapter for each published file whose volume or cover should change, while fewer than export.rewrites_open
    are open, and record the cover images wanted. A series compared whole stores the hash it was compared with; one cut
    short, or whose files' covers could not be read, is compared again next run."""
    conn, cap = ctx.conn, ctx.cfg["export"]["rewrites_open"]
    candidates = conn.execute(
        f"""SELECT s.* FROM series s WHERE s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND {workflow.SCOPE}
        ORDER BY s.id""",
        workflow.scope_params(ctx.cfg),
    ).fetchall()
    for series in candidates:
        if baseline.needed(conn, series):
            continue
        room = cap - conn.execute(f"SELECT count(*) FROM chapter WHERE {OPEN_REWRITES}").fetchone()[0]
        if room <= 0:
            log.info("%s: %d rewrites open (export.rewrites_open): the comparison waits", ctx.part, cap)
            return
        if not rewrite_covers.fill_states(ctx, series):  # outside the lock: it may read many files once
            continue
        # 1 transaction (the database-wide write lock): the hash and the comparison read the same inputs once.
        # ponytail: it reads the series' open downloads' ComicInfo.xml under the lock, only when the hash changed
        with db.transaction(conn):
            try:
                digest = _hash(ctx, series)
                if digest == _stored_hash(conn, series["id"]):
                    continue
                todo, wants, _ = _plan(ctx, series)
            except volumes.OverrideError as e:  # the series' own file: a person must fix it, as at comicinfo
                workflow.needs_human(conn, "series", series["id"], f"{e} (comparing its published files)")
                log.warning("%s: series %s (%s): %s", ctx.part, series["id"], series["name"], e)
                continue
            except volumes.VolumeError as e:  # a download's volume: its chapter is held at comicinfo; compared again
                log.warning("%s: series %s (%s): not compared: %s", ctx.part, series["id"], series["name"], e)
                continue
            for v in sorted(wants):
                conn.execute(
                    "INSERT INTO volume_cover_wanted (series_id, volume) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (series["id"], int(names.to_volume(v))),
                )
            made = 0
            for file_id, change in todo.items():
                if made >= room:
                    break
                made += _queue_chapter(ctx, series, file_id, *change)
            if made >= room and len(todo) > room:
                continue  # cut short: compared again next run
            conn.execute(
                """INSERT INTO series_naming (series_id, naming_hash, compared_at) VALUES (%s, %s, %s)
                ON CONFLICT (series_id) DO UPDATE SET naming_hash = excluded.naming_hash,
                    compared_at = excluded.compared_at""",
                (series["id"], digest, workflow.now(conn)),
            )
        if made:
            log.info("%s: series %s (%s): %d rewrite(s) queued", ctx.part, series["id"], series["name"], made)


def _plan(ctx, series):
    """(todo, wants, unknown) of 1 series. todo: {published file id: (volume, volume source, cover, title, unedited)} of
    the files whose volume, cover or (by a hand edit) title should change, in id order (cover: None, ("insert",
    volume, its series_volume_cover row) or ("drop",); unedited: what the file keeps of its sources' values afterwards,
    rewrite_plan.edit_changes); wants: the volumes whose cover image intake should fetch (that file's cover is then
    left for later); unknown: some file's cover is not known yet (covers are then left out). VolumeError as
    naming.resolve raises it."""
    conn, sid = ctx.conn, series["id"]
    rows, overrides, mangadex, platform = chapter_names.inputs(ctx, series)
    basis = series["naming_basis"]
    files = {r["id"]: r for r in conn.execute("SELECT * FROM published_file WHERE series_id = %s", (sid,))}
    edits, unedited = chapter_names.edits(conn, sid), {i: f["unedited"] for i, f in files.items()}
    held = rewrite_plan.held(rows, edits, unedited)
    edited = rewrite_plan.edit_changes(rows, edits, unedited)
    moved = {i: v for i, v in rewrite_plan.changes(basis, rows, overrides, mangadex, platform).items() if i not in held}
    moved.update({i: (v, source) for i, (v, source, _, _) in edited.items() if i in held})
    states = {i: f["cover_state"] for i, f in files.items()}
    unknown = any(s is None for s in states.values())
    cover, wants = {}, set()
    if not unknown:
        taken = {
            r[0]
            for r in conn.execute(
                """SELECT v.volume FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id
                WHERE c.series_id = %s AND c.status <> 'done'""",
                (sid,),
            )
        }
        images = {
            names.fmt_num(r["volume"]): r
            for r in conn.execute("SELECT volume, path, sha256 FROM series_volume_cover WHERE series_id = %s", (sid,))
        }
        given_up = {
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
        for fid, (action, v) in rewrite_plan.cover_changes(
            basis, rows, overrides, mangadex, moved, states, taken, shas
        ).items():
            if action == "drop":
                cover[fid] = ("drop",)
            elif v in images:
                cover[fid] = ("insert", v, images[v])
            else:
                if (
                    v not in given_up
                    and names.to_volume(v) == int(names.to_volume(v))
                    and records.get("manga")
                    and cover_images.volume_choice(records["manga"], records.get("covers") or [], names.to_volume(v))
                ):
                    wants.add(v)
                if rewrite_covers.held(states[fid]) is not None:  # another volume's cover: dropped meanwhile
                    cover[fid] = ("drop",)
    todo = {}
    for fid in sorted(set(moved) | set(cover) | set(edited)):
        current = (names.to_volume(files[fid]["comicinfo_volume"]), files[fid]["volume_source"])
        title, kept = edited.get(fid, (None, None, files[fid]["comicinfo_title"] or "", files[fid]["unedited"]))[2:]
        todo[fid] = (*moved.get(fid, current), cover.get(fid), title, kept)
    return todo, wants, unknown


def _stored_hash(conn, series_id):
    row = conn.execute("SELECT naming_hash FROM series_naming WHERE series_id = %s", (series_id,)).fetchone()
    return row[0] if row else None


def _hash(ctx, series):
    """The series' naming inputs: its basis, override file, hand edits, stored MangaDex aggregate, cover list and
    platform list, its stored cover images, the naming code, its published files' rows, its open downloads and the
    covers they took, and its open rewrites. VolumeError for a malformed override file."""
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
    inputs = [series["naming_basis"], overrides, [tuple(r) for r in fetched], CODE]
    inputs += [[tuple(r) for r in files], [tuple(r) for r in downloads], [tuple(r) for r in rewrites]]
    edits = conn.execute(
        "SELECT season, side, number, title, volume FROM naming_edit WHERE series_id = %s ORDER BY side, season,"
        " number",
        (sid,),
    ).fetchall()
    inputs += [[tuple(r) for r in images], [tuple(r) for r in taken], [tuple(r) for r in edits]]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def _queue_chapter(ctx, series, file_id, volume, source, cover, title, unedited):
    """1 rewrite chapter for a confirmed published file with no open chapter and nothing queued or awaited, at convert
    when its cover changes (an insert's cover handed over as a volume_cover row), else at comicinfo; it carries the
    file's title and unedited values afterwards (title "" = untitled). Returns 1 when made, else 0."""
    conn = ctx.conn
    row = conn.execute(
        f"SELECT * FROM published_file p WHERE p.id = %s AND p.state = 'confirmed' AND NOT {BUSY.format(p='p')}",
        (file_id,),
    ).fetchone()
    if row is None:
        return 0
    target = _target(ctx, series, row, volume, title)
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
            "comicinfo" if cover is None else "convert",
            file_id,
            target,
            series["comicinfo_series"],
            None if volume is None else names.fmt_num(volume),
            row["comicinfo_number"],
            title or None,
            row["comicinfo_date"],
            row["kind"],
            source,
            cover == ("drop",),
            None if unedited is None else json.dumps(unedited),
            now,
            now,
        ),  # fmt: skip
    ).fetchone()
    if made is None:
        return 0
    if cover is not None and cover[0] == "insert":
        conn.execute(
            "INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, %s, %s, %s)",
            (made["id"], cover[2]["path"], cover[2]["sha256"], cover[1]),
        )
    log.info(
        "%s: %s: volume %s -> %s (%s)%s%s%s", ctx.part, row["library_path"], row["comicinfo_volume"],
        None if volume is None else names.fmt_num(volume), source,
        f", title {title!r}" if title != (row["comicinfo_title"] or "") else "",
        f", renamed {target}" if target != row["library_path"] else "", _cover_text(cover),
    )  # fmt: skip
    return 1


def _cover_text(cover):
    return (
        "" if cover is None else f", insert volume {cover[1]}'s cover" if cover[0] == "insert" else ", drop its cover"
    )


def _target(ctx, series, row, volume, title):
    """The stored path the rewritten file takes (D7.8): a pipeline file named for its new volume and title; a bulk file
    keeps its name."""
    if row["origin"] != "pipeline":
        return row["library_path"]
    name = names.file_name(float(row["comicinfo_number"]), title, volume)
    return str(PurePosixPath(row["library_path"]).parent / name)


# --- the rewrite chapter's stages


def comicinfo_run(ctx, chapter):
    """Copy the library file to staging with a new ComicInfo.xml (the series-level fields now, the new volume, the
    old file's own fields and pages); every other member copied byte for byte."""
    conn = ctx.conn
    series = conn.execute("SELECT * FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()
    file = conn.execute("SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],)).fetchone()
    converted = chapter["convert_result"] and json.loads(chapter["convert_result"])  # the cover changed at convert
    source = config.to_local(ctx.cfg, file["library_path"])  # unchanged until finish: a rerun reads the same
    old = archive.read_comicinfo(source)
    if old is None:
        return workflow.NeedsHuman(f"{source} holds no ComicInfo.xml")
    kept = {k: v for k, v in comicinfo.read_all(old).items() if k not in SERIES_LEVEL and comicinfo.field_ok(k, v)}
    try:
        group = volumes.series_group(config.abs_path(ctx.cfg["paths"]["naming_overrides"]), series["rules_key"])
    except volumes.VolumeError as e:
        return workflow.NeedsHuman(str(e))
    fields = comicinfo_stage.series_fields(ctx, series, {"source_url": None, "source": None}, group)
    old_links = (comicinfo.read_all(old).get("Web") or "").split()
    links = (fields["Web"] or "").split()
    fields.update(kept)
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

    def record(c):
        c.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s WHERE id = %s",
            (config.to_stored(ctx.cfg, staged), size, chapter["id"]),
        )

    return workflow.Done(record)


def page_count(cbz):
    """How many pages a CBZ holds (its member list only)."""
    with zipfile.ZipFile(cbz) as z:
        return len(archive.archive_pages(z.namelist()))


def copy_replacing(source, dest, name, data):
    """dest: source's members in order, member `name` (compared without case) replaced by data (stored last when the
    source had none); every other member's bytes copied unchanged, checked by CRC against the source. Returns dest's
    size."""
    with zipfile.ZipFile(source) as z:
        members = [(i.filename, z.read(i), i.CRC) for i in z.infolist() if i.filename.lower() != name.lower()]
    size = archive.write(dest, [*((n, d) for n, d, _ in members), (name, data)])
    with zipfile.ZipFile(dest) as z:
        got = {i.filename: i.CRC for i in z.infolist()}
    if any(got.get(n) != crc for n, _, crc in members):
        dest.unlink()
        raise RuntimeError(f"{dest}: a copied member's CRC differs from {source}'s")
    return size


def publish_run(ctx, chapter):
    """Queue the rewrite, or a better copy's replacement, on its file's row with the API key's user's progress (SPEC
    "Retroactive updates"); the chapter is then done. A copy also queues its values (publish.row_values), puts its
    download aside the retention way and is recorded as a handled chapter `published`. A publish run again finds its
    rewrite queued or finished and is done."""
    conn = ctx.conn
    file = conn.execute("SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],)).fetchone()
    if file["pending_path"] == chapter["library_path"] or file["indexed_since"] is not None:
        return workflow.Done()  # queued, or finished, by an earlier run of this stage
    series = conn.execute("SELECT * FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()
    old = config.to_local(ctx.cfg, file["library_path"])
    seen = [f for f in ctx.kavita.series_files(series["kavita_series_id"]) if f["path"] == old]
    if not seen:  # a confirmed file Kavita lost: tried again by a later run, its attempts counted
        raise RuntimeError(f"Kavita does not list {old} in series {series['kavita_series_id']}")
    staged = config.to_local(ctx.cfg, chapter["staged_path"])
    target = config.to_local(ctx.cfg, chapter["library_path"])
    new_pages = page_count(staged)
    converted = chapter["convert_result"] and json.loads(chapter["convert_result"])
    clash = conn.execute(
        "SELECT id FROM published_file WHERE library_path = %s AND id <> %s", (chapter["library_path"], file["id"])
    ).fetchone()
    if clash or (target != old and workflow.exists(target)):
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

    def record(c):
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
                cover,
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
            ),  # fmt: skip
        )
        if copy:
            handled.record(c, chapter["series_id"], chapter["identity"], "published", chapter["content_digest"])

    return workflow.Done(record)


def _json(value):
    return None if value is None else json.dumps(value)


# --- finishing queued rewrites


def copy_shift(read, old_pages, new_pages):
    """The page shift a better copy's progress takes: the pages read scaled to its page count, as a shift from the
    read page (the restore clamps it)."""
    return round(read * new_pages / old_pages) - read if old_pages else 0


def finish_all(ctx):
    """Finish every queued rewrite that has failed fewer than workflow.attempt_limit times; a row whose act fails
    records its error and 1 attempt and resumes at its next step on the next run; the others go on (a share error
    ends the run). At the limit it waits for `rewrite retry` (or, before its rename, `rewrite drop`)."""
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


def finish(ctx, row):
    """The file acts of 1 queued rewrite, each recorded on the row once done (pending_step), so a retry starts at the
    next: 1 the staged file's time set to now; 2 the old file hard-linked into retention; 3 the staged file renamed over
    the target; 4 a renamed file's old name removed; 5 the row takes the new path, size and volume."""
    cfg, conn = ctx.cfg, ctx.conn
    staged = config.to_local(cfg, row["pending_staged"])
    target = config.to_local(cfg, row["pending_path"])
    old = config.to_local(cfg, row["library_path"])
    step = row["pending_step"]
    if step < 3 and not workflow.exists(staged):
        if workflow.exists(target) and target.stat().st_size == row["pending_size"] and target != old:
            step = _record(conn, row["id"], 3)  # renamed in before a crash recorded it
        elif target == old and _is_new(target, row):
            step = _record(conn, row["id"], 3)
        else:
            _clear(conn, row["id"])
            log.warning("%s: rewrite of %s dropped: its staged file is gone", ctx.part, row["library_path"])
            return
    if step < 1:
        os.utime(staged)  # the rename keeps it: Kavita skips a file it judges unchanged by its date
        step = _record(conn, row["id"], 1)
    if step < 2:
        retention.put_library(cfg, old)
        step = _record(conn, row["id"], 2)
    if step < 3:
        if target != old and workflow.exists(target):
            raise FileExistsError(f"{target} is already in the library; a library file is never overwritten")
        os.replace(staged, target)
        step = _record(conn, row["id"], 3)
    if step < 4:
        if target != old and workflow.exists(old):
            old.unlink()
        step = _record(conn, row["id"], 4)
    with db.transaction(conn):
        if row["pending_copy"]:  # a better copy: the row describes it now (SPEC "Retroactive updates")
            values = {k: v for k, v in row["pending_copy"]["values"].items() if k not in ("size", "cover_state")}
            conn.execute(
                f"UPDATE published_file SET {', '.join(f'{c} = %s' for c in values)} WHERE id = %s",
                (*values.values(), row["id"]),
            )
        conn.execute(
            f"""UPDATE published_file SET library_path = pending_path, size = pending_size,
                comicinfo_volume = pending_volume, volume_source = pending_volume_source,
                comicinfo_title = pending_title, unedited = pending_unedited,
                cover_state = coalesce(pending_cover, cover_state),
                {", ".join(f"{c} = NULL" for c in PENDING)}, pending_error = NULL, pending_attempts = 0,
                indexed_since = %s, wait_charges = 0
            WHERE id = %s""",
            (workflow.now(conn), row["id"]),
        )
        conn.execute("UPDATE series SET scan_due = 1 WHERE id = %s", (row["series_id"],))
    log.info("%s: rewrote %s as %s", ctx.part, row["library_path"], row["pending_path"])


def _is_new(path, row):
    """Whether a same-name target already holds the rewritten file (a crash after the rename): its size and its
    ComicInfo Volume are the queued ones, and its Title too when the rewrite changes it."""
    if not workflow.exists(path) or path.stat().st_size != row["pending_size"]:
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


def _record(conn, file_id, step):
    with db.transaction(conn):
        conn.execute("UPDATE published_file SET pending_step = %s WHERE id = %s", (step, file_id))
    return step


def _give_up_copy(conn, row):
    """A better copy's queued replacement given up (its staged file gone, or `rewrite drop`): the copy's handled record
    says dropped, not published; its download stays in retention. Inside the caller's transaction."""
    if row["pending_copy"] is None:
        return
    values = row["pending_copy"]["values"]
    conn.execute(
        """UPDATE handled_chapter SET outcome = 'dropped' WHERE id = (
            SELECT id FROM handled_chapter WHERE series_id = %s AND identity = %s AND content_digest IS NOT DISTINCT
                FROM %s AND outcome = 'published' ORDER BY id DESC LIMIT 1)""",
        (values["series_id"], values["identity"], row["pending_copy"]["digest"]),
    )


def _clear(conn, file_id):
    with db.transaction(conn):
        _give_up_copy(conn, conn.execute("SELECT * FROM published_file WHERE id = %s", (file_id,)).fetchone())
        conn.execute(
            f"""UPDATE published_file SET {", ".join(f"{c} = NULL" for c in (*PENDING, *CARRY))}, pending_error = NULL,
                pending_attempts = 0 WHERE id = %s""",
            (file_id,),
        )


def drop(conn, file_id, limit):
    """`rewrite drop FILE_ID`: clear a stuck rewrite (its attempts at workflow.attempt_limit, or its progress wait
    failed) that has not renamed its file in, with its progress carry; the caller removes the staged file it names;
    the next comparison of its series starts again. workflow.Refused otherwise: a rewrite export is still working on,
    or one past its rename (`rewrite retry` finishes it)."""
    row = conn.execute("SELECT * FROM published_file WHERE id = %s", (file_id,)).fetchone()
    stuck = row is not None and (
        (row["pending_path"] is not None and row["pending_attempts"] >= limit)
        or (row["indexed_since"] is not None and row["pending_error"] is not None)
    )
    if not stuck:
        raise workflow.Refused(f"published file {file_id} has no stuck rewrite")
    if row["pending_path"] is not None and row["pending_step"] >= 3:
        raise workflow.Refused(
            f"published file {file_id}'s rewrite has renamed its file in: rewrite retry {file_id} finishes it"
        )
    with db.transaction(conn):
        _give_up_copy(conn, row)
        conn.execute(
            f"""UPDATE published_file SET {", ".join(f"{c} = NULL" for c in (*PENDING, *CARRY))},
                pending_error = NULL, pending_attempts = 0, indexed_since = NULL, wait_charges = 0 WHERE id = %s""",
            (file_id,),
        )
        conn.execute("DELETE FROM series_naming WHERE series_id = %s", (row["series_id"],))
    return row


def retry(conn, file_id):
    """`rewrite retry FILE_ID`: a failed rewrite tries again from its next step, or its progress wait starts again.
    workflow.Refused when the row holds no error."""
    row = conn.execute("SELECT * FROM published_file WHERE id = %s", (file_id,)).fetchone()
    if row is None or row["pending_error"] is None:
        raise workflow.Refused(f"published file {file_id} has no failed rewrite")
    with db.transaction(conn):
        conn.execute(
            """UPDATE published_file SET pending_error = NULL, pending_attempts = 0, wait_charges = 0,
                indexed_since = CASE WHEN indexed_since IS NULL THEN NULL ELSE %s END WHERE id = %s""",
            (workflow.now(conn), file_id),
        )
    return row


# --- the progress restore


def restore_all(ctx):
    """For every rewritten file Kavita has re-read (listed at its size, its chapter modified after the snapshot),
    restore the API key's user's progress where Kavita lost it, and end the wait; then refresh the covers."""
    rows = ctx.conn.execute(
        """SELECT p.*, s.kavita_series_id AS kid FROM published_file p JOIN series s ON s.id = p.series_id
        WHERE p.indexed_since IS NOT NULL AND p.pending_error IS NULL AND s.kavita_series_id IS NOT NULL
        ORDER BY p.id"""
    ).fetchall()
    listed = {}
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
                f"""UPDATE published_file SET {", ".join(f"{c} = NULL" for c in PROGRESS)}, indexed_since = NULL,
                    wait_charges = 0 WHERE id = %s""",
                (row["id"],),
            )
    refresh_covers(ctx)


def refresh_covers(ctx):
    """Ask Kavita to regenerate the cover images of each series holding a re-read file whose cover a rewrite changed (a
    library scan keeps a re-read chapter's old one): 1 request per series per run; a request that fails is logged and
    asked again by the next run."""
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


def _later(now, before):
    return before is None or datetime.fromisoformat(now) > datetime.fromisoformat(before)


def _restore(ctx, row, seen):
    """The carried progress, written only when higher than what Kavita now holds: unread stays unread; fully read is
    marked read; else the pages read, shifted by an added or dropped cover, at least 1, at most the new page count
    (written lower too when Kavita still holds exactly the snapshot's page: a same-name file keeps it unshifted)."""
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


def charge_waits(ctx, waited_since, at):
    """1 charge for each rewritten file still not re-read whose wait began at or before waited_since (a repeated
    library scan request); at workflow.attempt_limit charges its row records the error. Inside the caller's
    transaction; returns how many were charged."""
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


def waiting(conn):
    """How many rewritten files wait for Kavita to re-read them."""
    return conn.execute(
        "SELECT count(*) FROM published_file WHERE indexed_since IS NOT NULL AND pending_error IS NULL"
    ).fetchone()[0]


# --- naming diff


def diff(ctx, series):
    """(rewrites the comparison would queue: [(path, old volume, new volume, new path, cover, new title or None)],
    cover images it would want: [volume], whether some file's cover is not known yet (covers then left out),
    differences not applied: [(path, stored volume, sources' volume)], the series' hand edits: [(chapter, title,
    volume)]) of 1 series; reads no library file. VolumeError for a malformed override file."""
    rows, overrides, mangadex, platform = chapter_names.inputs(ctx, series)
    basis = series["naming_basis"]
    todo, wants, unknown = _plan(ctx, series)
    files = {r["id"]: r for r in ctx.conn.execute("SELECT * FROM published_file WHERE series_id = %s", (series["id"],))}
    busy = {
        r[0]
        for r in ctx.conn.execute(
            f"SELECT p.id FROM published_file p WHERE p.series_id = %s AND {BUSY.format(p='p')}", (series["id"],)
        )
    }
    # what the comparison would queue now: confirmed files with nothing open, queued or awaited
    queued = [
        (
            files[i]["library_path"],
            files[i]["comicinfo_volume"],
            None if v is None else names.fmt_num(v),
            _target(ctx, series, files[i], v, title),
            _cover_text(cover).removeprefix(", ") or None,
            title if title != (files[i]["comicinfo_title"] or "") else None,
        )  # fmt: skip
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
