"""The junk commands Claude and a person use (SPEC "Junk decisions", review; "CLI").

`junk list`, `junk show`, `junk decide`. No lock; 1 short transaction each. `junk review` (the command that starts
Claude) is review_run. A frame junk decision's overriding page keeps (A24: page_keeps_over), which `junk decide` and
`junk row` print.
"""

from collections.abc import Sequence
from typing import NamedTuple

from manga_pipeline import config, db
from manga_pipeline.junk import candidates, store
from manga_pipeline.stages import junk as junk_stage

REVIEW_ID_VARIABLE = "MANGA_JUNK_REVIEW_ID"  # set by junk review in Claude's environment
VERDICTS = ("junk", "keep", "unsure")


class Refused(ValueError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """The command refuses: the item is not in the state it needs."""


def listable(conn: db.Connection, cfg: config.Config) -> list[db.Row]:
    """Return the open candidates Claude may be shown, a series at a time, at most junk.review_batch.

    Neither unsure nor stuck, in open_rows order, grouped by series in the order of each series' first candidate. A
    series that fits the room left is taken whole; one larger than the whole batch is cut to the room left (_cut:
    never inside a bundle); one that does not fit but is not larger than the batch waits for a later review, and the
    listing goes on until the batch is full.
    """
    limit, batch = cfg["junk"]["review_limit"], cfg["junk"]["review_batch"]
    by_series: dict[str, list[db.Row]] = {}
    for r in candidates.open_rows(conn):
        if r["verdict"] is None and r["reviews"] < limit:
            by_series.setdefault(r["series_key"], []).append(r)
    bundles = candidates.bundles(conn)
    out: list[db.Row] = []
    for rows in by_series.values():
        room = batch - len(out)
        if room <= 0:
            break
        if len(rows) <= room:
            out += rows
        elif len(rows) > batch and _cut(rows, bundles, out, batch):
            break
    return out


def _cut(rows: Sequence[db.Row], bundles: Sequence[Sequence[int]], out: list[db.Row], batch: int) -> bool:
    """List a series larger than the batch into out up to the batch, its bundles kept whole; return whether to stop.

    Its rows are taken in order, each with the other rows of every bundle it belongs to (its bundles' own ids, no
    closure over the members they share with other bundles). The series is cut before a set that does not fit the
    room left, which waits for a later review; but when nothing is listed yet that set alone exceeds the batch: it is
    listed whole and the listing stops.
    """
    taken: set[int] = set()
    for r in rows:
        if r["id"] in taken:
            continue
        ids = {r["id"]}.union(*(b for b in bundles if r["id"] in b))
        chunk = [x for x in rows if x["id"] in ids and x["id"] not in taken]
        taken.update(x["id"] for x in chunk)
        if len(chunk) <= batch - len(out):
            out += chunk
        elif not out:
            out += chunk
            return True
        else:
            return False
    return False


def listed_bundles(rows: Sequence[db.Row], bundles: Sequence[Sequence[int]]) -> list[list[db.Row]]:
    """Return the bundles (candidates.bundles) a listing shows, each as its listed members in listing order.

    A bundle shown in part (its other members unsure, stuck or past the batch) holds only those listed; one with none
    listed is left out. In order of each bundle's first listed member.
    """
    order = {r["id"]: n for n, r in enumerate(rows)}
    shown = [[r for r in rows if r["id"] in b] for b in bundles]
    return sorted((s for s in shown if s), key=lambda s: order[s[0]["id"]])


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
        why = "no chapter waits on it" if covering is None else _covered(covering, row["series_key"])
        msg = f"junk candidate {candidate_id} is not open: {why}"
        raise Refused(msg)
    if review_id and (row["review_id"] != review_id or row["review_open"] != 1):
        msg = f"junk candidate {candidate_id} was not listed by this review"
        raise Refused(msg)
    return row


def _covered(covering: db.Row, series_key: str) -> str:
    """Return why a candidate is closed by the decision covering it: that decision, or the agreeing ones."""
    agreed = store.agreeing(covering)
    if not agreed:
        return f"decision {covering['id']} ({covering['verdict']}) covers it (junk redecide {covering['id']})"
    other = "keep" if covering["verdict"] == "junk" else "junk"
    ids = ", ".join(map(str, agreed))
    return (
        f"decisions {ids} of other series agree it is {covering['verdict']} (junk redecide {agreed[0]} {other}"
        f" --series {series_key} opts this series out)"
    )


class Override(NamedTuple):
    """A page keep ranking first on an edge page whose frame a frame junk decision matches (A24).

    The frame junk decision's id, the page keep's id, and where: the series key, chapter id and page of a stored page
    scan's edge page.
    """

    frame: int
    keep: int
    series_key: str
    chapter: int
    page: int


def override_text(o: Override) -> str:
    """Return the line naming an overriding page keep, as `junk decide` and `junk row` print it."""
    return (
        f"page keep {o.keep} keeps series {o.series_key!r} chapter {o.chapter} page {o.page}, whose frame junk"
        f" decision {o.frame} matches: the page keep ranks first there (junk redecide {o.keep} junk lifts it)"
    )


def page_keeps_over(conn: db.Connection, frame: db.Row) -> list[Override]:
    """Return the page keeps that keep an edge page of a stored page scan whose frame a frame junk decision matches.

    Over its series' stored page scans for a series decision, every series' for a global one: an edge page whose frame
    the decision's key matches and whose page signature a page keep decides that ranks before the decision
    (store.in_lookup_order). In chapter, then page order.
    """
    query = "SELECT js.chapter_id, js.scan, s.rules_key FROM junk_scan js JOIN series s ON s.id = js.series_id"
    params: tuple[str, ...] = ()
    if frame["series_key"] != store.GLOBAL:
        query += " WHERE s.rules_key = %s"
        params = (frame["series_key"],)
    scans = [r for r in conn.execute(query + " ORDER BY js.chapter_id", params) if r["rules_key"]]
    lookup = store.decisions_of(conn, {r["rules_key"] for r in scans})
    stored = store.parse_key("frame", frame["key"])
    found: list[Override] = []
    for r in scans:
        sc = junk_stage.read_scan(r["scan"])
        if sc["layout"] == "strip" or sc["split"] is True:
            continue
        for u in sc["edges"]:
            if not (u["frame"] and u["sig"]) or not store.matches(stored, store.parse_key("frame", u["frame"])):
                continue
            page = lookup(r["rules_key"]).decide("page", store.parse_key("page", u["sig"]))
            if page is not None and page["verdict"] == "keep" and store.in_lookup_order([frame, page])[0] is page:
                found.append(Override(frame["id"], page["id"], r["rules_key"], r["chapter_id"], u["page"]))
    return found


def decision(row: db.Row, verdict: str, decider: str, at: str) -> store.Decision:
    """Return the decision a verdict on a candidate row writes: its key, kind, source and images, series-scoped."""
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


def _record(
    conn: db.Connection, rows: Sequence[db.Row], verdict: str, review_id: str | None, at: str
) -> list[Override]:
    """Record verdict on rows in the caller's transaction: a decision on each key, or the unsure mark.

    A decision on a candidate of a waiting chapter's bundle of more than 1 member (candidates.bundles) carries the
    bundle mark (SPEC "Junk decisions": a marked keep protects no gap). Returns the page keeps overriding each frame
    junk decision active on a key it wrote (page_keeps_over).
    """
    if verdict == "unsure":
        db.executemany(conn, "UPDATE junk_candidate SET verdict = 'unsure' WHERE id = %s", [(r["id"],) for r in rows])
        return []
    decider = "claude" if review_id else "human"
    bundled = {i for ids in candidates.bundles(conn) for i in ids}
    written: list[store.Decision] = []
    for r in rows:
        d = decision(r, verdict, decider, at)
        d["bundle"] = r["id"] in bundled
        written.append(d)
    store.upsert(conn, written)
    overrides: list[Override] = []
    for d in written:
        row = store.active_on(conn, d["series_key"], d["kind"], d["key"]) if d["kind"] == "frame" else None
        if row is not None and row["verdict"] == "junk":
            overrides += page_keeps_over(conn, row)
    return overrides


def decide(
    conn: db.Connection, candidate_ids: Sequence[int], verdict: str, review_id: str | None, at: str
) -> list[Override]:
    """Record a verdict on open candidates in 1 transaction, all or none; return the page keeps overriding it (_record).

    By claude under a review id (only candidates that review listed), else a human. junk / keep become series-scoped
    decisions on their keys; unsure marks them (their chapters become needs_human at the next process run).
    """
    if verdict not in VERDICTS:
        msg = f"verdict must be one of {VERDICTS}, got {verdict!r}"
        raise Refused(msg)
    with db.transaction(conn):
        rows = _open_all(conn, candidate_ids, review_id)
        return _record(conn, rows, verdict, review_id, at)


def decide_junk_keeping_rest(
    conn: db.Connection, candidate_ids: Sequence[int], review_id: str | None, at: str
) -> tuple[list[db.Row], list[Override]]:
    """Record junk on candidates of 1 series and keep the rest of it the review listed, in 1 transaction (--keep-rest).

    Only inside a review. After the junk rows are written, every other candidate of that series the review listed that
    is still open (a candidate the new rows cover is not) and not unsure is kept. Returns the kept rows and the page
    keeps overriding the junk (_record).
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
        overrides = _record(conn, rows, "junk", review_id, at)
        rest = conn.execute(
            """SELECT * FROM junk_candidate WHERE review_id = %s AND review_open = 1 AND series_key = %s
            AND verdict IS NULL AND NOT (id = ANY(%s)) ORDER BY id""",
            (review_id, series.pop(), list(candidate_ids)),
        ).fetchall()
        lookup = store.by_series(conn)  # read after the junk rows: a candidate they cover is not kept
        kept = [r for r in rest if candidates.is_open(conn, r, lookup)]
        _record(conn, kept, "keep", review_id, at)
    return kept, overrides
