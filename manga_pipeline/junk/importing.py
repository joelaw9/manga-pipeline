"""`junk import` (SPEC "Junk decisions", import): junk decisions imported from a bulk-converter or pipeline source.

Decisions from the bulk converter's junk.sqlite3 (read_store) or from another pipeline database on the same server
(read_database), and from a bulk-converter series record's decision rows. A source is never written: the store is opened
read-only (its WAL included) and copied into memory before it is read; the other database is opened through
db.read_only.

An import rebuilds the imported rows: in 1 transaction every decision whose decider is 'import' (and, from a
bulk-converter store, every imported sighting) is deleted and the source's rows are loaded fresh, so a decision the
bulk converter deleted or changed is followed exactly. The pipeline's own decisions and sightings are never touched;
a source row on a key the pipeline decided is left out, and so is one on an unsignable key (counted). No other row
refers to a decision's id.
"""

import base64
import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Protocol, TypedDict

from manga_pipeline import config, db, images, names
from manga_pipeline.convert import record_reader
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, frames, marks, store

SOURCE_TIMEOUT_S = 30  # how long the store's SQLite file may stay locked by its writer
VERDICT = {"approve": "junk", "keep": "keep"}  # the bulk converter's decision -> ours
UNIT_ORIGINS = ("block", "probe", "piece", "page", "band")  # the bulk converter's origins imported (not cut)


class SourceError(ValueError):
    """The source cannot be imported."""


class DecisionColumns(Protocol):
    """A bulk-converter store row's text columns by name (decision, added), as sqlite3.Row gives them.

    A record's decision rows come in as record_reader.RecordDecisionRow.
    """

    def __getitem__(self, key: str, /) -> str:
        """Return the column's text."""
        ...


# (rows to upsert, counts of what is not imported, the sightings: None when they are not read)
type Source = tuple[list[store.Decision], dict[str, int], list[candidates.SeenRow] | None]


class ImportResult(TypedDict):
    """What an import did (run): decisions added, removed, changed and unchanged; rows the pipeline kept; sightings.

    pipeline_kept: source rows left out because the pipeline decided their key; seen: imported sightings now;
    skipped: {what: count} of the rows not imported, "unsignable" included.
    """

    added: int
    removed: int
    changed: int
    unchanged: int
    pipeline_kept: int
    seen: int
    skipped: dict[str, int]


def snapshot(path: str | Path) -> sqlite3.Connection:
    """Return an in-memory copy of the SQLite database at `path`, read through a read-only connection."""
    path = Path(path).resolve()
    if not path.is_file():
        msg = f"no database at {path}"
        raise SourceError(msg)
    mem = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=SOURCE_TIMEOUT_S)) as src:
            src.backup(mem)
    except sqlite3.DatabaseError as e:
        mem.close()
        msg = f"{path} is not a readable SQLite database: {e}"
        raise SourceError(msg) from e
    mem.row_factory = sqlite3.Row
    return mem


def bulk_time(added: str) -> str:
    """Return the bulk converter's `added` (SQLite CURRENT_TIMESTAMP, UTC) as our time text (workflow.now's format)."""
    return added.replace(" ", "T") + "+00:00"


def winners(rows: Iterable[store.Decision]) -> list[store.Decision]:
    """Pick, of several source rows on 1 (series key, kind, key), the one the bulk converter obeyed.

    A keep before a junk, then the newest. rows: dicts as upsert takes; returns them in input order, 1 per key.
    """
    best: dict[tuple[str, str, str], tuple[tuple[bool, str], store.Decision]] = {}
    for r in rows:
        k = (r["series_key"], r["kind"], r["key"])
        rank = (r["verdict"] == "keep", r["decided_at"])
        if k not in best or rank >= best[k][0]:
            best[k] = (rank, r)
    return [r for _, r in best.values()]


def signable(rows: Sequence[store.Decision]) -> tuple[list[store.Decision], int]:
    """Return (the rows a decision may be written from, how many were left out: on an unsignable key)."""
    kept = [r for r in rows if not store.unsignable(r["kind"], r["key"])]
    return kept, len(rows) - len(kept)


def bulk_rows(src: sqlite3.Connection, name: str) -> tuple[list[store.Decision], dict[str, int]]:
    """Return (rows to upsert, counts of what is not imported) of a bulk-converter store.

    A block or probe approve with a chain comes over as a plain decision on its signature (the old tool removed the unit
    by it before trying the chain); only the chain is left out (counted).
    """
    rows: list[store.Decision] = []
    junk = src.execute(
        "SELECT id, series, decision, origin, tiles, aspect, chain, added FROM junk WHERE superseded IS NULL"
    ).fetchall()
    skipped = {"cut": 0, "chain": 0}
    for r in junk:
        if r["origin"] == "cut":
            skipped["cut"] += 1
            continue
        if r["chain"] is not None:
            skipped["chain"] += 1
        if r["origin"] not in UNIT_ORIGINS:
            msg = f"{name}: junk row {r['id']} has origin {r['origin']!r}, not one this program knows"
            raise SourceError(msg)
        tiles = tuple(int(t, 16) for t in r["tiles"].split(":"))
        key = images.sig_to_str((tiles, r["aspect"]))
        rows.append(_row(r["series"], r["origin"], key, r, f"{name} junk row {r['id']}"))
    for r in src.execute(
        "SELECT id, series, vec, aspect, decision, added FROM frames WHERE superseded IS NULL"
    ).fetchall():
        key = frames.key_text(frames.from_bytes(r["vec"], r["aspect"]))
        rows.append(_row(r["series"], "frame", key, r, f"{name} frames row {r['id']}"))
    for r in src.execute(
        "SELECT id, series, template, w, h, decision, added FROM marks WHERE superseded IS NULL"
    ).fetchall():
        source = f"{name} marks row {r['id']}"
        rows.append(_row(r["series"], "logo", logo_key(r["template"], (r["w"], r["h"]), source), r, source))
    return rows, skipped


def logo_key(png: bytes, size: Sequence[int], source: str) -> str:
    """Return a logo decision's key from a bulk-converter template PNG (marks.Template.key_text).

    SourceError when it is no template or its size is not the recorded (w, h).
    """
    try:
        template = marks.Template.from_png(png)
    except (OSError, ValueError) as e:
        msg = f"{source}: template is not a logo template: {e}"
        raise SourceError(msg) from None
    if template.size != tuple(size):
        msg = f"{source}: template is {template.size}, recorded as {tuple(size)}"
        raise SourceError(msg)
    return template.key_text()


def _row(
    series: str | None, kind: str, key: str, r: DecisionColumns | record_reader.RecordDecisionRow, source: str
) -> store.Decision:
    if r["decision"] not in VERDICT:
        msg = f"{source}: decision {r['decision']!r} is neither approve nor keep"
        raise SourceError(msg)
    return {
        "series_key": store.GLOBAL if series is None else series,
        "kind": kind,
        "key": key,
        "verdict": VERDICT[r["decision"]],
        "decider": "import",
        "decided_at": bulk_time(r["added"]),
        "source": source,
    }


def record_rows(record: record_reader.Record) -> list[store.Decision]:
    """Return the rows to upsert from a bulk-converter series record.

    Schema 4 carries the decision rows its runs used (series.decisions.rows; a marks row names its template in
    series.decisions.templates); a schema-3 record carries none it can give (its decisions are in the store). Cut rows
    are left out and chains dropped, as in the store import. RecordError on a malformed row.
    """
    if record["schema"] != record_reader.SCHEMA:
        return []
    try:
        block = record_reader.decisions(record)
        junk, frame_rows = block["rows"]["junk"], block["rows"]["frames"]
        out: list[store.Decision] = []
        for r in junk:  # its cut rows are left out by the reader
            if r["origin"] not in UNIT_ORIGINS:
                msg = f"origin {r['origin']!r}"
                raise SourceError(msg)
            tiles, aspect = r["key"]
            sig = images.sig_to_str((tuple(int(t, 16) for t in tiles.split(":")), float(aspect)))
            out.append(_row(r["series"], r["origin"], sig, r, f"record junk row {r['row']}"))
        for r in frame_rows:
            vec, aspect = r["key"]
            key = frames.key_text(frames.from_bytes(base64.b64decode(vec["b64"]), aspect))
            out.append(_row(r["series"], "frame", key, r, f"record frames row {r['row']}"))
        for r in block["rows"]["marks"]:
            tpl, w, h = r["key"]
            source = f"record marks row {r['row']}"
            png = base64.b64decode(block["templates"][tpl["tpl"]], validate=True)
            out.append(_row(r["series"], "logo", logo_key(png, (w, h), source), r, source))
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        why = f"{type(e).__name__}: {e}"
        msg = f"record series.decisions.rows: malformed row ({why})"
        raise record_reader.RecordError(msg) from None
    return out


def read_database(cfg: config.Config, name: str) -> Source:
    """Return (rows, skipped, seen) of another pipeline database on the server.

    Its decisions with their deciders, times and sources; its sightings are not read (seen None).
    """
    if name == cfg["database"]["name"]:
        msg = f"{name} is this pipeline's own database"
        raise SourceError(msg)
    try:
        with closing(db.read_only({**cfg, "database": {**cfg["database"], "name": name}})) as src:
            rows: list[store.Decision] = [
                {
                    "series_key": r["series_key"],
                    "kind": r["kind"],
                    "key": r["key"],
                    "verdict": r["verdict"],
                    "decider": r["decider"],
                    "decided_at": r["decided_at"],
                    "source": r["source"],
                }
                for r in src.execute("SELECT * FROM junk_decision ORDER BY id")
            ]
    except db.NoDatabaseError as e:
        raise SourceError(str(e)) from None
    except db.SchemaError as e:
        msg = f"{name} is not a pipeline database of this version: {e}"
        raise SourceError(msg) from None
    return rows, {}, None


SEEN_KINDS = ("block", "page", "band")  # the bulk converter's sightings counted for recurrence (its kinds = ours)
IMPORTED_SEEN_AT = "1970-01-01T00:00:00+00:00"  # the bulk converter saw them before any pipeline scan


def bulk_seen(src: sqlite3.Connection) -> list[candidates.SeenRow]:
    """Return the junk_seen rows of a bulk-converter store's sightings of edge blocks, pages and bands.

    Each is (series key, kind, key, chapter identity, IMPORTED_SEEN_AT, 'import'). The chapter is the identity of its
    archive name, as the baseline names sources.
    """
    rows = src.execute(  # an SQLite query: the kinds as 1 JSON array parameter
        """SELECT DISTINCT series, chapter, kind, tiles, aspect FROM junk_seen
        WHERE kind IN (SELECT value FROM json_each(?))""",
        (json.dumps(SEEN_KINDS),),
    )
    out: list[candidates.SeenRow] = []
    for r in rows:
        tiles = tuple(int(t, 16) for t in r["tiles"].split(":"))
        identity = names.identity(names.parse(Path(r["chapter"]).stem))
        out.append(
            (r["series"], r["kind"], images.sig_to_str((tiles, r["aspect"])), identity, IMPORTED_SEEN_AT, "import")
        )
    return sorted(set(out))


def tables(src: sqlite3.Connection) -> set[str]:
    """Return the names of the tables of SQLite connection src."""
    return {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


DECISION_COLUMNS = ("series_key", "kind", "key", "verdict", "decider", "decided_at", "source")
PLACES = sql.SQL(", ").join([sql.Placeholder()] * len(DECISION_COLUMNS))


def read_store(path: str | Path) -> Source:
    """Return (rows, skipped, seen) of the bulk converter's junk store at `path`.

    Its decisions, the rows not imported by what, and its sightings.
    """
    path = Path(path).resolve()
    with closing(snapshot(path)) as src:
        if not {"junk", "frames", "marks", "junk_seen"} <= tables(src):
            msg = f"{path} is not a bulk-converter junk store"
            raise SourceError(msg)
        try:
            (rows, skipped), seen = bulk_rows(src, path.name), bulk_seen(src)
        except sqlite3.Error as e:
            msg = f"{path}: its tables are not the expected ones: {e}"
            raise SourceError(msg) from e
    return rows, skipped, seen


def run(conn: db.Connection, source: Source) -> ImportResult:
    """Rebuild the imported rows from source, (rows, skipped, seen) of read_store or read_database (module docstring).

    Returns {"added", "removed", "changed", "unchanged" (decisions, compared by key with the rows before),
    "pipeline_kept" (source rows left out because the pipeline decided their key), "seen" (imported sightings now),
    "skipped": {what: count, "unsignable": rows on an unsignable key}}.
    """
    rows, skipped, seen = source
    rows, unsignable = signable(winners(rows))
    skipped = {**skipped, "unsignable": unsignable}
    with db.transaction(conn):
        before = {_ident(r): _value(r) for r in conn.execute("SELECT * FROM junk_decision WHERE decider = 'import'")}
        conn.execute("DELETE FROM junk_decision WHERE decider = 'import'")
        own = {_ident(r) for r in conn.execute("SELECT * FROM junk_decision")}
        kept = [r for r in rows if _ident(r) not in own]
        db.executemany(
            conn,
            sql.SQL("INSERT INTO junk_decision ({columns}) VALUES ({places})").format(
                columns=sql.SQL(", ").join(map(sql.Identifier, DECISION_COLUMNS)), places=PLACES
            ),
            [tuple(r[c] for c in DECISION_COLUMNS) for r in kept],
        )
        after = {_ident(r): _value(r) for r in kept if r["decider"] == "import"}
        if seen is not None:
            conn.execute("DELETE FROM junk_seen WHERE origin = 'import'")
            db.executemany(
                conn,
                """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                seen,
            )
        seen_now = db.one_row(conn, "SELECT count(*) FROM junk_seen WHERE origin = 'import'")[0]
    return {
        "added": len(after.keys() - before.keys()),
        "removed": len(before.keys() - after.keys()),
        "changed": sum(before[k] != after[k] for k in before.keys() & after.keys()),
        "unchanged": sum(before[k] == after[k] for k in before.keys() & after.keys()),
        "pipeline_kept": len(rows) - len(kept),
        "seen": seen_now,
        "skipped": skipped,
    }


def _ident(r: store.Decision | db.Row) -> tuple[str, str, str]:
    return (r["series_key"], r["kind"], r["key"])


def _value(r: store.Decision | db.Row) -> tuple[str, str, str | None]:
    return (r["verdict"], r["decided_at"], r["source"])
