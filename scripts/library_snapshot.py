"""The library snapshot (agent-workspace/plans/phase-10-16-recording.md): each published file's bytes and formats.

Read only, no lock, while the automation is paused (nothing renames, joins or adopts a file meanwhile). Every
`published_file` row (any state, every series), or the rows of the listed series, with its file on disk: the file's
bytes and {format: [pages, bytes]} over its pages (`archive.archive_pages`; bytes uncompressed; the format by
`images.detect` on a page's first bytes, the whole page when they leave a WebP unknown). The run report's sizes and
formats before (10.18 runs it before converting, and after). A file missing on disk is listed as missing, one that is
not a zip as unreadable; any other read error (a dropped share among them) is tried once more at the end, and one
failing again stops the script, exit 1, nothing written. The plain connection's session is read only (the live
database may be at an older schema than this program's, which `db.read_only` refuses).

Usage, from the project root: python -m scripts.library_snapshot OUT [--series ID...] [--config PATH]
Exit 0 written, 1 a file could not be read, 2 refused (not paused, or OUT in a refused folder).
"""

import argparse
import json
import sys
import zipfile
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from manga_pipeline import archive, config, db, errors, images, pause

THREADS = 8  # share reads at once (plan step 0: 0.045 s per file from Celestia over SMB)
HEAD = 64  # a page's first bytes: enough for images.detect but a VP8X WebP with a large chunk first
REFUSED_UNDER = ("retention", "library", "staging")  # paths keys: purged or scanned, never a snapshot's folder
MISSING, UNREADABLE = "missing", "unreadable"


class FileEntry(TypedDict):
    """1 published file: its row's columns, and its bytes and formats on disk (None, with error set, when unread)."""

    id: int
    series_id: int
    state: str
    origin: str
    kind: str | None
    identity: str
    source: str | None
    library_path: str
    joined_parts: int | None
    bytes: int | None
    formats: dict[str, list[int]] | None
    error: str | None


class Summary(TypedDict):
    """What the script prints: rows read, files read, errors, and per origin the bytes and the formats."""

    rows_read: int
    files_read: int
    errors: dict[str, int]
    bytes: dict[str, int]
    formats: dict[str, dict[str, list[int]]]


class Snapshot(TypedDict):
    """The snapshot written to OUT."""

    paused: str
    series: list[int] | None
    summary: Summary
    files: list[FileEntry]


class RefusedError(Exception):
    """The snapshot cannot be taken as asked (exit 2)."""


def page_format(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """Return a page's format: images.detect on its first bytes, on the whole page when a WebP stays unknown."""
    with z.open(info) as f:
        head = f.read(HEAD)
    fmt = images.detect(head)
    if fmt == images.UNKNOWN and head[8:12] == b"WEBP" and info.file_size > len(head):
        fmt = images.detect(z.read(info))
    return fmt


def file_formats(path: Path) -> tuple[int, dict[str, list[int]]]:
    """Return a CBZ's bytes and {format: [pages, uncompressed bytes]}. OSError and BadZipFile as they come."""
    size = path.stat().st_size
    out: dict[str, list[int]] = {}
    with zipfile.ZipFile(path) as z:
        infos = {i.filename: i for i in z.infolist()}
        for name in archive.archive_pages(infos):
            pages, total = out.get(fmt := page_format(z, infos[name]), [0, 0])
            out[fmt] = [pages + 1, total + infos[name].file_size]
    return size, out


def measure(cfg: config.Config, entry: FileEntry) -> FileEntry:
    """Return the entry with its file's bytes and formats, or its error (missing, unreadable).

    Any other OSError (a dropped share's FileNotFoundError among them) is raised.
    """
    try:
        size, formats = file_formats(config.to_local(cfg, entry["library_path"]))
    except FileNotFoundError as e:
        if errors.dropped_share(e):
            raise
        return {**entry, "bytes": None, "formats": None, "error": MISSING}
    except zipfile.BadZipFile:
        return {**entry, "bytes": None, "formats": None, "error": UNREADABLE}
    return {**entry, "bytes": size, "formats": formats, "error": None}


def rows(conn: db.Connection, series: Sequence[int] | None) -> list[FileEntry]:
    """Return every published_file row (or the listed series'), as entries not read yet, by id."""
    ids = None if series is None else list(series)
    found = conn.execute(
        """SELECT id, series_id, state, origin, kind, identity, source, library_path, joined_parts
        FROM published_file WHERE %s::bigint[] IS NULL OR series_id = ANY(%s) ORDER BY id""",
        (ids, ids),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "series_id": r["series_id"],
            "state": r["state"],
            "origin": r["origin"],
            "kind": r["kind"],
            "identity": r["identity"],
            "source": r["source"],
            "library_path": r["library_path"],
            "joined_parts": r["joined_parts"],
            "bytes": None,
            "formats": None,
            "error": None,
        }
        for r in found
    ]


def measure_all(cfg: config.Config, entries: list[FileEntry]) -> list[FileEntry]:
    """Measure every entry on THREADS threads; an entry whose read raised OSError is measured once more, alone.

    OSError when that second read fails too.
    """

    def first(entry: FileEntry) -> FileEntry | None:
        try:
            return measure(cfg, entry)
        except OSError:
            return None

    with ThreadPoolExecutor(THREADS) as pool:
        done = list(pool.map(first, entries))
    return [m if m is not None else measure(cfg, e) for e, m in zip(entries, done, strict=True)]


def summary(entries: list[FileEntry]) -> Summary:
    """Return the snapshot's totals: per origin the bytes and {format: [pages, bytes]}, and the errors by kind."""
    size: Counter[str] = Counter()
    formats: dict[str, dict[str, list[int]]] = {}
    for e in entries:
        if e["bytes"] is None or e["formats"] is None:
            continue
        size[e["origin"]] += e["bytes"]
        by = formats.setdefault(e["origin"], {})
        for fmt, (pages, total) in e["formats"].items():
            have = by.get(fmt, [0, 0])
            by[fmt] = [have[0] + pages, have[1] + total]
    return {
        "rows_read": len(entries),
        "files_read": sum(1 for e in entries if e["error"] is None),
        "errors": dict(Counter(e["error"] for e in entries if e["error"] is not None)),
        "bytes": dict(size),
        "formats": formats,
    }


def check_out(cfg: config.Config, out: Path) -> None:
    """Raise RefusedError when OUT lies in a folder the pipeline purges or scans."""
    where = out.resolve()
    for key in REFUSED_UNDER:
        folder = config.abs_path(cfg["paths"][key]).resolve()
        if where.is_relative_to(folder):
            msg = f"{out} is under paths.{key} ({folder}): pick a folder the pipeline does not purge or scan"
            raise RefusedError(msg)


def take(cfg: config.Config, conn: db.Connection, out: Path, series: Sequence[int] | None) -> Snapshot:
    """Take the snapshot and write it to OUT (a .part file first, then replaced); return it.

    RefusedError when the automation is not paused or OUT is in a refused folder; OSError when a file cannot be read.
    """
    check_out(cfg, out)
    paused = pause.paused(conn)
    if paused is None:
        msg = "the automation is not paused: run `state pause` first, so no file changes during the snapshot"
        raise RefusedError(msg)
    entries = measure_all(cfg, rows(conn, series))
    snap: Snapshot = {
        "paused": paused,
        "series": None if series is None else list(series),
        "summary": summary(entries),
        "files": entries,
    }
    part = out.with_name(out.name + ".part")
    part.write_bytes(json.dumps(snap, indent=1, ensure_ascii=False).encode())
    part.replace(out)
    return snap


def main(argv: list[str] | None = None) -> int:
    """Take the snapshot on the configured database and library; print its summary."""
    parser = argparse.ArgumentParser(description="The library snapshot: each published file's bytes and formats.")
    parser.add_argument("out", type=Path, help="the JSON file to write")
    parser.add_argument("--series", type=int, nargs="+", help="only these series (ids)")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    args = parser.parse_args(argv)
    cfg = config.init(args.config)
    try:
        with closing(db.open_connection(cfg)) as conn:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            snap = take(cfg, conn, args.out, args.series)
    except RefusedError as e:
        sys.stderr.write(f"refused: {e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"a file could not be read twice, nothing written: {e}\n")
        return 1
    sys.stdout.write(json.dumps(snap["summary"], indent=2) + f"\nwritten: {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
