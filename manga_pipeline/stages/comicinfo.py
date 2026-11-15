"""Stage comicinfo (SPEC "Chapter stages", "Publishing", "Metadata" ComicInfo): adds ComicInfo.xml to the staged CBZ
and settles the file's name in the series folder.

The download's ComicInfo.xml is kept field by field where a field is valid for the schema (Suwayomi writes Day, Month
and Year out of schema order: build orders them), except what this stage writes itself: the series' fields from its
stored records, the chapter's source, Manga, SeriesGroup and the pages. Number comes from the chapter's name where it
has none; Series and LocalizedSeries are the series'; Title and Volume are the bulk converter's (names.chapter_title,
volumes), as is the file name. A chapter whose series has never completed a metadata check waits on metadata_check.
"""

import json

from manga_pipeline import archive, comicinfo, config, names, volumes, workflow
from manga_pipeline.metadata import merge, tags

# Elements this stage writes itself: the download's own are never kept (Count is never written: SPEC "Metadata").
OWN = {
    "Summary",
    "Genre",
    "Tags",
    "LanguageISO",
    "AgeRating",
    "Writer",
    "Penciller",
    "Web",
    "Notes",
    "Manga",
    "SeriesGroup",
    "Count",
    "PageCount",
    "BlackAndWhite",
}
LEFT_TO_RIGHT = {"manhwa", "manhua", "korean", "chinese", "webtoon", "long strip", "web comic"}  # genres and tags


def run(ctx, chapter):
    series = ctx.conn.execute("SELECT * FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()
    if not series["comicinfo_series"]:
        raise ValueError(f"series {series['id']} has no ComicInfo Series name (no baseline)")
    if not checked(ctx, chapter):
        return workflow.Waiting("metadata_check")
    fields, item = comicinfo.read_download(config.to_local(ctx.cfg, chapter["download_path"]))
    fields = {k: v for k, v in fields.items() if k not in OWN}
    fields.setdefault("Number", names.fmt_num(item.number))
    overrides = config.abs_path(ctx.cfg["paths"]["naming_overrides"])
    try:
        volume = volumes.chapter_volume(series, item, fields.get("Volume"), overrides)
        group = volumes.series_group(overrides, series["rules_key"])
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
    fields.update(_series_fields(ctx, series, chapter, group))
    result, pages = json.loads(chapter["convert_result"]), json.loads(chapter["output_pages"])
    entries = page_entries(pages, bool(result["cover"]), _layout(ctx.conn, series["id"]) == "pages")
    fields["PageCount"] = len(entries)
    fields["BlackAndWhite"] = "Yes" if result["gray"] else "No"
    xml = comicinfo.build(fields, entries)
    workflow.holds_claim(ctx, chapter)
    size = archive.replace_member(config.to_local(ctx.cfg, chapter["staged_path"]), archive.COMICINFO, xml)
    target = config.to_local(ctx.cfg, series["folder"]) / names.file_name(item, title, volume)

    def record(conn):
        conn.execute(
            """UPDATE chapter SET staged_size = %s, library_path = %s, comicinfo_series = %s, comicinfo_volume = %s,
                comicinfo_number = %s, comicinfo_title = %s, comicinfo_date = %s
            WHERE id = %s""",
            (
                size,
                config.to_stored(ctx.cfg, target),
                fields["Series"],
                fields.get("Volume"),
                fields["Number"],
                title or None,  # an untitled chapter is recorded with no title, as the baseline records one
                names.ci_date(fields),
                chapter["id"],
            ),
        )

    return workflow.Done(record)


def checked(ctx, chapter):
    """Whether the chapter's series has completed a metadata check (found or not): the end of a metadata_check wait."""
    row = ctx.conn.execute(
        "SELECT data_version FROM series_check WHERE series_id = %s", (chapter["series_id"],)
    ).fetchone()
    return row is not None and row["data_version"] > 0


def _series_fields(ctx, series, chapter, group):
    """The elements the series' stored fields and the chapter's source give."""
    got = merge.stored(ctx.conn, series["id"], tags.load())
    web = [*got.links, *([chapter["source_url"]] if chapter["source_url"] else [])]
    genres = {g.lower() for g in (*got.genres, *got.tags)}
    strip = _layout(ctx.conn, series["id"]) == "strip"
    return {
        "Summary": got.summary,
        "Genre": ", ".join(got.genres),
        "Tags": ", ".join(got.tags),
        "LanguageISO": got.language,
        "AgeRating": got.age_rating,
        "Writer": ", ".join(got.writers),
        "Penciller": ", ".join(got.pencillers),
        "Web": " ".join(web),
        "Notes": f"Source: {chapter['source']}" if chapter["source"] else None,
        "Manga": "Yes" if strip or genres & LEFT_TO_RIGHT else "YesAndRightToLeft",
        "SeriesGroup": group,
    }


def page_entries(pages, cover, spreads):
    """ComicInfo Page entries of convert's output pages ([{index, name, width, height, bytes}] in archive order):
    FrontCover on the inserted volume cover (cover: one was inserted), DoublePage on a page wider than tall in a page
    series (spreads)."""
    out = []
    for page in pages:
        entry = {"Image": page["index"]}
        if cover and page["name"].rpartition(".")[0] == archive.COVER_STEM:
            entry["Type"] = "FrontCover"
        if spreads and page["width"] > page["height"]:
            entry["DoublePage"] = "true"
        entry.update(ImageSize=page["bytes"], ImageWidth=page["width"], ImageHeight=page["height"])
        out.append(entry)
    return out


def _layout(conn, series_id):
    row = conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"series {series_id} has no layout (series_conversion)")
    return row["layout"]
