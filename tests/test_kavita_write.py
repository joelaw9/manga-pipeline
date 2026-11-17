"""Series fields to Kavita (SPEC "Metadata"; decisions D7.10, D7.11, D7.22).

Written locked by export from stored records, only what differs, never over a person's locked edit; a tag-rule change
reaches Kavita with no source request.
"""

import io
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import db, export, intake
from manga_pipeline.metadata import kavita_write

from tests.timing import budget
from tests.world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def execute(world, sql, *params):
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(sql, params)


def checked(world, **mb):
    """Add a Kavita series checked by intake, its MangaBaka record found by search with the given fields."""
    kavita_id = world.kavita_series("Solo Leveling")
    record = {"id": 50, "title": "Solo Leveling", "status": "completed", "content_rating": "suggestive"}
    record |= {"genres": ["action", "fantasy"], "tags_v2": [], "description": "Hunters <b>rise</b>."} | mb
    world.sources.mangabaka.results["Solo Leveling"] = [{"id": 50, "title": "Solo Leveling"}]
    world.sources.mangabaka.records[("series", "50")] = record
    world.run(intake)
    return kavita_id


def meta(world, kavita_id):
    return world.kavita.metadata_rows[kavita_id]


def field_writes(world):
    return [w for w in world.kavita.writes if w[0] in ("metadata", "series", "cover")]


@budget(load="db")
def test_export_writes_the_series_fields_locked_and_then_nothing_while_they_hold(world):
    kavita_id = checked(world)
    world.run(export)
    m = meta(world, kavita_id)
    assert (m["summary"], m["summaryLocked"]) == ("Hunters rise.", True), "summary from MangaBaka, HTML dropped"
    genres = sorted(g["title"] for g in m["genres"])
    assert genres == ["Action", "Completed", "Fantasy"], "MangaBaka's genres and the status rule"
    assert m["genresLocked"], "MangaBaka's genres and the status rule"
    assert (m["publicationStatus"], m["ageRating"]) == (2, 8), "completed; suggestive is Teen"
    assert "https://mangabaka.org/50" in m["webLinks"]
    before = len(field_writes(world))
    world.run(export)
    assert len(field_writes(world)) == before, "nothing changed: no Kavita write"


@budget(load="db")
def test_a_field_a_person_locked_is_kept_and_reported_once(world, caplog):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] |= {"summary": "My own words", "summaryLocked": True}
    world.run(export)
    world.run(export)
    assert meta(world, kavita_id)["summary"] == "My own words", "a hand-locked field is never overwritten"
    assert len([r for r in caplog.records if "locked by hand" in r.getMessage()]) == 1, "reported once"


@budget(load="db")
def test_a_field_the_old_tool_wrote_is_replaced(world):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] |= {"summary": "Old tool text", "summaryLocked": True}
    sid = world.rows("SELECT id FROM series WHERE kavita_series_id = %s", kavita_id)[0][0]
    execute(world, "INSERT INTO series_import (series_id, fields) VALUES (%s, '[\"summary\"]')", sid)
    world.run(export)
    assert meta(world, kavita_id)["summary"] == "Hunters rise.", "the old tool's locked value is the pipeline's"


@budget(load="db")
def test_a_tag_rule_change_rewrites_every_series_with_no_source_request(world, monkeypatch):
    tags_v2 = [{"name": "Nobility", "weight": "core"}, {"name": "Revenge", "weight": "core"}]
    kavita_id = checked(world, tags_v2=tags_v2)
    world.run(export)
    assert "nobility" in {t["title"].lower() for t in meta(world, kavita_id)["tags"]}
    calls = len(world.sources.calls)
    real = kavita_write.tags.load

    def edited():
        rules = real()
        table = {**rules.table, ("tag", "nobility"): ("remove", "")}  # as a consolidation.csv row removing it
        return rules.__class__(**{**rules.__dict__, "fingerprint": "edited", "table": table})

    monkeypatch.setattr(kavita_write.tags, "load", edited)
    world.run(export)
    assert {t["title"].lower() for t in meta(world, kavita_id)["tags"]} == {"revenge"}, "recomputed and written"
    assert len(world.sources.calls) == calls, "from stored records alone"


@budget(load="db")
def test_no_series_write_while_a_library_scan_is_awaited(world, monkeypatch):
    checked(world)
    monkeypatch.setattr(export, "scan_awaited", lambda _ctx: "2026-11-15T00:00:00+00:00")
    world.run(export)
    assert field_writes(world) == [], "Kavita holds writes while its scan runs: a later run writes"


@budget(load="db")
def test_the_cover_is_uploaded_once(world):
    kavita_id = checked(world)
    out = io.BytesIO()
    Image.new("RGB", (1600, 10)).save(out, "PNG")
    manga = world.manga("Solo Leveling")
    world.suwayomi.thumbnails[manga] = out.getvalue()
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)
    world.run(export)
    world.run(export)
    assert [w for w in world.kavita.writes if w[0] == "cover"] == [("cover", kavita_id)]
    assert world.kavita.covers[kavita_id] == out.getvalue()


@budget(load="db")
def test_a_series_never_checked_gets_no_write(world):
    world.kavita_series("Solo Leveling")
    with closing(db.connect(world.cfg)) as conn:
        assert kavita_write.due(world.context(conn, "export"), kavita_write.tags.load()) == [], "no data, no write"


@budget(load="db")
def test_a_write_that_fails_partway_keeps_the_fields_already_written_as_the_pipelines(world, monkeypatch):
    kavita_id = checked(world, tags_v2=[{"name": "Nobility", "weight": "core"}])
    real = world.kavita.update_metadata
    calls = []

    def flaky(metadata):
        calls.append(1)
        if len(calls) == 2:  # the genres update fails after the plain fields went through
            raise kavita_write.http.HttpError(500, "http://kavita.invalid/api/Series/metadata", b"Could not update")
        real(metadata)

    monkeypatch.setattr(world.kavita, "update_metadata", flaky)
    world.run(export)
    monkeypatch.undo()
    world.sources.mangabaka.records[("series", "50")]["description"] = "New words."
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)
    world.run(export)
    assert meta(world, kavita_id)["summary"] == "New words.", "the summary written before the failure is the pipeline's"


@budget(load="db")
def test_a_series_kavita_recreated_is_written_afresh(world):
    kavita_id = checked(world)
    world.run(export)
    sid = world.rows("SELECT id FROM series WHERE kavita_series_id = %s", kavita_id)[0][0]
    world.kavita.series_rows[kavita_id + 50] = {**world.kavita.series_rows[kavita_id], "id": kavita_id + 50}
    execute(world, "UPDATE series SET kavita_series_id = %s WHERE id = %s", kavita_id + 50, sid)
    world.run(export)
    assert meta(world, kavita_id + 50)["summary"] == "Hunters rise.", "the new Kavita series gets the fields"


@budget(load="db")
def test_a_cover_a_person_uploaded_is_kept(world):
    kavita_id = checked(world)
    out = io.BytesIO()
    Image.new("RGB", (1600, 10)).save(out, "PNG")
    world.suwayomi.thumbnails[world.manga("Solo Leveling")] = out.getvalue()
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)
    world.kavita.series_rows[kavita_id]["coverImage"] = f"series{kavita_id}.jpg"  # uploaded in Kavita's UI
    world.run(export)
    world.run(export)
    assert [w for w in world.kavita.writes if w[0] == "cover"] == [], "never replaced"


@budget(load="db")
def test_lists_compare_by_kavitas_name_key_and_people_are_written(world):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] = {
        "seriesId": kavita_id,
        "webLinks": "",
        "genres": [{"id": 1, "title": "action"}, {"id": 2, "title": "Fant-asy"}],
        "genresLocked": True,
    }
    sid = world.rows("SELECT id FROM series WHERE kavita_series_id = %s", kavita_id)[0][0]
    world.sources.anilist.records[("media", "9")] = {
        "id": 9,
        "staff": {"edges": [{"role": "Story & Art", "node": {"name": {"full": "Chugong"}}}]},
    }
    execute(world, "INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', '9')", sid)
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)
    world.run(export)
    m = meta(world, kavita_id)
    assert [g["title"] for g in m["genres"]][:2] == ["action", "Fant-asy"], "the same names under Kavita's key"
    assert [p["name"] for p in m["writers"]] == ["Chugong"], "added, locked"
    assert m["writerLocked"], "added, locked"
    assert [p["name"] for p in m["pencillers"]] == ["Chugong"]
    m["writers"].append({"id": 5, "name": "From A File"})  # Kavita keeps file people
    execute(world, "UPDATE series_check SET data_version = data_version + 1")
    world.run(export)
    assert [p["name"] for p in meta(world, kavita_id)["writers"]] == ["Chugong", "From A File"], "only added"


@budget(load="db")
def test_a_list_whose_prune_fails_stays_the_pipelines(world, monkeypatch, caplog):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] = {
        "seriesId": kavita_id,
        "webLinks": "",
        "genres": [{"id": 1, "title": "Horror"}],
        "genresLocked": False,
    }
    real = world.kavita.update_metadata

    def prune_fails(metadata):
        titles = {g["title"] for g in metadata.get("genres") or []}
        if "Horror" not in titles and "Action" in titles:  # the prune: Horror dropped
            raise kavita_write.http.HttpError(500, "http://kavita.invalid/api/Series/metadata", b"Could not update")
        real(metadata)

    monkeypatch.setattr(world.kavita, "update_metadata", prune_fails)
    world.run(export)
    monkeypatch.undo()
    world.run(export)
    genres = sorted(g["title"] for g in meta(world, kavita_id)["genres"])
    assert genres == ["Action", "Completed", "Fantasy"], "the next run prunes: the half-written list is the pipeline's"
    assert not [r for r in caplog.records if "locked by hand" in r.getMessage()]
