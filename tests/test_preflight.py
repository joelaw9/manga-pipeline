"""preflight.run: every check runs; soft problems warn, hard ones stop the run (SPEC "Preflight")."""

import logging
import os
import secrets
import shutil
import sys
from contextlib import closing

import pytest

from manga_pipeline import config, db, preflight
from manga_pipeline.convert import vship
from manga_pipeline.preflight import HARD, SOFT, Check

from tests import pg, real_load
from tests.builders import site_cfg
from tests.fakes import convert as fake_convert
from tests.timing import budget

if sys.platform == "win32":
    import _winapi


def ok(_cfg):
    return None


def broken(text):
    return lambda _cfg: text


def with_min_free(mb, folder=None):
    cfg = {**config.cached(), "preflight": {"min_free_mb": mb}}
    if folder is not None:
        cfg["logging"] = {**cfg["logging"], "dir": str(folder)}
    return cfg


@budget(load="pure")
def test_shipped_checks():
    shipped = {c.name: (c.level, c.run) for c in vars(preflight).values() if isinstance(c, Check)}
    want = {
        "runtime version": (HARD, preflight.python_version),
        "free disk space": (SOFT, preflight.free_space),
        "folder layout": (HARD, preflight.folder_layout),
        "share folders": (HARD, preflight.share_folders),
        "workflow settings": (HARD, preflight.workflow_settings),
        "service settings": (HARD, preflight.service_settings),
        "share ownership": (HARD, preflight.share_ownership),
        "staging folder": (HARD, preflight.staging_folder),
        "image tools": (HARD, preflight.image_tools),
        "scorer": (HARD, preflight.scorer),
        "scratch space": (HARD, preflight.scratch_space),
        "OCR device": (HARD, preflight.ocr_device),
        "junk word lists": (HARD, preflight.junk_word_lists),
        "Claude settings": (HARD, preflight.claude_settings),
        "naming overrides": (HARD, preflight.naming_overrides),
        "fleet version": (HARD, preflight.fleet_version),
        "long paths": (HARD, preflight.long_paths),
    }
    assert shipped == want, "SPEC 'Preflight' lists every check with its kind"


@budget(load="pure")
def test_all_passing_returns_no_problems():
    assert preflight.run(config.cached(), [Check("a", HARD, ok), Check("b", SOFT, ok)]) == []


@budget(load="pure")
def test_soft_problem_is_logged_and_the_run_goes_on(caplog):
    with caplog.at_level(logging.WARNING, logger="manga_pipeline"):
        soft = preflight.run(config.cached(), [Check("disk", SOFT, broken("low"))])
    assert soft == ["disk: low"]
    assert "disk: low" in caplog.text


@budget(load="pure")
def test_hard_problems_stop_the_run_after_every_check_ran():
    seen = []
    checks = [
        Check("a", HARD, broken("first")),
        Check("b", SOFT, lambda _cfg: seen.append("b")),
        Check("c", HARD, broken("second")),
    ]
    with pytest.raises(preflight.PreflightFailed, match="a: first; c: second"):
        preflight.run(config.cached(), checks)
    assert seen == ["b"], "a hard failure must not skip the later checks"


@budget(load="pure")
def test_unknown_level_is_refused_before_any_check_runs():
    with pytest.raises(ValueError, match="HARD or SOFT: needs-x"):
        preflight.run(config.cached(), [Check("needs-x", "Hard", broken("missing"))])


@budget(load="pure")
def test_python_version_refuses_older_than_3_13():
    assert preflight.python_version(config.cached(), (3, 12, 9)) == "Python 3.13 or later is required, this is 3.12"
    assert preflight.python_version(config.cached(), (3, 13, 0)) is None


@budget(load="fs")
def test_free_space_compares_mib(tmp_path):
    free = shutil.disk_usage(tmp_path).free // preflight.MIB
    # 2% margins: other writers may change the free space meanwhile; MB instead of MiB would be 4.9% off.
    assert "below preflight.min_free_mb" in preflight.free_space(with_min_free(int(free * 1.02) + 1, tmp_path))
    assert preflight.free_space(with_min_free(int(free * 0.98), tmp_path)) is None


@budget(load="fs")
def test_zero_skips_the_check_and_negative_is_a_problem(tmp_path):
    missing = tmp_path / "missing"
    assert preflight.free_space(with_min_free(0, missing)) is None, "0 skips the check without reading the disk"
    assert preflight.free_space(with_min_free(-5, tmp_path)) == "preflight.min_free_mb must be 0 or more, got -5"


@budget(load="fs")
def test_free_space_reports_an_unreadable_drive(tmp_path, monkeypatch):
    def unreadable(_path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(shutil, "disk_usage", unreadable)
    assert preflight.free_space(with_min_free(1, tmp_path)).startswith("cannot read free space at")


@budget(load="fs")
def test_free_space_measures_the_drive_of_a_log_folder_not_made_yet(tmp_path):
    missing = tmp_path / "logs" / "not-yet"
    assert preflight.free_space(with_min_free(1, missing)) is None, "a console-only command never makes logging.dir"
    assert not (tmp_path / "logs").exists(), "the check creates no folder"


def site(tmp_path, database=pg.UNASSIGNED):
    """Config with the share folders under tmp_path/share and the database named database (default: one never
    built, for checks that open none)."""
    share = tmp_path / "share"
    paths = {key: str(share / key) for key in preflight.SHARE_FOLDERS} | {
        "scratch": str(tmp_path / "scratch"),
        "naming_overrides": str(tmp_path / "overrides"),
    }
    return {**pg.cfg_for(database), "paths": paths}


def adopted(tmp_path, owner=None):
    """A database and an owner file holding owner (default: that database's id)."""
    cfg = site(tmp_path, pg.process.database(tmp_path.resolve(), built=True))
    with closing(db.connect(cfg)) as conn:
        mine = db.database_id(conn)
    path = preflight.owner_file(cfg)
    path.parent.mkdir(parents=True)
    path.write_text(mine if owner is None else owner, encoding="utf-8")
    return cfg


@budget(load="db")
def test_share_owned_by_this_database_passes(tmp_path):
    assert preflight.share_ownership(adopted(tmp_path)) is None


@budget(load="db")
def test_share_owned_by_another_database_is_a_problem(tmp_path):
    assert "holds database id 'other'" in preflight.share_ownership(adopted(tmp_path, owner="other"))


@budget(load="fs")
def test_missing_owner_file_is_a_problem(tmp_path):
    problem = preflight.share_ownership(site(tmp_path))
    assert "is missing: the share is not mounted" in problem


@budget(load="fs")
def test_owner_file_without_a_database_is_a_problem_and_creates_none(tmp_path):
    cfg = site(tmp_path, f"{pg.PREFIX}missing_{secrets.token_hex(4)}")
    preflight.owner_file(cfg).parent.mkdir(parents=True)
    preflight.owner_file(cfg).write_text("abc", encoding="utf-8")
    assert "there is no database" in preflight.share_ownership(cfg)
    assert not db.built(cfg), "a check never creates the database"


def layout(tmp_path, **paths):
    """site() with some paths.* replaced."""
    cfg = site(tmp_path)
    return {**cfg, "paths": {**cfg["paths"], **{k: str(v) for k, v in paths.items()}}}


@budget(load="pure")
@pytest.mark.parametrize(
    ("case", "paths", "problem"),
    [
        ("staging = share root", {"staging": "share"}, "paths.staging {share} contains paths.downloads"),
        ("scratch = project root", {"scratch": config.ROOT}, "contains the project root"),
        ("staging in the library", {"staging": "share/library/Series"}, "lies inside paths.library"),
        ("scratch holds the covers", {"covers": "scratch/covers"}, "contains paths.covers"),
    ],
)
def test_folders_state_reset_empties_hold_nothing_kept(tmp_path, case, paths, problem):
    paths = {k: v if isinstance(v, type(config.ROOT)) else tmp_path / v for k, v in paths.items()}
    text = preflight.folder_layout(layout(tmp_path, **paths))
    want = problem.format(share=tmp_path.resolve() / "share", state=tmp_path.resolve() / "state")
    assert want in text, f"{case}: state reset would delete what the pipeline keeps"


@budget(load="pure")
def test_example_folder_layout_passes():
    assert preflight.folder_layout(real_load(config.EXAMPLE)) is None


def dir_link(link, target):
    """A link to a folder that needs no privilege: a junction on Windows, a symlink elsewhere."""
    if sys.platform == "win32":
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@budget(load="fs")
def test_staging_linked_into_the_library_is_a_problem(tmp_path):
    library = tmp_path / "share" / "library"
    (library / "Series").mkdir(parents=True)
    dir_link(tmp_path / "staging-link", library / "Series")
    text = preflight.folder_layout(layout(tmp_path, staging=tmp_path / "staging-link"))
    assert "lies inside paths.library" in text, "a link is judged by where it leads"


@budget(load="pure")
@pytest.mark.parametrize(
    ("paths", "problem"),
    [
        ({"staging": "x/../scratch"}, "contains paths.scratch"),
        ({"scratch": "state/.."}, "contains the project root"),
        ({"scratch": "."}, "contains the project root"),
    ],
    ids=["staging-is-scratch", "scratch-dotdot-root", "scratch-dot"],
)
def test_folder_layout_compares_resolved_paths(tmp_path, paths, problem):
    paths = {k: (tmp_path / v if k == "staging" else v) for k, v in paths.items()}
    cfg = layout(tmp_path, **paths)
    if "scratch" in paths:
        cfg["paths"]["scratch"] = paths["scratch"]  # relative: to the project root
    else:
        cfg["paths"]["scratch"] = str(tmp_path / "scratch")
    assert problem in preflight.folder_layout(cfg)


@budget(load="pure")
@pytest.mark.skipif(os.name != "nt", reason="Windows paths compare without case")
def test_folder_layout_on_windows_ignores_letter_case(tmp_path):
    library = str(tmp_path / "share" / "library")
    cfg = layout(tmp_path, staging=library.upper())
    assert "contains paths.library" in preflight.folder_layout(cfg)


@budget(load="pure")
def test_junk_word_lists_refuse_a_weak_or_paired_term_that_is_no_keyword():
    junk = config.cached()["junk"]
    cfg = {**config.cached(), "junk": {**junk, "weak": [*junk["weak"], "stray"], "paired": [*junk["paired"], "odd"]}}
    assert preflight.junk_word_lists(cfg) == (
        "junk.weak terms not in junk.keywords: ['stray']; junk.paired terms not in junk.keywords: ['odd']"
    ), "a weak or paired term only restricts a keyword: one that is no keyword would do nothing"
    assert preflight.junk_word_lists(config.cached()) is None


@budget(load="pure")
def test_workflow_settings_refuse_an_attempt_limit_below_1():
    cfg = {**config.cached(), "workflow": {"attempt_limit": 0}}
    assert preflight.workflow_settings(cfg) == "workflow.attempt_limit must be at least 1, got 0"
    assert preflight.workflow_settings(config.cached()) is None


@budget(load="pure")
def test_workflow_settings_refuse_a_series_named_by_a_number():
    cfg = {**config.cached(), "series": {"only": ["Alpha", 86]}}  # YAML reads an unquoted 86 as a number
    assert preflight.workflow_settings(cfg) == "series.only must list series names as text, got ['Alpha', 86]"


@budget(load="pure")
def test_workflow_settings_refuse_a_kavita_library_id_below_1():
    cfg = {**config.cached(), "kavita": {**config.cached()["kavita"], "library_id": 0}}
    assert preflight.workflow_settings(cfg) == "kavita.library_id must be at least 1, got 0", "scope reads it"


def services(**changes):
    """The test config with (section, key) = value changes, e.g. kavita__url="x"."""
    cfg = {**config.cached()}
    for name, value in changes.items():
        section, key = name.split("__")
        cfg[section] = {**cfg[section], key: value}
    return cfg


@budget(load="pure")
def test_service_settings_pass_with_urls_a_key_and_a_path_map():
    assert preflight.service_settings(config.cached()) is None


@budget(load="pure")
@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"suwayomi__url": ""}, "suwayomi.url must be an http or https URL"),
        ({"suwayomi__url": "localhost:4567"}, "suwayomi.url must be an http or https URL"),
        ({"kavita__url": "ftp://kavita"}, "kavita.url must be an http or https URL"),
        ({"kavita__url": "http://"}, "kavita.url must be an http or https URL"),
        ({"kavita__api_key": None}, "kavita.api_key is not set (env MANGA_PIPELINE_KAVITA_API_KEY)"),
        ({"kavita__api_key": "  "}, "kavita.api_key is not set (env MANGA_PIPELINE_KAVITA_API_KEY)"),
        ({"kavita__api_key": 12345}, "kavita.api_key is not set (env MANGA_PIPELINE_KAVITA_API_KEY)"),
        ({"paths__kavita_path_map": {}}, "paths.kavita_path_map is empty"),
        ({"paths__kavita_path_map": {"/kavita": ""}}, "must map non-empty strings to non-empty strings"),
        ({"paths__kavita_path_map": {"/kavita": 5}}, "must map non-empty strings to non-empty strings"),
        ({"paths__kavita_path_map": {"": "/mnt"}}, "must map non-empty strings to non-empty strings"),
        ({"paths__kavita_path_map": {"/manga": "/mnt/a", "/manga/": "/mnt/b"}}, "same prefix but for a trailing /"),
    ],
)
def test_service_settings_name_what_is_missing(change, problem):
    assert problem in preflight.service_settings(services(**change)), "SPEC 'Preflight': service settings"


@budget(load="pure")
def test_service_settings_report_every_problem_and_never_show_a_value():
    cfg = services(suwayomi__url="nope", kavita__api_key=None, paths__kavita_path_map={})
    got = preflight.service_settings(cfg)
    assert got.count(";") == 2, "every problem is named"
    cfg = services(kavita__url="secret-looking-value", kavita__api_key=0)
    assert "secret-looking-value" not in preflight.service_settings(cfg), "a setting's value is never shown"


@budget(load="fs")
def test_share_folders_missing_is_a_problem(tmp_path):
    cfg = site(tmp_path)
    (tmp_path / "share" / "library").mkdir(parents=True)
    text = preflight.share_folders(cfg)
    assert "paths.downloads" in text
    assert "is not an existing folder" in text
    assert "paths.library" not in text


@budget(load="pure")
def test_folder_checks_report_what_they_cannot_measure(tmp_path, monkeypatch):
    def broken(_self, *_args, **_kwargs):
        raise OSError(5, "Input/output error")

    cfg = site(tmp_path)
    monkeypatch.setattr(type(tmp_path), "resolve", broken)
    assert preflight.folder_layout(cfg).startswith("cannot resolve a configured folder")
    monkeypatch.setattr(type(tmp_path), "is_dir", broken)
    assert preflight.staging_folder(cfg).startswith("cannot read paths.staging")


@budget(load="fs")
def test_unreadable_owner_file_is_a_problem(tmp_path):
    cfg = site(tmp_path)
    preflight.owner_file(cfg).mkdir(parents=True)  # a folder where the file should be: reading it fails
    assert preflight.share_ownership(cfg).startswith(f"cannot read {preflight.owner_file(cfg)}")


@budget(load="db")
def test_share_ownership_applies_no_schema_step(tmp_path, monkeypatch):
    cfg = adopted(tmp_path)
    folder = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, folder)
    (folder / "0099_new.sql").write_text("CREATE TABLE n (x BIGINT);", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA", folder)
    assert preflight.share_ownership(cfg) is None
    with closing(pg._connect(cfg["database"]["name"])) as conn:
        steps = [r[0] for r in conn.execute("SELECT name FROM schema_step")]
    assert "0099_new" not in steps, "a check never upgrades the database"


@budget(load="pure")
@pytest.mark.parametrize(
    ("section", "key", "value", "want"),
    [
        ("kavita", "rescan_after_minutes", 0, "kavita.rescan_after_minutes must be at least 1, got 0"),
        ("export", "retention_days", -1, "export.retention_days must be at least 0, got -1"),
        ("intake", "settle_minutes", 0, "intake.settle_minutes must be at least 1, got 0"),
    ],
)
def test_workflow_settings_check_the_rescan_retention_and_settle_periods(section, key, value, want):
    cfg = {**config.cached(), section: {**config.cached()[section], key: value}}
    assert preflight.workflow_settings(cfg) == want, "a period below its floor is refused"


@budget(load="pure")
def test_share_folders_need_the_mangas_folder_and_retention(tmp_path):
    cfg = site_cfg(tmp_path)
    (tmp_path / "share" / "downloads" / "mangas").rmdir()
    (tmp_path / "share" / "retention").rmdir()
    (tmp_path / "share" / "candidates").rmdir()
    (tmp_path / "overrides").rmdir()
    text = preflight.share_folders(cfg)
    assert "the mangas folder of paths.downloads" in text, "downloads lie under mangas (SPEC Suwayomi)"
    assert "paths.retention" in text, "removed downloads go to the retention folder"
    assert "paths.candidates" in text, "the candidates folder is on the share: every machine writes it"
    assert "paths.naming_overrides" not in text, "the overrides have a check of their own"
    assert "paths.naming_overrides" in preflight.naming_overrides(cfg), "the overrides' own check"


@budget(load="pure")
def test_share_folders_need_retention_on_the_downloads_filesystem(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    assert preflight.share_folders(cfg) is None
    real_stat = os.stat
    retention = str(config.abs_path(cfg["paths"]["retention"]))

    def other_device(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        return os.stat_result((*st[:2], st.st_dev + 1, *st[3:])) if str(path) == retention else st

    monkeypatch.setattr(preflight.os, "stat", other_device)
    assert "same filesystem" in preflight.share_folders(cfg), "a download moves to retention by rename"


@budget(load="pure")
@pytest.mark.parametrize("inside", ["downloads", "library"])
def test_folder_layout_refuses_retention_overlapping_a_share_folder(tmp_path, inside):
    cfg = site_cfg(tmp_path)
    cfg["paths"]["retention"] = str(tmp_path / "share" / inside / "kept")
    assert "paths.retention" in preflight.folder_layout(cfg), "retention lies outside downloads and every library"
    assert preflight.folder_layout(site_cfg(tmp_path)) is None


# ---------------------------------------------------------------- process: image tools, scorer, scratch space


def with_tools(folder, **paths):
    names = ("avifenc", "avifdec", "jpegtran", "oxipng", "vship")
    return {**config.cached(), "tools": {n: str(paths.get(n, folder / n)) for n in names}}


@budget(load="fs")
def test_image_tools_names_every_missing_tool(tmp_path):
    for name in ("avifenc", "avifdec", "jpegtran", "oxipng", "vship"):
        (tmp_path / name).touch()
    assert preflight.image_tools(with_tools(tmp_path)) is None
    problem = preflight.image_tools(with_tools(tmp_path, oxipng=tmp_path / "gone", vship=tmp_path))
    assert "tools.oxipng" in problem, "a missing file and a folder are both problems"
    assert "tools.vship" in problem, "a missing file and a folder are both problems"
    assert "tools.avifenc" not in problem, "a present tool is not named"


@budget(load="fs")
def test_scorer_reports_a_library_that_does_not_load(tmp_path, monkeypatch):
    monkeypatch.setattr(vship, "Scorer", fake_convert.REAL_SCORER)
    (tmp_path / "vship").write_bytes(b"not a library")
    problem = preflight.scorer(with_tools(tmp_path))
    assert problem is not None
    assert "cannot load the Vship library" in problem


@budget(load="fs")
def test_scratch_space_measures_the_scratch_drive(tmp_path):
    free = shutil.disk_usage(tmp_path).free // preflight.MIB

    def cfg(mb, folder):
        return {
            **config.cached(),
            "paths": {**config.cached()["paths"], "scratch": str(folder)},
            "convert": {**config.cached()["convert"], "min_scratch_free_mb": mb},
        }

    assert "below convert.min_scratch_free_mb" in preflight.scratch_space(cfg(int(free * 1.02) + 1, tmp_path))
    assert preflight.scratch_space(cfg(int(free * 0.98), tmp_path / "not" / "yet")) is None, "nearest existing folder"
    assert preflight.scratch_space(cfg(-1, tmp_path)) == "convert.min_scratch_free_mb must be 0 or more, got -1"


@budget(load="pure")
@pytest.mark.parametrize(
    ("value", "problem"),
    [(1, None), (0, "LongPathsEnabled under HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem is 0: set it to 1")],
)
def test_long_paths_on_windows_need_the_registry_value_1(monkeypatch, value, problem):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(preflight, "long_paths_value", lambda: value)
    got = preflight.long_paths(config.cached())
    assert got == problem or got.startswith(problem), "a path of 260+ characters opens only with the value 1"


@budget(load="pure")
def test_long_paths_absent_is_a_problem(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(preflight, "long_paths_value", lambda: None)
    assert "LongPathsEnabled under HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem is absent: set it to 1" in (
        preflight.long_paths(config.cached())
    ), "Windows' default (no value) is off"


@budget(load="pure")
def test_long_paths_are_no_problem_off_windows(monkeypatch):
    def unread():
        msg = "no registry off Windows"
        raise AssertionError(msg)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(preflight, "long_paths_value", unread)
    assert preflight.long_paths(config.cached()) is None, "only Windows limits paths to 260 characters"


@budget(load="pure")
def test_workflow_settings_refuse_convert_workers_below_1():
    cfg = {**config.cached(), "convert": {**config.cached()["convert"], "workers": 0}}
    assert preflight.workflow_settings(cfg) == "convert.workers must be at least 1, got 0"
