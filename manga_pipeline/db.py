"""The PostgreSQL database (SPEC "State", "Config").

The login, the ordered schema steps that build it, and the write lock that makes write transactions run 1 at a time.

A schema step is a SQL file in schema/, applied in name order and recorded in schema_step, so steps added on 2
branches are both applied whatever order they merge in.
"""

import functools
import os
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, LiteralString, SupportsIndex, overload

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
    """The database's schema steps are not this program's.

    An unknown step, one that refuses it, a missing record, or (read_only) steps not applied yet.
    """


class CannotConnectError(RuntimeError):
    """The server cannot be reached, or refuses the login or the creation of the database (exit 4).

    The message never holds the password.
    """


class NoDatabaseError(RuntimeError):
    """The configured database does not exist on the server.

    Or, for a caller that does not build it, it holds no schema record.
    """


class MissingRowError(LookupError):
    """A query returned no row where the caller's own data guarantees one (a row named by a stored id, an aggregate)."""


# Any: a column's value is whatever type its column holds, which only the query knows (as __getitem__'s).
class Row(tuple[Any, ...]):  # pyright: ignore[reportExplicitAny]
    """A row that reads as sqlite3.Row does.

    By position (negative and sliced too), by column name (case-insensitive, the first column of a repeated name), as a
    sequence of values, with keys(); equal to a row of the same columns and values. Each column list has its own
    subclass (_row_class), whose _keys names the columns.
    """

    __slots__ = ()
    _keys: tuple[str, ...] = ()

    # Any (ANN401, reportExplicitAny): a column's value is whatever type its column holds, which only the query knows.
    @overload
    def __getitem__(self, key: SupportsIndex | str) -> Any: ...  # pyright: ignore[reportExplicitAny]  # noqa: ANN401
    @overload
    def __getitem__(self, key: slice) -> tuple[Any, ...]: ...  # pyright: ignore[reportExplicitAny]
    def __getitem__(self, key: SupportsIndex | str | slice) -> Any:  # pyright: ignore[reportExplicitAny]
        """Return a value by position or slice, or by column name (case-insensitive); IndexError for an unknown name."""
        if isinstance(key, str):
            folded = key.casefold()
            for i, name in enumerate(self._keys):
                if name.casefold() == folded:
                    return super().__getitem__(i)
            msg = f"no column named {key!r}"
            raise IndexError(msg)
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        """Return the column names, in order."""
        return list(self._keys)

    def __eq__(self, other: object) -> bool:
        """Return whether other is a Row of the same columns and values."""
        return isinstance(other, Row) and self._keys == other._keys and super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        """Return whether other is not a Row of the same columns and values."""
        return not self.__eq__(other)

    def __hash__(self) -> int:
        """Return a hash of the columns and values, so equal rows hash alike."""
        return hash((self._keys, tuple(self)))

    def __repr__(self) -> str:
        """Return the row as Row({column: value})."""
        return f"Row({dict(zip(self._keys, self, strict=True))!r})"


# A connection of the program (open_connection), for callers that annotate one without importing the driver.
type Connection = psycopg.Connection[Row]
# A statement as the program writes one: a literal, or one composed with psycopg.sql (no text built at run time).
type Query = LiteralString | sql.SQL | sql.Composed
# A statement's parameters: by position or by name, each any value psycopg adapts.
type Params = Sequence[object] | Mapping[str, object]


@functools.cache
def _row_class(keys: tuple[str, ...]) -> type[Row]:
    return type("Row", (Row,), {"__slots__": (), "_keys": keys})


def row(**columns: object) -> Row:
    """Return a Row of these columns and values, as a query returns one: a stored row's stand-in."""
    return _row_class(tuple(columns))(columns.values())


def _rows(cursor: psycopg.Cursor[Row]) -> RowMaker[Row]:
    return _row_class(tuple(c.name for c in cursor.description or ()))


def _login(cfg: config.Config) -> config.DatabaseConfig:
    """Return the 5 database settings, checked (SPEC "Config"): a config error names what is wrong."""
    settings = cfg["database"]
    for key in ("host", "name", "user"):
        if not isinstance(settings[key], str) or not settings[key]:
            msg = f"database.{key} must be a non-empty string"
            raise config.ConfigError(msg)
    port = settings["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= PORT_MAX:
        msg = f"database.port must be a whole number from 1 to {PORT_MAX}, got {port!r}"
        raise config.ConfigError(msg)
    if settings["password"] is not None and (not isinstance(settings["password"], str) or not settings["password"]):
        msg = "database.password must be null or a non-empty string"
        raise config.ConfigError(msg)
    return settings  # whether a login needs the password is the server's to say (SPEC "Config")


def open_connection(cfg: config.Config, dbname: str | None = None) -> Connection:
    """Open a connection whose host, port, database, user and password are the database settings.

    No password file is read (a PG* variable can set only what these do not, SPEC "Config").
    """
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


def _exists(cfg: config.Config, dbname: str) -> bool:
    """Whether the server lists dbname; raises CannotConnectError when the server cannot be used at all."""
    with closing(open_connection(cfg, MAINTENANCE_DATABASE)) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone() is not None


def connect(cfg: config.Config) -> Connection:
    """Open database.name, creating it if absent, and apply the schema steps it lacks.

    The connection is in autocommit mode (every write goes in an explicit transaction()) and returns Row rows. Raises
    SchemaError for a database holding a step this program does not have.
    """
    conn = open_database(cfg, builds=True)
    try:
        apply_steps(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_database(cfg: config.Config, *, builds: bool) -> Connection:
    """Open database.name and apply nothing (SPEC "State").

    A caller that builds gets the database created when the server has none; for any other caller, a database that is
    missing or holds no schema record is NoDatabaseError.
    """
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


def read_only(cfg: config.Config) -> Connection:
    """Open the existing database for reading only.

    Every transaction is read-only and repeatable-read, so 1 transaction reads 1 snapshot. Raises NoDatabaseError when
    there is none, and SchemaError unless it holds exactly this program's steps.
    """
    conn = open_connection(cfg)
    try:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        _check_exact_steps(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _check_exact_steps(conn: Connection) -> None:
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


def built(cfg: config.Config) -> bool:
    """Whether the database exists and holds a schema record; builds nothing (SPEC "State")."""
    try:
        conn = open_connection(cfg)
    except NoDatabaseError:
        return False
    with closing(conn):
        return _has_record(conn)


def peek_id(cfg: config.Config) -> str:
    """Return the id of the existing database, read without creating, applying or checking anything.

    Raises NoDatabaseError when there is none, Error when the id cannot be read.
    """
    with closing(open_connection(cfg)) as conn:
        return database_id(conn)


@contextmanager
def transaction(conn: Connection) -> Generator[Connection]:
    """Run 1 write transaction: committed when the block ends, rolled back when it raises.

    It takes the database-wide write lock first, so write transactions never overlap (SPEC "State").
    """
    if in_transaction(conn):
        msg = "a transaction is already open on this connection"
        raise RuntimeError(msg)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        yield conn


def in_transaction(conn: Connection) -> bool:
    """Return whether the connection is inside a transaction (its status is not idle)."""
    return conn.info.transaction_status != TransactionStatus.IDLE


def executemany(conn: Connection, statement: Query, rows: Iterable[Params]) -> None:
    """Execute the statement once per row, on 1 cursor of the connection."""
    with conn.cursor() as cur:
        cur.executemany(statement, rows)


def one_row(conn: Connection, query: Query, params: Params = ()) -> Row:
    """Return the query's 1st row, where the caller's own data guarantees one.

    MissingRowError naming the query if none.
    """
    row = conn.execute(query, params).fetchone()
    if row is None:
        text = query.as_string(conn) if isinstance(query, sql.Composable) else query
        msg = f"no row where one must be: {text!r} with {params!r}"
        raise MissingRowError(msg)
    return row


def database_id(conn: Connection) -> str:
    """Return the database's random id (32 hex characters), made when step 0001 built it."""
    return one_row(conn, "SELECT value FROM meta WHERE key = 'database_id'")[0]


def _create(cfg: config.Config) -> None:
    """Create database.name as UTF-8 with bytewise text order; another run creating it at the same time is fine.

    CannotConnectError naming the database when the server refuses (a role that may not create one, for one).
    """
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


def _has_record(conn: Connection) -> bool:
    return one_row(conn, "SELECT to_regclass('schema_step') IS NOT NULL")[0]


def now(conn: Connection) -> str:
    """Return the server's clock as the program stores a time: ISO 8601 in UTC to the second.

    As '2026-11-14T17:00:00+00:00', so times written from several machines compare (SPEC "State").
    """
    return one_row(
        conn,
        """SELECT to_char(date_trunc('second', clock_timestamp()) AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')""",
    )[0]


def apply_steps(conn: Connection) -> None:
    """Apply the schema steps the database lacks; SchemaError for a step this program does not have."""
    steps = {p.stem: p for p in SCHEMA.glob("*.sql")}
    # Under the write lock from reading the record to the last step: 2 runs opening the database at once apply each
    # step once.
    with transaction(conn):
        applied: set[str] = (
            {row[0] for row in conn.execute("SELECT name FROM schema_step")} if _has_record(conn) else set()
        )
        unknown = sorted(applied - steps.keys())
        if unknown:
            msg = f"database {conn.info.dbname}: holds schema step(s) this program does not have: {', '.join(unknown)}"
            raise SchemaError(msg)
        for name in sorted(steps.keys() - applied):
            try:
                # As bytes: psycopg takes a str query only as a literal, and a step is the program's own SQL file.
                conn.execute(steps[name].read_bytes())
            except psycopg.errors.RaiseException as e:  # a step that refuses the database names why
                msg = f"database {conn.info.dbname}: step {name}: {e.diag.message_primary}"
                raise SchemaError(msg) from None
            conn.execute("INSERT INTO schema_step (name, applied_at) VALUES (%s, %s)", (name, now(conn)))
