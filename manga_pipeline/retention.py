"""The retention folder (SPEC "Publishing"): every download the pipeline removes goes through here.

`put` moves a download to <paths.retention>/<source>/<title>/<file> (a taken name: the first free
"<stem> (<n>)<suffix>") and touches it, or deletes it when
export.retention_days is 0; `purge` deletes what is older than the period. Both are safe to run again after a crash.
"""

import errno
import logging
import os
import time

from manga_pipeline import config, errors

log = logging.getLogger(__name__)
DAY = 86400


def put(cfg, download):
    """Remove a download (a Path under <paths.downloads>/mangas) the retention way. A download already gone counts as
    removed (a crash after the move, before its outcome was recorded)."""
    if not errors.exists(download):
        log.info("retention: %s is already gone", download)
        return
    if cfg["export"]["retention_days"] == 0:
        download.unlink()
        log.info("retention: deleted %s", download)
        return
    folder = config.abs_path(cfg["paths"]["retention"]) / download.parent.parent.name / download.parent.name
    folder.mkdir(parents=True, exist_ok=True)
    target, n = folder / download.name, 1
    while errors.exists(target):  # an earlier download of the same name is kept: this one is "<stem> (2)<suffix>", ...
        n += 1
        target = folder / f"{download.stem} ({n}){download.suffix}"
    os.utime(download)  # the period runs from the move, not from the download; touched first, so a crash keeps it
    os.replace(download, target)
    log.info("retention: moved %s to %s", download, target)


def purge(cfg, now=None):
    """Delete retained files older than export.retention_days by modification time, and the folders that leaves
    empty. Returns how many files were deleted. A folder that cannot be read raises."""
    root = config.abs_path(cfg["paths"]["retention"])
    cutoff = (time.time() if now is None else now) - cfg["export"]["retention_days"] * DAY
    deleted = 0
    for folder, _, files in os.walk(root, topdown=False, onerror=_raise):
        for name in files:
            path = os.path.join(folder, name)
            if os.stat(path).st_mtime < cutoff:
                os.unlink(path)
                deleted += 1
        if folder != str(root) and not os.listdir(folder):
            try:
                os.rmdir(folder)
            except OSError as e:
                if e.errno != errno.ENOTEMPTY:  # a put into it since the listing keeps it
                    raise
    if deleted:
        log.info("retention: deleted %d file(s) past %d day(s)", deleted, cfg["export"]["retention_days"])
    return deleted


def _raise(error):
    raise error
