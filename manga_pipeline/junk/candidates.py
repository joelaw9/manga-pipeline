"""Junk candidates (SPEC "Junk decisions", candidates): which undecided units are candidates, 1 candidate row per key,
the candidates' images, which candidates are open, and the cleanup of closed ones.

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
from datetime import datetime, timedelta
from decimal import Decimal

from manga_pipeline import config, db, images, names, workflow
from manga_pipeline.junk import ocr, store

log = logging.getLogger(__name__)

RECUR_CHAPTERS = 3  # D5.2 (c): a unit matching units of this many chapters of the series is a candidate
FIRST_CHAPTERS = 3  # D5.2 (d): the first chapters of a series offer every unmatched edge block
UNIT_WIDTH = 1000  # px: the unit image's largest width
CONTEXT_PX = 300  # px of strip above and below the unit in its context image
CLOSED_KEEP = timedelta(hours=1)  # a candidate closed this long is deleted with its folder
ORPHAN_KEEP_S = 24 * 3600  # a folder no row names is removed once this old (another machine may be recording it)


class Gone(RuntimeError):
    """A candidate the chapter meant to join was deleted meanwhile: the chapter fails, and its retry writes the
    images anew."""


def folder_name(series_key, kind, key, chapter):
    """The folder of a new candidate: its unit (hashed) and the chapter claim that writes it (workflow.claim_name)."""
    unit = hashlib.sha1(f"{series_key}|{kind}|{key}".encode()).hexdigest()[:16]
    return f"{unit}-{workflow.claim_name(chapter)}"


def folder(cfg, name):
    return config.abs_path(cfg["paths"]["candidates"]) / name


# ---------------------------------------------------------------- seen units and the rules


def seen_rows(series_key, identity, units, at, origin="pipeline"):
    """junk_seen rows of units [(kind, key)] seen in a chapter."""
    return [(series_key, kind, key, identity, at, origin) for kind, key in units]


def write_seen(conn, rows):
    """Record seen units inside the caller's transaction; a unit seen again keeps its first time."""
    db.executemany(
        conn,
        """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        rows,
    )


def chapter_order(identity):
    """Sort key of a chapter identity (names.identity) by season, then number; unnumbered last."""
    _, season, _, number, _ = json.loads(identity)
    return (
        season is None,
        Decimal(season) if season is not None else 0,
        number is None,
        names.number_key(number) if number is not None else (0, ""),
        identity,
    )


def first_identities(conn, series_key, n=FIRST_CHAPTERS):
    """The first n distinct chapter identities of a series by first sighting, chapter order on a tie (every sighting
    imported from the bulk converter has the same time)."""
    rows = conn.execute(
        "SELECT identity, min(first_seen) FROM junk_seen WHERE series_key = %s GROUP BY identity", (series_key,)
    ).fetchall()
    return {r[0] for r in sorted(rows, key=lambda r: (r[1], chapter_order(r[0])))[:n]}


def chapters_showing(conn, series_key, kind, key):
    """How many chapter identities of the series show a unit matching (kind, key)."""
    parsed = store.parse_key(kind, key)
    rows = conn.execute("SELECT key, identity FROM junk_seen WHERE series_key = %s AND kind = %s", (series_key, kind))
    return len({ident for k, ident in rows if store.matches(kind, store.parse_key(kind, k), parsed)})


def strip_reasons(conn, series_key, unit, words):
    """Why a strip unit is a candidate (near, OCR keywords); [] when neither."""
    out = []
    hit = store.near(conn, series_key, images.sig_from_str(unit["sig"]))
    if hit is not None:
        row, d, diff = hit
        out.append(f"near decision {row['id']} ({row['series_key'] or 'global'}) distance {d}, aspect {diff:.1%}")
    if unit.get("text"):
        terms = ocr.text_hits(unit["text"], unit["end"] - unit["start"], words)
        if terms:
            out.append(f'text {", ".join(repr(t) for t in terms)}: "{ocr.normalize(unit["text"])[:160]}"')
    return out


def select(conn, series_key, identity, undecided, words):
    """The candidates among a chapter's undecided units: [{"kind", "key", "reason", "unit"}]. Strip edge blocks: the
    first probe from the chapter edge that is near or has a keyword is offered instead of its block; else the block
    when it is near, has a keyword, recurs, is the rest of a block whose edge piece is junk, or its chapter is among the
    first of the series. Tail
    pieces always. Pages, frames and bands: their per-chapter reasons, or recurrence. Logos always; a logo variant
    offers the first of its representatives' keys that an existing candidate matches, else its first. Never a unit
    with an unsignable key (no decision could be written on it), a block's probe included."""
    first = first_identities(conn, series_key)
    decisions = store.Decisions(conn, series_key)
    out = []
    for u in undecided:
        kind = u["kind"]
        key = u.get("key") or u.get("sig")
        reasons = []
        if kind != "block" and store.unsignable(kind, key):
            continue
        if kind == "piece":
            reasons = ["tail piece after a credit band"]
        elif kind == "block":
            probes = [p for p in u["probes"] if not store.unsignable("probe", p["sig"])]
            flagged = next(
                ((p, why) for p in probes for why in [strip_reasons(conn, series_key, p, words)] if why), None
            )
            if flagged is not None:
                probe, why = flagged
                unit = {**probe, "where": u["where"]}
                out.append({"kind": "probe", "key": probe["sig"], "unit": unit, "reason": "; ".join(why)})
                continue
            if store.unsignable(kind, key):
                continue
            reasons = strip_reasons(conn, series_key, u, words)
            if u.get("rest"):
                reasons.append("the rest of a block whose edge piece is junk")
            if identity in first:
                reasons.append("a first chapter of the series")
        elif kind == "logo":
            key = next((k for k in u["keys"] if find(conn, decisions, kind, k) is not None), u["keys"][0])
            out.append({"kind": kind, "key": key, "unit": u, "reason": "; ".join(u["reasons"])})
            continue
        else:
            reasons = list(u.get("reasons", []))
        if kind != "piece":
            n = chapters_showing(conn, series_key, "block" if kind == "block" else kind, key)
            if n >= RECUR_CHAPTERS:
                reasons.append(f"seen in {n} chapters")
        if reasons:
            out.append({"kind": kind, "key": key, "unit": u, "reason": "; ".join(reasons)})
    return out


# ---------------------------------------------------------------- candidate rows


def find(conn, decisions, kind, key):
    """The candidate row of the series (decisions: its store.Decisions) and kind whose key matches `key` and that no
    decision covers, or None. A covered candidate is closed: matching is not transitive, so a unit it matches may
    still be undecided, and that unit gets a candidate of its own."""
    parsed = store.parse_key(kind, key)
    for row in conn.execute(
        "SELECT * FROM junk_candidate WHERE series_key = %s AND kind = %s ORDER BY id", (decisions.series_key, kind)
    ):
        stored = store.parse_key(kind, row["key"])
        if store.matches(kind, stored, parsed) and decisions.decide(kind, stored) is None:
            return row
    return None


def join(conn, series_key, picks, at):
    """Inside the caller's transaction: each pick joins its matching candidate or opens one in the folder its images
    were written to (p["folder"]). Gone for a pick that found a candidate when its images were not written and now
    finds none. Returns the ids."""
    ids = []
    decisions = store.Decisions(conn, series_key)
    for p in picks:
        row = find(conn, decisions, p["kind"], p["key"])
        if row is None:
            if "folder" not in p:
                raise Gone(f"junk candidate for {p['kind']} {p['key'][:40]} was cleaned up meanwhile")
            new = conn.execute(
                """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (series_key, p["kind"], p["key"], p["reason"], p["folder"], at),
            )
            ids.append(new.fetchone()[0])
        else:
            ids.append(row["id"])
    return sorted(set(ids))


def stuck(row, limit):
    return row["verdict"] is None and row["reviews"] >= limit


def is_open(conn, row, decisions=None):
    """Whether a candidate row is open: a not-done junk-stage chapter waits on it, no decision covers it.
    decisions: a store.by_series lookup to share across calls."""
    waiting = conn.execute(
        """SELECT 1 FROM junk_scan js JOIN chapter c ON c.id = js.chapter_id
        WHERE c.stage = 'junk' AND c.status != 'done' AND js.candidates::jsonb @> to_jsonb(%s::bigint) LIMIT 1""",
        (row["id"],),
    ).fetchone()
    if waiting is None:
        return False
    lookup = (decisions or store.by_series(conn))(row["series_key"])
    return lookup.decide(row["kind"], store.parse_key(row["kind"], row["key"])) is None


def open_rows(conn):
    decisions = store.by_series(conn)
    rows = conn.execute("SELECT * FROM junk_candidate ORDER BY reviews, id").fetchall()
    return [r for r in rows if is_open(conn, r, decisions)]


def cleanup(cfg, conn):
    """junk review's clean-up (SPEC "Junk decisions"), in 1 transaction: a candidate found not open is stamped closed
    (closed_at, when not stamped yet), one found open again loses its stamp, and one stamped CLOSED_KEEP ago or more
    is deleted; a candidate listed by a review not yet ended is left alone. Then the deleted rows' folders are
    removed, and a folder no row names once it is ORPHAN_KEEP_S old (a machine may have written it and not yet
    recorded its row). Returns how many candidates were deleted."""
    with db.transaction(conn):
        now = db.now(conn)
        cutoff = (datetime.fromisoformat(now) - CLOSED_KEEP).isoformat(timespec="seconds")
        decisions = store.by_series(conn)
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
    for row in gone:
        shutil.rmtree(root / row["folder"], ignore_errors=True)
    if root.is_dir():
        old = time.time() - ORPHAN_KEEP_S
        for entry in root.iterdir():
            if entry.is_dir() and entry.name not in named and entry.stat().st_mtime < old:
                shutil.rmtree(entry)
    return len(gone)


# ---------------------------------------------------------------- images


def _fit(im, width=UNIT_WIDTH):
    if im.width <= width:
        return im
    return im.resize((width, max(1, round(im.height * width / im.width))))


def write_images(cfg, name, unit_im, context_im, info):
    """Write a candidate's folder: unit.png, context.png, info.json (reason, OCR text, chapters). Replaces an older
    folder of the same name whole."""
    dest = folder(cfg, name)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    images.save_png(_fit(unit_im), dest / "unit.png")
    images.save_png(_fit(context_im), dest / "context.png")
    (dest / "info.json").write_bytes(json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"))
    return dest
