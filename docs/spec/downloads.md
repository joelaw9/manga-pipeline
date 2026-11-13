# manga_pipeline spec — Downloads and Suwayomi

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Bad downloads

A chapter that fails validation is downloaded again through Suwayomi from another source. The rejected CBZ is deleted through Suwayomi, which also clears its downloaded mark. The pipeline picks the source itself; a chapter it cannot match with confidence on another source becomes `needs_human`. The chapter waits on `suwayomi_download` until Suwayomi reports the new download complete, then starts again at discover. The wait records which download it expects, so the replacement is taken as the same chapter whatever source, title or numbering it arrives under.

A chapter's identity is its scanlator, season, side-story label, number and title key, parsed from the chapter's name (its `ComicInfo.xml` `Number` gives the number when the name has none); scanlator and title compare by letters and digits only, lower-cased. Suwayomi's own chapter number is never part of it: it is per source, and numbers restart per season in some series. The identity is settled at the discover stage and never changes, whatever source a replacement comes from.

The pipeline records every chapter it has handled (published, rejected, duplicate or dropped) with the content digest of its download: the sha256 of the ordered list of its pages' sha1s, so a re-zipped copy with the same images has the same digest. A download whose digest equals a handled chapter's of the same series is the same pages again: it is put aside (moved to the retention folder, SPEC "Publishing") and not processed. This is checked at discover, and again at junk once the series' baseline exists. A chapter is recorded as handled when it is published, in the same transaction as the rename's record, so a crash or a `state reset` never publishes it twice.

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
