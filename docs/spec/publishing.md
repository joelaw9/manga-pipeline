# manga_pipeline spec — Publishing

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Publishing

The finished CBZ is written to a staging folder on the library's NFS export, outside every Kavita library root, then renamed into the series folder. Its `ComicInfo.xml` `Series` is the series' Kavita name.

A new series gets its own folder directly under the library root: `<library root>/<series>`. A chapter of a series Kavita already knows goes into the folder Kavita records for that series, wherever it is (older series sit under `Local` or a source-named folder until they are moved). The pipeline never creates a second folder for a series, and it does not move series folders.

A file the pipeline publishes is named `Ch.<number> - <title>.cbz`, with no volume, and is never renamed: Kavita takes the volume from `ComicInfo.xml`, so a later volume change needs no rename. A file the bulk converter named with a volume is renamed only when its chapter loses its volume altogether, with reading progress carried.

A chapter of a series Kavita knows triggers a Kavita series scan, after a check that the series folder exists at the path Kavita records. A new series is published when none of its chapters is still on its way through `intake` or `process` (waiting and `needs_human` chapters aside): all its finished chapters are renamed in together and 1 Kavita library scan is requested. Only 1 library scan is awaited at a time; another new series waits its turn. The wait ends when every chapter of the batch is indexed. Once Kavita knows the series, its later chapters use series scans.

Once Kavita has indexed the file, the pipeline deletes the CBZ from the downloads folder directly, not through Suwayomi, so Suwayomi keeps the chapter marked downloaded and does not fetch it again. With a retention period set, the CBZ is moved to a retention folder (outside the downloads folder and every library root) instead, and `export` deletes it from there when the period ends.
