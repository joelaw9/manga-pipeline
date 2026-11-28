"""Restores (SPEC "Retroactive updates"; plan phase-10-9b): a file's previous version put back from retention.

Each test starts from a real replace through the parts (a conversion, a better copy, a rebuild), then restores it.
"""

from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import cli, config, db, export, intake, process, rewrite, workflow
from manga_pipeline import restore as restore_module
from manga_pipeline.errors import Unreachable

from tests.builders import config_file
from tests.test_better_copies import copy, through
from tests.test_better_copies import library as bulk_library
from tests.test_conversions import SERIES, adopt, layout_override, report, set_cap
from tests.test_cover_rewrites import bulk_file, recorded_covers
from tests.test_rebuilds import file_row, published, rebuild, run_rebuild
from tests.test_rewrites import crash_after, progress, read, scan
from tests.timing import budget
from tests.world import World, cbz

KEPT = tuple(c for c in rewrite.VERSION if c != "size")  # size: the file's, which an adopted row does not store


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def path_of(world, row):
    return config.to_local(world.cfg, row["library_path"])


def content(world, stored):
    return config.to_local(world.cfg, stored).read_bytes()


def converted(world, names=("Ch.1.cbz",)):
    """Adopt files (4 pages, no ComicInfo.xml), convert them and let Kavita re-read them.

    Returns (Kavita series id, {name: adopted row before its conversion}, {name: adopted file's bytes}). The series'
    layout is pages: a conversion keeps the 4 pages, so a reader's progress partway is kept partway.
    """
    layout_override(world)
    kid = adopt(world, {n: cbz(seed=f"a {n}", pages=4) for n in names})
    rows = {r["library_path"].rsplit("/", 1)[1]: r for (r,) in world.rows("SELECT to_jsonb(p) FROM published_file p")}
    data = {n: (world.library / SERIES / n).read_bytes() for n in names}
    set_cap(world, len(names))
    for part in (intake, process, export):
        world.run(part)
    world.kavita.run_scans()
    scan(world)
    world.run(export)  # Kavita re-read the converted files: no longer busy
    assert world.rows("SELECT DISTINCT origin FROM published_file") == [("pipeline",)], "set up: converted"
    return kid, rows, data


def restore(world, *file_ids):
    with closing(db.connect(world.cfg)) as conn:
        return restore_module.run(world.context(conn, "export"), list(file_ids))


def reread(world):
    """Let Kavita re-read the restored files and the next export end their wait."""
    scan(world)
    world.run(export)


@budget(load="db", seconds=60)
def test_a_converted_adopted_file_is_restored_with_its_bytes_name_row_and_progress_and_named_again_swaps_back(world):
    kid, adopted, adopted_bytes = converted(world)
    before = adopted["Ch.1.cbz"]
    conversion = file_row(world, before["id"])
    conversion_bytes = content(world, conversion["library_path"])
    assert conversion_bytes != adopted_bytes["Ch.1.cbz"], "set up: the conversion changed the file"
    read(world, kid, "Ch.1.cbz", 2)
    assert restore(world, before["id"]) == (1, [])
    world.run(export)
    after = file_row(world, before["id"])
    assert path_of(world, after).read_bytes() == adopted_bytes["Ch.1.cbz"], "the adopted bytes back"
    assert {k: after[k] for k in KEPT} == {k: before[k] for k in KEPT}, "the row before the conversion back"
    assert after["size"] == path_of(world, after).stat().st_size, "the size of the file put back"
    assert content(world, after["previous"]) == conversion_bytes, "the converted version is its previous now"
    reread(world)
    assert progress(world, kid)["Ch.1.cbz"][0] == 2, "the progress carried"
    assert file_row(world, before["id"])["indexed_since"] is None, "Kavita re-read it at its size: the wait ended"
    lines = report(world)
    assert "  1 restored adopted file(s), not converted again until a person acts" in lines
    assert not [line for line in lines if "left as it is" in line], "a restored file is counted, not listed"
    assert restore(world, before["id"]) == (1, []), "named again"
    world.run(export)
    again = file_row(world, before["id"])
    assert content(world, again["library_path"]) == conversion_bytes, "the converted file back"
    assert {k: again[k] for k in rewrite.VERSION} == {k: conversion[k] for k in rewrite.VERSION}, "its row back"


@budget(load="db", seconds=40)
def test_a_better_copy_that_renamed_its_file_is_restored_under_the_old_name(world):
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", "1"), ("Chapter 2.cbz", "Grp_Chapter 2", "2", "1"))
    world.kavita_series(SERIES, bulk=bulk, basis="filename")
    for name, number in (("Vol.1 Ch.1.cbz", 1), ("Chapter 2.cbz", 2)):
        (world.library / SERIES / name).write_bytes(bulk_file(name, number))
    world.kavita.scan_library(4)
    world.kavita.run_scans()
    world.run(intake)
    world.run(export)
    ((before,),) = world.rows("SELECT to_jsonb(p) FROM published_file p WHERE library_path LIKE '%%Chapter 2.cbz'")
    old_bytes = (world.library / SERIES / "Chapter 2.cbz").read_bytes()
    copy(world, width=20)
    through(world)
    reread(world)
    copied = file_row(world, before["id"])
    assert copied["library_path"] != before["library_path"], "set up: the copy renamed the file"
    (world.library / SERIES / "Chapter 2.cbz").write_bytes(b"another file")
    assert restore(world, before["id"]) == (
        0,
        [f"file {before['id']}: its previous name {before['library_path']} is taken"],
    )
    (world.library / SERIES / "Chapter 2.cbz").unlink()
    assert restore(world, before["id"]) == (1, [])
    world.run(export)
    after = file_row(world, before["id"])
    assert after["library_path"] == before["library_path"], "under its old name"
    assert (world.library / SERIES / "Chapter 2.cbz").read_bytes() == old_bytes, "the bulk file back"
    assert not path_of(world, copied).exists(), "the copy's name is gone"
    assert {k: after[k] for k in KEPT} == {k: before[k] for k in KEPT}


@budget(load="db", seconds=60)
def test_a_rebuild_is_restored(world):
    before = published(world)
    first = content(world, before["library_path"])
    assert rebuild(world, before["id"]) == (1, [])
    run_rebuild(world)
    reread(world)
    assert restore(world, before["id"]) == (1, [])
    world.run(export)
    after = file_row(world, before["id"])
    assert content(world, after["library_path"]) == first
    assert {k: after[k] for k in rewrite.VERSION} == {k: before[k] for k in rewrite.VERSION}, "the first publish's row"


@budget(load="db", seconds=40)
def test_a_hand_edited_row_and_a_null_cover_state_are_restored_as_stored(world):
    _kid, adopted, _ = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    world.rows(  # the replaced row as a hand edit and a cover not read yet leave it
        """UPDATE published_file SET previous_values = jsonb_set(jsonb_set(previous_values, '{before,unedited}',
            '{"title": "Old"}'), '{before,cover_state}', 'null') WHERE id = %s RETURNING id""",
        file_id,
    )
    assert restore(world, file_id) == (1, [])
    world.run(export)
    after = file_row(world, file_id)
    assert (after["unedited"], after["cover_state"], after["pending_path"]) == ({"title": "Old"}, None, None)


@budget(load="db", seconds=60)
@pytest.mark.parametrize("purged", [False, True], ids=["kept", "previous-purged"])
def test_a_crash_after_a_same_name_restores_rename_is_settled_by_the_next_export(world, monkeypatch, purged):
    _kid, adopted, adopted_bytes = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    restore(world, file_id)
    ((previous,),) = world.rows("SELECT pending_copy ->> 'previous' FROM published_file WHERE id = %s", file_id)
    crash_after(monkeypatch, rewrite.STEP_RENAMED, "Ch.1.cbz")
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    assert world.rows("SELECT pending_step FROM published_file") == [(rewrite.STEP_RETAINED,)], "set up: unrecorded"
    if purged:
        config.to_local(world.cfg, previous).unlink()
    world.run(export)
    after = file_row(world, file_id)
    assert (after["origin"], after["pending_path"]) == ("library", None), (
        "settled: an adopted file holds no ComicInfo.xml, so only the inode test can tell its rename ran"
    )
    assert path_of(world, after).read_bytes() == adopted_bytes["Ch.1.cbz"]
    assert after["size"] == path_of(world, after).stat().st_size


@budget(load="db", seconds=60)
@pytest.mark.parametrize("unlinked", [False, True], ids=["retried", "link-made-again"])
def test_a_restore_whose_rename_fails_once_finishes_and_carries_the_progress(world, monkeypatch, unlinked):
    kid, adopted, adopted_bytes = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    read(world, kid, "Ch.1.cbz", 2)
    restore(world, file_id)
    real = Path.replace

    def denied(self, target):
        if self.name.startswith("restore-"):
            msg = "denied"
            raise PermissionError(msg)
        return real(self, target)

    monkeypatch.setattr(Path, "replace", denied)
    world.run(export)
    monkeypatch.undo()
    ((step, error, staged),) = world.rows("SELECT pending_step, pending_error, pending_staged FROM published_file")
    assert step == rewrite.STEP_RETAINED, "set up: failed at its rename"
    assert "denied" in error, "set up: failed at its rename"
    if unlinked:  # its staged link gone after the retention act, before the rename
        config.to_local(world.cfg, staged).unlink()
    world.run(export)
    after = file_row(world, file_id)
    assert (after["origin"], after["pending_error"], after["pending_attempts"]) == ("library", None, 0)
    assert path_of(world, after).read_bytes() == adopted_bytes["Ch.1.cbz"]
    reread(world)
    assert file_row(world, file_id)["indexed_since"] is None, "the error cleared: the progress wait ends"
    assert progress(world, kid)["Ch.1.cbz"][0] == 2


@budget(load="db", seconds=40)
def test_a_rewrite_queued_between_the_first_read_and_the_transaction_makes_the_file_busy_and_is_kept(
    world, monkeypatch
):
    _kid, adopted, _ = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    count = rewrite.page_count

    def queued_meanwhile(path):
        world.rows(
            """INSERT INTO chapter (series_id, kind, identity, stage, status, published_file_id, discovered_at,
                updated_at)
            SELECT series_id, 'rewrite', identity, 'comicinfo', 'ready', id, '2026-01-01', '2026-01-01'
            FROM published_file WHERE id = %s RETURNING id""",
            file_id,
        )
        return count(path)

    monkeypatch.setattr(rewrite, "page_count", queued_meanwhile)
    assert restore(world, file_id) == (0, [f"file {file_id}: busy"])
    assert world.rows("SELECT kind FROM chapter WHERE status <> 'done'") == [("rewrite",)], "the rewrite is kept"
    assert world.rows("SELECT pending_path FROM published_file") == [(None,)]


@budget(load="db", seconds=40)
def test_a_previous_written_without_its_values_is_refused(world):
    _kid, adopted, _ = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    world.rows(  # a replace by code without restores: previous moves, previous_values stays
        "UPDATE published_file SET previous = 'retention/library-files/x.cbz' WHERE id = %s RETURNING id", file_id
    )
    (why,) = restore(world, file_id)[1]
    assert "no previous version recorded with its row" in why


@budget(load="db", seconds=60)
def test_a_queued_restore_named_again_is_busy_and_still_finishes(world):
    _kid, adopted, adopted_bytes = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    assert restore(world, file_id) == (1, [])
    assert restore(world, file_id) == (0, [f"file {file_id}: busy"])
    world.run(export)
    assert (world.library / SERIES / "Ch.1.cbz").read_bytes() == adopted_bytes["Ch.1.cbz"]


@budget(load="db", seconds=60)
def test_a_previous_purged_before_export_fails_the_restore_and_rewrite_drop_clears_it(world):
    _kid, adopted, _ = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    library_bytes = (world.library / SERIES / "Ch.1.cbz").read_bytes()
    restore(world, file_id)
    ((previous,),) = world.rows("SELECT pending_copy ->> 'previous' FROM published_file")
    config.to_local(world.cfg, previous).unlink()
    for _ in range(world.cfg["workflow"]["attempt_limit"]):
        world.run(export)
    ((error,),) = world.rows("SELECT pending_error FROM published_file")
    assert f"its previous version {previous} is gone" in error
    handled = world.rows("SELECT * FROM handled_chapter ORDER BY id")
    with closing(db.connect(world.cfg)) as conn:
        rewrite.drop(conn, file_id, world.cfg["workflow"]["attempt_limit"])
    assert world.rows("SELECT pending_path, pending_copy, origin FROM published_file") == [(None, None, "pipeline")]
    assert world.rows("SELECT * FROM handled_chapter ORDER BY id") == handled, "a restore made no handled record"
    assert (world.library / SERIES / "Ch.1.cbz").read_bytes() == library_bytes, "nothing changed on the share"


def _set(sql):
    return lambda world, file_id: world.rows(sql + " RETURNING 1", file_id)


def _purge(world, file_id):
    ((previous,),) = world.rows("SELECT previous FROM published_file WHERE id = %s", file_id)
    config.to_local(world.cfg, previous).unlink()


def _out_of_scope(world, _file_id):
    world.cfg["series"]["only"] = ["Another Series"]


SERIES_STATUS = (
    "UPDATE series SET status = 'needs_human' WHERE id = (SELECT series_id FROM published_file WHERE id = %s)"
)
NO_KAVITA_ID = (
    "UPDATE series SET kavita_series_id = NULL WHERE id = (SELECT series_id FROM published_file WHERE id = %s)"
)


@budget(load="db", seconds=40)
@pytest.mark.parametrize(
    ("change", "why"),
    [
        (_set("UPDATE published_file SET state = 'unconfirmed' WHERE id = %s"), "not confirmed yet"),
        (_set("UPDATE published_file SET pending_error = 'stuck' WHERE id = %s"), "busy"),
        (_set("UPDATE published_file SET previous_values = NULL WHERE id = %s"), "no previous version recorded"),
        (_purge, "is no longer in retention"),
        (_set("UPDATE published_file SET comicinfo_series = 'Renamed' WHERE id = %s"), "changed since its replace"),
        (_set("UPDATE published_file SET comicinfo_volume = '2' WHERE id = %s"), "changed since its replace"),
        (_set("UPDATE published_file SET cover_state = 'volume:3' WHERE id = %s"), "changed since its replace"),
        (_set("UPDATE published_file SET joined_parts = 2 WHERE id = %s"), "changed since its replace"),
        (_set(SERIES_STATUS), "its series is needs_human"),
        (_out_of_scope, "its series is out of scope"),
        (_set(NO_KAVITA_ID), "its series is not in Kavita"),
    ],
    ids=[
        "unconfirmed", "busy", "no-previous-values", "purged", "series-renamed", "rewritten", "cover-rewritten",
        "joined", "series-not-ok", "out-of-scope", "not-in-kavita",
    ],
)  # fmt: skip
def test_a_file_a_restore_cannot_take_is_listed_with_why(world, change, why):
    _kid, adopted, _ = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    change(world, file_id)
    queued, listed = restore(world, file_id)
    assert queued == 0, why
    assert len(listed) == 1, why
    assert why in listed[0], listed


@budget(load="db", seconds=40)
def test_an_unknown_or_twice_named_file_is_listed_and_the_rest_queued(world):
    _kid, adopted, _ = converted(world)
    file_id = adopted["Ch.1.cbz"]["id"]
    assert restore(world, 999999, file_id, file_id) == (
        1,
        ["file 999999: no such published file", f"file {file_id}: named twice"],
    )


@budget(load="db", seconds=40)
def test_a_stale_cover_of_the_same_volume_is_not_a_change(world):
    kid = bulk_library(world, cover=("Vol.1 Ch.2.cbz",))
    recorded_covers(world, kid, {1: "Vol.1 Ch.2.cbz"})
    world.run(export)
    copy(world, width=20)
    through(world)
    reread(world)
    ((file_id, state),) = world.rows("SELECT id, cover_state FROM published_file WHERE origin = 'pipeline'")
    assert state == "volume:1", "set up: the copy holds volume 1's cover"
    world.rows("UPDATE published_file SET cover_state = 'stale:1' WHERE id = %s RETURNING id", file_id)
    assert restore(world, file_id) == (1, []), "a new image of the same volume is no rewrite"


@budget(load="db", seconds=40)
def test_a_file_kavita_does_not_list_is_listed_and_the_rest_queued(world, monkeypatch):
    _kid, adopted, _ = converted(world, names=("Ch.1.cbz", "Ch.2.cbz"))
    files = world.kavita.series_files
    monkeypatch.setattr(
        world.kavita, "series_files", lambda kid: [f for f in files(kid) if f["path"].name != "Ch.1.cbz"]
    )
    one, two = adopted["Ch.1.cbz"]["id"], adopted["Ch.2.cbz"]["id"]
    assert restore(world, one, two) == (1, [f"file {one}: Kavita does not list it"])


@budget(load="db", seconds=40)
def test_kavita_unreachable_stops_the_command(world, monkeypatch):
    _kid, adopted, _ = converted(world)

    def down(_kid):
        service = "kavita"
        raise Unreachable(service, "down")

    monkeypatch.setattr(world.kavita, "series_files", down)
    with pytest.raises(Unreachable):
        restore(world, adopted["Ch.1.cbz"]["id"])
    assert world.rows("SELECT pending_path FROM published_file") == [(None,)]


@budget(load="db", seconds=40)
def test_rewrite_restore_lists_and_exits_1_and_is_refused_at_retention_0(world, tmp_path, monkeypatch, capsys):
    _kid, adopted, _ = converted(world)
    monkeypatch.setattr(workflow, "context", lambda _cfg, conn, part: world.context(conn, part))
    path = str(config_file(tmp_path, world.cfg))
    file_id = adopted["Ch.1.cbz"]["id"]
    assert cli.main(["--config", path, "rewrite", "restore", str(file_id), "999999"]) == 1
    out = capsys.readouterr().out
    assert "restores queued 1, listed 1" in out
    assert "file 999999: no such published file" in out
    world.cfg = {**world.cfg, "export": {**world.cfg["export"], "retention_days": 0}}
    path = str(config_file(tmp_path, world.cfg))
    assert cli.main(["--config", path, "rewrite", "restore", str(file_id)]) == 2, "the undone version would be lost"
