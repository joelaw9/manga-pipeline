"""The SQLite database (SPEC "State"): connection settings and the ordered schema steps that build it.

A schema step is a SQL file in schema/, applied in name order and recorded in schema_step, so steps added on 2
branches are both applied whatever order they merge in.
"""

import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from manga_pipeline import config

SCHEMA = Path(__file__).with_name("schema")
BUSY_TIMEOUT_S = 30
# Workflow-state tables, emptied by `state reset` in this order (children first). A step adding one adds it here.
WORKFLOW_TABLES = ("page", "chapter")


class SchemaError(RuntimeError):
    pass


def connect(cfg):
    """Open database.path, creating it if absent, and apply the schema steps it lacks. The connection is in
    autocommit mode (every write goes in an explicit transaction()) and returns sqlite3.Row rows. Raises
    SchemaError for a database holding a step this program does not have."""
    path = config.abs_path(cfg["database"]["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, autocommit=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_steps(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


def read_only(cfg):
    """Open the existing database.path for reading only: nothing is created, applied or written. Raises
    FileNotFoundError when there is no database, and SchemaError unless it holds exactly this program's steps."""
    path = config.abs_path(cfg["database"]["path"])
    conn = _open_read_only(path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_step'").fetchone():
            raise SchemaError(f"{path}: holds no schema record (empty, or not built by this program)")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_step")}
        steps = {p.stem for p in SCHEMA.glob("*.sql")}
        if applied != steps:
            unknown, missing = sorted(applied - steps), sorted(steps - applied)
            raise SchemaError(
                f"{path}: schema steps differ from this program's (unknown: {', '.join(unknown) or 'none'};"
                f" not applied yet: {', '.join(missing) or 'none'}; a part's run applies them)"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def peek_id(cfg):
    """The id of the existing database at database.path, read without creating, applying or checking anything.
    Raises FileNotFoundError when there is none, sqlite3.Error when the id cannot be read."""
    with closing(_open_read_only(config.abs_path(cfg["database"]["path"]))) as conn:
        return database_id(conn)


def _open_read_only(path):
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S, autocommit=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn):
    """1 write transaction: committed when the block ends, rolled back when it raises. IMMEDIATE takes the write lock
    at the start, so a busy database waits there (busy timeout) instead of failing mid-transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def database_id(conn):
    """This database's random id, made when step 0001 built it."""
    return conn.execute("SELECT value FROM meta WHERE key = 'database_id'").fetchone()[0]


def _apply_steps(conn, path):
    steps = {p.stem: p for p in SCHEMA.glob("*.sql")}
    # 1 transaction from reading the record to the last step: 2 runs opening the database at once apply each step once.
    with transaction(conn):
        has_record = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_step'"
        ).fetchone()
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_step")} if has_record else set()
        unknown = sorted(applied - steps.keys())
        if unknown:
            raise SchemaError(f"{path}: holds schema step(s) this program does not have: {', '.join(unknown)}")
        for name in sorted(steps.keys() - applied):
            conn.executescript(steps[name].read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_step (name, applied_at) VALUES (?, ?)", (name, _now()))


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")
