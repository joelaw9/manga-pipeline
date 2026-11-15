"""Series identification (SPEC "Series identification"): which series a Suwayomi manga's downloads, or a hand-placed
download folder's, belong to.

`series_for` (a manga) and `series_for_folder` (a folder no library manga maps to) work through the order the spec
gives and write what they settle (a series row and the manga's or folder's link) in 1 transaction; a match outside
series.only, or a new series, writes nothing. `link` is `series link`.
"""

import logging
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

from manga_pipeline import archive, comicinfo, config, db, names, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
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


@dataclass(frozen=True)
class _Subject:
    """What is linked to a series: a Suwayomi manga or a hand-placed download folder."""

    label: str  # how messages name it
    held_name: str  # a held row's series name
    command: str  # `series link` naming it, before KAVITA_SERIES_ID
    find: str  # SQL of its linked series' row: 1 parameter, the key; {columns} picks the series columns
    key: object
    write_link: Callable  # (conn, series_id): store its link, inside the caller's transaction

    def linked(self, conn, columns="s.*"):
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
        find="SELECT {columns} FROM suwayomi_manga m JOIN series s ON s.id = m.series_id WHERE m.manga_id = %s",
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
        find="SELECT {columns} FROM download_folder f JOIN series s ON s.id = f.series_id WHERE f.folder = %s",
        key=folder,
        write_link=write,
    )


def series_for(ctx, manga, kavita_series):
    """The series row of a library manga, or NotTaken. A series row may be needs_human (near or double hit, no
    usable folder): its chapters are recorded and held."""
    conn, cfg = ctx.conn, ctx.cfg
    subject = _manga_subject(manga)
    row = subject.linked(conn)
    by_source_id = row is None
    if by_source_id:
        row = _by_source_id(conn, source_ids(manga))
    if row is not None:
        if not workflow.in_scope(cfg, row["name"], row["kavita_library_id"]):
            return OUT_OF_SCOPE
        if by_source_id:
            with db.transaction(conn):
                subject.write_link(conn, row["id"])
        return row
    return _by_names(ctx, subject, [manga["title"]], kavita_series)


def series_for_folder(ctx, folder, paths, kavita_series):
    """The series row of a hand-placed download folder ("<source folder>/<title folder>", holding paths), or NotTaken:
    its stored link, else its title folder's name, else (read only then) the ComicInfo Series its files agree on."""
    subject = _folder_subject(folder)
    row = subject.linked(ctx.conn)
    if row is not None:
        return row if workflow.in_scope(ctx.cfg, row["name"], row["kavita_library_id"]) else OUT_OF_SCOPE
    title = subject.held_name

    def titles():
        yield title
        series = comicinfo_series(paths)
        if series and series != title:
            yield series

    return _by_names(ctx, subject, titles(), kavita_series)


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
            if isinstance(e, OSError) and (workflow.dropped_share(e) or e.errno in workflow.SHARE_ERRNOS):
                raise
            continue
        if name:
            found.add(name)
    return found.pop() if len(found) == 1 else None


def _by_names(ctx, subject, titles, kavita_series):
    """Step 3 over each title in turn: the first with exactly 1 strict hit is the series; else the first near or
    double hit holds the subject's series (needs_human); else a new series. A held or new series is in scope when
    any title is."""
    cfg, tried, held = ctx.cfg, [], None
    for title in titles:
        tried.append(title)
        strict = [s for s in kavita_series.rows() if _strict_key(title) and _strict_key(title) in _strict_keys(s)]
        if len(strict) == 1:
            found = strict[0]
            if not workflow.in_scope(cfg, found["name"], found["libraryId"]):
                return OUT_OF_SCOPE
            return _store_kavita_series(ctx, subject, found)
        near = [
            s
            for s in kavita_series.rows()
            if names.key(title) and names.key(title) in {names.key(n) for n in series_names(s)}
        ]
        if held is None and (strict or near):
            kind = "matches 2 or more Kavita series" if strict else "nearly matches"
            candidates = ", ".join(f"{s['name']} (Kavita {s['id']})" for s in strict or near)
            held = f"{subject.label} {title!r} {kind}: {candidates}"
    if not any(workflow.in_scope(cfg, t, None) for t in tried):
        return OUT_OF_SCOPE
    if held:
        return _store_held_series(ctx.conn, subject, held)
    shown = " / ".join(repr(t) for t in tried)
    return NotTaken(f"new series {shown}: no Kavita series matches (new series come in a later phase)", listed=True)


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


def _store_kavita_series(ctx, subject, found):
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
        subject.write_link(conn, series_id)
    return conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()


def _folder(ctx, kavita_series_id):
    """(the series folder in stored form, None) or (None, why it has none)."""
    try:
        return library_folder(ctx.cfg, ctx.kavita.series_folder(kavita_series_id)), None
    except (kavita_client.FolderError, config.PathError) as e:
        return None, str(e)


def library_folder(cfg, folder):
    """A Kavita series folder in stored form; PathError unless it lies under paths.library (SPEC "State")."""
    stored = config.to_stored(cfg, folder)
    if stored.split("/", 1)[0] != "library":
        raise config.PathError(f"the series folder {folder} is not under paths.library")
    return stored


def _store_held_series(conn, subject, reason):
    """A needs_human series row for a manga or folder a person must link (no Kavita id: in scope as not yet known)."""
    with db.transaction(conn):
        series_id = conn.execute(
            "INSERT INTO series (name, origin) VALUES (%s, 'new') RETURNING id", (subject.held_name,)
        ).fetchone()[0]
        workflow.needs_human(conn, "series", series_id, f"{reason} -> {subject.command} KAVITA_SERIES_ID")
        subject.write_link(conn, series_id)
    return conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()


def folder_key(cfg, suwayomi, text):
    """A hand-placed folder named on the command line ("SOURCE_FOLDER/TITLE_FOLDER") as stored; workflow.Refused unless
    it is 2 folder names that exist under <downloads>/mangas, spelt as listed (a case-insensitive share would find
    another spelling, which discovery never keys by), and no library manga maps to it (discovery would never read the
    link)."""
    parts = text.split("/")
    if len(parts) != 2 or any(part in ("", ".", "..") or "\\" in part for part in parts):
        raise workflow.Refused(f"--folder takes SOURCE_FOLDER/TITLE_FOLDER under the downloads' mangas folder: {text}")
    root = config.abs_path(cfg["paths"]["downloads"]) / suwayomi_client.MANGAS_FOLDER
    if not ((root / parts[0]).is_dir() and parts[0] in os.listdir(root) and parts[1] in os.listdir(root / parts[0])):
        raise workflow.Refused(f"no download folder {parts[1]!r} in {root / parts[0]} (names are exact)")
    if any(suwayomi_client.download_folders(m) == tuple(parts) for m in suwayomi.library_manga()):
        raise workflow.Refused(
            f"a library manga maps to {text}: link that manga (series link MANGA_ID KAVITA_SERIES_ID)"
        )
    return text


def link(cfg, conn, suwayomi, kavita, kavita_series_id, *, manga_id=None, folder=None):
    """`series link` (SPEC "Series identification"): the downloads of the manga (manga_id) or of the hand-placed folder
    (folder, as the command line gives it) belong to the Kavita series. The row holding that Kavita id, else the
    subject's held row, else a new row gets its Kavita id, names and folder (refreshed when run again); the subject is
    linked to it. A held row of the subject (no Kavita id) is merged into the Kavita series' row: its chapters and links
    move there and it is deleted. workflow.Refused when the manga, the folder or the series does not exist, the series
    is outside kavita.library_id or has no folder, or the subject's row is another Kavita series' row that holds
    chapters. needs_human is left for `retry --series` to clear. Returns the series id."""
    if (manga_id is None) == (folder is None):
        raise ValueError("link takes a manga_id or a folder")
    if folder is not None:
        subject = _folder_subject(folder_key(cfg, suwayomi, folder))
    else:
        manga = suwayomi.manga(manga_id)
        if manga is None:
            raise workflow.Refused(f"Suwayomi holds no manga {manga_id}")
        subject = _manga_subject(manga)
    found = kavita.series(kavita_series_id)
    if found is None:
        raise workflow.Refused(f"Kavita holds no series {kavita_series_id}")
    if found["libraryId"] != cfg["kavita"]["library_id"]:
        raise workflow.Refused(
            f"Kavita series {kavita_series_id} is in library {found['libraryId']}, not kavita.library_id "
            f"{cfg['kavita']['library_id']}"
        )
    try:
        series_dir = library_folder(cfg, kavita.series_folder(kavita_series_id))
    except (kavita_client.FolderError, config.PathError) as e:
        raise workflow.Refused(str(e)) from None
    with db.transaction(conn):
        by_kavita = conn.execute("SELECT id FROM series WHERE kavita_series_id = %s", (kavita_series_id,)).fetchone()
        by_manga = subject.linked(conn, "s.id, s.kavita_series_id")
        other_kavita = by_manga and by_manga["kavita_series_id"] not in (None, kavita_series_id)
        if other_kavita and conn.execute("SELECT 1 FROM chapter WHERE series_id = %s", (by_manga["id"],)).fetchone():
            raise workflow.Refused(
                f"{subject.label} is linked to series {by_manga['id']} (Kavita {by_manga['kavita_series_id']}), "
                f"which has chapters: they are not moved to another Kavita series"
            )
        if by_kavita and by_manga and by_manga["kavita_series_id"] is None:
            _merge_held(conn, by_manga["id"], by_kavita[0])
        if other_kavita:
            by_manga = None  # its row stays the other series'; the manga or folder alone moves
        series_id = (by_kavita or by_manga or [None])[0]
        values = (
            found["name"],
            kavita_series_id,
            found["libraryId"],
            series_dir,
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
        subject.write_link(conn, series_id)
    log.info("%s linked to series %s (Kavita %s)", subject.label, series_id, kavita_series_id)
    return series_id


def _merge_held(conn, held_id, series_id):
    """Move a held row's chapters (workflow.move_chapters) and manga links to the series row, and delete it (inside
    link's transaction). A held row publishes nothing (it has no ComicInfo Series name), so it has no published or
    handled chapter; a layout or calibration computed for it is dropped."""
    workflow.move_chapters(conn, held_id, series_id)
    conn.execute("UPDATE suwayomi_manga SET series_id = %s WHERE series_id = %s", (series_id, held_id))
    conn.execute("UPDATE download_folder SET series_id = %s WHERE series_id = %s", (series_id, held_id))
    conn.execute("DELETE FROM series_conversion WHERE series_id = %s", (held_id,))
    conn.execute("DELETE FROM series_calibration WHERE series_id = %s", (held_id,))
    conn.execute("DELETE FROM series WHERE id = %s", (held_id,))
    log.info("held series %s merged into series %s", held_id, series_id)
