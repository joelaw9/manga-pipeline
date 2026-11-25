"""scripts/set_config.py: config.yaml keys set from the command line, comments and formatting kept (SPEC "Config")."""

import os
import stat
import subprocess
import sys
from pathlib import Path

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
    [f"kavita.api_key={SHOWN}", f"export.rewrites_open={SHOWN}", f"export.nope={SHOWN}", SHOWN],
)
@budget(load="fs")
def test_no_value_or_argument_is_printed(cfg_file, item, capsys):
    run(cfg_file, item)
    run(cfg_file, f"kavita.api_key={SHOWN}")  # the old value, now SHOWN, overwritten
    run(cfg_file, "kavita.api_key=other")
    out, err = capsys.readouterr()
    assert SHOWN not in out + err, "a value, old or new, or an argument lacking its = is never printed"


def refused_by_the_edit_check(cfg_file, text, item, capsys):
    """Assert the item is refused with exit 1 by the edit check on a file holding the text, left untouched."""
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, item) == 1, "the edit check fails"
    assert cfg_file.read_bytes() == text.encode(), "the file is untouched"
    assert "does not load as the original" in capsys.readouterr().err, "the edit check names its failure"


@budget(load="fs")
def test_a_duplicate_key_is_refused_by_the_edit_check(cfg_file, capsys):
    text = cfg_file.read_text(encoding="utf-8")
    old = next(line for line in text.split("\n") if line.startswith(REWRITES))
    text = replace_line(text, REWRITES, f"{old}\n  rewrites_open: 50")
    refused_by_the_edit_check(cfg_file, text, "export.rewrites_open=0", capsys)


@budget(load="fs")
def test_a_repeated_section_is_refused_by_the_edit_check(cfg_file, capsys):
    text = cfg_file.read_text(encoding="utf-8") + "export:\n  retention_days: 7\n  rewrites_open: 50\n"
    refused_by_the_edit_check(cfg_file, text, "export.rewrites_open=0", capsys)


@budget(load="fs")
def test_an_add_through_an_alias_is_refused_by_the_edit_check(cfg_file, capsys):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), "notify:", "notify: &n")
    for start in ("export:", "  retention_days:", REWRITES):
        text = replace_line(text, start, None)
    refused_by_the_edit_check(cfg_file, text + "export: *n\n", "export.retention_days=7", capsys)


@budget(load="fs")
def test_an_edit_through_an_alias_is_refused_by_the_edit_check(cfg_file, capsys):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), "  retention_days:", "  retention_days: &d 7")
    text = replace_line(text, REWRITES, "  rewrites_open: *d")
    refused_by_the_edit_check(cfg_file, text, "export.rewrites_open=0", capsys)


@budget(load="fs")
def test_a_value_breaking_its_flow_mapping_is_refused_by_the_edit_check(cfg_file, capsys):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), "  retention_days:", None)
    text = replace_line(text, REWRITES, None)
    text = replace_line(text, "export:", "export: {retention_days: 7, rewrites_open: 50}")
    refused_by_the_edit_check(cfg_file, text, "export.rewrites_open=1, 2", capsys)


@pytest.mark.parametrize("empty", ["  rewrites_open:", "  rewrites_open:   # c"])
@budget(load="fs")
def test_an_empty_value_is_set(cfg_file, empty, capsys):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), REWRITES, empty)
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "export.rewrites_open=5") == 0
    expected = replace_line(text, empty, empty.replace("rewrites_open:", "rewrites_open: 5"))
    assert cfg_file.read_bytes() == expected.encode(), "the value written after the colon, the comment kept"
    assert capsys.readouterr().out == "set export.rewrites_open\n"


@budget(load="fs")
def test_an_added_line_in_a_crlf_file_ends_crlf(cfg_file):
    full = cfg_file.read_text(encoding="utf-8")
    cfg_file.write_bytes(replace_line(full, REWRITES, None).replace("\n", "\r\n").encode())
    assert run(cfg_file, "export.rewrites_open=3") == 0
    expected = replace_line(full, REWRITES, "  rewrites_open: 3").replace("\n", "\r\n")
    assert cfg_file.read_bytes() == expected.encode(), "the file keeps CRLF, the added line too"


@budget(load="fs")
def test_a_key_is_added_after_a_block_scalar_ending_its_section(cfg_file):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), '  url: "http://localhost:5000"', None)
    text = replace_line(text, "  rescan_after_minutes:", "  url: >-\n    http://localhost:5000")
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "kavita.rescan_after_minutes=60") == 0
    block = "  url: >-\n    http://localhost:5000\n"
    expected = text.replace(block, f"{block}  rescan_after_minutes: 60\n")
    assert cfg_file.read_bytes() == expected.encode(), "the new line right after the block's text, at the indent"


@budget(load="fs")
def test_a_block_scalar_value_is_refused(cfg_file, capsys):
    text = replace_line(cfg_file.read_text(encoding="utf-8"), REWRITES, "  rewrites_open: |\n    50")
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "export.rewrites_open=0") == 2, "a block scalar is edited by hand"
    assert cfg_file.read_bytes() == text.encode(), "the file is untouched"
    assert "block scalar" in capsys.readouterr().err, "the refusal says why"


@budget(load="fs")
def test_a_refusal_mid_pass_applies_no_key(cfg_file):
    text = cfg_file.read_text(encoding="utf-8")
    for start in ("notify:", "  discord_webhook:"):
        text = replace_line(text, start, None)
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "export.rewrites_open=0", "notify.discord_webhook=null") == 2, "the 2nd section is missing"
    assert cfg_file.read_bytes() == text.encode(), "the 1st key is not applied either"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
@budget(load="fs")
def test_the_files_mode_is_kept(cfg_file):
    cfg_file.chmod(0o600)
    assert run(cfg_file, "export.rewrites_open=0") == 0
    assert cfg_file.stat().st_mode & 0o777 == 0o600, "a private file stays private"


@budget(load="fs")
def test_a_failed_replace_leaves_the_file_and_no_part(cfg_file, monkeypatch, capsys):
    before = cfg_file.read_bytes()

    def in_use(*_):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(Path, "replace", in_use)
    assert run(cfg_file, "export.rewrites_open=0") == 1, "a write failure exits 1"
    assert cfg_file.read_bytes() == before, "the file is untouched"
    assert not cfg_file.with_name("config.yaml.part").exists(), "no .part left behind"
    assert "cannot write" in capsys.readouterr().err, "the failure says what failed"


@budget(load="subprocess")
def test_the_script_runs_as_a_module(cfg_file):
    done = subprocess.run(
        [sys.executable, "-m", "scripts.set_config", "--config", str(cfg_file), "export.rewrites_open=0"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "set export.rewrites_open\n"), done.stderr
    assert config.load(cfg_file, env={})["export"]["rewrites_open"] == 0, "the file is written"


@budget(load="fs")
def test_an_unchanged_1st_copy_of_a_duplicate_key_is_refused(cfg_file, capsys):
    text = cfg_file.read_text(encoding="utf-8")
    old = next(line for line in text.split("\n") if line.startswith(REWRITES))
    text = replace_line(text, REWRITES, f"{old}\n  rewrites_open: 7")
    refused_by_the_edit_check(cfg_file, text, "export.rewrites_open=50", capsys)


@budget(load="fs")
def test_an_unchanged_key_in_a_repeated_section_is_refused(cfg_file, capsys):
    text = cfg_file.read_text(encoding="utf-8") + "export:\n  retention_days: 7\n  rewrites_open: 7\n"
    refused_by_the_edit_check(cfg_file, text, "export.rewrites_open=50", capsys)


@budget(load="fs")
def test_a_read_only_file_is_refused_up_front(cfg_file, capsys):
    before = cfg_file.read_bytes()
    cfg_file.chmod(stat.S_IREAD)
    try:
        assert run(cfg_file, "export.rewrites_open=0") == 2, "a read-only file is bad input"
    finally:
        cfg_file.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert cfg_file.read_bytes() == before, "the file is untouched"
    assert not cfg_file.with_name("config.yaml.part").exists(), "no .part made"
    assert "read-only" in capsys.readouterr().err, "the refusal says why"


@pytest.mark.skipif(os.name != "posix", reason="POSIX owners")
@budget(load="fs")
def test_the_files_owner_is_kept(cfg_file):
    before = cfg_file.stat()
    assert run(cfg_file, "export.rewrites_open=0") == 0
    after = cfg_file.stat()
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid), "the owner stays"


@budget(load="fs")
def test_the_program_check_sees_the_environment(cfg_file, monkeypatch, capsys):
    monkeypatch.setenv("MANGA_PIPELINE_KAVITA_API_KEY", "from-env")
    text = replace_line(cfg_file.read_text(encoding="utf-8"), "  api_key:", None)
    cfg_file.write_bytes(text.encode())
    assert run(cfg_file, "export.rewrites_open=0") == 0, "a secret only the environment holds passes the check"
    assert capsys.readouterr().out == "set export.rewrites_open\n"


@budget(load="fs")
def test_the_program_checks_error_names_the_file_not_its_part(cfg_file, capsys):
    assert run(cfg_file, "export.rewrites_open=abc") == 1
    err = capsys.readouterr().err
    assert ".part" not in err, "the error names the config file"
    assert str(cfg_file) in err, "the error names the config file"
