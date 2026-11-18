# manga_pipeline spec — Config, logging, locks, preflight, CLI, tests

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Config

`config.yaml.example` (committed, no secrets) is the schema for `config.yaml` (gitignored). Loading raises a config error naming the key when the file is missing, not YAML or not a mapping, or when a key is missing, unknown, or of another type than the example's. A file that is not YAML is reported by its line and column only: no error shows any of the file's content, which may hold a secret. A null example value allows any type; an empty example mapping allows any keys. An integer stands in for a float; a boolean only for a boolean.

Each key comes from, in rising priority: `config.yaml`, an environment variable, a `--set KEY=VALUE` flag. The merged result is checked, so a key may be left out of `config.yaml` when a variable or flag supplies it; a key missing from all 3 is refused, and the error names its variable and flag. An empty file or an empty section reads as an empty mapping. Nothing writes `config.yaml`: a variable or flag holds for that run only.

- Every key whose example value is not a non-empty mapping can be set. Its variable is `MANGA_PIPELINE_` plus the key path in capitals, with `.` and `-` as `_` (`logging.keep_days`: `MANGA_PIPELINE_LOGGING_KEEP_DAYS`); names compare without case. A `MANGA_PIPELINE_` variable that names no key is refused, and so is an example in which 2 keys give 1 name.
- The host, port, database, user and password of the login are always the 5 `database` keys, and no password file is read; a `PG*` variable can change only what those keys do not set (such as SSL). A `database` key missing or empty (the password may be null), or a port outside 1 to 65535, is a config error, raised when the database is opened, so it stops every command that opens one. Whether a login needs the password is the server's to say: a login it refuses exits 4 (SPEC "CLI"). The role must also be able to log in to the server's `postgres` database: a database is created through it, and a missing database is told from an unreachable server through it.
- The text converts by the example value's type: a string or null keeps the text as given (a secret is never parsed); an integer takes digits with an optional `-`; a float a plain decimal; a boolean `true` or `false` in any case; a list or an empty mapping is parsed as YAML. A bad value is refused naming its variable or flag.

| Key | Meaning |
|---|---|
| `logging.dir` | log folder, relative to the project root |
| `logging.level` | lowest level logged: DEBUG, INFO, WARNING, ERROR |
| `logging.keep_days` | rotated daily files kept |
| `lock.enabled` | take the command's lock (default true); false lets runs overlap (shared: every machine the same) |
| `preflight.min_free_mb` | free space (MiB) on `logging.dir`'s drive below which preflight warns; 0 skips the check |
| `database.host` | the PostgreSQL server: a host name or address, or the folder of its local socket (a path starting with `/`, such as `/var/run/postgresql`) |
| `database.port` | the server's port, 1 to 65535 |
| `database.name` | the pipeline's database on that server |
| `database.user` | the role the pipeline logs in as; over the local socket, the machine user that runs it |
| `database.password` | the role's password (secret: null in the example; `MANGA_PIPELINE_DATABASE_PASSWORD`); given when the server asks for one; over the local socket the server knows the machine user and asks for none |
| `paths.downloads` | Suwayomi's download root, the folder holding `mangas` (on the share) |
| `paths.library` | root of the Kavita library the pipeline publishes into (on the share) |
| `paths.staging` | staging folder for finished chapters (on the share, outside every library root) |
| `paths.retention` | where removed downloads and rewritten library files are kept for `export.retention_days` (on the share, same filesystem as downloads and the library, outside downloads and every library root) |
| `paths.naming_overrides` | the folder of per-series naming overrides, `<series rules key>.yaml` (SPEC "Series identification"), on the share: every part on every machine reads the same files; never written by the pipeline, and never emptied by `state reset` |
| `paths.scratch` | scratch folder for the chapter in hand (local disk) |
| `paths.covers` | folder of fetched cover images (on the share); a volume cover's path is relative to it (SPEC "Conversion") |
| `paths.candidates` | folder of the open junk candidates' images (on the share; SPEC "Junk decisions") |
| `paths.kavita_path_map` | Kavita path prefix to local path prefix, for the paths Kavita reports (SPEC "Services"); not empty |
| `workflow.attempt_limit` | failed attempts after which a chapter is `needs_human` |
| `intake.settle_minutes` | minutes a download's modified time must lie in the past before `intake` takes it (SPEC "Suwayomi") |
| `series.only` | names of the only series to work; empty = all |
| `suwayomi.url` | Suwayomi's base URL |
| `kavita.url` | Kavita's base URL |
| `kavita.api_key` | Kavita API key of an admin user (secret: null in the example) |
| `kavita.library_id` | id of the Kavita library the pipeline works; series of other libraries are out of scope |
| `kavita.rescan_after_minutes` | minutes after a library scan request before chapters still not indexed get another (and the scan is no longer awaited) (SPEC "Publishing") |
| `metadata.checks_per_run` | metadata checks an `intake` run makes beyond those a new chapter made due and first checks (SPEC "Metadata checks") |
| `metadata.recent_months`, `metadata.complete_months` | months since the last published update below which a series is checked every `metadata.recent_days`, and from which a complete series is checked every `metadata.complete_days`; between, every `metadata.quiet_days` |
| `metadata.recent_days`, `metadata.quiet_days`, `metadata.complete_days` | the 3 check intervals, in days |
| `export.retention_days` | days a removed download (or a rewritten library file) stays in `paths.retention`; 0 deletes it at once |
| `export.rewrites_open` | at most this many file rewrites open at a time (SPEC "Retroactive updates") |
| `notify.discord_webhook` | the Discord webhook the reports go to (SPEC "Needs human"); null = Discord off (secret: null in the example; set `MANGA_PIPELINE_NOTIFY_DISCORD_WEBHOOK`) |
| `tools.avifenc`, `tools.avifdec`, `tools.jpegtran`, `tools.oxipng` | the image tools' executables, per machine (relative = project root); nothing is searched on `PATH` (SPEC "Conversion") |
| `tools.vship` | the Vship scorer library (`libvship.so`, or the Windows DLL), per machine |
| `convert.workers` | processes that encode and decode one chapter's pages |
| `convert.min_scratch_free_mb` | free space (MiB) on `paths.scratch`'s drive below which `process` does not run |
| `sources.ranking` | Suwayomi source display names (`"<name> (<LANG>)"`) a bad download is re-downloaded from, best first (SPEC "Bad downloads") |
| `sources.download_timeout_hours` | hours a queued re-download may take before the next source is tried |
| `validate.non_chapter_names` | chapter names (without scanlator and number) of notice or hiatus pages: dropped at discover when unnumbered, judged at junk when numbered (SPEC "Validation"); empty = none |
| `junk.claude_token` | the token `junk review` gives `claude -p` (from `claude setup-token`; secret: null in the example) |
| `junk.claude_path` | the `claude` executable: a path, or a name found on `PATH` |
| `junk.review_limit` | reviews that list a candidate before it is stuck (SPEC "Junk decisions") |
| `junk.review_batch` | candidates `junk list` gives Claude per review |
| `junk.ocr_device` | where OCR runs: `cpu`, `dml` (DirectML, Windows) or `cuda` (NVIDIA, Linux) |
| `junk.keywords`, `junk.weak`, `junk.paired`, `junk.suppress` | the OCR keyword lists (SPEC "Junk decisions") |

Every key is machine-local or shared. Machine-local keys may differ between the machines that run the pipeline: `logging.*`, `preflight.*`, `database.*`, every `paths.*` key, `tools.*`, `convert.workers`, `convert.min_scratch_free_mb`, `junk.ocr_device`, `junk.claude_path`, `suwayomi.url`, `kavita.url`, and every secret (a key whose example value is null: `junk.claude_token`, `kavita.api_key`, `notify.discord_webhook`). Every other key is shared: its value enters the fleet's digest (SPEC "State"), so a machine whose value differs does not run. A machine that runs only `process` reaches no service, so its service URLs and Kavita key may be the example's; its shared keys (`kavita.library_id`, which the scope reads, among them) are the fleet's.

The share roots (`paths.downloads`, `paths.library`, `paths.staging`, `paths.retention`, `paths.candidates`, `paths.covers`) are each machine's own mount of the same folders: a POSIX path on Linux, a drive-letter or UNC path on Windows (a scheduled task with no logged-on user sees no mapped drive letter, so use UNC there). A stored path names its root and the path below it; SPEC "State" names the 2 columns that hold a name relative to their folder instead; the mapping compares the configured root as written, resolving no links, drive letters or UNC names.

## Logging

One setup call, from the entry point. Records go to the console and, for a command that runs under its own lock, to `<logging.dir>/<command>.log` (`intake.log`, `process.log`, `export.log`, `junk-review.log`), so commands running at the same time never share a file. At the first write after midnight the file is renamed `<command>.log.YYYY-MM-DD` and a new one starts; only the newest `logging.keep_days` dated files of each command are kept. Every other command logs to the console only.

## Run lock

Each command names the locks it runs under, or none. `intake`, `export` and `junk review` each have their own, 1 in the whole fleet; `process` has 1 per machine, `process@<host name>`. Each of these 4 also holds the "nothing else is running" lock shared. `state reset`, `state adopt` and `state version` hold that lock exclusive, so they run only while no part and no review runs on any machine, and hold it until they end, their file work included. `status`, `retry`, `chapter use`, `chapter drop`, `series link`, `series new`, `smoke`, `naming diff`, `rewrite retry`, `rewrite drop`, `junk list`, `junk show`, `junk decide` and `junk import` take none, so they work while a part is active.

The locks are session locks of the database: the command opens its 1 connection after config is loaded and before logging is set up, takes its locks on it, and does all its work on it; the server frees them when the connection ends, a crash or a machine that vanished included (its keepalive settings end a vanished client's session within minutes; a sleeping laptop took about 6). Opening never creates the database: a command that builds one (`state adopt`, `junk import`) creates it when it finds none. A command that takes a lock and cannot reach the server, or (other than `state adopt`) finds no database (SPEC "State"), prints the error to stderr and exits 4 without opening the log file. With `lock.enabled`, a command that finds a lock held prints an error naming the lock to stderr and exits 3 at once, without opening the log file; it never waits, and 2 runs starting in the same instant never both take it. With `lock.enabled` false no lock is taken and runs may overlap.

## Preflight

After config, the run lock and logging, and before the work, the run checks its requirements. Each check is hard (the run cannot work without it) or soft (the run can, but something is off), and every check runs. A soft problem is logged as a warning and the run goes on. A hard problem is printed to stderr and logged; once every check has run, the run exits 4. A check that cannot measure what it checks reports that as its problem. Each command has its own list of checks (the Runs column): `intake`, `process` and `export` (parts), `state reset` (reset), `state adopt` (adopt; it writes the owner file, so it checks the staging folder in place of share ownership), `junk review` (review), `state version` (version), `smoke`, `series link`; the other commands without a lock run none. A server that cannot be reached is not a check of its own: a command with a lock has already exited 4 (SPEC "Run lock"), and any other command that opens the database exits 4 (SPEC "CLI").

| Check | Kind | Runs | Problem when |
|---|---|---|---|
| free disk space | soft | parts, reset, adopt, review, version | less than `preflight.min_free_mb` MiB free on `logging.dir`'s drive (0 skips the check; a negative value is itself the problem) |
| runtime version | hard | parts, reset, adopt, review, version | Python older than 3.13 |
| share ownership | hard | parts, reset, review | the owner file in `paths.staging` is missing, or holds another database's id (SPEC "State"), or the database's id cannot be read (the server cannot be reached) |
| staging folder | hard | adopt | `paths.staging` is not an existing folder |
| folder layout | hard | parts, reset, adopt, review | `paths.staging`, `paths.scratch` or `paths.candidates` (the folders `state reset` empties) equals or contains `paths.downloads`, `paths.library`, `paths.retention`, `paths.covers`, `paths.naming_overrides`, `logging.dir`, the project root or another of the 3, or lies inside `paths.downloads` or `paths.library`; `paths.retention` lies inside `paths.downloads` or `paths.library`, or contains either |
| share folders | hard | parts, reset, adopt, review | `paths.downloads`, its `mangas` folder, `paths.library`, `paths.retention`, `paths.candidates` or `paths.covers` is not an existing folder, or `paths.retention` is not on the filesystem of `paths.downloads` and of `paths.library` |
| naming overrides | hard | `intake`, `process`, `export` | `paths.naming_overrides` is not an existing folder |
| fleet version | hard | parts, review | the database holds no fleet digest (`state version --yes` was never run), or this machine's differs from it; the problem names which differs: the program, a shared setting, a runtime dependency's version, or a tool's |
| workflow settings | hard | parts, review, smoke, `series link`, `metadata import`, `metadata audit` | `workflow.attempt_limit`, `kavita.library_id`, `kavita.rescan_after_minutes`, `junk.review_limit`, `junk.review_batch`, `convert.workers`, `intake.settle_minutes` or a `metadata` key is below 1, `metadata.complete_months` is not above `metadata.recent_months`, `export.retention_days` below 0, `export.rewrites_open` below 1, or `series.only` holds a name that is not text |
| OCR device | hard | `process` | `junk.ocr_device` is not `cpu`, `dml` or `cuda`, or its onnxruntime execution provider does not load (a session's first provider is checked, not the listing) |
| junk word lists | hard | `process` | `junk.weak` or `junk.paired` holds a term that is not in `junk.keywords` |
| image tools | hard | `process`, version | a `tools.avifenc`, `tools.avifdec`, `tools.jpegtran`, `tools.oxipng` or `tools.vship` path is not an existing file |
| scorer | hard | `process`, version | the scorer library does not load, or its GPU check fails |
| scratch space | hard | `process` | less than `convert.min_scratch_free_mb` MiB free on the drive of `paths.scratch` (measured at its nearest existing folder) |
| long paths | hard | `process` | on Windows, the registry value `LongPathsEnabled` (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`) is not 1: a share path of 260 or more characters could not be opened there |
| Claude settings | hard | review | `junk.claude_path` does not resolve to an executable file, or `junk.claude_token` is not a non-empty string |
| discord settings | hard | `export` | `notify.discord_webhook` is set but is not an https URL with a host, or holds a space or a control character (the value is never shown) |
| service settings | hard | `intake`, `export`, smoke, `series link`, `metadata import`, `metadata audit` | `suwayomi.url` or `kavita.url` is not an http or https URL; `kavita.api_key` is not a non-empty string; `paths.kavita_path_map` is empty, one of its keys or values is not a non-empty string, or 2 of its keys are the same prefix but for a trailing `/` |

The runtime check covers a runtime that can load the program; one too old to parse it fails before preflight runs.

## CLI

Commands: `intake`, `process`, `export` (SPEC "Parts"), `junk list [--all]`, `junk show ID`, `junk decide ID junk|keep|unsure`, `junk review`, `junk import PATH`, `junk import --database NAME` (SPEC "Junk decisions"; `junk list`, `show` and `decide` refuse with exit 2 a database never built (SPEC "State") or a candidate that is not open, `--all` while a review id is set; `junk import` refuses with exit 2 a file that is not a bulk-converter junk store, a database on the server that is not a pipeline database, or this pipeline's own database), `status` (what is waiting, on what, what needs a human, the series titles changed at the sources in the last 30 days, the Discord reports not sent (queued, with the oldest's time, and refused), the queued, awaited and failed rewrites, the re-downloads in progress, chapter notes, the downloads not taken, published files never confirmed, each part's last run per machine, and the claims held, by owner, with their age), `retry` (SPEC "Needs human"), `chapter use`, `chapter drop` (SPEC "Bad downloads"; no lock, refused with exit 2 like `retry`), `state reset`, `state adopt`, `state version --yes` (stores this machine's digest as the fleet's; SPEC "State"), `smoke` (SPEC "Services"), `series link MANGA_ID KAVITA_SERIES_ID`, `series link --folder SOURCE_FOLDER/TITLE_FOLDER KAVITA_SERIES_ID` (SPEC "Series identification"; no lock, refused with exit 2 like `retry`), `series set-id SERIES_ID SOURCE ID` (SPEC "Source ids"; no lock, refused with exit 2 like `retry`), `series new SERIES_ID [--name NAME]` (SPEC "Series identification"; no lock, refused with exit 2 like `retry`), `series refetch-covers SERIES_ID` (SPEC "Retroactive updates"; no lock, refused with exit 2 for a series that does not exist), `metadata import STATE_FILE CONFIG_FILE` (under `intake`'s lock; refused with exit 2 for a file that is not the old tool's state file or config), `metadata audit [--state PATH]` (SPEC "Metadata checks"; no lock, no database, read-only), `naming diff [SERIES_ID]` (SPEC "Retroactive updates"; no lock, read-only), `series edit FILE [--series-id ID]` (SPEC "Retroactive updates", hand edits; no lock, the share checks; refused with exit 2 for a file with any problem), `rewrite retry FILE_ID`, `rewrite drop FILE_ID` (SPEC "Retroactive updates"; no lock; refused with exit 2 for a file whose rewrite has not failed, or, for `drop`, is not stuck or is past its rename).

`--set KEY=VALUE` (repeatable) sets a config key for the run (SPEC "Config"). Exit codes: 0 done, 1 an unexpected error (logged with its traceback), a failed `smoke` check, or a `junk review` whose Claude run failed (non-zero exit or an error result), 2 usage or config error or a refused `retry`, `chapter use`, `chapter drop`, `series link`, `series new`, `series set-id`, `series edit`, `series refetch-covers`, `metadata import` or `junk` command, 3 another run, on this machine or another, holds a lock the command takes, 4 a hard preflight check failed, the database server cannot be reached or refuses the login (a 10-second connect timeout per attempt, and a failed attempt is followed by 1 on the `postgres` database; the message names the host and port, never the password), a command that takes a lock finds no database (SPEC "State"), or the database is not one this program's version can use (it holds a schema step the program does not have, or, for `status`, lacks one).

## Tests

Tests never read the live `config.yaml` (they get `tests/test_config.yaml`, paths in a per-run temp dir); a guard refuses the live file. Tests never open a network connection but to the test database server: a guard refuses every connection Python opens. The database is the exception, and the server enforces it: tests log in to a server on the developer's machine or in CI as a test role (`mp_test`), which those servers let in from the same machine without a password (so Claude's `junk` commands in a review test reach the database with every secret null); its password in `tests/test_config.yaml` protects nothing; the pipeline's own server has no such role, so a test pointed at it fails to log in. The developer's `MANGA_PIPELINE_*` and `PG*` variables never reach a test or a CLI subprocess it starts, and each test starts with no process config. The test server keeps a pool of databases (`mp_test_pool_<n>`) between runs. Each test process takes 2 when it starts, holding a lock on each for its life, so no other process or run uses them meanwhile; taking one empties it, or rebuilds it when it is not exactly what a build of the current step files makes (a dead run's leftovers, another branch's steps). Every database a test used is emptied in its teardown to the state of a new database with a new id; a test that left a connection open fails. A test asks for a database never built when it needs one. The harness creates only `mp_test_*` databases and never drops the pool's.
