"""A series' baseline (SPEC "State", "Publishing", "Calibration"): what a series Kavita already holds is taken from its
bulk-converter record before anything is published into it. Read by `process` only; no library file is opened.
"""

import json
import logging
from pathlib import Path

from manga_pipeline import db, names, workflow
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


def needed(series):
    """Whether the series must have a baseline before its chapters go on: a series Kavita holds that has none."""
    return series["origin"] == "kavita" and series["baseline_at"] is None


def record_problem(series):
    """Why the series' record cannot be used yet (no file, partial, broken, no known layout), or None."""
    try:
        record = _load(series)
    except FileNotFoundError:
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


def take(ctx, series):
    """Take the series' baseline from its record. Returns None once the series has one; else why not (the chapter
    waits). A record whose series name is not one of the series' Kavita names records nothing and makes the series
    needs_human."""
    problem = record_problem(series)
    if problem:
        return problem
    record = _load(series)
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
        if ctx.conn.execute("SELECT baseline_at FROM series WHERE id = %s", (series["id"],)).fetchone()[0]:
            return None  # another run took it meanwhile
        _write(ctx.conn, series, record, name, layout)
    log.info("%s: series %s baseline taken from its record", ctx.part, series["id"])
    return None


def _load(series):
    return record_reader.load(Path(series["folder"]))


def _write(conn, series, record, name, layout):
    folder = Path(series["folder"])
    stamp = workflow.now()
    for c in record["chapters"]:
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, state, pages_out, comicinfo_series,
                comicinfo_volume, comicinfo_number, comicinfo_title, origin)
            VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, 'bulk')""",
            (
                str(folder / c["output"]),
                series["id"],
                names.identity(names.parse(Path(c["file"]).stem)),
                c["pages_out"],
                name,
                None if c["volume"] is None else str(c["volume"]),
                None if c["number"] is None else str(c["number"]),
                c["title"] or None,
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
    store.upsert(conn, importing.winners(importing.record_rows(record)))  # the junk decisions it carries
    conn.execute(
        "UPDATE series SET baseline_at = %s, comicinfo_series = %s, naming_basis = %s WHERE id = %s",
        (stamp, name, record["series"]["naming"].get("basis"), series["id"]),
    )
