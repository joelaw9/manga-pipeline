"""Run locks: 1 per command name, <lock.dir>/<name>.lock (SPEC "Run lock")."""

import os
from contextlib import ExitStack, contextmanager

from manga_pipeline import config

if os.name == "nt":
    import msvcrt

    HELD = PermissionError  # msvcrt's lock violation; any other OSError is a real fault
else:
    import fcntl

    HELD = BlockingIOError


class LockHeld(RuntimeError):
    pass


@contextmanager
def held(cfg, *names):
    """Holds the named locks for the block when lock.enabled (no names: none). All or nothing: when another run holds
    one, the ones already taken are freed and LockHeld names the held one, at once. Each is an OS lock on its file, so
    the OS frees it when the process ends, crash included; a leftover file blocks nothing."""
    if not cfg["lock"]["enabled"]:
        yield
        return
    folder = config.abs_path(cfg["lock"]["dir"])
    folder.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(_held_one(folder / f"{name}.lock"))
        yield


@contextmanager
def _held_one(path):
    with open(path, "a+b") as f:
        try:
            _lock(f)
        except HELD as e:
            raise LockHeld(f"another run holds {path}") from e
        try:
            yield
        finally:
            _unlock(f)


def _lock(f):
    f.seek(0)
    if os.name == "nt":
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f):
    f.seek(0)
    if os.name == "nt":
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_UN)
