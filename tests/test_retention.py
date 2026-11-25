"""retention: removed downloads go through the retention folder and leave it after the period (SPEC "Publishing")."""

import io
import os
import time
import zipfile

from manga_pipeline import archive, config, retention

from tests.builders import site_cfg
from tests.timing import budget


def a_download(cfg, source="Src (EN)", title="Title", name="ch1.cbz"):
    path = config.abs_path(cfg["paths"]["downloads"]) / "mangas" / source / title / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cbz")
    return path


def with_days(cfg: config.Config, days) -> config.Config:
    return {**cfg, "export": {**cfg["export"], "retention_days": days}}


@budget(load="fs")
def test_put_moves_the_download_under_its_source_and_title_and_touches_it(tmp_path):
    cfg = with_days(site_cfg(tmp_path), 7)
    download = a_download(cfg)
    os.utime(download, (1, 1))
    retention.put(cfg, download)
    kept = config.abs_path(cfg["paths"]["retention"]) / "Src (EN)" / "Title" / "ch1.cbz"
    assert not download.exists(), "the download is moved, not copied"
    assert kept.read_bytes() == b"cbz", "the download is moved, not copied"
    assert kept.stat().st_mtime > time.time() - 60, "the period runs from the move"


@budget(load="fs")
def test_put_keeps_every_download_of_1_name(tmp_path):
    cfg = with_days(site_cfg(tmp_path), 7)
    for content in (b"first", b"second", b"third"):
        download = a_download(cfg)
        download.write_bytes(content)
        retention.put(cfg, download)
    folder = config.abs_path(cfg["paths"]["retention"]) / "Src (EN)" / "Title"
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == {
        "ch1.cbz": b"first",
        "ch1 (2).cbz": b"second",
        "ch1 (3).cbz": b"third",
    }, "a later rejected download of the same name never replaces an earlier one"


@budget(load="fs")
def test_put_with_period_0_deletes_and_a_missing_download_counts_as_removed(tmp_path):
    cfg = with_days(site_cfg(tmp_path), 0)
    download = a_download(cfg)
    retention.put(cfg, download)
    assert not download.exists()
    assert not any(config.abs_path(cfg["paths"]["retention"]).iterdir()), "period 0 keeps nothing"
    retention.put(cfg, download)  # gone already: a crash after the delete, before the outcome


@budget(load="fs")
def test_purge_deletes_only_files_past_the_period_and_their_emptied_folders(tmp_path):
    cfg = with_days(site_cfg(tmp_path), 7)
    old, new = a_download(cfg, title="Old"), a_download(cfg, title="New")
    retention.put(cfg, old)
    retention.put(cfg, new)
    root = config.abs_path(cfg["paths"]["retention"])
    stale = root / "Src (EN)" / "Old" / "ch1.cbz"
    old = time.time() - 8 * 86400
    os.utime(stale, (old, old))
    assert retention.purge(cfg) == 1
    assert not (root / "Src (EN)" / "Old").exists(), "an emptied folder goes too"
    assert (root / "Src (EN)" / "New" / "ch1.cbz").exists(), "a file inside its period stays"
    assert root.exists(), "the retention folder itself stays"


def page_cbz(page):
    """Return a 1-page CBZ holding page (bytes)."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("001.png", page)
    return data.getvalue()


@budget(load="fs")
def test_find_returns_the_kept_file_of_a_download_by_its_content_digest(tmp_path):
    cfg = with_days(site_cfg(tmp_path), 7)
    for page in (b"first", b"second"):
        download = a_download(cfg)
        download.write_bytes(page_cbz(page))
        retention.put(cfg, download)
    second = archive.pages_digest(config.abs_path(cfg["paths"]["retention"]) / "Src (EN)" / "Title" / "ch1 (2).cbz")
    kept = retention.find(cfg, download, second)
    assert kept is not None, "a download kept under a taken name is found by its pages"
    assert kept.name == "ch1 (2).cbz", "a download kept under a taken name is found by its pages"
    assert retention.find(cfg, download, "c" * 64) is None, "no kept file has those pages"
