# manga_pipeline spec — Parts, state and chapter stages

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Parts

The pipeline is 3 commands, each working the chapters at its own stages. The database is the hand-off between them: a chapter belongs to 1 part at a time, by its stage, and a part claims it before working on it. No part calls another, and nothing sits above them.

| Part | Work | Talks to |
|---|---|---|
| `intake` | resolves `suwayomi_download` waits; discovers new CBZ files; validates; identifies the series; runs metadata checks | Suwayomi, the metadata sources, Kavita (read only, for a series the database has not seen) |
| `process` | resolves `bulk_conversion` and `junk_decision` waits; junk; convert | the share only |
| `export` | resolves `kavita_scan` waits; comicinfo; publish; confirm; Kavita API writes; retroactive updates; empties the retention folder | Kavita, the share |

On the LXC a systemd timer starts each part, and `junk review`, every 10 minutes, at staggered minutes. Parts run at the same time as each other. A part never runs twice at once (SPEC "Run lock"), and a part with nothing to do exits 0 at once. `process` runs at low CPU and disk priority. On Windows the commands are run by hand.

Order of work:

- `intake`, per series: a new series gets its full metadata retrieval; a metadata check runs if one is due (SPEC "Metadata checks"); then each discovered chapter is validated. Series due a check that have no new chapters come last.
- `process` and `export` pick by priority: chapters of series Kavita already knows before chapters of new series, then the oldest first. They pick again after every chapter, so a new chapter of an ongoing series never waits behind a long new series, and they run until nothing is ready.
- `process` runs the junk stage for every ready chapter of a series before it converts any of them, so what repeats across chapters is seen before a decision is made.
- `export` works in this order each run: it ends `kavita_scan` waits, adds `ComicInfo.xml` to the ready chapters, then publishes series by series with 1 scan per series.

With `series.only` set, every part works only the series it names; nothing else is read or written.

Hand-off between parts:

- `intake` stores what validation measured (page sizes and hashes) in the database. `process` reuses it, and reads the CBZ from the downloads folder itself.
- `process` works on a chapter in a scratch folder on local disk and deletes it when the chapter leaves `process`, so local disk holds only the chapters in hand. It writes the finished chapter as a CBZ of pages into the staging folder on the share. `export` adds the `ComicInfo.xml` to that file and renames it into the library; the pages are packed once. Finished chapters therefore wait on the share, not on the machine's disk.
- A chapter waiting on a junk decision keeps only its candidate page images, in a candidates folder, until they are decided.
- A part works only on chapters that are ready for its own stages; a chapter waiting on another part is not its concern.

One chapter that fails or waits never stops a part or the chapters after it. Later chapters of the series are published while an earlier one is held.

When Suwayomi, Kavita, a metadata source or the share cannot be reached, the chapters and series that need it wait and are tried again by a later run of the part. That does not count as an attempt. For the rest of the run the part claims no chapter at the stage that met the unreachable service, and goes on with its other stages. For the share, "cannot be reached" is an input/output, stale-handle or timeout error on one of its paths.

## State

One SQLite database on the local disk of the machine that runs the pipeline, never on the share. It holds 3 kinds of data:

| Kind | What |
|---|---|
| workflow state | every chapter with its stage, what it waits on, its attempts and its last error; each series' status |
| knowledge | the junk decisions; each series' source IDs and conversion calibration |
| sync records | what was fetched for each series, from which source and when (written only by `intake`, with a version that rises on every change); what was written for each series and each published chapter (written only by `export`, with the version it was written from) |

It is the single record of what is pending. `state reset` clears the workflow state only; knowledge and sync records are kept, and the next `intake` rediscovers the downloads. It needs `--yes`, runs only while no part and no junk review is running, and also empties the staging and scratch folders. It never touches the downloads, the library or the retention folder.

A part claims a chapter in 1 short transaction before working on it and records the outcome in another; no transaction is open while a stage works. A claim left by a run that died is released when that part next starts. A command without a lock refuses a chapter a part has claimed, and an outcome is not recorded over a change a person made meanwhile.

The database's structure is built by ordered schema steps, each recorded once applied. A database holding a step the program does not know is refused, and nothing ever recreates a database by itself.

1 database owns the share at a time: a file in the staging folder holds the owning database's id, written by `state adopt --yes`. A part that finds the file missing (the share is not mounted, or was never adopted) or holding another id stops before any work (SPEC "Preflight").

## Chapter stages

A chapter is at 1 stage and has 1 status: `ready`, `claimed`, `waiting`, `needs_human` or `done`. A stage ends done, waiting (with what it waits on) or failed. A chapter that failed is tried again by a later run of its part, never in the same run. A chapter that reaches its attempt limit (`workflow.attempt_limit`) becomes `needs_human` and is left alone until a person acts. A series can be `needs_human` too (for example when its source IDs disagree); its chapters wait until a person clears it.

| Stage | Part | Does | Can wait on |
|---|---|---|---|
| discover | `intake` | accepts a CBZ once Suwayomi reports the download complete | |
| validate | `intake` | finds a broken archive, an undecodable or missing page, a bad download, a duplicate | `suwayomi_download` |
| junk | `process` | finds candidate junk pages (scanlation advertisement and credit pages) and applies the stored decisions | `bulk_conversion`, `junk_decision` |
| convert | `process` | re-encodes the pages (AVIF where it passes the quality gates) and writes the CBZ of pages to the staging folder | |
| comicinfo | `export` | adds `ComicInfo.xml` to the CBZ | |
| publish | `export` | renames the CBZ into the library and asks Kavita to scan | `kavita_scan` |
| confirm | `export` | checks Kavita indexed the file, then deletes the download (a rewrite has none) | |

The CBZ in the downloads folder is never modified. Work happens in the scratch folder.

## Needs human

A chapter or a series that becomes `needs_human` is reported 3 ways: a line in the log, the `status` command, and a message to a Discord webhook.

A person clears it with `retry` (a chapter by its id, or `--series` for a series and its chapters): the item is ready again at the stage where it stopped, with its attempts at 0. With no database, an id that does not exist, or an item that is not `needs_human` (a chapter a part has claimed included), is refused with exit code 2 and left unchanged; `--series` is refused when neither the series nor any of its chapters is `needs_human`.
