"""`junk cut` (SPEC "Junk decisions", decision commands): span sheets, cut and card-side proposals, and their record.

Ported from the bulk converter's optimizer/junk_cut.py, forms `new` and `edge` only: the snap (`_snap`), the anchor
(`_anchor`, at the seeds a decide leaves: strip_units.seeds_left, the old `_seed_list`), the segments (`_segments`),
`_interior_anchor`, the own-chapter check (`_check_cut_version`), `edge`'s half rule per unit (`_propose_edge`) and the
sheets (`_sheet`, `_sheets`, CONTEXT, SHEET_W, its colours); its argument forms from optimizer/junk.py (:2673-2766).

A proposal reads the database once, in 1 read-only snapshot, before its checks. Each chapter whose pixels it reads is
extracted (archive.pages, then stages.junk.write_pages) into <paths.scratch>/junk-cut/<pid>/x/ (removed when the call
ends); a named chapter holding no current scan is scanned again from its pages (OCR on), the scan kept in memory only.
Every checked chapter is decided with and without the proposed decision by strip_units.decide (the 1 owner of the decide
order). A proposal writes only sheets; `--record DIGEST` builds the decision again from the same arguments, before its
transaction, and inside it writes the decision in 1 act when the inputs digest (`state`, then `digest`: the database,
the arguments and the decision built) is unchanged.
"""

import contextlib
import hashlib
import itertools
import json
import math
import os
import shutil
import time
from collections.abc import Collection, Generator, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import archive, config, db, errors, images, page_store, retention, strip, workflow
from manga_pipeline.db import sql
from manga_pipeline.junk import anchored as an
from manga_pipeline.junk import chains, chapter_scan, groups, review, store, strip_units
from manga_pipeline.replies import Json
from manga_pipeline.stages import junk as junk_stage

type Side = Literal["above", "below"]
type Kind = Literal["cut", "keep"]
type Colour = tuple[int, int, int]
type Mark = tuple[int, int, Colour, int]
type Decided = strip_units.StripDecision

NEW_ROW = 10**12  # the proposed decision's id in a decide: the newest of all (the bulk converter's NEW_ROW)
NEW_ADDED = "9999-12-31T23:59:59+00:00"  # its decided_at there (NEW_ADDED)
# D7: the scanned chapters checked besides the named or picked ones: the most recent by chapter id; the rest are
# counted "not checked". Plan phase-10-21 step 2's timing: +0.06 s per checked chapter, 1 proposal within 2 minutes;
# the largest series held 205 scans.
CHECKED_RECENT = 200
CONTEXT = 300  # strip rows shown above and below a proposal sheet's range, or a span sheet's candidate unit
SHEET_W = 400  # proposal sheet width (px)
GREEN, BLUE, ORANGE, RED, GREY = (0, 170, 0), (0, 90, 255), (255, 140, 0), (220, 0, 0), (128, 128, 128)
COLOUR: dict[Kind, Colour] = {"cut": GREEN, "keep": BLUE}
DIGEST_HEX = 16  # hex digits of a digest
SCRATCH = "junk-cut"  # <paths.scratch>/junk-cut/<pid>/: a call's sheets (outside a review) and its pages (x/)
SCRATCH_AGE = 86400  # s: an earlier call's scratch folder older than this is removed at the next call
RULER_W = 64  # span sheet: the row ruler's column (px), a row number every RULER_STEP rows
RULER_STEP = 100
# span sheet: the span labels' column is LABEL_LANES lanes (so near spans' labels do not overlap), then 1 lane of the
# removed ranges' labels; a label's bar lies BAR_X into its lane, its text LABEL_X, LABEL_PAD px left after it
LABEL_LANES = 3
BAR_X, LABEL_X, LABEL_PAD = 4, 9, 4
WASH = 0.5  # span sheet: a removed row's pixels blended this far toward GREY
FONT_PX = 14
TOKEN_PARTS = 3  # CHAPTER:START:END
REGION_ARGS = 2  # --spans CHAPTER head|tail
UNIT_KINDS = ("block", "probe")  # the candidates --spans ID and --edge take (strip units with an edge)
DRAWN = ("cut", "card")  # why a range the proposed decision removes is listed: its cut or its card


class Named(NamedTuple):
    """1 named range CHAPTER:START:END[:keep]: strip rows [start, end) of a chapter, cut or kept."""

    chapter: int
    start: int
    end: int
    kind: Kind


class CutArgs(NamedTuple):
    """A cut proposal's arguments: its ranges, in the order given, credit and --inside."""

    ranges: list[Named]
    credit: bool
    inside: bool


class EdgeArgs(NamedTuple):
    """A card side's arguments: the open block or probe candidate and the side."""

    candidate: int
    side: Side


def parse_range(token: str) -> Named:
    """Return a token CHAPTER:START:END[:keep] (CHAPTER: a chapter id; START < END: strip rows); Refused otherwise."""
    head, _, last = token.rpartition(":")
    kind: Kind = "keep" if last == "keep" else "cut"
    parts = (head if kind == "keep" else token).split(":")
    if len(parts) != TOKEN_PARTS or not all(p.isdecimal() for p in parts):
        msg = f"{token!r}: not CHAPTER:START:END[:keep] (CHAPTER: a chapter id; START, END: strip rows)"
        raise workflow.Refused(msg)
    chapter, start, end = (int(p) for p in parts)
    if start >= end:
        msg = f"{token!r}: START must be below END"
        raise workflow.Refused(msg)
    return Named(chapter, start, end, kind)


def parse_edge(values: Sequence[str]) -> EdgeArgs:
    """Return --edge ID above|below; Refused otherwise."""
    candidate, side = values
    if not candidate.isdecimal() or side not in chains.SIDES:
        msg = f"--edge {' '.join(values)}: not ID above|below"
        raise workflow.Refused(msg)
    return EdgeArgs(int(candidate), "above" if side == "above" else "below")


# ---------------------------------------------------------------- the snapshot


class Scanned(NamedTuple):
    """A chapter as the snapshot read it: its row, its current strip scan (None: none) and its page rows."""

    row: db.Row
    scan: strip_units.StripScan | None
    pages: dict[int, images.MeasuredPage]


class Inputs(NamedTuple):
    """What a proposal reads of the database, in 1 snapshot.

    The series and its key; the active decisions of the series and the global ones, each key parsed (newest id first);
    its watermark keys; the ids of every chapter of it holding a current strip scan, by id; the chapters it may use
    (loaded: the named ones, those waiting on the candidate asked about, and the CHECKED_RECENT + 1 newest scanned),
    the waiting ones' ids in id order; the ids of its chapters not done at discover, validate or junk holding no
    current scan (not named); the database part of the inputs digest (state).
    """

    series: db.Row
    key: str
    parsed: list[tuple[db.Row, store.ParsedKey]]
    known: list[str]
    scanned: list[int]
    loaded: dict[int, Scanned]
    waiting: list[int]
    unscanned: list[int]
    state: Json


@contextlib.contextmanager
def _snapshot(cfg: config.Config) -> Generator[db.Connection]:
    """Yield a connection inside 1 read-only, repeatable-read transaction: every read of 1 proposal (db.read_only)."""
    conn = db.read_only(cfg)
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


def checked_others(scanned: Sequence[int], hand: Collection[int], cap: int) -> tuple[list[int], int]:
    """Return the scanned chapters checked besides those in hand, the cap newest by id, and how many are left over.

    The checked run reads it with cap CHECKED_RECENT; the snapshot loads it with 1 more for a card side, whose chapter
    in hand is picked after the snapshot (D7).
    """
    others = [c for c in reversed(scanned) if c not in hand]
    return others[:cap], len(others[cap:])


def _scanned() -> sql.Composable:
    return workflow.SCANNED.format(c=sql.Identifier("c"))


def state(conn: db.Connection, series: db.Row) -> Json:
    """Return the database part of the inputs digest (plan phase-10-21 D5).

    The ids of the active decisions of the series and the global ones, the series' watermark keys, and every series
    chapter holding a current strip scan, whether or not its download is usable: (id, content digest, scan time).
    """
    key = store.series_key(series)
    ids = conn.execute(
        sql.SQL("SELECT id FROM junk_decision WHERE series_key IN (%s, %s) AND {} ORDER BY id").format(store.ACTIVE),
        (key, store.GLOBAL),
    )
    marks = conn.execute("SELECT key FROM junk_watermark WHERE series_key = %s ORDER BY key", (key,))
    scans = conn.execute(
        sql.SQL("""SELECT c.id, c.content_digest, s.scanned_at FROM chapter c JOIN junk_scan s ON s.chapter_id = c.id
            WHERE c.series_id = %s AND {} ORDER BY c.id""").format(_scanned()),
        (series["id"],),
    )
    return [
        [r[0] for r in ids],
        [r[0] for r in marks],
        [[r["id"], r["content_digest"], str(r["scanned_at"])] for r in scans],
    ]


def digest(args: Json, db_state: Json, chapters: Sequence[tuple[int, str]]) -> str:
    """Return the inputs digest.

    Over the arguments (and the decision built), the database state, and each named or picked chapter's (id, content
    digest).
    """
    value: Json = [args, db_state, sorted([list(c) for c in chapters])]
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:DIGEST_HEX]


def _strip_scan(text: str) -> strip_units.StripScan:
    sc = junk_stage.read_scan(text)
    if sc["layout"] != "strip":
        msg = f"a current scan of layout {sc['layout']} in a strip series"
        raise ValueError(msg)
    return sc


def _inputs(conn: db.Connection, series: db.Row, named: Sequence[int], candidate: int = 0) -> Inputs:
    """Read a proposal's inputs; Refused for a series that is no strip. candidate: the candidate waited on (0: none)."""
    layout = conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series["id"],)).fetchone()
    if layout is None or layout["layout"] != "strip":
        msg = f"series {series['id']} is not a strip (layout {layout and layout['layout']}): a cut is strip-only"
        raise workflow.Refused(msg)
    key = store.series_key(series)
    decisions = conn.execute(
        sql.SQL("SELECT * FROM junk_decision WHERE series_key IN (%s, %s) AND {} ORDER BY id DESC").format(
            store.ACTIVE
        ),
        (key, store.GLOBAL),
    ).fetchall()
    known = [r[0] for r in conn.execute("SELECT key FROM junk_watermark WHERE series_key = %s ORDER BY key", (key,))]
    rows = conn.execute(
        sql.SQL("""SELECT c.id, c.stage = 'junk' AND c.status <> 'done'
                AND s.candidates::jsonb @> to_jsonb(%s::bigint) AS waits
            FROM chapter c JOIN junk_scan s ON s.chapter_id = c.id
            WHERE c.series_id = %s AND {} ORDER BY c.id""").format(_scanned()),
        (candidate, series["id"]),
    ).fetchall()
    scanned = [r["id"] for r in rows]
    waiting = [r["id"] for r in rows if r["waits"]]
    others, _ = checked_others(scanned, named, CHECKED_RECENT + (1 if candidate else 0))
    wanted = {*named, *waiting, *others}
    unscanned = conn.execute(
        sql.SQL("""SELECT c.id FROM chapter c WHERE c.series_id = %s AND c.status <> 'done'
            AND c.stage IN ('discover', 'validate', 'junk') AND NOT {} ORDER BY c.id""").format(_scanned()),
        (series["id"],),
    )
    return Inputs(
        series,
        key,
        [(r, store.parse_key(r["kind"], r["key"])) for r in decisions],
        known,
        scanned,
        _loaded(conn, sorted(wanted), set(scanned)),
        waiting,
        [r[0] for r in unscanned if r[0] not in named],
        state(conn, series),
    )


def _loaded(conn: db.Connection, ids: Sequence[int], scanned: set[int]) -> dict[int, Scanned]:
    """Return the chapters of ids: each row, its current strip scan (a scanned one's) and its page rows."""
    rows = conn.execute("SELECT * FROM chapter WHERE id = ANY(%s)", (list(ids),)).fetchall()
    scans = dict(
        conn.execute(
            "SELECT chapter_id, scan FROM junk_scan WHERE chapter_id = ANY(%s)", (sorted(set(ids) & scanned),)
        ).fetchall()
    )
    return {
        r["id"]: Scanned(r, _strip_scan(scans[r["id"]]) if r["id"] in scans else None, page_store.load(conn, r["id"]))
        for r in rows
    }


def _series_of(conn: db.Connection, chapters: Sequence[int]) -> db.Row:
    """Return the 1 series of the named chapters; Refused for an unknown chapter or chapters of 2 series."""
    rows = conn.execute("SELECT id, series_id FROM chapter WHERE id = ANY(%s)", (list(chapters),)).fetchall()
    missing = sorted(set(chapters) - {r["id"] for r in rows})
    if missing:
        msg = f"no chapter {', '.join(map(str, missing))}"
        raise workflow.Refused(msg)
    series = sorted({r["series_id"] for r in rows})
    if len(series) != 1:
        msg = f"the named chapters are of {len(series)} series ({', '.join(map(str, series))}): a cut is 1 series'"
        raise workflow.Refused(msg)
    return db.one_row(conn, "SELECT * FROM series WHERE id = %s", (series[0],))


def _open_candidate(conn: db.Connection, candidate: int, review_id: str | None) -> db.Row:
    """Return an open block or probe candidate's row (review.get_open); Refused otherwise."""
    try:
        row = review.get_open(conn, candidate, review_id)
    except review.Refused as e:
        raise workflow.Refused(str(e)) from None
    if row["kind"] not in UNIT_KINDS:
        msg = (
            f"junk candidate {candidate} is a {row['kind']}, not a block or probe: draw its chapter's head or tail"
            " (junk cut --spans CHAPTER head|tail) and name ranges"
        )
        raise workflow.Refused(msg)
    return row


def _candidate_series(conn: db.Connection, row: db.Row) -> db.Row:
    series = conn.execute("SELECT * FROM series WHERE rules_key = %s", (row["series_key"],)).fetchone()
    if series is None:
        msg = f"junk candidate {row['id']}: no series has the key {row['series_key']!r}"
        raise workflow.Refused(msg)
    return series


# ---------------------------------------------------------------- pages on local disk


def _download(cfg: config.Config, row: db.Row) -> Path | None:
    """Return a chapter's download when this machine can use it, else None."""
    stored = row["download_path"]
    if not stored or workflow.unusable_here(stored):
        return None
    path = config.to_local(cfg, stored)
    return path if errors.exists(path) else None


def _retained(cfg: config.Config, row: db.Row) -> Path | None:
    """Return a chapter's download as the retention folder keeps it (retention.find), else None."""
    stored = row["download_path"]
    if not stored or workflow.unusable_here(stored):
        return None
    return retention.find(cfg, config.to_local(cfg, stored), row["content_digest"])


class Strip(NamedTuple):
    """A chapter's strip in hand: its row, its scan, its layout and render(a, b) of strip rows [a, b)."""

    row: db.Row
    scan: strip_units.StripScan
    layout: an.Layout
    render: an.Render


class Hand(NamedTuple):
    """A named or picked chapter's strip and its decide without the proposal (read once, used by every step)."""

    strip: Strip
    before: Decided


def _pages_folder(work: Path, chapter: int) -> Path:
    return work / "x" / str(chapter)


def _read(cbz: Path) -> list[tuple[str, bytes]] | None:
    """Return a CBZ's pages (archive.pages), or None when the archive cannot be read.

    Only the zip's open and read are caught (archive.UNREADABLE, and OSError but a share's: a dropped share or a
    SHARE_ERRNOS error); measuring the pages, writing them and decoding them raise.
    """
    try:
        return archive.pages(cbz)
    except (*archive.UNREADABLE, OSError) as e:
        if isinstance(e, OSError) and (errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS):
            raise
        return None


def _extracted(ch: Scanned, cbz: Path, pages: Sequence[tuple[str, bytes]], work: Path) -> chapter_scan.Extracted:
    """Write a chapter's pages read from cbz under work (stages.junk.write_pages), measured with its page rows."""
    measured = images.measure_pages(cbz, ch.pages)
    paths, names, sizes, _ = junk_stage.write_pages(pages, measured, _pages_folder(work, ch.row["id"]))
    return chapter_scan.Extracted(paths, names, sizes)


def _in_hand(cfg: config.Config, ch: Scanned, pages: chapter_scan.Extracted, decisions: store.Decisions) -> Strip:
    """Return a chapter's strip from its extracted pages: its current scan, else the strip scanned again (in memory)."""
    sc = ch.scan if ch.scan is not None else chapter_scan.scan_strip(pages, decisions, cfg["junk"]["ocr_device"])
    layout = an.Layout(sc["height"], sc["width"], sc["spans"], sc["gaps"])
    return Strip(ch.row, sc, layout, chapter_scan.renderer(pages))


def _named_strip(cfg: config.Config, inp: Inputs, chapter: int, work: Path) -> Strip:
    """Return a named chapter's strip: from its download, else its retention original.

    Refused when it has no content digest, neither is on this machine, or its pages cannot be read.
    """
    ch = inp.loaded[chapter]
    if not ch.row["content_digest"]:
        msg = f"chapter {chapter} has no content digest: discovery sets it before junk"
        raise workflow.Refused(msg)
    cbz = _download(cfg, ch.row) or _retained(cfg, ch.row)
    if cbz is None:
        msg = f"no pages for chapter {chapter}: neither its download nor a retention original is on this machine"
        raise workflow.Refused(msg)
    pages = _read(cbz)
    if pages is None:
        msg = f"chapter {chapter}: its pages cannot be read ({cbz.name} is no readable archive)"
        raise workflow.Refused(msg)
    return _in_hand(cfg, ch, _extracted(ch, cbz, pages, work), _rules(inp).decisions)


def _waiting_strips(cfg: config.Config, inp: Inputs, work: Path) -> Generator[Strip]:
    """Yield, in id order, the strip of each chapter waiting on the candidate (at junk, not done, current strip scan).

    Only those with a content digest and a usable, readable download; the others are passed over.
    """
    decisions = _rules(inp).decisions
    for chapter in inp.waiting:
        ch = inp.loaded[chapter]
        cbz = _download(cfg, ch.row) if ch.row["content_digest"] else None
        pages = _read(cbz) if cbz is not None else None
        if cbz is None or pages is None:
            continue
        yield _in_hand(cfg, ch, _extracted(ch, cbz, pages, work), decisions)


@contextlib.contextmanager
def _workspace(cfg: config.Config) -> Generator[Path]:
    """Yield this call's scratch folder; earlier calls' over SCRATCH_AGE old go first, its pages (x/) at its end."""
    root = config.abs_path(cfg["paths"]["scratch"]) / SCRATCH
    own = root / str(os.getpid())
    if root.exists():
        for old in root.iterdir():
            try:
                aged = old != own and time.time() - old.stat().st_mtime > SCRATCH_AGE
            except OSError:  # another junk cut removed it meanwhile (Windows: PermissionError while delete-pending)
                continue
            if aged:
                shutil.rmtree(old, ignore_errors=True)
    own.mkdir(parents=True, exist_ok=True)
    try:
        yield own
    finally:
        shutil.rmtree(own / "x", ignore_errors=True)


def _sheet_folder(cfg: config.Config, review_id: str | None, work: Path) -> Path:
    """Return where sheets go: <paths.candidates>/review-<id>-cut/ in a review (Claude reads there), else work."""
    dest = groups.folder(cfg, f"{review_id}-cut") if review_id else work
    dest.mkdir(parents=True, exist_ok=True)
    return dest


# ---------------------------------------------------------------- deciding with and without


class Rules(NamedTuple):
    """What a decide applies: the decisions, the known watermark signatures and the cut decisions, oldest first."""

    decisions: store.Decisions
    known: list[images.Signature]
    cuts: list[db.Row]


def _rules(inp: Inputs, proposed: store.Decision | None = None) -> Rules:
    """Return today's rules, or (proposed) those with the proposed decision the newest of all (id NEW_ROW)."""
    parsed = inp.parsed
    if proposed is not None:
        row = db.row(id=NEW_ROW, superseded_by=None, retired_by=None, written_by=None, **proposed)
        parsed = [(row, store.parse_key(row["kind"], row["key"])), *parsed]
    decisions = store.Decisions.of_rows(inp.key, parsed)
    return Rules(decisions, [images.sig_from_str(k) for k in inp.known], decisions.cuts())


def _decide(s: Strip, rules: Rules) -> Decided:
    def decide_unit(kind: str, text: str) -> db.Row | None:
        return rules.decisions.decide(kind, store.parse_key(kind, text))

    return strip_units.decide(s.scan, decide_unit, rules.known, rules.cuts, s.render)


def _decider(review_id: str | None) -> str:
    return "claude" if review_id else "human"


def _cut_decision(inp: Inputs, text: str, decider: str, *, credit: bool) -> store.Decision:
    """Return a cut decision on chain text, its key the chain's key span, as store.write writes it.

    decided_at is NEW_ADDED until it is recorded.
    """
    key = chains.key_span(chains.parse(text))
    chains.check_cut(key, text)
    return {
        "series_key": inp.key,
        "kind": "cut",
        "key": key,
        "verdict": "junk",
        "decider": decider,
        "decided_at": NEW_ADDED,
        "source": "junk cut",
        "chain": text,
        "credit": credit,
    }


def _edge_decision(cand: db.Row, side: Side, refs: list[Json], decider: str) -> store.Decision:
    """Return an open candidate's junk decision (review.decision) carrying an edge version on side.

    decided_at is NEW_ADDED until it is recorded.
    """
    value: Json = {side: [{"segments": [{"kind": "edge"}], "ref": refs, "src": None}]}
    text = chains.text(value)
    chains.check_unit(text)
    out = review.decision(cand, "junk", decider, NEW_ADDED)
    out["chain"] = text
    return out


def _mine(d: Decided, why: Sequence[str] = DRAWN) -> list[strip.Rows]:
    """Return the ranges the proposed decision removes for these reasons (its cut or card), sorted."""
    return sorted((r["start"], r["end"]) for r in d["removed"] if r.get("decision") == NEW_ROW and r["why"] in why)


def _lost(s: Strip, before: Decided, after: Decided) -> list[strip.Rows]:
    """Return the rows removed without the proposal and kept with it: each part longer than 1 gutter.

    A shorter part is the join rule keeping its gutter elsewhere.
    """
    taken = [(r["start"], r["end"]) for r in after["removed"]]
    g = strip_units.gutter(s.layout.width)
    parts = [p for r in before["removed"] for p in an.uncovered(r["start"], r["end"], taken)]
    return [(a, b) for a, b in an.merged(parts) if b - a > g]


def _shown(text: str) -> str:
    return text.replace(f"junk decision {NEW_ROW}", "the proposed decision")


# ---------------------------------------------------------------- a cut version from named ranges


class Piece(NamedTuple):
    """1 segment of a cut version: kind, strip rows [start, end), tile hashes, exact (its far edge inside its span)."""

    kind: Kind
    start: int
    end: int
    tiles: str
    exact: bool


class Version(NamedTuple):
    """1 version built from a chapter's named ranges.

    Its chapter, its ranges as snapped, side, anchor seed, segments and JSON value.
    """

    chapter: int
    ranges: list[Named]
    side: Side
    anchor: an.Seed
    pieces: list[Piece]
    value: Json


def _snap(layout: an.Layout, n: Named, gutter: int, *, inside: bool) -> strip.Rows:
    """Return a named range's ends snapped to the nearest span start and end within 1 gutter; Refused otherwise.

    inside (--inside): an end with no boundary within the gutter that lies strictly inside a span stays as named (an
    exact boundary).
    """
    starts, ends = [a for a, _ in layout.spans], [b for _, b in layout.spans]
    a = min(starts, key=lambda x: abs(x - n.start), default=None)
    b = min(ends, key=lambda x: abs(x - n.end), default=None)

    def within(v: int) -> bool:
        return any(x < v < y for x, y in layout.spans)

    if inside and (a is None or abs(a - n.start) > gutter) and within(n.start):
        a = n.start
    if inside and (b is None or abs(b - n.end) > gutter) and within(n.end):
        b = n.end
    if a is None or b is None or abs(a - n.start) > gutter or abs(b - n.end) > gutter or b <= a:

        def near(xs: list[int], v: int) -> list[int]:
            return sorted(xs, key=lambda x: abs(x - v))[:3]

        msg = (
            f"chapter {n.chapter} range {n.start}:{n.end}: its ends lie at no span boundary within {gutter} px"
            f" (nearest span starts {near(starts, n.start)}, ends {near(ends, n.end)})"
            + ("" if inside else "; --inside makes an end inside a span an exact boundary")
        )
        raise workflow.Refused(msg)
    return a, b


def _anchor(s: Strip, top: int, bottom: int, seeds: Sequence[an.Seed]) -> tuple[Side, an.Seed]:
    """Return (side, seed) of ranges spanning [top, bottom): the end lying at a seed; Refused when neither does.

    Both ends at one: the seed ranking first in SEED_RANK, then the upper end.
    """
    at: list[tuple[int, int, Side, an.Seed]] = []
    for sd in seeds:
        sp = s.layout.next_span(sd["pos"], sd["side"])
        if sp is None:
            continue
        if sd["side"] == "below" and sd["pos"] <= top and sp[0] == top:
            at.append((an.SEED_RANK[sd["kind"]], 0, "below", sd))
        if sd["side"] == "above" and sd["pos"] >= bottom and sp[1] == bottom:
            at.append((an.SEED_RANK[sd["kind"]], 1, "above", sd))
    if not at:
        near = sorted(seeds, key=lambda sd: min(abs(sd["pos"] - top), abs(sd["pos"] - bottom)))[:4]
        msg = f"chapter {s.row['id']} rows {top}:{bottom}: neither end lies at a seed (nearest seeds: " + (
            ", ".join(f"{sd['kind']} {sd['side']} at {sd['pos']}" for sd in near) + ")"
        )
        raise workflow.Refused(msg)
    _, _, side, sd = min(at, key=lambda t: t[:2])
    return side, sd


def _segments(s: Strip, ranges: Sequence[Named], side: Side, span_sig: an.SpanSig) -> list[Piece]:
    """Return the segments of a chapter's snapped ranges outward from the anchor; Refused for what cannot be one.

    1 segment per span piece of a range (a uniform or unsignable piece is refused); the spans between 2 ranges must
    all be named; a piece whose far edge lies inside its span is exact and must be followed by the rest of that span.
    """
    layout, chapter = s.layout, s.row["id"]
    out: list[Piece] = []
    for n, nxt in zip(ranges, [*ranges[1:], None], strict=True):
        if nxt is not None and nxt.start > n.end and not layout.blank(n.end, nxt.start):
            msg = f"chapter {chapter}: a span between rows {n.end} and {nxt.start} is not named (name it :keep)"
            raise workflow.Refused(msg)
        for x, y in [sp for sp in layout.spans if sp[0] < n.end and n.start < sp[1]]:
            a, b = max(n.start, x), min(n.end, y)
            sig = span_sig(a, b)
            if sig is None:
                msg = (
                    f"chapter {chapter} rows {a}:{b}: a uniform or unsignable span cannot be a segment (it would match"
                    " any blank or flat rows): name ranges that leave it out"
                )
                raise workflow.Refused(msg)
            out.append(Piece(n.kind, a, b, sig.split("|")[0], b < y if side == "below" else a > x))
    walk = out if side == "below" else out[::-1]
    for p, nxt in zip(walk, [*walk[1:], None], strict=True):
        if p.exact and (nxt is None or nxt.exact):
            sp = next(sp for sp in layout.spans if sp[0] <= p.start and p.end <= sp[1])
            rest = (p.end, sp[1]) if side == "below" else (sp[0], p.start)
            how = (
                f"followed by the rest of its span as 1 range: name {chapter}:{rest[0]}:{rest[1]}:keep (or as a cut"
                " range)"
                if nxt is None
                else "followed by the rest of its span as 1 range, not split again"
            )
            msg = (
                f"chapter {chapter} rows {p.start}:{p.end}: an exact piece (a boundary inside span {sp[0]}-{sp[1]})"
                f" must be {how}, so the span is verified whole"
            )
            raise workflow.Refused(msg)
    return walk


def _interior_anchor(s: Strip, sd: an.Seed, pieces: Sequence[Piece]) -> None:
    """Refuse an anchor strictly inside a span unless the first segment is exact (it would match only there)."""
    inside = [sp for sp in s.layout.spans if sp[0] < sd["pos"] < sp[1]]
    if inside and not pieces[0].exact:
        x, y = inside[0]
        msg = (
            f"chapter {s.row['id']}: the anchor ({sd['kind']} seed {sd['side']} at {sd['pos']}) lies inside span"
            f" {x}-{y} (the edge of another removal there): only an exact piece (--inside, followed by the rest of the"
            " span) may start there; else name ranges anchored at a span boundary"
        )
        raise workflow.Refused(msg)


def _version(h: Hand, ranges: Sequence[Named], key: str, *, inside: bool) -> Version:
    """Return the version of a chapter's named ranges: snapped, anchored, its segments, src and ref (plan D2)."""
    s = h.strip
    layout = s.layout
    for n in ranges:
        if n.end > layout.height:
            msg = f"chapter {n.chapter} range {n.start}:{n.end}: past the strip end ({layout.height})"
            raise workflow.Refused(msg)
    g = strip_units.gutter(layout.width)
    snapped = sorted(Named(n.chapter, *_snap(layout, n, g, inside=inside), n.kind) for n in ranges)
    for a, b in itertools.pairwise(snapped):
        if b.start < a.end:
            msg = f"chapter {a.chapter}: ranges {a.start}:{a.end} and {b.start}:{b.end} overlap"
            raise workflow.Refused(msg)
    side, sd = _anchor(s, snapped[0].start, snapped[-1].end, h.before["seeds"])
    pieces = _segments(s, snapped, side, an.span_sigs(s.render))
    _interior_anchor(s, sd, pieces)
    segments: list[Json] = [
        {"kind": p.kind, "h": round((p.end - p.start) / layout.width, chains.DECIMALS), "tiles": p.tiles}
        | ({"exact": True} if p.exact else {})
        for p in pieces
    ]
    value: Json = {
        "segments": segments,
        "ref": [[key, str(s.row["id"]), sd["pos"]]],
        "src": {"file": PurePosixPath(s.row["download_path"]).name, "sha256": s.row["content_digest"]},
    }
    return Version(s.row["id"], snapped, side, sd, pieces, value)


def _already_removed(h: Hand, v: Version) -> list[str]:
    """Return a clause per named cut range (as snapped) the decide without the proposal already removes.

    Already removed: at most 1 gutter of its rows lies outside the merged removed ranges.
    """
    removed = an.merged((r["start"], r["end"]) for r in h.before["removed"])
    g = strip_units.gutter(h.strip.layout.width)
    out: list[str] = []
    for n in (n for n in v.ranges if n.kind == "cut" and an.hits(n.start, n.end, removed)):
        if sum(b - a for a, b in an.uncovered(n.start, n.end, removed)) <= g:
            by = ", ".join(f"{a}:{b}" for a, b in removed if an.hits(a, b, [(n.start, n.end)]))
            out.append(f"rows {n.start}:{n.end} are already removed (removed {by})")
    return out


def _own_check(h: Hand, v: Version, inp: Inputs) -> None:
    """Refuse a version that matches nowhere in its own chapter over its named cut pieces (as snapped).

    The version is tried alone, overlap extension and its cap included. The refusal names the named cut ranges already
    removed (_already_removed).
    """
    alone = _cut_decision(inp, chains.text({v.side: [v.value]}), "human", credit=False)
    got = _decide(h.strip, _rules(inp, alone))
    cuts = [(p.start, p.end) for p in v.pieces if p.kind == "cut"]
    if not any(an.hits(a, b, cuts) for a, b in _mine(got)):
        new = [_shown(t) for t in got["misses"] if t not in h.before["misses"]]
        msg = f"chapter {h.strip.row['id']}: the version matches nowhere in its own chapter over its named pieces" + (
            f" ({'; '.join(new)})" if new else ""
        )
        raise workflow.Refused("; ".join([msg, *_already_removed(h, v)]))


# ---------------------------------------------------------------- plans


class Plan(NamedTuple):
    """A proposal built.

    Its inputs, its arguments as the digest holds them (the decision's key, chain and credit included), the proposed
    decision, the named or picked chapters in hand, the rows its sheets draw, its lines so far, and for a card side
    the candidate.
    """

    inputs: Inputs
    args: Json
    decision: store.Decision
    hand: list[Hand]
    pieces: list[Named]
    lines: list[str]
    candidate: db.Row | None


def _digest_args(form: Json, decision: store.Decision) -> Json:
    """Return the arguments the digest holds: the form's, and the decision built (key, chain, credit)."""
    return [form, decision["key"], decision.get("chain"), decision.get("credit", False)]


def _by_chapter(ranges: Sequence[Named]) -> dict[int, list[Named]]:
    out: dict[int, list[Named]] = {}
    for n in ranges:
        out.setdefault(n.chapter, []).append(n)
    return out


def _hand(s: Strip, inp: Inputs) -> Hand:
    return Hand(s, _decide(s, _rules(inp)))


def _cut_plan(cfg: config.Config, args: CutArgs, review_id: str | None, work: Path) -> Plan:
    """Build a cut proposal: 1 version per named chapter, all on 1 side; its key the first version's key span."""
    if all(n.kind == "keep" for n in args.ranges):
        msg = "a cut names at least 1 cut range (CHAPTER:START:END without :keep)"
        raise workflow.Refused(msg)
    by = _by_chapter(args.ranges)
    with _snapshot(cfg) as conn:
        inp = _inputs(conn, _series_of(conn, list(by)), list(by))
    hand = [_hand(_named_strip(cfg, inp, c, work), inp) for c in by]
    versions = [_version(h, by[h.strip.row["id"]], inp.key, inside=args.inside) for h in hand]
    side = versions[0].side
    for v in versions[1:]:
        if v.side != side:
            msg = f"chapter {v.chapter}'s ranges anchor {v.side}, chapter {versions[0].chapter}'s {side}: 1 side only"
            raise workflow.Refused(msg)
    decision = _cut_decision(
        inp, chains.text({side: [v.value for v in versions]}), _decider(review_id), credit=args.credit
    )
    taken = next((r for r, _ in inp.parsed if r["kind"] == "cut" and r["key"] == decision["key"]), None)
    if taken is not None:
        msg = f"key span {decision['key']} is taken: covered by decision {taken['id']} (1 active cut per key span)"
        raise workflow.Refused(msg)
    for h, v in zip(hand, versions, strict=True):
        _own_check(h, v, inp)
    lines = [f"cut of series {inp.key}: side {side}, key span {decision['key']}" + (", credit" if args.credit else "")]
    lines += [
        f"version {i}: chapter {v.chapter}, from the {v.anchor['kind']} seed at row {v.anchor['pos']}: "
        + ", ".join(f"{p.kind} {p.start}:{p.end}" + (" exact" if p.exact else "") for p in v.pieces)
        for i, v in enumerate(versions, 1)
    ]
    if args.credit or any(
        h.strip.layout.region(v.anchor["pos"], strip_units.EDGE_PX) == "tail"
        for h, v in zip(hand, versions, strict=True)
    ):
        lines.append(
            "warning: the cut acts in a tail region (or carries credit): the tail pass decides the pieces it uncovers"
            " (a credit cut starts the credit block); check them on the sheets"
        )
    pieces = [Named(v.chapter, p.start, p.end, p.kind) for v in versions for p in v.pieces]
    form: Json = ["cut", [list(n) for n in args.ranges], args.credit, args.inside]
    return Plan(inp, _digest_args(form, decision), decision, hand, pieces, lines, None)


def _in_half(side: Side, anchor: int, height: int) -> bool:
    """Return the bulk converter's edge rule for 1 unit (junk_cut._propose_edge): below 2 x anchor >= H, above <= H."""
    return 2 * anchor >= height if side == "below" else 2 * anchor <= height


def _edge_plan(cfg: config.Config, args: EdgeArgs, review_id: str | None, work: Path) -> Plan:
    """Build a card side: the open candidate's junk decision carrying an edge version on the side.

    From the lowest-id chapter waiting on the candidate (current strip scan, content digest, usable download) where
    the side removes rows; its refs the units there in the half toward that edge. Refused when none.
    """
    with _snapshot(cfg) as conn:
        cand = _open_candidate(conn, args.candidate, review_id)
        inp = _inputs(conn, _candidate_series(conn, cand), [], args.candidate)
    decider, why = _decider(review_id), list[str]()
    trial = _rules(inp, _edge_decision(cand, args.side, [[inp.key, "0", 0]], decider))  # ref: matching reads none
    for s in _waiting_strips(cfg, inp, work):
        got = _decide(s, trial)
        units, card = _mine(got, ("match",)), _mine(got, ("card",))
        anchors = [(u, u[1] if args.side == "below" else u[0]) for u in units]
        refs: list[Json] = [
            [inp.key, str(s.row["id"]), p] for _, p in anchors if _in_half(args.side, p, s.layout.height)
        ]
        if card and refs:
            lines = [
                f"card side {args.side} of candidate {cand['id']} ({cand['kind']}) of series {inp.key}: an edge from"
                f" chapter {s.row['id']}'s unit at "
                + ", ".join(f"{a}:{b}" for (a, b), p in anchors if _in_half(args.side, p, s.layout.height))
            ]
            decision = _edge_decision(cand, args.side, refs, decider)
            form: Json = ["edge", cand["id"], args.side]
            pieces = [Named(s.row["id"], a, b, "cut") for a, b in card]
            return Plan(inp, _digest_args(form, decision), decision, [_hand(s, inp)], pieces, lines, cand)
        shutil.rmtree(_pages_folder(work, s.row["id"]), ignore_errors=True)
        found = ", ".join(f"{a}:{b}" for a, b in units) or "none"
        why.append(f"chapter {s.row['id']}: its unit at {found} of {s.layout.height} rows, the side removes nothing")
    msg = (
        f"junk candidate {args.candidate}: no chapter waiting on it has its unit in the {args.side} half with rows its"
        f" side removes ({'; '.join(why) or 'none waits with a current strip scan and a usable download'})"
    )
    raise workflow.Refused(msg)


# ---------------------------------------------------------------- the checked run


class Outcome(NamedTuple):
    """1 checked chapter's outcome: taken, no cut or a miss (its texts), and the ranges it also removes."""

    chapter: int
    misses: list[str]
    taken: bool
    also: list[strip.Rows]


class Checked(NamedTuple):
    """The checked run.

    Each checked chapter's outcome, the named or picked chapters' decides with the proposal (for their sheets), the
    "also removes" sheets of every checked chapter, and the line counting the chapters not checked.
    """

    outcomes: list[Outcome]
    after: dict[int, Decided]
    sheets: list[Path]
    unchecked: str


def _outcome(p: Plan, s: Strip, before: Decided, after: Decided) -> Outcome:
    """Return a checked chapter's outcome; Refused when the proposal changes it and its kept check fails.

    A miss: a miss text the decide with the proposal has and the one without lacks, or rows removed without it that it
    keeps (_lost).
    """
    chapter = s.row["id"]
    if after["removed"] != before["removed"] and after["kept_problem"]:
        msg = f"chapter {chapter}: {after['kept_problem']}"
        raise workflow.Refused(msg)
    misses = [_shown(t) for t in after["misses"] if t not in before["misses"]]
    lost = _lost(s, before, after)
    if lost:
        rows = ", ".join(f"{a}:{b}" for a, b in lost)
        misses.append(f"no longer removes rows {rows}, removed without it (it beats a chain at its seed)")
    named = [(n.start, n.end) for n in p.pieces if n.chapter == chapter]
    mine = _mine(after)
    return Outcome(chapter, misses, bool(mine), [r for r in mine if not an.hits(*r, named)])


def _also_sheets(s: Strip, before: Decided, o: Outcome, dest: Path) -> list[Path]:
    """Write a sheet per range a checked chapter's decide with the proposal also removes; return their paths.

    The range green on the left, what is removed without the proposal orange on the right.
    """
    removed: list[Mark] = [(r["start"], r["end"], ORANGE, 1) for r in before["removed"]]
    out: list[Path] = []
    for x, y in o.also:
        path = dest / f"also-{o.chapter}-{x}-{y}.png"
        images.save_png(_sheet(s, (x, y), [*removed, (x, y, GREEN, 0)]), path)
        out.append(path)
    return out


def _check(cfg: config.Config, p: Plan, work: Path, dest: Path) -> Checked:
    """Decide every checked chapter with and without the proposed decision (plan D0d, D7).

    Each one's "also removes" sheets are drawn while its pages are in hand.
    """
    today, proposed = _rules(p.inputs), _rules(p.inputs, p.decision)
    outcomes: list[Outcome] = []
    after: dict[int, Decided] = {}
    sheets: list[Path] = []
    for h in p.hand:
        got = _decide(h.strip, proposed)
        after[h.strip.row["id"]] = got
        outcomes.append(_outcome(p, h.strip, h.before, got))
        sheets += _also_sheets(h.strip, h.before, outcomes[-1], dest)
    others, beyond = checked_others(p.inputs.scanned, after, CHECKED_RECENT)
    unusable = 0
    for chapter in others:
        ch = p.inputs.loaded[chapter]
        cbz = _download(cfg, ch.row)
        pages = _read(cbz) if cbz is not None else None
        if cbz is None or pages is None:
            unusable += 1  # missing, or an archive it cannot read: no usable download here
            continue
        s = _in_hand(cfg, ch, _extracted(ch, cbz, pages, work), today.decisions)
        before = _decide(s, today)
        outcomes.append(_outcome(p, s, before, _decide(s, proposed)))
        sheets += _also_sheets(s, before, outcomes[-1], dest)
        shutil.rmtree(_pages_folder(work, chapter), ignore_errors=True)
    unscanned = len(p.inputs.unscanned)
    total = unscanned + unusable + beyond
    unchecked = (
        f"{total} not checked: {unscanned} without a current scan, {unusable} without a usable download,"
        f" {beyond} beyond the {CHECKED_RECENT} most recent scanned"
    )
    return Checked(sorted(outcomes, key=lambda o: o.chapter), after, sheets, unchecked)


# ---------------------------------------------------------------- sheets


def _font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=FONT_PX)


def _sheet(s: Strip, rows: strip.Rows, marks: Sequence[Mark]) -> Image.Image:
    """Return strip rows [a, b) +- CONTEXT at SHEET_W wide, marks outlined.

    A mark is (start, end, colour, half): half 0 on the left (what is proposed), 1 on the right (what is removed).
    """
    a, b = rows
    lo, hi = max(0, a - CONTEXT), min(s.layout.height, b + CONTEXT)
    im = s.render(lo, hi)
    scale = SHEET_W / im.width
    im = im.resize((SHEET_W, max(1, round(im.height * scale)))).convert("RGB")
    draw = ImageDraw.Draw(im)
    for x, y, colour, half in marks:
        if y > lo and x < hi:
            x0, x1 = (0, SHEET_W // 2 - 1) if half == 0 else (SHEET_W // 2, SHEET_W - 1)
            top, bottom = round((max(x, lo) - lo) * scale), round((min(y, hi) - lo) * scale)
            draw.rectangle((x0, top, x1, bottom), outline=colour, width=2)
    return im


def _named_sheets(p: Plan, checked: Checked, dest: Path) -> list[Path]:
    """Write a sheet per named range (a card side: per card range) of the chapters in hand; return their paths.

    Cut green, keep blue; on the right what is removed without the proposal orange, and its own rows beyond the named
    ones red.
    """
    out: list[Path] = []
    for h in p.hand:
        chapter = h.strip.row["id"]
        removed: list[Mark] = [(r["start"], r["end"], ORANGE, 1) for r in h.before["removed"]]
        mine = _mine(checked.after[chapter])
        for n in (n for n in p.pieces if n.chapter == chapter):
            ext: list[Mark] = [
                (x, y, RED, 1) for x, y in mine if an.hits(x, y, [(n.start, n.end)]) and (x < n.start or y > n.end)
            ]
            path = dest / f"cut-{chapter}-{n.start}-{n.end}.png"
            images.save_png(
                _sheet(h.strip, (n.start, n.end), [*removed, *ext, (n.start, n.end, COLOUR[n.kind], 0)]), path
            )
            out.append(path)
    return out


def lane_widths(height: int) -> tuple[int, int]:
    """Return a span sheet's label lane width and its removed lane's, for a strip `height` rows long.

    Each fits its longest label (START:END, `removed START:END`) after LABEL_X, with LABEL_PAD px to spare.
    """
    font = _font()

    def fit(label: str) -> int:
        return LABEL_X + math.ceil(font.getlength(label)) + LABEL_PAD

    return fit(f"{height}:{height}"), fit(f"removed {height}:{height}")


class SheetPage(NamedTuple):
    """1 span sheet: strip rows [a, b) drawn `scale` times, `height` px tall."""

    a: int
    b: int
    scale: float
    height: int

    def y(self, row: int) -> int:
        """Return a strip row's y on the sheet (its last row for a row past the end)."""
        return min(self.height - 1, round((row - self.a) * self.scale))

    def shows(self, rows: strip.Rows) -> bool:
        """Return whether strip rows [x, y) lie partly on this sheet."""
        return rows[1] > self.a and rows[0] < self.b


def _washed(im: Image.Image, pg: SheetPage, removed: Sequence[strip.Rows]) -> Image.Image:
    """Return a sheet's strip image with the removed rows blended WASH toward GREY."""
    for x, y in (r for r in removed if pg.shows(r)):
        top, bottom = round((max(x, pg.a) - pg.a) * pg.scale), round((min(y, pg.b) - pg.a) * pg.scale)
        if bottom > top:
            box = (0, top, im.width, bottom)
            part = im.crop(box)
            im.paste(Image.blend(part, Image.new("RGB", part.size, GREY), WASH), box[:2])
    return im


def _ruler(draw: ImageDraw.ImageDraw, pg: SheetPage, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
    for r in range(-(-pg.a // RULER_STEP) * RULER_STEP, pg.b, RULER_STEP):
        draw.line((RULER_W - 12, pg.y(r), RULER_W - 1, pg.y(r)), fill="black")
        draw.text((2, max(0, pg.y(r) - FONT_PX // 2)), str(r), fill="black", font=font)


def _span_labels(draw: ImageDraw.ImageDraw, pg: SheetPage, spans: Sequence[strip.Rows], left: int, lane: int) -> None:
    """Label each span on the sheet START:END in blue, in lanes from x `left`, `lane` px wide."""
    font = _font()
    for k, (x0, x1) in enumerate(spans):
        if not pg.shows((x0, x1)):
            continue
        x = left + (k % LABEL_LANES) * lane
        top = pg.y(max(x0, pg.a))
        draw.line((x + BAR_X, top, x + BAR_X, max(top, pg.y(min(x1, pg.b)) - 1)), fill=BLUE, width=3)
        draw.text((x + LABEL_X, max(0, top)), f"{x0}:{x1}", fill=BLUE, font=font)


def _removed_labels(draw: ImageDraw.ImageDraw, pg: SheetPage, removed: Sequence[strip.Rows], left: int) -> None:
    """Label each removed range on the sheet `removed START:END` in grey, in its lane at x `left`.

    A label starts below the one before it, so the labels of near ranges do not overlap.
    """
    font, below = _font(), 0
    for x0, x1 in (r for r in removed if pg.shows(r)):
        top = pg.y(max(x0, pg.a))
        draw.line((left + BAR_X, top, left + BAR_X, max(top, pg.y(min(x1, pg.b)) - 1)), fill=GREY, width=3)
        at = max(top, below)
        draw.text((left + LABEL_X, at), f"removed {x0}:{x1}", fill=GREY, font=font)
        below = at + FONT_PX + 2


def _span_sheets(s: Strip, window: strip.Rows, decided: Decided, dest: Path, stem: str) -> list[Path]:
    """Write the span sheets of strip rows `window` as `decided` (the decide without a proposal) leaves it.

    Each at most groups.SHEET_LONG_EDGE long and groups.SHEET_PIXELS, with a row ruler, every span labelled START:END
    (END exclusive: the range to name), the rows removed (merged) washed grey and labelled `removed START:END`, the
    seeds in the window marked in red (one at the window's end on its last row). Return their paths.
    """
    lo, hi = window
    lane, removed_lane = lane_widths(s.layout.height)
    labels = LABEL_LANES * lane + removed_lane
    scale = min(1.0, (groups.SHEET_LONG_EDGE - RULER_W - labels) / s.layout.width)
    img_w = max(1, round(s.layout.width * scale))
    sheet_w = RULER_W + img_w + labels
    per = max(1, int(min(groups.SHEET_LONG_EDGE, groups.SHEET_PIXELS // sheet_w) / scale))
    seeds = _in_window(decided["seeds"], window)
    removed = an.merged((r["start"], r["end"]) for r in decided["removed"])
    font = _font()
    out: list[Path] = []
    starts = list(range(lo, hi, per))
    for n, a in enumerate(starts, 1):
        b = min(hi, a + per)
        pg = SheetPage(a, b, scale, max(1, round((b - a) * scale)))
        sheet = Image.new("RGB", (sheet_w, pg.height), "white")
        sheet.paste(_washed(s.render(a, b).convert("RGB").resize((img_w, pg.height)), pg, removed), (RULER_W, 0))
        draw = ImageDraw.Draw(sheet)
        _ruler(draw, pg, font)
        _span_labels(draw, pg, s.layout.spans, RULER_W + img_w, lane)
        _removed_labels(draw, pg, removed, RULER_W + img_w + LABEL_LANES * lane)
        last = n == len(starts)
        for sd in (sd for sd in seeds if a <= sd["pos"] < b or (last and sd["pos"] == b)):
            draw.line((RULER_W, pg.y(sd["pos"]), RULER_W + img_w - 1, pg.y(sd["pos"])), fill=RED, width=2)
            label = f"seed {sd['side']} {sd['pos']} ({sd['kind']})"
            draw.text((RULER_W + 4, max(0, pg.y(sd["pos"]) - FONT_PX - 2)), label, fill=RED, font=font)
        path = dest / f"{stem}-{n}.png"
        images.save_png(sheet, path)
        out.append(path)
    return out


def _seed_line(where: str, sd: an.Seed | None) -> str:
    return f"nearest seed {where}: " + (f"{sd['kind']} {sd['side']} at row {sd['pos']}" if sd is not None else "none")


def _in_window(seeds: Sequence[an.Seed], window: strip.Rows) -> list[an.Seed]:
    """Return the seeds in rows window, its end included (the strip-end seed of a window to the strip's end)."""
    return [sd for sd in seeds if window[0] <= sd["pos"] <= window[1]]


# ---------------------------------------------------------------- the commands


def spans(cfg: config.Config, values: Sequence[str], review_id: str | None) -> list[str]:
    """`junk cut --spans ID` or `--spans CHAPTER head|tail`: draw span sheets; return the lines to print."""
    with _workspace(cfg) as work:
        if len(values) == 1 and values[0].isdecimal():
            return _candidate_spans(cfg, int(values[0]), review_id, work)
        if len(values) == REGION_ARGS and values[0].isdecimal() and values[1] in ("head", "tail"):
            return _region_spans(cfg, int(values[0]), values[1], review_id, work)
    msg = f"--spans {' '.join(values)}: not ID or CHAPTER head|tail"
    raise workflow.Refused(msg)


def _candidate_units(sc: strip_units.StripScan, key: str) -> list[strip.Rows]:
    """Return the scan's strip units (edge blocks, their pieces and remainders) whose signature matches key, sorted."""
    want = images.sig_from_str(key)
    units: list[tuple[int, int, str]] = []
    for b in sc["blocks"]:
        units.append((b["start"], b["end"], b["sig"]))
        for j in b["joins"]:
            units.append((j["start"], j["end"], j["sig"]))
            if j["rest"] is not None:
                units.append((j["rest"]["start"], j["rest"]["end"], j["rest"]["sig"]))
    return sorted({(a, b) for a, b, sig in units if images.matches(want, images.sig_from_str(sig))})


def _candidate_spans(cfg: config.Config, candidate: int, review_id: str | None, work: Path) -> list[str]:
    """Draw each unit matching an open block or probe candidate's key, +- CONTEXT rows.

    In the lowest-id chapter waiting on it (at junk, not done, current strip scan, content digest, usable download);
    its nearest seeds above and below are named, even outside the window. Refused when no unit there matches.
    """
    with _snapshot(cfg) as conn:
        cand = _open_candidate(conn, candidate, review_id)
        inp = _inputs(conn, _candidate_series(conn, cand), [], candidate)
    s = next(_waiting_strips(cfg, inp, work), None)
    if s is None:
        msg = f"junk candidate {candidate}: no chapter waiting on it has a current strip scan and a usable download"
        raise workflow.Refused(msg)
    units = _candidate_units(s.scan, cand["key"])
    if not units:
        msg = f"junk candidate {candidate}: no unit of chapter {s.row['id']} matches its key"
        raise workflow.Refused(msg)
    decided = _decide(s, _rules(inp))
    seeds = decided["seeds"]
    dest = _sheet_folder(cfg, review_id, work)
    lines = [f"chapter {s.row['id']}: candidate {candidate}'s units, {s.layout.height} rows"]
    for a, b in units:
        above = max((sd for sd in seeds if sd["pos"] <= a), key=lambda sd: sd["pos"], default=None)
        below = min((sd for sd in seeds if sd["pos"] >= b), key=lambda sd: sd["pos"], default=None)
        window = (max(0, a - CONTEXT), min(s.layout.height, b + CONTEXT))
        lines += [f"unit {a}:{b}", _seed_line("above", above), _seed_line("below", below)]
        stem = f"spans-{s.row['id']}-{a}-{b}"
        lines += [f"sheet: {p}" for p in _span_sheets(s, window, decided, dest, stem)]
    return lines


def _region_spans(cfg: config.Config, chapter: int, region: str, review_id: str | None, work: Path) -> list[str]:
    """Draw a chapter's first or last EDGE_PX rows (an.region_bounds), its seeds there marked."""
    with _snapshot(cfg) as conn:
        inp = _inputs(conn, _series_of(conn, [chapter]), [chapter])
    s = _named_strip(cfg, inp, chapter, work)
    window = an.region_bounds(s.layout.height, region, strip_units.EDGE_PX)
    dest = _sheet_folder(cfg, review_id, work)
    sheets = _span_sheets(s, window, _decide(s, _rules(inp)), dest, f"spans-{chapter}-{region}")
    return [f"chapter {chapter}: {region} rows {window[0]}:{window[1]} of {s.layout.height}"] + [
        f"sheet: {p}" for p in sheets
    ]


def _plan(cfg: config.Config, args: CutArgs | EdgeArgs, review_id: str | None, work: Path) -> Plan:
    if isinstance(args, CutArgs):
        return _cut_plan(cfg, args, review_id, work)
    return _edge_plan(cfg, args, review_id, work)


def _chapters(p: Plan) -> list[tuple[int, str]]:
    return [(h.strip.row["id"], h.strip.row["content_digest"]) for h in p.hand]


def propose(cfg: config.Config, args: CutArgs | EdgeArgs, review_id: str | None) -> list[str]:
    """Propose a cut or a card side, writing only sheets; return the lines to print (a digest when no miss)."""
    with _workspace(cfg) as work:
        p = _plan(cfg, args, review_id, work)
        dest = _sheet_folder(cfg, review_id, work)
        checked = _check(cfg, p, work, dest)
        sheets = [*_named_sheets(p, checked, dest), *checked.sheets]
    lines = list(p.lines)
    for o in checked.outcomes:
        text = "miss: " + "; ".join(o.misses) if o.misses else "taken" if o.taken else "no cut"
        lines.append(f"chapter {o.chapter}: {text}")
    lines += [
        f"also removes: chapter {o.chapter} rows " + ", ".join(f"{a}:{b}" for a, b in o.also)
        for o in checked.outcomes
        if o.also
    ]
    lines.append(checked.unchecked)
    lines += [f"sheet: {path}" for path in sheets]
    if any(o.misses for o in checked.outcomes):
        lines.append("no digest: a checked chapter's outcome is a miss; change the ranges or fix that first")
    else:
        lines.append(f"digest: {digest(p.args, p.inputs.state, _chapters(p))} (record it with --record DIGEST)")
    return lines


def command_text(args: CutArgs | EdgeArgs) -> str:
    """Return the command an act records: junk cut with its arguments."""
    if isinstance(args, EdgeArgs):
        return f"junk cut --edge {args.candidate} {args.side}"
    ranges = [f"{n.chapter}:{n.start}:{n.end}" + (":keep" if n.kind == "keep" else "") for n in args.ranges]
    flags = [*(["--credit"] if args.credit else []), *(["--inside"] if args.inside else [])]
    return " ".join(["junk cut", *ranges, *flags])


def record(cfg: config.Config, conn: db.Connection, args: CutArgs | EdgeArgs, given: str, review_id: str | None) -> int:
    """Record a proposal in 1 act when its inputs digest is still `given`; return the act's id. Refused otherwise.

    The decision is built before the transaction; inside it the key span (a cut) or the candidate (a card side) is
    checked free, the digest is read again from the database, and the decision written.
    """
    with _workspace(cfg) as work:
        p = _plan(cfg, args, review_id, work)
    with db.transaction(conn):
        if p.candidate is None:
            taken = store.active_on(conn, p.inputs.key, "cut", p.decision["key"])
            if taken is not None:
                msg = f"key span {p.decision['key']} is taken: covered by decision {taken['id']}"
                raise workflow.Refused(msg)
        else:
            _open_candidate(conn, p.candidate["id"], review_id)
        now = digest(p.args, state(conn, p.inputs.series), _chapters(p))
        if now != given:
            msg = (
                f"stale: the inputs digest is {now} now, not {given} (a decision, a watermark, a scan or a download"
                " changed since)"
            )
            raise workflow.Refused(msg)
        at = workflow.now(conn)
        act = store.new_act(conn, command_text(args), at)
        store.write(conn, [{**p.decision, "decided_at": at}], act)
    return act
