"""Fixture builders: a config with its own folders and its file, series and chapter rows, fake downloads."""

from contextlib import closing, contextmanager

import yaml

from manga_pipeline import config, db, fleet, lock, state, workflow

from tests import pg


def site_cfg(tmp_path, built=True, **changes):
    """The test config with every path in tmp_path and this process's database for tmp_path (built, or with no
    schema when not built: tests/pg.py); changes replace whole sections, and a replaced database section takes none."""
    share = tmp_path / "share"
    for key in ("downloads/mangas", "library", "staging", "retention", "candidates", "covers"):
        (share / key).mkdir(parents=True, exist_ok=True)
    (tmp_path / "overrides").mkdir(exist_ok=True)
    cfg = {
        **config.cached(),
        "database": changes.get("database") or pg.cfg_for(pg.process.database(tmp_path.resolve(), built))["database"],
        "paths": {
            "downloads": str(share / "downloads"),
            "library": str(share / "library"),
            "staging": str(share / "staging"),
            "retention": str(share / "retention"),
            "scratch": str(tmp_path / "scratch"),
            "covers": str(share / "covers"),
            "candidates": str(share / "candidates"),
            "naming_overrides": str(tmp_path / "overrides"),
            "kavita_path_map": {"/kavita": str(share)},
        },
    }
    return {**cfg, **changes}


def config_file(tmp_path, cfg):
    """cfg written as tmp_path/config.yaml, for --config and subprocesses."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def adopted(tmp_path, **changes):
    """site_cfg with its database built and owning the share."""
    cfg = site_cfg(tmp_path, **changes)
    with closing(db.connect(cfg)) as conn:
        state.adopt(cfg, conn)
        with db.transaction(conn):
            fleet.store(conn, cfg)  # as state version --yes: this machine's digest is the fleet's
    return cfg


def add_series(conn, name, origin="new", status="ok", kavita_library_id=None):
    """A series row; kavita_library_id None = Kavita does not hold it."""
    with db.transaction(conn):
        return conn.execute(
            "INSERT INTO series (name, origin, status, kavita_library_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, origin, status, kavita_library_id),
        ).fetchone()[0]


def add_chapter(conn, series_id, identity, stage="discover", discovered_at=None, download_path=None, **columns):
    """A ready chapter at stage (discovered_at defaults to now); columns set other chapter columns as given."""
    stamp = discovered_at or workflow.now(conn)
    with db.transaction(conn):
        chapter_id = conn.execute(
            """INSERT INTO chapter (series_id, kind, identity, download_path, stage, status, discovered_at, updated_at)
            VALUES (%s, 'download', %s, %s, %s, 'ready', %s, %s) RETURNING id""",
            (series_id, identity, download_path, stage, stamp, stamp),
        ).fetchone()[0]
        if columns:
            assignments = ", ".join(f"{name} = %s" for name in columns)
            conn.execute(f"UPDATE chapter SET {assignments} WHERE id = %s", (*columns.values(), chapter_id))
    return chapter_id


def chapter(conn, chapter_id):
    return conn.execute("SELECT * FROM chapter WHERE id = %s", (chapter_id,)).fetchone()


def download(cfg, series, name, data=b"fake cbz"):
    """A fake CBZ where Suwayomi writes downloads: <downloads>/mangas/Src (EN)/<series>/<name>.cbz (no Suwayomi
    chapter behind it; tests/world.py builds complete downloads)."""
    path = config.abs_path(cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / series / f"{name}.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run_part(cfg, part_module):
    """Run a part as cli.main does: its connection, its run lock and the shared idle lock, then part.run(cfg, conn)."""
    part = part_module.__name__.rsplit(".", 1)[1]
    with closing(db.connect(cfg)) as conn, lock.held(conn, cfg, [workflow.owner(part)], lock.SHARED):
        part_module.run(cfg, conn)


def claimed(conn, ctx, chapter_id):
    """The chapter claimed by ctx's run as workflow.claim claims it (owner, next count), for a test calling a stage."""
    with db.transaction(conn):
        return conn.execute(
            """UPDATE chapter SET status = 'claimed', claimed_by = %s, claim_count = claim_count + 1
            WHERE id = %s RETURNING *""",
            (ctx.owner, chapter_id),
        ).fetchone()


@contextmanager
def running(cfg, *names):
    """Another run holding these run locks (part names stand for workflow.owner of them) and the shared idle lock on
    its own connection: a part running, on this machine or another."""
    with closing(db.connect(cfg)) as conn, lock.held(conn, cfg, [workflow.owner(n) for n in names], lock.SHARED):
        yield conn
