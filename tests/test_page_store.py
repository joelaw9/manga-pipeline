"""page_store and chapter_edit: the page measurements and the junk edit the stage phases share (SPEC "Parts",
hand-off between parts)."""

import shutil

import psycopg
import pytest
from PIL import Image

from manga_pipeline import db, images, page_store, workflow

from tests import pages
from tests.builders import add_chapter, add_series, site_cfg
from tests.timing import budget


@pytest.fixture
def conn(tmp_path):
    c = db.connect(site_cfg(tmp_path))
    yield c
    c.close()


def chapter_cbz(path, n=2, blank=True):
    """n manga pages, then a blank white page when blank (phash and junk_signature None)."""
    content = {f"{i + 1:03}.jpg": pages.jpeg(pages.manga_page(seed=i)) for i in range(n)}
    if blank:
        content[f"{n + 1:03}.jpg"] = pages.jpeg(Image.new("RGB", (900, 1300), "white"))
    return pages.cbz(path, content)


def new_chapter(conn):
    return add_chapter(conn, add_series(conn, "S", "kavita"), '["", null, null, "1", ""]')  # step 0003's columns


@budget(load="db", seconds=2.0)
def test_saved_pages_load_back_and_are_not_decoded_again(conn, tmp_path, monkeypatch):
    cbz = chapter_cbz(tmp_path / "c.cbz")
    measured = images.measure_pages(cbz)
    assert measured[-1]["phash"] is None, "the fixture's last page is blank"
    assert measured[-1]["blank"], "the fixture's last page is blank"
    chapter_id = new_chapter(conn)
    with db.transaction(conn):
        page_store.save(conn, chapter_id, measured)
    known = page_store.load(conn, chapter_id)
    assert [known[i] for i in sorted(known)] == measured, "load gives back what save stored, None and bools included"
    decoded = []
    real = images._measure_decoded
    monkeypatch.setattr(images, "_measure_decoded", lambda data: decoded.append(data) or real(data))
    assert images.measure_pages(cbz, known=known) == measured
    assert decoded == [], "a page whose rows are stored is not decoded again, a blank page included"


@budget(load="db", seconds=2.0)
def test_save_replaces_every_row_of_the_chapter(conn, tmp_path):
    chapter_id = new_chapter(conn)
    with db.transaction(conn):
        page_store.save(conn, chapter_id, images.measure_pages(chapter_cbz(tmp_path / "a.cbz", n=3)))
    shorter = images.measure_pages(chapter_cbz(tmp_path / "b.cbz", n=1, blank=False))
    with db.transaction(conn):
        page_store.save(conn, chapter_id, shorter)
    assert list(page_store.load(conn, chapter_id).values()) == shorter, "a shorter chapter leaves no trailing rows"


@budget(load="db")
def test_save_outside_a_transaction_is_refused(conn):
    chapter_id = new_chapter(conn)
    with pytest.raises(RuntimeError, match="transaction"):
        page_store.save(conn, chapter_id, [])


@budget(load="db", seconds=2.0)
def test_save_refuses_a_page_missing_a_field_and_keeps_the_stored_rows(conn, tmp_path):
    chapter_id = new_chapter(conn)
    measured = images.measure_pages(chapter_cbz(tmp_path / "c.cbz", n=1, blank=False))
    with db.transaction(conn):
        page_store.save(conn, chapter_id, measured)
    with pytest.raises(ValueError, match="lacks"), db.transaction(conn):
        page_store.save(conn, chapter_id, [*measured, {"index": 1, "name": "2.jpg"}])
    assert list(page_store.load(conn, chapter_id).values()) == measured, "a refused save changes no stored row"


@budget(3.0, load="db")
def test_step_0003_upgrades_a_database_with_page_rows(tmp_path, monkeypatch):
    """A database built before 0003 keeps its page rows; chroma becomes a text column."""
    older = tmp_path / "schema"
    older.mkdir()
    for step in db.SCHEMA.glob("*.sql"):
        if step.stem < "0003":
            shutil.copy(step, older)
    cfg = site_cfg(tmp_path, built=False)
    monkeypatch.setattr(db, "SCHEMA", older)
    conn = db.connect(cfg)
    chapter_id = new_chapter(conn)
    with db.transaction(conn):
        conn.execute("INSERT INTO page (chapter_id, page_index, name) VALUES (%s, 0, '1.jpg')", (chapter_id,))
    conn.close()
    monkeypatch.undo()
    conn = db.connect(cfg)
    assert conn.execute("SELECT name FROM page WHERE chapter_id = %s", (chapter_id,)).fetchone()[0] == "1.jpg"
    chroma = conn.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'page' AND column_name = 'chroma'"
    ).fetchone()[0]
    assert chroma == "text", "chroma holds images.chroma's text"
    conn.close()


@budget(load="db")
@pytest.mark.parametrize("edit", ["null", "[]", "3", '"x"', "not json"])
def test_chapter_edit_holds_only_a_json_object(conn, edit):
    chapter_id = new_chapter(conn)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction(conn):
        conn.execute("INSERT INTO chapter_edit (chapter_id, edit) VALUES (%s, %s)", (chapter_id, edit))


@budget(load="db", seconds=2.0)
def test_state_reset_empties_chapter_edit_and_page_rows(conn, tmp_path):
    chapter_id = new_chapter(conn)
    with db.transaction(conn):
        conn.execute("INSERT INTO chapter_edit (chapter_id, edit) VALUES (%s, %s)", (chapter_id, '{"drop_pages": [1]}'))
        page_store.save(conn, chapter_id, images.measure_pages(chapter_cbz(tmp_path / "c.cbz", n=1, blank=False)))
    workflow.reset(conn)
    assert conn.execute("SELECT count(*) FROM chapter_edit").fetchone()[0] == 0, "reset clears the junk edits"
    assert conn.execute("SELECT count(*) FROM page").fetchone()[0] == 0, "reset clears the page rows"
