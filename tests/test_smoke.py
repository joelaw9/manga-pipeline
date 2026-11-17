"""The smoke command: reads only, both services tried, a failed check exits 1 (SPEC "Services", "CLI")."""

import pytest

from manga_pipeline import cli, config, smoke
from manga_pipeline.clients import kavita, suwayomi
from manga_pipeline.errors import Unreachable

from tests.fakes.kavita import FakeKavita
from tests.fakes.suwayomi import FakeSuwayomi, cbz
from tests.timing import budget

ROOT = "/kavita/library"


@pytest.fixture
def services(tmp_path, monkeypatch):
    """Provide a fake Suwayomi and a fake Kavita, given to smoke in place of the clients built from config.

    Suwayomi: 1 library manga, 2 chapters, 1 downloaded; Kavita: library 4, 1 series, 1 file.
    """
    suw = FakeSuwayomi(tmp_path / "mangas")
    manga = suw.add_manga("Re:Zero", suw.add_source("MangaDex"))
    first = suw.add_chapter(manga, "Ch.1: Start", "Group")
    suw.add_chapter(manga, "Ch.2")
    suw.enqueue([first])
    suw.finish_downloads()
    suw.writes.clear()
    folder = tmp_path / "library" / "Alpha"
    folder.mkdir(parents=True)
    (folder / "Ch.1.cbz").write_bytes(cbz())
    kav = FakeKavita({4: ROOT, 5: "/kavita/books"}, {"/kavita": str(tmp_path)})
    kav.add_series("Alpha", "Alpha")
    monkeypatch.setattr(suwayomi, "from_config", lambda _cfg: suw)
    monkeypatch.setattr(kavita, "from_config", lambda _cfg: kav)
    return suw, kav


@budget(load="fs")
def test_smoke_prints_what_both_services_answer_and_writes_nothing(services, capsys, tmp_path):
    suw, kav = services
    smoke.run(config.cached())
    out = capsys.readouterr().out
    assert "suwayomi: 1 manga in the library, 1 sources" in out
    assert '"Re:Zero" (MangaDex (EN)): 2 chapters, 1 downloaded' in out
    assert "  Group_Ch.1_ Start.cbz" in out
    assert 'kavita: 2 libraries: 4 "Library 4", 5 "Library 5"' in out
    assert "kavita: library 4 holds 1 series" in out
    assert f'"Alpha": 1 files, folder {tmp_path / "library" / "Alpha"}' in out
    assert f"{tmp_path / 'library' / 'Alpha' / 'Ch.1.cbz'}" in out
    assert "exists" in out
    assert suw.writes == [], "SPEC 'Services': smoke only reads"
    assert kav.writes == [], "SPEC 'Services': smoke only reads"


@budget(load="fs")
def test_empty_libraries_are_reported_not_a_failure(services, capsys):
    suw, kav = services
    suw.manga_rows.clear()
    kav.series_rows.clear()
    smoke.run(config.cached())
    out = capsys.readouterr().out
    assert "suwayomi: the library is empty" in out
    assert "kavita: library 4 holds 0 series" in out


@budget(load="fs")
def test_kavita_is_checked_though_suwayomi_failed(services, capsys, monkeypatch):
    suw, _ = services

    def down():
        msg = "suwayomi"
        raise Unreachable(msg, "timed out after 60 s")

    monkeypatch.setattr(suw, "library_size", down)
    with pytest.raises(smoke.SmokeError, match="suwayomi: Unreachable: suwayomi: timed out"):
        smoke.run(config.cached())
    out = capsys.readouterr().out
    assert "suwayomi: FAILED" in out, "SPEC 'Services': both are tried"
    assert "kavita: library 4 holds 1 series" in out, "SPEC 'Services': both are tried"


@budget(load="fs")
@pytest.mark.usefixtures("services")
def test_a_mapped_path_that_does_not_exist_fails_the_check(capsys, tmp_path):
    (tmp_path / "library" / "Alpha" / "Ch.1.cbz").unlink()
    with pytest.raises(smoke.SmokeError, match="1 of 1 mapped paths do not exist"):
        smoke.run(config.cached())
    assert "MISSING" in capsys.readouterr().out


@budget(load="fs")
@pytest.mark.usefixtures("services")
def test_a_library_id_kavita_does_not_have_fails_the_check():
    cfg = {**config.cached(), "kavita": {**config.cached()["kavita"], "library_id": 9}}
    with pytest.raises(smoke.SmokeError, match=r"kavita\.library_id 9 is none of Kavita's libraries"):
        smoke.run(cfg)


@budget(load="fs")
def test_a_series_without_a_usable_folder_is_reported_not_a_failure(services, capsys, tmp_path):
    _, kav = services
    other = tmp_path / "library" / "Other"
    other.mkdir()
    (other / "Ch.2.cbz").write_bytes(cbz(comicinfo="<ComicInfo><Series>Alpha</Series></ComicInfo>"))
    kav.scan_library(4)
    kav.run_scans()
    assert len(kav.series_in_library(4)) == 1, "both folders' files are 1 series"
    smoke.run(config.cached())
    assert "no usable folder" in capsys.readouterr().out


@budget(load="fs")
def test_cli_exit_codes(services, capsys, monkeypatch):
    assert cli.main(["smoke"]) == 0
    monkeypatch.setattr(services[0], "sources", lambda: 1 / 0)
    assert cli.main(["smoke"]) == 1, "SPEC 'CLI': a failed smoke check exits 1"
    assert "error: suwayomi: ZeroDivisionError" in capsys.readouterr().err


@budget(load="fs")
def test_cli_refuses_smoke_without_an_api_key(capsys):
    assert cli.main(["--set", "kavita.api_key=", "smoke"]) == 4, "SPEC 'Preflight': service settings are hard"
    assert "kavita.api_key is not set" in capsys.readouterr().err
