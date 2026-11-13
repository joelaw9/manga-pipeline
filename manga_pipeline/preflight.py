"""Preflight (SPEC "Preflight"): the run's hard and soft requirements, checked before its work.

A check is a function of the config returning the problem text, or None when the requirement holds; it turns its own
errors into problem text. A new check is a function and its Check here, plus its place in the command table's check
lists (cli.py).
"""

import logging
import os
import shutil
import sqlite3
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from manga_pipeline import config, db
from manga_pipeline.clients import suwayomi
from manga_pipeline.convert import tools, vship

log = logging.getLogger(__name__)
HARD, SOFT = "hard", "soft"
MIB = 1024 * 1024
OWNER_FILE = ".pipeline-owner"
SHARE_FOLDERS = ("downloads", "library", "staging", "retention")


@dataclass(frozen=True)
class Check:
    name: str
    level: str  # HARD: the run cannot work without it; SOFT: it can, with a warning
    run: Callable[[dict], str | None]


class PreflightFailed(RuntimeError):
    pass


def owner_file(cfg):
    """The share's owner file: it holds the id of the database that owns the share (SPEC "State")."""
    return config.abs_path(cfg["paths"]["staging"]) / OWNER_FILE


def python_version(cfg, version=sys.version_info):
    if version < (3, 13):
        return f"Python 3.13 or later is required, this is {version[0]}.{version[1]}"
    return None


def free_space(cfg):
    want = cfg["preflight"]["min_free_mb"]
    if want < 0:
        return f"preflight.min_free_mb must be 0 or more, got {want}"
    if want == 0:
        return None
    # The drive is what counts; logging.dir itself may not exist yet (console-only commands never create it).
    return _below(config.abs_path(cfg["logging"]["dir"]), want, "preflight.min_free_mb")


def _below(wanted, want, key):
    """The problem when the drive of wanted (measured at its nearest existing folder) has less than want MiB free."""
    folder = next((p for p in (wanted, *wanted.parents) if p.exists()), None)
    if folder is None:
        return f"cannot read free space at {wanted}: no part of the path exists"
    try:
        free = shutil.disk_usage(folder).free // MIB
    except OSError as e:
        return f"cannot read free space at {folder}: {e}"
    if free < want:
        return f"{free} MiB free at {folder}, below {key} {want}"
    return None


def database_location(cfg):
    try:
        database = config.abs_path(cfg["database"]["path"]).resolve()
        folders = {key: config.abs_path(cfg["paths"][key]).resolve() for key in SHARE_FOLDERS}
    except OSError as e:
        return f"cannot resolve database.path or a share folder: {e}"
    for key, folder in folders.items():
        if database.is_relative_to(folder):
            return f"database.path {database} lies under paths.{key} {folder}; it must be on local disk"
    return None


def folder_layout(cfg):
    """The folders `state reset` empties (paths.staging, paths.scratch) must hold nothing else the pipeline keeps."""
    try:
        paths = {
            key: config.abs_path(cfg["paths"][key]).resolve()
            for key in ("downloads", "library", "staging", "scratch", "retention")
        }
        kept = {
            "paths.downloads": paths["downloads"],
            "paths.library": paths["library"],
            "paths.retention": paths["retention"],
            "database.path": config.abs_path(cfg["database"]["path"]).resolve(),
            "logging.dir": config.abs_path(cfg["logging"]["dir"]).resolve(),
            "lock.dir": config.abs_path(cfg["lock"]["dir"]).resolve(),
            "the project root": config.ROOT.resolve(),
        }
    except OSError as e:
        return f"cannot resolve a configured folder: {e}"
    problems = []
    for key, other in (("staging", "scratch"), ("scratch", "staging")):
        folder = paths[key]
        for name, path in {**kept, f"paths.{other}": paths[other]}.items():
            if path.is_relative_to(folder):
                problems.append(f"paths.{key} {folder} contains {name} {path}")
        for name in ("paths.downloads", "paths.library"):
            if folder.is_relative_to(kept[name]) and folder != kept[name]:
                problems.append(f"paths.{key} {folder} lies inside {name} {kept[name]}")
    retention = paths["retention"]
    for name in ("paths.downloads", "paths.library"):
        if retention.is_relative_to(kept[name]) or kept[name].is_relative_to(retention):
            problems.append(f"paths.retention {retention} and {name} {kept[name]} overlap")
    return "; ".join(problems) or None


def _missing_folders(cfg, keys):
    """keys: paths.<key> names, or (key, subfolder) for a folder below one."""
    problems = []
    for key in keys:
        key, sub = key if isinstance(key, tuple) else (key, None)
        folder = config.abs_path(cfg["paths"][key])
        name = f"paths.{key}" if sub is None else f"the {sub} folder of paths.{key}"
        folder = folder if sub is None else folder / sub
        try:
            if not folder.is_dir():
                problems.append(f"{name} {folder} is not an existing folder")
        except OSError as e:
            problems.append(f"cannot read {name} {folder}: {e}")
    if problems:
        return f"{'; '.join(problems)} (the share is not mounted, or the folder was never made)"
    return None


def staging_folder(cfg):
    return _missing_folders(cfg, ("staging",))


def share_folders(cfg):
    missing = _missing_folders(
        cfg, ("downloads", ("downloads", suwayomi.MANGAS_FOLDER), "library", "retention", "naming_overrides")
    )
    if missing:
        return missing
    # A download is moved to retention by a rename, which cannot cross filesystems.
    downloads, retention = (config.abs_path(cfg["paths"][key]) for key in ("downloads", "retention"))
    try:
        if os.stat(downloads).st_dev != os.stat(retention).st_dev:
            return "paths.retention is not on the same filesystem as paths.downloads"
    except OSError as e:
        return f"cannot read the filesystem of paths.downloads or paths.retention: {e}"
    return None


def workflow_settings(cfg):
    problems = []
    for key, low in (
        ("workflow.attempt_limit", 1),
        ("kavita.library_id", 1),
        ("kavita.rescan_after_minutes", 1),
        ("export.retention_days", 0),
        ("convert.workers", 1),
    ):
        section, name = key.split(".")
        if cfg[section][name] < low:
            problems.append(f"{key} must be at least {low}, got {cfg[section][name]}")
    return "; ".join(problems) or None


def service_settings(cfg):
    """What the Suwayomi and Kavita clients need (SPEC "Services"). A value is never shown: one is a secret."""
    problems = []
    for section, name in (("suwayomi", "url"), ("kavita", "url")):
        value = cfg[section][name]
        parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
        if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
            problems.append(f"{section}.{name} must be an http or https URL")
    key = cfg["kavita"]["api_key"]
    if not isinstance(key, str) or not key.strip():
        problems.append(f"kavita.api_key is not set (env {config.ENV_PREFIX}_KAVITA_API_KEY)")
    path_map = cfg["paths"]["kavita_path_map"]
    if not path_map:
        problems.append("paths.kavita_path_map is empty: it maps each Kavita path prefix to its local prefix")
    elif not all(isinstance(text, str) and text for entry in path_map.items() for text in entry):
        problems.append("paths.kavita_path_map must map non-empty strings to non-empty strings")
    elif len({prefix.rstrip("/") for prefix in path_map}) < len(path_map):
        problems.append("paths.kavita_path_map has 2 keys that are the same prefix but for a trailing /")
    return "; ".join(problems) or None


def share_ownership(cfg):
    path = owner_file(cfg)
    try:
        owner = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{path} is missing: the share is not mounted, or no database adopted it (state adopt --yes)"
    except OSError as e:
        return f"cannot read {path}: {e}"
    database = config.abs_path(cfg["database"]["path"])
    if not database.exists():
        return f"{path} holds database id {owner!r}, and there is no database at {database}"
    try:
        mine = db.peek_id(cfg)  # applies nothing: a run refused here must not have upgraded the database
    except (OSError, sqlite3.Error) as e:
        return f"cannot read the database id from {database}: {e}"
    if owner != mine:
        return f"{path} holds database id {owner!r}, this database is {mine!r} (state adopt --yes moves the share)"
    return None


def image_tools(cfg):
    missing = [
        f"tools.{name} {path}"
        for name in (*tools.NAMES, "vship")
        if not (path := config.abs_path(cfg["tools"][name])).is_file()
    ]
    return f"not an existing file: {', '.join(missing)}" if missing else None


def scorer(cfg):
    try:
        vship.Scorer(config.abs_path(cfg["tools"]["vship"]))
    except (OSError, vship.VshipError) as e:
        return str(e)
    return None


def scratch_space(cfg):
    want = cfg["convert"]["min_scratch_free_mb"]
    if want < 0:
        return f"convert.min_scratch_free_mb must be 0 or more, got {want}"
    return _below(config.abs_path(cfg["paths"]["scratch"]), want, "convert.min_scratch_free_mb")


PYTHON_VERSION = Check("runtime version", HARD, python_version)
FREE_SPACE = Check("free disk space", SOFT, free_space)
DATABASE_LOCATION = Check("database location", HARD, database_location)
STAGING_FOLDER = Check("staging folder", HARD, staging_folder)
FOLDER_LAYOUT = Check("folder layout", HARD, folder_layout)
SHARE_FOLDERS_PRESENT = Check("share folders", HARD, share_folders)
WORKFLOW_SETTINGS = Check("workflow settings", HARD, workflow_settings)
SERVICE_SETTINGS = Check("service settings", HARD, service_settings)
SHARE_OWNERSHIP = Check("share ownership", HARD, share_ownership)
IMAGE_TOOLS = Check("image tools", HARD, image_tools)
SCORER = Check("scorer", HARD, scorer)
SCRATCH_SPACE = Check("scratch space", HARD, scratch_space)


def run(cfg, checks):
    """Run every check. Logs each soft problem as a warning and returns them; when any check is hard and failed,
    logs each and raises PreflightFailed naming all of them."""
    unknown = [c.name for c in checks if c.level not in (HARD, SOFT)]
    if unknown:
        raise ValueError(f"preflight check level must be HARD or SOFT: {', '.join(unknown)}")
    soft, hard = [], []
    for check in checks:
        problem = check.run(cfg)
        if problem is None:
            log.debug("%s: ok", check.name)
        elif check.level == HARD:
            log.error("%s: %s", check.name, problem)
            hard.append(f"{check.name}: {problem}")
        else:
            log.warning("%s: %s", check.name, problem)
            soft.append(f"{check.name}: {problem}")
    if hard:
        raise PreflightFailed(f"preflight failed: {'; '.join(hard)}")
    return soft
