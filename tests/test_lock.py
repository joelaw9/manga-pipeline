"""lock.held: one run at a time, unless lock.enabled is false (SPEC "Run lock")."""

import pytest

from manga_pipeline import config, lock

from tests.timing import budget


def with_lock(**changes):
    return {**config.cached(), "lock": {**config.cached()["lock"], **changes}}


@budget(load="fs")
def test_second_run_is_refused_while_held():
    with lock.held(config.cached()):
        with pytest.raises(lock.LockHeld, match="another run holds"):
            with lock.held(config.cached()):
                pass


@budget(load="fs")
def test_lock_is_free_after_the_block():
    with lock.held(config.cached()):
        pass
    with lock.held(config.cached()):
        pass


@budget(load="fs")
def test_leftover_lock_file_blocks_nothing():
    path = config.abs_path(config.cached()["lock"]["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    with lock.held(config.cached()):
        pass


@budget(load="pure")
def test_disabled_lock_lets_runs_overlap():
    cfg = with_lock(enabled=False)
    with lock.held(cfg):
        with lock.held(cfg):
            pass
