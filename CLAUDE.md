# manga_pipeline

## Project overview

Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion

The spec is the source of truth for every decision: `SPEC.md` is its index (each section in 1 or 2 lines), and the rules of each section are in the `docs/spec/` file it names. Read the relevant section before changing behaviour; update it in the same change.

## Core commands

Setup (system Python 3.13+, no packaging; pip 25.1+ for `--group`):

```bash
python -m pip install --group dev
```

Tests (pytest; each run ends with a per-test timing report and writes `state/test-timings.json`):

```bash
python -m pytest -q
```

While iterating, between implementation phases and commits: reruns only the tests whose Python code changed since the last `--testmon` run (map in `.testmondata`). It does not see edits to non-Python files (`config.yaml.example`, `tests/test_config.yaml`) or to `tests/timing.py`; after one of those, run the full suite. The full suite runs before every merge.

```bash
python -m pytest -q --testmon
```

```bash
python -m pytest -q tests/test_config.py
```

Lint and format (both must pass before a change is done):

```bash
python -m ruff check .
```

```bash
python -m ruff format .
```

Run:

```bash
python -m manga_pipeline.cli --help
```

## Layout

| Path | Role |
|---|---|
| `manga_pipeline/cli.py` | entry point: argparse subcommands (global `--config`, `--set`); `COMMANDS`, the command table: each command's locks, log file, preflight checks and handler; runs config (`config.init`), locks, logging, preflight, handler |
| `manga_pipeline/config.py` | `load` (file, then `ENV_PREFIX` variables, then `--set`; validated against `config.yaml.example`), `init`, `cached`, `reset`, `abs_path` |
| `manga_pipeline/db.py` | `connect(cfg)` (WAL, 30 s busy timeout, foreign keys, autocommit, `sqlite3.Row` rows; applies missing schema steps, refuses an unknown one: `SchemaError`), `read_only(cfg)` (opens an existing database without writing; `SchemaError` unless its steps are exactly the program's), `peek_id(cfg)` (the id, read without checking or applying anything), `transaction(conn)`, `database_id(conn)`, `WORKFLOW_TABLES` |
| `manga_pipeline/schema/` | schema steps `NNNN_name.sql`, applied in name order and recorded in `schema_step` |
| `manga_pipeline/workflow.py` | the only writer of chapter stage/status/waits/attempts/claims and `series.status`: `claim` (priority, scope, junk before convert), `work` (stage outcome `Done` / `Waiting` / `Failed`, `Unreachable` skips the stage for the run; 1 log line per outcome), `drain`, `end_waits`, `release_own_claims`, `needs_human`, `retry_chapter` / `retry_series` (`Refused`: exit 2), `reset`, `run_part` (run records in `meta`), `share_errors`, `in_scope` |
| `manga_pipeline/intake.py`, `process.py`, `export.py` | the 3 part loops: `run(cfg)`; their order of work (SPEC "Parts") |
| `manga_pipeline/status.py` | `status`: read-only report (`db.read_only`), printed |
| `manga_pipeline/state.py` | `state reset` (`workflow.reset`, then empties staging but its owner file, and scratch) and `state adopt` (writes the owner file) |
| `manga_pipeline/stages/` | 1 module per stage, `run(ctx, chapter)` returns an outcome; Phase 1 stubs (junk's `.wait` marker is a test hook) |
| `manga_pipeline/logs.py` | `setup(cfg, command)`: console plus daily-rotated `<command>.log`, or console only for `None` |
| `manga_pipeline/lock.py` | `held(cfg, *names)`: `<lock.dir>/<name>.lock` per name, all or nothing, an OS file lock (`msvcrt` on Windows, `fcntl` elsewhere); `LockHeld` |
| `manga_pipeline/preflight.py` | `run(cfg, checks)` (`Check(name, HARD or SOFT, fn)`): soft problems warn, hard ones raise `PreflightFailed` (exit 4); ships `PYTHON_VERSION`, `DATABASE_LOCATION`, `FOLDER_LAYOUT`, `SHARE_FOLDERS_PRESENT`, `WORKFLOW_SETTINGS`, `SHARE_OWNERSHIP`, `STAGING_FOLDER` (hard), `FREE_SPACE` (soft); `owner_file(cfg)` |
| `tests/__init__.py` | points `config.load` at `tests/test_config.yaml`; refuses the live config |
| `tests/conftest.py` | no-network guard, per-test timing report |
| `tests/builders.py` | fixture builders: `site_cfg(tmp_path)`, series and chapter rows, fake downloads |
| `tests/timing.py` | `@budget` per-test time budgets (copied from the unit-tests skill; project code now) |

## Python standards

- Dependencies go in `pyproject.toml` `[dependency-groups]`: `runtime` for the program, `dev` for tools.
- Read config through `config.cached()` (or `config.load(path)` for another file); resolve configured relative paths with `config.abs_path`.
- Log through `logging.getLogger(__name__)`; the entry point names its logger `"manga_pipeline.cli"` (run by `python -m`, its `__name__` is `__main__`, outside the package logger).
- Tests are pytest style: plain functions, fixtures, `parametrize`, bare `assert` with a message stating the rule. Tag each with `@budget(load=...)` from `tests/timing.py`.

## Code standards

- Simplest design that meets the requirement. One path per job: no fallbacks or alternate code paths that silently take over when the primary one fails.
- Fail fast: validate inputs at the boundary and raise a clear error naming what is wrong.
- Read config only through the config module: the entry point calls its `init` once (with `--set`), everything else reads `cached`. It merges `config.yaml`, `MANGA_PIPELINE_*` environment variables and `--set`, and checks the result against `config.yaml.example` (SPEC "Config").
- Log only through the logs module's logger; set up once, from the entry point (SPEC "Logging"). No `print` except a command's own output and its errors on stderr.
- Each command takes the locks its row in the command table names, once in the entry point (SPEC "Run lock"); never take a lock anywhere else or wait for one.
- A new requirement check is a function and its `Check` in the preflight module, plus its place in the command table's check lists in `cli.py` (SPEC "Preflight"): hard when the run cannot work without it, soft when it only warns. The function returns the problem text, or nothing when the requirement holds, and turns its own errors into problem text.
- Tests stay offline and never read the live `config.yaml` (SPEC "Tests"). Test only situations that occur in real use.
- Module docstrings or header comments name the SPEC section they implement. Comment only where the reason isn't obvious.
- Run the linter and formatter before finishing a change; both must pass.

## Merging

- `main` changes only through a pull request; its required `tests` check runs lint, format and the full test suite (`.github/workflows/tests.yml`; the rule is `.github/rulesets/main.json`, applied as README "Merging" says).
- Local merge commits and squash commits run the full test suite first: `.githooks/pre-merge-commit`, and `.githooks/pre-commit` for a commit that concludes a merge or a squash, both run `.githooks/full-suite`. A fast-forward runs no hook: merge with a plain `git merge BRANCH`, never `--ff` / `--ff-only` or `git pull` of another branch into it. Never skip the hooks with `--no-verify`; fix the failing test instead.

## Known gotchas

- `config.yaml` is gitignored and may hold secrets: never print, log, copy or commit it. A new key goes in `config.yaml.example`, `tests/test_config.yaml` and `config.yaml`; loading refuses a mismatch. A secret may instead come from its environment variable.
- `.gitattributes` forces LF line endings; keep every file LF.
- `state/` is local runtime output (logs, locks, scratch, test timings, and by default the database) and is gitignored: nothing there but the database is a source of truth.
