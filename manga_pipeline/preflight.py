"""Preflight (SPEC "Preflight"): the run's hard and soft requirements, checked before its work.

A check is a function of the config returning the problem text, or None when the requirement holds; it turns its own
errors into problem text. A new check is a function plus 1 line in CHECKS.
"""

import logging
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

from manga_pipeline import config

log = logging.getLogger(__name__)
HARD, SOFT = "hard", "soft"
MIB = 1024 * 1024


@dataclass(frozen=True)
class Check:
    name: str
    level: str  # HARD: the run cannot work without it; SOFT: it can, with a warning
    run: Callable[[dict], str | None]


class PreflightFailed(RuntimeError):
    pass


def python_version(cfg, version=sys.version_info):
    if version < (3, 13):
        return f"Python 3.13 or later is required, this is {version[0]}.{version[1]}"
    return None


def free_space(cfg):
    want = cfg["preflight"]["min_free_mb"]
    if want < 0:
        return f"preflight.min_free_mb must be 0 or more, got {want}"
    if want == 0:
        return None
    folder = config.abs_path(cfg["logging"]["dir"])
    try:
        free = shutil.disk_usage(folder).free // MIB
    except OSError as e:
        return f"cannot read free space at {folder}: {e}"
    if free < want:
        return f"{free} MiB free at {folder}, below preflight.min_free_mb {want}"
    return None


CHECKS = [
    Check("python version", HARD, python_version),
    Check("free disk space", SOFT, free_space),
]


def run(cfg, checks):
    """Run every check. Logs each soft problem as a warning and returns them; when any check is hard and failed,
    logs each and raises PreflightFailed naming all of them."""
    unknown = [c.name for c in checks if c.level not in (HARD, SOFT)]
    if unknown:
        raise ValueError(f"preflight check level must be HARD or SOFT: {', '.join(unknown)}")
    soft, hard = [], []
    for check in checks:
        problem = check.run(cfg)
        if problem is None:
            log.debug("%s: ok", check.name)
        elif check.level == HARD:
            log.error("%s: %s", check.name, problem)
            hard.append(f"{check.name}: {problem}")
        else:
            log.warning("%s: %s", check.name, problem)
            soft.append(f"{check.name}: {problem}")
    if hard:
        raise PreflightFailed(f"preflight failed: {'; '.join(hard)}")
    return soft
