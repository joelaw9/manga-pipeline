# manga_pipeline

## Project overview

Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion

The spec is the source of truth for every decision: `SPEC.md` is its index (each section in 1 or 2 lines), and the rules of each section are in the `docs/spec/` file it names. Read the relevant section before changing behaviour; update it in the same change.

## Core commands

Setup (system Python 3.13+, no packaging; pip 25.1+ for `--group`):

```bash
python -m pip install --group dev
```

The tests need a local PostgreSQL 17 server on `localhost:5432` with `max_connections = 300` (as its superuser: `ALTER SYSTEM SET max_connections = 300`, then restart the service), `pg_hba.conf` letting the test role in from this machine without a password (`host all mp_test 127.0.0.1/32 trust` and `host all mp_test ::1/128 trust`, above the other lines; then `SELECT pg_reload_conf()`), and the test role, which must never exist on the pipeline's own server:

```bash
psql -h localhost -U postgres -d postgres -c "CREATE ROLE mp_test LOGIN CREATEDB PASSWORD 'mp_test'"
```

Tests (pytest, in parallel with pytest-xdist; each run ends with a per-test timing report and writes `state/test-timings.json`; a serial `python -m pytest -q` also checks the per-test budgets; parallel runs, the merge hook's and CI's included, skip that check):

```bash
python -m pytest -q -n auto
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
| `manga_pipeline/cli.py` | entry point: argparse subcommands (global `--config`, `--set`); `COMMANDS`, the command table: each command's locks, idle mode, log file, preflight checks, handler `(cfg, args, conn)` and whether it builds the database; runs config (`config.init`), its connection and locks, logging, preflight, schema steps, handler |
| `manga_pipeline/config.py` | `load` (file, then `ENV_PREFIX` variables, then `--set`; validated against `config.yaml.example`), `init`, `cached`, `reset`, `abs_path`; stored paths: `to_stored(cfg, path)` (`<share root>/<path>`, `SHARE_ROOTS`), `to_local(cfg, stored)`, `PathError` |
| `manga_pipeline/db.py` | the only opener of the PostgreSQL database (psycopg 3): `connect(cfg)` (autocommit, rows that read as `sqlite3.Row` does; creates a missing database, applies missing schema steps under the write lock, refuses an unknown one: `SchemaError`), `open_database(cfg, builds)` (applies nothing; a builder gets a missing database created, any other caller `NoDatabase` for one that is missing or holds no schema record), `apply_steps(conn)`, `now(conn)` (the server's clock, the only clock stored times come from), `read_only(cfg)` (a read-only repeatable-read session: 1 snapshot; `SchemaError` unless its steps are exactly the program's), `built(cfg)`, `peek_id(cfg)` (the id, read without checking or applying anything), `transaction(conn)` (takes the database-wide advisory lock: write transactions never overlap), `in_transaction(conn)`, `executemany(conn, sql, rows)`, `database_id(conn)`, `WORKFLOW_TABLES`; `CannotConnect` (exit 4 in `cli.main`); the login uses only the `database` keys |
| `manga_pipeline/schema/` | schema steps `NNNN_name.sql`, applied in name order and recorded in `schema_step` |
| `manga_pipeline/page_store.py` | `save(conn, chapter_id, pages)` (a complete `images.measure_pages` result as the chapter's `page` rows, in the caller's transaction), `load(conn, chapter_id)` (the rows as `measure_pages(known=...)` takes them); the only writer of `page` rows, which hold the download's pages only |
| `manga_pipeline/workflow.py` | the only writer of chapter stage/status/waits/attempts/claims and `series.status`: `owner(part)` (`process@<host>`, else the part), `claim` (owner and `claim_count`; priority, scope, junk before convert, 1 machine per series or rules key, `retry_after`; frees dead owners' claims first), `holds_claim` (the claim guard: `ClaimLost`), `claim_name` (`<id>-<count>`), `work` (stage outcome `Done` / `Waiting` / `Failed`, `Unreachable` skips the stage for the run; 1 log line per outcome), `drain`, `end_waits`, `release_own_claims`, `needs_human`, `retry_chapter` / `retry_series` (`Refused`: exit 2), `reset`, `run_part` (run records in `meta`), `share_errors`, `in_scope(cfg, name, kavita_library_id)`; `Context` holds the run's 1 `suwayomi` and 1 `kavita` client |
| `manga_pipeline/errors.py` | `Unreachable(service, reason)`: raised by the clients and the share, handled by `workflow` (which re-exports it) |
| `manga_pipeline/clients/http.py` | `Service(name, min_gap)`: 1 per client per run; `request` retries, throttles, raises `Unreachable` (and remembers it), `HttpError` or `Timeout`; `redact_url`; no module state |
| `manga_pipeline/clients/suwayomi.py` | `Suwayomi` GraphQL client (`from_config`): paged library, manga, chapters, queue, sources, `enqueue`, `delete_downloaded`, `start_downloader`, the 2 source calls (`SourceUnavailable`); `GraphQLError`; download names `source_folder`, `manga_folder`, `chapter_file` |
| `manga_pipeline/clients/kavita.py` | `Kavita` client (`from_config`): token and the 401 rule, `libraries`, `series_in_library`, `series`, `volumes`, `series_files`, `series_folder` (`FolderError`), `local_path`, `scan_library` |
| `manga_pipeline/clients/kavita_match.py` | Kavita's name `normalize`, `series_names`, `off_names`, `match_folders` (copied from the bulk converter) |
| `manga_pipeline/rewrite.py` | retroactive rewrites (SPEC "Retroactive updates"): `compare` (export's start: naming hash, `export.rewrites_open`), the rewrite chapter's `comicinfo_run` and `publish_run` (queues on the `published_file` row), `finish_all` / `finish` (file acts recorded by `pending_step`), `restore_all` (progress once Kavita re-read the file), `drop`, `diff`; `metadata/rewrite_plan.py` is its pure part (`changes`, `differences`) |
| `manga_pipeline/hand_edits.py` | `series edit FILE [--series-id ID]` (SPEC "Retroactive updates", hand edits): `apply` checks the edit file whole (series, chapter tokens `10` / `S2 10` / `side 3`, fields, file-name, volume-number and volume-order clashes) and stores `naming_edit` rows; the resolver applies them last (`naming.apply_edit`, `resolve(edits=…)`), `rewrite_plan.edit_changes` writes them into published files and restores a removed one (`published_file.unedited`) |
| `manga_pipeline/rewrite_covers.py` | volume covers of published files (SPEC "Retroactive updates"): `fill_states` (each file's `cover_state`: a bulk file's from its record's `covers.volumes`, a pipeline file's from its member list), the rewrite chapter's `convert_run` (pages copied byte for byte, cover inserted or dropped; `process` dispatches to it), `shift_pages`; `metadata/rewrite_plan.cover_changes` is its pure part, `metadata/covers.fetch_wanted` its supply; `covers.refresh_volumes` (each check) refetches an image MangaDex now chooses another file for and marks its holders `stale:<V>` |
| `manga_pipeline/kavita_link.py` | a published new series' Kavita id (SPEC "Publishing"): `listing(ctx)` (once per run, `Context.kavita_cache`), `attach` (by ComicInfo name and folder, once per series per run), `attach_all` (for sync and discovery: returns the names still unmatched) |
| `manga_pipeline/smoke.py` | `smoke`: read-only check of both services; `Failed` = exit 1 |
| `manga_pipeline/intake.py`, `process.py`, `export.py` | the 3 part loops: `run(cfg)`; their order of work (SPEC "Parts") |
| `manga_pipeline/validation.py` | `check(cbz, key, published, same)` → `Verdict` (`ok` / `bad` / `human`, measured pages, notes): SPEC "Validation"; `chapter_key(identity)` = (season, side label, number) |
| `manga_pipeline/redownload.py` | a bad download's re-download (SPEC "Bad downloads"): the `chapter.redownload` state, `pick` (same chapter once, then ranked sources), `pick_manga` / `pick_chapter` (pure), `remove` (retention, then Suwayomi delete), `wait_over` for `suwayomi_download`; driven by `stages/validate.py` |
| `manga_pipeline/library_copy.py` | the library copy check (SPEC "Library copies"): `judge(ctx, chapter)` at junk's start (a duplicate dropped, a higher-rated copy takes its file, a doubtful one held), `after_junk` once junk decided (width, bits per pixel, junk: a better copy takes its file, `rewrite.BUSY` holds it) |
| `manga_pipeline/notices.py` | notice titles (`validate.non_chapter_names`): `title`, `drop` (discover drops an unnumbered one), `judge(ctx, chapter)` at junk: a numbered one is kept with a note or dropped, once (SPEC "Validation") |
| `manga_pipeline/measures.py` | `summary(pages)` (page count, median width, strip length, non-blank pHashes), `strip_length` |
| `manga_pipeline/fleet.py` | the fleet's version (SPEC "State"): `digest(cfg)` over the program, shared settings (`shared_settings`; `MACHINE_LOCAL_*`), runtime packages and tool versions; `store(conn, cfg)` (`state version --yes`), `problem(conn, cfg)` (preflight "fleet version") |
| `manga_pipeline/metadata/` | series metadata (SPEC "Source ids", "Metadata checks"): `sources.py` (MangaBaka, MangaUpdates, MangaDex, AniList clients, and the Tapas and Webtoons episode lists; `SourceError`), `episodes.py` (a platform's episode list read: posts, titles, season markers; pure), `platforms.py` (which platform page a series' links name, its stored list's volumes and titles; pure), `ids.py` (1 form per source, each origin's ids, `resolve`: agreement, a human id wins; pure), `search.py` (title search acceptance), `check.py` (`due`, `run`: 1 series' check, stored in 1 transaction; `make_urgent`; `Sources`, `new_sources`), `tags.py` (`load` the rule files of `tag_rules/`, `compute` a series' genres and tags from its records; pure), `covers.py`, `refresh.py` (a completed series' chapter refresh), `library.py` (`sync`: a row per library series), `step.py` (intake's metadata step), `importer.py` / `audit.py` / `human.py` (`metadata import`, `metadata audit`, `series set-id`, `series new`, `series refetch-covers`) |
| `manga_pipeline/status.py` | `status`: read-only report (`db.read_only`), printed |
| `manga_pipeline/state.py` | `state reset` (`workflow.reset`, then empties staging but its owner file, scratch and candidates), `state adopt` (writes the owner file) and `state version` (`fleet.store`) |
| `manga_pipeline/stages/` | 1 module per stage, `run(ctx, chapter)` returns an outcome; Phase 1 stubs (junk's `.wait` marker is a test hook) |
| `manga_pipeline/logs.py` | `setup(cfg, command)`: console plus daily-rotated `<command>.log`, or console only for `None` |
| `manga_pipeline/lock.py` | `held(conn, cfg, names, idle)`: run locks as database session advisory locks on the command's connection (key `NAMESPACE`, `hashtext(name)`), all or nothing; `IDLE` held `SHARED` by parts and review, `EXCLUSIVE` by the state commands; `HELD_SQL` (is a name's lock held in this database); `LockHeld` |
| `manga_pipeline/preflight.py` | `run(cfg, checks)` (`Check(name, HARD or SOFT, fn)`): soft problems warn, hard ones raise `PreflightFailed` (exit 4); ships `PYTHON_VERSION`, `FOLDER_LAYOUT`, `SHARE_FOLDERS_PRESENT`, `WORKFLOW_SETTINGS`, `SERVICE_SETTINGS`, `SHARE_OWNERSHIP`, `STAGING_FOLDER`, `OCR_DEVICE`, `JUNK_WORD_LISTS`, `IMAGE_TOOLS`, `SCORER`, `SCRATCH_SPACE`, `CLAUDE_SETTINGS`, `NAMING_OVERRIDES`, `FLEET_VERSION`, `LONG_PATHS` (hard), `FREE_SPACE` (soft); `owner_file(cfg)` |
| `tests/__init__.py` | points `config.load` at `tests/test_config.yaml`; refuses the live config |
| `tests/conftest.py` | no-network guard, per-test timing report; takes the process's pool databases (`tests/pg.py`) at start and empties those a test used in its teardown; fails a run that built `mp_test_unassigned` |
| `tests/pg.py` | test databases (SPEC "Tests"): the pool `mp_test_pool_<n>` kept on the test server, taken per process under advisory locks, made right on take (step-file fingerprint), emptied after every test; a leaked connection fails its test |
| `tests/builders.py` | fixture builders: `site_cfg(tmp_path, built=True)` (with its pool database), series and chapter rows, fake downloads, `run_part` (a part as `cli.main` runs it, under its locks), `running` (another run holding locks), `claimed` (a chapter claimed for a direct stage call) |
| `tests/fakes/` | `http.py` (`Clock`, `Urlopen`, `CannedService`, `NoNetwork`), `suwayomi.py` (`FakeSuwayomi`, `cbz`), `kavita.py` (`FakeKavita`: a scan is queued until `run_scans()`, then indexes a temp library with Kavita's name rule), `sources.py` (`fake_sources()`: in-memory metadata sources, every call in `calls`); they import the clients, never a part, a stage or `workflow` |
| `tests/fixtures_api/` | Suwayomi replies recorded from the real server by reads only |
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
- Talk to Suwayomi and Kavita only through `clients/` (SPEC "Services"); a client never imports `workflow`, a stage or a part. Every path Kavita reports goes through `Kavita.local_path`; a series' folder is `Kavita.series_folder`, never Kavita's `folderPath`. A stage uses the run's clients, `ctx.suwayomi` and `ctx.kavita`, and never builds its own; `intake`'s metadata step uses `ctx.sources` (built by `check.new_sources` when None). Tests use `tests/fakes/`.
- Module docstrings or header comments name the SPEC section they implement. Comment only where the reason isn't obvious.
- Run the linter and formatter before finishing a change; both must pass.

## Merging

- `main` changes only through a pull request; its required `tests` check runs lint, format and the full test suite (`.github/workflows/tests.yml`; the rule is `.github/rulesets/main.json`, applied as README "Merging" says).
- Local merge commits and squash commits run the full test suite first: `.githooks/pre-merge-commit`, and `.githooks/pre-commit` for a commit that concludes a merge or a squash, both run `.githooks/full-suite`. A fast-forward runs no hook: merge with a plain `git merge BRANCH`, never `--ff` / `--ff-only` or `git pull` of another branch into it. Never skip the hooks with `--no-verify`; fix the failing test instead.

## Known gotchas

- `config.yaml` is gitignored and may hold secrets: never print, log, copy or commit it. A new key goes in `config.yaml.example`, `tests/test_config.yaml` and `config.yaml`; loading refuses a mismatch. A secret may instead come from its environment variable.
- `.gitattributes` forces LF line endings; keep every file LF.
- `state/` is local runtime output (logs, locks, scratch, test timings) and is gitignored: nothing there is a source of truth; the database is on the PostgreSQL server.
- The only SQLite left is the bulk converter's `junk.sqlite3`, read by `junk import PATH`.
