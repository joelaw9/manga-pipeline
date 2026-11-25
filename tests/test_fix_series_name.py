"""scripts/fix_series_name.py (plan fix-adopted-series-name, step 1): adopted series take their files' Series name."""

import errno
import io
import json
import zipfile
from contextlib import closing

import pytest

from manga_pipeline import archive, comicinfo, config, db, export, intake, process, retention
from scripts import fix_series_name

from tests.builders import config_file, running
from tests.test_baseline import library_series, named_files
from tests.timing import budget
from tests.world import World

NAME = "Na Honjaman Level Up"  # the Series the library files carry: Kavita's original name


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / "world")


def adopted(world, chapters=()):
    """Return the id of a series adopted as the old adoption left it (Kavita's name), its chapters published since.

    Its 2 library files carry NAME; each pipeline file (Ch.<n> of chapters) carries Kavita's name, "Solo Leveling".
    """
    library_series(world, named_files(NAME, NAME), original=NAME)
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("UPDATE series SET comicinfo_series = name")
        conn.execute("UPDATE published_file SET comicinfo_series = 'Solo Leveling'")
    manga = world.manga("Solo Leveling")
    for n in chapters:
        world.download(manga, f"Chapter {n}", scanlator="Grp")
    if chapters:
        world.run(intake)
        world.run(process)
        world.run(export)
    ((sid,),) = world.rows("SELECT id FROM series")
    assert len(world.rows("SELECT 1 FROM published_file WHERE origin = 'pipeline'")) == len(chapters), "set up"
    return sid


def fix(world, tmp_path, *args):
    """Run the script as from the command line; return its exit code."""
    return fix_series_name.main([*args, "--config", str(config_file(tmp_path, world.cfg))])


def pipeline_files(world):
    """Return [(local path, size row)] of the pipeline files, by path."""
    rows = world.rows("SELECT library_path, size FROM published_file WHERE origin = 'pipeline' ORDER BY 1")
    return [(config.to_local(world.cfg, path), size) for path, size in rows]


def series_of(path):
    info = archive.read_comicinfo(path)
    assert info is not None
    return comicinfo.read_all(info).get("Series")


def members(path):
    """Return {member: bytes} of a file but its ComicInfo.xml."""
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist() if n != archive.COMICINFO}


def names(world):
    """Return the series' name and every row's (origin, comicinfo_series), as stored."""
    (series,) = world.rows("SELECT comicinfo_series FROM series")[0]
    return series, sorted(world.rows("SELECT origin, comicinfo_series FROM published_file"))


def retained(world):
    root = config.abs_path(world.cfg["paths"]["retention"]) / "library-files"
    return sorted(p.name for p in root.rglob("*.cbz")) if root.exists() else []


FIXED = (NAME, [("library", NAME), ("library", NAME), ("pipeline", NAME), ("pipeline", NAME)])


@budget(load="db", seconds=30)
def test_a_pipeline_file_is_rewritten_with_the_files_name_its_pages_unchanged_and_its_rows_follow(
    world, tmp_path, capsys
):
    sid = adopted(world, chapters=(3, 4))
    before = {path: members(path) for path, _ in pipeline_files(world)}
    assert world.rows("SELECT DISTINCT stage, status, comicinfo_series FROM chapter") == [
        ("confirm", "waiting", "Solo Leveling")
    ], "set up: open chapters, waiting for Kavita's scan"
    assert fix(world, tmp_path, str(sid)) == 0
    assert names(world) == FIXED, "the series and every row: the files' name"
    for path, size in pipeline_files(world):
        assert series_of(path) == NAME, "ComicInfo.xml Series replaced"
        assert members(path) == before[path], "every other member byte for byte"
        assert size == path.stat().st_size, "the row's size is the file's on disk"
        assert not path.with_name(f"{path.name}.part").exists(), "no temp file left"
    assert retained(world) == ["Ch.3.cbz", "Ch.4.cbz"], "each old file kept the retention way"
    assert world.rows("SELECT DISTINCT comicinfo_series FROM chapter") == [(NAME,)], "the open chapters follow"
    assert f"series {sid}: 'Solo Leveling' -> {NAME!r}: 2 files to rewrite" in capsys.readouterr().out
    assert fix(world, tmp_path, str(sid)) == 0
    assert f"series {sid}: already right" in capsys.readouterr().out, "a second run changes nothing"


@budget(load="db", seconds=20)
def test_a_series_without_pipeline_files_has_its_rows_fixed_only(world, tmp_path):
    adopted(world)
    library = sorted(world.library.joinpath("Solo Leveling").iterdir())
    stamps = [p.stat().st_mtime_ns for p in library]
    assert fix(world, tmp_path, "--all") == 0
    assert names(world) == (NAME, [("library", NAME), ("library", NAME)]), "series and library rows"
    assert [p.stat().st_mtime_ns for p in library] == stamps, "no library file is touched"
    assert retained(world) == []


@budget(load="db", seconds=30)
def test_a_dry_run_prints_and_records_the_fix_and_writes_nothing(world, tmp_path, capsys):
    sid = adopted(world, chapters=(3,))
    ((path, size),) = pipeline_files(world)
    assert fix(world, tmp_path, "--all", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"series {sid}: 'Solo Leveling' -> {NAME!r}: 1 files to rewrite" in out
    stored = config.to_stored(world.cfg, path)
    assert f"  {stored}" in out, "the file it would rewrite"
    (record,) = (config.abs_path(world.cfg["paths"]["scratch"]) / "fix-series-name").glob("*.json")
    assert json.loads(record.read_bytes()) == [
        {"series_id": sid, "kavita_name": "Solo Leveling", "old": "Solo Leveling", "new": NAME, "rewrite": [stored]}
    ], "the rollback record: the old name per series"
    assert series_of(path) == "Solo Leveling", "no file written"
    assert names(world)[0] == "Solo Leveling", "no row written"
    assert pipeline_files(world) == [(path, size)]


@budget(load="db", seconds=30)
def test_a_run_killed_between_files_is_finished_by_a_rerun_skipping_the_file_already_right(
    world, tmp_path, monkeypatch
):
    sid = adopted(world, chapters=(3, 4))
    real = retention.put_library
    kept = []

    def killed_before_the_2nd(cfg, file):
        if kept:
            raise KeyboardInterrupt
        real(cfg, file)
        kept.append(file)

    (first, _), (second, _) = pipeline_files(world)
    with monkeypatch.context() as m:
        m.setattr(retention, "put_library", killed_before_the_2nd)
        with pytest.raises(KeyboardInterrupt):
            fix(world, tmp_path, str(sid))
    assert names(world)[0] == "Solo Leveling", "no row written before the kill"
    assert series_of(first) == NAME
    assert series_of(second) == "Solo Leveling"
    second.with_name(f"{second.name}.part").write_bytes(b"a killed run's leftover")
    assert fix(world, tmp_path, str(sid)) == 0
    assert names(world) == FIXED
    assert [series_of(p) for p, _ in pipeline_files(world)] == [NAME, NAME]
    assert all(size == p.stat().st_size for p, size in pipeline_files(world)), "every pipeline row's size from disk"
    assert retained(world) == ["Ch.3.cbz", "Ch.4.cbz"], "the file rewritten before the kill is not rewritten again"
    assert not second.with_name(f"{second.name}.part").exists(), "the leftover is replaced"


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("change", "why"),
    [
        (
            "UPDATE published_file SET pending_path = library_path, pending_staged = 's', pending_step = 0",
            "a published file is busy",
        ),
        ("UPDATE published_file SET pending_error = 'failed' WHERE origin = 'library'", "a published file is busy"),
        ("UPDATE published_file SET indexed_since = now() WHERE origin = 'pipeline'", "a published file is busy"),
        (
            "UPDATE chapter SET status = 'claimed', claimed_by = 'process@elsewhere', waiting_on = NULL",
            "a chapter is claimed",
        ),
        ("UPDATE chapter SET stage = 'publish', status = 'ready', waiting_on = NULL", "on its way through publish"),
    ],
    ids=["pending-path", "pending-error", "indexed-since", "claimed", "at-publish"],
)
def test_a_series_in_flight_is_refused_and_left_as_it_is(world, tmp_path, capsys, change, why):
    sid = adopted(world, chapters=(3,))
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(change)
    assert fix(world, tmp_path, "--all") == 2, "a refused series: exit 2"
    out = capsys.readouterr().out
    assert f"series {sid}: refused: " in out
    assert why in out, "the refusal names why"
    assert f"refused series: {sid}" in out, "listed for the user at the end"
    assert names(world)[0] == "Solo Leveling", "nothing written"
    assert [series_of(p) for p, _ in pipeline_files(world)] == ["Solo Leveling"], "no file rewritten"


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("series", "why"),
    [((NAME, "Other Thing"), "differing Series names"), (("Other Thing", "Other Thing"), "none of Kavita's names")],
    ids=["differing", "not-kavitas"],
)
def test_library_files_whose_name_adoption_would_refuse_are_refused(world, tmp_path, capsys, series, why):
    sid = adopted(world, chapters=(3,))
    folder = world.library / "Solo Leveling"
    for name, data in named_files(*series).items():
        (folder / name).write_bytes(data)
    assert fix(world, tmp_path, str(sid)) == 2
    assert why in capsys.readouterr().out
    assert names(world)[0] == "Solo Leveling", "nothing written"


@budget(load="db", seconds=20)
def test_the_run_is_refused_when_retention_keeps_nothing(world, tmp_path, capsys):
    adopted(world)
    world.cfg = {**world.cfg, "export": {**world.cfg["export"], "retention_days": 0}}
    assert fix(world, tmp_path, "--all") == 2
    assert "export.retention_days is 0" in capsys.readouterr().err
    assert names(world)[0] == "Solo Leveling", "nothing written"


@budget(load="db", seconds=20)
def test_a_series_that_is_not_adopted_is_refused(world, tmp_path, capsys):
    world.kavita_series("Bulk Done")
    world.run(intake)
    ((sid,),) = world.rows("SELECT id FROM series")
    assert fix(world, tmp_path, str(sid)) == 2
    assert f"series {sid}: refused: Bulk Done is not an adopted series" in capsys.readouterr().out


@budget(load="db", seconds=20)
def test_the_script_is_refused_while_a_part_runs_and_a_dry_run_is_not(world, tmp_path):
    sid = adopted(world)
    with running(world.cfg, "export"):
        assert fix(world, tmp_path, str(sid)) == 3, "lock.IDLE exclusive: refused at once"
        assert fix(world, tmp_path, str(sid), "--dry-run") == 0, "a dry run takes no lock"
    assert names(world)[0] == "Solo Leveling", "nothing written"


def unzipped_comicinfo(path, xml=None):
    """Rewrite a file with its ComicInfo.xml replaced by xml, or left out (None)."""
    kept = members(path)
    path.write_bytes(b"")
    archive.write(path, [*kept.items(), *([(archive.COMICINFO, xml)] if xml is not None else [])])


@budget(load="db", seconds=20)
def test_a_series_that_does_not_exist_is_refused(world, tmp_path, capsys):
    adopted(world)
    assert fix(world, tmp_path, "999") == 2
    assert "series 999: refused: no such series" in capsys.readouterr().out


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("damage", "why"),
    [("library", "cannot be read"), ("no-comicinfo", "holds no ComicInfo.xml"), ("bad-field", "does not rebuild")],
    ids=["unreadable-library-file", "pipeline-file-without-comicinfo", "comicinfo-that-does-not-rebuild"],
)
def test_a_file_that_cannot_be_read_or_rewritten_is_refused_by_the_dry_run_and_the_real_run(
    world, tmp_path, capsys, damage, why
):
    sid = adopted(world, chapters=(3,))
    ((path, _),) = pipeline_files(world)
    if damage == "library":
        (world.library / "Solo Leveling" / "Ch.1.cbz").write_bytes(b"not a zip")
    elif damage == "no-comicinfo":
        unzipped_comicinfo(path)
    else:
        unzipped_comicinfo(
            path, b"<?xml version='1.0'?><ComicInfo><Series>Solo Leveling</Series><Bogus>x</Bogus></ComicInfo>"
        )
    for dry in (["--dry-run"], []):
        assert fix(world, tmp_path, str(sid), *dry) == 2
        assert why in capsys.readouterr().out, "the refusal names the file's problem, in the dry run too"
    assert names(world)[0] == "Solo Leveling", "nothing written"
    assert retained(world) == [], "no retention link made"


@budget(load="db", seconds=20)
def test_every_series_is_refused_while_the_library_root_is_not_there(world, tmp_path, capsys):
    sid = adopted(world)
    unmounted = tmp_path / "unmounted"
    world.library.rename(unmounted)
    assert fix(world, tmp_path, str(sid)) == 2, "refused before any series"
    assert "is not there" in capsys.readouterr().err
    unmounted.rename(world.library)
    assert names(world)[0] == "Solo Leveling", "nothing written"


@budget(load="db", seconds=30)
def test_all_goes_on_past_a_refused_series_and_fixes_the_next(world, tmp_path, capsys):
    library_series(world, named_files(NAME, NAME), original=NAME)
    library_series(world, named_files("Second Thing.", "Second Thing."), name="Second Thing")
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("UPDATE series SET comicinfo_series = name")
        conn.execute("UPDATE published_file SET comicinfo_series = 'x'")
    (first, second) = (r[0] for r in world.rows("SELECT id FROM series ORDER BY id"))
    (world.library / "Solo Leveling" / "Ch.1.cbz").write_bytes(b"not a zip")
    assert fix(world, tmp_path, "--all") == 2
    out = capsys.readouterr().out
    assert f"series {first}: refused: " in out
    assert f"refused series: {first}\n" in out, "only the refused series is listed"
    assert world.rows("SELECT id, comicinfo_series FROM series ORDER BY id") == [
        (first, "Solo Leveling"),
        (second, "Second Thing."),
    ], "the series after the refused one is fixed"


@budget(load="db", seconds=30)
def test_the_real_run_writes_its_rollback_record_before_it_fixes(world, tmp_path, capsys):
    sid = adopted(world, chapters=(3,))
    ((path, _),) = pipeline_files(world)
    assert fix(world, tmp_path, str(sid)) == 0
    (record,) = (config.abs_path(world.cfg["paths"]["scratch"]) / "fix-series-name").glob("*.json")
    assert json.loads(record.read_bytes()) == [
        {
            "series_id": sid,
            "kavita_name": "Solo Leveling",
            "old": "Solo Leveling",
            "new": NAME,
            "rewrite": [config.to_stored(world.cfg, path)],
        }
    ], "the old name per series, for a rollback"
    assert f"record: {record}" in capsys.readouterr().out


@budget(load="db", seconds=30)
def test_a_share_error_reading_a_file_ends_the_run_rather_than_refusing_the_series(world, tmp_path, monkeypatch):
    sid = adopted(world, chapters=(3,))

    def share_gone(_path):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(archive, "read_comicinfo", share_gone)
    with pytest.raises(OSError, match="input/output error"):
        fix(world, tmp_path, str(sid))
    assert names(world)[0] == "Solo Leveling", "nothing written"


@budget(load="pure")
@pytest.mark.parametrize("argv", [[], ["--all", "3"]], ids=["neither", "both"])
def test_all_or_series_ids_is_given_not_both(argv):
    with pytest.raises(SystemExit):
        fix_series_name.parse(argv)


@budget(load="pure")
def test_output_that_is_not_utf8_refuses_the_run_before_anything_is_read(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert fix_series_name.main(["--all", "--dry-run"]) == 2, "a series name outside cp1252 would stop the run midway"
    assert "PYTHONUTF8=1" in capsys.readouterr().err, "the error names the fix"
