"""A person's settlements of a series: `series set-id SERIES_ID SOURCE ID` (SPEC "Source ids"), an id for a source
that wins over every other origin; `series new SERIES_ID [--name NAME]` (SPEC "Series identification"), a held new
series made ready to be admitted; `series refetch-covers SERIES_ID`, its check made due now."""

from manga_pipeline import db, workflow
from manga_pipeline.metadata import check, ids


def set_id(conn, series_id, source, value):
    """Store the id (replacing the series' earlier human id for that source); Refused for an unknown series or source,
    or an id not in the source's form."""
    if source not in ids.SOURCES:
        raise workflow.Refused(f"unknown source {source!r}: one of {', '.join(ids.SOURCES)}")
    found = ids.form(source, value)
    if found is None:
        raise workflow.Refused(f"{value!r} is not a {source} id")
    if conn.execute("SELECT 1 FROM series WHERE id = %s", (series_id,)).fetchone() is None:
        raise workflow.Refused(f"no series {series_id}")
    with db.transaction(conn):
        conn.execute(
            "DELETE FROM series_source_id WHERE series_id = %s AND source = %s AND origin = 'human'",
            (series_id, source),
        )
        conn.execute("INSERT INTO series_source_id VALUES (%s, %s, 'human', %s)", (series_id, source, found))
    return found


def refetch_covers(conn, series_id):
    """`series refetch-covers SERIES_ID`: the series' check made due at once, so the next intake fetches its MangaDex
    cover list and refreshes every stored volume cover image MangaDex now chooses another file for (SPEC "Retroactive
    updates"). Refused for a series that does not exist."""
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM series WHERE id = %s", (series_id,)).fetchone() is None:
            raise workflow.Refused(f"no series {series_id}")
        check.make_urgent(conn, series_id)


def new_series(conn, series_id, name=None):
    """A held new series not yet admitted becomes pending again (status ok), under name when given, and its check is
    made due at once: the next intake checks it and admits it (metadata/admit.py). Refused for any other series, and
    for a name with nothing in it."""
    if name is not None and not name.strip():
        raise workflow.Refused("--name is empty")
    with db.transaction(conn):
        row = conn.execute(
            "SELECT origin, admitted_at, status, kavita_series_id FROM series WHERE id = %s", (series_id,)
        ).fetchone()
        if row is None:
            raise workflow.Refused(f"no series {series_id}")
        if row["origin"] != "new" or row["admitted_at"] is not None or row["kavita_series_id"] is not None:
            raise workflow.Refused(f"series {series_id} is not a new series waiting to be admitted")
        if row["status"] != "needs_human":
            raise workflow.Refused(f"series {series_id} is not held (needs_human): it is admitted at the next intake")
        conn.execute(
            "UPDATE series SET status = 'ok', status_reason = NULL, admit_name = %s WHERE id = %s",
            (name.strip() if name is not None else None, series_id),
        )
        check.make_urgent(conn, series_id)
