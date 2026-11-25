"""scripts/id_replay.py (phase 10.19 plan, step 3): what the new id rules would change, read only."""

import json
from contextlib import closing

from manga_pipeline import config, db, intake, workflow
from scripts import id_replay

from tests.builders import config_file
from tests.timing import budget
from tests.world import World


@budget(load="db", seconds=3)
def test_the_replay_lists_new_ids_and_link_changes_and_writes_nothing(tmp_path):
    world = World(tmp_path)
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.series_rows[kavita_id]["aniListId"] = 1
    world.kavita.metadata_rows[kavita_id] = {"seriesId": kavita_id, "webLinks": "https://anilist.co/manga/1"}
    world.sources.anilist.records[("media", "1")] = {"id": 1, "format": "NOVEL"}
    manga = world.manga("Solo Leveling")
    world.run(intake)  # today's code: MangaBaka not found, AniList 1 a novel's
    world.suwayomi.manga_rows[manga]["description"] = "https://mangadex.org/title/2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"
    world.sources.mangadex.records[("manga", "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c")] = {
        "id": "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"
    }
    tables = "SELECT (SELECT count(*) FROM series_source_id), (SELECT count(*) FROM fetched)"
    before = world.rows(tables)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        report = id_replay.replay(world.context(conn, "replay"))
    assert world.rows(tables) == before, "nothing written"
    (entry,) = report["series"]
    assert entry["found"] == [
        {"source": "mangadex", "id": "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c", "route": "description"}
    ]
    assert report["links"] == [
        {
            "series_id": entry["series_id"],
            "name": "Solo Leveling",
            "removed": ["https://anilist.co/manga/1"],
            "anilist_id_0": True,
        }
    ], "the novel's AniList id is cleared and its link removed"


@budget(load="db", seconds=3)
def test_the_replay_writes_its_json_under_scratch(tmp_path, capsys, monkeypatch):
    world = World(tmp_path)
    world.kavita_series("Solo Leveling")
    world.run(intake)
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    assert id_replay.main(["--config", str(config_file(tmp_path, world.cfg))]) == 0
    (written,) = (config.abs_path(world.cfg["paths"]["scratch"]) / "id-replay").glob("*.json")
    assert json.loads(written.read_text(encoding="utf-8"))["summary"]["(a) series replayed"] == 1
    assert "written:" in capsys.readouterr().out
