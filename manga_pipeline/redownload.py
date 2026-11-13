"""Re-downloading a bad download (SPEC "Bad downloads"): the chapter's re-download state, picking the next source and
matching the chapter on it, removing a rejected copy, and the suwayomi_download wait check. The validate stage drives
it; only `intake` calls it (it reaches Suwayomi).

State (chapter.redownload, JSON): {defect, original: {source, manga_id, chapter_id}, rejected: [{source, chapter_id,
digest}], tried: [source display name], same_source_done, expected: target | null, chosen: chapter id | null,
remove: [{path, chapter_id, dequeue}]}; a target is {source, manga_id, chapter_id, path, enqueued_at}.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz

from manga_pipeline import config, db, names, retention, workflow
from manga_pipeline.clients import suwayomi

log = logging.getLogger(__name__)
EXACT_TITLE_MIN = 90  # fuzzy ratio a lone near-title hit needs
TITLE_MARGIN = 10  # every other hit at least this far below it
NEIGHBOURS = 10  # chapters within this many numbers of ours check that 2 sources number alike
MIN_NEIGHBOURS = 3
AGREE_SHARE = 0.90
NEIGHBOUR_TITLE_MIN = 80


def load(chapter):
    return None if chapter["redownload"] is None else json.loads(chapter["redownload"])


def save(ctx, chapter, state):
    """Write the state in its own transaction, so each step lands before the next call to Suwayomi."""
    with db.transaction(ctx.conn):
        save_in(ctx, chapter, state)


def save_in(ctx, chapter, state, **columns):
    """Write the state (None clears it) and other chapter columns inside the caller's transaction, while this part
    holds the chapter's claim; raises when a person changed the row meanwhile."""
    sets = "".join(f", {name} = ?" for name in columns)
    changed = ctx.conn.execute(
        f"UPDATE chapter SET redownload = ?{sets} WHERE id = ? AND claimed_by = ? AND status = 'claimed'",
        (None if state is None else json.dumps(state), *columns.values(), chapter["id"], ctx.part),
    ).rowcount
    if not changed:
        raise RuntimeError(f"chapter {chapter['id']} changed while {ctx.part} re-downloaded it")


def new_state(ctx, chapter, state):
    """The state of a chapter rejected for the first time (state: what a person set, e.g. chosen, or {})."""
    found = target(ctx, chapter["suwayomi_chapter_id"])
    original = {
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
        "chosen": None,
        "remove": [],
        **state,
    }


def remove(ctx, chapter, state):
    """Remove the copies the state lists: off the queue when queued, the file the retention way, then Suwayomi's
    downloaded mark (a file already gone is no error to Suwayomi). Safe to run again."""
    for item in state["remove"]:
        if item.get("dequeue"):
            ctx.suwayomi.dequeue([item["chapter_id"]])
        retention.put(ctx.cfg, Path(item["path"]))
        ctx.suwayomi.delete_downloaded([item["chapter_id"]])
    if state["remove"]:
        state["remove"] = []
        save(ctx, chapter, state)


def target(ctx, chapter_id):
    """{source, manga_id, chapter_id, path} of a Suwayomi chapter of any manga; None when Suwayomi has no such
    chapter or no source for it."""
    row = ctx.suwayomi.chapter(chapter_id)
    manga = row and ctx.suwayomi.manga(row["mangaId"])
    if not manga or manga["source"] is None:
        return None
    display = manga["source"]["displayName"]
    path = (
        config.abs_path(ctx.cfg["paths"]["downloads"])
        / suwayomi.MANGAS_FOLDER
        / suwayomi.source_folder(display)
        / suwayomi.manga_folder(manga["title"])
        / suwayomi.chapter_file(row["scanlator"], row["name"])
    )
    return {"source": display, "manga_id": manga["id"], "chapter_id": chapter_id, "path": str(path)}


def pick(ctx, chapter, series, state):
    """The next download to queue: the original chapter once, then the best-ranked installed source not tried that
    matches the chapter. Records each source tried as it goes. Returns a target, or an outcome when none is left:
    Failed when every remaining source could not answer, else NeedsHuman."""
    if not state["same_source_done"]:
        found = target(ctx, state["original"]["chapter_id"])
        if found and not held_elsewhere(ctx, chapter, found):
            return {**found, "same_source": True}  # enqueue records it as done, with the expected download
        state["same_source_done"] = True
        save(ctx, chapter, state)
    installed = {s["displayName"]: s["id"] for s in ctx.suwayomi.sources()}
    down = []
    for name in ctx.cfg["sources"]["ranking"]:
        if name in state["tried"] or name == state["original"]["source"] or name not in installed:
            continue
        try:
            found = _match(ctx, chapter, series, state, installed[name])
        except suwayomi.SourceUnavailable as e:
            down.append(f"{name} ({str(e).splitlines()[0]})")  # Suwayomi appends the source's Java stack trace
            continue
        if found and not held_elsewhere(ctx, chapter, found):
            return found
        state["tried"].append(name)
        save(ctx, chapter, state)
    tried = ", ".join(dict.fromkeys([state["original"]["source"] or "the original source", *state["tried"]]))
    if down:
        return workflow.Failed(f"every remaining source could not answer: {'; '.join(down)} (tried: {tried})")
    return workflow.NeedsHuman(
        f"no source left to re-download from (tried: {tried}) -> chapter use {chapter['id']} SUWAYOMI_CHAPTER_ID, "
        f"or chapter drop {chapter['id']}"
    )


def held_elsewhere(ctx, chapter, found):
    """Whether another open chapter holds the target's Suwayomi chapter or file: its download, not this one's."""
    return (
        ctx.conn.execute(
            """SELECT 1 FROM chapter WHERE id != :id AND status != 'done' AND (suwayomi_chapter_id = :chapter
                OR download_path = :path OR json_extract(redownload, '$.expected.path') = :path)""",
            {"id": chapter["id"], "chapter": found["chapter_id"], "path": found["path"]},
        ).fetchone()
        is not None
    )


def _match(ctx, chapter, series, state, source_id):
    """The target on another source matching the chapter (SPEC "Bad downloads"), or None."""
    original = state["original"]
    known = [series["name"], series["localized_name"], series["original_name"]]
    manga = original["manga_id"] and ctx.suwayomi.manga(original["manga_id"])
    if manga:
        known.append(manga["title"])
    known = list(dict.fromkeys(n for n in known if n))
    hits = {}
    for query in known:
        for hit in ctx.suwayomi.search_source(source_id, query):
            hits[hit["id"]] = hit
    found = pick_manga(list(hits.values()), known)
    if found is None:
        return None
    other = ctx.suwayomi.fetch_manga_and_chapters(found["id"])["chapters"]
    ours = ctx.suwayomi.chapters(original["manga_id"]) if original["manga_id"] else []
    row = pick_chapter(chapter["identity"], ours, other)
    return None if row is None else target(ctx, row["id"])


def pick_manga(hits, known):
    """The 1 search hit that is the series: the only one whose title equals a known name by names.key, else the only
    one at a fuzzy ratio of EXACT_TITLE_MIN or more with every other hit TITLE_MARGIN below it; else None."""
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


def _parsed(row):
    """(identity list, number as float or None) of a Suwayomi chapter row, parsed as its download file's stem."""
    item = names.parse(suwayomi.chapter_file(row["scanlator"], row["name"])[: -len(".cbz")])
    return json.loads(names.identity(item)), (None if item.number is None else float(item.number))


def pick_chapter(identity, ours, other):
    """The row of `other` (the other source's chapters) that is our chapter (identity: names.identity JSON), checked
    against `ours` (the original source's chapters) for numbering that agrees; None when not confident."""
    me = json.loads(identity)
    key = tuple(me[1:4])
    parsed = [(row, *_parsed(row)) for row in other]
    rows = [row for row, ident, _ in parsed if tuple(ident[1:4]) == key]
    if len(rows) > 1:
        rows = [row for row in rows if _parsed(row)[0][0] == me[0]]
    if len(rows) != 1:
        return None
    by_key = {}
    for _, ident, _ in parsed:
        by_key.setdefault(tuple(ident[1:4]), []).append(ident[4])
    number = float(me[3])
    neighbours = []
    for row in ours:
        ident, n = _parsed(row)
        same_run = tuple(ident[1:3]) == key[:2] and tuple(ident[1:4]) != key  # same season and side label, not ours
        if n is not None and same_run and abs(n - number) <= NEIGHBOURS:
            neighbours.append(ident)
    if len(neighbours) < MIN_NEIGHBOURS:
        return None
    agree = sum(_agrees(ident, by_key.get(tuple(ident[1:4]), ())) for ident in neighbours)
    return rows[0] if agree >= AGREE_SHARE * len(neighbours) else None


def _agrees(ident, their_titles):
    """A neighbour chapter is listed on the other source under its key, with a like title where both have one."""
    if not their_titles:
        return False
    title = ident[4]
    return any(not title or not t or fuzz.ratio(title, t) >= NEIGHBOUR_TITLE_MIN for t in their_titles)


def enqueue(ctx, chapter, state, found):
    """Queue the target, start the downloader and record it as expected."""
    found = dict(found)
    if found.pop("same_source", False):
        state["same_source_done"] = True
    if found.pop("chosen", False):
        state["chosen"] = None
    # Recorded first: a download Suwayomi starts is never untracked. An interruption before the queue call leaves an
    # expected download that is neither queued nor downloaded ("lost"), which validate queues again.
    state["expected"] = {**found, "enqueued_at": workflow.now()}
    save(ctx, chapter, state)
    queue(ctx, found)


def queue(ctx, found):
    """Queue a target and start the downloader (Suwayomi skips a chapter already queued)."""
    ctx.suwayomi.enqueue([found["chapter_id"]])
    ctx.suwayomi.start_downloader()


def download_state(ctx, expected):
    """ "complete" (flagged, file present), "waiting" (queued or downloading), "lost" (neither queued nor downloaded)
    or "failed" (in error, gone from Suwayomi, flagged without its file, or not complete after the timeout)."""
    row = ctx.suwayomi.chapter(expected["chapter_id"])
    if row is None:
        return "failed"
    if row["isDownloaded"]:
        return "complete" if Path(expected["path"]).exists() else "failed"
    queued_at = datetime.fromisoformat(expected["enqueued_at"])
    if datetime.now(UTC) - queued_at > timedelta(hours=ctx.cfg["sources"]["download_timeout_hours"]):
        return "failed"
    for entry in ctx.suwayomi.download_queue()["queue"]:
        if entry["chapter"]["id"] == expected["chapter_id"]:
            return "failed" if entry["state"] == "ERROR" else "waiting"
    return "lost"


def wait_over(ctx, chapter):
    """The suwayomi_download wait ends when the expected download is complete or failed, or none is expected."""
    state = load(chapter)
    expected = state and state["expected"]
    return expected is None or download_state(ctx, expected) != "waiting"
