"""The fleet's version (SPEC "State", "Config", "Preflight").

Every machine that works runs the same program with the same shared settings, Python packages and image tools. `state
version --yes` stores this machine's digest of the 4 as the fleet's; a part or junk review whose digest differs, or
that finds none stored, does not run.

Shared settings are every config key but the machine-local ones (MACHINE_LOCAL_*) and the secrets. Packages are the
runtime dependency group of pyproject.toml at their installed versions (onnxruntime is per machine: SPEC "Config",
junk.ocr_device). Tools are the version lines of the 4 image tools and the scorer library's version.
"""

import ctypes
import hashlib
import json
import tomllib
from collections.abc import Iterator
from importlib import metadata
from typing import Literal

from manga_pipeline import config, db
from manga_pipeline.convert import tools

META_KEY = "fleet.version"
# Whole sections, then single keys, that may differ between machines (SPEC "Config"); the secrets are left out too
# (config.secret_keys).
MACHINE_LOCAL_SECTIONS = ("logging", "preflight", "database", "paths", "tools")
MACHINE_LOCAL_KEYS = (
    ("convert", "workers"),
    ("convert", "min_scratch_free_mb"),
    ("junk", "ocr_device"),
    ("junk", "claude_path"),
    ("suwayomi", "url"),
    ("kavita", "url"),
)
COMPILED = (".pyc", ".pyo")  # left out of the program digest: made by each machine
PART_NAMES = ("program", "shared settings", "runtime packages", "tools")


class _VshipVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_int), ("minor", ctypes.c_int), ("patch", ctypes.c_int), ("backend", ctypes.c_int)]


def digest(cfg: config.Config) -> dict[str, str]:
    """{part: sha256 hex} for PART_NAMES, of this machine."""
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() for name, value in _parts(cfg)}


def store(conn: db.Connection, cfg: config.Config) -> None:
    """Make this machine's digest the fleet's; inside the caller's transaction."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (META_KEY, json.dumps(digest(cfg), sort_keys=True)),
    )


def problem(conn: db.Connection, cfg: config.Config) -> str | None:
    """Why this machine may not work (no fleet digest stored, or which parts differ), or None."""
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (META_KEY,)).fetchone()
    if row is None:
        return "the database holds no fleet version: run state version --yes once the fleet is deployed"
    fleet, mine = _read_digest(row[0]), digest(cfg)
    differ = [name for name in PART_NAMES if fleet.get(name) != mine[name]]
    if differ:
        return (
            f"this machine's {', '.join(differ)} differ from the fleet's (state version --yes on the machine that"
            " set it, after a deploy or a change of a shared setting)"
        )
    return None


def _read_digest(text: str) -> dict[str, str]:
    """Return the fleet's stored digest (meta fleet.version, JSON text: {part: sha256 hex}). Unchecked."""
    return json.loads(text)


def _parts(cfg: config.Config) -> Iterator[tuple[str, object]]:
    yield "program", _program()
    yield "shared settings", shared_settings(cfg)
    yield "runtime packages", _packages()
    yield "tools", _tools(cfg)


def shared_settings(cfg: config.Config) -> dict[str, dict[str, object]]:
    """Return the config's shared keys and values: {section: {key: value}}."""
    left_out = set(MACHINE_LOCAL_KEYS) | set(config.secret_keys())
    out: dict[str, dict[str, object]] = {}
    for section, values in cfg.items():
        if section in MACHINE_LOCAL_SECTIONS:
            continue
        # Every section is a mapping once config.load has checked it, but a TypedDict is never assignable to
        # Mapping[str, Mapping[str, object]]: to the checker each section is an object.
        kept = {k: v for k, v in values.items() if (section, k) not in left_out}  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        if kept:
            out[section] = kept
    return out


def _program() -> dict[str, str]:
    """{relative path: sha256} of every file of the package but compiled ones (LF on every machine: .gitattributes)."""
    root = config.ROOT / "manga_pipeline"
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix not in COMPILED and "__pycache__" not in p.parts
    )
    return {p.relative_to(config.ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def _packages() -> dict[str, str]:
    """{distribution: installed version} of pyproject's runtime group."""
    with (config.ROOT / "pyproject.toml").open("rb") as f:
        runtime = tomllib.load(f)["dependency-groups"]["runtime"]
    return {package_name(spec): metadata.version(package_name(spec)) for spec in runtime}


def package_name(spec: str) -> str:
    """Return the distribution name at the start of a requirement ("psycopg[binary]==3.3.9" -> "psycopg")."""
    end = next((i for i, ch in enumerate(spec) if not (ch.isalnum() or ch in "._-")), len(spec))
    return spec[:end]


def _tools(cfg: config.Config) -> dict[str, str]:
    """Return {tool: its version}.

    The first line each executable prints (jpegtran's build date left out) and the scorer library's major.minor.patch
    and backend.
    """
    out: dict[str, str] = {}
    flags: tuple[tuple[Literal["avifenc", "avifdec", "jpegtran", "oxipng"], str], ...] = (
        ("avifenc", "--version"),
        ("avifdec", "--version"),
        ("jpegtran", "-version"),
        ("oxipng", "--version"),
    )
    for name, flag in flags:
        done = tools.run(config.abs_path(cfg["tools"][name]), flag)
        lines = (done.stdout or done.stderr).decode(errors="replace").strip().splitlines()
        if not lines:
            msg = f"tools.{name} printed no version for {flag}"
            raise RuntimeError(msg)
        text = lines[0]
        out[name] = text.split(" (build ")[0] if name == "jpegtran" else text
    lib = ctypes.CDLL(str(config.abs_path(cfg["tools"]["vship"])))
    lib.Vship_GetVersion.restype, lib.Vship_GetVersion.argtypes = _VshipVersion, []
    v = lib.Vship_GetVersion()
    out["vship"] = f"{v.major}.{v.minor}.{v.patch} backend {v.backend}"
    return out
