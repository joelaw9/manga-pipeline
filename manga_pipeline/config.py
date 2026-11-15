"""Load config.yaml from the project root, overridden by environment variables and --set, checked against
config.yaml.example (SPEC "Config")."""

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.yaml.example"
ENV_PREFIX = "MANGA_PIPELINE"
INT = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_process = None


class ConfigError(ValueError):
    pass


def load(path=None, example=None, env=None, sets=()):
    """The config file overridden by env (None = os.environ) and then by sets ("key=value" strings), refused unless
    the merged keys and value types match the example's: a null example value allows any type, an empty example
    mapping allows any keys, an int stands in for a float and a bool for nothing else."""
    path = Path(path or ROOT / "config.yaml")
    schema = _read(Path(example or EXAMPLE))
    cfg = _read(path, allow_empty=True)
    leaves = _leaves(schema)
    names = _env_names(leaves)
    _null_sections(cfg, schema)
    for source, parts, text in [*_from_env(os.environ if env is None else env, names), *_from_sets(sets, leaves)]:
        _put(cfg, parts, _convert(text, leaves[parts], source), source)
    _check(cfg, schema, path, (), names)
    return cfg


def init(path=None, sets=()):
    """Load the process config (the entry point calls this once) and return it; cached() returns it from then on."""
    global _process
    _process = load(path, sets=sets)
    return _process


def reset():
    """Forget the process config: the next cached() loads the default again."""
    global _process
    _process = None


def cached():
    """The process config: init's, else config.yaml + the environment, loaded once. Read-only: callers must not
    modify it."""
    global _process
    if _process is None:
        _process = load()
    return _process


def secret_keys(example=None):
    """The key paths of the secrets: the keys whose example value is null (SPEC "Config")."""
    return [parts for parts, value in _leaves(_read(Path(example or EXAMPLE))).items() if value is None]


def abs_path(value):
    """A configured path: absolute as given, else relative to the project root."""
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


# The share roots a stored path names (SPEC "State"): <root>/<path below it, with / separators>.
SHARE_ROOTS = ("downloads", "library", "staging", "retention", "candidates", "covers")


class PathError(ValueError):
    """A path that lies under no share root, or a stored path that names no root."""


def to_stored(cfg, path):
    """The stored form of a path on this machine: the share root it lies under (paths.<root>, compared as configured:
    no link, drive letter or UNC name is resolved) and the path below it, with / separators. PathError when it lies
    under no root."""
    path = Path(path)
    if ".." in path.parts:  # compared as written: a .. would pass for a path under the root it leaves
        raise PathError(f"{path} holds '..': a stored path names no parent folder")
    for root in SHARE_ROOTS:
        base = abs_path(cfg["paths"][root])
        if path == base or path.is_relative_to(base):
            below = path.relative_to(base).as_posix()
            return root if below == "." else f"{root}/{below}"
    raise PathError(f"{path} lies under no share root (paths.{', paths.'.join(SHARE_ROOTS)})")


def to_local(cfg, stored):
    """This machine's path for a stored path (to_stored's form). PathError for a text that names no share root."""
    root, _, below = stored.partition("/")
    if root not in SHARE_ROOTS or "\\" in stored or ".." in below.split("/"):
        raise PathError(f"stored path {stored!r} is not <share root>/<path>")
    base = abs_path(cfg["paths"][root])
    return base.joinpath(*below.split("/")) if below else base


def _read(path, allow_empty=False):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # Never the parser's own message: it quotes the offending line, and the file may hold a secret.
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(
            f"{path}: not valid YAML{where} (a value holding a backslash or starting with a special character"
            " needs single quotes)"
        ) from None
    if data is None and allow_empty:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: must be a YAML mapping, got {type(data).__name__}")
    return data


def _leaves(schema, parts=()):
    """{key path: example value} for every key env and --set can set: all but non-empty mappings."""
    out = {}
    for k, sub in schema.items():
        if isinstance(sub, dict) and sub:
            out |= _leaves(sub, (*parts, k))
        else:
            out[(*parts, k)] = sub
    return out


def _env_names(leaves):
    names = {}
    for parts in leaves:
        name = f"{ENV_PREFIX}_{_dotted(parts).upper().replace('.', '_').replace('-', '_')}"
        twin = next((p for p, n in names.items() if n == name), None)
        if twin:
            raise ConfigError(f"config example: keys {_dotted(twin)} and {_dotted(parts)} both give env {name}")
        names[parts] = name
    return names


def _from_env(env, names):
    by_name = {n: p for p, n in names.items()}
    for var in sorted(env):
        name = var.upper()
        if not name.startswith(f"{ENV_PREFIX}_"):
            continue
        if name not in by_name:
            raise ConfigError(f"env {var}: names no config key")
        yield f"env {var}", by_name[name], env[var]


def _from_sets(sets, leaves):
    by_key = {_dotted(p): p for p in leaves}
    for item in sets:
        key, sep, text = item.partition("=")
        if not sep:
            raise ConfigError(f"--set {item}: must be KEY=VALUE")
        if key not in by_key:
            raise ConfigError(f"--set {key}: names no config key")
        yield f"--set {key}", by_key[key], text


def _convert(text, schema, source):
    """Env or --set text as the example value's type. Null and str keep the text: a secret is never parsed."""
    if schema is None or isinstance(schema, str):
        return text
    if isinstance(schema, bool):
        if text.lower() not in ("true", "false"):
            raise ConfigError(f"{source}: must be true or false, got {text!r}")
        return text.lower() == "true"
    if isinstance(schema, (int, float)):
        pattern = INT if isinstance(schema, int) else DECIMAL
        if not pattern.fullmatch(text):
            raise ConfigError(f"{source}: must be {type(schema).__name__}, got {text!r}")
        return int(text) if isinstance(schema, int) else float(text)
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{source}: not valid YAML: {e}") from None
    if not _same_type(value, schema):
        raise ConfigError(f"{source}: must be {_kind(schema)}, got {_kind(value)}")
    return value


def _null_sections(cfg, schema):
    """An empty section (YAML null) where the example has a mapping reads as an empty mapping."""
    for k, sub in schema.items():
        if isinstance(sub, dict) and k in cfg:
            if cfg[k] is None:
                cfg[k] = {}
            if isinstance(cfg[k], dict):
                _null_sections(cfg[k], sub)


def _put(cfg, parts, value, source):
    node = cfg
    for i, k in enumerate(parts[:-1]):
        node.setdefault(k, {})
        if not isinstance(node[k], dict):
            raise ConfigError(f"{source}: {_dotted(parts[: i + 1])} in the config file is not a mapping")
        node = node[k]
    node[parts[-1]] = value


def _check(value, schema, path, parts, names):
    if schema is None:
        return
    key = _dotted(parts)
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: {key} must be a mapping, got {type(value).__name__}")
        if not schema:
            return
        missing = sorted(schema.keys() - value.keys(), key=str)
        if missing:
            raise ConfigError(f"{path}: missing key(s) {'; '.join(_hint((*parts, k), names) for k in missing)}")
        unknown = value.keys() - schema.keys()
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(_dotted((*parts, k)) for k in unknown))}")
        for k, sub in schema.items():
            _check(value[k], sub, path, (*parts, k), names)
    elif not _same_type(value, schema):
        raise ConfigError(f"{path}: {key} must be {type(schema).__name__}, got {type(value).__name__}")


def _hint(parts, names):
    key = _dotted(parts)
    if parts in names:
        return f"{key} (or env {names[parts]} / --set {key}=VALUE)"
    return f"{key} (a section: {', '.join(_dotted(p) for p in names if p[: len(parts)] == parts)})"


def _same_type(value, schema):
    if isinstance(value, bool) or isinstance(schema, bool):
        return type(value) is type(schema)
    if isinstance(schema, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(schema))


def _kind(value):
    return "a mapping" if isinstance(value, dict) else type(value).__name__


def _dotted(parts):
    return ".".join(str(k) for k in parts)
