"""Adopted files' conversions (SPEC "State"): queued by intake, linked by discover, each replacing its file.

The queue gives a chapter to each confirmed adopted file of an ok series in scope that no chapter names, by series and
number, under the cap; discover links the download to the library file; the conversion then runs as a better copy.
"""

import argparse
import errno
import hashlib
import io
import json
import logging
import os
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import (
    archive,
    cli,
    config,
    conversions,
    db,
    export,
    intake,
    preflight,
    process,
    rewrite,
    state,
    status,
    workflow,
)
from manga_pipeline.errors import Unreachable
from manga_pipeline.junk import candidates
from manga_pipeline.metadata import episodes, platforms
from manga_pipeline.stages import discover, publish, validate

from tests.builders import add_chapter, add_series, claimed, episode_record, one, platform_list
from tests.timing import budget
from tests.world import LIBRARY_ID, World, cbz

SERIES = "Solo Leveling"
OTHER = "Other (EN)"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, sources={"ranking": [OTHER], "download_timeout_hours": 24})


def set_cap(world, n):
    """Set export.rewrites_open, the cap on open conversions, for the world's next runs."""
    world.cfg = {**world.cfg, "export": {**world.cfg["export"], "rewrites_open": n}}


def adopt(world, files=None, cap=0):
    """Add a library series with no record (files: {name: CBZ bytes}, else Ch.1-3) and adopt it by 1 intake run.

    The cap is `cap` during that run (0: no conversion queued yet) and stays so. Returns the Kavita series id.
    """
    folder = world.library / SERIES
    folder.mkdir(parents=True)
    for name, data in (files or {f"Ch.{n}.cbz": cbz(seed=f"adopted {n}", pages=4) for n in (1, 2, 3)}).items():
        (folder / name).write_bytes(data)
    kid = world.kavita.add_series(SERIES, SERIES, LIBRARY_ID)
    set_cap(world, cap)
    world.run(intake)
    assert world.rows("SELECT DISTINCT origin FROM published_file") == [("library",)], "set up: adopted"
    return kid


def file_ids(world):
    """Return {file name: published file id} of the series' published files."""
    return {path.rsplit("/", 1)[1]: i for i, path in world.rows("SELECT id, library_path FROM published_file")}


def converting(world):
    """Return (file name, stage, status) of each chapter naming a published file, by chapter id."""
    rows = world.rows(
        """SELECT p.library_path, c.stage, c.status FROM chapter c JOIN published_file p ON p.id = c.published_file_id
        ORDER BY c.id"""
    )
    return [(path.rsplit("/", 1)[1], stage, status_) for path, stage, status_ in rows]


def queue(world, series=None):
    with closing(db.connect(world.cfg)) as conn:
        return conversions.queue(world.cfg, conn, series)


def link_of(world, name):
    """Return the link path of the published file named `name`."""
    return config.abs_path(world.cfg["paths"]["staging"]) / "adopted" / str(file_ids(world)[name]) / name


def chapter_of(world, name):
    """Return the id of the open chapter naming the published file `name`."""
    ((chapter_id,),) = world.rows(
        "SELECT c.id FROM chapter c WHERE c.published_file_id = %s AND c.status <> 'done'", file_ids(world)[name]
    )
    return chapter_id


def stage(world, part, chapter_id, run):
    """Run 1 stage function on a chapter claimed for part's run; return its outcome."""
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, part)
        return workflow.work(ctx, claimed(conn, ctx, chapter_id), run)


def report(world):
    with closing(db.read_only(world.cfg)) as conn:
        return status.report(conn)


def png(colour, size=(40, 60)):
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def archive_of(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def layout_override(world):
    """Name the series' layout in its override file, so adoption measures no file."""
    (config.abs_path(world.cfg["paths"]["naming_overrides"]) / f"{SERIES}.yaml").write_text(
        "layout: pages\n", encoding="utf-8", newline="\n"
    )


class _Rows(list):
    def fetchall(self):
        return list(self)


# --- the queue


@budget(load="db", seconds=6)
def test_each_adopted_file_gets_1_conversion_chapter_within_the_cap(world):
    adopt(world, {f"Ch.{n}.cbz": cbz(seed=f"a{n}", pages=4) for n in (10, 2, 1)})
    set_cap(world, 2)
    assert queue(world).queued.total() == 2
    assert converting(world) == [("Ch.1.cbz", "discover", "ready"), ("Ch.2.cbz", "discover", "ready")], (
        "by series, then number (10 after 2)"
    )
    ((path, identity, source),) = world.rows(
        "SELECT download_path, identity, suwayomi_chapter_id FROM chapter WHERE published_file_id = %s",
        file_ids(world)["Ch.1.cbz"],
    )
    assert path == config.to_stored(world.cfg, link_of(world, "Ch.1.cbz")), "its download is its link"
    assert (identity, source) == ('["", null, "", "1", ""]', None), "the file's identity, no Suwayomi chapter"
    done = queue(world)
    assert (done.queued.total(), done.left.total()) == (0, 1), "the cap is full: the 3rd file is left"
    set_cap(world, 5)
    assert queue(world).queued.total() == 1
    assert queue(world).queued.total() == 0, "a file named by a chapter gets no 2nd one"
    assert len(converting(world)) == 3


@budget(load="db", seconds=6)
def test_only_confirmed_files_of_ok_series_in_scope_are_queued(world):
    adopt(world, {f"Ch.{n}.cbz": cbz(seed=f"a{n}", pages=4) for n in (1, 2, 3, 4)})
    ids = file_ids(world)
    world.rows("UPDATE published_file SET state = 'unconfirmed' WHERE id = %s RETURNING id", ids["Ch.1.cbz"])
    world.rows("UPDATE published_file SET origin = 'bulk' WHERE id = %s RETURNING id", ids["Ch.2.cbz"])
    world.rows("UPDATE published_file SET pending_error = 'x' WHERE id = %s RETURNING id", ids["Ch.3.cbz"])  # busy
    set_cap(world, 10)
    world.rows("UPDATE series SET status = 'needs_human' RETURNING id")
    assert queue(world).queued.total() == 0, "a needs_human series gets none"
    world.rows("UPDATE series SET status = 'ok' RETURNING id")
    world.cfg = {**world.cfg, "series": {**world.cfg["series"], "only": ["Another Series"]}}
    assert queue(world).queued.total() == 0, "a series out of scope gets none"
    world.cfg = {**world.cfg, "series": {**world.cfg["series"], "only": []}}
    assert queue(world).queued.total() == 1
    assert converting(world) == [("Ch.4.cbz", "discover", "ready")], "only the confirmed, adopted, free file"


@budget(load="db", seconds=6)
def test_a_file_taken_or_converted_meanwhile_gets_no_conversion(world, monkeypatch):
    adopt(world)
    ids = file_ids(world)
    set_cap(world, 5)
    with closing(db.connect(world.cfg)) as conn, closing(db.connect(world.cfg)) as other:
        execute, calls = conn.execute, []

        def meanwhile(query, *params):
            """After the candidates are read, another run takes Ch.1 (a better copy) and converts Ch.2."""
            cursor = execute(query, *params)
            calls.append(query)
            if len(calls) == 1:
                rows = _Rows(cursor.fetchall())
                sid = one(other, "SELECT id FROM series")
                add_chapter(other, sid, '["", null, "", "1", ""]', "convert", published_file_id=ids["Ch.1.cbz"])
                other.execute("UPDATE published_file SET origin = 'pipeline' WHERE id = %s", (ids["Ch.2.cbz"],))
                return rows
            return cursor

        monkeypatch.setattr(conn, "execute", meanwhile)
        done = conversions.queue(world.cfg, conn)
    assert done.queued.total() == 1, "the transaction re-checks each file"
    assert converting(world) == [("Ch.1.cbz", "convert", "ready"), ("Ch.3.cbz", "discover", "ready")]


@budget(load="db", seconds=6)
def test_needs_human_conversions_count_against_the_cap(world):
    adopt(world)
    set_cap(world, 2)
    queue(world)
    world.rows("UPDATE chapter SET status = 'needs_human', last_error = 'x' RETURNING id")
    done = queue(world)
    assert (done.queued.total(), done.left.total()) == (0, 1), "held conversions keep their slots"
    assert "  3 not converted, 2 conversion(s) open" in report(world), "status shows it"


@budget(load="db", seconds=6)
def test_compare_keeps_its_own_count_while_conversions_are_open(world):
    adopt(world)
    set_cap(world, 2)
    queue(world)
    with closing(db.connect(world.cfg)) as conn:
        sid = one(conn, "SELECT id FROM series")
        with db.transaction(conn):
            bulk = one(
                conn,
                """INSERT INTO published_file (library_path, series_id, identity, state, origin)
                VALUES ('library/Solo Leveling/Ch.9.cbz', %s, '["", null, "", "9", ""]', 'confirmed', 'bulk')
                RETURNING id""",
                (sid,),
            )
        add_chapter(conn, sid, '["", null, "", "9", ""]', "comicinfo", published_file_id=bulk)
        world.rows("UPDATE chapter SET kind = 'rewrite' WHERE published_file_id = %s RETURNING id", bulk)
        open_rewrites = one(conn, _count(rewrite.OPEN_REWRITES))
        open_conversions = one(conn, _count(conversions.OPEN))
    assert open_rewrites == 1, "the 2 conversions are not counted against compare's rewrites"
    assert open_conversions == 2, "the rewrite is not counted against the conversions"


@budget(load="db", seconds=6)
def test_a_cap_of_0_passes_preflight_and_queues_no_conversion_nor_rewrite(world, caplog):
    adopt(world)  # its intake ran at cap 0
    assert preflight.workflow_settings(world.cfg) is None, "0 is allowed: it keeps conversions and rewrites off"
    assert converting(world) == [], "intake queues no conversion at 0"
    caplog.set_level(logging.INFO)
    world.run(export)
    assert "the comparison waits" in caplog.text, "export's compare queues no rewrite at 0"
    assert world.rows("SELECT id FROM chapter WHERE kind = 'rewrite'") == [], "no rewrite chapter"


def _count(where):
    return db.sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(where)


# --- the link (discover)


@budget(load="db", seconds=6)
def test_the_conversions_download_is_a_hard_link_with_the_files_name(world):
    adopt(world, cap=1)  # queued and drained in the same run
    link, library = link_of(world, "Ch.1.cbz"), world.library / SERIES / "Ch.1.cbz"
    assert link.name == library.name, "the link keeps the file's name, which discover and the later stages read"
    assert link.samefile(library), "a hard link: no space taken"
    assert converting(world) == [("Ch.1.cbz", "junk", "ready")]


@budget(load="db", seconds=6)
def test_an_unlinked_conversion_is_linked_when_discover_claims_it(world):
    adopt(world)
    set_cap(world, 1)
    queue(world)
    assert not link_of(world, "Ch.1.cbz").exists(), "the queue makes no link"
    world.run(intake)
    assert link_of(world, "Ch.1.cbz").samefile(world.library / SERIES / "Ch.1.cbz")
    assert converting(world) == [("Ch.1.cbz", "junk", "ready")]


@budget(load="db", seconds=6)
def test_a_right_link_is_kept(world, monkeypatch):
    adopt(world)
    set_cap(world, 1)
    queue(world)
    link = link_of(world, "Ch.1.cbz")
    link.parent.mkdir(parents=True)
    os.link(world.library / SERIES / "Ch.1.cbz", link)

    def no_link(*_args):
        pytest.fail("a link that is its file is kept, not made again")

    monkeypatch.setattr(conversions.os, "link", no_link)
    assert isinstance(stage(world, "intake", chapter_of(world, "Ch.1.cbz"), discover.run), workflow.Done)
    assert link.samefile(world.library / SERIES / "Ch.1.cbz")


@budget(load="db", seconds=6)
def test_a_wrong_file_at_a_link_path_is_never_read(world):
    adopt(world)
    set_cap(world, 1)
    queue(world)
    link = link_of(world, "Ch.1.cbz")
    link.parent.mkdir(parents=True)
    link.write_bytes(cbz(seed="another chapter", pages=4))
    world.run(intake)
    library = world.library / SERIES / "Ch.1.cbz"
    assert link.samefile(library), "removed unread, and the right file linked"
    ((digest,),) = world.rows("SELECT content_digest FROM chapter")
    assert digest == archive.pages_digest(library), "the chapter read its file, never the other one"


@budget(load="db", seconds=6)
def test_a_directory_at_a_link_path_is_a_link_error(world):
    adopt(world)
    set_cap(world, 1)
    queue(world)
    link = link_of(world, "Ch.1.cbz")
    (link / "inside").mkdir(parents=True)
    world.run(intake)
    ((stage_, status_, error),) = world.rows("SELECT stage, status, last_error FROM chapter")
    assert (stage_, status_) == ("discover", "needs_human")
    assert "is not a file" in error, "named as a link error"
    assert (link / "inside").is_dir(), "nothing removed"


@budget(load="db", seconds=8)
def test_a_missing_adopted_file_holds_only_its_own_chapter(world):
    adopt(world, {f"Ch.{n}.cbz": cbz(seed=f"a{n}", pages=4) for n in (1, 2)})
    (world.library / SERIES / "Ch.1.cbz").unlink()
    set_cap(world, 2)
    world.run(intake)
    assert converting(world) == [("Ch.1.cbz", "discover", "needs_human"), ("Ch.2.cbz", "junk", "ready")]
    ((error,),) = world.rows("SELECT last_error FROM chapter WHERE status = 'needs_human'")
    assert error == f"the adopted file library/{SERIES}/Ch.1.cbz is missing", "names the file"
    assert world.rows("SELECT status FROM series") == [("ok",)], "the series goes on"


@budget(load="db", seconds=30)
def test_a_retried_missing_file_chapter_is_relinked_and_converted(world):
    data = cbz(seed="a1", pages=4)
    adopt(world, {"Ch.1.cbz": data})
    (world.library / SERIES / "Ch.1.cbz").unlink()
    set_cap(world, 1)
    world.run(intake)
    assert converting(world) == [("Ch.1.cbz", "discover", "needs_human")], "set up"
    (world.library / SERIES / "Ch.1.cbz").write_bytes(data)
    with closing(db.connect(world.cfg)) as conn:
        workflow.retry_chapter(conn, chapter_of(world, "Ch.1.cbz"))
    for part in (intake, process, export):
        world.run(part)
    assert converting(world) == [("Ch.1.cbz", "publish", "done")]
    assert world.rows("SELECT origin FROM published_file") == [("pipeline",)], "linked again and converted"


@budget(load="db", seconds=8)
def test_a_retry_during_an_intake_run_is_linked_and_not_dropped(world, monkeypatch):
    data = cbz(seed="a1", pages=4)
    adopt(world, {"Ch.1.cbz": data})
    (world.library / SERIES / "Ch.1.cbz").unlink()
    set_cap(world, 1)
    world.run(intake)
    chapter_id = chapter_of(world, "Ch.1.cbz")
    (world.library / SERIES / "Ch.1.cbz").write_bytes(data)
    queued = conversions.queue

    def retry_meanwhile(cfg, conn, series=None):
        """Run the queue; then a person retries the chapter while the run drains."""
        done = queued(cfg, conn, series)
        with closing(db.connect(world.cfg)) as other:
            workflow.retry_chapter(other, chapter_id)
        return done

    monkeypatch.setattr(intake.conversions, "queue", retry_meanwhile)
    world.run(intake)
    assert converting(world) == [("Ch.1.cbz", "junk", "ready")], "linked when claimed, never dropped as gone"


@budget(load="db", seconds=6)
def test_a_dropped_share_at_a_conversions_link_is_unreachable(world, monkeypatch):
    adopt(world)
    set_cap(world, 1)
    queue(world)

    def share_down(_path):
        raise OSError(errno.EIO, "share gone")

    monkeypatch.setattr(conversions.errors, "exists", share_down)
    outcome = stage(world, "intake", chapter_of(world, "Ch.1.cbz"), discover.run)
    assert isinstance(outcome, Unreachable), "a dropped share is the run's problem"
    assert outcome.service == "share"
    assert world.rows("SELECT stage, status, attempts FROM chapter") == [("discover", "ready", 0)], "no attempt"


@budget(load="db", seconds=8)
def test_a_reset_removes_the_links_and_intake_queues_them_again(world):
    adopt(world, cap=1)
    link = link_of(world, "Ch.1.cbz")
    assert link.exists(), "set up"
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
    assert not link.exists(), "the links go with the staging folder"
    assert (world.library / SERIES / "Ch.1.cbz").exists(), "never the library file"
    world.run(intake)
    assert converting(world)[-1] == ("Ch.1.cbz", "junk", "ready"), "queued and linked again"
    assert link.samefile(world.library / SERIES / "Ch.1.cbz")


@budget(load="db", seconds=6)
def test_left_link_folders_are_removed(world):
    adopt(world, cap=1)
    left = config.abs_path(world.cfg["paths"]["staging"]) / "adopted" / "999"
    left.mkdir()
    (left / "Ch.9.cbz").write_bytes(b"left by a crash")
    world.run(intake)
    assert not left.exists(), "no open chapter's download is in it"
    assert link_of(world, "Ch.1.cbz").exists(), "an open chapter's link stays"


# --- the stages


@budget(load="db", seconds=8)
def test_a_conversion_at_validate_takes_its_files_cover_and_resolves_none(world, monkeypatch):
    cover = png((200, 10, 10))
    pages = {f"{i:03}.png": png((40 * i, 90, 120)) for i in range(1, 5)}
    layout_override(world)
    adopt(
        world,
        {
            "Vol.1 Ch.1.cbz": archive_of({"000_cover.png": cover, **pages}),
            "Vol.1 Ch.2.cbz": archive_of({"000_cover.png": b"", **pages}),  # its cover page cannot be read
        },
    )

    def no_resolve(*_args):
        pytest.fail("a conversion's volume cover is its file's, never resolved")

    monkeypatch.setattr(validate.chapter_names, "resolve", no_resolve)
    set_cap(world, 2)
    world.run(intake)
    ids = file_ids(world)
    good, bad = (one_chapter(world, ids[n]) for n in ("Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"))
    assert world.rows("SELECT stage FROM chapter WHERE id = %s", good) == [("junk",)]
    assert world.rows("SELECT volume, sha256 FROM volume_cover WHERE chapter_id = %s", good) == [
        ("1", hashlib.sha256(cover).hexdigest())
    ], "the cover its file holds, its own cover page stored"
    assert world.rows("SELECT stage FROM chapter WHERE id = %s", bad) == [("validate",)], "validation decides first"
    assert world.rows("SELECT count(*) FROM volume_cover WHERE chapter_id = %s", bad) == [(0,)], "its cover never read"


def one_chapter(world, file_id):
    ((chapter_id,),) = world.rows("SELECT id FROM chapter WHERE published_file_id = %s", file_id)
    return chapter_id


@budget(load="db", seconds=20)
def test_a_conversion_is_never_put_aside_as_handled_nor_judged_as_a_notice(world, monkeypatch):
    adopt(world, {"Ch.1.cbz": cbz(seed="a1", pages=4)})
    digest = archive.pages_digest(world.library / SERIES / "Ch.1.cbz")
    world.rows(
        """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
        SELECT id, '["", null, "", "1", ""]', 'published', %s, '2026-01-01T00:00:00+00:00' FROM series RETURNING id""",
        digest,
    )

    def no_notice(*_args):
        pytest.fail("a conversion's file is published: never judged as a notice")

    monkeypatch.setattr(process.junk.notices, "judge", no_notice)
    set_cap(world, 1)
    world.run(intake)
    assert converting(world) == [("Ch.1.cbz", "junk", "ready")], "not put aside at discover"
    world.run(process)
    assert converting(world) == [("Ch.1.cbz", "comicinfo", "ready")], "nor at junk, nor judged a notice"


@budget(load="db", seconds=30)
def test_a_conversion_replaces_its_file_end_to_end_with_origin_pipeline_and_its_measures(world):
    data = cbz(seed="a1", pages=4)
    adopt(world, {"Ch.1.cbz": data})
    library = world.library / SERIES / "Ch.1.cbz"
    original = archive.pages_digest(library)
    set_cap(world, 1)
    for part in (intake, process, export):
        world.run(part)
    ((origin, pages_in, width, length, junk, path),) = world.rows(
        "SELECT origin, pages_in, median_page_width, strip_length, junk, library_path FROM published_file"
    )
    assert origin == "pipeline", "the file is converted"
    assert None not in (pages_in, width, length, junk), "with the measurements and junk publish records"
    assert path == f"library/{SERIES}/Ch.1.cbz", "under its name"
    assert library.read_bytes() != data, "the library file is replaced"
    retained = list((config.abs_path(world.cfg["paths"]["retention"]) / "library-files").rglob("Ch.1.cbz"))
    assert [archive.pages_digest(p) for p in retained] == [original], "the old bytes kept the retention way"
    assert converting(world) == [("Ch.1.cbz", "publish", "done")]
    world.run(intake)
    assert not list((config.abs_path(world.cfg["paths"]["staging"]) / "adopted").iterdir()), "the link is gone"
    assert converting(world) == [("Ch.1.cbz", "publish", "done")], "never queued again"


@budget(load="db", seconds=10)
@pytest.mark.parametrize(
    ("broken", "truncated"),
    [("005.png", False), ("000_cover.png", False), ("000_cover.png", True)],
    ids=["empty-content-page", "empty-cover-page", "truncated-cover-page"],
)
def test_a_bad_adopted_file_is_downloaded_again_and_stays_conversion_work(world, broken, truncated):
    layout_override(world)
    pages = {f"{i:03}.png": png((40 * i, 90, 120)) for i in range(1, 5)}
    bad = png((9, 9, 9))[:60] if truncated else b""  # a sound header on a cut body, or nothing
    adopt(world, {"Ch.1.cbz": cbz(seed="a1", pages=4), "Ch.2.cbz": archive_of({**pages, broken: bad})})
    manga = world.manga(SERIES)
    world.download(manga, "Chapter 9")  # links the manga to the series
    other = world.suwayomi.add_manga(SERIES, world.suwayomi.add_source("Other"), in_library=False)
    for n in range(1, 10):
        world.suwayomi.add_chapter(manga, f"Chapter {n}")
        world.suwayomi.add_chapter(other, f"Ch. {n}")
    set_cap(world, 2)
    world.run(intake)
    chapter_id = chapter_of(world, "Ch.2.cbz")
    assert world.rows("SELECT stage, status, waiting_on FROM chapter WHERE id = %s", chapter_id) == [
        ("validate", "waiting", "suwayomi_download")
    ], "rejected and downloaded again"
    assert not link_of(world, "Ch.2.cbz").exists(), "its link went the retention way"
    assert (world.library / SERIES / "Ch.2.cbz").exists(), "the library file stays"
    world.suwayomi.finish_downloads(cbz(seed="good", pages=4))
    world.run(intake)
    ((stage_, path),) = world.rows("SELECT stage, download_path FROM chapter WHERE id = %s", chapter_id)
    assert stage_ == "junk", "the new download takes its place, an unreadable cover page giving no cover"
    assert not path.startswith("staging/"), "its download is the new one"
    assert world.rows("SELECT count(*) FROM volume_cover WHERE chapter_id = %s", chapter_id) == [(0,)], (
        "its file's broken cover page is never taken"
    )
    adopted = workflow.REPLACES_ADOPTED.format(c=db.sql.Identifier("c"))
    with closing(db.connect(world.cfg)) as conn:
        query = db.sql.SQL("SELECT id FROM chapter c WHERE {}").format(adopted)
        assert chapter_id in [r[0] for r in conn.execute(query)], "it goes by the file named: still conversion work"


# --- status: files left as they are


@budget(load="db", seconds=8)
def test_a_dropped_conversion_leaves_its_file_and_status_lists_it(world):
    adopt(world, {"Ch.1.cbz": cbz(seed="a1", pages=4)})
    set_cap(world, 1)
    queue(world)
    (link_of(world, "Ch.1.cbz") / "x").mkdir(parents=True)  # a link error: needs_human at discover
    world.run(intake)
    chapter_id = chapter_of(world, "Ch.1.cbz")
    cli.chapter_drop(world.cfg, argparse.Namespace(chapter=chapter_id))
    world.run(intake)
    assert (world.library / SERIES / "Ch.1.cbz").exists(), "the file stays"
    assert not link_of(world, "Ch.1.cbz").parent.exists(), "its link folder goes once the chapter is done"
    assert world.rows("SELECT count(*) FROM chapter") == [(1,)], "only state reset queues it again"
    file_id = file_ids(world)["Ch.1.cbz"]
    assert f"  file {file_id} (library/{SERIES}/Ch.1.cbz) left as it is: chapter {chapter_id}, dropped by a person" in (
        report(world)
    )


def queued_on_its_row(world, monkeypatch):
    """Run 1 conversion until its replacement is queued on its file's row, not finished; return its file id."""
    adopt(world, {"Ch.1.cbz": cbz(seed="a1", pages=4)})
    set_cap(world, 1)
    world.run(intake)
    world.run(process)
    with monkeypatch.context() as m:
        m.setattr(export.rewrite, "finish_all", lambda _ctx: None)
        world.run(export)
    ((file_id, staged),) = world.rows("SELECT id, pending_staged FROM published_file WHERE pending_path IS NOT NULL")
    assert staged is not None, "set up: queued"
    return file_id, staged


@budget(load="db", seconds=30)
def test_a_given_up_replacement_is_listed_as_given_up_and_reset_queues_it_again(world, monkeypatch):
    file_id, staged = queued_on_its_row(world, monkeypatch)
    config.to_local(world.cfg, staged).unlink()  # its staged file gone: export gives the replacement up
    world.run(export)
    assert world.rows("SELECT origin, pending_path FROM published_file") == [("library", None)], "given up"
    ((chapter_id,),) = world.rows("SELECT id FROM chapter")
    assert (
        f"  file {file_id} (library/{SERIES}/Ch.1.cbz) left as it is: chapter {chapter_id}, replacement given up"
        in report(world)
    )
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
    world.run(intake)
    assert converting(world) == [("Ch.1.cbz", "junk", "ready")], "reset queues it again"


@budget(load="db", seconds=30)
def test_a_busy_file_or_a_drop_at_publish_is_not_listed_as_replacement_given_up(world, monkeypatch):
    file_id, _staged = queued_on_its_row(world, monkeypatch)
    assert not [line for line in report(world) if "left as it is" in line], "a queued replacement is busy: not listed"
    world.rows(
        """UPDATE published_file SET pending_path = NULL, pending_staged = NULL, pending_step = NULL,
            pending_copy = NULL, carry_cover = NULL WHERE id = %s RETURNING id""",
        file_id,
    )
    world.rows("UPDATE chapter SET last_error = 'dropped: a duplicate' RETURNING id")
    (line,) = [line for line in report(world) if "left as it is" in line]
    assert line.endswith("dropped: a duplicate"), "a drop at publish gives its drop reason"


# --- priority


@budget(load="db", seconds=6)
def test_claim_picks_every_download_before_any_conversion(world):
    adopt(world)
    set_cap(world, 3)
    queue(world)  # the conversions are discovered first
    world.rows("UPDATE chapter SET stage = 'comicinfo', number = 1 RETURNING id")
    with closing(db.connect(world.cfg)) as conn:
        other = add_series(conn, "Other", origin="kavita", kavita_library_id=LIBRARY_ID)
        download = add_chapter(conn, other, '["", null, "", "5", ""]', "comicinfo")
        ctx = world.context(conn, "export")
        assert workflow.series_with_ready(ctx, ("comicinfo",))[0] == other, "a series with a download first"
        first = workflow.claim(ctx, ("comicinfo",))
    assert first is not None
    assert first["id"] == download, "every download before any conversion, though discovered later"


@budget(load="db", seconds=6)
def test_a_download_at_convert_is_claimed_while_its_series_has_ready_junk_conversions(world):
    adopt(world)
    set_cap(world, 3)
    queue(world)
    ids = file_ids(world)
    world.rows("UPDATE chapter SET stage = 'junk', number = 1 RETURNING id")
    world.rows("UPDATE chapter SET stage = 'convert' WHERE published_file_id = %s RETURNING id", ids["Ch.3.cbz"])
    with closing(db.connect(world.cfg)) as conn:
        sid = one(conn, "SELECT id FROM series")
        ctx = world.context(conn, "process")
        assert workflow.claim(ctx, ("convert",)) is None, "a conversion at convert waits for its series' junk"
        download = add_chapter(conn, sid, '["", null, "", "5", ""]', "convert")
        claimed_row = workflow.claim(ctx, ("convert",))
    assert claimed_row is not None
    assert claimed_row["id"] == download, "a download at convert ignores its series' ready junk conversions"


@budget(load="db", seconds=6)
def test_candidates_a_download_waits_on_are_listed_before_conversion_candidates(world):
    adopt(world)
    set_cap(world, 1)
    queue(world)
    world.rows("UPDATE chapter SET stage = 'junk', number = 1 RETURNING id")
    with closing(db.connect(world.cfg)) as conn:
        sid = one(conn, "SELECT id FROM series")
        download = add_chapter(conn, sid, '["", null, "", "5", ""]', "junk")
        conversion = chapter_of(world, "Ch.1.cbz")
        made = []
        for chapter_id in (conversion, download):  # the conversion's candidate is the older
            with db.transaction(conn):
                cid = one(
                    conn,
                    """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)
                    VALUES ('S', 'page', %s, 'r', %s, '2026-01-01T00:00:00+00:00') RETURNING id""",
                    (f"{chapter_id:016x}|0.7", f"c-{chapter_id}"),
                )
                conn.execute(
                    """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates)
                    VALUES (%s, %s, 'd', '{}', '2026-01-01T00:00:00+00:00', %s)""",
                    (chapter_id, sid, f"[{cid}]"),
                )
                conn.execute(
                    "UPDATE chapter SET status = 'waiting', waiting_on = 'junk_decision' WHERE id = %s", (chapter_id,)
                )
            made.append(cid)
        listed = [r["id"] for r in candidates.open_rows(conn)]
    assert listed == [made[1], made[0]], "the download's candidate first, though newer"


@budget(load="db", seconds=10)
def test_intake_validates_every_download_before_any_conversion(world, monkeypatch):
    adopt(world)
    set_cap(world, 3)
    queue(world)  # the conversions are older than the downloads
    world.download(world.manga(SERIES), "Chapter 4")
    world.kavita_series("Other")  # a later series: a per-series drain would reach its download after the conversions
    world.download(world.manga("Other"), "Chapter 5")
    seen = []
    for name, run in intake.STAGES.items():

        def watched(ctx, chapter, run=run, name=name):
            seen.append((name, chapter["published_file_id"] is not None))
            return run(ctx, chapter)

        monkeypatch.setitem(intake.STAGES, name, watched)
    world.run(intake)
    downloads = [i for i, (_, conversion) in enumerate(seen) if not conversion]
    conversions_seen = [i for i, (_, conversion) in enumerate(seen) if conversion]
    assert len(downloads) == 4, "2 downloads through discover and validate"
    assert len(conversions_seen) == 6, "3 conversions through discover and validate"
    assert max(downloads) < min(conversions_seen), "every download is validated before any conversion"


# --- names and titles (SPEC "Publishing", "Retroactive updates")


def store_list(world, titles):
    """Store a platform list for the series whose main episodes are {chapter number text: title}."""
    eps = [episode_record(episodes.MAIN, n, t) for n, t in titles.items()]
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(
            "INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)"
            " SELECT id, %s, %s, %s, 'x', '2026-11-01' FROM series",
            (platforms.WEBTOONS, platforms.KIND, json.dumps(platform_list(eps))),
        )


def library_files(world):
    return sorted(p.name for p in (world.library / SERIES).iterdir())


@budget(load="db", seconds=30)
def test_a_conversion_is_named_as_the_pipeline_names_a_file_taking_its_list_title_when_untitled(world):
    names = ("Ch.1.cbz", "Ch.2 - Own.cbz", "S2 - Chapter 3.cbz", "Side Story 1.cbz")
    adopt(world, {n: cbz(seed=n, pages=4) for n in names})
    store_list(world, {"1": "Ep. 1 - Start", "2": "Listed", "3": "Wrong Season"})
    set_cap(world, 4)
    for part in (intake, process, export):
        world.run(part)
    assert {status for _, _, status in converting(world)} == {"done"}, "set up: all 4 converted"
    assert sorted(world.rows("SELECT library_path, comicinfo_title FROM published_file")) == [
        (f"library/{SERIES}/Ch.1 - Side Story 1.cbz", "Side Story 1"),
        (f"library/{SERIES}/Ch.1 - Start.cbz", "Start"),
        (f"library/{SERIES}/Ch.2 - Own.cbz", "Own"),
        (f"library/{SERIES}/Ch.3.cbz", None),
    ], "an untitled main file takes its list title, a titled one keeps its own, a seasonal one none, a side one its own"
    assert library_files(world) == ["Ch.1 - Side Story 1.cbz", "Ch.1 - Start.cbz", "Ch.2 - Own.cbz", "Ch.3.cbz"], (
        "each file renamed in place, the old names gone"
    )


@budget(load="db", seconds=30)
def test_list_titles_false_keeps_an_untitled_conversion_untitled(world):
    adopt(world, {"Ch.1.cbz": cbz(seed="a", pages=4)})
    store_list(world, {"1": "Start"})
    (config.abs_path(world.cfg["paths"]["naming_overrides"]) / f"{SERIES}.yaml").write_bytes(b"list_titles: false\n")
    set_cap(world, 1)
    for part in (intake, process, export):
        world.run(part)
    assert converting(world) == [("Ch.1.cbz", "publish", "done")], "SPEC 'Publishing': the series turns list titles off"


@budget(load="db", seconds=30)
def test_2_conversions_naming_1_file_in_1_export_publish_the_first_and_hold_the_second(world):
    adopt(world, {"S1 - Chapter 1.cbz": cbz(seed="s1", pages=4), "S2 - Chapter 1.cbz": cbz(seed="s2", pages=4)})
    set_cap(world, 2)
    for part in (intake, process, export):
        world.run(part)
    got = sorted(world.rows("SELECT status FROM chapter WHERE published_file_id IS NOT NULL"))
    assert got == [("done",), ("needs_human",)], "the second names a file the first has queued"
    assert library_files(world) == ["Ch.1.cbz", "S2 - Chapter 1.cbz"], "never a failed rename, never an overwrite"
    assert world.rows("SELECT count(*) FROM published_file WHERE pending_error IS NOT NULL") == [(0,)]


@budget(load="db", seconds=30)
def test_a_conversion_naming_a_file_already_in_the_folder_is_needs_human(world):
    adopt(world, {"Ch.1.cbz": cbz(seed="a", pages=4)})
    store_list(world, {"1": "Start"})
    (world.library / SERIES / "Ch.1 - Start.cbz").write_bytes(b"a stray file")
    set_cap(world, 1)
    for part in (intake, process, export):
        world.run(part)
    assert world.rows("SELECT status FROM chapter WHERE published_file_id IS NOT NULL") == [("needs_human",)]
    assert (world.library / SERIES / "Ch.1 - Start.cbz").read_bytes() == b"a stray file", "never overwritten"


@budget(load="db", seconds=10)
def test_a_new_chapter_naming_a_file_queued_for_another_is_needs_human(world):
    adopt(world, {"Ch.1.cbz": cbz(seed="a", pages=4)})
    queued = f"library/{SERIES}/Ch.9.cbz"
    staged = config.abs_path(world.cfg["paths"]["staging"]) / "9.cbz"
    staged.write_bytes(cbz(seed="9", pages=4))
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(
            "UPDATE published_file SET pending_path = %s, pending_staged = %s, pending_step = 0", (queued, queued)
        )
        chapter_id = add_chapter(
            conn,
            1,
            '["grp", null, null, "9", ""]',
            stage="publish",
            library_path=queued,
            staged_path=config.to_stored(world.cfg, staged),
        )
    stage(world, "export", chapter_id, publish.run)
    assert world.rows("SELECT status FROM chapter WHERE id = %s", chapter_id) == [("needs_human",)], (
        "SPEC 'Retroactive updates': a name another file's queued rewrite holds is never taken"
    )
    assert not (world.library / SERIES / "Ch.9.cbz").exists()
