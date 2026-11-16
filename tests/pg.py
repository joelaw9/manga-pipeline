"""Test databases (SPEC "Tests"): a pool of databases mp_test_pool_<n> kept on the test server between runs. Each test
process takes 2 when it starts (a 3rd when a test asks for a 3rd folder), holding a session advisory lock per database
on its 1 connection to the server's maintenance database, so no other process uses them meanwhile; a process that
dies frees its locks with its connection. Taking a database makes it right (emptied, or rebuilt when it is not exactly
what a build of these step files makes), and every database a test used is emptied in that test's teardown.

Only mp_test_* databases are ever created; none is dropped but by the pool's own tests.
"""

import gc
import hashlib
import time
from contextlib import closing

import psycopg
from psycopg import sql

from manga_pipeline import config, db

PREFIX = "mp_test_"
POOL = f"{PREFIX}pool_"
LOCK_SPACE = 0x6D70  # the first key of a pool database's advisory lock; the second is its number
TAKEN_AT_START = 2  # tests with 1 or 2 folders find their databases ready
MAX_FAILED_TAKES = 3
SHIPPED = db.SCHEMA  # the program's steps; a test may patch db.SCHEMA, and its patch can outlive it into a reset
SHIPPED_STEPS = {p.stem for p in SHIPPED.glob("*.sql")}
STEPS_PRINT = hashlib.sha256(
    b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in sorted(SHIPPED.glob("*.sql")))
).hexdigest()
# Step 0001's id, made again: an emptied database has a new id, as a new one does.
NEW_ID = "INSERT INTO meta (key, value) VALUES ('database_id', replace(gen_random_uuid()::text, '-', ''))"
# The test role's client connections to a database (an autovacuum worker is not one).
OTHERS = (
    "SELECT pid, application_name FROM pg_stat_activity WHERE datname = %s AND usename = current_user"
    " AND backend_type = 'client backend'"
)
LEAK_GRACE_S = 1.0
UNASSIGNED = "mp_test_unassigned"  # tests/test_config.yaml's database.name: no test may build it

process = None  # this test process's Process, made by tests/conftest.py when the process starts


class LeakedConnection(AssertionError):
    pass


def _login():
    return config.cached()["database"]


def _connect(dbname):
    login = _login()
    return psycopg.connect(
        host=login["host"],
        port=login["port"],
        dbname=dbname,
        user=login["user"],
        password=login["password"],
        connect_timeout=db.CONNECT_TIMEOUT_S,
        application_name="manga_pipeline tests",
        autocommit=True,
    )


def cfg_for(name):
    """The test config with its database section naming database `name`."""
    return {**config.cached(), "database": {**_login(), "name": name}}


def _owned(name):
    if not name.startswith(PREFIX):
        msg = f"the test harness touches only {PREFIX}* databases, not {name!r}"
        raise ValueError(msg)
    return sql.Identifier(name)


def _relations(conn):
    """The relations of schema public (tables, sequences, indexes, views), as 1 fingerprint."""
    rows = conn.execute(
        "SELECT relkind::text || ':' || relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY 1"
    ).fetchall()
    return hashlib.sha256("\n".join(r[0] for r in rows).encode()).hexdigest()


def _print_of(conn, name):
    return conn.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s", (name,)
    ).fetchone()[0]


def _set_print(conn, name, value):
    conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(_owned(name), sql.Literal(value)))


class Process:
    """This test process's pool databases. slots[n] is the database the (n+1)-th folder of a test gets."""

    def __init__(self, pool=POOL, lock_space=LOCK_SPACE, take=TAKEN_AT_START):
        self.pool, self.lock_space = pool, lock_space
        self.owner = _connect(db.MAINTENANCE_DATABASE)
        self.held = set()  # numbers whose lock this process holds, retired ones included
        self.slots = []  # names, by slot
        self.used = {}  # this test: folder -> slot
        self.unbuilt = set()  # this test's names handed out with no schema
        self.order = None  # (tables in delete order, identity sequences), read once from an intact database
        for _ in range(take):
            self.slots.append(self._take())

    def _take(self):
        """The lowest-numbered pool database no process holds, made right: created if missing, its orphaned
        connections ended, emptied or rebuilt. A database that cannot be made right stays held, unused."""
        n, failures = 0, []
        while True:
            n += 1
            if n in self.held:  # a session lock is re-entrant: the numbers held are skipped here, not by the lock
                continue
            if not self.owner.execute("SELECT pg_try_advisory_lock(%s, %s)", (self.lock_space, n)).fetchone()[0]:
                continue
            self.held.add(n)
            name = f"{self.pool}{n}"
            if not self.owner.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                self.owner.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(
                        _owned(name)
                    )
                )
            try:
                self._end_others(name)
                self._reset(name, rebuild=False)
            except Exception as e:
                _set_print(self.owner, name, "")  # whoever takes it next rebuilds it
                failures.append(e)
                if len(failures) == MAX_FAILED_TAKES:  # a fault in the harness, not 1 bad database: stop here
                    msg = f"{MAX_FAILED_TAKES} pool databases in a row could not be made right"
                    raise RuntimeError(msg) from e
                continue
            return name

    def database(self, folder, built):
        """The database name for `folder` in the running test (SPEC "Tests": same folder, same database)."""
        if folder in self.used:
            return self.slots[self.used[folder]]
        n = len(self.used)
        while len(self.slots) <= n:
            self.slots.append(self._take())
        name = self.slots[n]
        self.used[folder] = n
        if not built:
            with closing(_connect(name)) as conn:
                conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            _set_print(self.owner, name, "")
            self.unbuilt.add(name)
        return name

    def end_test(self):
        """Empty every database the test used. Raises LeakedConnection after the reset when the test left a
        connection open; a database whose reset fails is retired (held, unused) and its slot takes another."""
        used, unbuilt = sorted(set(self.used.values())), self.unbuilt
        self.used, self.unbuilt = {}, set()
        leaks, failed = [], None
        for n in used:  # every database is reset, whichever fails
            name = self.slots[n]
            try:
                leaks += self._end_leaks(name)
                self._reset(name, rebuild=name in unbuilt)
            except Exception as e:
                _set_print(self.owner, name, "")
                self.slots[n] = self._take()
                failed = failed or e
        if failed:
            raise failed
        if leaks:
            msg = f"the test left database connections open (ended now): {', '.join(leaks)}"
            raise LeakedConnection(msg)

    def _end_leaks(self, name):
        """End and return the test's connections still open: a connection dropped but not yet collected closes with
        a collection, and a closed one's server process may outlive it for a moment, so only those still there after
        both count."""
        if not self._others(name):
            return []
        gc.collect()
        deadline = time.monotonic() + LEAK_GRACE_S
        while self._others(name) and time.monotonic() < deadline:
            time.sleep(0.05)
        return self._end_others(name)

    def _others(self, name):
        return self.owner.execute(OTHERS, (name,)).fetchall()

    def _end_others(self, name):
        """End every client connection of the test role to the database; returns them."""
        ended = []
        for pid, app in self._others(name):
            self.owner.execute("SELECT pg_terminate_backend(%s)", (pid,))
            ended.append(f"{name} ({app or 'no name'}, pid {pid})")
        return ended

    def _reset(self, name, rebuild):
        with closing(_connect(name)) as conn:
            if rebuild or not self._intact(conn, name):
                conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
                rebuild = True
            else:
                self.order = self.order or _order(conn)
                tables, sequences = self.order
                with conn.transaction():
                    for table in tables:
                        conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
                    for sequence in sequences:
                        conn.execute("SELECT setval(%s, 1, false)", (sequence,))
                    conn.execute(NEW_ID)
        if rebuild:
            self._build(name)

    def _intact(self, conn, name):
        """Built from these step files, holding exactly their steps and the relations its build made."""
        if not conn.execute("SELECT to_regclass('schema_step') IS NOT NULL").fetchone()[0]:
            return False
        steps = {row[0] for row in conn.execute("SELECT name FROM schema_step")}
        return steps == SHIPPED_STEPS and _print_of(self.owner, name) == f"{STEPS_PRINT}:{_relations(conn)}"

    def _build(self, name):
        patched, db.SCHEMA = db.SCHEMA, SHIPPED
        try:
            db.connect(cfg_for(name)).close()
        finally:
            db.SCHEMA = patched
        with closing(_connect(name)) as conn:
            _set_print(self.owner, name, f"{STEPS_PRINT}:{_relations(conn)}")

    def close(self):
        """Free this process's databases for the next run: they stay on the server."""
        self.owner.close()


def _order(conn):
    """The tables but schema_step, children before parents, and the identity sequences."""
    tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    tables.discard("schema_step")
    parents = {t: set() for t in tables}
    for child, parent in conn.execute(
        "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint WHERE contype = 'f'"
    ).fetchall():
        if child != parent:
            parents[child].add(parent)
    order = []
    while parents:
        leaves = sorted(t for t in parents if not any(t in ps for ps in parents.values()))
        if not leaves:
            msg = f"foreign keys form a cycle among {sorted(parents)}"
            raise RuntimeError(msg)
        order += leaves
        for t in leaves:
            del parents[t]
    sequences = [
        row[0]
        for row in conn.execute(
            "SELECT pg_get_serial_sequence(attrelid::regclass::text, attname) FROM pg_attribute"
            " WHERE attidentity <> '' AND attrelid::regclass::text = ANY(%s)",
            (sorted(tables),),
        )
    ]
    return order, sorted(sequences)


def drop_pool(pool):
    """Drop every database of `pool` (the pool's own tests clean up after themselves; the run's pool stays)."""
    if pool == POOL:
        msg = "the run's pool is never dropped by the harness"
        raise ValueError(msg)
    with closing(_connect(db.MAINTENANCE_DATABASE)) as conn:
        names = [r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE starts_with(datname, %s)", (pool,))]
        for name in names:
            conn.execute(sql.SQL("DROP DATABASE {}").format(_owned(name)))


def unassigned_built():
    """Whether a test built the database the test config names (dropped here when it did)."""
    with closing(_connect(db.MAINTENANCE_DATABASE)) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (UNASSIGNED,)).fetchone():
            return False
        conn.execute(sql.SQL("DROP DATABASE {}").format(_owned(UNASSIGNED)))
        return True
