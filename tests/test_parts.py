"""The 3 part loops with stub stages, run in turn in-process on fake downloads (SPEC "Parts", "Chapter stages")."""

import errno
import logging
import os
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import cli, config, db, export, intake, process, workflow
from manga_pipeline.stages import junk

from tests.builders import add_chapter, add_series, adopted, config_file, download, site_cfg
from tests.timing import budget


def run_all(cfg):
    for part in (intake, process, export):
        part.run(cfg)


def rows(cfg, sql, *params):
    with closing(db.connect(cfg)) as conn:
        return [tuple(r) for r in conn.execute(sql, params)]


def library_files(cfg):
    library = config.abs_path(cfg["paths"]["library"])
    return sorted(p.relative_to(library).as_posix() for p in library.rglob("*") if p.is_file())


@budget(load="db", seconds=2)
def test_parts_in_turn_publish_every_chapter_once_and_delete_the_downloads(tmp_path):
    cfg = site_cfg(tmp_path)
    downloads = [download(cfg, series, f"ch{n}") for series in ("Alpha", "Beta") for n in range(1, 4)]
    run_all(cfg)
    run_all(cfg)  # a second round finds nothing new and publishes nothing twice
    want = [f"{s}/ch{n}.cbz" for s in ("Alpha", "Beta") for n in range(1, 4)]
    assert library_files(cfg) == want
    assert not any(p.exists() for p in downloads), "confirm deletes each download"
    assert rows(cfg, "SELECT DISTINCT stage, status FROM chapter") == [("confirm", "done")]
    assert rows(cfg, "SELECT count(*), count(DISTINCT library_path) FROM published_file WHERE state = 'confirmed'") == [
        (6, 6)
    ]
    assert rows(cfg, "SELECT count(*) FROM handled_chapter WHERE outcome = 'published'") == [(6,)]
    assert not list(config.abs_path(cfg["paths"]["staging"]).iterdir()), "nothing is left in staging"


@budget(load="db", seconds=2)
def test_waiting_chapter_moves_on_once_its_marker_is_gone(tmp_path):
    cfg = site_cfg(tmp_path)
    held = download(cfg, "Alpha", "ch1")
    download(cfg, "Alpha", "ch2")
    marker = held.with_suffix(".wait")
    marker.write_bytes(b"")
    run_all(cfg)
    assert library_files(cfg) == ["Alpha/ch2.cbz"], "a waiting chapter never holds back the chapters after it"
    assert rows(cfg, "SELECT status, waiting_on FROM chapter WHERE identity = 'ch1'") == [("waiting", "junk_decision")]
    run_all(cfg)
    assert library_files(cfg) == ["Alpha/ch2.cbz"], "the wait holds while the marker is there"
    marker.unlink()
    run_all(cfg)
    assert library_files(cfg) == ["Alpha/ch1.cbz", "Alpha/ch2.cbz"]


@budget(load="db", seconds=2)
def test_failing_chapter_does_not_stop_the_chapters_after_it(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    for n in range(1, 4):
        download(cfg, "Alpha", f"ch{n}")
    real = process.STAGES["convert"]

    def convert(ctx, chapter):
        if chapter["identity"] == "ch1":
            raise ValueError("cannot decode page 3")
        return real(ctx, chapter)

    monkeypatch.setitem(process.STAGES, "convert", convert)
    run_all(cfg)
    assert library_files(cfg) == ["Alpha/ch2.cbz", "Alpha/ch3.cbz"]
    got = rows(cfg, "SELECT stage, status, attempts, last_error FROM chapter WHERE identity = 'ch1'")
    assert got == [("convert", "ready", 1, "ValueError: cannot decode page 3")]


@budget(load="db", seconds=2)
def test_new_chapter_of_a_known_series_is_worked_next(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        big = add_series(conn, "Big new series", "new")
        for n in range(50):
            add_chapter(conn, big, f"big{n:02}", "junk", "2026-01-01T00:00:00+00:00")
        known = add_series(conn, "Ongoing", "kavita")
    order = []

    def recording_junk(ctx, chapter):
        order.append(chapter["identity"])
        if len(order) == 1:  # the new chapter arrives while process works the big series
            add_chapter(ctx.conn, known, "ongoing-ch9", "junk")
        return workflow.Done()

    monkeypatch.setitem(process.STAGES, "junk", recording_junk)
    monkeypatch.setitem(process.STAGES, "convert", lambda ctx, chapter: workflow.Done())
    process.run(cfg)
    assert order[:3] == ["big00", "ongoing-ch9", "big01"], "process picks again by priority after every chapter"
    assert len(order) == 51


@budget(load="db", seconds=2)
def test_out_of_scope_series_is_untouched_by_every_part(tmp_path):
    cfg = site_cfg(tmp_path, series={"only": ["Alpha"]})
    download(cfg, "Alpha", "ch1")
    outside = download(cfg, "Beta", "ch1")
    outside.with_suffix(".wait").write_bytes(b"")
    with closing(db.connect(cfg)) as conn:
        beta = add_series(conn, "Beta")
        seeded = {stage: add_chapter(conn, beta, f"seed-{stage}", stage) for stage in workflow.STAGES}
    before = rows(cfg, "SELECT * FROM chapter WHERE series_id = ?", beta)
    run_all(cfg)
    assert library_files(cfg) == ["Alpha/ch1.cbz"]
    assert rows(cfg, "SELECT * FROM chapter WHERE series_id = ?", beta) == before, "no Beta row is written or added"
    assert len(before) == len(seeded) and outside.exists()


@budget(load="db")
@pytest.mark.parametrize("where", ["stage", "wait check"])
def test_unreachable_share_is_the_runs_result_with_exit_0(tmp_path, monkeypatch, where):
    cfg = adopted(tmp_path)
    held = download(cfg, "Alpha", "ch1")
    if where == "wait check":
        held.with_suffix(".wait").write_bytes(b"")
    intake.run(cfg)
    process.run(cfg)  # the chapter now waits (wait check) or is done with junk (stage)
    if where == "wait check":
        before = ("junk", "waiting", 0)
    else:
        with closing(db.connect(cfg)) as conn, db.transaction(conn):
            conn.execute("UPDATE chapter SET stage = 'junk'")
        before = ("junk", "ready", 0)

    def eio(*args):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setitem(process.STAGES, "junk", eio)
    monkeypatch.setattr(junk, "wait_over", eio)
    assert cli.main(["--config", str(config_file(tmp_path, cfg)), "process"]) == 0
    for handler in logging.getLogger("manga_pipeline").handlers[:]:  # close the log file cli.main opened
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()
    assert rows(cfg, "SELECT value FROM meta WHERE key = 'process.last_result'") == [("unreachable: share",)]
    assert rows(cfg, "SELECT stage, status, attempts FROM chapter") == [before]


def meta(cfg, part):
    return dict(rows(cfg, f"SELECT key, value FROM meta WHERE key LIKE '{part}.%'"))


def failing_scan(monkeypatch, cfg, error):
    """os.scandir of the downloads folder raises error (errno, text)."""
    downloads = config.abs_path(cfg["paths"]["downloads"])
    real = os.scandir

    def scandir(path="."):
        if Path(path) == downloads:
            raise OSError(*error, str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


@budget(load="db")
def test_unreadable_downloads_folder_is_an_unreachable_share(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    download(cfg, "Alpha", "ch1")
    failing_scan(monkeypatch, cfg, (errno.EIO, "Input/output error"))
    intake.run(cfg)
    got = meta(cfg, "intake")
    assert got["intake.last_result"] == "unreachable: share" and "intake.last_success" not in got
    assert rows(cfg, "SELECT count(*) FROM chapter") == [(0,)]


@budget(load="db")
def test_other_scan_error_fails_the_run_loudly(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    failing_scan(monkeypatch, cfg, (errno.EACCES, "Permission denied"))
    with pytest.raises(PermissionError):
        intake.run(cfg)
    assert meta(cfg, "intake")["intake.last_result"] == "error"


@budget(load="db")
def test_leftover_staged_files_are_not_taken_for_a_chapters_output(tmp_path):
    cfg = site_cfg(tmp_path)
    source = download(cfg, "Alpha", "ch1", b"the real chapter")
    staging = config.abs_path(cfg["paths"]["staging"])
    leftovers = {staging / "1.cbz": b"other database!!", staging / "0123abcd-1.cbz": b"another database"}
    for path, data in leftovers.items():
        path.write_bytes(data)  # same size as the download
    intake.run(cfg)
    process.run(cfg)
    ((staged,),) = rows(cfg, "SELECT staged_path FROM chapter")
    assert Path(staged).name not in {p.name for p in leftovers}
    assert Path(staged).read_bytes() == source.read_bytes()
    assert all(path.read_bytes() == data for path, data in leftovers.items()), "files of others are left alone"


@budget(load="db")
def test_each_outcome_and_the_run_counts_are_logged(tmp_path, caplog):
    cfg = site_cfg(tmp_path)
    download(cfg, "Alpha", "ch1")
    with caplog.at_level(logging.INFO, logger="manga_pipeline"):
        intake.run(cfg)
    assert "intake: chapter 1 (series 1, ch1, at discover) done" in caplog.text
    assert "intake: chapter 1 (series 1, ch1, at validate) done" in caplog.text
    assert "intake: run ended: ok (2 done, 0 waiting, 0 failed)" in caplog.text


@budget(load="db")
@pytest.mark.parametrize("error", [workflow.Unreachable("share"), OSError(errno.EIO, "io")], ids=["unreachable", "eio"])
def test_unreachable_share_in_a_wait_check_ends_the_run(tmp_path, monkeypatch, error):
    cfg = site_cfg(tmp_path)
    download(cfg, "Alpha", "ch1").with_suffix(".wait").write_bytes(b"")
    intake.run(cfg)
    process.run(cfg)

    def wait_over(ctx, chapter):
        raise error

    monkeypatch.setattr(junk, "wait_over", wait_over)
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = 'process.last_success'")
    process.run(cfg)
    got = meta(cfg, "process")
    assert got["process.last_result"] == "unreachable: share"
    assert "process.last_success" not in got, "a run that met an unreachable share is no success"


@budget(load="db")
def test_run_end_line_counts_waiting_and_failed(tmp_path, monkeypatch, caplog):
    cfg = site_cfg(tmp_path)
    download(cfg, "Alpha", "ch1").with_suffix(".wait").write_bytes(b"")
    download(cfg, "Beta", "ch2")
    download(cfg, "Gamma", "ch3")
    intake.run(cfg)
    real = process.STAGES["convert"]

    def convert(ctx, chapter):
        if chapter["identity"] == "ch2":
            raise ValueError("cannot decode page 3")
        return real(ctx, chapter)

    monkeypatch.setitem(process.STAGES, "convert", convert)
    with caplog.at_level(logging.INFO, logger="manga_pipeline"):
        process.run(cfg)
    assert "process: run ended: ok (3 done, 1 waiting, 1 failed)" in caplog.text
