"""A series' baseline (SPEC "State", "Publishing", "Calibration").

What a series Kavita already holds is taken from its bulk-converter record before anything is published into it, or,
with no record, adopted from its own files (adoption.py). Taken at validate (`intake`) and by the daily baseline pass;
no library file is opened for a record series.
"""

import json
import logging
from pathlib import Path, PurePosixPath

from manga_pipeline import adoption, config, db, errors, names, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.convert import calibrate, record_reader
from manga_pipeline.junk import importing, store

log = logging.getLogger(__name__)
# A source's fate in the record -> its handled_chapter outcome.
OUTCOME = {
    record_reader.CHAPTER: "published",
    record_reader.MERGED: "published",
    record_reader.CARRIED: "published",
    record_reader.FAILED: "published",
    record_reader.DUPLICATE: "duplicate",
    record_reader.REPLACED: "duplicate",
    record_reader.DELETED: "dropped",
}
LAYOUTS = ("strip", "pages")


def needed(conn: db.Connection, series: db.Row) -> bool:
    """Return whether the series must have a baseline before its chapters go on.

    True for a series Kavita holds that has none, or whose bulk rows were recorded before their source name, kind and
    volume source were kept (schema step 0054).
    """
    if series["origin"] != "kavita":
        return False
    if series["baseline_at"] is None:
        return True
    return db.one_row(
        conn,
        "SELECT EXISTS (SELECT 1 FROM published_file WHERE series_id = %s AND origin = 'bulk' AND kind IS NULL)",
        (series["id"],),
    )[0]


def has_record(cfg: config.Config, folder: str) -> bool:
    """Return whether a series folder (stored form) holds a bulk-converter record (parsing or not)."""
    return errors.exists(config.to_local(cfg, folder) / record_reader.RECORD_NAME)


def with_folder(ctx: workflow.Context, series: db.Row) -> db.Row | adoption.Problem:
    """Return the series row with its folder stored: the stored one, else Kavita's, stored now; or why it has none.

    A Kavita error reply raises as the client raises it.
    """
    if series["folder"] is not None:
        return series
    folder = adoption.folder_of(ctx, series)
    if isinstance(folder, adoption.Problem):
        return folder
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE series SET folder = %s WHERE id = %s AND folder IS NULL", (folder, series["id"]))
    return db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (series["id"],))


def record_problem(cfg: config.Config, series: db.Row) -> str | None:
    """Why the series' record cannot be used yet (no file, partial, broken, no known layout), or None."""
    try:
        record = _load(cfg, series)
    except FileNotFoundError as e:
        if errors.dropped_share(e):
            raise  # the share, not the record: the run's problem
        return f"no {record_reader.RECORD_NAME} in {series['folder']}"
    except record_reader.RecordError as e:
        return str(e)
    layout = record["series"]["layout"].get("layout")
    if layout not in LAYOUTS:
        return f"record series.layout.layout {layout!r} is not one of {LAYOUTS}"
    try:
        importing.record_rows(record)
    except record_reader.RecordError as e:
        return str(e)
    return None


def take(ctx: workflow.Context, series: db.Row) -> str | None:
    """Take the series' baseline from its record, or adopt it when it has none and no baseline (SPEC "State").

    Returns None once the series has one; else why not (the chapter waits). A record whose series name is not one of the
    series' Kavita names, or files that cannot be adopted, record nothing and make the series needs_human.
    """
    if series["baseline_at"] is None:
        try:  # the folder first: a record in it is the series' baseline, whatever its stored row lacked
            found = with_folder(ctx, series)
        except http.HttpError as e:
            return _kavita_failed(ctx, series, e)
        if isinstance(found, adoption.Problem):
            return _adopt(ctx, series, found)
        if not has_record(ctx.cfg, found["folder"]):
            return _adopt(ctx, found, found["folder"])
        series = found
    return _from_record(ctx, series)


def _from_record(ctx: workflow.Context, series: db.Row) -> str | None:
    """Take the series' baseline from its record (take's), or fill its bulk rows' naming fields."""
    problem = record_problem(ctx.cfg, series)
    if problem:
        return problem
    record = _load(ctx.cfg, series)
    if series["baseline_at"] is not None:  # taken before step 0054: only its rows' naming fields are filled
        with db.transaction(ctx.conn):
            _fill_naming(ctx.conn, series, record)
        return None
    name = record["series"]["naming"]["series_name"]
    kavita_names = [series["name"], series["localized_name"], series["original_name"]]
    if normalize(name) not in {normalize(n) for n in kavita_names if n}:
        reason = (
            f"record series name {name!r} is none of Kavita's names ({', '.join(n for n in kavita_names if n)}): "
            "a file carrying it would leave the series -> series link to refresh the names, or fix the record"
        )
        with db.transaction(ctx.conn):
            workflow.needs_human(ctx.conn, "series", series["id"], reason)
        return reason
    layout = record["series"]["layout"]["layout"]
    with db.transaction(ctx.conn):
        if db.one_row(ctx.conn, "SELECT baseline_at FROM series WHERE id = %s", (series["id"],))[0]:
            return None  # another run took it meanwhile
        _write(ctx.conn, series, record, name, layout)
    log.info("%s: series %s baseline taken from its record", ctx.part, series["id"])
    return None


def _adopt(ctx: workflow.Context, series: db.Row, folder: str | adoption.Problem) -> str | None:
    """Adopt the series: its waits first, then every read (adoption.prepare), then 1 transaction.

    folder: with_folder's answer, so Kavita's folder is read once per take.
    """
    if series["status"] == "needs_human":  # held by an earlier try: retry --series lets it try again
        return series["status_reason"]
    why = adoption.waiting(ctx.conn, series["id"])
    if why:
        return why
    try:
        found = adoption.prepare(ctx, series, folder)
    except http.HttpError as e:
        return _kavita_failed(ctx, series, e)
    return _record_adoption(ctx, series, found)


def _record_adoption(
    ctx: workflow.Context, series: db.Row, found: adoption.Prepared | list[adoption.Problem]
) -> str | None:
    """Record prepare's result in 1 transaction: the adoption, or the series held naming its problems."""
    if isinstance(found, list):
        reason = adoption.reason(found)
        with db.transaction(ctx.conn):
            if db.one_row(ctx.conn, "SELECT baseline_at FROM series WHERE id = %s", (series["id"],))[0]:
                return None  # another run adopted it meanwhile
            workflow.needs_human(ctx.conn, "series", series["id"], reason)
        return reason
    with db.transaction(ctx.conn):
        if db.one_row(ctx.conn, "SELECT baseline_at FROM series WHERE id = %s", (series["id"],))[0]:
            return None  # another run took it meanwhile
        _write_adopted(ctx.conn, series, found)
    log.info("%s: series %s adopted: %d files", ctx.part, series["id"], len(found.files))
    return None


def _kavita_failed(ctx: workflow.Context, series: db.Row, error: http.HttpError) -> str:
    """Log a Kavita error reply met taking the baseline; return the wait's reason: tried again, no attempt charged."""
    log.warning("%s: series %s: baseline not taken, Kavita replied with an error: %s", ctx.part, series["id"], error)
    return f"Kavita replied with an error: {error}"


def _write_adopted(conn: db.Connection, series: db.Row, found: adoption.Prepared) -> None:
    db.executemany(
        conn,
        """INSERT INTO published_file (library_path, series_id, identity, state, pages_out, comicinfo_series,
            comicinfo_volume, comicinfo_number, comicinfo_title, comicinfo_date, origin, source_name, kind,
            volume_source, cover_state)
        VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, %s, 'library', %s, %s, %s, %s)""",
        [
            (
                f.library_path,
                series["id"],
                f.identity,
                f.pages_out,
                series["name"],
                f.volume,
                f.number,
                f.title,
                f.date,
                f.source_name,
                f.kind,
                f.volume_source,
                f.cover_state,
            )
            for f in found.files
        ],
    )
    if found.layout is not None:
        conn.execute(
            """INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, %s, %s)
            ON CONFLICT (series_id) DO NOTHING""",
            (series["id"], *found.layout),
        )
    conn.execute(
        """UPDATE series SET baseline_at = %s, comicinfo_series = %s, folder = coalesce(folder, %s),
            naming_basis = coalesce(naming_basis, %s) WHERE id = %s""",
        (workflow.now(conn), series["name"], found.folder, found.basis, series["id"]),
    )


def _load(cfg: config.Config, series: db.Row) -> record_reader.Record:
    return record_reader.load(config.to_local(cfg, series["folder"]))


def _fill_naming(conn: db.Connection, series: db.Row, record: record_reader.Record) -> None:
    """Fill in the source name, kind and volume source of the series' bulk rows that lack them, from the record.

    Rows are matched by output file name (the series' folder may have moved since); a row the record no longer lists
    gets its kind from its identity's side label and no source name or volume source.
    """
    chapters = {c["output"]: c for c in record["chapters"]}
    rows = conn.execute(
        "SELECT id, library_path, identity FROM published_file WHERE series_id = %s AND origin = 'bulk'"
        " AND kind IS NULL",
        (series["id"],),
    ).fetchall()
    for r in rows:
        c = chapters.get(PurePosixPath(r["library_path"]).name)
        values: tuple[str | None, str, str | None]
        if c is None:
            side = names.read_identity(r["identity"])[2]
            values = (None, names.SIDE if side else names.MAIN, None)
        else:
            values = (Path(c["file"]).stem, c["kind"], c["volume_source"])
        conn.execute(
            "UPDATE published_file SET source_name = %s, kind = %s, volume_source = %s WHERE id = %s",
            (*values, r["id"]),
        )


def _write(conn: db.Connection, series: db.Row, record: record_reader.Record, name: str, layout: str) -> None:
    stamp = workflow.now(conn)
    for c in record["chapters"]:
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, state, pages_out, comicinfo_series,
                comicinfo_volume, comicinfo_number, comicinfo_title, origin, source_name, kind, volume_source)
            VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, 'bulk', %s, %s, %s)""",
            (
                f"{series['folder']}/{c['output']}",  # stored form: the folder's, then the file name
                series["id"],
                names.identity(names.parse(Path(c["file"]).stem)),
                c["pages_out"],
                name,
                None if c["volume"] is None else str(c["volume"]),
                None if c["number"] is None else str(c["number"]),
                c["title"] or None,
                Path(c["file"]).stem,
                c["kind"],
                c["volume_source"],
            ),
        )
    for s in record["sources"]:
        if not s["file"].lower().endswith(".cbz"):
            continue  # a carried ComicInfo.xml or a replaced cover: not a chapter
        conn.execute(
            """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
            VALUES (%s, %s, %s, %s, %s)""",
            (
                series["id"],
                names.identity(names.parse(Path(s["file"]).stem)),
                OUTCOME[s["fate"]],
                s.get("pages_digest"),
                stamp,
            ),
        )
    aspect = record["series"]["layout"].get("page_aspect")
    conn.execute(
        "INSERT INTO series_conversion (series_id, layout, page_aspect, origin) VALUES (%s, %s, %s, 'record')",
        (series["id"], layout, None if aspect is None else json.dumps(aspect)),
    )
    for group, (q, why) in calibrate.from_record(record).items():
        conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why) VALUES (%s, %s,"
            " %s, 1, 'record', %s)",
            (series["id"], group, q, why),
        )
    rows, unsignable = importing.signable(importing.winners(importing.record_rows(record)))
    if unsignable:
        log.info("series %s: %s record decision(s) on an unsignable key not imported", series["id"], unsignable)
    store.upsert(conn, rows)  # the junk decisions it carries
    conn.execute(
        "UPDATE series SET baseline_at = %s, comicinfo_series = %s, naming_basis = %s WHERE id = %s",
        (stamp, name, record["series"]["naming"].get("basis"), series["id"]),
    )
