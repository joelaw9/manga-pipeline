"""The junk decision store (SPEC "Junk decisions", decisions): lookup, the near rule and the 1 upsert.

Lookup of the decisions that apply to a series, the near rule, and the 1 upsert every writer uses (import, the
baseline's record rows, `junk decide`).

A series key is the series' rules key (series.rules_key: the name of the first folder it had); GLOBAL ('') marks a
decision of every series. A key is a unit's signature text (images.sig_to_str), for a frame frames.key_text, for a logo
marks.Template.key_text.

An unsignable key (a signature whose every tile is a uniform image's) would match any blank or flat rows: a unit
with one matches no decision, and no decision is written on one.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import NotRequired, TypedDict, TypeIs

import numpy as np

from manga_pipeline import db, images
from manga_pipeline.db import sql
from manga_pipeline.junk import chains, frames, marks

GLOBAL = ""
KINDS = ("block", "probe", "piece", "page", "band", "frame", "logo", "cut")  # cut: a cut decision (junk.chains)
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
# An active decision decides units; a superseded or retired one is history (SPEC "Junk decisions", decisions).
ACTIVE = sql.SQL("superseded_by IS NULL AND retired_by IS NULL")


def is_active(row: db.Row) -> bool:
    """Return whether a decision row is active (ACTIVE, for a row read already)."""
    return row["superseded_by"] is None and row["retired_by"] is None


type ParsedKey = images.Signature | frames.Frame | marks.Template  # a key's text parsed for its kind (parse_key)
type DecideUnit = Callable[[str, str], db.Row | None]  # (kind, key text) -> the deciding junk_decision row, or None


class Decision(TypedDict):
    """A junk decision as upsert writes it: its series key, kind, key text, verdict, decider, time and source.

    A block or probe junk decision may carry its card's extent (chain), a cut decision carries its chain and may carry
    credit (junk.chains).
    """

    series_key: str
    kind: str
    key: str
    verdict: str
    decider: str
    decided_at: str
    source: str | None
    images: NotRequired[str | None]  # the candidate folder of a decision made from a candidate
    chain: NotRequired[str | None]  # canonical chain text (junk.chains), None for none
    credit: NotRequired[bool]  # a cut decision whose tail ranges are tail bands


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


def matches(stored: ParsedKey, unit: ParsedKey) -> bool | np.bool_:
    """Whether a unit's parsed key matches a stored one (a logo's match is not symmetric: stored first).

    A key's kind is its parsed type (parse_key): a frame, a logo template or a signature; keys of 2 kinds never match.
    """
    if isinstance(stored, frames.Frame):
        return isinstance(unit, frames.Frame) and frames.matches(stored, unit)
    if isinstance(stored, marks.Template):
        return isinstance(unit, marks.Template) and marks.same_variant(stored, unit)
    return is_signature(unit) and images.matches(stored, unit)


def is_signature(key: ParsedKey) -> TypeIs[images.Signature]:
    """Return whether a parsed key is an image signature: not a frame's, not a logo template."""
    return not isinstance(key, (frames.Frame, marks.Template))


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


def decisions_of(conn: db.Connection, keys: Iterable[str]) -> Callable[[str], "Decisions"]:
    """Return a lookup of series key -> its Decisions for these keys, read in 1 query (the global rows parsed once)."""
    wanted = sorted(set(keys))
    rows = conn.execute(
        sql.SQL("SELECT * FROM junk_decision WHERE series_key = ANY(%s) AND {} ORDER BY id DESC").format(ACTIVE),
        ([*wanted, GLOBAL],),
    ).fetchall()
    parsed = [(row, parse_key(row["kind"], row["key"])) for row in rows]
    cache = {k: Decisions.of_rows(k, [p for p in parsed if p[0]["series_key"] in (k, GLOBAL)]) for k in wanted}

    def get(key: str) -> Decisions:
        return cache[key]

    return get


class Decisions:
    """The decisions that apply to 1 series (its own and the global ones), in lookup order (in_lookup_order)."""

    def __init__(self, conn: db.Connection, key: str) -> None:
        """Read the active decisions of series key `key` and the global ones, each key parsed, in lookup order."""
        rows = conn.execute(
            sql.SQL("SELECT * FROM junk_decision WHERE series_key IN (%s, %s) AND {} ORDER BY id DESC").format(ACTIVE),
            (key, GLOBAL),
        ).fetchall()
        self._set(key, [(row, parse_key(row["kind"], row["key"])) for row in rows])

    @classmethod
    def of_rows(cls, key: str, parsed: list[tuple[db.Row, ParsedKey]]) -> "Decisions":
        """Return the Decisions of series key `key` from rows read and parsed already (newest id first)."""
        self = cls.__new__(cls)
        self._set(key, parsed)
        return self

    def _set(self, key: str, parsed: list[tuple[db.Row, ParsedKey]]) -> None:
        keys = {row["id"]: k for row, k in parsed}
        self.series_key = key
        self._rows = [(row, keys[row["id"]]) for row in in_lookup_order(row for row, _ in parsed)]

    def decide(self, kind: str, key: ParsedKey) -> db.Row | None:
        """Return the deciding row for a unit of `kind` with parsed key `key`, or None (undecided)."""
        if is_signature(key) and images.unsignable(key):
            return None
        kinds = DECIDING[kind]
        for row, stored in self._rows:
            if row["kind"] in kinds and matches(stored, key):
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
        sql.SQL("""SELECT * FROM junk_decision WHERE verdict = 'junk' AND kind IN ({kinds}) AND {active}
        ORDER BY decided_at, id""").format(
            kinds=sql.SQL(", ").join([sql.Placeholder()] * len(NEAR_KINDS)), active=ACTIVE
        ),
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


def _check(r: Decision) -> None:
    if r["kind"] not in KINDS or r["verdict"] not in ("junk", "keep") or r["decider"] not in DECIDERS:
        msg = f"not a junk decision: {r}"
        raise ValueError(msg)
    if unsignable(r["kind"], r["key"]):
        msg = f"a decision on an unsignable key (it would match any blank or flat rows): {r}"
        raise ValueError(msg)
    _check_chain(r)


def _check_chain(r: Decision) -> None:
    """Which decisions carry a chain and credit (junk.chains; the bulk converter's row shapes); ValueError otherwise.

    A cut decision: series-scoped, junk, with its chain, credit allowed. A series block or probe junk decision: a chain
    or none, no credit. Any other decision: neither (no global rows carry one: none are made).
    """
    chain, credit, scoped = r.get("chain"), r.get("credit", False), r["series_key"] != GLOBAL
    if type(credit) is not bool:
        msg = f"credit {credit!r}: not a bool: {r}"
        raise ValueError(msg)
    if r["kind"] == "cut":
        if r["verdict"] != "junk" or not scoped or chain is None:
            msg = f"a cut decision is series-scoped, junk, and carries a chain: {r}"
            raise ValueError(msg)
        chains.check_cut(r["key"], chain)
        return
    if credit:
        msg = f"credit on a {r['kind']} decision (credit: cut decisions only): {r}"
        raise ValueError(msg)
    if chain is None:
        return
    if r["kind"] not in chains.UNIT_KINDS or r["verdict"] != "junk" or not scoped:
        msg = f"a chain on a decision that is no series block or probe junk decision: {r}"
        raise ValueError(msg)
    chains.check_unit(chain)


def _insert(conn: db.Connection, r: Decision, act: int | None = None) -> int:
    """Insert decision r as active, written by act (None: not by an act); return its id."""
    return db.one_row(
        conn,
        """INSERT INTO junk_decision
            (series_key, kind, key, verdict, decider, decided_at, source, images, chain, credit, written_by)
        VALUES (%(series_key)s, %(kind)s, %(key)s, %(verdict)s, %(decider)s, %(decided_at)s, %(source)s, %(images)s,
            %(chain)s, %(credit)s, %(act)s) RETURNING id""",
        {"images": None, "chain": None, "credit": False, **r, "act": act},
    )[0]


def _supersede(conn: db.Connection, old: db.Row, r: Decision) -> None:
    """Write r in place of the active row old, which is kept as history superseded by it."""
    conn.execute("UPDATE junk_decision SET superseded_by = id WHERE id = %s", (old["id"],))  # inactive first
    new = _insert(conn, r)
    conn.execute("UPDATE junk_decision SET superseded_by = %s WHERE id = %s", (new, old["id"]))


def _newest(conn: db.Connection, series_key_: str, kind: str, key: str, where: sql.Composable) -> db.Row | None:
    """Return the newest row on series key series_key_, kind and key that meets `where`."""
    query = sql.SQL("""SELECT * FROM junk_decision WHERE series_key = %s AND kind = %s AND md5(key) = md5(%s)
        AND {} ORDER BY id DESC LIMIT 1""").format(where)
    return conn.execute(query, (series_key_, kind, key)).fetchone()


def _on_key(conn: db.Connection, r: Decision, where: sql.Composable) -> db.Row | None:
    return _newest(conn, r["series_key"], r["kind"], r["key"], where)


def _keeps(old: db.Row, r: Decision) -> bool:
    """Whether the row old on r's key stays as it is (SPEC "Junk decisions", import)."""
    if old["superseded_by"] is not None:
        return True  # an import's history row: the newest on the key is the import that superseded it
    if r["decider"] != "import":
        return old["decider"] != "import"  # a pipeline decision supersedes only an import
    if old["decider"] != "import" or old["written_by"] is not None or old["retired_by"] is not None:
        return True  # the pipeline's own, or a key an act wrote or retired
    same = all(old[c] == r[c] for c in ("verdict", "decider", "decided_at", "source")) and (
        old["chain"] == r.get("chain") and old["credit"] == r.get("credit", False)
    )
    return same or r["decided_at"] < old["decided_at"]


def upsert(conn: db.Connection, rows: Iterable[Decision]) -> dict[str, int]:
    """Write decisions (series key, kind, key, verdict, decider, time, source, images) in the caller's transaction.

    By the new row's decider (SPEC "Junk decisions", import). An import: inserted on a key with no row; on a key whose
    newest row is the pipeline's or an act's (written or retired by 1), unchanged; over an active import it is at least
    as new as and not equal to, the old one superseded by it; else unchanged. A pipeline decision (claude, human):
    inserted on a key with no active row (one an undone act left inactive included); over an active import, the import
    superseded by it; over an active pipeline row, unchanged. ValueError for a row that is no junk decision, or on an
    unsignable key. Returns {"inserted", "updated", "unchanged"} counts (updated: an older row superseded).
    """
    if not db.in_transaction(conn):
        msg = "upsert runs inside the caller's transaction"
        raise RuntimeError(msg)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for r in rows:
        _check(r)
        old = _on_key(conn, r, sql.SQL("TRUE") if r["decider"] == "import" else ACTIVE)
        if old is None:
            _insert(conn, r)
            counts["inserted"] += 1
        elif _keeps(old, r):
            counts["unchanged"] += 1
        else:
            _supersede(conn, old, r)
            counts["updated"] += 1
    return counts


def _log(conn: db.Connection, act: int, ids: Iterable[int], change: str) -> None:
    db.executemany(
        conn, "INSERT INTO junk_act_row (act, decision, change) VALUES (%s, %s, %s)", [(act, i, change) for i in ids]
    )


def new_act(conn: db.Connection, command: str, at: str, undoes: int | None = None) -> int:
    """Record an act of junk redecide or junk undo in the caller's transaction; return its id."""
    return db.one_row(
        conn, "INSERT INTO junk_act (command, at, undoes) VALUES (%s, %s, %s) RETURNING id", (command, at, undoes)
    )[0]


def retire(conn: db.Connection, ids: Iterable[int], act: int) -> None:
    """Retire active decisions by act, logged; ValueError for one that is not active."""
    for i in ids:
        done = conn.execute(
            sql.SQL("UPDATE junk_decision SET retired_by = %s WHERE id = %s AND {}").format(ACTIVE), (act, i)
        ).rowcount
        if done != 1:
            msg = f"decision {i} is not active: it cannot be retired"
            raise ValueError(msg)
        _log(conn, act, [i], "retired")


def write(conn: db.Connection, rows: Iterable[Decision], act: int) -> list[int]:
    """Insert decisions written by act, logged; return their ids. The caller has retired what they replace."""
    ids: list[int] = []
    for r in rows:
        _check(r)
        ids.append(_insert(conn, r, act))
    _log(conn, act, ids, "wrote")
    return ids


def restore(conn: db.Connection, ids: Iterable[int], act: int) -> None:
    """Make decisions an act retired active again, logged (the partial unique index refuses a key now taken)."""
    for i in ids:
        conn.execute("UPDATE junk_decision SET retired_by = NULL WHERE id = %s", (i,))
        _log(conn, act, [i], "restored")


def unknown_series(conn: db.Connection, keys: Sequence[str]) -> list[str]:
    """Return the keys of keys that no series has as its rules key, sorted."""
    known = {r[0] for r in conn.execute("SELECT rules_key FROM series WHERE rules_key = ANY(%s)", (list(keys),))}
    return sorted(set(keys) - known)


def get(conn: db.Connection, decision_id: int) -> db.Row | None:
    """Return decision decision_id's row, any state, or None."""
    return conn.execute("SELECT * FROM junk_decision WHERE id = %s", (decision_id,)).fetchone()


def active_on(conn: db.Connection, series_key_: str, kind: str, key: str) -> db.Row | None:
    """Return the active decision of series key series_key_ on kind and key, or None."""
    return _newest(conn, series_key_, kind, key, ACTIVE)


def act_rows(conn: db.Connection, act: int, change: str) -> list[int]:
    """Return the decisions act changed by change (wrote, retired, restored), in id order."""
    rows = conn.execute(
        "SELECT decision FROM junk_act_row WHERE act = %s AND change = %s ORDER BY decision", (act, change)
    )
    return [r[0] for r in rows]


def history(conn: db.Connection, series_key_: str | None) -> tuple[list[db.Row], dict[int, list[str]]]:
    """Return decisions in id order, active and not: a series' and the global ones, or (None) every one.

    With each decision's act log: "<change> <act>" entries, oldest act first.
    """
    if series_key_ is None:
        rows = conn.execute("SELECT * FROM junk_decision ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM junk_decision WHERE series_key IN (%s, %s) ORDER BY id", (series_key_, GLOBAL)
        ).fetchall()
    return rows, acts_of(conn, [r["id"] for r in rows])


def acts_of(conn: db.Connection, ids: Sequence[int]) -> dict[int, list[str]]:
    """Return the act log of decisions ids: "<change> <act>" entries, oldest act first."""
    acts: dict[int, list[str]] = {}
    log = conn.execute(
        "SELECT decision, act, change FROM junk_act_row WHERE decision = ANY(%s) ORDER BY act, change", (list(ids),)
    )
    for decision, act, change in log:
        acts.setdefault(decision, []).append(f"{change} {act}")
    return acts
