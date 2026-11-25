"""The id replay (agent-workspace/plans/phase-10-19-identification.md, step 3): what the new id rules would change.

Read only, no lock: a plain connection whose session is read only (the live database may be at an older schema than
this program's, which `db.read_only` refuses; `db.connect` would apply steps). It writes 1 JSON file,
<paths.scratch>/id-replay/<timestamp>.json, and prints its summary.

(a) Every in-scope `ok` series a check reaches that holds a source stored as not found: `check.gather` with this code
(sources fetched, nothing written). Per series: the `search` ids it finds that the stored rows lack and whether a
description link gave each, a new disagreement (the series would be held), drift (ids of the fetched origins that
differ from the stored rows: changes at the services since the last check, not this code's), and the sources still not
found whose name list was longer than its search cap. A service failing for 1 series is counted, and the replay goes on.

(b) Every in-scope Kavita series with a completed check: from its stored ids and records and Kavita's metadata only, the
web links the next write removes (`kavita_write.wrong_links`) and whether it sets `aniListId` to 0.

Usage, from the project root: python -m scripts.id_replay [--config PATH]
"""

import argparse
import json
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from typing import TypedDict

from manga_pipeline import config, db, errors, workflow
from manga_pipeline.clients import http
from manga_pipeline.db import sql
from manga_pipeline.metadata import check, ids, kavita_write, merge, search, sources, tags
from manga_pipeline.metadata.records import SOURCE_RECORDS, STORED

PROGRESS_EVERY = 25
CAPS = {
    "mangabaka": search.NAMES_TRIED,
    "mangaupdates": search.NAMES_TRIED,
    "anilist": search.NAMES_TRIED,
    "mangadex": search.MANGADEX_NAMES_TRIED,
}
FAILURES = (errors.Unreachable, http.HttpError, http.HttpTimeoutError, sources.SourceError)


class Found(TypedDict):
    """An id a series' search found that its stored rows lack."""

    source: str
    id: str
    route: str  # "description" or "title search"


class Drift(TypedDict):
    """An id of a fetched origin that differs from the stored rows."""

    change: str  # "new" or "gone"
    source: str
    origin: str
    id: str


class Entry(TypedDict):
    """1 series' part (a) result."""

    series_id: int
    name: str
    read_at: str
    found: list[Found]
    disagreement: str | None
    drift: list[Drift]
    missed_past_cap: list[str]
    names: dict[str, list[str]]


class LinkChange(TypedDict):
    """1 series' part (b) result: the links its next write removes, and whether it sets aniListId to 0."""

    series_id: int
    name: str
    removed: list[str]
    anilist_id_0: bool


class Report(TypedDict):
    """The replay: its counts, part (a) entries and part (b) changes."""

    summary: dict[str, object]
    series: list[Entry]
    links: list[LinkChange]


def not_found(ctx: workflow.Context) -> list[int]:
    """Return the in-scope `ok` series a check reaches whose stored records hold a source as not found."""
    own = [STORED[name] for name in SOURCE_RECORDS]
    rows = ctx.conn.execute(
        sql.SQL("""SELECT s.id FROM series s
            WHERE s.status = 'ok' AND (s.kavita_series_id IS NOT NULL OR s.origin = 'new') AND {scope}
                AND EXISTS (SELECT 1 FROM fetched f WHERE f.series_id = s.id AND f.payload IS NULL
                    AND (f.source, f.kind) IN (SELECT * FROM unnest(%(sources)s::text[], %(kinds)s::text[])))
            ORDER BY s.id""").format(scope=workflow.SCOPE),
        {**workflow.scope_params(ctx.cfg), "sources": [s for s, _ in own], "kinds": [k for _, k in own]},
    ).fetchall()
    return [r[0] for r in rows]


def replay_one(ctx: workflow.Context, series_id: int) -> Entry | None:
    """Return series_id's part (a) entry; None when Kavita no longer holds it."""
    rows = ctx.conn.execute(
        "SELECT source, origin, source_id FROM series_source_id WHERE series_id = %s", (series_id,)
    ).fetchall()
    stored: set[ids.Known] = {(r[0], r[1], r[2]) for r in rows}
    gathered = check.gather(ctx, ctx.sources, series_id)
    if gathered is None:
        return None
    known = set(gathered.known)
    given: dict[str, set[str]] = {}
    for m in gathered.manga_rows:
        for s, v in ids.description_ids(m["description"]):
            given.setdefault(s, set()).add(v)
    described = {(s, next(iter(vs))) for s, vs in given.items() if len(vs) == 1}  # only these are used
    found = [
        Found(source=s, id=v, route="description" if (s, v) in described else "title search")
        for s, o, v in sorted(known - stored)
        if o == "search"
    ]
    fetched = set(check.FETCHED_ORIGINS)
    drift = [Drift(change="new", source=s, origin=o, id=v) for s, o, v in sorted(known - stored) if o in fetched]
    drift += [Drift(change="gone", source=s, origin=o, id=v) for s, o, v in sorted(stored - known) if o in fetched]
    own = search.unique(check.series_names(gathered.series))
    lists = {
        "mangabaka": search.variants(own),
        "mangaupdates": search.variants(own),
        "mangadex": search.lookup_names(own, gathered.records),
        "anilist": search.lookup_names(own, gathered.records),
    }
    agreed, _ = ids.resolve(gathered.known)
    return Entry(
        series_id=series_id,
        name=gathered.series["name"],
        read_at=datetime.now(UTC).isoformat(),
        found=found,
        disagreement=gathered.disagreement,
        drift=drift,
        missed_past_cap=[] if gathered.disagreement else _missed_past_cap(agreed, lists),
        names=lists,
    )


def _missed_past_cap(agreed: dict[str, str], lists: dict[str, list[str]]) -> list[str]:
    """Return the sources still without an id whose title search ran over a list longer than its cap.

    MangaDex searches only with a MangaUpdates id; MangaBaka's title search runs only without an AniList or
    MangaUpdates id (counted when neither is agreed at the end: an id learnt after MangaBaka's turn hides a search
    that ran, so this undercounts).
    """
    ran = {
        "mangabaka": "anilist" not in agreed and "mangaupdates" not in agreed,
        "mangaupdates": True,
        "mangadex": "mangaupdates" in agreed,
        "anilist": True,
    }
    return sorted(s for s, cap in CAPS.items() if s not in agreed and ran[s] and len(lists[s]) > cap)


def links(ctx: workflow.Context, rules: tags.Rules, failed: Counter[str]) -> list[LinkChange]:
    """Return part (b): per in-scope Kavita series with a completed check, the links and id the next write changes.

    A series whose Kavita reply fails is counted in failed.
    """
    conn = ctx.conn
    listed = {s["id"]: s for s in ctx.kavita.series_in_library(ctx.cfg["kavita"]["library_id"])}
    rows = conn.execute(
        sql.SQL("""SELECT s.id, s.name, s.kavita_series_id FROM series s JOIN series_check k ON k.series_id = s.id
            WHERE s.status = 'ok' AND s.kavita_series_id = ANY(%(listed)s) AND k.data_version > 0 AND {scope}
            ORDER BY s.id""").format(scope=workflow.SCOPE),
        {**workflow.scope_params(ctx.cfg), "listed": list(listed)},
    ).fetchall()
    out: list[LinkChange] = []
    for done, row in enumerate(rows):
        if done % PROGRESS_EVERY == 0:
            sys.stderr.write(f"id replay (b): {done}/{len(rows)} series\n")
        fields = merge.stored(conn, row["id"], rules)
        try:
            meta = ctx.kavita.metadata(row["kavita_series_id"])
        except FAILURES as e:  # this series' Kavita reply: counted, the replay goes on
            failed[f"(b) {type(e).__name__}"] += 1
            continue
        held = [u.strip() for u in (meta.get("webLinks") or "").split(",") if u.strip()]
        removed = kavita_write.wrong_links(held, fields)
        cleared = fields.kavita_ids["aniListId"] == 0 and bool(listed[row["kavita_series_id"]].get("aniListId"))
        if removed or cleared:
            out.append(LinkChange(series_id=row["id"], name=row["name"], removed=removed, anilist_id_0=cleared))
    return out


def summary(entries: list[Entry], failed: Counter[str], changed: list[LinkChange]) -> dict[str, object]:
    """Return the replay's counts."""
    found = Counter(f"{f['source']} by {f['route']}" for e in entries for f in e["found"])
    return {
        "(a) series replayed": len(entries),
        "(a) ids found, per source and route": dict(found),
        "(a) series newly disagreeing": sum(e["disagreement"] is not None for e in entries),
        "(a) series with drift": sum(bool(e["drift"]) for e in entries),
        "(a) sources missed past a cap": dict(Counter(s for e in entries for s in e["missed_past_cap"])),
        "series not replayed (a service failed)": dict(failed),
        "(b) series whose links change": len(changed),
        "(b) links removed": sum(len(c["removed"]) for c in changed),
        "(b) aniListId set to 0": sum(c["anilist_id_0"] for c in changed),
    }


def replay(ctx: workflow.Context) -> Report:
    """Return the whole replay: its summary, part (a) entries and part (b) changes."""
    series = not_found(ctx)
    entries: list[Entry] = []
    failed: Counter[str] = Counter()
    for done, series_id in enumerate(series):
        if done % PROGRESS_EVERY == 0:
            sys.stderr.write(f"id replay (a): {done}/{len(series)} series\n")
        try:
            entry = replay_one(ctx, series_id)
        except FAILURES as e:  # this series' service reply: counted, the replay goes on
            failed[type(e).__name__] += 1
            continue
        if entry is not None:
            entries.append(entry)
    changed = links(ctx, tags.load(), failed)
    return Report(summary=summary(entries, failed, changed), series=entries, links=changed)


def main(argv: list[str] | None = None) -> int:
    """Run the replay on the configured database and services; write its JSON, print its summary."""
    parser = argparse.ArgumentParser(description="The 10.19 id replay: read only, JSON to <paths.scratch>/id-replay/.")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    args = parser.parse_args(argv)
    cfg = config.init(args.config)
    with closing(db.open_connection(cfg)) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        report = replay(workflow.context(cfg, conn, "replay"))
    folder = config.abs_path(cfg["paths"]["scratch"]) / "id-replay"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_bytes(json.dumps(report, indent=2, ensure_ascii=False).encode())
    sys.stdout.write(json.dumps(report["summary"], indent=2) + f"\nwritten: {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
