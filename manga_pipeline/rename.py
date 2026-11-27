"""`series names` and `series rename` (SPEC "Retroactive updates", Series names).

`read_db` reads the run's series from the database (in the caller's snapshot); `check` then reads Kavita, the override
files and every file and decides each series' plan, writing nothing. `apply` renames each planned series in turn:
Kavita's name first (the files' old name kept as its localized name, so both match 1 series), the files, the folder,
then 1 transaction (when no file changes, Kavita's names come before the folder). `wait_for_kavita` requests 1 library
scan and waits until Kavita follows each moved folder; `finish_names` then gives each renamed series its localized
name (Kavita refuses to drop the old name while it lists the files in the folder that name named; a series whose
folder did not move gets it before its transaction). `waiting` is status' list, from the stored names.
"""

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from manga_pipeline import archive, comicinfo, config, db, errors, names, retention, rewrite, volumes, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients.kavita import FolderError, Kavita, ReplyError
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.db import sql
from manga_pipeline.metadata import admit, merge, series_name
from manga_pipeline.metadata.records import SeriesRecords
from manga_pipeline.replies.kavita import Series

log = logging.getLogger(__name__)
RESCAN_MINUTES = 10  # a scan requested while one runs does not work: request again this often
POLL_SECONDS = 30
# A folder rename the share denies right after the folder's files were rewritten is tried again once, after this
FOLDER_RETRY_SECONDS = 10
# A series a rename may act on: status ok, Kavita holds it, a folder, its check completed, a baseline or admitted new.
ELIGIBLE = sql.SQL("""(s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND s.folder IS NOT NULL
    AND (s.baseline_at IS NOT NULL OR s.origin = 'new')
    AND EXISTS (SELECT 1 FROM series_check k WHERE k.series_id = s.id AND k.data_version > 0))""")
# Archive kinds Kavita reads as chapters (name endings, any case): a file of these in a series folder must have its
# row, as it is rewritten
ARCHIVES = (".cbz", ".cbr", ".cb7", ".cbt", ".zip", ".rar", ".7z", ".7zip", ".tar.gz")
# What a Kavita write can raise for 1 series: the series is listed and the run goes on.
KAVITA_ERRORS = (http.HttpError, ReplyError, workflow.Unreachable)
NO_CHOICE = series_name.Chosen("", "", "")  # a series not decided yet
NOTHING_DUE = series_name.Due(files=False, kavita_name=False, localized=False, folder=False)


class RenameError(Exception):
    """A rename left something undone: the message lists what (exit 1)."""


@dataclass(frozen=True)
class File:
    """A published file of a series: its stored path and whether its row records a size."""

    path: str
    sized: bool


@dataclass(frozen=True)
class Planned:
    """A series' rename: what is due, where, and what stops it (problem; a clash holds the series)."""

    series: db.Row
    records: SeriesRecords
    files: tuple[File, ...]
    override: str | None = None
    chosen: series_name.Chosen = NO_CHOICE
    due: series_name.Due = NOTHING_DUE
    kavita: Series | None = None
    folder: str = ""  # the stored folder the files are in now (the target, after a stop past the folder rename)
    target: str = ""  # the stored folder the series gets
    rewrite: tuple[str, ...] = ()  # stored paths (under folder) of the files to rewrite
    problem: str | None = None
    clash: bool = False

    @property
    def name_changes(self) -> bool:
        """Return whether the series' name changes (its files' or Kavita's)."""
        return self.due.files or self.due.kavita_name

    def kavita_name(self) -> str:
        """Return Kavita's name after the rename: the rule's when the name changes, else Kavita's as it is."""
        return self.chosen.name if self.name_changes else (self.kavita or {}).get("name") or self.chosen.name

    def localized(self) -> str:
        """Return Kavita's localized name after the rename: the romanized name; else "" when the name changes."""
        if self.chosen.romaji or self.name_changes:
            return self.chosen.romaji
        return (self.kavita or {}).get("localizedName") or ""

    def line(self) -> str:
        """Return the series' plan as printed."""
        head = f"series {self.series['id']} ({self.series['comicinfo_series'] or self.series['name']})"
        if self.problem:
            return f"{head}: {'name taken' if self.clash else 'left'}: {self.problem}"
        acts: list[str] = []
        if self.due.files:
            acts.append(f"name -> {self.chosen.name!r} ({self.chosen.source}), {len(self.rewrite)} files rewritten")
        elif self.due.kavita_name:
            acts.append(f"Kavita name -> {self.chosen.name!r}")
        if self.due.localized or self.name_changes:
            acts.append(f"localized name -> {self.localized()!r}")
        if self.due.folder:
            acts.append(f"folder -> {self.target}")
        return f"{head}: {'; '.join(acts)}"


@dataclass(frozen=True)
class Run:
    """The run's series as read from the database, and the names other series hold there.

    new_holders: the names of the new series Kavita does not hold yet; stored: the stored names of every series of
    the library (status' clash test; `check` uses Kavita's listing instead).
    """

    planned: list[Planned]
    new_holders: series_name.Holders
    stored: series_name.Holders


@dataclass
class Outcome:
    """What `apply` did so far.

    The series renamed, those whose folder moved (waited for), those whose localized name waits for Kavita to follow
    their folder (finish_names), the lines of the rest.
    """

    renamed: list[Planned] = field(default_factory=list[Planned])
    moved: list[Planned] = field(default_factory=list[Planned])
    localized: list[Planned] = field(default_factory=list[Planned])
    left: list[str] = field(default_factory=list[str])

    def leave(self, line: str) -> None:
        """Record (and log) a series the run did not rename."""
        log.warning("%s", line)
        self.left.append(line)


def read_db(
    cfg: config.Config, conn: db.Connection, series_ids: Sequence[int] | None, *, for_status: bool = False
) -> Run:
    """Read the run's series (series_ids; None: every eligible series in scope): rows, records, files.

    for_status: no files; the library's stored names instead (status' clash test).

    workflow.Refused for an id of no series in scope; an asked-for series that may not be renamed gets its problem.
    """
    where = sql.SQL("TRUE") if series_ids is None else sql.SQL("s.id = ANY(%(ids)s)")
    params = {**workflow.scope_params(cfg), "ids": list(series_ids or [])}
    rows = conn.execute(
        sql.SQL("""SELECT s.*, {eligible} AS eligible, {busy} AS busy FROM series s
        WHERE {where} AND {scope} ORDER BY s.id""").format(
            eligible=ELIGIBLE, busy=rewrite.RENAME_BUSY.format(s=sql.Identifier("s")), where=where, scope=workflow.SCOPE
        ),
        params,
    ).fetchall()
    missing = sorted(set(series_ids or []) - {r["id"] for r in rows})
    if missing:
        msg = f"no such series in scope: {', '.join(map(str, missing))}"
        raise workflow.Refused(msg)
    by_series: dict[int, list[File]] = {}
    if not for_status:
        for f in conn.execute(
            sql.SQL("""SELECT p.series_id, p.library_path, p.size IS NOT NULL AS sized FROM published_file p
            JOIN series s ON s.id = p.series_id WHERE {where} AND {scope} ORDER BY p.library_path""").format(
                where=where, scope=workflow.SCOPE
            ),
            params,
        ):
            by_series.setdefault(f["series_id"], []).append(File(f["library_path"], f["sized"]))
    fetched: dict[int, list[db.Row]] = {}
    for r in conn.execute(
        sql.SQL("""SELECT f.series_id, f.source, f.kind, f.payload FROM fetched f JOIN series s ON s.id = f.series_id
        WHERE {where} AND {scope}""").format(where=where, scope=workflow.SCOPE),
        params,
    ):
        fetched.setdefault(r["series_id"], []).append(r)
    planned: list[Planned] = []
    for row in rows:
        if series_ids is None and not row["eligible"]:
            continue
        one = Planned(row, merge.records_of(fetched.get(row["id"], [])), tuple(by_series.get(row["id"], [])))
        if not row["eligible"]:
            one = replace(one, problem="not eligible: its status, Kavita id, folder, check or baseline")
        elif row["busy"]:
            one = replace(one, problem="busy: a chapter claimed or at publish or confirm, or a file with a change due")
        planned.append(one)
    stored = (
        conn.execute(
            """SELECT kavita_series_id, name, localized_name, original_name FROM series
            WHERE kavita_series_id IS NOT NULL AND kavita_library_id = %(scope_library)s""",
            params,
        ).fetchall()
        if for_status
        else []
    )
    return Run(
        planned,
        admit.new_series_index(conn),
        series_name.index(
            (
                series_name.kavita_key(r["kavita_series_id"]),
                (r["name"], r["localized_name"], r["original_name"]),
                f"Kavita series {r['kavita_series_id']} ({r['name']}) has that name",
            )
            for r in stored
        ),
    )


def with_override(cfg: config.Config, one: Planned) -> Planned:
    """Return the plan with its override file's name, or why it cannot be read (a share read: outside any snapshot)."""
    if one.problem:
        return one
    try:
        return replace(
            one,
            override=volumes.override_name(config.abs_path(cfg["paths"]["naming_overrides"]), one.series["rules_key"]),
        )
    except volumes.OverrideError as e:
        return replace(one, problem=str(e))
    except OSError as e:
        if errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS:
            raise  # the share's, not the file's: the caller's to report
        return replace(one, problem=f"its override file cannot be read: {e}")


def _own_keys(series: db.Row) -> set[str]:
    """Return the holder keys of the series itself."""
    return {series_name.series_key(series["id"]), series_name.kavita_key(series["kavita_series_id"])}


def _names(
    one: Planned, own: series_name.Own, kavita_localized: str | None, holders: Sequence[series_name.Holders]
) -> Planned:
    """Return the plan with the rule's names (a taken romanized name left out) and what is due."""
    chosen = series_name.chosen(own, one.records, one.override or one.series["admit_name"])
    if chosen.romaji and series_name.taken(chosen.romaji, holders, _own_keys(one.series)):
        log.info("series %s: its romanized name %r is taken: left out", one.series["id"], chosen.romaji)
        chosen = replace(chosen, romaji="")
    return replace(one, chosen=chosen, due=series_name.due(own, kavita_localized, one.series["folder"], chosen))


def waiting(cfg: config.Config, run: Run) -> list[str]:
    """Return status' lines of the series whose names are due, from the stored names (no Kavita or file read)."""
    lines: list[str] = []
    for planned in run.planned:
        if planned.problem:  # busy: renamed once it is not
            continue
        try:
            one = with_override(cfg, planned)
        except OSError as e:  # the share: 1 line for the list, not a failed status
            return [*lines, f"  override files cannot be read: {e}"]
        if one.problem:
            lines.append(f"  series {one.series['id']}: {one.problem}")
            continue
        s = one.series
        own = series_name.Own(s["name"], s["localized_name"], s["original_name"], s["comicinfo_series"])
        one = _names(one, own, s["localized_name"], (run.new_holders, run.stored))
        if one.due.any():
            due = one.due
            what = [w for w, on in (("files", due.files), ("Kavita name", due.kavita_name)) if on]
            what += [w for w, on in (("localized name", due.localized), ("folder", due.folder)) if on]
            lines.append(f"  series {s['id']} ({s['comicinfo_series']}): {one.chosen.name!r}: {', '.join(what)}")
    return lines


def check(ctx: workflow.Context, run: Run) -> list[Planned]:
    """Return each series' plan: Kavita's names, the rule, the clash test, the folder and every file read and checked.

    A share error ends the run (raised); a file that cannot be read or rebuilt is the series' problem.
    """
    listing = {s["id"]: s for s in ctx.kavita.series_in_library(ctx.cfg["kavita"]["library_id"])}
    taken_in_run: series_name.Holders = {}
    holders = (run.new_holders, admit.kavita_index(listing.values()), taken_in_run)
    out: list[Planned] = []
    for planned in run.planned:
        one = with_override(ctx.cfg, planned)
        if not one.problem:
            one = _decide(ctx.cfg, one, listing, holders)
        if not one.problem:
            sid = one.series["id"]
            mine = series_name.index(
                [
                    (
                        series_name.series_key(sid),
                        (one.chosen.name, one.chosen.romaji),
                        f"series {sid} of this run gets that name",
                    )
                ]
            )
            for key, more in mine.items():
                taken_in_run.setdefault(key, []).extend(more)
        out.append(one)
    return out


def _decide(
    cfg: config.Config, one: Planned, listing: dict[int, Series], holders: Sequence[series_name.Holders]
) -> Planned:
    """Return 1 series' plan (see check)."""
    series = one.series
    kavita = listing.get(series["kavita_series_id"])
    if kavita is None:
        return replace(one, problem=f"Kavita does not list series {series['kavita_series_id']}")
    own = series_name.Own(
        kavita["name"], kavita.get("localizedName"), kavita.get("originalName"), series["comicinfo_series"]
    )
    one = _names(replace(one, kavita=kavita, folder=series["folder"]), own, kavita.get("localizedName"), holders)
    if not one.due.any():
        return one
    if one.name_changes and (why := series_name.taken(one.chosen.name, holders, _own_keys(series))):
        return replace(one, problem=f"its name {one.chosen.name!r} is not free: {why}", clash=True)
    try:
        one = _folders(cfg, one)
        return replace(one, rewrite=tuple(_files_to_rewrite(cfg, one)))
    except _LeftError as e:
        return replace(one, problem=str(e))


class _LeftError(Exception):
    """The series is left as it is: the message says why."""


def _folders(cfg: config.Config, one: Planned) -> Planned:
    """Return the plan with its folder (where its files are now) and target; _LeftError when they do not fit.

    The parent folder is listed once and names compared exactly, so a case-only rename is seen on a share that ignores
    case too. After a stop past the folder rename, the old folder is gone and the target holds every published name.
    When files are rewritten, the folder must hold no archive (ARCHIVES) the series has no row for: it would keep the
    old name.
    """
    stored = one.series["folder"]
    parent = PurePosixPath(stored).parent
    target = str(parent / names.folder_name(one.chosen.name)) if one.due.folder else stored
    old, new = config.to_local(cfg, stored), config.to_local(cfg, target)
    if not errors.exists(old.parent):
        msg = f"its parent folder {parent} is missing"
        raise _LeftError(msg)
    there = {p.name for p in old.parent.iterdir()}
    published = {PurePosixPath(f.path).name for f in one.files}
    if old.name not in there:
        if target != stored and new.name in there and published and published <= {p.name for p in new.iterdir()}:
            one = replace(one, folder=target, target=target)
        else:
            msg = f"its folder {stored} is missing"
            raise _LeftError(msg)
    elif target != stored:
        others = [n for n in there if n != old.name]  # a listing holds the names as they are: no case-only twin
        if taken := series_name.folder_taken(one.chosen.name, others):
            msg = f"the folder name is taken by {parent / taken}"
            raise _LeftError(msg)
        one = replace(one, target=target)
    else:
        one = replace(one, target=target)
    if one.due.files:
        now = config.to_local(cfg, one.folder)
        stray = sorted(p.name for p in now.iterdir() if p.name.lower().endswith(ARCHIVES) and p.name not in published)
        if stray:
            msg = f"{one.folder} holds files the series has no row for: {', '.join(stray[:3])}"
            raise _LeftError(msg)
    return one


def _moved(one: Planned, path: str) -> str:
    """Return a file's stored path under the folder it is in now."""
    return one.folder + path[len(one.series["folder"]) :] if path.startswith(one.series["folder"] + "/") else path


def _files_to_rewrite(cfg: config.Config, one: Planned) -> list[str]:
    """Return the stored paths (as they are now) of the files to rewrite; _LeftError for a file not fit to.

    Every file must read and rebuild, and carry the stored ComicInfo name or the new name.
    """
    if not one.due.files:
        return []
    known = {normalize(one.series["comicinfo_series"] or ""), normalize(one.chosen.name)}
    out: list[str] = []
    for f in one.files:
        path = _moved(one, f.path)
        xml = _comicinfo(cfg, path)
        carried = _series_in(path, xml)
        if normalize(carried or "") not in known:
            msg = f"{path} carries Series {carried!r}, neither its stored name nor the new one"
            raise _LeftError(msg)
        new_xml(path, xml, one.chosen)
        if carried != one.chosen.name:
            out.append(path)
    return out


def _comicinfo(cfg: config.Config, stored: str) -> bytes:
    """Return a published file's ComicInfo.xml; _LeftError when it cannot be read or holds none.

    A share error raises as it is: the share's, not the file's, so the run stops.
    """
    try:
        xml = archive.read_comicinfo(config.to_local(cfg, stored))
    except OSError as e:
        if errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS:
            raise
        msg = f"{stored}: cannot be read ({e})"
        raise _LeftError(msg) from e
    except archive.UNREADABLE as e:
        msg = f"{stored}: cannot be read ({e})"
        raise _LeftError(msg) from e
    if xml is None:
        msg = f"{stored}: holds no ComicInfo.xml"
        raise _LeftError(msg)
    return xml


def _series_in(stored: str, xml: bytes) -> str | None:
    try:
        return comicinfo.read_all(xml).get("Series")
    except comicinfo.UNPARSEABLE as e:
        msg = f"{stored}: its ComicInfo.xml cannot be read ({e})"
        raise _LeftError(msg) from e


def new_xml(stored: str, xml: bytes, chosen: series_name.Chosen) -> bytes:
    """Return the file's ComicInfo.xml with Series the name and LocalizedSeries the romanized name (none: removed).

    Only the elements the ComicInfo schema takes are kept. _LeftError when it does not rebuild.
    """
    try:
        fields: dict[str, str | int | None] = {
            k: v for k, v in comicinfo.read_all(xml).items() if comicinfo.field_ok(k, v) and k != "LocalizedSeries"
        }
        fields["Series"] = chosen.name
        if chosen.romaji:
            fields["LocalizedSeries"] = chosen.romaji
        return comicinfo.build(fields, comicinfo.read_pages(xml))
    except (ET.ParseError, LookupError, ValueError) as e:
        msg = f"{stored}: its ComicInfo.xml does not rebuild ({e})"
        raise _LeftError(msg) from e


# --- the acts


def apply(ctx: workflow.Context, planned: Sequence[Planned], outcome: Outcome) -> None:
    """Rename each planned series without a problem, hold each clash; record each in outcome as it is done.

    A Kavita error or a file changed since the check lists the series and the run goes on. A share error raises: the
    series after it are not tried, outcome holds what was done, and a rerun finishes what was begun.
    """
    for one in planned:
        if one.clash and one.problem:
            with db.transaction(ctx.conn):
                workflow.needs_human(
                    ctx.conn, "series", one.series["id"], f"{one.problem} -> name it in its override file"
                )
            outcome.leave(one.line())
        elif one.problem:
            outcome.leave(one.line())
        elif one.due.any():
            _rename(ctx, one, outcome)


def _rename(ctx: workflow.Context, one: Planned, outcome: Outcome) -> None:
    """Rename 1 series, recording it in outcome.

    Kavita is written only before the folder moves (a moved series' localized name waits for finish_names), so a
    Kavita refusal leaves the rows as they are: the next run finds the series still due and finishes.
    """
    cfg, kavita, sid = ctx.cfg, ctx.kavita, one.series["id"]
    before = one.kavita or {}
    log.info(
        "series %s: renaming; before: Kavita name %r, localized name %r, sort name %r, folder %s",
        sid,
        before.get("name"),
        before.get("localizedName"),
        before.get("sortName"),
        one.series["folder"],
    )
    name, localized = one.kavita_name(), one.localized()
    written = one.name_changes or one.due.localized
    try:
        if one.due.files:
            _kavita_names(kavita, one, one.chosen.name, one.series["comicinfo_series"] or "")
        elif written:  # no file changes: Kavita's names first, so the folder never moves under a name Kavita lacks
            _kavita_names(kavita, one, name, localized)
        for path in one.rewrite:
            _rewrite_file(cfg, path, one.chosen)
        if one.folder != one.target:
            _rename_folder(config.to_local(cfg, one.folder), config.to_local(cfg, one.target))
        moved = one.series["folder"] != one.target
        if one.due.files and not moved:
            _kavita_names(kavita, one, name, localized)
    except (*KAVITA_ERRORS, _LeftError) as e:
        outcome.leave(f"series {sid}: not renamed: {e}")
        return
    # Until Kavita follows the folder the old name stays Kavita's localized name, and the stored ComicInfo.xml name
    # stays the old one: the series is still due (its files), so a run that stops before finish_names is finished by
    # the next one, also for a series with no romanized name.
    later = one.due.files and moved
    old = one.series["comicinfo_series"] or ""
    _store(cfg, ctx.conn, one, (name, old, old) if later else (name, localized, one.chosen.name))
    outcome.renamed.append(one)
    if moved:
        outcome.moved.append(one)
    if later:
        outcome.localized.append(one)
    log.info("series %s: renamed: %s", sid, one.line())


def _rename_folder(old: Path, new: Path) -> None:
    """Rename a series folder; denied (PermissionError), it is tried once more after FOLDER_RETRY_SECONDS.

    The share has denied the rename right after the folder's files were rewritten and allowed it seconds later.
    """
    try:
        old.rename(new)
    except PermissionError as e:
        log.warning("%s: rename denied (%s); tried again in %d s", old, e, FOLDER_RETRY_SECONDS)
        time.sleep(FOLDER_RETRY_SECONDS)
        old.rename(new)


def _kavita_names(kavita: Kavita, one: Planned, name: str, localized: str) -> None:
    """Give the series' Kavita name and localized name, both locked; _LeftError when Kavita does not read them back."""
    got = kavita.rename_series(one.series["kavita_series_id"], name, localized)
    if got is None:
        msg = f"Kavita no longer holds series {one.series['kavita_series_id']}"
    elif (got["name"], got.get("localizedName") or "") != (name, localized):
        msg = f"Kavita reads back {got['name']!r} / {got.get('localizedName')!r}, not what was sent"
    elif not (got.get("nameLocked") and got.get("localizedNameLocked")):
        msg = "Kavita reads back the names unlocked"
    else:
        return
    raise _LeftError(msg)


def _rewrite_file(cfg: config.Config, stored: str, chosen: series_name.Chosen) -> None:
    """Replace a file by a copy with its new ComicInfo.xml, the old file kept the retention way.

    A file already carrying the name (a rerun) is left.
    """
    file = config.to_local(cfg, stored)
    xml = _comicinfo(cfg, stored)
    if _series_in(stored, xml) == chosen.name:
        return
    data = new_xml(stored, xml, chosen)
    part = file.with_name(f"{file.name}.part")
    part.unlink(missing_ok=True)  # a stopped run's leftover
    retention.put_library(cfg, file)
    rewrite.copy_replacing(file, part, archive.COMICINFO, data)
    part.replace(file)


def _store(cfg: config.Config, conn: db.Connection, one: Planned, names_: tuple[str, str, str]) -> None:
    """Write the rename's rows in 1 transaction.

    Its folder and file paths, the recorded sizes of its files on disk, the series' names (names_: Kavita's name,
    localized name and the series' ComicInfo.xml name) and, when they were rewritten, its files' names.
    """
    sid, target = one.series["id"], one.target
    sizes = [
        (config.to_local(cfg, path).stat().st_size, path)
        for path in (target + f.path[len(one.series["folder"]) :] for f in one.files if f.sized)
    ]
    with db.transaction(conn):
        if target != one.series["folder"]:
            folder_rows(conn, sid, one.series["folder"], target)
        db.executemany(conn, "UPDATE published_file SET size = %s WHERE library_path = %s", sizes)
        kavita_name, localized, comicinfo_name = names_
        conn.execute(
            "UPDATE series SET name = %s, comicinfo_series = %s, localized_name = %s WHERE id = %s",
            (kavita_name, comicinfo_name, localized or None, sid),
        )
        if one.due.files:
            conn.execute("UPDATE published_file SET comicinfo_series = %s WHERE series_id = %s", (one.chosen.name, sid))
            conn.execute(
                """UPDATE chapter SET comicinfo_series = %s
                WHERE series_id = %s AND status <> 'done' AND comicinfo_series IS NOT NULL""",
                (one.chosen.name, sid),
            )


def folder_rows(conn: db.Connection, series_id: int, old: str, new: str) -> None:
    """Move a series' rows to its new folder (caller's transaction): its folder, and each path under the old one."""
    params = {"id": series_id, "old": old, "new": new}
    conn.execute("UPDATE series SET folder = %(new)s WHERE id = %(id)s", params)
    for table in ("published_file", "chapter"):
        conn.execute(
            sql.SQL("""UPDATE {table} SET library_path = %(new)s || substr(library_path, length(%(old)s) + 1)
            WHERE series_id = %(id)s AND starts_with(library_path, %(old)s || '/')""").format(
                table=sql.Identifier(table)
            ),
            params,
        )


def only_lines(cfg: config.Config, renamed: Iterable[Planned]) -> list[str]:
    """Return a line per renamed series its new names put out of scope (series.only names it by an old name)."""
    lines: list[str] = []
    for p in renamed:
        s = p.series
        after = [p.kavita_name(), s["alt_name"], p.chosen.name, p.localized(), s["original_name"]]
        if not workflow.in_scope(cfg, [n for n in after if n], s["kavita_library_id"]):
            lines.append(f"series {s['id']}: out of scope now: series.only must name it {p.chosen.name!r}")
    return lines


# --- the wait


def wait_for_kavita(
    ctx: workflow.Context,
    moved: Sequence[Planned],
    minutes: float,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Planned]:
    """Request a library scan, again every RESCAN_MINUTES, until Kavita lists each moved series in its new folder.

    Return those it has not followed within minutes; Kavita not answering ends the wait (all left counted).
    """
    library_id = ctx.cfg["kavita"]["library_id"]
    waiting_ = list(moved)
    start = last_request = time.monotonic()
    try:
        ctx.kavita.scan_library(library_id)
        while waiting_ and time.monotonic() - start < minutes * 60:
            sleep(POLL_SECONDS)
            waiting_ = [one for one in waiting_ if not _followed(ctx, one)]
            if waiting_ and time.monotonic() - last_request >= RESCAN_MINUTES * 60:
                ctx.kavita.scan_library(library_id)
                last_request = time.monotonic()
    except KAVITA_ERRORS as e:
        log.warning("Kavita did not answer during the wait: %s", e)
    return waiting_


def finish_names(ctx: workflow.Context, outcome: Outcome, behind: Sequence[Planned]) -> None:
    """Give each series of outcome.localized whose folder Kavita follows its localized name, read back; store it.

    The series' ComicInfo.xml name is then stored as the new name. A series Kavita has not followed (behind), or whose
    write Kavita refuses, keeps the old name as its localized and stored ComicInfo.xml name: still due, the next
    `series rename` finishes it.
    """
    waiting_ = {p.series["id"] for p in behind}
    for one in outcome.localized:
        sid = one.series["id"]
        if sid in waiting_:
            continue
        try:
            _kavita_names(ctx.kavita, one, one.kavita_name(), one.localized())
        except (*KAVITA_ERRORS, _LeftError) as e:
            outcome.leave(f"series {sid}: localized name not set: {e}")
            continue
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE series SET comicinfo_series = %s, localized_name = %s WHERE id = %s",
                (one.chosen.name, one.localized() or None, sid),
            )
        log.info("series %s: localized name %r given", sid, one.localized())


def _followed(ctx: workflow.Context, one: Planned) -> bool:
    try:
        folder = ctx.kavita.series_folder(one.series["kavita_series_id"])
    except FolderError:
        return False
    return folder == config.to_local(ctx.cfg, one.target)
