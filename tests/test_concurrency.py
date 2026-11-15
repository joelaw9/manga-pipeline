"""The parts as real processes: run at once on 1 database, and killed mid-stage (SPEC "Parts", "State", "Run lock").
The processes reach the world's fakes through tests/world_run.py."""

import os
import subprocess
import sys
import time
from contextlib import closing

import pytest

from manga_pipeline import config, db, fleet, intake, process, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import check

from tests.builders import config_file, run_part
from tests.timing import budget
from tests.world import World

PARTS = ("intake", "process", "export")
ROUNDS = 6  # intake, process and export each need at most 1 round once the one before has finished
# A hang guard, not a speed check: under the parallel suite every core is busy and a part takes far longer to start.
STEP_TIMEOUT_S = 120
# A process run whose convert stage writes the step file (argv[1]) and then blocks until killed.
BLOCKING_PROCESS = """
import sys, threading
from pathlib import Path
from manga_pipeline import cli, process
from tests.fakes import convert as fake_convert

def blocking_convert(ctx, chapter):
    Path(sys.argv[1]).write_text(str(chapter["id"]), encoding="utf-8")
    threading.Event().wait()

process.STAGES["convert"] = blocking_convert
fake_convert.install(setattr)
sys.exit(cli.main(["--config", sys.argv[2], "process"]))
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A World whose fakes the parts run in this process get too."""
    world = World(tmp_path)
    monkeypatch.setattr(suwayomi_client, "from_config", lambda cfg: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda cfg: world.kavita)
    monkeypatch.setattr(check, "new_sources", lambda: world.sources)
    return world


def downloads(world, series_count, per_series):
    """Complete downloads "Chapter 2".. of series_count Kavita series."""
    paths = []
    for s in range(series_count):
        manga = world.manga(f"Series {s}")
        world.kavita_series(f"Series {s}")
        paths += [world.download(manga, f"Chapter {n}")[1] for n in range(2, 2 + per_series)]
    return paths


def clean_env():
    """The environment without the developer's MANGA_PIPELINE_* variables."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(f"{config.ENV_PREFIX}_")}


def start(args, out):
    return subprocess.Popen(
        [sys.executable, *args], cwd=config.ROOT, env=clean_env(), stdout=out, stderr=subprocess.STDOUT
    )


def part_argv(world_file, config_path, *args):
    return ["-m", "tests.world_run", str(world_file), "--config", str(config_path), *args]


def rows(cfg, sql):
    with closing(db.connect(cfg)) as conn:
        return [tuple(r) for r in conn.execute(sql)]


@budget(load="subprocess", seconds=40)
def test_parts_at_once_publish_every_chapter_exactly_once(world, tmp_path):
    cfg = world.cfg
    paths = downloads(world, 6, 10)
    world_file = world.dump(tmp_path / "world.json")
    config_path = config_file(tmp_path, cfg)
    for round_ in range(ROUNDS):
        runs = []
        for part in PARTS:
            out = open(tmp_path / f"{part}-{round_}.out", "wb")
            runs.append((part, out, start(part_argv(world_file, config_path, part), out)))
        for part, out, run in runs:
            code = run.wait(timeout=STEP_TIMEOUT_S)
            out.close()
            assert code == 0, f"{part} round {round_}: {(tmp_path / f'{part}-{round_}.out').read_text()}"
        if rows(cfg, "SELECT count(*) FROM chapter WHERE status != 'done'") == [(0,)]:
            break
    assert rows(cfg, "SELECT count(*), count(DISTINCT identity || series_id) FROM chapter WHERE status = 'done'") == [
        (60, 60)
    ]
    library = [p for p in config.abs_path(cfg["paths"]["library"]).rglob("*.cbz") if p.name != "Ch.1.cbz"]
    assert len(library) == 60, "every chapter is published exactly once"
    assert rows(cfg, "SELECT count(*) FROM published_file WHERE state = 'confirmed' AND origin = 'pipeline'") == [(60,)]
    assert not any(p.exists() for p in paths)
    staging = config.abs_path(cfg["paths"]["staging"])
    assert [p.name for p in staging.iterdir()] == [".pipeline-owner"]


@budget(load="subprocess", seconds=30)
def test_killed_process_run_is_finished_by_the_next_run(world, tmp_path, monkeypatch):
    cfg = world.cfg
    downloads(world, 1, 1)
    run_part(cfg, intake)
    step = tmp_path / "in-convert"
    out = open(tmp_path / "killed.out", "wb")
    run = start(["-c", BLOCKING_PROCESS, str(step), str(config_file(tmp_path, cfg))], out)
    try:
        deadline = time.monotonic() + STEP_TIMEOUT_S
        while not step.exists():
            assert run.poll() is None, f"the run ended early: {(tmp_path / 'killed.out').read_text()}"
            assert time.monotonic() < deadline, "the run never reached the convert stage"
            time.sleep(0.05)  # polling interval, not a synchronisation
    finally:
        run.kill()
        run.wait()
        out.close()
    got = rows(cfg, "SELECT stage, status, claimed_by, attempts FROM chapter")
    assert got == [("convert", "claimed", workflow.owner("process"), 0)], "the killed run left its claim"

    junk_runs = []
    monkeypatch.setitem(process.STAGES, "junk", lambda ctx, chapter: junk_runs.append(chapter["id"]))
    run_part(cfg, process)
    got = rows(cfg, "SELECT stage, status, claimed_by, attempts FROM chapter")
    assert got == [("comicinfo", "ready", None, 0)], "the next run releases the claim and finishes the chapter"
    assert junk_runs == [], "the junk stage it had finished is not redone"


def outcome_lines(text, outcome):
    """(chapter id, stage) of each per-outcome log line "<part>: chapter N (series S, identity, at STAGE) <outcome>"."""
    found = []
    for line in text.splitlines():
        head, sep, tail = line.partition(": chapter ")
        if not sep or not tail.endswith(f") {outcome}"):
            continue
        chapter_id, _, rest = tail.partition(" (")
        found.append((int(chapter_id), rest.rpartition(", at ")[2].removesuffix(f") {outcome}")))
    return found


# 2 process runs over 40 chapters: every statement is a round trip to the server (23 s on SQLite, 33-40 s here).
@budget(load="subprocess", seconds=45)
def test_2_unlocked_runs_of_a_part_never_work_a_chapter_twice(world, tmp_path):
    cfg = {**world.cfg, "lock": {"enabled": False}}  # a shared setting: the fleet's version is set with it
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        fleet.store(conn, cfg)
    downloads(world, 4, 10)
    run_part(cfg, intake)
    path = config_file(tmp_path, cfg)
    world_file = world.dump(tmp_path / "world.json")
    runs = []
    for n in range(2):
        out = open(tmp_path / f"process-{n}.out", "wb")
        argv = part_argv(world_file, path, "process")
        runs.append((out, start(argv, out)))
    for out, run in runs:
        assert run.wait(timeout=STEP_TIMEOUT_S) == 0
        out.close()
    text = "".join((tmp_path / f"process-{n}.out").read_text(encoding="utf-8") for n in range(2))
    done = outcome_lines(text, "done")
    assert sorted(done) == sorted({(i, s) for i, s in done}), "every (chapter, stage) is worked at most once"
    assert len(done) == 80 and "outcome is dropped" not in text
    assert rows(cfg, "SELECT DISTINCT stage, status FROM chapter") == [("comicinfo", "ready")]
