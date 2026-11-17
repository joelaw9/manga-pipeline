"""Kavita series folders in stored form, and a published new series' Kavita id (SPEC "Publishing"): once Kavita lists
it, the listed series no row holds that is named as its ComicInfo Series under Kavita's name normalising and whose
folder is the series' folder. Each series is looked for at most once per run.
"""

import logging

from manga_pipeline import config, db, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.db import sql

log = logging.getLogger(__name__)

# An admitted new series published into its folder whose Kavita id is not found yet (series s), whatever its status:
# a needs_human one still owns its name and folder
UNATTACHED = sql.SQL("""s.origin = 'new' AND s.admitted_at IS NOT NULL AND s.kavita_series_id IS NULL
    AND EXISTS (SELECT 1 FROM published_file p WHERE p.series_id = s.id)""")


def library_folder(cfg, folder):
    """A Kavita series folder in stored form; PathError unless it lies under paths.library (SPEC "State")."""
    stored = config.to_stored(cfg, folder)
    if stored.split("/", 1)[0] != "library":
        msg = f"the series folder {folder} is not under paths.library"
        raise config.PathError(msg)
    return stored


def listing(ctx: workflow.Context):
    """The library's series (`series_in_library`, unfiltered), read once per run."""
    if "listing" not in ctx.kavita_cache:
        ctx.kavita_cache["listing"] = ctx.kavita.series_in_library(ctx.cfg["kavita"]["library_id"])
    return ctx.kavita_cache["listing"]


def attach(ctx: workflow.Context, series_id, rows):
    """The series' Kavita id: the stored one, else the one found among rows (the library's listing) and stored, else
    None (not an unattached series, or Kavita does not list it yet).
    """
    conn = ctx.conn
    query = sql.SQL("SELECT s.*, {} AS unattached FROM series s WHERE s.id = %s").format(UNATTACHED)
    series = db.one_row(conn, query, (series_id,))
    if series["kavita_series_id"] is not None or not series["unattached"]:
        return series["kavita_series_id"]
    tried = ctx.kavita_cache.setdefault("attach", {})
    if series_id in tried:
        return tried[series_id]
    found = _find(ctx, series, rows)
    if found is not None:
        with db.transaction(conn):
            stored = conn.execute(
                """UPDATE series SET kavita_series_id = %s, kavita_library_id = %s
                WHERE id = %s AND kavita_series_id IS NULL""",
                (found["id"], found["libraryId"], series_id),
            ).rowcount
        if stored:
            log.info("series %s (%s): Kavita lists it as series %s", series_id, series["name"], found["id"])
    tried[series_id] = db.one_row(conn, "SELECT kavita_series_id FROM series WHERE id = %s", (series_id,))[0]
    return tried[series_id]


def attach_all(ctx: workflow.Context, rows):
    """attach every unattached series in scope; returns the normalised names of those still unattached: a listed
    series so named may be one of them, and is not a series of its own.
    """
    ids = ctx.conn.execute(
        sql.SQL("SELECT s.id FROM series s WHERE {unattached} AND {scope} ORDER BY s.id").format(
            unattached=UNATTACHED, scope=workflow.SCOPE
        ),
        workflow.scope_params(ctx.cfg),
    ).fetchall()
    for (series_id,) in ids:
        attach(ctx, series_id, rows)
    left = ctx.conn.execute(
        sql.SQL("SELECT s.comicinfo_series FROM series s WHERE {unattached} AND {scope}").format(
            unattached=UNATTACHED, scope=workflow.SCOPE
        ),
        workflow.scope_params(ctx.cfg),
    ).fetchall()
    return {normalize(r[0]) for r in left if normalize(r[0])}


def _find(ctx: workflow.Context, series, rows):
    conn, key = ctx.conn, normalize(series["comicinfo_series"])
    held = {r[0] for r in conn.execute("SELECT kavita_series_id FROM series WHERE kavita_series_id IS NOT NULL")}
    for kseries in rows:
        if kseries["id"] in held or kseries["libraryId"] != ctx.cfg["kavita"]["library_id"]:
            continue
        if key not in {normalize(n) for n in series_names(kseries)}:
            continue
        try:
            folder = library_folder(ctx.cfg, ctx.kavita.series_folder(kseries["id"]))
        except (kavita_client.FolderError, config.PathError, ValueError):  # ValueError: a path under no map prefix
            continue
        if folder == series["folder"]:
            return kseries
    return None
