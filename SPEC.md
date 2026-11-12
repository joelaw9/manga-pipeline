# manga_pipeline — Spec

Single reference for every settled decision. Settings live in `config.yaml`; this doc explains them. Update it in the same change as the behaviour it describes.

## Purpose

Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion

## Scope and constraints

## Config

`config.yaml.example` (committed, no secrets) is the schema for `config.yaml` (gitignored). Loading raises a config error naming the key when the file is missing, not YAML or not a mapping, or when a key is missing, unknown, or of another type than the example's. A null example value allows any type; an empty example mapping allows any keys. An integer stands in for a float; a boolean only for a boolean.

Each key comes from, in rising priority: `config.yaml`, an environment variable, a `--set KEY=VALUE` flag. The merged result is checked, so a key may be left out of `config.yaml` when a variable or flag supplies it; a key missing from all 3 is refused, and the error names its variable and flag. An empty file or an empty section reads as an empty mapping. Nothing writes `config.yaml`: a variable or flag holds for that run only.

- Every key whose example value is not a non-empty mapping can be set. Its variable is `MANGA_PIPELINE_` plus the key path in capitals, with `.` and `-` as `_` (`logging.keep_days`: `MANGA_PIPELINE_LOGGING_KEEP_DAYS`); names compare without case. A `MANGA_PIPELINE_` variable that names no key is refused, and so is an example in which 2 keys give 1 name.
- The text converts by the example value's type: a string or null keeps the text as given (a secret is never parsed); an integer takes digits with an optional `-`; a float a plain decimal; a boolean `true` or `false` in any case; a list or an empty mapping is parsed as YAML. A bad value is refused naming its variable or flag.

| Key | Meaning |
|---|---|
| `logging.dir` | log folder, relative to the project root |
| `logging.level` | lowest level logged: DEBUG, INFO, WARNING, ERROR |
| `logging.keep_days` | rotated daily files kept |
| `lock.enabled` | take the run lock (default true); false lets runs overlap |
| `lock.path` | run lock file, relative to the project root |
| `preflight.min_free_mb` | free space (MiB) on `logging.dir`'s drive below which preflight warns; 0 skips the check |

## Logging

One setup call, from the entry point. Records go to the console and to `<logging.dir>/application.log`. At the first write after midnight the file is renamed `application.log.YYYY-MM-DD` and a new one starts; only the newest `logging.keep_days` dated files are kept.

## Run lock

With `lock.enabled`, every command runs under the run lock at `lock.path`: taken after config is loaded and before logging is set up, freed when the command ends. A run that finds the lock held prints an error naming the lock file to stderr and exits 3 at once, without opening the log file; it never waits.

A lock file left by a run that died does not block the next run. Where the lock is a file holding the holder's pid (no OS file lock), 2 runs starting in the same instant against a dead holder's file can both take it.

## Preflight

After config, the run lock and logging, and before the work, the run checks its requirements. Each check is hard (the run cannot work without it) or soft (the run can, but something is off), and every check runs. A soft problem is logged as a warning and the run goes on. A hard problem is printed to stderr and logged; once every check has run, the run exits 4. A check that cannot measure what it checks reports that as its problem.

| Check | Kind | Problem when |
|---|---|---|
| free disk space | soft | less than `preflight.min_free_mb` MiB free on `logging.dir`'s drive (0 skips the check; a negative value is itself the problem) |
| runtime version | hard | Python older than 3.13 |

The runtime check covers a runtime that can load the program; one too old to parse it fails before preflight runs.

## CLI

`--set KEY=VALUE` (repeatable) sets a config key for the run (SPEC "Config"). Exit codes: 0 done, 2 usage or config error, 3 another run holds the run lock, 4 a hard preflight check failed.

## Tests

Tests never read the live `config.yaml` (they get `tests/test_config.yaml`, paths in a per-run temp dir); a guard refuses the live file. Tests never open a network connection: a guard refuses every connection. The developer's `MANGA_PIPELINE_*` variables never reach a test, and each test starts with no process config.

## Decisions
