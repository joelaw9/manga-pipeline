"""Stage comicinfo (SPEC "Chapter stages", "Publishing", "Metadata" ComicInfo): adds ComicInfo.xml to the staged CBZ
and settles the file's name in the series folder.

The download's ComicInfo.xml is kept field by field where a field is valid for the schema (Suwayomi writes Day, Month
and Year out of schema order: build orders them), except what this stage writes itself: the series' fields from its
stored records, the chapter's source, Manga, SeriesGroup and the pages. Series and LocalizedSeries are the series';
Number, Title, Volume and the file name are the bulk converter's, resolved with the series' other chapters
(chapter_names.resolve).
"""

import json

from manga_pipeline import archive, baseline, chapter_names, comicinfo, config, names, volumes, workflow
from manga_pipeline.metadata import merge, naming, tags

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
        msg = f"series {series['id']} has no ComicInfo Series name (no baseline)"
        raise ValueError(msg)
    if baseline.needed(ctx.conn, series):  # a chapter past validate when the baseline's naming fields were added
        why = baseline.take(ctx, series)
        if why:
            return workflow.Waiting("bulk_conversion", {"why": why})
    read, _ = comicinfo.read_download(config.to_local(ctx.cfg, chapter["download_path"]))
    fields: dict[str, str | int] = {k: v for k, v in read.items() if k not in OWN}
    overrides = config.abs_path(ctx.cfg["paths"]["naming_overrides"])
    try:
        named = _copy_name(ctx, chapter) or chapter_names.resolve(ctx, series)[chapter["id"]]
        unedited = None
        if chapter["published_file_id"] is None and chapter_names.edits(ctx.conn, series["id"]):
            unedited = naming.unedited(named, chapter_names.resolve(ctx, series, edited=False)[chapter["id"]])
        group = volumes.series_group(overrides, series["rules_key"])
    except volumes.VolumeError as e:
        return workflow.NeedsHuman(str(e))
    fields["Number"] = names.fmt_num(named.number)
    fields["Title"] = named.title or f"Chapter {fields['Number']}"
    fields.pop("Volume", None)
    if named.volume is not None:
        fields["Volume"] = names.fmt_num(named.volume)
    fields["Series"] = series["comicinfo_series"]
    fields.pop("LocalizedSeries", None)
    if series["localized_name"]:
        fields["LocalizedSeries"] = series["localized_name"]
    fields.update(series_fields(ctx, series, chapter, group))
    result, pages = json.loads(chapter["convert_result"]), json.loads(chapter["output_pages"])
    entries = page_entries(pages, bool(result["cover"]), _layout(ctx.conn, series["id"]) == "pages")
    fields["PageCount"] = len(entries)
    fields["BlackAndWhite"] = "Yes" if result["gray"] else "No"
    xml = comicinfo.build(fields, entries)
    workflow.holds_claim(ctx, chapter)
    size = archive.replace_member(config.to_local(ctx.cfg, chapter["staged_path"]), archive.COMICINFO, xml)
    target = config.to_local(ctx.cfg, series["folder"]) / names.file_name(named.number, named.title, named.volume)
    if chapter["published_file_id"] is not None:  # a better copy takes its file's place, under its name
        target = config.to_local(ctx.cfg, _file(ctx, chapter)["library_path"])

    def record(conn):
        conn.execute(
            """UPDATE chapter SET staged_size = %s, library_path = %s, comicinfo_series = %s, comicinfo_volume = %s,
                comicinfo_number = %s, comicinfo_title = %s, comicinfo_date = %s, naming_kind = %s,
                naming_volume_source = %s, naming_unedited = %s
            WHERE id = %s""",
            (
                size,
                config.to_stored(ctx.cfg, target),
                fields["Series"],
                fields.get("Volume"),
                fields["Number"],
                named.title or None,  # an untitled chapter is recorded with no title, as the baseline records one
                names.ci_date(fields),
                named.kind,
                named.volume_source,
                None if unedited is None else json.dumps(unedited),
                chapter["id"],
            ),
        )

    return workflow.Done(record)


def _file(ctx, chapter):
    return ctx.conn.execute("SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],)).fetchone()


def _copy_name(ctx, chapter):
    """A better copy's naming: its file's as it is now (SPEC "Retroactive updates"); None for any other chapter."""
    if chapter["published_file_id"] is None:
        return None
    f = _file(ctx, chapter)
    volume = names.to_volume(f["comicinfo_volume"])
    return naming.Naming(
        f["kind"],
        volume,
        float(f["comicinfo_number"]),
        f["comicinfo_title"] or "",
        f["volume_source"],
        opens_covered_volume=False,
    )


def series_fields(ctx, series, chapter, group):
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
        msg = f"series {series_id} has no layout (series_conversion)"
        raise RuntimeError(msg)
    return row["layout"]
