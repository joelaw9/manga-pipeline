"""Admission of new series (SPEC "Series identification").

A new series whose first metadata check completed gets its name and folder, once, before any of its chapters is taken.
Its folder is `<library root>/<source folder>/<safe name>`, the source folder that of its first link (`source_folder`).

The name is the series name rule's (metadata/series_name.py: a person's name, the override file's `name` then
`series new --name`; else AniList's English title or a fallback; else the names the series arrived under, a folder's
ComicInfo Series before its title folder, a manga's title), and its romanized name, when free, its localized name. The
name must be free (series_name.taken, series_name.folder_taken) among the library's series, the other new series Kavita
does not hold yet and the folders in its source folder, and must leave a folder name once made safe; and none of its
agreed ids may be one a series the library holds or an admitted new series holds (a double hit), unless a person
confirmed it (`series new` after a completed check); and the source of its first link must be installed; else the
series is needs_human.
"""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from manga_pipeline import config, db, errors, names, volumes, workflow
from manga_pipeline.clients import suwayomi
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids, merge, series_name
from manga_pipeline.replies.kavita import Series
from manga_pipeline.replies.suwayomi import Source

log = logging.getLogger(__name__)
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
        self.holders = kavita_holders(listing)
        self._on_disk: dict[str, list[str]] = {}

    def read(self, source: str) -> None:
        """List the source folder, once: a share read, so made outside any transaction."""
        if source not in self._on_disk:
            parent = self.root / source
            self._on_disk[source] = [p.name for p in parent.iterdir()] if errors.exists(parent) else []

    def folder_clash(self, source: str, name: str) -> str | None:
        """Why a folder in the source folder (read first) holds the name, or None."""
        folder = series_name.folder_taken(name, self._on_disk[source])
        return None if folder is None else f"the library already has a folder {f'{source}/{folder}'!r}"


def kavita_holders(listing: Iterable[Series]) -> dict[str, str]:
    """Return {normalised name: who holds it} of every name of the listed Kavita series."""
    return {
        normalize(n): f"Kavita series {s['id']} ({s['name']}) has that name"
        for s in listing
        for n in series_names(s)
        if normalize(n)
    }


def new_series_holders(conn: db.Connection, series_id: int) -> dict[str, str]:
    """Return {normalised name: who holds it} of the other new series' names.

    Their ComicInfo and localized names, of the new series Kavita does not hold yet but series_id.
    """
    holders: dict[str, str] = {}
    for other in conn.execute(
        """SELECT id, comicinfo_series, localized_name FROM series
        WHERE origin = 'new' AND kavita_series_id IS NULL AND comicinfo_series IS NOT NULL AND id <> %s""",
        (series_id,),
    ):
        for n in (other["comicinfo_series"], other["localized_name"]):
            if n and normalize(n):
                holders.setdefault(
                    normalize(n), f"new series {other['id']} ({other['comicinfo_series']}) has that name"
                )
    return holders


def name_for(cfg: config.Config, conn: db.Connection, series: db.Row) -> series_name.Chosen:
    """Return the series' name and romanized name by SPEC "Series identification".

    OverrideError when its naming override file is malformed. A series not admitted yet has no rules key, so no
    override file: its override name applies at `series rename`.
    """
    key = series["rules_key"]
    override = volumes.override_name(config.abs_path(cfg["paths"]["naming_overrides"]), key) if key else None
    own = series_name.Own(
        series["name"], series["localized_name"], series["original_name"], series["alt_name"] or series["name"]
    )
    return series_name.chosen(own, merge.stored_records(conn, series["id"]), override or series["admit_name"])


def _admit(ctx: workflow.Context, series: db.Row, library: _Library, sources: Sequence[Source]) -> None:
    conn = ctx.conn
    try:
        chosen = name_for(ctx.cfg, conn, series)
    except volumes.OverrideError as e:
        with db.transaction(conn):
            workflow.needs_human(conn, "series", series["id"], f"new series {series['name']!r}: {e} -> fix the file")
        log.warning("series %s (%s): not admitted: %s", series["id"], series["name"], e)
        return
    name = chosen.name
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
            romaji = chosen.romaji and not _held(conn, series, chosen.romaji, library)
            if chosen.romaji and not romaji:
                log.info("series %s: its romanized name %r is taken: left out", series["id"], chosen.romaji)
            conn.execute(
                """UPDATE series SET comicinfo_series = %s, localized_name = %s, folder = %s, admitted_at = %s
                WHERE id = %s""",
                (
                    name,
                    chosen.romaji if romaji else None,
                    config.to_stored(ctx.cfg, folder),
                    workflow.now(conn),
                    series["id"],
                ),
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
    if not names.folder_name(name):
        return "its name leaves no folder name once made safe"
    return _held(conn, series, name, library) or library.folder_clash(source, name)


def _held(conn: db.Connection, series: db.Row, name: str, library: _Library) -> str | None:
    """Why a Kavita series, or another new series Kavita does not hold yet, has the name; or None."""
    return series_name.taken(name, {**new_series_holders(conn, series["id"]), **library.holders})
