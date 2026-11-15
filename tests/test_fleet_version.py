"""The fleet's version (SPEC "State", "Config", "Preflight"): a part or junk review runs only when its program, shared
settings, packages and tools are the fleet's; machine-local keys may differ."""

from contextlib import closing

import pytest

from manga_pipeline import db, fleet, preflight, workflow

from tests.builders import adopted
from tests.timing import budget


@pytest.fixture
def cfg(tmp_path):
    return adopted(tmp_path)  # its digest is the fleet's


def problem(cfg):
    return preflight.fleet_version(cfg)


@budget(load="db")
def test_the_machine_that_set_the_version_passes(cfg):
    assert problem(cfg) is None


@budget(load="db")
@pytest.mark.parametrize(
    "section, key, value",
    [
        ("junk", "claude_path", "/elsewhere/claude"),
        ("convert", "workers", 3),
        ("junk", "ocr_device", "cuda"),
        ("paths", "scratch", "/elsewhere/scratch"),
        ("tools", "avifenc", "/elsewhere/avifenc"),
        ("kavita", "url", "http://x"),
    ],
)
def test_a_machine_local_key_may_differ(cfg, section, key, value):
    assert problem({**cfg, section: {**cfg[section], key: value}}) is None, f"{section}.{key} is machine-local"


@budget(load="db")
@pytest.mark.parametrize(
    "section, key, value",
    [
        ("workflow", "attempt_limit", 9),
        ("series", "only", ["X"]),
        ("lock", "enabled", False),
        ("export", "retention_days", 99),
        ("junk", "keywords", ["x"]),
    ],
)
def test_a_changed_shared_setting_stops_the_machine(cfg, section, key, value):
    assert "shared settings" in problem({**cfg, section: {**cfg[section], key: value}})


@budget(load="db")
def test_another_program_or_package_or_tool_version_stops_the_machine(cfg, monkeypatch):
    real = fleet.digest(cfg)
    monkeypatch.setattr(fleet, "_packages", lambda: {"pillow": "12.3.0"})
    assert "runtime packages" in problem(cfg), "an older Pillow changes junk scans"
    monkeypatch.undo()
    from tests.fakes import convert as fake_convert

    monkeypatch.setattr(fleet, "_tools", lambda cfg: {**fake_convert.FAKE_TOOL_VERSIONS, "avifenc": "Version: 1.2.1"})
    assert "tools" in problem(cfg), "another avifenc version output"
    monkeypatch.setattr(fleet, "_tools", lambda cfg: dict(fake_convert.FAKE_TOOL_VERSIONS))
    monkeypatch.setattr(fleet, "_program", lambda: {"manga_pipeline/x.py": "another"})
    assert "program" in problem(cfg)
    assert fleet.digest(cfg) != real


@budget(load="db")
def test_a_database_with_no_version_stops_every_machine(cfg):
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = %s", (fleet.META_KEY,))
    assert "holds no fleet version" in problem(cfg)


@budget(load="pure")
def test_jpegtran_s_build_date_is_left_out_and_package_names_are_read_from_requirements():
    assert fleet._name("psycopg[binary]==3.3.9") == "psycopg" and fleet._name("pillow==13.0.0") == "pillow"


@budget(load="db")
def test_a_winerror_59_share_drop_costs_no_attempt(cfg):
    from tests.builders import add_chapter, add_series

    with closing(db.connect(cfg)) as conn:
        series_id = add_series(conn, "S")
        chapter_id = add_chapter(conn, series_id, "c1", "junk")
        ctx = workflow.Context(cfg, conn, "process", suwayomi=object(), kavita=object())
        row = workflow.claim(ctx, ("junk",))

        def stage(ctx, chapter):
            error = OSError(22, "An unexpected network error occurred")
            error.winerror = 59
            raise error

        assert isinstance(workflow.work(ctx, row, stage), workflow.Unreachable)
        after = conn.execute("SELECT status, attempts FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
    assert tuple(after) == ("ready", 0), "a Windows share drop is the share unreachable, not a failed attempt"
