"""Run locks (SPEC "Run lock"): session advisory locks of the database, taken on the command's 1 connection.

They hold across machines, and the server frees them when the connection ends.

A lock's key is 2 integers: NAMESPACE and the server's hashtext of the lock's name, so every machine computes the
same key and none collides with the write lock (db.WRITE_LOCK, 1 integer). IDLE is the "nothing else is running"
lock: parts and junk review hold it shared, the state commands exclusive.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from manga_pipeline import config, db
from manga_pipeline.db import sql

NAMESPACE = 0x6D70  # "mp"
IDLE = "nothing-else-running"
SHARED, EXCLUSIVE = "shared", "exclusive"


class LockHeld(RuntimeError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """Another session holds a lock the command asked for; the message names that lock."""


@contextmanager
def held(conn: db.Connection, cfg: config.Config, names: Iterable[str], idle: str | None = None) -> Iterator[None]:
    """Hold the named locks, and IDLE in mode idle (SHARED, EXCLUSIVE or None), for the block when lock.enabled.

    All or nothing: when another session holds one, the ones already taken are freed and LockHeld names the held
    one, at once.
    """
    if idle not in (None, SHARED, EXCLUSIVE):
        msg = f"idle must be {SHARED!r}, {EXCLUSIVE!r} or None, got {idle!r}"
        raise ValueError(msg)
    if not cfg["lock"]["enabled"]:
        yield
        return
    wanted = [(name, False) for name in names] + ([(IDLE, idle == SHARED)] if idle else [])
    taken: list[tuple[str, bool]] = []
    try:
        for name, shared in wanted:
            fn = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
            if not db.one_row(
                conn, sql.SQL("SELECT {fn}(%s, hashtext(%s))").format(fn=sql.Identifier(fn)), (NAMESPACE, name)
            )[0]:
                what = "a running command holds" if name == IDLE else "another run holds"
                msg = f"{what} the lock {name!r}"
                raise LockHeld(msg)
            taken.append((name, shared))
        yield
    finally:
        for name, shared in reversed(taken):
            if not conn.closed:
                fn = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
                conn.execute(f"SELECT {fn}(%s, hashtext(%s))", (NAMESPACE, name))


def held_sql(name: sql.Composable) -> sql.Composed:
    """Return, in SQL, whether some session holds the run lock `name` exclusive in this database.

    name is the SQL expression (a Composable) giving the lock's name: sql.Placeholder() for a parameter,
    sql.Identifier for a column.
    """
    return sql.SQL("""EXISTS (SELECT 1 FROM pg_locks l
    WHERE l.locktype = 'advisory' AND l.granted AND l.mode = 'ExclusiveLock' AND l.objsubid = 2
        AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
        AND l.classid = {namespace} AND l.objid::bigint = (hashtext({name})::bigint & 4294967295))""").format(
        namespace=sql.Literal(NAMESPACE), name=name
    )
