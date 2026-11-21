"""`metadata import STATE_FILE CONFIG_FILE` (SPEC "Metadata checks", Import): the old metadata tool's data imported.

The old metadata tool's ids, covers and written fields, and its config's manga overrides, into the database. Every
genre and tag list Kavita holds locked counts as the old tool's (it locked every list it wrote; its state file does not
list them all). Run again, it replaces what it imported before; once `export` has recorded any series' write
(`series_written` holds a row) it refuses, so a lock set after the cutover is never taken for the old tool's.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import yaml

from manga_pipeline import db, identify, workflow
from manga_pipeline.clients.kavita import Kavita
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids, library
from manga_pipeline.replies.kavita import Series

if TYPE_CHECKING:
    from manga_pipeline.replies.suwayomi import Manga

log = logging.getLogger(__name__)
SHOWN = 20  # skipped Kavita ids named in the log line
OLD_SOURCES = {"al": "anilist", "mu": "mangaupdates", "md": "mangadex", "mal": "myanimelist", "mb": "mangabaka"}


class OldCover(TypedDict, total=False):
    """The old tool's record of the cover it uploaded to a Kavita series."""

    sha256: str | None


class OldWritten(TypedDict, total=False):
    """The old tool's record of what it wrote to a Kavita series: the fields it wrote."""

    fields: list[str] | None


class OldConfig(TypedDict, total=False):
    """The old tool's config as the import reads it (unchecked): its map file (relative to it) and its overrides."""

    map: str | None
    overrides: dict[str, int | str] | None


type OldIds = dict[str, dict[str, str | int | None] | None]  # {Kavita id: {old source key (OLD_SOURCES): id}}
type OldCovers = dict[str, OldCover | None]
type OldMetadata = dict[str, OldWritten | None]
type OldStatus = dict[str, int | None]  # {Kavita id: the publication status it wrote}


class OldStateFile(TypedDict, total=False):
    """The old metadata tool's state file as read (unchecked but for `ids`): a part may be missing or null."""

    ids: OldIds | None
    covers: OldCovers | None
    metadata: OldMetadata | None
    status: OldStatus | None


class NotNamedParams(workflow.ScopeParams):
    """The parameters of the deletes of what the state file no longer names: SCOPE's, and the Kavita ids it names."""

    named: list[str]


class OldState(TypedDict):
    """The old metadata tool's state file's parts the import reads, keyed by Kavita series id (text); {} for none."""

    ids: OldIds
    covers: OldCovers
    metadata: OldMetadata
    status: OldStatus


def read_state(path: str | Path) -> OldState:
    """Return the state file's parts the import reads; Refused when it is not the old tool's state file."""
    try:
        state: OldStateFile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        msg = f"cannot read the state file {path}: {e}"
        raise workflow.Refused(msg) from None
    if not isinstance(state, dict) or not isinstance(state.get("ids"), dict):
        msg = f"{path} is not the old metadata tool's state file: it has no `ids`"
        raise workflow.Refused(msg)
    return {
        "ids": state.get("ids") or {},
        "covers": state.get("covers") or {},
        "metadata": state.get("metadata") or {},
        "status": state.get("status") or {},
    }


def read_overrides(path: str | Path) -> dict[str, int | str]:
    """Return the config's manga overrides as {Kavita series name: Suwayomi manga id or title}.

    The config's `overrides`, then its `map` file's (a path relative to the config's folder).
    """
    try:
        old: OldConfig = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        msg = f"cannot read the old config {path}: {e}"
        raise workflow.Refused(msg) from None
    if not isinstance(old, dict):
        msg = f"{path} is not the old metadata tool's config"
        raise workflow.Refused(msg)
    found: dict[str, int | str] = {}
    if map_name := old.get("map"):
        map_path = Path(path).parent / map_name
        try:
            found.update(json.loads(map_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            msg = f"cannot read the old config's map {map_path}: {e}"
            raise workflow.Refused(msg) from None
    found.update(old.get("overrides") or {})
    return found


def run(ctx: workflow.Context, state: OldState, overrides: Mapping[str, int | str]) -> None:
    """Import the state file's ids, covers and written fields of each in-scope library series, then link the overrides.

    Refused once `series_written` holds a row (checked again in the transaction that writes). The library's series
    are synced to rows first; each listed series' locked genre and tag lists are recorded as the old tool's, the state
    file's series or not; a series with neither a state file entry nor a locked list loses what an earlier import gave
    it. Logs the counts imported, missing and linked.
    """
    conn = ctx.conn
    _refuse_after_an_export(conn)
    listing = ctx.kavita.series_in_library(ctx.cfg["kavita"]["library_id"])
    library.sync(ctx, listing)
    rows = {
        str(r["kavita_series_id"]): r["id"]
        for r in conn.execute(
            sql.SQL("SELECT id, kavita_series_id FROM series s WHERE kavita_series_id IS NOT NULL AND {}").format(
                workflow.SCOPE
            ),
            workflow.scope_params(ctx.cfg),
        )
    }
    missing = sorted(set(state["ids"]) - set(rows), key=int)
    listed = {str(s["id"]) for s in listing}  # a row Kavita no longer lists has no metadata to read (a 404)
    locked = _locked_lists(ctx.kavita, [k for k in rows if k in listed])
    with db.transaction(conn):
        _refuse_after_an_export(conn)  # export runs under another lock: it may have written during the reads above
        # A series the state file no longer names keeps no id imported from an earlier one
        stale: NotNamedParams = {**workflow.scope_params(ctx.cfg), "named": list(state["ids"])}
        not_named = sql.SQL("NOT (s.kavita_series_id::text = ANY(%(named)s::text[])) AND {}").format(workflow.SCOPE)
        conn.execute(
            sql.SQL("""DELETE FROM series_source_id i USING series s WHERE s.id = i.series_id AND i.origin = 'import'
            AND {}""").format(not_named),
            stale,
        )
        conn.execute(
            sql.SQL("DELETE FROM series_import i USING series s WHERE s.id = i.series_id AND {}").format(not_named),
            stale,
        )
        for kavita_id, entry in state["ids"].items():
            if kavita_id in rows:
                _import_ids(conn, rows[kavita_id], entry or {})
        for kavita_id in [k for k in rows if k in state["ids"] or k in locked]:
            _import_written(conn, rows[kavita_id], kavita_id, state, locked.get(kavita_id, []))
    linked, unknown = _link_overrides(ctx, listing, overrides)
    log.info(
        "metadata import: %d series imported; %d not in the library or out of scope%s; %d override(s) linked%s;"
        " %d series' locked genre or tag lists recorded as the old tool's",
        len(state["ids"]) - len(missing),
        len(missing),
        f" (Kavita ids {', '.join(missing[:SHOWN])}{', ...' if len(missing) > SHOWN else ''})" if missing else "",
        linked,
        f"; not found: {', '.join(unknown)}" if unknown else "",
        len(locked),
    )


def _refuse_after_an_export(conn: db.Connection) -> None:
    """Refuse when `export` has recorded any series' write: a lock is then no longer known to be the old tool's."""
    if conn.execute("SELECT 1 FROM series_written LIMIT 1").fetchone() is not None:
        msg = "metadata import: export has recorded series writes already; the import only runs before the first export"
        raise workflow.Refused(msg)


def _locked_lists(kv: Kavita, kavita_ids: Sequence[str]) -> dict[str, list[str]]:
    """Return {Kavita id: the list fields ("genres", "tags") Kavita holds locked} of the listed series given."""
    found: dict[str, list[str]] = {}
    for kavita_id in kavita_ids:
        meta = kv.metadata(int(kavita_id))
        if fields := (["genres"] if meta.get("genresLocked") else []) + (["tags"] if meta.get("tagsLocked") else []):
            found[kavita_id] = fields
    return found


def _import_ids(conn: db.Connection, series_id: int, entry: Mapping[str, str | int | None]) -> None:
    conn.execute("DELETE FROM series_source_id WHERE series_id = %s AND origin = 'import'", (series_id,))
    for key, value in entry.items():
        source = OLD_SOURCES.get(key)
        found = ids.form(source, value) if source and value is not None else None
        if found:
            conn.execute(
                "INSERT INTO series_source_id VALUES (%s, %s, 'import', %s) ON CONFLICT DO NOTHING",
                (series_id, source, found),
            )


def _import_written(
    conn: db.Connection, series_id: int, kavita_id: str, state: OldState, locked: Sequence[str]
) -> None:
    """Store what the old tool wrote to the series: the state file's fields and the locked lists, status, cover."""
    written = state["metadata"].get(kavita_id) or {}
    fields = list(dict.fromkeys([*(written.get("fields") or []), *locked]))
    cover = state["covers"].get(kavita_id) or {}
    conn.execute(
        """INSERT INTO series_import (series_id, fields, status, cover_sha256) VALUES (%s, %s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET fields = excluded.fields, status = excluded.status,
            cover_sha256 = excluded.cover_sha256""",
        (series_id, json.dumps(fields), state["status"].get(kavita_id), cover.get("sha256")),
    )


def _link_overrides(
    ctx: workflow.Context, listing: Sequence[Series], overrides: Mapping[str, int | str]
) -> tuple[int, list[str]]:
    """Link each override's Suwayomi manga to the Kavita series it names. Returns (links made, names not found)."""
    by_name: dict[str, Series] = {}
    for kseries in listing:
        for name in (kseries.get("name"), kseries.get("localizedName"), kseries.get("originalName")):
            if name:
                by_name.setdefault(normalize(name), kseries)
    manga = list(ctx.suwayomi.library_manga())
    by_id = {m["id"]: m for m in manga}
    by_title: dict[str, Manga] = {}
    for m in manga:
        by_title.setdefault(normalize(m["title"]), m)
    linked = 0
    unknown: list[str] = []
    for name, target in overrides.items():
        kseries = by_name.get(normalize(name))
        found = by_id.get(target) if isinstance(target, int) else by_title.get(normalize(str(target)))
        if found is None or kseries is None:
            unknown.append(repr(name))
            continue
        try:
            # series link's own path: a held row of the manga is merged, the series' folder is filled
            subject = identify.manga_link_subject(ctx.suwayomi, found["id"])
            identify.link(ctx.cfg, ctx.conn, ctx.kavita, kseries["id"], subject)
        except workflow.Refused as e:
            unknown.append(f"{name!r} ({e})")
            continue
        linked += 1
    return linked, unknown
