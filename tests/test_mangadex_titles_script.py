"""scripts/mangadex_titles.py (plan phase-10-r20-mangadex-titles, step 3): every series' MangaDex chapter list, once."""

from contextlib import closing

from manga_pipeline import db, intake
from manga_pipeline.clients import http
from scripts import mangadex_titles

from tests.builders import config_file, running
from tests.timing import budget
from tests.world import World

MD = "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"


def checked_on_mangadex(tmp_path):
    """Return a world whose 1 series' check stored MangaDex record MD, with its chapter list then removed."""
    world = World(tmp_path)
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.metadata_rows[kavita_id] = {"webLinks": f"https://mangadex.org/title/{MD}"}
    world.sources.mangadex.records[("manga", MD)] = {"id": MD, "attributes": {}}
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("DELETE FROM fetched WHERE source = 'mangadex' AND kind = 'chapters'")
    world.sources.mangadex.records[("chapters", MD)] = [["1", "Start"]]
    return world


def stored(world):
    return world.rows("SELECT payload FROM fetched WHERE source = 'mangadex' AND kind = 'chapters'")


@budget(load="db", seconds=5)
def test_it_stores_each_series_list_by_its_stored_mangadex_id(tmp_path, capsys):
    world = checked_on_mangadex(tmp_path)
    versions = world.rows("SELECT data_version FROM series_check")
    path = config_file(tmp_path, world.cfg)
    assert mangadex_titles.main(["--config", str(path)], world.sources.mangadex) == 0
    assert stored(world) == [([["1", "Start"]],)]
    assert world.rows("SELECT data_version FROM series_check") == versions, "nothing else is written"
    assert "series read 1, stored 1, failed 0" in capsys.readouterr().out


@budget(load="db", seconds=5)
def test_a_failed_fetch_is_named_and_exits_1(tmp_path, capsys, monkeypatch):
    world = checked_on_mangadex(tmp_path)

    def down(_md_id):
        msg = "mangadex"
        raise http.Unreachable(msg, "down")

    monkeypatch.setattr(world.sources.mangadex, "chapter_list", down)
    assert mangadex_titles.main(["--config", str(config_file(tmp_path, world.cfg))], world.sources.mangadex) == 1
    assert stored(world) == []
    assert "stopped at series 1, 1 not stored" in capsys.readouterr().out, "an unreachable MangaDex stops the run"


@budget(load="db", seconds=5)
def test_it_refuses_while_a_part_runs(tmp_path):
    world = checked_on_mangadex(tmp_path)
    with running(world.cfg, "intake"):
        assert mangadex_titles.main(["--config", str(config_file(tmp_path, world.cfg))], world.sources.mangadex) == 3
    assert stored(world) == []


@budget(load="db", seconds=5)
def test_a_listed_series_it_cannot_store_is_refused(tmp_path):
    world = checked_on_mangadex(tmp_path)
    path = str(config_file(tmp_path, world.cfg))
    assert mangadex_titles.main(["--series", "1", "999", "--config", path], world.sources.mangadex) == 2
    assert stored(world) == []
