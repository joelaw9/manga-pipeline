"""What a junk decision decides now, from the sightings (SPEC "Junk decisions", decision commands).

A sighting (junk_seen) is a unit of a chapter identity of a series: strip edge blocks, edge pages and their frames,
bands. A decision decides a sighting when its series' lookup (store.Decisions) returns it for the sighting's key, or
returns a stand-in of an agreement it is one of (store.agreeing).
Sightings carry no place in the chapter, and none are kept of probes, tail pieces or logos.
"""

from collections.abc import Sequence
from typing import NamedTuple

from manga_pipeline import db
from manga_pipeline.junk import store


class Unit(NamedTuple):
    """A sighted unit a decision decides: its series key, chapter identity and kind."""

    series_key: str
    identity: str
    kind: str


def decided_by(conn: db.Connection, row: db.Row, series: Sequence[str] = ()) -> list[Unit]:
    """Return the sighted units decision row decides now, sorted.

    A series decision's in its series and in every other series where it is one of the decisions whose agreement
    decides the unit; a global one's in `series`, or, when none is named, in every series.
    """
    kinds = [unit for unit, deciding in store.DECIDING.items() if row["kind"] in deciding]
    scope = list(series) if row["series_key"] == store.GLOBAL and series else None  # None: every series
    query = "SELECT DISTINCT series_key, kind, key, identity FROM junk_seen WHERE kind = ANY(%s)"
    params: tuple[list[str], ...] = (kinds,)
    if scope is not None:
        query += " AND series_key = ANY(%s)"
        params = (kinds, scope)
    stored = store.parse_key(row["kind"], row["key"])
    parsed = ((s, store.parse_key(s["kind"], s["key"])) for s in conn.execute(query, params))
    hits = [(s, k) for s, k in parsed if store.matches(stored, k)]  # 1 comparison, before any lookup
    lookup = store.decisions_of(conn, {s["series_key"] for s, _ in hits})
    units: set[Unit] = set()
    for s, k in hits:
        deciding = lookup(s["series_key"]).decide(s["kind"], k)
        if deciding is not None and (deciding["id"] == row["id"] or row["id"] in store.agreeing(deciding)):
            units.add(Unit(s["series_key"], s["identity"], s["kind"]))
    return sorted(units)
