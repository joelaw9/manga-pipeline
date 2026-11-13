# manga_pipeline spec — Config, logging, locks, preflight, CLI, tests

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Config

`config.yaml.example` (committed, no secrets) is the schema for `config.yaml` (gitignored). Loading raises a config error naming the key when the file is missing, not YAML or not a mapping, or when a key is missing, unknown, or of another type than the example's. A file that is not YAML is reported by its line and column only: no error shows any of the file's content, which may hold a secret. A null example value allows any type; an empty example mapping allows any keys. An integer stands in for a float; a boolean only for a boolean.

Each key comes from, in rising priority: `config.yaml`, an environment variable, a `--set KEY=VALUE` flag. The merged result is checked, so a key may be left out of `config.yaml` when a variable or flag supplies it; a key missing from all 3 is refused, and the error names its variable and flag. An empty file or an empty section reads as an empty mapping. Nothing writes `config.yaml`: a variable or flag holds for that run only.

- Every key whose example value is not a non-empty mapping can be set. Its variable is `MANGA_PIPELINE_` plus the key path in capitals, with `.` and `-` as `_` (`logging.keep_days`: `MANGA_PIPELINE_LOGGING_KEEP_DAYS`); names compare without case. A `MANGA_PIPELINE_` variable that names no key is refused, and so is an example in which 2 keys give 1 name.
- The text converts by the example value's type: a string or null keeps the text as given (a secret is never parsed); an integer takes digits with an optional `-`; a float a plain decimal; a boolean `true` or `false` in any case; a list or an empty mapping is parsed as YAML. A bad value is refused naming its variable or flag.

| Key | Meaning |
|---|---|
| `logging.dir` | log folder, relative to the project root |
| `logging.level` | lowest level logged: DEBUG, INFO, WARNING, ERROR |
| `logging.keep_days` | rotated daily files kept |
| `lock.enabled` | take the command's lock (default true); false lets runs overlap |
| `lock.dir` | folder of the lock files, relative to the project root |
| `preflight.min_free_mb` | free space (MiB) on `logging.dir`'s drive below which preflight warns; 0 skips the check |
| `database.path` | the SQLite database file, relative to the project root; on local disk, never under a share folder |
| `paths.downloads` | Suwayomi's download root, the folder holding `mangas` (on the share) |
| `paths.library` | root of the Kavita library the pipeline publishes into (on the share) |
| `paths.staging` | staging folder for finished chapters (on the share, outside every library root) |
| `paths.retention` | where removed downloads are kept for `export.retention_days` (on the share, same filesystem as downloads, outside downloads and every library root) |
| `paths.naming_overrides` | the work folder of per-series volume overrides, `<series folder name>.yaml`; default `rules/naming` in the project, which the repository keeps empty (its files are the instance's, gitignored) |
| `paths.scratch` | scratch folder for the chapter in hand (local disk) |
| `paths.kavita_path_map` | Kavita path prefix to local path prefix, for the paths Kavita reports (SPEC "Services"); not empty |
| `workflow.attempt_limit` | failed attempts after which a chapter is `needs_human` |
| `series.only` | names of the only series to work; empty = all |
| `suwayomi.url` | Suwayomi's base URL |
| `kavita.url` | Kavita's base URL |
| `kavita.api_key` | Kavita API key of an admin user (secret: null in the example) |
| `kavita.library_id` | id of the Kavita library the pipeline works; series of other libraries are out of scope |
| `kavita.rescan_after_minutes` | minutes after a series scan request before chapters still not indexed get another (SPEC "Publishing") |
| `export.retention_days` | days a removed download stays in `paths.retention`; 0 deletes it at once |
| `tools.avifenc`, `tools.avifdec`, `tools.jpegtran`, `tools.oxipng` | the image tools' executables, per machine (relative = project root); nothing is searched on `PATH` (SPEC "Conversion") |
| `tools.vship` | the Vship scorer library (`libvship.so`, or the Windows DLL), per machine |
| `convert.workers` | processes that encode and decode one chapter's pages |
| `convert.min_scratch_free_mb` | free space (MiB) on `paths.scratch`'s drive below which `process` does not run |

## Logging

One setup call, from the entry point. Records go to the console and, for a command that runs under its own lock, to `<logging.dir>/<command>.log` (`intake.log`, `process.log`, `export.log`, `junk-review.log`), so commands running at the same time never share a file. At the first write after midnight the file is renamed `<command>.log.YYYY-MM-DD` and a new one starts; only the newest `logging.keep_days` dated files of each command are kept. Every other command logs to the console only.

## Run lock

Each command names the lock it runs under, or none: `intake`, `process`, `export` and `junk review` each have their own (`<lock.dir>/<command>.lock`); `status`, `retry`, `smoke`, `junk list`, `junk show` and `junk decide` take none, so they work while a part is active; `state reset` and `state adopt` take all 4, so they run only while nothing else does. With `lock.enabled`, a command's lock is taken after config is loaded and before logging is set up, and freed when the command ends. A command that finds its lock held prints an error naming the lock file to stderr and exits 3 at once, without opening the log file; it never waits.

A lock file left by a run that died does not block the next run. Where the lock is a file holding the holder's pid (no OS file lock), 2 runs starting in the same instant against a dead holder's file can both take it.

## Preflight

After config, the run lock and logging, and before the work, the run checks its requirements. Each check is hard (the run cannot work without it) or soft (the run can, but something is off), and every check runs. A soft problem is logged as a warning and the run goes on. A hard problem is printed to stderr and logged; once every check has run, the run exits 4. A check that cannot measure what it checks reports that as its problem. Each command has its own list of checks (the Runs column): `intake`, `process` and `export` (parts), `state reset` (reset), `state adopt` (adopt; it writes the owner file, so it checks the staging folder in place of share ownership), `smoke`, `series link`; the other commands without a lock run none.

| Check | Kind | Runs | Problem when |
|---|---|---|---|
| free disk space | soft | parts, reset, adopt | less than `preflight.min_free_mb` MiB free on `logging.dir`'s drive (0 skips the check; a negative value is itself the problem) |
| runtime version | hard | parts, reset, adopt | Python older than 3.13 |
| database location | hard | parts, reset, adopt | `database.path` lies under `paths.downloads`, `paths.library` or `paths.staging` |
| share ownership | hard | parts, reset | the owner file in `paths.staging` is missing, or holds another database's id (SPEC "State") |
| staging folder | hard | adopt | `paths.staging` is not an existing folder |
| folder layout | hard | parts, reset, adopt | `paths.staging` or `paths.scratch` (the folders `state reset` empties) equals or contains `paths.downloads`, `paths.library`, `paths.retention`, the database file, `logging.dir`, `lock.dir`, the project root or the other of the 2, or lies inside `paths.downloads` or `paths.library`; `paths.retention` lies inside `paths.downloads` or `paths.library`, or contains either |
| share folders | hard | parts, reset, adopt | `paths.downloads`, its `mangas` folder, `paths.library`, `paths.retention` or `paths.naming_overrides` is not an existing folder, or `paths.retention` is not on the filesystem of `paths.downloads` |
| workflow settings | hard | parts, smoke, `series link` | `workflow.attempt_limit`, `kavita.library_id` or `kavita.rescan_after_minutes` or `convert.workers` is below 1, or `export.retention_days` below 0 |
| image tools | hard | `process` | a `tools.avifenc`, `tools.avifdec`, `tools.jpegtran`, `tools.oxipng` or `tools.vship` path is not an existing file |
| scorer | hard | `process` | the scorer library does not load, or its GPU check fails |
| scratch space | hard | `process` | less than `convert.min_scratch_free_mb` MiB free on the drive of `paths.scratch` (measured at its nearest existing folder) |
| service settings | hard | `intake`, `export`, smoke, `series link` | `suwayomi.url` or `kavita.url` is not an http or https URL; `kavita.api_key` is not a non-empty string; `paths.kavita_path_map` is empty, one of its keys or values is not a non-empty string, or 2 of its keys are the same prefix but for a trailing `/` |

The runtime check covers a runtime that can load the program; one too old to parse it fails before preflight runs.

## CLI

Commands: `intake`, `process`, `export` (SPEC "Parts"), `junk list`, `junk show`, `junk decide`, `junk review` (SPEC "Junk decisions"), `status` (what is waiting, on what, what needs a human, the downloads not taken, and published files never confirmed), `retry` (SPEC "Needs human"), `state reset`, `state adopt` (SPEC "State"), `smoke` (SPEC "Services"), `series link` (SPEC "Series identification"; no lock, refused with exit 2 like `retry`).

`--set KEY=VALUE` (repeatable) sets a config key for the run (SPEC "Config"). Exit codes: 0 done, 1 an unexpected error (logged with its traceback) or a failed `smoke` check, 2 usage or config error or a refused `retry`, 3 another run holds the run lock, 4 a hard preflight check failed, or the database is not one this program's version can use (it holds a schema step the program does not have, or, for `status`, lacks one).

## Tests

Tests never read the live `config.yaml` (they get `tests/test_config.yaml`, paths in a per-run temp dir); a guard refuses the live file. Tests never open a network connection: a guard refuses every connection. The developer's `MANGA_PIPELINE_*` variables never reach a test, and each test starts with no process config.
