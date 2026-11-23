"""Admission of new series (SPEC "Series identification").

A new series whose first metadata check completed gets its name and folder, once, before any of its chapters is taken.
Its folder is `<library root>/<source folder>/<safe name>`, the source folder that of its first link (`source_folder`).

The name is the one a person gave (`series new --name`), else the stored AniList English title, else the names the
series arrived under (a folder's ComicInfo Series before its title folder; a manga's title). It must be unique under
Kavita's name normalising among the library's series, the other new series Kavita does not hold yet and the folders
in its source folder, and must leave a folder name once made safe; and none of its agreed ids may be one a series
the library holds or an admitted new series holds (a double hit), unless a person confirmed it (`series new` after a
completed check); and the source of its first link must be installed; else the series is needs_human.
"""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from manga_pipeline import config, db, errors, names, workflow
from manga_pipeline.clients import suwayomi
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids
from manga_pipeline.replies.kavita import Series
from manga_pipeline.replies.sources import AniListMedia
from manga_pipeline.replies.suwayomi import Source

log = logging.getLogger(__name__)
ANILIST_SUFFIX = " @COMIC"  # AniList's mark on a title also used by an anime, dropped as the old tool did
SOURCE_FIX = "install the source; then series new {series_id}"  # a series with no source folder: the fix


def run(ctx: workflow.Context, listing: Sequence[Series]) -> None:
    """Admit every in-scope pending new series whose check completed and is not due again at once.

    `series new` makes it due at once: the check runs first. listing: the library's series (Kavita's
    `series_in_library`, unfiltered). Suwayomi's sources are listed once, only when a series is pending.
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
    sources = ctx.suwayomi.sources()
    for series in pending:
        _admit(ctx, series, library, sources)


def source_folder(
    conn: db.Connection, series_id: int, sources: Sequence[Source]
) -> tuple[str, None] | tuple[None, str]:
    """Return (the series' source folder, None), or (None, why it has none) (SPEC "Publishing").

    That of its first link: its Suwayomi manga with the lowest id, its source's download folder name (sources:
    Suwayomi's `sources()`; a source not listed gives none), else its hand-placed download folder's first part.
    """
    manga = conn.execute(
        "SELECT manga_id, source_id FROM suwayomi_manga WHERE series_id = %s ORDER BY manga_id LIMIT 1", (series_id,)
    ).fetchone()
    if manga is not None:
        shown = {s["id"]: s["displayName"] for s in sources}
        if manga["source_id"] not in shown:
            return None, f"the source {manga['source_id']} of its manga {manga['manga_id']} is not installed"
        return suwayomi.source_folder(shown[manga["source_id"]]), None
    row = conn.execute(
        "SELECT folder FROM download_folder WHERE series_id = %s ORDER BY folder LIMIT 1", (series_id,)
    ).fetchone()
    if row is None:
        return None, "it has no link"
    return row["folder"].split("/", 1)[0], None


class _Library:
    """The names in the library a new series' name must not clash with.

    Its series' names as Kavita normalises them, and the folders in each source folder (each read once per admission
    run; a source folder the library lacks holds none). A library root that is not there is Unreachable("share").
    """

    def __init__(self, listing: Iterable[Series], root: Path) -> None:
        if not errors.exists(root):
            raise workflow.Unreachable(workflow.SHARE, f"library folder {root} missing")
        self.root = root
        self._kavita_keys = {normalize(n): s for s in listing for n in series_names(s) if normalize(n)}
        self._on_disk: dict[str, list[str]] = {}

    def series_clash(self, key: str) -> str | None:
        """Why a Kavita series holds the name of normalised key, or None."""
        if key not in self._kavita_keys:
            return None
        found = self._kavita_keys[key]
        return f"Kavita series {found['id']} ({found['name']}) has that name"

    def read(self, source: str) -> None:
        """List the source folder, once: a share read, so made outside any transaction."""
        if source not in self._on_disk:
            parent = self.root / source
            self._on_disk[source] = [p.name for p in parent.iterdir()] if errors.exists(parent) else []

    def folder_clash(self, source: str, key: str, safe: str) -> str | None:
        """Why a folder in the source folder (read first) holds the name (of normalised key, safe name), or None."""
        for folder in self._on_disk[source]:
            if folder.casefold() == safe.casefold() or normalize(folder) == key:
                return f"the library already has a folder {f'{source}/{folder}'!r}"
        return None


def name_for(conn: db.Connection, series: db.Row) -> str:
    """Return the series' name by SPEC "Series identification"."""
    if series["admit_name"]:
        return series["admit_name"]
    row = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND source = 'anilist' AND kind = 'media'", (series["id"],)
    ).fetchone()
    media: AniListMedia | None = row["payload"] if row else None
    return anilist_title(media) or series["alt_name"] or series["name"]


def anilist_title(media: AniListMedia | None) -> str | None:
    """Return an AniList record's English title less ANILIST_SUFFIX, None when it has none."""
    english = (((media or {}).get("title")) or {}).get("english")
    if not english:
        return None
    return english.removesuffix(ANILIST_SUFFIX).strip() or None


def _admit(ctx: workflow.Context, series: db.Row, library: _Library, sources: Sequence[Source]) -> None:
    conn = ctx.conn
    name = name_for(conn, series)
    # Before the transaction: its links do not change meanwhile, and the share is not read under the write lock
    source, clash = source_folder(conn, series["id"], sources)
    if source is not None:
        library.read(source)
    with db.transaction(conn):
        double = _id_clash(conn, series)
        if double:
            clash = double
            workflow.needs_human(
                conn,
                "series",
                series["id"],
                f"new series {name!r}: {double}, a double hit -> series link (MANGA_ID, or --folder FOLDER)"
                f" KAVITA_SERIES_ID if it is that series, else series new {series['id']}",
            )
        elif source is None:
            workflow.needs_human(
                conn,
                "series",
                series["id"],
                f"new series {name!r}: {clash} -> {SOURCE_FIX.format(series_id=series['id'])}",
            )
        elif clash := _clash(conn, series, name, library, source):
            workflow.needs_human(
                conn, "series", series["id"], f"new series {name!r}: {clash} -> series new {series['id']} --name NAME"
            )
        else:
            folder = library.root / source / names.folder_name(name)
            conn.execute(
                "UPDATE series SET comicinfo_series = %s, folder = %s, admitted_at = %s WHERE id = %s",
                (name, config.to_stored(ctx.cfg, folder), workflow.now(conn), series["id"]),
            )
    if clash:
        log.warning("series %s (%s): not admitted: %s", series["id"], series["name"], clash)
    else:
        log.info("series %s (%s): admitted as new series %r in %r", series["id"], series["name"], name, source)


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


def _clash(conn: db.Connection, series: db.Row, name: str, library: _Library, source: str) -> str | None:
    """Return why the name cannot be the series' in its source folder, or None.

    Runs inside the admission's transaction, so 2 admissions never both take a name.
    """
    safe = names.folder_name(name)
    if not safe:
        return "its name leaves no folder name once made safe"
    key = normalize(name)
    if not key:
        return "its name has no letter or digit Kavita keeps"
    return library.series_clash(key) or _new_series_clash(conn, series, key) or library.folder_clash(source, key, safe)


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
