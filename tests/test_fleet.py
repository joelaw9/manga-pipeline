"""process on several machines against 1 database (SPEC "Parts", "State", "Chapter stages", "Run lock").

Each machine here is its own connection holding its own run lock, with its own claim owner.
"""

import ast
import dataclasses
from contextlib import ExitStack, closing
from datetime import datetime as real_datetime
from pathlib import Path

import pytest
import yaml

from manga_pipeline import cli, config, db, handled, images, lock, notices, page_store, state, workflow
from manga_pipeline.convert import calibrate
from manga_pipeline.db import sql
from manga_pipeline.workflow import Failed, Waiting

from tests.builders import add_chapter, add_series, chapter, db_row, one, present, site_cfg
from tests.timing import budget
from tests.world import cbz


class Fleet:
    """Machines (name -> Context) sharing 1 database, each on its own connection holding its run lock."""

    def __init__(self, cfg: config.Config, stack):
        """Open the test's own connection on `stack`; no machine yet."""
        self.cfg, self.stack = cfg, stack
        self.conn = stack.enter_context(closing(db.connect(cfg)))  # the test's own view
        self.machines = {}

    def machine(self, name, cfg=None):
        """Start machine `name`: its own connection holding its run lock, owner process@<name>; return its Context."""
        cfg = cfg or self.cfg
        conn = db.connect(cfg)
        owner = f"process@{name}"
        held = lock.held(conn, cfg, [owner], lock.SHARED)
        held.__enter__()
        ctx = dataclasses.replace(workflow.context(cfg, conn, "process"), owner=owner)
        self.machines[name] = (ctx, held)
        self.stack.callback(self._close, name)
        return ctx

    def vanish(self, name):
        """End the machine's session (it died, or slept until the server ended it)."""
        self.machines[name][0].conn.close()
        for _ in range(50):  # until the server has seen the session end
            if not one(self.conn, sql.SQL("SELECT {}").format(lock.held_sql(sql.Placeholder())), (f"process@{name}",)):
                return
        msg = f"the server still holds {name}'s lock"
        raise AssertionError(msg)

    def _close(self, name):
        ctx, held = self.machines[name]
        if not ctx.conn.closed:
            held.__exit__(None, None, None)
            ctx.conn.close()

    def series(self, name, chapters=2, folder=None):
        """Add a Kavita series with `chapters` chapters at the junk stage; return (series id, [chapter ids])."""
        series_id = add_series(self.conn, name, origin="kavita")
        if folder:
            with db.transaction(self.conn):
                self.conn.execute("UPDATE series SET folder = %s WHERE id = %s", (folder, series_id))
        return series_id, [add_chapter(self.conn, series_id, f"c{i}", "junk") for i in range(chapters)]

    def row(self, chapter_id):
        """Return the chapter's row, read on the test's own connection."""
        return chapter(self.conn, chapter_id)


@pytest.fixture
def fleet(tmp_path):
    with ExitStack() as stack:
        yield Fleet(site_cfg(tmp_path), stack)


def returns(outcome):
    return lambda _ctx, _chapter: outcome


@budget(load="db", seconds=2)
def test_3_machines_claim_3_series_and_no_chapter_twice(fleet):
    for name in ("A", "B", "C", "D"):
        fleet.series(name, chapters=3)
    machines = [fleet.machine(n) for n in ("lxc", "celestia", "luna")]
    claims = [present(workflow.claim(ctx, ("junk",))) for ctx in machines]
    assert len({c["id"] for c in claims}) == 3, "no chapter is claimed twice"
    assert len({c["series_id"] for c in claims}) == 3, "machines work different series at once"
    assert present(workflow.claim(machines[0], ("junk",)))["series_id"] not in {c["series_id"] for c in claims}, (
        "a machine holding a claim in a series takes another series next, never a second chapter of one in hand"
    )


@budget(load="db")
def test_series_sharing_a_rules_key_are_never_worked_at_once(fleet):
    fleet.series("X local", chapters=1, folder="/share/library/Local/X")
    fleet.series("X source", chapters=1, folder="/share/library/Src/X")
    a, b = fleet.machine("a"), fleet.machine("b")
    assert workflow.claim(a, ("junk",)) is not None
    assert workflow.claim(b, ("junk",)) is None, "2 series rows sharing a rules key share junk knowledge"


@budget(load="db")
def test_between_2_chapters_a_series_may_move_to_another_machine(fleet):
    series_id, _ = fleet.series("S", chapters=2)
    a, b = fleet.machine("a"), fleet.machine("b")
    first = workflow.claim(a, ("junk",))
    workflow.work(a, first, returns(Waiting("junk_decision")))
    assert present(workflow.claim(b, ("junk",)))["series_id"] == series_id, "a holds no claim between chapters"


@budget(load="db", seconds=2)
def test_a_dead_machines_claims_are_freed_by_another_machines_next_claim(fleet):
    _, (c1,) = fleet.series("S", chapters=1)
    a, b = fleet.machine("a"), fleet.machine("b")
    assert present(workflow.claim(a, ("junk",)))["id"] == c1
    assert workflow.claim(b, ("junk",)) is None, "a live machine's claim stays"
    assert fleet.row(c1)["claimed_by"] == "process@a"
    fleet.vanish("a")
    taken = present(workflow.claim(b, ("junk",)))
    assert taken["id"] == c1, "freed within the next claim, then taken"
    assert taken["claimed_by"] == "process@b", "freed within the next claim, then taken"
    assert taken["attempts"] == 0, "freeing a dead claim costs no attempt"


@budget(load="db")
def test_with_locks_off_no_claim_is_freed(fleet):
    off = {**fleet.cfg, "lock": {"enabled": False}}
    _, (c1,) = fleet.series("S", chapters=1)
    a, b = fleet.machine("a", off), fleet.machine("b", off)
    workflow.claim(a, ("junk",))
    workflow.release_own_claims(b)
    assert workflow.claim(b, ("junk",)) is None, "with no run locks every owner would look dead: nothing is freed"
    assert fleet.row(c1)["claimed_by"] == "process@a", "with no run locks every owner would look dead: nothing is freed"


@budget(load="db")
def test_a_lock_held_in_another_database_keeps_no_claim_here(fleet, tmp_path):
    other_cfg = site_cfg(tmp_path / "other")
    _, (c1,) = fleet.series("S", chapters=1)
    with closing(db.connect(other_cfg)) as other, lock.held(other, other_cfg, ["process@a"]):
        with db.transaction(fleet.conn):
            fleet.conn.execute(
                "UPDATE chapter SET status = 'claimed', claimed_by = 'process@a', claim_count = 1 WHERE id = %s", (c1,)
            )
        assert present(workflow.claim(fleet.machine("b"), ("junk",)))["id"] == c1, "only a lock in this database counts"


def lost(fleet, cfg=None):
    """Machine a's claim of a chapter, then freed and taken by machine b (a slept): (a, a's stale row)."""
    _, (c1,) = fleet.series("S", chapters=1)
    a, b = fleet.machine("a", cfg), fleet.machine("b")
    stale = present(workflow.claim(a, ("junk",)))
    with db.transaction(fleet.conn):
        fleet.conn.execute(
            """UPDATE chapter SET claimed_by = %s, claim_count = claim_count + 1 WHERE id = %s""", (b.owner, c1)
        )
    return a, stale


@budget(load="db")
def test_a_run_that_lost_its_claim_records_nothing_and_spends_no_attempt(fleet):
    a, stale = lost(fleet)

    def stage(ctx, chapter):
        workflow.holds_claim(ctx, chapter)  # what a stage does before an act it cannot undo
        msg = "never reached"
        raise AssertionError(msg)

    assert isinstance(workflow.work(a, stale, stage), workflow.ClaimLost)
    after = fleet.row(stale["id"])
    assert (after["claimed_by"], after["attempts"], after["status"]) == ("process@b", 0, "claimed")
    workflow.work(a, stale, returns(workflow.Done()))  # a stage that reached its outcome all the same
    assert fleet.row(stale["id"])["stage"] == "junk", "its outcome is refused: its staged file is never published"


@budget(load="db")
def test_an_older_run_of_the_same_owner_never_passes_for_the_new_one(fleet):
    _, (c1,) = fleet.series("S", chapters=1)
    a = fleet.machine("a")
    stale = present(workflow.claim(a, ("junk",)))
    workflow.release_own_claims(a)  # the next run of a's owner starts
    fresh = present(workflow.claim(a, ("junk",)))
    assert fresh["claim_count"] == stale["claim_count"] + 1
    workflow.work(a, stale, returns(Failed("old run")))
    assert fleet.row(c1)["attempts"] == 0, "the old run's outcome is refused: same owner, older count"


@budget(load="db")
def test_a_lost_claim_puts_no_download_aside_and_adds_no_calibration_sample(fleet, tmp_path):
    a, stale = lost(fleet)
    download = tmp_path / "Ch 1.cbz"
    download.write_bytes(b"pages")
    with db.transaction(fleet.conn):
        fleet.conn.execute(
            "UPDATE chapter SET download_path = %s, content_digest = 'd' WHERE id = %s", (str(download), stale["id"])
        )
        handled.record(fleet.conn, stale["series_id"], "c0", "published", "d")
    stale = db_row(**{**dict(zip(stale.keys(), stale, strict=True)), "download_path": str(download)})
    with pytest.raises(workflow.ClaimLost):
        handled.put_aside(a, stale, "d")
    assert download.exists(), "no download is put aside under a lost claim"
    with pytest.raises(workflow.ClaimLost):
        calibrate.add(a.conn, stale, "colour", [], guard=lambda _conn: workflow.holds_claim(a, stale))
    assert one(fleet.conn, "SELECT count(*) FROM series_calibration") == 0


@budget(load="db", seconds=2)
def test_a_lost_claim_neither_notes_a_kept_notice_nor_puts_a_dropped_one_aside(fleet: Fleet):
    cfg: config.Config = {**fleet.cfg, "validate": {**fleet.cfg["validate"], "non_chapter_names": ["Author's Note"]}}
    a, stale = lost(fleet, cfg)
    download = config.abs_path(cfg["paths"]["downloads"]) / "Chapter 131 - Author's Note [END].cbz"
    download.parent.mkdir(parents=True, exist_ok=True)
    download.write_bytes(cbz())
    with db.transaction(fleet.conn):
        fleet.conn.execute(
            "UPDATE chapter SET download_path = %s WHERE id = %s", (config.to_stored(cfg, download), stale["id"])
        )
        fleet.conn.execute(
            "INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, 'pages', 'computed')",
            (stale["series_id"],),
        )
        page_store.save(fleet.conn, stale["id"], images.measure_pages(download))
    stale = {**dict(zip(stale.keys(), stale, strict=True)), "download_path": config.to_stored(cfg, download)}
    with pytest.raises(workflow.ClaimLost):
        notices.judge(a, stale)  # an end marker keeps it as a chapter, with a note
    assert fleet.row(stale["id"])["notes"] is None, "no note is written under a lost claim"
    with pytest.raises(workflow.ClaimLost):
        notices.drop(a, stale, download)
    assert download.exists(), "no notice is put aside under a lost claim"


@budget(load="db")
def test_a_failed_chapter_waits_5_minutes_on_every_machine_and_its_series_goes_on(fleet):
    _, (_c1, _c2) = fleet.series("S", chapters=2)
    a, b = fleet.machine("a"), fleet.machine("b")
    first = present(workflow.claim(a, ("junk",)))
    workflow.work(a, first, returns(Failed("bad page")))
    row = fleet.row(first["id"])
    assert row["retry_after"] == workflow.after(row["updated_at"], workflow.RETRY_PAUSE)
    other = present(workflow.claim(b, ("junk",)))
    assert other["id"] != first["id"], "no machine takes it during the pause; the series' other chapter goes on"
    workflow.work(b, other, returns(Waiting("junk_decision")))
    assert workflow.claim(b, ("junk",)) is None
    with db.transaction(fleet.conn):  # the pause is over
        fleet.conn.execute("UPDATE chapter SET retry_after = %s WHERE id = %s", (workflow.now(fleet.conn), first["id"]))
    assert workflow.claim(a, ("junk",)) is None, "never again in the run that failed it"
    assert present(workflow.claim(b, ("junk",)))["id"] == first["id"], "another run takes it once the pause is over"


@budget(load="db")
def test_retry_ends_the_pause_at_once(fleet):
    _, (c1,) = fleet.series("S", chapters=1)
    a = fleet.machine("a")
    workflow.work(a, workflow.claim(a, ("junk",)), returns(Failed("bad page")))
    with db.transaction(fleet.conn):
        fleet.conn.execute("UPDATE chapter SET status = 'needs_human' WHERE id = %s", (c1,))
    workflow.retry_chapter(fleet.conn, c1)
    assert fleet.row(c1)["retry_after"] is None
    assert present(workflow.claim(fleet.machine("b"), ("junk",)))["id"] == c1


@budget(load="db")
def test_every_stored_time_comes_from_the_servers_clock(fleet):
    fleet.series("S", chapters=1)
    a = fleet.machine("a")
    row = present(workflow.claim(a, ("junk",)))
    workflow.work(a, row, returns(Failed("bad page")))
    server = real_datetime.fromisoformat(workflow.now(fleet.conn))
    stored = real_datetime.fromisoformat(fleet.row(row["id"])["updated_at"])
    assert abs((server - stored).total_seconds()) <= 2, "the stored time is the server's"


def machine_clock_reads(source):
    """Return the lines of source that read the machine's clock as a datetime.

    datetime.now / utcnow / today or date.today, whether the class is imported (from datetime import datetime [as x])
    or reached through the module (import datetime [as x]).
    """
    tree = ast.parse(source)
    classes, modules = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            classes |= {a.asname or a.name for a in node.names if a.name in ("datetime", "date")}
        elif isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "datetime"}
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("now", "utcnow", "today"):
            continue
        owner = node.func.value
        if (isinstance(owner, ast.Name) and owner.id in classes) or (
            isinstance(owner, ast.Attribute)
            and owner.attr in ("datetime", "date")
            and isinstance(owner.value, ast.Name)
            and owner.value.id in modules
        ):
            lines.append(node.lineno)
    return lines


@budget(load="pure")
@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\ndatetime.now()",
        "from datetime import datetime as dt, timedelta\ndt.utcnow()",
        "import datetime\ndatetime.date.today()",
        "import datetime as d\nd.datetime.now(d.timezone.utc)",
    ],
)
def test_the_clock_check_finds_every_import_form(source):
    assert machine_clock_reads(source) == [2], "each way the code base imports datetime is seen"


@budget(load="fs")
def test_no_module_reads_the_machines_clock_as_a_datetime():
    root = Path(workflow.__file__).parent
    found = {
        p.relative_to(root).as_posix(): lines
        for p in sorted(root.rglob("*.py"))
        if (lines := machine_clock_reads(p.read_text(encoding="utf-8")))
    }
    assert found == {}, "every stored time comes from the server's clock (workflow.now), never the machine's"


@budget(load="pure")
def test_2_claims_of_1_chapter_never_share_a_scratch_folder_or_staged_file():
    assert workflow.claim_name({"id": 7, "claim_count": 1}) != workflow.claim_name({"id": 7, "claim_count": 2})


# ---------------------------------------------------------------- the command line


def argv(cfg, tmp_path, *command):
    path = tmp_path / "config.yaml"

    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return ["--config", str(path), *command]


@budget(load="db", seconds=2)
def test_a_second_process_on_1_host_exits_3_and_another_hosts_does_not_stop_it(tmp_path, capsys):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        with lock.held(conn, cfg, [workflow.owner("process")]):
            assert cli.main(argv(cfg, tmp_path, "process")) == 3
        with lock.held(conn, cfg, ["process@another-host"]):
            assert cli.main(argv(cfg, tmp_path, "process")) != 3, "another machine's process blocks nothing here"
    assert "process" in capsys.readouterr().err


@budget(load="db")
@pytest.mark.parametrize("command", [["process"], ["state", "reset", "--yes"]])
def test_a_command_with_locks_on_no_database_exits_4_and_creates_none(tmp_path, capsys, command):
    cfg = site_cfg(tmp_path, built=False)
    name = f"{cfg['database']['name']}_never"
    cfg: config.Config = {**cfg, "database": {**cfg["database"], "name": name}}
    assert cli.main(argv(cfg, tmp_path, *command)) == 4
    assert f"no database {name}" in capsys.readouterr().err
    assert not db.built(cfg), "nothing creates it"


@budget(load="db")
def test_state_reset_holds_its_lock_until_its_folders_are_emptied(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        state.adopt(cfg, conn)
    seen = []
    real = state.reset

    def watched(cfg, conn):
        real(cfg, conn)
        with closing(db.connect(cfg)) as other:
            try:
                with lock.held(other, cfg, ["intake"], lock.SHARED):
                    seen.append("a part could start")
            except lock.LockHeld:
                seen.append("held")

    monkeypatch.setitem(
        cli.COMMANDS,
        "state reset",
        cli.COMMANDS["state reset"].__class__(
            **{**cli.COMMANDS["state reset"].__dict__, "handler": lambda cfg, _args, conn: watched(cfg, conn)}
        ),
    )
    assert cli.main(argv(cfg, tmp_path, "state", "reset", "--yes")) == 0
    assert seen == ["held"], "no part starts until the reset has emptied its folders"
    assert Path(cfg["paths"]["staging"]).exists()
