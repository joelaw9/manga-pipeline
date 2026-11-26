"""A new chapter's volume and the published name it gives (SPEC "Publishing"): the bulk converter's rule."""

from pathlib import Path

import pytest

from manga_pipeline import chapter_names, config, export, intake, process, volumes

from tests.builders import present
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
    ("number", "want"),
    [("3", 0), ("4", 3), ("5", None), ("65", 1), ("66", 2), ("141", 2), ("65.5", None)],
    ids=["open-start", "single", "in-no-range", "closed-end", "range-start", "open-end", "between-ranges"],
)
def test_override_ranges_as_the_bulk_converter_reads_them(tmp_path, number, want):
    write_override(tmp_path, "Solo Leveling", 'layout: strip\nvolumes: {0: "-3", 3: "4", 1: "6-65", 2: "66-"}\n')
    overrides = present(volumes.override_file(tmp_path, "Solo Leveling"))
    assert volumes.override_volume(overrides, float(number)) == want


SILENT_WITCH = "volume_starts: {1: 0, 2: 5, 3: 9, 4: 14, 5: 18, 6: 22, 7: 27, 8: 33}\n"


@budget(load="fs")
@pytest.mark.parametrize(
    ("number", "want"),
    [("0", 1), ("4.1", 1), ("4.5", 1), ("5", 2), ("8.5", 2), ("13.5", 3), ("33", 8), ("140", 8)],
    ids=["first-start", "split-part", "half", "a-start", "half-before-a-start", "half-later", "last-start", "open-end"],
)
def test_volume_starts_put_a_chapter_in_the_volume_whose_start_it_has_reached(tmp_path, number, want):
    write_override(tmp_path, "Silent Witch", SILENT_WITCH)
    overrides = present(volumes.override_file(tmp_path, "Silent Witch"))
    assert volumes.override_volume(overrides, float(number)) == want, (
        "a volume runs from its start up to the next volume's start, decimal parts included; the last has no end"
    )


@budget(load="fs")
@pytest.mark.parametrize(
    ("text", "number", "want", "rule"),
    [
        ("volume_starts: {1: 3, 2: 10}\n", "2.5", None, "no volume below the lowest start"),
        ("volume_starts: {0: 0, 1: 4}\n", "3", 0, "volume 0 is a volume"),
        ('volume_starts: null\nvolumes: {1: "1-5"}\n', "5", 1, "volume_starts: null is the key absent"),
    ],
    ids=["below-the-first-start", "volume-0", "null-is-absent"],
)
def test_volume_starts_edges(tmp_path, text, number, want, rule):
    write_override(tmp_path, "Silent Witch", text)
    overrides = present(volumes.override_file(tmp_path, "Silent Witch"))
    assert volumes.override_volume(overrides, float(number)) == want, rule


@budget(load="fs")
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('volumes: {1: "1-4"}\nvolume_starts: {1: 0}\n', "both"),
        ("volume_starts: {}\n", "mapping"),
        ("volume_starts: [0, 5]\n", "mapping"),
        ('volume_starts: {"1": 0, 2: 5}\n', "key '1'"),
        ("volume_starts: {true: 0}\n", "key True"),
        ("volume_starts: {1: 0, 2: 5, 4: 9}\n", r"skips volume\(s\) 3:"),
        ("volume_starts: {1: 0, 2: 5, 5: 9, 6: 12}\n", r"skips volume\(s\) 3-4:"),
        ("volume_starts: {2: 0, 3: 5}\n", r"skips volume\(s\) 1:"),
        ("volume_starts: {2: 0, 4: 5}\n", r"skips volume\(s\) 1, 3:"),
        ("volume_starts: {1: 0, 100000000: 5}\n", r"skips volume\(s\) 2-99999999:"),
        ("volume_starts: {1: 0, 2: 0}\n", "does not come after"),
        ("volume_starts: {1: 5, 2: 3}\n", "does not come after"),
        ("volume_starts: {1: true}\n", "start True"),
        ('volume_starts: {1: "5"}\n', "start '5'"),
        ("volume_starts: {1: .nan}\n", "start nan"),
        ("volume_starts: {1: .inf}\n", "start inf"),
        ("volume_starts: {1: -1}\n", "start -1"),
    ],
    ids=[
        "both-keys",
        "empty",
        "a-list",
        "quoted-key",
        "bool-key",
        "skipped-volume",
        "skipped-volumes",
        "lowest-not-0-or-1",
        "lowest-and-later-gap",
        "huge-gap-as-a-span",
        "equal-starts",
        "falling-starts",
        "bool-start",
        "quoted-start",
        "nan-start",
        "inf-start",
        "negative-start",
    ],
)
def test_a_malformed_volume_starts_is_an_override_error_naming_the_problem(tmp_path, text, reason):
    write_override(tmp_path, "Silent Witch", text)
    with pytest.raises(volumes.OverrideError, match=reason):
        volumes.override_file(tmp_path, "Silent Witch")


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
    ("ours", "theirs", "source", "same"),
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
    """D7.21 (user 2026-11-15): 2 volumes from the files tell chapters apart.

    A download's own volume and a published file's volume from its file name must not differ; a volume a source or
    rule assigned never splits a chapter (MangaDex may move it later).
    """
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
@pytest.mark.parametrize("text", ["", "# no overrides yet\n"], ids=["empty", "comment-only"])
def test_an_empty_override_file_holds_no_overrides(tmp_path, text):
    write_override(tmp_path, "Solo Leveling", text)
    assert volumes.override_file(tmp_path, "Solo Leveling") == {}, "an empty file is a file with no keys, not no file"
    assert volumes.series_group(tmp_path, "Solo Leveling") is None, "and names no series group"


@budget(load="fs")
@pytest.mark.parametrize("text", ["series_group: [A, 'B, C']\n", "series_group: []\n", "series_group: ['']\n"])
def test_a_malformed_series_group_is_an_override_error(tmp_path, text):
    write_override(tmp_path, "Solo Leveling", text)
    with pytest.raises(volumes.OverrideError, match="series_group"):
        volumes.series_group(tmp_path, "Solo Leveling")


@budget(load="fs")
def test_an_override_name_is_read_from_the_file(tmp_path):
    write_override(tmp_path, "Solo Leveling", "name: Only I Level Up\n")
    assert volumes.override_name(tmp_path, "Solo Leveling") == "Only I Level Up", "SPEC 'Series names': name key"
    assert volumes.override_name(tmp_path, "Other") is None, "no file, no name"


@budget(load="fs")
@pytest.mark.parametrize("text", ["name: ''\n", "name: 12\n", "name: [A]\n", "name:\n"])
def test_a_malformed_override_name_is_an_override_error(tmp_path, text):
    write_override(tmp_path, "Solo Leveling", text)
    with pytest.raises(volumes.OverrideError, match="name"):
        volumes.override_name(tmp_path, "Solo Leveling")
