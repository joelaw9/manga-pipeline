"""Stage comicinfo (SPEC "Chapter stages", "Publishing", "Metadata" ComicInfo): ComicInfo.xml and the file's name.

Adds ComicInfo.xml to the staged CBZ and settles the file's name in the series folder.

The download's ComicInfo.xml is kept field by field where a field is valid for the schema (Suwayomi writes Day, Month
and Year out of schema order: build orders them), except what this stage writes itself: the series' fields from its
stored records (a series whose records give no genres and no tags keeps the download's Genre and Tags), the
chapter's source, Manga, SeriesGroup and the pages. Series and LocalizedSeries are the series';
Number, Title, Volume and the file name are the bulk converter's, resolved with the series' other chapters
(chapter_names.resolve).
"""

import json
from collections.abc import Collection, Mapping, Sequence

from manga_pipeline import archive, baseline, chapter_names, comicinfo, config, db, names, volumes, workflow
from manga_pipeline.metadata import merge, naming, tags
from manga_pipeline.metadata.records import STORED
from manga_pipeline.stages import convert

# Elements this stage writes itself: the download's own are not kept as such (Genre and Tags of a series whose records
# give neither are, through series_fields; Count is never written: SPEC "Metadata").
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


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Write the staged CBZ's ComicInfo.xml and settle its library path; record both with the chapter's naming.

    Waits on bulk_conversion when a baseline the series needs cannot be taken yet; a VolumeError from naming is
    needs_human. A better copy keeps its file's path. ValueError when the series has no ComicInfo Series name.
    """
    series = db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    if not series["comicinfo_series"]:
        msg = f"series {series['id']} has no ComicInfo Series name (no baseline)"
        raise ValueError(msg)
    if baseline.needed(ctx.conn, series):  # a chapter past validate when the baseline's naming fields were added
        why = baseline.take(ctx, series)
        if why:
            return workflow.Waiting("bulk_conversion", {"why": why})
    read, _ = comicinfo.read_download(config.to_local(ctx.cfg, chapter["download_path"]))
    fields: dict[str, str | int | None] = {k: v for k, v in read.items() if k not in OWN}
    try:
        named = _copy_name(ctx, chapter) or chapter_names.resolve(ctx, series)[chapter["id"]]
        unedited = None
        if chapter["published_file_id"] is None and chapter_names.edits(ctx.conn, series["id"]):
            unedited = naming.unedited(named, chapter_names.resolve(ctx, series, edited=False)[chapter["id"]])
        elements = series_fields(ctx, series, chapter["source"], chapter["source_url"], read)
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
    fields.update(elements)
    result = convert.read_result(chapter["convert_result"])
    pages = convert.read_output_pages(chapter["output_pages"])
    entries = page_entries(pages, cover=bool(result["cover"]), spreads=_layout(ctx.conn, series["id"]) == "pages")
    fields["PageCount"] = len(entries)
    fields["BlackAndWhite"] = "Yes" if result["gray"] else "No"
    xml = comicinfo.build(fields, entries)
    workflow.holds_claim(ctx, chapter)
    size = archive.replace_member(config.to_local(ctx.cfg, chapter["staged_path"]), archive.COMICINFO, xml)
    target = config.to_local(ctx.cfg, series["folder"]) / names.file_name(named.number, named.title, named.volume)
    if chapter["published_file_id"] is not None:  # a better copy takes its file's place, under its name
        target = config.to_local(ctx.cfg, _file(ctx, chapter)["library_path"])

    def record(conn: db.Connection) -> None:
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
                names.ci_date(read),  # the download's own Year, Month and Day: fields keeps them as they are
                named.kind,
                named.volume_source,
                None if unedited is None else json.dumps(unedited),
                chapter["id"],
            ),
        )

    return workflow.Done(record)


def _file(ctx: workflow.Context, chapter: db.Row) -> db.Row:
    return db.one_row(ctx.conn, "SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],))


def _copy_name(ctx: workflow.Context, chapter: db.Row) -> naming.Naming | None:
    """Return a better copy's naming: its file's as it is now (SPEC "Retroactive updates").

    None for any other chapter.
    """
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


def series_fields(
    ctx: workflow.Context, series: db.Row, source: str | None, source_url: str | None, own: Mapping[str, str]
) -> dict[str, str | None]:
    """Return the ComicInfo elements the series' stored fields, the chapter's source (and its page) and the file give.

    own: the file's own ComicInfo fields, whose Genre and Tags a series whose records give neither keeps. VolumeError
    from the series' override file (its series group).
    """
    got = merge.stored(ctx.conn, series["id"], tags.load())
    web = [*got.links, *([source_url] if source_url else [])]
    terms = {g.lower() for g in (*got.genres, *got.tags)}
    strip = _layout(ctx.conn, series["id"]) == "strip"
    genre, tag_list = genre_lists(got.genres, got.tags, own)
    overrides = config.abs_path(ctx.cfg["paths"]["naming_overrides"])
    return {
        "Summary": got.summary,
        "Genre": genre,
        "Tags": tag_list,
        "LanguageISO": got.language,
        "AgeRating": got.age_rating,
        "Writer": ", ".join(got.writers),
        "Penciller": ", ".join(got.pencillers),
        "Web": " ".join(web),
        "Notes": f"Source: {source}" if source else None,
        "Manga": "Yes" if strip else reading_direction(_mangabaka_type(ctx.conn, series["id"]), got.language, terms),
        "SeriesGroup": volumes.series_group(overrides, series["rules_key"]),
    }


def genre_lists(
    genres: Sequence[str], tag_list: Sequence[str], own: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """Return (Genre, Tags): the stored lists joined, or the file's own when the records give neither."""
    if genres or tag_list:
        return ", ".join(genres), ", ".join(tag_list)
    return own.get("Genre"), own.get("Tags")


def reading_direction(mangabaka_type: str | None, language: str | None, terms: Collection[str]) -> str:
    """Return the Manga element of a page series.

    By MangaBaka's type (manga reads right to left, any other type left to right); with none, by MangaDex's original
    language (Japanese right to left); with neither, by its stored genres and tags (terms, lower case).
    """
    if mangabaka_type:
        return "YesAndRightToLeft" if mangabaka_type.casefold() == "manga" else "Yes"
    if language:
        return "YesAndRightToLeft" if language.casefold() == "ja" else "Yes"
    return "Yes" if LEFT_TO_RIGHT.intersection(terms) else "YesAndRightToLeft"


def _mangabaka_type(conn: db.Connection, series_id: int) -> str | None:
    source, kind = STORED["mangabaka"]
    row = conn.execute(
        "SELECT payload ->> 'type' AS type FROM fetched WHERE series_id = %s AND source = %s AND kind = %s",
        (series_id, source, kind),
    ).fetchone()
    return None if row is None else row["type"]


def page_entries(pages: Sequence[convert.StagedPage], *, cover: bool, spreads: bool) -> list[dict[str, str | int]]:
    """Return the ComicInfo Page entries of convert's output pages.

    pages: [{index, name, width, height, bytes}] in archive order. FrontCover on the inserted volume cover (cover: one
    was inserted), DoublePage on a page wider than tall in a page series (spreads).
    """
    out: list[dict[str, str | int]] = []
    for page in pages:
        entry: dict[str, str | int] = {"Image": page["index"]}
        if cover and page["name"].rpartition(".")[0] == archive.COVER_STEM:
            entry["Type"] = "FrontCover"
        if spreads and page["width"] > page["height"]:
            entry["DoublePage"] = "true"
        entry.update(ImageSize=page["bytes"], ImageWidth=page["width"], ImageHeight=page["height"])
        out.append(entry)
    return out


def _layout(conn: db.Connection, series_id: int) -> str:
    row = conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is None:
        msg = f"series {series_id} has no layout (series_conversion)"
        raise RuntimeError(msg)
    return row["layout"]
