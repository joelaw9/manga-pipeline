"""The junk stage's library-copy check (SPEC "Library copies").

A chapter the series already has published is a duplicate, a better copy (which takes its file: test_better_copies),
or doubtful.
"""

import argparse
import io
import json
import random
import zipfile
from contextlib import closing
from pathlib import PurePosixPath

import pytest
from PIL import Image

from manga_pipeline import (
    cli,
    comicinfo,
    config,
    db,
    export,
    images,
    intake,
    library_copy,
    measures,
    process,
    retention,
    workflow,
)
from manga_pipeline.junk import review
from manga_pipeline.stages import junk

from tests import pages as fx
from tests import test_rewrites as rewrites
from tests.fakes.suwayomi import page as fake_page
from tests.timing import budget
from tests.world import World, cbz, decide_junk


class CopyWorld(World):
    """A World with the series' manga on its source."""

    manga_id: int


BEST = "Best (EN)"


@pytest.fixture
def world(tmp_path):
    w = CopyWorld(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    w.kavita_series("Solo Leveling")  # bulk Ch.1.cbz: 3 pages of 16 x 24
    w.manga_id = w.manga("Solo Leveling")
    return w


def published_copy(  # noqa: PLR0913  # 1 published_file row's varied columns
    world,
    source=BEST,
    pages_in=3,
    width=16,
    *,
    strip=4500,
    season=None,
    name=None,
    data=None,
    bpp=None,
    junk=None,
):
    """Add a pipeline-published file of the series with its stored original measurements."""
    path = world.library / "Solo Leveling" / (name or "Ch.5.cbz")
    path.write_bytes(data or cbz(seed="published 5"))
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, source, state, pages_in,
                median_page_width, strip_length, origin, bpp, junk, cover_state)
            VALUES (%s, 1, %s, %s, 'confirmed', %s, %s, %s, 'pipeline', %s, %s, 'none')""",
            (
                config.to_stored(world.cfg, path),
                json.dumps(["grp", season, "", "5", ""]),
                source,
                pages_in,
                width,
                strip,
                bpp,
                junk,
            ),
        )
    return path


def through_junk(world, name="Chapter 5", copies=(), *, gone=False):
    """Take a download through intake, then add the published copies (published_copy keywords), then run process."""
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
def test_a_chapter_without_a_content_digest_fails_before_any_check_acts(world):
    _, path = world.download(world.manga_id, "Chapter 5", data=cbz(seed="new Chapter 5"))
    world.run(intake)
    published_copy(world, width=17)  # narrower than 0.95 x: the library-copy check would drop it and put it aside
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET content_digest = NULL")
    world.run(process)
    ((stage, status, attempts, error),) = world.rows("SELECT stage, status, attempts, last_error FROM chapter")
    assert (stage, status, attempts) == ("junk", "ready", 1), "no digest: 1 failed attempt, before any check"
    assert "no content digest" in error, "no digest: 1 failed attempt, before any check"
    assert path.exists(), "nothing is put aside for a chapter the put-aside check cannot judge"
    assert world.rows("SELECT outcome FROM handled_chapter WHERE outcome = 'duplicate'") == [], "none is recorded"


@budget(load="db")
def test_a_lost_claim_puts_no_duplicate_aside(world, monkeypatch):
    events, real_junk, real_put = [], library_copy.published_junk, retention.put

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

    monkeypatch.setattr(library_copy, "published_junk", junk)
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
def test_a_higher_rated_copy_already_scanned_takes_its_file_in_1_junk_call(world, monkeypatch):
    path = world.download(world.manga_id, "Chapter 5 [R18]", data=cbz(seed="new Chapter 5"))[1]
    world.run(intake)

    def killed(*_args):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(junk, "decide", killed)
        with pytest.raises(KeyboardInterrupt):  # process killed once the scan is stored
            world.run(process)
    assert world.rows("SELECT count(*) FROM junk_scan") == [(1,)], "set up: the scan is stored"
    published_copy(world)  # untagged, the same width: the uncensored copy is better by its rating alone
    world.run(process)
    ((stage, status, file_id),) = world.rows("SELECT stage, status, published_file_id FROM chapter")
    assert (stage, status, file_id) == ("comicinfo", "ready", 2), "it names its file and goes on"
    assert path.exists(), "not put aside: no duplicate"
    assert world.rows("SELECT outcome FROM handled_chapter WHERE outcome = 'duplicate'") == [], "no duplicate"


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
    """Provide a series whose volume basis is `filename`, holding the bulk converter's Vol.1 Ch.3."""
    w = CopyWorld(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
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


# --- 2 open copies of 1 chapter: the later waits at junk (open_copy) while the earlier is ahead of it


def other_manga(world):
    """Add the series' manga on a 2nd source, Other (its download folder sorts before Src's); return its id."""
    world.source = world.suwayomi.add_source("Other")
    return world.manga("Solo Leveling")


def arrival(world):
    """Return the chapters' download file names in discovery order (discovery time, then id)."""
    rows = world.rows("SELECT download_path FROM chapter WHERE kind = 'download' ORDER BY discovered_at, id")
    return [PurePosixPath(path).name for (path,) in rows]


def state(world, download):
    """Return (stage, status, waiting_on, last_error or "") of the chapter of `download` (its path)."""
    ((stage, status, on, error),) = world.rows(
        "SELECT stage, status, waiting_on, last_error FROM chapter WHERE download_path = %s",
        config.to_stored(world.cfg, download),
    )
    return stage, status, on, error or ""


def chapter_id(world, download):
    ((found,),) = world.rows("SELECT id FROM chapter WHERE download_path = %s", config.to_stored(world.cfg, download))
    return found


def library_files(world, series="Solo Leveling"):
    return sorted(p.name for p in (world.library / series).glob("*.cbz"))


def published_from(world, name):
    """Return the source name of the series' library file `name`: the download stem the pipeline published it from."""
    path = config.to_stored(world.cfg, world.library / "Solo Leveling" / name)
    ((source,),) = world.rows("SELECT source_name FROM published_file WHERE library_path = %s", path)
    return source


def confirm(world):
    """Let Kavita run the requested scan, then run export: a published file waiting on it is confirmed."""
    world.kavita.run_scans()
    world.run(export)


@budget(load="db", seconds=10)
def test_2_open_copies_of_a_chapter_publish_1_file_and_the_later_is_dropped_as_a_duplicate(world):
    """2 sources' chapter 5, same measures, other titles, in 1 intake: 1 file; the later copy a duplicate of it."""
    _, earlier = world.download(other_manga(world), "Chapter 5 - Arise", "Alpha", data=cbz(seed="alpha 5"))
    _, later = world.download(world.manga_id, "Chapter 5 - Rise", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    assert arrival(world) == [earlier.name, later.name], "set up: Other's folder is listed first"
    for part in (process, export):
        world.run(part)
    confirm(world)
    world.run(process)
    assert library_files(world) == ["Ch.1.cbz", "Ch.5 - Arise.cbz"], "2 open copies of chapter 5 publish 1 file"
    stage, status, _, error = state(world, later)
    assert (stage, status) == ("junk", "done"), "the later copy is judged against the earlier's confirmed file"
    assert error.startswith("dropped: a duplicate of the published"), (
        "the later copy is judged against the earlier's confirmed file"
    )


@budget(load="db", seconds=10)
def test_2_open_copies_under_1_file_name_publish_with_no_name_clash(world):
    """2 sources' chapter 5 under the same name: no chapter needs_human "already in the library"; 1 file."""
    _, earlier = world.download(other_manga(world), "Chapter 5", data=cbz(seed="other 5"))
    _, later = world.download(world.manga_id, "Chapter 5", data=cbz(seed="src 5"))
    world.run(intake)
    assert arrival(world) == [earlier.name, later.name], "set up: Other's folder is listed first"
    for part in (process, export):
        world.run(part)
    confirm(world)
    world.run(process)
    assert world.rows("SELECT count(*) FROM chapter WHERE status = 'needs_human'") == [(0,)], (
        "the later copy never reaches publish beside the earlier: no name clash"
    )
    assert library_files(world) == ["Ch.1.cbz", "Ch.5.cbz"], "1 file of chapter 5"
    assert state(world, later)[3].startswith("dropped: a duplicate of the published"), "the later copy is a duplicate"


@budget(load="db", seconds=10)
def test_the_later_copy_waits_until_the_earlier_is_confirmed_then_is_judged(world):
    """The later copy waits open_copy while the earlier converts, publishes and waits for Kavita; judged once done."""
    _, earlier = world.download(other_manga(world), "Chapter 5 - Arise", "Alpha", data=cbz(seed="alpha 5"))
    _, later = world.download(world.manga_id, "Chapter 5 - Rise", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    world.run(process)
    assert state(world, earlier)[:2] == ("comicinfo", "ready"), "set up: the earlier copy passed junk"
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), "an earlier copy past junk is ahead of it"
    world.run(export)
    world.run(process)
    assert state(world, earlier)[:3] == ("confirm", "waiting", "kavita_scan"), "set up: published, not confirmed"
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), "the earlier copy is not done: still waiting"
    confirm(world)
    world.run(process)
    stage, status, _, error = state(world, later)
    assert (stage, status) == ("junk", "done"), "the next process judges it against the confirmed file"
    assert error.startswith("dropped: a duplicate of the published"), (
        "the next process judges it against the confirmed file"
    )


@budget(load="db", seconds=10)
def test_the_later_copy_waits_on_an_earlier_held_for_a_person_until_it_is_dropped(world):
    """An earlier copy needs_human at junk (an unsure candidate) holds the later; chapter drop lets it publish."""
    _, earlier = world.download(world.manga_id, "Chapter 5", "Alpha", data=cbz(seed="alpha 5", credit=True))
    world.run(intake)
    world.run(process)
    assert state(world, earlier)[:3] == ("junk", "waiting", "junk_decision"), "set up: its credit page is a candidate"
    with closing(db.connect(world.cfg)) as conn:
        ((candidate,),) = conn.execute("SELECT id FROM junk_candidate").fetchall()
        review.decide(conn, candidate, "unsure", None, workflow.now(conn))
    world.run(process)
    assert state(world, earlier)[:2] == ("junk", "needs_human"), "set up: unsure, a person decides"
    _, later = world.download(world.manga_id, "Chapter 5", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), "an earlier copy at junk is ahead of it"
    cli.chapter_drop(world.cfg, argparse.Namespace(chapter=chapter_id(world, earlier)))
    world.run(process)
    world.run(export)
    assert state(world, later)[:3] == ("confirm", "waiting", "kavita_scan"), "judged once the earlier is dropped"
    assert library_files(world) == ["Ch.1.cbz", "Ch.5.cbz"], "the later copy is published"


@budget(load="db", seconds=10)
def test_open_downloads_of_different_chapters_never_wait_on_each_other(world):
    """Chapter 5 and chapter 6 at junk together: their chapter keys differ, so neither waits."""
    _, five = world.download(other_manga(world), "Chapter 5", data=cbz(seed="other 5"))
    _, six = world.download(world.manga_id, "Chapter 6", data=cbz(seed="src 6"))
    world.run(intake)
    world.run(process)
    assert [state(world, five)[:3], state(world, six)[:3]] == [("comicinfo", "ready", None)] * 2, (
        "no open copy of their chapter"
    )


@budget(load="db", seconds=15)
def test_a_later_copy_of_a_higher_rating_waits_then_takes_the_earlier_copys_file(world):
    """The uncensored later copy waits; once the earlier is confirmed it takes that file: 1 file, holding its pages."""
    _, earlier = world.download(other_manga(world), "Chapter 5", "Alpha", data=cbz(seed="alpha 5"))
    _, later = world.download(world.manga_id, "Chapter 5 [R18]", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    assert arrival(world) == [earlier.name, later.name], "set up: Other's folder is listed first"
    for part in (process, export):
        world.run(part)
    confirm(world)
    for part in (process, export):
        world.run(part)
    assert library_files(world) == ["Ch.1.cbz", "Ch.5.cbz"], "1 file of chapter 5"
    assert published_from(world, "Ch.5.cbz") == later.stem, (
        "a higher edition rating is a better copy: it replaces the earlier copy's file"
    )


@budget(load="db", seconds=15)
def test_a_copy_being_downloaded_again_at_validate_holds_no_later_copy(world):
    """The earlier copy waits on suwayomi_download at validate: the later publishes; the replacement is judged after."""
    _, earlier = world.download(other_manga(world), "Chapter 5", "Alpha", data=b"PK\x03\x04 truncated download")
    _, later = world.download(world.manga_id, "Chapter 5", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    assert state(world, earlier)[:3] == ("validate", "waiting", "suwayomi_download"), "set up: a bad download"
    world.run(process)
    assert state(world, later)[:3] == ("comicinfo", "ready", None), "a copy at validate is never waited on"
    world.run(export)
    confirm(world)
    world.suwayomi.finish_downloads(cbz(seed="alpha 5 again"))
    world.run(intake)
    assert state(world, earlier)[:2] == ("junk", "ready"), "set up: the replacement passed validate"
    world.run(process)
    assert library_files(world) == ["Ch.1.cbz", "Ch.5.cbz"], "1 file of chapter 5"
    stage, status, _, error = state(world, earlier)
    assert (stage, status) == ("junk", "done"), "the replacement is judged against the confirmed file"
    assert error.startswith("dropped: a duplicate of the published"), (
        "the replacement is judged against the confirmed file"
    )


@budget(load="db", seconds=15)
def test_an_earlier_copy_reaching_junk_after_the_later_passed_it_waits(world, monkeypatch):
    """The earlier copy fails once at discover; it reaches junk while the later is past junk, unconfirmed: it waits."""
    _, earlier = world.download(other_manga(world), "Chapter 5 - Arise", "Alpha", data=cbz(seed="alpha 5"))
    _, later = world.download(world.manga_id, "Chapter 5 - Rise", "Beta", data=cbz(seed="beta 5"))
    real = comicinfo.download_number

    def unreadable_once(path):
        if path.name == earlier.name:
            msg = "the share hiccups"
            raise OSError(msg)
        return real(path)

    with monkeypatch.context() as m:
        m.setattr(comicinfo, "download_number", unreadable_once)
        world.run(intake)
    assert state(world, earlier)[:2] == ("discover", "ready"), "set up: 1 failed attempt at discover"
    assert arrival(world) == [earlier.name, later.name], "set up: Other's folder is listed first"
    world.run(process)
    assert state(world, later)[:2] == ("comicinfo", "ready"), "set up: the later copy passed junk"
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET retry_after = NULL")  # the retry pause is over
    world.run(intake)
    world.run(process)
    assert state(world, earlier)[:3] == ("junk", "waiting", "open_copy"), "a copy past junk is ahead of any copy"
    world.run(export)
    confirm(world)
    world.run(process)
    world.run(export)
    assert library_files(world) == ["Ch.1.cbz", "Ch.5 - Rise.cbz"], "1 file of chapter 5 is published"


@budget(load="db", seconds=30)
def test_an_open_rewrite_of_the_chapters_file_holds_no_download(tmp_path):
    """A rewrite chapter of Ch.3, needs_human at publish, is no download: a new copy of chapter 3 is judged as ever."""
    world = World(tmp_path)
    rewrites.library(world)
    rewrites.override(world, {2: "2-"})
    (world.library / rewrites.SERIES / "Vol.2 Ch.3.cbz").write_bytes(b"someone else's")
    world.run(export)
    assert world.rows(
        "SELECT c.status FROM chapter c JOIN published_file p ON p.id = c.published_file_id"
        " WHERE c.kind = 'rewrite' AND c.status <> 'done' AND p.library_path LIKE '%/Ch.3.cbz'"
    ) == [("needs_human",)], "set up: Ch.3's rewrite is held, its target taken"
    manga = next(i for i, m in world.suwayomi.manga_rows.items() if m["title"] == rewrites.SERIES)
    _, download = world.download(manga, "Chapter 3", "Grp", data=cbz(pages=4, seed="3 again"))
    world.run(intake)
    world.run(process)
    stage, status, on, error = state(world, download)
    assert (stage, status, on) == ("junk", "done", None), "an equal copy of the published Ch.3: no open_copy wait"
    assert error.startswith("dropped: a duplicate of the published"), "an equal copy of the published Ch.3"


@budget(load="db", seconds=10)
def test_2_open_copies_told_apart_by_their_volumes_are_both_published(by_filename):
    """Vol.1 Chapter 1 and Vol.2 Chapter 1 in a series whose volumes come from file names: 2 chapters, both publish."""
    world = by_filename
    world.download(world.manga_id, "Vol.1 Chapter 1", data=cbz(seed="vol 1"))
    world.download(world.manga_id, "Vol.2 Chapter 1", data=cbz(seed="vol 2"))
    world.run(intake)
    for part in (process, export):
        world.run(part)
    confirm(world)
    for part in (process, export):
        world.run(part)
    assert library_files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.3.cbz", "Vol.2 Ch.1.cbz"], (
        "2 volumes are 2 chapters: the later copy is no copy of the earlier's file"
    )


def better_copy_held_at_junk(world, earlier_name, later_name):
    """Bring 2 copies of chapter 1 to junk; return (earlier path, later path).

    The earlier copy is a bad download, held at validate; the later, rated above the bulk Ch.1.cbz, takes that file at
    junk and waits there on its credit page's junk decision; then the earlier's replacement arrives.
    """
    _, earlier = world.download(other_manga(world), earlier_name, "Alpha", data=b"PK\x03\x04 truncated download")
    # 3 pages, as the bulk file's
    _, later = world.download(world.manga_id, later_name, "Beta", data=cbz(pages=2, seed="beta 1", credit=True))
    world.run(intake)
    assert state(world, earlier)[:3] == ("validate", "waiting", "suwayomi_download"), "set up: a bad download"
    assert arrival(world) == [earlier.name, later.name], "set up: the earlier copy is discovered first"
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "junk_decision"), "set up: its credit page is a candidate"
    assert world.rows("SELECT count(*) FROM chapter WHERE published_file_id = 1") == [(1,)], "set up: it took Ch.1"
    world.suwayomi.finish_downloads(cbz(seed="alpha 1 again"))
    world.run(intake)
    assert state(world, earlier)[:2] == ("junk", "ready"), "set up: the replacement passed validate"
    return earlier, later


@budget(load="db", seconds=20)
def test_a_copy_that_took_its_file_is_not_held_by_an_earlier_copy_reaching_junk(world):
    """The later [R18] copy took Ch.1 at junk before the earlier got there: it publishes, the earlier is a duplicate."""
    earlier, later = better_copy_held_at_junk(world, "Chapter 1", "Chapter 1 [R18]")
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "junk_decision"), "a copy naming its file is not made to wait"
    decide_junk(world.cfg)
    for part in (process, export, process):
        world.run(part)
    assert state(world, later)[:2] == ("publish", "done"), "the better copy is published into Ch.1.cbz"
    stage, status, _, error = state(world, earlier)
    assert (stage, status) == ("junk", "done"), "the earlier copy, no better, is a duplicate"
    assert error.startswith("dropped: a duplicate of the published"), "the earlier copy, no better, is a duplicate"
    assert library_files(world) == ["Ch.1.cbz"], "1 file of chapter 1"
    assert published_from(world, "Ch.1.cbz") == later.stem, "the file holds the later copy's pages"


@budget(load="db", seconds=30)
def test_an_earlier_copy_better_than_a_busy_file_is_held_then_takes_it_on_retry(tmp_path):
    """As above, the earlier [R18] copy above the file: held "busy" while the later's rewrite is pending, then takes it.

    The bulk Ch.1 censored, the later copy untagged: a retry after the rewrite finishes lets the earlier take the file.
    """
    world = CopyWorld(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    world.kavita_series("Solo Leveling", bulk=(("Ch.1.cbz", "Grp_Chapter 1 [R15]", "1"),))
    world.manga_id = world.manga("Solo Leveling")
    earlier, later = better_copy_held_at_junk(world, "Chapter 1 [R18]", "Chapter 1")
    world.run(process)
    decide_junk(world.cfg)
    for part in (process, export, process):
        world.run(part)
    assert state(world, later)[:2] == ("publish", "done"), "set up: the later copy is published into Ch.1.cbz"
    stage, status, _, error = state(world, earlier)
    assert (stage, status) == ("junk", "needs_human"), "a better copy of a busy file is held"
    assert "busy" in error, "a better copy of a busy file is held"
    rewrites.scan(world)
    world.run(export)
    with closing(db.connect(world.cfg)) as conn:
        workflow.retry_chapter(conn, chapter_id(world, earlier))
    for part in (process, export):
        world.run(part)
    assert library_files(world) == ["Ch.1.cbz"], "1 file of chapter 1"
    assert published_from(world, "Ch.1.cbz") == earlier.stem, (
        "retried once the rewrite finished, the earlier copy takes the file"
    )


# --- the rule alone (library_copy.decide): what a source review's sample is judged by


def our_copy(
    width: int = 940, bpp: float = 1.0, junk: float | None = None, rating: int = 1
) -> library_copy.Copy[float]:
    """Return our copy: 10 pages, strip length 20000, with these measures (its bits per pixel always measured)."""
    return library_copy.Copy(library_copy.Measures(10, width, 20000, bpp), rating, junk)


def their_copy(
    width: int = 940, bpp: float | None = 1.0, junk: float | None = None, rating: int = 1
) -> library_copy.Copy[float | None]:
    """Return a published file's original: 10 pages, strip length 20000, with these measures."""
    return library_copy.Copy(library_copy.Measures(10, width, 20000, bpp), rating, junk)


def longer(pages: int, length: int) -> library_copy.Copy[float]:
    """Return our copy of the default measures with another page count and strip length."""
    return library_copy.Copy(library_copy.Measures(pages, 940, length, 1.0), 1, None)


@budget(load="pure")
@pytest.mark.parametrize(
    ("ours", "theirs", "want"),
    [
        (longer(13, 20000), their_copy(), library_copy.NotSameChapter("pages_in 13 against 10")),
        (our_copy(rating=2, width=400), their_copy(), library_copy.Better()),
        (our_copy(rating=0, width=2000), their_copy(), library_copy.Duplicate("a lower edition rating")),
        (our_copy(width=800), their_copy(), library_copy.Duplicate("median page width 800 against 940")),
        (our_copy(bpp=0.5), their_copy(), library_copy.Duplicate("bits per pixel 0.50 against 1.00")),
        (our_copy(width=1000), their_copy(), None),
        (our_copy(width=1000, junk=0), their_copy(junk=0), library_copy.Better()),
        (our_copy(bpp=1.2, junk=0), their_copy(junk=0), library_copy.Better()),
        (our_copy(junk=0), their_copy(junk=1), library_copy.Better()),
        (our_copy(junk=0), their_copy(junk=0), library_copy.Duplicate("no better in width, bits per pixel or junk")),
        (our_copy(width=1000, junk=2), their_copy(junk=1), library_copy.Duplicate("more junk removed: 2 against 1")),
        (our_copy(bpp=0.1, width=1000, junk=0), their_copy(bpp=None, junk=None), library_copy.Better()),
    ],
    ids=[
        "not-the-same-chapter",
        "higher-rating",
        "lower-rating",
        "narrower",
        "recompressed",
        "junk-not-known-yet",
        "wider",
        "denser",
        "less-junk",
        "no-better",
        "more-junk",
        "bulk-file-no-bpp-no-junk",
    ],
)
def test_the_better_copy_rule_on_2_copies(ours, theirs, want):
    assert library_copy.decide(ours, theirs, "pages") == want, "SPEC 'Library copies': the better-copy rule"


@budget(load="pure")
def test_strip_junk_differs_only_beyond_500_rows():
    assert library_copy.decide(our_copy(width=1000, junk=600), their_copy(junk=200), "strip") == library_copy.Better()
    assert library_copy.decide(our_copy(width=1000, junk=800), their_copy(junk=200), "strip") == library_copy.Duplicate(
        "more junk removed: 800 against 200"
    )
    assert library_copy.decide(longer(10, 26000), their_copy(), "strip") == library_copy.NotSameChapter(
        "strip_length 26000 against 20000"
    )
