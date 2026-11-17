"""Stored paths (SPEC "State", "Config"): every path the database holds is <share root>/<path below it> with /
separators, and each machine maps it by its own configured roots, so a database one machine filled is worked by
another that mounts the share elsewhere and writes its roots otherwise.
"""

import json
import sys
from contextlib import closing
from pathlib import Path
from typing import LiteralString

import pytest

from manga_pipeline import config, db, export, intake, process
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import sources

from tests.builders import run_part
from tests.timing import budget
from tests.world import World

if sys.platform == "win32":
    import _winapi

# Every stored path of the step 0 inventory (agent-workspace/progress/step0e-path-inventory.md, items 1-8).
STORED: list[LiteralString] = [
    "SELECT download_path FROM chapter",
    "SELECT staged_path FROM chapter",
    "SELECT library_path FROM chapter",
    "SELECT library_path FROM published_file",
    "SELECT folder FROM series",
    "SELECT path FROM unmatched_download",
    "SELECT redownload::jsonb #>> '{expected,path}' FROM chapter",
    "SELECT jsonb_array_elements(redownload::jsonb -> 'remove') ->> 'path' FROM chapter WHERE redownload IS NOT NULL",
]


def stored_paths(cfg):
    with closing(db.connect(cfg)) as conn:
        return [row[0] for sql in STORED for row in conn.execute(sql) if row[0] is not None]


def is_stored_form(text):
    root, _, below = text.partition("/")
    return root in config.SHARE_ROOTS and "\\" not in text and ":" not in text and ".." not in below.split("/")


@pytest.mark.parametrize(
    ("path", "stored"),
    [
        ("{share}/downloads/mangas/Src (EN)/T/Ch 1.cbz", "downloads/mangas/Src (EN)/T/Ch 1.cbz"),
        ("{share}/library/S/Vol.1 Ch.1.cbz", "library/S/Vol.1 Ch.1.cbz"),
        ("{share}/library", "library"),
    ],
)
@budget(load="fs")
def test_a_path_is_stored_by_its_root_and_maps_back(tmp_path, path, stored):
    cfg = {"paths": {root: str(tmp_path / "share" / root) for root in config.SHARE_ROOTS}}
    path = Path(path.format(share=tmp_path / "share"))
    assert config.to_stored(cfg, path) == stored
    assert config.to_local(cfg, stored) == path


@budget(load="fs")
def test_a_path_under_no_root_and_a_stored_text_naming_none_are_refused(tmp_path):
    cfg = {"paths": {root: str(tmp_path / "share" / root) for root in config.SHARE_ROOTS}}
    with pytest.raises(config.PathError, match="lies under no share root"):
        config.to_stored(cfg, tmp_path / "elsewhere" / "a.cbz")
    for text in ("nowhere/a.cbz", "library/../a.cbz", "library\\a.cbz", str(tmp_path / "share" / "library" / "a")):
        with pytest.raises(config.PathError, match="is not <share root>/<path>"):
            config.to_local(cfg, text)


def mount(target, link):
    """A second name for the share, as another machine mounts it."""
    if sys.platform == "win32":
        _winapi.CreateJunction(str(target), str(link))
    else:
        Path(link).symlink_to(target, target_is_directory=True)


@budget(load="db", seconds=6)
def test_a_database_filled_under_1_mount_is_worked_under_another_and_back(tmp_path, monkeypatch):
    world = World(tmp_path)
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: world.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: world.sources)
    world.kavita_series("Alpha")
    manga = world.manga("Alpha")
    for n in (2, 3):
        world.download(manga, f"Chapter {n}")
    here = world.cfg
    share = tmp_path / "share"
    mount(share, tmp_path / "elsewhere")
    # The other machine: the share under another name, its roots written with the other separator.
    there = {
        **here,
        "paths": {
            key: (
                value.replace(str(share), str(tmp_path / "elsewhere")).replace("\\", "/")
                if isinstance(value, str)
                else value
            )
            for key, value in here["paths"].items()
        },
    }
    assert there["paths"]["library"] != here["paths"]["library"]
    run_part(here, intake)
    run_part(there, process)
    run_part(here, export)
    world.kavita.run_scans()
    run_part(here, export)
    with closing(db.connect(here)) as conn:
        published = [r[0] for r in conn.execute("SELECT library_path FROM published_file WHERE origin = 'pipeline'")]
        states = sorted(conn.execute("SELECT DISTINCT stage, status FROM chapter").fetchall())
    assert sorted(published) == ["library/Alpha/Ch.2.cbz", "library/Alpha/Ch.3.cbz"]
    assert [tuple(s) for s in states] == [("confirm", "done")], "every chapter was worked across the 2 mounts"
    assert all((share / "library" / "Alpha" / f"Ch.{n}.cbz").exists() for n in (2, 3))
    bad = [p for p in stored_paths(here) if not is_stored_form(p)]
    assert not bad, f"stored paths not in <root>/<path> form: {bad}"


@budget(load="db", seconds=3)
def test_no_stored_path_is_absolute_after_a_full_run(tmp_path, monkeypatch):
    world = World(tmp_path)
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: world.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: world.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: world.sources)
    world.kavita_series("Alpha")
    manga = world.manga("Alpha")
    world.download(manga, "Chapter 2")
    (config.abs_path(world.cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / "Unknown").mkdir(parents=True)
    (
        config.abs_path(world.cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / "Unknown" / "Chapter 1.cbz"
    ).write_bytes(b"not taken")
    for part in (intake, process, export):
        run_part(world.cfg, part)
    found = stored_paths(world.cfg)
    assert any(p.startswith("downloads/") for p in found)
    assert any(p.startswith("library/") for p in found)
    assert all(is_stored_form(p) for p in found), [p for p in found if not is_stored_form(p)]
    assert json.dumps(found)  # all text
