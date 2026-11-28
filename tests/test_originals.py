"""Originals and previous versions (SPEC "Publishing", "Retroactive updates"; plan phase-10-9).

A published file's row records where its download was kept (its original), and a replace by a download chapter where
the replaced file was kept (its previous version).
"""

import json
from contextlib import closing

import pytest

from manga_pipeline import archive, config, db, export, intake, process, retention, rewrite
from scripts import record_originals

from tests.builders import config_file, running
from tests.test_better_copies import SERIES, library, pages, through
from tests.test_rewrites import override, scan
from tests.timing import budget
from tests.world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def manga(world):
    found = next((i for i, m in world.suwayomi.manga_rows.items() if m["title"] == SERIES), None)
    return found or world.manga(SERIES)


def chapter_3(world, width=16, scanlator=None):
    """Add a download of chapter 3 (new to the series) of `width` px pages; return its path."""
    return world.download(manga(world), "Chapter 3", scanlator, data=pages(width, 4, f"3{scanlator}"))[1]


def file_row(world, name):
    ((r,),) = world.rows("SELECT to_jsonb(p) FROM published_file p WHERE library_path = %s", f"library/{SERIES}/{name}")
    return r


def kept_file(world, stored):
    return config.to_local(world.cfg, stored)


def confirmed_chapter_3(world, width=16):
    """Publish and confirm chapter 3 as Ch.3.cbz; return its download's content digest."""
    library(world)
    download = chapter_3(world, width)
    digest = archive.pages_digest(download)
    through(world)
    scan(world)  # Kavita lists Ch.3.cbz
    world.run(export)  # confirms it
    assert file_row(world, "Ch.3.cbz")["state"] == "confirmed", "set up"
    return digest


@budget(load="db", seconds=15)
def test_a_confirmed_download_records_where_it_was_kept(world):
    digest = confirmed_chapter_3(world)
    original = file_row(world, "Ch.3.cbz")["original"]
    assert original is not None, "the row records its original"
    path = kept_file(world, original)
    assert path.is_relative_to(config.abs_path(world.cfg["paths"]["retention"])), "kept in retention"
    assert archive.pages_digest(path) == digest, "it is the download"
    assert file_row(world, "Ch.3.cbz")["previous"] is None, "a new file replaced nothing"


@budget(load="db", seconds=15)
def test_a_crash_after_confirm_put_the_download_aside_records_it_on_the_retry(world, monkeypatch):
    library(world)
    digest = archive.pages_digest(chapter_3(world))
    through(world)
    real = retention.put

    def put_then_die(cfg, download):
        real(cfg, download)
        msg = "killed"
        raise KeyboardInterrupt(msg)

    scan(world)
    monkeypatch.setattr(retention, "put", put_then_die)
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    world.run(export)  # confirm again: the download is gone already
    original = file_row(world, "Ch.3.cbz")["original"]
    assert original is not None, "found again by its content digest"
    assert archive.pages_digest(kept_file(world, original)) == digest


@budget(load="db", seconds=15)
def test_a_download_deleted_at_retention_0_records_no_original(tmp_path):
    world = World(tmp_path, export={"retention_days": 0, "rewrites_open": 50})
    confirmed_chapter_3(world)
    assert file_row(world, "Ch.3.cbz")["original"] is None


COPY = "Chapter 3 [R18]"  # a higher edition rating replaces the file whatever its measures (SPEC "Library copies")


def better_copy(world):
    """Add an uncensored chapter 3, which replaces Ch.3.cbz; return its download's content digest."""
    download = world.download(manga(world), COPY, data=pages(16, 4, "copy"))[1]
    return archive.pages_digest(download)


@budget(load="db", seconds=20)
def test_a_better_copy_records_its_own_original_and_the_replaced_file(world):
    confirmed_chapter_3(world)
    before = file_row(world, "Ch.3.cbz")["original"]
    published = (world.library / SERIES / "Ch.3.cbz").read_bytes()
    digest = better_copy(world)
    through(world)
    r = file_row(world, "Ch.3.cbz")
    assert r["source_name"] == COPY, "the copy took the file"
    assert r["original"] not in (None, before), "the copy's own download, not the first one"
    assert archive.pages_digest(kept_file(world, r["original"])) == digest
    assert r["previous"] is not None, "the replaced file's retention link, recorded in 1 pass"
    previous = kept_file(world, r["previous"])
    assert previous.is_relative_to(config.abs_path(world.cfg["paths"]["retention"]) / "library-files")
    assert previous.read_bytes() == published, "it is the file as it was published"
    assert (r["pending_previous"], r["pending_copy"]) == (None, None)


@budget(load="db", seconds=20)
def test_a_crash_after_a_copy_put_its_download_aside_records_the_copys_original(world, monkeypatch):
    confirmed_chapter_3(world)
    before = file_row(world, "Ch.3.cbz")["original"]
    digest = better_copy(world)
    for part in (intake, intake, process):
        world.run(part)
    real = retention.put

    def put_then_die(cfg, download):
        real(cfg, download)
        msg = "killed"
        raise KeyboardInterrupt(msg)

    monkeypatch.setattr(retention, "put", put_then_die)
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    world.run(export)
    r = file_row(world, "Ch.3.cbz")
    assert r["source_name"] == COPY, "the copy took the file"
    assert r["original"] != before, "never the earlier download's"
    assert archive.pages_digest(kept_file(world, r["original"])) == digest, "found again by its content digest"


@budget(load="db", seconds=20)
def test_a_copy_queued_without_an_original_records_none(world, monkeypatch):
    confirmed_chapter_3(world)
    better_copy(world)
    for part in (intake, intake, process):
        world.run(part)
    monkeypatch.setattr(rewrite, "finish_all", lambda _ctx: None)
    world.run(export)  # publishes the copy: its replace is queued, not finished
    monkeypatch.undo()
    ((file_id, queued),) = world.rows("SELECT id, pending_copy FROM published_file WHERE pending_copy IS NOT NULL")
    del queued["original"]  # as a program before originals queued it
    world.rows("UPDATE published_file SET pending_copy = %s WHERE id = %s RETURNING id", json.dumps(queued), file_id)
    world.run(export)
    r = file_row(world, "Ch.3.cbz")
    assert r["source_name"] == COPY, "the copy took the file"
    assert r["original"] is None, "not the earlier download's: the row now describes the copy"


@budget(load="db", seconds=20)
def test_a_comicinfo_rewrite_records_no_previous_version(world):
    confirmed_chapter_3(world)
    original = file_row(world, "Ch.3.cbz")["original"]
    earlier = f"retention/library-files/{SERIES}/an earlier.cbz"
    world.rows("UPDATE published_file SET previous = %s WHERE library_path LIKE '%%Ch.3.cbz' RETURNING id", earlier)
    override(world, {1: "1-"})
    world.run(export)
    r = file_row(world, "Vol.1 Ch.3.cbz")
    assert (r["original"], r["previous"]) == (original, earlier), "only a download chapter's replace changes them"


@budget(load="db", seconds=20)
def test_a_copy_given_up_after_its_retention_act_leaves_previous_as_it_was(world, monkeypatch):
    confirmed_chapter_3(world)
    earlier = f"retention/library-files/{SERIES}/an earlier.cbz"
    world.rows("UPDATE published_file SET previous = %s WHERE library_path LIKE '%%Ch.3.cbz' RETURNING id", earlier)
    better_copy(world)
    for part in (intake, intake, process):
        world.run(part)
    real = rewrite.record_step

    def die_after_retained(conn, file_id, step):
        done = real(conn, file_id, step)
        if step == rewrite.STEP_RETAINED:
            msg = "killed"
            raise KeyboardInterrupt(msg)
        return done

    monkeypatch.setattr(rewrite, "record_step", die_after_retained)
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    ((file_id, pending_previous),) = world.rows(
        "SELECT id, pending_previous FROM published_file WHERE pending_copy IS NOT NULL"
    )
    assert pending_previous is not None, "set up: the retention act recorded its link"
    limit = world.cfg["workflow"]["attempt_limit"]
    world.rows("UPDATE published_file SET pending_attempts = %s WHERE id = %s RETURNING id", limit, file_id)
    with closing(db.connect(world.cfg)) as conn:
        rewrite.drop(conn, file_id, limit)  # given up before its rename
    r = file_row(world, "Ch.3.cbz")
    assert r["source_name"] == "Chapter 3", "set up: the file stays as it was"
    assert (r["previous"], r["pending_previous"]) == (earlier, None), "a replace given up records no previous version"


@budget(load="db", seconds=20)
def test_a_publish_over_a_gone_files_row_clears_its_original(world):
    confirmed_chapter_3(world)
    world.rows(  # a row left at Ch.4.cbz whose file is gone (the target is free)
        "UPDATE published_file SET library_path = %s, previous = original, pending_previous = original"
        " WHERE library_path = %s RETURNING id",
        f"library/{SERIES}/Ch.4.cbz",
        f"library/{SERIES}/Ch.3.cbz",
    )
    world.download(manga(world), "Chapter 4", data=pages(16, 4, "4"))
    through(world)
    r = file_row(world, "Ch.4.cbz")
    assert (r["original"], r["previous"], r["pending_previous"]) == (None, None, None), (
        "the new file's are its own, recorded at confirm"
    )


@budget(load="db", seconds=20)
def test_the_backfill_records_each_files_original_by_its_downloads_name(world, tmp_path, capsys):
    digest = confirmed_chapter_3(world)
    world.rows(  # published before originals were recorded, then renamed by a rewrite
        "UPDATE published_file SET original = NULL, library_path = %s WHERE library_path = %s RETURNING id",
        f"library/{SERIES}/Vol.1 Ch.3.cbz",
        f"library/{SERIES}/Ch.3.cbz",
    )
    path = str(config_file(tmp_path, world.cfg))
    assert record_originals.main(["--dry-run", "--config", path]) == 0
    assert "files found 1, listed 0" in capsys.readouterr().out
    assert file_row(world, "Vol.1 Ch.3.cbz")["original"] is None, "a dry run writes nothing"
    assert record_originals.main(["--config", path]) == 0
    original = file_row(world, "Vol.1 Ch.3.cbz")["original"]
    assert archive.pages_digest(kept_file(world, original)) == digest
    assert record_originals.main(["--dry-run", "--config", path]) == 0
    assert "files found 0, listed 0" in capsys.readouterr().out.splitlines()[-1]


@budget(load="db", seconds=20)
def test_the_backfill_lists_a_file_whose_chapter_was_dropped(world, tmp_path, capsys):
    confirmed_chapter_3(world)
    world.rows("UPDATE published_file SET original = NULL WHERE library_path LIKE '%%Ch.3.cbz' RETURNING id")
    world.rows("UPDATE chapter SET last_error = 'dropped by a person' WHERE kind = 'download' RETURNING id")
    path = str(config_file(tmp_path, world.cfg))
    assert record_originals.main(["--config", path]) == 1
    out = capsys.readouterr().out
    assert "listed 1" in out
    assert "0 done download chapters named" in out


@budget(load="db", seconds=10)
def test_the_backfill_waits_for_no_part(world, tmp_path):
    path = str(config_file(tmp_path, world.cfg))
    with running(world.cfg, "intake"):
        assert record_originals.main(["--config", path]) == 3
