"""`series set-id`, `metadata import` and `metadata audit` (SPEC "Source ids", "Metadata checks", "CLI")."""

import argparse
import json
from contextlib import closing

import pytest
import yaml

from manga_pipeline import cli, config, db, intake, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import audit, check, human, importer, sources

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


def mangadex_record(md_id, mu_link):
    return {"id": md_id, "attributes": {"links": {"mu": mu_link}}}


@budget(load="db")
def test_set_id_makes_the_check_due_and_it_finds_again_every_id_a_search_found(world):
    wrong_md, right_md = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    kavita_id = world.kavita_series("Solo Leveling")
    mb, md = world.sources.mangabaka, world.sources.mangadex
    mb.results["Solo Leveling"] = [{"id": 50, "title": "Solo Leveling"}]
    mb.records[("series", "50")] = {"id": 50, "source": {"anilist": {"id": 111}, "manga_updates": {"id": "aaa1"}}}
    right = {"id": 60, "source": {"anilist": {"id": 222}, "manga_updates": {"id": "bbb2"}}}
    mb.records[("anilist", "222")] = mb.records[("series", "60")] = right
    md.results["Solo Leveling"] = [mangadex_record(wrong_md, "aaa1"), mangadex_record(right_md, "bbb2")]
    world.run(intake)
    sid = series_id(world, kavita_id)
    with closing(db.connect(world.cfg)) as conn:
        human.set_id(conn, sid, "anilist", "222")
        assert sid in check.due(world.context(conn, "intake")), "set-id makes the series' check due at once"
    world.run(intake)
    found = world.rows(
        "SELECT source, origin, source_id FROM series_source_id WHERE source IN ('mangabaka', 'mangadex',"
        " 'mangaupdates') ORDER BY 1"
    )
    assert found == [
        ("mangabaka", "search", "60"),
        ("mangadex", "search", right_md),
        ("mangaupdates", "mangabaka", str(int("bbb2", 36))),
    ], "every search id is found again from the person's AniList id, MangaDex's through the new MangaUpdates link"


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
def test_audit_lists_disagreements_missing_ids_and_unmatched_manga(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.metadata_rows[kavita_id] = {"webLinks": "https://anilist.co/manga/1"}
    world.suwayomi.add_tracker(world.manga("Solo Leveling"), 2, 2)
    world.manga("Nobody's Manga")
    out = "\n".join(audit.run(world.cfg, world.kavita, world.suwayomi, world.sources))
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
def test_audit_opens_no_database(world: World, monkeypatch, capsys):
    cfg: config.Config = {**world.cfg, "database": {**world.cfg["database"], "name": "mp_test_never_built_audit"}}
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _c: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _c: world.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: world.sources)
    world.kavita_series("Solo Leveling")
    cli.metadata_audit(cfg, argparse.Namespace(state=None))
    assert "series whose ids disagree: 0" in capsys.readouterr().out
    assert not db.built(cfg), "audit builds and migrates no database"


def lock(world, kavita_id, *, genres=False, tags=False):
    world.kavita.metadata_rows[kavita_id] = {
        "seriesId": kavita_id,
        "webLinks": "",
        "genresLocked": genres,
        "tagsLocked": tags,
    }


@budget(load="db")
def test_import_records_locked_lists_as_old_tools(world, tmp_path):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    lock(world, kavita_id, genres=True, tags=True)
    k = str(kavita_id)
    run_import(world, tmp_path, {"ids": {k: {"al": 1}}, "metadata": {k: {"fields": ["summary", "genres"]}}})
    assert world.rows("SELECT fields FROM series_import") == [(["summary", "genres", "tags"],)], (
        "a locked list the state file does not name is the old tool's too"
    )


@budget(load="db")
def test_import_covers_series_missing_from_state(world, tmp_path):
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita_series("Unlocked")
    world.run(intake)
    lock(world, kavita_id, genres=True, tags=True)
    run_import(world, tmp_path, {"ids": {}})
    assert world.rows("SELECT series_id, fields, status, cover_sha256 FROM series_import") == [
        (series_id(world, kavita_id), ["genres", "tags"], None, None)
    ], "a locked series the state file lacks gets a row of its locked lists; an unlocked one none"


@budget(load="db")
def test_import_without_locks_unchanged(world, tmp_path):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    k = str(kavita_id)
    run_import(world, tmp_path, {"ids": {k: {"al": 1}}, "metadata": {k: {"fields": ["summary"]}}})
    assert world.rows("SELECT fields FROM series_import") == [(["summary"],)], "no lock: the state file's fields"


@budget(load="db")
def test_import_skips_series_kavita_no_longer_lists(world, tmp_path, monkeypatch):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    del world.kavita.series_rows[kavita_id]  # Kavita deleted it: its metadata would answer 404
    asked = []
    monkeypatch.setattr(world.kavita, "metadata", lambda sid: asked.append(sid) or {})
    run_import(world, tmp_path, {"ids": {}})
    assert asked == [], "no lock is read for a series the library listing lacks"


@budget(load="db")
def test_import_lock_only_row_replaces_stale_row(world, tmp_path):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    k = str(kavita_id)
    run_import(world, tmp_path, {"ids": {k: {"al": 1}}, "status": {k: 2}, "covers": {k: {"sha256": "ab" * 32}}})
    lock(world, kavita_id, tags=True)
    run_import(world, tmp_path, {"ids": {}})
    assert world.rows("SELECT fields, status, cover_sha256 FROM series_import") == [(["tags"], None, None)], (
        "a series the state file no longer names keeps only its locked lists"
    )


@budget(load="db")
def test_reimport_after_an_export_records_no_locks(world, tmp_path):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    lock(world, kavita_id, tags=True)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(
            "INSERT INTO series_written (series_id, kavita_series_id, inputs, written, written_at)"
            " VALUES (%s, %s, 'x', '{}', 'now')",
            (series_id(world, kavita_id), kavita_id),
        )
    with pytest.raises(workflow.Refused, match="before the first export"):
        run_import(world, tmp_path, {"ids": {}})
    assert world.rows("SELECT count(*) FROM series_import") == [(0,)], "nothing imported after an export"


@budget(load="db")
def test_import_refuses_when_an_export_writes_during_its_reads(world, tmp_path, monkeypatch):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    lock(world, kavita_id, tags=True)
    read = world.kavita.metadata

    def metadata_while_export_writes(sid):
        with closing(db.connect(world.cfg)) as conn:  # export, under its own lock, records a write meanwhile
            conn.execute(
                "INSERT INTO series_written (series_id, kavita_series_id, inputs, written, written_at)"
                " VALUES (%s, %s, 'x', '{}', 'now') ON CONFLICT DO NOTHING",
                (series_id(world, kavita_id), kavita_id),
            )
        return read(sid)

    monkeypatch.setattr(world.kavita, "metadata", metadata_while_export_writes)
    with pytest.raises(workflow.Refused, match="before the first export"):
        run_import(world, tmp_path, {"ids": {str(kavita_id): {"al": 1}}})
    assert world.rows("SELECT count(*) FROM series_import") == [(0,)], "nothing imported: the check is made again"
    assert world.rows("SELECT count(*) FROM series_source_id WHERE origin = 'import'") == [(0,)], "nothing imported"
