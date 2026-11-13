"""The external image tools (SPEC "Conversion"): run one, its stderr in the error. Paths come from config `tools.*`
through the caller; nothing is searched on PATH. Copied from the bulk converter's optimizer/tools.py without its
config read, WinGet search and version listing."""

import logging
import subprocess
import time

log = logging.getLogger(__name__)

NAMES = ("avifenc", "avifdec", "jpegtran", "oxipng")  # the executables; tools.vship is a library (convert.vship)


class ToolError(subprocess.CalledProcessError):
    """A tool's non-zero exit, with its stderr in the message."""

    def __str__(self):
        err = self.stderr.decode(errors="replace") if isinstance(self.stderr, bytes) else (self.stderr or "")
        return f"{super().__str__()} stderr: {err.strip() or '(empty)'}"


def run(*args):
    """Run a tool (output captured); raises ToolError on a non-zero exit. Logs the command, exit and seconds at
    DEBUG."""
    args = [str(a) for a in args]
    t0 = time.perf_counter()
    p = subprocess.run(args, capture_output=True)
    log.debug("%s -> exit %d %.3fs", subprocess.list2cmdline(args), p.returncode, time.perf_counter() - t0)
    if p.returncode:
        raise ToolError(p.returncode, p.args, p.stdout, p.stderr)
