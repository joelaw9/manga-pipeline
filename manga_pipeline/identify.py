"""Series identification (SPEC "Series identification"): which series a Suwayomi manga's downloads belong to.

`series_for` works through the order the spec gives and writes what it settles (a series row and the manga's link) in
1 transaction; a match outside series.only, or a new series, writes nothing. `link` is `series link`.
"""

import logging
from dataclasses import dataclass

from manga_pipeline import db, names, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients.kavita_match import normalize, series_names

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotTaken:
    """The manga's downloads are not processed: out of scope (listed nowhere) or a new series (listed by status)."""

    reason: str
    listed: bool


OUT_OF_SCOPE = NotTaken("out of scope", listed=False)


class KavitaSeries:
    """The library's series, read from Kavita once per discovery and only when a manga needs them."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._rows = None

    def rows(self):
        if self._rows is None:
            self._rows = self._ctx.kavita.series_in_library(self._ctx.cfg["kavita"]["library_id"])
        return self._rows


def series_for(ctx, manga, kavita_series):
    """The series row of a library manga, or NotTaken. A series row may be needs_human (near or double hit, no
    usable folder): its chapters are recorded and held."""
    conn, cfg = ctx.conn, ctx.cfg
    row = conn.execute(
        "SELECT s.* FROM suwayomi_manga m JOIN series s ON s.id = m.series_id WHERE m.manga_id = %s", (manga["id"],)
    ).fetchone()
    by_source_id = row is None
    if by_source_id:
        row = _by_source_id(conn, source_ids(manga))
    if row is not None:
        if not workflow.in_scope(cfg, row["name"], row["kavita_library_id"]):
            return OUT_OF_SCOPE
        if by_source_id:
            _link_manga(conn, manga, row["id"])
        return row
    title = manga["title"]
    strict = [s for s in kavita_series.rows() if _strict_key(title) and _strict_key(title) in _strict_keys(s)]
    if len(strict) == 1:
        found = strict[0]
        if not workflow.in_scope(cfg, found["name"], found["libraryId"]):
            return OUT_OF_SCOPE
        return _store_kavita_series(ctx, manga, found)
    near = [
        s
        for s in kavita_series.rows()
        if names.key(title) and names.key(title) in {names.key(n) for n in series_names(s)}
    ]
    if strict or near:
        if not workflow.in_scope(cfg, title, None):
            return OUT_OF_SCOPE
        kind = "matches 2 or more Kavita series" if strict else "nearly matches"
        candidates = ", ".join(f"{s['name']} (Kavita {s['id']})" for s in strict or near)
        return _store_held_series(conn, manga, f"Suwayomi manga {manga['id']} {title!r} {kind}: {candidates}")
    if not workflow.in_scope(cfg, title, None):
        return OUT_OF_SCOPE
    return NotTaken(f"new series {title!r}: no Kavita series matches (new series come in a later phase)", listed=True)


def source_ids(manga):
    """The (source, id) pairs a manga carries that series_source_id can hold. None are derived yet: the MangaDex
    UUID and tracker ids come with the metadata phase, which also fills series_source_id."""
    return []


def _by_source_id(conn, ids):
    for source, source_id in ids:
        row = conn.execute(
            """SELECT s.* FROM series_source_id i JOIN series s ON s.id = i.series_id
            WHERE i.source = %s AND i.source_id = %s""",
            (source, source_id),
        ).fetchone()
        if row is not None:
            return row
    return None


def _strict_key(name):
    return normalize(name)


def _strict_keys(series):
    return {normalize(n) for n in series_names(series)}


def _store_kavita_series(ctx, manga, found):
    """The series row of a strict Kavita hit: the row already holding its Kavita id (another source's manga of the
    same series), else a new one with its names and folder; a series with no usable folder is needs_human."""
    conn = ctx.conn
    existing = conn.execute("SELECT id FROM series WHERE kavita_series_id = %s", (found["id"],)).fetchone()
    folder, problem = _folder(ctx, found["id"])
    with db.transaction(conn):
        if existing is None:
            series_id = conn.execute(
                """INSERT INTO series (name, kavita_series_id, kavita_library_id, folder, origin, localized_name,
                    original_name)
                VALUES (%s, %s, %s, %s, 'kavita', %s, %s) RETURNING id""",
                (
                    found["name"],
                    found["id"],
                    found["libraryId"],
                    folder,
                    found.get("localizedName") or None,
                    found.get("originalName") or None,
                ),
            ).fetchone()[0]
            if problem:
                workflow.needs_human(conn, "series", series_id, problem)
        else:
            series_id = existing[0]
        _link_manga(conn, manga, series_id)
    return conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()


def _folder(ctx, kavita_series_id):
    """(the series folder as text, None) or (None, why it has none)."""
    try:
        return str(ctx.kavita.series_folder(kavita_series_id)), None
    except kavita_client.FolderError as e:
        return None, str(e)


def _store_held_series(conn, manga, reason):
    """A needs_human series row for a manga a person must link (no Kavita id: in scope as not yet known)."""
    with db.transaction(conn):
        series_id = conn.execute(
            "INSERT INTO series (name, origin) VALUES (%s, 'new') RETURNING id", (manga["title"],)
        ).fetchone()[0]
        workflow.needs_human(conn, "series", series_id, f"{reason} -> series link {manga['id']} KAVITA_SERIES_ID")
        _link_manga(conn, manga, series_id)
    return conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()


def _link_manga(conn, manga, series_id):
    """Store the manga's link to its series (inside the caller's transaction, or its own)."""
    sql = """INSERT INTO suwayomi_manga (manga_id, source_id, title, series_id) VALUES (%s, %s, %s, %s)
        ON CONFLICT (manga_id) DO UPDATE SET series_id = excluded.series_id, title = excluded.title"""
    params = (manga["id"], str(manga["sourceId"]), manga["title"], series_id)
    if db.in_transaction(conn):
        conn.execute(sql, params)
    else:
        with db.transaction(conn):
            conn.execute(sql, params)


def link(cfg, conn, suwayomi, kavita, manga_id, kavita_series_id):
    """`series link` (SPEC "Series identification"): the manga's downloads belong to the Kavita series. The row holding
    that Kavita id, else the manga's held row, else a new row gets its Kavita id, names and folder (refreshed when
    run again); the manga is linked to it. A held row of the manga (no Kavita id) is merged into the Kavita series' row:
    its chapters and links move there and it is deleted. workflow.Refused when the manga or the series does not exist,
    the series is outside kavita.library_id or has no folder, or the manga's row is another Kavita series' row that
    holds chapters. needs_human is left for `retry --series` to clear. Returns the series id."""
    manga = suwayomi.manga(manga_id)
    if manga is None:
        raise workflow.Refused(f"Suwayomi holds no manga {manga_id}")
    found = kavita.series(kavita_series_id)
    if found is None:
        raise workflow.Refused(f"Kavita holds no series {kavita_series_id}")
    if found["libraryId"] != cfg["kavita"]["library_id"]:
        raise workflow.Refused(
            f"Kavita series {kavita_series_id} is in library {found['libraryId']}, not kavita.library_id "
            f"{cfg['kavita']['library_id']}"
        )
    try:
        folder = str(kavita.series_folder(kavita_series_id))
    except kavita_client.FolderError as e:
        raise workflow.Refused(str(e)) from None
    with db.transaction(conn):
        by_kavita = conn.execute("SELECT id FROM series WHERE kavita_series_id = %s", (kavita_series_id,)).fetchone()
        by_manga = conn.execute(
            """SELECT s.id, s.kavita_series_id FROM suwayomi_manga m JOIN series s ON s.id = m.series_id
            WHERE m.manga_id = %s""",
            (manga_id,),
        ).fetchone()
        other_kavita = by_manga and by_manga["kavita_series_id"] not in (None, kavita_series_id)
        if other_kavita and conn.execute("SELECT 1 FROM chapter WHERE series_id = %s", (by_manga["id"],)).fetchone():
            raise workflow.Refused(
                f"manga {manga_id} is linked to series {by_manga['id']} (Kavita {by_manga['kavita_series_id']}), "
                f"which has chapters: they are not moved to another Kavita series"
            )
        if by_kavita and by_manga and by_manga["kavita_series_id"] is None:
            _merge_held(conn, by_manga["id"], by_kavita[0])
        if other_kavita:
            by_manga = None  # its row stays the other series'; the manga alone moves
        series_id = (by_kavita or by_manga or [None])[0]
        values = (
            found["name"],
            kavita_series_id,
            found["libraryId"],
            folder,
            found.get("localizedName") or None,
            found.get("originalName") or None,
        )
        if series_id is None:
            series_id = conn.execute(
                """INSERT INTO series (name, kavita_series_id, kavita_library_id, folder, localized_name, original_name,
                    origin)
                VALUES (%s, %s, %s, %s, %s, %s, 'kavita') RETURNING id""",
                values,
            ).fetchone()[0]
        else:
            conn.execute(
                """UPDATE series SET name = %s, kavita_series_id = %s, kavita_library_id = %s, folder = %s,
                    localized_name = %s, original_name = %s, origin = 'kavita'
                WHERE id = %s""",
                (*values, series_id),
            )
        _link_manga(conn, manga, series_id)
    log.info("manga %s linked to series %s (Kavita %s)", manga_id, series_id, kavita_series_id)
    return series_id


def _merge_held(conn, held_id, series_id):
    """Move a held row's chapters (workflow.move_chapters) and manga links to the series row, and delete it (inside
    link's transaction). A held row publishes nothing (it has no ComicInfo Series name), so it has no published or
    handled chapter; a layout or calibration computed for it is dropped."""
    workflow.move_chapters(conn, held_id, series_id)
    conn.execute("UPDATE suwayomi_manga SET series_id = %s WHERE series_id = %s", (series_id, held_id))
    conn.execute("DELETE FROM series_conversion WHERE series_id = %s", (held_id,))
    conn.execute("DELETE FROM series_calibration WHERE series_id = %s", (held_id,))
    conn.execute("DELETE FROM series WHERE id = %s", (held_id,))
    log.info("held series %s merged into series %s", held_id, series_id)
