"""Preflight (SPEC "Preflight"): the run's hard and soft requirements, checked before its work.

A check is a function of the config returning the problem text, or None when the requirement holds; it turns its own
errors into problem text. A new check is a function and its Check here, plus its place in the command table's check
lists (cli.py).
"""

import logging
import shutil
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
LONG_PATHS_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"
# A paths.<key> that names a folder (paths.kavita_path_map is a mapping)
type FolderKey = Literal[
    "downloads", "library", "staging", "retention", "scratch", "covers", "candidates", "naming_overrides"
]


@dataclass(frozen=True)
class Check:
    """A preflight check: its name, its level (HARD or SOFT) and its function of the config returning the problem."""

    name: str
    level: str  # HARD: the run cannot work without it; SOFT: it can, with a warning
    run: Callable[[config.Config], str | None]


class PreflightError(RuntimeError):
    """A hard check failed; the message names every failed hard check (exit 4)."""


def owner_file(cfg: config.Config) -> Path:
    """Return the share's owner file: it holds the id of the database that owns the share (SPEC "State")."""
    return config.abs_path(cfg["paths"]["staging"]) / OWNER_FILE


def python_version(_cfg: config.Config, version: tuple[int | str, ...] = sys.version_info) -> str | None:
    """Check that Python is 3.13 or later."""
    if version < (3, 13):
        return f"Python 3.13 or later is required, this is {version[0]}.{version[1]}"
    return None


def free_space(cfg: config.Config) -> str | None:
    """Check that logging.dir's drive has preflight.min_free_mb MiB free; 0 skips the check, below 0 is a problem."""
    # The drive is what counts; logging.dir itself may not exist yet (console-only commands never create it).
    return _floor(config.abs_path(cfg["logging"]["dir"]), cfg["preflight"]["min_free_mb"], "preflight.min_free_mb")


def share_space(cfg: config.Config) -> str | None:
    """Check that paths.library's drive has preflight.min_share_free_mb MiB free; 0 skips, below 0 is a problem."""
    want = cfg["preflight"]["min_share_free_mb"]
    return _floor(config.abs_path(cfg["paths"]["library"]), want, "preflight.min_share_free_mb")


def _floor(wanted: Path, want: int, key: str) -> str | None:
    """Return the problem when wanted's drive has under want MiB free; want 0 skips, below 0 is itself the problem."""
    if want < 0:
        return f"{key} must be 0 or more, got {want}"
    if want == 0:
        return None
    return _below(wanted, want, key)


def _below(wanted: Path, want: int, key: str) -> str | None:
    """Return the problem when the drive of wanted (measured at its nearest existing folder) has under want MiB free."""
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


def folder_layout(cfg: config.Config) -> str | None:
    """Check that the folders `state reset` empties hold nothing else the pipeline keeps.

    Those folders are paths.staging, paths.scratch and paths.candidates.
    """
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
    problems: list[str] = []
    emptied = ("staging", "scratch", "candidates")
    for key in emptied:
        folder = paths[key]
        others = {f"paths.{o}": paths[o] for o in emptied if o != key}
        for name, path in {**kept, **others}.items():
            if path.is_relative_to(folder):
                problems.append(f"paths.{key} {folder} contains {name} {path}")
        problems.extend(
            f"paths.{key} {folder} lies inside {name} {kept[name]}"
            for name in ("paths.downloads", "paths.library")
            if folder.is_relative_to(kept[name]) and folder != kept[name]
        )
    retention = paths["retention"]
    problems.extend(
        f"paths.retention {retention} and {name} {kept[name]} overlap"
        for name in ("paths.downloads", "paths.library")
        if retention.is_relative_to(kept[name]) or kept[name].is_relative_to(retention)
    )
    return "; ".join(problems) or None


def _missing_folders(cfg: config.Config, keys: Iterable[FolderKey | tuple[FolderKey, str]]) -> str | None:
    """keys: paths.<key> names, or (key, subfolder) for a folder below one."""
    problems: list[str] = []
    for entry in keys:
        key, sub = entry if isinstance(entry, tuple) else (entry, None)
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


def staging_folder(cfg: config.Config) -> str | None:
    """Check that paths.staging is an existing folder."""
    return _missing_folders(cfg, ("staging",))


def share_folders(cfg: config.Config) -> str | None:
    """Check that the share folders exist and paths.retention is on the filesystem of paths.downloads and paths.library.

    The folders: paths.downloads and its Suwayomi mangas folder, paths.library, paths.retention, paths.candidates and
    paths.covers.
    """
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


def workflow_settings(cfg: config.Config) -> str | None:
    """Check the workflow settings' minimums, complete_months above recent_months, and series.only names as text."""
    problems: list[str] = []
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


def service_settings(cfg: config.Config) -> str | None:
    """Check what the Suwayomi and Kavita clients need (SPEC "Services"). A value is never shown: one is a secret."""
    problems: list[str] = []
    for key, value in (("suwayomi.url", cfg["suwayomi"]["url"]), ("kavita.url", cfg["kavita"]["url"])):
        parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
        if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
            problems.append(f"{key} must be an http or https URL")
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


def discord_settings(cfg: config.Config) -> str | None:
    """Check notify.discord_webhook: unset (Discord off), or an https URL with a host (SPEC "Needs human").

    The value is never shown: it is a secret whole.
    """
    url = cfg["notify"]["discord_webhook"]
    if url is None:
        return None
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.netloc
        or not url.isprintable()
        or any(c.isspace() for c in url)
    ):
        return (
            "notify.discord_webhook must be an https URL with a host and no spaces or control characters "
            f"(env {config.ENV_PREFIX}_NOTIFY_DISCORD_WEBHOOK)"
        )
    return None


def discord_webhook(cfg: config.Config) -> str | None:
    """Check that notify.discord_webhook is set: `watch` has nowhere else to send (SPEC "Needs human")."""
    if cfg["notify"]["discord_webhook"] is None:
        return f"notify.discord_webhook is not set: nowhere to send (env {config.ENV_PREFIX}_NOTIFY_DISCORD_WEBHOOK)"
    return None


def share_ownership(cfg: config.Config) -> str | None:
    """Check that the share's owner file holds this database's id (SPEC "State"); the database is not upgraded."""
    path = owner_file(cfg)
    try:
        owner = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{path} is missing: the share is not mounted, or no database adopted it (state adopt --yes)"
    except OSError as e:
        return f"cannot read {path}: {e}"
    try:
        mine = db.peek_id(cfg)  # applies nothing: a run refused here must not have upgraded the database
    except db.NoDatabaseError as e:
        return f"{path} holds database id {owner!r}, and there is {e}"
    except (db.CannotConnectError, db.Error) as e:
        return f"cannot read the database id: {e}"
    if owner != mine:
        return f"{path} holds database id {owner!r}, this database is {mine!r} (state adopt --yes moves the share)"
    return None


def image_tools(cfg: config.Config) -> str | None:
    """Check that the tools.<name> path of every image tool and of vship is an existing file."""
    missing = [
        f"tools.{name} {path}"
        for name in (*tools.NAMES, "vship")
        if not (path := config.abs_path(cfg["tools"][name])).is_file()
    ]
    return f"not an existing file: {', '.join(missing)}" if missing else None


def scorer(cfg: config.Config) -> str | None:
    """Check that the vship scorer loads from tools.vship."""
    try:
        vship.Scorer(config.abs_path(cfg["tools"]["vship"]))
    except (OSError, vship.VshipError) as e:
        return str(e)
    return None


def scratch_space(cfg: config.Config) -> str | None:
    """Check that paths.scratch's drive has convert.min_scratch_free_mb MiB free; below 0 is a problem."""
    want = cfg["convert"]["min_scratch_free_mb"]
    if want < 0:
        return f"convert.min_scratch_free_mb must be 0 or more, got {want}"
    return _below(config.abs_path(cfg["paths"]["scratch"]), want, "convert.min_scratch_free_mb")


def claude_settings(cfg: config.Config) -> str | None:
    """Check that junk review can start Claude: junk.claude_path resolves to an executable, junk.claude_token is set."""
    problems: list[str] = []
    if shutil.which(cfg["junk"]["claude_path"]) is None:
        problems.append(f"junk.claude_path {cfg['junk']['claude_path']!r} is no executable (path or name on PATH)")
    token = cfg["junk"]["claude_token"]
    if not isinstance(token, str) or not token.strip():
        problems.append("junk.claude_token is not a non-blank string (set MANGA_PIPELINE_JUNK_CLAUDE_TOKEN)")
    return "; ".join(problems) or None


def ocr_device(cfg: config.Config) -> str | None:
    """junk.ocr_device is known and its onnxruntime provider loads (SPEC "Preflight")."""
    return ocr.provider_problem(cfg["junk"]["ocr_device"])


def junk_word_lists(cfg: config.Config) -> str | None:
    """junk.weak and junk.paired hold only terms of junk.keywords (SPEC "Preflight")."""
    return ocr.word_list_problem(cfg["junk"])


def naming_overrides(cfg: config.Config) -> str | None:
    """Check that paths.naming_overrides is an existing folder."""
    return _missing_folders(cfg, ("naming_overrides",))


def long_paths_value() -> int | None:
    """Windows' LongPathsEnabled registry value, or None when it is absent."""
    if sys.platform != "win32":
        msg = "LongPathsEnabled is a Windows registry value"
        raise RuntimeError(msg)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LONG_PATHS_KEY) as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    except FileNotFoundError:
        return None


def long_paths(_cfg: config.Config) -> str | None:
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


def fleet_version(cfg: config.Config) -> str | None:
    """Check that this machine runs the fleet's version (SPEC "State"): fleet.problem, read on its own connection."""
    try:
        with closing(db.open_database(cfg, builds=False)) as conn:
            return fleet.problem(conn, cfg)
    except Exception as e:  # a check turns its own errors into its problem text
        return f"cannot compare this machine with the fleet's version: {type(e).__name__}: {e}"


PYTHON_VERSION = Check("runtime version", HARD, python_version)
FREE_SPACE = Check("free disk space", SOFT, free_space)
SHARE_SPACE = Check("share free space", SOFT, share_space)
STAGING_FOLDER = Check("staging folder", HARD, staging_folder)
FOLDER_LAYOUT = Check("folder layout", HARD, folder_layout)
SHARE_FOLDERS_PRESENT = Check("share folders", HARD, share_folders)
WORKFLOW_SETTINGS = Check("workflow settings", HARD, workflow_settings)
SERVICE_SETTINGS = Check("service settings", HARD, service_settings)
DISCORD_SETTINGS = Check("discord settings", HARD, discord_settings)
DISCORD_WEBHOOK = Check("discord webhook", HARD, discord_webhook)
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


def run(cfg: config.Config, checks: Sequence[Check]) -> list[str]:
    """Run every check; log each soft problem as a warning and return them.

    When any check is hard and failed, logs each and raises PreflightError naming all of them.
    """
    unknown = [c.name for c in checks if c.level not in (HARD, SOFT)]
    if unknown:
        msg = f"preflight check level must be HARD or SOFT: {', '.join(unknown)}"
        raise ValueError(msg)
    soft: list[str] = []
    hard: list[str] = []
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
        raise PreflightError(msg)
    return soft
