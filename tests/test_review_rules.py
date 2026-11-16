"""Rules added by the Phase 6d code reviews (SPEC "State", "Parts"): a database holding old-form paths is refused,
a series folder outside paths.library needs a human, a Windows machine skips names Windows cannot use, a dropped SMB
share is not "a file already gone", and junk review records an unreachable share."""

import errno
import os
import shutil
from contextlib import closing

import pytest

from manga_pipeline import archive, config, db, discovery, kavita_link, retention, workflow
from manga_pipeline.junk import candidates, review_run

from tests.builders import add_chapter, add_series, adopted, one, site_cfg
from tests.timing import budget
from tests.world import World


@budget(load="db", seconds=3)
def test_a_database_holding_old_absolute_paths_is_refused(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path, built=False)
    before = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, before)
    (before / "0043_concurrency.sql").unlink()
    monkeypatch.setattr(db, "SCHEMA", before)
    with closing(db.connect(cfg)) as conn:  # a database an older version built and filled
        series_id = add_series(conn, "S")
        with db.transaction(conn):
            conn.execute("UPDATE series SET folder = '/mnt/share/library/S' WHERE id = %s", (series_id,))
    monkeypatch.undo()
    with pytest.raises(db.SchemaError, match=r"stored by an older version.*build a new database"):
        db.connect(cfg)


@budget(load="db", seconds=3)
def test_a_series_folder_outside_the_library_needs_a_human(tmp_path):
    world = World(tmp_path)
    world.kavita_series("Elsewhere", folder="../elsewhere/Elsewhere")
    world.download(world.manga("Elsewhere"), "Chapter 2")
    with closing(db.connect(world.cfg)) as conn:
        discovery.scan(world.context(conn, "intake"))
    ((status, reason, folder),) = world.rows("SELECT status, status_reason, folder FROM series")
    assert status == "needs_human", "held with the folder named"
    assert folder is None, "held with the folder named"
    assert "'..'" in reason, "held with the folder named"


@budget(load="db")
@pytest.mark.parametrize(
    ("folder", "download", "windows_takes"),
    [
        ("library/Plain", "downloads/mangas/Src (EN)/Plain/Chapter 1.cbz", True),
        ("library/Re:Zero", "downloads/mangas/Src (EN)/Re_Zero/Chapter 1.cbz", False),
        ("library/Who Am I?", "downloads/mangas/Src (EN)/Who Am I_/Chapter 1.cbz", False),
        ("library/Trailing.", "downloads/mangas/Src (EN)/Trailing/Chapter 1.cbz", False),
        ("library/Plain", 'downloads/mangas/Src (EN)/Plain/Ch "1".cbz', False),
    ],
)
def test_a_windows_machine_skips_names_windows_cannot_use(tmp_path, monkeypatch, folder, download, windows_takes):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        series_id = add_series(conn, "S")
        with db.transaction(conn):
            conn.execute("UPDATE series SET folder = %s WHERE id = %s", (folder, series_id))
        add_chapter(conn, series_id, "c1", "junk", download_path=download)
        ctx = workflow.Context(cfg, conn, "process", suwayomi=object(), kavita=object())
        monkeypatch.setattr(os, "name", "nt")
        assert (workflow.claim(ctx, ("junk",)) is not None) == windows_takes
        monkeypatch.setattr(os, "name", "posix")
        if not windows_takes:
            assert workflow.claim(ctx, ("junk",)) is not None, "a Linux machine works it"


@budget(load="fs")
def test_a_dropped_smb_share_is_not_a_file_already_gone(tmp_path, monkeypatch):
    present = tmp_path / "a.cbz"
    present.write_bytes(b"x")
    assert workflow.exists(present)
    assert not workflow.exists(tmp_path / "gone.cbz")

    def dropped(_path, *_a, **_k):
        error = FileNotFoundError(errno.ENOENT, "The network name cannot be found")
        error.winerror = 67
        raise error

    cfg = site_cfg(tmp_path, built=False)
    monkeypatch.setattr(os, "stat", dropped)
    with pytest.raises(workflow.Unreachable), workflow.share_errors():
        workflow.exists(present)
    with pytest.raises(workflow.Unreachable), workflow.share_errors():  # put aside, never "already gone"
        retention.put(cfg, present)
    monkeypatch.setattr(archive, "pages_digest", dropped)
    with pytest.raises(workflow.Unreachable), workflow.share_errors():  # a digest, never "unreadable"
        archive.digest_or_none(present)


@budget(load="db")
def test_junk_review_records_an_unreachable_share_and_exits_quietly(tmp_path, monkeypatch):
    cfg = adopted(tmp_path)

    def unreachable(_cfg, _conn):
        raise OSError(errno.EIO, "share gone")

    monkeypatch.setattr(candidates, "cleanup", unreachable)
    with closing(db.connect(cfg)) as conn:
        review_run.run(cfg, conn)
        result = one(conn, "SELECT value FROM meta WHERE key = 'junk-review.last_result'")
    assert result == "unreachable: share"


@budget(load="pure")
def test_a_path_with_a_parent_step_is_never_stored(tmp_path):
    cfg = {"paths": {root: str(tmp_path / "share" / root) for root in config.SHARE_ROOTS}}
    with pytest.raises(config.PathError, match=r"'\.\.'"):
        config.to_stored(cfg, tmp_path / "share" / "library" / ".." / "elsewhere")


@budget(load="pure")
@pytest.mark.parametrize("root", ["staging", None])
def test_a_series_folder_under_another_root_or_none_is_refused(tmp_path, root):
    cfg = {"paths": {r: str(tmp_path / "share" / r) for r in config.SHARE_ROOTS}}
    folder = tmp_path / "share" / root / "S" if root else tmp_path / "elsewhere" / "S"
    with pytest.raises(config.PathError):
        kavita_link.library_folder(cfg, folder)
    assert kavita_link.library_folder(cfg, tmp_path / "share" / "library" / "S") == "library/S", "the library is stored"
