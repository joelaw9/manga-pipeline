"""Stage validate (SPEC "Chapter stages", "Validation", "Bad downloads"): reads and measures every page of the
download. A sound one goes on with its page rows and notes; another defect needs a human; a bad download is removed
and downloaded again (redownload.py), and the replacement is validated here, in place.

The stage is a state machine over chapter.redownload; every step can run again after a crash or an unreachable
Suwayomi, because each lands in the database before the next call.
"""

import json
from pathlib import Path

from manga_pipeline import (
    archive,
    comicinfo,
    db,
    handled,
    names,
    page_store,
    redownload,
    validation,
    volumes,
    workflow,
)


def run(ctx, chapter):
    state = redownload.load(chapter)
    if state is None:
        verdict = _check(ctx, chapter, chapter["download_path"])
        if verdict.kind == "ok":
            return _done(chapter, verdict)
        if verdict.kind == "human":
            return workflow.NeedsHuman(verdict.reason)
        state = _reject(ctx, chapter, redownload.new_state(ctx, chapter, {}), _own(chapter), verdict.reason)
    elif "original" not in state:  # chapter use on a chapter never rejected: its file in hand, if any, gives way
        state = redownload.new_state(ctx, chapter, state)
        if Path(chapter["download_path"]).exists():
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
        }
        for row in ctx.conn.execute(
            "SELECT identity, page_phashes, strip_length, comicinfo_volume FROM published_file WHERE series_id = %s",
            (chapter["series_id"],),
        )
    ]
    ours = validation.chapter_key(chapter["identity"])
    series = _series(ctx, chapter)
    try:
        verdict = validation.check(
            Path(path), ours, published, lambda p: volumes.same_as_published(series, path, p["volume"])
        )
    except volumes.VolumeError as e:
        return validation.Verdict("human", str(e))
    theirs = _file_key(path)
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
    digest = archive.digest_or_none(copy["path"])
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
        "content_digest": archive.digest_or_none(expected["path"]),
    }
    if verdict.kind == "human":
        # The replacement becomes the chapter's own download: chapter drop removes it, retry judges it again.
        with db.transaction(ctx.conn):
            redownload.save_in(ctx, chapter, None, **adopted)
        return workflow.NeedsHuman(f"the re-download from {expected['source']}: {verdict.reason}")
    return _done(chapter, verdict, adopted)


def _done(chapter, verdict, adopted=None):
    def record(conn):
        columns = {**(adopted or {}), "notes": json.dumps(verdict.notes) if verdict.notes else None}
        if adopted:
            columns["redownload"] = None
        conn.execute(
            f"UPDATE chapter SET {', '.join(f'{name} = %s' for name in columns)} WHERE id = %s",
            (*columns.values(), chapter["id"]),
        )
        page_store.save(conn, chapter["id"], verdict.pages)

    return workflow.Done(record)
