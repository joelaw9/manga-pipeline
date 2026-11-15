"""`series set-id SERIES_ID SOURCE ID` (SPEC "Source ids"): a person's id for a source, which wins over every other
origin."""

from manga_pipeline import db, workflow
from manga_pipeline.metadata import ids


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
