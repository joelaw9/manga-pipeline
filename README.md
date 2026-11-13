# manga_pipeline

Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion

## Setup

Python 3.13+ (system install) and pip 25.1+. Installs the runtime and dev dependencies listed in `pyproject.toml`:

```bash
python -m pip install --group dev
```

## Usage

```bash
python -m manga_pipeline.cli --help
```

Commands: `intake`, `process`, `export` (the 3 parts, each on its own timer), `status`, `retry CHAPTER_ID` / `retry --series ID`, `state reset --yes`, `state adopt --yes`, `smoke` (checks Suwayomi and Kavita with reads only; exit 1 when a check fails). `--config` and `--set` go before the command. A part refuses to run (exit 4) until this database owns the share: run `state adopt --yes` once, with the share mounted.

## Tests

```bash
python -m pytest -q -n auto
```

Affected tests only, for iterating (pytest-testmon); the full suite runs before a merge:

```bash
python -m pytest -q --testmon
```

## Configuration

`config.yaml` holds this machine's settings and is not committed. It starts as a copy of `config.yaml.example`, which is committed and holds no secrets. Every key in `config.yaml` must also be in the example, with the same type: the program refuses to start otherwise and names the key.

Any key can also come from an environment variable, `MANGA_PIPELINE_` plus the key in capitals with `.` as `_` (e.g. `MANGA_PIPELINE_LOGGING_LEVEL`), or for one run from `--set logging.level=DEBUG`. A flag beats the variable, which beats the file; a key supplied that way may be left out of `config.yaml`.

## Merging

`main` changes only through a pull request whose `tests` check (`.github/workflows/tests.yml`: lint, format, the full test suite) passes. Once the repo is on GitHub, apply the rule once with the GitHub CLI from the project folder. GitHub enforces it on public repos, and on private ones only with GitHub Pro or a paid organization plan:

```bash
gh api --method POST "repos/{owner}/{repo}/rulesets" --input .github/rulesets/main.json
```

Locally, git hooks in `.githooks/` run the full test suite before every merge commit and squash commit; a failing suite refuses the commit. A fast-forward creates no commit and runs no hook: `git merge --ff` / `--ff-only` and `git pull` (fast-forward only here) move a branch without the suite, so merge branches with a plain `git merge BRANCH`. The scaffold turns the hooks on; a fresh clone needs:

```bash
git config core.hooksPath .githooks
```

```bash
git config merge.ff false
```

```bash
git config pull.ff only
```

`merge.ff false` makes a plain `git merge` create a merge commit, so the hook runs; a squash merge then needs `git merge --squash --ff BRANCH` (the suite runs when you commit it). An abandoned squash leaves `.git/SQUASH_MSG`, and every commit runs the suite until `git reset` removes it.

## Preflight

Before its work the program checks its requirements (SPEC "Preflight"): a hard problem stops the run with exit code 4, a soft one (e.g. less free disk space than `preflight.min_free_mb`) is logged as a warning.

## Logs

Log records go to the console. `intake`, `process` and `export` also write them to their own file in the `logging.dir` folder (`intake.log`, `process.log`, `export.log`). A new file starts each midnight; the newest `logging.keep_days` dated files of each are kept.
