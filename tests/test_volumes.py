"""A new chapter's volume and the published name it gives (SPEC "Publishing"): the bulk converter's rule."""

from pathlib import Path

import pytest

from manga_pipeline import chapter_names, config, export, intake, process, volumes

from tests.timing import budget
from tests.world import World, cbz
from tests.world import comicinfo as ci


def write_override(folder, name, text):
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / f"{name}.yaml").write_text(text, encoding="utf-8")


def world_overrides(world):
    return config.abs_path(world.cfg["paths"]["naming_overrides"])


@budget(load="fs")
@pytest.mark.parametrize(
    "number, want",
    [("3", 0), ("4", 3), ("5", None), ("65", 1), ("66", 2), ("141", 2), ("65.5", None)],
    ids=["open-start", "single", "in-no-range", "closed-end", "range-start", "open-end", "between-ranges"],
)
def test_override_ranges_as_the_bulk_converter_reads_them(tmp_path, number, want):
    write_override(tmp_path, "Solo Leveling", 'layout: strip\nvolumes: {0: "-3", 3: "4", 1: "6-65", 2: "66-"}\n')
    ranges = volumes.override_file(tmp_path, "Solo Leveling")["volumes"]
    assert volumes.override_volume(ranges, float(number)) == want


@budget(load="fs")
@pytest.mark.parametrize(
    "text",
    [
        "volumes: [1, 2]\n",
        'volumes: {x: "1-5"}\n',
        'volumes: {1: "9-2"}\n',
        'volumes: {1: "a-b"}\n',
        "- a\n",
        "volumes: {1: [\n",
        "layout: webtoon\n",
    ],
    ids=["not-a-mapping", "bad-volume", "backwards", "bad-range", "file-not-a-mapping", "bad-yaml", "bad-layout"],
)
def test_a_malformed_override_is_an_override_error(tmp_path, text):
    write_override(tmp_path, "Solo Leveling", text)
    with pytest.raises(volumes.OverrideError, match="Solo Leveling"):
        volumes.override_file(tmp_path, "Solo Leveling")


@budget(load="fs")
def test_a_series_without_an_override_file_has_none(tmp_path):
    assert volumes.override_file(tmp_path, "Solo Leveling") is None, "only an override basis needs the file"


@budget(load="db", seconds=2)
def test_a_chapter_of_an_override_series_is_published_with_the_bulk_converters_name(tmp_path):
    world = World(tmp_path / "world")
    write_override(world_overrides(world), "Solo Leveling", 'volumes: {1: "1-65", 2: "66-"}\n')
    world.kavita_series("Solo Leveling", bulk=(("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1"),), basis="override")
    world.download(
        world.manga("Solo Leveling"),
        "Chapter 141: Arise (END)",
        data=cbz(comicinfo=ci(Number="141", Title="Chapter 141 : Arise (END)", Volume="9")),
    )
    for part in (intake, process, export):
        world.run(part)
    target = world.library / "Solo Leveling" / "Vol.2 Ch.141 - Arise.cbz"
    assert target.is_file(), "Vol.<override volume> Ch.<n> - <cleaned title>.cbz"
    assert world.rows("SELECT comicinfo_volume, comicinfo_title FROM published_file WHERE origin = 'pipeline'") == [
        ("2", "Arise")
    ], "ComicInfo Volume is the computed one (the download's 9 is dropped), Title the cleaned one"


@budget(load="db", seconds=2)
def test_an_override_series_without_its_file_needs_a_human_at_validate(tmp_path):
    world = World(tmp_path / "world")
    world.kavita_series("Solo Leveling", basis="override")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    for part in (intake, process, export):
        world.run(part)
    assert world.rows("SELECT stage, status, attempts FROM chapter") == [("validate", "needs_human", 0)], (
        "the first stage that names the chapter holds it"
    )
    assert not list(Path(world.library / "Solo Leveling").glob("Ch.2*")), "nothing published"


@budget(load="db", seconds=2)
def test_without_a_volume_the_downloads_volume_is_dropped_and_the_title_falls_back(tmp_path):
    world = World(tmp_path / "world")
    world.kavita_series("Solo Leveling")  # basis none
    world.download(world.manga("Solo Leveling"), "Chapter 2", data=cbz(comicinfo=ci(Number="2", Volume="9")))
    for part in (intake, process, export):
        world.run(part)
    assert (world.library / "Solo Leveling" / "Ch.2.cbz").is_file()
    assert world.rows("SELECT comicinfo_volume, comicinfo_title FROM published_file WHERE origin = 'pipeline'") == [
        (None, None)
    ], "name and ComicInfo agree: no volume; an untitled chapter is recorded with no title, as the baseline does"


@budget(load="pure")
@pytest.mark.parametrize(
    "ours, theirs, source, same",
    [
        (2, "1", "filename", False),
        (1, "1", "filename", True),
        (None, "1", "filename", True),
        (2, None, "filename", True),
        (2, "1", "mangadex", True),
        (2, "1", "override", True),
        (2, "1", None, True),
    ],
)
def test_2_volumes_from_the_files_tell_chapters_apart(ours, theirs, source, same):
    """D7.21 (user 2026-11-15): a download's own volume and a published file's volume from its file name must not
    differ; a volume a source or rule assigned never splits a chapter (MangaDex may move it later)."""
    assert chapter_names.same_chapter(lambda: ours, theirs, source) is same


@budget(load="fs")
@pytest.mark.parametrize(
    ("text", "group"),
    [
        (None, None),
        ("volumes: {1: '1-'}\n", None),
        ("series_group: Hunters\n", "Hunters"),
        ("series_group: [A, B]\n", "A, B"),
    ],
)
def test_the_series_group_is_the_override_files_names_joined(tmp_path, text, group):
    if text is not None:
        write_override(tmp_path, "Solo Leveling", text)
    assert volumes.series_group(tmp_path, "Solo Leveling") == group, "no file or no key: none"


@budget(load="fs")
@pytest.mark.parametrize("text", ["series_group: [A, 'B, C']\n", "series_group: []\n", "series_group: ['']\n"])
def test_a_malformed_series_group_is_an_override_error(tmp_path, text):
    write_override(tmp_path, "Solo Leveling", text)
    with pytest.raises(volumes.OverrideError, match="series_group"):
        volumes.series_group(tmp_path, "Solo Leveling")
