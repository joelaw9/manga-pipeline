"""A chapter's page rows (SPEC "Parts", hand-off between parts): the measurements images.measure_pages takes of the
download's pages, stored so a later stage passes them back as measure_pages(cbz, known=load(...)). The page table holds
the download's pages only; convert's output pages are chapter.output_pages.
"""

from manga_pipeline import db, images
from manga_pipeline.db import sql

COLUMNS = {"index": "page_index"}  # measure_pages field -> page column, where the names differ


def _columns():
    """The page columns of images.PAGE_FIELDS, in its order, as an SQL list."""
    return sql.SQL(", ").join(sql.Identifier(COLUMNS.get(f, f)) for f in images.PAGE_FIELDS)


def save(conn, chapter_id, pages):
    """Replace the chapter's page rows with pages, a complete measure_pages result. Runs in the caller's transaction:
    raises RuntimeError when none is open, ValueError when a page lacks a field.
    """
    if not db.in_transaction(conn):
        msg = "page_store.save needs an open transaction (db.transaction)"
        raise RuntimeError(msg)
    rows = []
    for page in pages:
        missing = [f for f in images.PAGE_FIELDS if f not in page]
        if missing:
            msg = f"chapter {chapter_id}: page {page.get('index')} lacks {', '.join(missing)}"
            raise ValueError(msg)
        row = [page[f] for f in images.PAGE_FIELDS]
        row[images.PAGE_FIELDS.index("blank")] = int(page["blank"])
        rows.append((chapter_id, *row))
    marks = sql.SQL(", ").join([sql.Placeholder()] * (len(images.PAGE_FIELDS) + 1))
    conn.execute("DELETE FROM page WHERE chapter_id = %s", (chapter_id,))
    db.executemany(
        conn,
        sql.SQL("INSERT INTO page (chapter_id, {columns}) VALUES ({marks})").format(columns=_columns(), marks=marks),
        rows,
    )


def load(conn, chapter_id):
    """{index: {field: value}} of the chapter's page rows, every field (None included), blank as a bool."""
    out = {}
    query = sql.SQL("SELECT {} FROM page WHERE chapter_id = %s ORDER BY page_index").format(_columns())
    for row in conn.execute(query, (chapter_id,)):
        page = dict(zip(images.PAGE_FIELDS, row, strict=True))
        page["blank"] = bool(page["blank"])
        out[page["index"]] = page
    return out
