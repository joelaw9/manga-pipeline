"""Series identification (SPEC "Series identification"): which series a Suwayomi manga's downloads, or a hand-placed
download folder's, belong to.

`series_for` (a manga) and `series_for_folder` (a folder no library manga maps to) work through the order the spec
gives and write what they settle (a series row and the manga's or folder's link) in 1 transaction; a match outside
series.only writes nothing. A new series gets its row (origin new, not admitted) and link, and its downloads are not
taken until it is admitted (metadata/admit.py). `link` is `series link`.
"""

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

from manga_pipeline import archive, comicinfo, config, db, errors, kavita_link, names, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids as metadata_ids
from manga_pipeline.metadata import search

log = logging.getLogger(__name__)
FOLDER_LINK_PARTS = 2  # SOURCE_FOLDER/TITLE_FOLDER


@dataclass(frozen=True)
class NotTaken:
    """The manga's downloads are not processed: out of scope (listed nowhere) or a new series (listed by status)."""

    reason: str
    listed: bool


OUT_OF_SCOPE = NotTaken("out of scope", listed=False)
NOT_ADMITTED = NotTaken("new series, metadata not fetched yet", listed=True)


class KavitaSeries:
    """The library's series, read from Kavita once per discovery and only when a manga needs them."""

    def __init__(self, ctx: workflow.Context):
        self._ctx = ctx
        self._rows = None

    def rows(self):
        if self._rows is None:
            self._rows = self._ctx.kavita.series_in_library(self._ctx.cfg["kavita"]["library_id"])
        return self._rows


ALL_COLUMNS = sql.SQL("s.*")  # every series column: what _Subject.linked reads by default


@dataclass(frozen=True)
class _Subject:
    """What is linked to a series: a Suwayomi manga or a hand-placed download folder."""

    label: str  # how messages name it
    held_name: str  # a held row's series name
    command: str  # `series link` naming it, before KAVITA_SERIES_ID
    find: sql.SQL  # SQL of its linked series' row: 1 parameter, the key; {columns} picks the series columns
    key: int | str  # the SQL parameter of find: a manga id or a download folder
    write_link: Callable  # (conn, series_id): store its link, inside the caller's transaction

    def linked(self, conn, columns=ALL_COLUMNS):
        return conn.execute(self.find.format(columns=columns), (self.key,)).fetchone()


def _manga_subject(manga):
    def write(conn, series_id):
        conn.execute(
            """INSERT INTO suwayomi_manga (manga_id, source_id, title, series_id) VALUES (%s, %s, %s, %s)
            ON CONFLICT (manga_id) DO UPDATE SET series_id = excluded.series_id, title = excluded.title""",
            (manga["id"], str(manga["sourceId"]), manga["title"], series_id),
        )

    return _Subject(
        label=f"Suwayomi manga {manga['id']}",
        held_name=manga["title"],
        command=f"series link {manga['id']}",
        find=sql.SQL(
            "SELECT {columns} FROM suwayomi_manga m JOIN series s ON s.id = m.series_id WHERE m.manga_id = %s"
        ),
        key=manga["id"],
        write_link=write,
    )


def _folder_subject(folder):
    def write(conn, series_id):
        conn.execute(
            """INSERT INTO download_folder (folder, series_id) VALUES (%s, %s)
            ON CONFLICT (folder) DO UPDATE SET series_id = excluded.series_id""",
            (folder, series_id),
        )

    return _Subject(
        label=f"download folder {folder!r}",
        held_name=folder.split("/")[1],
        command=f'series link --folder "{folder}"',
        find=sql.SQL("SELECT {columns} FROM download_folder f JOIN series s ON s.id = f.series_id WHERE f.folder = %s"),
        key=folder,
        write_link=write,
    )


def series_for(ctx: workflow.Context, manga, kavita_series):
    """The series row of a library manga, or NotTaken. A series row may be needs_human (near or double hit, no
    usable folder): its chapters are recorded and held."""
    conn, cfg = ctx.conn, ctx.cfg
    subject = _manga_subject(manga)
    row = subject.linked(conn)
    by_source_id = row is None
    if by_source_id:
        rows = _by_source_id(conn, source_ids(manga))
        if len(rows) > 1:
            candidates = ", ".join(f"{r['name']} (series {r['id']})" for r in rows)
            return _store_held_series(
                conn, subject, f"{subject.label} has a source id of 2 or more series: {candidates}"
            )
        row = rows[0] if rows else None
    if row is not None:
        if not workflow.row_in_scope(cfg, row):
            return OUT_OF_SCOPE
        if by_source_id:
            with db.transaction(conn):
                subject.write_link(conn, row["id"])
        return _taken(ctx, row)
    return _by_names(ctx, subject, [manga["title"]], kavita_series)


def series_for_folder(ctx: workflow.Context, folder, paths, kavita_series):
    """The series row of a hand-placed download folder ("<source folder>/<title folder>", holding paths), or NotTaken:
    its stored link, else its title folder's name, else (read only then) the ComicInfo Series its files agree on."""
    subject = _folder_subject(folder)
    row = subject.linked(ctx.conn)
    if row is not None:
        return _taken(ctx, row) if workflow.row_in_scope(ctx.cfg, row) else OUT_OF_SCOPE
    title = subject.held_name

    def titles():
        yield title
        series = comicinfo_series(paths)
        if series and series != title:
            yield series

    return _by_names(ctx, subject, titles(), kavita_series)


def link_manga(conn, manga, series_id):
    """Store a manga's link to a series; inside the caller's transaction."""
    _manga_subject(manga).write_link(conn, series_id)


def sweep(ctx: workflow.Context, library, kavita_series):
    """Link each library manga with no link to its series when steps 2 or 3 find exactly 1 (SPEC "Series
    identification"); a near, double or missing hit writes nothing. Returns the manga left unlinked."""
    conn = ctx.conn
    linked = {row[0] for row in conn.execute("SELECT manga_id FROM suwayomi_manga")}
    by_key = {}
    for kseries in kavita_series.rows():
        for key in _strict_keys(kseries):
            by_key.setdefault(key, {})[kseries["id"]] = kseries
    left = []
    for manga in library:
        if manga["id"] in linked:
            continue
        rows = _by_source_id(conn, source_ids(manga))
        if not rows:
            strict = list(by_key.get(_strict_key(manga["title"]), {}).values()) if _strict_key(manga["title"]) else []
            if len(strict) == 1:
                rows = conn.execute("SELECT * FROM series WHERE kavita_series_id = %s", (strict[0]["id"],)).fetchall()
        if len(rows) == 1 and workflow.row_in_scope(ctx.cfg, rows[0]):
            with db.transaction(conn):
                link_manga(conn, manga, rows[0]["id"])
            log.info(
                "Suwayomi manga %s %r linked to series %s (%s)",
                manga["id"],
                manga["title"],
                rows[0]["id"],
                rows[0]["name"],
            )
        else:
            left.append(manga)
    return left


def comicinfo_series(paths):
    """The ComicInfo.xml Series every download that carries a non-empty one and can be read agrees on, else None. A
    dropped share raises."""
    found = set()
    for path in paths:
        try:
            xml = archive.read_comicinfo(path)
            name = comicinfo.read_fields(xml, ("Series",))["Series"] if xml else ""
        except (*archive.UNREADABLE, ET.ParseError, LookupError, OSError) as e:
            # A file this machine cannot read (permissions, held open, gone) says nothing; a failing share ends the run
            if isinstance(e, OSError) and (errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS):
                raise
            continue
        if name:
            found.add(name)
    return found.pop() if len(found) == 1 else None


def _by_names(ctx: workflow.Context, subject, titles, kavita_series):
    """Step 3 over each title in turn: the first with exactly 1 strict hit is the series; else the first near or
    double hit holds the subject's series (needs_human); else a new series (pending: its row and link are stored, its
    downloads not taken). A held or new series is in scope when any title is; its row keeps a second title (a
    folder's ComicInfo Series) as its alt_name."""
    cfg, tried, held = ctx.cfg, [], None
    for title in titles:
        tried.append(title)
        strict = [s for s in kavita_series.rows() if _strict_key(title) and _strict_key(title) in _strict_keys(s)]
        if len(strict) == 1:
            found = strict[0]
            if not workflow.in_scope(cfg, series_names(found), found["libraryId"]):
                return OUT_OF_SCOPE
            # A published new series Kavita lists now is matched first (it may be this subject's second source); one
            # it lists under a name a still unmatched new series is published as is held, not made a second series
            if _strict_keys(found) & kavita_link.attach_all(ctx, kavita_series.rows()):
                reason = (
                    f"{subject.label} {title!r} matches Kavita series {found['name']} ({found['id']}), named as a"
                    " published new series not found in Kavita yet"
                )
                return _store_held_series(ctx.conn, subject, reason, tried[1] if len(tried) > 1 else None)
            return _store_kavita_series(ctx, subject, found)
        near = [s for s in kavita_series.rows() if _near(title, s)]
        if held is None and (strict or near):
            kind = "matches 2 or more Kavita series" if strict else "nearly matches"
            candidates = ", ".join(f"{s['name']} (Kavita {s['id']})" for s in strict or near)
            held = f"{subject.label} {title!r} {kind}: {candidates}"
    if not workflow.in_scope(cfg, tried, None):
        return OUT_OF_SCOPE
    alt_name = tried[1] if len(tried) > 1 else None
    if held:
        return _store_held_series(ctx.conn, subject, held, alt_name)
    _store_new_series(ctx.conn, subject, alt_name)
    return NOT_ADMITTED


def _near(title, kseries):
    """A near hit (D7.18): a Kavita name equal to the title under the lenient key, or within search.close's fuzzy
    ratio of it."""
    key = names.key(title)
    return bool(key) and any(key == names.key(n) or search.close(title, n) for n in series_names(kseries))


def _taken(ctx: workflow.Context, row):
    """The linked row of a manga or folder, with its folder; NotTaken for a pending new series (a held one is
    returned: its chapters are recorded and held)."""
    if row["origin"] == "new" and row["admitted_at"] is None and row["status"] == "ok":
        return NOT_ADMITTED
    return _with_folder(ctx, row)


def source_ids(manga):
    """The (source, id) pairs a manga carries (SPEC "Source ids": its trackers and its MangaDex URL)."""
    return metadata_ids.suwayomi_ids(manga)


def _by_source_id(conn, pairs):
    """The series rows holding any of the ids, from any origin; 2 or more is a double hit."""
    if not pairs:
        return []
    return conn.execute(
        """SELECT DISTINCT s.* FROM series_source_id i JOIN series s ON s.id = i.series_id
        WHERE (i.source, i.source_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))
        ORDER BY s.id""",
        ([p[0] for p in pairs], [p[1] for p in pairs]),
    ).fetchall()


def _strict_key(name):
    return normalize(name)


def _strict_keys(series):
    return {normalize(n) for n in series_names(series)}


def _store_kavita_series(ctx: workflow.Context, subject, found):
    """The series row of a strict Kavita hit: the row already holding its Kavita id (another source's manga of the
    same series), else a new one with its names and folder; a series with no usable folder is needs_human."""
    conn = ctx.conn
    existing = conn.execute("SELECT id FROM series WHERE kavita_series_id = %s", (found["id"],)).fetchone()
    folder, problem = _folder(ctx, found["id"]) if existing is None else (None, None)
    with db.transaction(conn):
        if existing is None:
            series_id = db.one_row(
                conn,
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
            )[0]
            if problem:
                workflow.needs_human(conn, "series", series_id, problem)
        else:
            series_id = existing[0]
        subject.write_link(conn, series_id)
    return _with_folder(ctx, db.one_row(conn, "SELECT * FROM series WHERE id = %s", (series_id,)))


def _with_folder(ctx: workflow.Context, row):
    """The row with its folder: a Kavita series' row the library listing stored has none until a download needs it
    (SPEC "Metadata checks"); one Kavita gives no usable folder is needs_human."""
    if row["kavita_series_id"] is None or row["folder"] is not None:
        return row
    folder, problem = _folder(ctx, row["kavita_series_id"])
    with db.transaction(ctx.conn):
        if folder is not None:
            ctx.conn.execute("UPDATE series SET folder = %s WHERE id = %s", (folder, row["id"]))
        else:
            workflow.needs_human(ctx.conn, "series", row["id"], problem)
    return ctx.conn.execute("SELECT * FROM series WHERE id = %s", (row["id"],)).fetchone()


def _folder(ctx: workflow.Context, kavita_series_id):
    """(the series folder in stored form, None) or (None, why it has none)."""
    try:
        return kavita_link.library_folder(ctx.cfg, ctx.kavita.series_folder(kavita_series_id)), None
    except (kavita_client.FolderError, config.PathError) as e:
        return None, str(e)


def _store_new_series(conn, subject, alt_name):
    """A pending new series' row (origin new, status ok, not admitted) and the subject's link to it."""
    with db.transaction(conn):
        series_id = db.one_row(
            conn,
            "INSERT INTO series (name, alt_name, origin) VALUES (%s, %s, 'new') RETURNING id",
            (subject.held_name, alt_name),
        )[0]
        subject.write_link(conn, series_id)
    log.info(
        "%s: new series %s (%s): admitted after its first metadata check", subject.label, series_id, subject.held_name
    )


def _store_held_series(conn, subject, reason, alt_name=None):
    """A needs_human series row for a manga or folder a person must link (no Kavita id: in scope as not yet known)."""
    with db.transaction(conn):
        series_id = db.one_row(
            conn,
            "INSERT INTO series (name, alt_name, origin) VALUES (%s, %s, 'new') RETURNING id",
            (subject.held_name, alt_name),
        )[0]
        workflow.needs_human(
            conn,
            "series",
            series_id,
            f"{reason} -> {subject.command} KAVITA_SERIES_ID, or series new {series_id} [--name NAME]",
        )
        subject.write_link(conn, series_id)
    return conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()


def _names(folder):
    """The entry names of a folder, spelt as listed."""
    return {p.name for p in folder.iterdir()}


def folder_key(cfg, suwayomi, text):
    """A hand-placed folder named on the command line ("SOURCE_FOLDER/TITLE_FOLDER") as stored; workflow.Refused unless
    it is 2 folder names that exist under <downloads>/mangas, spelt as listed (a case-insensitive share would find
    another spelling, which discovery never keys by), and no library manga maps to it (discovery would never read the
    link)."""
    parts = text.split("/")
    if len(parts) != FOLDER_LINK_PARTS or any(part in ("", ".", "..") or "\\" in part for part in parts):
        msg = f"--folder takes SOURCE_FOLDER/TITLE_FOLDER under the downloads' mangas folder: {text}"
        raise workflow.Refused(msg)
    root = config.abs_path(cfg["paths"]["downloads"]) / suwayomi_client.MANGAS_FOLDER
    if not ((root / parts[0]).is_dir() and parts[0] in _names(root) and parts[1] in _names(root / parts[0])):
        msg = f"no download folder {parts[1]!r} in {root / parts[0]} (names are exact)"
        raise workflow.Refused(msg)
    if any(suwayomi_client.download_folders(m) == tuple(parts) for m in suwayomi.library_manga()):
        msg = f"a library manga maps to {text}: link that manga (series link MANGA_ID KAVITA_SERIES_ID)"
        raise workflow.Refused(msg)
    return text


def link_subject(cfg, suwayomi, *, manga_id=None, folder=None):
    """What `series link` links: the manga (manga_id) or the hand-placed folder (folder, as the command line gives it;
    folder_key). workflow.Refused when the manga or the folder does not exist."""
    if (manga_id is None) == (folder is None):
        msg = "link takes a manga_id or a folder"
        raise ValueError(msg)
    if folder is not None:
        return _folder_subject(folder_key(cfg, suwayomi, folder))
    manga = suwayomi.manga(manga_id)
    if manga is None:
        msg = f"Suwayomi holds no manga {manga_id}"
        raise workflow.Refused(msg)
    return _manga_subject(manga)


def link(cfg, conn, kavita, kavita_series_id, subject):
    """`series link` (SPEC "Series identification"): the downloads of the subject (a manga or a hand-placed folder:
    link_subject) belong to the Kavita series. The row holding that Kavita id, else the subject's held row, else a
    new row gets its Kavita id, names and folder (refreshed when run again); the subject is linked to it. A held row of
    the subject (no Kavita id) is merged into the Kavita series' row: its chapters and links move there and it is
    deleted. workflow.Refused when the series does not exist, is outside kavita.library_id or has no folder, the
    subject's row is another Kavita series' row that holds chapters, or the subject's row is an admitted new series (its
    rows, published files included, are its own). needs_human is left for `retry --series` to clear. Returns the series
    id."""
    found = _kavita_series(cfg, kavita, kavita_series_id)
    try:
        series_dir = kavita_link.library_folder(cfg, kavita.series_folder(kavita_series_id))
    except (kavita_client.FolderError, config.PathError) as e:
        raise workflow.Refused(str(e)) from None
    with db.transaction(conn):
        series_id = _link_row(conn, subject, kavita_series_id)
        values = (
            found["name"],
            kavita_series_id,
            found["libraryId"],
            series_dir,
            found.get("localizedName") or None,
            found.get("originalName") or None,
        )
        if series_id is None:
            series_id = db.one_row(
                conn,
                """INSERT INTO series (name, kavita_series_id, kavita_library_id, folder, localized_name, original_name,
                    origin)
                VALUES (%s, %s, %s, %s, %s, %s, 'kavita') RETURNING id""",
                values,
            )[0]
        else:
            conn.execute(
                """UPDATE series SET name = %s, kavita_series_id = %s, kavita_library_id = %s, folder = %s,
                    localized_name = %s, original_name = %s, origin = 'kavita'
                WHERE id = %s""",
                (*values, series_id),
            )
        subject.write_link(conn, series_id)
    log.info("%s linked to series %s (Kavita %s)", subject.label, series_id, kavita_series_id)
    return series_id


def _kavita_series(cfg, kavita, kavita_series_id):
    """The Kavita series link names; workflow.Refused when Kavita holds none or it is outside kavita.library_id."""
    found = kavita.series(kavita_series_id)
    if found is None:
        msg = f"Kavita holds no series {kavita_series_id}"
        raise workflow.Refused(msg)
    if found["libraryId"] != cfg["kavita"]["library_id"]:
        msg = (
            f"Kavita series {kavita_series_id} is in library {found['libraryId']}, not kavita.library_id "
            f"{cfg['kavita']['library_id']}"
        )
        raise workflow.Refused(msg)
    return found


def _link_row(conn, subject, kavita_series_id):
    """The id of the series row the link takes (inside link's transaction): the row holding the Kavita id (a held row
    of the subject merged into it), else the subject's row, else None (a new row). workflow.Refused for an admitted new
    series, or another Kavita series' row that holds chapters."""
    by_kavita = conn.execute("SELECT id FROM series WHERE kavita_series_id = %s", (kavita_series_id,)).fetchone()
    by_manga = subject.linked(conn, sql.SQL("s.id, s.kavita_series_id, s.origin, s.admitted_at"))
    if by_manga and by_manga["origin"] == "new" and by_manga["admitted_at"] is not None:
        msg = (
            f"{subject.label} is linked to series {by_manga['id']}, a new series admitted under its own name: "
            f"it is not moved to another series"
        )
        raise workflow.Refused(msg)
    other_kavita = by_manga and by_manga["kavita_series_id"] not in (None, kavita_series_id)
    if other_kavita and conn.execute("SELECT 1 FROM chapter WHERE series_id = %s", (by_manga["id"],)).fetchone():
        msg = (
            f"{subject.label} is linked to series {by_manga['id']} (Kavita {by_manga['kavita_series_id']}), "
            f"which has chapters: they are not moved to another Kavita series"
        )
        raise workflow.Refused(msg)
    if by_kavita and by_manga and by_manga["kavita_series_id"] is None:
        _merge_held(conn, by_manga["id"], by_kavita[0])
    if other_kavita:
        by_manga = None  # its row stays the other series'; the manga or folder alone moves
    return (by_kavita or by_manga or [None])[0]


def _merge_held(conn, held_id, series_id):
    """Move a held row's chapters (workflow.move_chapters), handled chapters, manga and folder links and the ids a
    person set to the series row, and delete it (inside link's transaction); its other Phase 7 rows go with it
    (ON DELETE CASCADE). A held row is never admitted, so it publishes nothing; a layout or calibration computed for
    it is dropped. A human id the series row already holds is kept once; a different one is then a disagreement
    (SPEC "Source ids")."""
    workflow.move_chapters(conn, held_id, series_id)
    conn.execute("UPDATE handled_chapter SET series_id = %s WHERE series_id = %s", (series_id, held_id))
    conn.execute("UPDATE suwayomi_manga SET series_id = %s WHERE series_id = %s", (series_id, held_id))
    conn.execute("UPDATE download_folder SET series_id = %s WHERE series_id = %s", (series_id, held_id))
    conn.execute(
        """INSERT INTO series_source_id (series_id, source, origin, source_id)
        SELECT %s, source, origin, source_id FROM series_source_id WHERE series_id = %s AND origin = 'human'
        ON CONFLICT DO NOTHING""",
        (series_id, held_id),
    )
    conn.execute("DELETE FROM series_conversion WHERE series_id = %s", (held_id,))
    conn.execute("DELETE FROM series_calibration WHERE series_id = %s", (held_id,))
    conn.execute("DELETE FROM series WHERE id = %s", (held_id,))
    log.info("held series %s merged into series %s", held_id, series_id)
