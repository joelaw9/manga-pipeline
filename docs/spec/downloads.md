# manga_pipeline spec — Downloads and Suwayomi

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Bad downloads

A chapter whose download is bad (see "Validation") is downloaded again through Suwayomi, first from the same Suwayomi chapter once, then from other sources by rank. The rejected CBZ is removed the retention way (SPEC "Publishing"), then deleted through Suwayomi, which clears its downloaded mark (the file being gone already is no error to Suwayomi). The rejection is recorded as a handled chapter (`rejected`) with the rejected file's digest, under the chapter's own identity. Nothing is added to Suwayomi's library: a source search stores its hits outside it, and their chapters can be downloaded from there.

`sources.ranking` lists source display names (`"<name> (<LANG>)"`), best first. The next source is the highest-ranked installed one not tried yet for the chapter and not the original's; an unranked source is never picked automatically. On it the series is searched by each of its Kavita names and the manga's title; the hits of all searches are pooled, and the series is the 1 hit whose title equals a known name under letters-and-digits lower-casing, else the 1 hit whose fuzzy ratio to a known name is at least 90 with every other hit at least 10 below; else the source is skipped. The chapter is the source's 1 chapter whose parsed season, side label and number equal the chapter's (more than 1: the one with the chapter's scanlator, else the source is skipped), and the source's numbering must agree with the original's: of the original source's chapters of the same season and side label numbered within 10 of the chapter's, at least 3, and at least 90 % of them listed on the other source under the same season, side label and number (with titles, where both have one, at a fuzzy ratio of at least 80); else the source is skipped. A Suwayomi chapter another open chapter holds (as its download, or one it expects) is never picked: its source is skipped. A skipped or failed source counts as tried. A source that cannot answer now is passed over without counting as tried; when every remaining source cannot answer, the chapter fails (1 attempt). When no source is left, the chapter becomes `needs_human`, naming the sources tried.

After queueing the download, the pipeline starts Suwayomi's downloader, and the chapter waits on `suwayomi_download` with the download it expects (Suwayomi chapter, source, the path Suwayomi will write, when it was queued). The pipeline records the expected download before it queues it, so a download Suwayomi makes is never untracked. `intake` ends the wait when the expected chapter is flagged downloaded, its queue entry is in error, it is neither queued nor downloaded (then it is queued again, and the wait goes on), or, while not downloaded, `sources.download_timeout_hours` have passed since it was queued; Suwayomi unreachable during this check ends `intake`'s run. Then validate looks at it in place, without going back to discover: a download flagged with its file present is validated as the chapter's new file, and only when it passes does the chapter take its path, Suwayomi chapter, source and digest; its identity never changes, whatever source, title or numbering it arrived under. A bad replacement is rejected like the first copy, and the next source is tried. A download in error, missing, flagged without its file, or timed out is removed from Suwayomi's queue, its file (if any) removed as above, and the next source is tried. A path an open chapter holds or expects is never taken as a new download nor listed as not taken.

A person settles a chapter that is `needs_human` at validate with `chapter use CHAPTER_ID SUWAYOMI_CHAPTER_ID` (that Suwayomi chapter is the replacement: the file in hand, if still there, is rejected, and the next `intake` queues the chosen one; a chosen chapter that does not exist, that another open chapter holds, or whose download is numbered otherwise (season, side label or number) leaves the chapter `needs_human`) or `chapter drop CHAPTER_ID` (also at discover or junk: the download, if still there, goes the retention way, then the chapter is done and recorded as a handled chapter, `dropped`, with its digest). Both take no lock and are refused with exit 2 unless the chapter is `needs_human` at those stages; `chapter drop` is also refused while a re-download still has a copy to remove or a download it expects (`retry` lets `intake` finish them). `retry` on a chapter being re-downloaded starts the source search again from the top (same source first) and forgets a person's choice; what it rejected stays recorded. A download numbered otherwise than its chapter is held at validate on every look, so a retry holds it again.

The re-download state is workflow state: `state reset` clears it. A replacement already queued then arrives as an ordinary download when its manga is in Suwayomi's library, else is listed by `status` as not taken.

### Validation

Validate reads every page of the download in full, in archive order, in this order of checks: each page's bytes and format, page by page; then every page decoded; then every page's orientation; then the series-wide checks. The first defect found decides. The page measurements (bytes, sha1, width, height, format, blank flag, pHash, junk signature, colour class, chroma) are stored for `process`; they describe the download's pages and are never changed afterwards.

| Found | Outcome |
|---|---|
| an archive that cannot be read (broken, truncated, CRC error, encrypted), no pages, a zero-byte page, a page of a known format that does not decode | bad download: re-downloaded (above) |
| a page of unknown format (by its first bytes), or JPEG XL | `needs_human` |
| a page with an EXIF orientation that turns or mirrors it (2 to 8) | `needs_human` |
| mislabelled: at least 90 % of its non-blank pages within pHash Hamming distance 6 of a non-blank page of the series' published files with another season, side label or number | `needs_human` |
| the same page bytes twice | noted (`status`), kept |
| short: strip length (sum of height × 1000 / width) below 40 % of the median of the series' published files | noted (`status`), published |

The mislabel and short checks run once the series has at least 3 published files with stored page measurements (pHashes for the one, strip length for the other); a published file with none stored does not count. The pipeline stores them when it publishes a chapter it measured; the bulk converter's files have none. Junk members, folders, wrong extensions and page names are not defects: `process` writes the pages anew.

A download whose name, without its scanlator prefix and chapter number, is one of `validate.non_chapter_names` (a notice or hiatus page) is dropped at discover, removed the retention way and recorded as a handled chapter, `dropped`. An archive that cannot be read is not judged at discover: the chapter takes its number from its name alone and goes on to validate.

### Library copies

When a chapter reaches junk (after the series' baseline exists) and the series already has a published file with the chapter's season, side label and number, still in the library:

- 2 or more such files: `needs_human`.
- Not the same chapter: `needs_human`. The same chapter = its page count (series layout `pages`) or strip length (layout `strip`) within 15 % of the published copy's original.
- A better copy: `needs_human` naming the published file; the published file stays as it is. Better = its median page width at least 1.05 × the published copy's original, or at least 0.95 × and from a higher-ranked source.
- Otherwise the download is a duplicate: dropped, removed the retention way and recorded as a handled chapter, `duplicate`.

The published copy's original is what the pipeline stored when it published that file. A published file with no stored original (the bulk converter's, or one whose chapter had no page measurements when it was published) is measured from the library file itself: its page count and its pages' widths and heights, read from each page's whole bytes without decoding it; its junk was removed, so it is shorter than its original. Ranks: the sources of `sources.ranking` in order, then other sources (a source recorded by its Suwayomi id rather than its display name among them), then the bulk converter's files.

A chapter's identity is its scanlator, season, side-story label, number and title key, parsed from the chapter's name (its `ComicInfo.xml` `Number` gives the number when the name has none); scanlator and title compare by letters and digits only, lower-cased. Suwayomi's own chapter number is never part of it: it is per source, and numbers restart per season in some series. A number whose decimal part ends in 0 without being all zeros (26.10, written 26-10 by some sources; not 26.0) has no single reading, 26.1 or the 10th part of 26: the download is `needs_human` at discover, unless its pages are a handled chapter's (below), and it is never taken for chapter 26.1, as a first download or as a replacement. The identity is settled at the discover stage and never changes, whatever source a replacement comes from.

The pipeline records every chapter it has handled (published, rejected, duplicate or dropped) with the content digest of its download: the sha256 of the ordered list of its pages' sha1s, so a re-zipped copy with the same images has the same digest. A download whose digest equals a handled chapter's of the same series is the same pages again: it is put aside (moved to the retention folder, SPEC "Publishing") and not processed: the chapter is dropped, and no second handled chapter is recorded, since the digest already is. A handled chapter recorded without a digest (an archive that could not be read) matches nothing. This is checked at discover, and again at junk once the series' baseline exists. A chapter is recorded as handled when it is published, in the same transaction as the rename's record, so a crash or a `state reset` never publishes it twice.

## Suwayomi

- `paths.downloads` is Suwayomi's download root; downloads lie under its `mangas` folder.
- Suwayomi writes a CBZ under its final name while it is still incomplete, and flags the chapter downloaded only once the file is closed. `intake` finds downloads by listing the folder, maps each manga folder to a manga of Suwayomi's library by rebuilding the source and title folder names, matches each file to its chapter by rebuilding the file name from the chapter's scanlator and name, and takes it only when the chapter is flagged downloaded; an unflagged file is skipped without a word. A file becomes a chapter only when no unfinished chapter has its path and it is still there once that is known (a confirm may move it away meanwhile). Listing the folder opens no CBZ.
- A download that cannot be processed is not taken and is listed by `status` with its reason and when it was first seen: a folder no library manga maps to, a file no chapter rebuilds to, a file 2 chapters rebuild to, or a series that is new (SPEC "Series identification"). A download of a series out of scope is not listed.
- Suwayomi's own updater skips series it marks completed. When a completed series' metadata check is due, `intake` asks Suwayomi to refresh that series' chapter list, so an epilogue or side story is found and downloaded.

## Series identification

A Suwayomi manga with downloads is matched to a series in this order, and the match is stored:

1. the series a stored link names (a link made earlier by this order, or by `series link`);
2. a source id of the manga equal to one stored for a series;
3. exactly 1 series of the library `kavita.library_id` whose name, localized name or original name equals the manga's title under Kavita's name normalising.

A series found by step 3 that already has a row (another source's manga of the same series) is that row. 2 or more strict hits, or a near hit (a series whose name equals the title under a lenient key: Unicode NFKC, case-folded, letters and digits only) make the manga's series `needs_human`, naming the candidates; its chapters are held. A manga with no hit at all is a new series, which is not processed yet. A series whose files lie in no single folder (SPEC "Services") is `needs_human`.

A person links a manga with `series link MANGA_ID KAVITA_SERIES_ID`: the Kavita series must be in `kavita.library_id` and have a folder. The series row holding that Kavita id, else the manga's held row (no Kavita id), else a new row gets the Kavita id, names and folder, and the manga is linked to it. A held row of the manga (no Kavita id) is merged into that row: its chapters and links move there (a chapter past junk goes back to junk, so the series' baseline and handled chapters apply) and it is deleted. A manga whose row is another Kavita series' row with chapters is refused. Run again, it refreshes a series' stored names and folder. `retry --series` then clears `needs_human`.
