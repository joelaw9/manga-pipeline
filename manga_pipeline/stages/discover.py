"""Stage discover (SPEC "Chapter stages", "Bad downloads"): the first look inside a download.

It settles the chapter's identity (the ComicInfo Number numbers a chapter whose name has none) and content digest,
drops an unnumbered notice or hiatus page (a numbered one is judged at junk: notices.py) and a download a handled
chapter already had, and makes a chapter with no number, or a number with 2 readings (names.two_readings),
needs_human. An archive it cannot read gets no digest and goes on to validate, which judges it. Finding the downloads
is discovery.py's.

An adopted file's conversion (it names its published file: SPEC "State") first gets its download, the link to its
file (conversions.link), and is never put aside as a handled chapter: its pages are already published.
"""

from manga_pipeline import (
    archive,
    comicinfo,
    config,
    conversions,
    db,
    errors,
    handled,
    library_copy,
    names,
    notices,
    workflow,
)


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Settle the chapter's identity and digest from its download, or drop it or make it needs_human.

    Dropped: a gone download, an unnumbered notice, a download a handled chapter already had. Needs_human: no number,
    a number with 2 readings, or a conversion whose file is missing or cannot be linked.
    """
    if chapter["published_file_id"] is not None and (why := conversions.link(ctx, chapter)):
        return workflow.NeedsHuman(why)
    return _settle(ctx, chapter)


def _settle(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    download = config.to_local(ctx.cfg, chapter["download_path"])
    if not errors.exists(download):
        # Moved away after discovery listed it, or put aside by a run that crashed before its outcome.
        return workflow.Dropped("the download is gone")
    number = comicinfo.download_number(download)
    title = notices.title(ctx.cfg, download, number)
    if title.kind == names.NON_CHAPTER and title.number is None:
        return notices.drop(ctx, chapter, download)
    digest = archive.digest_or_none(download)
    item = names.parse(download.stem, number)
    if item.number is None:
        return workflow.NeedsHuman(f"{download.name}: no chapter number in its name or its ComicInfo Number")
    chapter_number = float(item.number)
    why = digest and chapter["published_file_id"] is None and handled.put_aside(ctx, chapter, digest)
    if why:
        return workflow.Dropped(why)
    stop = _stop(ctx, chapter, item, digest, download.name)
    if stop is not None:
        return stop

    def record(conn: db.Connection) -> None:
        conn.execute(
            "UPDATE chapter SET identity = %s, number = %s, content_digest = %s WHERE id = %s",
            (names.identity(item), chapter_number, digest, chapter["id"]),
        )

    return workflow.Done(record)


def _stop(
    ctx: workflow.Context, chapter: db.Row, item: names.Item, digest: str | None, name: str
) -> workflow.NeedsHuman | workflow.Dropped | None:
    """Return what holds or drops a numbered download before it is recorded, else None.

    A number with 2 readings is held for a person; a part a joined file holds is dropped (SPEC "Split chapters").
    """
    unclear = names.two_readings(item.number)
    if unclear:
        return workflow.NeedsHuman(f"{name}: {unclear}")
    return library_copy.held_part(ctx, chapter, names.identity(item), digest)
