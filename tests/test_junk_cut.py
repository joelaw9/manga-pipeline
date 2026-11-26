"""`junk cut` (SPEC "Junk decisions", decision commands; manga_pipeline.junk.cut) end to end on a world of fakes.

Span sheets, cut and card-side proposals, their refusals and their record. The oracle is I Was the Real Head of the
House c0003's D&C card (tests/fixtures_pages/junk/s5_2_real_head_c0003, its crop as a 1-page chapter: strip row r is
row r + CROP of the chapter the bulk converter cut): a cut [cut][cut][cut][keep logo][keep (c)] removes the 3 notice
lines (10.4's #38 and #44 in tests/test_junk_anchored.py and tests/test_junk_stage.py).

The 10 FIND tests 10.4 handed over (the bulk converter's tests/test_anchored.py #8, 12, 13, 25, 34, 37, 55, 56, 62,
67) test skip segments (#8, #62), none exemptions (#12, #13, #56), explain (#25), global rows (#34, #37, #67) and the
cut source check (#55): none tests `new` or `edge`, so none is ported (plan phase-10-21, "Not in this plan").
"""

import errno
import io
import json
import os
import shutil
import time
import zipfile
import zlib
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import cli, config, db, process, retention
from manga_pipeline.junk import anchored, chains, cut, groups, ocr, review

from tests.builders import config_file, present, run_part
from tests.test_junk_stage import Site, edits, imported, panels_cbz, strip_logo_cbz
from tests.timing import budget
from tests.world import cbz, decide_junk

FIX = Path(__file__).parent / "fixtures_pages" / "junk" / "s5_2_real_head_c0003"
CROP = 111380  # the crop's first row in the chapter's strip
NOTICE = [(111612, 111630), (111638, 111657), (111665, 111684)]  # the 3 notice lines 10.4's oracle cuts
PANELS = ["a", "b", "c", "d", "e", "f"]  # panels_cbz: page n's panels at rows 1200n + 100 and 1200n + 700, 400 tall


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    return Site(tmp_path, monkeypatch)


class Cli:
    """cli.main on the site's config, stdout and stderr captured: (exit code, out, err)."""

    def __init__(self, site, tmp_path, capsys):
        """Write the site's config file."""
        self.path, self.capsys = str(config_file(tmp_path, site.cfg)), capsys

    def __call__(self, *argv):
        """Run 1 command line."""
        self.capsys.readouterr()
        code = cli.main(["--config", self.path, *argv])
        got = self.capsys.readouterr()
        return code, got.out, got.err


@pytest.fixture
def run(site, tmp_path, capsys):
    return Cli(site, tmp_path, capsys)


def pages_cbz(pages):
    """Return a CBZ of RGB arrays, 1 page each, in order."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n, a in enumerate(pages, 1):
            out = io.BytesIO()
            Image.fromarray(a).save(out, "PNG")
            z.writestr(f"{n:03}.png", out.getvalue())
    return data.getvalue()


def noise(seed, w, h):
    return np.random.default_rng(zlib.crc32(seed.encode())).integers(0, 256, (h, w, 3), dtype=np.uint8)


def strip_series(site, series="Long"):
    if series not in site.mangas:
        site.world.kavita_series(series, layout="strip")
        site.mangas[series] = site.world.manga(series)


def chapter_of(site, data, number=2, series="Long"):
    """Download data as chapter `number` of a strip series and run intake and process; return the chapter's id."""
    strip_series(site, series)
    site.download(series, number, data)
    site.parts()
    return max(r[0] for r in site.rows("SELECT id FROM chapter"))


def real_head(site, number=2, series="Long"):
    """Download the real-head crop as a 1-page chapter and scan it (it waits on its notice block); return its id."""
    return chapter_of(site, pages_cbz([np.asarray(Image.open(FIX / "strip.png").convert("RGB"))]), number, series)


def oracle(chapter):
    """Return the oracle's tokens: the 3 notice lines cut, the logo and the (c) line kept (rows of the crop)."""
    return [
        f"{chapter}:{111830 - CROP}:{111847 - CROP}:keep",
        f"{chapter}:{111751 - CROP}:{111787 - CROP}:keep",
        f"{chapter}:{111612 - CROP}:{111684 - CROP}",
    ]


def digest_of(out):
    return next(ln.split()[1] for ln in out.splitlines() if ln.startswith("digest: "))


def cuts(site):
    return site.rows("SELECT id, chain, written_by FROM junk_decision WHERE kind = 'cut' ORDER BY id")


def candidate_of(site, key):
    ((cid,),) = site.rows("SELECT id FROM junk_candidate WHERE key = %s", key)
    return cid


def scan_of(site, chapter):
    ((text,),) = site.rows("SELECT scan FROM junk_scan WHERE chapter_id = %s", chapter)
    return json.loads(text)


# ---------------------------------------------------------------- the oracle and a cut's record


@budget(load="db", seconds=60)
def test_the_real_head_cut_proposed_recorded_and_decided_removes_10_4s_ranges(site, run):
    chapter = real_head(site)
    code, out, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    assert f"chapter {chapter}: taken" in out, out
    assert cuts(site) == [], "a proposal writes no decision"
    code, recorded, err = run("junk", "cut", *oracle(chapter), "--record", digest_of(out))
    assert code == 0, err
    ((act,),) = site.rows("SELECT max(id) FROM junk_act")
    assert f"junk act {act}" in recorded, "record prints the act id"
    ((_, chain, written),) = cuts(site)
    assert written == act, "the cut decision is the act's"
    ((decider,),) = site.rows("SELECT decider FROM junk_decision WHERE kind = 'cut'")
    assert decider == "human", "outside a review a person decides"
    (version,) = chains.parse(chain).get("below", [])  # both ends at a gap seed: the upper end anchors (no band)
    assert [s["kind"] for s in version["segments"]] == ["cut", "cut", "cut", "keep", "keep"], version
    assert version["ref"] == [("Long", str(chapter), 111612 - CROP)], "ref: series key, chapter id, the anchor (D2)"
    assert present(version["src"])["sha256"] == site.rows("SELECT content_digest FROM chapter")[0][0], (
        "src: the pages digest"
    )
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "a cut ends no wait: no candidate is covered"
    assert decide_junk(site.cfg, "keep") >= 1, "the notice block is kept as a whole unit"
    run_part(site.cfg, process)
    (edit,) = edits(site)
    got = [(r["start"] + CROP, r["end"] + CROP) for r in edit["strip_removed"] if r["why"] == "cut"]
    assert got == NOTICE, "the waiting chapter, decided again, has the cut applied: 10.4's ranges"


@budget(load="db", seconds=60)
def test_junk_undo_of_the_record_removes_the_cut(site, run):
    chapter = real_head(site)
    _, out, _ = run("junk", "cut", *oracle(chapter))
    run("junk", "cut", *oracle(chapter), "--record", digest_of(out))
    ((act,),) = site.rows("SELECT max(id) FROM junk_act")
    assert run("junk", "undo", str(act))[0] == 0
    assert site.rows("SELECT count(*) FROM junk_decision WHERE kind = 'cut' AND retired_by IS NULL") == [(0,)], (
        "junk undo of the act retires its cut"
    )


@budget(load="db", seconds=60)
def test_a_second_record_and_a_proposal_on_a_taken_key_span_are_refused(site, run):
    chapter = real_head(site)
    _, out, _ = run("junk", "cut", *oracle(chapter))
    assert run("junk", "cut", *oracle(chapter), "--record", digest_of(out))[0] == 0
    code, _, err = run("junk", "cut", *oracle(chapter), "--record", digest_of(out))
    assert code == 2, "recorded twice: the second is refused"
    assert "is taken: covered by decision" in err, err
    code, _, err = run("junk", "cut", *oracle(chapter))
    assert (code, "is taken: covered by decision" in err) == (2, True), err
    assert len(cuts(site)) == 1, "nothing more is written"


@budget(load="db", seconds=60)
@pytest.mark.parametrize("what", ["decision", "watermark"])
def test_a_record_after_a_decision_or_a_watermark_is_stale(site, run, what):
    chapter = real_head(site)
    _, out, _ = run("junk", "cut", *oracle(chapter))
    if what == "decision":
        assert decide_junk(site.cfg, "keep") == 1
    else:
        with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
            conn.execute("INSERT INTO junk_watermark (series_key, key) VALUES ('Long', 'ffffffffffffffff|1.0')")
    code, _, err = run("junk", "cut", *oracle(chapter), "--record", digest_of(out))
    assert (code, "stale" in err) == (2, True), err
    assert cuts(site) == [], "a stale record writes nothing"


@budget(load="db", seconds=90)
@pytest.mark.parametrize("what", ["rescanned", "new-scan", "download"])
def test_a_record_after_a_scan_or_a_named_download_changes_is_stale(site, run, what):
    chapter = real_head(site, 2)
    other = real_head(site, 3)
    _, out, _ = run("junk", "cut", *oracle(chapter))
    if what == "new-scan":
        real_head(site, 4)
    else:
        with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
            if what == "rescanned":
                later = "2099-01-01T00:00:00+00:00"
                conn.execute("UPDATE junk_scan SET scanned_at = %s WHERE chapter_id = %s", (later, other))
            else:
                conn.execute("UPDATE chapter SET content_digest = %s WHERE id = %s", ("b" * 64, chapter))
    code, _, err = run("junk", "cut", *oracle(chapter), "--record", digest_of(out))
    assert (code, "stale" in err) == (2, True), err
    assert cuts(site) == []


@budget(load="db", seconds=90)
def test_the_checked_chapters_are_capped_and_each_not_checked_is_counted(site, run, monkeypatch):
    named, _, newest, unscanned = (real_head(site, n) for n in (2, 3, 4, 5))
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (unscanned,))
    ((stored,),) = site.rows("SELECT download_path FROM chapter WHERE id = %s", newest)
    config.to_local(site.cfg, stored).unlink()
    monkeypatch.setattr(cut, "CHECKED_RECENT", 1)
    code, out, err = run("junk", "cut", *oracle(named))
    assert code == 0, err
    assert "3 not checked: 1 without a current scan, 1 without a usable download, 1 beyond the 1 most recent" in out
    assert [ln for ln in out.splitlines() if ln.startswith("chapter ")] == [f"chapter {named}: taken"], out


@budget(load="db", seconds=90)
def test_named_chapters_among_the_newest_are_not_counted_in_the_cap(site, run, monkeypatch):
    oldest, older, named, newest = (real_head(site, n) for n in (2, 3, 4, 5))
    monkeypatch.setattr(cut, "CHECKED_RECENT", 1)
    code, out, err = run("junk", "cut", *oracle(named), *oracle(newest))
    assert code == 0, err
    checked = sorted(int(ln.split()[1].rstrip(":")) for ln in out.splitlines() if ln.startswith("chapter "))
    assert checked == [older, named, newest], "the named ones, then the 1 newest scanned chapter not named"
    assert "1 beyond the 1 most recent" in out, f"chapter {oldest} is left over: {out}"


@budget(load="db", seconds=60)
def test_a_checked_chapters_also_removed_rows_get_a_sheet(site, run):
    named = real_head(site, 2)
    other = real_head(site, 3)
    code, out, err = run("junk", "cut", *oracle(named))
    assert code == 0, err
    assert f"also removes: chapter {other} rows" in out, out
    also = [p for p in sheets_of(out) if p.name.startswith(f"also-{other}-")]
    assert also, "every checked chapter's also removed rows are drawn"
    assert all(p.is_file() for p in also)


@budget(load="db", seconds=60)
def test_inside_a_review_claude_decides_and_the_sheets_go_to_its_cut_folder(site, run, monkeypatch):
    chapter = real_head(site)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert run("junk", "list")[0] == 0
    code, out, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    assert all(p.parent == groups.folder(site.cfg, "r1-cut") for p in sheets_of(out)), out
    assert run("junk", "cut", *oracle(chapter), "--record", digest_of(out))[0] == 0
    assert site.rows("SELECT decider FROM junk_decision WHERE kind = 'cut'") == [("claude",)]
    ((cid,),) = site.rows("SELECT id FROM junk_candidate")
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r2")
    for argv in (["--spans", str(cid)], ["--edge", str(cid), "below"]):
        code, _, err = run("junk", "cut", *argv)
        assert (code, "not listed by this review" in err) == (2, True), err


@budget(load="db", seconds=30)
def test_inside_a_review_a_cut_takes_only_a_chapter_of_a_series_it_listed(site, run, monkeypatch):
    listed = real_head(site, 2, "Long")
    other = real_head(site, 2, "Other")
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):  # unsure: never listed
        conn.execute("UPDATE junk_candidate SET verdict = 'unsure' WHERE series_key = 'Other'")
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert run("junk", "list")[0] == 0
    code, _, err = run("junk", "cut", *oracle(listed))
    assert code == 0, err
    for argv in (oracle(other), [*oracle(other), "--record", "0" * cut.DIGEST_HEX], ["--spans", str(other), "tail"]):
        code, _, err = run("junk", "cut", *argv)
        assert (code, f"chapter {other}: this review listed no candidate of its series" in err) == (2, True), err
    monkeypatch.delenv(review.REVIEW_ID_VARIABLE)
    code, out, err = run("junk", "cut", *oracle(other))
    assert (code, "digest: " in out) == (0, True), f"outside a review the same call proposes: {err}"


@budget(load="db", seconds=60)
def test_2_named_chapters_are_2_versions_of_1_cut(site, run):
    first = real_head(site, 2)
    second = real_head(site, 3)
    code, out, err = run("junk", "cut", *oracle(first), *oracle(second))
    assert code == 0, err
    assert f"chapter {first}: taken" in out, out
    assert f"chapter {second}: taken" in out, out
    run("junk", "cut", *oracle(first), *oracle(second), "--record", digest_of(out))
    ((_, chain, _),) = cuts(site)
    refs = [v["ref"][0][1] for v in chains.parse(chain).get("below", [])]
    assert refs == [str(first), str(second)], "1 version per named chapter, in the order named"


# ---------------------------------------------------------------- refusals


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("ranges", "inside", "why"),
    [
        (["200:240"], False, "no span boundary"),
        (["258:277"], False, "neither end lies at a seed"),
        (["232:250", "285:304"], False, "is not named"),
        (["232:277", "258:304"], False, "overlap"),
        (["232:700"], False, "past the strip end"),
        (["371:390"], True, "must be followed by the rest of its span as 1 range: name"),
    ],
    ids=["no-boundary", "no-seed", "unnamed-span", "overlap", "past-the-end", "exact-without-its-rest"],
)
def test_a_cut_that_cannot_be_built_is_refused(site, run, ranges, inside, why):
    chapter = real_head(site)
    code, out, err = run("junk", "cut", *(f"{chapter}:{r}" for r in ranges), *(["--inside"] if inside else []))
    assert (code, out) == (2, ""), "refused: exit 2, nothing printed"
    assert why in err, err


@budget(load="db", seconds=30)
def test_an_unsignable_span_cannot_be_a_segment(site, run, monkeypatch):
    # a faint line whose every tile signs as a uniform image's (synthetic pixels rarely do: its signer says so here)
    chapter = real_head(site)
    line = (111612 - CROP, 111630 - CROP)
    signed = anchored.span_sigs

    def faint(render):
        span_sig = signed(render)
        return lambda a, b: None if (a, b) == line else span_sig(a, b)

    monkeypatch.setattr(anchored, "span_sigs", faint)
    code, _, err = run("junk", "cut", f"{chapter}:{line[0]}:{line[1]}")
    assert (code, "uniform or unsignable" in err) == (2, True), err


@budget(load="db", seconds=60)
def test_a_cut_naming_chapters_of_2_series_is_refused(site, run):
    first = real_head(site, 2, "Long")
    second = real_head(site, 2, "Other")
    code, _, err = run("junk", "cut", *oracle(first), *oracle(second))
    assert (code, "of 2 series" in err) == (2, True), err


@budget(load="db", seconds=30)
def test_a_cut_leaving_only_blank_rows_fails_the_kept_check(site, run):
    page = np.full((1600, 300, 3), 255, np.uint8)
    page[100:1500, 20:280] = noise("one", 260, 1400)
    chapter = chapter_of(site, pages_cbz([page]))
    code, _, err = run("junk", "cut", f"{chapter}:100:1500")
    assert (code, "junk removal would leave" in err) == (2, True), err


@budget(load="db", seconds=30)
def test_a_chapter_of_a_pages_series_is_refused(site, run):
    site.download("Paged", 2, cbz(seed="p2"))
    site.parts()
    ((chapter,),) = site.rows("SELECT id FROM chapter")
    code, _, err = run("junk", "cut", f"{chapter}:0:100")
    assert (code, "is not a strip" in err) == (2, True), err


@budget(load="db", seconds=60)
def test_a_named_chapter_without_pages_is_refused_and_with_its_retention_original_rescanned(site, run):
    chapter = real_head(site)
    ((stored,),) = site.rows("SELECT download_path FROM chapter")
    download = config.to_local(site.cfg, stored)
    retention.put(site.cfg, download)  # kept the retention way, as export does after publishing
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM junk_scan")  # past junk: no current scan
    code, out, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    assert f"chapter {chapter}: taken" in out, "scanned again from its retention original (D8)"
    assert run("junk", "cut", *oracle(chapter), "--record", digest_of(out))[0] == 0, "and recorded"
    assert len(cuts(site)) == 1
    for kept in config.abs_path(site.cfg["paths"]["retention"]).rglob("*.cbz"):
        kept.unlink()
    code, _, err = run("junk", "cut", f"{chapter}:{111612 - CROP}:{111630 - CROP}")
    assert (code, f"no pages for chapter {chapter}" in err) == (2, True), err


@budget(load="db", seconds=60)
def test_a_cut_anchors_at_the_far_edge_of_a_match_ending_in_a_keep(site, run):
    # notice lines 232:250 and 258:277 are 8 rows apart (no gap): a recorded cut [cut 285:304][keep 258:277] from the
    # gap above 304 ends in a keep at 258, the only seed above 250 (a chained seed, as the bulk converter's)
    chapter = real_head(site)
    first = [f"{chapter}:258:277:keep", f"{chapter}:285:304"]
    _, out, _ = run("junk", "cut", *first)
    assert run("junk", "cut", *first, "--record", digest_of(out))[0] == 0
    code, out, err = run("junk", "cut", f"{chapter}:232:250")
    assert code == 0, err
    assert "from the chained seed at row 258" in out, out
    assert f"chapter {chapter}: taken" in out, out


# ---------------------------------------------------------------- --inside and the interior anchor


@budget(load="db", seconds=60)
def test_a_lower_end_at_a_removed_seed_beats_an_upper_end_at_a_gap_seed(site, run):
    # a recorded cut removes 232:250: its start is a removed seed above, where the line 157:178 (its upper end at a
    # gap seed) ends its walk up; the removed seed (rank 0) beats the gap seed (rank 3)
    chapter = real_head(site)
    first = [f"{chapter}:232:250", f"{chapter}:258:277:keep"]
    _, out, _ = run("junk", "cut", *first)
    assert run("junk", "cut", *first, "--record", digest_of(out))[0] == 0
    code, out, err = run("junk", "cut", f"{chapter}:157:178")
    assert code == 0, err
    assert "side above" in out, "the lower end's removed seed ranks before the upper end's gap seed"
    assert "from the removed seed at row 232" in out, out


@budget(load="db", seconds=60)
def test_a_chained_seed_beats_a_gap_seed_for_the_same_range(site, run):
    # a recorded cut [cut 232:250][keep 258:277] from the gap below 232 ends in a keep at 277; 285:304 starts 8 rows
    # below it (no gap) and ends at a gap seed: the chained seed (rank 2) beats the gap seed (rank 3)
    chapter = real_head(site)
    first = [f"{chapter}:232:250", f"{chapter}:258:277:keep"]
    _, out, _ = run("junk", "cut", *first)
    assert run("junk", "cut", *first, "--record", digest_of(out))[0] == 0
    code, out, err = run("junk", "cut", f"{chapter}:285:304")
    assert code == 0, err
    assert "side below" in out, out
    assert "from the chained seed at row 277" in out, out


@budget(load="db", seconds=60)
def test_named_ends_that_snap_are_checked_as_snapped(site, run):
    chapter = real_head(site)
    keeps = [
        f"{chapter}:{111830 - CROP - 3}:{111847 - CROP + 4}:keep",
        f"{chapter}:{111751 - CROP}:{111787 - CROP}:keep",
    ]
    code, out, err = run("junk", "cut", *keeps, f"{chapter}:{111612 - CROP + 5}:{111684 - CROP - 6}")
    assert code == 0, err
    assert f"cut {111612 - CROP}:{111630 - CROP}" in out, "each end snapped to its span boundary"
    assert f"chapter {chapter}: taken" in out, out


@budget(load="db", seconds=60)
def test_credit_is_the_rows_and_warns(site, run):
    chapter = real_head(site)
    code, out, err = run("junk", "cut", *oracle(chapter), "--credit")
    assert code == 0, err
    assert ", credit" in out, out
    assert "warning: the cut acts in a tail region (or carries credit)" in out, out
    run("junk", "cut", *oracle(chapter), "--credit", "--record", digest_of(out))
    assert site.rows("SELECT credit FROM junk_decision WHERE kind = 'cut'") == [(True,)]


@budget(load="db", seconds=60)
def test_a_cut_naming_only_kept_ranges_or_a_chapter_without_readable_pages_is_refused(site, run):
    chapter = real_head(site)
    code, _, err = run("junk", "cut", f"{chapter}:100:200:keep")
    assert (code, "at least 1 cut range" in err) == (2, True), err
    ((stored, digest),) = site.rows("SELECT download_path, content_digest FROM chapter")
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET content_digest = NULL")
    code, _, err = run("junk", "cut", *oracle(chapter))
    assert (code, "has no content digest" in err) == (2, True), err
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET content_digest = %s", (digest,))
    config.to_local(site.cfg, stored).write_bytes(b"not a zip")
    code, _, err = run("junk", "cut", *oracle(chapter))
    assert (code, "its pages cannot be read" in err) == (2, True), err


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("argv", "why"),
    [
        ([], "takes 1 of --spans, --edge or ranges"),
        (["1:0:10", "--spans", "1"], "takes 1 of --spans, --edge or ranges"),
        (["--spans", "1", "--record", "abc"], "draws sheets only"),
        (["--edge", "1", "below", "--credit"], "go with ranges"),
        (["--edge", "x", "below"], "not ID above|below"),
        (["--spans", "1", "middle"], "not ID or CHAPTER head|tail"),
        (["1:10:5"], "START must be below END"),
        (["1:a:5"], "not CHAPTER:START:END"),
    ],
    ids=["none", "2-forms", "spans-record", "edge-credit", "edge-id", "spans-region", "start-end", "not-a-range"],
)
def test_a_command_line_of_no_form_is_refused(run, argv, why):
    code, _, err = run("junk", "cut", *argv)
    assert (code, why in err) == (2, True), err


@budget(load="db", seconds=60)
def test_inside_makes_an_exact_segment_and_an_anchor_inside_a_span_needs_one(site, run):
    chapter = real_head(site)
    logo = (111751 - CROP, 111787 - CROP)
    exact = [f"{chapter}:{logo[0]}:{logo[0] + 19}", f"{chapter}:{logo[0] + 19}:{logo[1]}:keep", "--inside"]
    code, out, err = run("junk", "cut", *exact)
    assert code == 0, err
    assert f"cut {logo[0]}:{logo[0] + 19} exact" in out, "an end inside a span is an exact boundary"
    run("junk", "cut", *exact, "--record", digest_of(out))
    code, _, err = run("junk", "cut", f"{chapter}:{logo[0] + 19}:{logo[1]}", "--inside")
    assert (code, "lies inside span" in err) == (2, True), "the cut's edge inside the logo anchors only an exact piece"


# ---------------------------------------------------------------- the checked run


def panel_strip(site, panels, number=2, series="Long"):
    return chapter_of(site, panels_cbz(panels), number, series)


@budget(load="db", seconds=60)
def test_a_checked_chapter_without_its_download_is_not_checked_and_does_not_make_the_record_stale(site, run):
    chapter = real_head(site, 2)
    other = real_head(site, 3)
    ((stored,),) = site.rows("SELECT download_path FROM chapter WHERE id = %s", other)
    _, out, _ = run("junk", "cut", *oracle(chapter))
    config.to_local(site.cfg, stored).unlink()
    code, out2, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    assert "1 without a usable download" in out2, out2
    assert f"chapter {other}:" not in out2, "it has no outcome"
    assert digest_of(out2) == digest_of(out), "the digest reads the database only"
    assert run("junk", "cut", *oracle(chapter), "--record", digest_of(out))[0] == 0, "not stale"


@budget(load="db", seconds=60)
def test_an_unrelated_held_chapters_miss_does_not_block_a_cut(site, run):
    chapter = real_head(site, 2)
    held = panel_strip(site, PANELS, 3)
    sc = scan_of(site, held)
    band, first = sc["blocks"][-2], sc["spans"][0]
    seg = {"kind": "cut", "h": round(400 / 300, 4), "tiles": "0123456789abcdef"}  # panel a's height, other tiles
    card = chains.text({"below": [{"segments": [seg], "ref": [["S", "c.cbz", 0]], "src": None}]})
    imported(
        site.cfg,
        {
            "series_key": "Long",
            "kind": "block",
            "key": band["sig"],
            "verdict": "junk",
            "decider": "import",
            "decided_at": "2026-11-01T00:00:00+00:00",
            "source": "test",
            "chain": card,
        },
    )
    assert first, "panel a"
    run_part(site.cfg, process)
    ((reason,),) = site.rows("SELECT last_error FROM chapter WHERE id = %s", held)
    assert "does not match" in reason, "precondition: the held chapter misses without the cut"
    code, out, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    assert f"chapter {held}: no cut" in out, "its card misses with and without the cut: no miss of this proposal"
    assert "digest: " in out, out


@budget(load="db", seconds=90)
@pytest.mark.parametrize("g_height", [100, 700], ids=["fewer-rows-in-all", "more-rows-in-all"])
def test_a_longer_cut_beating_a_card_at_its_seed_loses_rows_and_prints_no_digest(site, run, g_height):
    # page 3: panel e 2500-2900 (its block decision's card cuts panel f 3100-3500 below it), g from 3600 below f: the
    # proposal [keep f][cut g] beats the card at e's edge; f's rows are lost even where g removes more in all
    pages = []
    for top, bottom in [("a", "b"), ("c", "d")]:
        im = np.full((1200, 300, 3), 255, np.uint8)
        im[100:500, 20:280] = noise(top, 260, 400)
        im[700:1100, 20:280] = noise(bottom, 260, 400)
        pages.append(im)
    last = np.full((1500 + g_height, 300, 3), 255, np.uint8)
    last[100:500, 20:280] = noise("e", 260, 400)
    last[700:1100, 20:280] = noise("f", 260, 400)
    last[1200 : 1200 + g_height, 20:280] = noise("g", 260, g_height)
    pages.append(last)
    chapter = chapter_of(site, pages_cbz(pages))
    sc = scan_of(site, chapter)
    e = next(b for b in sc["blocks"] if b["start"] == 2500)
    f_rows, g_rows = (3100, 3500), (3600, 3600 + g_height)
    f_tiles = _span_segment(chapter, site, f_rows)  # f's tiles as the matcher signs them
    card = chains.text({"below": [{"segments": [f_tiles], "ref": [["S", "c.cbz", 0]], "src": None}]})
    imported(
        site.cfg,
        {
            "series_key": "Long",
            "kind": "block",
            "key": e["sig"],
            "verdict": "junk",
            "decider": "import",
            "decided_at": "2026-11-01T00:00:00+00:00",
            "source": "test",
            "chain": card,
        },
    )
    code, out, err = run("junk", "cut", f"{chapter}:{f_rows[0]}:{f_rows[1]}:keep", f"{chapter}:{g_rows[0]}:{g_rows[1]}")
    assert code == 0, err
    assert "no longer removes rows 3100:3500" in out, "the card's rows are lost, named"
    assert "no digest" in out, "a proposal with a miss prints no digest"
    assert not [ln for ln in out.splitlines() if ln.startswith("digest: ")], "a proposal with a miss prints no digest"


@budget(load="db", seconds=90)
def test_a_cut_growing_past_its_segment_in_another_chapter_prints_no_digest(site, run):
    # a notice line N 2500-2518 at the strip's end, a panel 2300-2460 above it; in chapter 3 a faint stroke joins that
    # panel to N, so the cut's overlap extension there grows past N's own height (the cap): a miss of the proposal
    def page(*, stroke):
        a = np.full((3000, 300, 3), 255, np.uint8)
        a[100:2200, 20:280] = noise("story", 260, 2100)
        a[2300:2460, 20:280] = noise("panel", 260, 160)
        a[2500:2518, 40:260] = noise("notice", 220, 18)
        if stroke:
            a[2460:2500, 58:61] = 240
        return a

    clean = chapter_of(site, pages_cbz([page(stroke=False)]), 2)
    joined = chapter_of(site, pages_cbz([page(stroke=True)]), 3)
    code, out, err = run("junk", "cut", f"{clean}:2500:2518")
    assert code == 0, err
    assert f"chapter {clean}: taken" in out, out
    assert f"chapter {joined}: miss: the proposed decision's cut grows past its segment" in out, out
    assert not [ln for ln in out.splitlines() if ln.startswith("digest: ")], "a proposal with a miss prints no digest"


def _span_segment(chapter, site, rows):
    """Return a cut segment of a chapter's strip rows as the matcher signs them (its download's pixels)."""
    ((stored,),) = site.rows("SELECT download_path FROM chapter WHERE id = %s", chapter)
    with zipfile.ZipFile(config.to_local(site.cfg, stored)) as z:
        a = np.vstack([np.asarray(Image.open(io.BytesIO(z.read(n))).convert("RGB")) for n in sorted(z.namelist())])
    s, e = rows
    tiles = present(anchored.signable(Image.fromarray(a[s:e]))).split("|")[0]
    return {"kind": "cut", "h": round((e - s) / a.shape[1], 4), "tiles": tiles}


# ---------------------------------------------------------------- card sides (--edge)


def candidate_for(site, chapter, start):
    sc = scan_of(site, chapter)
    block = next(b for b in sc["blocks"] if b["start"] == start)
    return candidate_of(site, block["sig"]), block


@budget(load="db", seconds=90)
def test_an_edge_side_writes_the_candidates_junk_decision_with_its_side_and_its_chapter_removes_the_rest(site, run):
    chapter = panel_strip(site, PANELS)
    cid, e = candidate_for(site, chapter, 2500)  # panel e, in the tail half
    code, out, err = run("junk", "cut", "--edge", str(cid), "below")
    assert code == 0, err
    assert f"chapter {chapter}: taken" in out, out
    code, recorded, err = run("junk", "cut", "--edge", str(cid), "below", "--record", digest_of(out))
    assert code == 0, err
    ((act,),) = site.rows("SELECT max(id) FROM junk_act")
    assert f"junk act {act}" in recorded
    ((did, kind, key, source, images, chain, written),) = site.rows(
        "SELECT id, kind, key, source, images, chain, written_by FROM junk_decision"
    )
    ((folder,),) = site.rows("SELECT folder FROM junk_candidate WHERE id = %s", cid)
    assert (kind, key, source, images, written) == ("block", e["sig"], f"candidate {cid}", folder, act), (
        "1 junk decision on the candidate, with its images, by the act"
    )
    assert run("junk", "undo", str(act))[0] == 0
    assert site.rows("SELECT retired_by IS NOT NULL FROM junk_decision WHERE id = %s", did) == [(True,)], (
        "junk undo of the act retires the side's decision"
    )
    assert run("junk", "cut", "--edge", str(cid), "below", "--record", digest_of(out))[0] == 0
    ((did, chain),) = site.rows("SELECT id, chain FROM junk_decision WHERE retired_by IS NULL")
    assert chains.parse(chain) == {
        "below": [{"segments": [{"kind": "edge"}], "ref": [("Long", str(chapter), 2900)], "src": None}]
    }
    run_part(site.cfg, process)
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    (edit,) = edits(site)
    assert {"start": 2900, "end": 3600, "why": "card", "decision": did} in edit["strip_removed"], (
        "its wait ended, the chapter is decided with the side: every row from the block to the strip end"
    )


@budget(load="db", seconds=90)
def test_an_edge_side_takes_the_first_chapter_where_its_unit_lies_in_the_half_toward_that_edge(site, run):
    head = panel_strip(site, ["x", "b", "c", "d", "e", "f"], 2)
    tail = panel_strip(site, ["a2", "b2", "c2", "d2", "x", "f2"], 3)
    cid, _ = candidate_for(site, head, 100)
    assert candidate_for(site, tail, 2500)[0] == cid, "x in both chapters is 1 candidate"
    code, out, err = run("junk", "cut", "--edge", str(cid), "below")
    assert code == 0, err
    assert f"an edge from chapter {tail}'s unit at 2500:2900" in out, "the first chapter has x in its head half"


@budget(load="db", seconds=120)
def test_an_edge_side_picked_among_the_newest_checks_the_next_newest(site, run, monkeypatch):
    older = panel_strip(site, ["a2", "b2", "c2", "d2", "e2", "f2"], 2)
    newer = panel_strip(site, ["a3", "b3", "c3", "d3", "e3", "f3"], 3)
    picked = panel_strip(site, PANELS, 4)
    cid, _ = candidate_for(site, picked, 2500)
    monkeypatch.setattr(cut, "CHECKED_RECENT", 1)
    code, out, err = run("junk", "cut", "--edge", str(cid), "below")
    assert code == 0, err
    checked = sorted(int(ln.split()[1].rstrip(":")) for ln in out.splitlines() if ln.startswith("chapter "))
    assert checked == [newer, picked], f"the picked chapter, then the 1 newest other; not {older}: {out}"


@budget(load="db", seconds=60)
def test_an_edge_side_off_its_half_everywhere_is_refused(site, run):
    chapter = panel_strip(site, PANELS)
    cid, _ = candidate_for(site, chapter, 100)  # panel a, in the head half
    code, _, err = run("junk", "cut", "--edge", str(cid), "below")
    assert (code, "half" in err) == (2, True), err
    assert site.rows("SELECT count(*) FROM junk_decision") == [(0,)]


@budget(load="db", seconds=60)
def test_an_edge_side_on_a_closed_candidate_or_one_no_block_or_probe_is_refused(site, run):
    chapter = panel_strip(site, PANELS)
    cid, _ = candidate_for(site, chapter, 2500)
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [cid], "keep", None, "2026-11-25T00:00:00+00:00")
    code, _, err = run("junk", "cut", "--edge", str(cid), "below")
    assert (code, "is not open" in err) == (2, True), err
    strip_series(site, "Logo")
    site.download("Logo", 2, strip_logo_cbz("s2"))
    site.parts()
    ((logo,),) = site.rows("SELECT id FROM junk_candidate WHERE kind = 'logo'")
    code, _, err = run("junk", "cut", "--edge", str(logo), "below")
    assert (code, "not a block or probe" in err) == (2, True), err
    code, _, err = run("junk", "cut", "--spans", str(logo))
    assert (code, "head or tail" in err) == (2, True), "a logo or piece: draw the chapter's head or tail"


# ---------------------------------------------------------------- span sheets


def assert_within_bounds(paths):
    for p in paths:
        with Image.open(p) as im:
            assert im.height <= groups.SHEET_LONG_EDGE, (p, im.size)
            assert im.width <= groups.SHEET_LONG_EDGE, (p, im.size)
            assert im.width * im.height <= groups.SHEET_PIXELS, (p, im.size)


def sheets_of(out):
    return [Path(ln.split(" ", 1)[1]) for ln in out.splitlines() if ln.startswith("sheet: ")]


@budget(load="db", seconds=60)
def test_spans_of_a_candidate_pick_a_waiting_chapter_and_its_sheets_stay_within_the_bounds(site, run):
    first = real_head(site, 2)
    chapter = real_head(site, 3)
    ((cid,),) = site.rows("SELECT id FROM junk_candidate")
    ((stored,),) = site.rows("SELECT download_path FROM chapter WHERE id = %s", first)
    config.to_local(site.cfg, stored).unlink()
    code, out, err = run("junk", "cut", "--spans", str(cid))
    assert code == 0, err
    assert out.startswith(f"chapter {chapter}:"), "the lowest-id waiting chapter with a usable download, its id printed"
    assert f"unit {111612 - CROP}:{111684 - CROP}" in out, out
    paths = sheets_of(out)
    assert paths, out
    assert all(p.is_file() for p in paths)
    assert_within_bounds(paths)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET key = 'ffffffffffffffff|0.5' WHERE id = %s", (cid,))
    code, _, err = run("junk", "cut", "--spans", str(cid))
    assert (code, "no unit of chapter" in err) == (2, True), err
    code, out, err = run("junk", "cut", "--spans", str(chapter), "tail")
    assert code == 0, err
    assert f"chapter {chapter}: tail rows" in out, out
    assert_within_bounds(sheets_of(out))


def wave(f, w, h):
    y, x = np.mgrid[0:h, 0:w]
    v = 127 + 100 * np.sin(x / (7 + f)) * np.cos(y / (11 + f))
    return np.stack([v, (v * 0.6 + 30 * f) % 256, 255 - v], -1).astype(np.uint8)


@budget(load="db", seconds=60)
def test_spans_draw_every_match_and_name_the_nearest_seeds_outside_the_window(site, run, monkeypatch):
    # a notice x glued to the story (a hard join) at the head and at the tail: probes, 1 candidate (a keyword)
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "join our discord")
    head = np.full((1600, 300, 3), 255, np.uint8)
    head[100:300, 20:280] = wave(1, 260, 200)
    head[300:1100, 20:280] = wave(9, 260, 800)
    tail = np.full((1600, 300, 3), 255, np.uint8)
    tail[300:1100, 20:280] = wave(5, 260, 800)
    tail[1100:1300, 20:280] = wave(1, 260, 200)
    chapter_of(site, pages_cbz([head, tail]))
    ((cid, kind),) = site.rows("SELECT id, kind FROM junk_candidate WHERE kind = 'probe'")
    assert kind == "probe"
    code, out, err = run("junk", "cut", "--spans", str(cid))
    assert code == 0, err
    units = [ln for ln in out.splitlines() if ln.startswith("unit ")]
    assert len(units) == 2, out
    assert "nearest seed below: gap above at row 1100" in out, "the head probe's seed below lies past its window"
    assert len(sheets_of(out)) >= 2, "each unit drawn"


@budget(load="db", seconds=60)
def test_a_reviews_sheets_survive_a_second_list_and_go_when_the_review_ends(site, run, monkeypatch):
    real_head(site)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert run("junk", "list")[0] == 0
    ((cid,),) = site.rows("SELECT id FROM junk_candidate")
    code, out, err = run("junk", "cut", "--spans", str(cid))
    assert code == 0, err
    paths = sheets_of(out)
    assert all(p.parent == groups.folder(site.cfg, "r1-cut") for p in paths), "in the candidates folder (Read-allowed)"
    assert run("junk", "list")[0] == 0
    assert all(p.is_file() for p in paths), "a second junk list keeps them"
    groups.remove_all(site.cfg)
    assert not any(p.exists() for p in paths), "they go with the review's"


@budget(load="db", seconds=60)
def test_a_call_leaves_no_pages_and_removes_an_old_scratch_folder(site, run):
    chapter = real_head(site)
    root = config.abs_path(site.cfg["paths"]["scratch"]) / cut.SCRATCH
    old = root / "999999"
    old.mkdir(parents=True)
    stamp = time.time() - cut.SCRATCH_AGE - 60
    os.utime(old, (stamp, stamp))
    code, out, err = run("junk", "cut", *oracle(chapter))
    assert code == 0, err
    assert not old.exists(), "an earlier call's folder over 1 day old is removed"
    assert not list(root.rglob("x")), "the pages extracted for the call are removed"
    assert sheets_of(out), "its sheets stay"
    shutil.rmtree(root)


@budget(load="db", seconds=60)
@pytest.mark.parametrize(
    ("error", "refused"),
    [
        (zipfile.BadZipFile("bad"), True),
        (OSError(errno.EACCES, "denied"), True),
        (OSError(errno.EIO, "share"), False),
        (OSError(errno.ESTALE, "share"), False),
    ],
)
def test_an_unreadable_archive_is_refused_and_a_share_error_is_not_1(site, run, monkeypatch, error, refused):
    chapter = real_head(site)

    def fail(_cbz):
        raise error

    monkeypatch.setattr(cut.archive, "pages", fail)
    code, _, err = run("junk", "cut", *oracle(chapter))
    assert ("its pages cannot be read" in err) == refused, (
        f"an unreadable archive is refused; a share error ({error}) ends the command as the share's: {err}"
    )
    assert code != 0, err
