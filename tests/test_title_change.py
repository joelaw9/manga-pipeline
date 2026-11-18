"""A series title changed at the sources is reported, never applied (SPEC "Retroactive updates", "Needs human")."""

import json
import logging
from contextlib import closing

import pytest

from manga_pipeline import db, export, intake
from manga_pipeline.metadata import check

from tests.builders import anilist_media, one
from tests.timing import budget
from tests.world import World

AL = "105398"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def titled(english, al_id=AL):
    return anilist_media(id=int(al_id), title={"romaji": None, "english": english, "native": None})


def checked_with(world, english, al_id=AL):
    """Run a due check of the 1 series with AniList's record of al_id holding english; return the series id."""
    world.sources.anilist.records[("media", al_id)] = titled(english, al_id)
    (sid,) = world.rows("SELECT id FROM series")[0]
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM series_source_id WHERE series_id = %s AND source = 'anilist'", (sid,))
        conn.execute("INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', %s)", (sid, al_id))
        check.make_urgent(conn, sid)
    world.run(intake)
    return sid


def reports(world):
    return world.rows("SELECT text FROM notification WHERE kind = 'title'")


@budget(load="db", seconds=4)
def test_a_changed_anilist_title_is_reported_once_and_changes_nothing(world, caplog):
    world.kavita_series("Solo Leveling")
    world.run(intake)
    checked_with(world, "Solo Leveling @COMIC")
    with caplog.at_level(logging.WARNING, logger="manga_pipeline"):
        sid = checked_with(world, "Only I Level Up")
    want = f'series {sid} (Solo Leveling): AniList title changed: "Solo Leveling" -> "Only I Level Up"'
    assert reports(world) == [(want,)], "SPEC 'Retroactive updates': a changed title is reported"
    assert caplog.text.count("AniList title changed") == 1, "1 log line"
    assert world.rows("SELECT name FROM series") == [("Solo Leveling",)], "the series keeps Kavita's name"
    checked_with(world, "Only I Level Up")
    world.run(export)
    world.run(export)
    assert world.discord.posts == [f"{want} -> rename the series in Kavita if wanted"], "1 Discord message"


@budget(load="db", seconds=4)
def test_the_same_change_seen_again_is_not_reported_again(world):
    world.kavita_series("Solo Leveling")
    world.run(intake)
    for english in ("Solo Leveling", "Only I Level Up", "Solo Leveling", "Only I Level Up"):
        checked_with(world, english)
    assert len(reports(world)) == 2, "each change once: A -> B and B -> A"


@budget(load="db", seconds=4)
@pytest.mark.parametrize(
    ("first", "then"),
    [
        (("Solo Leveling", AL), ("Solo Leveling", AL)),
        ((None, AL), ("Only I Level Up", AL)),
        (("Solo Leveling", AL), ("Another Manga", "999")),
    ],
    ids=["unchanged", "first-title", "another-anilist-id"],
)
def test_no_report_without_a_change_of_the_same_record_s_title(world, first, then):
    world.kavita_series("Solo Leveling")
    world.run(intake)
    checked_with(world, *first)
    checked_with(world, *then)
    assert reports(world) == [], "only the same AniList record's title, both set, is compared"


@budget(load="db")
def test_a_new_series_not_admitted_yet_is_not_reported(world):
    world.sources.anilist.records[("media", AL)] = titled("New")
    with closing(db.connect(world.cfg)) as conn:
        with db.transaction(conn):
            sid = one(conn, "INSERT INTO series (name, origin) VALUES ('Pending', 'new') RETURNING id")
            conn.execute("INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', %s)", (sid, AL))
            conn.execute(
                """INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)
                VALUES (%s, 'anilist', 'media', %s, 'h', '2026-01-01T00:00:00+00:00')""",
                (sid, json.dumps(titled("Old"))),
            )
        assert check.run(world.context(conn, "intake"), world.sources, sid), "the check completes"
    assert reports(world) == [], "a series not admitted yet is named from its record when admitted"
    assert world.rows("SELECT payload::jsonb -> 'title' ->> 'english' FROM fetched WHERE source = 'anilist'") == [
        ("New",)
    ], "the new record is stored"
