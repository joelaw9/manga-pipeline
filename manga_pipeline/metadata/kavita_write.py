"""Series fields to Kavita (SPEC "Metadata", decisions D7.10, D7.11, D7.22): export writes a series' fields through
the Kavita API, locked, when the data, the tag rules or the field merge they come from changed; writes only the fields
that differ; uploads its cover when it changed.

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
from pathlib import Path

from manga_pipeline import config, db, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.db import sql
from manga_pipeline.metadata import ids, merge, tags

log = logging.getLogger(__name__)
FINGERPRINT = "metadata.tag_fingerprint"  # meta: the tag rules' fingerprint the last export run worked with
INCOMPLETE = ""  # series_written.inputs of a write that has not finished: still due
# field: (Kavita value key, Kavita lock key, kind); kind: text, number, names (list of {title}), people ({name})
SCALARS = {
    "summary": ("summary", "summaryLocked"),
    "status": ("publicationStatus", "publicationStatusLocked"),
    "age_rating": ("ageRating", "ageRatingLocked"),
    "language": ("language", "languageLocked"),
}
LISTS = {
    "genres": ("genres", "genresLocked", "title"),
    "tags": ("tags", "tagsLocked", "title"),
}
# People are only added: Kavita keeps the people its scans read from the files whatever a metadata update sends
# (seen on Kavita 0.9.1, 2026-11-15), so a list never matches the sources' exactly and is never guarded or pruned.
PEOPLE = {
    "writers": ("writers", "writerLocked"),
    "pencillers": ("pencillers", "pencillerLocked"),
}
IMPORTED = {"summary": "summary", "genres": "genres", "tags": "tags"}  # our field: the old tool's `fields` name
COVER = "cover"  # the name a kept hand-uploaded cover is reported under


# The code that makes and writes the fields: a change to it rewrites every series' fields (only what differs)
CODE = hashlib.sha256(Path(merge.__file__).read_bytes() + Path(__file__).read_bytes()).hexdigest()


def inputs(version, rules):
    """What a series' written fields come from: its data version, the tag rules and this code."""
    return f"{version}:{rules.fingerprint}:{CODE}"


def due(ctx, rules):
    """Ids of the in-scope Kavita series with a completed check whose written fields or cover are out of date, or
    written to another Kavita series (Kavita recreated it)."""
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


def run(ctx):
    """Write every due series' fields; a series Kavita refuses, or whose cover file cannot be read, is logged and stays
    due. Kavita unreachable ends the step (and the run, as everything after it needs Kavita)."""
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
            if isinstance(e, OSError) and workflow.dropped_share(e):
                raise
            log.warning("series %s: fields not written: %s", series_id, e)


def write(ctx, series_id, rules):
    conn, kv = ctx.conn, ctx.kavita
    series = conn.execute("SELECT * FROM series WHERE id = %s", (series_id,)).fetchone()
    kid = series["kavita_series_id"]
    if kv.series(kid) is None:
        log.info("series %s (%s): not written: Kavita no longer holds series %s", series_id, series["name"], kid)
        return
    fields = merge.stored(conn, series_id, rules)
    version = conn.execute("SELECT data_version FROM series_check WHERE series_id = %s", (series_id,)).fetchone()[0]
    before = conn.execute("SELECT * FROM series_written WHERE series_id = %s", (series_id,)).fetchone()
    if before is not None and before["kavita_series_id"] != kid:
        before = None  # written to a Kavita series that is gone: this one holds none of it
    imported = conn.execute("SELECT * FROM series_import WHERE series_id = %s", (series_id,)).fetchone()
    state = {
        "written": dict(before["written"]) if before else {},
        "kept": set(before["kept"]) if before else set(),
        "cover": before["cover_sha256"] if before else None,
    }

    def record(*, final=False):
        _record(conn, series_id, kid, inputs(version, rules) if final else INCOMPLETE, state)

    guard = _Guard(series, state, imported)
    meta = kv.metadata(kid)
    changed = False
    for name, (key, lock) in SCALARS.items():
        value = getattr(fields, "age_rating_kavita" if name == "age_rating" else name)
        if value is None or not guard.may(name, meta.get(lock), _scalar(meta.get(key)), value):
            continue
        if meta.get(key) != value or not meta.get(lock):
            meta[key], meta[lock] = value, True
            changed = True
        state["written"][name] = value
    if changed:
        kv.update_metadata(meta)
    record()
    for name, list_field in LISTS.items():
        key, lock, label = list_field
        values = getattr(fields, name)
        if not values:
            continue  # a source that gives nothing never clears a list
        meta = kv.metadata(kid)
        if not guard.may(name, meta.get(lock), _list_key(meta.get(key), label), _key(values)):
            continue

        def added(key_now, name=name):
            state["written"][name] = key_now  # what Kavita holds once the add step went through
            record()

        _set_list(kv, kid, list_field, values, added)
        state["written"][name] = _key(values)
        record()
    for name, (key, lock) in PEOPLE.items():
        _add_people(kv, kid, key, lock, getattr(fields, name))
    _links_and_ids(kv, kid, fields)
    state["cover"] = _cover(ctx, series, kid, state, imported, guard=guard, record=record)
    record(final=True)
    log.info("series %s (%s): fields written to Kavita", series_id, series["name"])


class _Guard:
    """D7.22: whether a field may be written over Kavita's value; a person's locked edit is reported once."""

    def __init__(self, series, state, imported):
        self.series, self.state, self.imported = series, state, imported

    def may(self, name, locked, current, ours):
        if not locked or current == ours or self._ours(name, current):
            self.state["kept"].discard(name)
            return True
        if name not in self.state["kept"]:
            log.warning(
                "series %s (%s): %s locked by hand at %r: kept", self.series["id"], self.series["name"], name, current
            )
            self.state["kept"].add(name)
        return False

    def _ours(self, name, current):
        """Whether a locked value is one the pipeline wrote (or, for a field it never wrote, the old tool)."""
        written = self.state["written"]
        if name in written:
            return written[name] == current
        if self.imported is None:
            return False
        if name == "status":
            return self.imported["status"] is not None and self.imported["status"] == current
        return IMPORTED.get(name) in (self.imported["fields"] or [])


def _scalar(value):
    return value if value not in ("", None) else None


def _key(values):
    """A list's comparison key: its entries under Kavita's name normalising, sorted."""
    return sorted({normalize(v) for v in values})


def _list_key(entries, label):
    return _key(v[label] for v in entries or []) or None


def _set_list(kv, kid, list_field, values, added_step):
    """Add the missing entries (in the sources' spelling), then drop the others, in 2 updates of this list alone;
    lock it. list_field: its LISTS entry (key, lock, label). added_step(key) is told what Kavita holds after the add
    step, before the prune can fail."""
    key, lock, label = list_field
    wanted = {normalize(v) for v in values}
    meta = kv.metadata(kid)
    have = {normalize(v[label]) for v in meta.get(key) or []}
    added = [v for v in dict.fromkeys(values) if normalize(v) not in have]
    if added or not meta.get(lock):
        meta[key] = (meta.get(key) or []) + [{"id": 0, label: v} for v in added]
        meta[lock] = True
        kv.update_metadata(meta)
        added_step(_list_key(meta[key], label))
    if have - wanted:
        meta = kv.metadata(kid)
        meta[key], meta[lock] = [v for v in meta.get(key) or [] if normalize(v[label]) in wanted], True
        kv.update_metadata(meta)


def _add_people(kv, kid, key, lock, names):
    """Add the sources' people Kavita lacks (by its name key), locked."""
    meta = kv.metadata(kid)
    have = {normalize(p["name"]) for p in meta.get(key) or []}
    added = [n for n in dict.fromkeys(names) if normalize(n) not in have]
    if added or (names and not meta.get(lock)):
        meta[key], meta[lock] = (meta.get(key) or []) + [{"id": 0, "name": n} for n in added], True
        kv.update_metadata(meta)


def _links_and_ids(kv, kid, fields):
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


def _cover(ctx, series, kid, state, imported, *, guard, record):
    """Upload the series' cover when Kavita does not hold it from the pipeline; the sha256 Kavita now holds from it.
    A cover uploaded to Kavita before the pipeline's first upload, and not by the old tool, is a person's: kept.
    (Kavita names every upload alike, so a person's upload after the pipeline's is not told apart.)"""
    row = ctx.conn.execute("SELECT path, sha256 FROM series_cover WHERE series_id = %s", (series["id"],)).fetchone()
    if row is None:
        return state["cover"]
    held = state["cover"] or (imported["cover_sha256"] if imported else None)
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


def _record(conn, series_id, kid, inputs_text, state):
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
