"""retention: removed downloads go through the retention folder and leave it after the period (SPEC "Publishing")."""

import os
import time

from manga_pipeline import config, retention

from tests.builders import site_cfg
from tests.timing import budget


def a_download(cfg, source="Src (EN)", title="Title", name="ch1.cbz"):
    path = config.abs_path(cfg["paths"]["downloads"]) / "mangas" / source / title / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cbz")
    return path


def with_days(cfg, days):
    return {**cfg, "export": {"retention_days": days}}


@budget(load="fs")
def test_put_moves_the_download_under_its_source_and_title_and_touches_it(tmp_path):
    cfg = with_days(site_cfg(tmp_path), 7)
    download = a_download(cfg)
    os.utime(download, (1, 1))
    retention.put(cfg, download)
    kept = config.abs_path(cfg["paths"]["retention"]) / "Src (EN)" / "Title" / "ch1.cbz"
    assert not download.exists() and kept.read_bytes() == b"cbz", "the download is moved, not copied"
    assert kept.stat().st_mtime > time.time() - 60, "the period runs from the move"


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
    os.utime(stale, (time.time() - 8 * 86400,) * 2)
    assert retention.purge(cfg) == 1
    assert not (root / "Src (EN)" / "Old").exists(), "an emptied folder goes too"
    assert (root / "Src (EN)" / "New" / "ch1.cbz").exists(), "a file inside its period stays"
    assert root.exists(), "the retention folder itself stays"
