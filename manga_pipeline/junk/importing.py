"""`junk import` (SPEC "Junk decisions", import): decisions from the bulk converter's junk.sqlite3 or from another
pipeline database, and from a bulk-converter series record's decision rows. The source is never written: it is opened
read-only (its WAL included) and copied into memory before it is read.

An import rebuilds the imported rows: in 1 transaction every decision whose decider is 'import' (and, from a
bulk-converter store, every imported sighting) is deleted and the source's rows are loaded fresh, so a decision the
bulk converter deleted or changed is followed exactly. The pipeline's own decisions and sightings are never touched;
a source row on a key the pipeline decided is left out. No other row refers to a decision's id.
"""

import base64
import sqlite3
from contextlib import closing
from pathlib import Path

from manga_pipeline import db, images, names
from manga_pipeline.convert import record_reader
from manga_pipeline.junk import frames, store

VERDICT = {"approve": "junk", "keep": "keep"}  # the bulk converter's decision -> ours
UNIT_ORIGINS = ("block", "probe", "piece", "page", "band")  # the bulk converter's origins imported (not cut)


class SourceError(ValueError):
    """The source cannot be imported."""


def snapshot(path):
    """An in-memory copy of the SQLite database at `path`, read through a read-only connection."""
    path = Path(path).resolve()
    if not path.is_file():
        raise SourceError(f"no database at {path}")
    mem = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=db.BUSY_TIMEOUT_S)) as src:
            src.backup(mem)
    except sqlite3.DatabaseError as e:
        mem.close()
        raise SourceError(f"{path} is not a readable SQLite database: {e}") from e
    mem.row_factory = sqlite3.Row
    return mem


def bulk_time(added):
    """The bulk converter's `added` (SQLite CURRENT_TIMESTAMP, UTC) as our time text (workflow.now's format)."""
    return added.replace(" ", "T") + "+00:00"


def winners(rows):
    """Of several source rows on 1 (series key, kind, key), the one the bulk converter obeyed: a keep before a junk,
    then the newest. rows: dicts as upsert takes; returns them in input order, 1 per key."""
    best = {}
    for r in rows:
        k = (r["series_key"], r["kind"], r["key"])
        rank = (r["verdict"] == "keep", r["decided_at"])
        if k not in best or rank >= best[k][0]:
            best[k] = (rank, r)
    return [r for _, r in best.values()]


def bulk_rows(src, name):
    """(rows to upsert, counts of what is not imported) of a bulk-converter store. A block or probe approve with a
    chain comes over as a plain decision on its signature (the old tool removed the unit by it before trying the
    chain); only the chain is left out (counted)."""
    rows = []
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
            raise SourceError(f"{name}: junk row {r['id']} has origin {r['origin']!r}, not one this program knows")
        tiles = tuple(int(t, 16) for t in r["tiles"].split(":"))
        key = images.sig_to_str((tiles, r["aspect"]))
        rows.append(_row(r["series"], r["origin"], key, r, f"{name} junk row {r['id']}"))
    for r in src.execute(
        "SELECT id, series, vec, aspect, decision, added FROM frames WHERE superseded IS NULL"
    ).fetchall():
        key = frames.key_text(frames.from_bytes(r["vec"], r["aspect"]))
        rows.append(_row(r["series"], "frame", key, r, f"{name} frames row {r['id']}"))
    skipped["marks"] = src.execute("SELECT count(*) FROM marks WHERE superseded IS NULL").fetchone()[0]
    return rows, skipped


def _row(series, kind, key, r, source):
    if r["decision"] not in VERDICT:
        raise SourceError(f"{source}: decision {r['decision']!r} is neither approve nor keep")
    return {
        "series_key": store.GLOBAL if series is None else series,
        "kind": kind,
        "key": key,
        "verdict": VERDICT[r["decision"]],
        "decider": "import",
        "decided_at": bulk_time(r["added"]),
        "source": source,
    }


def record_rows(record):
    """Rows to upsert from a bulk-converter series record: schema 4 carries the decision rows its runs used
    (series.decisions.rows); a schema-3 record carries none it can give (its decisions are in the store). Cut rows are
    left out and chains dropped, as in the store import. RecordError on a malformed row."""
    if record["schema"] != record_reader.SCHEMA:
        return []
    block = record["series"]["decisions"]
    try:
        junk, frame_rows = block["rows"]["junk"], block["rows"]["frames"]
        out = []
        for r in junk:
            if r["origin"] == "cut":
                continue
            if r["origin"] not in UNIT_ORIGINS:
                raise SourceError(f"origin {r['origin']!r}")
            tiles, aspect = r["key"]
            sig = images.sig_to_str((tuple(int(t, 16) for t in tiles.split(":")), float(aspect)))
            out.append(_row(r["series"], r["origin"], sig, r, f"record junk row {r['row']}"))
        for r in frame_rows:
            vec, aspect = r["key"]
            key = frames.key_text(frames.from_bytes(base64.b64decode(vec["b64"]), aspect))
            out.append(_row(r["series"], "frame", key, r, f"record frames row {r['row']}"))
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        why = f"{type(e).__name__}: {e}"
        raise record_reader.RecordError(f"record series.decisions.rows: malformed row ({why})") from None
    return out


def pipeline_rows(src):
    """Rows of another pipeline database, with their deciders, times and sources."""
    return [
        {c: r[c] for c in ("series_key", "kind", "key", "verdict", "decider", "decided_at", "source")}
        for r in src.execute("SELECT * FROM junk_decision ORDER BY id")
    ]


SEEN_KINDS = ("block", "page", "band")  # the bulk converter's sightings counted for recurrence (its kinds = ours)
IMPORTED_SEEN_AT = "1970-01-01T00:00:00+00:00"  # the bulk converter saw them before any pipeline scan


def bulk_seen(src):
    """junk_seen rows of a bulk-converter store's sightings of edge blocks, pages and bands: (series key, kind, key,
    chapter identity, IMPORTED_SEEN_AT, 'import'). The chapter is the identity of its archive name, as the baseline
    names sources."""
    rows = src.execute(
        f"""SELECT DISTINCT series, chapter, kind, tiles, aspect FROM junk_seen
        WHERE kind IN ({", ".join("?" * len(SEEN_KINDS))})""",
        SEEN_KINDS,
    )
    out = []
    for r in rows:
        tiles = tuple(int(t, 16) for t in r["tiles"].split(":"))
        identity = names.identity(names.parse(Path(r["chapter"]).stem))
        out.append(
            (r["series"], r["kind"], images.sig_to_str((tiles, r["aspect"])), identity, IMPORTED_SEEN_AT, "import")
        )
    return sorted(set(out))


def tables(src):
    return {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


DECISION_COLUMNS = ("series_key", "kind", "key", "verdict", "decider", "decided_at", "source")
PLACES = ", ".join("?" * len(DECISION_COLUMNS))


def run(conn, path, own_database):
    """Rebuild the imported rows from the database at `path` (module docstring). Returns {"added", "removed",
    "changed", "unchanged" (decisions, compared by key with the rows before), "pipeline_kept" (source rows left out
    because the pipeline decided their key), "seen" (imported sightings now), "skipped": {what: count}}.
    own_database: this program's database file, refused as a source."""
    path = Path(path).resolve()
    if path == Path(own_database).resolve():
        raise SourceError(f"{path} is this pipeline's own database")
    with closing(snapshot(path)) as src:
        found = tables(src)
        try:
            if {"junk", "frames", "marks", "junk_seen"} <= found:
                (rows, skipped), seen = bulk_rows(src, path.name), bulk_seen(src)
            elif {"junk_decision", "schema_step"} <= found:
                rows, skipped, seen = pipeline_rows(src), {}, None
            else:
                raise SourceError(f"{path} is neither a bulk-converter junk store nor a pipeline database")
        except sqlite3.Error as e:
            raise SourceError(f"{path}: its tables are not the expected ones: {e}") from e
    rows = winners(rows)
    with db.transaction(conn):
        before = {_ident(r): _value(r) for r in conn.execute("SELECT * FROM junk_decision WHERE decider = 'import'")}
        conn.execute("DELETE FROM junk_decision WHERE decider = 'import'")
        own = {_ident(r) for r in conn.execute("SELECT * FROM junk_decision")}
        kept = [r for r in rows if _ident(r) not in own]
        conn.executemany(
            f"INSERT INTO junk_decision ({', '.join(DECISION_COLUMNS)}) VALUES ({PLACES})",
            [tuple(r[c] for c in DECISION_COLUMNS) for r in kept],
        )
        after = {_ident(r): _value(r) for r in kept if r["decider"] == "import"}
        if seen is not None:
            conn.execute("DELETE FROM junk_seen WHERE origin = 'import'")
            conn.executemany(
                """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
                VALUES (?, ?, ?, ?, ?, ?)""",
                seen,
            )
        seen_now = conn.execute("SELECT count(*) FROM junk_seen WHERE origin = 'import'").fetchone()[0]
    return {
        "added": len(after.keys() - before.keys()),
        "removed": len(before.keys() - after.keys()),
        "changed": sum(before[k] != after[k] for k in before.keys() & after.keys()),
        "unchanged": sum(before[k] == after[k] for k in before.keys() & after.keys()),
        "pipeline_kept": len(rows) - len(kept),
        "seen": seen_now,
        "skipped": skipped,
    }


def _ident(r):
    return (r["series_key"], r["kind"], r["key"])


def _value(r):
    return (r["verdict"], r["decided_at"], r["source"])
