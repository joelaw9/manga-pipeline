"""The person's commands: status, retry, state reset; and the share sentinel stopping a part before any work (SPEC
"CLI", "Needs human", "State", "Preflight")."""

import logging
import shutil
import sys
from contextlib import closing
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from manga_pipeline import cli, config, db, intake, preflight, process, status
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import sources

from tests import pg
from tests.builders import add_chapter, add_series, adopted, config_file, download, run_part, running, site_cfg
from tests.timing import budget
from tests.world import World, cbz

if sys.platform == "win32":
    import _winapi

PARTS = ("intake", "process", "export", "junk-review")


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


def main(tmp_path, cfg, *argv):
    return cli.main(["--config", str(config_file(tmp_path, cfg)), *argv])


def rows(cfg, sql, *params):
    with closing(db.connect(cfg)) as conn:
        return [tuple(r) for r in conn.execute(sql, params)]


@pytest.fixture
def seeded(tmp_path):
    """An adopted site with a needs_human series, a needs_human chapter, a waiting chapter and 2 ready ones."""
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        ok = add_series(conn, "Alpha")
        held = add_series(conn, "Beta", status="needs_human")
        with db.transaction(conn):
            conn.execute("UPDATE series SET status_reason = 'source ids disagree' WHERE id = %s", (held,))
        ids = {
            "stuck": add_chapter(conn, ok, "c1", "convert", status="needs_human", attempts=3, last_error="bad page 3"),
            "waiting": add_chapter(conn, ok, "c2", "junk", status="waiting", waiting_on="bulk_conversion"),
            "ready": add_chapter(conn, ok, "c3", "junk"),
            "held": add_chapter(conn, held, "c1", "validate"),
        }
    return cfg, ids, ok, held


@budget(load="db")
def test_status_reports_while_every_part_runs(tmp_path, seeded, capsys):
    cfg, ids, ok, held = seeded
    with running(cfg, *PARTS):
        assert main(tmp_path, cfg, "status") == 0, "status takes no lock"
    out = capsys.readouterr().out
    for line in (
        "  validate   ready        1",
        "  junk       ready        1",
        "  junk       waiting      1",
        "  convert    needs_human  1",
        "  bulk_conversion    1",
        f"  Alpha (series {ok}): 1 chapter(s)",
        f"  series {held} (Beta): source ids disagree -> retry --series {held}",
        f"  chapter {ids['stuck']} (Alpha / c1, at convert): bad page 3 -> retry {ids['stuck']}",
    ):
        assert line in out.splitlines(), f"status shows {line!r}"


@budget(load="fs")
def test_status_without_a_database_says_so_and_creates_none(tmp_path, capsys):
    cfg = site_cfg(tmp_path, built=False)
    assert main(tmp_path, cfg, "status") == 0
    assert "no run has built it yet" in capsys.readouterr().out
    assert not db.built(cfg), "status builds nothing"


@budget(load="db")
def test_retry_chapter_while_every_part_runs(tmp_path, seeded):
    cfg, ids, _, _ = seeded
    with running(cfg, *PARTS):
        assert main(tmp_path, cfg, "retry", str(ids["stuck"])) == 0, "retry takes no lock"
    got = rows(cfg, "SELECT stage, status, attempts FROM chapter WHERE id = %s", ids["stuck"])
    assert got == [("convert", "ready", 0)], "ready again at the stage where it stopped, attempts 0"


@budget(load="db")
def test_retry_series_clears_it_and_its_needs_human_chapters(tmp_path, seeded):
    cfg, ids, ok, held = seeded
    assert main(tmp_path, cfg, "retry", "--series", str(held)) == 0
    assert rows(cfg, "SELECT status, status_reason FROM series WHERE id = %s", held) == [("ok", None)]
    assert main(tmp_path, cfg, "retry", "--series", str(ok)) == 0, "an ok series with a needs_human chapter"
    assert rows(cfg, "SELECT status, attempts FROM chapter WHERE id = %s", ids["stuck"]) == [("ready", 0)]
    assert rows(cfg, "SELECT status FROM chapter WHERE id = %s", ids["waiting"]) == [("waiting",)]


@budget(load="db")
@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("missing-chapter", "no chapter 999"),
        ("ready", "is ready, not needs_human"),
        ("claimed", "is claimed, not needs_human"),
        ("missing-series", "no series 999"),
        ("ok-series", "has no needs_human chapter"),
    ],
)
def test_retry_refuses_what_is_not_needs_human(tmp_path, seeded, capsys, target, message):
    cfg, ids, ok, _ = seeded
    with closing(db.connect(cfg)) as conn:
        with db.transaction(conn):
            conn.execute("UPDATE chapter SET status = 'claimed', claimed_by = 'process' WHERE id = %s", (ids["ready"],))
            conn.execute("UPDATE chapter SET status = 'ready', attempts = 0 WHERE id = %s", (ids["stuck"],))
        claimed_id = ids["ready"]
        ready_id = add_chapter(conn, ok, "c4", "junk")
    argv = {
        "missing-chapter": ["999"],
        "ready": [str(ready_id)],
        "claimed": [str(claimed_id)],
        "missing-series": ["--series", "999"],
        "ok-series": ["--series", str(ok)],
    }[target]
    before = rows(cfg, "SELECT * FROM chapter ORDER BY id") + rows(cfg, "SELECT * FROM series ORDER BY id")
    assert main(tmp_path, cfg, "retry", *argv) == 2
    assert message in capsys.readouterr().err
    assert rows(cfg, "SELECT * FROM chapter ORDER BY id") + rows(cfg, "SELECT * FROM series ORDER BY id") == before


@budget(load="db")
@pytest.mark.parametrize("held", PARTS)
def test_state_reset_waits_for_no_part(tmp_path, seeded, held):
    cfg = seeded[0]
    with running(cfg, held):
        assert main(tmp_path, cfg, "state", "reset", "--yes") == 3
    assert rows(cfg, "SELECT count(*) FROM chapter") == [(4,)], "a refused reset changes nothing"


@budget(load="db", seconds=2)
def test_state_reset_clears_workflow_state_and_keeps_the_rest(tmp_path, monkeypatch):
    world = World(tmp_path)
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: world.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: world.sources)
    cfg = world.cfg
    world.kavita_series("Alpha")
    manga = world.manga("Alpha")
    _, kept_download = world.download(manga, "Chapter 2", data=cbz(credit=True))
    _, published_download = world.download(manga, "Chapter 3")
    for part in ("intake", "process", "export"):
        assert main(tmp_path, cfg, part) == 0
    with closing(db.connect(cfg)) as conn:
        database_id = db.database_id(conn)
        with db.transaction(conn):
            conn.execute("UPDATE series SET status = 'needs_human', status_reason = 'x'")
    staging = config.abs_path(cfg["paths"]["staging"])
    scratch = config.abs_path(cfg["paths"]["scratch"])
    (staging / "7.cbz").write_bytes(b"left by a run")
    (scratch / "7").mkdir(parents=True)
    (scratch / "7" / "page.png").write_bytes(b"")
    library = sorted(p.name for p in config.abs_path(cfg["paths"]["library"]).rglob("*.cbz"))

    assert main(tmp_path, cfg, "state", "reset", "--yes") == 0
    assert rows(cfg, "SELECT count(*) FROM chapter") == [(0,)]
    assert rows(cfg, "SELECT count(*) FROM page") == [(0,)]
    assert rows(cfg, "SELECT status, status_reason FROM series") == [("ok", None)]
    assert rows(cfg, "SELECT count(*) FROM handled_chapter") == [(2,)], "knowledge is kept (bulk + published)"
    assert rows(cfg, "SELECT count(*) FROM published_file") == [(2,)], "sync records are kept"
    with closing(db.connect(cfg)) as conn:
        assert db.database_id(conn) == database_id
    assert [p.name for p in staging.iterdir()] == [preflight.OWNER_FILE], "staging keeps only the owner file"
    assert not list(scratch.iterdir())
    assert kept_download.exists(), "downloads are never touched"
    assert published_download.exists(), "downloads are never touched"
    assert sorted(p.name for p in config.abs_path(cfg["paths"]["library"]).rglob("*.cbz")) == library

    run_part(cfg, intake)
    got = rows(cfg, "SELECT download_path, stage, status FROM chapter ORDER BY id")
    assert got == [
        (config.to_stored(cfg, kept_download), "junk", "ready"),
        (config.to_stored(cfg, published_download), "discover", "done"),
    ], "the next intake rediscovers the downloads, and puts aside the one already published"


@budget(load="db")
def test_state_reset_without_a_scratch_folder(tmp_path):
    cfg = adopted(tmp_path)
    assert main(tmp_path, cfg, "state", "reset", "--yes") == 0


@budget(load="db")
@pytest.mark.parametrize("owner", [None, "another-database"], ids=["missing", "another-id"])
def test_share_sentinel_stops_a_part_before_any_work(tmp_path, owner):
    cfg = adopted(tmp_path)
    download(cfg, "Alpha", "ch1")
    with closing(db.connect(cfg)) as conn:
        add_chapter(conn, add_series(conn, "Beta"), "c1", "junk")
    sentinel = preflight.owner_file(cfg)
    if owner is None:
        sentinel.unlink()
    else:
        sentinel.write_text(owner, encoding="utf-8")
    before = rows(cfg, "SELECT * FROM chapter")
    for part in ("intake", "process", "export"):
        assert main(tmp_path, cfg, part) == 4
    assert rows(cfg, "SELECT * FROM chapter") == before, "no chapter row is added or touched"


def dir_link(link, target):
    """A link to a folder that needs no privilege: a junction on Windows, a symlink elsewhere."""
    if sys.platform == "win32":
        _winapi.CreateJunction(str(target), str(link))
    else:
        Path(link).symlink_to(target, target_is_directory=True)


@budget(load="db")
def test_state_reset_removes_links_without_following_them(tmp_path):
    cfg = adopted(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.cbz").write_bytes(b"")
    staging = config.abs_path(cfg["paths"]["staging"])
    dir_link(staging / "link", outside)
    (staging / "sub").mkdir()
    dir_link(staging / "sub" / "nested", outside)
    assert main(tmp_path, cfg, "state", "reset", "--yes") == 0
    assert [p.name for p in staging.iterdir()] == [preflight.OWNER_FILE]
    assert (outside / "keep.cbz").exists(), "a link in staging is removed, its target never emptied"


@budget(load="db")
def test_chapter_ids_are_never_reused(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        series = add_series(conn, "Alpha")
        first = add_chapter(conn, series, "c1")
    assert main(tmp_path, cfg, "state", "reset", "--yes") == 0
    with closing(db.connect(cfg)) as conn:
        assert add_chapter(conn, series, "c1") > first, "an id names files on the share; reset must not reuse it"


@budget(load="fs")
def test_retry_without_a_database_is_refused_and_creates_none(tmp_path, capsys):
    cfg = site_cfg(tmp_path, built=False)
    assert main(tmp_path, cfg, "retry", "1") == 2
    assert "built yet: nothing to retry" in capsys.readouterr().err
    assert not db.built(cfg), "retry builds nothing"


@pytest.fixture
def missing(tmp_path):
    """site_cfg naming a database the server does not have; dropped afterwards if a command created it."""
    cfg = site_cfg(tmp_path)
    cfg = {**cfg, "database": {**cfg["database"], "name": f"{cfg['database']['name']}_never"}}
    yield cfg
    with closing(pg.connect(db.MAINTENANCE_DATABASE)) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(pg.owned(cfg["database"]["name"])))


def created(cfg):
    """Whether the server has the database cfg names, with or without a schema."""
    try:
        db.peek_id(cfg)
    except db.NoDatabaseError:
        return False
    except db.Error:
        return True  # it exists, holding no meta table
    return True


@budget(load="db")
@pytest.mark.parametrize("argv", [["state", "version", "--yes"], ["state", "reset", "--yes"], ["process"]])
def test_a_command_with_locks_on_an_empty_database_exits_4_and_builds_nothing(tmp_path, capsys, argv):
    cfg = site_cfg(tmp_path, built=False)
    assert main(tmp_path, cfg, *argv) == 4
    assert f"no database {cfg['database']['name']}" in capsys.readouterr().err, "an empty database is no database"
    assert not db.built(cfg), "only state adopt and junk import build a database"


@budget(load="db", seconds=3)  # a build from no schema
def test_state_adopt_builds_an_existing_empty_database(tmp_path):
    cfg = site_cfg(tmp_path, built=False)
    assert main(tmp_path, cfg, "state", "adopt", "--yes") == 0
    assert db.built(cfg), "a database created by hand is adopted and built"


@budget(load="db")
def test_junk_review_on_a_missing_database_exits_4_and_creates_none(tmp_path, missing, capsys):
    assert main(tmp_path, missing, "junk", "review") == 4
    assert f"no database {missing['database']['name']}" in capsys.readouterr().err
    assert not created(missing), "junk review builds no database"


@budget(load="db")
@pytest.mark.parametrize("argv", [["junk", "list"], ["junk", "show", "1"], ["junk", "decide", "1", "junk"]])
def test_junk_commands_on_a_missing_database_are_refused_and_create_none(tmp_path, missing, capsys, argv):
    assert main(tmp_path, missing, *argv) == 2
    assert "built yet" in capsys.readouterr().err
    assert not created(missing), "the junk commands build no database"


@budget(load="db")
def test_a_builder_whose_role_cannot_create_the_database_exits_4(tmp_path, missing, monkeypatch, capsys):
    real = db.open_connection

    class NoCreate:
        """The maintenance connection of a role without the right to create a database."""

        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, *params):
            if isinstance(statement, sql.Composable):
                msg = "permission denied to create database"
                raise psycopg.errors.InsufficientPrivilege(msg)
            return self.conn.execute(statement, *params)

        def close(self):
            self.conn.close()

    def opened(cfg, dbname=None):
        conn = real(cfg, dbname)
        return NoCreate(conn) if dbname == db.MAINTENANCE_DATABASE else conn

    monkeypatch.setattr(db, "open_connection", opened)
    assert main(tmp_path, missing, "state", "adopt", "--yes") == 4
    err = capsys.readouterr().err
    assert missing["database"]["name"] in err, "named on stderr"
    assert "permission denied to create database" in err, "named on stderr"


@budget(load="db", seconds=5)
def test_a_connection_the_server_ends_mid_run_exits_1_with_its_traceback(tmp_path, monkeypatch):
    cfg = adopted(tmp_path, logging={**config.cached()["logging"], "dir": str(tmp_path / "logs")})

    def ended_mid_run(cfg, conn):
        with closing(db.connect(cfg)) as server:
            server.execute("SELECT pg_terminate_backend(%s, 5000)", (conn.info.backend_pid,))
        conn.execute("SELECT 1")

    monkeypatch.setattr(process, "run", ended_mid_run)
    assert main(tmp_path, cfg, "process") == 1
    text = (config.abs_path(cfg["logging"]["dir"]) / "process.log").read_text(encoding="utf-8")
    assert text.count("Traceback") == 1, "logged with its traceback, once"
    assert "process: failed" in text, "logged with its traceback, once"


@budget(load="db")
def test_process_stops_at_start_on_a_paired_term_that_is_no_keyword(tmp_path, capsys):
    cfg = adopted(tmp_path)
    cfg = {**cfg, "junk": {**cfg["junk"], "paired": [*cfg["junk"]["paired"], "nonsense"]}}
    assert main(tmp_path, cfg, "process") == 4
    err = capsys.readouterr().err
    assert "junk word lists" in err, "the check and the stray term are named"
    assert "['nonsense']" in err, "the check and the stray term are named"


@budget(load="db")
@pytest.mark.parametrize("argv", [["status"], ["retry", "1"]])
def test_database_from_a_newer_version_exits_4(tmp_path, capsys, argv):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        conn.execute("INSERT INTO schema_step (name, applied_at) VALUES ('9999_newer', 'x')")
    assert main(tmp_path, cfg, *argv) == 4
    assert "9999_newer" in capsys.readouterr().err


@budget(load="db")
def test_state_reset_on_a_bad_folder_layout_exits_4_and_deletes_nothing(tmp_path):
    cfg = adopted(tmp_path)
    kept = [download(cfg, "Alpha", "ch1"), config.abs_path(cfg["paths"]["library"]) / "Alpha" / "ch0.cbz"]
    kept[1].parent.mkdir(parents=True)
    kept[1].write_bytes(b"published")
    with closing(db.connect(cfg)) as conn:
        add_chapter(conn, add_series(conn, "Alpha"), "c1", "junk")
    share = config.abs_path(cfg["paths"]["staging"]).parent
    shutil.move(preflight.owner_file(cfg), share / preflight.OWNER_FILE)
    bad = {**cfg, "paths": {**cfg["paths"], "staging": str(share)}}  # the share root holds downloads and library
    assert main(tmp_path, bad, "state", "reset", "--yes") == 4
    assert all(p.exists() for p in kept), "a reset refused by preflight deletes nothing"
    assert rows(cfg, "SELECT count(*) FROM chapter") == [(1,)]


def steps_with(tmp_path, monkeypatch, name):
    """The program's schema steps plus an empty step `name`."""
    folder = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, folder)
    (folder / f"{name}.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA", folder)


@budget(load="db")
def test_status_on_a_database_lacking_a_step_exits_4(tmp_path, monkeypatch, capsys):
    cfg = adopted(tmp_path)
    steps_with(tmp_path, monkeypatch, "0002_newer")
    assert main(tmp_path, cfg, "status") == 4
    assert "not applied yet: 0002_newer" in capsys.readouterr().err


@budget(load="db")
def test_part_on_a_database_from_a_newer_version_exits_4(tmp_path, capsys):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        conn.execute("INSERT INTO schema_step (name, applied_at) VALUES ('9999_newer', 'x')")
    assert main(tmp_path, cfg, "intake") == 4
    assert "does not have: 9999_newer" in capsys.readouterr().err


@budget(load="db")
def test_part_without_its_downloads_folder_exits_4(tmp_path, capsys):
    cfg = adopted(tmp_path)
    shutil.rmtree(config.abs_path(cfg["paths"]["downloads"]))
    assert main(tmp_path, cfg, "intake") == 4
    assert "share folders: paths.downloads" in capsys.readouterr().err


@budget(load="db")
def test_status_sections_come_from_1_snapshot(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as writer:
        series = add_series(writer, "Alpha")

        class WritesAfterFirstRead:
            """A connection a part writes through between the report's first query and the rest."""

            def __init__(self, conn):
                self.conn, self.reads = conn, 0

            def transaction(self):
                return self.conn.transaction()

            def execute(self, query, *params):
                cursor = self.conn.execute(query, *params)
                text = query.as_string(self.conn) if isinstance(query, sql.Composable) else query
                if text.lstrip().upper().startswith("SELECT"):
                    self.reads += 1
                    if self.reads == 1:
                        cursor = list(cursor)
                        add_chapter(writer, series, "c1", "junk", status="needs_human", last_error="x")
                        return _Rows(cursor)
                return cursor

        with closing(db.read_only(cfg)) as conn:
            lines = status.report(WritesAfterFirstRead(conn))
    entries = [line for line in lines if line.startswith("  ")]
    assert entries, "a chapter written mid-report shows in no section"
    assert all(line == "  none" for line in entries), "a chapter written mid-report shows in no section"


class _Rows(list):
    def fetchall(self):
        return list(self)
