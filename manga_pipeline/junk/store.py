"""The junk decision store (SPEC "Junk decisions", decisions): lookup, the near rule and the 1 upsert.

Lookup of the decisions that apply to a series, the near rule, and the 1 upsert every writer uses (import, the
baseline's record rows, `junk decide`).

A series key is the series' rules key (series.rules_key: the name of the first folder it had); GLOBAL ('') marks a
decision of every series. A key is a unit's signature text (images.sig_to_str), for a frame frames.key_text, for a logo
marks.Template.key_text.

An unsignable key (a signature whose every tile is a uniform image's) would match any blank or flat rows: a unit
with one matches no decision, and no decision is written on one.
"""

from collections.abc import Callable, Iterable
from typing import TypedDict

import numpy as np

from manga_pipeline import db, images
from manga_pipeline.db import sql
from manga_pipeline.junk import frames, marks

GLOBAL = ""
KINDS = ("block", "probe", "piece", "page", "band", "frame", "logo")
# A unit's kind -> the kinds of the decisions that decide it (the bulk converter's junk.UNIT_KINDS).
DECIDING: dict[str, tuple[str, ...]] = {
    "block": ("block", "probe"),
    "probe": ("block", "probe"),
    "piece": ("piece",),
    "page": ("page",),
    "band": ("band",),
    "frame": ("frame",),
    "logo": ("logo",),
}
SIGNATURE_KINDS = ("block", "probe", "piece", "page", "band")  # keyed by an image signature
NEAR_KINDS = ("block", "band", "probe")  # the approved decisions the near rule compares against
DECIDERS = ("import", "claude", "human")

type ParsedKey = images.Signature | frames.Frame | marks.Template  # a key's text parsed for its kind (parse_key)
type DecideUnit = Callable[[str, str], db.Row | None]  # (kind, key text) -> the deciding junk_decision row, or None


class Decision(TypedDict):
    """A junk decision as upsert writes it: its series key, kind, key text, verdict, decider, time and source."""

    series_key: str
    kind: str
    key: str
    verdict: str
    decider: str
    decided_at: str
    source: str | None


def series_key(series: db.Row) -> str:
    """Return the series key of a series row: its rules key, which a later folder does not change."""
    if not series["rules_key"]:
        msg = f"series {series['id']} has no folder yet: it has no series key"
        raise ValueError(msg)
    return series["rules_key"]


def unsignable(kind: str, text: str) -> bool:
    """Whether the key text of a unit of `kind` is an unsignable signature."""
    return kind in SIGNATURE_KINDS and images.unsignable(images.sig_from_str(text))


def parse_key(kind: str, text: str) -> ParsedKey:
    """Return a decision key's text parsed for kind: a frame, a logo template, else a signature."""
    if kind == "frame":
        return frames.from_key(text)
    if kind == "logo":
        return marks.Template.from_key(text)
    return images.sig_from_str(text)


def matches(kind: str, stored: ParsedKey, unit: ParsedKey) -> bool | np.bool_:
    """Whether a unit's parsed key matches a stored one (a logo's match is not symmetric: stored first)."""
    if kind == "frame":
        return frames.matches(_frame(stored), _frame(unit))
    if kind == "logo":
        return marks.same_variant(_template(stored), _template(unit))
    return images.matches(_signature(stored), _signature(unit))


def _frame(key: ParsedKey) -> frames.Frame:
    """Return a frame's parsed key; TypeError for another kind's."""
    if not isinstance(key, frames.Frame):
        msg = f"a frame key parsed as {type(key).__name__}"
        raise TypeError(msg)
    return key


def _template(key: ParsedKey) -> marks.Template:
    """Return a logo's parsed key; TypeError for another kind's."""
    if not isinstance(key, marks.Template):
        msg = f"a logo key parsed as {type(key).__name__}"
        raise TypeError(msg)
    return key


def _signature(key: ParsedKey) -> images.Signature:
    """Return a signature kind's parsed key; TypeError for a frame's or a logo's."""
    if isinstance(key, (frames.Frame, marks.Template)):
        msg = f"a signature key parsed as {type(key).__name__}"
        raise TypeError(msg)
    return key


def in_lookup_order(rows: Iterable[db.Row]) -> list[db.Row]:
    """Return decision rows in lookup order.

    Decisions the pipeline made (claude, human) first, so one wins for every signature it covers, their keeps before
    their junk; then the imported ones in the bulk converter's order: the series' keeps, its junk, global keeps, global
    junk; newest first within each. Keeps go first because matching is not transitive: a unit within reach of a kept and
    a junk key stays kept, whichever is newer (the bulk converter's rule). Equal rows keep their given order.
    """
    newest = sorted(rows, key=lambda r: r["decided_at"], reverse=True)
    return sorted(
        newest,
        key=lambda r: (
            r["decider"] == "import",
            r["decider"] == "import" and r["series_key"] == GLOBAL,
            r["verdict"] != "keep",
        ),
    )


def by_series(conn: db.Connection) -> Callable[[str], "Decisions"]:
    """Return a lookup of series key -> its Decisions, each built once (for loops over many candidates)."""
    cache: dict[str, Decisions] = {}

    def get(key: str) -> Decisions:
        if key not in cache:
            cache[key] = Decisions(conn, key)
        return cache[key]

    return get


class Decisions:
    """The decisions that apply to 1 series (its own and the global ones), in lookup order (in_lookup_order)."""

    def __init__(self, conn: db.Connection, key: str) -> None:
        """Read the decisions of series key `key` and the global ones, each key parsed, in lookup order."""
        rows = in_lookup_order(
            conn.execute(
                "SELECT * FROM junk_decision WHERE series_key IN (%s, %s) ORDER BY id DESC", (key, GLOBAL)
            ).fetchall()
        )
        self.series_key = key
        self._rows = [(row, parse_key(row["kind"], row["key"])) for row in rows]

    def decide(self, kind: str, key: ParsedKey) -> db.Row | None:
        """Return the deciding row for a unit of `kind` with parsed key `key`, or None (undecided)."""
        if kind in SIGNATURE_KINDS and images.unsignable(_signature(key)):
            return None
        kinds = DECIDING[kind]
        for row, stored in self._rows:
            if row["kind"] in kinds and matches(row["kind"], stored, key):
                return row
        return None


def near(
    conn: db.Connection,
    series_key_: str,
    sig: images.Signature,
    threshold: int = images.NEAR_THRESHOLD,
    aspect_tol: float = images.NEAR_ASPECT_TOLERANCE,
) -> tuple[db.Row, int, float] | None:
    """Return (row, distance, aspect difference) of the approved decision nearest to signature `sig`, or None.

    The approved decision of NEAR_KINDS (any series, or global) nearest to signature `sig`: same tile count, every tile
    within `threshold`, aspect difference within `aspect_tol`; smallest distance, then aspect difference, then the
    oldest. None when none is that near, or when such a decision that applies to the series (its own or global) matches
    `sig` at the live rule (it decides; near is for the undecided), or when `sig` is unsignable.
    """
    tiles, aspect = sig
    if images.unsignable(sig):
        return None
    best: tuple[db.Row, int, float] | None = None
    rows = conn.execute(
        sql.SQL("""SELECT * FROM junk_decision WHERE verdict = 'junk' AND kind IN ({kinds})
        ORDER BY decided_at, id""").format(kinds=sql.SQL(", ").join([sql.Placeholder()] * len(NEAR_KINDS))),
        NEAR_KINDS,
    )
    for row in rows:
        t, a = images.sig_from_str(row["key"])
        if len(t) != len(tiles):
            continue
        if row["series_key"] in (series_key_, GLOBAL) and images.matches((t, a), sig):
            return None
        diff = abs(a - aspect) / max(a, aspect)
        if diff > aspect_tol:
            continue
        d = max((x ^ y).bit_count() for x, y in zip(t, tiles, strict=True))
        if d <= threshold and (best is None or (d, diff) < best[1:]):
            best = (row, d, diff)
    return best


def upsert(conn: db.Connection, rows: Iterable[Decision]) -> dict[str, int]:
    """Write decisions (dicts: series_key, kind, key, verdict, decider, decided_at, source) in the caller's transaction.

    A new key is inserted. An existing row is replaced only when its decider is 'import' and the new one is a pipeline
    decision or at least as new; a pipeline-made row ('claude', 'human') is never touched. ValueError for a row that is
    no junk decision, or on an unsignable key. Returns {"inserted", "updated", "unchanged"} counts.
    """
    if not db.in_transaction(conn):
        msg = "upsert runs inside the caller's transaction"
        raise RuntimeError(msg)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for r in rows:
        if r["kind"] not in KINDS or r["verdict"] not in ("junk", "keep") or r["decider"] not in DECIDERS:
            msg = f"not a junk decision: {r}"
            raise ValueError(msg)
        if unsignable(r["kind"], r["key"]):
            msg = f"a decision on an unsignable key (it would match any blank or flat rows): {r}"
            raise ValueError(msg)
        old = conn.execute(
            "SELECT * FROM junk_decision WHERE series_key = %s AND kind = %s AND key = %s",
            (r["series_key"], r["kind"], r["key"]),
        ).fetchone()
        if old is None:
            conn.execute(
                """INSERT INTO junk_decision (series_key, kind, key, verdict, decider, decided_at, source)
                VALUES (%(series_key)s, %(kind)s, %(key)s, %(verdict)s, %(decider)s, %(decided_at)s, %(source)s)""",
                r,
            )
            counts["inserted"] += 1
        elif old["decider"] == "import" and (r["decider"] != "import" or r["decided_at"] >= old["decided_at"]):
            same = all(old[c] == r[c] for c in ("verdict", "decider", "decided_at", "source"))
            if not same:
                conn.execute(
                    """UPDATE junk_decision SET verdict = %(verdict)s, decider = %(decider)s,
                        decided_at = %(decided_at)s, source = %(source)s WHERE id = %(id)s""",
                    {**r, "id": old["id"]},
                )
            counts["unchanged" if same else "updated"] += 1
        else:
            counts["unchanged"] += 1
    return counts
