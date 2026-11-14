"""A chapter's page rows (SPEC "Parts", hand-off between parts): the measurements images.measure_pages takes of the
download's pages, stored so a later stage passes them back as measure_pages(cbz, known=load(...)). The page table holds
the download's pages only; convert's output pages are chapter.output_pages."""

from manga_pipeline import db, images

COLUMNS = {"index": "page_index"}  # measure_pages field -> page column, where the names differ


def _column(field):
    return COLUMNS.get(field, field)


def save(conn, chapter_id, pages):
    """Replace the chapter's page rows with pages, a complete measure_pages result. Runs in the caller's transaction:
    raises RuntimeError when none is open, ValueError when a page lacks a field."""
    if not db.in_transaction(conn):
        raise RuntimeError("page_store.save needs an open transaction (db.transaction)")
    rows = []
    for page in pages:
        missing = [f for f in images.PAGE_FIELDS if f not in page]
        if missing:
            raise ValueError(f"chapter {chapter_id}: page {page.get('index')} lacks {', '.join(missing)}")
        row = [page[f] for f in images.PAGE_FIELDS]
        row[images.PAGE_FIELDS.index("blank")] = int(page["blank"])
        rows.append((chapter_id, *row))
    columns = ", ".join(_column(f) for f in images.PAGE_FIELDS)
    marks = ", ".join(["%s"] * (len(images.PAGE_FIELDS) + 1))
    conn.execute("DELETE FROM page WHERE chapter_id = %s", (chapter_id,))
    db.executemany(conn, f"INSERT INTO page (chapter_id, {columns}) VALUES ({marks})", rows)


def load(conn, chapter_id):
    """{index: {field: value}} of the chapter's page rows, every field (None included), blank as a bool."""
    columns = ", ".join(_column(f) for f in images.PAGE_FIELDS)
    out = {}
    for row in conn.execute(f"SELECT {columns} FROM page WHERE chapter_id = %s ORDER BY page_index", (chapter_id,)):
        page = dict(zip(images.PAGE_FIELDS, row, strict=True))
        page["blank"] = bool(page["blank"])
        out[page["index"]] = page
    return out
