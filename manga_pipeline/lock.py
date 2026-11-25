"""Run locks (SPEC "Run lock"): session advisory locks of the database, taken on the command's 1 connection.

They hold across machines, and the server frees them when the connection ends.

A lock's key is 2 integers: NAMESPACE and the server's hashtext of the lock's name, so every machine computes the
same key and none collides with the write lock (db.WRITE_LOCK, 1 integer). IDLE is the "nothing else is running"
lock: parts and junk review hold it shared, the state commands exclusive. AUTOMATION is held shared by every scheduled
run (cli --scheduled), which reads the automation's pause only once it holds it (SPEC "Run lock").
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from manga_pipeline import config, db
from manga_pipeline.db import sql

NAMESPACE = 0x6D70  # "mp"
IDLE = "nothing-else-running"
AUTOMATION = "automation"  # held shared by every run the timers and tasks start (--scheduled); state pause polls it
SHARED, EXCLUSIVE = "shared", "exclusive"


class LockHeld(RuntimeError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """Another session holds a lock the command asked for; the message names that lock."""


@contextmanager
def held(
    conn: db.Connection, cfg: config.Config, names: Iterable[str], idle: str | None = None, *, automation: bool = False
) -> Iterator[None]:
    """Hold the named locks, IDLE in mode idle (SHARED, EXCLUSIVE or None), and AUTOMATION shared when automation.

    For the block, when lock.enabled.

    All or nothing: when another session holds one, the ones already taken are freed and LockHeld names the held
    one, at once.
    """
    if idle not in (None, SHARED, EXCLUSIVE):
        msg = f"idle must be {SHARED!r}, {EXCLUSIVE!r} or None, got {idle!r}"
        raise ValueError(msg)
    if not cfg["lock"]["enabled"]:
        yield
        return
    wanted = (
        [(name, False) for name in names]
        + ([(IDLE, idle == SHARED)] if idle else [])
        + ([(AUTOMATION, True)] if automation else [])
    )
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
    return sql.SQL("""EXISTS (SELECT 1 FROM pg_locks l WHERE {key} AND l.mode = 'ExclusiveLock')""").format(
        key=_granted_here(name)
    )


def holders_sql(name: sql.Composable) -> sql.Composed:
    """Return, in SQL, how many other sessions hold the run lock `name` in this database, in any mode.

    name as for held_sql. This session's own hold is not counted.
    """
    return sql.SQL(
        """(SELECT count(DISTINCT l.pid) FROM pg_locks l WHERE {key} AND l.pid <> pg_backend_pid())"""
    ).format(key=_granted_here(name))


def _granted_here(name: sql.Composable) -> sql.Composed:
    """Return the SQL condition on pg_locks row l: a granted hold of the run lock `name` in this database."""
    return sql.SQL("""l.locktype = 'advisory' AND l.granted AND l.objsubid = 2
        AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
        AND l.classid = {namespace} AND l.objid::bigint = (hashtext({name})::bigint & 4294967295)""").format(
        namespace=sql.Literal(NAMESPACE), name=name
    )
