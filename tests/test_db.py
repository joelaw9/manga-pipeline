"""db: the login and its settings, schema steps applied once each in name order, an unknown step refused, the write
lock, read_only's 1 snapshot, rows that read as sqlite3.Row does (SPEC "State", "Config"); and the test harness's
databases (SPEC "Tests")."""

import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing

import psycopg
import pytest
import yaml

from manga_pipeline import cli, config, db

from tests import pg
from tests.builders import one, present
from tests.timing import budget

SHIPPED = db.SCHEMA
SHIPPED_STEPS = sorted(p.stem for p in SHIPPED.glob("*.sql"))
BUILD = 3.0  # budget of a test that builds a database from no schema (8 steps), in its body or its teardown


def db_cfg(tmp_path, *, built=True):
    return pg.cfg_for(pg.current().database(tmp_path.resolve(), built))


def steps_folder(tmp_path, *extra):
    """A schema folder holding the shipped steps plus extra (name, sql) steps."""
    folder = tmp_path / "schema"
    shutil.copytree(SHIPPED, folder, dirs_exist_ok=True)
    for name, sql in extra:
        (folder / f"{name}.sql").write_text(sql, encoding="utf-8")
    return folder


def step_names(conn):
    return [row[0] for row in conn.execute("SELECT name FROM schema_step ORDER BY name")]


def tables(conn):
    return {row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


@budget(load="db")
def test_new_database_has_every_table_and_an_id(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        want = {"schema_step", "meta", "series", "chapter", "page", "handled_chapter", "published_file"}
        assert want <= tables(conn), "step 0001 builds the skeleton's tables"
        assert set(db.WORKFLOW_TABLES) <= tables(conn), "state reset empties tables that exist"
        identity = db.database_id(conn)
        assert len(identity) == 32, "the id is 32 hex characters"
        assert set(identity) <= set("0123456789abcdef"), "the id is 32 hex characters"
        assert not db.in_transaction(conn), "no transaction is left open after connect"


@budget(load="db")
def test_settings_are_in_effect(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        assert conn.autocommit, "every write goes in an explicit transaction()"
        encoding, collate, ctype = present(
            conn.execute(
                "SELECT pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname = %s",
                (conn.info.dbname,),
            ).fetchone()
        )
        assert (encoding, collate, ctype) == ("UTF8", "C", "C"), "UTF-8 with bytewise text order"
        isolation = one(conn, "SHOW default_transaction_isolation")
        assert isolation == "read committed", "a write transaction sees what committed before its write lock"
        assert "default_transaction_isolation" in conn.info.options, "set by the connection, not left to the server"


@budget(load="db")
def test_a_database_that_opens_at_once_needs_no_login_to_postgres(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    real, logins = psycopg.Connection.connect.__func__, []

    def spy(cls, *args, **kwargs):
        logins.append(kwargs["dbname"])
        return real(cls, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(spy))
    db.connect(cfg).close()
    db.open_database(cfg, builds=False).close()
    assert logins, "the spy sees every login"
    assert db.MAINTENANCE_DATABASE not in logins, f"only the configured database is logged in to: {logins}"


@budget(load="db")
def test_a_password_file_named_in_the_environment_is_not_read(tmp_path, monkeypatch):
    passfile = tmp_path / "pgpass"
    passfile.write_text("*:*:*:*:from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        used = {o.keyword: o.val for o in conn.pgconn.info}[b"passfile"]
    assert used == os.devnull.encode(), "the login's password is database.password alone"


@budget(load="db")
def test_text_orders_bytewise(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        with db.transaction(conn):
            db.executemany(conn, "INSERT INTO meta (key, value) VALUES (%s, 'x')", [("a",), ("B",), ("_",), ("é",)])
        keys = [r[0] for r in conn.execute("SELECT key FROM meta WHERE key <> 'database_id' ORDER BY key")]
    assert keys == ["B", "_", "a", "é"], "text compares by its UTF-8 bytes, as in SQLite"


@budget(load="db")
def test_reopen_keeps_data_and_id(tmp_path):
    cfg = db_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        first_id = db.database_id(conn)
        with db.transaction(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'kept')")
    with closing(db.connect(cfg)) as conn:
        assert db.database_id(conn) == first_id, "re-opening must not rebuild the database"
        assert one(conn, "SELECT value FROM meta WHERE key = 'probe'") == "kept"
        assert step_names(conn) == SHIPPED_STEPS


@budget(load="db")
def test_transaction_rolls_back_when_the_block_raises(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn:

        def failing_block():
            with db.transaction(conn):
                conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")
                msg = "stage failed"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="stage failed"):
            failing_block()
        assert conn.execute("SELECT 1 FROM meta WHERE key = 'probe'").fetchone() is None
        assert not db.in_transaction(conn)


@budget(load="db")
def test_a_transaction_inside_a_transaction_is_refused(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn, db.transaction(conn):
        assert db.in_transaction(conn)
        with pytest.raises(RuntimeError, match="already open"), db.transaction(conn):
            pass


@budget(1.5, load="db")
def test_2_write_transactions_never_overlap(tmp_path):
    cfg = db_cfg(tmp_path)
    spans = []

    def writer():
        with closing(db.connect(cfg)) as conn, db.transaction(conn):
            start = time.perf_counter()
            time.sleep(0.2)
            spans.append((start, time.perf_counter()))

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (_, end1), (start2, _) = sorted(spans)
    assert start2 >= end1, "the write lock makes the 2nd write transaction wait for the 1st to end"


@budget(BUILD, load="db")
def test_2_runs_opening_a_new_database_at_once_apply_each_step_once(tmp_path):
    cfg = db_cfg(tmp_path, built=False)
    errors = []

    def opener():
        try:
            db.connect(cfg).close()
        except Exception as e:  # reported by the assert below
            errors.append(e)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"both opens succeed: {errors}"
    with closing(db.connect(cfg)) as conn:
        assert step_names(conn) == SHIPPED_STEPS, "each step applied and recorded once"
        assert one(conn, "SELECT count(*) FROM meta WHERE key = 'database_id'") == 1


@budget(BUILD, load="db")
@pytest.mark.parametrize(("first", "second"), [("0098_a", "0099_b"), ("0099_b", "0098_a")])
def test_steps_from_2_branches_both_apply_whatever_merges_first(tmp_path, monkeypatch, first, second):
    cfg = db_cfg(tmp_path)
    sql = {"0098_a": "CREATE TABLE a (x BIGINT);", "0099_b": "CREATE TABLE b (x BIGINT);"}
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, (first, sql[first])))
    db.connect(cfg).close()
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, (second, sql[second])))
    with closing(db.connect(cfg)) as conn:
        assert step_names(conn) == [*SHIPPED_STEPS, "0098_a", "0099_b"]
        conn.execute("SELECT x FROM a UNION ALL SELECT x FROM b")


@budget(BUILD, load="db")
def test_database_with_an_unknown_step_is_refused(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0099_newer", "CREATE TABLE n (x BIGINT);")))
    db.connect(cfg).close()
    monkeypatch.undo()
    with pytest.raises(db.SchemaError, match="does not have: 0099_newer"):
        db.connect(cfg)


@budget(BUILD, load="db")
def test_failed_step_leaves_no_trace(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0099_bad", "CREATE TABLE ok (x BIGINT); NOT SQL;")))
    with pytest.raises(psycopg.errors.SyntaxError):
        db.connect(cfg)
    monkeypatch.undo()
    with closing(db.connect(cfg)) as conn:
        assert step_names(conn) == SHIPPED_STEPS
        assert "ok" not in tables(conn)


@budget(load="db")
def test_read_only_reads_and_never_writes(tmp_path):
    with closing(db.read_only(db_cfg(tmp_path))) as conn:
        assert len(db.database_id(conn)) == 32
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")


@budget(load="db")
def test_read_only_reads_1_snapshot_while_another_connection_commits(tmp_path):
    cfg = db_cfg(tmp_path)
    with closing(db.connect(cfg)) as writer, closing(db.read_only(cfg)) as reader:
        with reader.transaction():
            before = one(reader, "SELECT count(*) FROM meta")
            with db.transaction(writer):
                writer.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")
            assert one(reader, "SELECT count(*) FROM meta") == before, "1 transaction, 1 snapshot"
        assert one(reader, "SELECT count(*) FROM meta") == before + 1


@budget(BUILD, load="db")
def test_read_only_refuses_a_database_with_other_steps_or_none(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0099_newer", "CREATE TABLE n (x BIGINT);")))
    with pytest.raises(db.SchemaError, match="not applied yet: 0099_newer"):
        db.read_only(cfg)
    monkeypatch.undo()
    with pytest.raises(db.SchemaError, match="holds no schema record"):
        db.read_only(db_cfg(tmp_path / "empty", built=False))


@budget(load="db")
def test_a_missing_database_is_no_database_and_nothing_creates_it_but_connect():
    cfg = pg.cfg_for(f"{pg.PREFIX}missing_{secrets.token_hex(4)}")
    with pytest.raises(db.NoDatabase, match="no database"):
        db.read_only(cfg)
    with pytest.raises(db.NoDatabase):
        db.peek_id(cfg)
    assert not db.built(cfg), "a database that does not exist is not built"
    with pytest.raises(db.NoDatabase):
        db.peek_id(cfg)  # built() created nothing


@budget(BUILD, load="db")
def test_built_is_false_for_an_empty_database_and_true_once_connected(tmp_path):
    cfg = db_cfg(tmp_path, built=False)
    assert not db.built(cfg), "a database holding no schema record is not built"
    db.connect(cfg).close()
    assert db.built(cfg)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"host": ""}, "database.host must be a non-empty string"),
        ({"name": None}, "database.name must be a non-empty string"),
        ({"port": 0}, "database.port must be a whole number from 1 to 65535"),
        ({"port": 65536}, "database.port must be a whole number from 1 to 65535"),
        ({"password": 5}, "database.password must be null or a non-empty string"),
        ({"password": ""}, "database.password must be null or a non-empty string"),
    ],
)
@budget(load="pure")
def test_bad_database_settings_are_config_errors(change, error):
    cfg = {**config.cached(), "database": {**config.cached()["database"], **change}}
    with pytest.raises(config.ConfigError, match=error):
        db.connect(cfg)


@budget(load="db")
def test_a_refused_login_cannot_connect_and_never_shows_the_password():
    login = {"user": "mp_test_no_such_role", "password": "wrong-secret"}  # a role the test server does not have
    cfg = {**config.cached(), "database": {**config.cached()["database"], **login}}
    with pytest.raises(db.CannotConnect) as e:
        db.connect(cfg)
    assert "localhost:5432" in str(e.value)
    assert "mp_test_no_such_role" in str(e.value)
    assert "wrong-secret" not in str(e.value), "the message never holds the password"


@budget(load="db")
def test_a_socket_folder_needs_no_password():
    cfg = {**config.cached(), "database": {**config.cached()["database"], "host": "/nowhere", "password": None}}
    with pytest.raises(db.CannotConnect, match="/nowhere"):  # not a config error: the login is tried
        db.connect(cfg)


# Rows: every sqlite3.Row behaviour the code relies on (SPEC "State"; plan S3), compared with sqlite3.Row itself.


@pytest.fixture
def rows(tmp_path):
    query = """SELECT 1 AS a, 'x' AS "B", 3 AS a"""
    lite = sqlite3.connect(":memory:")
    lite.row_factory = sqlite3.Row
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        yield conn.execute(query).fetchone(), lite.execute(query).fetchone()
    lite.close()


@budget(load="db")
def test_a_row_reads_by_position_and_name_as_sqlite3_row_does(rows):
    row, lite = rows
    for key in (0, 1, 2, -1, -3, "a", "A", "b", "B", slice(0, 2), slice(None, None, -1)):
        assert row[key] == lite[key], f"row[{key!r}]"
    assert row["a"] == 1, "a repeated name reads its first column"
    with pytest.raises(IndexError):
        row["missing"]
    with pytest.raises(IndexError):
        row[3]


@budget(load="db")
def test_a_row_is_a_sequence_with_keys_as_sqlite3_row_is(rows):
    row, lite = rows
    assert tuple(row) == tuple(lite)
    assert list(row) == list(lite)
    assert len(row) == len(lite)
    assert row.keys() == lite.keys()
    a, b, c = row
    assert (a, b, c) == (1, "x", 3), "a row unpacks to its values"
    assert list(zip(["p", "q", "r"], row, strict=True)) == [("p", 1), ("q", "x"), ("r", 3)]
    assert bool(row), "a row with columns is true"


@budget(load="db")
def test_rows_build_a_dict_as_sqlite3_rows_do(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        pairs = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        row = present(conn.execute("SELECT key, value FROM meta").fetchone())
    assert set(pairs) == {"database_id"}, "dict(rows) of 2-column rows maps the 1st column to the 2nd"
    assert dict(row) == {"key": "database_id", "value": row["value"]}, "dict(row) maps names to values"


@budget(load="db")
def test_rows_are_equal_as_sqlite3_rows_are(tmp_path):
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        one, same = (conn.execute("SELECT 1 AS a, 2 AS b").fetchone() for _ in range(2))
        renamed = conn.execute("SELECT 1 AS a, 2 AS c").fetchone()
    assert one == same, "same columns and values: equal"
    assert hash(one) == hash(same), "same columns and values: equal"
    assert one != renamed, "other column names: not equal"
    assert one != (1, 2), "a row is not equal to a tuple"


# The test harness (SPEC "Tests").


@budget(load="db")
def test_2_databases_of_1_test_have_different_ids(tmp_path):
    with closing(db.connect(db_cfg(tmp_path / "lxc"))) as lxc, closing(db.connect(db_cfg(tmp_path / "win"))) as win:
        assert lxc.info.dbname != win.info.dbname
        assert db.database_id(lxc) != db.database_id(win)
        assert db_cfg(tmp_path / "lxc")["database"]["name"] == lxc.info.dbname, "1 folder, 1 database"


def _fill(cfg):
    with closing(db.connect(cfg)) as conn:
        with db.transaction(conn):
            conn.execute("INSERT INTO series (name, origin) VALUES ('Alpha', 'new')")
            conn.execute(
                "INSERT INTO chapter (series_id, kind, identity, stage, status, discovered_at, updated_at)"
                " VALUES (1, 'download', 'c1', 'discover', 'ready', 't', 't')"
            )
        return db.database_id(conn)


@budget(1.0, load="db")
def test_a_reset_database_equals_a_new_one(tmp_path):
    cfg = db_cfg(tmp_path)
    old_id = _fill(cfg)
    pg.current().end_test()  # what the test's teardown does
    with closing(db.connect(cfg)) as conn:
        assert one(conn, "SELECT count(*) FROM series") == 0, "every table is emptied"
        assert step_names(conn) == SHIPPED_STEPS, "the schema record is kept"
        assert db.database_id(conn) != old_id, "a reset database has a new id"
        conn.execute("INSERT INTO series (name, origin) VALUES ('Beta', 'new')")
        assert one(conn, "SELECT id FROM series") == 1, "identities start at 1 again"


@budget(BUILD, load="db")
def test_a_database_left_with_a_fake_step_or_table_is_rebuilt(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0099_fake", "CREATE TABLE fake (x BIGINT);")))
    db.connect(cfg).close()
    monkeypatch.undo()
    pg.current().end_test()
    cfg = db_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        assert step_names(conn) == SHIPPED_STEPS
        assert "fake" not in tables(conn)
        conn.execute("CREATE TABLE stray (x BIGINT)")
    pg.current().end_test()
    with closing(db.connect(db_cfg(tmp_path))) as conn:
        assert "stray" not in tables(conn), "a table no build makes is gone after the reset"


@budget(pg.LEAK_GRACE_S + 1.0, load="db")  # it waits out the grace a closed connection gets
def test_a_leaked_connection_fails_the_reset_and_is_ended(tmp_path):
    cfg = db_cfg(tmp_path)
    leaked = db.connect(cfg)
    with pytest.raises(pg.LeakedConnection, match=cfg["database"]["name"]):
        pg.current().end_test()
    with pytest.raises(psycopg.OperationalError):
        leaked.execute("SELECT 1")
    leaked.close()


POOL_BUDGET = 10.0  # a pool of its own: CREATE DATABASE and a build per database, slower while 32 workers run


@pytest.fixture
def own_pool():
    """A pool name and lock space of the test's own: it races no worker and leaves nothing in the run's pool."""
    pool, space = f"{pg.PREFIX}pooltest_{secrets.token_hex(4)}_", secrets.randbits(30)
    yield pool, space
    pg.drop_pool(pool)


@budget(POOL_BUDGET, load="db")
def test_a_held_pool_database_is_not_taken_by_another_process(own_pool):
    pool, space = own_pool
    first = pg.Process(pool, space, take=1)
    second = pg.Process(pool, space, take=1)
    try:
        assert first.slots != second.slots, "a database a live process holds goes to no other"
    finally:
        first.close()
        second.close()


@budget(POOL_BUDGET, load="db")
def test_a_database_left_dirty_by_a_dead_process_is_made_right_by_the_next(own_pool):
    pool, space = own_pool
    dead = pg.Process(pool, space, take=1)
    name = dead.slots[0]
    old_id = _fill(pg.cfg_for(name))
    with closing(pg._connect(name)) as conn:
        conn.execute("CREATE TABLE stray (x BIGINT)")
    dead.close()  # dies: its lock is freed, its database stays as it left it
    nxt = pg.Process(pool, space, take=1)
    try:
        assert nxt.slots == [name], "the freed database is taken again"
        with closing(db.connect(pg.cfg_for(name))) as conn:
            assert "stray" not in tables(conn), "rebuilt"
            assert step_names(conn) == SHIPPED_STEPS, "rebuilt"
            assert one(conn, "SELECT count(*) FROM series") == 0
            assert db.database_id(conn) != old_id
    finally:
        nxt.close()


@budget(POOL_BUDGET, load="db")
def test_a_database_with_rows_is_emptied_when_taken_and_stays_after_its_process_ends(own_pool):
    pool, space = own_pool
    first = pg.Process(pool, space, take=1)
    name = first.slots[0]
    _fill(pg.cfg_for(name))
    first.close()
    second = pg.Process(pool, space, take=1)
    try:
        with closing(db.connect(pg.cfg_for(name))) as conn:
            assert one(conn, "SELECT count(*) FROM chapter") == 0, "emptied when taken"
        with closing(pg._connect(db.MAINTENANCE_DATABASE)) as conn:
            assert conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    finally:
        second.close()


@budget(POOL_BUDGET, load="db")
def test_a_database_built_from_other_step_files_is_rebuilt_when_taken(own_pool):
    pool, space = own_pool
    first = pg.Process(pool, space, take=1)
    name = first.slots[0]
    with closing(pg._connect(name)) as conn:
        pg._set_print(first.owner, name, "other step files:" + pg._relations(conn))
        conn.execute("INSERT INTO meta (key, value) VALUES ('marker', 'from the other branch')")
    first.close()
    second = pg.Process(pool, space, take=1)
    try:
        with closing(pg._connect(name)) as conn:
            assert conn.execute("SELECT 1 FROM meta WHERE key = 'marker'").fetchone() is None
        assert pg._print_of(second.owner, name).startswith(pg.STEPS_PRINT), "rebuilt from these step files"
    finally:
        second.close()


@budget(POOL_BUDGET, load="db")
def test_a_database_whose_reset_failed_is_rebuilt_by_its_next_taker(own_pool, tmp_path, monkeypatch):
    pool, space = own_pool
    first = pg.Process(pool, space, take=1)
    try:
        name = first.database(tmp_path, built=True)
        _fill(pg.cfg_for(name))
        real = pg.Process._reset

        def failing(self, db_name, rebuild):
            if db_name == name:
                msg = "this reset fails"
                raise RuntimeError(msg)
            return real(self, db_name, rebuild)

        monkeypatch.setattr(pg.Process, "_reset", failing)
        with pytest.raises(RuntimeError, match="this reset fails"):
            first.end_test()
        monkeypatch.undo()
        assert first.slots != [name], "the failed database is retired: its slot takes another"
        assert not pg._print_of(first.owner, name), "its fingerprint is cleared: the next taker rebuilds it"
    finally:
        first.close()
    second = pg.Process(pool, space, take=1)
    try:
        assert second.slots == [name], "the freed database is taken again"
        assert pg._print_of(second.owner, name).startswith(pg.STEPS_PRINT), "rebuilt from these step files"
        with closing(db.connect(pg.cfg_for(name))) as conn:
            assert one(conn, "SELECT count(*) FROM series") == 0
    finally:
        second.close()


@budget(POOL_BUDGET, load="db")
def test_pool_databases_that_cannot_be_made_right_stop_the_run_after_max_failed_takes(own_pool, monkeypatch):
    pool, space = own_pool
    real, owners = pg._connect, []

    def connect(dbname):
        conn = real(dbname)
        owners.append(conn)
        return conn

    def failing(self, name, rebuild):
        del self, name, rebuild  # Process._reset's parameters, rebuild passed by keyword
        msg = "this reset fails"
        raise RuntimeError(msg)

    monkeypatch.setattr(pg, "_connect", connect)
    monkeypatch.setattr(pg.Process, "_reset", failing)
    try:
        with pytest.raises(RuntimeError, match=f"{pg.MAX_FAILED_TAKES} pool databases in a row"):
            pg.Process(pool, space, take=1)
    finally:
        for conn in owners:
            conn.close()
    with closing(real(db.MAINTENANCE_DATABASE)) as conn:
        made = one(conn, "SELECT count(*) FROM pg_database WHERE starts_with(datname, %s)", (pool,))
    assert made == pg.MAX_FAILED_TAKES, "it stops at the limit instead of trying every database"


# The offline rule for the database is the server's (SPEC "Tests"): only the test role logs in.


def _cli_status(tmp_path, cfg):
    path = tmp_path / "config.yaml"
    if cfg is not None:
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "manga_pipeline.cli", "--config", str(path), "status"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@budget(load="subprocess")
def test_a_cli_run_with_no_config_reaches_no_database(tmp_path):
    done = _cli_status(tmp_path, None)
    assert done.returncode == 2, "no config: a config error before any login"
    assert "config" in done.stderr, "no config: a config error before any login"


@budget(load="subprocess")
@pytest.mark.parametrize(
    "login",
    [
        {"host": "localhost", "port": 5432, "name": "manga_pipeline", "user": "root", "password": "not-a-test-secret"},
        {"host": "/var/run/postgresql", "port": 5432, "name": "manga_pipeline", "user": "root", "password": None},
    ],
)
def test_a_cli_run_with_a_production_config_fails_to_log_in(tmp_path, login):
    done = _cli_status(tmp_path, {**config.cached(), "database": login})
    assert done.returncode == 4, f"the server refuses the pipeline's own login to tests: {done.stderr}"
    assert "cannot use the database server" in done.stderr
    assert "not-a-test-secret" not in done.stderr


@budget(load="db")
def test_a_bad_database_setting_exits_2_and_a_refused_login_exits_4(tmp_path, capsys):
    base = config.cached()  # cli.main replaces the cached config with the file's

    def status(login):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({**base, "database": {**base["database"], **login}}))
        return cli.main(["--config", str(path), "status"])

    with pytest.raises(SystemExit) as exit_:
        status({"port": 0})
    assert exit_.value.code == 2, "a bad database setting is a config error"
    assert "database.port" in capsys.readouterr().err
    assert status({"user": "mp_test_no_such_role", "password": "wrong-secret"}) == 4, "a refused login exits 4"
    err = capsys.readouterr().err
    assert "cannot use the database server localhost:5432" in err
    assert "wrong-secret" not in err


@budget(load="db")
def test_a_wait_on_the_write_lock_longer_than_lock_timeout_raises(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    monkeypatch.setattr(db, "LOCK_TIMEOUT", "200ms")  # the shipped 30 s, shortened for the test
    with (
        closing(db.connect(cfg)) as holder,
        closing(db.connect(cfg)) as waiter,
        db.transaction(holder),
        pytest.raises(psycopg.errors.LockNotAvailable),
        db.transaction(waiter),
    ):
        pass


@budget(load="db")
def test_a_failed_reset_still_resets_the_tests_other_databases(tmp_path, monkeypatch):
    first, second = db_cfg(tmp_path / "a"), db_cfg(tmp_path / "b")
    _fill(second)
    real = pg.Process._reset

    def failing(self, name, rebuild):
        if name == first["database"]["name"]:
            msg = "this reset fails"
            raise RuntimeError(msg)
        return real(self, name, rebuild)

    monkeypatch.setattr(pg.Process, "_reset", failing)
    with pytest.raises(RuntimeError, match="this reset fails"):
        pg.current().end_test()
    monkeypatch.undo()
    with closing(db.connect(second)) as conn:
        assert one(conn, "SELECT count(*) FROM series") == 0, "the other database is emptied too"
