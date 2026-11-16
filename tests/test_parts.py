"""The 3 part loops run in turn in-process on a world of fakes (SPEC "Parts", "Chapter stages")."""

import errno
import logging
import os
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import cli, config, db, export, intake, process, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import check
from manga_pipeline.stages import comicinfo, confirm, junk, publish

from tests.builders import add_chapter, add_series, config_file, run_part
from tests.timing import budget
from tests.world import World, cbz, decide_junk


class Site:
    """A World whose fakes every part run gets (from_config patched), with series made on first use."""

    def __init__(self, tmp_path, monkeypatch, **changes):
        self.world = World(tmp_path, **changes)
        self.cfg = self.world.cfg
        self.mangas = {}
        monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: self.world.suwayomi)
        monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: self.world.kavita)
        monkeypatch.setattr(check, "new_sources", lambda: self.world.sources)

    def download(self, series, number, data=None):
        """A complete download "Chapter <number>" of series (a Kavita series with bulk Ch.1 and a record)."""
        if series not in self.mangas:
            self.world.kavita_series(series)
            self.mangas[series] = self.world.manga(series)
        return self.world.download(self.mangas[series], f"Chapter {number}", data=data)[1]

    def run_all(self):
        """intake, process, export, then Kavita runs the scans and export confirms."""
        for part in (intake, process, export):
            run_part(self.cfg, part)
        self.world.kavita.run_scans()
        run_part(self.cfg, export)


@pytest.fixture
def site(tmp_path, monkeypatch):
    return Site(tmp_path, monkeypatch)


def rows(cfg, sql, *params):
    with closing(db.connect(cfg)) as conn:
        return [tuple(r) for r in conn.execute(sql, params or None)]  # none: a literal % stays %


def published(cfg):
    """The library's pipeline-published files (bulk Ch.1.cbz and records aside), as <series>/<name>."""
    library = config.abs_path(cfg["paths"]["library"])
    files = (p.relative_to(library).as_posix() for p in library.rglob("*.cbz") if p.name != "Ch.1.cbz")
    return sorted(files)


def chapter_of(cfg, path):
    return rows(
        cfg,
        "SELECT stage, status, attempts, last_error, waiting_on FROM chapter WHERE download_path = %s",
        config.to_stored(cfg, path),
    )


@budget(load="db", seconds=6)
def test_parts_in_turn_publish_every_chapter_once_and_remove_the_downloads(site):
    downloads = [site.download(series, n) for series in ("Alpha", "Beta") for n in range(2, 5)]
    site.run_all()
    site.run_all()  # a second round finds nothing new and publishes nothing twice
    assert published(site.cfg) == [f"{s}/Ch.{n}.cbz" for s in ("Alpha", "Beta") for n in range(2, 5)]
    assert not any(p.exists() for p in downloads), "confirm removes each download"
    assert rows(site.cfg, "SELECT DISTINCT stage, status FROM chapter") == [("confirm", "done")]
    assert rows(
        site.cfg,
        "SELECT count(*), count(DISTINCT library_path) FROM published_file"
        " WHERE state = 'confirmed' AND origin = 'pipeline'",
    ) == [(6, 6)]
    staging = config.abs_path(site.cfg["paths"]["staging"])
    assert [p.name for p in staging.iterdir()] == [".pipeline-owner"], "nothing is left in staging"


@budget(load="db", seconds=3)
def test_waiting_chapter_moves_on_once_its_candidate_is_decided(site):
    held = site.download("Alpha", 2, data=cbz(credit=True))
    site.download("Alpha", 3)
    site.run_all()
    assert published(site.cfg) == ["Alpha/Ch.3.cbz"], "a waiting chapter never holds back the chapters after it"
    assert chapter_of(site.cfg, held)[0][1::3] == ("waiting", "junk_decision")
    site.run_all()
    assert published(site.cfg) == ["Alpha/Ch.3.cbz"], "the wait holds while its candidate is undecided"
    assert decide_junk(site.cfg) == 1, "the credit page is 1 candidate"
    site.run_all()
    assert published(site.cfg) == ["Alpha/Ch.2.cbz", "Alpha/Ch.3.cbz"]


@budget(load="db", seconds=3)
def test_failing_chapter_does_not_stop_the_chapters_after_it(site, monkeypatch):
    bad = site.download("Alpha", 2)
    for n in (3, 4):
        site.download("Alpha", n)
    real = process.STAGES["convert"]

    def convert(ctx, chapter):
        if chapter["download_path"] == config.to_stored(site.cfg, bad):
            msg = "cannot decode page 3"
            raise ValueError(msg)
        return real(ctx, chapter)

    monkeypatch.setitem(process.STAGES, "convert", convert)
    site.run_all()
    assert published(site.cfg) == ["Alpha/Ch.3.cbz", "Alpha/Ch.4.cbz"]
    assert chapter_of(site.cfg, bad) == [("convert", "ready", 1, "ValueError: cannot decode page 3", None)]


@budget(load="db", seconds=2)
def test_new_chapter_of_a_known_series_is_worked_next(site, monkeypatch):
    cfg = site.cfg
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
    monkeypatch.setitem(process.STAGES, "convert", lambda _ctx, _chapter: workflow.Done())
    run_part(cfg, process)
    assert order[:3] == ["big00", "ongoing-ch9", "big01"], "process picks again by priority after every chapter"
    assert len(order) == 51


@budget(load="db", seconds=3)
def test_out_of_scope_series_is_untouched_by_every_part(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch, series={"only": ["Alpha"]})
    site.download("Alpha", 2)
    outside = site.download("Beta", 2)
    with closing(db.connect(site.cfg)) as conn:
        beta = add_series(conn, "Beta")
        seeded = {stage: add_chapter(conn, beta, f"seed-{stage}", stage) for stage in workflow.STAGES}
    before = rows(site.cfg, "SELECT * FROM chapter WHERE series_id = %s", beta)
    site.run_all()
    assert published(site.cfg) == ["Alpha/Ch.2.cbz"]
    assert rows(site.cfg, "SELECT * FROM chapter WHERE series_id = %s", beta) == before, "no Beta row is written"
    assert rows(site.cfg, "SELECT count(*) FROM series") == [(2,)], "Beta's manga is never identified"
    assert len(before) == len(seeded)
    assert outside.exists()


@budget(load="db", seconds=2)
@pytest.mark.parametrize("where", ["stage", "wait check"])
def test_unreachable_share_is_the_runs_result_with_exit_0(site, tmp_path, monkeypatch, where):
    cfg = site.cfg
    site.download("Alpha", 2, data=cbz(credit=where == "wait check"))
    run_part(cfg, intake)
    run_part(cfg, process)  # the chapter now waits (wait check) or is done with junk (stage)
    if where == "wait check":
        before = ("junk", "waiting", 0)
    else:
        with closing(db.connect(cfg)) as conn, db.transaction(conn):
            conn.execute("UPDATE chapter SET stage = 'junk'")
        before = ("junk", "ready", 0)

    def eio(*args):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setitem(process.STAGES, "junk", eio)
    monkeypatch.setattr(junk, "junk_decision_over", eio)
    assert cli.main(["--config", str(config_file(tmp_path, cfg)), "process"]) == 0
    for handler in logging.getLogger("manga_pipeline").handlers[:]:  # close the log file cli.main opened
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()
    assert meta(cfg, "process")["process.last_result"] == "unreachable: share"
    assert rows(cfg, "SELECT stage, status, attempts FROM chapter") == [before]


def meta(cfg, part):
    """The run records of the part on this machine (workflow.owner), named by part ("process.last_result")."""
    owner = workflow.owner(part)
    return {
        k.replace(owner, part, 1): v
        for k, v in rows(cfg, "SELECT key, value FROM meta WHERE key LIKE %s", f"{owner}.%")
    }


def failing_scan(monkeypatch, cfg, error):
    """os.scandir of the downloads' mangas folder raises error (errno, text)."""
    mangas = config.abs_path(cfg["paths"]["downloads"]) / "mangas"
    real = os.scandir

    def scandir(path="."):
        if Path(path) == mangas:
            raise OSError(*error, str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


@budget(load="db")
def test_unreadable_downloads_folder_is_an_unreachable_share(site, monkeypatch):
    site.download("Alpha", 2)
    failing_scan(monkeypatch, site.cfg, (errno.EIO, "Input/output error"))
    run_part(site.cfg, intake)
    got = meta(site.cfg, "intake")
    assert got["intake.last_result"] == "unreachable: share"
    assert "intake.last_success" not in got
    assert rows(site.cfg, "SELECT count(*) FROM chapter") == [(0,)]


@budget(load="db")
def test_other_scan_error_fails_the_run_loudly(site, monkeypatch):
    failing_scan(monkeypatch, site.cfg, (errno.EACCES, "Permission denied"))
    with pytest.raises(PermissionError):
        run_part(site.cfg, intake)
    assert meta(site.cfg, "intake")["intake.last_result"] == "error"


@budget(load="db", seconds=2)
def test_leftover_staged_files_are_not_taken_for_a_chapters_output(site):
    source = site.download("Alpha", 2, data=cbz(seed="the real chapter"))
    staging = config.abs_path(site.cfg["paths"]["staging"])
    leftovers = {staging / "1.cbz": b"other database!!", staging / "0123abcd-1.cbz": b"another database"}
    for path, data in leftovers.items():
        path.write_bytes(data)
    run_part(site.cfg, intake)
    run_part(site.cfg, process)
    ((staged,),) = rows(site.cfg, "SELECT staged_path FROM chapter")
    assert Path(staged).name not in {p.name for p in leftovers}
    with closing(db.connect(site.cfg)) as conn:
        assert Path(staged).name.startswith(db.database_id(conn)[:8]), "the staged name carries this database's id"
    assert source.exists()
    assert all(path.read_bytes() == data for path, data in leftovers.items()), "files of others are left alone"


@budget(load="db", seconds=3)
def test_an_export_stage_that_lost_its_claim_leaves_every_file_untouched(site):

    download = site.download("Alpha", 2)
    run_part(site.cfg, intake)
    run_part(site.cfg, process)
    with closing(db.connect(site.cfg)) as conn:
        ctx = workflow.Context(site.cfg, conn, "export")

        def lost(stage, run):
            """The chapter's claim at stage, freed and taken by another run before the stage's act."""
            row = workflow.claim(ctx, (stage,))
            with db.transaction(conn):
                conn.execute(
                    "UPDATE chapter SET claimed_by = 'export@elsewhere', claim_count = claim_count + 1 WHERE id = %s",
                    (row["id"],),
                )
            with pytest.raises(workflow.ClaimLost):
                run(ctx, row)
            with db.transaction(conn):  # handed back, as the other run would
                conn.execute("UPDATE chapter SET status = 'ready', claimed_by = NULL WHERE id = %s", (row["id"],))

        def done(stage, run):
            assert isinstance(workflow.work(ctx, workflow.claim(ctx, (stage,)), run), workflow.Done)

        ((stored,),) = rows(site.cfg, "SELECT staged_path FROM chapter")
        staged = config.to_local(site.cfg, stored)
        before = staged.read_bytes()
        lost("comicinfo", comicinfo.run)
        assert staged.read_bytes() == before, "no ComicInfo.xml is written under a lost claim"
        done("comicinfo", comicinfo.run)
        lost("publish", publish.run)
        assert staged.exists(), "nothing is moved into the library under a lost claim"
        assert not published(site.cfg), "nothing is moved into the library under a lost claim"
        done("publish", publish.run)
        export.request_scan(ctx)
        site.world.kavita.run_scans()
        lost("confirm", confirm.run)
        assert download.exists(), "no download is put aside under a lost claim"


@budget(load="db")
def test_each_outcome_and_the_run_counts_are_logged(site, caplog):
    site.download("Alpha", 2)
    with caplog.at_level(logging.INFO, logger="manga_pipeline"):
        run_part(site.cfg, intake)
    identity = '["", null, "", "2", ""]'
    assert f"intake: chapter 1 (series 1, {identity}, at discover) done" in caplog.text
    assert f"intake: chapter 1 (series 1, {identity}, at validate) done" in caplog.text
    assert "intake: run ended: ok (2 done, 0 waiting, 0 failed)" in caplog.text


@budget(load="db", seconds=2)
@pytest.mark.parametrize("error", [workflow.Unreachable("share"), OSError(errno.EIO, "io")], ids=["unreachable", "eio"])
def test_unreachable_share_in_a_wait_check_ends_the_run(site, monkeypatch, error):
    site.download("Alpha", 2, data=cbz(credit=True))
    run_part(site.cfg, intake)
    run_part(site.cfg, process)

    def wait_over(ctx, chapter):
        raise error

    monkeypatch.setattr(junk, "junk_decision_over", wait_over)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = %s", (f"{workflow.owner('process')}.last_success",))
    run_part(site.cfg, process)
    got = meta(site.cfg, "process")
    assert got["process.last_result"] == "unreachable: share"
    assert "process.last_success" not in got, "a run that met an unreachable share is no success"


@budget(load="db", seconds=2)
def test_run_end_line_counts_waiting_and_failed(site, monkeypatch, caplog):
    site.download("Alpha", 2, data=cbz(credit=True))
    bad = site.download("Beta", 2)
    site.download("Gamma", 2)
    run_part(site.cfg, intake)
    real = process.STAGES["convert"]

    def convert(ctx, chapter):
        if chapter["download_path"] == config.to_stored(site.cfg, bad):
            msg = "cannot decode page 3"
            raise ValueError(msg)
        return real(ctx, chapter)

    monkeypatch.setitem(process.STAGES, "convert", convert)
    with caplog.at_level(logging.INFO, logger="manga_pipeline"):
        run_part(site.cfg, process)
    assert f"{workflow.owner('process')}: run ended: ok (3 done, 1 waiting, 1 failed)" in caplog.text
