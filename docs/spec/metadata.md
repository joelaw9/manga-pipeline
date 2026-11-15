# manga_pipeline spec — Metadata

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Metadata

Everything `ComicInfo.xml` can hold is written there, except `Count`: a completed series' publication status is set through the Kavita API and locked, so its chapters are not all rewritten when 1 more arrives. What Kavita needs beyond the file is written through the Kavita API; for a new series, once Kavita has indexed it. A CBZ that arrives without a `ComicInfo.xml` gets one.

### Series fields

A series' fields come from its stored records alone:

| Field | From |
|---|---|
| summary | the first of AniList, MangaDex (English), MangaBaka, MangaUpdates that has one, as plain text (HTML and Markdown read by their parsers; a MangaDex summary ends at its first horizontal rule; a closing "(Source: …)" note dropped) |
| genres, tags | SPEC "Tag rules" |
| publication status | MangaBaka: releasing or upcoming = Ongoing, hiatus = Hiatus, completed = Completed, cancelled = Cancelled; never Ended |
| age rating | MangaBaka's content rating: safe = Everyone, suggestive = Teen, erotica = Mature 17+, pornographic = Adults Only 18+ |
| language | MangaDex's original language |
| writers, pencillers | AniList's staff in the role Story or Art, and Story & Art for both |
| web links | 1 page per source whose record was found, and MyAnimeList |
| provider ids | AniList, MyAnimeList and MangaBaka ids (SPEC "Source ids") |

`export` writes a series' fields to Kavita, each locked, when the data version, the tag rules or the code that makes and writes them changed since its last write, its cover changed, or Kavita holds the series under a new id; it writes only the fields Kavita does not already hold. It writes nothing while a library scan is awaited (SPEC "Publishing"): Kavita holds such writes back. A field with no value at the sources is left as it is. Web links are only added (1 per source), and so are writers and pencillers: Kavita keeps the people its scans read from the files whatever an update sends. Lists are compared under Kavita's name normalising. Each field is recorded as soon as Kavita accepts it. The cover is uploaded when Kavita does not hold the series' current cover from the pipeline, and Kavita must then serve it; a cover uploaded to Kavita before the pipeline's first upload, and not by the old tool, is a person's, kept and reported once (Kavita names every upload alike, so a person's upload after the pipeline's is not told apart). A series Kavita no longer holds is not written.

A locked Kavita field is overwritten only when it holds the value the pipeline last wrote there, or, for a field the pipeline has never written to the series, a value the old metadata tool wrote (its summary, genres, tags, and the publication status it set). Any other locked value is a person's edit: it is kept, and reported once in the log.

## Source ids

A series' metadata comes from 4 sources: AniList, MangaUpdates, MangaDex and MangaBaka; MyAnimeList ids are kept too (Kavita's `malId`), though MyAnimeList is never fetched. Each id a series holds is stored with its origin:

| Origin | Ids |
|---|---|
| `suwayomi` | a linked Suwayomi manga's tracker records (AniList, MangaUpdates) and the MangaDex id in its URL |
| `kavita` | the series' Kavita web links and its `aniListId`, `malId`, `mangaBakaId` |
| `anilist` | the MyAnimeList id of the fetched AniList record |
| `mangabaka` | the cross-links of the fetched MangaBaka record (AniList, MangaUpdates, MyAnimeList) |
| `mangadex` | the cross-links of the fetched MangaDex record (AniList, MangaUpdates, MyAnimeList) |
| `search` | found by a title search |
| `import` | the old metadata tool's state file (SPEC "Metadata checks", import) |
| `human` | `series set-id SERIES SOURCE ID` |

Ids are compared in 1 form per source: a MangaUpdates id as the number its base-36 text spells (an all-digit MangaUpdates link is a retired legacy id and is ignored), a MangaDex id as its UUID. Every id a series holds for 1 source must be the same, whatever its origin; when 2 differ, the series is `needs_human`, naming the source and each id with its origin, and its check stops there. A `human` id wins: for that source no other origin is compared. `series set-id` stores it (refused with exit 2 for a series that does not exist, a source not in the list above, or an id that is not that source's form), and `retry --series` then clears `needs_human`. 2 series holding the same id is not a disagreement: each series is judged alone, and Series identification treats that id as a double hit.

A source a series has no id for is searched by the series' names: its Kavita name, localized name and original name, or, for a series Kavita does not hold, the names it was found by (the Suwayomi title; a hand-placed folder's title folder name and `ComicInfo.xml` `Series`). In this order: MangaBaka through its AniList or MangaUpdates id, and by a title search only when the series holds neither or they lead to a novel; MangaUpdates by title; MangaDex by its MangaUpdates link; AniList by title. A search hit is accepted only when exactly 1 result's title or alternative title equals a name under the lenient key (SPEC "Series identification"); for MangaBaka, also its top result for the first name when that result is close to a name (`rapidfuzz` ratio of the lenient keys at least 90, or 1 key of at least 15 characters starting the other) and no other result is; for MangaUpdates, also 1 of its top 3 results whose associated names hold a name. A novel never matches, and a record a check fetches by an id that turns out to be a novel's is not used and is logged: that source has no record until a person fixes the id (`series set-id`). An accepted id has origin `search`. A source whose search finds nothing is recorded as such for the check; every later check searches again.

## Tag rules

The tag rules (consolidation, derived tags, promoted tags, allowlist, top-up exclusions) are files of the program, written by hand. A change to them reaches every series: the sync records hold a fingerprint of the rule files, and when it changes, each series' tags are worked out again from the stored source data, with no source lookups. Series whose tags change are rewritten (SPEC "Retroactive updates").

The rule files are part of the program (`manga_pipeline/metadata/tag_rules/`, so the fleet's version covers them): `consolidation.csv` (a name of a kind becomes another name or kind, or is removed), `derived_tags.yaml` (a tag or genre given when a series' terms match a rule), `promote_tags.txt` (a tag given whenever the series' rule terms (step 3) hold it, at a minimum MangaBaka weight when 1 is named; a term from another source, or from MangaBaka's genres, status or type, counts at any weight, unless it is spelt exactly as a MangaBaka tag: MangaBaka carries MangaUpdates' categories and weights the low-vote ones incidental, so a category it repeats keeps MangaBaka's weight), `tag_allowlist.txt` (tags a top-up may add) and `top_up_exclude.txt` (names a top-up never adds). The fingerprint is the sha256 of the 5 files and of the code that applies them (the tag module and its name key), so a change to either recomputes every series. A rule file that cannot be read as its kind (an unknown kind or weight, a rule without terms) stops the run, naming the file and the entry.

A series' genres and tags are worked out from its stored records, in this order:

1. The sources' lists. Genres come from MangaBaka (or from MangaUpdates when MangaBaka does not know the series), AniList, and MangaDex's genre tags. Tags come from AniList (rank at least 60, not a spoiler) and MangaDex's theme tags; a tag equal to a genre is dropped.
2. Consolidation. Every name goes through the table, under its own kind, else the other. A MangaUpdates plural marker (`Angel/s`, `Elf/ves`), a final `s` after 4 letters, case and punctuation do not tell names apart; a tag equal to a genre is dropped, here and after step 3.
3. Derived and promoted tags. Rules match the series' terms:
   - MangaBaka's genres, tags, status, content rating and type;
   - MangaUpdates' categories and genres, too, when MangaBaka has fewer than 10 tags;
   - MangaUpdates' terms and type alone, when MangaBaka does not know the series.
4. The top-up, which brings a series to 4 genres and 6 tags. It draws from MangaBaka's genres and its tags of core, defining, recurrent or no weight, and adds only names that are a consolidation target or in the allowlist and not excluded. A series still short tries again with the MangaUpdates categories MangaBaka lacks. Past the minimum, only allowlisted tags and tags the table makes genres are added, and only at a core, defining or recurrent weight.

A top-up never adds a name because other series of the library use it, so a series' lists never depend on the order series are written.

A fix a person makes while answering a `needs_human` item, such as a series' source ID, is stored in the database.

## Metadata checks

How often a series is checked against its sources depends on how long ago its last published update was, not on its status alone. A series marked complete still receives epilogues, side stories and tag changes, so no series is ever exempt.

| Last published update | Checked |
|---|---|
| less than 6 months ago | weekly |
| 6 months ago or more | monthly |
| 12 months ago or more, and marked complete | yearly |

A new chapter arriving for a series makes its check due at once. The periods and intervals are settings (`metadata.*`, SPEC "Config"). The last published update is the newest chapter upload date Suwayomi holds for a manga linked to the series; for a series with no linked manga, the publish date of its newest MangaDex chapter; with neither, it counts as 6 months ago or more. A series is marked complete when a linked manga's Suwayomi status is completed, or its MangaBaka status is. Kavita's dates and a hand-placed file's time never count.

Every series of `kavita.library_id` in scope (`series.only`) is checked. `intake` lists the library each run and stores each series it does not hold, with its names; the folder of a Kavita series is fetched when first needed (its first download). A listed series is matched to a row by its Kavita id, else, for a row whose Kavita id is no longer listed, by its names, confirmed by its folder (a row with no folder yet: by its names alone, when exactly 1 such row matches) (Kavita recreated the series under a new id: the row takes the new id, and this is reported in the log); else it is a new row. A series' folder that cannot be found does not stop its check; a series Kavita no longer holds is not checked, and its next check moves by the quiet interval.

`intake` runs the checks before its chapter work, and a series' due check before its new chapters are validated. Kavita or Suwayomi unreachable while the library is listed skips the checks for the run; the chapter work goes on. A check made due by a new chapter, and a series' first check, always run; of the others at most `metadata.checks_per_run` run, the longest due first. A check:

1. gathers the series' ids from its linked Suwayomi manga and its Kavita record (SPEC "Source ids");
2. fetches MangaBaka (by id, else found as Source ids says), then the other sources' records by id, searching for those with none: MangaUpdates' series record, MangaDex's series record, volume aggregate and cover list, AniList's media record with its staff; the cross-links of each record add ids, and every id is compared (SPEC "Source ids") before it is used;
3. stores each fetched record with its hash, and "not found" for a source with no id after its search;
4. picks the series cover (below) and fetches it;
5. completes: the series' data version rises by 1 when any stored hash changed, and on its first completed check whatever it found; the next check is set by the table above.

A service that cannot be reached, or answers with an error or a reply of an unexpected shape, ends the check storing nothing: it stays due for the next run, and it never holds the series' chapters or stops the run (a failing share does). Each check is 1 log line: the series, the sources fetched, found or not, and whether the data changed.

When a completed series' check is due, its Suwayomi chapter list is refreshed (SPEC "Suwayomi").

### Covers

A series' cover is the Suwayomi thumbnail of a linked manga when it is at least 1500 px wide; else a MangaDex cover, the first of: English volume 1, the lowest-numbered English volume, volume 1 in the series' original language, volume 1 in any language, the series' main cover; else the wider of the AniList cover and the Suwayomi thumbnail. A series with no linked manga skips the Suwayomi steps; with no source either, it has no cover. An image that cannot be read (an error page served as the image), or one its host no longer has, is skipped as if the source had none. The chosen image is stored as `series/<sha256>.<ext>` under `paths.covers` and recorded with its source URL; a file there no series names is deleted once it is a day old.

### Import

`metadata import STATE_FILE CONFIG_FILE` reads the old metadata tool's state file (`.sync_state.json`) and its config's `map` and `overrides`. It first brings the series rows in step with the library, as `intake` does, and imports only series that exist in the library and are in scope, reporting the rest. From the state file: each id, with origin `import` (a `null` id, "searched, nothing found", is skipped: every check searches again); each cover's hash, as the cover Kavita already holds; and which Kavita fields the old tool wrote and the publication status it set (SPEC "Retroactive updates" reads them). `map` (a JSON file, its path relative to the config's folder) and `overrides` (both Kavita series name to Suwayomi manga id or title) become links of those manga to those series. It can run again: it replaces the ids it imported before, and never touches another origin's. An imported series holds no fetched data, so its first check is due.

### Audit

`metadata audit` is read-only, opens no database and ignores `series.only`. For every series of the library it gathers each origin's ids itself (Kavita, Suwayomi trackers of the manga matched to it, the state file given by `--state PATH`, and MangaBaka's and MangaDex's cross-links, fetched at their rate limits), and lists the series whose ids disagree, per source the series no origin gives an id for, and the Suwayomi library manga no series matches by name.

## Retroactive updates

A check can change what already-published chapters and series should carry: a moved volume or season boundary, a status change, the chapter count once a series completes, a changed chapter title.

The pipeline records what it wrote for every published chapter and every series, and the version of the fetched data it wrote from. Whatever was written from an older version than the one now fetched is worked out again. Series-level fields (summary, genres, tags, status, age rating, people, language, links) are rewritten through the Kavita API, where they are locked. A published chapter goes back to the comicinfo stage and is republished over its file only when a value Kavita reads from each file would differ: `Series`, `Volume`, `Number`, `Title` or its date. The rewritten files are indexed by the run's 1 library scan, after all its rewrites, and a file rewritten for that reason carries the current series-level fields too. A retroactive update never re-encodes pages: the rewrite takes the CBZ from the library, replaces its `ComicInfo.xml`, copies the pages untouched, and renames the result over the file.

A published chapter the bulk converter wrote is compared by the values its series record holds for it, so only a real difference causes a rewrite.

A check never renames a series. Its `Series` name stays the one Kavita has; a changed title at the sources is reported (log, `status`, Discord) and changes nothing.
