"""A person's settlements of a series: `series set-id`, `series new` and `series refetch-covers`.

`series set-id SERIES_ID SOURCE ID` (SPEC "Source ids"), an id for a source that wins over every other origin;
`series new SERIES_ID [--name NAME]` (SPEC "Series identification"), a held new series made ready to be admitted;
`series refetch-covers SERIES_ID`, its check made due now.
"""

from manga_pipeline import db, workflow
from manga_pipeline.metadata import check, ids


def set_id(conn: db.Connection, series_id: int, source: str, value: str) -> str:
    """Store the id, replacing the series' earlier human id for that source.

    Every id a search found for the series goes, and its check is made due at once: a search may have gone through the
    id this one corrects, so that check searches them all again. Refused for an unknown series or source, or an id not
    in the source's form.
    """
    if source not in ids.SOURCES:
        msg = f"unknown source {source!r}: one of {', '.join(ids.SOURCES)}"
        raise workflow.Refused(msg)
    found = ids.form(source, value)
    if found is None:
        msg = f"{value!r} is not a {source} id"
        raise workflow.Refused(msg)
    if conn.execute("SELECT 1 FROM series WHERE id = %s", (series_id,)).fetchone() is None:
        msg = f"no series {series_id}"
        raise workflow.Refused(msg)
    with db.transaction(conn):
        conn.execute(
            """DELETE FROM series_source_id
            WHERE series_id = %s AND (origin = 'search' OR (source = %s AND origin = 'human'))""",
            (series_id, source),
        )
        conn.execute("INSERT INTO series_source_id VALUES (%s, %s, 'human', %s)", (series_id, source, found))
        check.make_urgent(conn, series_id)
    return found


def refetch_covers(conn: db.Connection, series_id: int) -> None:
    """Make the series' check due at once (`series refetch-covers SERIES_ID`).

    The next intake then fetches its MangaDex cover list and refreshes every stored volume cover image MangaDex now
    chooses another file for (SPEC "Retroactive updates"). Refused for a series that does not exist.
    """
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM series WHERE id = %s", (series_id,)).fetchone() is None:
            msg = f"no series {series_id}"
            raise workflow.Refused(msg)
        check.make_urgent(conn, series_id)


def new_series(conn: db.Connection, series_id: int, name: str | None = None) -> None:
    """Make a held new series not yet admitted pending again (status ok), under name when given.

    Its check is made due at once: the next intake checks it and admits it (metadata/admit.py). When its check has
    completed, its admission compared its ids, so the person saw them: it is then confirmed a series of its own and its
    ids are not compared again. Refused for any other series, and for a name with nothing in it.
    """
    if name is not None and not name.strip():
        msg = "--name is empty"
        raise workflow.Refused(msg)
    with db.transaction(conn):
        row = conn.execute(
            "SELECT origin, admitted_at, status, kavita_series_id FROM series WHERE id = %s", (series_id,)
        ).fetchone()
        if row is None:
            msg = f"no series {series_id}"
            raise workflow.Refused(msg)
        if row["origin"] != "new" or row["admitted_at"] is not None or row["kavita_series_id"] is not None:
            msg = f"series {series_id} is not a new series waiting to be admitted"
            raise workflow.Refused(msg)
        if row["status"] != "needs_human":
            msg = f"series {series_id} is not held (needs_human): it is admitted at the next intake"
            raise workflow.Refused(msg)
        conn.execute(
            """UPDATE series SET status = 'ok', status_reason = NULL, admit_name = %s, admit_confirmed =
                EXISTS (SELECT 1 FROM series_check WHERE series_id = %s AND data_version > 0)
            WHERE id = %s""",
            (name.strip() if name is not None else None, series_id, series_id),
        )
        check.make_urgent(conn, series_id)
