"""Stage validate (SPEC "Chapter stages", "Validation", "Bad downloads"): reads and measures every page of the
download. A sound one goes on with its page rows and notes; another defect needs a human; a bad download is removed
and downloaded again (redownload.py), and the replacement is validated here, in place.

The stage is a state machine over chapter.redownload; every step can run again after a crash or an unreachable
Suwayomi, because each lands in the database before the next call. Before any of it, a series Kavita holds gets its
baseline (waiting on bulk_conversion until its record can be read), and a series that has never completed a
metadata check waits on metadata_check (a platform-basis series also until the check its new chapter made due
completes: check.completed): its chapters' names and volume covers need both.
"""

import functools
import json
from pathlib import Path

from manga_pipeline import (
    archive,
    baseline,
    chapter_names,
    comicinfo,
    config,
    db,
    handled,
    names,
    page_store,
    redownload,
    validation,
    volumes,
    workflow,
)
from manga_pipeline.metadata import check, covers


def run(ctx, chapter):
    series = _series(ctx, chapter)
    if baseline.needed(ctx.conn, series):
        why = baseline.take(ctx, series)
        if why:
            return workflow.Waiting("bulk_conversion", {"why": why})
    if not check.completed(ctx, chapter):
        return workflow.Waiting("metadata_check")
    state = redownload.load(chapter)
    if state is None:
        verdict = _check(ctx, chapter, chapter["download_path"])
        if verdict.kind == "ok":
            return _done(ctx, chapter, verdict)
        if verdict.kind == "human":
            return workflow.NeedsHuman(verdict.reason)
        state = _reject(ctx, chapter, redownload.new_state(ctx, chapter, {}), _own(chapter), verdict.reason)
    elif "original" not in state:  # chapter use on a chapter never rejected: its file in hand, if any, gives way
        state = redownload.new_state(ctx, chapter, state)
        if workflow.exists(config.to_local(ctx.cfg, chapter["download_path"])):
            state = _reject(ctx, chapter, state, _own(chapter), "replaced by the download a person chose")
        else:
            redownload.save(ctx, chapter, state)
    if state["expected"] is not None:
        outcome = _candidate(ctx, chapter, state)
        if outcome is not None:
            return outcome
    redownload.remove(ctx, chapter, state)
    if state["chosen"] is not None:
        found = redownload.target(ctx, state["chosen"])
        why = (
            "does not exist"
            if found is None
            else "is another open chapter's download"
            if redownload.held_elsewhere(ctx, chapter, found)
            else None
        )
        if why:  # the choice stays recorded: retry clears it, another chapter use replaces it
            return workflow.NeedsHuman(f"the Suwayomi chapter chosen with chapter use {why}")
        found["chosen"] = True  # enqueue clears it with the expected download, so an interruption keeps the choice
    else:
        found = redownload.pick(ctx, chapter, _series(ctx, chapter), state)
        if not isinstance(found, dict):
            return found
    redownload.enqueue(ctx, chapter, state, found)
    return _waiting(found)


def _waiting(expected):
    return workflow.Waiting("suwayomi_download", {"source": expected["source"], "chapter_id": expected["chapter_id"]})


def _own(chapter):
    """The chapter's download in hand, shaped as a target."""
    return {"source": chapter["source"], "chapter_id": chapter["suwayomi_chapter_id"], "path": chapter["download_path"]}


def _check(ctx, chapter, path):
    published = [
        {
            "key": validation.chapter_key(row["identity"]),
            "page_phashes": None if row["page_phashes"] is None else json.loads(row["page_phashes"]),
            "strip_length": row["strip_length"],
            "volume": row["comicinfo_volume"],
            "volume_source": row["volume_source"],
        }
        for row in ctx.conn.execute(
            """SELECT identity, page_phashes, strip_length, comicinfo_volume, volume_source FROM published_file
            WHERE series_id = %s""",
            (chapter["series_id"],),
        )
    ]
    ours = validation.chapter_key(chapter["identity"])
    local = config.to_local(ctx.cfg, path)  # path: a stored download path
    volume = functools.cache(lambda: chapter_names.download_volume(ctx.cfg, path))
    try:
        verdict = validation.check(
            local, ours, published, lambda p: chapter_names.same_chapter(volume, p["volume"], p["volume_source"])
        )
    except volumes.VolumeError as e:
        return validation.Verdict("human", str(e))
    theirs = _file_key(local)
    if verdict.kind != "bad" and theirs != ours:
        # A file numbered otherwise (only a person's choice can bring one) would be published under its own number
        # (comicinfo reads the file's name) while the chapter's identity and records say ours. Checked on every look,
        # so a retry of such a chapter is held again.
        return validation.Verdict("human", f"it is numbered {theirs}, not {ours} (season, side label, number)")
    return verdict


def _file_key(path):
    """(season, side label, number) of a download as discover reads it: its name, numbered by its ComicInfo Number
    when the name has none."""
    return validation.chapter_key(names.identity(names.parse(Path(path).stem, comicinfo.download_number(path))))


def _series(ctx, chapter):
    return ctx.conn.execute("SELECT * FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()


def _reject(ctx, chapter, state, copy, why):
    """Record a rejected copy (a target: {source, chapter_id, path}) and queue its removal; the state and the handled
    chapter land in 1 transaction."""
    digest = archive.digest_or_none(config.to_local(ctx.cfg, copy["path"]))
    queued = copy["path"] != chapter["download_path"]  # an expected download, not the file discovered
    state["defect"] = state["defect"] or why
    state["rejected"].append({"source": copy["source"], "chapter_id": copy["chapter_id"], "digest": digest, "why": why})
    state["remove"].append({"path": copy["path"], "chapter_id": copy["chapter_id"], "dequeue": queued})
    with db.transaction(ctx.conn):
        handled.record(ctx.conn, chapter["series_id"], chapter["identity"], "rejected", digest)
        redownload.save_in(ctx, chapter, state)
    return state


def _candidate(ctx, chapter, state):
    """Judge the expected download. Returns the stage's outcome, or None once the state moved on to the next pick."""
    expected = state["expected"]
    status = redownload.download_state(ctx, expected)
    if status == "waiting":
        return _waiting(expected)
    if (
        status == "lost"
    ):  # neither queued nor downloaded, within the timeout: Suwayomi dropped it, or it never got there
        redownload.queue(ctx, expected)
        return _waiting(expected)
    state["tried"].append(expected["source"])
    state["expected"] = None
    if status == "failed":
        state["remove"].append({"path": expected["path"], "chapter_id": expected["chapter_id"], "dequeue": True})
        redownload.save(ctx, chapter, state)
        return None
    verdict = _check(ctx, chapter, expected["path"])
    if verdict.kind == "bad":
        _reject(ctx, chapter, state, expected, verdict.reason)
        return None
    adopted = {
        "download_path": expected["path"],
        "suwayomi_chapter_id": expected["chapter_id"],
        "source": expected["source"],
        "source_url": expected.get("url"),  # a re-download queued before schema step 0053 recorded none
        "content_digest": archive.digest_or_none(config.to_local(ctx.cfg, expected["path"])),
    }
    if verdict.kind == "human":
        # The replacement becomes the chapter's own download: chapter drop removes it, retry judges it again.
        with db.transaction(ctx.conn):
            redownload.save_in(ctx, chapter, None, **adopted)
        return workflow.NeedsHuman(f"the re-download from {expected['source']}: {verdict.reason}")
    return _done(ctx, chapter, verdict, adopted)


def _done(ctx, chapter, verdict, adopted=None):
    """Done with the verdict's pages and notes (and the adopted re-download), and the volume cover of a chapter that
    opens a covered volume (SPEC "Metadata", Volume covers)."""
    download = (adopted or {}).get("download_path", chapter["download_path"])
    try:
        series = _series(ctx, chapter)
        if series["origin"] == "new" and series["naming_basis"] is None:  # the claim waited for the whole batch
            chapter_names.choose_basis(ctx, series, {chapter["id"]: download})
        cover = _volume_cover(ctx, chapter, download)
    except volumes.VolumeError as e:
        if adopted:  # the replacement becomes the chapter's own download, as for a held verdict
            with db.transaction(ctx.conn):
                redownload.save_in(ctx, chapter, None, **adopted)
        return workflow.NeedsHuman(str(e))

    def record(conn):
        columns = {**(adopted or {}), "notes": json.dumps(verdict.notes) if verdict.notes else None}
        if adopted:
            columns["redownload"] = None
        conn.execute(
            f"UPDATE chapter SET {', '.join(f'{name} = %s' for name in columns)} WHERE id = %s",
            (*columns.values(), chapter["id"]),
        )
        page_store.save(conn, chapter["id"], verdict.pages)
        if cover:
            conn.execute(
                """INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, %s, %s, %s)
                ON CONFLICT (chapter_id) DO NOTHING""",
                (chapter["id"], *cover),
            )

    return workflow.Done(record)


def bulk_conversion_over(ctx, chapter):
    """The bulk_conversion wait ends once the series has its baseline (taken here when its record can be read, so a
    chapter waiting past validate, left by an older version, goes on with one)."""
    series = _series(ctx, chapter)
    return not baseline.needed(ctx.conn, series) or baseline.take(ctx, series) is None


def _volume_cover(ctx, chapter, download):
    """(path, sha256, volume) of the cover the chapter takes when it opens a covered volume and MangaDex has that
    volume's cover; else None. A chapter that already has its cover keeps it."""
    if ctx.conn.execute("SELECT 1 FROM volume_cover WHERE chapter_id = %s", (chapter["id"],)).fetchone():
        return None
    named = chapter_names.resolve(ctx, _series(ctx, chapter), {chapter["id"]: download}).get(chapter["id"])
    if named is None or not named.opens_covered_volume:
        return None
    taken = ctx.conn.execute(
        """SELECT 1 FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id
        WHERE c.series_id = %s AND v.volume = %s AND c.status != 'done'""",
        (chapter["series_id"], names.fmt_num(named.volume)),
    ).fetchone()
    if taken:  # an open chapter of that volume took its cover first: it keeps it
        return None
    found = covers.volume_cover(ctx, chapter["series_id"], named.volume)
    return found and (*found, names.fmt_num(named.volume))
