"""Stage discover (SPEC "Chapter stages", "Bad downloads"): the first look inside a download.

It settles the chapter's identity (the ComicInfo Number numbers a chapter whose name has none) and content digest,
drops an unnumbered notice or hiatus page (a numbered one is judged at junk: notices.py) and a download a handled
chapter already had, and makes a chapter with no number, or a number with 2 readings (names.two_readings),
needs_human. An archive it cannot read gets no digest and goes on to validate, which judges it. Finding the downloads
is discovery.py's.
"""

from manga_pipeline import archive, comicinfo, config, db, errors, handled, names, notices, workflow


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Settle the chapter's identity and digest from its download, or drop it or make it needs_human.

    Dropped: a gone download, an unnumbered notice, a download a handled chapter already had. Needs_human: no number,
    or a number with 2 readings.
    """
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
    why = digest and handled.put_aside(ctx, chapter, digest)
    if why:
        return workflow.Dropped(why)
    unclear = names.two_readings(item.number)
    if unclear:
        return workflow.NeedsHuman(f"{download.name}: {unclear}")

    def record(conn: db.Connection) -> None:
        conn.execute(
            "UPDATE chapter SET identity = %s, content_digest = %s WHERE id = %s",
            (names.identity(item), digest, chapter["id"]),
        )

    return workflow.Done(record)
