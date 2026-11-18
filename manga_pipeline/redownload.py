"""Re-downloading a bad download (SPEC "Bad downloads"): its state, the next source, removal and the wait check.

The chapter's re-download state, picking the next source and matching the chapter on it, removing a rejected copy, and
the suwayomi_download wait check. The validate stage drives it; only `intake` calls it (it reaches Suwayomi).

State (chapter.redownload, JSON): {defect, original: {source, manga_id, chapter_id}, rejected: [{source, chapter_id,
digest}], tried: [source display name], same_source_done, expected: target | null, chosen: chapter id | null,
remove: [{path, chapter_id, dequeue}]}; a target is {source, manga_id, chapter_id, path, enqueued_at}.
"""

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Literal

from rapidfuzz import fuzz

from manga_pipeline import config, db, errors, names, retention, workflow
from manga_pipeline.clients import suwayomi
from manga_pipeline.db import sql
from manga_pipeline.replies import suwayomi as suwayomi_replies

log = logging.getLogger(__name__)
EXACT_TITLE_MIN = 90  # fuzzy ratio a lone near-title hit needs
TITLE_MARGIN = 10  # every other hit at least this far below it
NEIGHBOURS = 10  # chapters within this many numbers of ours check that 2 sources number alike
MIN_NEIGHBOURS = 3
AGREE_SHARE = 0.90
NEIGHBOUR_TITLE_MIN = 80


def load(chapter: db.Row) -> workflow.RedownloadState | None:
    """Return the chapter's re-download state (chapter.redownload, parsed), or None when it has none."""
    return None if chapter["redownload"] is None else workflow.read_redownload(chapter["redownload"])


def save(ctx: workflow.Context, chapter: db.Row, state: workflow.Redownload) -> None:
    """Write the state in its own transaction, so each step lands before the next call to Suwayomi."""
    with db.transaction(ctx.conn):
        save_in(ctx, chapter, state)


def save_in(ctx: workflow.Context, chapter: db.Row, state: workflow.Redownload | None, **columns: object) -> None:
    """Write the state (None clears it) and other chapter columns inside the caller's transaction.

    Runs while this part holds the chapter's claim; raises when a person changed the row meanwhile.
    """
    sets = sql.SQL("").join(sql.SQL(", {} = %s").format(sql.Identifier(name)) for name in columns)
    changed = ctx.conn.execute(
        sql.SQL(
            "UPDATE chapter SET redownload = %s{sets} WHERE id = %s AND claimed_by = %s AND claim_count = %s"
            " AND status = 'claimed'"
        ).format(sets=sets),
        (
            None if state is None else json.dumps(state),
            *columns.values(),
            chapter["id"],
            ctx.owner,
            chapter["claim_count"],
        ),
    ).rowcount
    if not changed:
        msg = f"chapter {chapter['id']} changed while {ctx.part} re-downloaded it"
        raise RuntimeError(msg)


def new_state(ctx: workflow.Context, chapter: db.Row, chosen: int | None) -> workflow.Redownload:
    """Return the state of a chapter rejected for the first time (chosen: the replacement a person chose, or None).

    A hand-placed chapter (no Suwayomi chapter) has no original source.
    """
    found = None if chapter["suwayomi_chapter_id"] is None else target(ctx, chapter["suwayomi_chapter_id"])
    original: workflow.Original = {
        # by Suwayomi's display name, which sources.ranking uses (chapter.source may hold a source id)
        "source": chapter["source"] if found is None else found["source"],
        "manga_id": None if found is None else found["manga_id"],
        "chapter_id": chapter["suwayomi_chapter_id"],
    }
    return {
        "defect": None,
        "original": original,
        "rejected": [],
        "tried": [],
        "same_source_done": False,
        "expected": None,
        "chosen": chosen,
        "remove": [],
    }


def remove(ctx: workflow.Context, chapter: db.Row, state: workflow.Redownload) -> None:
    """Remove the copies the state lists. Safe to run again.

    Each comes off the queue when queued, its file goes the retention way, then Suwayomi's downloaded mark when it has
    a Suwayomi chapter (a file already gone is no error to Suwayomi).
    """
    for item in state["remove"]:
        workflow.holds_claim(ctx, chapter)  # before each removal: it cannot be undone
        chapter_id = item["chapter_id"]  # None: a hand-placed copy, which Suwayomi knows nothing of
        if chapter_id is not None and item["dequeue"]:
            ctx.suwayomi.dequeue([chapter_id])
        retention.put(ctx.cfg, config.to_local(ctx.cfg, item["path"]))
        if chapter_id is not None:
            ctx.suwayomi.delete_downloaded([chapter_id])
    if state["remove"]:
        state["remove"] = []
        save(ctx, chapter, state)


def target(ctx: workflow.Context, chapter_id: int) -> workflow.Target | None:
    """Return {source, manga_id, chapter_id, path, url} of a Suwayomi chapter of any manga.

    None when Suwayomi has no such chapter or no source for it.
    """
    row = ctx.suwayomi.chapter(chapter_id)
    if row is None:
        return None
    manga = ctx.suwayomi.manga(row["mangaId"])
    if not manga or manga["source"] is None:
        return None
    source_folder, manga_folder = suwayomi.download_folders(manga["source"], manga["title"])
    path = (
        config.abs_path(ctx.cfg["paths"]["downloads"])
        / suwayomi.MANGAS_FOLDER
        / source_folder
        / manga_folder
        / suwayomi.chapter_file(row["scanlator"], row["name"])
    )
    return {
        "source": manga["source"]["displayName"],
        "manga_id": manga["id"],
        "chapter_id": chapter_id,
        "path": config.to_stored(ctx.cfg, path),
        "url": row["realUrl"] or None,  # the chapter's page at its source (SPEC "Metadata", ComicInfo)
    }


def pick(
    ctx: workflow.Context, chapter: db.Row, series: db.Row, state: workflow.Redownload
) -> workflow.Target | workflow.SameSource | workflow.Failed | workflow.NeedsHuman:
    """Pick the next download to queue: the original chapter once, then the best-ranked matching source.

    After the original chapter, the best-ranked installed source not tried that matches the chapter. Records each
    source tried as it goes. Returns a target, or an outcome when none is left: Failed when every remaining source could
    not answer, else NeedsHuman. A hand-placed chapter has no original chapter; its numbering is checked against the
    series' reference manga, found again on every pick.
    """
    original = state["original"]
    original_id = original["chapter_id"]
    hand_placed = original_id is None
    if not state["same_source_done"] and not hand_placed:
        found = target(ctx, original_id)
        if found and not held_elsewhere(ctx, chapter, found):
            return {**found, "same_source": True}  # enqueue records it as done, with the expected download
        state["same_source_done"] = True
        save(ctx, chapter, state)
    reference = reference_manga(ctx, chapter["series_id"]) if hand_placed else original["manga_id"]
    if hand_placed and reference is None:
        return workflow.NeedsHuman("the series has no Suwayomi manga to check numbering against; no source tried")
    installed = {s["displayName"]: s["id"] for s in ctx.suwayomi.sources()}
    down: list[str] = []
    for name in ctx.cfg["sources"]["ranking"]:
        if name in state["tried"] or name == original["source"] or name not in installed:
            continue
        try:
            found = _match(ctx, chapter, series, reference, installed[name])
        except suwayomi.SourceUnavailableError as e:
            down.append(f"{name} ({str(e).splitlines()[0]})")  # Suwayomi appends the source's Java stack trace
            continue
        if found and not held_elsewhere(ctx, chapter, found):
            return found
        state["tried"].append(name)
        save(ctx, chapter, state)
    first = original["source"] or ("the hand-placed file" if hand_placed else "the original source")
    tried = ", ".join(dict.fromkeys([first, *state["tried"]]))
    if down:
        return workflow.Failed(f"every remaining source could not answer: {'; '.join(down)} (tried: {tried})")
    return workflow.NeedsHuman(
        f"no source left to re-download from (tried: {tried})"  # status adds the commands that settle it
    )


def held_elsewhere(ctx: workflow.Context, chapter: db.Row, found: workflow.Target) -> bool:
    """Whether another open chapter holds the target's Suwayomi chapter or file: its download, not this one's."""
    return (
        ctx.conn.execute(
            """SELECT 1 FROM chapter WHERE id != %(id)s AND status != 'done' AND (suwayomi_chapter_id = %(chapter)s
                OR download_path = %(path)s OR redownload::jsonb #>> '{expected,path}' = %(path)s)""",
            {"id": chapter["id"], "chapter": found["chapter_id"], "path": found["path"]},
        ).fetchone()
        is not None
    )


def reference_manga(ctx: workflow.Context, series_id: int) -> int | None:
    """Return the Suwayomi manga linked to the series that a hand-placed chapter's numbering is checked against.

    Of those Suwayomi still holds with an installed source, the best ranked by sources.ranking, then the lowest id;
    else None.
    """
    ranking = ctx.cfg["sources"]["ranking"]
    best: tuple[int, int] | None = None
    rows = ctx.conn.execute("SELECT manga_id FROM suwayomi_manga WHERE series_id = %s ORDER BY manga_id", (series_id,))
    for (manga_id,) in rows.fetchall():
        manga = ctx.suwayomi.manga(manga_id)
        if manga is None or manga["source"] is None:
            continue
        name = manga["source"]["displayName"]
        rank = ranking.index(name) if name in ranking else len(ranking)
        if best is None or rank < best[0]:
            best = (rank, manga_id)
    return None if best is None else best[1]


def _match(
    ctx: workflow.Context, chapter: db.Row, series: db.Row, reference: int | None, source_id: str
) -> workflow.Target | None:
    """Return the target on another source matching the chapter (SPEC "Bad downloads"), or None.

    reference is the manga whose chapters the source's numbering must agree with (None: no source agrees).
    """
    names_given: list[str | None] = [series["name"], series["localized_name"], series["original_name"]]
    manga = ctx.suwayomi.manga(reference) if reference else None
    if manga:
        names_given.append(manga["title"])
    known = list(dict.fromkeys(n for n in names_given if n))
    hits: dict[int, suwayomi_replies.Manga] = {}
    for query in known:
        for hit in ctx.suwayomi.search_source(source_id, query):
            hits[hit["id"]] = hit
    found = pick_manga(list(hits.values()), known)
    if found is None:
        return None
    other = ctx.suwayomi.fetch_manga_and_chapters(found["id"])["chapters"]
    ours = ctx.suwayomi.chapters(reference) if reference else []
    row = pick_chapter(chapter["identity"], chapter["number"], ours, other)  # a download past discover has one
    return None if row is None else target(ctx, row["id"])


def pick_manga(hits: Sequence[suwayomi_replies.Manga], known: Iterable[str]) -> suwayomi_replies.Manga | None:
    """Return the 1 search hit that is the series, or None.

    It is the only one whose title equals a known name by names.key, else the only one at a fuzzy ratio of
    EXACT_TITLE_MIN or more with every other hit TITLE_MARGIN below it.
    """
    keys = {names.key(n) for n in known}
    exact = [h for h in hits if names.key(h["title"]) in keys]
    if exact:
        return exact[0] if len(exact) == 1 else None
    scored = sorted(((max(fuzz.ratio(names.key(h["title"]), k) for k in keys), h["id"], h) for h in hits), reverse=True)
    if not scored or scored[0][0] < EXACT_TITLE_MIN:
        return None
    if len(scored) > 1 and scored[1][0] > scored[0][0] - TITLE_MARGIN:
        return None
    return scored[0][2]


def _parsed(row: suwayomi_replies.Chapter) -> tuple[list[str | None], float | None]:
    """(identity list, number as float or None) of a Suwayomi chapter row, parsed as its download file's stem."""
    item = names.parse(suwayomi.chapter_file(row["scanlator"], row["name"])[: -len(".cbz")])
    return names.read_identity(names.identity(item)), (None if item.number is None else float(item.number))


def pick_chapter(
    identity: str, number: float, ours: Sequence[suwayomi_replies.Chapter], other: Sequence[suwayomi_replies.Chapter]
) -> suwayomi_replies.Chapter | None:
    """Return the row of `other` (the other source's chapters) that is our chapter; None when not confident.

    identity is the chapter's names.identity JSON, number its number. The row is checked against `ours` (the original
    source's chapters) for numbering that agrees.
    """
    me = names.read_identity(identity)
    key = tuple(me[1:4])
    parsed = [(row, *_parsed(row)) for row in other]
    rows = [row for row, ident, _ in parsed if tuple(ident[1:4]) == key]
    if len(rows) > 1:
        rows = [row for row in rows if _parsed(row)[0][0] == me[0]]
    if len(rows) != 1:
        return None
    by_key: dict[tuple[str | None, ...], list[str | None]] = {}
    for _, ident, _ in parsed:
        by_key.setdefault(tuple(ident[1:4]), []).append(ident[4])
    neighbours: list[list[str | None]] = []
    for row in ours:
        ident, n = _parsed(row)
        same_run = tuple(ident[1:3]) == key[:2] and tuple(ident[1:4]) != key  # same season and side label, not ours
        if n is not None and same_run and abs(n - number) <= NEIGHBOURS:
            neighbours.append(ident)
    if len(neighbours) < MIN_NEIGHBOURS:
        return None
    agree = sum(_agrees(ident, by_key.get(tuple(ident[1:4]), ())) for ident in neighbours)
    return rows[0] if agree >= AGREE_SHARE * len(neighbours) else None


def _agrees(ident: list[str | None], their_titles: Sequence[str | None]) -> bool:
    """Return whether a neighbour chapter is listed on the other source under its key.

    Its title must be like theirs where both have one.
    """
    if not their_titles:
        return False
    title = ident[4]
    return any(not title or not t or fuzz.ratio(title, t) >= NEIGHBOUR_TITLE_MIN for t in their_titles)


def enqueue(
    ctx: workflow.Context,
    chapter: db.Row,
    state: workflow.Redownload,
    found: workflow.Target | workflow.SameSource | workflow.Chosen,
) -> None:
    """Queue the target, start the downloader and record it as expected."""
    if "same_source" in found:
        state["same_source_done"] = True
    if "chosen" in found:
        state["chosen"] = None
    # Recorded first: a download Suwayomi starts is never untracked. An interruption before the queue call leaves an
    # expected download that is neither queued nor downloaded ("lost"), which validate queues again.
    expected: workflow.Expected = {
        "source": found["source"],
        "manga_id": found["manga_id"],
        "chapter_id": found["chapter_id"],
        "path": found["path"],
        "url": found["url"],
        "enqueued_at": workflow.now(ctx.conn),
    }
    state["expected"] = expected
    save(ctx, chapter, state)
    queue(ctx, expected)


def queue(ctx: workflow.Context, found: workflow.Expected) -> None:
    """Queue a target and start the downloader (Suwayomi skips a chapter already queued)."""
    ctx.suwayomi.enqueue([found["chapter_id"]])
    ctx.suwayomi.start_downloader()


def download_state(
    ctx: workflow.Context, expected: workflow.Expected
) -> Literal["complete", "waiting", "lost", "failed"]:
    """Return the expected download's state: "complete", "waiting", "lost" or "failed".

    "complete" (flagged, file present), "waiting" (queued or downloading), "lost" (neither queued nor downloaded) or
    "failed" (in error, gone from Suwayomi, flagged without its file, or not complete after the timeout).
    """
    row = ctx.suwayomi.chapter(expected["chapter_id"])
    if row is None:
        return "failed"
    if row["isDownloaded"]:
        return "complete" if errors.exists(config.to_local(ctx.cfg, expected["path"])) else "failed"
    queued_at = datetime.fromisoformat(expected["enqueued_at"])
    if datetime.fromisoformat(workflow.now(ctx.conn)) - queued_at > timedelta(
        hours=ctx.cfg["sources"]["download_timeout_hours"]
    ):
        return "failed"
    for entry in ctx.suwayomi.download_queue()["queue"]:
        if entry["chapter"]["id"] == expected["chapter_id"]:
            return "failed" if entry["state"] == "ERROR" else "waiting"
    return "lost"


def wait_over(ctx: workflow.Context, chapter: db.Row) -> bool:
    """Return whether the suwayomi_download wait ends.

    It ends when the expected download is complete or failed, or none is expected (no state, or only a person's
    choice).
    """
    state = load(chapter)
    if state is None or "original" not in state:
        return True
    expected = state["expected"]
    return expected is None or download_state(ctx, expected) != "waiting"
