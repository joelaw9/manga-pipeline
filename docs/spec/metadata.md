# manga_pipeline spec — Metadata

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Metadata

Everything `ComicInfo.xml` can hold is written there, except `Count`: a completed series' publication status is set through the Kavita API and locked, so its chapters are not all rewritten when 1 more arrives. What Kavita needs beyond the file is written through the Kavita API; for a new series, once Kavita has indexed it. A CBZ that arrives without a `ComicInfo.xml` gets one.

## Tag rules

The tag rules (consolidation, derived tags, promoted tags, allowlist) are files in the repository, written by hand. A change to them reaches every series: the sync records hold a fingerprint of the rule files, and when it changes, each series' tags are worked out again from the stored source data, with no source lookups. Series whose tags change are rewritten (SPEC "Retroactive updates").

A fix a person makes while answering a `needs_human` item, such as a series' source ID, is stored in the database.

## Metadata checks

How often a series is checked against its sources depends on how long ago its last published update was, not on its status alone. A series marked complete still receives epilogues, side stories and tag changes, so no series is ever exempt.

| Last published update | Checked |
|---|---|
| less than 6 months ago | weekly |
| 6 months ago or more | monthly |
| 12 months ago or more, and marked complete | yearly |

A new chapter arriving for a series makes its check due at once. The periods and intervals are settings.

## Retroactive updates

A check can change what already-published chapters and series should carry: a moved volume or season boundary, a status change, the chapter count once a series completes, a changed chapter title.

The pipeline records what it wrote for every published chapter and every series, and the version of the fetched data it wrote from. Whatever was written from an older version than the one now fetched is worked out again. Series-level fields (summary, genres, tags, status, age rating, people, language, links) are rewritten through the Kavita API, where they are locked. A published chapter goes back to the comicinfo stage and is republished over its file only when a value Kavita reads from each file would differ: `Series`, `Volume`, `Number`, `Title` or its date. The rewritten files are indexed by the run's 1 library scan, after all its rewrites, and a file rewritten for that reason carries the current series-level fields too. A retroactive update never re-encodes pages: the rewrite takes the CBZ from the library, replaces its `ComicInfo.xml`, copies the pages untouched, and renames the result over the file.

A published chapter the bulk converter wrote is compared by the values its series record holds for it, so only a real difference causes a rewrite.

A check never renames a series. Its `Series` name stays the one Kavita has; a changed title at the sources is reported (log, `status`, Discord) and changes nothing.
