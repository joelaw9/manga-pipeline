"""Stage validate (SPEC "Chapter stages", "Validation", "Bad downloads"): reads and measures every download page.

A sound download goes on with its page rows and notes; another defect needs a human; a bad download is removed and
downloaded again (redownload.py), and the replacement is validated here, in place.

The stage is a state machine over chapter.redownload; every step can run again after a crash or an unreachable
Suwayomi, because each lands in the database before the next call. Before any of it, a series Kavita holds gets its
baseline (waiting on bulk_conversion until its record can be read), and a series that has never completed a
metadata check waits on metadata_check (a platform-basis series also until the check its new chapter made due
completes: check.completed): its chapters' names and volume covers need both.
"""

import functools
import json
from pathlib import Path
from typing import ReadOnly, TypedDict

from manga_pipeline import (
    archive,
    baseline,
    chapter_names,
    comicinfo,
    config,
    db,
    errors,
    handled,
    names,
    page_store,
    redownload,
    rewrite_covers,
    validation,
    volumes,
    workflow,
)
from manga_pipeline.db import sql
from manga_pipeline.metadata import check, covers


class PublishedFile(validation.Published):
    """A published file of the series as the checks read it, with the volume its same-chapter test reads."""

    volume: str | None
    volume_source: str | None


class Copy(TypedDict):
    """A copy of the chapter that is rejected: its source, Suwayomi chapter (None: hand-placed) and stored path."""

    source: ReadOnly[str | None]
    chapter_id: ReadOnly[int | None]
    path: ReadOnly[str]


class Adopted(TypedDict):
    """The chapter columns a judged re-download sets: it becomes the chapter's own download."""

    download_path: str
    suwayomi_chapter_id: int
    source: str
    source_url: str | None
    content_digest: str | None


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Validate the chapter's download, or step its re-download state machine (chapter.redownload) on.

    A download renamed away is dropped first (handled.renamed); then the series' prerequisites wait. A sound download
    is done; another defect is needs_human; a bad one is
    rejected, removed and the next download queued, and a re-download's candidate is judged when it arrives.
    """
    why = handled.renamed(ctx.cfg, ctx.conn, chapter)
    if why:
        return workflow.Dropped(why)
    waits = _prerequisites(ctx, chapter)
    if waits is not None:
        return waits
    state = redownload.load(chapter)
    if state is None:
        verdict = _check(ctx, chapter, chapter["download_path"])
        if verdict.kind == "ok":
            return _done(ctx, chapter, verdict)
        if verdict.kind == "human":
            return workflow.NeedsHuman(verdict.reason)
        state = _reject(ctx, chapter, redownload.new_state(ctx, chapter, None), _own(chapter), verdict.reason)
    elif "original" not in state:
        state = _first_choice(ctx, chapter, state)
    if (expected := state["expected"]) is not None:
        outcome = _candidate(ctx, chapter, state, expected)
        if outcome is not None:
            return outcome
    redownload.remove(ctx, chapter, state)
    return _next_download(ctx, chapter, state)


def _prerequisites(ctx: workflow.Context, chapter: db.Row) -> workflow.Waiting | None:
    """Return the wait of a chapter whose series lacks what its names and covers need, else None.

    The baseline of a series Kavita holds (bulk_conversion while its record cannot be read), then a completed metadata
    check.
    """
    series = _series(ctx, chapter)
    if baseline.needed(ctx.conn, series):
        why = baseline.take(ctx, series)
        if why:
            return workflow.Waiting("bulk_conversion", {"why": why})
    if not check.completed(ctx, chapter):
        return workflow.Waiting("metadata_check")
    return None


def _first_choice(ctx: workflow.Context, chapter: db.Row, state: workflow.PersonChoice) -> workflow.Redownload:
    """Return the re-download state of `chapter use` on a chapter never rejected.

    Its file in hand, if any, gives way.
    """
    started = redownload.new_state(ctx, chapter, state["chosen"])
    if errors.exists(config.to_local(ctx.cfg, chapter["download_path"])):
        return _reject(ctx, chapter, started, _own(chapter), "replaced by the download a person chose")
    redownload.save(ctx, chapter, started)
    return started


def _next_download(ctx: workflow.Context, chapter: db.Row, state: workflow.Redownload) -> workflow.OUTCOMES:
    """Queue the next download, the one chosen with chapter use else the next pick, and wait on it.

    The pick's outcome when it gives none, needs_human for a refused choice.
    """
    if state["chosen"] is not None:
        found = _chosen(ctx, chapter, state["chosen"])
    else:
        found = redownload.pick(ctx, chapter, _series(ctx, chapter), state)
    if not isinstance(found, dict):
        return found
    redownload.enqueue(ctx, chapter, state, found)
    return _waiting(found)


def _chosen(ctx: workflow.Context, chapter: db.Row, chapter_id: int) -> workflow.Chosen | workflow.NeedsHuman:
    """Return the Suwayomi chapter chosen with chapter use as a target, marked chosen.

    needs_human when it does not exist or is another open chapter's download (a refused choice stays recorded: retry
    clears it, another chapter use replaces it).
    """
    found = redownload.target(ctx, chapter_id)
    if found is None:
        return workflow.NeedsHuman("the Suwayomi chapter chosen with chapter use does not exist")
    if redownload.held_elsewhere(ctx, chapter, found):
        return workflow.NeedsHuman("the Suwayomi chapter chosen with chapter use is another open chapter's download")
    return {**found, "chosen": True}  # enqueue clears it with the expected download: an interruption keeps the choice


def _waiting(expected: workflow.Target | workflow.Expected) -> workflow.Waiting:
    return workflow.Waiting("suwayomi_download", {"source": expected["source"], "chapter_id": expected["chapter_id"]})


def _own(chapter: db.Row) -> Copy:
    """Return the chapter's download in hand, shaped as a target."""
    return {"source": chapter["source"], "chapter_id": chapter["suwayomi_chapter_id"], "path": chapter["download_path"]}


def _phashes(text: str) -> list[str]:
    """Return a published file's page_phashes (JSON text): its non-blank pages' pHashes. Unchecked."""
    return json.loads(text)


def _check(ctx: workflow.Context, chapter: db.Row, path: str) -> validation.Verdict:
    published: list[PublishedFile] = [
        {
            "key": validation.chapter_key(row["identity"]),
            "page_phashes": None if row["page_phashes"] is None else _phashes(row["page_phashes"]),
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


def _file_key(path: Path) -> validation.ChapterKey:
    """Return (season, side label, number) of a download as discover reads it.

    Its name, numbered by its ComicInfo Number when the name has none.
    """
    return validation.chapter_key(names.identity(names.parse(Path(path).stem, comicinfo.download_number(path))))


def _series(ctx: workflow.Context, chapter: db.Row) -> db.Row:
    return db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))


def _reject(
    ctx: workflow.Context, chapter: db.Row, state: workflow.Redownload, copy: Copy, why: str
) -> workflow.Redownload:
    """Record a rejected copy (a target: {source, chapter_id, path}) and queue its removal.

    The state and the handled chapter land in 1 transaction.
    """
    digest = archive.digest_or_none(config.to_local(ctx.cfg, copy["path"]))
    queued = copy["path"] != chapter["download_path"]  # an expected download, not the file discovered
    state["defect"] = state["defect"] or why
    state["rejected"].append({"source": copy["source"], "chapter_id": copy["chapter_id"], "digest": digest, "why": why})
    state["remove"].append({"path": copy["path"], "chapter_id": copy["chapter_id"], "dequeue": queued})
    with db.transaction(ctx.conn):
        handled.record(ctx.conn, chapter["series_id"], chapter["identity"], "rejected", digest)
        redownload.save_in(ctx, chapter, state)
    return state


def _candidate(
    ctx: workflow.Context, chapter: db.Row, state: workflow.Redownload, expected: workflow.Expected
) -> workflow.OUTCOMES | None:
    """Judge the expected download (the state's). Returns the stage's outcome, or None once the state moved on."""
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
    adopted: Adopted = {
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


def _done(
    ctx: workflow.Context, chapter: db.Row, verdict: validation.Verdict, adopted: Adopted | None = None
) -> workflow.Done | workflow.NeedsHuman:
    """Return Done with the verdict's pages and notes (and the adopted re-download), and the chapter's volume cover.

    The volume cover is that of a chapter that opens a covered volume (SPEC "Metadata", Volume covers).
    """
    download = chapter["download_path"] if adopted is None else adopted["download_path"]
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

    def record(conn: db.Connection) -> None:
        notes = json.dumps(verdict.notes) if verdict.notes else None
        columns: dict[str, object] = (
            {"notes": notes} if adopted is None else {**adopted, "notes": notes, "redownload": None}
        )
        conn.execute(
            sql.SQL("UPDATE chapter SET {} WHERE id = %s").format(
                sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in columns)
            ),
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


def bulk_conversion_over(ctx: workflow.Context, chapter: db.Row) -> bool:
    """Return whether the bulk_conversion wait ends: the series has its baseline.

    The baseline is taken here when its record can be read, so a chapter waiting past validate, left by an older
    version, goes on with one.
    """
    series = _series(ctx, chapter)
    return not baseline.needed(ctx.conn, series) or baseline.take(ctx, series) is None


def _volume_cover(ctx: workflow.Context, chapter: db.Row, download: str) -> tuple[str, str, str] | None:
    """Return (path, sha256, volume) of the cover the chapter takes, else None.

    It takes one when it opens a covered volume and MangaDex has that volume's cover. A chapter that already has its
    cover keeps it. A chapter that names its published file (an adopted file's conversion) takes the cover its file
    holds (rewrite_covers.held_cover), and none is resolved.
    """
    if ctx.conn.execute("SELECT 1 FROM volume_cover WHERE chapter_id = %s", (chapter["id"],)).fetchone():
        return None
    if chapter["published_file_id"] is not None:
        file = db.one_row(ctx.conn, "SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],))
        return rewrite_covers.held_cover(ctx, file)
    named = chapter_names.resolve(ctx, _series(ctx, chapter), {chapter["id"]: download}).get(chapter["id"])
    if named is None or (covered := named.covered_volume) is None:
        return None
    volume = names.fmt_num(covered)  # the chapter's volume
    taken = ctx.conn.execute(
        """SELECT 1 FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id
        WHERE c.series_id = %(series)s AND v.volume = %(volume)s AND c.status != 'done'
        UNION ALL SELECT 1 FROM published_file WHERE series_id = %(series)s
            AND (cover_state IN ('volume:' || %(volume)s, 'stale:' || %(volume)s)
                OR pending_cover IN ('volume:' || %(volume)s, 'stale:' || %(volume)s))""",
        {"series": chapter["series_id"], "volume": volume},
    ).fetchone()
    if taken:  # an open chapter of that volume, or a published file, took its cover first: it keeps it
        return None
    found = covers.volume_cover(ctx, chapter["series_id"], covered)
    return found and (*found, volume)
