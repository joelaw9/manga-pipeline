"""The junk commands Claude and a person use (SPEC "Junk decisions", review; "CLI").

`junk list`, `junk show`, `junk decide`. No lock; 1 short transaction each. `junk review` (the command that starts
Claude) is review_run.
"""

from manga_pipeline import config, db
from manga_pipeline.junk import candidates, store

REVIEW_ID_VARIABLE = "MANGA_JUNK_REVIEW_ID"  # set by junk review in Claude's environment
VERDICTS = ("junk", "keep", "unsure")


class Refused(ValueError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """The command refuses: the item is not in the state it needs."""


def listable(conn: db.Connection, cfg: config.Config) -> list[db.Row]:
    """Return the open candidates Claude may be shown.

    Neither unsure nor stuck, fewest reviews first, at most junk.review_batch.
    """
    limit = cfg["junk"]["review_limit"]
    rows = [r for r in candidates.open_rows(conn) if r["verdict"] is None and r["reviews"] < limit]
    return rows[: cfg["junk"]["review_batch"]]


def listing(
    conn: db.Connection, cfg: config.Config, review_id: str | None = None, *, everything: bool = False
) -> list[db.Row]:
    """Return the candidates `junk list` prints: listable ones, or (everything) every open candidate.

    With a review id, each listed candidate records it (counted once when that review ends). `everything` is refused
    under a review id.
    """
    if everything and review_id:
        msg = "junk list --all is for a person; it is refused inside a review"
        raise Refused(msg)
    if not review_id:
        return candidates.open_rows(conn) if everything else listable(conn, cfg)
    with db.transaction(conn):  # read and mark together: cleanup cannot delete a candidate in between
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

    With a review id, Refused too unless that review listed it and is still open on it; None (a person) takes any.
    """
    row = conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (candidate_id,)).fetchone()
    if row is None or not candidates.is_open(conn, row):
        msg = f"junk candidate {candidate_id} is not open (unknown, decided, or no chapter waits on it)"
        raise Refused(msg)
    if review_id and (row["review_id"] != review_id or row["review_open"] != 1):
        msg = f"junk candidate {candidate_id} was not listed by this review"
        raise Refused(msg)
    return row


def decide(conn: db.Connection, candidate_id: int, verdict: str, review_id: str | None, at: str) -> db.Row:
    """Record a verdict on an open candidate, by claude under a review id (only one that review listed), else a human.

    junk / keep become a series-scoped decision on its key; unsure marks it (its chapters become needs_human at the
    next process run).
    """
    if verdict not in VERDICTS:
        msg = f"verdict must be one of {VERDICTS}, got {verdict!r}"
        raise Refused(msg)
    decider = "claude" if review_id else "human"
    with db.transaction(conn):
        row = get_open(conn, candidate_id, review_id)
        if verdict == "unsure":
            conn.execute("UPDATE junk_candidate SET verdict = 'unsure' WHERE id = %s", (candidate_id,))
            return row
        store.upsert(
            conn,
            [
                {
                    "series_key": row["series_key"],
                    "kind": row["kind"],
                    "key": row["key"],
                    "verdict": verdict,
                    "decider": decider,
                    "decided_at": at,
                    "source": f"candidate {candidate_id}",
                }
            ],
        )
    return row
