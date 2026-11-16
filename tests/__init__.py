"""Tests never read the live config.yaml (SPEC "Tests").

Importing this package points config.load (and with it config.init and config.cached) at tests/test_config.yaml,
whose "$TMP/..." values land in one temp dir per test run, and removes every MANGA_PIPELINE_* and PG* variable from
the environment so the developer's shell cannot change a result. Asking for the live config.yaml raises LiveConfigRead.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

from manga_pipeline import config

TEST_CONFIG = Path(__file__).with_name("test_config.yaml")
LIVE_CONFIG = (config.ROOT / "config.yaml").resolve()
TMP = Path(tempfile.mkdtemp(prefix="manga_pipeline-tests-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
real_load = config.load


class LiveConfigRead(AssertionError):
    pass


def _in_tmp(value):
    if isinstance(value, dict):
        return {k: _in_tmp(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_in_tmp(v) for v in value]
    if isinstance(value, str) and value.startswith("$TMP/"):
        return str(TMP / value.removeprefix("$TMP/"))
    return value


def _load(path=None, example=None, env=None, sets=()):
    p = Path(path) if path is not None else TEST_CONFIG
    if p.resolve() == LIVE_CONFIG:
        msg = f"a test read the live {LIVE_CONFIG}; use tests/test_config.yaml"
        raise LiveConfigRead(msg)
    cfg = real_load(p, example, env, sets)
    return _in_tmp(cfg) if p == TEST_CONFIG else cfg


# PG* too: libpq would read them (SPEC "Tests"); the CLI subprocesses a test starts inherit this environment.
for name in [n for n in os.environ if n.upper().startswith((f"{config.ENV_PREFIX}_", "PG"))]:
    del os.environ[name]
config.load = _load
config.reset()
