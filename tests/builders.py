"""Fixture builders: a config with its own folders and its file, series and chapter rows, fake downloads."""

import yaml

from manga_pipeline import config, db, state, workflow


def site_cfg(tmp_path, **changes):
    """The test config with the database and every path in tmp_path; changes replace whole sections."""
    share = tmp_path / "share"
    for key in ("downloads/mangas", "library", "staging", "retention"):
        (share / key).mkdir(parents=True, exist_ok=True)
    (tmp_path / "overrides").mkdir(exist_ok=True)
    cfg = {
        **config.cached(),
        "database": {"path": str(tmp_path / "pipeline.sqlite3")},
        "paths": {
            "downloads": str(share / "downloads"),
            "library": str(share / "library"),
            "staging": str(share / "staging"),
            "retention": str(share / "retention"),
            "scratch": str(tmp_path / "scratch"),
            "covers": str(tmp_path / "covers"),
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
    state.adopt(cfg)
    return cfg


def add_series(conn, name, origin="new", status="ok", kavita_library_id=None):
    """A series row; kavita_library_id None = Kavita does not hold it."""
    with db.transaction(conn):
        return conn.execute(
            "INSERT INTO series (name, origin, status, kavita_library_id) VALUES (?, ?, ?, ?) RETURNING id",
            (name, origin, status, kavita_library_id),
        ).fetchone()[0]


def add_chapter(conn, series_id, identity, stage="discover", discovered_at=None, download_path=None, **columns):
    """A ready chapter at stage (discovered_at defaults to now); columns set other chapter columns as given."""
    stamp = discovered_at or workflow.now()
    with db.transaction(conn):
        chapter_id = conn.execute(
            """INSERT INTO chapter (series_id, kind, identity, download_path, stage, status, discovered_at, updated_at)
            VALUES (?, 'download', ?, ?, ?, 'ready', ?, ?) RETURNING id""",
            (series_id, identity, download_path, stage, stamp, stamp),
        ).fetchone()[0]
        if columns:
            assignments = ", ".join(f"{name} = ?" for name in columns)
            conn.execute(f"UPDATE chapter SET {assignments} WHERE id = ?", (*columns.values(), chapter_id))
    return chapter_id


def chapter(conn, chapter_id):
    return conn.execute("SELECT * FROM chapter WHERE id = ?", (chapter_id,)).fetchone()


def download(cfg, series, name, data=b"fake cbz"):
    """A fake CBZ where Suwayomi writes downloads: <downloads>/mangas/Src (EN)/<series>/<name>.cbz (no Suwayomi
    chapter behind it; tests/world.py builds complete downloads)."""
    path = config.abs_path(cfg["paths"]["downloads"]) / "mangas" / "Src (EN)" / series / f"{name}.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
