"""scripts/move_series_folder.py (plan fix-new-series-source-folder, step 1): a series folder moved with its rows."""

import pytest

from manga_pipeline import export, intake, rename
from manga_pipeline.clients import suwayomi
from scripts import move_series_folder

from tests.builders import config_file, running
from tests.test_new_series_publish import ready
from tests.timing import budget
from tests.world import SOURCE_FOLDER, World, cbz

AT_SOURCE = f"library/{SOURCE_FOLDER}/Brand New Thing"
AT_ROOT = "library/Brand New Thing"


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = World(tmp_path / "world")
    monkeypatch.setattr(suwayomi, "from_config", lambda _cfg: world.suwayomi)
    return world


def published(world):
    """Return the id of a new series published into its source folder (its 3 files) and confirmed."""
    sid, _ = ready(world)
    world.run(export)
    world.kavita.run_scans()
    world.run(export)
    return sid


def move(world, tmp_path, *args):
    """Run the script as from the command line; return its exit code."""
    return move_series_folder.main([*args, "--config", str(config_file(tmp_path, world.cfg))])


def rows(world, sid):
    """Return the series' folder and the library paths of its published files and chapters, as stored."""
    (folder,) = world.rows("SELECT folder FROM series WHERE id = %s", sid)[0]
    files = sorted(r[0] for r in world.rows("SELECT library_path FROM published_file WHERE series_id = %s", sid))
    chapters = sorted(r[0] for r in world.rows("SELECT library_path FROM chapter WHERE series_id = %s", sid))
    return folder, files, chapters


def under(folder):
    return (folder, [f"{folder}/Ch.{n}.cbz" for n in (1, 2, 3)], [f"{folder}/Ch.{n}.cbz" for n in (1, 2, 3)])


def on_disk(world, folder):
    path = world.library.joinpath(*folder.split("/")[1:])
    return sorted(p.name for p in path.iterdir()) if path.exists() else None


@budget(load="db", seconds=30)
def test_a_move_renames_the_folder_and_rewrites_its_3_columns_and_moves_back_to_the_source_folder(
    world, tmp_path, capsys
):
    sid = published(world)
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 0
    assert rows(world, sid) == under(AT_ROOT), "series.folder, published_file and chapter library paths rewritten"
    assert on_disk(world, AT_ROOT) == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"], '--to-parent "" puts it at the root'
    assert on_disk(world, AT_SOURCE) is None, "1 rename: nothing left behind"
    assert move(world, tmp_path, str(sid)) == 0
    assert rows(world, sid) == under(AT_SOURCE), "without --to-parent: its source folder (admit.source_folder)"
    assert f"series {sid}: {AT_ROOT} -> {AT_SOURCE}: moved" in capsys.readouterr().out


@budget(load="db", seconds=30)
def test_a_dry_run_prints_the_move_and_writes_nothing(world, tmp_path, capsys):
    sid = published(world)
    assert move(world, tmp_path, str(sid), "--to-parent", "", "--dry-run") == 0
    assert rows(world, sid) == under(AT_SOURCE), "no row written"
    assert on_disk(world, AT_SOURCE) == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"], "no folder moved"
    assert f"series {sid}: {AT_SOURCE} -> {AT_ROOT}: moved" in capsys.readouterr().out


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("change", "why"),
    [
        ("UPDATE published_file SET pending_error = 'failed' RETURNING id", "a published file is busy"),
        ("UPDATE chapter SET stage = 'publish', status = 'ready' WHERE id = (SELECT min(id) FROM chapter) RETURNING id",
         "on its way through publish or confirm"),
    ],
    ids=["busy-file", "chapter-at-publish"],
)  # fmt: skip
def test_a_series_in_flight_is_refused_and_left_as_it_is(world, tmp_path, capsys, change, why):
    sid = published(world)
    world.rows(change)
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 2, "a refused series: exit 2"
    assert why in capsys.readouterr().out, "the refusal names why"
    assert rows(world, sid) == under(AT_SOURCE), "nothing written"
    assert on_disk(world, AT_SOURCE) == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"], "nothing moved"


@budget(load="db", seconds=30)
def test_a_rename_whose_rows_were_not_committed_is_resumed_with_rows_only(world, tmp_path, capsys):
    sid = published(world)
    world.library.joinpath(SOURCE_FOLDER, "Brand New Thing").rename(world.library / "Brand New Thing")  # then killed
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 0
    assert rows(world, sid) == under(AT_ROOT), "the rows follow the folder already renamed"
    assert "rows only (resumed)" in capsys.readouterr().out


@budget(load="db", seconds=30)
def test_a_failed_transaction_renames_the_folder_back_and_stops(world, tmp_path, monkeypatch):
    sid = published(world)

    def fails(_conn, _series_id, _old, _new):
        msg = "the database went away"
        raise RuntimeError(msg)

    monkeypatch.setattr(rename, "folder_rows", fails)
    with pytest.raises(RuntimeError, match="went away"):
        move(world, tmp_path, str(sid), "--to-parent", "")
    assert on_disk(world, AT_SOURCE) == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"], "renamed back"
    assert on_disk(world, AT_ROOT) is None, "renamed back"
    assert rows(world, sid) == under(AT_SOURCE), "nothing committed"


@budget(load="db", seconds=20)
@pytest.mark.parametrize("stray", [None, "Stray.cbz"], ids=["empty", "with-a-file"])
def test_a_fileless_series_whose_target_exists_is_refused(world, tmp_path, capsys, stray):
    sid, _ = ready(world)  # admitted, nothing published: no folder on disk
    target = world.library / "Brand New Thing"
    target.mkdir()
    if stray:
        (target / stray).write_bytes(cbz(seed="stray"))
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 2
    assert "the target exists" in capsys.readouterr().out, "a folder it did not make is never taken for its own"
    assert world.rows("SELECT folder FROM series WHERE id = %s", sid) == [(AT_SOURCE,)], "nothing written"


@budget(load="db", seconds=20)
def test_a_fileless_series_moves_its_rows_only_and_its_empty_folder_is_removed(world, tmp_path, capsys):
    sid, _ = ready(world)
    world.library.joinpath(SOURCE_FOLDER, "Brand New Thing").mkdir(parents=True)  # made, nothing published yet
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 0
    assert world.rows("SELECT folder FROM series WHERE id = %s", sid) == [(AT_ROOT,)]
    assert on_disk(world, AT_SOURCE) is None, "the empty old folder removed"
    assert on_disk(world, AT_ROOT) is None, "no folder made: export makes it at the first publish"
    assert "rows only (no files)" in capsys.readouterr().out


@budget(load="db", seconds=30)
def test_the_script_is_refused_while_a_part_runs(world, tmp_path, monkeypatch):
    sid = published(world)
    asked = []
    monkeypatch.setattr(suwayomi, "from_config", lambda _cfg: asked.append("suwayomi") or world.suwayomi)
    with running(world.cfg, "export"):
        assert move(world, tmp_path, str(sid)) == 3, "lock.IDLE exclusive: refused at once"
    assert asked == [], "Suwayomi is asked only under the lock"
    assert rows(world, sid) == under(AT_SOURCE), "nothing written"


@budget(load="db", seconds=30)
def test_a_folder_missing_some_of_its_published_files_is_not_moved(world, tmp_path, capsys):
    sid = published(world)
    world.library.joinpath(SOURCE_FOLDER, "Brand New Thing", "Ch.2.cbz").unlink()
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 2
    assert "the folder does not hold its published files" in capsys.readouterr().out
    assert rows(world, sid) == under(AT_SOURCE), "nothing written"
    assert on_disk(world, AT_SOURCE) == ["Ch.1.cbz", "Ch.3.cbz"], "nothing moved"


@budget(load="db", seconds=10)
def test_a_series_that_is_not_new_is_refused(world, tmp_path, capsys):
    world.kavita_series("Solo Leveling")
    world.run(intake)
    ((sid,),) = world.rows("SELECT id FROM series WHERE origin = 'kavita'")
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 2
    assert f"series {sid}: refused: Solo Leveling is not a new series" in capsys.readouterr().out
    assert on_disk(world, "library/Solo Leveling") == [".optimizer.json", "Ch.1.cbz"], "nothing moved"


@budget(load="db", seconds=30)
def test_every_series_is_refused_while_the_library_root_is_not_there(world, tmp_path, capsys):
    sid = published(world)
    unmounted = tmp_path / "unmounted"
    world.library.rename(unmounted)
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 2, "refused before any series"
    assert "is not there" in capsys.readouterr().err
    unmounted.rename(world.library)
    assert rows(world, sid) == under(AT_SOURCE), "nothing written"


@budget(load="db", seconds=30)
@pytest.mark.parametrize("parent", ["Nope", f"{SOURCE_FOLDER}/Brand New Thing"], ids=["absent", "not-directly-under"])
def test_a_to_parent_that_is_no_folder_directly_under_the_root_is_refused(world, tmp_path, capsys, parent):
    sid = published(world)
    assert move(world, tmp_path, str(sid), "--to-parent", parent) == 2
    assert "is not a folder directly under the library root" in capsys.readouterr().err
    assert rows(world, sid) == under(AT_SOURCE), "nothing written"


@budget(load="db", seconds=30)
def test_a_series_whose_files_folder_and_target_are_both_missing_is_refused(world, tmp_path, capsys):
    sid = published(world)
    world.library.joinpath(SOURCE_FOLDER, "Brand New Thing").rename(tmp_path / "elsewhere")
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 2
    assert "is missing, yet the series has published files" in capsys.readouterr().out
    assert rows(world, sid) == under(AT_SOURCE), "rows never point at a folder its files are not in"


@budget(load="db", seconds=30)
def test_already_there_is_refused_when_its_files_lie_elsewhere(world, tmp_path, capsys):
    sid = published(world)
    world.library.joinpath(SOURCE_FOLDER, "Brand New Thing").rename(world.library / "Brand New Thing")
    assert move(world, tmp_path, str(sid)) == 2, "target = its stored folder, which no longer holds its files"
    assert "the folder does not hold its published files" in capsys.readouterr().out


@budget(load="db", seconds=30)
def test_a_series_with_no_target_or_no_row_is_refused(world, tmp_path, capsys, monkeypatch):
    sid = published(world)
    monkeypatch.setattr(world.suwayomi, "sources", list)  # its manga's source is not installed
    assert move(world, tmp_path, str(sid), "9999") == 2
    out = capsys.readouterr().out
    assert f"series {sid}: refused: the source {world.source} of its manga" in out, "no target: refused naming why"
    assert "series 9999: refused: no such series" in out
    assert rows(world, sid) == under(AT_SOURCE), "nothing written"


@budget(load="db", seconds=20)
def test_a_fileless_series_rows_only_move_rewrites_its_chapters_library_paths(world, tmp_path):
    sid, _ = ready(world)
    world.rows(  # named at comicinfo, as export does before publish
        "UPDATE chapter SET library_path = %s || '/Ch.' || id || '.cbz' WHERE series_id = %s RETURNING id",
        AT_SOURCE,
        sid,
    )
    assert move(world, tmp_path, str(sid), "--to-parent", "") == 0
    paths = [r[0] for r in world.rows("SELECT library_path FROM chapter WHERE series_id = %s", sid)]
    assert paths, "set up: chapters with library paths"
    assert all(p.startswith(f"{AT_ROOT}/Ch.") for p in paths), "chapter.library_path under the new folder"
