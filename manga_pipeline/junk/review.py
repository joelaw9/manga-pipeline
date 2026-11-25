"""The junk commands Claude and a person use (SPEC "Junk decisions", review; "CLI").

`junk list`, `junk show`, `junk decide`. No lock; 1 short transaction each. `junk review` (the command that starts
Claude) is review_run.
"""

from collections.abc import Sequence

from manga_pipeline import config, db
from manga_pipeline.junk import candidates, store

REVIEW_ID_VARIABLE = "MANGA_JUNK_REVIEW_ID"  # set by junk review in Claude's environment
VERDICTS = ("junk", "keep", "unsure")


class Refused(ValueError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """The command refuses: the item is not in the state it needs."""


def listable(conn: db.Connection, cfg: config.Config) -> list[db.Row]:
    """Return the open candidates Claude may be shown, a series at a time, at most junk.review_batch.

    Neither unsure nor stuck, in open_rows order, grouped by series in the order of each series' first candidate. A
    series that fits the room left is taken whole; one larger than the whole batch is cut to the room left; one that
    does not fit but is not larger than the batch waits for a later review, and the listing goes on.
    """
    limit, batch = cfg["junk"]["review_limit"], cfg["junk"]["review_batch"]
    by_series: dict[str, list[db.Row]] = {}
    for r in candidates.open_rows(conn):
        if r["verdict"] is None and r["reviews"] < limit:
            by_series.setdefault(r["series_key"], []).append(r)
    out: list[db.Row] = []
    for rows in by_series.values():
        room = batch - len(out)
        if room == 0:
            break
        if len(rows) <= room:
            out += rows
        elif len(rows) > batch:
            out += rows[:room]
    return out


def listing(
    conn: db.Connection, cfg: config.Config, review_id: str | None = None, *, everything: bool = False
) -> list[db.Row]:
    """Return the candidates `junk list` prints: listable ones, or (everything) every open candidate.

    With a review id, each listed candidate records it (counted once when that review ends), and a review lists once:
    a later list under its id returns what it listed that is still open and not unsure, and marks nothing new, so what
    --keep-rest keeps is what the review was shown. `everything` is refused under a review id.
    """
    if everything and review_id:
        msg = "junk list --all is for a person; it is refused inside a review"
        raise Refused(msg)
    if not review_id:
        return candidates.open_rows(conn) if everything else listable(conn, cfg)
    with db.transaction(conn):  # read and mark together: cleanup cannot delete a candidate in between
        shown = {
            r[0]
            for r in conn.execute(
                "SELECT id FROM junk_candidate WHERE review_id = %s AND review_open = 1", (review_id,)
            )
        }
        if shown:
            return [r for r in candidates.open_rows(conn) if r["id"] in shown and r["verdict"] is None]
        rows = listable(conn, cfg)
        db.executemany(
            conn,
            "UPDATE junk_candidate SET review_id = %s, review_open = 1 WHERE id = %s",
            [(review_id, r["id"]) for r in rows],
        )
    return rows


def chapters(conn: db.Connection, candidate_id: int) -> int:
    """How many chapters wait on a candidate."""
    return db.one_row(
        conn, "SELECT count(*) FROM junk_scan WHERE candidates::jsonb @> to_jsonb(%s::bigint)", (candidate_id,)
    )[0]


def get_open(conn: db.Connection, candidate_id: int, review_id: str | None) -> db.Row:
    """Return the candidate row of candidate_id; Refused unless it is open.

    With a review id, Refused too unless that review listed it and is still open on it; None (a person) takes any. A
    candidate a decision covers is refused naming that decision.
    """
    row = conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (candidate_id,)).fetchone()
    if row is None:
        msg = f"junk candidate {candidate_id} is not open: no such candidate"
        raise Refused(msg)
    if not candidates.is_open(conn, row):
        covering = candidates.covering(store.by_series(conn), row)
        why = (
            f"decision {covering['id']} ({covering['verdict']}) covers it (junk redecide {covering['id']})"
            if covering is not None
            else "no chapter waits on it"
        )
        msg = f"junk candidate {candidate_id} is not open: {why}"
        raise Refused(msg)
    if review_id and (row["review_id"] != review_id or row["review_open"] != 1):
        msg = f"junk candidate {candidate_id} was not listed by this review"
        raise Refused(msg)
    return row


def _decision(row: db.Row, verdict: str, decider: str, at: str) -> store.Decision:
    return {
        "series_key": row["series_key"],
        "kind": row["kind"],
        "key": row["key"],
        "verdict": verdict,
        "decider": decider,
        "decided_at": at,
        "source": f"candidate {row['id']}",
        "images": row["folder"],
    }


def _open_all(conn: db.Connection, candidate_ids: Sequence[int], review_id: str | None) -> list[db.Row]:
    """Return the open candidate rows of candidate_ids (get_open each), refusing a candidate named twice."""
    if len(set(candidate_ids)) != len(candidate_ids):
        msg = f"a candidate is named twice: {list(candidate_ids)}"
        raise Refused(msg)
    return [get_open(conn, i, review_id) for i in candidate_ids]


def _record(conn: db.Connection, rows: Sequence[db.Row], verdict: str, review_id: str | None, at: str) -> None:
    """Record verdict on rows in the caller's transaction: a decision on each key, or the unsure mark."""
    if verdict == "unsure":
        db.executemany(conn, "UPDATE junk_candidate SET verdict = 'unsure' WHERE id = %s", [(r["id"],) for r in rows])
        return
    decider = "claude" if review_id else "human"
    store.upsert(conn, [_decision(r, verdict, decider, at) for r in rows])


def decide(
    conn: db.Connection, candidate_ids: Sequence[int], verdict: str, review_id: str | None, at: str
) -> list[db.Row]:
    """Record a verdict on open candidates in 1 transaction, all or none; return their rows.

    By claude under a review id (only candidates that review listed), else a human. junk / keep become series-scoped
    decisions on their keys; unsure marks them (their chapters become needs_human at the next process run).
    """
    if verdict not in VERDICTS:
        msg = f"verdict must be one of {VERDICTS}, got {verdict!r}"
        raise Refused(msg)
    with db.transaction(conn):
        rows = _open_all(conn, candidate_ids, review_id)
        _record(conn, rows, verdict, review_id, at)
    return rows


def decide_junk_keeping_rest(
    conn: db.Connection, candidate_ids: Sequence[int], review_id: str | None, at: str
) -> list[db.Row]:
    """Record junk on candidates of 1 series and keep the rest of it the review listed, in 1 transaction (--keep-rest).

    Only inside a review. After the junk rows are written, every other candidate of that series the review listed that
    is still open (a candidate the new rows cover is not) and not unsure is kept. Returns the kept rows.
    """
    if not review_id:
        msg = "--keep-rest is for a review: a person names every candidate"
        raise Refused(msg)
    with db.transaction(conn):
        rows = _open_all(conn, candidate_ids, review_id)
        series = {r["series_key"] for r in rows}
        if len(series) != 1:
            msg = f"--keep-rest takes candidates of 1 series, got {sorted(series)}"
            raise Refused(msg)
        _record(conn, rows, "junk", review_id, at)
        rest = conn.execute(
            """SELECT * FROM junk_candidate WHERE review_id = %s AND review_open = 1 AND series_key = %s
            AND verdict IS NULL AND NOT (id = ANY(%s)) ORDER BY id""",
            (review_id, series.pop(), list(candidate_ids)),
        ).fetchall()
        lookup = store.by_series(conn)  # read after the junk rows: a candidate they cover is not kept
        kept = [r for r in rest if candidates.is_open(conn, r, lookup)]
        _record(conn, kept, "keep", review_id, at)
    return kept
