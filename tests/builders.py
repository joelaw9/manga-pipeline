"""Fixture builders: a config with its own folders and its file, series and chapter rows, fake downloads."""

import os
import time
from contextlib import closing, contextmanager
from typing import TypedDict, Unpack

import yaml

from manga_pipeline import config, db, fleet, lock, state, workflow
from manga_pipeline.db import sql

from tests import pg


class WinOSError(OSError):
    """An OSError carrying winerror on every platform, as Windows reports one (a dropped share's code)."""

    winerror: int | None


class WinFileNotFoundError(FileNotFoundError):
    """A FileNotFoundError carrying winerror on every platform, as Windows reports one (a dropped share's code)."""

    winerror: int | None


def one(conn, query, params=()):
    """Return the first column of the query's 1st row, asserting there is one."""
    row = conn.execute(query, params).fetchone()
    assert row is not None, f"a row from: {query}"
    return row[0]


def present(value):
    """value, asserting it is not None."""
    assert value is not None, "a value is present"
    return value


def problem_text(problem):
    """Return a check's problem text, asserting it names one."""
    assert problem is not None, "the check names a problem"
    return problem


class ConfigChanges(TypedDict, total=False):
    """Whole config sections a test replaces (site_cfg's changes): any of config.Config's."""

    logging: config.LoggingConfig
    lock: config.LockConfig
    preflight: config.PreflightConfig
    database: config.DatabaseConfig
    paths: config.PathsConfig
    workflow: config.WorkflowConfig
    intake: config.IntakeConfig
    series: config.SeriesConfig
    suwayomi: config.SuwayomiConfig
    kavita: config.KavitaConfig
    metadata: config.MetadataConfig
    export: config.ExportConfig
    tools: config.ToolsConfig
    convert: config.ConvertConfig
    sources: config.SourcesConfig
    validate: config.ValidateConfig
    junk: config.JunkConfig


def site_cfg(tmp_path, *, built=True, **changes: Unpack[ConfigChanges]) -> config.Config:
    """Return the test config with every path in tmp_path and this process's database for tmp_path.

    The database is built, or with no schema when not built (tests/pg.py); changes replace whole sections, and a
    replaced database section takes none.
    """
    share = tmp_path / "share"
    for key in ("downloads/mangas", "library", "staging", "retention", "candidates", "covers"):
        (share / key).mkdir(parents=True, exist_ok=True)
    (tmp_path / "overrides").mkdir(exist_ok=True)
    cfg: config.Config = {
        **config.cached(),
        "database": changes.get("database") or pg.cfg_for(pg.current().database(tmp_path.resolve(), built))["database"],
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


def share_paths(cfg: config.Config, share) -> config.Config:
    """Return cfg with every share root's folder under share (config.SHARE_ROOTS), the other paths kept."""
    paths: config.PathsConfig = {**cfg["paths"]}
    for root in config.SHARE_ROOTS:
        paths[root] = str(share / root)
    return {**cfg, "paths": paths}


def config_file(tmp_path, cfg):
    """Write cfg as tmp_path/config.yaml, for --config and subprocesses."""
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
    """Add a series row; kavita_library_id None = Kavita does not hold it.

    A new series is an admitted one (SPEC "Series identification": only those have chapters).
    """
    with db.transaction(conn):
        series_id = one(
            conn,
            "INSERT INTO series (name, origin, status, kavita_library_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, origin, status, kavita_library_id),
        )
        if origin == "new":
            conn.execute("UPDATE series SET admitted_at = '2026-01-01T00:00:00+00:00' WHERE id = %s", (series_id,))
    return series_id


def add_chapter(conn, series_id, identity, stage="discover", discovered_at=None, **columns):
    """Add a ready chapter at stage (discovered_at defaults to now).

    columns set other chapter columns (download_path among them) as given.
    """
    stamp = discovered_at or workflow.now(conn)
    with db.transaction(conn):
        chapter_id = one(
            conn,
            """INSERT INTO chapter (series_id, kind, identity, stage, status, discovered_at, updated_at)
            VALUES (%s, 'download', %s, %s, 'ready', %s, %s) RETURNING id""",
            (series_id, identity, stage, stamp, stamp),
        )
        if columns:
            assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in columns)
            conn.execute(
                sql.SQL("UPDATE chapter SET {} WHERE id = %s").format(assignments), (*columns.values(), chapter_id)
            )
    return chapter_id


def chapter(conn, chapter_id):
    """Return the chapter row of chapter_id, asserting it exists."""
    row = conn.execute("SELECT * FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
    assert row is not None, f"chapter {chapter_id} exists"
    return row


def download(cfg, series, name, data=b"fake cbz"):
    """Write a fake CBZ where Suwayomi writes downloads: <downloads>/mangas/Src (EN)/<series>/<name>.cbz.

    No Suwayomi chapter is behind it; tests/world.py builds complete downloads.
    """
    path = config.abs_path(cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / series / f"{name}.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    settle(path)
    return path


def settle(path):
    """Date the file's modified time an hour back: past intake.settle_minutes, so intake takes it."""
    then = time.time() - 3600
    os.utime(path, (then, then))
    return path


def run_part(cfg, part_module):
    """Run a part as cli.main does: its connection, its run lock and the shared idle lock, then part.run(cfg, conn)."""
    part = part_module.__name__.rsplit(".", 1)[1]
    with closing(db.connect(cfg)) as conn, lock.held(conn, cfg, [workflow.owner(part)], lock.SHARED):
        part_module.run(cfg, conn)


def claimed(conn, ctx, chapter_id):
    """Claim the chapter for ctx's run as workflow.claim claims it (owner, next count); return its row.

    For a test calling a stage.
    """
    with db.transaction(conn):
        row = conn.execute(
            """UPDATE chapter SET status = 'claimed', claimed_by = %s, claim_count = claim_count + 1
            WHERE id = %s RETURNING *""",
            (ctx.owner, chapter_id),
        ).fetchone()
    assert row is not None, f"chapter {chapter_id} is claimed"
    return row


@contextmanager
def running(cfg, *names):
    """Provide another run holding these run locks and the shared idle lock on its own connection.

    Part names stand for workflow.owner of them. It is a part running, on this machine or another.
    """
    with closing(db.connect(cfg)) as conn, lock.held(conn, cfg, [workflow.owner(n) for n in names], lock.SHARED):
        yield conn
