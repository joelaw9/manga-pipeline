"""db.connect: settings in effect, schema steps applied once each in name order, an unknown step refused (SPEC
"State")."""

import shutil
import sqlite3

import pytest

from manga_pipeline import config, db

from tests.timing import budget

SHIPPED = db.SCHEMA


def db_cfg(tmp_path):
    return {**config.cached(), "database": {"path": str(tmp_path / "pipeline.sqlite3")}}


def steps_folder(tmp_path, *extra):
    """A schema folder holding the shipped steps plus extra (name, sql) steps."""
    folder = tmp_path / "schema"
    shutil.copytree(SHIPPED, folder, dirs_exist_ok=True)
    for name, sql in extra:
        (folder / f"{name}.sql").write_text(sql, encoding="utf-8")
    return folder


def step_names(conn):
    return [row[0] for row in conn.execute("SELECT name FROM schema_step ORDER BY name")]


@budget(load="db")
def test_new_database_has_every_table_and_an_id(tmp_path):
    conn = db.connect(db_cfg(tmp_path))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    want = {"schema_step", "meta", "series", "chapter", "page", "handled_chapter", "published_file"}
    assert want <= tables, "step 0001 builds the skeleton's tables"
    assert set(db.WORKFLOW_TABLES) <= tables, "state reset empties tables that exist"
    assert len(db.database_id(conn)) == 32
    conn.close()


@budget(load="db")
def test_settings_are_in_effect(tmp_path):
    conn = db.connect(db_cfg(tmp_path))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.in_transaction, "no transaction is left open after connect"
    conn.close()


@budget(load="db")
def test_reopen_keeps_data_and_id(tmp_path):
    cfg = db_cfg(tmp_path)
    conn = db.connect(cfg)
    first_id = db.database_id(conn)
    with db.transaction(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'kept')")
    conn.close()
    conn = db.connect(cfg)
    assert db.database_id(conn) == first_id, "re-opening must not rebuild the database"
    assert conn.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()[0] == "kept"
    assert step_names(conn) == ["0001_skeleton"]
    conn.close()


@budget(load="db")
def test_transaction_rolls_back_when_the_block_raises(tmp_path):
    conn = db.connect(db_cfg(tmp_path))
    with pytest.raises(RuntimeError, match="stage failed"):
        with db.transaction(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")
            raise RuntimeError("stage failed")
    assert conn.execute("SELECT 1 FROM meta WHERE key = 'probe'").fetchone() is None
    conn.close()


@budget(load="db")
@pytest.mark.parametrize("first, second", [("0002_a", "0003_b"), ("0003_b", "0002_a")])
def test_steps_from_2_branches_both_apply_whatever_merges_first(tmp_path, monkeypatch, first, second):
    cfg = db_cfg(tmp_path)
    sql = {"0002_a": "CREATE TABLE a (x INTEGER) STRICT;", "0003_b": "CREATE TABLE b (x INTEGER) STRICT;"}
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, (first, sql[first])))
    db.connect(cfg).close()
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, (second, sql[second])))
    conn = db.connect(cfg)
    assert step_names(conn) == ["0001_skeleton", "0002_a", "0003_b"]
    conn.execute("SELECT x FROM a UNION ALL SELECT x FROM b")
    conn.close()


@budget(load="db")
def test_database_with_an_unknown_step_is_refused(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0002_newer", "CREATE TABLE n (x INTEGER) STRICT;")))
    db.connect(cfg).close()
    monkeypatch.undo()
    with pytest.raises(db.SchemaError, match="does not have: 0002_newer"):
        db.connect(cfg)


@budget(load="db")
def test_read_only_reads_and_never_writes(tmp_path):
    cfg = db_cfg(tmp_path)
    with pytest.raises(FileNotFoundError, match="no database at"):
        db.read_only(cfg)
    assert not (tmp_path / "pipeline.sqlite3").exists(), "read_only never creates a database"
    db.connect(cfg).close()
    conn = db.read_only(cfg)
    assert len(db.database_id(conn)) == 32
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")
    conn.close()


@budget(load="db")
def test_read_only_refuses_a_database_with_other_steps(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    db.connect(cfg).close()
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0002_newer", "CREATE TABLE n (x INTEGER) STRICT;")))
    with pytest.raises(db.SchemaError, match="not applied yet: 0002_newer"):
        db.read_only(cfg)


@budget(load="db")
def test_failed_step_leaves_no_trace(tmp_path, monkeypatch):
    cfg = db_cfg(tmp_path)
    db.connect(cfg).close()
    monkeypatch.setattr(db, "SCHEMA", steps_folder(tmp_path, ("0002_bad", "CREATE TABLE ok (x INTEGER); NOT SQL;")))
    with pytest.raises(Exception, match="syntax error"):
        db.connect(cfg)
    monkeypatch.undo()
    conn = db.connect(cfg)
    assert step_names(conn) == ["0001_skeleton"]
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ok'").fetchone() is None
    conn.close()


@budget(load="db")
def test_read_only_refuses_a_file_without_a_schema_record(tmp_path):
    cfg = db_cfg(tmp_path)
    (tmp_path / "pipeline.sqlite3").write_bytes(b"")
    with pytest.raises(db.SchemaError, match="holds no schema record"):
        db.read_only(cfg)
