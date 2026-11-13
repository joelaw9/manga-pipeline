"""The state commands (SPEC "State"): `state reset` clears the workflow state, `state adopt` makes this database the
share's owner. Both run under every part's lock (cli command table)."""

import logging
import shutil
from contextlib import closing

from manga_pipeline import config, db, preflight, workflow

log = logging.getLogger(__name__)


def reset(cfg):
    """Empty the workflow tables and set every series ok, then empty the staging folder (keeping the owner file) and
    the scratch folder. Downloads, the library, knowledge and sync records are never touched."""
    with closing(db.connect(cfg)) as conn:
        workflow.reset(conn)
    owner = preflight.owner_file(cfg)
    for key in ("staging", "scratch"):
        folder = config.abs_path(cfg["paths"][key])
        if not folder.exists():
            continue
        for entry in folder.iterdir():
            if entry == owner:
                continue
            # A symlink or junction is removed itself, never followed; rmtree does the same for those inside a folder.
            if entry.is_dir() and not entry.is_symlink() and not entry.is_junction():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    log.info("workflow state reset; staging and scratch emptied")


def adopt(cfg):
    """Write this database's id to the owner file. The staging folder is never created (preflight requires it), so an
    unmounted share is never written under its mount point."""
    with closing(db.connect(cfg)) as conn:
        database_id = db.database_id(conn)
    path = preflight.owner_file(cfg)
    path.write_text(database_id, encoding="utf-8")
    log.info("%s now holds database id %s", path, database_id)
