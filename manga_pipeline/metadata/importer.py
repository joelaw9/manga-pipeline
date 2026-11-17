"""`metadata import STATE_FILE CONFIG_FILE` (SPEC "Metadata checks", Import): the old metadata tool's data imported.

The old metadata tool's ids, covers and written fields, and its config's manga overrides, into the database. Run
again, it replaces what it imported before.
"""

import json
import logging
from pathlib import Path

import yaml

from manga_pipeline import db, identify, workflow
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids, library

log = logging.getLogger(__name__)
SHOWN = 20  # skipped Kavita ids named in the log line
OLD_SOURCES = {"al": "anilist", "mu": "mangaupdates", "md": "mangadex", "mal": "myanimelist", "mb": "mangabaka"}


def read_state(path):
    """Return the state file's parts the import reads; Refused when it is not the old tool's state file."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        msg = f"cannot read the state file {path}: {e}"
        raise workflow.Refused(msg) from None
    if not isinstance(state, dict) or not isinstance(state.get("ids"), dict):
        msg = f"{path} is not the old metadata tool's state file: it has no `ids`"
        raise workflow.Refused(msg)
    return {part: state.get(part) or {} for part in ("ids", "covers", "metadata", "status")}


def read_overrides(path):
    """Return the config's manga overrides as {Kavita series name: Suwayomi manga id or title}.

    The config's `overrides`, then its `map` file's (a path relative to the config's folder).
    """
    try:
        old = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        msg = f"cannot read the old config {path}: {e}"
        raise workflow.Refused(msg) from None
    if not isinstance(old, dict):
        msg = f"{path} is not the old metadata tool's config"
        raise workflow.Refused(msg)
    found = {}
    if old.get("map"):
        map_path = Path(path).parent / old["map"]
        try:
            found.update(json.loads(map_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            msg = f"cannot read the old config's map {map_path}: {e}"
            raise workflow.Refused(msg) from None
    found.update(old.get("overrides") or {})
    return found


def run(ctx: workflow.Context, state, overrides):
    """Import the state file's ids, covers and written fields of each in-scope library series, then link the overrides.

    The library's series are synced to rows first; a series the state file no longer names loses what an earlier
    import gave it. Logs the counts imported, missing and linked.
    """
    conn = ctx.conn
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
    with db.transaction(conn):
        # A series the state file no longer names keeps no id imported from an earlier one
        stale = {**workflow.scope_params(ctx.cfg), "named": list(state["ids"])}
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
                _import_series(conn, rows[kavita_id], kavita_id, entry or {}, state)
    linked, unknown = _link_overrides(ctx, listing, overrides)
    log.info(
        "metadata import: %d series imported; %d not in the library or out of scope%s; %d override(s) linked%s",
        len(state["ids"]) - len(missing),
        len(missing),
        f" (Kavita ids {', '.join(missing[:SHOWN])}{', ...' if len(missing) > SHOWN else ''})" if missing else "",
        linked,
        f"; not found: {', '.join(unknown)}" if unknown else "",
    )


def _import_series(conn, series_id, kavita_id, entry, state):
    conn.execute("DELETE FROM series_source_id WHERE series_id = %s AND origin = 'import'", (series_id,))
    for key, value in entry.items():
        source = OLD_SOURCES.get(key)
        found = ids.form(source, value) if source and value is not None else None
        if found:
            conn.execute(
                "INSERT INTO series_source_id VALUES (%s, %s, 'import', %s) ON CONFLICT DO NOTHING",
                (series_id, source, found),
            )
    written = state["metadata"].get(kavita_id) or {}
    cover = state["covers"].get(kavita_id) or {}
    conn.execute(
        """INSERT INTO series_import (series_id, fields, status, cover_sha256) VALUES (%s, %s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET fields = excluded.fields, status = excluded.status,
            cover_sha256 = excluded.cover_sha256""",
        (series_id, json.dumps(written.get("fields") or []), state["status"].get(kavita_id), cover.get("sha256")),
    )


def _link_overrides(ctx: workflow.Context, listing, overrides):
    """Link each override's Suwayomi manga to the Kavita series it names. Returns (links made, names not found)."""
    by_name = {}
    for kseries in listing:
        for name in (kseries.get("name"), kseries.get("localizedName"), kseries.get("originalName")):
            if name:
                by_name.setdefault(normalize(name), kseries)
    manga = list(ctx.suwayomi.library_manga())
    by_id = {m["id"]: m for m in manga}
    by_title = {}
    for m in manga:
        by_title.setdefault(normalize(m["title"]), m)
    linked, unknown = 0, []
    for name, target in overrides.items():
        kseries = by_name.get(normalize(name))
        found = by_id.get(target) if isinstance(target, int) else by_title.get(normalize(str(target)))
        if found is None or kseries is None:
            unknown.append(repr(name))
            continue
        try:
            # series link's own path: a held row of the manga is merged, the series' folder is filled
            subject = identify.link_subject(ctx.cfg, ctx.suwayomi, manga_id=found["id"])
            identify.link(ctx.cfg, ctx.conn, ctx.kavita, kseries["id"], subject)
        except workflow.Refused as e:
            unknown.append(f"{name!r} ({e})")
            continue
        linked += 1
    return linked, unknown
