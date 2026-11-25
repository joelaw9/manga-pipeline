"""scripts/set_config.py: config.yaml keys set from the command line, comments and formatting kept (SPEC "Config")."""

import pytest

from manga_pipeline import config
from scripts import set_config

from tests.timing import budget

SHOWN = "zq-shown-81734"  # a value no message may show
REWRITES = "  rewrites_open: 50   # at most"  # the start of the example's export.rewrites_open line


@pytest.fixture
def cfg_file(tmp_path):
    """Return a config.yaml copied from config.yaml.example (it passes the config check)."""
    path = tmp_path / "config.yaml"
    path.write_bytes(config.EXAMPLE.read_bytes())
    return path


def run(path, *items):
    return set_config.main(["--config", str(path), *items])


def replace_line(text, start, line):
    """Return the text with the 1 line starting with `start` replaced by `line` (None drops it)."""
    lines = text.split("\n")
    i = next(n for n, old in enumerate(lines) if old.startswith(start))
    lines[i : i + 1] = [] if line is None else [line]
    return "\n".join(lines)


@budget(load="fs")
def test_a_value_is_replaced_and_its_trailing_comment_kept(cfg_file, capsys):
    before = cfg_file.read_text(encoding="utf-8")
    old = next(line for line in before.split("\n") if line.startswith(REWRITES))
    assert run(cfg_file, "export.rewrites_open=0") == 0
    expected = replace_line(before, REWRITES, old.replace("rewrites_open: 50", "rewrites_open: 0"))
    assert cfg_file.read_bytes() == expected.encode(), "only the value's text changes; the comment stays"
    assert capsys.readouterr().out == "set export.rewrites_open\n"


@budget(load="fs")
def test_a_missing_key_is_added_at_its_sections_end_with_its_indent(cfg_file, capsys):
    full = cfg_file.read_text(encoding="utf-8")
    cfg_file.write_bytes(replace_line(full, REWRITES, None).encode())
    assert run(cfg_file, "export.rewrites_open=3") == 0
    expected = replace_line(full, REWRITES, "  rewrites_open: 3")
    assert cfg_file.read_bytes() == expected.encode(), "1 new line after the section's last entry, nothing else"
    assert capsys.readouterr().out == "added export.rewrites_open\n"


@budget(load="fs")
def test_several_keys_are_set_in_1_run(cfg_file, capsys):
    assert run(cfg_file, "preflight.min_share_free_mb=12345", "export.rewrites_open=0") == 0
    cfg = config.load(cfg_file, env={})
    assert cfg["preflight"]["min_share_free_mb"] == 12345, "the first key is set"
    assert cfg["export"]["rewrites_open"] == 0, "the second key is set"
    assert capsys.readouterr().out == "set preflight.min_share_free_mb\nset export.rewrites_open\n"


@budget(load="fs")
def test_a_block_list_is_replaced_whole(cfg_file):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), "  only: []", "  only:\n    - A\n    - B  # b")
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "series.only=[C]") == 0
    assert "  only:\n    [C]  # b\n" in cfg_file.read_text(encoding="utf-8"), "the items replaced, the rest kept"
    assert config.load(cfg_file, env={})["series"]["only"] == ["C"], "the new list loads"


@pytest.mark.parametrize(
    ("items", "code"),
    [
        (["export.nope=1"], 2),  # a key config.yaml.example lacks
        (["export=1"], 2),  # a section, not a value
        (["paths.kavita_path_map={}"], 2),  # a mapping
        (["export.rewrites_open"], 2),  # no =
        (["export..rewrites_open=1"], 2),  # an empty key part
        (["export.rewrites_open=[1"], 2),  # not YAML
        (["export.rewrites_open=abc"], 1),  # fails the config check: not an int
        (["export.retention_days=1", "export.nope=1"], 2),  # all or nothing
    ],
)
@budget(load="fs")
def test_a_refused_edit_leaves_the_file_untouched(cfg_file, items, code, capsys):
    before = cfg_file.read_bytes()
    assert run(cfg_file, *items) == code, "refused with its exit code"
    assert cfg_file.read_bytes() == before, "the file is untouched"
    assert not cfg_file.with_name("config.yaml.part").exists(), "no .part left behind"
    assert "untouched" in capsys.readouterr().err, "the refusal says the file is untouched"


@budget(load="fs")
def test_a_key_whose_section_is_missing_is_refused(cfg_file, capsys):
    text = cfg_file.read_text(encoding="utf-8")
    for start in ("export:", "  retention_days:", REWRITES):
        text = replace_line(text, start, None)
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "export.rewrites_open=1") == 2, "a section is never created"
    assert cfg_file.read_bytes() == text.encode(), "the file is untouched"
    assert "section export is missing" in capsys.readouterr().err, "the refusal names the section"


@budget(load="fs")
def test_an_equal_value_is_unchanged_and_the_file_not_written(cfg_file, capsys):
    before = cfg_file.stat().st_mtime_ns
    assert run(cfg_file, "export.rewrites_open=50") == 0
    assert capsys.readouterr().out == "unchanged export.rewrites_open\n"
    assert cfg_file.stat().st_mtime_ns == before, "nothing to write"


@budget(load="fs")
def test_line_endings_stay_lf(cfg_file):
    assert run(cfg_file, "export.rewrites_open=0", "kavita.api_key=abc") == 0
    data = cfg_file.read_bytes()
    assert b"\r" not in data, "written LF, never CRLF"
    assert data.count(b"\n") == config.EXAMPLE.read_bytes().count(b"\n"), "no line added or lost"


@pytest.mark.parametrize(
    "item",
    [f"kavita.api_key={SHOWN}", f"export.rewrites_open={SHOWN}", f"export.nope={SHOWN}"],
)
@budget(load="fs")
def test_no_value_is_printed(cfg_file, item, capsys):
    run(cfg_file, item)
    run(cfg_file, f"kavita.api_key={SHOWN}")  # the old value, now SHOWN, overwritten
    run(cfg_file, "kavita.api_key=other")
    out, err = capsys.readouterr()
    assert SHOWN not in out + err, "a value, old or new, is never printed"
