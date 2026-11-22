"""Incidents (SPEC "Needs human"): a stalled part, downloads not taken and Kavita refusing the key, 1 message each.

`watch` finds the first 2 and sends what is queued; intake queues the key incident, its answered listing removes it.
"""

import logging
from contextlib import closing
from datetime import datetime, timedelta

import pytest

from manga_pipeline import cli, db, export, incidents, intake, notify, process, workflow
from manga_pipeline.clients import discord as discord_client
from manga_pipeline.errors import Unreachable

from tests.builders import config_file, site_cfg
from tests.fakes.discord import FakeDiscord
from tests.timing import budget
from tests.world import World

KEY_POST = f"{incidents.KEY_REFUSED} -> {incidents.KEY_FIX}"
WEBHOOK = "https://discord.invalid/api/webhooks/0/watch"


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


@pytest.fixture
def discord(monkeypatch):
    """Make every Discord client built from config 1 FakeDiscord (watch builds its own); return it."""
    fake = FakeDiscord()
    monkeypatch.setattr(discord_client, "from_config", lambda _cfg: fake)
    return fake


@pytest.fixture
def cfg(tmp_path):
    return site_cfg(tmp_path)


@pytest.fixture
def world(tmp_path, discord):
    w = World(tmp_path)
    w.discord = discord  # the parts' sends and watch's go to 1 Discord
    return w


def ago(conn, hours):
    """Return the stored time `hours` before the database's clock."""
    return (datetime.fromisoformat(db.now(conn)) - timedelta(hours=hours)).isoformat()


def succeeded(cfg, owner, hours_ago):
    """Record owner's last success `hours_ago` hours ago, as run_part does; return the stored time."""
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        stamp = ago(conn, hours_ago)
        workflow.set_meta(conn, f"{owner}.last_success", stamp)
    return stamp


def not_taken(cfg, path, hours_ago):
    """List a download not taken since `hours_ago` hours ago, as discovery does."""
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        conn.execute(
            "INSERT INTO unmatched_download (path, reason, first_seen) VALUES (%s, 'no manga maps to it', %s)",
            (path, ago(conn, hours_ago)),
        )


def watch(cfg):
    with closing(db.connect(cfg)) as conn:
        incidents.watch(cfg, conn)


def rows(cfg, query, *params):
    with closing(db.connect(cfg)) as conn:
        return [tuple(r) for r in conn.execute(query, params or None)]


def key_rows(cfg):
    return rows(
        cfg,
        "SELECT sent_at IS NOT NULL FROM notification WHERE kind = 'incident' AND text = %s",
        "Kavita refuses the API key",
    )


def result(world, part):
    return world.rows("SELECT value FROM meta WHERE key = %s", f"{part}.last_result")[0][0]


def stall(owner, stamp):
    return f"{owner}: no successful run since {stamp} -> {incidents.STALL_FIX}"


# --- stalls


@budget(load="db")
def test_a_part_with_no_success_in_24_hours_is_reported_once_while_it_lasts(cfg, discord):
    stamp = succeeded(cfg, "export", 25)
    watch(cfg)
    watch(cfg)
    assert discord.posts == [stall("export", stamp)], "SPEC 'Needs human': 1 message per incident while it lasts"


@budget(load="db")
def test_watch_reports_export_s_stall_with_no_export_run(cfg, discord):
    stamp = succeeded(cfg, "export", 30)
    watch(cfg)
    assert rows(cfg, "SELECT count(*) FROM meta WHERE key = 'export.last_start'") == [(0,)], "no export ran"
    assert discord.posts == [stall("export", stamp)], "SPEC 'Needs human': a stalled export cannot silence its stall"


@budget(load="db")
def test_a_part_that_succeeds_again_and_stalls_again_is_reported_again(cfg, discord):
    first = succeeded(cfg, "intake", 50)
    watch(cfg)
    succeeded(cfg, "intake", 1)
    watch(cfg)
    assert discord.posts == [stall("intake", first)], "nothing is sent when an incident clears"
    second = succeeded(cfg, "intake", 26)  # a later success, then 24 h more without one
    watch(cfg)
    assert discord.posts == [stall("intake", first), stall("intake", second)], "a new stall is a new message"


@budget(load="db")
def test_a_part_within_24_hours_is_not_reported(cfg, discord):
    succeeded(cfg, "export", 23)
    watch(cfg)
    assert discord.posts == []
    assert rows(cfg, "SELECT count(*) FROM notification") == [(0,)]


@budget(load="db")
def test_each_process_machine_is_watched_on_its_own(cfg, discord):
    luna = succeeded(cfg, "process@luna", 30)
    succeeded(cfg, "process@celestia", 2)
    watch(cfg)
    assert discord.posts == [stall("process@luna", luna)], "SPEC 'Needs human': process is watched per machine"


@budget(load="db")
def test_junk_review_s_stall_is_reported(cfg, discord):
    stamp = succeeded(cfg, "junk-review", 48)
    watch(cfg)
    assert discord.posts == [stall("junk-review", stamp)]


@budget(load="db")
def test_a_part_that_never_succeeded_is_not_watched(cfg, discord):
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        workflow.set_meta(conn, "export.last_start", ago(conn, 50))
        workflow.set_meta(conn, "export.last_result", "error")
    watch(cfg)
    assert discord.posts == [], "SPEC 'Needs human': a part that has never recorded a success is not watched"


# --- downloads not taken


@budget(load="db")
def test_a_folder_of_downloads_not_taken_for_24_hours_is_reported_once(cfg, discord):
    folder = "downloads/mangas/Src/Solo Leveling"
    for n, hours in ((1, 30), (2, 28), (3, 26)):
        not_taken(cfg, f"{folder}/Chapter {n}.cbz", hours)
    oldest = rows(cfg, "SELECT min(first_seen) FROM unmatched_download")[0][0]
    watch(cfg)
    not_taken(cfg, f"{folder}/Chapter 4.cbz", 25)
    watch(cfg)
    assert discord.posts == [f"downloads not taken in {folder} since {oldest} -> {incidents.DOWNLOADS_FIX}"], (
        "SPEC 'Needs human': 1 incident per folder, named by its oldest download; a file added later changes nothing"
    )


@budget(load="db")
def test_downloads_not_taken_for_less_than_24_hours_are_not_reported(cfg, discord):
    not_taken(cfg, "downloads/mangas/Src/Solo Leveling/Chapter 1.cbz", 23)
    watch(cfg)
    assert discord.posts == []


# --- Kavita refusing the key


@budget(load="db", seconds=4)
def test_a_refused_kavita_key_is_reported_once_while_it_lasts(world):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 2")
    for part in (intake, process, export):
        world.run(part)
    assert world.rows("SELECT waiting_on FROM chapter") == [("kavita_scan",)], "export has Kavita work"
    world.kavita.refuse_key = True
    world.run(intake)  # no new download: intake meets Kavita in its metadata step only
    world.run(intake)
    assert result(world, "intake") == "ok", "SPEC 'Metadata checks': a skipped metadata step leaves the run ok"
    assert key_rows(world.cfg) == [(False,)], "SPEC 'Needs human': 1 row while the refusal lasts"
    world.run(export)
    assert world.kavita.refused_calls, "export asked Kavita and was refused"
    assert result(world, "export") == "unreachable: kavita"
    assert key_rows(world.cfg) == [(True,)], "export queues no incident of its own; it sends intake's"
    assert world.discord.posts == [KEY_POST], "1 message in all"


@budget(load="db", seconds=3)
def test_a_key_refused_again_after_kavita_answered_is_reported_again(world):
    world.kavita_series("Solo Leveling")
    world.kavita.refuse_key = True
    world.run(intake)
    watch(world.cfg)
    world.kavita.refuse_key = False
    world.run(intake)
    assert key_rows(world.cfg) == [], "SPEC 'Needs human': a listing Kavita answers removes the sent incident"
    world.kavita.refuse_key = True
    world.run(intake)
    watch(world.cfg)
    assert world.discord.posts == [KEY_POST, KEY_POST], "the next refusal is a new message"


@budget(load="db", seconds=2)
def test_a_refusal_met_in_discovery_is_reported(world):
    manga = world.manga("Unlinked Manga")
    world.download(manga, "Chapter 1")
    world.kavita.refuse_key = True
    world.run(intake)
    assert result(world, "intake") == "unreachable: kavita", "discovery asked Kavita: the run ends there"
    assert key_rows(world.cfg) == [(False,)], "queued at the end of the run that met it, wherever it met it"
    watch(world.cfg)
    assert world.discord.posts == [KEY_POST]


@budget(load="db", seconds=3)
def test_an_outage_that_is_not_a_refusal_leaves_the_key_incident_open(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    world.kavita.refuse_key = True
    world.run(intake)
    watch(world.cfg)
    world.kavita.refuse_key = False

    def down(_library_id):
        service = "kavita"
        raise Unreachable(service, "down")

    monkeypatch.setattr(world.kavita, "series_in_library", down)
    world.run(intake)
    assert key_rows(world.cfg) == [(True,)], "an outage is not an answer: the incident stays open"
    monkeypatch.undo()
    world.kavita.refuse_key = True
    world.run(intake)
    watch(world.cfg)
    assert world.discord.posts == [KEY_POST], "the same refusal: no 2nd message"


@budget(load="db", seconds=4)
def test_a_run_whose_kavita_key_was_not_refused_queues_no_incident(world):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 2")
    for part in (intake, process, export):
        world.run(part)
    world.kavita.run_scans()
    world.run(export)
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("confirm", "done")]
    assert world.rows("SELECT count(*) FROM notification WHERE kind = 'incident'") == [(0,)]


@budget(load="db")
def test_incidents_are_sent_alone_before_a_digest_of_the_other_reports(cfg, discord):
    with closing(db.connect(cfg)) as conn:
        with db.transaction(conn):
            for i in range(7):
                notify.queue(conn, "series", i + 1, f"series {i + 1} (S{i}): source ids disagree", "retry")
        incidents.queue_key_refused(conn)
        notify.send(conn, discord)
    assert len(discord.posts) == 2, "SPEC 'Needs human': the incident alone, then the digest"
    assert discord.posts[0] == KEY_POST, "incidents are sent first"
    assert discord.posts[1].startswith("7 reports"), "the digest counts the other reports only"
    assert incidents.KEY_REFUSED not in discord.posts[1], "an incident is never in a digest"


@budget(load="db")
def test_watch_refuses_to_run_without_a_webhook(cfg, tmp_path, capsys):
    assert cli.main(["--config", str(config_file(tmp_path, cfg)), "watch"]) == 4
    assert "discord webhook" in capsys.readouterr().err, "SPEC 'Preflight': watch has nowhere to send"


@budget(load="db")
def test_watch_runs_with_the_share_and_the_fleet_version_missing(cfg, tmp_path, discord):
    stamp = succeeded(cfg, "export", 25)  # cfg's site is not adopted: no owner file, no fleet version
    path = config_file(tmp_path, {**cfg, "notify": {"discord_webhook": WEBHOOK}})
    assert cli.main(["--config", str(path), "watch"]) == 0
    assert rows(cfg, "SELECT kind FROM notification") == [("incident",)]
    assert discord.posts == [stall("export", stamp)], "SPEC 'Needs human': watch needs only the database and a webhook"


@budget(load="db")
def test_an_unreachable_discord_stops_the_incidents_and_keeps_them_queued(cfg, discord):
    first = succeeded(cfg, "export", 30)
    second = succeeded(cfg, "intake", 26)
    with closing(db.connect(cfg)) as conn:
        with db.transaction(conn):
            notify.queue(conn, "series", 1, "a", "retry 1")
            notify.queue(conn, "series", 2, "b", "retry 2")
        discord.failure = Unreachable("discord", "HTTP 503")
        incidents.watch(cfg, conn)
        assert discord.posts == []
        assert rows(cfg, "SELECT count(*) FROM notification WHERE sent_at IS NULL AND failed IS NULL") == [(4,)], (
            "SPEC 'Needs human': an unreachable Discord loses nothing"
        )
        discord.failure = None
        notify.send(conn, discord)
    assert discord.posts == [stall("export", first), stall("intake", second), "a -> retry 1", "b -> retry 2"], (
        "the incidents first, then the reports"
    )


@budget(load="db", seconds=3)
def test_a_refusal_that_ends_before_it_is_sent_is_still_sent_once(world):
    world.kavita_series("Solo Leveling")
    world.kavita.refuse_key = True
    world.run(intake)
    world.kavita.refuse_key = False
    world.run(intake)
    assert key_rows(world.cfg) == [(False,)], "SPEC 'Needs human': an unsent incident stays until it is sent"
    watch(world.cfg)
    assert world.discord.posts == [KEY_POST]
    world.run(intake)
    assert key_rows(world.cfg) == [], "sent, the next answered listing removes it"
