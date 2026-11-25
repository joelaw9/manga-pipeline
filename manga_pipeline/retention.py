"""The retention folder (SPEC "Publishing"): every download the pipeline removes goes through here.

`put` moves a download to <paths.retention>/<source>/<title>/<file> (a taken name: the first free
"<stem> (<n>)<suffix>") and touches it, or deletes it when
export.retention_days is 0; `purge` deletes what is older than the period. Both are safe to run again after a crash.
"""

import errno
import glob
import logging
import os
import time
from pathlib import Path
from typing import NoReturn

from manga_pipeline import archive, config, errors

log = logging.getLogger(__name__)
DAY = 86400


def put(cfg: config.Config, download: Path) -> None:
    """Remove a download (a Path under <paths.downloads>/mangas) the retention way.

    A download already gone counts as removed (a crash after the move, before its outcome was recorded).
    """
    if not errors.exists(download):
        log.info("retention: %s is already gone", download)
        return
    if cfg["export"]["retention_days"] == 0:
        download.unlink()
        log.info("retention: deleted %s", download)
        return
    folder = _folder(cfg, download)
    folder.mkdir(parents=True, exist_ok=True)
    target, n = folder / download.name, 1
    while errors.exists(target):  # an earlier download of the same name is kept: this one is "<stem> (2)<suffix>", ...
        n += 1
        target = folder / f"{download.stem} ({n}){download.suffix}"
    os.utime(download)  # the period runs from the move, not from the download; touched first, so a crash keeps it
    download.replace(target)
    log.info("retention: moved %s to %s", download, target)


def find(cfg: config.Config, download: Path, content_digest: str) -> Path | None:
    """Return the file put kept a download as: its name or a taken "<stem> (<n>)<suffix>" with this content digest.

    None when no such file is kept (gone, purged, or an archive that cannot be read).
    """
    folder = _folder(cfg, download)
    if not errors.exists(folder):
        return None
    taken = sorted(folder.glob(f"{glob.escape(download.stem)} (*){glob.escape(download.suffix)}"))
    kept = (p for p in [folder / download.name, *taken] if errors.exists(p))
    return next((p for p in kept if archive.digest_or_none(p) == content_digest), None)


def _folder(cfg: config.Config, download: Path) -> Path:
    """Return where put keeps a download: <paths.retention>/<source>/<title>/."""
    return config.abs_path(cfg["paths"]["retention"]) / download.parent.parent.name / download.parent.name


def put_library(cfg: config.Config, file: Path) -> None:
    """Keep a library file about to be replaced (SPEC "Retroactive updates") the retention way.

    The way is a hard link to it under <paths.retention>/library-files/<its folder below the library root>/ (a free
    "(n)" name, as put picks), its retention period starting now; the library keeps the file until the replace.
    Nothing when export.retention_days is 0.
    """
    if cfg["export"]["retention_days"] == 0:
        return
    root = config.abs_path(cfg["paths"]["library"])
    folder = config.abs_path(cfg["paths"]["retention"]) / "library-files" / file.parent.relative_to(root)
    folder.mkdir(parents=True, exist_ok=True)
    target, n = folder / file.name, 1
    while errors.exists(target):
        n += 1
        target = folder / f"{file.stem} ({n}){file.suffix}"
    os.link(file, target)
    os.utime(target)  # 1 inode with the library file until the replace: the period runs from now
    log.info("retention: kept %s as %s", file, target)


def purge(cfg: config.Config, now: float | None = None) -> int:
    """Delete retained files older than export.retention_days by modification time, and the folders that leaves empty.

    Returns how many files were deleted. A folder that cannot be read raises.
    """
    root = config.abs_path(cfg["paths"]["retention"])
    cutoff = (time.time() if now is None else now) - cfg["export"]["retention_days"] * DAY
    deleted = 0
    for folder, _, files in root.walk(top_down=False, on_error=_raise):
        for name in files:
            path = folder / name
            if path.is_dir():  # a link to a folder: Path.walk lists it as a file; never purged, its folder kept
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        if folder != root and not any(folder.iterdir()):
            try:
                folder.rmdir()
            except OSError as e:
                if e.errno != errno.ENOTEMPTY:  # a put into it since the listing keeps it
                    raise
    if deleted:
        log.info("retention: deleted %d file(s) past %d day(s)", deleted, cfg["export"]["retention_days"])
    return deleted


def _raise(error: OSError) -> NoReturn:
    raise error
