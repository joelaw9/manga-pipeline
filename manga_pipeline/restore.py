"""`rewrite restore` (SPEC "Retroactive updates"): a file's previous version put back, queued as a replace.

A download chapter's replace (a conversion, a better copy, a rebuild) keeps the replaced version in retention
(`previous`) with its row (`previous_values`). A restore queues that version on the file's row as a better copy's
replace is queued, under its name then and with its row then, the reader's progress carried; `rewrite.finish` links
it in and `rewrite._settle_restore` writes the row. The command writes nothing on the share.
"""

import json
import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from manga_pipeline import archive, config, db, errors, rewrite, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.db import sql
from manga_pipeline.stages import publish

log = logging.getLogger(__name__)


type Listings = dict[int, dict[Path, kavita_client.SeriesFile] | str]
"""Kavita's files per Kavita series id, read once per run; or why Kavita refused the series (http.HttpError)."""


class NotRestoredError(Exception):
    """A file `rewrite restore` named was not queued (exit 1); each is listed with why."""


def run(ctx: workflow.Context, file_ids: Sequence[int]) -> tuple[int, list[str]]:
    """Queue a restore of each named file; return (queued, [listed file: why]).

    workflow.Refused at export.retention_days 0 (the version a restore undoes would not be kept). Kavita's files are
    read once per series (a series Kavita refuses lists its files); Unreachable stops the command, the files queued so
    far staying queued.
    """
    if ctx.cfg["export"]["retention_days"] == 0:
        msg = "export.retention_days is 0: the version a restore undoes would not be kept"
        raise workflow.Refused(msg)
    queued, listed, seen = 0, list[str](), set[int]()
    listings: Listings = {}
    for file_id in file_ids:
        if file_id in seen:
            listed.append(f"file {file_id}: named twice")
            continue
        seen.add(file_id)
        why = _restore_one(ctx, file_id, listings)
        if why is None:
            queued += 1
        else:
            listed.append(f"file {file_id}: {why}")
    if queued:
        log.info("restores: %d queued", queued)
    return queued, listed


def _restore_one(ctx: workflow.Context, file_id: int, listings: Listings) -> str | None:
    """Queue 1 file's restore; return None when queued, else why not.

    The database checks first; then, outside any transaction, the share and Kavita reads; then 1 transaction checks the
    file again and writes the queued replace (rewrite.publish_run's columns).
    """
    cfg, conn = ctx.cfg, ctx.conn
    first = _read(cfg, conn, file_id)
    why = _why(conn, first)
    if first is None or why is not None:
        return why
    read = _previous_read(cfg, first)
    if isinstance(read, str):
        return read
    size, new_pages = read
    seen = _listed(ctx, listings, first)
    if isinstance(seen, str):
        return seen
    before = first["previous_values"]["before"]
    staged = config.abs_path(cfg["paths"]["staging"]) / f"restore-{file_id}.cbz"  # named here; finish makes it
    with db.transaction(conn):
        why = _why(conn, _read(cfg, conn, file_id), first)
        if why is not None:
            return why
        conn.execute(
            """UPDATE published_file SET pending_path = %s, pending_staged = %s, pending_size = %s,
                pending_volume = %s, pending_volume_source = %s, pending_cover = %s, pending_step = 0,
                pending_attempts = 0, carry_pages_read = %s, carry_old_pages = %s, carry_new_pages = %s,
                carry_page_shift = %s, carry_kavita_modified = %s, carry_cover = TRUE, pending_copy = %s,
                pending_title = %s, pending_unedited = %s
            WHERE id = %s""",
            (
                _target(first),
                config.to_stored(cfg, staged),
                size,
                before["comicinfo_volume"],
                before["volume_source"],
                before["cover_state"],
                seen["pages_read"],
                seen["pages"],
                new_pages,
                rewrite.copy_shift(seen["pages_read"], seen["pages"], new_pages),
                seen["modified"],
                json.dumps({"values": before, "previous": first["previous"], "restore": True}),
                before["comicinfo_title"],
                None if before["unedited"] is None else json.dumps(before["unedited"]),
                file_id,
            ),
        )
    log.info("restore: %s queued as %s from %s", first["library_path"], _target(first), first["previous"])
    return None


def _listed(ctx: workflow.Context, listings: Listings, file: db.Row) -> kavita_client.SeriesFile | str:
    """Return Kavita's entry of the file, else why there is none. Unreachable stops the command."""
    kid: int = file["kid"]
    if kid not in listings:
        try:
            listings[kid] = {f["path"]: f for f in ctx.kavita.series_files(kid)}
        except http.HttpError as e:  # this series' problem, read once: its files are listed, the rest go on
            listings[kid] = f"Kavita refused its series {kid}: {e}"
    listing = listings[kid]
    if isinstance(listing, str):
        return listing
    return listing.get(config.to_local(ctx.cfg, file["library_path"])) or "Kavita does not list it"


def _previous_read(cfg: config.Config, file: db.Row) -> tuple[int, int] | str:
    """Return (size, page count) of a file's previous version, else why it cannot be restored (a share read).

    Gone from retention or not a readable archive, its name taken on disk by another file, or the library file it
    replaces missing (finish's retention act would fail on it).
    """
    previous: str = file["previous"]
    source, target = config.to_local(cfg, previous), _target(file)
    with workflow.share_errors():
        if not errors.exists(source):
            return f"its previous version {previous} is no longer in retention"
        if not errors.exists(config.to_local(cfg, file["library_path"])):
            return f"its library file {file['library_path']} is missing"
        if target != file["library_path"] and errors.exists(config.to_local(cfg, target)):
            return f"its previous name {target} is taken"
        try:
            pages = rewrite.page_count(source)
        except archive.UNREADABLE as e:
            return f"its previous version {previous} cannot be read: {e}"
        return source.stat().st_size, pages


def _read(cfg: config.Config, conn: db.Connection, file_id: int) -> db.Row | None:
    """Return a published file's row with its series' status, scope, Kavita id and BUSY; None when there is none."""
    return conn.execute(
        sql.SQL("""SELECT f.*, s.status AS series_status, s.kavita_series_id AS kid, {scope} AS in_scope,
            {busy} AS busy
        FROM published_file f JOIN series s ON s.id = f.series_id WHERE f.id = %(file)s""").format(
            scope=workflow.SCOPE, busy=rewrite.BUSY.format(p=sql.Identifier("f"))
        ),
        {**workflow.scope_params(cfg), "file": file_id},
    ).fetchone()


def _target(file: db.Row) -> str:
    """Return the stored path a restore takes: its previous version's name, in the file's folder now."""
    name = PurePosixPath(file["previous_values"]["before"]["library_path"]).name
    return str(PurePosixPath(file["library_path"]).parent / name)


def _why(conn: db.Connection, file: db.Row | None, read: db.Row | None = None) -> str | None:
    """Return why a file (_read's row) cannot be restored now, from the database alone; None when it can.

    read: the row as the command first read it, before its share and Kavita reads (None: this is that read).
    """
    if file is None:
        return "no such published file"
    return _file_why(file, read) or _series_why(conn, file)


def _file_why(file: db.Row, read: db.Row | None) -> str | None:
    """Return why the file itself cannot be restored now, else None."""
    if file["state"] != "confirmed":
        return "not confirmed yet"
    if file["busy"]:
        return "busy"
    recorded = file["previous_values"]
    if file["previous"] is None or recorded is None or recorded["previous"] != file["previous"]:
        return "no previous version recorded with its row (replaced before restores were recorded)"
    if rewrite.compared(file) != recorded["after"]:
        return "changed since its replace (a rewrite, a join or series rename)"
    if read is not None and (file["previous"], file["library_path"]) != (read["previous"], read["library_path"]):
        return "changed since it was read: name it again"
    return None


def _series_why(conn: db.Connection, file: db.Row) -> str | None:
    """Return why the file's series or its previous name stops its restore now, else None."""
    if file["series_status"] != "ok" or not file["in_scope"]:
        return f"its series is {file['series_status']}" if file["in_scope"] else "its series is out of scope"
    if file["kid"] is None:
        return "its series is not in Kavita"
    target = _target(file)
    clash = conn.execute(
        "SELECT 1 FROM published_file WHERE library_path = %s AND id <> %s", (target, file["id"])
    ).fetchone()
    if clash or publish.queued(conn, file["series_id"], target, file["id"]):
        return f"its previous name {target} is taken"
    return None
