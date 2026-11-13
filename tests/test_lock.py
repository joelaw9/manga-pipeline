"""lock.held: 1 run per lock name, several names all or nothing, none when lock.enabled is false (SPEC "Run lock")."""

import pytest

from manga_pipeline import config, lock

from tests.timing import budget


def with_lock(**changes):
    return {**config.cached(), "lock": {**config.cached()["lock"], **changes}}


def lock_file(name):
    return config.abs_path(config.cached()["lock"]["dir"]) / f"{name}.lock"


@budget(load="fs")
def test_second_run_of_a_command_is_refused_naming_its_lock_file():
    with lock.held(config.cached(), "intake"):
        with pytest.raises(lock.LockHeld) as refused:
            with lock.held(config.cached(), "intake"):
                pass
    assert str(refused.value) == f"another run holds {lock_file('intake')}"


@budget(load="fs")
def test_different_commands_run_at_once():
    with lock.held(config.cached(), "intake"):
        with lock.held(config.cached(), "process"):
            pass


@budget(load="fs")
def test_lock_is_free_after_the_block():
    with lock.held(config.cached(), "export"):
        pass
    with lock.held(config.cached(), "export"):
        pass


@budget(load="fs")
def test_leftover_lock_file_blocks_nothing():
    path = lock_file("intake")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    with lock.held(config.cached(), "intake"):
        pass


@budget(load="fs")
def test_several_locks_are_all_or_nothing():
    names = ("intake", "process", "export", "junk-review")
    with lock.held(config.cached(), "export"):
        with pytest.raises(lock.LockHeld, match="export.lock"):
            with lock.held(config.cached(), *names):
                pass
        with lock.held(config.cached(), "intake"):
            pass  # taken before export was found held, and freed again
    with lock.held(config.cached(), *names):
        with pytest.raises(lock.LockHeld, match="junk-review.lock"):
            with lock.held(config.cached(), "junk-review"):
                pass


@budget(load="pure")
def test_disabled_lock_lets_runs_overlap():
    cfg = with_lock(enabled=False)
    with lock.held(cfg, "intake"):
        with lock.held(cfg, "intake"):
            pass
