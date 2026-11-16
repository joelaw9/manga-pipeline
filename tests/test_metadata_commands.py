"""`series set-id`, `metadata import` and `metadata audit` (SPEC "Source ids", "Metadata checks", "CLI")."""

import argparse
import json
from contextlib import closing

import pytest
import yaml

from manga_pipeline import cli, db, intake, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import audit, check, human, importer

from tests.timing import budget
from tests.world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def series_id(world, kavita_id):
    return world.rows("SELECT id FROM series WHERE kavita_series_id = %s", kavita_id)[0][0]


def run_import(world, tmp_path, state, old_config=None):
    state_file, config_file = tmp_path / "state.json", tmp_path / "old.yaml"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    config_file.write_text(yaml.safe_dump(old_config or {}), encoding="utf-8")
    with closing(db.connect(world.cfg)) as conn:
        importer.run(
            world.context(conn, "intake"), importer.read_state(state_file), importer.read_overrides(config_file)
        )


@budget(load="db")
@pytest.mark.parametrize(
    ("source", "value", "refusal"),
    [("tvdb", "1", "unknown source"), ("anilist", "abc", "is not a anilist id"), ("anilist", "5", "no series 999")],
)
def test_set_id_refuses_what_it_cannot_store(world, source, value, refusal):
    with closing(db.connect(world.cfg)) as conn, pytest.raises(workflow.Refused, match=refusal):
        human.set_id(conn, 999, source, value)


@budget(load="db")
def test_set_id_replaces_the_series_earlier_human_id(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    sid = series_id(world, kavita_id)
    with closing(db.connect(world.cfg)) as conn:
        human.set_id(conn, sid, "mangaupdates", "04gtb06")
        human.set_id(conn, sid, "mangaupdates", "15")
    assert world.rows("SELECT source, origin, source_id FROM series_source_id WHERE origin = 'human'") == [
        ("mangaupdates", "human", "15")
    ]


@budget(load="db")
def test_import_stores_ids_and_written_fields_and_keeps_human_ids(world, tmp_path):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    sid = series_id(world, kavita_id)
    with closing(db.connect(world.cfg)) as conn:
        human.set_id(conn, sid, "anilist", "9")
    k = str(kavita_id)
    state = {
        "ids": {k: {"al": 105398, "mu": None, "mb": 50}, "424242": {"al": 1}},
        "covers": {k: {"sha256": "ab" * 32}},
        "metadata": {k: {"fields": ["summary", "genres"]}},
        "status": {k: 2},
    }
    run_import(world, tmp_path, state)
    run_import(world, tmp_path, state)  # run again: the same rows
    assert world.rows("SELECT source, origin, source_id FROM series_source_id ORDER BY 1, 2") == [
        ("anilist", "human", "9"),
        ("anilist", "import", "105398"),
        ("mangabaka", "import", "50"),
    ], "a null id is skipped; a person's id is kept"
    assert world.rows("SELECT fields, status, cover_sha256 FROM series_import") == [
        (["summary", "genres"], 2, "ab" * 32)
    ]


@budget(load="db")
def test_import_takes_2_series_sharing_an_id_and_links_overrides(world, tmp_path):
    first, second = world.kavita_series("Alpha"), world.kavita_series("Beta")
    manga = world.manga("Beta Manga")
    world.run(intake)
    state = {"ids": {str(first): {"al": 7}, str(second): {"al": 7}}}
    run_import(world, tmp_path, state, {"overrides": {"Beta": manga}})
    assert world.rows("SELECT count(*) FROM series_source_id WHERE source_id = '7'") == [(2,)], "both imported"
    assert world.rows("SELECT manga_id, series_id FROM suwayomi_manga") == [(manga, series_id(world, second))]


@budget(load="db")
def test_import_refuses_a_file_that_is_not_the_state_file(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(workflow.Refused, match="not the old metadata tool's state file"):
        importer.read_state(path)


@budget(load="db")
def test_audit_lists_disagreements_missing_ids_and_unmatched_manga(world, capsys):
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.metadata_rows[kavita_id] = {"webLinks": "https://anilist.co/manga/1"}
    world.suwayomi.add_tracker(world.manga("Solo Leveling"), 2, 2)
    world.manga("Nobody's Manga")
    with closing(db.connect(world.cfg)) as conn:
        audit.run(world.context(conn, "intake"), world.sources)
    out = capsys.readouterr().out
    assert "series whose ids disagree: 1\n  Solo Leveling" in out
    assert "1 (kavita), 2 (suwayomi)" in out
    assert "series with no mangadex id: 1" in out
    assert "no series matches by name: 1\n  Nobody's Manga" in out
    assert world.rows("SELECT count(*) FROM series_source_id") == [(0,)], "read-only"


@budget(load="db")
def test_import_again_drops_ids_the_newer_state_file_no_longer_holds(world, tmp_path):
    first, second = world.kavita_series("Alpha"), world.kavita_series("Beta")
    world.run(intake)
    run_import(world, tmp_path, {"ids": {str(first): {"al": 1}, str(second): {"al": 2}}})
    run_import(world, tmp_path, {"ids": {str(first): {"al": 3}}})
    assert world.rows("SELECT source_id FROM series_source_id WHERE origin = 'import'") == [("3",)]
    assert world.rows("SELECT count(*) FROM series_import") == [(1,)], "the dropped series' import record goes too"


@budget(load="db")
def test_audit_opens_no_database(world, monkeypatch, capsys):
    cfg = {**world.cfg, "database": {**world.cfg["database"], "name": "mp_test_never_built_audit"}}
    monkeypatch.setattr(suwayomi_client, "from_config", lambda c: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda c: world.kavita)
    monkeypatch.setattr(check, "new_sources", lambda: world.sources)
    world.kavita_series("Solo Leveling")
    cli.metadata_audit(cfg, argparse.Namespace(state=None))
    assert "series whose ids disagree: 0" in capsys.readouterr().out
    assert not db.built(cfg), "audit builds and migrates no database"
