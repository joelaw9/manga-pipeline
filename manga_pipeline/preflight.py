"""Preflight (SPEC "Preflight"): the run's hard and soft requirements, checked before its work.

A check is a function of the config returning the problem text, or None when the requirement holds; it turns its own
errors into problem text. A new check is a function and its Check here, plus its place in the command table's check
lists (cli.py).
"""

import logging
import shutil
import sys
import urllib.parse
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

from manga_pipeline import config, db, fleet
from manga_pipeline.clients import suwayomi
from manga_pipeline.convert import tools, vship
from manga_pipeline.junk import ocr

if sys.platform == "win32":
    import winreg

log = logging.getLogger(__name__)
HARD, SOFT = "hard", "soft"
MIB = 1024 * 1024
OWNER_FILE = ".pipeline-owner"
# The share folders a run needs (SPEC "Preflight"): the share roots of SPEC "State".
SHARE_FOLDERS = ("downloads", "library", "staging", "retention", "candidates", "covers")
LONG_PATHS_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"


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


def python_version(_cfg, version=sys.version_info):
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


def folder_layout(cfg):
    """The folders `state reset` empties (paths.staging, paths.scratch, paths.candidates) must hold nothing else the
    pipeline keeps."""
    try:
        paths = {
            key: config.abs_path(cfg["paths"][key]).resolve()
            for key in (
                "downloads",
                "library",
                "staging",
                "scratch",
                "candidates",
                "retention",
                "covers",
                "naming_overrides",
            )
        }
        kept = {
            "paths.downloads": paths["downloads"],
            "paths.library": paths["library"],
            "paths.retention": paths["retention"],
            "paths.covers": paths["covers"],
            "paths.naming_overrides": paths["naming_overrides"],
            "logging.dir": config.abs_path(cfg["logging"]["dir"]).resolve(),
            "the project root": config.ROOT.resolve(),
        }
    except OSError as e:
        return f"cannot resolve a configured folder: {e}"
    problems = []
    emptied = ("staging", "scratch", "candidates")
    for key in emptied:
        folder = paths[key]
        others = {f"paths.{o}": paths[o] for o in emptied if o != key}
        for name, path in {**kept, **others}.items():
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
        cfg, ("downloads", ("downloads", suwayomi.MANGAS_FOLDER), "library", "retention", "candidates", "covers")
    )
    if missing:
        return missing
    # A download is moved to retention by a rename, and a rewritten library file kept there by a hard link (SPEC
    # "Retroactive updates"): neither crosses filesystems.
    retention = config.abs_path(cfg["paths"]["retention"])
    for key in ("downloads", "library"):
        try:
            if config.abs_path(cfg["paths"][key]).stat().st_dev != retention.stat().st_dev:
                return f"paths.retention is not on the same filesystem as paths.{key}"
        except OSError as e:
            return f"cannot read the filesystem of paths.{key} or paths.retention: {e}"
    return None


def workflow_settings(cfg):
    problems = []
    for key, low in (
        ("workflow.attempt_limit", 1),
        ("intake.settle_minutes", 1),
        ("kavita.library_id", 1),
        ("kavita.rescan_after_minutes", 1),
        ("export.retention_days", 0),
        ("export.rewrites_open", 1),
        ("convert.workers", 1),
        ("junk.review_limit", 1),
        ("junk.review_batch", 1),
        *((f"metadata.{name}", 1) for name in cfg["metadata"]),
    ):
        section, name = key.split(".")
        if cfg[section][name] < low:
            problems.append(f"{key} must be at least {low}, got {cfg[section][name]}")
    metadata = cfg["metadata"]
    if metadata["complete_months"] <= metadata["recent_months"]:
        problems.append("metadata.complete_months must be above metadata.recent_months")
    if not all(isinstance(name, str) for name in cfg["series"]["only"]):  # scope compares names as text
        problems.append(f"series.only must list series names as text, got {cfg['series']['only']!r}")
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
    try:
        mine = db.peek_id(cfg)  # applies nothing: a run refused here must not have upgraded the database
    except db.NoDatabase as e:
        return f"{path} holds database id {owner!r}, and there is {e}"
    except (db.CannotConnect, db.Error) as e:
        return f"cannot read the database id: {e}"
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


def claude_settings(cfg):
    """junk review can start Claude: junk.claude_path resolves to an executable, junk.claude_token is set."""
    problems = []
    if shutil.which(cfg["junk"]["claude_path"]) is None:
        problems.append(f"junk.claude_path {cfg['junk']['claude_path']!r} is no executable (path or name on PATH)")
    token = cfg["junk"]["claude_token"]
    if not isinstance(token, str) or not token.strip():
        problems.append("junk.claude_token is not a non-blank string (set MANGA_PIPELINE_JUNK_CLAUDE_TOKEN)")
    return "; ".join(problems) or None


def ocr_device(cfg):
    """junk.ocr_device is known and its onnxruntime provider loads (SPEC "Preflight")."""
    return ocr.provider_problem(cfg["junk"]["ocr_device"])


def junk_word_lists(cfg):
    """junk.weak and junk.paired hold only terms of junk.keywords (SPEC "Preflight")."""
    return ocr.word_list_problem(cfg["junk"])


def naming_overrides(cfg):
    return _missing_folders(cfg, ("naming_overrides",))


def long_paths_value():
    """Windows' LongPathsEnabled registry value, or None when it is absent."""
    if sys.platform != "win32":
        msg = "LongPathsEnabled is a Windows registry value"
        raise RuntimeError(msg)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LONG_PATHS_KEY) as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    except FileNotFoundError:
        return None


def long_paths(_cfg):
    """On Windows, paths of 260 or more characters open (SPEC "Preflight"); elsewhere they always do."""
    if sys.platform != "win32":
        return None
    try:
        value = long_paths_value()
    except OSError as e:
        return f"cannot read LongPathsEnabled under HKLM\\{LONG_PATHS_KEY}: {e}"
    if value != 1:
        return (
            f"LongPathsEnabled under HKLM\\{LONG_PATHS_KEY} is {'absent' if value is None else value}: set it to 1"
            " (a DWORD), or a share path of 260 or more characters cannot be opened"
        )
    return None


def fleet_version(cfg):
    """This machine runs the fleet's version (SPEC "State"): fleet.problem, read on a connection of its own."""
    try:
        with closing(db.open_database(cfg, builds=False)) as conn:
            return fleet.problem(conn, cfg)
    except Exception as e:  # a check turns its own errors into its problem text
        return f"cannot compare this machine with the fleet's version: {type(e).__name__}: {e}"


PYTHON_VERSION = Check("runtime version", HARD, python_version)
FREE_SPACE = Check("free disk space", SOFT, free_space)
STAGING_FOLDER = Check("staging folder", HARD, staging_folder)
FOLDER_LAYOUT = Check("folder layout", HARD, folder_layout)
SHARE_FOLDERS_PRESENT = Check("share folders", HARD, share_folders)
WORKFLOW_SETTINGS = Check("workflow settings", HARD, workflow_settings)
SERVICE_SETTINGS = Check("service settings", HARD, service_settings)
SHARE_OWNERSHIP = Check("share ownership", HARD, share_ownership)
OCR_DEVICE = Check("OCR device", HARD, ocr_device)
JUNK_WORD_LISTS = Check("junk word lists", HARD, junk_word_lists)
IMAGE_TOOLS = Check("image tools", HARD, image_tools)
SCORER = Check("scorer", HARD, scorer)
SCRATCH_SPACE = Check("scratch space", HARD, scratch_space)
CLAUDE_SETTINGS = Check("Claude settings", HARD, claude_settings)
NAMING_OVERRIDES = Check("naming overrides", HARD, naming_overrides)
FLEET_VERSION = Check("fleet version", HARD, fleet_version)
LONG_PATHS = Check("long paths", HARD, long_paths)


def run(cfg, checks):
    """Run every check. Logs each soft problem as a warning and returns them; when any check is hard and failed,
    logs each and raises PreflightFailed naming all of them."""
    unknown = [c.name for c in checks if c.level not in (HARD, SOFT)]
    if unknown:
        msg = f"preflight check level must be HARD or SOFT: {', '.join(unknown)}"
        raise ValueError(msg)
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
        msg = f"preflight failed: {'; '.join(hard)}"
        raise PreflightFailed(msg)
    return soft
