"""Admission of new series (SPEC "Series identification"): a new series whose first metadata check completed gets its
name and folder, once, before any of its chapters is taken.

The name is the one a person gave (`series new --name`), else the stored AniList English title, else the names the
series arrived under (a folder's ComicInfo Series before its title folder; a manga's title). It must be unique under
Kavita's name normalising among the library's series, the other new series Kavita does not hold yet and the folders
under the library root, and must leave a folder name once made safe; else the series is needs_human.
"""

import logging

from manga_pipeline import config, db, names, workflow
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.db import sql

log = logging.getLogger(__name__)
ANILIST_SUFFIX = " @COMIC"  # AniList's mark on a title also used by an anime, dropped as the old tool did


def run(ctx, listing):
    """Admit every in-scope pending new series whose check completed and is not due again at once (`series new`
    makes it so: the check runs first). listing: the library's series (Kavita's
    `series_in_library`, unfiltered)."""
    conn, cfg = ctx.conn, ctx.cfg
    pending = conn.execute(
        sql.SQL("""SELECT s.* FROM series s JOIN series_check k ON k.series_id = s.id
        WHERE s.origin = 'new' AND s.admitted_at IS NULL AND s.status = 'ok' AND k.data_version > 0 AND NOT k.urgent
            AND {scope}
        ORDER BY s.id""").format(scope=workflow.SCOPE),
        workflow.scope_params(cfg),
    ).fetchall()
    if not pending:
        return
    kavita_keys = {normalize(n): s for s in listing for n in series_names(s) if normalize(n)}
    root = config.abs_path(cfg["paths"]["library"])
    on_disk = [p.name for p in root.iterdir()]
    for series in pending:
        _admit(ctx, series, kavita_keys, on_disk, root)


def name_for(conn, series):
    """The series' name by SPEC "Series identification"."""
    if series["admit_name"]:
        return series["admit_name"]
    row = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND source = 'anilist' AND kind = 'media'", (series["id"],)
    ).fetchone()
    english = ((((row["payload"] if row else None) or {}).get("title")) or {}).get("english")
    if english:
        english = english.removesuffix(ANILIST_SUFFIX).strip()
    return english or series["alt_name"] or series["name"]


def _admit(ctx, series, kavita_keys, on_disk, root):
    conn = ctx.conn
    name = name_for(conn, series)
    safe = names.folder_name(name)
    with db.transaction(conn):
        clash = _clash(conn, series, name, kavita_keys, on_disk)
        if clash:
            workflow.needs_human(
                conn, "series", series["id"], f"new series {name!r}: {clash} -> series new {series['id']} --name NAME"
            )
        else:
            conn.execute(
                "UPDATE series SET comicinfo_series = %s, folder = %s, admitted_at = %s WHERE id = %s",
                (name, config.to_stored(ctx.cfg, root / safe), workflow.now(conn), series["id"]),
            )
    if clash:
        log.warning("series %s (%s): not admitted: %s", series["id"], series["name"], clash)
    else:
        log.info("series %s (%s): admitted as new series %r, folder %r", series["id"], series["name"], name, safe)


def _clash(conn, series, name, kavita_keys, on_disk):
    """Why the name cannot be the series', or None (inside the admission's transaction, so 2 admissions never both
    take a name)."""
    safe = names.folder_name(name)
    if not safe:
        return "its name leaves no folder name once made safe"
    key = normalize(name)
    if not key:
        return "its name has no letter or digit Kavita keeps"
    if key in kavita_keys:
        found = kavita_keys[key]
        return f"Kavita series {found['id']} ({found['name']}) has that name"
    others = conn.execute(
        """SELECT id, comicinfo_series FROM series
        WHERE origin = 'new' AND kavita_series_id IS NULL AND comicinfo_series IS NOT NULL AND id <> %s""",
        (series["id"],),
    ).fetchall()
    for other in others:
        if normalize(other["comicinfo_series"]) == key:
            return f"new series {other['id']} ({other['comicinfo_series']}) has that name"
    for folder in on_disk:
        if folder.casefold() == safe.casefold() or normalize(folder) == key:
            return f"the library already has a folder {folder!r}"
    return None
