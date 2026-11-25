"""scripts/queue_first.py (D-8): the Reading collection's series, then the open-download series, are queued first.

Through the program's own queue (conversions.queue), under its cap, with no lock; Kavita refusing, or no collection
titled "Reading", exits 1 and queues nothing.
"""

from contextlib import closing

import pytest

from manga_pipeline import conversions, db, intake, pause
from scripts import queue_first

from tests.builders import config_file
from tests.timing import budget
from tests.world import LIBRARY_ID, World, cbz


@pytest.fixture
def world(tmp_path):
    """3 adopted series, A, B and C (2 files each), no conversion queued; B has an open download."""
    w = World(tmp_path)
    for name in ("A", "B", "C"):
        folder = w.library / name
        folder.mkdir(parents=True)
        for n in (1, 2):
            (folder / f"Ch.{n}.cbz").write_bytes(cbz(seed=f"{name}{n}", pages=4))
        w.kavita.add_series(name, name, LIBRARY_ID)
    w.download(w.manga("B"), "Chapter 9")
    cap(w, 0)
    w.run(intake)
    assert w.rows("SELECT count(*) FROM published_file WHERE origin = 'library'") == [(6,)], "set up: adopted"
    return w


def cap(world, n):
    world.cfg = {**world.cfg, "export": {**world.cfg["export"], "rewrites_open": n}}


def reading(world, *names, title="Reading"):
    """Make the API key's user's collection `title` hold the named series."""
    kids = [k for k, s in world.kavita.series_rows.items() if s["name"] in names]
    world.kavita.collection_rows = {2: {"title": title, "series": kids}, 3: {"title": "Favourites", "series": []}}


def queued(world):
    """Return {series name: conversions queued}."""
    rows = world.rows(
        """SELECT s.name, count(*) FROM chapter c JOIN published_file p ON p.id = c.published_file_id
        JOIN series s ON s.id = c.series_id GROUP BY s.name"""
    )
    return dict(rows)


def front_load(world):
    with closing(db.connect(world.cfg)) as conn:
        return queue_first.front_load(world.cfg, conn, world.kavita)


def ids(world):
    return dict(world.rows("SELECT name, id FROM series"))


@budget(load="db", seconds=8)
def test_reading_then_open_download_series_are_queued_first_within_the_cap(world):
    reading(world, "C")
    cap(world, 3)
    lines = front_load(world)
    assert queued(world) == {"C": 2, "B": 1}, "Reading first, then the series with an open download; A waits"
    sid = ids(world)
    assert lines == [
        f"series {sid['C']} (C): 2 queued, 0 left (the cap is full)",
        f"series {sid['B']} (B): 1 queued, 1 left (the cap is full)",
        "3 queued over 2 series; 1 left (the cap is full)",
    ]


@budget(load="db", seconds=8)
def test_a_full_cap_queues_nothing_and_says_so(world):
    reading(world, "C")
    lines = front_load(world)
    assert queued(world) == {}
    assert lines[-1] == "0 queued over 2 series; 4 left (the cap is full)"


@budget(load="db", seconds=8)
def test_the_script_and_intake_together_never_exceed_the_cap(world):
    reading(world, "C")
    cap(world, 3)
    front_load(world)
    world.run(intake)
    open_ = world.rows(db.sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(conversions.OPEN).as_string(None))
    assert open_ == [(3,)], "the queue's transactions re-check the cap"
    assert queued(world) == {"C": 2, "B": 1}, "intake found no room left"


def main(world, tmp_path, monkeypatch):
    monkeypatch.setattr(queue_first.kavita_client, "from_config", lambda _cfg: world.kavita)
    return queue_first.main(["--config", str(config_file(tmp_path, world.cfg))])


@budget(load="db", seconds=8)
def test_kavita_refusing_exits_1_and_queues_nothing(world, tmp_path, monkeypatch, capsys):
    reading(world, "C")
    cap(world, 3)
    world.kavita.refuse_key = True
    assert main(world, tmp_path, monkeypatch) == 1
    assert "the API key was refused" in capsys.readouterr().err
    assert queued(world) == {}


@budget(load="db", seconds=8)
def test_no_collection_named_reading_exits_1_naming_it(world, tmp_path, monkeypatch, capsys):
    reading(world, "C", title="Read later")
    cap(world, 3)
    assert main(world, tmp_path, monkeypatch) == 1
    assert '0 Kavita collections are titled "Reading"' in capsys.readouterr().err
    assert queued(world) == {}


@budget(load="db", seconds=8)
def test_the_reading_collection_is_matched_without_case(world, tmp_path, monkeypatch, capsys):
    reading(world, "A", title="READING")
    cap(world, 2)
    assert main(world, tmp_path, monkeypatch) == 0
    assert queued(world) == {"A": 2}
    assert capsys.readouterr().out.endswith("2 queued over 2 series; 2 left (the cap is full)\n")


@budget(load="db", seconds=8)
def test_a_scheduled_run_queues_nothing_while_paused_and_a_run_by_hand_does(world, tmp_path, monkeypatch, capsys):
    reading(world, "C")
    cap(world, 3)
    with closing(db.connect(world.cfg)) as conn:
        pause.set_pause(conn)
    monkeypatch.setattr(queue_first.kavita_client, "from_config", lambda _cfg: world.kavita)
    path = str(config_file(tmp_path, world.cfg))
    assert queue_first.main(["--scheduled", "--config", path]) == 0
    assert "automation paused since" in capsys.readouterr().out
    assert queued(world) == {}, "the intake drop-in's run does nothing while the automation is paused"
    assert queue_first.main(["--config", path]) == 0
    assert queued(world) == {"C": 2, "B": 1}, "a run by hand works during a pause"
