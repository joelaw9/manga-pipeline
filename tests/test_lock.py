"""lock.held: run locks are session locks of the database (SPEC "Run lock").

1 session per lock name, several names all or nothing, the idle lock shared or exclusive, none when lock.enabled is
false; a lock dies with its session.
"""

from contextlib import closing

import pytest

from manga_pipeline import config, db, lock
from manga_pipeline.db import sql

from tests.builders import one, site_cfg
from tests.timing import budget


@pytest.fixture
def cfg(tmp_path):
    return site_cfg(tmp_path)


def held(_cfg, conn, name):
    """Whether some session holds the run lock `name` (lock.held_sql), asked on conn."""
    return one(conn, sql.SQL("SELECT {}").format(lock.held_sql(sql.Placeholder())), (name,))


@budget(load="db")
def test_a_second_session_is_refused_a_held_lock_naming_it(cfg):
    with (
        closing(db.connect(cfg)) as a,
        closing(db.connect(cfg)) as b,
        lock.held(a, cfg, ["intake"]),
        pytest.raises(lock.LockHeld, match="another run holds the lock 'intake'"),
        lock.held(b, cfg, ["intake"]),
    ):
        pass


@budget(load="db")
def test_different_locks_are_held_at_once_and_freed_after_the_block(cfg):
    with closing(db.connect(cfg)) as a, closing(db.connect(cfg)) as b:
        with lock.held(a, cfg, ["process@one"]), lock.held(b, cfg, ["process@two"]):
            assert held(cfg, a, "process@one"), "both machines' locks are seen"
            assert held(cfg, a, "process@two"), "both machines' locks are seen"
        assert not held(cfg, a, "process@one"), "freed when the block ends"
        with lock.held(b, cfg, ["process@one"]):
            pass


@budget(load="db")
def test_several_locks_are_all_or_nothing(cfg):
    with closing(db.connect(cfg)) as a, closing(db.connect(cfg)) as b, lock.held(a, cfg, ["export"]):
        with pytest.raises(lock.LockHeld, match="'export'"), lock.held(b, cfg, ["intake", "export"]):
            pass
        assert not held(cfg, a, "intake"), "a lock taken before the held one was found is freed again"


@budget(load="db")
def test_parts_share_the_idle_lock_and_a_state_command_needs_it_alone(cfg):
    with closing(db.connect(cfg)) as a, closing(db.connect(cfg)) as b:
        with (
            lock.held(a, cfg, ["intake"], lock.SHARED),
            lock.held(b, cfg, ["export"], lock.SHARED),
            closing(db.connect(cfg)) as c,
            pytest.raises(lock.LockHeld, match="a running command holds"),
            lock.held(c, cfg, [], lock.EXCLUSIVE),
        ):
            pass
        with (
            lock.held(a, cfg, [], lock.EXCLUSIVE),
            pytest.raises(lock.LockHeld),
            lock.held(b, cfg, ["intake"], lock.SHARED),
        ):
            pass


@budget(load="db")
def test_a_lock_dies_with_its_session(cfg):
    with closing(db.connect(cfg)) as b:
        a = db.connect(cfg)
        with lock.held(a, cfg, ["process@gone"]):
            a.close()  # the machine vanished: its session ends
            for _ in range(50):  # the server frees the lock once it has seen the session end
                if not held(cfg, b, "process@gone"):
                    break
            assert not held(cfg, b, "process@gone"), "a closed session holds no lock"
        with lock.held(b, cfg, ["process@gone"]):
            pass


@budget(load="db")
def test_locks_of_another_database_are_not_held_here(cfg, tmp_path):
    other_cfg = site_cfg(tmp_path / "other")
    with (
        closing(db.connect(other_cfg)) as other,
        closing(db.connect(cfg)) as here,
        lock.held(other, other_cfg, ["process@elsewhere"]),
    ):
        assert not held(cfg, here, "process@elsewhere"), "a lock is this database's only"
        with lock.held(here, cfg, ["process@elsewhere"]):
            pass


@budget(load="db")
def test_no_lock_when_locks_are_off(cfg: config.Config):
    off: config.Config = {**cfg, "lock": {**config.cached()["lock"], "enabled": False}}
    with (
        closing(db.connect(cfg)) as a,
        closing(db.connect(cfg)) as b,
        lock.held(a, off, ["intake"], lock.EXCLUSIVE),
        lock.held(b, off, ["intake"], lock.EXCLUSIVE),
    ):
        assert not held(cfg, a, "intake")


@budget(load="db")
def test_idle_mode_is_checked(cfg: config.Config):
    with (
        closing(db.connect(cfg)) as conn,
        pytest.raises(ValueError, match="idle must be"),
        lock.held(conn, cfg, [], "sometimes"),
    ):
        pass
