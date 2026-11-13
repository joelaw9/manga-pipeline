"""workflow: claim by priority, outcomes, attempt limit, needs_human, share errors, guarded outcome writes, stale
claims and run records (SPEC "Parts", "State", "Chapter stages")."""

import errno
from contextlib import closing

import pytest

from manga_pipeline import db, workflow
from manga_pipeline.workflow import Done, Dropped, Failed, NeedsHuman, Waiting

from tests.builders import add_chapter, add_series, chapter, site_cfg
from tests.timing import budget


@pytest.fixture
def ctx(tmp_path):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield workflow.Context(cfg, conn, "process")


def returns(outcome):
    return lambda ctx, chapter: outcome


def raises(error):
    def stage(ctx, chapter):
        raise error

    return stage


def claimed(ctx, stage="junk"):
    """A new series' chapter at stage, claimed by ctx's part."""
    chapter_id = add_chapter(ctx.conn, add_series(ctx.conn, "S"), "c1", stage)
    row = workflow.claim(ctx, (stage,))
    assert row["id"] == chapter_id
    return row


@budget(load="db")
def test_claim_order_is_kavita_series_first_then_oldest_then_id(ctx):
    new = add_series(ctx.conn, "New", "new")
    known = add_series(ctx.conn, "Known", "kavita")
    a = add_chapter(ctx.conn, new, "a", "junk", "2026-01-01T00:00:00+00:00")
    b = add_chapter(ctx.conn, known, "b", "junk", "2026-01-03T00:00:00+00:00")
    c = add_chapter(ctx.conn, known, "c", "junk", "2026-01-02T00:00:00+00:00")
    d = add_chapter(ctx.conn, known, "d", "junk", "2026-01-02T00:00:00+00:00")
    order = []
    while row := workflow.claim(ctx, ("junk",)):
        order.append(row["id"])
    assert order == [c, d, b, a], "a series Kavita knows comes first, then the oldest discovery, then id"
    assert {chapter(ctx.conn, i)["claimed_by"] for i in order} == {"process"}


@budget(load="db")
def test_claim_picks_only_the_parts_stages(ctx):
    add_chapter(ctx.conn, add_series(ctx.conn, "S"), "c1", "comicinfo")
    assert workflow.claim(ctx, workflow.PART_STAGES["process"]) is None


@budget(load="db")
def test_convert_waits_for_the_series_ready_junk(ctx):
    series = add_series(ctx.conn, "S")
    convert = add_chapter(ctx.conn, series, "c1", "convert", "2026-01-01T00:00:00+00:00")
    junk = add_chapter(ctx.conn, series, "c2", "junk", "2026-01-02T00:00:00+00:00")
    assert workflow.claim(ctx, ("junk", "convert"))["id"] == junk, "a series' junk runs before any of it converts"
    workflow.work(ctx, chapter(ctx.conn, junk), returns(Done()))
    assert workflow.claim(ctx, ("junk", "convert"))["id"] == convert


@budget(load="db")
def test_junk_that_failed_this_run_does_not_hold_its_series_conversions(ctx):
    series = add_series(ctx.conn, "S")
    junk = add_chapter(ctx.conn, series, "c1", "junk", "2026-01-01T00:00:00+00:00")
    convert = add_chapter(ctx.conn, series, "c2", "convert", "2026-01-02T00:00:00+00:00")
    workflow.work(ctx, workflow.claim(ctx, ("junk", "convert")), returns(Failed("bad page")))
    assert chapter(ctx.conn, junk)["status"] == "ready"
    assert workflow.claim(ctx, ("junk", "convert"))["id"] == convert, "1 failing chapter never holds its series"


@budget(load="db")
def test_needs_human_series_holds_its_chapters(ctx):
    add_chapter(ctx.conn, add_series(ctx.conn, "S", status="needs_human"), "c1", "junk")
    assert workflow.claim(ctx, ("junk",)) is None


@budget(load="db")
def test_out_of_scope_series_is_not_claimed(ctx):
    cfg = {**ctx.cfg, "series": {"only": ["Other"]}}
    add_chapter(ctx.conn, add_series(ctx.conn, "S"), "c1", "junk")
    assert workflow.claim(workflow.Context(cfg, ctx.conn, "process"), ("junk",)) is None


@budget(load="pure")
@pytest.mark.parametrize(
    ("only", "library", "want"),
    [
        ([], None, True),
        ([], 4, True),
        ([], 5, False),
        (["S"], 4, True),
        (["S"], None, True),
        (["Other"], 4, False),
        (["S"], 5, False),
    ],
    ids=["new", "its-library", "other-library", "named", "named-new", "not-named", "named-other-library"],
)
def test_in_scope_needs_the_configured_library_and_a_name_in_series_only(only, library, want):
    cfg = {"kavita": {"library_id": 4}, "series": {"only": only}}
    assert workflow.in_scope(cfg, "S", library) is want, "SPEC 'Parts': its library or new, and named"


@budget(load="db")
def test_a_run_has_1_client_of_each_service(ctx):
    assert ctx.suwayomi.base == ctx.cfg["suwayomi"]["url"] and ctx.kavita.base == ctx.cfg["kavita"]["url"]
    given = workflow.Context(ctx.cfg, ctx.conn, "intake", suwayomi="a suwayomi", kavita="a kavita")
    assert (given.suwayomi, given.kavita) == ("a suwayomi", "a kavita"), "a test's fakes are used as given"


@budget(load="db")
def test_series_of_another_kavita_library_is_not_claimed(ctx):
    other = ctx.cfg["kavita"]["library_id"] + 1
    add_chapter(ctx.conn, add_series(ctx.conn, "Elsewhere", "kavita", kavita_library_id=other), "c1", "junk")
    assert workflow.claim(ctx, ("junk",)) is None, "SPEC 'Parts': other libraries' series are out of scope"
    mine = add_series(ctx.conn, "Mine", "kavita", kavita_library_id=ctx.cfg["kavita"]["library_id"])
    chapter_id = add_chapter(ctx.conn, mine, "c1", "junk")
    assert workflow.claim(ctx, ("junk",))["id"] == chapter_id


@budget(load="db")
def test_done_moves_to_the_next_stage_with_attempts_reset(ctx):
    row = claimed(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE chapter SET attempts = 2 WHERE id = ?", (row["id"],))
    workflow.work(ctx, chapter(ctx.conn, row["id"]), returns(Done()))
    after = chapter(ctx.conn, row["id"])
    assert (after["stage"], after["status"], after["attempts"], after["claimed_by"]) == ("convert", "ready", 0, None)


@budget(load="db")
def test_done_at_the_last_stage_is_done(ctx):
    ctx = workflow.Context(ctx.cfg, ctx.conn, "export")
    row = claimed(ctx, "confirm")
    workflow.work(ctx, row, returns(Done()))
    assert (chapter(ctx.conn, row["id"])["stage"], chapter(ctx.conn, row["id"])["status"]) == ("confirm", "done")


@budget(load="db")
def test_done_record_runs_with_the_outcome(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, returns(Done(lambda conn: conn.execute("INSERT INTO meta VALUES ('rec', 'x')"))))
    assert ctx.conn.execute("SELECT value FROM meta WHERE key = 'rec'").fetchone()[0] == "x"


@budget(load="db")
def test_failing_record_is_a_failed_attempt(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, returns(Done(lambda conn: conn.execute("INSERT INTO meta VALUES ('database_id', 'x')"))))
    after = chapter(ctx.conn, row["id"])
    assert (after["stage"], after["status"], after["attempts"]) == ("junk", "ready", 1), "the whole outcome rolls back"
    assert after["last_error"].startswith("recording the outcome: IntegrityError")


@budget(load="db")
def test_waiting_records_what_it_waits_on(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, returns(Waiting("junk_decision", {"pages": [3]})))
    after = chapter(ctx.conn, row["id"])
    got = (after["stage"], after["status"], after["waiting_on"], after["wait_data"], after["claimed_by"])
    assert got == ("junk", "waiting", "junk_decision", '{"pages": [3]}', None)


@budget(load="db")
@pytest.mark.parametrize(
    "stage", [returns(Failed("bad page")), raises(ValueError("bad page"))], ids=["failed", "raise"]
)
def test_failure_counts_an_attempt_and_stays_at_the_stage(ctx, stage):
    row = claimed(ctx)
    workflow.work(ctx, row, stage)
    after = chapter(ctx.conn, row["id"])
    assert (after["stage"], after["status"], after["attempts"]) == ("junk", "ready", 1)
    assert "bad page" in after["last_error"]


@budget(load="db")
def test_attempt_limit_makes_the_chapter_needs_human(ctx, caplog):
    row = claimed(ctx)
    for _ in range(ctx.cfg["workflow"]["attempt_limit"] - 1):
        workflow.work(ctx, row, returns(Failed("bad page")))
        assert workflow.claim(ctx, ("junk",)) is None, "a chapter that failed is tried again by a later run"
        ctx = workflow.Context(ctx.cfg, ctx.conn, ctx.part)  # the next run
        row = workflow.claim(ctx, ("junk",))
    workflow.work(ctx, row, returns(Failed("bad page")))
    after = chapter(ctx.conn, row["id"])
    assert (after["status"], after["attempts"]) == ("needs_human", 3)
    assert "needs human: chapter" in caplog.text, "needs_human is reported in the log"
    assert workflow.claim(ctx, ("junk",)) is None, "a needs_human chapter is left alone"


@budget(load="db")
@pytest.mark.parametrize(
    "error",
    [
        workflow.Unreachable("share"),
        OSError(errno.EIO, "io"),
        OSError(errno.ESTALE, "stale"),
        TimeoutError(errno.ETIMEDOUT, "t"),
    ],
    ids=["unreachable", "eio", "estale", "etimedout"],
)
def test_unreachable_share_leaves_attempts_unchanged_and_skips_the_stage(ctx, error):
    row = claimed(ctx)
    other = add_chapter(ctx.conn, add_series(ctx.conn, "T"), "c2", "convert")
    outcome = workflow.work(ctx, row, raises(error))
    assert isinstance(outcome, workflow.Unreachable) and outcome.service == "share"
    after = chapter(ctx.conn, row["id"])
    assert (after["stage"], after["status"], after["attempts"], after["claimed_by"]) == ("junk", "ready", 0, None)
    assert workflow.claim(ctx, ("junk",)) is None, "the stage is not claimed again in this run"
    assert workflow.claim(ctx, ("junk", "convert"))["id"] == other, "the part goes on with its other stages"


@budget(load="db")
def test_other_os_error_is_a_failed_attempt(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, raises(FileNotFoundError(errno.ENOENT, "gone")))
    assert chapter(ctx.conn, row["id"])["attempts"] == 1


@budget(load="db")
def test_outcome_does_not_overwrite_a_persons_change(ctx):
    row = claimed(ctx)

    def stage(ctx, chapter):
        with db.transaction(ctx.conn):  # a person acts on the row while the stage works
            ctx.conn.execute(
                "UPDATE chapter SET status = 'ready', claimed_by = NULL, attempts = 0 WHERE id = ?", (row["id"],)
            )
        return Failed("bad page")

    workflow.work(ctx, row, stage)
    after = chapter(ctx.conn, row["id"])
    assert (after["status"], after["attempts"], after["last_error"]) == ("ready", 0, None)


@budget(load="db")
def test_no_transaction_is_open_while_a_stage_runs(ctx):
    seen = []
    workflow.work(ctx, claimed(ctx), lambda ctx, chapter: seen.append(ctx.conn.in_transaction) or Done())
    assert seen == [False]


@budget(load="db")
def test_release_own_claims_frees_only_this_parts_claims(ctx):
    mine = claimed(ctx)
    add_chapter(ctx.conn, mine["series_id"], "c2", "comicinfo")
    other = workflow.claim(workflow.Context(ctx.cfg, ctx.conn, "export"), ("comicinfo",))
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE chapter SET attempts = 1 WHERE id = ?", (mine["id"],))
    workflow.release_own_claims(ctx)
    assert (chapter(ctx.conn, mine["id"])["status"], chapter(ctx.conn, mine["id"])["attempts"]) == ("ready", 1)
    assert chapter(ctx.conn, other["id"])["claimed_by"] == "export", "another part's claim is not this part's to free"


@budget(load="db")
def test_release_own_claims_frees_a_claim_on_a_series_now_out_of_scope(ctx):
    row = claimed(ctx)
    cfg = {**ctx.cfg, "series": {"only": ["Other"]}}
    workflow.release_own_claims(workflow.Context(cfg, ctx.conn, "process"))
    assert chapter(ctx.conn, row["id"])["status"] == "ready", "SPEC 'Parts': a dead run's claim is freed in any scope"


@budget(load="db")
def test_claims_are_kept_when_locks_are_off(ctx):
    row = claimed(ctx)
    cfg = {**ctx.cfg, "lock": {**ctx.cfg["lock"], "enabled": False}}
    workflow.release_own_claims(workflow.Context(cfg, ctx.conn, "process"))
    assert chapter(ctx.conn, row["id"])["status"] == "claimed", "with locks off another live run may hold it"


@budget(load="db")
def test_end_waits_readies_a_chapter_whose_condition_holds(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, returns(Waiting("junk_decision")))
    workflow.end_waits(ctx, "junk_decision", lambda ctx, chapter: False)
    assert chapter(ctx.conn, row["id"])["status"] == "waiting"
    workflow.end_waits(ctx, "junk_decision", lambda ctx, chapter: True)
    after = chapter(ctx.conn, row["id"])
    assert (after["stage"], after["status"], after["waiting_on"]) == ("junk", "ready", None)


@budget(load="db")
def test_end_waits_never_checks_a_wait_of_an_out_of_scope_series(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, returns(Waiting("junk_decision")))
    cfg = {**ctx.cfg, "series": {"only": ["Other"]}}
    asked = []
    workflow.end_waits(
        workflow.Context(cfg, ctx.conn, "process"), "junk_decision", lambda ctx, chapter: asked.append(chapter["id"])
    )
    assert asked == [], "SPEC 'Parts': nothing out of scope is read"
    assert chapter(ctx.conn, row["id"])["status"] == "waiting"


@budget(load="db")
def test_needs_human_series(ctx, caplog):
    series = add_series(ctx.conn, "S")
    with db.transaction(ctx.conn):
        workflow.needs_human(ctx.conn, "series", series, "source ids disagree")
    row = ctx.conn.execute("SELECT status, status_reason FROM series WHERE id = ?", (series,)).fetchone()
    assert tuple(row) == ("needs_human", "source ids disagree")
    assert "needs human: series" in caplog.text


def meta(ctx):
    return {r["key"]: r["value"] for r in ctx.conn.execute("SELECT * FROM meta WHERE key LIKE 'process.%'")}


@budget(load="db")
def test_run_records_start_end_result_and_success(ctx):
    workflow.run_part(ctx, lambda ctx: None)
    got = meta(ctx)
    assert set(got) == {"process.last_start", "process.last_end", "process.last_result", "process.last_success"}
    assert got["process.last_result"] == "ok" and got["process.last_success"] == got["process.last_end"]


@budget(load="db")
def test_unreachable_run_ends_with_its_result_and_no_success(ctx):
    def body(ctx):
        raise workflow.Unreachable("share")

    workflow.run_part(ctx, body)
    got = meta(ctx)
    assert got["process.last_result"] == "unreachable: share"
    assert "process.last_success" not in got, "a run that could not reach the share is not a success"


@budget(load="db")
def test_needs_human_clears_what_a_waiting_chapter_waits_on(ctx):
    row = claimed(ctx)
    workflow.work(ctx, row, returns(Waiting("junk_decision", {"pages": [3]})))
    with db.transaction(ctx.conn):
        workflow.needs_human(ctx.conn, "chapter", row["id"], "junk candidates unreadable")
    after = chapter(ctx.conn, row["id"])
    assert (after["status"], after["waiting_on"], after["wait_data"]) == ("needs_human", None, None)


@budget(load="db")
def test_failing_wait_check_is_logged_and_the_others_go_on(ctx, caplog):
    first = claimed(ctx)
    workflow.work(ctx, first, returns(Waiting("junk_decision")))
    add_chapter(ctx.conn, first["series_id"], "c2", "junk")
    second = workflow.claim(ctx, ("junk",))
    workflow.work(ctx, second, returns(Waiting("junk_decision")))

    def holds(ctx, chapter):
        if chapter["id"] == first["id"]:
            raise ValueError("candidate image unreadable")
        return True

    workflow.end_waits(ctx, "junk_decision", holds)
    assert chapter(ctx.conn, first["id"])["status"] == "waiting"
    assert chapter(ctx.conn, second["id"])["status"] == "ready"
    assert f"chapter {first['id']}" in caplog.text and "candidate image unreadable" in caplog.text


@budget(load="db")
def test_dropped_outcome_is_neither_logged_nor_counted(ctx, caplog):
    row = claimed(ctx)

    def stage(ctx, chapter):
        with db.transaction(ctx.conn):  # a person acts on the row while the stage works
            ctx.conn.execute("UPDATE chapter SET status = 'ready', claimed_by = NULL WHERE id = ?", (row["id"],))
        return Failed("bad page")

    workflow.work(ctx, row, stage)
    assert (ctx.counts, ctx.failed) == ({}, set())
    assert "outcome is dropped" in caplog.text and "failed: bad page" not in caplog.text


@budget(load="db")
def test_crash_after_an_unreachable_stage_records_error_and_raises(ctx):
    def body(ctx):
        workflow.work(ctx, claimed(ctx), raises(OSError(errno.EIO, "io")))
        raise KeyError("a bug in the loop")

    with pytest.raises(KeyError):
        workflow.run_part(ctx, body)
    got = dict(ctx.conn.execute("SELECT key, value FROM meta WHERE key = 'process.last_result'").fetchall())
    assert got == {"process.last_result": "error"}, "a crash is an error, whatever the run met before"


@budget(load="db")
def test_dropped_ends_the_chapter_done_where_it_stands_and_runs_its_record(ctx):
    row = claimed(ctx, "junk")
    written = []
    workflow.work(ctx, row, returns(Dropped("same pages again", lambda conn: written.append(row["id"]))))
    after = chapter(ctx.conn, row["id"])
    assert (after["stage"], after["status"]) == ("junk", "done"), "a dropped chapter is done at its own stage"
    assert written == [row["id"]], "its record runs with the outcome"
    assert ctx.counts["dropped"] == 1


@budget(load="db")
def test_needs_human_outcome_needs_no_attempts(ctx):
    row = claimed(ctx, "junk")
    workflow.work(ctx, row, returns(NeedsHuman("target name taken")))
    after = chapter(ctx.conn, row["id"])
    assert (after["status"], after["attempts"], after["last_error"]) == ("needs_human", 0, "target name taken"), (
        "no attempt can change the result: needs_human at once"
    )


@budget(load="db")
def test_drain_returns_how_many_ended_done(ctx):
    series = add_series(ctx.conn, "S")
    for name in ("a", "b", "c"):
        add_chapter(ctx.conn, series, name, "junk")
    outcomes = iter([Done(), Failed("x"), Done()])
    assert workflow.drain(ctx, {"junk": lambda ctx, chapter: next(outcomes)}) == 2


@budget(load="db")
def test_charge_waits_costs_1_attempt_per_waiting_chapter_and_ends_at_the_limit(ctx):
    series = add_series(ctx.conn, "S")
    tired = add_chapter(ctx.conn, series, "a", "confirm", attempts=2)
    fresh = add_chapter(ctx.conn, series, "b", "confirm")
    ready = add_chapter(ctx.conn, series, "c", "confirm")
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE chapter SET status = 'waiting', waiting_on = 'kavita_scan' WHERE id IN (?, ?)", (tired, fresh)
        )
    assert workflow.charge_waits(ctx, series, "kavita_scan", "not indexed") == 2
    assert chapter(ctx.conn, tired)["status"] == "needs_human", "the limit reached by waiting is needs_human"
    assert (chapter(ctx.conn, fresh)["status"], chapter(ctx.conn, fresh)["attempts"]) == ("waiting", 1)
    assert chapter(ctx.conn, ready)["attempts"] == 0, "only waiting chapters are charged"
