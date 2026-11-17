"""config.load merges the config file, env and --set (in rising priority) and checks the result.

It refuses a result whose keys or value types differ from the example (SPEC "Config"); tests never read the live
config.yaml (SPEC "Tests").
"""

import types
import typing

import pytest

from manga_pipeline import config

from tests import TMP, LiveConfigReadError, builders, real_load
from tests.timing import budget

EXAMPLE = """\
logging:
  dir: "state/logs"
  keep_days: 14
paths: {}
tool: null
ratio: 0.5
enabled: true
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def check(tmp_path, text, env=None, sets=(), example=EXAMPLE):
    return real_load(write(tmp_path, "config.yaml", text), write(tmp_path, "example.yaml", example), env or {}, sets)


def env_name(key):
    return f"{config.ENV_PREFIX}_{key}"


@budget(load="fs")
def test_shipped_example_passes_itself():
    assert real_load(config.EXAMPLE), "config.yaml.example must validate against itself"


@budget(load="fs")
@pytest.mark.parametrize(
    ("line", "column"),
    [
        ('  api_key: "hunter2\\media"', 21),
        ("  api_key: *hunter2", 12),
        ("  api_key: !hunter2 x", 12),
        ("  api_key: @hunter2", 12),
    ],
    ids=["escape", "alias", "tag", "reserved-character"],
)
def test_a_file_that_is_not_yaml_is_named_by_line_and_column_without_its_content(tmp_path, line, column):
    path = tmp_path / "config.yaml"
    path.write_text(f"kavita:\n{line}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError) as e:
        real_load(path)
    assert f"not valid YAML at line 2, column {column}" in str(e.value), "SPEC 'Config': line and column"
    assert "hunter2" not in str(e.value), "SPEC 'Config': no error shows the file's content"


@budget(load="fs")
def test_default_config_is_the_test_copy():
    cfg = config.cached()
    folders = {k: v for k, v in cfg["paths"].items() if k != "kavita_path_map"}
    local_prefixes = cfg["paths"]["kavita_path_map"].values()
    paths = [cfg["logging"]["dir"], *folders.values(), *local_prefixes]
    outside = [p for p in paths if not config.abs_path(p).is_relative_to(TMP)]
    assert not outside, "tests must get tests/test_config.yaml, every path in its $TMP dir"


@budget(load="fs")
def test_live_config_is_refused():
    with pytest.raises(LiveConfigReadError):
        config.load(config.ROOT / "config.yaml")


@budget(load="fs")
def test_missing_file_is_refused(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        real_load(tmp_path / "absent.yaml", write(tmp_path, "example.yaml", EXAMPLE))


@budget(load="fs")
@pytest.mark.parametrize("text", ["- a\n", "just text\n"], ids=["list", "scalar"])
def test_non_mapping_is_refused(tmp_path, text):
    with pytest.raises(config.ConfigError, match="must be a YAML mapping"):
        check(tmp_path, text)


@budget(load="fs")
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("  keep_days: 14\n", "", r"missing key\(s\) logging\.keep_days"),
        ("  keep_days: 14\n", "  keep_days: 14\n  keep_dayz: 1\n", r"unknown key\(s\) logging\.keep_dayz"),
        ("keep_days: 14", 'keep_days: "14"', "logging.keep_days must be int, got str"),
        ("keep_days: 14", "keep_days: true", "logging.keep_days must be int, got bool"),
        ("enabled: true", "enabled: 1", "enabled must be bool, got int"),
        ("paths: {}", "paths: []", "paths must be a mapping, got list"),
    ],
    ids=["missing", "unknown", "str-for-int", "bool-for-int", "int-for-bool", "list-for-mapping"],
)
def test_mismatch_is_refused_naming_the_key(tmp_path, old, new, message):
    with pytest.raises(config.ConfigError, match=message):
        check(tmp_path, EXAMPLE.replace(old, new))


@budget(load="fs")
def test_example_conventions(tmp_path):
    """An int stands in for a float, a null example value takes any type, an empty example mapping takes any keys."""
    text = EXAMPLE.replace("ratio: 0.5", "ratio: 1").replace("tool: null", "tool: [a]")
    cfg = check(tmp_path, text.replace("paths: {}", "paths: {anything: 1}"))
    assert (cfg["ratio"], cfg["tool"], cfg["paths"]) == (1, ["a"], {"anything": 1})


@budget(load="fs")
def test_env_and_set_convert_to_the_example_types(tmp_path):
    env = {
        env_name("LOGGING_KEEP_DAYS"): "-7",
        env_name("RATIO"): "2.5",
        env_name("ENABLED"): "FALSE",
        env_name("TOOL"): "012345",
        env_name("PATHS"): "{a: 1}",
    }
    cfg = check(tmp_path, EXAMPLE, env, ["logging.dir=x"])
    got = (cfg["logging"], cfg["ratio"], cfg["enabled"], cfg["tool"], cfg["paths"])
    assert got == ({"dir": "x", "keep_days": -7}, 2.5, False, "012345", {"a": 1}), "a null key keeps the text as is"


@budget(load="fs")
def test_set_beats_env_beats_file(tmp_path):
    env = {env_name("LOGGING_KEEP_DAYS"): "7"}
    assert check(tmp_path, EXAMPLE, env)["logging"]["keep_days"] == 7, "env beats the file"
    assert check(tmp_path, EXAMPLE, env, ["logging.keep_days=3"])["logging"]["keep_days"] == 3, "--set beats env"


@budget(load="fs")
def test_env_name_is_case_insensitive(tmp_path):
    env = {env_name("LOGGING_KEEP_DAYS").lower(): "7"}
    assert check(tmp_path, EXAMPLE, env)["logging"]["keep_days"] == 7


@budget(load="fs")
def test_key_missing_from_file_can_come_from_env(tmp_path):
    text = EXAMPLE.replace("  keep_days: 14\n", "")
    assert check(tmp_path, text, {env_name("LOGGING_KEEP_DAYS"): "7"})["logging"]["keep_days"] == 7
    name = env_name("LOGGING_KEEP_DAYS")
    with pytest.raises(config.ConfigError, match=rf"logging\.keep_days \(or env {name} / --set logging\.keep_days="):
        check(tmp_path, text)


@budget(load="fs")
def test_missing_section_names_its_keys(tmp_path):
    text = EXAMPLE.replace('logging:\n  dir: "state/logs"\n  keep_days: 14\n', "")
    with pytest.raises(config.ConfigError, match=r"logging \(a section: logging\.dir, logging\.keep_days\)"):
        check(tmp_path, text)


@budget(load="fs")
@pytest.mark.parametrize(
    "text",
    ["", "logging:\npaths: {}\ntool: null\nratio: 0.5\nenabled: true\n"],
    ids=["empty-file", "empty-section"],
)
def test_empty_file_or_section_takes_env(tmp_path, text):
    env = {
        env_name("LOGGING_DIR"): "d",
        env_name("LOGGING_KEEP_DAYS"): "1",
        env_name("PATHS"): "{}",
        env_name("TOOL"): "t",
        env_name("RATIO"): "1",
        env_name("ENABLED"): "true",
    }
    assert check(tmp_path, text, env)["logging"] == {"dir": "d", "keep_days": 1}


@budget(load="fs")
@pytest.mark.parametrize(
    ("key", "text", "message"),
    [
        ("LOGGING_KEEP_DAYS", "14days", "must be int"),
        ("LOGGING_KEEP_DAYS", "", "must be int"),
        ("LOGGING_KEEP_DAYS", " 1", "must be int"),
        ("RATIO", "nan", "must be float"),
        ("RATIO", "inf", "must be float"),
        ("ENABLED", "1", "must be true or false"),
        ("PATHS", "{unclosed", "not valid YAML"),
        ("PATHS", "[a]", "must be a mapping, got list"),
    ],
)
def test_bad_env_text_is_refused_naming_the_variable(tmp_path, key, text, message):
    with pytest.raises(config.ConfigError, match=rf"env {env_name(key)}: {message}"):
        check(tmp_path, EXAMPLE, {env_name(key): text})


@budget(load="fs")
def test_unknown_prefixed_env_is_refused(tmp_path):
    with pytest.raises(config.ConfigError, match=rf"env {env_name('LOGGING_KEEP_DAYZ')}: names no config key"):
        check(tmp_path, EXAMPLE, {env_name("LOGGING_KEEP_DAYZ"): "1"})


@budget(load="fs")
def test_colliding_env_names_are_refused(tmp_path):
    example = "a:\n  b_c: 1\na_b:\n  c: 2\n"
    with pytest.raises(config.ConfigError, match=rf"keys a\.b_c and a_b\.c both give env {env_name('A_B_C')}"):
        check(tmp_path, example, example=example)


@budget(load="fs")
@pytest.mark.parametrize(
    ("item", "message"),
    [
        ("logging.keep_days", "must be KEY=VALUE"),
        ("nope=1", "names no config key"),
        ("logging=1", "names no config key"),
    ],
    ids=["no-equals", "unknown", "section"],
)
def test_bad_set_is_refused(tmp_path, item, message):
    with pytest.raises(config.ConfigError, match=f"--set .*{message}"):
        check(tmp_path, EXAMPLE, sets=[item])


@budget(load="fs")
def test_set_below_a_non_mapping_file_value_is_refused(tmp_path):
    text = EXAMPLE.replace('logging:\n  dir: "state/logs"\n  keep_days: 14\n', "logging: 1\n")
    with pytest.raises(config.ConfigError, match=r"--set logging\.dir: logging in the config file is not a mapping"):
        check(tmp_path, text, sets=["logging.dir=x"])


@budget(load="fs")
def test_init_sets_the_process_config_until_reset():
    assert config.init(sets=["logging.level=WARNING"]) is config.cached()
    assert config.cached()["logging"]["level"] == "WARNING", "every module reads init's config through cached()"
    config.init()
    assert config.cached()["logging"]["level"] != "WARNING", "a second init replaces the first"
    config.init(sets=["logging.level=WARNING"])
    config.reset()
    assert config.cached()["logging"]["level"] != "WARNING", "reset forgets init's config"


def _example_leaves(mapping, parts=()):
    """{key path: value} of a loaded config's leaves: every value but a non-empty mapping (what env and --set set)."""
    out = {}
    for key, value in mapping.items():
        if isinstance(value, dict) and value:
            out |= _example_leaves(value, (*parts, key))
        else:
            out[(*parts, key)] = value
    return out


def _typed_leaves(shape, parts=()):
    """{key path: type} of a TypedDict's leaves, descending into section TypedDicts."""
    out = {}
    for key, hint in typing.get_type_hints(shape).items():
        if typing.is_typeddict(hint):
            out |= _typed_leaves(hint, (*parts, key))
        else:
            out[(*parts, key)] = hint
    return out


@budget(load="fs")
def test_the_config_types_name_every_key_of_the_example_with_its_type():
    example = _example_leaves(real_load(config.EXAMPLE))
    typed = _typed_leaves(config.Config)
    assert sorted(example) == sorted(typed), "config.Config and its sections hold exactly the example's keys"
    for parts, value in example.items():
        hint = typed[parts]
        if value is None:
            assert set(typing.get_args(hint)) == {str, types.NoneType}, f"{parts}: a null example leaf is str | None"
        else:
            assert (typing.get_origin(hint) or hint) is type(value), f"{parts}: typed as the example value's type"


@budget(load="pure")
def test_the_sections_a_test_may_replace_are_config_sections():
    sections = typing.get_type_hints(builders.ConfigChanges).keys()
    assert sections == typing.get_type_hints(config.Config).keys(), "ConfigChanges names every Config section, no other"
