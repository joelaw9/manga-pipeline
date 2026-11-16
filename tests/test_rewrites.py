"""Retroactive rewrites (SPEC "Retroactive updates"): a published file whose volume should change is rewritten in
place (renamed when its name holds the volume), never re-encoded, with the Kavita series id and the reader's progress
kept; queued on its file's row, finished step by step by export, restored once Kavita has re-read it."""

import io
import os
import time
import zipfile
from contextlib import closing

import pytest

from manga_pipeline import comicinfo as comicinfo_xml
from manga_pipeline import config, db, export, intake, process, retention, rewrite, state, workflow

from tests.timing import budget
from tests.world import World, cbz, comicinfo

SERIES = "Solo Leveling"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def library(world):
    """A Kavita series with 2 bulk chapters (Vol.1 Ch.1, Vol.1 Ch.2, their ComicInfo as the bulk converter wrote it)
    and chapter 3 published by the pipeline with no volume (Ch.3.cbz), all confirmed. Returns the Kavita id."""
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", "1"), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", "1"))
    kid = world.kavita_series(SERIES, bulk=bulk, basis="filename")
    for name, number in (("Vol.1 Ch.1.cbz", 1), ("Vol.1 Ch.2.cbz", 2)):
        xml = comicinfo(Series=SERIES, Number=number, Volume=1, Translator="Grp")
        (world.library / SERIES / name).write_bytes(cbz(pages=4, seed=name, comicinfo=xml))
    world.kavita.scan_library(4)
    world.kavita.run_scans()
    world.download(world.manga(SERIES), "Chapter 3", data=cbz(pages=4, seed="3"))
    for part in (intake, intake, process, export):
        world.run(part)
    world.kavita.run_scans()
    world.run(export)
    assert files(world) == ["Ch.3.cbz", "Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "set up"
    assert world.rows("SELECT count(*) FROM published_file WHERE state = 'confirmed'") == [(3,)], "set up"
    return kid


def files(world):
    return sorted(n for n in os.listdir(world.library / SERIES) if n.endswith(".cbz"))


def override(world, volumes):
    folder = config.abs_path(world.cfg["paths"]["naming_overrides"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{SERIES}.yaml").write_text(
        "volumes:\n" + "".join(f'  {v}: "{r}"\n' for v, r in volumes.items()), encoding="utf-8"
    )


def scan(world):
    """The next export requests a library scan (the last request older than kavita.rescan_after_minutes, SPEC
    "Publishing"), and Kavita runs it."""
    world.rows(f"UPDATE meta SET value = '2000-01-01T00:00:00+00:00' WHERE key = '{export.REQUESTED_AT}' RETURNING key")
    world.run(export)
    world.kavita.run_scans()


def progress(world, kid):
    return {f["path"].name: (f["pages_read"], f["pages"]) for f in world.kavita.series_files(kid)}


def read(world, kid, name, pages):
    f = next(f for f in world.kavita.series_files(kid) if f["path"].name == name)
    world.kavita.save_progress(kid, f["volume_id"], f["chapter_id"], pages)


@budget(load="db", seconds=30)
def test_an_override_range_rewrites_and_renames_published_files_keeping_progress(world):
    kid = library(world)
    read(world, kid, "Vol.1 Ch.2.cbz", 4)  # fully read
    read(world, kid, "Ch.3.cbz", 2)
    override(world, {1: "1", 2: "2-"})
    world.run(export)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"], (
        "the bulk file keeps its name, the pipeline file is renamed for its new volume; no old file is left"
    )
    assert world.rows("SELECT library_path, comicinfo_volume FROM published_file ORDER BY id") == [
        (f"library/{SERIES}/Vol.1 Ch.1.cbz", "1"),
        (f"library/{SERIES}/Vol.1 Ch.2.cbz", "2"),
        (f"library/{SERIES}/Vol.2 Ch.3.cbz", "2"),
    ], "1 row per file, at its new path and volume"
    kept = sorted(p.name for p in config.abs_path(world.cfg["paths"]["retention"]).rglob("*.cbz"))
    assert {"Ch.3.cbz", "Vol.1 Ch.2.cbz"} <= set(kept), "the old files are kept the retention way"
    scan(world)
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 0, "Kavita lost the progress (a new volume: a new chapter)"
    world.run(export)
    after = progress(world, kid)
    assert after["Vol.1 Ch.2.cbz"] == (4, 4), "fully read stays read"
    assert after["Vol.2 Ch.3.cbz"][0] == 2, "part-read keeps its pages"
    assert after["Vol.1 Ch.1.cbz"][0] == 0, "unread stays unread"
    assert world.rows("SELECT kavita_series_id FROM series") == [(kid,)], "the series id is kept"
    assert world.rows("SELECT count(*) FROM published_file WHERE indexed_since IS NOT NULL") == [(0,)], "done"


@budget(load="db", seconds=30)
def test_a_rewrite_keeps_the_old_files_own_fields_and_pages_and_carries_the_series_fields(world):
    library(world)
    before = (world.library / SERIES / "Vol.1 Ch.2.cbz").read_bytes()
    override(world, {1: "1", 2: "2-"})
    world.run(export)
    path = world.library / SERIES / "Vol.1 Ch.2.cbz"
    with zipfile.ZipFile(path) as new, zipfile.ZipFile(io.BytesIO(before)) as old:
        pages = [n for n in old.namelist() if n.endswith(".png")]
        assert all(new.read(n) == old.read(n) for n in pages), "pages copied byte for byte"
        fields = comicinfo_xml.read_all(new.read("ComicInfo.xml"))
    assert (fields["Volume"], fields["Number"], fields["Translator"]) == ("2", "2", "Grp"), "new volume, own fields"
    assert fields["Manga"] and fields["Series"] == SERIES, "the series-level fields now"


def crash_after(monkeypatch, step, name):
    """A crash (the process killed) right after finish's act `step` on the file named `name`, before it is recorded."""
    real = rewrite._record

    def record(conn, file_id, done):
        path = conn.execute("SELECT library_path FROM published_file WHERE id = %s", (file_id,)).fetchone()[0]
        if done == step and path.endswith("/" + name):
            msg = "killed"
            raise KeyboardInterrupt(msg)
        return real(conn, file_id, done)

    monkeypatch.setattr(rewrite, "_record", record)


@budget(load="db", seconds=60)
@pytest.mark.parametrize("name", ["Vol.1 Ch.2.cbz", "Ch.3.cbz"], ids=["same-name", "rename"])
@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_a_crash_after_each_act_of_a_rewrite_recovers_on_the_next_export(world, monkeypatch, step, name):
    kid = library(world)
    read(world, kid, "Vol.1 Ch.2.cbz", 2)
    read(world, kid, "Ch.3.cbz", 2)
    override(world, {2: "2-"})
    crash_after(monkeypatch, step, name)
    started = time.time() - 1
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    world.run(export)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"], "1 file per chapter"
    assert world.rows("SELECT library_path, comicinfo_volume, pending_path FROM published_file ORDER BY id")[1:] == [
        (f"library/{SERIES}/Vol.1 Ch.2.cbz", "2", None),
        (f"library/{SERIES}/Vol.2 Ch.3.cbz", "2", None),
    ], "finished from its recorded step"
    with zipfile.ZipFile(world.library / SERIES / "Vol.1 Ch.2.cbz") as z:
        assert comicinfo_xml.read_fields(z.read("ComicInfo.xml"))["Volume"] == "2", "the new file is in place"
    kept = config.abs_path(world.cfg["paths"]["retention"]) / "library-files" / SERIES
    assert os.stat(kept / name).st_mtime > started, "the old file kept, with its retention period from now"
    scan(world)
    world.run(export)
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 2
    assert progress(world, kid)["Vol.2 Ch.3.cbz"][0] == 2


@budget(load="db", seconds=30)
def test_state_reset_mid_rewrite_loses_neither_the_file_nor_the_progress(world, monkeypatch):
    kid = library(world)
    read(world, kid, "Ch.3.cbz", 2)
    override(world, {2: "2-"})
    crash_after(monkeypatch, 2, "Ch.3.cbz")  # linked into retention, not yet renamed in
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
    assert world.rows("SELECT count(*) FROM chapter") == [(0,)], "the rewrite chapters are gone"
    world.run(export)
    assert "Vol.2 Ch.3.cbz" in files(world) and "Ch.3.cbz" not in files(world), "finished from its row"
    scan(world)
    world.run(export)
    assert progress(world, kid)["Vol.2 Ch.3.cbz"][0] == 2, "the progress is restored"


@budget(load="db", seconds=30)
def test_a_target_already_in_the_library_is_never_overwritten(world):
    library(world)
    (world.library / SERIES / "Vol.2 Ch.3.cbz").write_bytes(b"someone else's")
    override(world, {2: "2-"})
    world.run(export)
    ((status, error),) = world.rows(
        "SELECT status, last_error FROM chapter WHERE kind = 'rewrite' AND status <> 'done'"
    )
    assert status == "needs_human"
    assert "Vol.2 Ch.3.cbz" in error
    assert (world.library / SERIES / "Vol.2 Ch.3.cbz").read_bytes() == b"someone else's"


@budget(load="db", seconds=60)
def test_a_rewrite_failing_before_its_rename_is_retried_then_stuck_and_dropped(world, monkeypatch):
    library(world)
    override(world, {2: "2-"})
    monkeypatch.setattr(retention, "put_library", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    for _ in range(4):  # workflow.attempt_limit 3: tried 3 times, then left for a person
        world.run(export)
    monkeypatch.undo()
    stuck = world.rows("SELECT id, pending_attempts, pending_error FROM published_file WHERE pending_error IS NOT NULL")
    assert [(n, "denied" in e) for _, n, e in stuck] == [(3, True), (3, True)], "3 attempts each; the run went on"
    with closing(db.connect(world.cfg)) as conn:
        for file_id, _, _ in stuck:
            rewrite.drop(conn, file_id, 3)
    world.run(export)  # compared again: the rewrites are made afresh
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"]


@budget(load="db", seconds=60)
def test_a_rewrite_failing_after_its_rename_is_not_dropped_but_retried(world, monkeypatch):
    library(world)
    override(world, {2: "2-"})
    real = type(world.library).unlink

    def unlink(self, *args, **kwargs):
        if self.name == "Ch.3.cbz":
            msg = "locked"
            raise PermissionError(msg)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(world.library), "unlink", unlink)
    for _ in range(3):
        world.run(export)
    monkeypatch.undo()
    ((file_id,),) = world.rows("SELECT id FROM published_file WHERE pending_error IS NOT NULL")
    with closing(db.connect(world.cfg)) as conn, pytest.raises(workflow.Refused, match="rewrite retry"):
        rewrite.drop(conn, file_id, 3)
    with closing(db.connect(world.cfg)) as conn:
        rewrite.retry(conn, file_id)
    world.run(export)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"], "finished: no second file left"


@budget(load="db", seconds=30)
def test_progress_already_higher_in_kavita_is_not_lowered(world):
    kid = library(world)
    read(world, kid, "Ch.3.cbz", 2)
    override(world, {2: "2-"})
    world.run(export)
    scan(world)
    read(world, kid, "Vol.2 Ch.3.cbz", 3)  # read on in Kavita before the restore
    world.run(export)
    assert progress(world, kid)["Vol.2 Ch.3.cbz"][0] == 3, "the carried progress is written only when higher"


@budget(load="db", seconds=60)
def test_a_rewrite_kavita_never_re_reads_is_charged_to_the_limit(world):
    library(world)
    override(world, {2: "2-"})
    world.run(export)
    for _ in range(3):  # 3 repeat requests, Kavita running none
        old = "2000-01-01T00:00:00+00:00"
        world.rows(f"UPDATE meta SET value = '{old}' WHERE key = '{export.REQUESTED_AT}' RETURNING key")
        world.rows(f"UPDATE published_file SET indexed_since = '{old}' WHERE indexed_since IS NOT NULL RETURNING id")
        world.run(export)
    errors = world.rows("SELECT pending_error FROM published_file WHERE pending_error IS NOT NULL")
    assert len(errors) == 2 and all("not re-read" in e for (e,) in errors), "past the limit: listed for a person"


@budget(load="db", seconds=30)
def test_naming_diff_lists_the_rewrites_and_the_differences_apart(world):
    library(world)
    override(world, {2: "2-"})
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "export")
        series = conn.execute("SELECT * FROM series WHERE origin = 'kavita'").fetchone()
        queued, wants, unknown, apart, edits = rewrite.diff(ctx, series)
    assert queued == [
        (f"library/{SERIES}/Vol.1 Ch.2.cbz", "1", "2", f"library/{SERIES}/Vol.1 Ch.2.cbz", None, None),
        (f"library/{SERIES}/Ch.3.cbz", None, "2", f"library/{SERIES}/Vol.2 Ch.3.cbz", None, None),
    ]
    assert (wants, unknown) == ([], False), "export's comparisons read the files' covers; no image to want"
    assert apart == [] and edits == [], "no source volume to read again in a filename series; no hand edits"
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite'") == [(0,)], "it writes nothing"


@budget(load="db", seconds=30)
def test_process_keeps_a_queued_rewrites_staged_file(world, monkeypatch):
    library(world)
    override(world, {2: "2-"})
    crash_after(monkeypatch, 1, "Ch.3.cbz")
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
        ((staged,),) = conn.execute("SELECT pending_staged FROM published_file WHERE pending_staged IS NOT NULL")
        path = config.to_local(world.cfg, staged)
        os.utime(path, (1, 1))  # older than a day
        process.clean_staging(world.context(conn, "process"))
    assert path.exists(), "a queued rewrite's staged file is kept"


@budget(load="db", seconds=30)
def test_the_cap_holds_and_a_series_cut_short_is_compared_again(tmp_path):
    world = World(tmp_path, export={"retention_days": 7, "rewrites_open": 1})
    library(world)
    before = world.rows("SELECT naming_hash FROM series_naming")
    override(world, {1: "1", 2: "2-"})
    with closing(db.connect(world.cfg)) as conn:
        rewrite.compare(world.context(conn, "export"))
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite'") == [(1,)], "1 open at a time"
    assert world.rows("SELECT naming_hash FROM series_naming") == before, "cut short: its old hash is kept"
    world.run(export)
    world.run(export)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"]
    assert world.rows("SELECT comicinfo_volume FROM published_file ORDER BY id") == [("1",), ("2",), ("2",)]


@budget(load="db", seconds=30)
def test_a_change_landing_during_a_rewrite_is_not_lost(world):
    library(world)
    override(world, {2: "2-"})
    with closing(db.connect(world.cfg)) as conn:
        rewrite.compare(world.context(conn, "export"))  # queued, not yet written
    override(world, {1: "1-2", 3: "3-"})
    world.run(export)  # the first rewrites finish
    scan(world)
    world.run(export)  # re-read and restored: the series is compared again with the new override
    world.run(export)
    assert world.rows("SELECT comicinfo_volume FROM published_file ORDER BY id") == [("1",), ("1",), ("3",)]
    assert "Vol.3 Ch.3.cbz" in files(world)


@budget(load="db", seconds=30)
def test_a_needs_human_or_out_of_scope_series_gets_no_rewrite(world):
    library(world)
    override(world, {2: "2-"})
    world.rows("UPDATE series SET status = 'needs_human', status_reason = 'held' RETURNING id")
    world.run(export)
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite'") == [(0,)], "a held series is not compared"


@budget(load="db", seconds=30)
def test_an_out_of_scope_series_gets_no_rewrite(tmp_path):
    world = World(tmp_path)
    library(world)
    override(world, {2: "2-"})
    out = World(tmp_path, series={"only": ["Another"]})
    with closing(db.connect(out.cfg)) as conn:
        rewrite.compare(out.context(conn, "export"))
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite'") == [(0,)], "outside series.only"
