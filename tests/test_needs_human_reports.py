"""A needs_human chapter and series reach a person 3 ways, once (SPEC "Needs human"; PLAN Phase 8 "Done when")."""

import logging
from contextlib import closing

from manga_pipeline import config, db, export, intake, process, status, validation
from manga_pipeline.clients import discord as discord_client
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import sources

from tests.builders import one, run_part
from tests.timing import budget
from tests.world import World


@budget(load="db", seconds=10)
def test_a_needs_human_chapter_and_series_each_give_1_log_line_1_status_entry_and_1_discord_message(
    tmp_path, monkeypatch, caplog
):
    world = World(tmp_path)
    # Every part runs as cli.main runs it, with the world's fakes as its clients
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: world.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: world.sources)
    monkeypatch.setattr(discord_client, "from_config", lambda _cfg: world.discord)
    # A series whose ids disagree: Kavita's AniList link and Suwayomi's tracker name 2 records
    beta = world.kavita_series("Beta")
    world.kavita.metadata_rows[beta] = {"webLinks": "https://anilist.co/manga/1"}
    world.suwayomi.add_tracker(world.manga("Beta"), 2, 2)
    # A chapter that fails convert every time: needs_human once its attempts reach the limit
    world.kavita_series("Alpha")
    _, bad = world.download(world.manga("Alpha"), "Chapter 2")

    def convert(_ctx, _chapter):
        msg = "cannot decode page 3"
        raise ValueError(msg)

    monkeypatch.setitem(process.STAGES, "convert", convert)
    with caplog.at_level(logging.WARNING, logger="manga_pipeline"):
        for _ in range(world.cfg["workflow"]["attempt_limit"] + 1):
            with closing(db.connect(world.cfg)) as conn:  # the back-off a failed chapter waits out between runs
                conn.execute("UPDATE chapter SET retry_after = NULL")
            for part in (intake, process, export):
                run_part(world.cfg, part)
        for _ in range(3):
            run_part(world.cfg, export)
    with closing(db.connect(world.cfg)) as conn:
        chapter = one(conn, "SELECT id FROM chapter WHERE download_path = %s", (config.to_stored(world.cfg, bad),))
        series = one(conn, "SELECT id FROM series WHERE kavita_series_id = %s", (beta,))
        assert one(conn, "SELECT status FROM chapter WHERE id = %s", (chapter,)) == "needs_human"
        lines = status.report(conn)
    for kind, item in (("chapter", chapter), ("series", series)):
        assert caplog.text.count(f"needs human: {kind} {item}:") == 1, f"1 log line for the {kind}"
        # the fake pages are blank, so the chapter also has a line of its own among the chapter notes
        entries = [
            line for line in lines if line.startswith(f"  {kind} {item} (") and validation.BLANK_NOTE not in line
        ]
        assert len(entries) == 1, f"1 status entry for the {kind}"
        posts = [p for p in world.discord.posts if p.startswith(f"{kind} {item} (")]
        assert len(posts) == 1, f"1 Discord message for the {kind}, none on the 3 later export runs"
    assert len(world.discord.posts) == 2, "nothing else was sent"
