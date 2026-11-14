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
- Series written by the bulk converter carry its series record, `.optimizer.json`, in the series folder. A chapter of a library series that the pipeline did not create and that has no record waits (`bulk_conversion`), once it has passed validation, until the record exists. The pipeline reads the record and never writes it.

## Parts

3 commands (`intake`, `process`, `export`) share the work by chapter stage; the database is the only hand-off, and each runs from its own 10-minute timer; `process` runs on several machines, never 2 in 1 series at once. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#parts)

## State

1 PostgreSQL 17 database on a server holds workflow state, knowledge and sync records; its write transactions run 1 at a time; a claim names its owner and count and is freed when its owner's session ends; paths are stored relative to their share root and times from the server's clock; `state reset` clears only the workflow state. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#state)

## Chapter stages

discover, validate, junk, convert, comicinfo, publish, confirm. A stage ends done, waiting or failed; a chapter past its attempt limit is `needs_human`. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#chapter-stages)

## Conversion

The convert stage re-encodes a chapter with the bulk converter's tools, route and gates (AVIF where the quality gates pass, else lossless, else untouched) into a store-mode CBZ in staging; it never writes the series folder. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#conversion)

## Calibration

AVIF quality per series and encode group: taken from the bulk-converter record, or computed from samples of the first chapters converted. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#calibration)

## Needs human

A chapter or series that needs a person is reported 3 ways (log, `status`, Discord) and cleared with `retry`. Rules: [docs/spec/pipeline.md](docs/spec/pipeline.md#needs-human)

## Bad downloads

Validation reads every page; a bad download is removed and downloaded again through Suwayomi (same chapter once, then other sources by rank), other defects need a human; a copy of a published chapter is dropped or held; `chapter use` and `chapter drop` settle a held chapter; handled chapters are remembered with their content digest, so the same pages appearing again are put aside, not processed twice. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#bad-downloads)

## Suwayomi

A download is taken only once Suwayomi flags its chapter downloaded; a completed series is refreshed by `intake` when its check is due. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#suwayomi)

## Series identification

A Suwayomi manga is matched to a series by a stored link, a source id, or exactly 1 Kavita name; a near or double hit is `needs_human`, fixed with `series link`. Rules: [docs/spec/downloads.md](docs/spec/downloads.md#series-identification)

## Services

Suwayomi and Kavita are reached through 1 HTTP layer: what is retried, when a service counts as unreachable, how Kavita's paths map to local ones, and the read-only `smoke` check. Rules: [docs/spec/services.md](docs/spec/services.md#services)

## Junk decisions

A chapter's junk units are matched against stored decisions, per signature; Claude decides the rest through `junk review` while the chapter waits; decisions are imported from the bulk converter; the chapter edit tells convert what to drop, crop, join or cut. Rules: [docs/spec/junk.md](docs/spec/junk.md#junk-decisions)

## Metadata

`ComicInfo.xml` holds everything it can except `Count`; what Kavita needs beyond the file goes through the Kavita API. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#metadata)

## Tag rules

Hand-written rule files in the repository; a change to them recomputes every series' tags from stored source data. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#tag-rules)

## Metadata checks

How often a series is checked depends on the time since its last published update; no series is exempt. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#metadata-checks)

## Retroactive updates

A check can change what is already published: series-level fields go through the Kavita API, a file is rewritten only when a per-file value changes, and pages are never re-encoded. Rules: [docs/spec/metadata.md](docs/spec/metadata.md#retroactive-updates)

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
