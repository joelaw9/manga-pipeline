"""scripts/adoption_census.py (plan step A0): what adopting the library's record-less series would do, read only."""

import json
from contextlib import closing

from manga_pipeline import config, db, intake, workflow
from manga_pipeline.clients import http
from scripts import adoption_census

from tests.builders import add_chapter, config_file, one
from tests.timing import budget
from tests.world import World, cbz


@budget(load="db", seconds=3)
def test_the_census_counts_what_adoption_would_do_and_writes_nothing(tmp_path, monkeypatch):
    world = World(tmp_path)
    world.series_without_record("Solo Leveling", bulk=(("Vol.1 Ch.1.cbz", "Vol.1 Ch.1", "1"),))
    world.series_without_record("Held", bulk=(("Extras.cbz", "Extras", "1"),))
    world.kavita_series("Bulk Done")
    world.series_without_record("Unchecked", folder="mangas/Local/Unchecked")
    (world.library / "Held" / "Extras.cbz").write_bytes(cbz(seed="x"))

    def down(name):
        if name == "Unchecked":
            msg = "mangabaka"
            raise http.Unreachable(msg, "down")
        return []

    monkeypatch.setattr(world.sources.mangabaka, "search", down)
    world.run(intake)  # rows, folders and checks; the pass adopts nothing
    with closing(db.connect(world.cfg)) as conn:  # back to before the pass: each series without a baseline
        conn.execute("DELETE FROM published_file")
        conn.execute("DELETE FROM series_conversion")
        conn.execute("UPDATE series SET baseline_at = NULL, naming_basis = NULL, status = 'ok', status_reason = NULL")
        add_chapter(conn, one(conn, "SELECT id FROM series WHERE name = 'Solo Leveling'"), '["", null, "", "2", ""]')
    with closing(db.read_only(world.cfg)) as conn:
        counts = adoption_census.census(world.context(conn, "census"))
    assert counts["series without a record"] == 3, "the bulk series is not counted"
    assert counts["held (D7), series per reason"] == {"no number": 1, "series held": 1}
    assert counts["page formats (files)"] == {".png": 2}, "the 2 adoptable series' files"
    assert counts["files with a filename volume"] == 1
    assert counts["series whose check has not completed"] == 1
    assert counts["series with a chapter at discover"] == 1
    assert counts["Local/ series without a record"] == 1
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)], "nothing written"


@budget(load="db", seconds=3)
def test_the_census_reads_a_series_with_no_stored_folder_by_its_kavita_folder(tmp_path):
    world = World(tmp_path)
    world.series_without_record("Solo Leveling", bulk=(("Ch.1.cbz", "Ch.1", "1"),))
    overrides = config.abs_path(world.cfg["paths"]["naming_overrides"])
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / "Solo Leveling.yaml").write_text("layout: scroll\n", encoding="utf-8", newline="\n")
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:  # before the pass stored its folder (and rules key)
        conn.execute(
            "UPDATE series SET folder = NULL, rules_key = NULL, status = 'ok', status_reason = NULL, baseline_at = NULL"
        )
    with closing(db.read_only(world.cfg)) as conn:
        counts = adoption_census.census(world.context(conn, "census"))
    assert counts["held (D7), series per reason"] == {"override": 1, "series held": 1}, (
        "the override file is found by the Kavita folder's name"
    )
    assert world.rows("SELECT folder FROM series") == [(None,)], "nothing written"


@budget(load="db", seconds=3)
def test_the_census_gives_its_other_counts_and_main_prints_them_as_json(tmp_path, monkeypatch, capsys):
    world = World(tmp_path)
    world.series_without_record("Solo Leveling", bulk=(("Ch.1.cbz", "Ch.1", "1"),))
    world.series_without_record("Held", bulk=(("Extras.cbz", "Extras", "1"),))
    erring = world.series_without_record("Erring", bulk=(("Ch.1.cbz", "Ch.1", "1"),))
    gone = world.series_without_record("Gone", bulk=(("Ch.1.cbz", "Ch.1", "1"),))
    world.download(world.manga("Solo Leveling"), "Chapter 2", scanlator="Grp")
    world.run(intake)  # rows, folders and checks; the download waits at validate
    with closing(db.connect(world.cfg)) as conn:  # back to before the baseline: each series without one
        conn.execute("DELETE FROM published_file")
        conn.execute("DELETE FROM series_conversion")
        conn.execute("UPDATE series SET baseline_at = NULL, naming_basis = NULL, status = 'ok', status_reason = NULL")
        conn.execute("UPDATE series SET status = 'needs_human', status_reason = 'held' WHERE name = 'Held'")
        conn.execute("UPDATE chapter SET stage = 'validate', status = 'waiting', waiting_on = 'bulk_conversion'")
    world.kavita.series_rows.pop(gone)  # Kavita no longer lists it
    files = world.kavita.series_files

    def erring_reply(series_id):
        if series_id == erring:
            raise http.HttpError(500, "http://kavita.invalid/api/Series/volumes", b"oops")
        return files(series_id)

    monkeypatch.setattr(world.kavita, "series_files", erring_reply)
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    assert adoption_census.main(["--config", str(config_file(tmp_path, world.cfg))]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["series without a record"] == 3, "Gone is not in Kavita's listing"
    assert counts["series not read (Kavita error)"] == {"HttpError": 1}
    assert counts["held (D7), series per reason"] == {"no number": 1, "series held": 1}
    assert counts["Phase A: ok series adoptable now"] == 1, "Solo Leveling"
    assert counts["Phase A: ok series with a completed check held (D7)"] == 0, "Held is held already"
    assert counts["downloads present for those series"] == 1
    assert counts["chapters waiting on bulk_conversion"] == 1
    seconds = counts["seconds per series"]
    assert seconds["mean"] is not None, "each prepared series is timed"
    assert seconds["max"] is not None, "each prepared series is timed"


@budget(load="db", seconds=3)
def test_the_census_counts_an_ok_checked_series_d7_holds_for_phase_a(tmp_path):
    world = World(tmp_path)
    world.series_without_record("Held", bulk=(("Extras.cbz", "Extras", "1"),))
    world.run(intake)  # the pass holds it
    with closing(db.connect(world.cfg)) as conn:  # as before the pass
        conn.execute("UPDATE series SET status = 'ok', status_reason = NULL")
    with closing(db.read_only(world.cfg)) as conn:
        counts = adoption_census.census(world.context(conn, "census"))
    assert counts["Phase A: ok series with a completed check held (D7)"] == 1
    assert counts["Phase A: ok series adoptable now"] == 0
