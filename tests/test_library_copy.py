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
from manga_pipeline.junk import ocr, review
from manga_pipeline.stages import junk

from tests import pages as fx
from tests import test_rewrites as rewrites
from tests.builders import present
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
    pages_in: int | None = 3,
    width: int | None = 16,
    *,
    strip: int | None = 4500,
    season=None,
    name=None,
    data=None,
    bpp=None,
    junk=None,
    origin="pipeline",
):
    """Add a published file of the series (a pipeline one by default) with its stored original measurements."""
    path = world.library / "Solo Leveling" / (name or "Ch.5.cbz")
    path.write_bytes(data or cbz(seed="published 5"))
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, source, state, pages_in,
                median_page_width, strip_length, origin, bpp, junk, cover_state)
            VALUES (%s, 1, %s, %s, 'confirmed', %s, %s, %s, %s, %s, %s, 'none')""",
            (
                config.to_stored(world.cfg, path),
                json.dumps(["grp", season, "", "5", ""]),
                source,
                pages_in,
                width,
                strip,
                origin,
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
        # 1 of 4 pages dropped from the published file's download, none from this one: 3 pages after junk on both
        ({"pages_in": 4, "junk": 1}, "less junk"),
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
        review.decide(conn, [candidate], "unsure", None, workflow.now(conn))
    world.run(process)
    assert state(world, earlier)[:2] == ("junk", "needs_human"), "set up: unsure, a person decides"
    _, later = world.download(world.manga_id, "Chapter 5", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), "an earlier copy at junk is ahead of it"
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), (
        "the wait is not over while the earlier copy is held for a person"
    )
    cli.chapter_drop(world.cfg, argparse.Namespace(chapter=chapter_id(world, earlier)))
    world.run(process)
    world.run(export)
    assert state(world, later)[:3] == ("confirm", "waiting", "kavita_scan"), "judged once the earlier is dropped"
    assert library_files(world) == ["Ch.1.cbz", "Ch.5.cbz"], "the later copy is published"


@budget(load="db", seconds=10)
def test_the_later_copy_waits_on_an_earlier_held_for_a_person_at_publish(world):
    """The earlier copy's file name is taken in the library before export: needs_human at publish, the later waits."""
    _, earlier = world.download(other_manga(world), "Chapter 5 - Arise", "Alpha", data=cbz(seed="alpha 5"))
    _, later = world.download(world.manga_id, "Chapter 5 - Rise", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    world.run(process)
    assert state(world, earlier)[:2] == ("comicinfo", "ready"), "set up: the earlier copy passed junk"
    (world.library / "Solo Leveling" / "Ch.5 - Arise.cbz").write_bytes(b"someone else's")
    world.run(export)
    assert state(world, earlier)[:2] == ("publish", "needs_human"), "set up: its target is already in the library"
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), "an earlier copy held at publish is ahead of it"


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
    _, later = world.download(world.manga_id, "Vol.2 Chapter 1", data=cbz(seed="vol 2"))
    world.run(intake)
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "open_copy"), (
        "a copy of the same chapter key waits while the earlier is ahead, whatever its volume"
    )
    world.run(export)
    confirm(world)
    for part in (process, export):
        world.run(part)
    assert library_files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.3.cbz", "Vol.2 Ch.1.cbz"], (
        "2 volumes are 2 chapters: the later copy is no copy of the earlier's file"
    )


def later_copy_at_junk(world, earlier_name, later_name):
    """Bring 2 copies of chapter 1 in; return (earlier path, later path).

    The earlier copy is a bad download, held at validate; the later, rated above the bulk Ch.1.cbz, waits at junk on its
    credit page's junk decision, naming no file: a copy is judged once its junk is decided.
    """
    _, earlier = world.download(other_manga(world), earlier_name, "Alpha", data=b"PK\x03\x04 truncated download")
    # 3 pages, as the bulk file's
    _, later = world.download(world.manga_id, later_name, "Beta", data=cbz(pages=2, seed="beta 1", credit=True))
    world.run(intake)
    assert state(world, earlier)[:3] == ("validate", "waiting", "suwayomi_download"), "set up: a bad download"
    assert arrival(world) == [earlier.name, later.name], "set up: the earlier copy is discovered first"
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "junk_decision"), "set up: its credit page is a candidate"
    assert world.rows("SELECT count(*) FROM chapter WHERE published_file_id IS NOT NULL") == [(0,)], (
        "no copy takes its file before its junk is decided"
    )
    return earlier, later


def replacement_arrives(world, earlier):
    """Deliver the earlier copy's re-download and take it through intake: it passes validate."""
    world.suwayomi.finish_downloads(cbz(seed="alpha 1 again"))
    world.run(intake)
    assert state(world, earlier)[:2] == ("junk", "ready"), "set up: the replacement passed validate"


@budget(load="db", seconds=20)
def test_an_earlier_copy_is_not_held_by_a_later_copy_waiting_on_its_junk_decision(world):
    """The later [R18] copy waits on its junk decision; the earlier, discovered first, is judged meanwhile.

    Once decided, the later copy takes Ch.1 and publishes; the earlier, no better than the bulk file, is a duplicate.
    """
    earlier, later = later_copy_at_junk(world, "Chapter 1", "Chapter 1 [R18]")
    replacement_arrives(world, earlier)
    world.run(process)
    assert state(world, later)[:3] == ("junk", "waiting", "junk_decision"), "set up: still waiting on its decision"
    stage, status, _, error = state(world, earlier)
    assert (stage, status) == ("junk", "done"), "a later copy at junk naming no file is not ahead of the earlier"
    assert error.startswith("dropped: a duplicate of the published"), "the earlier copy, no better, is a duplicate"
    decide_junk(world.cfg)
    world.run(process)
    assert state(world, later)[:3] == ("comicinfo", "ready", None), "decided, the better copy takes its file"
    for part in (export, process):
        world.run(part)
    assert state(world, later)[:2] == ("publish", "done"), "the better copy is published into Ch.1.cbz"
    assert library_files(world) == ["Ch.1.cbz"], "1 file of chapter 1"
    assert published_from(world, "Ch.1.cbz") == later.stem, "the file holds the later copy's pages"


@budget(load="db", seconds=30)
def test_an_earlier_copy_better_than_a_busy_file_is_held_then_takes_it_on_retry(tmp_path):
    """The earlier [R18] copy above the file: held "busy" while the later's rewrite is pending, then takes it.

    The bulk Ch.1 censored, the later copy untagged: a retry after the rewrite finishes lets the earlier take the file.
    """
    world = CopyWorld(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    world.kavita_series("Solo Leveling", bulk=(("Ch.1.cbz", "Grp_Chapter 1 [R15]", "1"),))
    world.manga_id = world.manga("Solo Leveling")
    earlier, later = later_copy_at_junk(world, "Chapter 1 [R18]", "Chapter 1")
    decide_junk(world.cfg)
    for part in (process, export):
        world.run(part)
    assert state(world, later)[:2] == ("publish", "done"), "set up: the later copy is published into Ch.1.cbz"
    replacement_arrives(world, earlier)
    world.run(process)
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


def our_copy(width: int = 940, bpp: float = 1.0, junk: float = 0, rating: int = 1) -> library_copy.Copy[float]:
    """Return our copy, its junk decided: 10 pages, strip length 20000, with these measures (bpp always measured)."""
    return library_copy.Copy(library_copy.Measures(10, width, 20000, bpp), rating, junk)


def their_copy(
    width: int = 940, bpp: float | None = 1.0, junk: float | None = 0, *, length: float = 10, junk_out: bool = True
) -> library_copy.PublishedCopy:
    """Return a published copy, untagged: an original of 10 pages, strip length 20000; length, junk_out as compared."""
    return library_copy.PublishedCopy(library_copy.Measures(10, width, 20000, bpp), 1, junk, length, junk_out)


def longer(pages: int, length: int, junk: float = 0) -> library_copy.Copy[float]:
    """Return our copy of the default measures with another page count and strip length."""
    return library_copy.Copy(library_copy.Measures(pages, 940, length, 1.0), 1, junk)


@budget(load="pure")
@pytest.mark.parametrize(
    ("ours", "theirs", "want"),
    [
        (longer(13, 20000), their_copy(), library_copy.NotSameChapter("pages_in after junk 13 against 10")),
        (longer(13, 20000), their_copy(junk_out=False), library_copy.NotSameChapter("pages_in 13 against 10")),
        (
            longer(13, 20000, junk=3),
            their_copy(),
            library_copy.Duplicate("more junk removed: 3 against 0"),
        ),
        (our_copy(rating=2, width=400), their_copy(), library_copy.Better()),
        (our_copy(rating=0, width=2000), their_copy(), library_copy.Duplicate("a lower edition rating")),
        (our_copy(width=800), their_copy(), library_copy.Duplicate("median page width 800 against 940")),
        (our_copy(bpp=0.5), their_copy(), library_copy.Duplicate("bits per pixel 0.50 against 1.00")),
        (our_copy(width=1000), their_copy(), library_copy.Better()),
        (our_copy(bpp=1.2), their_copy(), library_copy.Better()),
        (our_copy(junk=0), their_copy(junk=1, length=9), library_copy.Better()),
        (our_copy(junk=0), their_copy(), library_copy.Duplicate("no better in width, bits per pixel or junk")),
        (
            our_copy(width=1000, junk=2),
            their_copy(junk=1, length=9),
            library_copy.Duplicate("more junk removed: 2 against 1"),
        ),
        (our_copy(bpp=0.1, width=1000), their_copy(bpp=None, junk=None), library_copy.Better()),
    ],
    ids=[
        "not-the-same-chapter",
        "not-the-same-chapter-junk-in",
        "the-same-chapter-after-junk",
        "higher-rating",
        "lower-rating",
        "narrower",
        "recompressed",
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
def test_the_better_copy_rule_needs_our_junk():
    with pytest.raises(ValueError, match="junk is not decided"):
        library_copy.decide(
            library_copy.Copy(library_copy.Measures(10, 940, 20000, 1.0), 1, None), their_copy(), "pages"
        )


@budget(load="pure")
def test_strip_junk_differs_only_beyond_500_rows():
    strip = 20000  # the published copy's strip length
    assert (
        library_copy.decide(our_copy(width=1000, junk=600), their_copy(junk=200, length=strip), "strip")
        == library_copy.Better()
    )
    assert library_copy.decide(
        our_copy(width=1000, junk=800), their_copy(junk=200, length=strip), "strip"
    ) == library_copy.Duplicate("more junk removed: 800 against 200")
    assert library_copy.decide(longer(10, 26000), their_copy(length=strip), "strip") == library_copy.NotSameChapter(
        "strip_length after junk 26000 against 20000"
    )


# ---------------------------------------------------------------- adopted files (origin library)

UNMEASURED = {"pages_in": None, "width": None, "strip": None}


def zipped(pages: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for name, page in pages.items():
            z.writestr(name, page)
    return data.getvalue()


@budget(load="db")
def test_a_download_with_the_published_files_own_pages_is_a_duplicate_at_once(world):
    same = zipped({"000_cover.png": fake_page(9, "cover"), **{f"{n:03}.png": fake_page(n, "same") for n in (1, 2, 3)}})
    world.download(world.manga_id, "Chapter 5", data=same)
    world.run(intake)
    published_copy(world, BEST, None, None, data=same, origin="library", strip=None)
    world.run(process)
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), "the digest over every page, the cover too, equals the download's"
    assert "the same pages" in error, "the digest over every page, the cover too, equals the download's"


@budget(load="db")
@pytest.mark.parametrize(("origin", "compared"), [("library", True), ("pipeline", False)])
def test_an_adopted_files_bits_per_pixel_are_measured_from_it(world, origin, compared):
    with zipfile.ZipFile(io.BytesIO(cbz(seed="new Chapter 5"))) as z:
        padded = {n: z.read(n) + bytes(20000) for n in z.namelist()}  # the same pixels in many more bytes
    through_junk(world, copies=[{"data": zipped(padded), "origin": origin, **UNMEASURED}])
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), "an equal or worse copy is a duplicate"
    assert ("bits per pixel" in error and "against" in error) == compared, (
        "an adopted file holds its download's pages, so its bits per pixel count; a pipeline file's were re-encoded"
    )


@budget(load="db")
def test_original_gives_an_adopted_files_bits_per_pixel(world):
    page = fake_page(1, "bpp")
    world.run(intake)  # the series' row
    path = published_copy(world, BEST, None, None, data=zipped({"001.png": page}), origin="library", strip=None)
    ctx = workflow.context(world.cfg, db.connect(world.cfg), "test")
    try:
        row = ctx.conn.execute("SELECT * FROM published_file WHERE library_path LIKE %s", ("%Ch.5.cbz",)).fetchone()
        got = library_copy.original(ctx, present(row))
    finally:
        ctx.conn.close()
    width, height = present(images.dimensions(page))
    assert got.bpp == pytest.approx(8 * len(page) / (width * height)), (
        f"source review reads {path.name}'s bits per pixel through original: bytes over pixels"
    )


# ---------------------------------------------------------------- the same chapter after junk (D3)


def chapter_5_file(world):
    """Return (ctx, the published row of Ch.5.cbz) on a new connection; the caller closes ctx.conn."""
    ctx = workflow.context(world.cfg, db.connect(world.cfg), "test")
    row = ctx.conn.execute("SELECT * FROM published_file WHERE library_path LIKE %s", ("%Ch.5.cbz",)).fetchone()
    return ctx, present(row)


@budget(load="db")
def test_copies_are_the_same_chapter_after_junk_though_not_before(world):
    """Chapter 134: the official copy, no junk, strip 22,576; the converted file's original 26,952, junk included."""
    world.run(intake)  # the series' row
    published_copy(world, pages_in=40, width=800, strip=26952, junk=4376)  # a converted adopted file: junk stored
    ours = library_copy.Copy(library_copy.Measures(36, 800, 22576, 1.0), 1, 0.0)
    ctx, row = chapter_5_file(world)
    try:
        theirs = library_copy.published_copy(ctx, row, "strip")
    finally:
        ctx.conn.close()
    assert abs(22576 - 26952) > library_copy.SAME_CHAPTER * 26952, "set up: with junk in, another chapter"
    assert library_copy.decide(ours, theirs, "strip") == library_copy.Better(), (
        "22,576 against 26,952 less its 4,376 of junk: the same chapter, and less junk removed"
    )


def judged_after_junk(world, name, copy):
    """Take a download of `name` with a credit page (4 pages) to junk, add the published copy, decide the page junk.

    copy: published_copy keywords, or None for the bulk file. Returns outcome once the chapter is judged.
    """
    world.download(world.manga_id, name, data=cbz(seed=f"new {name}", credit=True))
    world.run(intake)
    if copy is not None:
        published_copy(world, **copy)
    world.run(process)
    assert outcome(world)[:2] == ("junk", "waiting"), "set up: its credit page waits on a junk decision"
    decide_junk(world.cfg, "junk")
    world.run(process)
    return outcome(world)


@budget(load="db", seconds=5)
def test_a_bulk_file_is_compared_as_it_is_against_ours_after_junk(world):
    stage, status, error = judged_after_junk(world, "Chapter 1", None)  # the bulk Ch.1.cbz: 3 pages, measured
    assert (stage, status) == ("junk", "done"), "3 pages after junk against the bulk file's 3 (4 before: not the same)"
    assert error.startswith("dropped: a duplicate of the published"), "the same chapter, no better: a duplicate"


@budget(load="db", seconds=5)
def test_a_pipeline_file_without_stored_junk_is_compared_with_junk_in_on_both_sides(world):
    stage, status, error = judged_after_junk(world, "Chapter 5", {"pages_in": 4, "width": 17, "junk": None})
    assert (stage, status) == ("junk", "done"), "4 pages against its stored 4, junk in on both (3 after junk: not)"
    assert "median page width 16 against 17" in error, "the same chapter, then narrower: a duplicate"


@budget(load="db", seconds=5)
def test_an_unconverted_adopted_file_is_compared_with_junk_in_on_both_sides(world):
    wide = zipped({f"{n:03}.jpg": fx.jpeg(fx.manga_page(size=(300, 420), seed=n)) for n in range(1, 5)})
    adopted = {"data": wide, "origin": "library", **UNMEASURED}  # 4 pages, credit page included
    stage, status, error = judged_after_junk(world, "Chapter 5", adopted)
    assert (stage, status) == ("junk", "done"), "4 pages against its 4, junk in on both (3 after junk: not)"
    assert "median page width 16 against 300" in error, "the same chapter, then narrower: a duplicate"


SAME = zipped({f"{n:03}.png": fake_page(n, "same") for n in (1, 2, 3)})


@budget(load="db")
@pytest.mark.parametrize(
    ("published", "verdict"),
    [
        ({"width": 17}, None),  # narrower than 0.95 x: a duplicate, but only once its junk is decided
        ({"data": SAME, "origin": "library", **UNMEASURED}, "the same pages"),
    ],
    ids=["narrower", "same-pages"],
)
def test_no_verdict_but_same_pages_before_junk(world, monkeypatch, published, verdict):
    world.download(world.manga_id, "Chapter 5", data=SAME)
    world.run(intake)
    published_copy(world, **published)

    def killed(*_args):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(junk, "decide", killed)  # process killed once the scan is stored
        try:
            world.run(process)
        except KeyboardInterrupt:
            assert verdict is None, "a copy dropped at once reaches no junk decision"
    scans = world.rows("SELECT count(*) FROM junk_scan")
    if verdict is None:
        assert scans == [(1,)], "no verdict before junk: it is scanned first"
        assert world.rows("SELECT count(*) FROM handled_chapter WHERE outcome = 'duplicate'") == [(0,)], (
            "no verdict before junk"
        )
    else:
        assert verdict in outcome(world)[2], "the published file's own pages: a duplicate at once"
        assert scans == [(0,)], "dropped before any scan"


COVERED = zipped({"000_cover.png": fake_page(9, "cover"), **{f"{n:03}.png": fake_page(n, "x") for n in (1, 2, 3)}})


def png(width, height, shade):
    """Return a plain PNG page of this size."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), (shade, 90, 200 - shade)).save(out, "PNG")
    return out.getvalue()


# a wide 000_cover page before pages 16, 16 and 30 px wide: every page's median width 23, the content pages' 16
WIDE_COVER = zipped(
    {
        "000_cover.png": png(40, 60, 10),
        "001.png": png(16, 24, 40),
        "002.png": png(16, 24, 70),
        "003.png": png(30, 45, 100),
    }
)


@budget(load="db")
@pytest.mark.parametrize(
    ("download", "published"),
    [
        (COVERED, {"junk": 0}),  # ours a 000_cover page and 3 pages, against a stored original of 3
        (cbz(seed="new Chapter 5"), {"data": COVERED, **UNMEASURED}),  # ours 3, against a file of a 000_cover and 3
    ],
    ids=["ours", "theirs"],
)
def test_a_000_cover_page_is_not_counted_on_either_side(world, download, published):
    world.download(world.manga_id, "Chapter 5", data=download)
    world.run(intake)
    published_copy(world, **published)
    world.run(process)
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), f"3 pages against 3, the cover page left out: {error}"
    assert error.startswith("dropped: a duplicate of the published"), "3 pages against 3, the cover page left out"


def published_values(world, data, tmp_path):
    """Publish a download of chapter 5 holding data; return (its pages' measure_pages, its published row's values)."""
    world.download(world.manga_id, "Chapter 5", data=data)
    world.run(intake)
    world.run(process)
    decide_junk(world.cfg, "keep")  # a page offered as a candidate, if any
    for part in (process, export):
        world.run(part)
    path = tmp_path / "download.cbz"
    path.write_bytes(data)
    ((stored,),) = world.rows(
        "SELECT jsonb_build_object('pages_in', pages_in, 'median_page_width', median_page_width, 'strip_length',"
        " strip_length, 'page_phashes', page_phashes, 'bpp', bpp) FROM published_file WHERE origin = 'pipeline'"
    )
    return images.measure_pages(path), stored


@budget(load="db", seconds=5)
def test_a_000_cover_page_of_a_download_is_not_in_its_stored_lengths(world, tmp_path):
    pages, stored = published_values(world, WIDE_COVER, tmp_path)
    content = measures.summary([p for p in pages if not p["name"].startswith("000_cover")])
    assert (stored["pages_in"], stored["strip_length"]) == (3, content["strip_length"]), (
        "the stored original's lengths leave the 000_cover page out"
    )
    assert stored["median_page_width"] == measures.summary(pages)["median_page_width"] == 23, "width over every page"


@budget(load="db", seconds=5)
def test_a_download_without_a_cover_page_stores_todays_values(world, tmp_path):
    pages, stored = published_values(world, cbz(seed="new Chapter 5"), tmp_path)
    m = measures.summary(pages)
    assert stored == {
        "pages_in": m["pages_in"],
        "median_page_width": m["median_page_width"],
        "strip_length": m["strip_length"],
        "page_phashes": json.dumps(m["page_phashes"]),
        "bpp": pytest.approx(measures.bits_per_pixel(pages)),
    }, "every page counted, as before"


@budget(load="db", seconds=10)
def test_a_download_never_waits_on_a_chapter_at_junk_that_names_its_file(world):
    """A chapter at junk naming its file (an adopted file's conversion) is never ahead, and never waits."""
    _, first = world.download(other_manga(world), "Chapter 5", "Alpha", data=cbz(seed="alpha 5"))
    _, second = world.download(world.manga_id, "Chapter 5", "Beta", data=cbz(seed="beta 5"))
    world.run(intake)
    assert arrival(world) == [first.name, second.name], "set up: the chapter naming its file is discovered first"
    published_copy(world)
    ids = {download: chapter_id(world, download) for download in (first, second)}
    world.rows("UPDATE chapter SET published_file_id = 2 WHERE id = %s RETURNING id", ids[first])

    def ahead(download):
        """Return whether earlier_copy finds a copy ahead of the chapter of `download`."""
        with closing(db.connect(world.cfg)) as conn:
            found = conn.execute("SELECT * FROM chapter WHERE id = %s", (ids[download],)).fetchone()
            return library_copy.earlier_copy(conn, present(found))

    assert not ahead(second), "an earlier chapter at junk naming its file is not ahead"
    world.rows("UPDATE chapter SET stage = 'convert' WHERE id = %s RETURNING id", ids[second])
    assert not ahead(first), "a chapter naming its file waits on no copy, even one past junk"


@budget(load="db", seconds=5)
def test_our_width_is_measured_over_every_page_as_publish_stores_it(world):
    world.download(world.manga_id, "Chapter 5", data=WIDE_COVER)
    world.run(intake)
    published_copy(world, width=23, junk=0)  # stored as publish stores WIDE_COVER: 3 pages, strip 4500, width 23
    world.run(process)
    decide_junk(world.cfg, "keep")  # the wide cover page, when it is offered
    world.run(process)
    stage, status, error = outcome(world)
    assert (stage, status) == ("junk", "done"), error
    assert error.endswith("no better in width, bits per pixel or junk"), (
        "our width over every page, 23, as the stored one's (over the content pages it would be 16: narrower)"
    )


@budget(load="db", seconds=5)
def test_a_pipeline_file_without_a_stored_original_is_compared_as_measured_against_ours_after_junk(world):
    stage, status, error = judged_after_junk(world, "Chapter 5", {"origin": "pipeline", **UNMEASURED})
    assert (stage, status) == ("junk", "done"), "3 pages after junk against its file's 3 (4 before: not the same)"
    assert error.startswith("dropped: a duplicate of the published"), "the same chapter, no better: a duplicate"


@budget(load="db", seconds=10)
def test_a_strip_copy_is_the_same_chapter_after_junk_and_takes_its_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    world = CopyWorld(tmp_path, sources={"ranking": [BEST, "Src (EN)"], "download_timeout_hours": 24})
    world.kavita_series("Solo Leveling", layout="strip")
    world.manga_id = world.manga("Solo Leveling")
    world.download(world.manga_id, "Chapter 5", data=cbz(seed="new Chapter 5"))  # 3 pages 16 x 24: strip 4500
    world.run(intake)
    published_copy(world, strip=5850, junk=1350)  # its original 30 % longer, the excess junk its download lost
    world.run(process)
    decide_junk(world.cfg, "keep")
    world.run(process)
    ((stage, status, file_id),) = world.rows("SELECT stage, status, published_file_id FROM chapter")
    assert (stage, status, file_id) == ("comicinfo", "ready", 2), (
        "4500 against 5850 less 1350: the same chapter after junk (not before), and less junk removed: better"
    )
