"""Series fields to Kavita (SPEC "Metadata"; decisions D7.10, D7.11, D7.22).

Written locked by export from stored records, only what differs, never over a person's locked edit; a tag-rule change
reaches Kavita with no source request.
"""

import io
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from manga_pipeline import config, db, export, intake
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.metadata import human, kavita_write

from tests.timing import budget
from tests.world import World

MD = "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def execute(world, sql, *params):
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(sql, params)


def checked(world, *, language=None, name="Solo Leveling", mb_id=50, **mb):
    """Add a Kavita series checked by intake, its MangaBaka record (mb_id) found by search with the given fields.

    language: also a MangaDex record (by the series' Kavita link) of that original language.
    """
    kavita_id = world.kavita_series(name)
    if language is not None:
        world.kavita.metadata_rows[kavita_id] = {"seriesId": kavita_id, "webLinks": f"https://mangadex.org/title/{MD}"}
        world.sources.mangadex.records[("manga", MD)] = {"id": MD, "attributes": {"originalLanguage": language}}
    record = {"id": mb_id, "title": name, "status": "completed", "content_rating": "suggestive"}
    record |= {"genres": ["action", "fantasy"], "tags_v2": [], "description": "Hunters <b>rise</b>."} | mb
    world.sources.mangabaka.results[name] = [{"id": mb_id, "title": name}]
    world.sources.mangabaka.records[("series", str(mb_id))] = record
    world.run(intake)
    return kavita_id


def series_id(world, kavita_id):
    return world.rows("SELECT id FROM series WHERE kavita_series_id = %s", kavita_id)[0][0]


def due(world):
    """Return the Kavita ids of the series export would write now."""
    with closing(db.connect(world.cfg)) as conn:
        ids = kavita_write.due(world.context(conn, "export"), kavita_write.tags.load())
    return [world.rows("SELECT kavita_series_id FROM series WHERE id = %s", i)[0][0] for i in ids]


def with_covers(world, *names):
    """Give each named series' Suwayomi manga a cover image of its own, and check every series again."""
    for n, name in enumerate(names):
        out = io.BytesIO()
        Image.new("RGB", (1600, 10 + n)).save(out, "PNG")
        world.suwayomi.thumbnails[world.manga(name)] = out.getvalue()
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)


def cover_path(world, kavita_id):
    path = world.rows("SELECT path FROM series_cover WHERE series_id = %s", series_id(world, kavita_id))[0][0]
    return config.to_local(world.cfg, path)


def meta(world, kavita_id):
    return world.kavita.metadata_rows[kavita_id]


def field_writes(world):
    return [w for w in world.kavita.writes if w[0] in ("metadata", "series", "cover")]


@budget(load="db")
def test_export_writes_the_series_fields_locked_and_then_nothing_while_they_hold(world):
    kavita_id = checked(world, language="ko")
    world.run(export)
    m = meta(world, kavita_id)
    assert (m["summary"], m["publicationStatus"], m["ageRating"], m["language"]) == ("Hunters rise.", 2, 8, "ko"), (
        "summary from MangaBaka, HTML dropped; completed; suggestive is Teen; MangaDex's original language"
    )
    locks = (m["summaryLocked"], m["publicationStatusLocked"], m["ageRatingLocked"], m["languageLocked"])
    assert locks == (True, True, True, True), "each scalar written locks its own field"
    genres = sorted(g["title"] for g in m["genres"])
    assert genres == ["Action", "Completed", "Fantasy"], "MangaBaka's genres and the status rule"
    assert m["genresLocked"], "MangaBaka's genres and the status rule"
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


@budget(load="db")
def test_a_check_committed_between_the_reads_leaves_the_series_due(world, monkeypatch):
    kavita_id = checked(world)
    real = kavita_write.merge.stored

    def then_a_check(conn, sid, rules):
        fields = real(conn, sid, rules)
        execute(world, "UPDATE series_check SET data_version = data_version + 1")  # another part's check commits
        return fields

    monkeypatch.setattr(kavita_write.merge, "stored", then_a_check)
    world.run(export)
    assert due(world) == [kavita_id], "fields read before the check are recorded under the version before it: due"


@budget(load="db")
def test_a_status_lock_on_a_series_the_old_tool_wrote_is_replaced(world):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] |= {"publicationStatus": 0, "publicationStatusLocked": True}
    sid = series_id(world, kavita_id)
    execute(world, "INSERT INTO series_import (series_id, fields) VALUES (%s, '[\"summary\"]')", sid)
    world.run(export)
    assert meta(world, kavita_id)["publicationStatus"] == 2, (
        "a locked status on a series the old tool wrote fields of is the old tool's: MangaBaka's Completed replaces it"
    )


@budget(load="db")
def test_a_status_lock_on_a_series_the_old_tool_never_wrote_is_kept_and_reported_once(world, caplog):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] |= {"publicationStatus": 0, "publicationStatusLocked": True}
    world.run(export)
    world.run(export)
    assert meta(world, kavita_id)["publicationStatus"] == 0, "no import row: the locked status is a person's"
    reports = [r for r in caplog.records if "status locked by hand" in r.getMessage()]
    assert len(reports) == 1, "a person's locked status is reported once"


@budget(load="db")
def test_a_writers_list_locked_by_hand_gains_the_source_name_and_keeps_the_persons(world):
    kavita_id = checked(world)
    world.kavita.metadata_rows[kavita_id] = {
        "seriesId": kavita_id,
        "webLinks": "",
        "writers": [{"id": 5, "name": "Their Pick"}],
        "writerLocked": True,
    }
    world.sources.anilist.records[("media", "9")] = {
        "id": 9,
        "staff": {"edges": [{"role": "Story", "node": {"name": {"full": "Chugong"}}}]},
    }
    execute(world, "INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', '9')", series_id(world, kavita_id))
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)
    world.run(export)
    m = meta(world, kavita_id)
    assert [p["name"] for p in m["writers"]] == ["Their Pick", "Chugong"], "people are only added, locked or not"
    assert m["writerLocked"], "the list stays locked"


@budget(load="db")
def test_provider_ids_reach_kavita_through_the_series_update(world):
    kavita_id = checked(world)
    world.run(export)
    updates = [w for w in world.kavita.writes if w[0] == "series"]
    assert updates == [("series", kavita_id, {"mangaBakaId": 50})], "the MangaBaka id is set on the Kavita series"


@budget(load="db")
def test_a_series_kavita_no_longer_holds_is_not_tried_again_until_its_data_changes(world):
    kavita_id = checked(world)
    del world.kavita.series_rows[kavita_id]
    world.run(export)
    assert due(world) == [], "not written, and not due again on the next export"
    execute(world, "UPDATE series_check SET data_version = data_version + 1")
    assert due(world) == [kavita_id], "due again once its data version rises"


@budget(load="db")
def test_a_cover_file_that_cannot_be_read_leaves_the_next_series_written(world, monkeypatch, caplog):
    first = checked(world)
    second = checked(world, name="Tower Of God", mb_id=51)
    with_covers(world, "Solo Leveling", "Tower Of God")
    unreadable = cover_path(world, first)
    real = Path.read_bytes

    def read_bytes(path):
        if path == unreadable:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    world.run(export)
    assert [w for w in world.kavita.writes if w[0] == "cover"] == [("cover", second)], "the next series' cover goes up"
    assert due(world) == [first], "the series whose cover could not be read stays due"
    assert [r for r in caplog.records if "fields not written" in r.getMessage()], "the failure is logged"


@budget(load="db")
def test_a_cover_kavita_does_not_serve_is_recorded_first_and_the_series_stays_due(world, monkeypatch):
    kavita_id = checked(world)
    with_covers(world, "Solo Leveling")

    def not_served(kid, _image):
        msg = f"Kavita accepted the cover of series {kid} but does not serve it"
        raise kavita_client.CoverError(msg)

    monkeypatch.setattr(world.kavita, "upload_cover", not_served)
    world.run(export)
    sid = series_id(world, kavita_id)
    recorded = world.rows("SELECT cover_sha256 FROM series_written WHERE series_id = %s", sid)
    current = world.rows("SELECT sha256 FROM series_cover WHERE series_id = %s", sid)
    assert recorded == current, "the cover is recorded before its upload: a failed check never makes it a person's"
    assert due(world) == [kavita_id], "the series stays due"


@budget(load="db")
def test_a_series_reply_missing_a_field_leaves_that_series_unwritten_and_the_next_written(world, monkeypatch, caplog):
    first = checked(world)
    second = checked(world, name="Tower Of God", mb_id=51)
    real = world.kavita.update_series

    def missing_field(series, **changes):
        if series["id"] == first:
            msg = f"Kavita's reply for series {first} lacks cbrId"
            raise kavita_client.ReplyError(msg)
        real(series, **changes)

    monkeypatch.setattr(world.kavita, "update_series", missing_field)
    world.run(export)
    assert due(world) == [first], "the series Kavita's reply fails stays due"
    assert ("series", second, {"mangaBakaId": 51}) in world.kavita.writes, "the next series is written"
    assert [r for r in caplog.records if "fields not written" in r.getMessage()], "the failure is logged"


@budget(load="db")
def test_a_lock_only_import_row_takes_over_the_locked_lists_and_status(world):
    kavita_id = checked(world, tags_v2=[{"name": "Nobility", "weight": "core"}])
    world.kavita.metadata_rows[kavita_id] |= {
        "genres": [{"id": 1, "title": "Old Genre"}],
        "genresLocked": True,
        "tags": [{"id": 2, "title": "Old Tag"}],
        "tagsLocked": True,
        "publicationStatus": 0,
        "publicationStatusLocked": True,
    }
    execute(
        world,
        'INSERT INTO series_import (series_id, fields) VALUES (%s, \'["genres", "tags"]\')',
        series_id(world, kavita_id),
    )
    world.run(export)
    m = meta(world, kavita_id)
    assert sorted(g["title"] for g in m["genres"]) == ["Action", "Completed", "Fantasy"], (
        "a list `metadata import` recorded from Kavita's lock is the old tool's: replaced, pruned"
    )
    assert [t["title"] for t in m["tags"]] == ["Nobility"], (
        "a list `metadata import` recorded from Kavita's lock is the old tool's: replaced, pruned"
    )
    assert m["publicationStatus"] == 2, "a lock-only import row makes the locked status the old tool's too"


def anilist_in_kavita(world, kavita_id, al_id):
    """Give the Kavita series an AniList id and a link to its page."""
    world.kavita.series_rows[kavita_id]["aniListId"] = al_id
    world.kavita.metadata_rows[kavita_id] = {"seriesId": kavita_id, "webLinks": f"https://anilist.co/manga/{al_id}"}


def anilist_links(world, kavita_id):
    return [u for u in meta(world, kavita_id)["webLinks"].split(",") if "anilist.co" in u]


@budget(load="db")
def test_a_persons_anilist_id_replaces_the_wrong_id_and_link_in_kavita(world, caplog):
    kavita_id = world.kavita_series("Solo Leveling")
    anilist_in_kavita(world, kavita_id, 1)
    world.sources.anilist.records[("media", "1")] = {"id": 1, "format": "MANGA"}
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:
        human.set_id(conn, series_id(world, kavita_id), "anilist", "2")
    world.sources.anilist.records[("media", "2")] = {"id": 2, "format": "MANGA"}
    world.run(intake)
    with caplog.at_level("INFO"):
        world.run(export)
    assert world.kavita.series_rows[kavita_id]["aniListId"] == 2, "the person's id"
    assert anilist_links(world, kavita_id) == ["https://anilist.co/manga/2"], "the wrong link removed, the right added"
    assert "web link https://anilist.co/manga/1 removed" in caplog.text, "each removal logged"


@budget(load="db")
def test_an_anilist_id_whose_record_is_a_novels_is_cleared_to_0_with_its_links(world):
    kavita_id = world.kavita_series("Solo Leveling")
    anilist_in_kavita(world, kavita_id, 1)
    world.sources.anilist.records[("media", "1")] = {"id": 1, "format": "NOVEL"}
    world.run(intake)
    world.run(export)
    assert world.kavita.series_rows[kavita_id]["aniListId"] == 0, "0 clears Kavita's AniList id"
    assert anilist_links(world, kavita_id) == [], "no AniList page is the series'"
    before = len(field_writes(world))
    world.run(export)
    assert len(field_writes(world)) == before, "nothing written again"


@budget(load="db")
def test_no_0_is_written_over_an_anilist_id_kavita_does_not_hold(world):
    world.kavita_series("Solo Leveling")
    world.suwayomi.add_tracker(world.manga("Solo Leveling"), 2, 1)
    world.sources.anilist.records[("media", "1")] = {"id": 1, "format": "NOVEL"}
    world.run(intake)
    known = world.rows("SELECT source, origin, source_id FROM series_source_id WHERE source = 'anilist'")
    assert known == [("anilist", "suwayomi", "1")], "the novel's id is the series' AniList id"
    world.run(export)
    assert not [w for w in world.kavita.writes if w[0] == "series" and "aniListId" in w[2]], "Kavita holds none"


@budget(load="db")
def test_a_found_anilist_record_writes_its_id_and_keeps_other_sources_links(world):
    kavita_id = world.kavita_series("Solo Leveling")
    anilist_in_kavita(world, kavita_id, 1)
    world.kavita.metadata_rows[kavita_id]["webLinks"] += ",https://example.com/solo"
    world.sources.anilist.records[("media", "1")] = {"id": 1, "format": "MANGA"}
    world.run(intake)
    world.run(export)
    assert meta(world, kavita_id)["webLinks"] == "https://anilist.co/manga/1,https://example.com/solo", (
        "nothing removed"
    )
    assert not [w for w in world.kavita.writes if w[0] == "series" and "aniListId" in w[2]], "Kavita holds it already"


@budget(load="db")
def test_a_mangaupdates_link_to_another_id_than_the_persons_is_removed(world):
    kavita_id = checked(world)
    wrong, right = str(int("04gtb06", 36)), str(int("04gtb07", 36))
    world.kavita.metadata_rows[kavita_id]["webLinks"] = "https://www.mangaupdates.com/series/04gtb06"
    with closing(db.connect(world.cfg)) as conn:
        execute(
            world,
            "DELETE FROM series_source_id WHERE series_id = %s AND source = 'mangaupdates'",
            series_id(world, kavita_id),
        )
        human.set_id(conn, series_id(world, kavita_id), "mangaupdates", right)
    world.sources.mangaupdates.records[("series", right)] = {"title": "Solo Leveling", "type": "Manhwa"}
    world.run(intake)
    world.run(export)
    links = meta(world, kavita_id)["webLinks"]
    assert "mangaupdates.com/series/4gtb07" in links, "the person's page added"
    assert "04gtb06" not in links, f"the link to {wrong} removed"


@budget(load="db")
def test_a_written_series_gaining_a_wrong_link_gets_only_the_removal(world):
    kavita_id = checked(world)
    world.run(export)
    world.kavita.metadata_rows[kavita_id]["webLinks"] += ",https://mangabaka.org/99"
    execute(world, "UPDATE series_check SET data_version = data_version + 1")  # due again
    before = len(field_writes(world))
    world.run(export)
    assert field_writes(world)[before:] == [("metadata", kavita_id)], "1 metadata write, nothing else"
    assert "mangabaka.org/99" not in meta(world, kavita_id)["webLinks"], "the link to another MangaBaka id removed"
