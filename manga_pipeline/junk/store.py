"""The junk decision store (SPEC "Junk decisions", decisions): lookup, the near rule and the 1 upsert.

Lookup of the decisions that apply to a series, the near rule, and the 1 upsert every writer uses (import, the
baseline's record rows, `junk decide`).

A series key is the series' rules key (series.rules_key: the name of the first folder it had); GLOBAL ('') marks a
decision of every series. A key is a unit's signature text (images.sig_to_str), for a frame frames.key_text, for a logo
marks.Template.key_text.

An unsignable key (a signature whose every tile is a uniform image's) would match any blank or flat rows: a unit
with one matches no decision, and no decision is written on one.

Every lookup reads the store index (index): every active decision, each key parsed, cached for the process under the
store's version (database id, largest decision id, largest act id) and read again when the version moves. Every writer
moves it: a decision is inserted (a new id), or retired or restored by an act (a new act id); write transactions never
overlap (db.transaction). A script or schema step that changes junk_decision rows without a new id or act runs while
no part runs (lock.IDLE exclusive), as scripts/mark_bundle_rows.py does, or a running process keeps its old index.
"""

import math
from collections.abc import Callable, Iterable, Sequence
from typing import NamedTuple, NotRequired, TypedDict, TypeIs

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
NEAR_KINDS = ("block", "band", "probe")  # the junk decisions the near rule compares against
# The index's aspect bins: 12 % wide (log scale), so 2 aspects the live rule (3 %) or the near rule (10 %) compares lie
# in 1 bin or 2 adjacent ones: a query reads its own bin and both neighbours.
ASPECT_BIN = math.log(1.12)
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
    credit (junk.chains). The bundle mark (default unset): a keep so marked protects no gap (Decisions.judged_keep).
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
    bundle: NotRequired[bool]  # the bundle mark: made on a member of a many-piece rest-of-card bundle (W5)


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

    The series' keeps, its junk, global keeps, global junk; within each, the pipeline's decisions (claude, human)
    before the imported ones, then newest first. Keeps go first because matching is not transitive: a unit within reach
    of a kept and a junk key stays kept, whichever is newer and whoever made it. Equal rows keep their given order.
    """
    newest = sorted(rows, key=lambda r: r["decided_at"], reverse=True)
    return sorted(newest, key=lambda r: (r["series_key"] == GLOBAL, r["verdict"] != "keep", r["decider"] == "import"))


type Keyed = tuple[db.Row, ParsedKey]  # a decision row and its key parsed (parse_key)
type Version = tuple[str, int | None, int | None]  # the store's version: database id, largest decision id, act id


class _Binned(NamedTuple):
    """A signature decision as the index bins it: its row, signature, series key and verdict (each read once)."""

    row: db.Row
    sig: images.Signature
    series_key: str
    verdict: str


def _aspect_bin(aspect: float) -> int:
    return math.floor(math.log(aspect) / ASPECT_BIN)


def _ordered(rows: list[Keyed]) -> list[Keyed]:
    """Return keyed rows in lookup order (in_lookup_order)."""
    parsed = {id(row): key for row, key in rows}
    return [(row, parsed[id(row)]) for row in in_lookup_order(row for row, _ in rows)]


class Index:
    """Every active decision, each key parsed (index_of): what every Decisions is built from.

    Per series key (GLOBAL included): its rows but cuts, and its cut rows apart, oldest first; the signature rows
    (SIGNATURE_KINDS) binned by (kind, tile count, aspect bin) for the near rule.
    """

    def __init__(self, rows: Iterable[db.Row]) -> None:
        """File active decision rows, each key parsed (ValueError for one that does not parse)."""
        self._own: dict[str, list[Keyed]] = {}
        self._cuts: dict[str, list[db.Row]] = {}
        self._bins: dict[tuple[str, int, int], list[_Binned]] = {}
        self._ordered: dict[str, list[Keyed]] = {}
        for row in sorted(rows, key=lambda r: r["id"], reverse=True):  # newest id first: lookup order's tie order
            key = parse_key(row["kind"], row["key"])
            if row["kind"] == "cut":
                self._cuts.setdefault(row["series_key"], []).append(row)
                continue
            self._own.setdefault(row["series_key"], []).append((row, key))
            if row["kind"] in SIGNATURE_KINDS and is_signature(key):
                tiles, aspect = key
                entry = _Binned(row, key, row["series_key"], row["verdict"])
                self._bins.setdefault((row["kind"], len(tiles), _aspect_bin(aspect)), []).append(entry)
        for cut_rows in self._cuts.values():
            cut_rows.sort(key=lambda r: (r["decided_at"], r["id"]))

    def ordered(self, key: str) -> list[Keyed]:
        """Return the rows but cuts of series key `key` and the global ones, in lookup order (built once per key)."""
        if key not in self._ordered:
            own = self._own.get(key, [])
            self._ordered[key] = _ordered(own if key == GLOBAL else [*own, *self._own.get(GLOBAL, [])])
        return self._ordered[key]

    def cuts(self, key: str) -> list[db.Row]:
        """Return the active cut decisions of series key `key`, oldest first (decided_at, then id)."""
        return self._cuts.get(key, [])

    def binned(self, kinds: Iterable[str], tiles: int, aspect: float) -> list[_Binned]:
        """Return the signature rows of these kinds with `tiles` tiles in aspect's bin and its 2 neighbours."""
        b = _aspect_bin(aspect)
        return [e for kind in kinds for n in (b - 1, b, b + 1) for e in self._bins.get((kind, tiles, n), [])]


def index_of(rows: Iterable[db.Row]) -> Index:
    """Return the index of active decision rows, a pure function of them; ValueError for a key that does not parse."""
    return Index(rows)


VERSION = sql.SQL("""SELECT (SELECT value FROM meta WHERE key = 'database_id'), (SELECT max(id) FROM junk_decision),
    (SELECT max(id) FROM junk_act)""")
_cached: list[tuple[Version, Index]] = []  # the process's store index under its version (index), at most 1


def version(conn: db.Connection) -> Version:
    """Return the store's version: the database id, the largest decision id and the largest act id (None: none)."""
    db_id, decision, act = db.one_row(conn, VERSION)
    return db_id, decision, act


def index(conn: db.Connection) -> Index:
    """Return the store index, read again only when the store's version moved since this process last read it.

    Inside the caller's transaction the version and the rows are read on it (the caller writes under the write lock or
    reads 1 snapshot, and sees its own writes); outside one, in a repeatable-read transaction of its own, so the version
    names exactly the rows read. Rows read in a transaction that then rolls back stay cached under its version: ids are
    never reused, so the next committed write moves the version past them.
    """
    if db.in_transaction(conn):
        return _read(conn)
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return _read(conn)


def _read(conn: db.Connection) -> Index:
    now = version(conn)
    if _cached and _cached[0][0] == now:
        return _cached[0][1]
    built = index_of(conn.execute(sql.SQL("SELECT * FROM junk_decision WHERE {}").format(ACTIVE)).fetchall())
    _cached[:] = [(now, built)]
    return built


def by_series(conn: db.Connection) -> Callable[[str], "Decisions"]:
    """Return a lookup of series key -> its Decisions, all over 1 read of the store index (for loops over many)."""
    idx = index(conn)

    def get(key: str) -> Decisions:
        return Decisions.of_rows(idx, key, [])

    return get


def decisions_of(conn: db.Connection, keys: Iterable[str]) -> Callable[[str], "Decisions"]:
    """Return a lookup of series key -> its Decisions for these keys, each built now over 1 read of the store index."""
    idx = index(conn)
    cache = {k: Decisions.of_rows(idx, k, []) for k in set(keys)}

    def get(key: str) -> Decisions:
        return cache[key]

    return get


def _near_cap(unit_kind: str, *, own: bool) -> int:
    """Return the near rule's cap: a probe unit's, else a decision's of the unit's series or global (own), else."""
    if unit_kind == "probe":
        return images.NEAR_PROBE
    return images.NEAR_THRESHOLD if own else images.NEAR_OTHER_SERIES


class Decisions:
    """The decisions that apply to 1 series (its own and the global ones), in lookup order (in_lookup_order)."""

    def __init__(self, conn: db.Connection, key: str) -> None:
        """Return the Decisions of series key `key` over the store index."""
        self._set(index(conn), key, [])

    @classmethod
    def of_rows(cls, idx: Index, key: str, layered: list[Keyed]) -> "Decisions":
        """Return the Decisions of series key `key` over index idx, with rows of the series layered on top.

        layered: rows not in the store (junk cut's proposed decision), each key parsed; a layered cut row is in cuts().
        """
        self = cls.__new__(cls)
        self._set(idx, key, layered)
        return self

    def _set(self, idx: Index, key: str, layered: list[Keyed]) -> None:
        self.series_key = key
        self._index = idx
        self._layered = [(row, k) for row, k in layered if row["kind"] != "cut"]
        self._cuts = idx.cuts(key)
        self._rows = idx.ordered(key)
        if layered:
            added = [row for row, _ in layered if row["kind"] == "cut"]
            self._cuts = sorted([*self._cuts, *added], key=lambda r: (r["decided_at"], r["id"]))
            self._rows = _ordered([*self._layered, *self._rows])  # layered first: newest on a tie

    def cuts(self) -> list[db.Row]:
        """Return the series' active cut decisions, oldest first (decided_at, then id): the cut pool (junk.anchored)."""
        return list(self._cuts)

    def decide(self, kind: str, key: ParsedKey) -> db.Row | None:
        """Return the deciding row for a unit of `kind` with parsed key `key`, or None (undecided)."""
        if is_signature(key) and images.unsignable(key):
            return None
        kinds = DECIDING[kind]
        for row, stored in self._rows:
            if row["kind"] in kinds and matches(stored, key):
                return row
        return None

    def judged_keep(self, sig: images.Signature) -> bool:
        """Return whether a tail piece is judged kept (W5, the bulk converter's Library.judged_keep).

        Some active piece keep with no bundle mark matches it, the series' or global: every matching row counts, not
        only the one that decides it. An unsignable signature is judged by none, as decide decides it by none.
        """
        if images.unsignable(sig):
            return False
        return any(
            row["kind"] == "piece" and row["verdict"] == "keep" and not row["bundle"] and matches(stored, sig)
            for row, stored in self._rows
        )

    def near(self, sig: images.Signature, unit_kind: str) -> tuple[db.Row, int, float] | None:
        """Return (row, distance, aspect difference) of the junk decision nearest to a strip unit's signature, or None.

        The junk decision of NEAR_KINDS, of any series or global, nearest to signature `sig` of a unit of `unit_kind`:
        same tile count, aspect difference within NEAR_ASPECT_TOLERANCE, every tile within the cap (_near_cap: 8 for
        the series' and global decisions, 6 for another series', 4 for a probe unit); smallest distance, then aspect
        difference, then the oldest. None when none is that near, when such a decision of the series or global matches
        `sig` at the live rule (it decides; near is for the undecided), or when `sig` is unsignable.
        """
        tiles, aspect = sig
        if images.unsignable(sig):
            return None
        found = self._index.binned(NEAR_KINDS, len(tiles), aspect) + [
            _Binned(row, k, row["series_key"], row["verdict"])
            for row, k in self._layered
            if row["kind"] in NEAR_KINDS and is_signature(k) and len(k[0]) == len(tiles)
        ]
        best: tuple[int, float, str, int] | None = None
        hit: tuple[db.Row, int, float] | None = None
        for e in found:
            if e.verdict != "junk":
                continue
            own = e.series_key in (self.series_key, GLOBAL)
            if own and images.matches(e.sig, sig):
                return None
            t, a = e.sig
            diff = abs(a - aspect) / max(a, aspect)
            if diff > images.NEAR_ASPECT_TOLERANCE:
                continue
            d = max((x ^ y).bit_count() for x, y in zip(t, tiles, strict=True))
            rank = (d, diff, e.row["decided_at"], e.row["id"])
            if d <= _near_cap(unit_kind, own=own) and (best is None or rank < best):
                best, hit = rank, (e.row, d, diff)
        return hit


def _check(r: Decision) -> None:
    if r["kind"] not in KINDS or r["verdict"] not in ("junk", "keep") or r["decider"] not in DECIDERS:
        msg = f"not a junk decision: {r}"
        raise ValueError(msg)
    try:
        key = parse_key(r["kind"], r["key"])
    except (ValueError, OSError) as e:  # OSError: a logo key's PNG that does not open
        msg = f"a decision key that does not parse as a {r['kind']} key: {r}"
        raise ValueError(msg) from e
    if r["kind"] in SIGNATURE_KINDS and is_signature(key) and not (math.isfinite(key[1]) and key[1] > 0):
        msg = f"a decision key whose aspect is no positive number (the store index bins it by aspect): {r}"
        raise ValueError(msg)
    if unsignable(r["kind"], r["key"]):
        msg = f"a decision on an unsignable key (it would match any blank or flat rows): {r}"
        raise ValueError(msg)
    if type(r.get("bundle", False)) is not bool:
        msg = f"bundle {r.get('bundle')!r}: not a bool: {r}"
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
            (series_key, kind, key, verdict, decider, decided_at, source, images, chain, credit, bundle, written_by)
        VALUES (%(series_key)s, %(kind)s, %(key)s, %(verdict)s, %(decider)s, %(decided_at)s, %(source)s, %(images)s,
            %(chain)s, %(credit)s, %(bundle)s, %(act)s) RETURNING id""",
        {"images": None, "chain": None, "credit": False, "bundle": False, **r, "act": act},
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
        old["chain"] == r.get("chain")
        and old["credit"] == r.get("credit", False)
        and old["bundle"] == r.get("bundle", False)
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
