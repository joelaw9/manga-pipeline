"""The library's series as rows (SPEC "Metadata checks"): each series of `kavita.library_id` in scope has a row, so it
is checked whether or not anything downloads it.
"""

import logging

from manga_pipeline import config, db, kavita_link, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients.kavita_match import normalize, series_names

log = logging.getLogger(__name__)


def sync(ctx: workflow.Context, listing):
    """Store each listed series (the library's listing, `Kavita.series_in_library`) the database does not hold. A
    listed series no row holds the id of takes the row of a series Kavita no longer lists whose names match and whose
    folder is the listed one's (Kavita recreated the series); else it is a new row, its folder fetched when first
    needed."""
    conn, cfg = ctx.conn, ctx.cfg
    # A published new series is matched first; a listed series named as one still unmatched gets no row of its own
    claimed = kavita_link.attach_all(ctx, listing)
    listed = {
        s["id"]: s
        for s in listing
        if workflow.in_scope(cfg, series_names(s), s["libraryId"])
        and not claimed & {normalize(n) for n in series_names(s)}
    }
    rows = conn.execute(
        "SELECT id, name, localized_name, original_name, comicinfo_series, kavita_series_id, folder FROM series"
        " WHERE kavita_series_id IS NOT NULL AND kavita_library_id = %s",
        (cfg["kavita"]["library_id"],),
    ).fetchall()
    held = {r["kavita_series_id"] for r in rows}
    gone = [r for r in rows if r["kavita_series_id"] not in listed]
    for kseries in (s for s in listed.values() if s["id"] not in held):
        row = _recreated(ctx, kseries, gone)
        with db.transaction(conn):
            if row is None:
                conn.execute(
                    """INSERT INTO series (name, kavita_series_id, kavita_library_id, origin, localized_name,
                        original_name) VALUES (%s, %s, %s, 'kavita', %s, %s)""",
                    (
                        kseries["name"],
                        kseries["id"],
                        kseries["libraryId"],
                        kseries.get("localizedName") or None,
                        kseries.get("originalName") or None,
                    ),
                )
            else:
                gone.remove(row)
                conn.execute("UPDATE series SET kavita_series_id = %s WHERE id = %s", (kseries["id"], row["id"]))
        if row is not None:
            log.warning(
                "series %s (%s): Kavita series %s is now %s (Kavita recreated it)",
                row["id"],
                row["name"],
                row["kavita_series_id"],
                kseries["id"],
            )


def _recreated(ctx: workflow.Context, kseries, gone):
    """A row of gone matching the listed series: by names, and by folder when the row has one; a row with no folder
    (nothing published or downloaded) matches by names alone, when exactly 1 does."""
    keys = {normalize(n) for n in series_names(kseries)}
    names = ("name", "localized_name", "original_name", "comicinfo_series")
    named = [r for r in gone if keys & {normalize(r[n]) for n in names if r[n]}]
    folderless = [r for r in named if r["folder"] is None]
    if len(folderless) == 1 and len(named) == 1:
        return folderless[0]
    for row in named:
        if row["folder"] is None:
            continue
        try:
            folder = kavita_link.library_folder(ctx.cfg, ctx.kavita.series_folder(kseries["id"]))
        except (kavita_client.FolderError, config.PathError, ValueError):  # ValueError: a path under no map prefix
            return None
        if folder == row["folder"]:
            return row
    return None
