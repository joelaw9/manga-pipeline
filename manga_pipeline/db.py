"""The PostgreSQL database (SPEC "State", "Config"): the login, the ordered schema steps that build it, and the write
lock that makes write transactions run 1 at a time.

A schema step is a SQL file in schema/, applied in name order and recorded in schema_step, so steps added on 2
branches are both applied whatever order they merge in.
"""

import functools
import os
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, SupportsIndex, overload

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import RowMaker

from manga_pipeline import config

SCHEMA = Path(__file__).with_name("schema")
CONNECT_TIMEOUT_S = 10
# How long a statement waits for a lock (the write lock included) before it fails, as SQLite's busy timeout did.
LOCK_TIMEOUT = "30s"
# The key of the database-wide advisory lock every write transaction takes first (SPEC "State").
WRITE_LOCK = 0x6D616E6761
# Workflow-state tables, emptied by `state reset` in this order (children first). A step adding one adds it here,
# unless its rows belong to a chapter through a foreign key with ON DELETE CASCADE (chapter_edit): emptying chapter
# empties it.
WORKFLOW_TABLES = ("page", "chapter", "unmatched_download", "junk_candidate", "series_naming", "volume_cover_wanted")
MAINTENANCE_DATABASE = "postgres"  # where a missing database is created from

Error = psycopg.Error  # any database error, for callers that report one without importing the driver
PORT_MAX = 65535  # the highest TCP port


class SchemaError(RuntimeError):
    pass


class CannotConnectError(RuntimeError):
    """The server cannot be reached, or refuses the login or the creation of the database (exit 4). The message never
    holds the password."""


class NoDatabaseError(RuntimeError):
    """The configured database does not exist on the server (or, for a caller that does not build it, holds no schema
    record)."""


class Row(tuple):
    """A row that reads as sqlite3.Row does: by position (negative and sliced too), by column name (case-insensitive,
    the first column of a repeated name), as a sequence of values, with keys(); equal to a row of the same columns
    and values. Each column list has its own subclass (_row_class), whose _keys names the columns."""

    __slots__ = ()
    _keys: tuple[str, ...] = ()

    @overload
    def __getitem__(self, key: SupportsIndex | str) -> Any: ...
    @overload
    def __getitem__(self, key: slice) -> tuple[Any, ...]: ...
    def __getitem__(self, key: SupportsIndex | str | slice) -> Any:
        if isinstance(key, str):
            folded = key.casefold()
            for i, name in enumerate(self._keys):
                if name.casefold() == folded:
                    return tuple.__getitem__(self, i)
            msg = f"no column named {key!r}"
            raise IndexError(msg)
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._keys)

    def __eq__(self, other):
        return isinstance(other, Row) and self._keys == other._keys and tuple.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self._keys, tuple(self)))

    def __repr__(self):
        return f"Row({dict(zip(self._keys, self, strict=True))!r})"


@functools.cache
def _row_class(keys: tuple[str, ...]) -> type[Row]:
    return type("Row", (Row,), {"__slots__": (), "_keys": keys})


def _rows(cursor) -> RowMaker[Row]:
    return _row_class(tuple(c.name for c in cursor.description or ()))


def _login(cfg):
    """The 5 database settings, checked (SPEC "Config"): a config error names what is wrong."""
    settings = cfg["database"]
    for key in ("host", "name", "user"):
        if not isinstance(settings[key], str) or not settings[key]:
            msg = f"database.{key} must be a non-empty string"
            raise config.ConfigError(msg)
    port = settings["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= PORT_MAX:
        msg = f"database.port must be a whole number from 1 to 65535, got {port!r}"
        raise config.ConfigError(msg)
    if settings["password"] is not None and (not isinstance(settings["password"], str) or not settings["password"]):
        msg = "database.password must be null or a non-empty string"
        raise config.ConfigError(msg)
    return settings  # whether a login needs the password is the server's to say (SPEC "Config")


def open_connection(cfg, dbname=None):
    """A connection whose host, port, database, user and password are the database settings; no password file is read
    (a PG* variable can set only what these do not, SPEC "Config")."""
    login = _login(cfg)
    dbname = dbname or login["name"]
    try:
        return psycopg.Connection[Row].connect(
            host=login["host"],
            port=login["port"],
            dbname=dbname,
            user=login["user"],
            password=login["password"] or "",
            passfile=os.devnull,
            connect_timeout=CONNECT_TIMEOUT_S,
            application_name="manga_pipeline",
            # Read committed: a write transaction's statements after the write lock see what committed before it.
            options=f"-c lock_timeout={LOCK_TIMEOUT} -c default_transaction_isolation=read\\ committed",
            autocommit=True,
            row_factory=_rows,
        )
    except psycopg.OperationalError as e:
        failure = e
    # libpq reports a missing database inside a connection failure (no SQLSTATE once it tried 2 addresses): the
    # server's own list says which it is.
    if dbname != MAINTENANCE_DATABASE and not _exists(cfg, dbname):
        msg = f"no database {dbname} on {login['host']}:{login['port']}"
        raise NoDatabaseError(msg)
    msg = f"cannot use the database server {login['host']}:{login['port']} as {login['user']}: {failure}"
    raise CannotConnectError(msg) from None


def _exists(cfg, dbname):
    """Whether the server lists dbname; raises CannotConnectError when the server cannot be used at all."""
    with closing(open_connection(cfg, MAINTENANCE_DATABASE)) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone() is not None


def connect(cfg):
    """Open database.name, creating it if absent, and apply the schema steps it lacks. The connection is in
    autocommit mode (every write goes in an explicit transaction()) and returns Row rows. Raises SchemaError for a
    database holding a step this program does not have."""
    conn = open_database(cfg, builds=True)
    try:
        apply_steps(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_database(cfg, builds):
    """Open database.name and apply nothing (SPEC "State"). A caller that builds gets the database created when the
    server has none; for any other caller, a database that is missing or holds no schema record is NoDatabaseError."""
    try:
        conn = open_connection(cfg)
    except NoDatabaseError:
        if not builds:
            raise
        _create(cfg)
        return open_connection(cfg)
    if not builds and not _has_record(conn):
        conn.close()
        login = cfg["database"]
        msg = (
            f"no database {login['name']} on {login['host']}:{login['port']}: it holds no schema record (empty, or "
            "not built by this program)"
        )
        raise NoDatabaseError(msg)
    return conn


def read_only(cfg):
    """Open the existing database for reading only: every transaction is read-only and repeatable-read, so 1
    transaction reads 1 snapshot. Raises NoDatabaseError when there is none, and SchemaError unless it holds exactly
    this program's steps."""
    conn = open_connection(cfg)
    try:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        _check_exact_steps(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _check_exact_steps(conn):
    """SchemaError unless the database holds a schema record with exactly this program's steps."""
    if not _has_record(conn):
        msg = f"database {conn.info.dbname}: holds no schema record (empty, or not built by this program)"
        raise SchemaError(msg)
    applied = {row[0] for row in conn.execute("SELECT name FROM schema_step")}
    steps = {p.stem for p in SCHEMA.glob("*.sql")}
    if applied != steps:
        unknown, missing = sorted(applied - steps), sorted(steps - applied)
        msg = (
            f"database {conn.info.dbname}: schema steps differ from this program's (unknown: "
            f"{', '.join(unknown) or 'none'}; not applied yet: {', '.join(missing) or 'none'}; "
            "a part's run applies them)"
        )
        raise SchemaError(msg)


def built(cfg):
    """Whether the database exists and holds a schema record; builds nothing (SPEC "State")."""
    try:
        conn = open_connection(cfg)
    except NoDatabaseError:
        return False
    with closing(conn):
        return _has_record(conn)


def peek_id(cfg):
    """The id of the existing database, read without creating, applying or checking anything. Raises NoDatabaseError
    when there is none, Error when the id cannot be read."""
    with closing(open_connection(cfg)) as conn:
        return database_id(conn)


@contextmanager
def transaction(conn):
    """1 write transaction: committed when the block ends, rolled back when it raises. It takes the database-wide
    write lock first, so write transactions never overlap (SPEC "State")."""
    if in_transaction(conn):
        msg = "a transaction is already open on this connection"
        raise RuntimeError(msg)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        yield conn


def in_transaction(conn):
    return conn.info.transaction_status != TransactionStatus.IDLE


def executemany(conn, statement, rows):
    with conn.cursor() as cur:
        cur.executemany(statement, rows)


def database_id(conn):
    """This database's random id (32 hex characters), made when step 0001 built it."""
    return conn.execute("SELECT value FROM meta WHERE key = 'database_id'").fetchone()[0]


def _create(cfg):
    """Create database.name as UTF-8 with bytewise text order; another run creating it at the same time is fine.
    CannotConnectError naming the database when the server refuses (a role that may not create one, for one)."""
    login = cfg["database"]
    name = login["name"]
    with closing(open_connection(cfg, MAINTENANCE_DATABASE)) as conn:
        try:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(
                    sql.Identifier(name)
                )
            )
        except (psycopg.errors.DuplicateDatabase, psycopg.errors.UniqueViolation):
            pass  # 2 creates racing: the loser gets either error (PostgreSQL's createdb checks the name racily)
        except psycopg.Error as e:
            msg = f"cannot create the database {name} on {login['host']}:{login['port']} as {login['user']}: {e}"
            raise CannotConnectError(msg) from None


def _has_record(conn):
    return conn.execute("SELECT to_regclass('schema_step') IS NOT NULL").fetchone()[0]


def now(conn):
    """The server's clock as the program stores a time: ISO 8601 in UTC to the second ('2026-11-14T17:00:00+00:00'),
    so times written from several machines compare (SPEC "State")."""
    return conn.execute(
        """SELECT to_char(date_trunc('second', clock_timestamp()) AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')"""
    ).fetchone()[0]


def apply_steps(conn):
    """Apply the schema steps the database lacks; SchemaError for a step this program does not have."""
    steps = {p.stem: p for p in SCHEMA.glob("*.sql")}
    # Under the write lock from reading the record to the last step: 2 runs opening the database at once apply each
    # step once.
    with transaction(conn):
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_step")} if _has_record(conn) else set()
        unknown = sorted(applied - steps.keys())
        if unknown:
            msg = f"database {conn.info.dbname}: holds schema step(s) this program does not have: {', '.join(unknown)}"
            raise SchemaError(msg)
        for name in sorted(steps.keys() - applied):
            try:
                conn.execute(steps[name].read_text(encoding="utf-8"))
            except psycopg.errors.RaiseException as e:  # a step that refuses the database names why
                msg = f"database {conn.info.dbname}: step {name}: {e.diag.message_primary}"
                raise SchemaError(msg) from None
            conn.execute("INSERT INTO schema_step (name, applied_at) VALUES (%s, %s)", (name, now(conn)))
