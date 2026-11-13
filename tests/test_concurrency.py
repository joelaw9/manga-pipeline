"""The parts as real processes: run at once on 1 database, and killed mid-stage (SPEC "Parts", "State", "Run lock")."""

import os
import subprocess
import sys
import time
from contextlib import closing

from manga_pipeline import config, db, intake, process

from tests.builders import adopted, config_file, download
from tests.timing import budget

PARTS = ("intake", "process", "export")
ROUNDS = 6  # intake, process and export each need at most 1 round once the one before has finished
STEP_TIMEOUT_S = 30
# A process run whose convert stage writes the step file (argv[1]) and then blocks until killed.
BLOCKING_PROCESS = """
import sys, threading
from pathlib import Path
from manga_pipeline import cli, process

def blocking_convert(ctx, chapter):
    Path(sys.argv[1]).write_text(str(chapter["id"]), encoding="utf-8")
    threading.Event().wait()

process.STAGES["convert"] = blocking_convert
sys.exit(cli.main(["--config", sys.argv[2], "process"]))
"""


def clean_env():
    """The environment without the developer's MANGA_PIPELINE_* variables."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(f"{config.ENV_PREFIX}_")}


def start(args, out):
    return subprocess.Popen(
        [sys.executable, *args], cwd=config.ROOT, env=clean_env(), stdout=out, stderr=subprocess.STDOUT
    )


def rows(cfg, sql):
    with closing(db.connect(cfg)) as conn:
        return [tuple(r) for r in conn.execute(sql)]


@budget(load="subprocess", seconds=30)
def test_parts_at_once_publish_every_chapter_exactly_once(tmp_path):
    cfg = adopted(tmp_path)
    path = config_file(tmp_path, cfg)
    downloads = [download(cfg, f"Series {s}", f"ch{n:02}") for s in range(6) for n in range(10)]
    for round_ in range(ROUNDS):
        runs = []
        for part in PARTS:
            out = open(tmp_path / f"{part}-{round_}.out", "wb")
            runs.append((part, out, start(["-m", "manga_pipeline.cli", "--config", str(path), part], out)))
        for part, out, run in runs:
            code = run.wait(timeout=STEP_TIMEOUT_S)
            out.close()
            assert code == 0, f"{part} round {round_}: {(tmp_path / f'{part}-{round_}.out').read_text()}"
        if rows(cfg, "SELECT count(*) FROM chapter WHERE status != 'done'") == [(0,)]:
            break
    assert rows(cfg, "SELECT count(*), count(DISTINCT identity || series_id) FROM chapter WHERE status = 'done'") == [
        (60, 60)
    ]
    library = list(config.abs_path(cfg["paths"]["library"]).rglob("*.cbz"))
    assert len(library) == 60, "every chapter is published exactly once"
    assert rows(cfg, "SELECT count(*) FROM published_file WHERE state = 'confirmed'") == [(60,)]
    assert rows(cfg, "SELECT count(*) FROM handled_chapter WHERE outcome = 'published'") == [(60,)]
    assert not any(p.exists() for p in downloads)
    staging = config.abs_path(cfg["paths"]["staging"])
    assert [p.name for p in staging.iterdir()] == [".pipeline-owner"]


@budget(load="subprocess", seconds=30)
def test_killed_process_run_is_finished_by_the_next_run(tmp_path, monkeypatch):
    cfg = adopted(tmp_path)
    download(cfg, "Alpha", "ch1")
    intake.run(cfg)
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
    assert got == [("convert", "claimed", "process", 0)], "the killed run left its claim"

    junk_runs = []
    monkeypatch.setitem(process.STAGES, "junk", lambda ctx, chapter: junk_runs.append(chapter["id"]))
    process.run(cfg)
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


@budget(load="subprocess", seconds=30)
def test_2_unlocked_runs_of_a_part_never_work_a_chapter_twice(tmp_path):
    cfg = adopted(tmp_path)
    for n in range(40):
        download(cfg, f"Series {n % 4}", f"ch{n:02}")
    intake.run(cfg)
    path = config_file(tmp_path, cfg)
    runs = []
    for n in range(2):
        out = open(tmp_path / f"process-{n}.out", "wb")
        argv = ["-m", "manga_pipeline.cli", "--config", str(path), "--set", "lock.enabled=false", "process"]
        runs.append((out, start(argv, out)))
    for out, run in runs:
        assert run.wait(timeout=STEP_TIMEOUT_S) == 0
        out.close()
    text = "".join((tmp_path / f"process-{n}.out").read_text(encoding="utf-8") for n in range(2))
    done = outcome_lines(text, "done")
    assert sorted(done) == sorted({(i, s) for i, s in done}), "every (chapter, stage) is worked at most once"
    assert len(done) == 80 and "outcome is dropped" not in text
    assert rows(cfg, "SELECT DISTINCT stage, status FROM chapter") == [("comicinfo", "ready")]
