"""`junk redecide` and `junk undo` (SPEC "Junk decisions", decision commands): every change of a decision is an act.

An act writes decisions and retires others in 1 transaction, logged row by row (store.write, store.retire); `junk undo`
reverses an act exactly: what it wrote is retired, what it retired is active again. No lock; a person's command.
"""

from collections.abc import Sequence
from typing import NamedTuple

from manga_pipeline import db
from manga_pipeline.junk import store, where
from manga_pipeline.junk.review import Refused

VERDICTS = ("junk", "keep")


def _rows(conn: db.Connection, ids: Sequence[int]) -> list[db.Row]:
    """Return the decisions ids, each active; Refused for an unknown or inactive one."""
    rows: list[db.Row] = []
    for i in ids:
        row = store.get(conn, i)
        if row is None:
            msg = f"no junk decision {i}"
            raise Refused(msg)
        if not store.is_active(row):
            msg = f"junk decision {i} is not active (it is history): nothing to re-decide"
            raise Refused(msg)
        rows.append(row)
    return rows


class Step(NamedTuple):
    """What an act does to 1 decision: the series it writes 1 of the new verdict in, and whether it retires it."""

    targets: list[str]
    retire: bool


def _checked(conn: db.Connection, ids: Sequence[int], verdict: str, *, global_only: bool) -> list[db.Row]:
    """Return the decisions ids, active, of the other verdict (and global, when global_only); Refused otherwise."""
    if verdict not in VERDICTS:
        msg = f"verdict must be one of {VERDICTS}, got {verdict!r}"
        raise Refused(msg)
    if len(set(ids)) != len(ids):
        msg = f"a decision is named twice: {list(ids)}"
        raise Refused(msg)
    rows = _rows(conn, ids)
    for row in rows:
        if row["kind"] == "cut":
            msg = f"junk decision {row['id']} is a cut decision: it changes only by junk undo of the act that wrote it"
            raise Refused(msg)
        if row["verdict"] == verdict:
            msg = f"junk decision {row['id']} is {verdict} already"
            raise Refused(msg)
        if global_only and row["series_key"] != store.GLOBAL:
            msg = f"junk decision {row['id']} is series {row['series_key']!r}'s: --series and --retire are for global"
            raise Refused(msg)
    return rows


def _written(row: db.Row, series_key: str, verdict: str, at: str) -> store.Decision:
    return {
        "series_key": series_key,
        "kind": row["kind"],
        "key": row["key"],
        "verdict": verdict,
        "decider": "human",
        "decided_at": at,
        "source": f"redecided decision {row['id']}",
        "images": row["images"],
    }


def _discovered(conn: db.Connection, row: db.Row, verdict: str) -> Step:
    """Return a global decision's step with neither option: a junk's keeps where its sightings are, a keep retired."""
    if verdict == "junk":
        return Step([], retire=True)
    found = sorted({u.series_key for u in where.decided_by(conn, row)})
    if not found:
        msg = f"global junk decision {row['id']} decides no sighted unit: name --series or --retire it"
        raise Refused(msg)
    return Step(found, retire=True)


def redecide(conn: db.Connection, ids: Sequence[int], verdict: str, at: str) -> int:
    """Re-decide active decisions to verdict in 1 act, all or none (SPEC "Junk decisions"); return the act's id.

    A series decision is retired and 1 of verdict written in its place. A global junk gets keeps in every series whose
    sightings it decides and is retired; a global keep is retired.
    """
    rows = _checked(conn, ids, verdict, global_only=False)
    plan = {
        row["id"]: Step([row["series_key"]], retire=True)
        if row["series_key"] != store.GLOBAL
        else _discovered(conn, row, verdict)  # the sightings are read before the act's transaction
        for row in rows
    }
    return _apply(conn, " ".join(["junk redecide", *map(str, ids), verdict]), verdict, at, plan)


def redecide_in(conn: db.Connection, ids: Sequence[int], verdict: str, series: Sequence[str], at: str) -> int:
    """Re-decide global decisions to verdict in the series named only (--series); the global ones stay."""
    if len(set(series)) != len(series):
        msg = f"a series is named twice: {list(series)}"
        raise Refused(msg)
    rows = _checked(conn, ids, verdict, global_only=True)
    missing = store.unknown_series(conn, series)
    if missing:
        msg = f"no series has the key(s) {missing}"
        raise Refused(msg)
    plan = {row["id"]: Step(list(series), retire=False) for row in rows}
    command = " ".join(["junk redecide", *map(str, ids), verdict, *(f"--series {s}" for s in series)])
    return _apply(conn, command, verdict, at, plan)


def retire_global(conn: db.Connection, ids: Sequence[int], verdict: str, at: str) -> int:
    """Retire global decisions (--retire): no decision is written, and their units become candidates again."""
    rows = _checked(conn, ids, verdict, global_only=True)
    plan = {row["id"]: Step([], retire=True) for row in rows}
    return _apply(conn, " ".join(["junk redecide", *map(str, ids), verdict, "--retire"]), verdict, at, plan)


def _apply(conn: db.Connection, command: str, verdict: str, at: str, plan: dict[int, Step]) -> int:
    """Carry out plan as 1 act: retire what it retires, write what it writes, then check the result; all or none."""
    with db.transaction(conn):
        rows = _rows(conn, list(plan))  # still active now
        act = store.new_act(conn, command, at)
        written: list[store.Decision] = []
        for row in rows:
            step = plan[row["id"]]
            for s in step.targets:
                own = store.active_on(conn, s, row["kind"], row["key"])
                if own is not None and own["id"] != row["id"]:
                    msg = f"series {s!r} has its own decision {own['id']} on that key: re-decide that one"
                    raise Refused(msg)
            if step.retire:
                store.retire(conn, [row["id"]], act)
            written += [_written(row, s, verdict, at) for s in step.targets]
        store.write(conn, written, act)
        _check_result(conn, written)
    return act


def _check_result(conn: db.Connection, written: Sequence[store.Decision]) -> None:
    """Refuse when a written junk key is decided the other way in its series (a keep ranks first: never refused)."""
    lookup = store.decisions_of(conn, {w["series_key"] for w in written if w["verdict"] == "junk"})
    for w in written:
        if w["verdict"] != "junk":
            continue
        deciding = lookup(w["series_key"]).decide(w["kind"], store.parse_key(w["kind"], w["key"]))
        if deciding is not None and deciding["verdict"] != "junk":
            msg = (
                f"in series {w['series_key']!r} decision {deciding['id']} ({deciding['verdict']}) still decides that"
                " key: re-decide it too"
            )
            raise Refused(msg)


def undo(conn: db.Connection, act: int, at: str) -> int:
    """Reverse an act in 1 new act: what it wrote is retired, what it retired is active again; return the new act."""
    with db.transaction(conn):
        row = conn.execute("SELECT * FROM junk_act WHERE id = %s", (act,)).fetchone()
        if row is None:
            msg = f"no junk act {act}"
            raise Refused(msg)
        if row["undoes"] is not None:
            msg = f"junk act {act} is an undo: re-decide again instead"
            raise Refused(msg)
        undone = conn.execute("SELECT id FROM junk_act WHERE undoes = %s", (act,)).fetchone()
        if undone is not None:
            msg = f"junk act {act} is undone already (by act {undone[0]})"
            raise Refused(msg)
        wrote, retired = store.act_rows(conn, act, "wrote"), store.act_rows(conn, act, "retired")
        for i in wrote:
            d = db.one_row(conn, "SELECT * FROM junk_decision WHERE id = %s", (i,))
            if not store.is_active(d):
                msg = f"decision {i} that act {act} wrote has changed since: undo that change first"
                raise Refused(msg)
        for i in retired:
            d = db.one_row(conn, "SELECT * FROM junk_decision WHERE id = %s", (i,))
            if d["retired_by"] != act:
                msg = f"decision {i} that act {act} retired has changed since: undo that change first"
                raise Refused(msg)
            taken = store.active_on(conn, d["series_key"], d["kind"], d["key"])
            if taken is not None and taken["id"] not in wrote:
                msg = f"decision {taken['id']} now holds the key of decision {i}: re-decide it first"
                raise Refused(msg)
        new = store.new_act(conn, f"junk undo {act}", at, undoes=act)
        store.retire(conn, wrote, new)
        store.restore(conn, retired, new)
    return new
