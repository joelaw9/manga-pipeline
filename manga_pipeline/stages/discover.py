"""Stage discover (SPEC "Chapter stages", "Bad downloads"): the first look inside a download. It settles the chapter's
identity (the ComicInfo Number numbers a chapter whose name has none) and content digest, drops a notice or hiatus
page and a download a handled chapter already had, and makes a chapter with no number needs_human. An archive it
cannot read gets no digest and goes on to validate, which judges it. Finding the downloads is discovery.py's.
"""

from pathlib import Path

from manga_pipeline import archive, comicinfo, handled, names, retention, workflow


def run(ctx, chapter):
    download = Path(chapter["download_path"])
    if not download.exists():
        # Moved away after discovery listed it, or put aside by a run that crashed before its outcome.
        return workflow.Dropped("the download is gone")
    if names.parse_name(download.stem, None, ctx.cfg["validate"]["non_chapter_names"]).kind == names.NON_CHAPTER:
        return _non_chapter(ctx, chapter, download)
    try:
        xml = archive.read_comicinfo(download)
    except archive.UNREADABLE:
        xml = None
    digest = archive.digest_or_none(download)
    number = comicinfo.read_fields(xml, ("Number",))["Number"] if xml else ""
    item = names.parse(download.stem, number)
    if item.number is None:
        return workflow.NeedsHuman(f"{download.name}: no chapter number in its name or its ComicInfo Number")
    why = digest and handled.put_aside(ctx, chapter, digest)
    if why:
        return workflow.Dropped(why)

    def record(conn):
        conn.execute(
            "UPDATE chapter SET identity = ?, content_digest = ? WHERE id = ?",
            (names.identity(item), digest, chapter["id"]),
        )

    return workflow.Done(record)


def _non_chapter(ctx, chapter, download):
    """A notice or hiatus page (validate.non_chapter_names): removed the retention way and recorded as dropped."""
    digest = archive.digest_or_none(download)
    retention.put(ctx.cfg, download)

    def record(conn):
        handled.record(conn, chapter["series_id"], chapter["identity"], "dropped", digest)

    return workflow.Dropped(f"{download.name}: a notice, not a chapter (validate.non_chapter_names)", record)
