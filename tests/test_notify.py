"""notify: the Discord queue and its send (SPEC "Needs human")."""

from contextlib import closing

import pytest

from manga_pipeline import db, export, notify, status, workflow
from manga_pipeline.clients import http
from manga_pipeline.errors import Unreachable

from tests.builders import add_chapter, add_series, db_row, one, site_cfg
from tests.fakes.discord import FakeDiscord
from tests.timing import budget
from tests.world import World


@pytest.fixture
def conn(tmp_path):
    with closing(db.connect(site_cfg(tmp_path))) as connection:
        yield connection


def queued(conn, *texts, fix="retry 1"):
    with db.transaction(conn):
        for i, text in enumerate(texts):
            notify.queue(conn, "series", i + 1, text, fix)


def units(text):
    return len(text.encode("utf-16-le")) // 2


def unsent(conn):
    return one(conn, "SELECT count(*) FROM notification WHERE sent_at IS NULL AND failed IS NULL")


@budget(load="db")
def test_a_report_is_queued_once_per_kind_item_and_text(conn):
    queued(conn, "series 1 (A): gone")
    queued(conn, "series 1 (A): gone")
    with db.transaction(conn):
        notify.queue(conn, "chapter", 1, "series 1 (A): gone", "retry 1")
    assert one(conn, "SELECT count(*) FROM notification") == 2, "SPEC 'Needs human': once per kind, item and reason"


@budget(load="db")
def test_queue_runs_only_inside_its_writer_s_transaction(conn):
    with pytest.raises(RuntimeError, match="transaction"):
        notify.queue(conn, "series", 1, "text", "fix")


@budget(load="db")
def test_up_to_5_reports_are_posted_1_each_and_never_again(conn):
    queued(conn, "a", "b", "c", "d", "e")
    discord = FakeDiscord()
    notify.send(conn, discord)
    notify.send(conn, discord)
    notify.send(conn, discord)
    assert discord.posts == [f"{t} -> retry 1" for t in "abcde"], "SPEC 'Needs human': each report once, oldest first"
    assert unsent(conn) == 0


@budget(load="db")
def test_more_than_5_reports_go_as_1_digest_that_fits_discord(conn):
    queued(conn, *[f"series {i} (Name): {'a long reason ' * 60}" for i in range(7)])
    discord = FakeDiscord()
    notify.send(conn, discord)
    notify.send(conn, discord)
    assert len(discord.posts) == 1, "SPEC 'Needs human': a burst is 1 digest"
    (text,) = discord.posts
    lines = text.split("\n")
    assert lines[0].startswith("7 reports"), "the digest counts every report"
    assert [line[:11] for line in lines[1:6]] == [f"- series {i} " for i in range(5)], "the oldest 5, oldest first"
    assert all(line.endswith(" -> retry 1") for line in lines[1:6]), "each entry keeps its clearing command"
    assert len(lines) == 7, "the count, the first 5, and where the rest are"
    assert lines[-1] == "The rest: `status`."
    assert units(text) <= notify.LIMIT, "Discord refuses a message over 2000"
    assert unsent(conn) == 0, "every report the digest covers is sent"


@budget(load="pure")
def test_a_digest_of_rows_left_after_another_sender_lists_them_all():
    text = notify.digest([db_row(text=t, fix="retry 1") for t in "ab"])
    assert text == "2 reports need a look:\n- a -> retry 1\n- b -> retry 1", "no 'the rest' when every row is listed"


@budget(load="pure")
def test_a_long_message_is_cut_and_keeps_its_fix():
    text = notify.message("chapter 1 (😀 Name / x): " + "😀 reason " * 400, "retry 1, chapter drop 1")
    assert units(text) <= notify.LIMIT, "Discord counts UTF-16 units at most"
    assert text.endswith("… -> retry 1, chapter drop 1"), "SPEC 'Needs human': the clearing command is never cut"


@budget(load="db")
def test_an_unreachable_discord_leaves_the_reports_for_the_next_run(conn):
    queued(conn, "a", "b")
    discord = FakeDiscord()
    discord.failure = Unreachable("discord", "HTTP 503")
    notify.send(conn, discord)
    assert unsent(conn) == 2, "SPEC 'Needs human': a report Discord did not take is sent later"
    discord.failure = None
    notify.send(conn, discord)
    assert discord.posts == ["a -> retry 1", "b -> retry 1"]


@budget(load="db")
@pytest.mark.parametrize("status", [401, 403, 404, 500, 501])
def test_a_refused_webhook_or_a_failing_discord_stops_the_send_and_keeps_the_reports(conn, status):
    queued(conn, "a", "b")
    discord = FakeDiscord()
    discord.failure = http.HttpError(status, "discord webhook", b"")
    notify.send(conn, discord)
    assert unsent(conn) == 2, "SPEC 'Needs human': a wrong webhook or a failing Discord loses nothing"


@budget(load="db")
def test_a_refused_message_is_marked_failed_and_the_rest_are_sent(conn):
    queued(conn, "a", "b")

    class RefusesA(FakeDiscord):
        def post(self, text):
            if text.startswith("a"):
                raise http.HttpError(400, "discord webhook", b'{"content": ["too long"]}')
            super().post(text)

    discord = RefusesA()
    notify.send(conn, discord)
    assert discord.posts == ["b -> retry 1"], "SPEC 'Needs human': 1 refused message never blocks the others"
    failed = one(conn, "SELECT failed FROM notification WHERE text = 'a'")
    assert failed.startswith("HTTP 400 from discord webhook"), "the refusal is kept for status"


@budget(load="db")
def test_a_report_another_sender_claimed_is_not_posted(conn):
    queued(conn, "a", "b")
    with db.transaction(conn):
        conn.execute("UPDATE notification SET sent_at = %s WHERE text = 'a'", (db.now(conn),))
    discord = FakeDiscord()
    notify.send(conn, discord)
    assert discord.posts == ["b -> retry 1"], "a claimed report is posted by its claimer only"


@budget(load="db")
def test_discord_off_keeps_the_reports_queued(conn):
    queued(conn, "a")
    notify.send(conn, None)
    assert unsent(conn) == 1, "SPEC 'Needs human': with no webhook, status still shows them"


@budget(load="db")
def test_a_series_needs_human_again_for_the_same_reason_sends_nothing_new(conn):
    series = add_series(conn, "Name", origin="kavita")
    for _ in range(2):
        with db.transaction(conn):
            workflow.needs_human(conn, "series", series, "Kavita no longer holds series 7")
        workflow.retry_series(conn, series)
    workflow.reset(conn)
    with db.transaction(conn):
        workflow.needs_human(conn, "series", series, "Kavita no longer holds series 7")
    assert one(conn, "SELECT count(*) FROM notification") == 1, "SPEC 'Needs human': retry or reset sends nothing new"
    with db.transaction(conn):
        workflow.needs_human(conn, "series", series, "another reason")
    texts = [r[0] for r in conn.execute("SELECT text || ' -> ' || fix FROM notification ORDER BY id")]
    assert texts == [
        f"series {series} (Name): Kavita no longer holds series 7 -> retry --series {series}",
        f"series {series} (Name): another reason -> retry --series {series}",
    ], "a new reason is a new report, written as status writes it"


@budget(load="db")
def test_a_chapter_report_names_its_series_identity_stage_and_fixes(conn):
    chapter = add_chapter(conn, add_series(conn, "Name"), '["", 0, "", 3.0, ""]', stage="validate")
    with db.transaction(conn):
        workflow.needs_human(conn, "chapter", chapter, "bad page 4")
    text, fix = conn.execute("SELECT text, fix FROM notification").fetchone()
    assert text == f'chapter {chapter} (Name / ["", 0, "", 3.0, ""], at validate): bad page 4'
    assert fix == f"retry {chapter}, chapter use {chapter} SUWAYOMI_CHAPTER_ID, chapter drop {chapter}"


@budget(load="db", seconds=3)
def test_export_sends_the_reports_even_when_its_run_fails(tmp_path, monkeypatch):
    world = World(tmp_path)
    with closing(db.connect(world.cfg)) as conn:
        queued(conn, "a")

    def fails(_ctx):
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(export, "_work", fails)
    with pytest.raises(RuntimeError):
        world.run(export)
    assert world.discord.posts == ["a -> retry 1"], "SPEC 'Needs human': a failed run still sends"


@budget(load="db", seconds=3)
def test_an_unreachable_discord_never_changes_export_s_result(tmp_path):
    world = World(tmp_path)
    with closing(db.connect(world.cfg)) as conn:
        queued(conn, "a")
    world.discord.failure = Unreachable("discord", "HTTP 503")
    world.run(export)
    assert world.rows("SELECT value FROM meta WHERE key = 'export.last_result'") == [("ok",)], (
        "SPEC 'Needs human': Discord never fails the part"
    )


@budget(load="db")
def test_status_shows_the_reports_not_sent_and_the_titles_changed(conn):
    queued(conn, "a", "b")
    with db.transaction(conn):
        notify.queue(conn, "title", 9, 'series 9 (N): AniList title changed: "A" -> "B"', "rename it in Kavita")
        conn.execute("UPDATE notification SET failed = 'HTTP 400 from discord webhook' WHERE text = 'b'")
    with conn.transaction():
        lines = status.report(conn)
    assert any(line.startswith("  2 queued, the oldest since ") for line in lines), "SPEC 'Needs human': unsent shown"
    assert "  refused: b: HTTP 400 from discord webhook" in lines, "a refused report is shown with why"
    assert any(line.startswith('  series 9 (N): AniList title changed: "A" -> "B" (seen ') for line in lines), (
        "SPEC 'Retroactive updates': a changed title shows in status"
    )


@budget(load="db")
def test_status_lists_title_changes_of_the_last_30_days_only(conn):
    with db.transaction(conn):
        notify.queue(conn, "title", 9, 'series 9 (N): AniList title changed: "A" -> "B"', "rename it in Kavita")
        conn.execute("UPDATE notification SET queued_at = '2020-01-01T00:00:00+00:00'")
    with conn.transaction():
        lines = status.report(conn)
    at = lines.index("Series titles changed at the sources (last 30 days):")
    assert lines[at + 1] == "  none", "SPEC 'Retroactive updates': status lists the changes of the last 30 days"
