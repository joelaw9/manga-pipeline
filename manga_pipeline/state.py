"""The state commands (SPEC "State"): `state reset` clears the workflow state, `state adopt` makes this database the
share's owner. Both run under every part's lock (cli command table).
"""

import logging
import shutil

from manga_pipeline import config, db, fleet, preflight, workflow

log = logging.getLogger(__name__)


def reset(cfg, conn):
    """Empty the workflow tables and set every series ok, then empty the staging folder (keeping the owner file and
    the staged files of queued rewrites: SPEC "Retroactive updates"), the scratch folder and the junk candidates folder.
    Downloads, the library, knowledge and sync records are never touched.
    """
    workflow.reset(conn)
    queued = {
        config.to_local(cfg, r[0])
        for r in conn.execute("SELECT pending_staged FROM published_file WHERE pending_staged IS NOT NULL")
    }
    owner = preflight.owner_file(cfg)
    for key in ("staging", "scratch", "candidates"):
        folder = config.abs_path(cfg["paths"][key])
        if not folder.exists():
            continue
        for entry in folder.iterdir():
            if entry == owner or entry in queued:
                continue
            # A symlink or junction is removed itself, never followed; rmtree does the same for those inside a folder.
            if entry.is_dir() and not entry.is_symlink() and not entry.is_junction():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    log.info("workflow state reset; staging, scratch and candidates emptied")


def adopt(cfg, conn):
    """Write this database's id to the owner file. The staging folder is never created (preflight requires it), so an
    unmounted share is never written under its mount point.
    """
    database_id = db.database_id(conn)
    path = preflight.owner_file(cfg)
    path.write_text(database_id, encoding="utf-8")
    log.info("%s now holds database id %s", path, database_id)


def version(cfg, conn):
    """Store this machine's digest as the fleet's version (SPEC "State"): run once per deploy or change of a shared
    setting, while nothing else runs (the exclusive lock).
    """
    with db.transaction(conn):
        fleet.store(conn, cfg)
    log.info("this machine's program, shared settings, packages and tools are now the fleet's version")
