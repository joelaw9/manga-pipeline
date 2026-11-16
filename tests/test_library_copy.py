"""The junk stage's library-copy check (SPEC "Library copies"): a chapter the series already has published is a
duplicate, a better copy (which takes its file: test_better_copies), or doubtful."""

import io
import json
import random
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import config, db, export, images, intake, library_copy, measures, process, retention, workflow

from tests import pages as fx
from tests.fakes.suwayomi import page as fake_page
from tests.timing import budget
from tests.world import World, cbz

BEST = "Best (EN)"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    w.kavita_series("Solo Leveling")  # bulk Ch.1.cbz: 3 pages of 16 x 24
    w.manga_id = w.manga("Solo Leveling")
    return w


def published_copy(
    world,
    number="5",
    source=BEST,
    pages_in=3,
    width=16,
    strip=4500,
    season=None,
    name=None,
    data=None,
    bpp=None,
    junk=None,
):
    """A pipeline-published file of the series with its stored original measurements."""
    path = world.library / "Solo Leveling" / (name or f"Ch.{number}.cbz")
    path.write_bytes(data or cbz(seed=f"published {number}"))
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, source, state, pages_in,
                median_page_width, strip_length, origin, bpp, junk, cover_state)
            VALUES (%s, 1, %s, %s, 'confirmed', %s, %s, %s, 'pipeline', %s, %s, 'none')""",
            (
                config.to_stored(world.cfg, path),
                json.dumps(["grp", season, "", number, ""]),
                source,
                pages_in,
                width,
                strip,
                bpp,
                junk,
            ),
        )
    return path


def through_junk(world, name="Chapter 5", copies=(), gone=False):
    """A download taken through intake, then the published copies (published_copy keywords) added, then process."""
    _, path = world.download(world.manga_id, name, data=cbz(seed=f"new {name}"))
    world.run(intake)
    for copy in copies:
        made = published_copy(world, **copy)
        if gone:
            made.unlink()
    world.run(process)
    return path


def outcome(world):
    ((stage, status, error),) = world.rows("SELECT stage, status, last_error FROM chapter")
    return stage, status, error or ""


@budget(load="db")
@pytest.mark.parametrize("source", ["Src (EN)", "Unranked (EN)"], ids=["lower-ranked", "higher-ranked"])
def test_an_equal_copy_is_a_duplicate_put_aside_whatever_its_source(world, source):
    path = through_junk(world, copies=[{"source": source}])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), "width, bits per pixel and junk decide; the source's rank does not"
    assert error.startswith("dropped: a duplicate of the published"), (
        "width, bits per pixel and junk decide; the source's rank does not"
    )
    kept = config.abs_path(world.cfg["paths"]["retention"]) / "Src (EN)" / "Solo Leveling" / path.name
    assert not path.exists(), "removed the retention way"
    assert kept.exists(), "removed the retention way"
    assert world.rows("SELECT outcome FROM handled_chapter WHERE outcome = 'duplicate'") == [("duplicate",)]


@budget(load="db")
def test_a_lost_claim_puts_no_duplicate_aside(world, monkeypatch):

    events, real_junk, real_put = [], library_copy._junk, retention.put

    def junk(ctx, *args):
        if not events:  # another run takes the claim while this one compares the copies
            with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
                conn.execute(
                    "UPDATE chapter SET claimed_by = 'process@elsewhere', claim_count = claim_count + 1"
                    " WHERE status = 'claimed'"
                )
            events.append("taken")
        return real_junk(ctx, *args)

    def put(cfg, path):
        ((holder,),) = world.rows("SELECT claimed_by FROM chapter")
        events.append(f"put aside under {holder}'s claim")
        return real_put(cfg, path)

    monkeypatch.setattr(library_copy, "_junk", junk)
    monkeypatch.setattr(retention, "put", put)
    through_junk(world, copies=[{}])
    assert events == ["taken", f"put aside under {workflow.owner('process')}'s claim"], (
        "no duplicate is put aside under a lost claim; the chapter claimed again puts it aside"
    )
    assert outcome(world)[:2] == ("junk", "done")


@budget(load="db")
@pytest.mark.parametrize(
    ("published", "why"),
    [
        ({"width": 15}, "wider"),  # 16 >= 1.05 x 15
        ({"bpp": 0.001}, "more bits per pixel"),
        ({"junk": 1}, "less junk"),  # 1 page dropped from the published file's download, none from this one
    ],
)
def test_a_better_copy_takes_its_file_and_goes_on(world, published, why):
    through_junk(world, copies=[published])
    ((stage, status, file_id),) = world.rows("SELECT stage, status, published_file_id FROM chapter")
    assert (stage, status, file_id) == ("comicinfo", "ready", 2), f"{why}: it names its file and goes on"


@budget(load="db")
@pytest.mark.parametrize(
    ("published", "why"),
    [
        ({"width": 17}, "narrower than 0.95 x"),
        ({"width": 15, "bpp": 1e9}, "wider, but fewer bits per pixel"),
    ],
)
def test_a_copy_worse_on_any_measure_is_a_duplicate(world, published, why):
    through_junk(world, copies=[published])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), why
    assert "a duplicate" in error, why


@budget(load="db")
def test_a_copy_of_another_length_is_not_the_same_chapter(world):
    through_junk(world, copies=[{"pages_in": 10}])
    _stage, status, error = outcome(world)
    assert status == "needs_human"
    assert "not the same chapter" in error
    assert "pages_in 3 against 10" in error


@budget(load="db")
def test_2_published_files_of_the_chapter_need_a_human(world):
    through_junk(world, copies=[{}, {"name": "Ch.5 - again.cbz"}])
    assert outcome(world)[1] == "needs_human"
    assert "2 published files" in outcome(world)[2]


@budget(load="db")
def test_another_season_with_the_same_number_is_no_copy(world):
    through_junk(world, copies=[{"season": "2"}])
    assert outcome(world)[:2] == ("comicinfo", "ready"), "S2 Chapter 5 is not Chapter 5"


@budget(load="db")
def test_an_equal_copy_of_a_bulk_file_leaves_it(world):
    through_junk(world, "Chapter 1")  # the bulk Ch.1.cbz: 3 pages of the same width, no stored measurements
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), (
        "measured from the library file; a bulk file is not ranked and its bits per pixel are not compared"
    )
    assert "a duplicate" in error, (
        "measured from the library file; a bulk file is not ranked and its bits per pixel are not compared"
    )


@budget(load="db")
def test_a_published_file_gone_from_the_library_is_no_copy(world):
    through_junk(world, copies=[{}], gone=True)
    assert outcome(world)[:2] == ("comicinfo", "ready")


@budget(load="db")
def test_a_pipeline_copy_with_no_stored_original_is_measured_from_the_library_file(world):
    path = through_junk(world, copies=[{"pages_in": None, "width": None, "strip": None}])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), (
        "3 pages of 16 px read from the file's headers: an equal copy from a lower-ranked source"
    )
    assert "a duplicate" in error, (
        "3 pages of 16 px read from the file's headers: an equal copy from a lower-ranked source"
    )
    assert not path.exists()


@budget(load="db", seconds=4)
def test_a_library_file_whose_pages_are_bigger_than_a_header_read_is_measured(world):
    size = (512, 768)
    page = io.BytesIO()
    noise = Image.frombytes("RGB", size, random.Random(1).randbytes(size[0] * size[1] * 3))
    noise.save(page, "AVIF", quality=100)
    assert len(page.getvalue()) > images.HEADER_BYTES, "the page must be bigger than a header read to test the rule"
    library_file = io.BytesIO()
    with zipfile.ZipFile(library_file, "w") as z:
        for n in (1, 2, 3):
            z.writestr(f"{n:03}.avif", page.getvalue())
    copy = {"pages_in": None, "width": None, "strip": None, "data": library_file.getvalue()}
    through_junk(world, copies=[copy])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), (
        "the bulk converter's big AVIF pages are sized from their whole bytes: 3 pages, wider than the download's"
    )
    assert "a duplicate" in error, (
        "the bulk converter's big AVIF pages are sized from their whole bytes: 3 pages, wider than the download's"
    )


# ---------------------------------------------------------------- volumes in a `filename` series


@pytest.fixture
def by_filename(tmp_path):
    """A series whose volume basis is `filename`, holding the bulk converter's Vol.1 Ch.3."""
    w = World(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    w.kavita_series("Solo Leveling", bulk=(("Vol.1 Ch.3.cbz", "Grp_Vol.1 Chapter 3", "3", 1),), basis="filename")
    w.manga_id = w.manga("Solo Leveling")
    return w


@budget(load="db", seconds=3)
def test_another_file_name_volume_is_another_chapter_and_is_published(by_filename):
    """Vol.2 Ch.3 beside the published Vol.1 Ch.3 is no copy of it; it is also the chapter that takes the baseline."""
    by_filename.download(by_filename.manga_id, "Vol.2 Chapter 3")
    for part in (intake, process, export):
        by_filename.run(part)
    assert (by_filename.library / "Solo Leveling" / "Vol.2 Ch.3.cbz").is_file(), "published beside Vol.1 Ch.3"
    assert by_filename.rows("SELECT comicinfo_volume FROM published_file WHERE origin = 'pipeline'") == [("2",)]


@budget(load="db")
@pytest.mark.parametrize("name", ["Vol.01 Chapter 3", "Chapter 3"])
def test_the_same_volume_or_none_is_the_same_chapter(by_filename, name):
    by_filename.download(by_filename.manga_id, name)
    by_filename.run(intake)
    by_filename.run(process)
    _stage, status, error = outcome(by_filename)
    assert status == "done", "judged against Vol.1 Ch.3"
    assert "a duplicate of the published" in error, "judged against Vol.1 Ch.3"


def pages_cbz(path, seed):
    """3 distinct manga pages (other seed = other pages) as CBZ bytes, written to path too."""
    members = {f"{i:03}.jpg": fx.jpeg(fx.manga_page(size=(300, 420), seed=seed + i)) for i in range(1, 4)}
    return fx.cbz(path, members).read_bytes()


@budget(load="db", seconds=5)
def test_a_download_under_another_volume_with_that_chapters_pages_is_mislabelled(by_filename, tmp_path):
    world = by_filename
    world.download(world.manga_id, "Chapter 10")
    world.run(intake)
    world.run(process)  # takes the baseline: the series' volume basis is known at the next validate
    phashes = {}
    for seed in (200, 300, 400):
        path = tmp_path / f"p{seed}.cbz"
        pages_cbz(path, seed)
        phashes[seed] = json.dumps(measures.summary(images.measure_pages(path))["page_phashes"])
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE published_file SET page_phashes = %s", (phashes[200],))  # the bulk Vol.1 Ch.3
        for number, seed in (("1", 300), ("2", 400)):
            conn.execute(
                """INSERT INTO published_file (library_path, series_id, identity, state, page_phashes, origin)
                VALUES (%s, 1, %s, 'confirmed', %s, 'pipeline')""",
                (f"/lib/Ch.{number}.cbz", json.dumps(["grp", None, "", number, ""]), phashes[seed]),
            )
    world.download(world.manga_id, "Vol.2 Chapter 3", data=pages_cbz(tmp_path / "d.cbz", 200))
    world.run(intake)
    (status, error) = world.rows("SELECT status, last_error FROM chapter ORDER BY id")[1]
    assert status == "needs_human", "Vol.2 Ch.3 holding Vol.1 Ch.3's pages"
    assert error.startswith("mislabelled"), "Vol.2 Ch.3 holding Vol.1 Ch.3's pages"


@budget(load="db")
def test_a_volume_that_is_not_whole_needs_a_human_at_validate(by_filename):
    by_filename.download(by_filename.manga_id, "Vol.2.5 Chapter 3")
    by_filename.run(intake)
    stage, status, error = outcome(by_filename)
    assert (stage, status) == ("validate", "needs_human"), "a person, not an error"
    assert "not a whole number" in error, "a person, not an error"


@budget(load="db")
def test_a_duplicate_dropped_at_junk_is_no_chapter_status_lists(world):
    """Validate notes the download's repeated page; dropped as a duplicate, its notes go with it."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n, page in enumerate((1, 2, 1), 1):
            z.writestr(f"{n:03}.png", fake_page(page, "new"))
    world.download(world.manga_id, "Chapter 5", data=data.getvalue())
    world.run(intake)
    assert world.rows("SELECT notes FROM chapter") != [(None,)], "validate notes the repeated page"
    published_copy(world)
    world.run(process)
    assert outcome(world)[2].startswith("dropped: a duplicate"), outcome(world)
    assert world.rows("SELECT notes FROM chapter") == [(None,)], "a dropped chapter carries no notes"


@budget(load="db")
def test_a_bulk_files_junk_comes_from_its_record_and_less_junk_is_better(world):
    record = world.library / "Solo Leveling" / ".optimizer.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["chapters"][0]["edits"]["drops"] = [3]  # the bulk converter dropped a credit page from Ch.1's download
    record.write_text(json.dumps(data), encoding="utf-8")
    through_junk(world, "Chapter 1")  # the same width; this download loses no page to junk
    ((stage, status, file_id),) = world.rows("SELECT stage, status, published_file_id FROM chapter")
    assert (stage, status, file_id) == ("comicinfo", "ready", 1), "less junk than the bulk file's own download"
