"""Stage discover (SPEC "Chapter stages", "Bad downloads"): the first look inside a download. It settles the chapter's
identity (the ComicInfo Number numbers a chapter whose name has none) and content digest, drops a download a handled
chapter already had, and makes a chapter with no number needs_human. Finding the downloads is discovery.py's.
"""

from pathlib import Path

from manga_pipeline import archive, comicinfo, handled, names, workflow


def run(ctx, chapter):
    download = Path(chapter["download_path"])
    if not download.exists():
        # Moved away after discovery listed it, or put aside by a run that crashed before its outcome.
        return workflow.Dropped("the download is gone")
    xml = archive.read_comicinfo(download)
    number = comicinfo.read_fields(xml, ("Number",))["Number"] if xml else ""
    item = names.parse(download.stem, number)
    if item.number is None:
        return workflow.NeedsHuman(f"{download.name}: no chapter number in its name or its ComicInfo Number")
    digest = archive.pages_digest(download)
    why = handled.put_aside(ctx, chapter, digest)
    if why:
        return workflow.Dropped(why)

    def record(conn):
        conn.execute(
            "UPDATE chapter SET identity = ?, content_digest = ? WHERE id = ?",
            (names.identity(item), digest, chapter["id"]),
        )

    return workflow.Done(record)
