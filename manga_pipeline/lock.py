"""Run lock: one run at a time per project (SPEC "Run lock")."""

import os
from contextlib import contextmanager

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
def held(cfg):
    """Holds the run lock for the block when lock.enabled, raising LockHeld at once if another run holds it. It is an
    OS lock on the file, so the OS frees it when the process ends, crash included; a leftover file blocks nothing."""
    if not cfg["lock"]["enabled"]:
        yield
        return
    path = config.abs_path(cfg["lock"]["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
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
