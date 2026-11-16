"""Retroactive volume cover rewrites (SPEC "Retroactive updates"): a published file whose volume cover should change is
rewritten in place through convert (process), comicinfo and publish, its other pages copied byte for byte, its Page
entries shifted and the reader's progress shifted with them."""

import hashlib
import io
import json
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import comicinfo as comicinfo_xml
from manga_pipeline import (
    config,
    db,
    export,
    intake,
    names,
    process,
    retention,
    rewrite,
    rewrite_covers,
    status,
    workflow,
)
from manga_pipeline.clients import http
from manga_pipeline.metadata import covers as cover_images
from manga_pipeline.metadata import naming, rewrite_plan

from tests.builders import claimed
from tests.test_rewrites import SERIES, files, override, progress, read, scan
from tests.timing import budget
from tests.world import World, bulk_record, cbz, comicinfo


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def png(width, height=None, colour=(200, 40, 40)):
    out = io.BytesIO()
    Image.new("RGB", (width, height or width * 3 // 2), colour).save(out, "PNG")
    return out.getvalue()


def jpeg(width=64, colour=(40, 40, 200)):
    out = io.BytesIO()
    Image.new("RGB", (width, width * 3 // 2), colour).save(out, "JPEG")
    return out.getvalue()


def bulk_file(name, number, cover=False, mixed=False):
    """A bulk-converted CBZ (its ComicInfo as the bulk converter wrote it, with Page entries): a 000_cover page first
    when cover, a JPEG page among its PNG pages when mixed."""
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(cbz(pages=4, seed=name))) as src, zipfile.ZipFile(data, "w") as z:
        pages = [(n, src.read(n)) for n in src.namelist() if n.endswith(".png")]
        if mixed:
            pages[1] = ("002.jpg", jpeg())
        if cover:
            pages.insert(0, ("000_cover.png", png(64)))
        entries = "".join(f'<Page Image="{i}" ImageSize="{len(d)}" />' for i, (_, d) in enumerate(pages))
        for n, d in pages:
            z.writestr(n, d)
        xml = comicinfo(Series=SERIES, Number=number, Volume=1, Translator="Grp", PageCount=len(pages))
        z.writestr("ComicInfo.xml", xml.replace("</ComicInfo>", f"<Pages>{entries}</Pages></ComicInfo>"))
    return data.getvalue()


def library(world, cover=(), mixed=()):
    """As test_rewrites.library: bulk Vol.1 Ch.1 and Vol.1 Ch.2 (the outputs in cover hold a 000_cover page, which the
    record does not list yet; those in mixed hold a JPEG page), pipeline Ch.3 with no volume; all confirmed, their
    covers read by export's comparison. Returns the Kavita id."""
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", "1"), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", "1"))
    kid = world.kavita_series(SERIES, bulk=bulk, basis="filename")
    for name, number in (("Vol.1 Ch.1.cbz", 1), ("Vol.1 Ch.2.cbz", 2)):
        (world.library / SERIES / name).write_bytes(bulk_file(name, number, name in cover, name in mixed))
    world.kavita.scan_library(4)
    world.kavita.run_scans()
    world.download(world.manga(SERIES), "Chapter 3", data=cbz(pages=4, seed="3"))
    for part in (intake, intake, process, export):
        world.run(part)
    world.kavita.run_scans()
    world.run(export)
    assert files(world) == ["Ch.3.cbz", "Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "set up"
    assert world.rows("SELECT count(*) FROM published_file WHERE cover_state IS NULL") == [(0,)], "covers read"
    return kid


def recorded_covers(world, kid, covers):
    """The series' record now lists covers {volume: output}; the files' covers are read again."""
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", "1"), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", "1"))
    chapters = [(o, f"{s}.cbz", n, (world.library / SERIES / o).read_bytes(), v) for o, s, n, v in bulk]
    record = bulk_record(SERIES, kid, chapters, basis="filename", covers=covers)
    (world.library / SERIES / ".optimizer.json").write_text(json.dumps(record), encoding="utf-8")
    world.rows("UPDATE published_file SET cover_state = NULL RETURNING id")


def store_image(world, volume, data):
    """The series' volume cover image, stored as intake stores one."""
    sha = hashlib.sha256(data).hexdigest()
    rel = f"volume/{sha}.png"
    path = config.abs_path(world.cfg["paths"]["covers"]) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    world.rows(
        """INSERT INTO series_volume_cover (series_id, volume, source_url, path, sha256)
        SELECT id, %s, 'https://uploads.mangadex.org/covers/x', %s, %s FROM series WHERE origin = 'kavita'
        ON CONFLICT (series_id, volume) DO UPDATE SET path = excluded.path, sha256 = excluded.sha256
        RETURNING series_id""",
        volume,
        rel,
        sha,
    )
    return sha


def members(world, name):
    with zipfile.ZipFile(world.library / SERIES / name) as z:
        return (
            z.namelist(),
            comicinfo_xml.read_all(z.read("ComicInfo.xml")),
            comicinfo_xml.read_pages(z.read("ComicInfo.xml")),
        )


def states(world):
    return dict(world.rows("SELECT library_path, cover_state FROM published_file ORDER BY id"))


def refreshes(world):
    return [w for w in world.kavita.writes if w[0] == "refresh_covers"]


def rewrites(world):
    return world.rows("SELECT stage, status, drop_cover FROM chapter WHERE kind = 'rewrite' ORDER BY id")


@budget(load="db", seconds=60)
def test_a_cover_stored_after_publish_is_inserted_with_its_volume_in_1_rewrite_and_progress_shifted(world):
    kid = library(world)
    read(world, kid, "Ch.3.cbz", 2)
    override(world, {1: "1-2", 2: "3-"})
    store_image(world, 2, png(900))
    world.run(export)
    assert rewrites(world) == [("convert", "ready", False)], "1 rewrite carries volume, name and cover; at convert"
    world.run(process)
    world.run(export)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"]
    names_, fields, pages = members(world, "Vol.2 Ch.3.cbz")
    assert names_[0] == "000_cover" + names_[1][3:], "the cover goes in first, in the format of the chapter's pages"
    assert pages[0]["Type"] == "FrontCover"
    assert [p["Image"] for p in pages] == [str(i) for i in range(len(pages))]
    assert fields["PageCount"] == str(len(pages))
    assert fields["Volume"] == "2"
    assert states(world)[f"library/{SERIES}/Vol.2 Ch.3.cbz"] == "volume:2"
    scan(world)
    world.run(export)
    assert progress(world, kid)["Vol.2 Ch.3.cbz"][0] == 3, "2 pages read, 1 page put in front"
    assert refreshes(world) == [("refresh_covers", world.cfg["kavita"]["library_id"], kid)], (
        "Kavita asked once to regenerate the series' cover images"
    )
    world.run(export)
    assert len(rewrites(world)) == 1, "nothing more to rewrite"
    assert len(refreshes(world)) == 1, "and not again"


@budget(load="db", seconds=60)
def test_a_moved_boundary_drops_the_old_volumes_cover(world):
    kid = library(world, cover=("Vol.1 Ch.2.cbz",))
    recorded_covers(world, kid, {1: "Vol.1 Ch.2.cbz"})
    world.run(export)  # the covers read again: Ch.2 holds volume 1's, in volume 1: kept
    assert rewrites(world) == []
    assert states(world)[f"library/{SERIES}/Vol.1 Ch.2.cbz"] == "volume:1"
    read(world, kid, "Vol.1 Ch.2.cbz", 3)
    override(world, {1: "1", 2: "2-"})
    world.run(export)
    assert rewrites(world)[0] == ("convert", "ready", True), "volume 2 has no image: volume 1's cover is dropped"
    world.run(process)
    world.run(export)
    names_, fields, pages = members(world, "Vol.1 Ch.2.cbz")
    assert "000_cover.png" not in names_
    assert fields["Volume"] == "2"
    assert len(pages) == 4
    assert "Type" not in pages[0]
    assert [p["Image"] for p in pages] == ["0", "1", "2", "3"]
    assert states(world)[f"library/{SERIES}/Vol.1 Ch.2.cbz"] == "none"
    scan(world)
    world.run(export)
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 2, "3 pages read, the cover page before them gone"


@budget(load="db", seconds=60)
def test_a_dropped_cover_lowers_the_progress_kavita_kept_for_a_same_name_file(world):
    kid = library(world, cover=("Vol.1 Ch.2.cbz",))
    read(world, kid, "Vol.1 Ch.2.cbz", 3)
    recorded_covers(world, kid, {3: "Vol.1 Ch.2.cbz"})  # volume 3's cover in a volume 1 file
    world.run(export)
    world.run(process)
    world.run(export)
    assert "000_cover.png" not in members(world, "Vol.1 Ch.2.cbz")[0]
    scan(world)
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 3, "Kavita kept the chapter and its progress unshifted"
    world.run(export)
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 2, "written lower: the page read is 1 earlier now"


@budget(load="db", seconds=60)
def test_an_unfit_cover_is_recorded_and_tried_again_only_for_a_new_image(world):
    library(world, mixed=("Vol.1 Ch.1.cbz",))
    override(world, {1: "1-2", 2: "3-"})
    sha = store_image(world, 1, png(900))
    before = (world.library / SERIES / "Vol.1 Ch.1.cbz").read_bytes()
    world.run(export)
    world.run(process)
    assert world.rows("SELECT last_error FROM chapter WHERE kind = 'rewrite' AND published_file_id = 1") == [
        ("dropped: volume 1 cover unfit: its pages (JPEG, PNG) share no cover format",)
    ]
    assert states(world)[f"library/{SERIES}/Vol.1 Ch.1.cbz"] == f"unfit:{sha}"
    world.run(export)
    world.run(export)
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite' AND published_file_id = 1") == [(1,)]
    assert (world.library / SERIES / "Vol.1 Ch.1.cbz").read_bytes() == before, "the file is left as it is"
    store_image(world, 1, png(900, colour=(10, 200, 10)))
    world.run(export)
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite' AND published_file_id = 1") == [(2,)]


@budget(load="db", seconds=60)
def test_an_unfit_cover_for_a_file_holding_another_volumes_cover_still_drops_that_cover(world):
    kid = library(world, cover=("Vol.1 Ch.2.cbz",), mixed=("Vol.1 Ch.2.cbz",))
    recorded_covers(world, kid, {1: "Vol.1 Ch.2.cbz"})
    world.run(export)
    override(world, {1: "1", 2: "2-"})
    sha = store_image(world, 2, png(900))
    world.run(export)
    world.run(process)
    world.run(export)
    assert "000_cover.png" not in members(world, "Vol.1 Ch.2.cbz")[0], "volume 1's cover is gone"
    assert states(world)[f"library/{SERIES}/Vol.1 Ch.2.cbz"] == f"unfit:{sha}"


@budget(load="db", seconds=60)
def test_a_wanted_cover_is_fetched_by_intake_and_inserted_by_the_next_export_and_process(world):
    library(world)
    md = world.sources.mangadex
    md.images[("md-1", "v2.png")] = png(900)
    for kind, payload in (
        ("manga", {"id": "md-1", "attributes": {"originalLanguage": "ko"}}),
        ("covers", [{"volume": "2", "locale": "en", "fileName": "v2.png"}]),
    ):
        world.rows(
            """INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)
            SELECT id, 'mangadex', %s, %s, %s, '2026-11-01' FROM series WHERE origin = 'kavita'
            ON CONFLICT (series_id, source, kind) DO UPDATE SET payload = excluded.payload RETURNING series_id""",
            kind,
            json.dumps(payload),
            kind,
        )
    override(world, {1: "1-2", 2: "3-"})
    world.run(export)
    assert world.rows("SELECT volume FROM volume_cover_wanted") == [(2,)]
    assert rewrites(world) == [("publish", "done", False)], "the volume changes now; the cover waits for its image"
    world.run(intake)
    assert world.rows("SELECT count(*) FROM volume_cover_wanted") == [(0,)]
    assert world.rows("SELECT volume FROM series_volume_cover") == [(2,)], "fetched by intake"
    scan(world)
    world.run(export)  # the volume rewrite re-read by Kavita and its progress restored: the file is free again
    world.run(export)
    assert rewrites(world)[-1] == ("convert", "ready", False)
    world.run(process)
    world.run(export)
    assert members(world, "Vol.2 Ch.3.cbz")[0][0].startswith("000_cover.")


@budget(load="db", seconds=60)
def test_a_wanted_cover_failing_to_the_limit_is_listed_and_no_longer_fetched(world):
    library(world)
    world.rows(
        """INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)
        SELECT id, 'mangadex', 'manga', '{"id": "md-1"}', 'm', '2026-11-01' FROM series WHERE origin = 'kavita'
        ON CONFLICT (series_id, source, kind) DO UPDATE SET payload = excluded.payload RETURNING series_id"""
    )
    world.sources.mangadex.images[("md-1", "v2.png")] = b"an error page"
    world.rows(
        """INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)
        SELECT id, 'mangadex', 'covers', '[{"volume": "2", "locale": "en", "fileName": "v2.png"}]', 'c', '2026-11-01'
        FROM series WHERE origin = 'kavita'
        ON CONFLICT (series_id, source, kind) DO UPDATE SET payload = excluded.payload RETURNING series_id"""
    )
    override(world, {1: "1-2", 2: "3-"})
    world.run(export)
    for _ in range(4):
        world.run(intake)
    ((attempts, error),) = world.rows("SELECT attempts, last_error FROM volume_cover_wanted")
    assert attempts == 3 and "not an image" in error, "3 attempts (workflow.attempt_limit), then left"
    with db.read_only(world.cfg) as conn:
        report = " | ".join(status.report(conn))
    assert "volume 2 cover not fetched" in report
    assert files(world)[-1] == "Vol.2 Ch.3.cbz", "the volume rewrite went on without the cover"


@budget(load="db", seconds=60)
def test_a_record_that_cannot_be_read_skips_its_series_and_the_run_goes_on(world):
    library(world)
    world.rows("UPDATE published_file SET cover_state = NULL RETURNING id")
    world.rows("DELETE FROM series_naming RETURNING series_id")
    (world.library / SERIES / ".optimizer.json").write_text("{not json", encoding="utf-8")
    override(world, {1: "1-2", 2: "3-"})
    world.run(export)
    assert rewrites(world) == [], "not compared: its covers are unknown"
    assert world.rows("SELECT count(*) FROM series_naming") == [(0,)], "no hash stored: compared again next run"
    with db.read_only(world.cfg) as conn:
        assert "whose cover is not known yet: 1" in " | ".join(status.report(conn))


# --- the pure parts


def row(fid, number, volume, source="override"):
    return naming.Row(key=("published", fid), settled=True, kind=names.MAIN, number=number, season=None,
                      src_volume=None, volume=volume, volume_source=source, marker_title="", title="", date=None,
                      order=fid)  # fmt: skip


@budget(load="pure", seconds=1)
def test_cover_changes_keep_a_held_cover_and_skip_a_taken_volume_and_an_unfit_image():
    rows = [row(1, 1, 1), row(2, 2, 1), row(3, 3, 2), row(4, 4, 2), row(5, 5, 3)]
    states = {1: "none", 2: "volume:1", 3: "none", 4: "none", 5: "unfit:abc"}
    got = rewrite_plan.cover_changes("override", rows, None, {}, {}, states, {"2"}, {"3": "abc"})
    assert got == {}, "volume 1 held by a later file (kept); volume 2 taken by an open chapter; 3 unfit for its image"
    got = rewrite_plan.cover_changes("override", rows, None, {}, {}, states, set(), {"3": "new"})
    assert got == {3: ("insert", "2"), 5: ("insert", "3")}, "the lowest published file; a new image is tried"


@budget(load="pure", seconds=1)
def test_cover_changes_replace_a_moved_files_cover_on_its_new_volumes_first():
    rows = [row(1, 1, 1), row(2, 2, 1), row(3, 3, 2)]
    states = {1: "volume:1", 2: "none", 3: "volume:2"}
    moved = {1: (2, "override"), 3: (3, "override")}  # 1 is now volume 2's first; 3 moves on to volume 3
    got = rewrite_plan.cover_changes("override", rows, None, {}, moved, states, set(), {})
    assert got == {1: ("insert", "2"), 2: ("insert", "1"), 3: ("insert", "3")}


@budget(load="pure", seconds=1)
def test_cover_changes_drop_a_moved_files_cover_where_its_new_volume_has_one():
    rows = [row(1, 1, 2), row(4, 4, 2), row(5, 5, 3)]
    states = {1: "volume:2", 4: "volume:2", 5: "volume:3"}
    moved = {4: (3, "override")}  # 4 moves into volume 3, whose cover 5 holds (kept)
    got = rewrite_plan.cover_changes("override", rows, None, {}, moved, states, set(), {})
    assert got == {4: ("drop", None)}, "a later holder keeps its cover; the moved file drops volume 2's"


@budget(load="pure", seconds=1)
def test_shift_pages_adds_a_front_cover_or_takes_the_dropped_page_out():
    entries = [{"Image": "0", "Type": "FrontCover"}, {"Image": "1"}, {"Image": "2", "DoublePage": "true"}]
    dropped = rewrite_covers.shift_pages(entries, {"dropped_index": 0, "cover": None})
    assert dropped == [{"Image": "0"}, {"Image": "1", "DoublePage": "true"}]
    cover = {"bytes": 10, "width": 5, "height": 7}
    added = rewrite_covers.shift_pages(dropped, {"dropped_index": None, "cover": cover, "inserted_index": 0})
    assert added[0] == {"Image": "0", "Type": "FrontCover", "ImageSize": "10", "ImageWidth": "5", "ImageHeight": "7"}
    assert [e["Image"] for e in added] == ["0", "1", "2"]
    assert added[2]["DoublePage"] == "true"
    assert rewrite_covers.shift_pages([], {"dropped_index": None, "cover": cover, "inserted_index": 0}) == []
    later = rewrite_covers.shift_pages(dropped, {"dropped_index": None, "cover": cover, "inserted_index": 1})
    assert [(e["Image"], e.get("Type")) for e in later] == [("0", None), ("1", "FrontCover"), ("2", None)], (
        "a source cover page sorting first keeps index 0; the inserted cover goes where it sorts"
    )


@budget(load="db", seconds=60)
def test_validate_gives_no_second_cover_to_a_volume_a_published_file_is_queued_to_hold(world, monkeypatch):
    library(world)
    override(world, {1: "1-2", 2: "3-"})
    store_image(world, 2, png(900))
    world.run(export)
    world.run(process)
    monkeypatch.setattr(retention, "put_library", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    world.run(export)  # published (queued as volume 2 with its cover), not finished
    monkeypatch.undo()
    assert world.rows("SELECT comicinfo_volume, pending_cover FROM published_file WHERE id = 3") == [(None, "volume:2")]
    manga = next(i for i, m in world.suwayomi.manga_rows.items() if m["title"] == SERIES)
    world.download(manga, "Chapter 4", data=cbz(pages=4, seed="4"))
    world.run(intake)
    world.run(intake)
    assert world.rows("SELECT stage FROM chapter WHERE kind = 'download' AND status <> 'done'") == [("junk",)]
    taken = world.rows(
        "SELECT count(*) FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id WHERE c.kind = 'download'"
    )
    assert taken == [(0,)], "chapter 4 opens volume 2 but takes no cover"


@budget(load="db", seconds=60)
def test_comicinfo_run_again_after_convert_shifts_the_page_entries_once(world):
    library(world, cover=("Vol.1 Ch.2.cbz",))
    recorded_covers(world, 1, {1: "Vol.1 Ch.2.cbz"})
    world.run(export)
    override(world, {1: "1", 2: "2-"})
    world.run(export)
    world.run(process)
    with closing(db.connect(world.cfg)) as conn:  # comicinfo twice, as after a crash before its outcome
        ctx = world.context(conn, "export")
        chapter = claimed(conn, ctx, conn.execute("SELECT id FROM chapter WHERE drop_cover").fetchone()[0])
        for _ in range(2):
            assert isinstance(rewrite.comicinfo_run(ctx, chapter), workflow.Done)
        staged = config.to_local(world.cfg, chapter["staged_path"])
    with zipfile.ZipFile(staged) as z:
        pages = comicinfo_xml.read_pages(z.read("ComicInfo.xml"))
    assert [p["Image"] for p in pages] == ["0", "1", "2", "3"], "the dropped cover's entry gone once, not twice"


@budget(load="db", seconds=60)
@pytest.mark.parametrize("failure", ["timeout", "5xx"])
def test_mangadex_down_with_a_wanted_cover_spends_no_attempt_and_intake_goes_on(world, monkeypatch, failure):
    library(world)
    world.rows(
        """INSERT INTO volume_cover_wanted (series_id, volume) SELECT id, 2 FROM series WHERE origin = 'kavita'
        RETURNING series_id"""
    )
    error = http.Timeout("slow") if failure == "timeout" else http.HttpError(503, "https://uploads.mangadex.org/x", b"")
    monkeypatch.setattr(cover_images, "volume_cover", lambda *a: (_ for _ in ()).throw(error))
    manga = next(i for i, m in world.suwayomi.manga_rows.items() if m["title"] == SERIES)
    world.download(manga, "Chapter 4", data=cbz(pages=4, seed="4"))
    world.run(intake)
    world.run(intake)
    assert world.rows("SELECT attempts FROM volume_cover_wanted") == [(0,)], "MangaDex's problem: no attempt"
    assert world.rows("SELECT stage FROM chapter WHERE kind = 'download' AND status <> 'done'") == [("junk",)], (
        "intake went on: chapter 4 discovered and validated"
    )


@budget(load="db", seconds=60)
def test_a_failed_cover_refresh_leaves_the_file_waiting_and_a_volume_only_rewrite_asks_none(world, monkeypatch):
    kid = library(world, cover=("Vol.1 Ch.2.cbz",))
    read(world, kid, "Vol.1 Ch.2.cbz", 3)
    recorded_covers(world, kid, {3: "Vol.1 Ch.2.cbz"})  # volume 3's cover in a volume 1 file: dropped
    override(world, {1: "1-2", 2: "3-"})  # and Ch.3 gets volume 2 (no image: volume only)
    world.run(export)
    world.run(process)
    world.run(export)
    monkeypatch.setattr(world.kavita, "refresh_covers", lambda *a: (_ for _ in ()).throw(RuntimeError("busy")))
    scan(world)
    world.run(export)
    monkeypatch.undo()
    assert world.rows("SELECT library_path FROM published_file WHERE carry_cover") == [
        (f"library/{SERIES}/Vol.1 Ch.2.cbz",)
    ], "the refresh is still due; the volume-only rewrite asks none"
    assert world.rows("SELECT count(*) FROM published_file WHERE indexed_since IS NOT NULL") == [(0,)], (
        "Kavita re-read the files: no wait, no charge, no scan asked for them"
    )
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 2, "its progress was restored once"
    read(world, kid, "Vol.1 Ch.2.cbz", 3)  # read on before the next run
    world.rows(f"UPDATE meta SET value = '2000-01-01T00:00:00+00:00' WHERE key = '{export.REQUESTED_AT}' RETURNING key")
    scans = [w for w in world.kavita.writes if w[0] == "scan_library"]
    world.run(export)
    assert refreshes(world) == [("refresh_covers", world.cfg["kavita"]["library_id"], kid)]
    assert world.rows("SELECT count(*) FROM published_file WHERE carry_cover") == [(0,)]
    assert progress(world, kid)["Vol.1 Ch.2.cbz"][0] == 3, "the restore ran once: the page read on is kept"
    assert [w for w in world.kavita.writes if w[0] == "scan_library"] == scans, "no library scan asked for a refresh"
