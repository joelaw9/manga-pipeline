"""The naming resolver's wiring.

SPEC "Publishing", "State", "Metadata" Volume covers, "Library copies", "Bad downloads": which chapters count when
naming, the baseline's naming fields on an older baseline, the volume-cover supply and the copy check's volume rule
(D7.21).
"""

import hashlib
import io
import json
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import config, db, export, intake, names, process, workflow
from manga_pipeline.clients import http
from manga_pipeline.metadata import covers
from manga_pipeline.stages import publish

from tests.timing import budget
from tests.world import World, bulk_record, cbz

MD = "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"
BAD = b"PK\x03\x04 truncated download"
OTHER = "Other (EN)"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def png(width, height=1400):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def mangadex_series(world):
    """Add a `mangadex`-basis series holding Vol.1 Ch.1-2.

    Its MangaDex aggregate puts chapters 3 and 4 in volume 2 and has volume 2's cover. Returns the Suwayomi manga id.
    """
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", 1), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", 1))
    kavita_id = world.kavita_series("Solo Leveling", bulk=bulk, basis="mangadex")
    world.kavita.metadata_rows[kavita_id] = {"seriesId": kavita_id, "webLinks": f"https://mangadex.org/title/{MD}"}
    md = world.sources.mangadex
    md.records[("manga", MD)] = {"id": MD, "attributes": {"originalLanguage": "ko"}}
    chapters = {"3": {"chapter": "3"}, "4": {"chapter": "4"}}
    md.records[("aggregate", MD)] = {"volumes": {"2": {"volume": "2", "chapters": chapters}}}
    md.records[("covers", MD)] = [{"volume": "2", "locale": "en", "fileName": "v2.png"}]
    md.images[(MD, "v2.png")] = png(900)
    return world.manga("Solo Leveling")


def bulk_naming(world):
    return world.rows(
        "SELECT library_path, source_name, kind, volume_source FROM published_file WHERE origin = 'bulk' ORDER BY 1"
    )


def forget_bulk_naming(world):
    """Leave the bulk rows as a baseline taken before schema step 0054 left them."""
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("UPDATE published_file SET source_name = NULL, kind = NULL, volume_source = NULL")


@budget(load="db", seconds=8)
def test_a_named_side_story_held_at_publish_keeps_its_number(world, monkeypatch):
    """A chapter comicinfo named keeps its name while it is needs_human: the next side story is numbered after it."""
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    monkeypatch.setattr(publish, "run", lambda _ctx, _chapter: workflow.NeedsHuman("held for the test"))
    world.download(manga, "Side Story 1", scanlator="Grp")
    for part in (intake, process, export):
        world.run(part)
    world.download(manga, "Side Story 2", scanlator="Grp")
    for part in (intake, process, export):
        world.run(part)
    assert world.rows("SELECT stage, status, naming_kind, comicinfo_number FROM chapter ORDER BY id") == [
        ("publish", "needs_human", "side", "1"),
        ("publish", "needs_human", "side", "2"),
    ], "a held, named side story still holds side number 1: the next one takes 2"


@budget(load="db", seconds=3)
def test_an_older_baseline_gets_its_naming_fields_at_the_next_validate(world):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 2", scanlator="Grp")
    world.run(intake)
    forget_bulk_naming(world)
    folder = world.library / "Solo Leveling"
    gone = config.to_stored(world.cfg, folder / "Side Story 1.cbz")  # a bulk file the record no longer lists
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, state, origin)
            VALUES (%s, %s, %s, 'confirmed', 'bulk')""",
            (gone, 1, names.identity(names.parse("Grp_Side Story 1"))),
        )
    record = json.loads((folder / ".optimizer.json").read_bytes())
    record["series"]["naming"]["series_name"] = "Another Name"  # a fresh baseline would hold the series for this
    (folder / ".optimizer.json").write_bytes(json.dumps(record).encode())
    world.download(manga, "Chapter 3", scanlator="Grp")
    world.run(intake)
    assert bulk_naming(world) == [
        (config.to_stored(world.cfg, folder / "Ch.1.cbz"), "Grp_Chapter 1", "main", "none"),
        (gone, None, "side", None),
    ], "filled from the record by output file name; an unlisted row's kind from its identity's side label"
    assert world.rows("SELECT status FROM series") == [("ok",)], (
        "filling an existing baseline never checks the record's series name"
    )
    assert world.rows("SELECT stage, status FROM chapter ORDER BY id") == [("junk", "ready"), ("junk", "ready")]


@budget(load="db", seconds=4)
@pytest.mark.parametrize("readable", [True, False], ids=["record", "no-record"])
def test_comicinfo_fills_the_bulk_rows_naming_fields_or_waits_for_the_record(world, readable):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2", scanlator="Grp")
    world.run(intake)
    world.run(process)
    forget_bulk_naming(world)
    if not readable:
        (world.library / "Solo Leveling" / ".optimizer.json").unlink()
    world.run(export)
    (row,) = world.rows("SELECT stage, status, waiting_on FROM chapter")
    if readable:
        assert bulk_naming(world)[0][1:] == ("Grp_Chapter 1", "main", "none"), "filled from the record"
        assert row[0] != "comicinfo", "the chapter goes on to publish"
    else:
        assert row == ("comicinfo", "waiting", "bulk_conversion"), "no record: the chapter waits for it"
        assert bulk_naming(world)[0][1:] == (None, None, None)


@budget(load="db", seconds=3)
def test_a_bulk_conversion_wait_left_at_junk_ends_with_a_baseline(world):
    kavita_id = world.series_without_record("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2", scanlator="Grp")
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:  # as an older version left a chapter waiting past validate
        conn.execute("UPDATE chapter SET stage = 'junk'")
    assert world.rows("SELECT stage, status, waiting_on FROM chapter") == [("junk", "waiting", "bulk_conversion")]
    world.write_record("Solo Leveling", bulk_record("Solo Leveling", kavita_id, []))
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")]
    assert world.rows("SELECT baseline_at IS NOT NULL FROM series") == [(1,)], "the wait ends with the baseline taken"


@budget(load="db", seconds=4)
def test_a_volume_cover_an_open_chapter_holds_is_not_taken_by_a_lower_chapter(world):
    manga = mangadex_series(world)
    world.download(manga, "Chapter 4", scanlator="Grp")
    world.run(intake)
    world.download(manga, "Chapter 3", scanlator="Grp")
    world.run(intake)
    (ch4, ch3) = (r[0] for r in world.rows("SELECT id FROM chapter ORDER BY id"))
    assert world.rows("SELECT chapter_id, volume FROM volume_cover") == [(ch4, "2")], (
        "volume 2's cover stays with chapter 4, which took it first; chapter 3 takes none"
    )
    assert world.rows("SELECT stage, status FROM chapter WHERE id = %s", ch3) == [("junk", "ready")]


@budget(load="db", seconds=4)
def test_a_volume_cover_mangadex_no_longer_serves_gives_no_cover(world, monkeypatch):
    manga = mangadex_series(world)
    asked = []

    def gone(md_id, file_name):
        asked.append(file_name)
        raise http.HttpError(404, f"https://uploads.mangadex.org/covers/{md_id}/{file_name}", b"")

    monkeypatch.setattr(world.sources.mangadex, "cover_image", gone)
    world.download(manga, "Chapter 3", scanlator="Grp")
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], "the chapter goes on"
    assert asked, "asked, answered 404: no cover"
    assert world.rows("SELECT count(*) FROM volume_cover") == [(0,)], "asked, answered 404: no cover"


@budget(load="db", seconds=4)
def test_a_volume_cover_file_holding_other_bytes_is_written_again(world):
    manga = mangadex_series(world)
    world.download(manga, "Chapter 3", scanlator="Grp")
    world.run(intake)
    ((rel, sha256),) = world.rows("SELECT path, sha256 FROM series_volume_cover")
    stored = config.abs_path(world.cfg["paths"]["covers"]) / rel
    stored.write_bytes(b"not the cover")
    with closing(db.connect(world.cfg)) as conn:
        got = covers.volume_cover(world.context(conn, "intake"), 1, 2)
    assert got == (rel, sha256)
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == sha256, "the file holds the cover's bytes again"


@budget(load="db", seconds=4)
def test_a_passing_replacement_whose_naming_fails_becomes_the_chapters_download(tmp_path):
    world = World(tmp_path, sources={"ranking": [OTHER], "download_timeout_hours": 24})
    world.kavita_series("Solo Leveling", basis="override")  # and no override file: naming raises VolumeError
    manga = world.manga("Solo Leveling")
    for n in [*range(1, 12), *range(13, 21)]:
        world.suwayomi.add_chapter(manga, f"Chapter {n}")
    other = world.suwayomi.add_manga("Solo Leveling", world.suwayomi.add_source("Other"), in_library=False)
    other_chapters = {n: world.suwayomi.add_chapter(other, f"Ch. {n}") for n in range(1, 21)}
    world.download(manga, "Chapter 12", data=BAD)
    world.run(intake)
    world.suwayomi.finish_downloads(BAD)  # the same chapter again: broken, so the next source is queued
    world.run(intake)
    world.suwayomi.finish_downloads(cbz(seed="other"))
    world.run(intake)
    replacement = config.to_stored(world.cfg, world.suwayomi.path(other_chapters[12]))
    ((status, download, redownload, error),) = world.rows(
        "SELECT status, download_path, redownload, last_error FROM chapter"
    )
    assert (status, download, redownload) == ("needs_human", replacement, None), (
        "the replacement is saved as the chapter's download and the re-download state cleared"
    )
    assert "override" in error


@budget(load="db", seconds=4)
@pytest.mark.parametrize(
    ("basis", "want"),
    [
        ("mangadex", ("junk", "done", "a duplicate of the published")),
        ("filename", ("validate", "needs_human", "not a whole number")),
    ],
)
def test_a_fractional_volume_is_only_read_against_a_file_name_volume(world, basis, want):
    """D7.21: a fractional volume is only read against a file-name volume.

    A published chapter whose volume a source assigned is the same chapter whatever the download's volume, so the
    download's fractional volume is never read and the copies are compared; against a file-name basis it is read, and a
    person must act before the copy check.
    """
    world.kavita_series("Solo Leveling", bulk=(("Vol.1 Ch.12.cbz", "Grp_Vol.1 Chapter 12", "12", 1),), basis=basis)
    world.download(world.manga("Solo Leveling"), "Vol.2.5 Ch.12")
    world.run(intake)
    world.run(process)
    ((stage, status, error),) = world.rows("SELECT stage, status, last_error FROM chapter")
    assert (stage, status) == want[:2], f"{basis}: {stage}: {error}"
    assert want[2] in error, f"{basis}: {stage}: {error}"
