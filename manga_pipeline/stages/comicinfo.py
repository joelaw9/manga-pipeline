"""Stage comicinfo (SPEC "Chapter stages", "Publishing", "Metadata"): adds ComicInfo.xml to the staged CBZ and settles
the file's name in the series folder.

The download's ComicInfo.xml is kept field by field where a field is valid for the schema (Suwayomi writes Day, Month
and Year out of schema order: build orders them); Number comes from the chapter's name where it has none; Series and
LocalizedSeries are the series'; Title and Volume are the bulk converter's (names.chapter_title, volumes), as is the
file name.
"""

from pathlib import Path

from manga_pipeline import archive, comicinfo, config, names, volumes, workflow


def run(ctx, chapter):
    series = ctx.conn.execute("SELECT * FROM series WHERE id = ?", (chapter["series_id"],)).fetchone()
    if not series["comicinfo_series"]:
        raise ValueError(f"series {series['id']} has no ComicInfo Series name (no baseline)")
    download = Path(chapter["download_path"])
    xml = archive.read_comicinfo(download)
    fields = {k: v for k, v in comicinfo.read_all(xml).items() if comicinfo.field_ok(k, v)} if xml else {}
    item = names.parse(download.stem, fields.get("Number", ""))
    fields.setdefault("Number", names.fmt_num(item.number))
    try:
        overrides = config.abs_path(ctx.cfg["paths"]["naming_overrides"])
        volume = volumes.chapter_volume(series, item, fields.get("Volume"), overrides)
    except volumes.VolumeError as e:
        return workflow.NeedsHuman(str(e))
    title = names.chapter_title(item, fields.get("Title"))
    fields["Title"] = title or f"Chapter {names.fmt_num(item.number)}"
    fields.pop("Volume", None)
    if volume is not None:
        fields["Volume"] = names.fmt_num(volume)
    fields["Series"] = series["comicinfo_series"]
    fields.pop("LocalizedSeries", None)
    if series["localized_name"]:
        fields["LocalizedSeries"] = series["localized_name"]
    size = archive.replace_member(Path(chapter["staged_path"]), archive.COMICINFO, comicinfo.build(fields))
    target = Path(series["folder"]) / names.file_name(item, title, volume)

    def record(conn):
        conn.execute(
            """UPDATE chapter SET staged_size = ?, library_path = ?, comicinfo_series = ?, comicinfo_volume = ?,
                comicinfo_number = ?, comicinfo_title = ?, comicinfo_date = ?
            WHERE id = ?""",
            (
                size,
                str(target),
                fields["Series"],
                fields.get("Volume"),
                fields["Number"],
                fields.get("Title"),
                names.ci_date(fields),
                chapter["id"],
            ),
        )

    return workflow.Done(record)
