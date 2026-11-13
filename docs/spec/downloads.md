# manga_pipeline spec — Downloads and Suwayomi

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Bad downloads

A chapter that fails validation is downloaded again through Suwayomi from another source. The rejected CBZ is deleted through Suwayomi, which also clears its downloaded mark. The pipeline picks the source itself; a chapter it cannot match with confidence on another source becomes `needs_human`. The chapter waits on `suwayomi_download` until Suwayomi reports the new download complete, then starts again at discover. The wait records which download it expects, so the replacement is taken as the same chapter whatever source, title or numbering it arrives under.

The pipeline records every chapter it has handled, recognised by series, chapter number and scanlator, because Suwayomi gives a chapter it lists again a new id. The same chapter from the same scanlator appearing again is deleted and not processed.

## Suwayomi

- Suwayomi writes a CBZ under its final name while it is still incomplete, and flags the chapter downloaded only once the file is closed. `intake` finds downloads by listing the folder, matches each file to its chapter by rebuilding the file name from the chapter's scanlator and name, and takes it only when the chapter is flagged downloaded.
- Suwayomi's own updater skips series it marks completed. When a completed series' metadata check is due, `intake` asks Suwayomi to refresh that series' chapter list, so an epilogue or side story is found and downloaded.
