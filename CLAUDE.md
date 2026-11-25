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

Lint and format (both must pass before a change is done). ruff runs every rule (`select = ["ALL"]`); each exclusion and per-file ignore in `pyproject.toml` states its reason. Fix the code a rule flags; a new exclusion, per-file ignore or `noqa` needs the user's approval first:

```bash
python -m ruff check .
```

```bash
python -m ruff format .
```

Types (basedpyright, `[tool.basedpyright]` in `pyproject.toml`: basic mode for the tests; the program, `manga_pipeline/`, and `scripts/` are held to every setting strict mode turns on but `reportUnnecessaryIsInstance` and `reportMissingTypeStubs` (listed there, each exclusion with its reason and what reopens it), and to `reportExplicitAny`: no parameter, argument, variable or member of the program or a script is Unknown, and no `Any` is written in them; must pass on both platforms before a change is done; a `# pyright: ignore[...]` or another excluded setting needs the user's approval first):

```bash
python -m basedpyright --warnings
```

```bash
python -m basedpyright --warnings --pythonplatform Linux
```

Import contracts (import-linter, `[tool.importlinter]` in `pyproject.toml`: each contract encodes a rule of this file or SPEC; all must be kept; a new contract or `ignore_imports` entry needs the user's approval first):

```bash
lint-imports --no-cache
```

Dead code (vulture over the program only, `[tool.vulture]`: program code must be used by the program; must report nothing; the script drops only the findings on TypedDict field declarations, which are read by key; a new `ignore_names` entry needs the user's approval first):

```bash
python scripts/vulture_check.py
```

Known vulnerabilities (pip-audit; CI's non-required `audit` job runs it on every PR and weekly; a `--ignore-vuln` needs the user's approval first):

```bash
python -m pip_audit --strict
```

Run:

```bash
python -m manga_pipeline.cli --help
```

## Layout

| Path | Role |
|---|---|
| `manga_pipeline/cli.py` | entry point: argparse subcommands (global `--config`, `--set`, `--scheduled`); `COMMANDS`, the command table: each command's locks, idle mode, log file, preflight checks, handler `(cfg, args, conn)`, whether it builds the database and whether it is `automated` (the timers and tasks start it; only those take `--scheduled`); runs config (`config.init`), its connection and locks, logging, preflight, schema steps, handler; a `--scheduled` run first holds `lock.AUTOMATION` and reads the pause (`_scheduled`): paused, it ends there; `watch` (no lock of its own; scheduled, it holds `AUTOMATION` on a connection apart; `WATCH_CHECKS`: runtime version and the Discord checks only); `state pause` (polls `pause.waiting`) and `state resume`, which open the database without applying steps |
| `manga_pipeline/config.py` | `load` (file, then `ENV_PREFIX` variables, then `--set`; validated against `config.yaml.example`), `init`, `cached`, `reset`, `abs_path`; stored paths: `to_stored(cfg, path)` (`<share root>/<path>`, `SHARE_ROOTS`), `to_local(cfg, stored)`, `PathError` |
| `manga_pipeline/db.py` | the only opener of the PostgreSQL database (psycopg 3): `open_connection(cfg, dbname)` (every connection: the login from the `database` keys), `connect(cfg)` (autocommit, rows that read as `sqlite3.Row` does; creates a missing database, applies missing schema steps under the write lock, refuses an unknown one: `SchemaError`), `open_database(cfg, builds)` (applies nothing; a builder gets a missing database created, any other caller `NoDatabaseError` for one that is missing or holds no schema record), `apply_steps(conn)`, `now(conn)` (the server's clock, the only clock stored times come from), `read_only(cfg)` (a read-only repeatable-read session: 1 snapshot; `SchemaError` unless its steps are exactly the program's), `built(cfg)`, `peek_id(cfg)` (the id, read without checking or applying anything), `transaction(conn)` (takes the database-wide advisory lock: write transactions never overlap), `in_transaction(conn)`, `executemany(conn, sql, rows)`, `one_row(conn, query, params)` (a row the caller's data guarantees, else `MissingRowError`), `database_id(conn)`, `WORKFLOW_TABLES`, `Connection` (the type of a connection, so callers annotate one without importing psycopg); `CannotConnectError` (exit 4 in `cli.main`); the login uses only the `database` keys |
| `manga_pipeline/schema/` | schema steps `NNNN_name.sql`, applied in name order and recorded in `schema_step` |
| `manga_pipeline/page_store.py` | `save(conn, chapter_id, pages)` (a complete `images.measure_pages` result as the chapter's `page` rows, in the caller's transaction), `load(conn, chapter_id)` (the rows as `measure_pages(known=...)` takes them); the only writer of `page` rows, which hold the download's pages only |
| `manga_pipeline/workflow.py` | the only writer of chapter stage/status/waits/attempts/claims and `series.status`: `owner(part)` (`process@<host>`, else the part), `claim` (owner and `claim_count`; priority, scope, junk before convert, 1 machine per series or rules key, a chapter replacing an adopted file last and ignored by the others' gates (`REPLACES_ADOPTED`), `retry_after`; frees dead owners' claims first), `holds_claim` (the claim guard: `ClaimLost`), `claim_name` (`<id>-<count>`), `work` (stage outcome `Done` / `Waiting` / `Failed`, `Unreachable` skips the stage for the run; 1 log line per outcome), `drain`, `end_waits`, `release_own_claims`, `needs_human`, `retry_chapter` / `retry_series` (`Refused`: exit 2), `layout_changed` (an override's new layout: the series' downloads at convert back to junk, those waiting at junk ready), `reset`, `run_part` (run records in `meta`), `share_errors`, `in_scope(cfg, name, kavita_library_id)`; `context(cfg, conn, part)` builds the run's `Context`: its owner, its 1 `suwayomi` and 1 `kavita` client, its metadata `sources` and its `discord` client (None when off); `needs_human` queues its Discord report, worded by `human_line(kind, row, reason)` (what and why, the fix), which `status` prints too; the re-download state's types (`Redownload`, `PersonChoice`, `Target`, `Expected`; `read_redownload`), which `redownload` and the validate stage use; a chapter's notes, read by `read_notes` |
| `manga_pipeline/notify.py` | the Discord queue (SPEC "Needs human"): `queue(conn, kind, item_id, text, fix)` in the writer's transaction (once per kind, item and text), `send(conn, discord)` (export, every run's end, and `watch`: incidents first, 1 post each, never in a digest; then claim, post, digest over 5, a refused message marked failed), `message`, `digest` |
| `manga_pipeline/errors.py` | `Unreachable(service, reason)`: raised by the clients and the share, handled by `workflow` (which re-exports it) |
| `manga_pipeline/clients/http.py` | `Service(name, min_gap, user_agent, shown)`: 1 per client per run (`shown`: how its URLs appear in logs and errors, default `redact_url`); `request` retries, throttles, raises `Unreachable` (and remembers it), `HttpError` or `HttpTimeoutError`; `redact_url`; no module state |
| `manga_pipeline/clients/suwayomi.py` | `Suwayomi` GraphQL client (`from_config`): paged library, manga, chapters, queue, sources, `enqueue`, `delete_downloaded`, `start_downloader`, `set_in_library`, the 4 source calls (search, chapter fetch, `chapter_pages`, `page`: `SourceUnavailableError`); `GraphQLError`; download names `source_folder`, `manga_folder`, `chapter_file` |
| `manga_pipeline/clients/kavita.py` | `Kavita` client (`from_config`): token and the 401 rule (`refused`: why Kavita refused the API key in this run, else None), `libraries`, `series_in_library`, `collections` and `collection_series` (the user's collections, for `scripts/queue_first.py`), `series`, `volumes`, `series_files`, `series_folder` (`FolderError`), `local_path`, `scan_library`; `update_series` raises `ReplyError` for a series reply lacking a field it resends |
| `manga_pipeline/clients/discord.py` | `Discord` webhook client (`from_config`: None when `notify.discord_webhook` is null): `post(text)`; its Service shows the URL as `discord webhook` |
| `manga_pipeline/clients/kavita_match.py` | Kavita's name `normalize`, `series_names` (copied from the bulk converter) |
| `manga_pipeline/replies/` | service replies as types (`TypedDict`s, imports nothing of the program but itself): `kavita.py` (library, series DTO, series metadata, volumes), `suwayomi.py` (manga, chapter, source, download queue, page list and its validator `check_chapter_pages`), `sources.py` (MangaBaka, MangaUpdates, MangaDex and AniList records, a platform list's `Episode`) |
| `manga_pipeline/rewrite.py` | retroactive rewrites (SPEC "Retroactive updates"): `compare` (export's start: naming hash, `export.rewrites_open`), the rewrite chapter's `comicinfo_run` and `publish_run` (queues on the `published_file` row), `finish_all` / `finish` (file acts recorded by `pending_step`: `record_step`), `restore_all` (progress once Kavita re-read the file), `drop`, `diff`; `metadata/rewrite_plan.py` is its pure part (`changes`, `differences`) |
| `manga_pipeline/hand_edits.py` | `series edit FILE [--series-id ID]` (SPEC "Retroactive updates", hand edits): `apply` checks the edit file whole (series, chapter tokens `10` / `S2 10` / `side 3`, fields, file-name, volume-number and volume-order clashes) and stores `naming_edit` rows; the resolver applies them last (`naming.apply_edit`, `resolve(edits=…)`), `rewrite_plan.edit_changes` writes them into published files and restores a removed one (`published_file.unedited`) |
| `manga_pipeline/rewrite_covers.py` | volume covers of published files (SPEC "Retroactive updates"): `content_pages` (a download's measured pages but a `000_cover` page: those a length counts, SPEC "Library copies"); `fill_states` (each file's `cover_state`: a bulk file's from its record's `covers.volumes`, a pipeline file's from its member list), the rewrite chapter's `convert_run` (pages copied byte for byte, cover inserted or dropped; `process` dispatches to it) and its `convert_result` (`RewriteResult`, `read_result`), `shift_pages`; `metadata/rewrite_plan.cover_changes` is its pure part, `metadata/covers.fetch_wanted` its supply; `covers.refresh_volumes` (each check) refetches an image MangaDex now chooses another file for and marks its holders `stale:<V>` |
| `manga_pipeline/kavita_link.py` | an admitted new series' Kavita id (SPEC "Publishing"): `listing(ctx)` (once per run, `Context.kavita_cache`), `attach` (by ComicInfo name and folder, once per series per run), `attach_all` (for sync and discovery: returns the names still unmatched) |
| `manga_pipeline/incidents.py` | incidents (SPEC "Needs human"), queued as notify kind `incident`: `watch(cfg, conn)` (the `watch` command: `queue_stalls`, a part's `<owner>.last_success` over `STALL` old; `queue_downloads`, a folder's oldest `unmatched_download` over `UNMATCHED` old; then `notify.send`), `queue_key_refused` (intake's end, when `Kavita.refused`), `clear_key_refused` (the metadata step's answered listing removes a sent key incident); the incident texts' only owner |
| `manga_pipeline/smoke.py` | `smoke`: read-only check of both services, its lines yielded as each is known for `cli` to print; `SmokeError` = exit 1 |
| `manga_pipeline/intake.py`, `process.py`, `export.py` | the 3 part loops: `run(cfg)`; their order of work (SPEC "Parts"); `intake` queues adopted files' conversions before its drain, which is global (every download before any conversion) |
| `manga_pipeline/conversions.py` | adopted files' conversions (SPEC "State"): `queue(cfg, conn, series=None)` (the only inserter of conversion chapters: confirmed adopted files of ok series in scope named by no chapter, by series then number, or the listed series first; under `export.rewrites_open`, counted apart (`OPEN`), re-checked in each file's transaction), `link(ctx, chapter)` (discover's: the download as a hard link to the file, `<paths.staging>/adopted/<id>/<name>`), `clean_links` (link folders no open chapter uses); only `intake` runs them |
| `manga_pipeline/validation.py` | `check(cbz, key, published, same)` → `Verdict` (`ok` / `bad` / `human`, measured pages, notes): SPEC "Validation"; `chapter_key(identity)` = (season, side label, number) |
| `manga_pipeline/redownload.py` | a bad download's re-download (SPEC "Bad downloads"): the `chapter.redownload` state, `pick` (same chapter once, then ranked sources), `pick_manga` / `pick_chapter` (pure; the numbering reference as (identity, number) pairs, `numbering`), `remove` (retention, then Suwayomi delete), `wait_over` for `suwayomi_download`; driven by `stages/validate.py` |
| `manga_pipeline/library_copy.py` | the library copy check (SPEC "Library copies"): `earlier_copy(conn, chapter)` (another open download of the chapter's key is ahead of it: junk waits `open_copy`), `judge(ctx, chapter)` at junk's start (only 2 published files, an unreadable volume or the same pages), `after_junk` once junk decided (the rest on lengths after junk: same chapter, rating, width, bits per pixel, junk; a better copy takes its file, `rewrite.BUSY` holds it) through `decide` (the rule, pure, over our `Copy`, the published copy and the layout); `published_copy(ctx, row, layout)` (the published copy as compared, its length and junk by how it was measured; `source_sample` uses it too) |
| `manga_pipeline/adoption.py` | a record-less library series' adoption (SPEC "State"): `waiting(conn, series_id)` (no completed check, a chapter at discover: database reads only), `folder_of` (the stored folder, else Kavita's, no write; `baseline.with_folder` stores it before `take` decides record or adoption), `prepare(ctx, series, folder)` (folder: `folder_of`'s answer; every read, nothing written: `Prepared` (files, folder, layout, basis) or the `Problem`s that hold it, D7), `plan` (pure: 1 library file as a download is read), `reason` (the needs_human text); `baseline.take` adopts a series with no record and no baseline and writes `prepare`'s result in 1 transaction |
| `manga_pipeline/source_review.py` | the `source` commands but `sample` (SPEC "Source review"), each returning the lines `cli` prints: `survey`, `measure`, `search` (read only), `switch`, `replace`, `retire`; what `sample` shares: `reviewed` (the series check, `Refused`), `published`, `sample_files`, `reference` (the numbering reference), `chapters` (a library manga's stored list, never fetched); `measure_file` (`library_copy.original`, `published_junk`) for its own commands |
| `manga_pipeline/source_sample.py` | `source sample`: pages through the reader API into `<paths.scratch>/source/`, `validation.check`, `junk.chapter_scan` with the stored decisions, `library_copy.decide`; writes nothing |
| `manga_pipeline/notices.py` | notice titles (`validate.non_chapter_names`): `title`, `drop` (discover drops an unnumbered one), `judge(ctx, chapter)` at junk: a numbered one is kept with a note or dropped, once (SPEC "Validation") |
| `manga_pipeline/measures.py` | `summary(pages)` (page count, median width, strip length, non-blank pHashes), `strip_length` |
| `manga_pipeline/fleet.py` | the fleet's version (SPEC "State"): `digest(cfg)` over the program, shared settings (`shared_settings`; `MACHINE_LOCAL_*`), runtime packages and tool versions; `store(conn, cfg)` (`state version --yes`), `problem(conn, cfg)` (preflight "fleet version") |
| `manga_pipeline/metadata/` | series metadata (SPEC "Source ids", "Metadata checks"): `sources.py` (MangaBaka, MangaUpdates, MangaDex, AniList clients, and the Tapas and Webtoons episode lists; `SourceError`; `Sources`, the run's 6 source clients, and `new_sources`), `episodes.py` (a platform's episode list read: posts, titles, season markers; pure), `platforms.py` (which platform page a series' links name, its stored list's volumes and titles; pure), `ids.py` (1 form per source, each origin's ids, `resolve`: agreement, a human id wins; pure), `search.py` (title search acceptance), `check.py` (`due`, `run`: 1 series' check, stored in 1 transaction; `make_urgent`), `records.py` (a series' source records by name: `SeriesRecords`, `Records`, and `STORED`, the (source, kind) `fetched` keeps each under; types only), `tags.py` (`load` the rule files of `tag_rules/`, `compute` a series' genres and tags from its records; pure), `covers.py`, `refresh.py` (a completed series' chapter refresh), `library.py` (`sync`: a row per library series), `step.py` (intake's metadata step; its answered Kavita listing clears a sent key incident), `importer.py` / `audit.py` / `human.py` (`metadata import`, `metadata audit`, `series set-id`, `series new`, `series refetch-covers`) |
| `manga_pipeline/status.py` | `status`: read-only report (`db.read_only`), returned as lines for `cli` to print |
| `manga_pipeline/state.py` | `state reset` (`workflow.reset`, then empties staging but its owner file, scratch and candidates), `state adopt` (writes the owner file) and `state version` (`fleet.store`) |
| `manga_pipeline/stages/` | 1 module per stage, `run(ctx, chapter)` returns an outcome; Phase 1 stubs (junk's `.wait` marker is a test hook) |
| `manga_pipeline/logs.py` | `setup(cfg, command)`: console plus daily-rotated `<command>.log`, or console only for `None` |
| `manga_pipeline/lock.py` | `held(conn, cfg, names, idle, automation=False)`: run locks as database session advisory locks on the command's connection (key `NAMESPACE`, `hashtext(name)`), all or nothing; `IDLE` held `SHARED` by parts and review, `EXCLUSIVE` by the state commands; `AUTOMATION` held shared by every `--scheduled` run; `held_sql(name)` (is a name's lock held exclusive in this database; name an SQL Composable), `holders_sql(name)` (how many other sessions hold it); `LockHeld` |
| `manga_pipeline/pause.py` | the automation's pause (SPEC "Parts", "Run lock"): 1 `meta` row (`KEY`), `paused`, `set_pause`, `clear`, `waiting` (other sessions holding `lock.AUTOMATION`); read by `cli` for every `--scheduled` run and by `status` |
| `manga_pipeline/preflight.py` | `run(cfg, checks)` (`Check(name, HARD or SOFT, fn)`): soft problems warn, hard ones raise `PreflightError` (exit 4); ships `PYTHON_VERSION`, `FOLDER_LAYOUT`, `SHARE_FOLDERS_PRESENT`, `WORKFLOW_SETTINGS`, `SERVICE_SETTINGS`, `DISCORD_SETTINGS`, `DISCORD_WEBHOOK`, `SHARE_OWNERSHIP`, `STAGING_FOLDER`, `OCR_DEVICE`, `JUNK_WORD_LISTS`, `IMAGE_TOOLS`, `SCORER`, `SCRATCH_SPACE`, `CLAUDE_SETTINGS`, `NAMING_OVERRIDES`, `FLEET_VERSION`, `LONG_PATHS` (hard), `FREE_SPACE`, `SHARE_SPACE` (soft); `owner_file(cfg)` |
| `tests/__init__.py` | points `config.load` at `tests/test_config.yaml`; refuses the live config |
| `tests/conftest.py` | no-network guard, per-test timing report; takes the process's pool databases (`tests/pg.py`) at start and empties those a test used in its teardown; fails a run that built `mp_test_unassigned` |
| `tests/pg.py` | test databases (SPEC "Tests"): the pool `mp_test_pool_<n>` kept on the test server, taken per process under advisory locks, made right on take (step-file fingerprint), emptied after every test; a leaked connection fails its test; `current()` (this test process's `Process`) |
| `tests/builders.py` | fixture builders: `site_cfg(tmp_path, built=True)` (with its pool database), series and chapter rows, fake downloads, `run_part` (a part as `cli.main` runs it, under its locks), `running` (another run holding locks), `claimed` (a chapter claimed for a direct stage call); assertion helpers `one(conn, sql, params)` (the 1 row a query must return), `present(value)` (a value that must not be None), `problem_text(problem)` (a check's problem text, which must be set); `WinOSError`, `WinFileNotFoundError` (OS errors carrying `winerror`) |
| `tests/fakes/` | `http.py` (`Clock`, `Urlopen`, `CannedService`, `NoNetwork`), `suwayomi.py` (`FakeSuwayomi`, `cbz`), `kavita.py` (`FakeKavita`: `refuse_key` refuses every call as the real 401 rule does; a scan is queued until `run_scans()`, then indexes a temp library with Kavita's name rule), `sources.py` (`fake_sources()`: in-memory metadata sources, every call in `calls`), `discord.py` (`FakeDiscord`: `posts`, `failure`); they import the clients, never a part, a stage or `workflow` |
| `tests/fixtures_api/` | Suwayomi replies recorded from the real server by reads only |
| `tests/timing.py` | `@budget` per-test time budgets (copied from the unit-tests skill; project code now) |
| `scripts/vulture_check.py` | the dead-code check: vulture over the program, less the findings on TypedDict field declarations (located with ast) |
| `scripts/queue_first.py` | front-loading (plan phase-9c D-8): the Kavita "Reading" collection's series, then those with an open download, through `conversions.queue(series=...)`; no lock, no share; `--scheduled` (the drop-in) queues nothing while the automation is paused; exit 1 on Kavita unreachable or refusing or no single "Reading" collection, 2 with no database; `python -m scripts.queue_first [--scheduled] [--config PATH]`, also the intake drop-in `deploy/systemd/manga-pipeline-intake.service.d/queue-first.conf` |
| `scripts/adoption_census.py` | the adoption census (plan step A0): read only, `adoption.prepare` over every in-scope record-less series without a baseline, counts as JSON; `python -m scripts.adoption_census [--config PATH]` |
| `scripts/move_series_folder.py` | moves series folders into their source folder (plan fix-new-series-source-folder, step 1): under `lock.IDLE` exclusive, refusals first (the whole run: no library root, a `--to-parent` that is no folder directly under it; a series: not a new series, a chapter claimed or at publish / confirm, a `rewrite.BUSY` file, no target), target by `admit.source_folder` or `--to-parent` ("" = the root), 1 rename and 1 transaction (`series.folder`, `published_file.library_path`, `chapter.library_path`), resumes a rename not committed; no Kavita scan; `python -m scripts.move_series_folder SERIES_ID... [--to-parent FOLDER] [--dry-run] [--config PATH]` |
| `scripts/fix_series_name.py` | gives adopted series the `Series` name their library files carry (plan fix-adopted-series-name, step 1): the name by `adoption.series_name`; under `lock.IDLE` exclusive (`--dry-run`: no lock, reads only, 1 snapshot); both runs write the rollback record `<paths.scratch>/fix-series-name/<timestamp>.json` (the real run before each fix); refusals per series (no such series, not adopted, an unreadable library file, a name adoption refuses, a busy published file, a chapter claimed or not done at publish, a pipeline file with no ComicInfo.xml or one whose ComicInfo does not rebuild), the whole run when `export.retention_days` is 0 or the library root is missing; a share error ends the run; each pipeline file carrying another `Series` kept the retention way and replaced by a `.cbz.part` copy (`rewrite.copy_replacing`), then 1 transaction (`series.comicinfo_series`, every library and pipeline row's `comicinfo_series`, pipeline rows' `size`, open chapters); `python -m scripts.fix_series_name (--all \| SERIES_ID...) [--dry-run] [--config PATH]` |
| `scripts/set_config.py` | sets `config.yaml` keys at deploys: `KEY=VALUE` (dotted key of `config.yaml.example`, YAML value text written as given); only the value's text changes, a missing key is added at its block section's end (no section created, no mapping set); the edited text passes `config.load` (environment ignored) before an atomic replace, all keys or none; prints `set` / `added` / `unchanged KEY`, never a value; no lock; exit 2 bad input or refused key, 1 config check failed; `python -m scripts.set_config [--config PATH] KEY=VALUE...` |
| `deploy/` | `systemd/manga-pipeline-{intake,process,export,junk-review,watch}.{service,timer}` (the LXC's 5 timers, SPEC "Parts"; `watch`: SPEC "Needs human") and `README.md` (install, deploy procedure); no program code |

## Python standards

- Dependencies go in `pyproject.toml` `[dependency-groups]`: `runtime` for the program, `dev` for tools.
- Read config through `config.cached()` (or `config.load(path)` for another file); resolve configured relative paths with `config.abs_path`.
- Log through `logging.getLogger(__name__)`; the entry point names its logger `"manga_pipeline.cli"` (run by `python -m`, its `__name__` is `__main__`, outside the package logger).
- Tests are pytest style: plain functions, fixtures, `parametrize`, bare `assert` with a message stating the rule. Tag each with `@budget(load=...)` from `tests/timing.py`.

## Code standards

- Simplest design that meets the requirement. One path per job: no fallbacks or alternate code paths that silently take over when the primary one fails.
- Fail fast: validate inputs at the boundary and raise a clear error naming what is wrong.
- JSON and YAML loads stay in the named readers `tests/test_boundaries.py` lists; every other function gets a typed shape from one. A new reader calls the shape's existing validator where one exists; otherwise it is an unchecked reader and needs the user's approval first.
- Read config only through the config module: the entry point calls its `init` once (with `--set`), everything else reads `cached`. It merges `config.yaml`, `MANGA_PIPELINE_*` environment variables and `--set`, and checks the result against `config.yaml.example` (SPEC "Config").
- Log only through the logs module's logger; set up once, from the entry point (SPEC "Logging"). No `print` except a command's own output and its errors on stderr.
- Each command takes the locks its row in the command table names, once in the entry point (SPEC "Run lock"), or a one-off script under `scripts/` that holds `IDLE` exclusive itself; never take a lock anywhere else or wait for one (`state pause` alone polls until no scheduled run holds `automation`: SPEC "Run lock").
- A new requirement check is a function and its `Check` in the preflight module, plus its place in the command table's check lists in `cli.py` (SPEC "Preflight"): hard when the run cannot work without it, soft when it only warns. The function returns the problem text, or nothing when the requirement holds, and turns its own errors into problem text.
- Tests stay offline and never read the live `config.yaml` (SPEC "Tests"). Test only situations that occur in real use.
- Talk to Suwayomi and Kavita only through `clients/` (SPEC "Services"); a client never imports `workflow`, a stage or a part. Every path Kavita reports goes through `Kavita.local_path`; a series' folder is `Kavita.series_folder`, never Kavita's `folderPath`. A stage uses the run's clients, `ctx.suwayomi` and `ctx.kavita`, and never builds its own; `intake`'s metadata step uses `ctx.sources` (built by `workflow.context` through `sources.new_sources`). Tests use `tests/fakes/`.
- Module docstrings or header comments name the SPEC section they implement. Comment only where the reason isn't obvious.
- Run the linter, the formatter, the type checker, the import contracts (`lint-imports --no-cache`) and the dead-code check (`python scripts/vulture_check.py`) before finishing a change; all must pass.

## Merging

- `main` changes only through a pull request; it has 2 required checks: `tests` runs lint, format, types, import contracts, dead code and the full test suite, and `tests-linux` runs the full test suite on Linux (`.github/workflows/tests.yml`, whose `audit` job, pip-audit, is not required; the rule is `.github/rulesets/main.json`, applied as README "Merging" says).
- Local merge commits and squash commits run lint, the format check, the type check, the import contracts, the dead-code check and the full test suite first: `.githooks/pre-merge-commit`, and `.githooks/pre-commit` for a commit that concludes a merge or a squash, both run `.githooks/full-suite`. A fast-forward runs no hook: merge with a plain `git merge BRANCH`, never `--ff` / `--ff-only` or `git pull` of another branch into it. Never skip the hooks with `--no-verify`; fix the failing test instead.

## Known gotchas

- `config.yaml` is gitignored and may hold secrets: never print, log, copy or commit it. A new key goes in `config.yaml.example`, `tests/test_config.yaml` and `config.yaml`; loading refuses a mismatch. A secret may instead come from its environment variable.
- `.gitattributes` forces LF line endings; keep every file LF.
- `state/` is local runtime output (logs, locks, scratch, test timings) and is gitignored: nothing there is a source of truth; the database is on the PostgreSQL server.
