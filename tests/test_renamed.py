"""A download Suwayomi renamed (SPEC "Bad downloads"): the chapter that lost its file is dropped, its new file kept."""

import argparse
import os
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import cli, config, db, handled, intake, names, process, workflow
from manga_pipeline.db import sql

from tests import builders
from tests.builders import add_chapter, add_series, chapter, settle, site_cfg
from tests.timing import budget
from tests.world import World, cbz

# --- the rename through the parts


class MangaWorld(World):
    """A World with the series' manga on its source."""

    manga_id: int


@pytest.fixture
def world(tmp_path):
    w = MangaWorld(tmp_path)
    w.kavita_series("Solo Leveling")
    w.manga_id = w.manga("Solo Leveling")
    return w


def rename(world, suwayomi_chapter, scanlator):
    """Do as Suwayomi does when a source reports another scanlator: the chapter's download takes its new name."""
    old = world.suwayomi.path(suwayomi_chapter)
    world.suwayomi.chapter_rows[suwayomi_chapter]["scanlator"] = scanlator
    new = world.suwayomi.path(suwayomi_chapter)
    old.rename(new)
    settle(new)
    return new


def by_path(world, path):
    """Return (id, stage, status, waiting_on, last_error or "") of the chapter whose download is `path`."""
    ((found,),) = world.rows(
        "SELECT ROW(id, stage, status, waiting_on, coalesce(last_error, '')) FROM chapter WHERE download_path = %s",
        config.to_stored(world.cfg, path),
    )
    return found


def handled_of(world, chapter_id):
    """Return [(count,)] of the handled chapters holding the chapter's content digest."""
    return world.rows(
        """SELECT count(*) FROM handled_chapter
        WHERE content_digest = (SELECT content_digest FROM chapter WHERE id = %s)""",
        chapter_id,
    )


@budget(load="db", seconds=10)
def test_a_renamed_download_drops_the_stranded_chapter_and_publishes_its_new_file(world):
    suwayomi_chapter, old = world.download(world.manga_id, "Chapter 5", "Valir Scans", data=cbz(seed="v5"))
    world.run(intake)
    assert by_path(world, old)[1:3] == ("junk", "ready"), "set up: validated, at junk"
    new = rename(world, suwayomi_chapter, "admins")
    world.run(intake)
    world.run(process)
    stranded_id, stage, status, _, error = by_path(world, old)
    assert (stage, status) == ("junk", "done"), "the chapter whose file was renamed away is dropped"
    assert error.startswith("dropped: download renamed: chapter"), "the reason names the chapter holding its pages"
    assert handled_of(world, stranded_id) == [(0,)], "no handled chapter: the renamed copy is not put aside"
    new_id, stage, status, _, _ = by_path(world, new)
    assert (stage, status) == ("comicinfo", "ready"), "the renamed copy goes past junk and is converted"
    assert new_id != stranded_id, "the renamed file is another chapter"


@budget(load="db", seconds=10)
def test_a_chapter_at_validate_whose_download_was_renamed_is_dropped(world):
    suwayomi_chapter, old = world.download(world.manga_id, "Chapter 5", "Valir Scans", data=cbz(seed="v5"))
    world.run(intake)
    stranded = by_path(world, old)[0]
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):  # held at validate, as a metadata wait holds it
        conn.execute("UPDATE chapter SET stage = 'validate', status = 'ready' WHERE id = %s", (stranded,))
    new = rename(world, suwayomi_chapter, "admins")
    world.run(intake)
    assert world.rows("SELECT attempts FROM chapter WHERE id = %s", stranded) == [(1,)], (
        "set up: tried before its new file's digest is known, it fails once (plan known limit 6)"
    )
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):  # the retry wait over
        conn.execute("UPDATE chapter SET retry_after = NULL WHERE id = %s", (stranded,))
    world.run(intake)
    _, stage, status, _, error = by_path(world, old)
    assert (stage, status) == ("validate", "done"), "validate drops a chapter whose download was renamed away"
    assert error.startswith("dropped: download renamed: chapter"), "the reason names the chapter holding its pages"
    assert handled_of(world, stranded) == [(0,)], "nothing is recorded as handled"
    assert by_path(world, new)[1:3] == ("junk", "ready"), "the renamed copy is validated"


@budget(load="db", seconds=10)
def test_retry_closes_a_stranded_chapter_held_for_a_person_and_frees_its_new_file(world):
    suwayomi_chapter, old = world.download(world.manga_id, "Chapter 5", "Valir Scans", data=cbz(seed="v5"))
    world.run(intake)
    new = rename(world, suwayomi_chapter, "admins")
    stranded = by_path(world, old)[0]
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):  # as 3 FileNotFoundError attempts left it
        conn.execute("UPDATE chapter SET status = 'needs_human', attempts = 3 WHERE id = %s", (stranded,))
    world.run(intake)
    world.run(process)
    assert by_path(world, new)[1:4] == ("junk", "waiting", "open_copy"), "set up: the held chapter is ahead of it"
    with closing(db.connect(world.cfg)) as conn:
        with pytest.raises(workflow.Refused, match="download renamed"):
            cli.chapter_drop(world.cfg, argparse.Namespace(chapter=stranded))
        workflow.retry_chapter(conn, stranded)
    world.run(process)
    assert by_path(world, old)[1:3] == ("junk", "done"), "retry: the next process drops it"
    world.run(process)
    assert by_path(world, new)[1:3] == ("comicinfo", "ready"), "the next process ends the copy's wait; it converts"
    assert handled_of(world, stranded) == [(0,)], "nothing is recorded as handled"


# --- the rule's conditions


def identity(name):
    return names.identity(names.parse(name))


def download(cfg, name):
    return config.to_stored(cfg, builders.download(cfg, "T", name))


@pytest.fixture
def site(tmp_path):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        series = add_series(conn, "T")
        gone = config.to_stored(
            cfg, Path(cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / "T" / "Valir_Chapter 5.cbz"
        )
        stranded = add_chapter(
            conn, series, identity("Valir_Chapter 5"), "junk", download_path=gone, content_digest="d5"
        )
        twin = add_chapter(
            conn,
            series,
            identity("admins_Chapter 5"),
            "validate",
            download_path=download(cfg, "admins_Chapter 5"),
            content_digest="d5",
        )
        yield cfg, conn, series, stranded, twin


def renamed(site, chapter_id):
    cfg, conn, *_ = site
    return handled.renamed(cfg, conn, chapter(conn, chapter_id))


def update(conn, chapter_id, **columns):
    with db.transaction(conn):
        for name, value in columns.items():
            conn.execute(
                sql.SQL("UPDATE chapter SET {} = %s WHERE id = %s").format(sql.Identifier(name)), (value, chapter_id)
            )


@budget(load="db")
def test_a_gone_download_with_a_twin_is_renamed_away(site):
    _, _, _, stranded, twin = site
    assert renamed(site, stranded) == f"download renamed: chapter {twin} has its pages at admins_Chapter 5.cbz"


@budget(load="db")
@pytest.mark.parametrize(
    "change",
    [
        "another key",
        "twin gone",
        "twin done",
        "re-download",
        "names a published file",
        "download present",
    ],
)
def test_not_renamed_away(site, change):
    cfg, conn, series, stranded, twin = site
    if change == "another key":
        update(conn, twin, identity=identity("admins_Chapter 6"))
    elif change == "twin gone":
        config.to_local(cfg, chapter(conn, twin)["download_path"]).unlink()
    elif change == "twin done":
        update(conn, twin, status="done")
    elif change == "re-download":
        update(conn, stranded, redownload='{"original": null, "tried": []}')
    elif change == "names a published file":
        with db.transaction(conn):
            file_id = conn.execute(
                """INSERT INTO published_file (library_path, series_id, identity, state, origin)
                VALUES ('library/T/Ch.5.cbz', %s, '["", null, "", "5", ""]', 'confirmed', 'bulk') RETURNING id""",
                (series,),
            ).fetchone()["id"]
        update(conn, stranded, published_file_id=file_id)
    else:
        update(conn, stranded, download_path=download(cfg, "Valir_Chapter 5"))
    assert renamed(site, stranded) is None, f"SPEC 'Bad downloads': not renamed away when {change}"


@budget(load="db")
@pytest.mark.skipif(os.name != "nt", reason="Windows: checking such a path there raises instead of answering")
def test_a_twin_path_windows_cannot_open_is_skipped_there(site):
    cfg, conn, _, stranded, twin = site
    path = Path(cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / "T" / "admins_Chapter 5?.cbz"
    update(conn, twin, download_path=config.to_stored(cfg, path))
    assert renamed(site, stranded) is None, "a path Windows cannot open is not checked there"
