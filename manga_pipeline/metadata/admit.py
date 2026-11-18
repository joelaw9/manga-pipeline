"""Admission of new series (SPEC "Series identification").

A new series whose first metadata check completed gets its name and folder, once, before any of its chapters is taken.

The name is the one a person gave (`series new --name`), else the stored AniList English title, else the names the
series arrived under (a folder's ComicInfo Series before its title folder; a manga's title). It must be unique under
Kavita's name normalising among the library's series, the other new series Kavita does not hold yet and the folders
under the library root, and must leave a folder name once made safe; and none of its agreed ids may be one a series
the library holds or an admitted new series holds (a double hit), unless a person confirmed it (`series new` after a
completed check); else the series is needs_human.
"""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from manga_pipeline import config, db, names, workflow
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids
from manga_pipeline.replies.kavita import Series

if TYPE_CHECKING:
    from manga_pipeline.replies.sources import AniListMedia

log = logging.getLogger(__name__)
ANILIST_SUFFIX = " @COMIC"  # AniList's mark on a title also used by an anime, dropped as the old tool did


def run(ctx: workflow.Context, listing: Sequence[Series]) -> None:
    """Admit every in-scope pending new series whose check completed and is not due again at once.

    `series new` makes it due at once: the check runs first. listing: the library's series (Kavita's
    `series_in_library`, unfiltered).
    """
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
    library = _Library(listing, config.abs_path(cfg["paths"]["library"]))
    for series in pending:
        _admit(ctx, series, library)


class _Library:
    """The names in the library a new series' name must not clash with.

    Its series' names as Kavita normalises them, and the folders under its root (read once per admission run).
    """

    def __init__(self, listing: Iterable[Series], root: Path) -> None:
        self.root = root
        self._kavita_keys = {normalize(n): s for s in listing for n in series_names(s) if normalize(n)}
        self._on_disk = [p.name for p in root.iterdir()]

    def series_clash(self, key: str) -> str | None:
        """Why a Kavita series holds the name of normalised key, or None."""
        if key not in self._kavita_keys:
            return None
        found = self._kavita_keys[key]
        return f"Kavita series {found['id']} ({found['name']}) has that name"

    def folder_clash(self, key: str, safe: str) -> str | None:
        """Why a folder under the root holds the name (of normalised key, safe folder name), or None."""
        for folder in self._on_disk:
            if folder.casefold() == safe.casefold() or normalize(folder) == key:
                return f"the library already has a folder {folder!r}"
        return None


def name_for(conn: db.Connection, series: db.Row) -> str:
    """Return the series' name by SPEC "Series identification"."""
    if series["admit_name"]:
        return series["admit_name"]
    row = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND source = 'anilist' AND kind = 'media'", (series["id"],)
    ).fetchone()
    media: AniListMedia | None = row["payload"] if row else None
    english = (((media or {}).get("title")) or {}).get("english")
    if english:
        english = english.removesuffix(ANILIST_SUFFIX).strip()
    return english or series["alt_name"] or series["name"]


def _admit(ctx: workflow.Context, series: db.Row, library: _Library) -> None:
    conn = ctx.conn
    name = name_for(conn, series)
    safe = names.folder_name(name)
    with db.transaction(conn):
        double = _id_clash(conn, series)
        clash = double or _clash(conn, series, name, safe, library)
        if double:
            workflow.needs_human(
                conn,
                "series",
                series["id"],
                f"new series {name!r}: {double}, a double hit -> series link (MANGA_ID, or --folder FOLDER)"
                f" KAVITA_SERIES_ID if it is that series, else series new {series['id']}",
            )
        elif clash:
            workflow.needs_human(
                conn, "series", series["id"], f"new series {name!r}: {clash} -> series new {series['id']} --name NAME"
            )
        else:
            conn.execute(
                "UPDATE series SET comicinfo_series = %s, folder = %s, admitted_at = %s WHERE id = %s",
                (name, config.to_stored(ctx.cfg, library.root / safe), workflow.now(conn), series["id"]),
            )
    if clash:
        log.warning("series %s (%s): not admitted: %s", series["id"], series["name"], clash)
    else:
        log.info("series %s (%s): admitted as new series %r, folder %r", series["id"], series["name"], name, safe)


def _id_clash(conn: db.Connection, series: db.Row) -> str | None:
    """Return which series holds one of the series' agreed ids (SPEC "Source ids", a human id winning), or None.

    The series compared are those the library holds: a Kavita id, or admitted (in this run too: inside the admission's
    transaction). None for a series a person confirmed (`series new` after its check completed).
    """
    if series["admit_confirmed"]:
        return None
    rows = conn.execute(
        """SELECT s.id, s.name, i.source, i.origin, i.source_id
        FROM series_source_id i JOIN series s ON s.id = i.series_id
        WHERE s.id = %s OR s.kavita_series_id IS NOT NULL OR s.admitted_at IS NOT NULL
        ORDER BY s.id""",
        (series["id"],),
    ).fetchall()
    known: dict[int, list[ids.Known]] = {}
    series_names: dict[int, str] = {}
    for r in rows:
        known.setdefault(r["id"], []).append((r["source"], r["origin"], r["source_id"]))
        series_names[r["id"]] = r["name"]
    own, _ = ids.resolve(known.pop(series["id"], []))
    for other, held in known.items():
        agreed, _ = ids.resolve(held)
        for source, value in sorted(own.items()):
            if agreed.get(source) == value:
                return f"series {other} ({series_names[other]}) holds its {source} id {value}"
    return None


def _clash(conn: db.Connection, series: db.Row, name: str, safe: str, library: _Library) -> str | None:
    """Return why the name cannot be the series', or None.

    Runs inside the admission's transaction, so 2 admissions never both take a name.
    """
    if not safe:
        return "its name leaves no folder name once made safe"
    key = normalize(name)
    if not key:
        return "its name has no letter or digit Kavita keeps"
    return library.series_clash(key) or _new_series_clash(conn, series, key) or library.folder_clash(key, safe)


def _new_series_clash(conn: db.Connection, series: db.Row, key: str) -> str | None:
    """Why another new series Kavita does not hold yet has the name of normalised key, or None."""
    others = conn.execute(
        """SELECT id, comicinfo_series FROM series
        WHERE origin = 'new' AND kavita_series_id IS NULL AND comicinfo_series IS NOT NULL AND id <> %s""",
        (series["id"],),
    ).fetchall()
    for other in others:
        if normalize(other["comicinfo_series"]) == key:
            return f"new series {other['id']} ({other['comicinfo_series']}) has that name"
    return None
