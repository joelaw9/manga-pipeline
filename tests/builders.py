"""Fixture builders: a config with its own folders and its file, series and chapter rows, fake downloads."""

import os
import time
from contextlib import closing, contextmanager
from typing import TypedDict, Unpack

import yaml

from manga_pipeline import config, db, fleet, lock, names, state, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import episodes, platforms
from manga_pipeline.metadata.records import SeriesRecords
from manga_pipeline.replies import kavita
from manga_pipeline.replies import sources as source_replies

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


def kavita_series(series_id: int, name: str) -> kavita.Series:
    """Return a Kavita series DTO as Kavita sends it: its own name in every name field, no ids, no cover."""
    return {
        "id": series_id,
        "name": name,
        "localizedName": name,
        "originalName": name,
        "libraryId": 1,
        "coverImage": None,
        "aniListId": 0,
        "malId": 0,
        "mangaBakaId": 0,
        "nameLocked": False,
        "sortName": name,
        "sortNameLocked": False,
        "localizedNameLocked": False,
        "coverImageLocked": False,
        "cbrId": 0,
        "comicVineId": None,
        "hardcoverId": None,
        "metronId": 0,
        "metadataProviderOverride": None,
    }


class AniListFields(TypedDict, total=False):
    """The fields of an AniList media a test gives (anilist_media): any of replies.sources.AniListMedia's."""

    id: int
    idMal: int | None
    format: str | None
    status: str | None
    title: source_replies.AniListTitle | None
    synonyms: list[str] | None
    description: str | None
    genres: list[str] | None
    tags: list[source_replies.AniListTag] | None
    coverImage: source_replies.AniListCover | None
    startDate: source_replies.AniListDate | None
    staff: source_replies.AniListStaffConnection | None
    externalLinks: list[source_replies.AniListLink] | None


def anilist_media(**fields: Unpack[AniListFields]) -> source_replies.AniListMedia:
    """Return an AniList media as AniList sends it: media 1, a manga, every field the test does not give null."""
    return {
        "id": 1,
        "idMal": None,
        "format": "MANGA",
        "status": None,
        "title": None,
        "synonyms": None,
        "description": None,
        "genres": None,
        "tags": None,
        "coverImage": None,
        "startDate": None,
        "staff": None,
        "externalLinks": None,
        **fields,
    }


def series_records(
    *,
    mangabaka: source_replies.MangaBakaSeries | None = None,
    mangaupdates: source_replies.MangaUpdatesSeries | None = None,
    mangadex: source_replies.MangaDexManga | None = None,
    anilist: source_replies.AniListMedia | None = None,
) -> SeriesRecords:
    """Return a series' records as a check stores them: None for a source the test gives none."""
    return {"mangabaka": mangabaka, "mangaupdates": mangaupdates, "mangadex": mangadex, "anilist": anilist}


def episode_record(kind: str, chapter: str | None, title: str | None, number: int = 0) -> episodes.EpisodeRecord:
    """Return an episode record of a platform list (episodes.build's), with no marker, date or id."""
    return {
        "number": number,
        "chapter": chapter,
        "raw_title": title or "",
        "title": title,
        "kind": kind,
        "season_marker": None,
        "date": None,
        "id": None,
    }


def platform_list(eps: list[episodes.EpisodeRecord]) -> platforms.PlatformList:
    """Return a stored Webtoons list of these episode records, with no season ranges and unread coverage."""
    return {
        "name": platforms.WEBTOONS,
        "ref": "webtoon:1",
        "episodes": eps,
        "coverage": {"status": "unknown", "reason": "unread"},
        "seasons": [],
    }


def present(value):
    """value, asserting it is not None."""
    assert value is not None, "a value is present"
    return value


def db_row(**columns: object) -> db.Row:
    """Return a db.Row of these columns, as a query returns it: a stored row's stand-in (a junk decision, say)."""
    row_class: type[db.Row] = type("Row", (db.Row,), {"__slots__": (), "_keys": tuple(columns)})
    return row_class(columns.values())


def scope_cfg(library_id: int, only: list[str]) -> config.Config:
    """Return the test config with this Kavita library and series.only: what the scope rule reads."""
    cfg = config.cached()
    return {**cfg, "kavita": {**cfg["kavita"], "library_id": library_id}, "series": {**cfg["series"], "only": only}}


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
    notify: config.NotifyConfig
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


def loaded_cfg(tmp_path, data) -> config.Config:
    """Return data (a whole config as plain mappings) written as a config file and loaded as config.load loads one.

    For values the example's check leaves open, which config.Config does not describe: a secret's of any type, a
    mapping's contents, a list's elements (an unquoted number in series.only, say).
    """
    return config.load(config_file(tmp_path, data))


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

    columns set other chapter columns (download_path among them) as given; a chapter past discover carries its number:
    columns' number=, else its identity's (identity_number).
    """
    stamp = discovered_at or workflow.now(conn)
    if stage != "discover" and "number" not in columns:
        columns = {"number": identity_number(identity), **columns}
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


def identity_number(identity):
    """Return the number discover writes with an identity (names.identity's); a download past discover has one."""
    return float(present(names.read_identity(identity)[3]))


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
