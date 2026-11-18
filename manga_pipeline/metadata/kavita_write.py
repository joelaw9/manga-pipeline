"""Series fields to Kavita (SPEC "Metadata", decisions D7.10, D7.11, D7.22): written through its API when they change.

Export writes a series' fields through the Kavita API, locked, when the data, the tag rules or the field merge they
come from changed; writes only the fields that differ; uploads its cover when it changed.

A locked Kavita field is overwritten only when it holds the value the pipeline last wrote, or, for a field the
pipeline never wrote, a value the old metadata tool wrote (`series_import`). Any other locked value is a person's
edit: kept, and reported once. Each field is recorded as soon as Kavita accepts it (a list after its add step too),
so a write that fails partway never makes the pipeline's own value look like a person's.

Kavita fails an update that changes genres and tags together, or removes and adds several entries of one list, so each
list has its own updates: add, then prune (the old tool's set_list). Lists are compared by Kavita's own name key.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, TypedDict

from manga_pipeline import config, db, errors, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids, merge, tags
from manga_pipeline.replies.kavita import Person, Tag

log = logging.getLogger(__name__)
FINGERPRINT = "metadata.tag_fingerprint"  # meta: the tag rules' fingerprint the last export run worked with
INCOMPLETE = ""  # series_written.inputs of a write that has not finished: still due
type _Value = str | int | list[str]  # a field's value as written: a scalar, or a list's comparison key (_key)
# The scalar fields (summary, status, age rating, language) are written by _write_scalars, 1 by 1.
# field: (Kavita's list key, its lock key, the field's names); each entry {id, title}
LISTS: dict[
    str,
    tuple[
        Literal["genres", "tags"],
        Literal["genresLocked", "tagsLocked"],
        Callable[[merge.SeriesFields], tuple[str, ...]],
    ],
] = {
    "genres": ("genres", "genresLocked", lambda f: f.genres),
    "tags": ("tags", "tagsLocked", lambda f: f.tags),
}
# People are only added: Kavita keeps the people its scans read from the files whatever a metadata update sends
# (seen on Kavita 0.9.1, 2026-11-15), so a list never matches the sources' exactly and is never guarded or pruned.
# field: (Kavita's people key, its lock key, the field's names); each entry {id, name}
PEOPLE: dict[
    str,
    tuple[
        Literal["writers", "pencillers"],
        Literal["writerLocked", "pencillerLocked"],
        Callable[[merge.SeriesFields], tuple[str, ...]],
    ],
] = {
    "writers": ("writers", "writerLocked", lambda f: f.writers),
    "pencillers": ("pencillers", "pencillerLocked", lambda f: f.pencillers),
}
IMPORTED = {"summary": "summary", "genres": "genres", "tags": "tags"}  # our field: the old tool's `fields` name
COVER = "cover"  # the name a kept hand-uploaded cover is reported under


# The code that makes and writes the fields: a change to it rewrites every series' fields (only what differs)
CODE = hashlib.sha256(Path(merge.__file__).read_bytes() + Path(__file__).read_bytes()).hexdigest()


class _State(TypedDict):
    """What the pipeline wrote to a Kavita series: {field: value}, the fields a person's locked edit keeps, cover."""

    written: dict[str, _Value]
    kept: set[str]
    cover: str | None  # sha256


def inputs(version: int, rules: tags.Rules) -> str:
    """Return what a series' written fields come from: its data version, the tag rules and this code."""
    return f"{version}:{rules.fingerprint}:{CODE}"


def due(ctx: workflow.Context, rules: tags.Rules) -> list[int]:
    """Return the ids of the in-scope Kavita series with a completed check whose fields or cover need writing.

    Their written fields or cover are out of date, or written to another Kavita series (Kavita recreated it).
    """
    rows = ctx.conn.execute(
        sql.SQL("""SELECT s.id FROM series s JOIN series_check k ON k.series_id = s.id
        LEFT JOIN series_written w ON w.series_id = s.id LEFT JOIN series_cover c ON c.series_id = s.id
        WHERE s.status = 'ok' AND s.kavita_series_id IS NOT NULL AND k.data_version > 0 AND {scope}
            AND (w.series_id IS NULL OR w.inputs <> k.data_version || ':' || %(tags)s || ':' || %(merge)s
                OR w.kavita_series_id <> s.kavita_series_id OR c.sha256 IS DISTINCT FROM w.cover_sha256)
        ORDER BY s.id""").format(scope=workflow.SCOPE),
        {**workflow.scope_params(ctx.cfg), "tags": rules.fingerprint, "merge": CODE},
    ).fetchall()
    return [r[0] for r in rows]


def run(ctx: workflow.Context) -> None:
    """Write every due series' fields.

    A series Kavita refuses, or whose cover file cannot be read, is logged and stays due. Kavita unreachable ends the
    step (and the run, as everything after it needs Kavita).
    """
    rules = tags.load()
    previous = workflow.get_meta(ctx.conn, FINGERPRINT)
    if previous != rules.fingerprint:
        log.info("%s: tag rules changed: every series' fields are worked out again", ctx.part)
        with db.transaction(ctx.conn):
            workflow.set_meta(ctx.conn, FINGERPRINT, rules.fingerprint)
    for series_id in due(ctx, rules):
        try:
            write(ctx, series_id, rules)
        except (http.HttpError, kavita_client.CoverError, FileNotFoundError) as e:
            if isinstance(e, OSError) and errors.dropped_share(e):
                raise
            log.warning("series %s: fields not written: %s", series_id, e)


def write(ctx: workflow.Context, series_id: int, rules: tags.Rules) -> None:
    """Write 1 series' fields to its Kavita series: scalars, lists, people, links and ids, then its cover.

    Nothing is written when Kavita no longer holds the series. What was written is recorded as it goes, and the
    series' inputs once all of it is.
    """
    conn, kv = ctx.conn, ctx.kavita
    series = db.one_row(conn, "SELECT * FROM series WHERE id = %s", (series_id,))
    kid = series["kavita_series_id"]
    if kv.series(kid) is None:
        log.info("series %s (%s): not written: Kavita no longer holds series %s", series_id, series["name"], kid)
        return
    fields = merge.stored(conn, series_id, rules)
    version = db.one_row(conn, "SELECT data_version FROM series_check WHERE series_id = %s", (series_id,))[0]
    imported = conn.execute("SELECT * FROM series_import WHERE series_id = %s", (series_id,)).fetchone()
    state = _state(conn, series_id, kid)

    def record(*, final: bool = False) -> None:
        _record(conn, series_id, kid, inputs(version, rules) if final else INCOMPLETE, state)

    guard = _Guard(series, state, imported)
    _write_scalars(kv, kid, fields, guard)
    record()
    _write_lists(kv, kid, fields, guard, record)
    for key, lock, names_of in PEOPLE.values():
        _add_people(kv, kid, key, lock, names_of(fields))
    _links_and_ids(kv, kid, fields)
    state["cover"] = _cover(ctx, kid, guard, record)
    record(final=True)
    log.info("series %s (%s): fields written to Kavita", series_id, series["name"])


def _state(conn: db.Connection, series_id: int, kid: int) -> _State:
    """Return what the pipeline wrote to Kavita series kid before.

    {"written": {field: value}, "kept": {field a person's locked edit keeps}, "cover": sha256 or None}; nothing when it
    wrote to a Kavita series that is gone.
    """
    before = conn.execute("SELECT * FROM series_written WHERE series_id = %s", (series_id,)).fetchone()
    if before is None or before["kavita_series_id"] != kid:
        return {"written": {}, "kept": set(), "cover": None}
    return {"written": dict(before["written"]), "kept": set(before["kept"]), "cover": before["cover_sha256"]}


def _write_scalars(kv: kavita_client.Kavita, kid: int, fields: merge.SeriesFields, guard: "_Guard") -> None:
    """Write the scalar fields that differ from Kavita's or are unlocked, in 1 update, where the guard lets them.

    Each one written goes in the guard's state. In order: summary, status, age rating, language.
    """
    meta = kv.metadata(kid)
    changed = False
    if _scalar_due(guard, "summary", fields.summary, meta.get("summary"), locked=meta.get("summaryLocked")):
        meta["summary"], meta["summaryLocked"] = fields.summary, True
        changed = True
    if _scalar_due(
        guard, "status", fields.status, meta.get("publicationStatus"), locked=meta.get("publicationStatusLocked")
    ):
        meta["publicationStatus"], meta["publicationStatusLocked"] = fields.status, True
        changed = True
    if _scalar_due(
        guard, "age_rating", fields.age_rating_kavita, meta.get("ageRating"), locked=meta.get("ageRatingLocked")
    ):
        meta["ageRating"], meta["ageRatingLocked"] = fields.age_rating_kavita, True
        changed = True
    if _scalar_due(guard, "language", fields.language, meta.get("language"), locked=meta.get("languageLocked")):
        meta["language"], meta["languageLocked"] = fields.language, True
        changed = True
    if changed:
        kv.update_metadata(meta)


def _write_lists(
    kv: kavita_client.Kavita, kid: int, fields: merge.SeriesFields, guard: "_Guard", record: Callable[[], None]
) -> None:
    """Write each list field the sources give, where the guard lets it (_set_list).

    Recorded after its add step and once it is whole. A source that gives nothing never clears a list.
    """
    written = guard.state["written"]
    for name, (key, lock, names_of) in LISTS.items():
        values = names_of(fields)
        if not values:
            continue
        meta = kv.metadata(kid)
        if not guard.may(name, locked=meta.get(lock), current=_list_key(meta.get(key)), ours=_key(values)):
            continue

        def added(key_now: list[str], name: str = name) -> None:
            written[name] = key_now  # what Kavita holds once the add step went through
            record()

        _set_list(kv, kid, name, values, added_step=added)
        written[name] = _key(values)
        record()


class _Guard:
    """D7.22: whether a field may be written over Kavita's value; a person's locked edit is reported once."""

    def __init__(self, series: db.Row, state: _State, imported: db.Row | None) -> None:
        self.series, self.state, self.imported = series, state, imported

    def may(self, name: str, *, locked: bool | None, current: _Value | None, ours: _Value) -> bool:
        if not locked or current == ours or self._ours(name, current):
            self.state["kept"].discard(name)
            return True
        if name not in self.state["kept"]:
            log.warning(
                "series %s (%s): %s locked by hand at %r: kept", self.series["id"], self.series["name"], name, current
            )
            self.state["kept"].add(name)
        return False

    def _ours(self, name: str, current: _Value | None) -> bool:
        """Whether a locked value is one the pipeline wrote (or, for a field it never wrote, the old tool)."""
        written = self.state["written"]
        if name in written:
            return written[name] == current
        if self.imported is None:
            return False
        if name == "status":
            return self.imported["status"] is not None and self.imported["status"] == current
        return IMPORTED.get(name) in (self.imported["fields"] or [])

    def held_cover(self) -> str | None:
        """Return the sha256 of the cover Kavita holds from the pipeline, or before its first upload from the old tool.

        None: neither.
        """
        return self.state["cover"] or (self.imported["cover_sha256"] if self.imported else None)


def _scalar(value: str | int | None) -> str | int | None:
    return value if value not in ("", None) else None


def _scalar_due(
    guard: _Guard, name: str, value: str | int | None, current: str | int | None, *, locked: bool | None
) -> bool:
    """Return whether a scalar field's value is to be written over Kavita's (current, locked or not).

    Not when the sources give none or the guard keeps Kavita's; a value the guard lets is recorded as written, and is
    written when it differs from Kavita's or Kavita's is unlocked.
    """
    if value is None or not guard.may(name, locked=locked, current=_scalar(current), ours=value):
        return False
    guard.state["written"][name] = value
    return current != value or not locked


def _key(values: Iterable[str]) -> list[str]:
    """Return a list's comparison key: its entries under Kavita's name normalising, sorted."""
    return sorted({normalize(v) for v in values})


def _list_key(entries: Iterable[Tag] | None) -> list[str] | None:
    return _key(v["title"] for v in entries or []) or None


def _set_list(
    kv: kavita_client.Kavita,
    kid: int,
    name: str,
    values: tuple[str, ...],
    *,
    added_step: Callable[[list[str]], None],
) -> None:
    """Add the missing entries of list field name (LISTS) in the sources' spelling, then drop the others; lock it.

    2 updates of this list alone. added_step(key) is told what Kavita holds after the add step, before the prune can
    fail.
    """
    key, lock, _ = LISTS[name]
    wanted = {normalize(v) for v in values}
    meta = kv.metadata(kid)
    held: list[Tag] = meta.get(key) or []
    have = {normalize(v["title"]) for v in held}
    added = [v for v in dict.fromkeys(values) if normalize(v) not in have]
    if added or not meta.get(lock):
        entries: list[Tag] = held + [{"id": 0, "title": v} for v in added]
        meta[key] = entries
        meta[lock] = True
        kv.update_metadata(meta)
        added_step(_key(v["title"] for v in entries))  # entries is never empty: values is not
    if have - wanted:
        meta = kv.metadata(kid)
        now: list[Tag] = meta.get(key) or []
        meta[key], meta[lock] = [v for v in now if normalize(v["title"]) in wanted], True
        kv.update_metadata(meta)


def _add_people(
    kv: kavita_client.Kavita,
    kid: int,
    key: Literal["writers", "pencillers"],
    lock: Literal["writerLocked", "pencillerLocked"],
    names: tuple[str, ...],
) -> None:
    """Add the sources' people Kavita lacks (by its name key), locked."""
    meta = kv.metadata(kid)
    held: list[Person] = meta.get(key) or []
    have = {normalize(p["name"]) for p in held}
    added = [n for n in dict.fromkeys(names) if normalize(n) not in have]
    if added or (names and not meta.get(lock)):
        meta[key], meta[lock] = held + [{"id": 0, "name": n} for n in added], True
        kv.update_metadata(meta)


def _links_and_ids(kv: kavita_client.Kavita, kid: int, fields: merge.SeriesFields) -> None:
    """Web links appended (a link to a source already there is kept as it is); the provider ids set when known."""
    meta = kv.metadata(kid)
    links = [u.strip() for u in (meta.get("webLinks") or "").split(",") if u.strip()]
    have = {ids.link_id(u) or u for u in links}
    added = [u for u in fields.links if (ids.link_id(u) or u) not in have]
    if added:
        meta["webLinks"] = ",".join(links + added)
        kv.update_metadata(meta)
    series = kv.series(kid)
    if series is None:
        return
    changes = {k: v for k, v in fields.kavita_ids.items() if v is not None and series.get(k) != v}
    if changes:
        kv.update_series(series, **changes)


def _cover(ctx: workflow.Context, kid: int, guard: _Guard, record: Callable[[], None]) -> str | None:
    """Upload the series' cover when Kavita does not hold it from the pipeline; return the sha256 Kavita now holds.

    A cover uploaded to Kavita before the pipeline's first upload, and not by the old tool, is a person's: kept.
    (Kavita names every upload alike, so a person's upload after the pipeline's is not told apart.)
    """
    series, state = guard.series, guard.state
    row = ctx.conn.execute("SELECT path, sha256 FROM series_cover WHERE series_id = %s", (series["id"],)).fetchone()
    if row is None:
        return state["cover"]
    held = guard.held_cover()
    uploaded = ctx.kavita.has_uploaded_cover(kid)
    if uploaded and (held is None or COVER in state["kept"]):
        guard.may(COVER, locked=True, current="a cover uploaded by hand", ours=row["sha256"])
        return row["sha256"]  # settled: the person's cover stays, and the series is not due again for it
    if held == row["sha256"] and uploaded:
        return held
    data = config.to_local(ctx.cfg, row["path"]).read_bytes()
    state["cover"] = row["sha256"]  # recorded first: a verification that fails must not make it a person's cover
    record()
    ctx.kavita.upload_cover(kid, data)
    state["kept"].discard(COVER)
    log.info("series %s: cover uploaded to Kavita", series["id"])
    return row["sha256"]


def _record(conn: db.Connection, series_id: int, kid: int, inputs_text: str, state: _State) -> None:
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO series_written (series_id, kavita_series_id, inputs, written, kept, cover_sha256, written_at)
            VALUES (%(id)s, %(kid)s, %(inputs)s, %(written)s, %(kept)s, %(cover)s, %(now)s)
            ON CONFLICT (series_id) DO UPDATE SET kavita_series_id = excluded.kavita_series_id,
                inputs = excluded.inputs, written = excluded.written, kept = excluded.kept,
                cover_sha256 = excluded.cover_sha256, written_at = excluded.written_at""",
            {
                "id": series_id,
                "kid": kid,
                "inputs": inputs_text,
                "written": json.dumps(state["written"]),
                "kept": json.dumps(sorted(state["kept"])),
                "cover": state["cover"],
                "now": workflow.now(conn),
            },
        )
