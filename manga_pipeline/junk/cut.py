"""`junk cut` (SPEC "Junk decisions", decision commands): span sheets, cut and card-side proposals, and their record.

Ported from the bulk converter's optimizer/junk_cut.py, forms `new` and `edge` only: the snap (`_snap`), the anchor
(`_anchor`), the segments (`_segments`), `_interior_anchor`, the own-chapter check (`_check_cut_version`) and the
sheets (`_sheet`, CONTEXT, SHEET_W, its colours); its argument forms from optimizer/junk.py (:2673-2766).

A proposal reads the database once, in 1 read-only snapshot, before its checks. Each chapter whose pixels it reads is
extracted into <paths.scratch>/junk-cut/<pid>/x/ (removed when the call ends); a named chapter holding no current scan
is scanned again from its pages (OCR on), the scan kept in memory only. Every checked chapter is decided with and
without the proposed decision by strip_units.decide (the 1 owner of the decide order). A proposal writes only sheets;
`--record DIGEST` builds the decision again from the same arguments, before its transaction, and inside it writes the
decision in 1 act when the inputs digest (`state`, then `digest`: read from the database only) is unchanged.
"""

import contextlib
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import time
import zipfile
from collections.abc import Generator, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple, cast

from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import archive, config, db, errors, images, page_store, strip, workflow
from manga_pipeline.db import sql
from manga_pipeline.junk import anchored as an
from manga_pipeline.junk import chains, chapter_scan, groups, review, store, strip_units
from manga_pipeline.replies import Json
from manga_pipeline.stages import junk as junk_stage

type Side = Literal["above", "below"]
type Kind = Literal["cut", "keep"]
type Colour = tuple[int, int, int]

NEW_ROW = 10**12  # the proposed decision's id in a decide: the newest of all (the bulk converter's NEW_ROW)
NEW_ADDED = "9999-12-31T23:59:59+00:00"  # its decided_at there (NEW_ADDED)
# D7: the scanned chapters checked besides the named or picked ones: the most recent by chapter id; the rest are
# counted "not checked". Plan phase-10-21 step 2's timing (1 proposal within 2 minutes) sets the value.
CHECKED_RECENT = 20
CONTEXT = 300  # strip rows shown above and below a proposal sheet's range, or a span sheet's candidate unit
SHEET_W = 400  # proposal sheet width (px)
GREEN, BLUE, ORANGE, RED = (0, 170, 0), (0, 90, 255), (255, 140, 0), (220, 0, 0)
COLOUR: dict[Kind, Colour] = {"cut": GREEN, "keep": BLUE}
DIGEST_HEX = 16  # hex digits of a digest
SCRATCH = "junk-cut"  # <paths.scratch>/junk-cut/<pid>/: a call's sheets (outside a review) and its pages (x/)
SCRATCH_AGE = 86400  # s: an earlier call's scratch folder older than this is removed at the next call
RULER_W = 64  # span sheet: the row ruler's column (px), a row number every RULER_STEP rows
RULER_STEP = 100
LABEL_W = 300  # span sheet: the span labels' column (px), in LABEL_LANES lanes so near spans' labels do not overlap
LABEL_LANES = 3
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
    if len(parts) != TOKEN_PARTS or not all(p.isdigit() for p in parts):
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
    if not candidate.isdigit() or side not in chains.SIDES:
        msg = f"--edge {' '.join(values)}: not ID above|below"
        raise workflow.Refused(msg)
    return EdgeArgs(int(candidate), "above" if side == "above" else "below")


# ---------------------------------------------------------------- the snapshot


class Scanned(NamedTuple):
    """A chapter as the snapshot read it.

    Its row, its current strip scan (None: none), its page rows, and whether its scan waits on the candidate asked
    about.
    """

    row: db.Row
    scan: strip_units.StripScan | None
    pages: dict[int, images.MeasuredPage]
    waits: bool


class Inputs(NamedTuple):
    """What a proposal reads of the database, in 1 snapshot.

    The series and its key; the active decisions of the series and the global ones (newest id first); its watermark
    keys; every chapter of it holding a current strip scan, by id; how many of its chapters not done at discover,
    validate or junk hold none; the named chapters by id; the database part of the inputs digest (state).
    """

    series: db.Row
    key: str
    decisions: list[db.Row]
    known: list[str]
    scanned: list[Scanned]
    unscanned: list[int]
    named: dict[int, Scanned]
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
    """Return the inputs digest: the arguments, the database state and each named or picked chapter's (id, digest)."""
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
    """Read a proposal's inputs; Refused for a series that is no strip. candidate: the candidate `waits` asks about."""
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
        sql.SQL("""SELECT c.*, s.scan AS junk_scan, s.candidates::jsonb @> to_jsonb(%s::bigint) AS junk_waits
            FROM chapter c JOIN junk_scan s ON s.chapter_id = c.id
            WHERE c.series_id = %s AND {} ORDER BY c.id""").format(_scanned()),
        (candidate, series["id"]),
    ).fetchall()
    scanned = [Scanned(r, _strip_scan(r["junk_scan"]), page_store.load(conn, r["id"]), r["junk_waits"]) for r in rows]
    by_id = {s.row["id"]: s for s in scanned}
    named_rows = conn.execute("SELECT * FROM chapter WHERE id = ANY(%s)", (list(named),)).fetchall()
    unscanned = conn.execute(
        sql.SQL("""SELECT c.id FROM chapter c WHERE c.series_id = %s AND c.status <> 'done'
            AND c.stage IN ('discover', 'validate', 'junk') AND NOT {} ORDER BY c.id""").format(_scanned()),
        (series["id"],),
    )
    return Inputs(
        series,
        key,
        decisions,
        known,
        scanned,
        [r[0] for r in unscanned if r[0] not in named],
        {
            r["id"]: by_id.get(r["id"]) or Scanned(r, None, page_store.load(conn, r["id"]), waits=False)
            for r in named_rows
        },
        state(conn, series),
    )


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


def _usable_name(stored: str | None) -> bool:
    """Return whether a stored download path names a file this machine can open (Windows refuses some names)."""
    if not stored:
        return False
    return not (os.name == "nt" and re.search(workflow.WINDOWS_UNUSABLE, stored))


def _download(cfg: config.Config, row: db.Row) -> Path | None:
    """Return a chapter's download when this machine can use it, else None."""
    if not _usable_name(row["download_path"]):
        return None
    path = config.to_local(cfg, row["download_path"])
    return path if errors.exists(path) else None


def _retained(cfg: config.Config, row: db.Row) -> Path | None:
    """Return a chapter's download as the retention folder keeps it (same content digest), else None."""
    if not _usable_name(row["download_path"]):
        return None
    download = config.to_local(cfg, row["download_path"])
    folder = config.abs_path(cfg["paths"]["retention"]) / download.parent.parent.name / download.parent.name
    if not errors.exists(folder):
        return None
    taken = sorted(folder.glob(f"{glob.escape(download.stem)} (*){glob.escape(download.suffix)}"))
    found = (p for p in [folder / download.name, *taken] if errors.exists(p))
    return next((p for p in found if archive.pages_digest(p) == row["content_digest"]), None)


class Strip(NamedTuple):
    """A chapter's strip in hand: its row, its scan, its layout and render(a, b) of strip rows [a, b)."""

    row: db.Row
    scan: strip_units.StripScan
    layout: an.Layout
    render: an.Render


def _extract(cbz: Path, known: dict[int, images.MeasuredPage], work: Path) -> chapter_scan.Extracted:
    """Write a CBZ's pages to `work` in archive order (as the junk stage extracts a download)."""
    measured = images.measure_pages(cbz, known)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    paths: list[Path]
    names: list[str]
    paths, names = [], []
    for i, (name, data) in enumerate(archive.pages(cbz)):
        path = work / f"{i:04d}{Path(name).suffix.lower()}"
        path.write_bytes(data)
        paths.append(path)
        names.append(name)
    return chapter_scan.Extracted(paths, names, [(m["width"], m["height"]) for m in measured])


def _pages_folder(work: Path, chapter: int) -> Path:
    return work / "x" / str(chapter)


def _in_hand(cfg: config.Config, ch: Scanned, cbz: Path, work: Path, decisions: store.Decisions) -> Strip:
    """Return a chapter's strip from its pages: its current scan, else the strip scanned again (held in memory)."""
    pages = _extract(cbz, ch.pages, _pages_folder(work, ch.row["id"]))
    sc = ch.scan if ch.scan is not None else chapter_scan.scan_strip(pages, decisions, cfg["junk"]["ocr_device"])
    layout = an.Layout(sc["height"], sc["width"], sc["spans"], sc["gaps"])
    return Strip(ch.row, sc, layout, chapter_scan.renderer(pages))


def _named_strip(cfg: config.Config, inp: Inputs, chapter: int, work: Path) -> Strip:
    """Return a named chapter's strip: from its download, else its retention original; Refused when neither is."""
    ch = inp.named[chapter]
    cbz = _download(cfg, ch.row) or _retained(cfg, ch.row)
    if cbz is None:
        msg = f"no pages for chapter {chapter}: neither its download nor a retention original is on this machine"
        raise workflow.Refused(msg)
    return _in_hand(cfg, ch, cbz, work, _decisions(inp.key, inp.decisions))


@contextlib.contextmanager
def _workspace(cfg: config.Config) -> Generator[Path]:
    """Yield this call's scratch folder; earlier calls' over SCRATCH_AGE old go first, its pages (x/) at its end."""
    root = config.abs_path(cfg["paths"]["scratch"]) / SCRATCH
    own = root / str(os.getpid())
    if root.exists():
        for old in root.iterdir():
            if old != own and time.time() - old.stat().st_mtime > SCRATCH_AGE:
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
    """What a decide applies: decide_unit, the known watermark signatures and the cut decisions, oldest first."""

    decide_unit: store.DecideUnit
    known: list[images.Signature]
    cuts: list[db.Row]


def _decisions(key: str, rows: Sequence[db.Row]) -> store.Decisions:
    return store.Decisions.of_rows(key, [(r, store.parse_key(r["kind"], r["key"])) for r in rows])


def _row(**columns: object) -> db.Row:
    """Return a stored row's stand-in of these columns: the proposed decision as a decide reads it."""
    row_class = cast("type[db.Row]", type("Row", (db.Row,), {"__slots__": (), "_keys": tuple(columns)}))
    return row_class(columns.values())


def _rules(inp: Inputs, proposed: store.Decision | None = None) -> Rules:
    """Return today's rules, or (proposed) those with the proposed decision the newest of all (id NEW_ROW)."""
    rows = inp.decisions
    if proposed is not None:
        rows = [_row(id=NEW_ROW, superseded_by=None, retired_by=None, written_by=None, **proposed), *rows]
    decisions = _decisions(inp.key, rows)

    def decide_unit(kind: str, text: str) -> db.Row | None:
        return decisions.decide(kind, store.parse_key(kind, text))

    return Rules(decide_unit, [images.sig_from_str(k) for k in inp.known], decisions.cuts())


def _decide(s: Strip, rules: Rules) -> strip_units.StripDecision:
    return strip_units.decide(s.scan, rules.decide_unit, rules.known, rules.cuts, s.render)


def _decider(review_id: str | None) -> str:
    return "claude" if review_id else "human"


def _cut_decision(inp: Inputs, text: str, decider: str, *, credit: bool) -> store.Decision:
    """Return a cut decision on chain text, its key the chain's key span, as store.write writes it.

    decided_at is NEW_ADDED until it is recorded.
    """
    return {
        "series_key": inp.key,
        "kind": "cut",
        "key": chains.key_span(chains.check_cut(chains.key_span(chains.parse(text)), text)),
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


def _mine(d: strip_units.StripDecision, why: Sequence[str] = DRAWN) -> list[strip.Rows]:
    """Return the ranges the proposed decision removes for these reasons (its cut or card), sorted."""
    return sorted((r["start"], r["end"]) for r in d["removed"] if r.get("decision") == NEW_ROW and r["why"] in why)


def _lost(s: Strip, before: strip_units.StripDecision, after: strip_units.StripDecision) -> list[strip.Rows]:
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
    """1 version built from a chapter's named ranges: its chapter, side, anchor seed, segments and JSON value."""

    chapter: int
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


def _version(s: Strip, ranges: Sequence[Named], inp: Inputs, *, inside: bool) -> Version:
    """Return the version of a chapter's named ranges: snapped, anchored, its segments, src and ref (plan D2)."""
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
    side, sd = _anchor(s, snapped[0].start, snapped[-1].end, _decide(s, _rules(inp))["seeds"])
    pieces = _segments(s, snapped, side, an.span_sigs(s.render))
    _interior_anchor(s, sd, pieces)
    segments: list[Json] = [
        {"kind": p.kind, "h": round((p.end - p.start) / layout.width, chains.DECIMALS), "tiles": p.tiles}
        | ({"exact": True} if p.exact else {})
        for p in pieces
    ]
    value: Json = {
        "segments": segments,
        "ref": [[inp.key, str(s.row["id"]), sd["pos"]]],
        "src": {"file": PurePosixPath(s.row["download_path"]).name, "sha256": s.row["content_digest"]},
    }
    return Version(s.row["id"], side, sd, pieces, value)


def _own_check(s: Strip, v: Version, inp: Inputs, ranges: Sequence[Named]) -> None:
    """Refuse a version that matches nowhere in its own chapter over its named cut pieces.

    The version is tried alone, overlap extension and its cap included.
    """
    alone = _cut_decision(inp, chains.text({v.side: [v.value]}), "human", credit=False)
    before, got = _decide(s, _rules(inp)), _decide(s, _rules(inp, alone))
    cuts = [(n.start, n.end) for n in ranges if n.kind == "cut"]
    if not any(an.hits(a, b, cuts) for a, b in _mine(got)):
        new = [_shown(t) for t in got["misses"] if t not in before["misses"]]
        msg = f"chapter {s.row['id']}: the version matches nowhere in its own chapter over its named pieces" + (
            f" ({'; '.join(new)})" if new else ""
        )
        raise workflow.Refused(msg)


# ---------------------------------------------------------------- plans


class Plan(NamedTuple):
    """A proposal built.

    Its inputs, its arguments as the digest holds them, the proposed decision, the named or picked chapters in hand,
    the rows its sheets draw, its lines so far, and for a card side the candidate.
    """

    inputs: Inputs
    args: Json
    decision: store.Decision
    hand: list[Strip]
    pieces: list[Named]
    lines: list[str]
    candidate: db.Row | None


def _by_chapter(ranges: Sequence[Named]) -> dict[int, list[Named]]:
    out: dict[int, list[Named]] = {}
    for n in ranges:
        out.setdefault(n.chapter, []).append(n)
    return out


def _cut_plan(cfg: config.Config, args: CutArgs, review_id: str | None, work: Path) -> Plan:
    """Build a cut proposal: 1 version per named chapter, all on 1 side; its key the first version's key span."""
    by = _by_chapter(args.ranges)
    with _snapshot(cfg) as conn:
        inp = _inputs(conn, _series_of(conn, list(by)), list(by))
    hand = [_named_strip(cfg, inp, c, work) for c in by]
    versions = [_version(s, by[s.row["id"]], inp, inside=args.inside) for s in hand]
    side = versions[0].side
    for v in versions[1:]:
        if v.side != side:
            msg = f"chapter {v.chapter}'s ranges anchor {v.side}, chapter {versions[0].chapter}'s {side}: 1 side only"
            raise workflow.Refused(msg)
    decision = _cut_decision(
        inp, chains.text({side: [v.value for v in versions]}), _decider(review_id), credit=args.credit
    )
    taken = next((r for r in inp.decisions if r["kind"] == "cut" and r["key"] == decision["key"]), None)
    if taken is not None:
        msg = f"key span {decision['key']} is taken: covered by decision {taken['id']} (1 active cut per key span)"
        raise workflow.Refused(msg)
    for s, v in zip(hand, versions, strict=True):
        _own_check(s, v, inp, by[s.row["id"]])
    lines = [f"cut of series {inp.key}: side {side}, key span {decision['key']}" + (", credit" if args.credit else "")]
    lines += [
        f"version {i}: chapter {v.chapter}, from the {v.anchor['kind']} seed at row {v.anchor['pos']}: "
        + ", ".join(f"{p.kind} {p.start}:{p.end}" + (" exact" if p.exact else "") for p in v.pieces)
        for i, v in enumerate(versions, 1)
    ]
    if args.credit or any(
        s.layout.region(v.anchor["pos"], strip_units.EDGE_PX) == "tail" for s, v in zip(hand, versions, strict=True)
    ):
        lines.append(
            "warning: the cut acts in a tail region (or carries credit): the tail pass decides the pieces it uncovers"
            " (a credit cut starts the credit block); check them on the sheets"
        )
    pieces = [Named(v.chapter, p.start, p.end, p.kind) for v in versions for p in v.pieces]
    args_json: Json = ["cut", [list(n) for n in args.ranges], args.credit, args.inside]
    return Plan(inp, args_json, decision, hand, pieces, lines, None)


def _beside(side: Side, unit: strip.Rows, card: Sequence[strip.Rows]) -> bool:
    """Return whether a card range lies on the side of a unit (what its edge version removes)."""
    return any(c[0] >= unit[1] for c in card) if side == "below" else any(c[1] <= unit[0] for c in card)


def _edge_plan(cfg: config.Config, args: EdgeArgs, review_id: str | None, work: Path) -> Plan:
    """Build a card side: the open candidate's junk decision carrying an edge version on the side.

    From the lowest-id chapter waiting on the candidate (current strip scan, usable download) where the side removes
    rows (its unit in the half toward that edge); Refused when none.
    """
    with _snapshot(cfg) as conn:
        cand = _open_candidate(conn, args.candidate, review_id)
        inp = _inputs(conn, _candidate_series(conn, cand), [], args.candidate)
    decider, why = _decider(review_id), list[str]()
    trial = _rules(inp, _edge_decision(cand, args.side, [[inp.key, "0", 0]], decider))  # ref: matching reads none
    for ch in (c for c in inp.scanned if c.waits and c.row["stage"] == "junk" and c.row["status"] != "done"):
        cbz = _download(cfg, ch.row)
        if cbz is None:
            why.append(f"chapter {ch.row['id']}: no usable download")
            continue
        s = _in_hand(cfg, ch, cbz, work, _decisions(inp.key, inp.decisions))
        got = _decide(s, trial)
        units, card = _mine(got, ("match",)), _mine(got, ("card",))
        sided = [u for u in units if _beside(args.side, u, card)]
        if sided:
            refs: list[Json] = [[inp.key, str(s.row["id"]), u[1] if args.side == "below" else u[0]] for u in sided]
            lines = [
                f"card side {args.side} of candidate {cand['id']} ({cand['kind']}) of series {inp.key}: an edge from"
                f" chapter {s.row['id']}'s unit at " + ", ".join(f"{a}:{b}" for a, b in sided)
            ]
            pieces = [Named(s.row["id"], a, b, "cut") for a, b in card]
            decision = _edge_decision(cand, args.side, refs, decider)
            return Plan(inp, ["edge", cand["id"], args.side], decision, [s], pieces, lines, cand)
        shutil.rmtree(_pages_folder(work, s.row["id"]), ignore_errors=True)
        found = ", ".join(f"{a}:{b}" for a, b in units) or "none"
        why.append(f"chapter {s.row['id']}: its unit at {found} of {s.layout.height} rows, the side removes nothing")
    msg = (
        f"junk candidate {args.candidate}: no chapter waiting on it has its unit in the {args.side} half with rows its"
        f" side removes ({'; '.join(why) or 'none waits with a current strip scan'})"
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

    Each checked chapter's outcome, the named or picked chapters' 2 decides (for the sheets), and the line counting
    the chapters not checked.
    """

    outcomes: list[Outcome]
    decides: dict[int, tuple[strip_units.StripDecision, strip_units.StripDecision]]
    unchecked: str


def _outcome(p: Plan, s: Strip, before: strip_units.StripDecision, after: strip_units.StripDecision) -> Outcome:
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


def _recent(inp: Inputs, hand: set[int]) -> tuple[list[Scanned], int]:
    """Return the scanned chapters checked besides those in hand (the CHECKED_RECENT newest by id) and how many not."""
    others = [c for c in reversed(inp.scanned) if c.row["id"] not in hand]
    return others[:CHECKED_RECENT], len(others[CHECKED_RECENT:])


def _check(cfg: config.Config, p: Plan, work: Path) -> Checked:
    """Decide every checked chapter with and without the proposed decision (plan D0d, D7)."""
    today, proposed = _rules(p.inputs), _rules(p.inputs, p.decision)
    outcomes: list[Outcome] = []
    decides: dict[int, tuple[strip_units.StripDecision, strip_units.StripDecision]] = {}
    for s in p.hand:
        before, after = _decide(s, today), _decide(s, proposed)
        decides[s.row["id"]] = (before, after)
        outcomes.append(_outcome(p, s, before, after))
    others, beyond = _recent(p.inputs, set(decides))
    unusable = 0
    for ch in others:
        cbz = _download(cfg, ch.row)
        if cbz is None:
            unusable += 1
            continue
        try:
            s = _in_hand(cfg, ch, cbz, work, _decisions(p.inputs.key, p.inputs.decisions))
        except (ValueError, zipfile.BadZipFile):  # a download it cannot read is no usable download here
            unusable += 1
            continue
        outcomes.append(_outcome(p, s, _decide(s, today), _decide(s, proposed)))
        shutil.rmtree(_pages_folder(work, ch.row["id"]), ignore_errors=True)
    unscanned = len(p.inputs.unscanned)
    total = unscanned + unusable + beyond
    unchecked = (
        f"{total} not checked: {unscanned} without a current scan, {unusable} without a usable download,"
        f" {beyond} beyond the {CHECKED_RECENT} most recent scanned"
    )
    return Checked(sorted(outcomes, key=lambda o: o.chapter), decides, unchecked)


# ---------------------------------------------------------------- sheets


def _font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=FONT_PX)


def _sheet(s: Strip, rows: strip.Rows, marks: Sequence[tuple[int, int, Colour, int]]) -> Image.Image:
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


def _proposal_sheets(p: Plan, checked: Checked, dest: Path) -> list[Path]:
    """Write a sheet per named range and per range it also removes; return their paths.

    Cut green, keep blue; on the right what is removed without the proposal orange, and its own rows beyond the named
    ones red.
    """
    out: list[Path] = []
    for s in p.hand:
        chapter = s.row["id"]
        before, after = checked.decides[chapter]
        removed = [(r["start"], r["end"], ORANGE, 1) for r in before["removed"]]
        mine = _mine(after)
        sheets: list[tuple[str, strip.Rows, list[tuple[int, int, Colour, int]]]] = []
        for n in (n for n in p.pieces if n.chapter == chapter):
            ext = [(x, y, RED, 1) for x, y in mine if an.hits(x, y, [(n.start, n.end)]) and (x < n.start or y > n.end)]
            sheets.append(
                (
                    f"cut-{chapter}-{n.start}-{n.end}",
                    (n.start, n.end),
                    [*removed, *ext, (n.start, n.end, COLOUR[n.kind], 0)],
                )
            )
        also = next(o.also for o in checked.outcomes if o.chapter == chapter)
        sheets += [(f"also-{chapter}-{x}-{y}", (x, y), [*removed, (x, y, GREEN, 0)]) for x, y in also]
        for stem, rows, marks in sheets:
            path = dest / f"{stem}.png"
            images.save_png(_sheet(s, rows, marks), path)
            out.append(path)
    return out


def _span_sheets(s: Strip, window: strip.Rows, seeds: Sequence[an.Seed], dest: Path, stem: str) -> list[Path]:
    """Write the span sheets of strip rows `window`; return their paths.

    Each at most groups.SHEET_LONG_EDGE long and groups.SHEET_PIXELS, with a row ruler, every span labelled START:END
    (END exclusive: the range to name), the seeds given marked in red.
    """
    lo, hi = window
    scale = min(1.0, (groups.SHEET_LONG_EDGE - RULER_W - LABEL_W) / s.layout.width)
    img_w = max(1, round(s.layout.width * scale))
    sheet_w = RULER_W + img_w + LABEL_W
    per = max(1, int(min(groups.SHEET_LONG_EDGE, groups.SHEET_PIXELS // sheet_w) / scale))
    font = _font()
    out: list[Path] = []
    for n, a in enumerate(range(lo, hi, per), 1):
        b = min(hi, a + per)
        height = max(1, round((b - a) * scale))
        sheet = Image.new("RGB", (sheet_w, height), "white")
        sheet.paste(s.render(a, b).convert("RGB").resize((img_w, height)), (RULER_W, 0))
        draw = ImageDraw.Draw(sheet)

        def y(row: int, a: int = a) -> int:
            return round((row - a) * scale)

        for r in range(-(-a // RULER_STEP) * RULER_STEP, b, RULER_STEP):
            draw.line((RULER_W - 12, y(r), RULER_W - 1, y(r)), fill="black")
            draw.text((2, max(0, y(r) - FONT_PX // 2)), str(r), fill="black", font=font)
        lane_w = LABEL_W // LABEL_LANES
        for k, (x0, x1) in enumerate(s.layout.spans):
            if x1 <= a or x0 >= b:
                continue
            left = RULER_W + img_w + (k % LABEL_LANES) * lane_w + 4
            draw.line((left, y(max(x0, a)), left, max(y(max(x0, a)), y(min(x1, b)) - 1)), fill=BLUE, width=3)
            draw.text((left + 5, max(0, y(max(x0, a)))), f"{x0}:{x1}", fill=BLUE, font=font)
        for sd in (sd for sd in seeds if a <= sd["pos"] < b):
            draw.line((RULER_W, y(sd["pos"]), RULER_W + img_w - 1, y(sd["pos"])), fill=RED, width=2)
            label = f"seed {sd['side']} {sd['pos']} ({sd['kind']})"
            draw.text((RULER_W + 4, max(0, y(sd["pos"]) - FONT_PX - 2)), label, fill=RED, font=font)
        path = dest / f"{stem}-{n}.png"
        images.save_png(sheet, path)
        out.append(path)
    return out


def _seed_line(where: str, sd: an.Seed | None) -> str:
    return f"nearest seed {where}: " + (f"{sd['kind']} {sd['side']} at row {sd['pos']}" if sd is not None else "none")


# ---------------------------------------------------------------- the commands


def spans(cfg: config.Config, values: Sequence[str], review_id: str | None) -> list[str]:
    """`junk cut --spans ID` or `--spans CHAPTER head|tail`: draw span sheets; return the lines to print."""
    with _workspace(cfg) as work:
        if len(values) == 1 and values[0].isdigit():
            return _candidate_spans(cfg, int(values[0]), review_id, work)
        if len(values) == REGION_ARGS and values[0].isdigit() and values[1] in ("head", "tail"):
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

    In the lowest-id chapter waiting on it (at junk, not done, current strip scan, usable download); its nearest seeds
    above and below are named, even outside the window.
    """
    with _snapshot(cfg) as conn:
        cand = _open_candidate(conn, candidate, review_id)
        inp = _inputs(conn, _candidate_series(conn, cand), [], candidate)
    waiting = [c for c in inp.scanned if c.waits and c.row["stage"] == "junk" and c.row["status"] != "done"]
    picked = next(((c, cbz) for c in waiting if (cbz := _download(cfg, c.row)) is not None), None)
    if picked is None:
        msg = f"junk candidate {candidate}: no chapter waiting on it has a current strip scan and a usable download"
        raise workflow.Refused(msg)
    ch, cbz = picked
    s = _in_hand(cfg, ch, cbz, work, _decisions(inp.key, inp.decisions))
    seeds = _decide(s, _rules(inp))["seeds"]
    dest = _sheet_folder(cfg, review_id, work)
    lines = [f"chapter {s.row['id']}: candidate {candidate}'s units, {s.layout.height} rows"]
    for a, b in _candidate_units(s.scan, cand["key"]):
        above = max((sd for sd in seeds if sd["pos"] <= a), key=lambda sd: sd["pos"], default=None)
        below = min((sd for sd in seeds if sd["pos"] >= b), key=lambda sd: sd["pos"], default=None)
        window = (max(0, a - CONTEXT), min(s.layout.height, b + CONTEXT))
        shown = [sd for sd in seeds if window[0] <= sd["pos"] < window[1]]
        lines += [f"unit {a}:{b}", _seed_line("above", above), _seed_line("below", below)]
        lines += [f"sheet: {p}" for p in _span_sheets(s, window, shown, dest, f"spans-{s.row['id']}-{a}-{b}")]
    return lines


def _region_spans(cfg: config.Config, chapter: int, region: str, review_id: str | None, work: Path) -> list[str]:
    """Draw a chapter's first or last EDGE_PX rows (an.region_bounds), its seeds there marked."""
    with _snapshot(cfg) as conn:
        inp = _inputs(conn, _series_of(conn, [chapter]), [chapter])
    s = _named_strip(cfg, inp, chapter, work)
    window = an.region_bounds(s.layout.height, region, strip_units.EDGE_PX)
    seeds = [sd for sd in _decide(s, _rules(inp))["seeds"] if window[0] <= sd["pos"] < window[1]]
    dest = _sheet_folder(cfg, review_id, work)
    sheets = _span_sheets(s, window, seeds, dest, f"spans-{chapter}-{region}")
    return [f"chapter {chapter}: {region} rows {window[0]}:{window[1]} of {s.layout.height}"] + [
        f"sheet: {p}" for p in sheets
    ]


def _plan(cfg: config.Config, args: CutArgs | EdgeArgs, review_id: str | None, work: Path) -> Plan:
    if isinstance(args, CutArgs):
        return _cut_plan(cfg, args, review_id, work)
    return _edge_plan(cfg, args, review_id, work)


def _chapters(p: Plan) -> list[tuple[int, str]]:
    return [(s.row["id"], s.row["content_digest"]) for s in p.hand]


def propose(cfg: config.Config, args: CutArgs | EdgeArgs, review_id: str | None) -> list[str]:
    """Propose a cut or a card side, writing only sheets; return the lines to print (a digest when no miss)."""
    with _workspace(cfg) as work:
        p = _plan(cfg, args, review_id, work)
        checked = _check(cfg, p, work)
        sheets = _proposal_sheets(p, checked, _sheet_folder(cfg, review_id, work))
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
                f"stale: the inputs digest is {now} now, not {given} (a decision, a watermark or a scan changed since)"
            )
            raise workflow.Refused(msg)
        at = workflow.now(conn)
        act = store.new_act(conn, command_text(args), at)
        store.write(conn, [{**p.decision, "decided_at": at}], act)
    return act
