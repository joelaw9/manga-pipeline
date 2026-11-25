"""Junk candidates (SPEC "Junk decisions", candidates): their selection, rows, images, open state and cleanup.

Which undecided units are candidates, 1 candidate row per key, the candidates' images, which candidates are open, and
the cleanup of closed ones.

A candidate is open while a chapter at the junk stage that is not done waits on it (its id is in the chapter's
junk_scan.candidates) and no decision covers it. Its folder in paths.candidates (on the share) is named by its series
key, kind and key and by the chapter claim that wrote it, and its row stores that name, so no 2 rows share a folder.
junk review cleans up (SPEC "Junk decisions"): candidates closed for an hour go, with their folders.
"""

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable, Container, Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NotRequired, TypedDict

from PIL import Image

from manga_pipeline import config, db, images, names, workflow
from manga_pipeline.db import sql
from manga_pipeline.junk import logo_units, ocr, page_units, store, strip_units

log = logging.getLogger(__name__)

RECUR_CHAPTERS = 3  # D5.2 (c): a unit matching units of this many chapters of the series is a candidate
FIRST_CHAPTERS = 3  # D5.2 (d): the first chapters of a series offer every unmatched edge block
UNIT_WIDTH = 1000  # px: the unit image's largest width
CONTEXT_PX = 300  # px of strip above and below the unit in its context image
CLOSED_KEEP = timedelta(hours=1)  # a candidate closed this long is deleted with its folder
ORPHAN_KEEP_S = 24 * 3600  # a folder no row names is removed once this old (another machine may be recording it)

# An undecided unit of a chapter's scan: what its layout's decide and the logos' leave undecided.
type Unit = (
    strip_units.BlockUnit | strip_units.PieceUnit | page_units.PageUnit | page_units.BandUnit | logo_units.LogoUnit
)
# A junk_seen row: series key, kind, key, chapter identity, first seen, origin.
type SeenRow = tuple[str, str, str, str, str, str]


class ProbeUnit(strip_units.Piece):
    """A strip edge block's probe offered in its block's place, at the block's edge."""

    where: str


class Candidate(TypedDict):
    """A candidate among a chapter's undecided units (select): its kind, key text, unit and reason.

    folder: the folder its images were written to, once written.
    """

    kind: str
    key: str
    unit: Unit | ProbeUnit
    reason: str
    folder: NotRequired[str]


class Gone(RuntimeError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """A candidate the chapter meant to join was deleted meanwhile.

    The chapter fails, and its retry writes the images anew.
    """


def folder_name(series_key: str, kind: str, key: str, chapter: db.Row) -> str:
    """Return the folder name of a new candidate: its unit (hashed) and the chapter claim that writes it.

    The claim's name is workflow.claim_name.
    """
    unit = hashlib.sha1(f"{series_key}|{kind}|{key}".encode(), usedforsecurity=False).hexdigest()[:16]
    return f"{unit}-{workflow.claim_name(chapter)}"


class CandidateInfo(TypedDict):
    """A candidate folder's info.json: its series key, kind, reason, OCR text and how many chapters show it."""

    series: str
    kind: str
    reason: str
    text: str
    chapters: int


def folder(cfg: config.Config, name: str) -> Path:
    """Return the path of the candidate folder `name` under paths.candidates."""
    return config.abs_path(cfg["paths"]["candidates"]) / name


# ---------------------------------------------------------------- seen units and the rules


def seen_rows(
    series_key: str, identity: str, units: Iterable[tuple[str, str]], at: str, origin: str = "pipeline"
) -> list[SeenRow]:
    """junk_seen rows of units [(kind, key)] seen in a chapter."""
    return [(series_key, kind, key, identity, at, origin) for kind, key in units]


def write_seen(conn: db.Connection, rows: Iterable[SeenRow]) -> None:
    """Record seen units inside the caller's transaction; a unit seen again keeps its first time."""
    db.executemany(
        conn,
        """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        rows,
    )


def chapter_order(identity: str) -> tuple[bool, Decimal | int, bool, tuple[int, str] | None, str]:
    """Sort key of a chapter identity (names.identity) by season, then number; unnumbered last."""
    _, season, _, number, _ = names.read_identity(identity)
    return (
        season is None,
        Decimal(season) if season is not None else 0,
        number is None,
        names.number_key(number) if number is not None else (0, ""),
        identity,
    )


def first_identities(conn: db.Connection, series_key: str, n: int = FIRST_CHAPTERS) -> set[str]:
    """Return the first n distinct chapter identities of a series by first sighting.

    Chapter order on a tie (every sighting imported from the bulk converter has the same time).
    """
    rows = conn.execute(
        "SELECT identity, min(first_seen) FROM junk_seen WHERE series_key = %s GROUP BY identity", (series_key,)
    ).fetchall()
    return {r[0] for r in sorted(rows, key=lambda r: (r[1], chapter_order(r[0])))[:n]}


def chapters_showing(conn: db.Connection, series_key: str, kind: str, key: str) -> int:
    """How many chapter identities of the series show a unit matching (kind, key)."""
    parsed = store.parse_key(kind, key)
    rows = conn.execute("SELECT key, identity FROM junk_seen WHERE series_key = %s AND kind = %s", (series_key, kind))
    return len({ident for k, ident in rows if store.matches(store.parse_key(kind, k), parsed)})


def strip_reasons(conn: db.Connection, series_key: str, unit: strip_units.Piece, words: ocr.WordLists) -> list[str]:
    """Why a strip unit is a candidate (near, OCR keywords); [] when neither."""
    out: list[str] = []
    hit = store.near(conn, series_key, images.sig_from_str(unit["sig"]))
    if hit is not None:
        row, d, diff = hit
        out.append(f"near decision {row['id']} ({row['series_key'] or 'global'}) distance {d}, aspect {diff:.1%}")
    if text := unit.get("text"):
        terms = ocr.text_hits(text, unit["end"] - unit["start"], words)
        if terms:
            out.append(f'text {", ".join(repr(t) for t in terms)}: "{ocr.normalize(text)[:160]}"')
    return out


def _key(unit: Unit) -> str:
    """Return an undecided unit's key: its "key", or a strip unit's signature."""
    if "key" in unit:
        return unit["key"]
    return unit["sig"]


def _recurrence(conn: db.Connection, series_key: str, kind: str, key: str) -> list[str]:
    """["seen in N chapters"] when N >= RECUR_CHAPTERS chapters of the series show a unit matching (kind, key)."""
    n = chapters_showing(conn, series_key, kind, key)
    return [f"seen in {n} chapters"] if n >= RECUR_CHAPTERS else []


def _flagged_probe(
    conn: db.Connection, series_key: str, block: strip_units.BlockUnit, words: ocr.WordLists
) -> Candidate | None:
    """Return the candidate of a strip edge block's first signable probe that is near or has a keyword, or None.

    The probe is the first from the chapter edge; near and keyword are strip_reasons'.
    """
    probes = [p for p in block["probes"] if not store.unsignable("probe", p["sig"])]
    flagged = next(((p, why) for p in probes for why in [strip_reasons(conn, series_key, p, words)] if why), None)
    if flagged is None:
        return None
    probe, why = flagged
    return {"kind": "probe", "key": probe["sig"], "unit": {**probe, "where": block["where"]}, "reason": "; ".join(why)}


def _block_candidate(
    conn: db.Connection, series_key: str, block: strip_units.BlockUnit, words: ocr.WordLists, *, first_chapter: bool
) -> Candidate | None:
    """Return a strip edge block's candidate, or None.

    Its flagged probe, else the block when its key is signable and it is near, has a keyword, is the rest of a block
    whose edge piece is junk, is in a first chapter of the series (first_chapter), or recurs; else None.
    """
    probe = _flagged_probe(conn, series_key, block, words)
    if probe is not None:
        return probe
    key = _key(block)
    if store.unsignable("block", key):
        return None
    reasons = strip_reasons(conn, series_key, block, words)
    if block.get("rest"):
        reasons.append("the rest of a block whose edge piece is junk")
    if first_chapter:
        reasons.append("a first chapter of the series")
    reasons += _recurrence(conn, series_key, "block", key)
    return {"kind": "block", "key": key, "unit": block, "reason": "; ".join(reasons)} if reasons else None


def select(
    conn: db.Connection, series_key: str, identity: str, undecided: Iterable[Unit], words: ocr.WordLists
) -> list[Candidate]:
    """Return the candidates among a chapter's undecided units: [{"kind", "key", "reason", "unit"}].

    Strip edge blocks: the first probe from the chapter edge that is near or has a keyword is offered instead of its
    block; else the block when it is near, has a keyword, recurs, is the rest of a block whose edge piece is junk, or
    its chapter is among the first of the series. Tail pieces always. Pages, frames and bands: their per-chapter
    reasons, or recurrence. Logos always; a logo variant offers the first of its representatives' keys that an existing
    candidate matches, else its first. Never a unit with an unsignable key (no decision could be written on it), a
    block's probe included.
    """
    first_chapter = identity in first_identities(conn, series_key)
    decisions = store.Decisions(conn, series_key)
    out: list[Candidate] = []
    for u in undecided:
        kind, key = u["kind"], _key(u)
        found: Candidate | None
        if u["kind"] == "block":
            found = _block_candidate(conn, series_key, u, words, first_chapter=first_chapter)
        elif store.unsignable(kind, key):
            continue
        elif u["kind"] == "logo":
            key = next((k for k in u["keys"] if find(conn, decisions, kind, k) is not None), u["keys"][0])
            found = {"kind": kind, "key": key, "unit": u, "reason": "; ".join(u["reasons"])}
        else:
            reasons: list[str] = (
                ["tail piece after a credit band"]
                if kind == "piece"
                else [*u.get("reasons", []), *_recurrence(conn, series_key, kind, key)]
            )
            found = {"kind": kind, "key": key, "unit": u, "reason": "; ".join(reasons)} if reasons else None
        if found is not None:
            out.append(found)
    return out


# ---------------------------------------------------------------- candidate rows


def find(conn: db.Connection, decisions: store.Decisions, kind: str, key: str) -> db.Row | None:
    """Return the candidate row of the series and kind whose key matches `key` and that no decision covers, or None.

    decisions: the series' store.Decisions. A covered candidate is closed: matching is not transitive, so a unit it
    matches may still be undecided, and that unit gets a candidate of its own.
    """
    parsed = store.parse_key(kind, key)
    for row in conn.execute(
        "SELECT * FROM junk_candidate WHERE series_key = %s AND kind = %s ORDER BY id", (decisions.series_key, kind)
    ):
        stored = store.parse_key(kind, row["key"])
        if store.matches(stored, parsed) and decisions.decide(kind, stored) is None:
            return row
    return None


def join(conn: db.Connection, series_key: str, picks: Iterable[Candidate], at: str) -> list[int]:
    """Join each pick to its matching candidate, or open one, inside the caller's transaction.

    A new candidate opens in the folder its images were written to (p["folder"]). Gone for a pick that found a candidate
    when its images were not written and now finds none. Returns the ids.
    """
    ids: list[int] = []
    decisions = store.Decisions(conn, series_key)
    for p in picks:
        row = find(conn, decisions, p["kind"], p["key"])
        if row is None:
            if "folder" not in p:
                msg = f"junk candidate for {p['kind']} {p['key'][:40]} was cleaned up meanwhile"
                raise Gone(msg)
            new = db.one_row(
                conn,
                """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (series_key, p["kind"], p["key"], p["reason"], p["folder"], at),
            )
            ids.append(new[0])
        else:
            ids.append(row["id"])
    return sorted(set(ids))


def stuck(row: db.Row, limit: int) -> bool:
    """Return whether a candidate row is stuck: no verdict after `limit` reviews or more."""
    return row["verdict"] is None and row["reviews"] >= limit


# The scans of the not-done junk-stage chapters: a candidate one of them waits on is waited on.
WAITING_SCANS = sql.SQL(
    "junk_scan js JOIN chapter c ON c.id = js.chapter_id WHERE c.stage = 'junk' AND c.status != 'done'"
)


def covering(decisions: Callable[[str], store.Decisions], row: db.Row) -> db.Row | None:
    """Return the decision that covers a candidate row, or None (is_open's second half)."""
    return decisions(row["series_key"]).decide(row["kind"], store.parse_key(row["kind"], row["key"]))


def _undecided(decisions: Callable[[str], store.Decisions], row: db.Row) -> bool:
    return covering(decisions, row) is None


def is_open(conn: db.Connection, row: db.Row, decisions: Callable[[str], store.Decisions] | None = None) -> bool:
    """Return whether a candidate row is open: a not-done junk-stage chapter waits on it, no decision covers it.

    decisions: a store.by_series lookup to share across calls.
    """
    waiting = conn.execute(
        sql.SQL("SELECT 1 FROM {} AND js.candidates::jsonb @> to_jsonb(%s::bigint) LIMIT 1").format(WAITING_SCANS),
        (row["id"],),
    ).fetchone()
    return waiting is not None and _undecided(decisions or store.by_series(conn), row)


def open_rows(conn: db.Connection) -> list[db.Row]:
    """Return the open candidate rows, fewest reviews first, in a fixed number of queries whatever their count.

    Among those, the candidates a waiting chapter that does not replace an adopted file shows first (SPEC "State"),
    then the oldest.
    """
    rows = conn.execute(
        sql.SQL("""SELECT k.* FROM junk_candidate k ORDER BY k.reviews, NOT EXISTS (
            SELECT 1 FROM junk_scan js JOIN chapter c ON c.id = js.chapter_id
            WHERE c.stage = 'junk' AND c.status != 'done' AND js.candidates::jsonb @> to_jsonb(k.id)
                AND NOT {adopted}), k.id""").format(adopted=workflow.REPLACES_ADOPTED.format(c=sql.Identifier("c")))
    ).fetchall()
    query = sql.SQL("SELECT DISTINCT jsonb_array_elements(js.candidates::jsonb)::bigint FROM {}").format(WAITING_SCANS)
    waited_on = {r[0] for r in conn.execute(query)}
    waiting = [r for r in rows if r["id"] in waited_on]
    decisions = store.decisions_of(conn, {r["series_key"] for r in waiting})
    return [r for r in waiting if _undecided(decisions, r)]


def cleanup(cfg: config.Config, conn: db.Connection) -> int:
    """Clean up the candidates for junk review (SPEC "Junk decisions").

    In 1 transaction: a candidate found not open is stamped closed (closed_at, when not stamped yet), one found open
    again loses its stamp, and one stamped CLOSED_KEEP ago or more is deleted; a candidate listed by a review not yet
    ended is left alone. Then the deleted rows' folders are removed, and a folder no row names once it is ORPHAN_KEEP_S
    old (a machine may have written it and not yet recorded its row). Returns how many candidates were deleted.
    """
    with db.transaction(conn):
        now = db.now(conn)
        cutoff = (datetime.fromisoformat(now) - CLOSED_KEEP).isoformat(timespec="seconds")
        decisions = store.by_series(conn)
        stamp: list[tuple[str, int]]
        unstamp: list[tuple[int]]
        gone: list[db.Row]
        stamp, unstamp, gone = [], [], []
        for row in conn.execute("SELECT * FROM junk_candidate").fetchall():
            if row["review_open"]:
                continue
            if is_open(conn, row, decisions):
                if row["closed_at"] is not None:
                    unstamp.append((row["id"],))
            elif row["closed_at"] is None:
                stamp.append((now, row["id"]))
            elif row["closed_at"] <= cutoff:
                gone.append(row)
        db.executemany(conn, "UPDATE junk_candidate SET closed_at = %s WHERE id = %s", stamp)
        db.executemany(conn, "UPDATE junk_candidate SET closed_at = NULL WHERE id = %s", unstamp)
        db.executemany(conn, "DELETE FROM junk_candidate WHERE id = %s", [(row["id"],) for row in gone])
        named = {r[0] for r in conn.execute("SELECT folder FROM junk_candidate")}
    root = config.abs_path(cfg["paths"]["candidates"])
    keep_decided(cfg, conn)  # a decision's images move to decided/ before the gone rows' folders are removed
    for row in gone:
        shutil.rmtree(root / row["folder"], ignore_errors=True)
    _remove_orphans(root, named)
    return len(gone)


def _remove_orphans(root: Path, named: Container[str]) -> None:
    """Remove each folder in the candidates folder `root` that no row names (`named`) once it is ORPHAN_KEEP_S old.

    A machine may have written it and not yet recorded its row.
    """
    if root.is_dir():
        old = time.time() - ORPHAN_KEEP_S
        for entry in root.iterdir():
            if entry.name == DECIDED:
                continue  # decisions' images: never swept
            if entry.is_dir() and entry.name not in named and entry.stat().st_mtime < old:
                shutil.rmtree(entry)


# ---------------------------------------------------------------- images


def _fit(im: Image.Image, width: int = UNIT_WIDTH) -> Image.Image:
    if im.width <= width:
        return im
    return im.resize((width, max(1, round(im.height * width / im.width))))


def write_images(
    cfg: config.Config, name: str, unit_im: Image.Image, context_im: Image.Image, info: CandidateInfo
) -> Path:
    """Write a candidate's folder: unit.png, context.png, info.json (reason, OCR text, chapters).

    Replaces an older folder of the same name whole.
    """
    dest = folder(cfg, name)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    images.save_png(_fit(unit_im), dest / "unit.png")
    images.save_png(_fit(context_im), dest / "context.png")
    (dest / "info.json").write_bytes(json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"))
    return dest


def read_info(dest: Path) -> CandidateInfo:
    """Return a candidate folder's info.json (write_images'). Unchecked."""
    return json.loads((dest / "info.json").read_bytes())


DECIDED = "decided"  # the folder in paths.candidates of the images decisions keep (never swept)


def kept_images(cfg: config.Config, name: str | None) -> str:
    """Return where a decision's kept images are: decided/<name>, else the candidate folder, else why there are none."""
    if name is None:
        return "no images kept"
    root = config.abs_path(cfg["paths"]["candidates"])
    for where_ in (root / DECIDED / name, root / name):
        if where_.is_dir():
            return f"{where_ / 'unit.png'}\t{where_ / 'context.png'}"
    return f"images missing: {name}"


def keep_decided(cfg: config.Config, conn: db.Connection) -> int:
    """Move every top-level folder a decision names and no candidate does into decided/ (its images); count them.

    A rename on the share; a folder whose copy is in decided/ already (a move cut short) is removed. Run by junk
    review's cleanup and by state reset, before either removes a folder.
    """
    root = config.abs_path(cfg["paths"]["candidates"])
    if not root.is_dir():
        return 0
    folders = [e.name for e in root.iterdir() if e.is_dir() and e.name != DECIDED]
    named = [  # a folder a candidate row still names stays: an act may reopen that candidate
        r[0]
        for r in conn.execute(
            """SELECT DISTINCT d.images FROM junk_decision d WHERE d.images = ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM junk_candidate k WHERE k.folder = d.images)""",
            (folders,),
        )
    ]
    decided = root / DECIDED
    if named:
        decided.mkdir(exist_ok=True)
    for name in named:
        if (decided / name).exists():
            shutil.rmtree(root / name)
        else:
            (root / name).rename(decided / name)
    return len(named)
