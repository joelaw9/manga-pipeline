"""The PostgreSQL database (SPEC "State", "Config"): the login, the ordered schema steps that build it, and the write
lock that makes write transactions run 1 at a time.

A schema step is a SQL file in schema/, applied in name order and recorded in schema_step, so steps added on 2
branches are both applied whatever order they merge in.
"""

import os
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

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
WORKFLOW_TABLES = ("page", "chapter", "unmatched_download", "junk_candidate")
MAINTENANCE_DATABASE = "postgres"  # where a missing database is created from

Error = psycopg.Error  # any database error, for callers that report one without importing the driver


class SchemaError(RuntimeError):
    pass


class CannotConnect(RuntimeError):
    """The server cannot be reached, or refuses the login (exit 4). The message never holds the password."""


class NoDatabase(RuntimeError):
    """The configured database does not exist on the server."""


class Row(tuple):
    """A row that reads as sqlite3.Row does: by position (negative and sliced too), by column name (case-insensitive,
    the first column of a repeated name), as a sequence of values, with keys(); equal to a row of the same columns
    and values."""

    def __new__(cls, keys, values):
        row = super().__new__(cls, values)
        row._keys = keys
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            folded = key.casefold()
            for i, name in enumerate(self._keys):
                if name.casefold() == folded:
                    return tuple.__getitem__(self, i)
            raise IndexError(f"no column named {key!r}")
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._keys)

    def __eq__(self, other):
        return isinstance(other, Row) and self._keys == other._keys and tuple.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((tuple(self._keys), tuple(self)))

    def __repr__(self):
        return f"Row({dict(zip(self._keys, self, strict=True))!r})"


def _rows(cursor):
    keys = tuple(c.name for c in cursor.description or ())
    return lambda values: Row(keys, values)


def _login(cfg):
    """The 5 database settings, checked (SPEC "Config"): a config error names what is wrong."""
    settings = cfg["database"]
    for key in ("host", "name", "user"):
        if not isinstance(settings[key], str) or not settings[key]:
            raise config.ConfigError(f"database.{key} must be a non-empty string")
    port = settings["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise config.ConfigError(f"database.port must be a whole number from 1 to 65535, got {port!r}")
    if settings["password"] is not None and not isinstance(settings["password"], str):
        raise config.ConfigError("database.password must be a string")
    return settings  # whether a login needs the password is the server's to say (SPEC "Config")


def _open(cfg, dbname=None):
    """A connection from the database settings only: no PG* variable, service file or password file has a say."""
    login = _login(cfg)
    dbname = dbname or login["name"]
    try:
        return psycopg.connect(
            host=login["host"],
            port=login["port"],
            dbname=dbname,
            user=login["user"],
            password=login["password"] or "",
            passfile=os.devnull,
            connect_timeout=CONNECT_TIMEOUT_S,
            application_name="manga_pipeline",
            options=f"-c lock_timeout={LOCK_TIMEOUT}",
            autocommit=True,
            row_factory=_rows,
        )
    except psycopg.OperationalError as e:
        failure = e
    # libpq reports a missing database inside a connection failure (no SQLSTATE once it tried 2 addresses): the
    # server's own list says which it is.
    if dbname != MAINTENANCE_DATABASE and not _exists(cfg, dbname):
        raise NoDatabase(f"no database {dbname} on {login['host']}:{login['port']}")
    raise CannotConnect(
        f"cannot use the database server {login['host']}:{login['port']} as {login['user']}: {failure}"
    ) from None


def _exists(cfg, dbname):
    """Whether the server lists dbname; raises CannotConnect when the server cannot be used at all."""
    with closing(_open(cfg, MAINTENANCE_DATABASE)) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone() is not None


def connect(cfg):
    """Open database.name, creating it if absent, and apply the schema steps it lacks. The connection is in
    autocommit mode (every write goes in an explicit transaction()) and returns Row rows. Raises SchemaError for a
    database holding a step this program does not have."""
    try:
        conn = _open(cfg)
    except NoDatabase:
        _create(cfg)
        conn = _open(cfg)
    try:
        _apply_steps(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def read_only(cfg):
    """Open the existing database for reading only: every transaction is read-only and repeatable-read, so 1
    transaction reads 1 snapshot. Raises NoDatabase when there is none, and SchemaError unless it holds exactly this
    program's steps."""
    conn = _open(cfg)
    try:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if not _has_record(conn):
            raise SchemaError(
                f"database {conn.info.dbname}: holds no schema record (empty, or not built by this program)"
            )
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_step")}
        steps = {p.stem for p in SCHEMA.glob("*.sql")}
        if applied != steps:
            unknown, missing = sorted(applied - steps), sorted(steps - applied)
            raise SchemaError(
                f"database {conn.info.dbname}: schema steps differ from this program's (unknown: "
                f"{', '.join(unknown) or 'none'}; not applied yet: {', '.join(missing) or 'none'}; "
                "a part's run applies them)"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def built(cfg):
    """Whether the database exists and holds a schema record; builds nothing (SPEC "State")."""
    try:
        conn = _open(cfg)
    except NoDatabase:
        return False
    with closing(conn):
        return _has_record(conn)


def peek_id(cfg):
    """The id of the existing database, read without creating, applying or checking anything. Raises NoDatabase when
    there is none, Error when the id cannot be read."""
    with closing(_open(cfg)) as conn:
        return database_id(conn)


@contextmanager
def transaction(conn):
    """1 write transaction: committed when the block ends, rolled back when it raises. It takes the database-wide
    write lock first, so write transactions never overlap (SPEC "State")."""
    if in_transaction(conn):
        raise RuntimeError("a transaction is already open on this connection")
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
    """Create database.name as UTF-8 with bytewise text order; another run creating it at the same time is fine."""
    name = cfg["database"]["name"]
    with closing(_open(cfg, MAINTENANCE_DATABASE)) as conn:
        try:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(
                    sql.Identifier(name)
                )
            )
        except psycopg.errors.DuplicateDatabase:
            pass


def _has_record(conn):
    return conn.execute("SELECT to_regclass('schema_step') IS NOT NULL").fetchone()[0]


def _apply_steps(conn):
    steps = {p.stem: p for p in SCHEMA.glob("*.sql")}
    # Under the write lock from reading the record to the last step: 2 runs opening the database at once apply each
    # step once.
    with transaction(conn):
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_step")} if _has_record(conn) else set()
        unknown = sorted(applied - steps.keys())
        if unknown:
            raise SchemaError(
                f"database {conn.info.dbname}: holds schema step(s) this program does not have: {', '.join(unknown)}"
            )
        for name in sorted(steps.keys() - applied):
            conn.execute(steps[name].read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_step (name, applied_at) VALUES (%s, %s)", (name, _now()))


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")
