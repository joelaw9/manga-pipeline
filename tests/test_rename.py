"""`series names` and `series rename` (SPEC "Retroactive updates", Series names).

Kavita first, then the files, the folder, the rows.
"""

import copy
import errno
import sys
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import archive, cli, comicinfo, config, db, intake, rename, status, workflow
from manga_pipeline.clients import http
from manga_pipeline.metadata import check, series_name, step

from tests.builders import anilist_media, config_file
from tests.timing import budget
from tests.world import World
from tests.world import comicinfo as comicinfo_xml

AL = "105398"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / "world")


def checked(world, english, romaji=None, **where):
    """Add a bulk series Kavita holds as name (in folder), with AniList's record; run intake; return its id.

    where: name (default "Ore Dake"), folder (default the name), al_id (its AniList id, default AL).

    Its files carry name as ComicInfo Series, as the bulk converter wrote it.
    """
    name, folder, al_id = where.get("name", "Ore Dake"), where.get("folder"), where.get("al_id", AL)
    world.kavita_series(name, folder)
    for n, f in enumerate(sorted((world.library / (folder or name)).glob("*.cbz")), 1):
        archive.replace_member(f, archive.COMICINFO, comicinfo_xml(Series=name, Number=n).encode())
    world.sources.anilist.records[("media", al_id)] = anilist_media(
        id=int(al_id), title={"romaji": romaji, "english": english, "native": None}
    )
    world.run(intake)
    (sid,) = world.rows("SELECT id FROM series WHERE name = %s", name)[0]
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', %s)", (sid, al_id))
        check.make_urgent(conn, sid)
        conn.execute("DELETE FROM meta WHERE key = %s", (step.BASELINE_PASS_AT,))  # the daily baseline pass: now
    world.run(intake)
    return sid


def renamed(world, *ids):
    """Rename the series (all due ones without ids) as the command does; return (done, left).

    Kavita runs each scan the wait requests, so it follows each moved folder before the localized names are given
    (the wait gives up after about 1 s: a series Kavita does not follow keeps its old name as its localized name).
    """
    outcome = rename.Outcome()
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "export")
        rename.apply(ctx, rename.check(ctx, rename.read_db(world.cfg, conn, list(ids) or None)), outcome)
        behind = rename.wait_for_kavita(ctx, outcome.moved, 0.02, sleep=lambda _s: world.kavita.run_scans())
        rename.finish_names(ctx, outcome, behind)
    return outcome.renamed, outcome.left


def files(world, folder):
    """Return {file name: (Series, LocalizedSeries)} of the CBZs in the library folder ({} when it is not there)."""
    path = world.library / folder
    out = {}
    for f in sorted(path.glob("*.cbz")):
        fields = comicinfo.read_all(archive.read_comicinfo(f) or b"<ComicInfo/>")
        out[f.name] = (fields.get("Series"), fields.get("LocalizedSeries"))
    return out


def kavita(world, sid):
    (kid,) = world.rows("SELECT kavita_series_id FROM series WHERE id = %s", sid)[0]
    s = world.kavita.series_rows[kid]
    return kid, (s["name"], s.get("localizedName"), s.get("nameLocked"), s.get("localizedNameLocked"))


@budget(load="db", seconds=10)
def test_a_series_is_renamed_in_kavita_then_its_files_and_folder_and_keeps_its_kavita_id(world):
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    before = files(world, "Ore Dake")
    kid, _ = kavita(world, sid)
    done, left = renamed(world)
    assert left == [], "nothing left"
    assert [p.series["id"] for p in done] == [sid]
    assert kavita(world, sid) == (kid, ("Solo Leveling", "Ore Dake Level Up", True, True)), (
        "Kavita's name the new one, its localized name the romaji, both locked"
    )
    assert files(world, "Ore Dake") == {}, "the old folder is gone"
    assert files(world, "Solo Leveling") == dict.fromkeys(before, ("Solo Leveling", "Ore Dake Level Up")), (
        "every file carries the name as Series and the romaji as LocalizedSeries"
    )
    assert world.rows("SELECT name, comicinfo_series, localized_name, folder FROM series") == [
        ("Solo Leveling", "Solo Leveling", "Ore Dake Level Up", "library/Solo Leveling")
    ], "1 transaction stores the names and the folder"
    paths = world.rows("SELECT library_path, comicinfo_series FROM published_file ORDER BY 1")
    assert paths == [(f"library/Solo Leveling/{n}", "Solo Leveling") for n in sorted(before)], "file rows follow"
    kept = list((config.abs_path(world.cfg["paths"]["retention"]) / "library-files" / "Ore Dake").glob("*.cbz"))
    assert sorted(p.name for p in kept) == sorted(before), "each old file is kept the retention way"
    world.kavita.scan_library(world.cfg["kavita"]["library_id"])
    world.kavita.run_scans()
    assert kavita(world, sid)[0] == kid, "the scan matches the files to the same series"
    folder = world.kavita.series_folder(kid)
    assert folder == world.library / "Solo Leveling", "Kavita follows the folder"
    assert renamed(world) == ([], []), "nothing is due afterwards"


@budget(load="db", seconds=10)
def test_a_name_held_only_by_its_folder_is_renamed_as_kavita_allows(world):
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    kid, _ = kavita(world, sid)
    world.kavita.series_rows[kid].update(localizedName="Ore Dake Level Up", originalName="Na Honjaman")
    done, left = renamed(world, sid)
    assert (left, [p.series["id"] for p in done]) == ([], [sid]), (
        "Kavita refuses to drop a name its folder carries unless the localized name holds it: written first"
    )
    assert kavita(world, sid) == (kid, ("Solo Leveling", "Ore Dake Level Up", True, True))


@budget(load="db", seconds=10)
def test_a_series_whose_name_is_right_gets_only_its_folder_renamed(world):
    sid = checked(world, "Solo Leveling", name="Solo Leveling", folder="Solo Leveling (old)")
    before = files(world, "Solo Leveling (old)")
    writes = len(world.kavita.writes)
    done, _ = renamed(world)
    assert [p.series["id"] for p in done] == [sid]
    assert files(world, "Solo Leveling") == before, "files untouched"
    assert [w for w in world.kavita.writes[writes:] if w[0] != "scan_library"] == [], (
        "no Kavita write for a folder rename"
    )
    assert world.rows("SELECT folder FROM series") == [("library/Solo Leveling",)]


@budget(load="db", seconds=10)
def test_a_wrong_kavita_localized_name_is_the_only_change_when_all_else_is_right(world):
    sid = checked(world, "Solo Leveling", romaji="Ore Dake", name="Solo Leveling")
    kid, _ = kavita(world, sid)
    world.kavita.series_rows[kid]["localizedName"] = "Something Else"
    before = files(world, "Solo Leveling")
    done, _ = renamed(world)
    assert [p.series["id"] for p in done] == [sid]
    assert kavita(world, sid)[1] == ("Solo Leveling", "Ore Dake", True, True), "the romaji written, the name kept"
    assert files(world, "Solo Leveling") == before, "no file rewritten"


@budget(load="db", seconds=10)
def test_a_name_another_series_holds_holds_the_series_and_changes_nothing(world):
    world.kavita_series("Solo-Leveling")
    sid = checked(world, "Solo Leveling")
    before = files(world, "Ore Dake")
    done, left = renamed(world, sid)
    assert done == []
    assert "not free" in left[0], "listed"
    assert world.rows("SELECT status FROM series WHERE id = %s", sid) == [("needs_human",)], "D2: a clash holds it"
    assert files(world, "Ore Dake") == before, "nothing changed"
    assert kavita(world, sid)[1][0] == "Ore Dake", "Kavita's name unchanged"


@budget(load="db", seconds=10)
def test_a_romaji_another_series_holds_is_left_out(world):
    world.kavita_series("Ore-Dake Level Up")
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    renamed(world, sid)
    assert kavita(world, sid)[1][:2] == ("Solo Leveling", ""), "no romaji: Kavita's localized name emptied"
    assert set(files(world, "Solo Leveling").values()) == {("Solo Leveling", None)}, "no LocalizedSeries"


@budget(load="db", seconds=10)
def test_a_kavita_write_that_does_not_read_back_touches_no_file(world, monkeypatch):
    sid = checked(world, "Solo Leveling")
    before = files(world, "Ore Dake")
    monkeypatch.setattr(world.kavita, "rename_series", lambda kid, _n, _l: world.kavita.series(kid))
    done, left = renamed(world, sid)
    assert done == []
    assert "not what was sent" in left[0], "listed"
    assert files(world, "Ore Dake") == before, "no file touched"
    assert world.rows("SELECT comicinfo_series FROM series") == [("Ore Dake",)], "no row written"


@budget(load="db", seconds=10)
def test_a_rename_stopped_before_its_transaction_is_finished_by_the_next_run(world, monkeypatch):
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    names = sorted(files(world, "Ore Dake"))

    def stops(*_args):
        msg = "stopped"
        raise RuntimeError(msg)

    with monkeypatch.context() as m:
        m.setattr(rename, "_store", stops)
        with pytest.raises(RuntimeError, match="stopped"):
            renamed(world, sid)
    assert files(world, "Ore Dake") == {}, "the folder was renamed before the stop"
    assert world.rows("SELECT folder FROM series") == [("library/Ore Dake",)], "the rows were not"
    done, left = renamed(world, sid)
    assert (left, [p.series["id"] for p in done]) == ([], [sid]), "the next run finishes it"
    assert sorted(files(world, "Solo Leveling")) == names
    assert world.rows("SELECT folder, comicinfo_series FROM series") == [("library/Solo Leveling", "Solo Leveling")]


@budget(load="db", seconds=10)
def test_a_busy_series_is_left_as_it_is(world):
    sid = checked(world, "Solo Leveling")
    world.rows("UPDATE published_file SET pending_error = 'failed' RETURNING id")
    done, left = renamed(world, sid)
    assert done == []
    assert "busy" in left[0], "listed with why"
    assert files(world, "Ore Dake") != {}, "nothing moved"


@budget(load="db", seconds=10)
def test_the_wait_requests_scans_until_kavita_follows_the_folder(world):
    checked(world, "Solo Leveling")
    done, _ = renamed(world)
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "export")
        assert rename.wait_for_kavita(ctx, done, 0, sleep=lambda _s: None) == done, "not followed: listed"
        assert rename.wait_for_kavita(ctx, done, 5, sleep=lambda _s: world.kavita.run_scans()) == [], "followed"
    assert [w for w in world.kavita.writes if w[0] == "scan_library"], "a library scan requested"


@budget(load="db", seconds=10)
def test_series_names_lists_the_plan_and_writes_nothing(world, tmp_path, monkeypatch, capsys):
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    before = files(world, "Ore Dake")
    assert cli.main(["--config", str(config_file(tmp_path, world.cfg)), "series", "names"]) == 0
    out = capsys.readouterr().out
    assert f"series {sid} (Ore Dake): name -> 'Solo Leveling' (anilist)" in out, out
    assert "folder -> library/Solo Leveling" in out, out
    assert files(world, "Ore Dake") == before, "nothing written"
    assert world.rows("SELECT comicinfo_series FROM series") == [("Ore Dake",)]


@budget(load="pure")
def test_a_file_with_an_element_the_schema_does_not_take_rebuilds_without_it():
    xml = comicinfo_xml(Series="Old", LocalizedSeries="Stale", Number="3", Extension="x").encode()
    got = comicinfo.read_all(rename.new_xml("f.cbz", xml, series_name.Chosen("New", "", "anilist")))
    assert got == {"Series": "New", "Number": "3"}, "Series the name, no stale LocalizedSeries, unknown element dropped"


def raising(error, real, on_kid):
    """Return a rename_series that raises error for on_kid and calls real for the others."""

    def rename_series(kid, name, localized):
        if kid == on_kid:
            raise error
        return real(kid, name, localized)

    return rename_series


@budget(load="db", seconds=10)
def test_a_kavita_refusal_lists_the_series_and_the_run_goes_on(world, monkeypatch):
    first = checked(world, "Solo Leveling")
    second = checked(world, "Second Life", name="Niban", al_id="7")
    kid, _ = kavita(world, first)
    refusal = http.HttpError(400, "http://kavita.invalid/api/Series/update", b"bad name")
    monkeypatch.setattr(world.kavita, "rename_series", raising(refusal, world.kavita.rename_series, kid))
    done, left = renamed(world)
    assert [p.series["id"] for p in done] == [second], "the next series is renamed"
    assert left == [f"series {first}: not renamed: HTTP 400 from http://kavita.invalid/api/Series/update: bad name"]
    assert files(world, "Ore Dake") != {}, "the refused series is left as it is"


@budget(load="db", seconds=10)
def test_a_kavita_refusal_of_the_localized_name_after_the_folder_moved_is_finished_by_the_next_run(world, monkeypatch):
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    real, calls = world.kavita.rename_series, []

    def second_call_fails(k, name, localized):
        calls.append(k)
        if len(calls) == 2:
            raise http.HttpError(400, "http://kavita.invalid/api/Series/update", b"")
        return real(k, name, localized)

    with monkeypatch.context() as m:
        m.setattr(world.kavita, "rename_series", second_call_fails)
        done, left = renamed(world, sid)
    assert ([p.series["id"] for p in done], len(left)) == ([sid], 1), "renamed, its localized name listed"
    assert "localized name not set" in left[0]
    assert world.rows("SELECT folder, comicinfo_series, localized_name FROM series") == [
        ("library/Solo Leveling", "Ore Dake", "Ore Dake")
    ], "the folder is stored; the old name stays the localized and the stored ComicInfo name, so it is still due"
    assert kavita(world, sid)[1][:2] == ("Solo Leveling", "Ore Dake"), "as Kavita holds it"
    done, left = renamed(world, sid)
    assert ([p.series["id"] for p in done], left) == ([sid], []), "the next run finishes"
    assert kavita(world, sid)[1][:2] == ("Solo Leveling", "Ore Dake Level Up")
    assert world.rows("SELECT localized_name FROM series") == [("Ore Dake Level Up",)]


@budget(load="db", seconds=10)
def test_recorded_sizes_follow_the_rewritten_files(world):
    sid = checked(world, "Solo Leveling")
    world.rows("UPDATE published_file SET size = 1 WHERE series_id = %s RETURNING id", sid)
    renamed(world, sid)
    for path, size in world.rows("SELECT library_path, size FROM published_file"):
        assert size == config.to_local(world.cfg, path).stat().st_size, "the size of the file on disk"


@budget(load="db", seconds=10)
@pytest.mark.parametrize("stray", ["Stray.cbz", "Stray.CBR", "Stray.tar.gz"])
def test_an_archive_in_the_folder_without_a_row_leaves_the_series(world, stray):
    sid = checked(world, "Solo Leveling")
    (world.library / "Ore Dake" / stray).write_bytes(b"")
    done, left = renamed(world, sid)
    assert done == []
    assert f"has no row for: {stray}" in left[0], "any archive Kavita reads, in any case, would keep the old name"


@budget(load="db", seconds=10)
def test_the_wait_ends_when_kavita_does_not_answer(world, monkeypatch):
    checked(world, "Solo Leveling")
    done, _ = renamed(world)

    def unreachable(_kid):
        service, msg = "kavita", "timed out"
        raise workflow.Unreachable(service, msg)

    monkeypatch.setattr(world.kavita, "series_folder", unreachable)
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "export")
        assert rename.wait_for_kavita(ctx, done, 5, sleep=lambda _s: None) == done, "listed, not raised"


@budget(load="db", seconds=10)
def test_status_lists_the_names_due_and_an_override_file_it_cannot_read(world):
    sid = checked(world, "Solo Leveling")
    other = checked(world, "Second Life", name="Niban", al_id="7")
    (rules_key,) = world.rows("SELECT rules_key FROM series WHERE id = %s", other)[0]
    overrides = config.abs_path(world.cfg["paths"]["naming_overrides"])
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / f"{rules_key}.yaml").write_bytes(b"name: [A]\n")
    lines = status.run(world.cfg)
    at = lines.index("Series names waiting for series rename:")
    section = lines[at + 1 :]
    assert f"  series {sid} (Ore Dake): 'Solo Leveling': files, Kavita name, folder" in section, section
    assert any(line.startswith(f"  series {other}: ") and "name" in line for line in section), section


@budget(load="db", seconds=20)
def test_series_rename_lists_what_it_left_and_exits_1(world, tmp_path, monkeypatch, capsys):
    world.kavita_series("Solo-Leveling")
    sid = checked(world, "Solo Leveling")
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    argv = ["--config", str(config_file(tmp_path, world.cfg)), "series", "rename", "--all", "--wait", "0"]
    assert cli.main(argv) == 1, "a series not renamed: exit 1"
    assert f"series {sid} (Ore Dake): name taken" in capsys.readouterr().err


@budget(load="db", seconds=20)
def test_series_rename_refuses_to_run_without_retention(world, tmp_path, monkeypatch, capsys):
    checked(world, "Solo Leveling")
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    cfg_file = str(config_file(tmp_path, world.cfg))
    argv = ["--config", cfg_file, "--set", "export.retention_days=0", "series", "rename", "--all"]
    assert cli.main(argv) == 2, "refused: nothing would keep the original files"
    assert "retention_days is 0" in capsys.readouterr().err


def never_sleeps(_seconds):
    msg = "slept"
    raise AssertionError(msg)


@budget(load="db", seconds=10)
def test_a_zero_wait_requests_the_scan_and_never_sleeps(world):
    checked(world, "Solo Leveling")
    done, _ = renamed(world)
    with closing(db.connect(world.cfg)) as conn:
        assert rename.wait_for_kavita(world.context(conn, "export"), done, 0, sleep=never_sleeps) == done
    assert [w for w in world.kavita.writes if w[0] == "scan_library"], "the scan is still requested"


@budget(load="db", seconds=10)
def test_a_scan_request_kavita_does_not_answer_ends_the_wait(world, monkeypatch):
    checked(world, "Solo Leveling")
    done, _ = renamed(world)

    def unreachable(_library_id):
        service, msg = "kavita", "timed out"
        raise workflow.Unreachable(service, msg)

    monkeypatch.setattr(world.kavita, "scan_library", unreachable)
    with closing(db.connect(world.cfg)) as conn:
        assert rename.wait_for_kavita(world.context(conn, "export"), done, 5, sleep=never_sleeps) == done


@budget(load="db", seconds=10)
def test_kavita_is_renamed_before_the_folder_moves_when_no_file_changes(world, monkeypatch):
    sid = checked(world, "Solo Leveling", name="Solo Leveling", folder="Old Folder")
    kid, _ = kavita(world, sid)
    world.kavita.series_rows[kid]["name"] = "Wrong Name"
    refusal = http.HttpError(400, "http://kavita.invalid/api/Series/update", b"")
    monkeypatch.setattr(world.kavita, "rename_series", raising(refusal, world.kavita.rename_series, kid))
    done, left = renamed(world, sid)
    assert (done, len(left)) == ([], 1), "listed"
    assert files(world, "Old Folder") != {}, "the folder did not move under a name Kavita lacks"
    assert world.rows("SELECT folder FROM series") == [("library/Old Folder",)]


@budget(load="db", seconds=10)
def test_an_override_file_that_cannot_be_read_is_a_line_of_the_status_list(world, monkeypatch):
    sid = checked(world, "Solo Leveling")

    def denied(_overrides, _key):
        msg = "permission denied"
        raise PermissionError(13, msg)

    monkeypatch.setattr(rename.volumes, "override_name", denied)
    lines = status.run(world.cfg)
    section = lines[lines.index("Series names waiting for series rename:") + 1 :]
    assert section == [f"  series {sid}: its override file cannot be read: [Errno 13] permission denied"], section


@budget(load="db", seconds=10)
def test_a_renamed_series_series_only_named_by_its_old_name_is_listed(world):
    sid = checked(world, "Solo Leveling")
    world.rows("UPDATE series SET original_name = NULL RETURNING id")  # an original name keeps it in scope
    done, _ = renamed(world)
    cfg = copy.deepcopy(world.cfg)
    cfg["series"]["only"] = ["Ore Dake"]
    assert rename.only_lines(cfg, done) == [
        f"series {sid}: out of scope now: series.only must name it 'Solo Leveling'"
    ], "workflow.in_scope over its names after the rename"
    cfg["series"]["only"] = ["Ore Dake", "Solo Leveling"]
    assert rename.only_lines(cfg, done) == [], "still named there"


@budget(load="db", seconds=20)
def test_series_rename_exits_1_when_kavita_has_not_followed_within_the_wait(world, tmp_path, monkeypatch, capsys):
    sid = checked(world, "Solo Leveling")
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    argv = ["--config", str(config_file(tmp_path, world.cfg)), "series", "rename", str(sid), "--wait", "0"]
    assert cli.main(argv) == 1, "Kavita has not followed within --wait 0: exit 1"
    captured = capsys.readouterr()
    assert f"series {sid} (Ore Dake): name -> 'Solo Leveling'" in captured.out, captured.out
    assert "Kavita has not followed its folder to library/Solo Leveling" in captured.err
    assert [w for w in world.kavita.writes if w[0] == "scan_library"], "the scan is requested"


@budget(load="db", seconds=20)
def test_series_rename_exits_0_once_kavita_follows(world, tmp_path, monkeypatch, capsys):
    sid = checked(world, "Solo Leveling")
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    monkeypatch.setattr(rename, "POLL_SECONDS", 0)
    request = world.kavita.scan_library

    def scan_runs_at_once(library_id):
        request(library_id)
        world.kavita.run_scans()

    monkeypatch.setattr(world.kavita, "scan_library", scan_runs_at_once)
    argv = ["--config", str(config_file(tmp_path, world.cfg)), "series", "rename", "--all"]
    assert cli.main(argv) == 0, "renamed and followed: exit 0"
    assert f"series {sid} (Ore Dake): name -> 'Solo Leveling'" in capsys.readouterr().out


@budget(load="db", seconds=10)
def test_a_dropped_share_in_an_override_read_is_1_status_line_and_ends_a_rename(world, monkeypatch):
    sid = checked(world, "Solo Leveling")

    def dropped(_overrides, _key):
        msg = "I/O error"
        raise OSError(errno.EIO, msg)

    monkeypatch.setattr(rename.volumes, "override_name", dropped)
    lines = status.run(world.cfg)
    section = lines[lines.index("Series names waiting for series rename:") + 1 :]
    assert section == ["  override files cannot be read: [Errno 5] I/O error"], "1 line, not 1 per series"
    with pytest.raises(OSError, match="I/O error"):
        renamed(world, sid)


@budget(load="db", seconds=10)
def test_a_case_only_rename_renames_the_folder(world):
    sid = checked(world, "Solo Leveling", name="solo leveling")
    (rules_key,) = world.rows("SELECT rules_key FROM series WHERE id = %s", sid)[0]
    overrides = config.abs_path(world.cfg["paths"]["naming_overrides"])
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / f"{rules_key}.yaml").write_bytes(b"name: Solo Leveling\n")  # a person's spelling: case-only
    done, left = renamed(world, sid)
    assert ([p.series["id"] for p in done], left) == ([sid], [])
    assert sorted(p.name for p in world.library.iterdir() if p.is_dir()) == ["Solo Leveling"], "renamed in place"
    assert world.rows("SELECT folder, comicinfo_series FROM series") == [("library/Solo Leveling", "Solo Leveling")]


@pytest.mark.skipif(
    sys.platform == "win32", reason="2 folders differing only in case need a case-sensitive file system"
)
@budget(load="db", seconds=10)
def test_a_folder_differing_only_in_case_is_taken(world):
    sid = checked(world, "Solo Leveling", name="solo leveling")
    (rules_key,) = world.rows("SELECT rules_key FROM series WHERE id = %s", sid)[0]
    overrides = config.abs_path(world.cfg["paths"]["naming_overrides"])
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / f"{rules_key}.yaml").write_bytes(b"name: Solo Leveling\n")
    (world.library / "Solo Leveling").mkdir()
    done, left = renamed(world, sid)
    assert done == []
    assert "the folder name is taken by library/Solo Leveling" in left[0]


@budget(load="db", seconds=10)
def test_status_tests_a_romaji_against_series_out_of_series_only(world):
    world.kavita_series("Ore-Dake Level Up")
    sid = checked(world, "Solo Leveling", romaji="Ore Dake Level Up")
    renamed(world, sid)
    cfg = copy.deepcopy(world.cfg)
    cfg["series"]["only"] = ["Solo Leveling"]
    lines = status.run(cfg)
    section = lines[lines.index("Series names waiting for series rename:") + 1 :]
    assert section == ["  none"], "the romaji another series of the library holds is left out, as series rename does"


@pytest.mark.parametrize("denials", [1, 2])
@budget(load="db", seconds=10)
def test_a_folder_rename_the_share_denies_is_tried_once_more(world, monkeypatch, denials):
    sid = checked(world, "Solo Leveling")
    real, tries, waits = Path.rename, [], []

    def denied(self, target):
        tries.append(self.name)
        if len(tries) <= denials:
            raise PermissionError(13, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(rename.time, "sleep", waits.append)
    monkeypatch.setattr(Path, "rename", denied)
    if denials == 1:
        done, left = renamed(world, sid)
        assert ([p.series["id"] for p in done], left) == ([sid], [])
        assert waits[0] == rename.FOLDER_RETRY_SECONDS, "1 pause before the 2nd try"
        assert (world.library / "Solo Leveling").is_dir(), "renamed on the 2nd try"
    else:
        with pytest.raises(PermissionError):
            renamed(world, sid)
        assert tries == ["Ore Dake", "Ore Dake"], "tried twice, then the share error ends the run"


@budget(load="db", seconds=10)
def test_a_series_with_no_romanized_name_kavita_has_not_followed_is_finished_by_the_next_run(world):
    sid = checked(world, "Solo Leveling")
    outcome = rename.Outcome()
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "export")
        rename.apply(ctx, rename.check(ctx, rename.read_db(world.cfg, conn, [sid])), outcome)
        rename.finish_names(ctx, outcome, outcome.moved)  # Kavita has not followed: nothing given
    assert world.rows("SELECT comicinfo_series, localized_name FROM series") == [("Ore Dake", "Ore Dake")]
    done, left = renamed(world)
    assert ([p.series["id"] for p in done], left) == ([sid], []), "the next run finishes it"
    assert world.rows("SELECT comicinfo_series, localized_name FROM series") == [("Solo Leveling", None)], (
        "the old name no longer the localized name: a series with no romanized name is not left with it"
    )
    assert kavita(world, sid)[1][:2] == ("Solo Leveling", "")
