"""The comicinfo stage's ComicInfo.xml (SPEC "Metadata" ComicInfo, "Publishing").

Series fields from stored records, the chapter's source, pages, and the folder check before publish.
"""

import io
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from functools import cache
from pathlib import Path

import pytest
import xmlschema
from PIL import Image

from manga_pipeline import comicinfo, config, db, export, intake, process
from manga_pipeline.metadata import merge, tags
from manga_pipeline.stages import comicinfo as stage

from tests.builders import present, settle
from tests.fakes.suwayomi import cbz
from tests.timing import budget
from tests.world import World
from tests.world import comicinfo as ci


@cache
def xsd():
    return xmlschema.XMLSchema(str(comicinfo.XSD))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def known_series(world):
    """Solo Leveling, found at MangaBaka by search, with AniList through MangaBaka's cross-link."""
    kavita_id = world.kavita_series("Solo Leveling")
    mb, al = world.sources.mangabaka, world.sources.anilist
    mb.results["Solo Leveling"] = [{"id": 50, "title": "Solo Leveling"}]
    mb.records[("series", "50")] = {
        "id": 50,
        "title": "Solo Leveling",
        "content_rating": "suggestive",
        "status": "completed",
        "genres": ["action", "fantasy"],
        "source": {"anilist": {"id": 7}},
    }
    al.records[("media", "7")] = {
        "id": 7,
        "description": "Hunters &amp; gates.<br>",
        "genres": ["Action", "Fantasy"],
        "staff": {"edges": [{"role": "Story & Art", "node": {"name": {"full": "Dubu"}}}]},
    }
    return kavita_id


def published_xml(world):
    (path,) = (world.library / "Solo Leveling").glob("Ch.2*.cbz")
    with zipfile.ZipFile(path) as z:
        return z.read("ComicInfo.xml")


def run_all(world):
    for part in (intake, process, export):
        world.run(part)


@budget(load="db", seconds=3)
def test_a_published_file_holds_the_series_fields_its_source_and_its_pages(world):
    known_series(world)
    download = cbz(comicinfo=ci(Number="2", Summary="From the source", Genre="Isekai", Translator="Grp"))
    chapter_id, _ = world.download(world.manga("Solo Leveling"), "Chapter 2", data=download)
    run_all(world)
    xml = published_xml(world)
    xsd().validate(xml)  # the XSD itself, beside the program's own checker
    got = comicinfo.read_all(xml)
    with closing(db.connect(world.cfg)) as conn:
        sid = world.rows("SELECT id FROM series WHERE name = 'Solo Leveling'")[0][0]
        fields = merge.stored(conn, sid, tags.load())
    assert got["Summary"] == "Hunters & gates.", "the stored records' summary, never the download's"
    assert got["Genre"] == ", ".join(fields.genres), "genres from the tag rules"
    assert "Isekai" not in got["Genre"], "genres from the tag rules"
    assert (got["AgeRating"], got["Writer"], got["Penciller"]) == ("Teen", "Dubu", "Dubu")
    assert got["Translator"] == "Grp", "a per-chapter element of the download is kept"
    assert got["Notes"] == "Source: Src (EN)"
    assert got["Web"].split(" ") == [*fields.links, f"https://source.invalid/chapter/{chapter_id}"], (
        "the series' links, then the chapter's page at its source"
    )
    assert got["Manga"] == "YesAndRightToLeft", "a page series with no left-to-right genre reads right to left"
    root = ET.fromstring(xml)
    pages = present(root.find("Pages")).findall("Page")
    assert got["PageCount"] == str(len(pages)) == "3"
    assert got["BlackAndWhite"] in ("Yes", "No")
    assert all(p.get("ImageWidth") and p.get("ImageSize") for p in pages), "each page measured"
    assert "Count" not in got, "Count is never written"


@budget(load="db", seconds=3)
def test_a_hand_placed_chapter_has_no_source_and_no_records_leave_series_elements_out(world):
    world.kavita_series("Solo Leveling")
    folder = world.mangas / "Local" / "Solo Leveling"
    folder.mkdir(parents=True)
    (folder / "Chapter 2.cbz").write_bytes(cbz(comicinfo=ci(Number="2", Summary="From the source")))
    settle(folder / "Chapter 2.cbz")
    run_all(world)
    got = comicinfo.read_all(published_xml(world))
    assert not {"Notes", "Web", "Summary", "Genre", "Writer"} & set(got), "nothing the stored records do not give"


@budget(load="db", seconds=3)
def test_a_series_whose_files_moved_is_held_before_anything_is_published(world):
    known_series(world)
    world.download(world.manga("Solo Leveling"), "Chapter 2", data=cbz(comicinfo=ci(Number="2")))
    world.run(intake)
    world.run(process)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("UPDATE series SET folder = 'library/Elsewhere' WHERE name = 'Solo Leveling'")
    world.run(export)
    status, reason = world.rows("SELECT status, status_reason FROM series WHERE name = 'Solo Leveling'")[0]
    assert status == "needs_human"
    assert "library/Elsewhere" in reason
    assert "library/Solo Leveling" in reason
    assert world.rows("SELECT stage FROM chapter") == [("publish",)], "nothing published into it"
    assert not list((world.library / "Solo Leveling").glob("Ch.2*")), "no file in either folder"


@budget(load="db", seconds=3)
def test_a_series_linked_again_publishes_into_its_new_folder(world):
    """The fix for a moved folder: series link records Kavita's folder; the chapter named before goes there."""
    known_series(world)
    world.download(world.manga("Solo Leveling"), "Chapter 2", data=cbz(comicinfo=ci(Number="2")))
    world.run(intake)
    world.run(process)
    world.run(export)  # the chapter is named for library/Solo Leveling and published there; take it back to publish
    ((library_path,),) = world.rows("SELECT library_path FROM chapter")
    moved = world.library / "Moved" / "Solo Leveling"  # its files keep the series' name: Kavita keeps the series
    moved.parent.mkdir()
    (world.library / "Solo Leveling").rename(moved)
    staged = config.to_local(world.cfg, world.rows("SELECT staged_path FROM chapter")[0][0])
    (moved / Path(library_path).name).rename(staged)
    world.kavita.scan_library(world.cfg["kavita"]["library_id"])
    world.kavita.run_scans()
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("DELETE FROM published_file WHERE origin = 'pipeline'")
        conn.execute("DELETE FROM handled_chapter WHERE outcome = 'published'")
        conn.execute("UPDATE chapter SET stage = 'publish', status = 'ready', waiting_on = NULL")
        conn.execute("UPDATE series SET folder = 'library/Moved/Solo Leveling' WHERE name = 'Solo Leveling'")
    world.run(export)
    assert (moved / Path(library_path).name).is_file(), "renamed into the folder the series has now"
    assert world.rows("SELECT library_path FROM chapter") == [
        (f"library/Moved/Solo Leveling/{Path(library_path).name}",)
    ]


@budget(load="db", seconds=3)
def test_the_override_files_series_group_is_written(world):
    known_series(world)
    overrides = world.cfg["paths"]["naming_overrides"]
    folder = config.abs_path(overrides)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Solo Leveling.yaml").write_text("series_group: [Hunters, Gates]\n", encoding="utf-8")
    world.download(world.manga("Solo Leveling"), "Chapter 2", data=cbz(comicinfo=ci(Number="2")))
    run_all(world)
    assert comicinfo.read_all(published_xml(world))["SeriesGroup"] == "Hunters, Gates"


@budget()
def test_page_entries_mark_the_inserted_cover_and_spreads_in_a_page_series():
    pages = [
        {"index": 0, "name": "000_cover.avif", "width": 800, "height": 1200, "bytes": 10},
        {"index": 1, "name": "001.avif", "width": 1600, "height": 1200, "bytes": 20},
    ]
    got = stage.page_entries(pages, cover=True, spreads=True)
    assert got[0]["Type"] == "FrontCover"
    assert "DoublePage" not in got[0]
    assert got[1]["DoublePage"] == "true"
    assert "Type" not in got[1]
    assert "DoublePage" not in stage.page_entries(pages, cover=False, spreads=False)[1], "a strip has no spreads"
    assert "Type" not in stage.page_entries(pages, cover=False, spreads=False)[0], "a source cover page is not marked"


MD = "2bdf5af2-7d55-4c03-8a1b-8b6b1b9a2b4c"


def png(width, height=1400):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


@budget(load="db", seconds=5)
def test_a_mangadex_series_names_its_new_chapter_and_side_story_and_covers_the_new_volume(world):
    """SPEC "Publishing": the volume from the recorded basis (MangaDex).

    A side story goes in the next volume after the main story, numbered and titled as the bulk converter does; the first
    chapter of a covered volume takes MangaDex's cover of that volume.
    """
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", 1), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", 1))
    kavita_id = world.kavita_series("Solo Leveling", bulk=bulk, basis="mangadex")
    world.kavita.metadata_rows[kavita_id] = {"seriesId": kavita_id, "webLinks": f"https://mangadex.org/title/{MD}"}
    md = world.sources.mangadex
    md.records[("manga", MD)] = {"id": MD, "attributes": {"originalLanguage": "ko"}}
    md.records[("aggregate", MD)] = {"volumes": {"2": {"volume": "2", "chapters": {"3": {"chapter": "3"}}}}}
    md.records[("covers", MD)] = [
        {"volume": "2", "locale": "ko", "fileName": "v2ko.png"},
        {"volume": "2", "locale": "en", "fileName": "v2.png"},
    ]
    md.images[(MD, "v2.png")] = png(900)
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 3", scanlator="Grp")
    world.download(manga, "Side Story 1", scanlator="Grp")
    run_all(world)
    folder = world.library / "Solo Leveling"
    assert (folder / "Vol.2 Ch.3.cbz").is_file(), "the MangaDex volume of chapter 3"
    assert (folder / "Vol.3 Ch.1 - Side Story 1.cbz").is_file(), "a side story: the next volume, numbered and titled"
    with zipfile.ZipFile(folder / "Vol.2 Ch.3.cbz") as z:
        assert any(n.startswith("000_cover.") for n in z.namelist()), "volume 2's first chapter takes its cover"
    with zipfile.ZipFile(folder / "Vol.3 Ch.1 - Side Story 1.cbz") as z:
        assert not any(n.startswith("000_cover.") for n in z.namelist()), "a side story opens no covered volume"
    ((volume, url),) = world.rows("SELECT volume, source_url FROM series_volume_cover")
    assert volume == 2, "English beats the original language"
    assert url.endswith(f"/covers/{MD}/v2.png"), "English beats the original language"
    assert world.rows(
        "SELECT kind, volume_source FROM published_file WHERE origin = 'pipeline' ORDER BY comicinfo_number"
    ) == [("side", "side"), ("main", "mangadex")]
