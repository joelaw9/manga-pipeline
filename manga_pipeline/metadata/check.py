"""A series' metadata check (SPEC "Metadata checks", "Source ids"): which series are due, and 1 check of 1 series.

A check gathers the series' ids, fetches each source (searching for an id it lacks), compares every id as it learns
it, picks the cover, and stores all of it in 1 transaction when it completes. A check that cannot complete, or whose
series' stored ids changed while it fetched, stores nothing and stays due; one whose ids disagree stores the ids and
makes the series needs_human.
"""

import copy
import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeIs

from manga_pipeline import config, db, notify, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import covers, ids, platforms, refresh, search, series_name, sources
from manga_pipeline.metadata.records import SOURCE_RECORDS, STORED, Records
from manga_pipeline.replies.kavita import SeriesMetadata
from manga_pipeline.replies.sources import AniListMedia, MangaBakaSeries, MangaUpdatesSeries
from manga_pipeline.replies.suwayomi import Manga

log = logging.getLogger(__name__)
FETCHED_ORIGINS = ("suwayomi", "kavita", "anilist", "mangabaka", "mangadex")  # replaced by every completed check
DAYS_PER_MONTH = 30  # metadata.*_months as days
MANGABAKA_COMPLETE = "completed"
SCRUBBED = "removed characters the database cannot hold (NUL, lone surrogate)"
# Key paths a record's hash leaves out, per (source, kind) of records.STORED ("[]": each list item, "*": each key):
# fields that change with the source's own activity (counts, ranks, ratings, votes, update times) and that nothing
# reads. A field a rule reads never belongs here.
VOLATILE: dict[tuple[str, str], tuple[tuple[str, ...], ...]] = {
    ("mangaupdates", "series"): (
        ("rank",),
        ("bayesian_rating",),
        ("rating_votes",),
        ("last_updated",),
        ("latest_chapter",),
        ("categories", "[]", "votes"),
        ("categories", "[]", "votes_plus"),
        ("categories", "[]", "votes_minus"),
        ("recommendations",),
        ("category_recommendations",),
    ),
    ("mangabaka", "series"): (
        ("rating",),
        ("popularity",),
        ("last_updated_at",),
        ("source", "*", "rating"),
        ("source", "*", "rating_normalized"),
        ("tags_v2", "[]", "series_count"),
    ),
    ("mangadex", "manga"): (
        ("attributes", "updatedAt"),
        ("attributes", "version"),
        ("attributes", "latestUploadedChapter"),
        ("attributes", "availableTranslatedLanguages"),
        ("relationships", "[]", "attributes", "updatedAt"),
        ("relationships", "[]", "attributes", "version"),
    ),
    ("mangadex", "covers"): (("[]", "updatedAt"), ("[]", "version")),
}


class Disagree(Exception):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """2 ids of a source differ; the message is ids.resolve's problem text, which the check stores (needs_human)."""


def completed(ctx: workflow.Context, chapter: db.Row) -> bool:
    """Return whether the chapter's series has completed a metadata check (found or not).

    The end of a metadata_check wait. A series on a platform basis also needs the check its new chapter made due to
    complete, so the chapter is never named from a list older than its arrival (SPEC "Metadata", platform lists).
    """
    row = ctx.conn.execute(
        """SELECT k.data_version, k.urgent, s.naming_basis FROM series s
        LEFT JOIN series_check k ON k.series_id = s.id WHERE s.id = %s""",
        (chapter["series_id"],),
    ).fetchone()
    if row is None or not row["data_version"]:
        return False
    return not (row["naming_basis"] in platforms.BASES and row["urgent"])


def due(ctx: workflow.Context) -> list[int]:
    """Return the ids of the in-scope, not needs_human series whose check runs this run.

    Every urgent one and every one never completed and not put off (a series Kavita no longer holds is put off by the
    quiet interval), then at most metadata.checks_per_run others, the longest due first. A series
    is checked when Kavita holds it, or when it is a new series Kavita does not hold yet (admitted or not).
    """
    rows = ctx.conn.execute(
        sql.SQL("""SELECT s.id,
            k.series_id IS NULL OR k.urgent OR (k.data_version = 0 AND k.next_check_at IS NULL) AS always
        FROM series s LEFT JOIN series_check k ON k.series_id = s.id
        WHERE s.status = 'ok' AND (s.kavita_series_id IS NOT NULL OR s.origin = 'new') AND {scope}
            AND (k.series_id IS NULL OR k.urgent OR (k.data_version = 0 AND k.next_check_at IS NULL)
                OR k.next_check_at <= %(now)s)
        ORDER BY k.next_check_at NULLS FIRST, s.id""").format(scope=workflow.SCOPE),
        {**workflow.scope_params(ctx.cfg), "now": workflow.now(ctx.conn)},
    ).fetchall()
    always = [r[0] for r in rows if r[1]]
    others = [r[0] for r in rows if not r[1]][: ctx.cfg["metadata"]["checks_per_run"]]
    return always + others


def make_urgent(conn: db.Connection, series_id: int) -> None:
    """Make the series' check due at once, for a new chapter; inside the caller's transaction."""
    conn.execute(
        """INSERT INTO series_check (series_id, urgent) VALUES (%s, true)
        ON CONFLICT (series_id) DO UPDATE SET urgent = true""",
        (series_id,),
    )


def run(ctx: workflow.Context, clients: sources.Sources, series_id: int) -> bool:
    """Check 1 series. Returns True when it completed.

    Unreachable, HttpError and SourceError from a service end the check (nothing stored) and propagate. A new series
    Kavita does not hold yet has no Kavita record or ids.
    """
    conn = ctx.conn
    gathered = gather(ctx, clients, series_id)
    if gathered is None:
        _put_off(ctx, series_id)
        return False
    series, stored, known = gathered.series, gathered.stored, gathered.known
    if gathered.disagreement is not None:
        if _store_disagreement(conn, series, stored, known, gathered.disagreement):
            log.warning("series %s (%s): needs_human: %s", series_id, series["name"], gathered.disagreement)
        return False
    records, kavita_meta, manga_rows = gathered.records, gathered.kavita_meta, gathered.manga_rows
    _scrub_records(series, records)
    platform = _platform(clients, series, kavita_meta, records, _stored_platform(conn, series_id))
    if _scrub(platform):
        log.warning("series %s (%s): platform list: %s", series_id, series["name"], SCRUBBED)
    refresh.completed(ctx, series_id, manga_rows)
    last = _last_update(ctx, clients, manga_rows, records)
    complete = any(m["status"] == refresh.COMPLETED for m in manga_rows) or (
        (records["mangabaka"] or {}).get("status") == MANGABAKA_COMPLETE
    )
    cover = covers.pick(ctx, clients, manga_rows, records)
    now = workflow.now(conn)
    with db.transaction(conn):
        if _ids_changed(conn, series, stored):
            return False
        retitled = _title_change(conn, series, records["anilist"])
        changed = _store(conn, series_id, known, records, now)
        _store_platform(conn, series_id, platform, now)
        if cover is not None:
            covers.store(conn, series_id, cover)
        version = db.one_row(
            conn,
            """INSERT INTO series_check (series_id, data_version, checked_at, next_check_at, urgent)
            VALUES (%(id)s, 1, %(now)s, %(next)s, false)
            ON CONFLICT (series_id) DO UPDATE SET
                data_version = series_check.data_version
                    + CASE WHEN %(changed)s OR series_check.data_version = 0 THEN 1 ELSE 0 END,
                checked_at = %(now)s, next_check_at = %(next)s, urgent = false
            RETURNING data_version""",
            {
                "id": series_id,
                "now": now,
                "next": next_check(ctx.cfg, now, last, complete=complete),
                "changed": changed,
            },
        )[0]
    if retitled is not None:  # logged once its report is stored, before anything else can fail
        log.warning("%s (not applied)", retitled)
    covers.refresh_volumes(ctx, series_id)  # the cover list just stored may choose another file for a volume
    fetched = ", ".join(
        f"{STORED[name][0]} {'found' if records[name] is not None else 'not found'}" for name in SOURCE_RECORDS
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


@dataclass(frozen=True)
class Gathered:
    """What a check read: the series, its ids (stored, then as the fetch grew them) and its source records.

    disagreement: the problem text when 2 ids of a source differ (the fetch stopped there), else None.
    """

    series: db.Row
    stored: list[ids.Known]
    known: list[ids.Known]
    records: Records
    kavita_meta: SeriesMetadata | None
    manga_rows: list[Manga]
    disagreement: str | None


def gather(ctx: workflow.Context, clients: sources.Sources, series_id: int) -> Gathered | None:
    """Read 1 series' ids and fetch its sources' records, writing nothing (the reading part of a check).

    None when Kavita no longer holds the series. Unreachable, HttpError and SourceError from a
    service propagate. A new series Kavita does not hold yet has no Kavita record or ids.
    """
    conn = ctx.conn
    series = db.one_row(conn, "SELECT * FROM series WHERE id = %s", (series_id,))
    mangas = conn.execute(
        "SELECT manga_id, title FROM suwayomi_manga WHERE series_id = %s ORDER BY manga_id", (series_id,)
    ).fetchall()
    in_kavita = series["kavita_series_id"] is not None
    kavita_series = ctx.kavita.series(series["kavita_series_id"]) if in_kavita else None
    if in_kavita and kavita_series is None:
        return None
    manga_rows = [m for m in (ctx.suwayomi.manga(row["manga_id"]) for row in mangas) if m is not None]
    kavita_meta = None
    kavita_ids: list[ids.Pair] = []
    if kavita_series is not None:  # a new series Kavita does not hold yet has no Kavita record or ids
        kavita_meta = ctx.kavita.metadata(series["kavita_series_id"])
        kavita_ids = ids.kavita_ids(kavita_series, kavita_meta)
    stored = _stored_ids(conn, series_id)
    known = stored + _origin_ids(manga_rows, kavita_ids)
    records = _empty_records()
    try:
        _agreed(known)  # ids that already disagree stop the check before any source is asked
        _described(clients, series, known, manga_rows)
        _fetch(clients, records, series_names(series), known)
    except Disagree as e:
        return Gathered(series, stored, known, records, kavita_meta, manga_rows, str(e))
    return Gathered(series, stored, known, records, kavita_meta, manga_rows, None)


def _put_off(ctx: workflow.Context, series_id: int) -> None:
    """Put off the check of a series Kavita no longer holds by the quiet interval (logged).

    A deleted series never takes a capped check's place.
    """
    conn = ctx.conn
    series = db.one_row(conn, "SELECT * FROM series WHERE id = %s", (series_id,))
    now = workflow.now(conn)
    later = (datetime.fromisoformat(now) + timedelta(days=ctx.cfg["metadata"]["quiet_days"])).isoformat()
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO series_check (series_id, next_check_at) VALUES (%s, %s)
            ON CONFLICT (series_id) DO UPDATE SET next_check_at = excluded.next_check_at, urgent = false""",
            (series["id"], later),
        )
    log.info(
        "series %s (%s): not checked: Kavita no longer holds series %s",
        series["id"],
        series["name"],
        series["kavita_series_id"],
    )


def series_names(series: db.Row) -> list[str]:
    """Return the names a source is searched by.

    The series' Kavita names; a new series' names it arrived under (its manga's title or title folder, a folder's
    ComicInfo Series) and, once admitted, its name.
    """
    names = (
        series["name"],
        series["alt_name"],
        series["localized_name"],
        series["original_name"],
        series["comicinfo_series"],
    )
    return list(dict.fromkeys(n for n in names if n))


def _stored_ids(conn: db.Connection, series_id: int) -> list[ids.Known]:
    """Return the ids no fetch replaces: found by search, imported, set by a person."""
    rows = conn.execute(
        """SELECT source, origin, source_id FROM series_source_id
        WHERE series_id = %s AND origin IN ('search', 'import', 'human')""",
        (series_id,),
    ).fetchall()
    return [tuple(r) for r in rows]


def _origin_ids(manga_rows: Iterable[Manga], kavita_ids: Iterable[ids.Pair]) -> list[ids.Known]:
    known: list[ids.Known] = []
    for manga in manga_rows:
        known += [(s, "suwayomi", v) for s, v in ids.suwayomi_ids(manga)]
    return known + [(s, "kavita", v) for s, v in kavita_ids]


def _stored_platform(conn: db.Connection, series_id: int) -> platforms.PlatformList | None:
    row = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND kind = %s", (series_id, platforms.KIND)
    ).fetchone()
    return row["payload"] if row else None


def _platform(
    clients: sources.Sources,
    series: db.Row,
    kavita_meta: SeriesMetadata | None,
    records: Records,
    stored: platforms.PlatformList | None,
) -> platforms.PlatformList | None:
    """Return the series' platform episode list (SPEC "Metadata", platform lists), or None.

    The first candidate page that has a list. Unreachable (Tapas blocking included) and other HTTP errors end the
    check, as for any source.
    """
    kavita_links = [u.strip() for u in ((kavita_meta or {}).get("webLinks") or "").split(",") if u.strip()]
    anilist_links = platforms.anilist_links(records["anilist"])
    for name, ref in platforms.candidates(kavita_links, anilist_links, series["naming_basis"]):
        if name == platforms.TAPAS:
            known = (stored or {}).get("series_id") if (stored or {}).get("ref") == ref else None
            series_id = known or clients.tapas.series_id(ref)  # the /info page only when the id is not known
            if series_id is not None and (listed := clients.tapas.episodes(series_id)) is not None:
                return platforms.tapas_record(ref, listed[0], listed[1], series_id)
        else:
            items = clients.webtoons.episodes(ref)
            if items is not None:
                return platforms.webtoons_record(ref, items, app_only=clients.webtoons.app_only(ref))
    return None


def _store_platform(conn: db.Connection, series_id: int, platform: platforms.PlatformList | None, now: str) -> None:
    """Store the series' platform list (outside the data hash: a new episode is not a data change).

    A list with fewer main episodes than the stored one of the same page does not replace it (a Webtoons Original cut
    back to its web episodes); no list removes the stored one.
    """
    stored = conn.execute(
        "SELECT source, payload FROM fetched WHERE series_id = %s AND kind = %s", (series_id, platforms.KIND)
    ).fetchone()
    if (
        stored is not None
        and platform is not None
        and stored["payload"]["ref"] == platform["ref"]
        and stored["source"] == platform["name"]
        and platforms.main_episodes(platform) < platforms.main_episodes(stored["payload"])
    ):
        return
    conn.execute("DELETE FROM fetched WHERE series_id = %s AND kind = %s", (series_id, platforms.KIND))
    if platform is not None:
        conn.execute(
            """INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                series_id,
                platform["name"],
                platforms.KIND,
                json.dumps(platform),
                _hash(platform, platform["name"], platforms.KIND),
                now,
            ),
        )


type _Step = Callable[[sources.Sources, Records, list[ids.Known], str], None]  # 1 source's fetch by its id


def _empty_records() -> Records:
    return {
        "mangabaka": None,
        "mangaupdates": None,
        "mangadex": None,
        "mangadex_aggregate": None,
        "mangadex_covers": None,
        "anilist": None,
    }


def _described(clients: sources.Sources, series: db.Row, known: list[ids.Known], manga_rows: Iterable[Manga]) -> None:
    """Add the ids the series' Suwayomi descriptions give to known, as search hits (SPEC "Source ids").

    Only for a source no origin gives an id for, only when the descriptions give exactly 1 id for it, and only once
    that id's record is fetched and is neither a novel's nor gone: a wrong kind's id never joins.
    """
    held = {source for source, _, _ in known}
    given: dict[str, set[str]] = {}
    for manga in manga_rows:
        for source, value in ids.description_ids(manga["description"]):
            given.setdefault(source, set()).add(value)
    for source, values in sorted(given.items()):
        if source in held or len(values) != 1:
            continue
        value = values.pop()
        if _a_comic(clients, source, value):
            known.append((source, "search", value))
            log.info("series %s (%s): %s id %s from a description link", series["id"], series["name"], source, value)
        else:
            log.info(
                "series %s (%s): description link %s %s not used: a novel's or gone",
                series["id"],
                series["name"],
                source,
                value,
            )


def _a_comic(clients: sources.Sources, source: str, value: str) -> bool:
    """Return whether the source holds a record of that id and it is not a novel's (MangaDex holds only comics)."""
    if source == "anilist":
        media = clients.anilist.media(value)
        return media is not None and not _novel(media, "format")
    if source == "mangaupdates":
        record = clients.mangaupdates.series(value)
        return record is not None and not _novel(record, "type")
    return clients.mangadex.manga(value) is not None


def _fetch(clients: sources.Sources, records: Records, names: list[str], known: list[ids.Known]) -> None:
    """Fetch every source's records into records.

    known grows with each search hit (origin search) and each record's cross-links. Disagree as soon as 2 ids of a
    source differ. A source with no id at its turn (none agreed, its search missed) is fetched once the others are,
    by the id a later record's cross-links gave it; no search then, and no third round. MangaBaka and MangaUpdates
    are searched by the names and their suffix-stripped variants; AniList and MangaDex by `search.lookup_names`,
    which adds the titles of the records fetched before their turn.
    """
    own = search.unique(names)
    plain = search.variants(own)
    turns: tuple[tuple[str, _Step, Callable[[], str | None]], ...] = (
        ("mangabaka", _mangabaka, lambda: search.mangabaka(clients.mangabaka, plain, _agreed(known))),
        ("mangaupdates", _mangaupdates, lambda: search.mangaupdates(clients.mangaupdates, plain)),
        (
            "mangadex",
            _mangadex,
            lambda: search.mangadex(clients.mangadex, search.lookup_names(own, records), _agreed(known)),
        ),
        ("anilist", _anilist, lambda: search.anilist(clients.anilist, search.lookup_names(own, records))),
    )
    missed: list[tuple[str, _Step]] = []
    for source, step, searcher in turns:
        source_id = _id_for(known, source, searcher)
        if source_id is None:
            missed.append((source, step))
        else:
            step(clients, records, known, source_id)
    for source, step in missed:
        if (source_id := _agreed(known).get(source)) is not None:
            step(clients, records, known, source_id)
    _agreed(known)


def _mangabaka(clients: sources.Sources, records: Records, known: list[ids.Known], mb_id: str) -> None:
    if (mb := _comic(clients.mangabaka.series(mb_id), "type", mb_id)) is not None:
        records["mangabaka"] = mb
        _learn(known, "mangabaka", ids.mangabaka_ids(mb))


def _mangaupdates(clients: sources.Sources, records: Records, _known: list[ids.Known], mu_id: str) -> None:
    records["mangaupdates"] = _comic(clients.mangaupdates.series(mu_id), "type", mu_id)


def _mangadex(clients: sources.Sources, records: Records, known: list[ids.Known], md_id: str) -> None:
    if (md := clients.mangadex.manga(md_id)) is not None:
        records["mangadex"] = md
        records["mangadex_aggregate"] = clients.mangadex.aggregate(md_id)
        records["mangadex_covers"] = clients.mangadex.covers(md_id)
        _learn(known, "mangadex", ids.mangadex_ids(md))


def _anilist(clients: sources.Sources, records: Records, known: list[ids.Known], al_id: str) -> None:
    if (al := _comic(clients.anilist.media(al_id), "format", al_id)) is not None:
        records["anilist"] = al
        _learn(known, "anilist", ids.anilist_ids(al))


def _agreed(known: Iterable[ids.Known]) -> dict[str, str]:
    """Each source's id the known ids agree on (ids.resolve); Disagree when 2 ids of a source differ."""
    result, problem = ids.resolve(known)
    if problem:
        raise Disagree(problem)
    return result


def _id_for(known: list[ids.Known], source: str, searcher: Callable[[], str | None]) -> str | None:
    """Return the source's agreed id, else the id searcher() finds, else None.

    A found id is then known as a search hit, and checked to agree.
    """
    value = _agreed(known).get(source)
    if value is None:
        value = searcher()
        if value is not None:
            known.append((source, "search", value))
            _agreed(known)
    return value


def _learn(known: list[ids.Known], source: str, pairs: Iterable[ids.Pair]) -> None:
    """Add a source's record's cross-links (other sources' ids, by that source's origin) to known."""
    known.extend((s, source, v) for s, v in pairs if s != source)


def _novel(record: MangaBakaSeries | MangaUpdatesSeries | AniListMedia, field: str) -> bool:
    """Return whether a source's record is a novel's: its kind field (`type`, AniList's `format`) says novel."""
    kind = record.get(field)
    return isinstance(kind, str) and kind.lower() == "novel"


def _comic[R: (MangaBakaSeries, MangaUpdatesSeries, AniListMedia)](
    record: R | None, field: str, source_id: str
) -> R | None:
    """Return the record, or None when it is a novel's (an id that led to a manga's light novel).

    The library is comics. A dropped record is logged; a person fixes the id with `series set-id`.
    """
    if record is not None and _novel(record, field):
        log.warning("record %s is a novel's: not used (fix the series' id with `series set-id`)", source_id)
        return None
    return record


def _title_change(conn: db.Connection, series: db.Row, media: AniListMedia | None) -> str | None:
    """Queue the report of a changed AniList English title and return its text; None when unchanged.

    SPEC "Metadata checks": a changed title is reported (log, `status`, Discord) as a name waiting for `series rename`.
    Only the same AniList id's record is compared, both titles set. Inside the store transaction, before the record is
    stored.
    """
    if series["kavita_series_id"] is None and series["admitted_at"] is None:
        return None  # a new series not admitted yet is named from its record when it is admitted
    row = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND source = 'anilist' AND kind = 'media'", (series["id"],)
    ).fetchone()
    old: AniListMedia | None = row["payload"] if row else None
    if old is None or media is None or old["id"] != media["id"]:
        return None
    before, after = series_name.anilist_english(old), series_name.anilist_english(media)
    if not before or not after or before == after:
        return None
    text = f'series {series["id"]} ({series["name"]}): name waiting: "{before}" -> "{after}"'
    notify.queue(conn, "title", series["id"], text, f"series rename {series['id']}")
    return text


def _ids_changed(conn: db.Connection, series: db.Row, stored: list[ids.Known]) -> bool:
    """Return whether the series' stored ids are no longer `stored`, read when its check began (logged).

    A person changed them while the check fetched (`series set-id`): the check stores nothing, so their change is
    never undone, and stays due. Inside the store transaction, which no other write overlaps.
    """
    if set(_stored_ids(conn, series["id"])) == set(stored):
        return False
    log.info("series %s (%s): not stored: its ids changed while it was checked", series["id"], series["name"])
    return True


def _store_disagreement(
    conn: db.Connection, series: db.Row, stored: list[ids.Known], known: Iterable[ids.Known], problem: str
) -> bool:
    """Store the ids and hold the series (needs_human); returns False, storing nothing, when its ids changed."""
    with db.transaction(conn):
        if _ids_changed(conn, series, stored):
            return False
        _store_ids(conn, series["id"], known)
        workflow.needs_human(
            conn, "series", series["id"], f"{problem} -> fix in Kavita or `series set-id SERIES SOURCE ID`"
        )
    return True


def _store_ids(conn: db.Connection, series_id: int, known: Iterable[ids.Known]) -> None:
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


def _store(conn: db.Connection, series_id: int, known: Iterable[ids.Known], records: Records, now: str) -> bool:
    """Store the ids and records; returns whether any record's hash changed (or is new)."""
    _store_ids(conn, series_id, known)
    old = {
        (r["source"], r["kind"]): r["payload_hash"]
        for r in conn.execute("SELECT source, kind, payload_hash FROM fetched WHERE series_id = %s", (series_id,))
    }
    rows = [
        (series_id, *STORED[name], None if p is None else json.dumps(p), _hash(p, *STORED[name]), now)
        for name, p in records.items()
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


def _hash(payload: object, source: str, kind: str) -> str:
    """Return the sha256 of the payload less the VOLATILE key paths of its (source, kind)."""
    paths = VOLATILE.get((source, kind), ())
    kept = copy.deepcopy(payload) if paths else payload
    for path in paths:
        _drop(kept, path)
    return hashlib.sha256(json.dumps(kept, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _drop(value: object, path: tuple[str, ...]) -> None:
    """Delete a key path from a decoded JSON value in place ("[]": each list item, "*": each key); absent parts skip."""
    head, rest = path[0], path[1:]
    if not rest:
        if _is_object(value):
            value.pop(head, None)
        return
    children: list[object] = []
    if head == "[]" and _is_array(value):
        children = value
    elif _is_object(value):
        children = list(value.values()) if head == "*" else [value.get(head)]
    for child in children:
        _drop(child, rest)


def _scrub_records(series: db.Row, records: Records) -> None:
    """Scrub each fetched record (_scrub) in place, logging each one that held what PostgreSQL cannot store."""
    for name, record in records.items():
        if _scrub(record):
            log.warning("series %s (%s): %s record: %s", series["id"], series["name"], name, SCRUBBED)


def _scrub(value: object) -> bool:
    """Remove every NUL and lone surrogate from the texts of a decoded JSON value in place, keys included.

    PostgreSQL's text and jsonb hold neither (json.loads joins a surrogate pair, so a surrogate left is lone). Returns
    whether anything was removed.
    """
    changed = False
    if _is_array(value):
        for i, item in enumerate(value):
            value[i], dirty = _scrubbed(item)
            changed |= dirty
    elif _is_object(value):
        entries = list(value.items())
        value.clear()
        for key, item in entries:
            clean_key = _clean(key)
            value[clean_key], dirty = _scrubbed(item)
            changed |= dirty or clean_key != key
    return changed


def _is_object(value: object) -> TypeIs[dict[str, object]]:
    """Return whether a decoded JSON value is an object (json.loads' dict, its keys texts)."""
    return isinstance(value, dict)


def _is_array(value: object) -> TypeIs[list[object]]:
    """Return whether a decoded JSON value is an array (json.loads' list)."""
    return isinstance(value, list)


def _scrubbed(value: object) -> tuple[object, bool]:
    """Return the value scrubbed (a text as a new, clean text) and whether anything was removed."""
    if isinstance(value, str):
        kept = _clean(value)
        return kept, kept != value
    return value, _scrub(value)


def _clean(text: str) -> str:
    return "".join(c for c in text if c != "\x00" and not "\ud800" <= c <= "\udfff")


def _last_update(
    ctx: workflow.Context, clients: sources.Sources, manga_rows: Iterable[Manga], records: Records
) -> datetime | None:
    """Return the series' last published update (SPEC "Metadata checks") as an aware datetime, or None."""
    dates: list[int] = []
    for manga in manga_rows:
        dates += [int(c["uploadDate"]) for c in ctx.suwayomi.chapters(manga["id"]) if c.get("uploadDate")]
    if dates:
        return datetime.fromtimestamp(max(dates) / 1000, UTC)
    manga = records["mangadex"]
    if not manga_rows and manga is not None:
        latest = clients.mangadex.latest_chapter_date(manga["id"])
        return datetime.fromisoformat(latest) if latest else None
    return None


def next_check(cfg: config.Config, now: str, last: datetime | None, *, complete: bool) -> str:
    """Return when a series checked at `now` (ISO) is next due.

    recent_days while its last chapter (`last`) is recent, complete_days for a complete series quiet long enough, else
    quiet_days; quiet_days too with no last chapter, complete or not.
    """
    m = cfg["metadata"]
    at = datetime.fromisoformat(now)
    age = at - last if last is not None else None
    if age is None:
        days = m["quiet_days"]
    elif age < timedelta(days=m["recent_months"] * DAYS_PER_MONTH):
        days = m["recent_days"]
    elif complete and age >= timedelta(days=m["complete_months"] * DAYS_PER_MONTH):
        days = m["complete_days"]
    else:
        days = m["quiet_days"]
    return (at + timedelta(days=days)).isoformat(timespec="seconds")
