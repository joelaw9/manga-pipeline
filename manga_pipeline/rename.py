"""`series names` and `series rename` (SPEC "Retroactive updates", Series names).

`read_db` reads the run's series from the database (in the caller's snapshot); `check` then reads Kavita and every file
and decides each series' plan, writing nothing. `apply` renames each planned series in turn: Kavita's name first (the
files' old name kept as its localized name, so both match 1 series), the files, the folder, Kavita's localized name,
then 1 transaction. `wait_for_kavita` requests 1 library scan and waits until Kavita follows each moved folder.
"""

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from manga_pipeline import archive, comicinfo, config, db, errors, names, retention, rewrite, volumes, workflow
from manga_pipeline.clients.kavita import FolderError
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.db import sql
from manga_pipeline.metadata import admit, merge, series_name
from manga_pipeline.metadata.records import SeriesRecords
from manga_pipeline.replies.kavita import Series

log = logging.getLogger(__name__)
RESCAN_MINUTES = 10  # a scan requested while one runs does not work: request again this often
POLL_SECONDS = 30
# A series a rename may act on: status ok, Kavita holds it, a folder, its check completed, a baseline or admitted new.
ELIGIBLE = sql.SQL("""(s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND s.folder IS NOT NULL
    AND (s.baseline_at IS NOT NULL OR s.origin = 'new')
    AND EXISTS (SELECT 1 FROM series_check k WHERE k.series_id = s.id AND k.data_version > 0))""")


NO_CHOICE = series_name.Chosen("", "", "")  # a series not checked yet
NOTHING_DUE = series_name.Due(files=False, kavita_name=False, localized=False, folder=False)


class RenameError(Exception):
    """A rename left something undone: the message lists what (exit 1)."""


@dataclass(frozen=True)
class File:
    """A published file of a series: its stored path and its row's size (None: not recorded)."""

    path: str
    size: int | None


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
            return f"{head}: {'needs_human' if self.clash else 'left'}: {self.problem}"
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
    """The run's series as read from the database, and the names other series hold.

    new_holders: the names of the new series Kavita does not hold yet; stored_names: every series' stored names,
    {normalised name: (series id, who holds it)} (status' clash test).
    """

    planned: list[Planned]
    new_holders: dict[str, str]
    stored_names: dict[str, tuple[int, str]]


def read_db(cfg: config.Config, conn: db.Connection, series_ids: Sequence[int] | None) -> Run:
    """Read the run's series (series_ids; None: every eligible series in scope): rows, records, files.

    workflow.Refused for an id of no series in scope; an asked-for series that may not be renamed gets its problem.
    """
    where = sql.SQL("TRUE") if series_ids is None else sql.SQL("s.id = ANY(%(ids)s)")
    rows = conn.execute(
        sql.SQL("""SELECT s.*, {eligible} AS eligible, {busy} AS busy FROM series s
        WHERE {where} AND {scope} ORDER BY s.id""").format(
            eligible=ELIGIBLE, busy=rewrite.RENAME_BUSY.format(s=sql.Identifier("s")), where=where, scope=workflow.SCOPE
        ),
        {**workflow.scope_params(cfg), "ids": list(series_ids or [])},
    ).fetchall()
    missing = sorted(set(series_ids or []) - {r["id"] for r in rows})
    if missing:
        msg = f"no such series in scope: {', '.join(map(str, missing))}"
        raise workflow.Refused(msg)
    planned: list[Planned] = []
    for row in rows:
        if series_ids is None and not row["eligible"]:
            continue
        files = tuple(
            File(f["library_path"], f["size"])
            for f in conn.execute(
                "SELECT library_path, size FROM published_file WHERE series_id = %s ORDER BY 1", (row["id"],)
            )
        )
        one = Planned(row, merge.stored_records(conn, row["id"]), files)
        if not row["eligible"]:
            one = replace(one, problem="not eligible: its status, Kavita id, folder, check or baseline")
        elif row["busy"]:
            one = replace(one, problem="busy: a chapter claimed or at publish or confirm, or a file with a change due")
        planned.append(one)
    stored: dict[str, tuple[int, str]] = {}
    for r in conn.execute("SELECT id, name, localized_name, original_name, comicinfo_series FROM series"):
        for n in (r["name"], r["localized_name"], r["original_name"], r["comicinfo_series"]):
            if n and normalize(n):
                stored.setdefault(normalize(n), (r["id"], f"series {r['id']} ({r['name']}) has that name"))
    return Run(planned, admit.new_series_holders(conn, 0), stored)


def with_override(cfg: config.Config, one: Planned) -> Planned:
    """Return the plan with its override file's name, or the file's problem (a share read: outside any snapshot)."""
    if one.problem:
        return one
    try:
        return replace(
            one,
            override=volumes.override_name(config.abs_path(cfg["paths"]["naming_overrides"]), one.series["rules_key"]),
        )
    except volumes.OverrideError as e:
        return replace(one, problem=str(e))


def waiting(cfg: config.Config, run: Run) -> list[str]:
    """Return status' lines of the series whose names are due, from the stored records and names (no Kavita read)."""
    lines: list[str] = []
    for planned in run.planned:
        if planned.problem:  # busy: renamed once it is not
            continue
        one = with_override(cfg, planned)
        if one.problem:  # its override file's
            lines.append(f"  series {one.series['id']}: {one.problem}")
            continue
        s = one.series
        own = series_name.Own(s["name"], s["localized_name"], s["original_name"], s["comicinfo_series"])
        chosen = series_name.chosen(own, one.records, one.override or s["admit_name"])
        holders = {k: who for k, (sid, who) in run.stored_names.items() if sid != s["id"]}
        if chosen.romaji and series_name.taken(chosen.romaji, {**run.new_holders, **holders}):
            chosen = replace(chosen, romaji="")
        due = series_name.due(own, s["localized_name"], s["folder"], chosen)
        if due.any():
            what = [
                w
                for w, on in (
                    ("files", due.files),
                    ("Kavita name", due.kavita_name),
                    ("localized name", due.localized),
                    ("folder", due.folder),
                )
                if on
            ]
            lines.append(f"  series {s['id']} ({s['comicinfo_series']}): {chosen.name!r}: {', '.join(what)}")
    return lines


def check(ctx: workflow.Context, run: Run) -> list[Planned]:
    """Return each series' plan: Kavita's names, the rule, the clash test, the folder and every file read and checked.

    A share error ends the run (raised); a file that cannot be read or rebuilt is the series' problem.
    """
    library_id = ctx.cfg["kavita"]["library_id"]
    listing = {s["id"]: s for s in ctx.kavita.series_in_library(library_id)}
    taken_in_run: dict[str, str] = {}
    out: list[Planned] = []
    for planned in run.planned:
        one = with_override(ctx.cfg, planned)
        if one.problem:
            out.append(one)
            continue
        decided = _decide(ctx, one, listing, run.new_holders, taken_in_run)
        if not decided.problem:
            who = f"series {one.series['id']} of this run gets that name"
            for n in (decided.chosen.name, decided.chosen.romaji):
                if normalize(n):
                    taken_in_run[normalize(n)] = who
        out.append(decided)
    return out


def _decide(
    ctx: workflow.Context,
    one: Planned,
    listing: dict[int, Series],
    new_holders: dict[str, str],
    taken_in_run: dict[str, str],
) -> Planned:
    """Return 1 series' plan (see check)."""
    series = one.series
    kavita = listing.get(series["kavita_series_id"])
    if kavita is None:
        return replace(one, problem=f"Kavita does not list series {series['kavita_series_id']}")
    own = series_name.Own(
        kavita["name"], kavita.get("localizedName"), kavita.get("originalName"), series["comicinfo_series"]
    )
    chosen = series_name.chosen(own, one.records, one.override or series["admit_name"])
    holders = {
        **new_holders,
        **admit.kavita_holders(s for i, s in listing.items() if i != kavita["id"]),
        **taken_in_run,
    }
    if chosen.romaji and series_name.taken(chosen.romaji, holders):
        log.info("series %s: its romanized name %r is taken: left out", series["id"], chosen.romaji)
        chosen = replace(chosen, romaji="")
    one = replace(one, chosen=chosen, kavita=kavita, folder=series["folder"])
    due = series_name.due(own, kavita.get("localizedName"), series["folder"], chosen)
    one = replace(one, due=due)
    if not due.any():
        return one
    if due.files or due.kavita_name:
        why = series_name.taken(chosen.name, holders)
        if why:
            return replace(one, problem=f"its name {chosen.name!r} is not free: {why}", clash=True)
    try:
        one = _folders(ctx.cfg, one)
        return replace(one, rewrite=tuple(_files_to_rewrite(ctx.cfg, one)))
    except _LeftError as e:
        return replace(one, problem=str(e))


class _LeftError(Exception):
    """The series is left as it is: the message says why."""


def _folders(cfg: config.Config, one: Planned) -> Planned:
    """Return the plan with its folder (where its files are now) and target; _LeftError when they do not fit.

    After a stop past the folder rename, the old folder is gone and the target holds every published name.
    """
    stored = one.series["folder"]
    parent = PurePosixPath(stored).parent
    target = str(parent / names.folder_name(one.chosen.name)) if one.due.folder else stored
    old, new = config.to_local(cfg, stored), config.to_local(cfg, target)
    published = {PurePosixPath(f.path).name for f in one.files}
    if target != stored and not errors.exists(old):
        if errors.exists(new) and published <= {p.name for p in new.iterdir()}:
            return replace(one, folder=target, target=target)
        msg = f"its folder {stored} is missing"
        raise _LeftError(msg)
    if not errors.exists(old):
        msg = f"its folder {stored} is missing"
        raise _LeftError(msg)
    if target != stored:
        others = [p.name for p in new.parent.iterdir() if p.name != old.name] if errors.exists(new.parent) else []
        if taken := series_name.folder_taken(one.chosen.name, others):
            msg = f"the folder name is taken by {parent / taken}"
            raise _LeftError(msg)
    return replace(one, target=target)


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
    except (ET.ParseError, LookupError, ValueError) as e:
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


def apply(ctx: workflow.Context, planned: Sequence[Planned]) -> tuple[list[Planned], list[str]]:
    """Rename each planned series without a problem, hold each clash; return those renamed and the lines of the rest.

    A share error raises: the series after it are not tried, and a rerun finishes what was begun.
    """
    renamed: list[Planned] = []
    left: list[str] = []
    for one in planned:
        if one.clash and one.problem:
            with db.transaction(ctx.conn):
                workflow.needs_human(
                    ctx.conn, "series", one.series["id"], f"{one.problem} -> name it in its override file"
                )
            left.append(one.line())
        elif one.problem:
            left.append(one.line())
        elif one.due.any():
            why = _rename(ctx, one)
            if why:
                left.append(f"series {one.series['id']}: not renamed: {why}")
            else:
                renamed.append(one)
    return renamed, left


def _rename(ctx: workflow.Context, one: Planned) -> str | None:
    """Rename 1 series; return why a Kavita write failed (nothing further done for it), else None."""
    cfg, kavita, kid = ctx.cfg, ctx.kavita, one.series["kavita_series_id"]
    before = one.kavita or {}
    log.info(
        "series %s: renaming; before: Kavita name %r, localized name %r, sort name %r, folder %s",
        one.series["id"],
        before.get("name"),
        before.get("localizedName"),
        before.get("sortName"),
        one.series["folder"],
    )
    if one.due.files:
        bridge = one.series["comicinfo_series"] or ""
        if why := _read_back(kavita.rename_series(kid, one.chosen.name, bridge), one, one.chosen.name, bridge):
            return why
    sizes: dict[str, int] = {}
    for path in one.rewrite:
        sizes[path] = _rewrite_file(cfg, path, one.chosen)
    if one.folder != one.target:
        old, new = config.to_local(cfg, one.folder), config.to_local(cfg, one.target)
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    name, localized = one.kavita_name(), one.localized()
    written = one.name_changes or one.due.localized
    if written and (why := _read_back(kavita.rename_series(kid, name, localized), one, name, localized)):
        return why
    _store(ctx.conn, one, name, localized, sizes)
    log.info("series %s: renamed: %s", one.series["id"], one.line())
    return None


def _read_back(got: Series | None, one: Planned, name: str, localized: str) -> str | None:
    """Return why Kavita's read-back is not the names sent, both locked, or None."""
    if got is None:
        return f"Kavita no longer holds series {one.series['kavita_series_id']}"
    if (got["name"], got.get("localizedName") or "") != (name, localized):
        return f"Kavita reads back {got['name']!r} / {got.get('localizedName')!r}, not what was sent"
    if not (got.get("nameLocked") and got.get("localizedNameLocked")):
        return "Kavita reads back the names unlocked"
    return None


def _rewrite_file(cfg: config.Config, stored: str, chosen: series_name.Chosen) -> int:
    """Replace a file by a copy with its new ComicInfo.xml, the old file kept the retention way; return its size.

    A file already carrying the name (a rerun) is left.
    """
    file = config.to_local(cfg, stored)
    xml = _comicinfo(cfg, stored)
    if _series_in(stored, xml) == chosen.name:
        return file.stat().st_size
    data = new_xml(stored, xml, chosen)
    part = file.with_name(f"{file.name}.part")
    part.unlink(missing_ok=True)  # a stopped run's leftover
    retention.put_library(cfg, file)
    size = rewrite.copy_replacing(file, part, archive.COMICINFO, data)
    part.replace(file)
    return size


def _store(conn: db.Connection, one: Planned, kavita_name: str, localized: str, sizes: dict[str, int]) -> None:
    """Write the rename's rows in 1 transaction: the series' names and folder, its files' paths, names and sizes."""
    sid, name = one.series["id"], one.chosen.name
    with db.transaction(conn):
        conn.execute(
            "UPDATE series SET name = %s, comicinfo_series = %s, localized_name = %s WHERE id = %s",
            (kavita_name, name, localized or None, sid),
        )
        if one.target != one.series["folder"]:
            folder_rows(conn, sid, one.series["folder"], one.target)
        if one.due.files:
            conn.execute("UPDATE published_file SET comicinfo_series = %s WHERE series_id = %s", (name, sid))
            conn.execute(
                """UPDATE chapter SET comicinfo_series = %s
                WHERE series_id = %s AND status <> 'done' AND comicinfo_series IS NOT NULL""",
                (name, sid),
            )
            db.executemany(
                conn,
                "UPDATE published_file SET size = %s WHERE library_path = %s AND size IS NOT NULL",
                [(size, path) for path, size in sizes.items()],
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


# --- the wait


def wait_for_kavita(
    ctx: workflow.Context,
    moved: Sequence[Planned],
    minutes: float,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Planned]:
    """Request a library scan, again every RESCAN_MINUTES, until Kavita lists each moved series in its new folder.

    Return those it has not followed after minutes; Kavita not answering ends the wait (all left counted).
    """
    library_id = ctx.cfg["kavita"]["library_id"]
    waiting = list(moved)
    start = last_request = time.monotonic()
    ctx.kavita.scan_library(library_id)
    while waiting:
        sleep(POLL_SECONDS)
        try:
            waiting = [one for one in waiting if not _followed(ctx, one)]
            if waiting and time.monotonic() - last_request >= RESCAN_MINUTES * 60:
                ctx.kavita.scan_library(library_id)
                last_request = time.monotonic()
        except workflow.Unreachable as e:
            log.warning("Kavita did not answer during the wait: %s", e)
            return waiting
        if time.monotonic() - start >= minutes * 60:
            break
    return waiting


def _followed(ctx: workflow.Context, one: Planned) -> bool:
    try:
        folder = ctx.kavita.series_folder(one.series["kavita_series_id"])
    except FolderError:
        return False
    return folder == config.to_local(ctx.cfg, one.target)


def moved_series(planned: Sequence[Planned]) -> list[Planned]:
    """Return the series whose folder a rename moved."""
    return [p for p in planned if not p.problem and p.series["folder"] != p.target and p.target]
