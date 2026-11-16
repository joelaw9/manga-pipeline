"""Errors raised below the workflow layer and handled by it (SPEC "Parts", "Services")."""

import errno
from pathlib import Path

# A share path failing with these is the share being unreachable, not the chapter failing.
SHARE_ERRNOS = {errno.EIO, errno.ESTALE, errno.ETIMEDOUT}
# On Windows an SMB share's failures carry their cause in winerror: the network path or name is gone or no longer
# answers (53 bad net path, 59 unexpected network error, 64 net name deleted, 67 bad net name, 121 semaphore timeout,
# 1231 network unreachable).
SHARE_WINERRORS = {53, 59, 64, 67, 121, 1231}


class Unreachable(Exception):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """A service the work needs cannot be reached. From a stage: the chapter stays ready at no attempt, and the part
    claims no chapter at that stage for the rest of the run; raised outside a stage, it ends the run."""

    def __init__(self, service, reason=""):
        super().__init__(f"{service}: {reason}" if reason else service)
        self.service = service
        self.reason = reason


def dropped_share(error):
    """A FileNotFoundError that is a dropped SMB share, not a missing file (Windows reports both as ENOENT)."""
    return getattr(error, "winerror", None) in SHARE_WINERRORS


def exists(path):
    """Whether a file exists on the share. Path.exists answers False for an SMB share that dropped (WinError 53, 67
    come back as ENOENT); this raises it, so share_errors makes it Unreachable("share")."""
    try:
        Path(path).stat()
    except FileNotFoundError as e:
        if dropped_share(e):
            raise
        return False
    return True
