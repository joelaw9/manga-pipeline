"""Stage junk (SPEC "Chapter stages", "State", "Bad downloads", "Junk decisions"): checks, then scan or decide.

First the put-aside check (the series' baseline, taken at validate, recorded what was handled), then the open-copy wait
and the library-copy check's part that no junk changes (SPEC "Library copies"), then a numbered notice title's
judgement (notices.py), then junk; the rest of the library-copy check runs once the junk is decided (_finished). A
chapter that names its published file (an adopted file's conversion, a better copy that took its file) skips the
put-aside check and the notice judgement: its file's pages are already published.

A chapter with no current scan is scanned (pages extracted to its scratch folder, measured, its units keyed, OCR on
the strip units no decision covers, every content page's logo measured) and stays ready (Ready), so every ready junk
chapter of a series is scanned before any is decided. A chapter with a current scan is decided from it, its download
extracted again (once per decide, removed when it ends) only when a chain or a cut decision is tried or candidate
images are written: done with its chapter edit, waiting on junk_decision with its candidates (their images written
first), or needs_human (a chain or cut decision that should apply and does not; a removal that would leave too little;
a candidate Claude was unsure of or past the review limit). Done keeps the scan and the watermark keys its decide used
on the chapter's edit row (SPEC "Junk decisions"). A strip chapter's rest-of-card bundle (W5,
strip_units.rest_members) is recorded with the candidates it waits on (junk_scan.bundles); a bundle whose members'
decisions differ gives the chapter a note at Done. A decide that ends the stage logs the units other series' agreement
removed or kept (agreed_units); a watermark an agreement removed is not remembered.
"""

import dataclasses
import json
import logging
import math
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw

from manga_pipeline import (
    archive,
    config,
    db,
    errors,
    handled,
    images,
    library_copy,
    notices,
    page_store,
    strip,
    volumes,
    workflow,
)
from manga_pipeline.convert import plan
from manga_pipeline.db import sql
from manga_pipeline.junk import (
    anchored,
    candidates,
    chapter_scan,
    groups,
    logo_units,
    marks,
    ocr,
    page_units,
    sheet_draw,
    store,
    strip_units,
)

log = logging.getLogger(__name__)


def read_scan(text: str) -> chapter_scan.Scan:
    """Return a chapter's stored scan (junk_scan.scan, JSON text). Unchecked."""
    return json.loads(text)


def _candidate_ids(text: str) -> list[int]:
    """Return the candidates a chapter waits on (junk_scan.candidates, JSON text): their ids. Unchecked."""
    return json.loads(text)


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Run the renamed, put-aside, open-copy, library-copy and notice checks, then scan the chapter, or decide it.

    A chapter with no content digest fails before any check acts: the put-aside check and its scan need the digest.
    """
    if not chapter["content_digest"]:
        msg = f"chapter {chapter['id']} has no content digest: discovery sets it before junk"
        raise ValueError(msg)
    why = handled.renamed(ctx.cfg, ctx.conn, chapter)
    if why:  # a scan it made before its file went goes with it, so no candidate counts it

        def forget_scan(conn: db.Connection) -> None:
            conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))

        return workflow.Dropped(why, record=forget_scan)
    series = db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    names_file = chapter["published_file_id"] is not None
    why = not names_file and handled.put_aside(ctx, chapter, chapter["content_digest"])
    if why:
        return workflow.Dropped(why)
    if library_copy.earlier_copy(ctx.conn, chapter):
        return workflow.Waiting("open_copy")
    outcome = library_copy.judge(ctx, chapter) or (None if names_file else notices.judge(ctx, chapter))
    if outcome:
        return outcome
    key = store.series_key(series)
    row = current_scan(ctx.conn, chapter)
    if row is None:
        return scan(ctx, chapter, series, key)
    return decide(ctx, chapter, key, row)


def current_scan(conn: db.Connection, chapter: db.Row) -> db.Row | None:
    """Return the chapter's junk_scan row when it is current, else None.

    Current: same series, same content digest, holding logos (a scan made before logo detection holds none), made under
    the series' layout.
    """
    current = workflow.SCANNED.format(c=sql.Identifier("c"))
    query = sql.SQL("SELECT s.* FROM chapter c JOIN junk_scan s ON s.chapter_id = c.id WHERE c.id = %s AND {}")
    return conn.execute(query.format(current), (chapter["id"],)).fetchone()


# ---------------------------------------------------------------- pages on local disk


def _scratch(ctx: workflow.Context, chapter: db.Row) -> Path:
    return config.abs_path(ctx.cfg["paths"]["scratch"]) / workflow.claim_name(chapter) / "junk"


class Pages:
    """A download's pages for 1 decide: extracted into the claim's scratch folder on first use, at most once.

    The caller removes the claim's folder when the decide ends. render(a, b) draws strip rows [a, b) over the content
    pages as the scan laid them (the matcher's pixels, SPEC "Junk decisions": chains and cuts).
    """

    def __init__(self, ctx: workflow.Context, chapter: db.Row) -> None:
        """Hold the chapter whose download is extracted on first use."""
        self._ctx, self._chapter = ctx, chapter
        self._files: tuple[list[Path], list[str], list[strip.Size]] | None = None
        self._render: Callable[[int, int], Image.Image] | None = None

    def files(self) -> tuple[list[Path], list[str], list[strip.Size]]:
        """Return the extracted pages' (paths, names, sizes), in archive order, extracting them the first time."""
        if self._files is None:
            paths, names, sizes, _ = extract(
                config.to_local(self._ctx.cfg, self._chapter["download_path"]),
                page_store.load(self._ctx.conn, self._chapter["id"]),
                _scratch(self._ctx, self._chapter),
            )
            self._files = (paths, names, sizes)
        return self._files

    def render(self, start: int, end: int) -> Image.Image:
        """Return the image of strip rows [start, end) over the download's content pages."""
        if self._render is None:
            self._render = chapter_scan.renderer(chapter_scan.Extracted(*self.files()))
        return self._render(start, end)


def extract(
    cbz: Path, known: Mapping[int, images.KnownPage], work: Path
) -> tuple[list[Path], list[str], list[strip.Size], list[images.MeasuredPage]]:
    """Write a CBZ's pages to `work` in archive order; return (paths, names, sizes, measurements).

    known: its page rows (page_store.load), so a page is measured only for what they lack.
    """
    measured = images.measure_pages(cbz, known)
    return write_pages(archive.pages(cbz), measured, work)


def write_pages(
    pages: Sequence[tuple[str, bytes]], measured: list[images.MeasuredPage], work: Path
) -> tuple[list[Path], list[str], list[strip.Size], list[images.MeasuredPage]]:
    """Write pages read from a CBZ (archive.pages) to `work`, emptied first; return extract's 4 lists."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    paths: list[Path]
    names: list[str]
    paths, names = [], []
    for i, (name, data) in enumerate(pages):
        path = work / f"{i:04d}{Path(name).suffix.lower()}"
        path.write_bytes(data)
        paths.append(path)
        names.append(name)
    sizes = [(m["width"], m["height"]) for m in measured]
    return paths, names, sizes, measured


def _layout(ctx: workflow.Context, chapter: db.Row, series: db.Row, content_paths: list[Path]) -> str:
    """Return the series' layout (SPEC "State").

    Its override file's `layout`, stored as the series' layout; else the stored one; else measured, for a new series
    over up to strip.SAMPLE_CHAPTERS of its chapters (this one among them), for any other over this chapter.
    OverrideError for a malformed override file. An override that changes a stored layout rescans the series' open
    downloads (workflow.layout_changed).
    """
    wanted = volumes.override_layout(config.abs_path(ctx.cfg["paths"]["naming_overrides"]), series["rules_key"])
    if wanted:
        with db.transaction(ctx.conn):
            before = ctx.conn.execute(
                "SELECT layout FROM series_conversion WHERE series_id = %s", (series["id"],)
            ).fetchone()
            if before is not None and before[0] != wanted:
                workflow.layout_changed(ctx.conn, series["id"])
            ctx.conn.execute(
                """INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, %s, 'override')
                ON CONFLICT (series_id) DO UPDATE SET layout = excluded.layout, origin = excluded.origin
                WHERE series_conversion.layout <> excluded.layout OR series_conversion.origin <> excluded.origin""",
                (series["id"], wanted),
            )
        return wanted
    stored = ctx.conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series["id"],)).fetchone()
    if stored is not None:
        return stored[0]
    chapters = [content_paths]
    if series["origin"] == "new":
        chapters += _sample(ctx, chapter, series)
    return strip.ensure_series_layout(ctx.conn, series["id"], chapters)


def _sample(ctx: workflow.Context, chapter: db.Row, series: db.Row) -> list[list[Path]]:
    """Return the content pages of up to SAMPLE_CHAPTERS - 1 more of a new series' chapters past validate.

    The chapters are spread out by id (first, last, then evenly between: first, middle and last for 3), this chapter
    counting as 1 of them, each extracted to the claim's scratch folder; only chapters whose download this machine
    can use and that still exist (the claim query's count, SPEC "State").
    """
    rows = ctx.conn.execute(
        """SELECT id, download_path FROM chapter
        WHERE series_id = %s AND kind = 'download' AND status <> 'done' AND stage NOT IN ('discover', 'validate')
        ORDER BY id""",
        (series["id"],),
    ).fetchall()
    usable = [
        r
        for r in rows
        if r["download_path"]
        and not workflow.unusable_here(r["download_path"])
        and (r["id"] == chapter["id"] or errors.exists(config.to_local(ctx.cfg, r["download_path"])))
    ]
    n, last = len(usable), strip.SAMPLE_CHAPTERS - 1
    spread = [0, n - 1, *(k * n // last for k in range(1, last))]
    picked = list(dict.fromkeys(usable[i]["id"] for i in spread)) if usable else []
    others = [i for i in picked if i != chapter["id"]][: strip.SAMPLE_CHAPTERS - 1]  # this chapter is always measured
    out: list[list[Path]] = []
    for r in usable:
        if r["id"] not in others:
            continue
        work = _scratch(ctx, chapter).parent / "sample" / str(r["id"])
        work.mkdir(parents=True, exist_ok=True)
        pages = archive.pages(config.to_local(ctx.cfg, r["download_path"]))
        content = strip.content_indexes([name for name, _ in pages])
        paths: list[Path] = []
        for i in content:
            name, data = pages[i]
            path = work / f"{i:04d}{Path(name).suffix.lower()}"
            path.write_bytes(data)
            paths.append(path)
        out.append(paths)
    return out


def _aspects(ctx: workflow.Context, series: db.Row, sizes: Sequence[strip.Size]) -> list[float]:
    """Return the series' page aspect candidates, computed again from every live chapter (SPEC "State", Layout).

    A record's stored candidates stand (aspect_origin record). Else those of the measured pages of the series' live
    chapters (workflow.live_page_sizes) replace the stored ones when the 2 differ to 2 decimals; with no page-like page
    among them, the stored ones stand, else this chapter's are taken. Read and written in 1 transaction.
    """
    with db.transaction(ctx.conn):
        aspects, stored = chapter_scan.page_aspects(ctx.conn, series["id"], sizes)
        row = db.one_row(ctx.conn, "SELECT aspect_origin FROM series_conversion WHERE series_id = %s", (series["id"],))
        whole = None
        if not (stored and row["aspect_origin"] == "record"):
            whole = strip.page_aspect([workflow.live_page_sizes(ctx.conn, series["id"])])
        if whole is not None and not (stored and strip.same_aspects(aspects, whole)):
            aspects, stored = whole, False
        if aspects and not stored:
            ctx.conn.execute(
                "UPDATE series_conversion SET page_aspect = %s, aspect_origin = 'computed' WHERE series_id = %s",
                (json.dumps(aspects), series["id"]),
            )
    return aspects


# ---------------------------------------------------------------- scan


def scan(ctx: workflow.Context, chapter: db.Row, series: db.Row, key: str) -> workflow.OUTCOMES:
    """Scan the chapter's pages by the series' layout and record the scan; the chapter stays ready (Ready).

    The pages are extracted to the claim's scratch folder, removed afterwards. A malformed override file, or strip OCR
    text holding a NUL character, is needs_human.
    """
    work = _scratch(ctx, chapter)
    try:
        cbz = config.to_local(ctx.cfg, chapter["download_path"])
        paths, names, sizes, measured = extract(cbz, page_store.load(ctx.conn, chapter["id"]), work)
        content = strip.content_indexes(names)
        try:
            layout = _layout(ctx, chapter, series, [paths[i] for i in content])
        except volumes.VolumeError as e:
            return workflow.NeedsHuman(str(e))
        pages = chapter_scan.Extracted(paths, names, sizes)
        sc: chapter_scan.Scan
        if layout == "strip":
            junk = ctx.cfg["junk"]
            sc = chapter_scan.scan_strip(pages, store.Decisions(ctx.conn, key), junk["ocr_device"], junk["ocr_workers"])
            nul = _nul_rows(sc)
            if nul:  # PostgreSQL's jsonb cannot hold it: storing the scan would fail every claim query
                rows = ", ".join(f"{s}-{e}" for s, e in nul)
                return workflow.NeedsHuman(f"OCR text of strip rows {rows} holds a NUL character: it cannot be stored")
        else:
            uniform = [m["junk_signature"] is None for m in measured]
            sc = chapter_scan.scan_pages(pages, uniform, _aspects(ctx, series, sizes))
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)  # the claim's folder: nothing of it is kept
    at = workflow.now(ctx.conn)
    seen = candidates.seen_rows(key, chapter["identity"], chapter_scan.seen_units(sc), at)

    def record(conn: db.Connection) -> None:
        conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chapter_id) DO UPDATE SET series_id = excluded.series_id,
                content_digest = excluded.content_digest, scan = excluded.scan, scanned_at = excluded.scanned_at,
                candidates = '[]', bundles = '[]'""",
            (chapter["id"], chapter["series_id"], chapter["content_digest"], json.dumps(sc), at),
        )
        conn.execute(  # a new scan: its first decide records the refill anew
            """UPDATE chapter SET junk_decided_at = NULL, junk_candidates = NULL, junk_new_series = NULL
            WHERE id = %s""",
            (chapter["id"],),
        )
        candidates.write_seen(conn, seen)

    return workflow.Ready(record=record)


def _nul_rows(sc: strip_units.StripScan) -> list[tuple[int, int]]:
    """Return [(start, end)] of the strip units whose OCR text holds a NUL.

    Those are the edge blocks and their join pieces, the only units OCR reads.
    """
    units = [u for b in sc["blocks"] for u in (b, *b["joins"])]
    return [(u["start"], u["end"]) for u in units if "\x00" in u.get("text", "")]


# ---------------------------------------------------------------- decide


def _refill(conn: db.Connection, chapter: db.Row, key: str, waited_on: int) -> None:
    """Record the junk refill with the outcome of the first decide after a scan (SPEC "Junk decisions", review).

    Its time, the candidates it waited on and whether the series had no decision of its own then. A later decide of
    that scan keeps it.
    """
    conn.execute(
        sql.SQL("""UPDATE chapter SET junk_decided_at = %s, junk_candidates = %s,
            junk_new_series = NOT EXISTS (SELECT 1 FROM junk_decision WHERE series_key = %s AND {active})
        WHERE id = %s AND junk_candidates IS NULL""").format(active=store.ACTIVE),
        (workflow.now(conn), waited_on, key, chapter["id"]),
    )


def _remember(conn: db.Connection, watermarks: Iterable[tuple[str, str]]) -> None:
    """With the outcome: the series' later chapters know these bands as watermarks."""
    db.executemany(
        conn, "INSERT INTO junk_watermark (series_key, key) VALUES (%s, %s) ON CONFLICT DO NOTHING", watermarks
    )


class Kept(NamedTuple):
    """What Done keeps on the chapter's edit row (SPEC "Junk decisions"): its scan and its decide's watermark keys.

    scan: the junk_scan row's text; watermarks: the series' known watermark keys the decide was given.
    """

    scan: str
    watermarks: list[str]


def _copy_closed(
    chapter: db.Row, key: str, applied: chapter_scan.Applied, copy: workflow.Dropped | workflow.NeedsHuman
) -> workflow.Dropped | workflow.NeedsHuman:
    """Return the library copy check's outcome of a chapter that offers no candidate (library_copy.after_junk).

    A duplicate dropped, or held needs_human for 2 published files, a volume that cannot be read, another chapter or a
    better copy of a busy file: no edit kept.
    """
    watermarks = [(key, k) for k in applied.watermarks]

    def closed(conn: db.Connection) -> None:
        _remember(conn, watermarks)
        _refill(conn, chapter, key, 0)  # no candidate: the copy check decides it
        if isinstance(copy, workflow.Dropped):  # a held copy keeps its scan for its retry
            conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))
        if copy.record:
            copy.record(conn)

    return dataclasses.replace(copy, record=closed)


def _done(
    chapter: db.Row, key: str, applied: chapter_scan.Applied, members: list[strip_units.RestPiece], kept: Kept
) -> workflow.Done:
    """Return Done with the chapter's edit and its kept scan with the time; its junk_scan row goes.

    Its rest-of-card note (rest_note) takes the place of an earlier one.
    """
    edit, watermarks = applied.edit, [(key, k) for k in applied.watermarks]

    def done(conn: db.Connection) -> None:
        _remember(conn, watermarks)
        _refill(conn, chapter, key, 0)
        conn.execute(
            """INSERT INTO chapter_edit (chapter_id, edit, scan, scan_watermarks, scan_at) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chapter_id) DO UPDATE SET edit = excluded.edit, scan = excluded.scan,
                scan_watermarks = excluded.scan_watermarks, scan_at = excluded.scan_at""",
            (chapter["id"], json.dumps(edit), kept.scan, json.dumps(kept.watermarks), workflow.now(conn)),
        )
        conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))
        text = db.one_row(conn, "SELECT notes FROM chapter WHERE id = %s", (chapter["id"],))[0]
        before = workflow.read_notes(text or "[]")
        notes = with_rest_note(before, rest_note(members))
        if notes != before:
            conn.execute(
                "UPDATE chapter SET notes = %s WHERE id = %s", (json.dumps(notes) if notes else None, chapter["id"])
            )

    return workflow.Done(record=done)


MIXED_NOTE = "rest of card mixed:"  # a chapter note's start: its bundle's members' decisions differ
BAR_W, BAR_LANE = 6, 14  # px: the red bar beside a strip unit's rows in its context image, and its lane
BOX_W = 4  # px: the red box round a page unit (its page, or a band) in its context image
PAGE_CONTEXT_H, PAGE_GAP = 800, 8  # px: a page unit's context pages' height (at most) and the gap between them


def rest_note(members: Sequence[strip_units.RestPiece]) -> str | None:
    """Return a chapter's rest-of-card note naming its bundle's members' rows, when every one is decided, not alike.

    None for no bundle, an undecided member, or members all junk or all kept.
    """
    verdicts = {m["verdict"] for m in members}
    if not members or None in verdicts or len(verdicts) == 1:
        return None
    return f"{MIXED_NOTE} rows " + ", ".join(f"{m['start']}-{m['end']}" for m in members)


def with_rest_note(notes: Sequence[str], note: str | None) -> list[str]:
    """Return a chapter's notes with its earlier rest-of-card note replaced by note (None: removed)."""
    return [*(n for n in notes if not n.startswith(MIXED_NOTE)), *([note] if note else [])]


def decide(ctx: workflow.Context, chapter: db.Row, key: str, row: db.Row) -> workflow.OUTCOMES:
    """Decide the chapter from its current scan; its download is extracted only when pixels are needed (Pages).

    The claim's scratch folder is removed when the decide ends.
    """
    pages = Pages(ctx, chapter)
    try:
        return _decided(ctx, chapter, key, row, pages)
    finally:
        shutil.rmtree(_scratch(ctx, chapter).parent, ignore_errors=True)  # the claim's folder: nothing of it is kept


def _decided(ctx: workflow.Context, chapter: db.Row, key: str, row: db.Row, pages: Pages) -> workflow.OUTCOMES:
    """Decide the chapter from its current scan (decide).

    Done with its chapter edit when no candidate is left (or the library copy check's outcome, when it has one);
    waiting on junk_decision with its candidates (their images written first); or needs_human (a chain or cut decision
    that should apply and does not; a removal that would leave too little; a candidate Claude was unsure of or past
    the review limit). A strip's chains and cut decisions read its pixels through `pages`.
    """
    sc = read_scan(row["scan"])
    decisions = store.Decisions(ctx.conn, key)
    inputs = decide_inputs(ctx.conn, decisions)
    memo = inputs.memo
    render = pages.render if sc["layout"] == "strip" else None
    applied = chapter_scan.apply(sc, inputs.decide_unit, inputs.watermarks, inputs.cuts, render)
    applied = applied._replace(watermarks=own_watermarks(applied.watermarks, applied.edit["strip_removed"], memo))
    members = strip_units.rest_members(applied.rest, lambda sig: decisions.judged_keep(images.sig_from_str(sig)))
    watermarks = [(key, k) for k in applied.watermarks]
    if applied.problem:
        _log_agreed(chapter, agreed_units(sc, applied, memo))

        def too_much(conn: db.Connection) -> None:
            _refill(conn, chapter, key, 0)  # no candidate: the edit itself needs a person

        return workflow.NeedsHuman(applied.problem, record=too_much)
    picks = candidates.select(ctx.conn, key, applied.undecided, ocr.word_lists(ctx.cfg["junk"]))
    if not picks:
        _log_agreed(chapter, agreed_units(sc, applied, memo))
        copy = library_copy.after_junk(ctx, chapter, applied.edit)
        if copy is not None:
            return _copy_closed(chapter, key, applied, copy)
        return _done(chapter, key, applied, members, Kept(row["scan"], inputs.watermarks))
    limit = ctx.cfg["junk"]["review_limit"]
    rows = [candidates.find(ctx.conn, decisions, p["kind"], p["key"]) for p in picks]
    held = [r for r in rows if r is not None and (r["verdict"] == "unsure" or candidates.stuck(r, limit))]
    if held:
        # the chapter keeps the candidates it waits on open, so junk decide can name them
        found = [None if r is None else int(r["id"]) for r in rows]
        bundle = bundle_record(picks, found, members)

        def hold(conn: db.Connection) -> None:
            _remember(conn, watermarks)
            _record_candidates(conn, chapter, [i for i in found if i is not None], bundle)
            _refill(conn, chapter, key, len(picks))

        _log_agreed(chapter, agreed_units(sc, applied, memo))
        return workflow.NeedsHuman(held_reason(held, limit, bundle), record=hold)
    new = [p for p, r in zip(picks, rows, strict=True) if r is None]
    if new:
        write_images(ctx, chapter, key, draw_candidates(new, pages, applied.edit["strip_removed"]))
    at = workflow.now(ctx.conn)

    def wait(conn: db.Connection) -> None:
        _remember(conn, watermarks)
        ids = candidates.join(conn, key, picks, at)
        _record_candidates(conn, chapter, ids, bundle_record(picks, ids, members))
        _refill(conn, chapter, key, len(picks))

    return workflow.Waiting("junk_decision", {"candidates": len(picks)}, record=wait)


type Memo = dict[tuple[str, str], db.Row | None]  # a decide's lookups: (unit kind, key text) -> the deciding row


class DecideInputs(NamedTuple):
    """A decide's inputs but its scan: the decision lookup, its memo, the known watermark keys and the cut decisions.

    The lookup is over the series' decisions, each answer kept in memo; the watermark keys sorted; the cut decisions as
    store.Decisions.cuts gives them.
    """

    decide_unit: store.DecideUnit
    memo: Memo
    watermarks: list[str]
    cuts: list[db.Row]


def decide_inputs(conn: db.Connection, decisions: store.Decisions) -> DecideInputs:
    """Return a decide's inputs over a series' decisions, read now (its watermark keys)."""
    memo: Memo = {}

    def decide_unit(kind: str, text: str) -> db.Row | None:
        if (kind, text) not in memo:
            memo[kind, text] = decisions.decide(kind, store.parse_key(kind, text))
        return memo[kind, text]

    known = conn.execute(
        "SELECT key FROM junk_watermark WHERE series_key = %s ORDER BY key", (decisions.series_key,)
    ).fetchall()
    return DecideInputs(decide_unit, memo, [r["key"] for r in known], decisions.cuts())


def own_watermarks(found: Sequence[str], removed: Sequence[plan.RemovedRange], memo: Memo) -> list[str]:
    """Return the watermark keys a decide found, but those only an agreement removed: none of them is remembered.

    From the rows that removed the strip ranges (_removing): a key a stand-in removed is dropped unless a stored row
    of the decide removed a range of that key too.
    """
    rows = _removing(removed, memo)
    agreed = {r["key"] for _, r in rows if store.agreeing(r)} - {r["key"] for _, r in rows if not store.agreeing(r)}
    return [k for k in found if k not in agreed]


def _removing(removed: Sequence[plan.RemovedRange], memo: Memo) -> list[tuple[str, db.Row]]:
    """Return (unit kind, deciding row) of each removed strip range a block or probe lookup of the decide decided."""
    out: list[tuple[str, db.Row]] = []
    for r in removed:
        for kind in ("block", "probe"):
            row = memo.get((kind, r["hash"])) if "hash" in r else None
            if row is not None and row["id"] == r.get("decision"):
                out.append((kind, row))
                break
    return out


def agreed_units(sc: chapter_scan.Scan, applied: chapter_scan.Applied, memo: Memo) -> list[str]:
    """Return the units of a decide's result that other series' agreement removed or kept, each named.

    From the decide's lookups (memo), the final row of each unit, no intermediate hit: a strip range removed by a
    stand-in's decision, an edge block left whole and kept by one; an edge page's row as page_units.page_decision
    ranks it; a band's row (a junk band only when its page is cropped); a logo variant decided only by agreement
    (marks.resolve over its representatives). Each with its kind, verdict and the agreeing decisions. A refused edit
    (applied.problem) names each agreed junk the decide looked up, and nothing kept.
    """
    if not any(r is not None and store.agreeing(r) for r in memo.values()):
        return []
    if applied.problem:
        refused = {(kind, r["agreeing"]): r for (kind, _), r in memo.items() if r is not None and store.agreeing(r)}
        return [
            x for (kind, _), r in refused.items() if r["verdict"] == "junk" for x in _named(f"{kind} (edit refused)", r)
        ]
    edit = applied.edit
    if sc["layout"] == "strip":
        out = _agreed_strip(sc, edit["strip_removed"], memo)
        variants = logo_units.decide(sc["marks"], logo_units.strip_survives(edit["strip_removed"]), _stored(memo))
    else:
        out = [] if sc["split"] is True else _agreed_pages(sc, edit, memo)
        variants = logo_units.decide(sc["marks"], logo_units.page_survives(edit), _stored(memo))
    for unit in variants["undecided"]:  # undecided by stored rows alone: decided, if at all, by agreement
        out += _named(f"logo on page {unit['page']}", marks.resolve([memo.get(("logo", k)) for k in unit["keys"]]))
    return out


def _named(what: str, row: db.Row | None) -> list[str]:
    """Return [what, its verdict and the agreeing decisions] when a stand-in row decides it, else []."""
    if row is None or not store.agreeing(row):
        return []
    return [f"{what} {row['verdict']} by decisions {' '.join(map(str, store.agreeing(row)))}"]


def _stored(memo: Memo) -> store.DecideUnit:
    """Return a lookup over memo that leaves out stand-in rows: what stored rows alone decide."""

    def lookup(kind: str, text: str) -> db.Row | None:
        row = memo.get((kind, text))
        return None if row is None or store.agreeing(row) else row

    return lookup


def _agreed_strip(sc: chapter_scan.StoredStripScan, removed: Sequence[plan.RemovedRange], memo: Memo) -> list[str]:
    """Return the strip ranges a stand-in's decision removed and the edge blocks one kept (agreed_units).

    A kept block is named only when no removed range (a probe's, a card's, a cut's) touches it: it is left whole.
    """
    out: list[str] = []
    for r in removed:
        found = _removing([r], memo)
        out += [x for kind, row in found for x in _named(f"{kind} rows {r['start']}-{r['end']}", row)]
    for b in sc["blocks"]:
        row = memo.get(("block", b["sig"]))
        whole = all(r["end"] <= b["start"] or r["start"] >= b["end"] for r in removed)
        if row is not None and row["verdict"] == "keep" and whole:
            out += _named(f"block rows {b['start']}-{b['end']}", row)
    return out


def _agreed_pages(sc: page_units.PageScan, edit: plan.ChapterEdit, memo: Memo) -> list[str]:
    """Return the edge pages and bands a stand-in row decides (agreed_units), as the band pass skips dropped pages."""

    def lookup(kind: str, text: str) -> db.Row | None:
        return memo.get((kind, text))

    out: list[str] = []
    skipped = set(edit["drop_pages"])
    cropped = {b["page"] for b in edit["bands"]}  # a page whose junk bands would leave too little is not
    for u in sc["edges"]:
        row = page_units.page_decision(u, lookup)
        if row is not None:
            out += _named(f"{row['kind']} of page {u['page']}", row)
        if u["frame"] and lookup("frame", u["frame"]) is not None:
            skipped.add(u["page"])
    for page, listed in sc["bands"].items():
        if int(page) in skipped:
            continue
        rows = [lookup("band", band["sig"]) for band in listed]
        out += [
            x
            for r in rows
            if r is not None and (r["verdict"] == "keep" or int(page) in cropped)
            for x in _named(f"band on page {page}", r)
        ]
    return out


def _log_agreed(chapter: db.Row, units: Sequence[str]) -> None:
    """Log the units an agreement removed or kept in the result of a decide that ends the chapter's junk stage."""
    if units:
        log.info("chapter %s: by the agreement of other series: %s", chapter["id"], "; ".join(units))


def _record_candidates(
    conn: db.Connection, chapter: db.Row, ids: Iterable[int], bundles: Sequence[candidates.Bundle]
) -> None:
    """Record the candidates a chapter waits on (each once, sorted) and its rest-of-card bundle."""
    conn.execute(
        "UPDATE junk_scan SET candidates = %s, bundles = %s WHERE chapter_id = %s",
        (json.dumps(sorted(set(ids))), json.dumps(bundles), chapter["id"]),
    )


def bundle_record(
    picks: Sequence[candidates.Candidate], ids: Sequence[int | None], members: Sequence[strip_units.RestPiece]
) -> list[candidates.Bundle]:
    """Return a chapter's bundle as junk_scan.bundles records it: its members' candidate ids and its member count.

    ids: each pick's candidate id (None: it has none). [] when no member is a candidate (no bundle, or all decided).
    """
    rows = {(m["start"], m["end"]) for m in members}
    found: set[int] = set()
    for p, i in zip(picks, ids, strict=True):
        unit = p["unit"]
        if i is not None and p["kind"] == "piece" and "start" in unit and (unit["start"], unit["end"]) in rows:
            found.add(i)
    return [{"ids": sorted(found), "size": len(members)}] if found else []


def held_reason(rows: Sequence[db.Row], limit: int, bundles: Iterable[candidates.Bundle]) -> str:
    """Return the needs_human reason naming each held candidate: Claude was unsure, or undecided after limit reviews.

    For a held candidate of a bundle of more than 1 member, also that bundle's candidates, to be judged together.
    """
    parts = [
        f"candidate {r['id']} ({r['kind']}): "
        + ("Claude was unsure" if r["verdict"] == "unsure" else f"undecided after {limit} reviews")
        for r in rows
    ]
    held = {r["id"] for r in rows}
    for b in bundles:
        if b["size"] > 1 and held & set(b["ids"]):
            ids = " ".join(map(str, b["ids"]))
            parts.append(
                f"rest of card {ids}: judge them together (junk decide {ids} junk|keep), or for junk among content"
                " junk cut CHAPTER:A:B --record DIGEST and keep the content members"
            )
    return "junk " + "; ".join(parts) + " -> junk decide ID junk|keep, then retry"


def junk_decision_over(ctx: workflow.Context, chapter: db.Row) -> bool | workflow.NeedsHuman:
    """Return whether the junk_decision wait is over (SPEC "Junk decisions"): True, NeedsHuman or False.

    Over (True) when the chapter's scan is stale (it rescans) or a candidate it waits on is covered by a decision or
    gone: the chapter is decided again and waits on what is still undecided then (a decision can make another candidate
    moot: a dropped page's band). Else NeedsHuman when one is unsure or stuck; else False.
    """
    row = ctx.conn.execute("SELECT * FROM junk_scan WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    if row is None or current_scan(ctx.conn, chapter) is None:
        return True
    limit = ctx.cfg["junk"]["review_limit"]
    ids = _candidate_ids(row["candidates"])
    held: list[db.Row] = []
    decisions = store.by_series(ctx.conn)
    for cid in ids:
        c = ctx.conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (cid,)).fetchone()
        if c is None:
            return True  # cleaned up: it was closed
        if decisions(c["series_key"]).decide(c["kind"], store.parse_key(c["kind"], c["key"])) is not None:
            return True
        if c["verdict"] == "unsure" or candidates.stuck(c, limit):
            held.append(c)
    if held:
        return workflow.NeedsHuman(held_reason(held, limit, candidates.read_bundles(row["bundles"])))
    return not ids


def open_copy_over(ctx: workflow.Context, chapter: db.Row) -> bool:
    """Return whether the open_copy wait is over (SPEC "Library copies"): no earlier open copy is ahead of it now."""
    return not library_copy.earlier_copy(ctx.conn, chapter)


# ---------------------------------------------------------------- candidate images


class Drawn(NamedTuple):
    """A new candidate's images, drawn: its unit and context images, its position and the removed ranges shown."""

    pick: candidates.Candidate
    unit: Image.Image
    context: Image.Image
    position: candidates.Position
    removed: list[candidates.Removed]


def draw_candidates(
    picks: Sequence[candidates.Candidate], pages: Pages, removed: Sequence[plan.RemovedRange]
) -> Iterator[Drawn]:
    """Draw the images of new candidates, 1 at a time (SPEC "Junk decisions", candidates).

    From the decide's pages (extracted once per decide; the decide removes them). A strip unit is rendered over the
    download's content pages (strip.content_indexes of its page names), as the scan laid them; removed: the strip
    ranges the decide's edit takes out, washed in a strip unit's context.
    """
    paths, names, sizes = pages.files()
    content = strip.content_indexes(names)
    for p in picks:
        unit = p["unit"]
        near: list[candidates.Removed] = []
        if "logo" in unit:  # a logo variant
            page = images.open_flat(paths[unit["page"]])[0].convert("RGB")
            unit_im, context_im = logo_units.review_images(page, unit["logo"])
            position: candidates.Position = {"page": unit["page"], "box": unit["logo"]["box"]}
            if "rows" in unit["logo"]:
                position["rows"] = unit["logo"]["rows"]
        elif "start" in unit:  # a strip unit: an edge block, its probe or a tail piece
            unit_im, context_im, near, height = _strip_images(content, paths, sizes, unit, removed)
            position = {"where": unit["where"], "rows": [unit["start"], unit["end"]], "height": height}
        else:  # a page unit: an edge page or frame, or a band
            unit_im, context_im = _page_images(paths, unit)
            position = {"page": unit["page"]}
            if "box" in unit:
                position |= {"where": unit["where"], "box": unit["box"]}
        yield Drawn(p, unit_im, context_im, position, near)


def write_images(ctx: workflow.Context, chapter: db.Row, key: str, drawn: Iterable[Drawn]) -> None:
    """Write new candidates' folders from their drawn images (SPEC "Junk decisions", candidates).

    info.json records where the unit sits, the removed ranges its context shows and the decisions its reason cites.
    """
    for d in drawn:
        p = d.pick
        info: candidates.CandidateInfo = {
            "series": key,
            "kind": p["kind"],
            "reason": p["reason"],
            "text": p["unit"].get("text", ""),
            "chapters": candidates.chapters_showing(ctx.conn, key, seen_kind(p["kind"]), p["key"]),
            "chapter": int(chapter["id"]),
            "position": d.position,
            "removed": d.removed,
            "cited": p.get("cited", []),
        }
        name = candidates.folder_name(key, p["kind"], p["key"], chapter)
        workflow.holds_claim(ctx, chapter)  # after rendering, immediately before the folder is written
        candidates.write_images(ctx.cfg, name, d.unit, d.context, info)
        p["folder"] = name  # the row join opens names it


def seen_kind(kind: str) -> str:
    """Return the kind a unit is recorded as seen under (a probe is part of a seen edge block)."""
    return "block" if kind == "probe" else kind


def _strip_images(
    content: Sequence[int],
    paths: Sequence[Path],
    sizes: Sequence[strip.Size],
    u: strip_units.BlockUnit | strip_units.PieceUnit | candidates.ProbeUnit,
    removed: Sequence[plan.RemovedRange],
) -> tuple[Image.Image, Image.Image, list[candidates.Removed], int]:
    """Return a strip unit's image, its context image, the removed ranges the context shows and the strip's height."""
    width, laid, height = strip.strip_layout([sizes[i] for i in content])
    pages: list[strip.StripPage] = [{**p, "path": str(paths[i])} for p, i in zip(laid, content, strict=True)]
    unit_im = strip.render_rows(pages, width, u["start"], u["end"])
    lo, hi = max(0, u["start"] - candidates.CONTEXT_PX), min(height, u["end"] + candidates.CONTEXT_PX)
    near: list[candidates.Removed] = []
    for r in (r for r in removed if r["end"] > lo and r["start"] < hi):
        shown: candidates.Removed = {"start": r["start"], "end": r["end"], "why": r["why"]}
        if "decision" in r:
            shown["decision"] = r["decision"]
        near.append(shown)
    context = strip_context(strip.render_rows(pages, width, lo, hi), (lo, hi), (u["start"], u["end"]), near)
    return unit_im, context, near, height


def removed_label(r: candidates.Removed) -> str:
    """Return a removed range's label on a candidate's context: its rows and its decision, or its rule."""
    return f"removed {r['start']}:{r['end']} " + (f"decision {r['decision']}" if "decision" in r else r["why"])


def strip_context(
    im: Image.Image, window: strip.Rows, unit: strip.Rows, removed: Sequence[candidates.Removed]
) -> Image.Image:
    """Return a strip unit's context image: strip rows `window` (im), the unit's rows marked by a red bar on the left.

    Every removed range washed grey and labelled on the right (removed_label). Drawn at the size the reader is sent
    (at most candidates.UNIT_WIDTH wide, groups.SHEET_LONG_EDGE long and groups.SHEET_PIXELS), so the bar and the
    labels keep their width.
    """
    labelled = [((r["start"], r["end"]), removed_label(r)) for r in removed]
    right = sheet_draw.lane_width([t for _, t in labelled]) if labelled else 0
    w, h = im.size
    lanes = BAR_LANE + right  # unscaled: (w s + lanes) h s <= SHEET_PIXELS, solved for s
    by_area = (math.sqrt((lanes * h) ** 2 + 4 * w * h * groups.SHEET_PIXELS) - lanes * h) / (2 * w * h)
    scale = min(1.0, (candidates.UNIT_WIDTH - lanes) / w, groups.SHEET_LONG_EDGE / h, by_area)
    pg = sheet_draw.SheetPage(window[0], window[1], scale, _px(h * scale))
    strip_im = im.convert("RGB").resize((_px(w * scale), pg.height))
    strip_im = sheet_draw.washed(strip_im, pg, anchored.merged(r for r, _ in labelled))
    out = Image.new("RGB", (BAR_LANE + strip_im.width + right, pg.height), "white")
    out.paste(strip_im, (BAR_LANE, 0))
    draw = ImageDraw.Draw(out)
    x0 = (BAR_LANE - BAR_W) // 2
    draw.rectangle((x0, pg.y(unit[0]), x0 + BAR_W - 1, max(pg.y(unit[0]), pg.bottom(unit[1]))), fill=sheet_draw.RED)
    sheet_draw.removed_labels(draw, pg, labelled, BAR_LANE + strip_im.width)
    return out


def _px(size: float) -> int:
    """Return a scaled size in whole pixels, rounded down (so a bound it was scaled to holds), at least 1."""
    return max(1, int(size + 1e-6))


def _page_images(
    paths: Sequence[Path], u: page_units.PageUnit | page_units.BandUnit
) -> tuple[Image.Image, Image.Image]:
    """Return a page unit's image (its page, or a band's crop) and its page among its neighbours, the unit boxed red.

    The neighbours are drawn PAGE_CONTEXT_H high, lower when that is wider than candidates.UNIT_WIDTH, so the context is
    never scaled after the box is drawn.
    """
    i = u["page"]
    page = images.open_flat(paths[i])[0].convert("RGB")
    unit_im = page.crop(_box(u["box"])) if "box" in u else page  # a band's crop
    near = [(j, images.open_flat(paths[j])[0].convert("RGB")) for j in (i - 1, i, i + 1) if 0 <= j < len(paths)]
    gaps = PAGE_GAP * (len(near) - 1)
    h = _px(min(PAGE_CONTEXT_H, (candidates.UNIT_WIDTH - gaps) / sum(im.width / im.height for _, im in near)))
    near = [(j, im.resize((_px(im.width * h / im.height), h))) for j, im in near]
    context = Image.new("RGB", (sum(im.width for _, im in near) + gaps, h), "white")
    draw = ImageDraw.Draw(context)
    x = 0
    for j, im in near:
        context.paste(im, (x, 0))
        if j == i:
            scale = h / page.height
            x0, y0, x1, y1 = [round(v * scale) for v in u["box"]] if "box" in u else [0, 0, im.width, h]
            draw.rectangle((x + x0, y0, x + max(x0, x1 - 1), max(y0, y1 - 1)), outline=sheet_draw.RED, width=BOX_W)
        x += im.width + PAGE_GAP
    return unit_im, context


def _box(box: Sequence[int]) -> tuple[int, int, int, int]:
    """Return a band's box [x0, y0, x1, y1] as the 4-tuple Pillow's crop takes."""
    x0, y0, x1, y1 = box
    return x0, y0, x1, y1
