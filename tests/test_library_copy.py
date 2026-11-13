"""The junk stage's library-copy check (SPEC "Library copies"): a chapter the series already has published is a
duplicate, a better copy, or doubtful."""

import io
import json
import random
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import config, db, images, intake, process

from tests.timing import budget
from tests.world import World, cbz

BEST = "Best (EN)"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    w.kavita_series("Solo Leveling")  # bulk Ch.1.cbz: 3 pages of 16 x 24
    w.manga_id = w.manga("Solo Leveling")
    return w


def published_copy(world, number="5", source=BEST, pages_in=3, width=16, strip=4500, season=None, name=None, data=None):
    """A pipeline-published file of the series with its stored original measurements."""
    path = world.library / "Solo Leveling" / (name or f"Ch.{number}.cbz")
    path.write_bytes(data or cbz(seed=f"published {number}"))
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, source, state, pages_in,
                median_page_width, strip_length, origin)
            VALUES (?, 1, ?, ?, 'confirmed', ?, ?, ?, 'pipeline')""",
            (str(path), json.dumps(["grp", season, "", number, ""]), source, pages_in, width, strip),
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
def test_an_equal_copy_from_a_lower_ranked_source_is_a_duplicate_put_aside(world):
    path = through_junk(world, copies=[{}])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done") and error.startswith("dropped: a duplicate of the published")
    kept = config.abs_path(world.cfg["paths"]["retention"]) / "Src (EN)" / "Solo Leveling" / path.name
    assert not path.exists() and kept.exists(), "removed the retention way"
    assert world.rows("SELECT outcome FROM handled_chapter WHERE outcome = 'duplicate'") == [("duplicate",)]


@budget(load="db")
@pytest.mark.parametrize(
    "published, why",
    [
        ({"width": 15}, "wider"),  # 16 >= 1.05 x 15
        ({"source": "Unranked (EN)"}, "same width, higher-ranked source"),
    ],
)
def test_a_better_copy_needs_a_human_until_replacing_is_built(world, published, why):
    through_junk(world, copies=[published])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "needs_human") and "a better copy than the published" in error, why
    assert "Phase 7e" in error


@budget(load="db")
def test_a_copy_of_another_length_is_not_the_same_chapter(world):
    through_junk(world, copies=[{"pages_in": 10}])
    stage, status, error = outcome(world)
    assert status == "needs_human" and "not the same chapter" in error and "pages_in 3 against 10" in error


@budget(load="db")
def test_2_published_files_of_the_chapter_need_a_human(world):
    through_junk(world, copies=[{}, {"name": "Ch.5 - again.cbz"}])
    assert outcome(world)[1] == "needs_human" and "2 published files" in outcome(world)[2]


@budget(load="db")
def test_another_season_with_the_same_number_is_no_copy(world):
    through_junk(world, copies=[{"season": "2"}])
    assert outcome(world)[:2] == ("comicinfo", "ready"), "S2 Chapter 5 is not Chapter 5"


@budget(load="db")
def test_a_bulk_copy_is_measured_from_the_library_file_and_ranks_last(world):
    through_junk(world, "Chapter 1")  # the bulk Ch.1.cbz: 3 pages of the same width, no stored measurements
    stage, status, error = outcome(world)
    assert status == "needs_human" and "a better copy" in error and "against bulk" in error


@budget(load="db")
def test_a_published_file_gone_from_the_library_is_no_copy(world):
    through_junk(world, copies=[{}], gone=True)
    assert outcome(world)[:2] == ("comicinfo", "ready")


@budget(load="db")
def test_a_pipeline_copy_with_no_stored_original_is_measured_from_the_library_file(world):
    path = through_junk(world, copies=[{"pages_in": None, "width": None, "strip": None}])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done") and "a duplicate" in error, (
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
    assert (stage, status) == ("junk", "done") and "a duplicate" in error, (
        "the bulk converter's big AVIF pages are sized from their whole bytes: 3 pages, wider than the download's"
    )


@budget(load="db")
def test_a_source_recorded_by_its_id_ranks_with_the_unranked_sources(world):
    through_junk(world, copies=[{"source": "2327480808438768017"}])
    stage, status, error = outcome(world)
    assert status == "needs_human" and "a better copy" in error, "Src (EN) is ranked; a bare source id is not"
