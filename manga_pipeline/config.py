"""Load config.yaml from the project root, overridden by environment variables and --set (SPEC "Config").

Checked against config.yaml.example.
"""

import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date
from pathlib import Path
from typing import TypedDict, overload

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "config.yaml"
EXAMPLE = ROOT / "config.yaml.example"
ENV_PREFIX = "MANGA_PIPELINE"
INT = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")


# A YAML value as yaml.safe_load builds it (PyYAML's SafeConstructor): a scalar (a timestamp is a date or datetime,
# !!binary is bytes), a sequence (a list; !!omap and !!pairs give a list of key-value tuples), a !!set, or a mapping,
# whose keys are scalars (a key that would be a list, set or mapping is refused as unhashable). Typed through the
# read-only abstract types, so a value the program typed (a list of names) is a YAML value too.
type YamlKey = str | int | float | bool | date | bytes | None
type Yaml = YamlKey | Sequence[Yaml] | AbstractSet[YamlKey] | Mapping[YamlKey, Yaml]
type KeyPath = tuple[YamlKey, ...]  # a key's place in a mapping: its section keys, then its own


class ConfigError(ValueError):
    """The config does not load, does not match config.yaml.example, or holds an invalid value (SPEC "Config")."""


# The config's shape: 1 TypedDict per section of config.yaml.example, which stays the source of truth (load checks a
# config against the example; tests/test_config.py checks these types against it). Where load checks less than a type
# here states, the type is what the program relies on:
# - a list's element type: load checks only that the value is a list;
# - an empty mapping's contents (paths.kavita_path_map): load allows any keys and values;
# - a null leaf (a secret: database.password, kavita.api_key, junk.claude_token): load allows any type; it is
#   str | None, as the code that uses it checks or expects.


class LoggingConfig(TypedDict):
    """The logging section (SPEC "Logging")."""

    dir: str
    level: str
    keep_days: int


class LockConfig(TypedDict):
    """The lock section (SPEC "Run lock")."""

    enabled: bool


class PreflightConfig(TypedDict):
    """The preflight section (SPEC "Preflight")."""

    min_free_mb: int
    min_share_free_mb: int


class DatabaseConfig(TypedDict):
    """The database section: the login (SPEC "Config"); password is a secret, null when the server asks none."""

    host: str
    port: int
    name: str
    user: str
    password: str | None


class PathsConfig(TypedDict):
    """The paths section: the share roots and local folders, and kavita_path_map {Kavita prefix: local prefix}."""

    downloads: str
    library: str
    staging: str
    retention: str
    scratch: str
    covers: str
    candidates: str
    naming_overrides: str
    kavita_path_map: dict[str, str]


class WorkflowConfig(TypedDict):
    """The workflow section."""

    attempt_limit: int


class IntakeConfig(TypedDict):
    """The intake section."""

    settle_minutes: int


class SeriesConfig(TypedDict):
    """The series section: only, the names of the only series to work (empty: all)."""

    only: list[str]


class SuwayomiConfig(TypedDict):
    """The suwayomi section."""

    url: str


class KavitaConfig(TypedDict):
    """The kavita section; api_key is a secret."""

    url: str
    api_key: str | None
    library_id: int
    rescan_after_minutes: int


class MetadataConfig(TypedDict):
    """The metadata section: how many checks a run makes and how often a series is checked."""

    checks_per_run: int
    recent_months: int
    complete_months: int
    recent_days: int
    quiet_days: int
    complete_days: int


class NotifyConfig(TypedDict):
    """The notify section; discord_webhook is a secret, None = Discord off (SPEC "Needs human")."""

    discord_webhook: str | None


class ExportConfig(TypedDict):
    """The export section."""

    retention_days: int
    rewrites_open: int


class ToolsConfig(TypedDict):
    """The tools section: each tool's path on this machine."""

    avifenc: str
    avifdec: str
    jpegtran: str
    oxipng: str
    vship: str


class ConvertConfig(TypedDict):
    """The convert section."""

    workers: int
    min_scratch_free_mb: int


class SourcesConfig(TypedDict):
    """The sources section: ranking, re-download source names best first."""

    ranking: list[str]
    download_timeout_hours: int


class ValidateConfig(TypedDict):
    """The validate section: non_chapter_names, titles dropped at discover."""

    non_chapter_names: list[str]


class JunkConfig(TypedDict):
    """The junk section; claude_token is a secret, and the 5 lists hold keyword texts."""

    claude_token: str | None
    claude_path: str
    review_limit: int
    review_batch: int
    ocr_device: str
    keywords: list[str]
    weak: list[str]
    paired: list[str]
    suppress: list[str]


class Config(TypedDict):
    """A loaded config: every section of config.yaml.example (see the shape note above the sections)."""

    logging: LoggingConfig
    lock: LockConfig
    preflight: PreflightConfig
    database: DatabaseConfig
    paths: PathsConfig
    workflow: WorkflowConfig
    intake: IntakeConfig
    series: SeriesConfig
    suwayomi: SuwayomiConfig
    kavita: KavitaConfig
    metadata: MetadataConfig
    export: ExportConfig
    notify: NotifyConfig
    tools: ToolsConfig
    convert: ConvertConfig
    sources: SourcesConfig
    validate: ValidateConfig
    junk: JunkConfig


class _Process:
    """Holder of the process config: set by init and cached, forgotten by reset."""

    cfg: Config | None = None


_process = _Process()


@overload
def load(
    path: str | Path | None = None, example: None = None, env: Mapping[str, str] | None = None, sets: Iterable[str] = ()
) -> Config: ...
@overload
def load(
    path: str | Path | None, example: str | Path, env: Mapping[str, str] | None = None, sets: Iterable[str] = ()
) -> dict[YamlKey, Yaml]: ...
def load(
    path: str | Path | None = None,
    example: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    sets: Iterable[str] = (),
) -> Config | dict[YamlKey, Yaml]:
    """Return the config file overridden by env (None = os.environ) and then by sets ("key=value" strings).

    Refused unless the merged keys and value types match the example's: a null example value allows any type, an empty
    example mapping allows any keys, an int stands in for a float and a bool for nothing else. Checked against the
    shipped example, the result is a Config (tests/test_config.py holds Config to the example); against another, the
    mapping that example describes.
    """
    path = Path(path or DEFAULT)
    schema = _read(Path(example or EXAMPLE))
    cfg = _read(path, allow_empty=True)
    leaves = _leaves(schema)
    names = _env_names(leaves)
    _null_sections(cfg, schema)
    for source, parts, text in [*_from_env(os.environ if env is None else env, names), *_from_sets(sets, leaves)]:
        _put(cfg, parts, _convert(text, leaves[parts], source), source)
    _check(cfg, schema, path, (), names)
    return cfg


def init(path: str | Path | None = None, sets: Iterable[str] = ()) -> Config:
    """Load the process config (the entry point calls this once) and return it; cached() returns it from then on."""
    _process.cfg = load(path, sets=sets)
    return _process.cfg


def reset() -> None:
    """Forget the process config: the next cached() loads the default again."""
    _process.cfg = None


def cached() -> Config:
    """Return the process config: init's, else config.yaml + the environment, loaded once.

    Read-only: callers must not modify it.
    """
    if _process.cfg is None:
        _process.cfg = load()
    return _process.cfg


def secret_keys(example: str | Path | None = None) -> list[tuple[str, str]]:
    """Return the (section, key) of each secret: each key whose example value is null (SPEC "Config").

    ConfigError for a null example value that is not a section's key (a secret is a key of a section).
    """
    out: list[tuple[str, str]] = []
    for parts, value in _leaves(_read(Path(example or EXAMPLE))).items():
        if value is not None:
            continue
        match parts:
            case (str() as section, str() as name):
                out.append((section, name))
            case _:
                msg = f"config example: secret {_dotted(parts)} is not a section's key"
                raise ConfigError(msg)
    return out


def abs_path(value: str | Path) -> Path:
    """Return a configured path as a Path: absolute as given, else relative to the project root."""
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


# The share roots a stored path names (SPEC "State"): <root>/<path below it, with / separators>.
SHARE_ROOTS = ("downloads", "library", "staging", "retention", "candidates", "covers")


class PathError(ValueError):
    """A path that lies under no share root, or a stored path that names no root."""


def to_stored(cfg: Config, path: str | Path) -> str:
    """Return the stored form of a path on this machine.

    The share root it lies under (paths.<root>, compared as configured: no link, drive letter or UNC name is resolved)
    and the path below it, with / separators. PathError when it lies under no root.
    """
    path = Path(path)
    if ".." in path.parts:  # compared as written: a .. would pass for a path under the root it leaves
        msg = f"{path} holds '..': a stored path names no parent folder"
        raise PathError(msg)
    for root in SHARE_ROOTS:
        base = abs_path(cfg["paths"][root])
        if path == base or path.is_relative_to(base):
            below = path.relative_to(base).as_posix()
            return root if below == "." else f"{root}/{below}"
    msg = f"{path} lies under no share root (paths.{', paths.'.join(SHARE_ROOTS)})"
    raise PathError(msg)


def to_local(cfg: Config, stored: str) -> Path:
    """Return this machine's path for a stored path (to_stored's form).

    PathError for a text that names no share root.
    """
    root, _, below = stored.partition("/")
    if root not in SHARE_ROOTS or "\\" in stored or ".." in below.split("/"):
        msg = f"stored path {stored!r} is not <share root>/<path>"
        raise PathError(msg)
    base = abs_path(cfg["paths"][root])
    return base.joinpath(*below.split("/")) if below else base


def _read(path: Path, *, allow_empty: bool = False) -> dict[YamlKey, Yaml]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = f"{path}: not found"
        raise ConfigError(msg) from None
    try:
        data: Yaml = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # Never the parser's own message: it quotes the offending line, and the file may hold a secret.
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        msg = (
            f"{path}: not valid YAML{where} (a value holding a backslash or starting with a special character"
            " needs single quotes)"
        )
        raise ConfigError(msg) from None
    if data is None and allow_empty:
        return {}
    if not isinstance(data, dict):
        msg = f"{path}: must be a YAML mapping, got {type(data).__name__}"
        raise ConfigError(msg)
    return data


def _leaves(schema: dict[YamlKey, Yaml], parts: KeyPath = ()) -> dict[KeyPath, Yaml]:
    """{key path: example value} for every key env and --set can set: all but non-empty mappings."""
    out: dict[KeyPath, Yaml] = {}
    for k, sub in schema.items():
        if isinstance(sub, dict) and sub:
            out |= _leaves(sub, (*parts, k))
        else:
            out[(*parts, k)] = sub
    return out


def _env_names(leaves: dict[KeyPath, Yaml]) -> dict[KeyPath, str]:
    names: dict[KeyPath, str] = {}
    for parts in leaves:
        name = f"{ENV_PREFIX}_{_dotted(parts).upper().replace('.', '_').replace('-', '_')}"
        twin = next((p for p, n in names.items() if n == name), None)
        if twin:
            msg = f"config example: keys {_dotted(twin)} and {_dotted(parts)} both give env {name}"
            raise ConfigError(msg)
        names[parts] = name
    return names


def _from_env(env: Mapping[str, str], names: dict[KeyPath, str]) -> Iterator[tuple[str, KeyPath, str]]:
    by_name = {n: p for p, n in names.items()}
    for var in sorted(env):
        name = var.upper()
        if not name.startswith(f"{ENV_PREFIX}_"):
            continue
        if name not in by_name:
            msg = f"env {var}: names no config key"
            raise ConfigError(msg)
        yield f"env {var}", by_name[name], env[var]


def _from_sets(sets: Iterable[str], leaves: dict[KeyPath, Yaml]) -> Iterator[tuple[str, KeyPath, str]]:
    by_key = {_dotted(p): p for p in leaves}
    for item in sets:
        key, sep, text = item.partition("=")
        if not sep:
            msg = f"--set {item}: must be KEY=VALUE"
            raise ConfigError(msg)
        if key not in by_key:
            msg = f"--set {key}: names no config key"
            raise ConfigError(msg)
        yield f"--set {key}", by_key[key], text


def _convert(text: str, schema: Yaml, source: str) -> Yaml:
    """Env or --set text as the example value's type. Null and str keep the text: a secret is never parsed."""
    if schema is None or isinstance(schema, str):
        return text
    if isinstance(schema, bool):
        if text.lower() not in ("true", "false"):
            msg = f"{source}: must be true or false, got {text!r}"
            raise ConfigError(msg)
        return text.lower() == "true"
    if isinstance(schema, (int, float)):
        pattern = INT if isinstance(schema, int) else DECIMAL
        if not pattern.fullmatch(text):
            msg = f"{source}: must be {type(schema).__name__}, got {text!r}"
            raise ConfigError(msg)
        return int(text) if isinstance(schema, int) else float(text)
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as e:
        msg = f"{source}: not valid YAML: {e}"
        raise ConfigError(msg) from None
    if not _same_type(value, schema):
        msg = f"{source}: must be {_kind(schema)}, got {_kind(value)}"
        raise ConfigError(msg)
    return value


def _null_sections(cfg: dict[YamlKey, Yaml], schema: dict[YamlKey, Yaml]) -> None:
    """Make an empty section (YAML null) where the example has a mapping read as an empty mapping."""
    for k, sub in schema.items():
        if isinstance(sub, dict) and k in cfg:
            if cfg[k] is None:
                cfg[k] = {}
            section = cfg[k]
            if isinstance(section, dict):
                _null_sections(section, sub)


def _put(cfg: dict[YamlKey, Yaml], parts: KeyPath, value: Yaml, source: str) -> None:
    node = cfg
    for i, k in enumerate(parts[:-1]):
        child = node.setdefault(k, {})
        if not isinstance(child, dict):
            msg = f"{source}: {_dotted(parts[: i + 1])} in the config file is not a mapping"
            raise ConfigError(msg)
        node = child
    node[parts[-1]] = value


def _check(value: Yaml, schema: Yaml, path: Path, parts: KeyPath, names: dict[KeyPath, str]) -> None:
    if schema is None:
        return
    key = _dotted(parts)
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            msg = f"{path}: {key} must be a mapping, got {type(value).__name__}"
            raise ConfigError(msg)
        if not schema:
            return
        missing = sorted(schema.keys() - value.keys(), key=str)
        if missing:
            msg = f"{path}: missing key(s) {'; '.join(_hint((*parts, k), names) for k in missing)}"
            raise ConfigError(msg)
        unknown = value.keys() - schema.keys()
        if unknown:
            msg = f"{path}: unknown key(s) {', '.join(sorted(_dotted((*parts, k)) for k in unknown))}"
            raise ConfigError(msg)
        for k, sub in schema.items():
            _check(value[k], sub, path, (*parts, k), names)
    elif not _same_type(value, schema):
        msg = f"{path}: {key} must be {type(schema).__name__}, got {type(value).__name__}"
        raise ConfigError(msg)


def _hint(parts: KeyPath, names: dict[KeyPath, str]) -> str:
    key = _dotted(parts)
    if parts in names:
        return f"{key} (or env {names[parts]} / --set {key}=VALUE)"
    return f"{key} (a section: {', '.join(_dotted(p) for p in names if p[: len(parts)] == parts)})"


def _same_type(value: Yaml, schema: Yaml) -> bool:
    if isinstance(value, bool) or isinstance(schema, bool):
        return type(value) is type(schema)
    if isinstance(schema, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(schema))


def _kind(value: Yaml) -> str:
    return "a mapping" if isinstance(value, dict) else type(value).__name__


def _dotted(parts: KeyPath) -> str:
    return ".".join(str(k) for k in parts)
