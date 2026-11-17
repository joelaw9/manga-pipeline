"""Metadata checks (SPEC "Metadata checks", "Source ids", "Series identification", "Suwayomi"): run by intake for
every library series, storing ids, records and covers, holding a series whose ids disagree.
"""

import io
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from manga_pipeline import db, intake
from manga_pipeline.clients import http
from manga_pipeline.metadata import check

from tests.timing import budget
from tests.world import World

MD = "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def png(width):
    out = io.BytesIO()
    Image.new("RGB", (width, 10)).save(out, "PNG")
    return out.getvalue()


def checks(world):
    return world.rows("SELECT series_id, data_version, urgent FROM series_check ORDER BY series_id")


def ids_of(world, series_id):
    return world.rows(
        "SELECT source, origin, source_id FROM series_source_id WHERE series_id = %s ORDER BY 1, 2", series_id
    )


def execute(world, sql, *params):
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(sql, params)


def series_id(world, kavita_id):
    return world.rows("SELECT id FROM series WHERE kavita_series_id = %s", kavita_id)[0][0]


@budget(load="db")
def test_every_library_series_gets_a_row_and_a_first_check_finding_nothing_completes(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.run(intake)
    sid = series_id(world, kavita_id)
    assert checks(world) == [(sid, 1, False)], "a check that found nothing completes: data version 0 -> 1"
    stored = world.rows("SELECT source, kind, payload IS NULL FROM fetched WHERE series_id = %s ORDER BY 1, 2", sid)
    assert all(none for _, _, none in stored), "each source's kinds stored as not found"
    assert len(stored) == 6, "each source's kinds stored as not found"
    names = [c[2] for c in world.sources.calls if c[1] == "search"]
    assert "Solo Leveling" in names, "a source with no id is searched by the series' name"
    world.run(intake)
    assert checks(world) == [(sid, 1, False)], "not due again: no second check"


@budget(load="db")
def test_ids_found_by_search_and_cross_links_are_stored_with_their_origins(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.sources.mangabaka.results["Solo Leveling"] = [{"id": 50, "title": "Solo Leveling"}]
    world.sources.mangabaka.records[("series", "50")] = {
        "id": 50,
        "title": "Solo Leveling",
        "source": {"anilist": {"id": 105398}, "manga_updates": {"id": "04gtb06"}},
    }
    world.sources.anilist.records[("media", "105398")] = {"id": 105398, "idMal": 121496}
    world.run(intake)
    sid = series_id(world, kavita_id)
    mu = str(int("04gtb06", 36))
    assert ids_of(world, sid) == [
        ("anilist", "mangabaka", "105398"),
        ("mangabaka", "search", "50"),
        ("mangaupdates", "mangabaka", mu),
        ("myanimelist", "anilist", "121496"),
    ]
    found = world.rows("SELECT source FROM fetched WHERE series_id = %s AND payload IS NOT NULL ORDER BY 1", sid)
    assert found == [("anilist",), ("mangabaka",)], "each record fetched by the id it learnt"


@budget(load="db")
def test_ids_that_disagree_hold_the_series_and_store_both(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.metadata_rows[kavita_id] = {"webLinks": "https://anilist.co/manga/1"}
    manga = world.manga("Solo Leveling")
    world.suwayomi.add_tracker(manga, 2, 2)
    world.run(intake)
    sid = series_id(world, kavita_id)
    reason = world.rows("SELECT status, status_reason FROM series WHERE id = %s", sid)[0]
    assert reason[0] == "needs_human"
    assert "anilist ids disagree: 1 (kavita), 2 (suwayomi)" in reason[1]
    assert checks(world) == [], "a held check does not complete"
    assert world.sources.calls == [], "no source is asked"


@budget(load="db")
def test_a_human_id_settles_a_disagreement(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.metadata_rows[kavita_id] = {"webLinks": "https://anilist.co/manga/1"}
    world.suwayomi.add_tracker(world.manga("Solo Leveling"), 2, 2)
    world.run(intake)
    sid = series_id(world, kavita_id)
    execute(world, "INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', '2')", sid)
    execute(world, "UPDATE series SET status = 'ok', status_reason = NULL WHERE id = %s", sid)
    world.run(intake)
    assert checks(world) == [(sid, 1, False)]
    assert ("media", "2") in {(c[1], c[2]) for c in world.sources.calls}, "AniList fetched by the person's id"


@budget(load="db")
def test_a_library_manga_with_no_download_is_linked_by_the_sweep(world):
    kavita_id = world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.manga("Unknown Title")
    world.run(intake)
    assert world.rows("SELECT manga_id, series_id FROM suwayomi_manga") == [(manga, series_id(world, kavita_id))], (
        "only the manga with exactly 1 strict hit is linked; no row for the other"
    )


@budget(load="db")
def test_a_source_id_2_series_hold_is_a_double_hit_for_a_new_manga(world):
    first, second = world.kavita_series("Alpha"), world.kavita_series("Beta")
    world.run(intake)
    for kavita_id in (first, second):
        execute(
            world, "INSERT INTO series_source_id VALUES (%s, 'anilist', 'import', '7')", series_id(world, kavita_id)
        )
    manga = world.manga("Gamma")
    world.suwayomi.add_tracker(manga, 2, 7)
    world.download(manga, "Chapter 1")
    world.run(intake)
    held = world.rows("SELECT name, status FROM series WHERE kavita_series_id IS NULL")
    assert held == [("Gamma", "needs_human")], "the manga's series is held, naming both series"


@budget(load="db")
def test_a_new_chapter_makes_its_series_check_run_beyond_the_cap(world):
    world.cfg["metadata"]["checks_per_run"] = 1
    a, b = world.kavita_series("Alpha"), world.kavita_series("Beta")
    world.run(intake)
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.download(world.manga("Beta"), "Chapter 2")
    world.run(intake)
    dates = dict(world.rows("SELECT series_id, next_check_at > '2001' FROM series_check"))
    assert dates == {series_id(world, a): True, series_id(world, b): True}, (
        "the cap's 1 check went to Alpha; Beta's was made due by its new chapter and ran too"
    )


@budget(load="db")
def test_a_source_that_cannot_be_reached_leaves_the_check_due_with_nothing_stored(world, monkeypatch):
    world.kavita_series("Solo Leveling")

    def down(_name):
        msg = "mangabaka"
        raise http.Unreachable(msg, "down")

    monkeypatch.setattr(world.sources.mangabaka, "search", down)
    world.run(intake)
    assert checks(world) == []
    assert world.rows("SELECT count(*) FROM fetched") == [(0,)]


@budget(load="db")
def test_a_kavita_series_recreated_under_a_new_id_keeps_its_row(world):
    old = world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")  # a download gives the row its folder
    world.run(intake)
    sid = series_id(world, old)
    row = world.kavita.series_rows.pop(old)
    world.kavita.series_rows[old + 50] = {**row, "id": old + 50}
    for entry in world.kavita.files.values():
        entry["series"] = old + 50
    world.run(intake)
    assert world.rows("SELECT id, kavita_series_id FROM series WHERE name = 'Solo Leveling'") == [(sid, old + 50)]


@budget(load="db")
def test_the_cover_is_a_wide_thumbnail_else_mangadex_volume_1_in_english(world):
    kavita_id = world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.suwayomi.thumbnails[manga] = png(800)
    world.kavita.metadata_rows[kavita_id] = {"webLinks": f"https://mangadex.org/title/{MD}"}
    md = world.sources.mangadex
    md.records[("manga", MD)] = {"id": MD, "attributes": {"originalLanguage": "ko"}}
    md.records[("covers", MD)] = [
        {"volume": "2", "locale": "en", "fileName": "v2.png"},
        {"volume": "1", "locale": "ko", "fileName": "v1ko.png"},
        {"volume": "1", "locale": "en", "fileName": "v1.png"},
    ]
    md.images[(MD, "v1.png")] = png(1000)
    world.run(intake)
    sid = series_id(world, kavita_id)
    url, path = world.rows("SELECT source_url, path FROM series_cover WHERE series_id = %s", sid)[0]
    assert url == f"mangadex:{MD}/v1.png", "English volume 1 beats a thin thumbnail"
    assert path.startswith("covers/series/"), "English volume 1 beats a thin thumbnail"
    world.suwayomi.thumbnails[manga] = png(1600)
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    world.run(intake)
    url = world.rows("SELECT source_url FROM series_cover WHERE series_id = %s", sid)[0][0]
    assert url == f"suwayomi:manga/{manga}/thumbnail", "a thumbnail at least 1500 px wide is the cover"


@budget(load="db")
def test_a_completed_series_queues_only_the_new_chapters_it_lacks(world):
    world.kavita_series("Solo Leveling", bulk=(("Ch.1.cbz", "Grp_Chapter 1", "1"),))
    manga = world.suwayomi.add_manga("Solo Leveling", world.source, status="COMPLETED")
    world.download(manga, "Chapter 2")
    world.suwayomi.add_chapter(manga, "Chapter 3", listed=False)  # the source has it; Suwayomi lists it on refresh
    world.suwayomi.add_chapter(manga, "Chapter 2", listed=False)  # another copy of a chapter the series has
    world.run(intake)
    queued = [world.suwayomi.chapter_rows[e["chapter"]]["name"] for e in world.suwayomi.queue]
    assert queued == ["Chapter 3"], "only the added chapter the series lacks is downloaded"
    assert json.dumps(world.suwayomi.downloader) == '"STARTED"'


@budget(load="db")
def test_a_library_series_gets_its_folder_from_the_baseline_pass(world):
    world.kavita_series("Solo Leveling")
    world.manga("Solo Leveling")
    world.run(intake)
    assert world.rows("SELECT folder, rules_key, status FROM series") == [
        ("library/Solo Leveling", "Solo Leveling", "ok")
    ], "the daily baseline pass fetches the folder of a series with no baseline (and so its rules key)"


@budget(load="db")
def test_a_check_that_fails_on_a_bad_reply_holds_the_chapter_until_it_completes(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.suwayomi.thumbnails[manga] = b"<html>challenge</html>"
    world.download(manga, "Chapter 2")
    monkeypatch.setattr(world.sources.mangaupdates, "search", lambda _name: [{"unexpected": "shape"}])
    world.run(intake)
    assert world.rows("SELECT stage, waiting_on, attempts FROM chapter") == [("validate", "metadata_check", 0)], (
        "a series never checked holds its chapter at validate, at no attempt: its name and covers need the data"
    )
    assert world.rows("SELECT value FROM meta WHERE key LIKE %s", "%intake%last_result")[0][0].startswith("ok")
    assert world.rows("SELECT count(*) FROM series_check WHERE data_version > 0") == [(0,)], "the check stays due"
    monkeypatch.undo()
    world.run(intake)
    assert world.rows("SELECT count(*) FROM series_cover") == [(0,)], "an error page is never a cover"
    assert world.rows("SELECT data_version FROM series_check") == [(1,)]
    assert world.rows("SELECT stage FROM chapter") == [("junk",)], "the completed check ends the wait in that run"


@budget(load="db")
def test_kavita_unreachable_skips_the_metadata_step_not_validation(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 2")
    world.run(intake)
    world.download(manga, "Chapter 3")

    def down(_library_id):
        msg = "kavita"
        raise http.Unreachable(msg, "down")

    monkeypatch.setattr(world.kavita, "series_in_library", down)
    world.run(intake)
    assert world.rows("SELECT stage FROM chapter ORDER BY id") == [("junk",), ("junk",)]


@budget(load="db")
def test_a_new_chapter_key_2_manga_list_is_queued_once(world):
    world.kavita_series("Solo Leveling")
    first = world.suwayomi.add_manga("Solo Leveling", world.source, status="COMPLETED")
    other = world.suwayomi.add_source("Other")
    second = world.suwayomi.add_manga("Solo Leveling", other, status="COMPLETED")
    world.download(first, "Chapter 1")
    world.download(second, "Chapter 1")
    world.suwayomi.add_chapter(first, "Chapter 9", listed=False)
    world.suwayomi.add_chapter(second, "Chapter 9", listed=False)
    world.run(intake)
    assert len(world.suwayomi.queue) == 1, "1 copy of the new chapter, from the first manga"


@budget(load="db")
def test_a_recreated_series_keeps_its_row(world):
    old = world.kavita_series("Solo Leveling")
    world.run(intake)
    sid = series_id(world, old)
    row = world.kavita.series_rows.pop(old)
    world.kavita.series_rows[old + 50] = {**row, "id": old + 50}
    for file in world.kavita.files.values():  # Kavita recreates a series with its files
        if file["series"] == old:
            file["series"] = old + 50
    world.run(intake)
    assert world.rows("SELECT id, kavita_series_id FROM series") == [(sid, old + 50)], "no ghost row is left"


@budget(load="db")
def test_the_sweep_never_links_a_series_out_of_scope(tmp_path):
    world = World(tmp_path, series={"only": ["Alpha"]})
    world.kavita_series("Alpha")
    world.kavita_series("Beta")
    world.manga("Beta")
    world.run(intake)
    assert world.rows("SELECT count(*) FROM suwayomi_manga") == [(0,)]


@budget()
@pytest.mark.parametrize(
    ("last_days", "complete", "days"),
    [(10, False, 7), (200, False, 30), (200, True, 30), (400, True, 365), (400, False, 30), (None, True, 365)],
)
def test_the_next_check_follows_the_last_published_update(tmp_path, last_days, complete, days):
    cfg = World(tmp_path).cfg
    now = datetime(2026, 11, 1, tzinfo=UTC)
    last = None if last_days is None else now - timedelta(days=last_days)
    got = check.next_check(cfg, now.isoformat(), last, complete)
    assert got == (now + timedelta(days=days)).isoformat(timespec="seconds")


@budget(load="db")
def test_a_series_deleted_from_kavita_never_takes_a_capped_check(world):
    world.cfg["metadata"]["checks_per_run"] = 1
    gone, kept = world.kavita_series("Alpha"), world.kavita_series("Beta")
    world.run(intake)
    execute(world, "UPDATE series_check SET next_check_at = '2000-01-01T00:00:00+00:00'")
    del world.kavita.series_rows[gone]
    world.run(intake)
    world.run(intake)
    due = dict(world.rows("SELECT series_id, next_check_at > '2001' FROM series_check"))
    assert due == {series_id(world, gone): True, series_id(world, kept): True}, (
        "the deleted series is pushed back, so the cap's 1 check reaches Beta"
    )


@budget(load="db")
def test_an_id_that_leads_to_a_novel_is_not_used(world):
    kavita_id = world.kavita_series("Solo Leveling")
    world.kavita.metadata_rows[kavita_id] = {"webLinks": "https://anilist.co/manga/5"}
    world.sources.anilist.records[("media", "5")] = {"id": 5, "format": "NOVEL", "idMal": 9}
    world.run(intake)
    sid = series_id(world, kavita_id)
    stored = world.rows("SELECT payload IS NULL FROM fetched WHERE series_id = %s AND source = 'anilist'", sid)
    assert stored == [(True,)], "the light novel's record is not the series' record"
    assert ("myanimelist", "anilist", "9") not in ids_of(world, sid), "nor are its cross-links"
