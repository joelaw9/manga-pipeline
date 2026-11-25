"""Fix adopted series' ComicInfo names (agent-workspace/plans/fix-adopted-series-name.md, step 1; SPEC "State").

An adopted series (one with published files of origin `library`) takes as its `comicinfo_series` the Series its library
files carry, picked by adoption.series_name, the rule adoption uses. Per series, refused: no such series, not an
adopted series, a library file that cannot be read, a name adoption would refuse (differing values, none of Kavita's
names); then, only when the name differs from the stored one: a published file with `pending_path`, `indexed_since` or
`pending_error`, a chapter claimed or not done at publish, a pipeline file holding no ComicInfo.xml or one whose
ComicInfo.xml does not rebuild with the new Series. A share error reading a file ends the run (not a refusal). A series
whose stored name is its files' name is already right. Else each pipeline file carrying another Series gets its new
ComicInfo.xml built, is kept the retention way (retention.put_library), copied with its ComicInfo.xml replaced
(rewrite.copy_replacing) to `<file>.cbz.part` beside it (a leftover of that name deleted first), and the copy replaces
it; a file already carrying the name is skipped, so a rerun after a kill goes on. Then 1 transaction:
series.comicinfo_series, each library row's comicinfo_series (its file's Series, else the name), each pipeline row's
comicinfo_series and size as its file on disk holds them, and the open chapters' comicinfo_series.
Under lock.IDLE exclusive: no part runs meanwhile. --dry-run reads only, in 1 snapshot (no lock: it runs while the fleet
runs), and prints what each series would get. Each run writes its rollback record, every fix decided (old and new name
per series, the files to rewrite), to <paths.scratch>/fix-series-name/<timestamp>.json: a dry run at its end, a real
run before each fix is applied, so a killed run leaves the old names. Every series is refused when
export.retention_days is 0 (nothing would be kept) or the library root is not there. No Kavita scan: the person
requests it.

Usage, from the project root:
python -m scripts.fix_series_name (--all | SERIES_ID...) [--dry-run] [--config PATH]
Exit 0; 2 when a series (or the run) was refused, the refused series listed last; 3 when a part runs.
"""

import argparse
import dataclasses
import json
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from manga_pipeline import adoption, archive, comicinfo, config, db, errors, lock, retention, rewrite

RECORD_FOLDER = "fix-series-name"  # under paths.scratch: the runs' rollback records


class RefusedError(Exception):
    """The series cannot be fixed now; the message says why."""


@dataclass(frozen=True)
class Fix:
    """A series' fix: its stored and new name, its library files' Series and the pipeline files to rewrite."""

    series_id: int
    kavita_name: str
    old: str | None
    new: str
    library: dict[str, str | None]  # stored path -> its file's Series
    rewrite: list[str]  # stored paths of the pipeline files carrying another Series

    def line(self) -> str:
        """Return the fix as the script prints it."""
        if self.old == self.new:
            return f"series {self.series_id}: already right ({self.new!r})"
        return f"series {self.series_id}: {self.old!r} -> {self.new!r}: {len(self.rewrite)} files to rewrite"


def adopted(conn: db.Connection) -> list[int]:
    """Return the ids of the adopted series: those with a published file of origin `library`."""
    return [
        r[0] for r in conn.execute("SELECT DISTINCT series_id FROM published_file WHERE origin = 'library' ORDER BY 1")
    ]


def decide(cfg: config.Config, conn: db.Connection, series_id: int) -> Fix:
    """Return the series' fix (see the module docstring), reading only; RefusedError when it may not be fixed now."""
    series = conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()
    if series is None:
        msg = "no such series"
        raise RefusedError(msg)
    files = conn.execute(
        "SELECT library_path, origin FROM published_file WHERE series_id = %s ORDER BY library_path", (series_id,)
    ).fetchall()
    library = {f["library_path"]: _series(cfg, f["library_path"]) for f in files if f["origin"] == "library"}
    if not library:
        msg = f"{series['name']} is not an adopted series"
        raise RefusedError(msg)
    name = adoption.series_name(list(library.values()), series)
    if isinstance(name, adoption.Problem):
        raise RefusedError(name.text)
    fix = Fix(series_id, series["name"], series["comicinfo_series"], name, library, [])
    if fix.old == name:
        return fix
    busy, claimed = db.one_row(
        conn,
        """SELECT
            EXISTS (SELECT 1 FROM published_file WHERE series_id = %(id)s
                AND (pending_path IS NOT NULL OR indexed_since IS NOT NULL OR pending_error IS NOT NULL)),
            EXISTS (SELECT 1 FROM chapter WHERE series_id = %(id)s
                AND (status = 'claimed' OR (stage = 'publish' AND status <> 'done')))""",
        {"id": series_id},
    )
    if busy:
        msg = "a published file is busy (a rewrite, a copy or a wait for Kavita)"
        raise RefusedError(msg)
    if claimed:
        msg = "a chapter is claimed, or on its way through publish"
        raise RefusedError(msg)
    pipeline = [f["library_path"] for f in files if f["origin"] == "pipeline"]
    return dataclasses.replace(fix, rewrite=[p for p in pipeline if _needs_rewrite(cfg, p, name)])


def _comicinfo(cfg: config.Config, stored: str) -> bytes | None:
    """Return a published file's ComicInfo.xml bytes, or None; RefusedError when the file cannot be read.

    A share error raises as it is: the share's, not the file's, so the run stops.
    """
    try:
        return archive.read_comicinfo(config.to_local(cfg, stored))
    except OSError as e:
        if errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS:
            raise
        msg = f"{stored}: cannot be read ({e})"
        raise RefusedError(msg) from e
    except archive.UNREADABLE as e:
        msg = f"{stored}: cannot be read ({e})"
        raise RefusedError(msg) from e


def _series(cfg: config.Config, stored: str) -> str | None:
    """Return the Series a published file's ComicInfo.xml carries, or None; RefusedError when it cannot be read."""
    return _series_in(stored, _comicinfo(cfg, stored))


def _series_in(stored: str, xml: bytes | None) -> str | None:
    """Return the Series a ComicInfo.xml (of the file stored) carries, or None; RefusedError when it does not parse."""
    if xml is None:
        return None
    try:
        return comicinfo.read_all(xml).get("Series")
    except (ET.ParseError, LookupError, ValueError) as e:  # as adoption.plan reads it
        msg = f"{stored}: its ComicInfo.xml cannot be read ({e})"
        raise RefusedError(msg) from e


def _needs_rewrite(cfg: config.Config, stored: str, name: str) -> bool:
    """Return whether a pipeline file carries another Series than name.

    RefusedError when it holds no ComicInfo.xml, or one that does not rebuild with name.
    """
    xml = _comicinfo(cfg, stored)
    if xml is None:
        msg = f"{stored}: holds no ComicInfo.xml"
        raise RefusedError(msg)
    if _series_in(stored, xml) == name:
        return False
    _new_comicinfo(stored, xml, name)
    return True


def _new_comicinfo(stored: str, xml: bytes, name: str) -> bytes:
    """Return a ComicInfo.xml (of the file stored) rebuilt with Series name; RefusedError when it does not rebuild."""
    try:
        fields: dict[str, str | int | None] = {**comicinfo.read_all(xml), "Series": name}
        return comicinfo.build(fields, comicinfo.read_pages(xml))
    except (ET.ParseError, LookupError, ValueError) as e:
        msg = f"{stored}: its ComicInfo.xml does not rebuild ({e})"
        raise RefusedError(msg) from e


def apply(cfg: config.Config, conn: db.Connection, fix: Fix) -> None:
    """Carry out a fix decide returned: each pipeline file still carrying another Series rewritten, then the rows."""
    if fix.old == fix.new:
        return
    for stored in fix.rewrite:
        if _series(cfg, stored) != fix.new:  # a rerun's file rewritten before the kill is skipped
            _rewrite(cfg, stored, fix.new)
    pipeline = [
        (_series(cfg, r[0]), config.to_local(cfg, r[0]).stat().st_size, r[0])
        for r in conn.execute(
            "SELECT library_path FROM published_file WHERE series_id = %s AND origin = 'pipeline'", (fix.series_id,)
        )
    ]
    with db.transaction(conn):
        conn.execute("UPDATE series SET comicinfo_series = %s WHERE id = %s", (fix.new, fix.series_id))
        db.executemany(
            conn,
            "UPDATE published_file SET comicinfo_series = %s WHERE library_path = %s",
            [(value or fix.new, stored) for stored, value in fix.library.items()],
        )
        db.executemany(
            conn, "UPDATE published_file SET comicinfo_series = %s, size = %s WHERE library_path = %s", pipeline
        )
        conn.execute(
            """UPDATE chapter SET comicinfo_series = %s
            WHERE series_id = %s AND status <> 'done' AND comicinfo_series IS NOT NULL""",
            (fix.new, fix.series_id),
        )


def _rewrite(cfg: config.Config, stored: str, name: str) -> None:
    """Replace a pipeline file by a copy whose ComicInfo.xml Series is name, the old file kept the retention way.

    The new ComicInfo.xml is built first: one that does not rebuild leaves no retention link.
    """
    file = config.to_local(cfg, stored)
    old = archive.read_comicinfo(file)
    if old is None:  # decide refused it; gone since: fail
        msg = f"{file}: holds no ComicInfo.xml"
        raise RuntimeError(msg)
    xml = _new_comicinfo(stored, old, name)
    part = file.with_name(f"{file.name}.part")
    part.unlink(missing_ok=True)  # a killed run's leftover
    retention.put_library(cfg, file)
    rewrite.copy_replacing(file, part, archive.COMICINFO, xml)
    part.replace(file)


def _run_problem(cfg: config.Config) -> str | None:
    """Why no series may be fixed in this run, or None: nothing would be kept, or the library root is not there."""
    if cfg["export"]["retention_days"] == 0:
        return "export.retention_days is 0: a rewritten file would not be kept"
    library = config.abs_path(cfg["paths"]["library"])
    if not errors.exists(library):
        return f"the library folder {library} is not there (share not mounted?)"
    return None


def record_path(cfg: config.Config) -> Path:
    """Return the run's record file, <paths.scratch>/fix-series-name/<timestamp>.json, its folder made."""
    folder = config.abs_path(cfg["paths"]["scratch"]) / RECORD_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{time.strftime('%Y%m%d-%H%M%S')}.json"


def _write_record(path: Path, fixes: list[Fix]) -> None:
    """(Re)write the run's record: every fix decided so far."""
    record = [
        {"series_id": f.series_id, "kavita_name": f.kavita_name, "old": f.old, "new": f.new, "rewrite": f.rewrite}
        for f in fixes
    ]
    part = path.with_name(f"{path.name}.part")  # a kill mid-write leaves the last whole record
    part.write_bytes(json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8"))
    part.replace(path)


def run(cfg: config.Config, conn: db.Connection, args: argparse.Namespace) -> bool:
    """Decide, and unless args.dry_run carry out, each series' fix, printing it; return whether any was refused.

    A fix that fails raises: the series after it are not tried, and a rerun goes on from what is on disk.
    """
    problem = _run_problem(cfg)
    if problem:
        sys.stderr.write(f"error: {problem}\n")
        return True
    refused: list[int] = []
    fixes: list[Fix] = []
    record = record_path(cfg)
    for series_id in adopted(conn) if args.all else args.series_ids:
        try:
            fix = decide(cfg, conn, series_id)
        except RefusedError as e:
            sys.stdout.write(f"series {series_id}: refused: {e}\n")
            refused.append(series_id)
            continue
        sys.stdout.write(f"{fix.line()}\n")
        if fix.old == fix.new:
            continue
        fixes.append(fix)
        if args.dry_run:
            sys.stdout.writelines(f"  {stored}\n" for stored in fix.rewrite)
        else:
            _write_record(record, fixes)  # before the fix: a killed run leaves the old names
            apply(cfg, conn, fix)
    if args.dry_run:
        _write_record(record, fixes)
    if args.dry_run or fixes:
        sys.stdout.write(f"record: {record}\n")
    if refused:
        sys.stdout.write(f"refused series: {', '.join(map(str, refused))}\n")
    return bool(refused)


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments: --all or series ids, not both."""
    parser = argparse.ArgumentParser(description="Give adopted series the ComicInfo name their library files carry.")
    parser.add_argument("series_ids", metavar="SERIES_ID", type=int, nargs="*")
    parser.add_argument("--all", action="store_true", help="every adopted series")
    parser.add_argument("--dry-run", action="store_true", help="print and record each series' fix only; no lock")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    args = parser.parse_args(argv)
    if args.all == bool(args.series_ids):
        parser.error("give --all or SERIES_ID..., not both")
    return args


def main(argv: list[str] | None = None) -> int:
    """Fix the series' names on the configured database and share; print 1 line per series."""
    args = parse(argv)
    if sys.stdout.encoding.lower().replace("-", "") != "utf8":  # a name outside the code page would stop the run
        sys.stderr.write(f"error: output encoding is {sys.stdout.encoding}, not UTF-8: run with PYTHONUTF8=1\n")
        return 2
    cfg = config.init(args.config)
    if args.dry_run:
        with closing(db.read_only(cfg)) as conn, conn.transaction():  # repeatable-read: 1 snapshot
            refused = run(cfg, conn, args)
        return 2 if refused else 0
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE):
                refused = run(cfg, conn, args)
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
