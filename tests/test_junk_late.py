"""`junk late` (SPEC "Junk decisions", decision commands; plan phase-10-9c): published files whose junk edit is late.

Each test publishes and confirms real chapters through the parts (the junk stage keeps their scans), then changes the
decisions, watermarks or rows and lists. The kept scan's clearing (export) and published_file.junk_chapter (publish,
a replace's settle, a restore, a join) too.
"""

import errno
import json
from contextlib import closing

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import cli, config, db, export, intake, process, rewrite, state, strip, workflow
from manga_pipeline.convert import plan
from manga_pipeline.errors import Unreachable
from manga_pipeline.junk import chapter_scan, ocr, redecide, review, store
from manga_pipeline.junk import late as junk_late

from tests.builders import config_file, site_cfg
from tests.test_better_copies import copy, through
from tests.test_better_copies import library as bulk_library
from tests.test_joins import SERIES as JOINS_SERIES
from tests.test_joins import published as joins_published
from tests.test_junk_cut import FIX, Cli, oracle, pages_cbz
from tests.test_junk_stage import KEYWORD_TEXT, Site, band_cbz, panels_cbz, strip_logo_cbz
from tests.test_rebuilds import file_row, published, rebuild, run_rebuild
from tests.test_restore import converted, reread, restore
from tests.test_rewrites import scan
from tests.timing import budget
from tests.world import World, cbz, decide_junk, logo_cbz


@pytest.fixture
def site(tmp_path, monkeypatch):
    return Site(tmp_path, monkeypatch)


def confirmed(site):
    """Run the parts until every open chapter is published, then let Kavita read them and export confirm them."""
    world = site.world
    for part in (intake, intake, process, export):
        world.run(part)
    scan(world)
    world.run(export)
    assert site.rows("SELECT count(*) FROM published_file WHERE origin = 'pipeline' AND state <> 'confirmed'") == [
        (0,)
    ], "set up: every pipeline file confirmed"


def late(site):
    """Return `junk late`'s lines."""
    with closing(db.read_only(site.cfg)) as conn:
        return junk_late.run(site.cfg, conn)


def listed(site):
    """Return `junk late`'s listed lines, its count line left out."""
    return late(site)[:-1]


def file_id(site, series):
    ((found,),) = site.rows(
        "SELECT id FROM published_file WHERE origin = 'pipeline' AND library_path LIKE %s", f"library/{series}/%%"
    )
    return found


def credit(site, verdict, series="Alpha", number=3):
    """Publish and confirm chapter `number` of `series`: 4 pages and the credit page, decided `verdict`.

    Returns the file's id.
    """
    site.download(series, number, cbz(pages=4, seed=f"{series} {number}", credit=True))
    site.parts()
    decide_junk(site.cfg, verdict)
    confirmed(site)
    return file_id(site, series)


def decision(site):
    """Return the 1 active decision a person made."""
    ((found,),) = site.rows(
        "SELECT id FROM junk_decision WHERE decider = 'human' AND retired_by IS NULL AND superseded_by IS NULL"
    )
    return found


def credit_unit(site):
    """Return (kind, key) of the credit page as the decision a person made on it holds them."""
    ((kind, key),) = site.rows("SELECT kind, key FROM junk_decision WHERE decider = 'human' ORDER BY id LIMIT 1")
    return kind, key


def redecided(site, ids, verdict):
    with closing(db.connect(site.cfg)) as conn:
        return redecide.redecide(conn, ids, verdict, workflow.now(conn))


def retired(site, ids):
    """Retire decisions in 1 act, as a person's junk redecide --retire does a global one; return the act."""
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        act = store.new_act(conn, "junk redecide --retire", workflow.now(conn))
        store.retire(conn, ids, act)
    return act


def undone(site, act):
    with closing(db.connect(site.cfg)) as conn:
        return redecide.undo(conn, act, workflow.now(conn))


def upsert(site, *decisions):
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        store.upsert(conn, list(decisions))


def row(series_key, kind, key, verdict, decider="human"):
    return {
        "series_key": series_key,
        "kind": kind,
        "key": key,
        "verdict": verdict,
        "decider": decider,
        "decided_at": "2026-11-28T00:00:00+00:00",
        "source": "test",
    }


def kept_scan(site, file):
    ((text,),) = site.rows(
        "SELECT e.scan FROM chapter_edit e JOIN published_file f ON f.junk_chapter = e.chapter_id WHERE f.id = %s", file
    )
    return json.loads(text)


# ---------------------------------------------------------------- page chapters


@budget(load="db", seconds=60)
def test_unchanged_decisions_list_nothing_and_a_new_decision_lists_the_page_until_undone(site):
    file = credit(site, "keep")
    assert late(site) == ["files with a kept scan 1, listed 0"], "the same decide: nothing late"
    assert late(site) == ["files with a kept scan 1, listed 0"], "run again: the same answer"
    act = redecided(site, [decision(site)], "junk")
    (line,) = listed(site)
    assert line.startswith(f"file {file} library/Alpha/"), line
    assert "pages dropped anew 4 (cause decisions); original in retention" in line, line
    undone(site, act)
    assert listed(site) == [], "the act undone: the file's edit stands again"


@budget(load="db", seconds=60)
def test_a_retired_junk_decision_lists_the_page_kept_anew_until_undone(site):
    credit(site, "junk")
    act = retired(site, [decision(site)])
    (line,) = listed(site)
    assert "pages kept anew 4 (cause decisions)" in line, line
    undone(site, act)
    assert listed(site) == [], "the junk decision active again"


@budget(load="db", seconds=60)
def test_a_retired_keep_that_would_only_wait_on_review_is_not_listed(site):
    credit(site, "keep")
    retired(site, [decision(site)])
    assert listed(site) == [], "undecided, the credit page stays: its rebuild would only wait on review"


@budget(load="db", seconds=90)
def test_an_agreement_starting_lists_the_file_and_a_further_agreeing_row_does_not(site):
    credit(site, "keep")
    unit = credit_unit(site)
    retired(site, [decision(site)])  # its own keep gone, other series' agreement decides
    upsert(site, row("Beta", *unit, "junk"))
    assert listed(site) == [], "1 other series is no agreement"
    upsert(site, row("Gamma", *unit, "junk"))
    (line,) = listed(site)
    assert "pages dropped anew 4" in line, "2 other series agree: the page is removed today"


@budget(load="db", seconds=90)
def test_a_file_an_agreement_decided_is_not_listed_for_a_further_agreeing_row(site):
    credit(site, "junk", series="Beta")
    credit(site, "junk", series="Gamma")
    site.download("Alpha", 3, cbz(pages=4, seed="Alpha 3", credit=True))
    site.parts()
    assert site.rows("SELECT count(*) FROM junk_candidate WHERE closed_at IS NULL AND series_key = 'Alpha'") == [(0,)]
    confirmed(site)
    file = file_id(site, "Alpha")
    assert site.rows(
        "SELECT edit::jsonb -> 'drop_pages' FROM chapter_edit WHERE chapter_id = "
        "(SELECT junk_chapter FROM published_file WHERE id = %s)",
        file,
    ) == [([4],)], "set up: agreed"
    upsert(site, row("Delta", *credit_unit(site), "junk"))
    assert listed(site) == [], "the same pages removed: a decision id alone is no difference"


@budget(load="db", seconds=60)
def test_a_claude_decision_superseding_an_import_of_the_same_verdict_is_not_listed(site):
    site.download("Alpha", 3, cbz(pages=4, seed="Alpha 3", credit=True))
    site.parts()
    ((kind, key),) = site.rows("SELECT kind, key FROM junk_candidate")
    upsert(site, row("Alpha", kind, key, "junk", decider="import"))
    confirmed(site)
    upsert(site, row("Alpha", kind, key, "junk", decider="claude"))
    assert site.rows("SELECT count(*) FROM junk_decision WHERE superseded_by IS NOT NULL") == [(1,)], "set up"
    assert listed(site) == []


@budget(load="db", seconds=60)
def test_a_changed_layout_a_gone_original_and_a_dropped_share_are_reported(site, monkeypatch):
    file = credit(site, "keep")
    redecided(site, [decision(site)], "junk")
    ((original,),) = site.rows("SELECT original FROM published_file WHERE id = %s", file)
    config.to_local(site.cfg, original).unlink()
    (line,) = listed(site)
    assert line.endswith(f"(cause decisions); its original {original} is gone"), line

    def share_down(_path):
        raise OSError(errno.EIO, "share gone")

    with monkeypatch.context() as m:
        m.setattr(junk_late.errors, "exists", share_down)
        with pytest.raises(Unreachable):
            late(site)
    site.rows("UPDATE series_conversion SET layout = 'strip' RETURNING series_id")
    (line,) = listed(site)
    assert "the series' layout changed since its scan (pages -> strip)" in line, line


@budget(load="db", seconds=60)
@pytest.mark.parametrize(
    "change",
    [
        "UPDATE published_file SET joined_parts = 2 WHERE id = %s",
        "UPDATE published_file SET pending_error = 'stuck' WHERE id = %s",
        "UPDATE published_file SET state = 'unconfirmed' WHERE id = %s",
        "UPDATE series SET status = 'needs_human' WHERE id = (SELECT series_id FROM published_file WHERE id = %s)",
        "UPDATE series SET kavita_library_id = 99 WHERE id = (SELECT series_id FROM published_file WHERE id = %s)",
        "UPDATE published_file SET junk_chapter = NULL WHERE id = %s",
    ],
    ids=["joined", "busy", "unconfirmed", "series-not-ok", "out-of-scope", "no-junk-chapter"],
)
def test_a_file_a_rebuild_cannot_take_is_no_candidate(site, change):
    file = credit(site, "keep")
    redecided(site, [decision(site)], "junk")
    site.rows(change + " RETURNING 1", file)
    assert late(site) == ["files with a kept scan 0, listed 0"]


@budget(load="db", seconds=60)
def test_a_file_whose_original_was_never_recorded_is_listed_saying_so(site):
    file = credit(site, "keep")
    redecided(site, [decision(site)], "junk")
    site.rows("UPDATE published_file SET original = NULL WHERE id = %s RETURNING id", file)
    (line,) = listed(site)
    assert line.endswith("(cause decisions); no original recorded"), line


@budget(load="db", seconds=60)
def test_a_kept_scan_missing_a_field_exits_1_naming_the_file(site, tmp_path, capsys):
    file = credit(site, "keep")
    site.rows("UPDATE chapter_edit SET scan = (scan::jsonb - 'edges')::text RETURNING 1")
    path = str(config_file(tmp_path, site.cfg))
    code = cli_main(path, "junk", "late")
    assert code == 1, "a kept scan the decide cannot read stops the command"
    err = capsys.readouterr().err
    assert f"error: file {file}: its kept scan cannot be decided" in err, "cli's handled path prints the 1 error line"
    assert "Traceback" not in err, "a handled LateError, not an unexpected error logged with its traceback"


@budget(load="db", seconds=60)
def test_an_edit_that_cannot_be_read_stops_the_command_naming_the_edit(site):
    file = credit(site, "keep")
    site.rows("UPDATE chapter_edit SET edit = '{}' RETURNING 1")
    with pytest.raises(junk_late.LateError, match=f"file {file}: its edit cannot be read"):
        late(site)


@budget(load="db", seconds=10)
def test_junk_late_on_an_unbuilt_database_exits_2(tmp_path, capsys):
    cfg = site_cfg(tmp_path, built=False)
    assert cli_main(str(config_file(tmp_path, cfg)), "junk", "late") == 2
    assert "no database" in capsys.readouterr().err


def cli_main(path, *argv):
    return cli.main(["--config", path, *argv])


@budget(load="pool", seconds=60)
def test_a_new_band_decision_on_a_page_chapter_lists_the_band(site):
    site.download("Alpha", 2, band_cbz("m2", on=3))
    site.parts()
    decide_junk(site.cfg, "keep")
    confirmed(site)
    redecided(site, [decision(site)], "junk")
    (line,) = listed(site)
    assert "bands anew on pages 3 (cause decisions)" in line, line


@budget(load="pool", seconds=60)
def test_a_new_logo_decision_on_a_page_chapter_lists_its_blank(site):
    site.download("Alpha", 2, logo_cbz("k2"))
    site.parts()
    decide_junk(site.cfg, "keep")
    confirmed(site)
    redecided(site, [decision(site)], "junk")
    (line,) = listed(site)
    assert "blanks anew on pages 3 (cause decisions)" in line, line


# ---------------------------------------------------------------- strip chapters


def strip_series(site, series="Long"):
    if series not in site.mangas:
        site.world.kavita_series(series, layout="strip")
        site.mangas[series] = site.world.manga(series)


def decided(site, verdicts):
    """Decide every open candidate by its kind ({kind: verdict}, else keep) and run process, until none is offered."""
    for _ in range(4):
        with closing(db.connect(site.cfg)) as conn:
            rows = review.listing(conn, site.cfg, everything=True)
            for r in rows:
                review.decide(conn, [r["id"]], verdicts.get(r["kind"], "keep"), None, workflow.now(conn))
        if not rows:
            return
        site.world.run(process)


@budget(load="pool", seconds=60)
def test_a_new_logo_decision_on_a_strip_chapter_lists_its_blank(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    strip_series(site)
    site.download("Long", 2, strip_logo_cbz("s2"))
    site.parts()
    decided(site, {})
    confirmed(site)
    assert late(site) == ["files with a kept scan 1, listed 0"], "a strip file's same decide: nothing late"
    ((logo,),) = site.rows("SELECT id FROM junk_decision WHERE kind = 'logo'")
    redecided(site, [logo], "junk")
    (line,) = listed(site)
    assert "blanks anew on pages 2 (cause decisions)" in line, line


MARK_ROWS = 2600  # panels_cbz: a row of the 5th panel (page 2's first, rows 2500-2900)


def mark_block(site):
    """Return the scan's edge block holding MARK_ROWS: the band the tests decide junk."""
    ((text,),) = site.rows("SELECT scan FROM junk_scan ORDER BY chapter_id DESC LIMIT 1")
    return next(b for b in json.loads(text)["blocks"] if b["start"] <= MARK_ROWS < b["end"])


def marked(site, panels, number=3):
    """Publish chapter `number` of Long (panels_cbz(panels)) with its mark band an imported junk block decision.

    Every other candidate is kept, a tail piece judged junk. Returns the file's id.
    """
    strip_series(site)
    site.download("Long", number, panels_cbz(panels))
    site.parts()
    upsert(site, row("Long", "block", mark_block(site)["sig"], "junk", decider="import"))
    site.world.run(process)
    decided(site, {"piece": "junk"})
    confirmed(site)
    return file_id(site, "Long")


@budget(load="pool", seconds=90)
def test_a_strip_removals_decision_retired_lists_the_rows_kept_anew_until_undone(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    file = marked(site, ["a2", "b2", "c2", "d2", "mark", "mark"])
    assert late(site) == ["files with a kept scan 1, listed 0"], "a strip file's same decide: nothing late"
    ((mark,),) = site.rows("SELECT id FROM junk_decision WHERE decider = 'import'")
    act = retired(site, [mark])
    (line,) = listed(site)
    assert line.startswith(f"file {file} "), line
    assert "rows kept anew " in line, line
    assert line.endswith("(cause decisions); original in retention"), line
    undone(site, act)
    assert listed(site) == [], "the decision active again"


@budget(load="pool", seconds=90)
def test_a_watermark_learned_since_lists_the_tail_piece_kept_anew_by_watermarks(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    marked(site, ["a3", "b3", "c3", "d3", "mark", "f3"])
    ((edit, watermarks),) = site.rows("SELECT edit, scan_watermarks FROM chapter_edit")
    assert "piece" in {r["why"] for r in json.loads(edit)["strip_removed"]}, "set up: the tail piece after the band"
    assert watermarks == [], "set up: no watermark known at its decide"
    ((key,),) = site.rows("SELECT key FROM junk_decision WHERE decider = 'import'")
    site.rows("INSERT INTO junk_watermark (series_key, key) VALUES ('Long', %s) RETURNING key", key)
    (line,) = listed(site)
    assert "rows kept anew " in line, line
    assert "(cause watermarks)" in line, "today's decisions with the kept keys give its edit: the watermark is why"


@budget(load="pool", seconds=90)
def test_a_cause_the_2nd_decide_cannot_tell_keeps_the_differences(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    file = marked(site, ["a3", "b3", "c3", "d3", "mark", "f3"])
    ((key,),) = site.rows("SELECT key FROM junk_decision WHERE decider = 'import'")
    site.rows("INSERT INTO junk_watermark (series_key, key) VALUES ('Long', %s) RETURNING key", key)
    ((original,),) = site.rows("SELECT original FROM published_file WHERE id = %s", file)
    config.to_local(site.cfg, original).write_bytes(b"not a zip")
    real, calls = chapter_scan.apply, []

    def apply(sc, decide_unit, watermarks, cuts=(), render=None):
        calls.append(watermarks)
        if len(calls) == 2 and render is not None:  # the decide with the kept keys tries a chain: reads the original
            render(0, 10)
        return real(sc, decide_unit, watermarks, cuts, render)

    monkeypatch.setattr(chapter_scan, "apply", apply)
    (line,) = listed(site)
    assert "rows kept anew " in line, "the differences the 1st decide found are kept"
    assert f"(cause not told: its original {original} cannot be read: " in line, line


@budget(load="pool", seconds=90)
def test_a_strip_decide_now_refused_is_listed_with_why(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    marked(site, ["a2", "b2", "c2", "d2", "mark", "mark"])
    keeps = [r[0] for r in site.rows("SELECT id FROM junk_decision WHERE verdict = 'keep' AND decider = 'human'")]
    redecided(site, keeps, "junk")
    (line,) = listed(site)
    assert "its decide is now refused: " in line, line
    assert line.endswith("; original in retention"), line


@budget(load="pool", seconds=120)
def test_a_cut_decision_recorded_after_done_lists_the_rows_read_from_the_original(site, tmp_path, capsys, monkeypatch):
    real = np.asarray(Image.open(FIX / "strip.png").convert("RGB"))
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # nothing offered: the notice lines stay
    strip_series(site)
    site.download("Long", 2, pages_cbz([real]))
    site.parts()
    decided(site, {})
    confirmed(site)
    file = file_id(site, "Long")
    assert late(site) == ["files with a kept scan 1, listed 0"]
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    other = real.copy()
    other[:8] = 0  # another chapter's download: a duplicate would be put aside
    site.download("Long", 3, pages_cbz([other]))
    site.parts()
    chapter = max(r[0] for r in site.rows("SELECT id FROM chapter"))
    run = Cli(site, tmp_path, capsys)
    code, out, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    digest = next(ln.split()[1] for ln in out.splitlines() if ln.startswith("digest: "))
    assert run("junk", "cut", *oracle(chapter), "--record", digest)[0] == 0
    (line,) = listed(site)
    assert line.startswith(f"file {file} "), line
    assert "rows removed anew " in line, line
    assert line.endswith("(cause decisions); original in retention"), line
    work = config.abs_path(site.cfg["paths"]["scratch"]) / junk_late.FOLDER
    assert not work.exists() or not any(work.iterdir()), "the extracted original is removed when its file is done"
    ((original,),) = site.rows("SELECT original FROM published_file WHERE id = %s", file)

    def truncated(*_args, **_kwargs):  # what Pillow raises for a page whose pixels end early, read at the render
        msg = "image file is truncated"
        raise OSError(msg)

    with monkeypatch.context() as m:
        m.setattr(strip, "render_rows", truncated)
        lines = late(site)
    assert lines[0].startswith(f"file {file} "), lines
    assert "cannot be read: image file is truncated" in lines[0], "a page that fails at the render: listed, no stop"
    assert lines[-1] == "files with a kept scan 1, listed 1", "the run goes on"
    config.to_local(site.cfg, original).write_bytes(b"not a zip")
    lines = late(site)
    assert lines[0].startswith(f"file {file} "), lines
    assert "cannot be read" in lines[0], lines
    assert lines[-1] == "files with a kept scan 1, listed 1", "the run goes on"


# ---------------------------------------------------------------- the kept scan's clearing and state reset


@budget(load="db", seconds=60)
def test_export_clears_the_kept_scans_past_retention_and_keeps_younger_ones(site):
    credit(site, "keep")
    credit(site, "keep", series="Beta")
    ((alpha,),) = site.rows("SELECT junk_chapter FROM published_file WHERE id = %s", file_id(site, "Alpha"))
    site.rows("UPDATE chapter_edit SET scan_at = '2000-01-01T00:00:00+00:00' WHERE chapter_id = %s RETURNING 1", alpha)
    site.world.run(export)
    assert site.rows(
        "SELECT scan IS NULL, scan_watermarks IS NULL, scan_at IS NULL FROM chapter_edit WHERE chapter_id = %s", alpha
    ) == [(True, True, True)], "the old scan cleared, its 3 columns"
    assert site.rows("SELECT count(*) FROM chapter_edit WHERE scan IS NOT NULL") == [(1,)], "the younger one kept"
    assert late(site) == ["files with a kept scan 1, listed 0"], "a cleared scan's file is no candidate"
    site.world.cfg = {**site.world.cfg, "export": {**site.world.cfg["export"], "retention_days": 0}}
    site.world.run(export)
    assert site.rows("SELECT count(*) FROM chapter_edit WHERE scan IS NOT NULL") == [(0,)], "retention 0 clears all"


@budget(load="db", seconds=60)
def test_after_state_reset_nothing_is_listed(site):
    credit(site, "keep")
    redecided(site, [decision(site)], "junk")
    assert len(listed(site)) == 1, "set up: late"
    with closing(db.connect(site.cfg)) as conn:
        state.reset(site.cfg, conn)
    assert late(site) == ["files with a kept scan 0, listed 0"], "the edits and their scans are gone"


# ---------------------------------------------------------------- the differences (D3)


def edit(**keys):
    """Return a chapter edit with keys set, the rest empty, as convert reads it (plan.check_edit)."""
    empty = {"drop_pages": [], "bands": [], "join": [], "split": False, "split_pages": None, "strip_removed": []}
    return plan.check_edit(json.dumps({**empty, "blank": [], **keys}))


@budget(load="pure", seconds=1)
def test_ranges_split_differently_over_the_same_rows_and_other_decision_ids_are_no_difference():
    stored = edit(strip_removed=[{"start": 100, "end": 300, "why": "match", "decision": 1}])
    now = edit(
        strip_removed=[
            {"start": 100, "end": 200, "why": "match", "decision": 7},
            {"start": 200, "end": 300, "why": "cut", "decision": 9},
        ]
    )
    assert junk_late.differences(stored, now) == [], "the same rows removed: no pixel differs"


@budget(load="pure", seconds=1)
def test_each_kind_of_difference_is_named_with_its_rows_or_pages():
    stored = edit(
        strip_removed=[{"start": 100, "end": 300, "why": "match"}],
        drop_pages=[1],
        bands=[{"page": 2, "box": [0, 0, 10, 10]}],
        blank=[{"page": 4, "box": [0, 0, 5, 5], "fill": [255, 255, 255]}],
    )
    now = edit(
        strip_removed=[{"start": 200, "end": 400, "why": "match"}],
        drop_pages=[3],
        bands=[{"page": 2, "box": [0, 0, 10, 12]}],
        join=[[5, 6]],
        split=True,
        split_pages=4,
    )
    assert junk_late.differences(stored, now) == [
        "rows removed anew 300-400",
        "rows kept anew 100-200",
        "pages dropped anew 3",
        "pages kept anew 1",
        "bands anew on pages 2",
        "bands gone on pages 2",
        "joins anew at pages 5",
        "blanks gone on pages 4",
        "split False -> True",
        "split_pages None -> 4",
    ]


def applied(edit_, problem=None):
    return chapter_scan.Applied(edit_, [], [], problem, [])


@budget(load="pure", seconds=1)
def test_the_cause_is_named_by_the_decide_with_the_kept_watermark_keys():
    stored = edit()
    rows = edit(strip_removed=[{"start": 0, "end": 10, "why": "match"}])
    more = edit(strip_removed=[{"start": 0, "end": 20, "why": "match"}])
    assert junk_late.cause(stored, applied(stored), rows) == "watermarks", "the kept keys give its edit"
    assert junk_late.cause(stored, applied(rows), rows) == "decisions", "the keys learned since change nothing"
    assert junk_late.cause(stored, applied(rows), more) == "both", "each changes something"
    assert junk_late.cause(stored, applied(stored, "refused"), rows) == "both", "refused with the kept keys"


# ---------------------------------------------------------------- published_file.junk_chapter


def junk_chapter(world, file):
    ((found,),) = world.rows("SELECT junk_chapter FROM published_file WHERE id = %s", file)
    return found


def newest_download(world):
    ((found,),) = world.rows("SELECT max(id) FROM chapter WHERE kind = 'download'")
    return found


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@budget(load="db", seconds=60)
def test_a_rebuild_sets_junk_chapter_when_it_settles_not_while_queued_and_a_restore_puts_it_back(world, monkeypatch):
    first = published(world)
    assert first["junk_chapter"] == newest_download(world), "set at publish: the chapter whose edit it carries"
    rebuild(world, first["id"])
    with monkeypatch.context() as m:
        m.setattr(rewrite, "finish_all", lambda _ctx: None)
        run_rebuild(world)
        assert world.rows("SELECT pending_copy IS NOT NULL FROM published_file WHERE id = %s", first["id"]) == [
            (True,)
        ], "set up: queued"
        assert junk_chapter(world, first["id"]) == first["junk_chapter"], "a queued replace leaves it"
    world.run(export)
    rebuilt = newest_download(world)
    assert rebuilt != first["junk_chapter"], "set up: the rebuild is its own chapter"
    assert junk_chapter(world, first["id"]) == rebuilt, "set at settle with the replace's values"
    reread(world)
    assert restore(world, first["id"]) == (1, [])
    world.run(export)
    assert junk_chapter(world, first["id"]) == first["junk_chapter"], "a restore puts it back"


@budget(load="db", seconds=60)
def test_a_dropped_replace_leaves_junk_chapter(world, monkeypatch):
    first = published(world)
    rebuild(world, first["id"])
    with monkeypatch.context() as m:
        m.setattr(rewrite, "finish_all", lambda _ctx: None)
        run_rebuild(world)
    world.rows("UPDATE published_file SET pending_attempts = 99 WHERE id = %s RETURNING id", first["id"])
    with closing(db.connect(world.cfg)) as conn:
        rewrite.drop(conn, first["id"], 3)
    world.run(export)
    assert junk_chapter(world, first["id"]) == first["junk_chapter"]


@budget(load="db", seconds=60)
def test_a_restore_of_a_version_recorded_without_junk_chapter_writes_null(world):
    first = published(world)
    rebuild(world, first["id"])
    run_rebuild(world)
    reread(world)
    world.rows(  # as previous_values recorded before schema 0084
        "UPDATE published_file SET previous_values = previous_values #- '{before,junk_chapter}' WHERE id = %s"
        " RETURNING id",
        first["id"],
    )
    assert restore(world, first["id"]) == (1, [])
    world.run(export)
    assert junk_chapter(world, first["id"]) is None


@budget(load="db", seconds=60)
def test_a_better_copy_sets_junk_chapter_at_settle(world):
    bulk_library(world)
    copy(world, width=20)
    through(world)
    ((file, found),) = world.rows("SELECT id, junk_chapter FROM published_file WHERE origin = 'pipeline'")
    assert found == newest_download(world), f"file {file}: the copy's chapter"


@budget(load="db", seconds=60)
def test_a_conversion_sets_junk_chapter_at_settle(world):
    _kid, adopted, _ = converted(world)
    file = adopted["Ch.1.cbz"]["id"]
    assert adopted["Ch.1.cbz"]["junk_chapter"] is None, "an adopted file passed no junk"
    ((conversion,),) = world.rows("SELECT id FROM chapter WHERE published_file_id = %s", file)
    assert junk_chapter(world, file) == conversion


@budget(load="db", seconds=60)
def test_a_join_clears_junk_chapter(world):
    joins_published(world, "Chapter 3.1", "Chapter 3.2")
    assert world.rows("SELECT count(junk_chapter) FROM published_file WHERE origin = 'pipeline'") == [(2,)], "set up"
    world.run(export)  # joins them
    assert world.rows(
        "SELECT junk_chapter FROM published_file WHERE library_path = %s", f"library/{JOINS_SERIES}/Ch.3.cbz"
    ) == [(None,)], "its pages come from several downloads: no single junk edit"


@budget(load="db", seconds=60)
def test_after_state_reset_a_queued_replace_settles_and_an_older_version_is_restored(world, monkeypatch):
    first = published(world)
    rebuild(world, first["id"])
    with monkeypatch.context() as m:
        m.setattr(rewrite, "finish_all", lambda _ctx: None)
        run_rebuild(world)
    rebuilt = newest_download(world)
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
    world.run(export)
    assert junk_chapter(world, first["id"]) == rebuilt, "settled: a chapter id that names nothing now, no key broken"
    reread(world)
    assert restore(world, first["id"]) == (1, [])
    world.run(export)
    after = file_row(world, first["id"])
    assert (after["junk_chapter"], after["pending_path"]) == (first["junk_chapter"], None), "restored"
