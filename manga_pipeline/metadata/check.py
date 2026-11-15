"""A series' metadata check (SPEC "Metadata checks", "Source ids"): which series are due, and 1 check of 1 series.

A check gathers the series' ids, fetches each source (searching for an id it lacks), compares every id as it learns
it, picks the cover, and stores all of it in 1 transaction when it completes. A check that cannot complete stores
nothing and stays due; one whose ids disagree stores the ids and makes the series needs_human.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from manga_pipeline import db, workflow
from manga_pipeline.metadata import covers, ids, refresh, search, sources

log = logging.getLogger(__name__)
FETCHED_ORIGINS = ("suwayomi", "kavita", "anilist", "mangabaka", "mangadex")  # replaced by every completed check
DAYS_PER_MONTH = 30  # metadata.*_months as days
MANGABAKA_COMPLETE = "completed"
# (source, kind) of every record a check stores; a source with no id stores None for each of its kinds.
KINDS = {
    "mangabaka": ("series",),
    "mangaupdates": ("series",),
    "mangadex": ("manga", "aggregate", "covers"),
    "anilist": ("media",),
}


@dataclass
class Sources:
    """The run's 4 source clients."""

    mangabaka: sources.MangaBaka
    mangaupdates: sources.MangaUpdates
    mangadex: sources.MangaDex
    anilist: sources.AniList


def new_sources():
    return Sources(sources.MangaBaka(), sources.MangaUpdates(), sources.MangaDex(), sources.AniList())


class Disagree(Exception):
    pass


def completed(ctx, chapter):
    """Whether the chapter's series has completed a metadata check (found or not): the end of a metadata_check
    wait."""
    row = ctx.conn.execute(
        "SELECT data_version FROM series_check WHERE series_id = %s", (chapter["series_id"],)
    ).fetchone()
    return row is not None and row["data_version"] > 0


def due(ctx):
    """Ids of the in-scope, not needs_human series whose check runs this run: every urgent or never completed one,
    then at most metadata.checks_per_run others, the longest due first."""
    rows = ctx.conn.execute(
        f"""SELECT s.id, k.series_id IS NULL OR k.urgent OR k.data_version = 0 AS always
        FROM series s LEFT JOIN series_check k ON k.series_id = s.id
        WHERE s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND {workflow.SCOPE}
            AND (k.series_id IS NULL OR k.urgent OR k.data_version = 0 OR k.next_check_at <= %(now)s)
        ORDER BY k.next_check_at NULLS FIRST, s.id""",
        {**workflow.scope_params(ctx.cfg), "now": workflow.now(ctx.conn)},
    ).fetchall()
    always = [r[0] for r in rows if r[1]]
    others = [r[0] for r in rows if not r[1]][: ctx.cfg["metadata"]["checks_per_run"]]
    return always + others


def make_urgent(conn, series_id):
    """A new chapter makes its series' check due at once; inside the caller's transaction."""
    conn.execute(
        """INSERT INTO series_check (series_id, urgent) VALUES (%s, true)
        ON CONFLICT (series_id) DO UPDATE SET urgent = true""",
        (series_id,),
    )


def run(ctx, clients, series_id):
    """Check 1 series. Returns True when it completed. Unreachable, HttpError and SourceError from a service end the
    check (nothing stored) and propagate."""
    conn = ctx.conn
    series = conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()
    mangas = conn.execute(
        "SELECT manga_id, title FROM suwayomi_manga WHERE series_id = %s ORDER BY manga_id", (series_id,)
    ).fetchall()
    kavita_series = ctx.kavita.series(series["kavita_series_id"])
    if kavita_series is None:
        # Not checked, and not due again for the quiet interval: a deleted series never takes a capped check's place
        now = workflow.now(conn)
        later = (datetime.fromisoformat(now) + timedelta(days=ctx.cfg["metadata"]["quiet_days"])).isoformat()
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO series_check (series_id, next_check_at) VALUES (%s, %s)
                ON CONFLICT (series_id) DO UPDATE SET next_check_at = excluded.next_check_at, urgent = false""",
                (series_id, later),
            )
        log.info(
            "series %s (%s): not checked: Kavita no longer holds series %s",
            series_id,
            series["name"],
            series["kavita_series_id"],
        )
        return False
    manga_rows = [m for m in (ctx.suwayomi.manga(row["manga_id"]) for row in mangas) if m is not None]
    known = _stored_ids(conn, series_id) + _origin_ids(ctx, series, kavita_series, manga_rows)
    try:
        records = _fetch(clients, _names(series), known)
    except Disagree as e:
        _store_disagreement(conn, series_id, known, str(e))
        log.warning("series %s (%s): needs_human: %s", series_id, series["name"], e)
        return False
    refresh.completed(ctx, series_id, manga_rows)
    last = _last_update(ctx, clients, manga_rows, records)
    complete = any(m["status"] == refresh.COMPLETED for m in manga_rows) or (
        (records[("mangabaka", "series")] or {}).get("status") == MANGABAKA_COMPLETE
    )
    cover = covers.pick(ctx, clients, manga_rows, records)
    now = workflow.now(conn)
    with db.transaction(conn):
        changed = _store(conn, series_id, known, records, now)
        if cover is not None:
            covers.store(conn, series_id, cover)
        version = conn.execute(
            """INSERT INTO series_check (series_id, data_version, checked_at, next_check_at, urgent)
            VALUES (%(id)s, 1, %(now)s, %(next)s, false)
            ON CONFLICT (series_id) DO UPDATE SET
                data_version = series_check.data_version
                    + CASE WHEN %(changed)s OR series_check.data_version = 0 THEN 1 ELSE 0 END,
                checked_at = %(now)s, next_check_at = %(next)s, urgent = false
            RETURNING data_version""",
            {"id": series_id, "now": now, "next": _next_check(ctx.cfg, now, last, complete), "changed": changed},
        ).fetchone()[0]
    fetched = ", ".join(
        f"{source} {'found' if records[(source, KINDS[source][0])] is not None else 'not found'}" for source in KINDS
    )
    log.info(
        "series %s (%s): checked: %s; data %s (version %s)",
        series_id,
        series["name"],
        fetched,
        "changed" if changed else "unchanged",
        version,
    )
    return True


def _names(series):
    """The names a source is searched by: the series' Kavita names."""
    return list(dict.fromkeys(n for n in (series["name"], series["localized_name"], series["original_name"]) if n))


def _stored_ids(conn, series_id):
    """The ids no fetch replaces: found by search, imported, set by a person."""
    rows = conn.execute(
        """SELECT source, origin, source_id FROM series_source_id
        WHERE series_id = %s AND origin IN ('search', 'import', 'human')""",
        (series_id,),
    ).fetchall()
    return [tuple(r) for r in rows]


def _origin_ids(ctx, series, kavita_series, manga_rows):
    known = []
    for manga in manga_rows:
        known += [(s, "suwayomi", v) for s, v in ids.suwayomi_ids(manga)]
    metadata = ctx.kavita.metadata(series["kavita_series_id"])
    return known + [(s, "kavita", v) for s, v in ids.kavita_ids(kavita_series, metadata)]


def _fetch(clients, names, known):
    """Every source's records, {(source, kind): payload or None}; known grows with each search hit (origin search)
    and each record's cross-links. Disagree as soon as 2 ids of a source differ."""

    def agreed():
        result, problem = ids.resolve(known)
        if problem:
            raise Disagree(problem)
        return result

    def learn(source, origin, pairs):
        known.extend((s, origin, v) for s, v in pairs if s != source)

    def id_for(source, searcher):
        value = agreed().get(source)
        if value is None:
            value = searcher()
            if value is not None:
                known.append((source, "search", value))
                agreed()
        return value

    records = {(source, kind): None for source, kinds in KINDS.items() for kind in kinds}
    mb_id = id_for("mangabaka", lambda: search.mangabaka(clients.mangabaka, names, agreed()))
    if mb_id is not None and (record := _comic(clients.mangabaka.series(mb_id), "type", "novel", mb_id)) is not None:
        records[("mangabaka", "series")] = record
        learn("mangabaka", "mangabaka", ids.mangabaka_ids(record))
    mu_id = id_for("mangaupdates", lambda: search.mangaupdates(clients.mangaupdates, names))
    if mu_id is not None:
        records[("mangaupdates", "series")] = _comic(clients.mangaupdates.series(mu_id), "type", "novel", mu_id)
    md_id = id_for("mangadex", lambda: search.mangadex(clients.mangadex, names, agreed()))
    if md_id is not None and (record := clients.mangadex.manga(md_id)) is not None:
        records[("mangadex", "manga")] = record
        records[("mangadex", "aggregate")] = clients.mangadex.aggregate(md_id)
        records[("mangadex", "covers")] = clients.mangadex.covers(md_id)
        learn("mangadex", "mangadex", ids.mangadex_ids(record))
    al_id = id_for("anilist", lambda: search.anilist(clients.anilist, names))
    if al_id is not None and (record := _comic(clients.anilist.media(al_id), "format", "novel", al_id)) is not None:
        records[("anilist", "media")] = record
        learn("anilist", "anilist", ids.anilist_ids(record))
    agreed()
    return records


def _comic(record, field, novel, source_id):
    """The record, or None when it is a novel's (an id that led to a manga's light novel): the library is comics. A
    dropped record is logged; a person fixes the id with `series set-id`."""
    if record is not None and (record.get(field) or "").lower() == novel:
        log.warning("record %s is a novel's: not used (fix the series' id with `series set-id`)", source_id)
        return None
    return record


def _store_disagreement(conn, series_id, known, problem):
    with db.transaction(conn):
        _store_ids(conn, series_id, known)
        workflow.needs_human(
            conn, "series", series_id, f"{problem} -> fix in Kavita or `series set-id SERIES SOURCE ID`"
        )


def _store_ids(conn, series_id, known):
    conn.execute(
        "DELETE FROM series_source_id WHERE series_id = %s AND origin = ANY(%s::text[])",
        (series_id, list(FETCHED_ORIGINS)),
    )
    db.executemany(
        conn,
        """INSERT INTO series_source_id (series_id, source, origin, source_id) VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING""",
        [(series_id, source, origin, value) for source, origin, value in dict.fromkeys(known)],
    )


def _store(conn, series_id, known, records, now):
    """Store the ids and records; returns whether any record's hash changed (or is new)."""
    _store_ids(conn, series_id, known)
    old = dict(
        ((r["source"], r["kind"]), r["payload_hash"])
        for r in conn.execute("SELECT source, kind, payload_hash FROM fetched WHERE series_id = %s", (series_id,))
    )
    rows = [
        (series_id, source, kind, None if p is None else json.dumps(p), _hash(p), now)
        for (source, kind), p in records.items()
    ]
    db.executemany(
        conn,
        """INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (series_id, source, kind) DO UPDATE SET
            payload = excluded.payload, payload_hash = excluded.payload_hash, fetched_at = excluded.fetched_at""",
        rows,
    )
    return any(old.get((source, kind)) != h for _, source, kind, _, h, _ in rows)


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _last_update(ctx, clients, manga_rows, records):
    """The series' last published update (SPEC "Metadata checks") as an aware datetime, or None."""
    dates = []
    for manga in manga_rows:
        dates += [int(c["uploadDate"]) for c in ctx.suwayomi.chapters(manga["id"]) if c.get("uploadDate")]
    if dates:
        return datetime.fromtimestamp(max(dates) / 1000, UTC)
    manga = records[("mangadex", "manga")]
    if not manga_rows and manga is not None:
        latest = clients.mangadex.latest_chapter_date(manga["id"])
        return datetime.fromisoformat(latest) if latest else None
    return None


def _next_check(cfg, now, last, complete):
    m = cfg["metadata"]
    now = datetime.fromisoformat(now)
    age = now - last if last is not None else timedelta.max
    if age < timedelta(days=m["recent_months"] * DAYS_PER_MONTH):
        days = m["recent_days"]
    elif complete and age >= timedelta(days=m["complete_months"] * DAYS_PER_MONTH):
        days = m["complete_days"]
    else:
        days = m["quiet_days"]
    return (now + timedelta(days=days)).isoformat(timespec="seconds")
