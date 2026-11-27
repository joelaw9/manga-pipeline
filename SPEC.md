# manga_pipeline — Spec

The single reference for every settled decision. This file is the index: each section below names what it covers and the file under `docs/spec/` that holds its rules. Settings live in `config.yaml`; the spec explains them. Update the section's file in the same change as the behaviour it describes, and this file when a section is added, renamed or moved.

## Purpose

Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion

## Scope and constraints

- Takes what Suwayomi downloads (new chapters of existing series, and whole new series of any size) and lands it in the Kavita library: validated, junk removed, images converted, `ComicInfo.xml` written.
- Keeps series metadata current: a full retrieval for a new series, a refresh of the fields that go stale for an existing one, and a rewrite of what is already published when a refresh changes it.
- Never waits on Suwayomi, Kavita or a junk decision. A chapter that depends on one of them is recorded as waiting and picked up by a later run.
- Runs unattended on a Debian LXC with an NVIDIA GPU. `process` (junk and convert) also runs on Windows machines with NVIDIA GPUs against the same database, each chapter worked once, with the same output whichever machine works it.
- Suwayomi downloads CBZ files. The downloads folder and the library are on the same NFS export.
- Series written by the bulk converter carry its series record, `.optimizer.json`, in the series folder; the pipeline reads it and never writes it. The bulk converter no longer runs, so no new record appears; the pipeline is the only converter. A library series with no record is adopted: its files are recorded, then each goes through the pipeline and replaces itself.

## Parts

3 commands (`intake`, `process`, `export`) share the work by chapter stage; the database is the only hand-off, and each runs from its own 10-minute timer; `process` runs on several machines, never 2 in 1 series at once; work that replaces an adopted file is picked after every other chapter. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#parts)

## State

1 PostgreSQL 17 database on a server holds workflow state, knowledge and sync records; its write transactions run 1 at a time; a claim names its owner and count and is freed when its owner's session ends; paths are stored relative to their share root and times from the server's clock; a library series with no bulk-converter record is adopted: its own files are its baseline, and each is then converted through the pipeline; `state reset` clears only the workflow state. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#state)

## Chapter stages

discover, validate, junk, convert, comicinfo, publish, confirm. A stage ends done, waiting or failed; a chapter past its attempt limit is `needs_human`. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#chapter-stages)

## Conversion

The convert stage re-encodes a chapter with the bulk converter's tools, route and gates (AVIF where the quality gates pass, else lossless, else untouched) into a store-mode CBZ in staging; it never writes the series folder. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#conversion)

## Calibration

AVIF quality per series and encode group: taken from the bulk-converter record, from a spread of an adopted series' files (as the bulk converter took it), or computed from samples of the first chapters converted, 20 samples fixing a group. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#calibration)

## Needs human

A chapter or series that needs a person, or a series title changed at the sources, is reported 3 ways (log, `status`, 1 Discord message per item and reason, queued with the change and sent by `export`); `retry` clears a needs_human item. A part with no success in 24 hours, downloads not taken for 24 hours and Kavita refusing the key are incidents: 1 Discord message each while they last, never in a digest, found by the hourly `watch`, which also sends. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#needs-human)

## Bad downloads

Validation reads every page; a bad download is removed and downloaded again through Suwayomi (same chapter once, then other sources by rank), other defects need a human; a copy of a published chapter is judged once its junk is decided: dropped, held, or it replaces the file; `chapter use` and `chapter drop` settle a held chapter; handled chapters are remembered with their content digest, so the same pages appearing again are put aside, not processed twice. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#bad-downloads)

## Suwayomi

Any CBZ under the downloads folder is taken once its modified time has settled, Suwayomi's or placed by hand; a completed series' chapter list is refreshed by `intake` when its check is due, and only chapters it adds are downloaded. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#suwayomi)

## Series identification

A Suwayomi manga, or a hand-placed folder, is matched to a series by a stored link, a source id, or exactly 1 Kavita name (a folder: its name, else its files' ComicInfo Series); a near or double hit is `needs_human`, fixed with `series link`; no hit is a new series, admitted under its own name and folder after its first metadata check (`series new` settles a held one); a library manga with no downloads is linked when it matches. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#series-identification)

## Source review

The `source` commands take a library series, step by step, to a better source in Suwayomi: survey, measure, search, sample (pages through the reader API, judged by the better-copy rule, nothing stored), then switch, replace and retire by hand. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#source-review)

## Services

Suwayomi and Kavita are reached through 1 HTTP layer: what is retried, when a service counts as unreachable, how Kavita's paths map to local ones, and the read-only `smoke` check. Rules: [docs/spec/services.md](docs/spec/services.md#services)

## Junk decisions

A chapter's junk units are matched against stored decisions, per signature, and against what 2 or more other series agree on; Claude decides the rest through `junk review` while the chapter waits; the bulk converter's decisions, imported once, stay as knowledge; decisions keep their history and can be listed, shown, re-decided, undone and backed up; `junk cut` proposes and records a cut or a card's side from span sheets; the chapter edit tells convert what to drop, crop, join or cut. Rules: [docs/spec/junk.md](docs/spec/junk.md#junk-decisions)

## Metadata

`ComicInfo.xml` holds everything it can except `Count`; what Kavita needs beyond the file goes through the Kavita API. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#metadata)

## Source ids

Every id a series holds for a source, from any origin, must agree, else the series is `needs_human`; a person's id wins; a source with no id is searched by title. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#source-ids)

## Tag rules

Hand-written rule files in the program; a series' genres and tags come from its stored records through consolidation, derived tags and a top-up limited to the rule files' names; a change to them recomputes every series' tags from stored source data. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#tag-rules)

## Metadata checks

How often a series is checked depends on the time since its last published update; no series is exempt; a check fetches every source (and a Tapas or Webtoons series' episode list, for its volumes and titles), stores what it found and picks the cover; the old tool's ids are imported, and `metadata audit` lists disagreements before cutover. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#metadata-checks)

## Retroactive updates

A check can change what is already published: series-level fields go through the Kavita API; a file is rewritten (and renamed) only when its volume is filled, its override file's range or volume start gives another, its cover should change or a hand edit (`series edit`) changes its title or volume, with the reader's progress carried, and pages are never re-encoded; `series rename` gives a series its name (Series names) in Kavita, its files and its folder; a run of published parts N.1 … N.k is joined into chapter N (Split chapters). Rules: [docs/spec/metadata.md](docs/spec/metadata.md#retroactive-updates)

## Publishing

A finished CBZ is staged on the share and renamed into its series folder under the bulk converter's file name (`Vol.<v> Ch.<n> - <title>.cbz`); a new series lands whole with 1 library scan; the download goes to the retention folder once Kavita has indexed the file. Rules: [docs/spec/publishing.md](docs/spec/publishing.md#publishing)

## Config

`config.yaml.example` is the schema; each key comes from the file, a `MANGA_PIPELINE_` variable or `--set`, in rising priority; each key is machine-local or shared, and shared keys must match across machines. Rules and key table: [docs/spec/runtime.md](docs/spec/runtime.md#config)

## Logging

Console, plus 1 daily-rotated log file per command that runs under its own lock. Rules: [docs/spec/runtime.md](docs/spec/runtime.md#logging)

## Run lock

Each command names the locks it runs under, or none; the locks are database session locks, held across machines; a command that finds its lock held exits 3 at once. Rules: [docs/spec/runtime.md](docs/spec/runtime.md#run-lock)

## Preflight

Hard and soft requirement checks before the work; a hard problem exits 4. Rules and check table: [docs/spec/runtime.md](docs/spec/runtime.md#preflight)

## CLI

The commands, `--set`, and the exit codes. Rules: [docs/spec/runtime.md](docs/spec/runtime.md#cli)

## Tests

Tests are offline and never read the live `config.yaml`. Rules: [docs/spec/runtime.md](docs/spec/runtime.md#tests)
