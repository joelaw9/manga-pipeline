"""scripts/library_snapshot.py (plan phase-10-16-recording): each published file's bytes and formats, read only."""

import errno
import io
import json
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import config, db, images, intake, lock, pause
from scripts import library_snapshot

from tests.builders import WinFileNotFoundError, config_file, present
from tests.timing import budget
from tests.world import World

SERIES = "Solo Leveling"
BULK = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", "1"), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", "1"))


def image(fmt, **save):
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, fmt, **save)
    return out.getvalue()


def vp8x_webp():
    """Return a WebP whose ICC chunk puts its image chunk past the first 64 bytes."""
    data = image("WEBP", icc_profile=bytes(200))
    assert images.detect(data[: library_snapshot.HEAD]) == images.UNKNOWN, "set up: the head does not name it"
    return data


def archive_of(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.kavita_series(SERIES, bulk=BULK)
    world.run(intake)  # the baseline records the 2 bulk files
    folder = world.library / SERIES
    png, jpeg = image("PNG"), image("JPEG")  # the same bytes on every call
    (folder / "Vol.1 Ch.1.cbz").write_bytes(
        archive_of({"001.png": png, "002.jpg": jpeg, "__MACOSX/._002.jpg": b"junk", "ComicInfo.xml": b"<x/>"})
    )
    (folder / "Vol.1 Ch.2.cbz").write_bytes(archive_of({"001.webp": vp8x_webp()}))
    return world


def take(world, out, series=None):
    with closing(db.open_connection(world.cfg)) as conn:
        return library_snapshot.take(world.cfg, conn, out, series)


def paused(world):
    with closing(db.connect(world.cfg)) as conn:
        pause.set_pause(conn)


def by_name(snap):
    return {e["library_path"].rsplit("/", 1)[1]: e for e in snap["files"]}


@budget(load="db", seconds=10)
def test_each_file_is_listed_with_its_bytes_and_pages_and_bytes_per_format(world, tmp_path):
    paused(world)
    out = tmp_path / "before.json"
    snap = take(world, out)
    got = by_name(snap)
    assert got["Vol.1 Ch.1.cbz"]["formats"] == {
        images.PNG: [1, len(image("PNG"))],
        images.JPEG: [1, len(image("JPEG"))],
    }, "pages by archive_pages: the __MACOSX member and ComicInfo.xml are no pages; bytes uncompressed"
    assert got["Vol.1 Ch.2.cbz"]["formats"] == {images.WEBP_LOSSY: [1, len(vp8x_webp())]}, (
        "a VP8X WebP is read whole when its head leaves it unknown"
    )
    assert got["Vol.1 Ch.1.cbz"]["bytes"] == (world.library / SERIES / "Vol.1 Ch.1.cbz").stat().st_size
    assert {(e["origin"], e["state"], e["error"]) for e in snap["files"]} == {("bulk", "confirmed", None)}
    assert json.loads(out.read_bytes()) == snap, "written as returned"
    assert snap["summary"]["rows_read"] == snap["summary"]["files_read"] == 2
    first, second = got["Vol.1 Ch.1.cbz"], got["Vol.1 Ch.2.cbz"]
    assert snap["summary"]["formats"] == {"bulk": {**present(first["formats"]), **present(second["formats"])}}
    assert snap["summary"]["bytes"] == {"bulk": present(first["bytes"]) + present(second["bytes"])}


@budget(load="db", seconds=10)
def test_a_missing_file_is_listed_missing_and_a_non_zip_unreadable(world, tmp_path):
    paused(world)
    (world.library / SERIES / "Vol.1 Ch.1.cbz").unlink()
    (world.library / SERIES / "Vol.1 Ch.2.cbz").write_bytes(b"not a zip")
    got = by_name(take(world, tmp_path / "s.json"))
    assert (got["Vol.1 Ch.1.cbz"]["error"], got["Vol.1 Ch.2.cbz"]["error"]) == ("missing", "unreadable")


@budget(load="db", seconds=10)
def test_a_read_error_is_tried_once_more_and_a_second_failure_writes_nothing(world, tmp_path, monkeypatch):
    paused(world)
    real, failed = library_snapshot.file_formats, []

    def once(path):
        if path.name not in failed:
            failed.append(path.name)
            raise OSError(errno.EIO, "share dropped")
        return real(path)

    monkeypatch.setattr(library_snapshot, "file_formats", once)
    assert take(world, tmp_path / "a.json")["summary"]["files_read"] == 2, "each file read on its second try"

    def always(path):
        raise OSError(errno.EIO, f"share dropped at {path.name}")

    monkeypatch.setattr(library_snapshot, "file_formats", always)
    with pytest.raises(OSError, match="share dropped"):
        take(world, tmp_path / "b.json")
    assert not list(tmp_path.glob("b.json*")), "nothing written when a file cannot be read"


@budget(load="db", seconds=10)
def test_it_refuses_while_the_automation_runs_and_an_out_in_retention(world, tmp_path):
    with pytest.raises(library_snapshot.RefusedError, match="not paused"):
        take(world, tmp_path / "s.json")
    paused(world)
    retention = config.abs_path(world.cfg["paths"]["retention"])
    with pytest.raises(library_snapshot.RefusedError, match=r"paths\.retention"):
        take(world, retention / "s.json")


@budget(load="db", seconds=10)
def test_series_takes_only_the_listed_series_rows(world, tmp_path):
    paused(world)
    with closing(db.connect(world.cfg)) as conn:
        series_id = db.one_row(conn, "SELECT id FROM series", ())[0]
    with pytest.raises(library_snapshot.RefusedError, match="without a published file"):
        take(world, tmp_path / "s.json", [series_id, series_id + 1])
    assert len(take(world, tmp_path / "t.json", [series_id])["files"]) == 2


@budget(load="db", seconds=10)
def test_a_broken_archive_member_is_listed_unreadable(world, tmp_path):
    paused(world)
    path = world.library / SERIES / "Vol.1 Ch.1.cbz"
    data = bytearray(archive_of({"001.png": image("PNG") * 50}))
    data[60:80] = bytes(20)  # inside the deflate stream: the archive opens, its member does not inflate
    path.write_bytes(bytes(data))
    assert by_name(take(world, tmp_path / "s.json"))["Vol.1 Ch.1.cbz"]["error"] == "unreadable", (
        "archive.UNREADABLE: what reading a broken archive raises"
    )


@budget(load="db", seconds=10)
def test_a_dropped_share_is_tried_again_not_listed_missing(world, tmp_path, monkeypatch):
    paused(world)
    real, failed = library_snapshot.file_formats, []

    def dropped(path):
        if not failed:
            failed.append(path.name)
            error = WinFileNotFoundError(2, "share gone")
            error.winerror = 53
            raise error
        return real(path)

    monkeypatch.setattr(library_snapshot, "file_formats", dropped)
    snap = take(world, tmp_path / "s.json")
    assert snap["summary"]["errors"] == {}, "a dropped share's FileNotFoundError is no missing file"


@budget(load="db", seconds=10)
def test_it_refuses_an_existing_out_a_missing_folder_and_while_a_scheduled_run_holds_the_automation(world, tmp_path):
    paused(world)
    (tmp_path / "s.json").write_bytes(b"{}")
    with pytest.raises(library_snapshot.RefusedError, match="never overwritten"):
        take(world, tmp_path / "s.json")
    with pytest.raises(library_snapshot.RefusedError, match="not a folder"):
        take(world, tmp_path / "nowhere" / "s.json")
    library = config.abs_path(world.cfg["paths"]["library"])
    library.rename(library.with_name("gone"))
    with pytest.raises(library_snapshot.RefusedError, match="not reachable"):
        take(world, tmp_path / "u.json")
    library.with_name("gone").rename(library)
    with (
        closing(db.connect(world.cfg)) as other,
        lock.held(other, world.cfg, [], lock.SHARED, automation=True),
        pytest.raises(library_snapshot.RefusedError, match="not paused"),
    ):
        take(world, tmp_path / "t.json")


@budget(load="db", seconds=10)
def test_a_resume_during_the_snapshot_writes_nothing(world, tmp_path, monkeypatch):
    paused(world)
    real = library_snapshot.measure_all

    def resumed(cfg, entries):
        with closing(db.connect(world.cfg)) as conn:
            pause.clear(conn)
        return real(cfg, entries)

    monkeypatch.setattr(library_snapshot, "measure_all", resumed)
    with pytest.raises(library_snapshot.RefusedError, match="resumed"):
        take(world, tmp_path / "s.json")
    assert not list(tmp_path.glob("s.json*"))


@budget(load="db", seconds=10)
def test_main_exits_2_refused_and_0_written(world, tmp_path, capsys):
    path = config_file(tmp_path, world.cfg)
    out = tmp_path / "s.json"
    assert library_snapshot.main([str(out), "--config", str(path)]) == 2, "not paused"
    paused(world)
    assert library_snapshot.main([str(out), "--config", str(path)]) == 0
    assert '"files_read": 2' in capsys.readouterr().out
