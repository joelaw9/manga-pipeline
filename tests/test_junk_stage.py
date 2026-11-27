"""The junk stage end to end on a world of fakes (SPEC "Junk decisions", "Chapter stages", "Needs human").

Candidates, the junk_decision wait, the junk commands, cleanup and state reset.
"""

import io
import json
import zipfile
import zlib
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import cli, config, db, errors, export, identify, intake, process, state, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.convert import plan
from manga_pipeline.junk import anchored, candidates, chains, chapter_scan, logo_units, ocr, redecide, review, store
from manga_pipeline.metadata import sources
from manga_pipeline.stages import convert as convert_stage
from manga_pipeline.stages import junk as junk_stage
from scripts import w5_cutover

from tests import pages as fx
from tests.builders import config_file, one, present, run_part
from tests.convert_pages import gray, jpeg
from tests.fakes.suwayomi import page as fake_page
from tests.test_reslice import PAGES_28, recut
from tests.test_reslice import page as reslice_page
from tests.timing import budget
from tests.world import World, cbz, decide_junk, logo_cbz, logo_page


class Site:
    """A World whose client and source builders hand out its fakes, with 1 Suwayomi manga per series name."""

    def __init__(self, tmp_path, monkeypatch):
        """Build the World and patch the client and source builders to return its fakes."""
        self.world = World(tmp_path)
        self.cfg = self.world.cfg
        self.mangas = {}
        monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: self.world.suwayomi)
        monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: self.world.kavita)
        monkeypatch.setattr(sources, "new_sources", lambda: self.world.sources)

    def download(self, series, number, data):
        """Add chapter `number` of `series` (its Kavita series and manga made at first use); return its CBZ path."""
        if series not in self.mangas:
            self.world.kavita_series(series)
            self.mangas[series] = self.world.manga(series)
        return self.world.download(self.mangas[series], f"Chapter {number}", data=data)[1]

    def parts(self):
        """Run intake, then process."""
        run_part(self.cfg, intake)
        run_part(self.cfg, process)

    def rows(self, sql, *params):
        """Return the rows of query `sql` on the site's database, as tuples."""
        with closing(db.connect(self.cfg)) as conn:
            return [tuple(r) for r in conn.execute(sql, params)]

    def chapters(self):
        """Return every chapter's (stage, status, waiting_on), in id order."""
        return self.rows("SELECT stage, status, waiting_on FROM chapter ORDER BY id")


@pytest.fixture
def site(tmp_path, monkeypatch):
    return Site(tmp_path, monkeypatch)


def noise(seed, size=(16, 24)):
    rng = np.random.default_rng(zlib.crc32(seed.encode()))
    out = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)).save(out, "PNG")
    return out.getvalue()


def banner_cbz(seed):
    """Return 8 pages of the fake page size, the last a noisy banner page shared by every chapter (same bytes).

    No size or text rule offers it, only recurrence.
    """
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(1, 8):
            z.writestr(f"{n:03}.png", fake_page(n, seed))
        z.writestr("008.png", noise("banner"))
    return data.getvalue()


def strip_cbz(seed):
    """Return 3 tall white pages of a long strip, each with 2 noisy panels.

    The chapter's own panels, but the last panel is the same credit panel in every chapter.
    """
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(1, 4):
            im = Image.new("RGB", (300, 1200), "white")
            im.paste(Image.open(io.BytesIO(noise(f"{seed} {n} a", (260, 400)))), (20, 100))
            last = n == 3
            im.paste(Image.open(io.BytesIO(noise("credit" if last else f"{seed} {n} b", (260, 400)))), (20, 700))
            out = io.BytesIO()
            im.save(out, "PNG")
            z.writestr(f"{n:03}.png", out.getvalue())
    return data.getvalue()


def open_candidates(site):
    with closing(db.connect(site.cfg)) as conn:
        return review.listing(conn, site.cfg, everything=True)


@budget(load="db", seconds=30)
def test_the_same_credit_page_in_20_chapters_is_1_candidate(site):
    for n in range(2, 22):
        site.download("Alpha", n, cbz(seed=f"c{n}", credit=True))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")] * 20, "every chapter waits on its credit page"
    got = open_candidates(site)
    assert len(got) == 1, "the same page in 20 chapters is 1 candidate"
    assert got[0]["kind"] in ("page", "frame"), "the same page in 20 chapters is 1 candidate"
    assert decide_junk(site.cfg, "junk") == 1
    run_part(site.cfg, process)
    assert all(s[0] != "junk" for s in site.chapters()), "1 decision frees all 20"


@budget(load="db", seconds=10)
def test_a_banner_in_3_ready_chapters_holds_all_3_but_in_2_none(site):
    for n in (2, 3):
        site.download("Two", n, banner_cbz(f"t{n}"))
    for n in (2, 3, 4):
        site.download("Three", n, banner_cbz(f"h{n}"))
    site.parts()
    by_series = site.rows(
        "SELECT s.name, c.stage, c.status FROM chapter c JOIN series s ON s.id = c.series_id ORDER BY c.id"
    )
    assert [r[1:] for r in by_series if r[0] == "Two"] == [("convert", "ready")] * 2 or all(
        r[1] != "junk" for r in by_series if r[0] == "Two"
    ), "2 chapters are no recurrence"
    three = [r[1:] for r in by_series if r[0] == "Three"]
    assert three == [("junk", "waiting")] * 3, "scanned together, all 3 chapters showing the banner wait"
    reasons = [r["reason"] for r in open_candidates(site)]
    assert reasons == ["seen in 3 chapters"], "the banner is offered for its recurrence"


@budget(load="db", seconds=10)
def test_a_chapter_failing_its_scan_does_not_hold_its_series(site):
    bad = site.download("Alpha", 2, cbz(seed="bad"))
    site.download("Alpha", 3, cbz(seed="good"))
    run_part(site.cfg, intake)
    bad.write_bytes(b"not a zip any more")  # the download breaks after discovery
    run_part(site.cfg, process)
    got = site.chapters()
    assert got[0][:2] == ("junk", "ready"), "the broken chapter failed (ready again, 1 attempt)"
    assert got[1][0] != "junk", "its scanned sibling is decided in the same run"


@budget(load="db", seconds=10)
def test_unsure_makes_the_chapter_needs_human_until_decided_and_retried(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    (cand,) = open_candidates(site)
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [cand["id"]], "unsure", None, workflow.now(conn))
        assert review.listing(conn, site.cfg) == [], "an unsure candidate is never listed to Claude again"
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "needs_human", None)], "unsure: a person decides"
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [cand["id"]], "keep", None, workflow.now(conn))
        workflow.retry_chapter(conn, site.rows("SELECT id FROM chapter")[0][0])
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "decided and retried, the chapter goes on"


@budget(load="db", seconds=10)
def test_a_candidate_past_the_review_limit_makes_its_chapter_needs_human(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET reviews = %s", (site.cfg["junk"]["review_limit"],))
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "needs_human", None)], "stuck: a person decides"
    site.download("Alpha", 3, cbz(seed="c3", credit=True))
    site.parts()
    assert site.chapters()[1] == ("junk", "needs_human", None), (
        "a new chapter showing a stuck candidate is held at once"
    )
    (cand,) = site.rows("SELECT id FROM junk_candidate")
    listed = site.rows("SELECT candidates FROM junk_scan WHERE chapter_id = (SELECT max(id) FROM chapter)")
    assert listed == [(f"[{cand[0]}]",)], "the held chapter keeps the candidate open, so junk decide can name it"


@budget(load="db", seconds=10)
def test_an_imported_decision_closes_the_candidate_and_its_folder(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    (cand,) = open_candidates(site)
    folder = config.abs_path(site.cfg["paths"]["candidates"]) / cand["folder"]
    assert (folder / "unit.png").is_file(), "its images are written"
    assert (folder / "context.png").is_file(), "its images are written"
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        store.upsert(
            conn,
            [
                {
                    "series_key": cand["series_key"],
                    "kind": cand["kind"],
                    "key": cand["key"],
                    "verdict": "keep",
                    "decider": "import",
                    "decided_at": workflow.now(conn),
                    "source": "t",
                }
            ],
        )
    assert open_candidates(site) == [], "a decision from any source closes the candidate"
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "its chapter goes on"
    with closing(db.connect(site.cfg)) as conn:
        candidates.cleanup(site.cfg, conn)  # junk review's clean-up: stamps it closed
        assert folder.exists(), "a candidate closed less than an hour ago keeps its folder"
        with db.transaction(conn):
            conn.execute("UPDATE junk_candidate SET closed_at = '2000-01-01T00:00:00+00:00'")
        assert candidates.cleanup(site.cfg, conn) == 1
    assert not folder.exists(), "a candidate closed an hour ago is deleted with its folder"


@budget(load="db", seconds=10)
def test_state_reset_empties_candidates_and_their_folder(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    with closing(db.connect(site.cfg)) as conn:
        conn.execute("INSERT INTO junk_watermark (series_key, key) VALUES ('Alpha', 'k')")
    with closing(db.connect(site.cfg)) as conn:
        state.reset(site.cfg, conn)
    assert site.rows("SELECT count(*) FROM junk_candidate") == [(0,)], "candidates are workflow state"
    assert site.rows("SELECT count(*) FROM junk_watermark") == [(1,)], "a series' watermarks are knowledge: kept"
    assert site.rows("SELECT count(*) FROM junk_scan") == [(0,)], "scans go with their chapters"
    assert list(config.abs_path(site.cfg["paths"]["candidates"]).iterdir()) == [], "the folder is emptied"


@budget(load="db", seconds=10)
def test_junk_commands_list_show_decide_and_refuse(site, tmp_path, capsys, monkeypatch):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    path = str(config_file(tmp_path, site.cfg))
    (cand,) = open_candidates(site)
    assert cli.main(["--config", path, "junk", "list"]) == 0
    line = capsys.readouterr().out.strip().split("\t")
    assert line[0] == str(cand["id"]), "list prints id, ..., image paths"
    assert line[-1].endswith("context.png"), "list prints id, ..., image paths"
    assert cli.main(["--config", path, "junk", "show", str(cand["id"])]) == 0
    assert "unit:" in capsys.readouterr().out, "show prints the images"
    assert cli.main(["--config", path, "junk", "decide", "999", "keep"]) == 2, "an unknown candidate is refused"
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", path, "junk", "list", "--all"]) == 2, "--all is refused inside a review"
    assert cli.main(["--config", path, "junk", "list"]) == 0
    capsys.readouterr()
    assert site.rows("SELECT review_id, review_open, reviews FROM junk_candidate") == [("r1", 1, 0)], (
        "a review's list records its id; the review counts when it ends"
    )
    assert cli.main(["--config", path, "junk", "show", str(cand["id"])]) == 0, "a review shows a candidate it listed"
    assert cli.main(["--config", path, "junk", "decide", str(cand["id"]), "junk"]) == 0
    assert site.rows("SELECT decider, verdict FROM junk_decision") == [("claude", "junk")], "Claude's verdict"


@budget(load="db", seconds=10)
def test_inside_a_review_a_candidate_it_did_not_list_is_neither_shown_nor_decided(site, tmp_path, capsys, monkeypatch):
    """`junk show` and `junk decide` under a review id refuse (exit 2) an open candidate that review never listed."""
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    path = str(config_file(tmp_path, site.cfg))
    (cand,) = open_candidates(site)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "show", str(cand["id"])]) == 2, "show refuses an unlisted candidate"
    assert "not listed by this review" in capsys.readouterr().err, "the refusal says why"
    assert cli.main(["--config", path, "junk", "decide", str(cand["id"]), "junk"]) == 2, (
        "decide refuses an unlisted candidate"
    )
    assert "not listed by this review" in capsys.readouterr().err, "the refusal says why"
    assert site.rows("SELECT count(*) FROM junk_decision") == [(0,)], "no verdict is recorded"


@budget(load="db", seconds=10)
def test_a_candidate_an_ended_review_listed_is_refused_in_the_next_review(site, tmp_path, capsys, monkeypatch):
    """A candidate listed by review r0, which has ended, is not r1's to decide: exit 2 and no decision."""
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    path = str(config_file(tmp_path, site.cfg))
    (cand,) = open_candidates(site)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r0")
    assert cli.main(["--config", path, "junk", "list"]) == 0
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET reviews = reviews + 1, review_open = 0")  # as junk review ends r0
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", path, "junk", "decide", str(cand["id"]), "junk"]) == 2, (
        "another review's candidate is refused"
    )
    capsys.readouterr()
    assert site.rows("SELECT count(*) FROM junk_decision") == [(0,)], "no verdict is recorded"


@budget(load="db", seconds=10)
def test_with_no_review_id_a_person_decides_a_candidate_a_review_listed(site, tmp_path, capsys, monkeypatch):
    """Outside a review, `junk decide` takes any open candidate, one a running review listed included, as human."""
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    path = str(config_file(tmp_path, site.cfg))
    (cand,) = open_candidates(site)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", path, "junk", "list"]) == 0
    capsys.readouterr()
    monkeypatch.delenv(review.REVIEW_ID_VARIABLE)
    assert cli.main(["--config", path, "junk", "decide", str(cand["id"]), "keep"]) == 0, (
        "a person decides an open candidate whatever review listed it"
    )
    assert site.rows("SELECT decider, verdict FROM junk_decision") == [("human", "keep")], "a person's verdict"


@budget(load="db", seconds=10)
def test_a_chapter_without_a_content_digest_fails_instead_of_scanning_for_ever(site):
    site.download("Alpha", 2, cbz(seed="x"))
    run_part(site.cfg, intake)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET content_digest = NULL")
    run_part(site.cfg, process)  # returns: a scan that could never be current would make drain spin
    got = site.rows("SELECT stage, status, attempts, last_error FROM chapter")
    assert got[0][:3] == ("junk", "ready", 1), "no digest: 1 failed attempt"
    assert "content digest" in got[0][3], "no digest: 1 failed attempt"
    assert site.rows("SELECT count(*) FROM junk_scan") == [(0,)], "no scan is written"


@budget(load="db", seconds=10)
def test_a_malformed_stored_page_aspect_fails_the_scan_naming_its_series(site):
    site.download("Alpha", 2, cbz(seed="x"))
    run_part(site.cfg, intake)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE series_conversion SET layout = 'pages', page_aspect = '[]'")  # its baseline's row
    run_part(site.cfg, process)
    ((stage, status, attempts, error),) = site.rows("SELECT stage, status, attempts, last_error FROM chapter")
    assert (stage, status, attempts) == ("junk", "ready", 1), "the scan reads the series' page aspects checked"
    assert "page_aspect must be a non-empty list of numbers" in error, "the failure names what is wrong"
    assert site.rows("SELECT count(*) FROM junk_scan") == [(0,)], "no scan is written"


@budget(load="db", seconds=5)
def test_a_strip_chapter_waits_on_its_candidates_and_finishes_once_they_are_kept(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    for n in (2, 3, 4):
        site.download("Long", n, strip_cbz(f"l{n}"))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")] * 3, "each strip chapter waits on its blocks"
    got = open_candidates(site)
    assert got, got
    assert {c["kind"] for c in got} <= {"block", "probe"}, got
    assert decide_junk(site.cfg, "keep") == len(got)
    run_part(site.cfg, process)
    assert all(c[0] != "junk" for c in site.chapters()), "kept blocks free every chapter"
    assert open_candidates(site) == [], "no candidate is offered again"


@budget(load="db", seconds=5)
def test_a_strip_chapter_whose_junk_would_leave_too_little_needs_a_human(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_cbz("w2"))
    site.parts()
    assert decide_junk(site.cfg, "junk") > 0, "a first chapter offers its blocks"
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):  # as a first decide after its scan would be
        conn.execute("UPDATE chapter SET junk_candidates = NULL")
    run_part(site.cfg, process)
    ((status, reason, waited_on),) = site.rows("SELECT status, last_error, junk_candidates FROM chapter")
    assert status == "needs_human", "a removal that would leave too little: a person decides"
    assert "junk removal would leave" in reason, reason
    assert waited_on == 0, "that first decide counts in the junk refill, with no candidate"


@budget(load="db", seconds=10)
def test_a_lost_claim_writes_no_candidate_folder(site, monkeypatch):
    site.download("Alpha", 2, cbz(credit=True))
    events, real_name, real_write = [], candidates.folder_name, candidates.write_images

    def folder_name(key, kind, unit_key, chapter):
        if not events:  # another run takes the claim while this one renders the candidate's images
            with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
                conn.execute(
                    "UPDATE chapter SET claimed_by = 'process@elsewhere', claim_count = claim_count + 1 WHERE id = %s",
                    (chapter["id"],),
                )
            events.append("taken")
        return real_name(key, kind, unit_key, chapter)

    def write_images(*args):
        ((holder,),) = site.rows("SELECT claimed_by FROM chapter")
        events.append(f"written under {holder}'s claim")
        return real_write(*args)

    monkeypatch.setattr(candidates, "folder_name", folder_name)
    monkeypatch.setattr(candidates, "write_images", write_images)
    site.parts()
    assert events == ["taken", f"written under {workflow.owner('process')}'s claim"], (
        "nothing is written under a lost claim; the chapter claimed again writes its candidate"
    )
    assert site.chapters() == [("junk", "waiting", "junk_decision")]


@budget(load="db", seconds=5)
def test_ocr_text_holding_a_nul_makes_its_chapter_needs_human_at_once(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "CREDITS\x00")  # the OCR engine read a NUL off a block
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_cbz("n2"))
    site.parts()
    ((status, attempts, reason),) = site.rows("SELECT status, attempts, last_error FROM chapter")
    assert (status, attempts) == ("needs_human", 0), "no attempt can store the text: held at once, no attempt spent"
    assert "strip rows" in reason, f"the reason names the unit's rows: {reason}"
    assert "NUL" in reason, f"the reason names the unit's rows: {reason}"
    assert site.rows("SELECT count(*) FROM junk_scan") == [(0,)], "nothing is stored"


@budget(load="db", seconds=5)
def test_a_failed_ocr_read_fails_the_scan_naming_the_units_rows_and_stores_nothing(site, monkeypatch):
    def fail(_im, _device):
        msg = "CUDA out of memory"
        raise RuntimeError(msg)

    monkeypatch.setattr(ocr, "text", fail)
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_cbz("n2"))
    site.parts()
    ((status, attempts, reason),) = site.rows("SELECT status, attempts, last_error FROM chapter")
    assert (status, attempts) == ("ready", 1), "a failed scan spends an attempt, as any stage failure does"
    assert "strip rows" in reason, f"the error names the unit's rows: {reason}"
    assert "CUDA out of memory" in reason, f"the error names the read's own error: {reason}"
    assert site.rows("SELECT count(*) FROM junk_scan") == [(0,)], "no partial text is stored"


@budget(load="db", seconds=10)
def test_a_chapter_leaving_junk_has_its_chapter_edit_with_every_key(site):
    site.download("Alpha", 2, cbz(seed="e2", credit=True))
    site.parts()
    assert decide_junk(site.cfg, "junk") == 1
    run_part(site.cfg, process)
    ((text,),) = site.rows("SELECT edit FROM chapter_edit")
    assert json.loads(text) == {
        "drop_pages": [3],
        "bands": [],
        "join": [],
        "split": False,
        "split_pages": None,
        "strip_removed": [],
        "blank": [],
    }, "the credit page (download index 3) is dropped; every key is present"


# ---------------------------------------------------------------- logos (SPEC "Junk decisions": units, chapter edit)


def decide_kinds(cfg, verdicts):
    """Record a person's verdict per candidate kind {kind: verdict} on every open candidate.

    Returns the kinds decided.
    """
    with closing(db.connect(cfg)) as conn:
        rows = review.listing(conn, cfg, everything=True)
        for r in rows:
            review.decide(conn, [r["id"]], verdicts[r["kind"]], None, workflow.now(conn))
    return sorted(r["kind"] for r in rows)


def edits(site):
    return [json.loads(text) for (text,) in site.rows("SELECT edit FROM chapter_edit ORDER BY chapter_id")]


@budget(load="db", seconds=10)
def test_a_badge_holds_its_chapters_on_1_candidate_and_a_junk_decision_blanks_it(site):
    for n in (2, 3):
        site.download("Alpha", n, logo_cbz(f"b{n}"))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")] * 2, "an undecided badge holds its chapter"
    got = open_candidates(site)
    assert [c["kind"] for c in got] == ["logo"], "the same badge in 2 chapters is 1 logo candidate"
    folder = config.abs_path(site.cfg["paths"]["candidates"]) / got[0]["folder"]
    assert {p.name for p in folder.iterdir()} >= {"unit.png", "context.png"}, "its 2 review images"
    assert decide_junk(site.cfg, "junk") == 1
    run_part(site.cfg, process)
    assert all(c[0] != "junk" for c in site.chapters()), "1 decision frees both"
    for edit in edits(site):
        assert [b["page"] for b in edit["blank"]] == [3], "the badge page is blanked"
        assert edit["drop_pages"] == [], "the badge page is blanked"
        assert plan.check_edit(json.dumps(edit)) == edit, "convert accepts the blank"
        x0, y0, x1, y1 = edit["blank"][0]["box"]
        assert 0 <= x0 < x1 <= 1200, "the box lies in the page"
        assert 0 <= y0 < y1 <= 1800, "the box lies in the page"
        assert all(245 <= v <= 255 for v in edit["blank"][0]["fill"]), "the fill is the white margin around it"


@budget(load="db", seconds=10)
def test_a_kept_badge_gives_no_blank(site):
    site.download("Alpha", 2, logo_cbz("k2"))
    site.parts()
    assert decide_junk(site.cfg, "keep") == 1
    run_part(site.cfg, process)
    assert [e["blank"] for e in edits(site)] == [[]], "a kept badge stays"


@budget(load="db", seconds=10)
def test_a_scan_made_before_logos_is_scanned_again(site):
    site.download("Alpha", 2, logo_cbz("o2"))
    site.parts()
    with closing(db.connect(site.cfg)) as conn:
        conn.execute("UPDATE junk_scan SET scan = (scan::jsonb - 'marks')::text, candidates = '[]'")
        conn.execute("DELETE FROM junk_candidate")
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "the chapter is held again"
    (scan,) = site.rows("SELECT scan FROM junk_scan")
    assert [g["page"] for g in json.loads(scan[0])["marks"]] == [3], "its new scan holds the badge"


def strip_logo_cbz(seed):
    """Return 4 white strip pages of 1200 x 1800, each with a gray noisy panel of its own.

    A gray chapter: a coloured panel would make the badge page colour. The third page also has the badge.
    """
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(1, 5):
            im = Image.open(io.BytesIO(logo_page())) if n == 3 else Image.new("RGB", (1200, 1800), "white")
            im.paste(Image.open(io.BytesIO(noise(f"{seed} {n}", (1000, 600)))).convert("L"), (100, 900))
            out = io.BytesIO()
            im.save(out, "PNG")
            z.writestr(f"{n:03}.png", out.getvalue())
    return data.getvalue()


@budget(load="db", seconds=20)
def test_a_badge_in_a_strip_chapter_is_offered_and_blanked_on_its_page(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_logo_cbz("s2"))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")]
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    assert list(json.loads(scan)) == [
        "layout",
        "width",
        "height",
        "gaps",
        "spans",
        "blocks",
        "measured",
        "middle",
        "marks",
    ], "a strip scan is stored as strip_units.scan gives it, with its logos (no content pages: nothing reads them)"
    kinds = decide_kinds(site.cfg, {"block": "keep", "probe": "keep", "piece": "keep", "logo": "junk"})
    assert kinds.count("logo") == 1, kinds
    run_part(site.cfg, process)
    (edit,) = edits(site)
    assert [b["page"] for b in edit["blank"]] == [2], "download page 2 is blanked"
    assert edit["strip_removed"] == [], "download page 2 is blanked"
    assert plan.check_edit(json.dumps(edit)) == edit, "convert accepts a strip chapter's blank"


def jpeg_logo_cbz(seed):
    """6 gray JPEG pages of 1200 x 1800 (AVIF outcome: a blank applies), the third a white page with the badge."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(6):
            im = (
                Image.open(io.BytesIO(logo_page(at=(100, 400))))  # at the top it would also be a band
                if n == 2
                else gray((1200, 1800), seed=zlib.crc32(f"{seed}{n}".encode()))
            )
            z.writestr(f"{n + 1:03}.jpg", jpeg(im))
    return data.getvalue()


@budget(load="pool", seconds=60)
@pytest.mark.parametrize("verdict", ["junk", "keep"])
def test_a_badge_is_painted_over_in_the_published_chapter_only_when_junk(site, verdict):
    site.download("Alpha", 2, jpeg_logo_cbz("p2"))
    site.parts()
    kinds = decide_kinds(site.cfg, {"page": "keep", "frame": "keep", "logo": verdict})
    assert kinds.count("logo") == 1, kinds
    run_part(site.cfg, process)
    run_part(site.cfg, export)
    ((path, result),) = site.rows(
        "SELECT p.library_path, c.convert_result FROM published_file p JOIN chapter c ON c.identity = p.identity"
        " WHERE p.origin = 'pipeline'"
    )
    result = json.loads(result)
    assert result["outcome"] == "avif", result
    assert result["blanked"] == (["003.jpg"] if verdict == "junk" else []), result
    logo = present(logo_units.measure(2, Image.open(io.BytesIO(logo_page(at=(100, 400)))).convert("RGB")))
    x0, y0, x1, y1 = logo["box"]
    with zipfile.ZipFile(config.to_local(site.cfg, path)) as z:
        name = sorted(n for n in z.namelist() if n != "ComicInfo.xml")[2]
        page = np.asarray(Image.open(io.BytesIO(z.read(name))).convert("L")).astype(int)
    spread = np.ptp(page[y0:y1, x0:x1])
    if verdict == "junk":
        assert spread <= 12, "the published page's badge box is 1 flat colour: the badge is painted over"
    else:
        assert spread > 100, "a kept badge is still on the published page"


# ---------------------------------------------------------------- bands at 1 chapter (D5.16)


def band_cbz(seed, on, pages=8):
    """Return a page-format chapter of 600 x 852 art pages.

    Page `on` (a download index) has the same WARNING band stacked over it behind a gutter in every chapter (as
    tests/test_junk_page.banded_chapter). 8 pages: 3 middle pages, so the edge pages have middle pages of their size
    beside them.
    """
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(pages):
            page_seed = zlib.crc32(f"{seed}{n}".encode())
            im = fx.manga_page((600, 852), seed=page_seed, noise=3)
            if n == on:
                im = fx.stack(fx.warning_banner(), (40, 600), fx.manga_page((600, 840), seed=page_seed, noise=3))
            z.writestr(f"{n + 1:03}.png", fx.png(im))
    return data.getvalue()


@budget(load="pool", seconds=60)
def test_a_band_on_a_middle_page_holds_its_chapter_and_1_decision_frees_the_next(site):
    site.download("Alpha", 2, band_cbz("m2", on=3))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "D5.16: an undecided band holds its chapter"
    assert [c["kind"] for c in open_candidates(site)] == ["band"], "the band, on a middle page, in 1 chapter"
    assert decide_junk(site.cfg, "junk") == 1
    run_part(site.cfg, process)
    (edit,) = edits(site)
    assert [b["page"] for b in edit["bands"]] == [3], "the band is cropped off the middle page"
    site.download("Alpha", 3, band_cbz("m3", on=3))
    site.parts()
    assert all(c[0] != "junk" for c in site.chapters()), "the next chapter's band is decided: it does not wait"


@budget(load="pool", seconds=60)
def test_an_edge_page_decided_junk_frees_its_chapter_though_its_band_is_unsure(site):
    site.download("Alpha", 2, band_cbz("e2", on=0))
    site.parts()
    got = open_candidates(site)
    assert sorted(c["kind"] == "band" for c in got) == [False, True], "the edge page and its band are both offered"
    with closing(db.connect(site.cfg)) as conn:
        for c in got:
            verdict = "unsure" if c["kind"] == "band" else "junk"
            review.decide(conn, [c["id"]], verdict, None, workflow.now(conn))
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "the page is dropped, so its band holds nothing: no person is asked"
    (edit,) = edits(site)
    assert (edit["drop_pages"], edit["bands"]) == ([0], []), "the page goes whole"


@budget(load="pool", seconds=60)
def test_a_candidate_deleted_under_a_waiting_chapter_ends_its_wait(site):
    """Schema step 0042 deletes candidates on unsignable keys.

    A chapter waiting on one and on another, still undecided, is decided again at once, and waits on what is undecided
    then.
    """
    site.download("Alpha", 2, band_cbz("g2", on=0))
    site.parts()
    ((before,),) = site.rows("SELECT candidates FROM junk_scan")
    page = next(c["id"] for c in open_candidates(site) if c["kind"] != "band")  # the edge page (or its frame)
    with closing(db.connect(site.cfg)) as conn:
        conn.execute("DELETE FROM junk_candidate WHERE id = %s", (page,))
    run_part(site.cfg, process)
    ((after,),) = site.rows("SELECT candidates FROM junk_scan")
    assert page in json.loads(before), "decided again: the gone one is not listed"
    assert page not in json.loads(after), "decided again: the gone one is not listed"
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "it waits on the page and band offered again"


# ---------------------------------------------------------------- series-wide watermarks


def panels_cbz(panels):
    """Return 3 tall white pages of a long strip with 2 noisy panels each.

    `panels` names the 6 panels' noise in reading order (the same name is the same panel).
    """
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(3):
            im = Image.new("RGB", (300, 1200), "white")
            for k, top in enumerate((100, 700)):
                im.paste(Image.open(io.BytesIO(noise(panels[2 * n + k], (260, 400)))), (20, top))
            out = io.BytesIO()
            im.save(out, "PNG")
            z.writestr(f"{n + 1:03}.png", out.getvalue())
    return data.getvalue()


def imported(cfg, decision):
    """Write decision anew as an imported one (any import row deleted first: a new id); a pipeline row is kept."""
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        conn.execute("DELETE FROM junk_decision WHERE decider = 'import'")
        store.upsert(conn, [decision])


@budget(load="pool", seconds=60)
def test_a_watermark_1_chapter_shows_is_known_to_the_series_next_chapters(site, monkeypatch):
    """Chapter 2 ends with the band twice: a watermark.

    Chapter 3 has it once at its tail, before a story panel: known as a watermark it starts no credit block, so the
    panel after it is no tail piece. The memory is by key: the decision rebuilt under a new id changes nothing.
    Without the memory the panel is a tail piece.
    """
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, panels_cbz(["a2", "b2", "c2", "d2", "mark", "mark"]))
    site.parts()
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    mark = json.loads(scan)["blocks"][-1]["sig"]
    decision: store.Decision = {
        "series_key": "Long",
        "kind": "block",
        "key": mark,
        "verdict": "junk",
        "decider": "import",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "source": "test",
    }
    imported(site.cfg, decision)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "its other blocks are still undecided"
    assert site.rows("SELECT series_key, key FROM junk_watermark") == [("Long", mark)], "remembered while it waits"
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    assert site.rows("SELECT series_key, key FROM junk_watermark") == [("Long", mark)], "twice at 1 tail: a watermark"
    imported(site.cfg, decision)
    site.download("Long", 3, panels_cbz(["a3", "b3", "c3", "d3", "mark", "f3"]))
    site.parts()
    assert "piece" not in {c["kind"] for c in open_candidates(site)}, "a known watermark starts no credit block"
    with closing(db.connect(site.cfg)) as conn:
        conn.execute("DELETE FROM junk_watermark")
    site.download("Long", 4, panels_cbz(["a4", "b4", "c4", "d4", "mark", "f4"]))
    site.parts()
    assert "piece" in {c["kind"] for c in open_candidates(site)}, "unknown, the band starts the credit block"


# ---------------------------------------------------------------- the series key


@budget(load="db", seconds=10)
def test_a_renamed_series_folder_keeps_its_junk_decisions_and_sightings(site):
    site.download("Alpha", 2, cbz(seed="r2", credit=True))
    site.parts()
    assert decide_junk(site.cfg, "junk") == 1
    run_part(site.cfg, process)
    world = site.world
    (world.library / "Alpha").rename(world.library / "Beta")
    world.kavita.files = {path.replace("/Alpha/", "/Beta/"): f for path, f in world.kavita.files.items()}
    ((kavita_id,),) = site.rows("SELECT kavita_series_id FROM series")
    with closing(db.connect(site.cfg)) as conn:
        subject = identify.manga_link_subject(world.suwayomi, site.mangas["Alpha"])
        identify.link(site.cfg, conn, world.kavita, kavita_id, subject)
    ((folder, key),) = site.rows("SELECT folder, rules_key FROM series")
    assert folder.endswith("Beta"), "the folder follows Kavita; the rules key stays"
    assert key == "Alpha", "the folder follows Kavita; the rules key stays"
    site.download("Alpha", 3, cbz(seed="r3", credit=True))
    site.parts()
    assert site.chapters()[1][0] != "junk", "the series' decision still drops the credit page"
    assert site.rows("SELECT DISTINCT series_key FROM junk_seen") == [("Alpha",)], "new sightings join the old ones"


@budget(load="db", seconds=10)
def test_a_chapter_joining_a_candidate_cleaned_up_meanwhile_fails_once_then_writes_its_images(site, monkeypatch):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()  # chapter 2 opens the credit page's candidate and waits on it
    site.download("Alpha", 3, cbz(credit=True))
    real = candidates.join

    def cleaned_up_first(conn, series_key, picks, at):  # junk review's clean-up lands between decide and record
        conn.execute("DELETE FROM junk_candidate")
        return real(conn, series_key, picks, at)

    monkeypatch.setattr(candidates, "join", cleaned_up_first)
    site.parts()
    (third,) = site.rows("SELECT attempts, last_error FROM chapter WHERE identity LIKE '%%\"3\"%%'")
    assert third[0] == 1, "the chapter fails once"
    assert "cleaned up meanwhile" in third[1], "the chapter fails once"
    monkeypatch.setattr(candidates, "join", real)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET retry_after = NULL")  # the retry pause is over
    site.parts()
    assert [c[1:] for c in site.chapters()] == [("waiting", "junk_decision")] * 2, "both wait on the candidate again"
    (cand,) = open_candidates(site)
    images = config.abs_path(site.cfg["paths"]["candidates"]) / cand["folder"]
    assert (images / "unit.png").is_file(), "the retry wrote the candidate's images anew"


# ---------------------------------------------------------------- an override's layout change (SPEC "State")


def set_override_layout(site, series, layout):
    """Write the series' override file naming `layout`."""
    folder = config.abs_path(site.cfg["paths"]["naming_overrides"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{series}.yaml").write_bytes(f"layout: {layout}\n".encode())


def decided_from(monkeypatch):
    """Record each junk decision's (chapter id, layout of the scan it is decided from), in order; return the list.

    A decided chapter's scan row is deleted once it leaves junk, so the layout is seen as it is decided.
    """
    seen = []
    real = junk_stage.decide

    def decide(ctx, chapter, key, row):
        seen.append((chapter["id"], json.loads(row["scan"])["layout"]))
        return real(ctx, chapter, key, row)

    monkeypatch.setattr(junk_stage, "decide", decide)
    return seen


@budget(load="db", seconds=20)
def test_a_layout_change_rescans_a_chapter_waiting_on_a_junk_decision_in_the_same_run(site, monkeypatch):
    """A pages series' waiting chapter is scanned again under the override's strip layout in the run of the change."""
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    site.download("Alpha", 2, cbz(seed="w2", credit=True))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "the credit page holds the chapter"
    set_override_layout(site, "Alpha", "strip")
    site.download("Alpha", 3, cbz(seed="w3"))
    seen = decided_from(monkeypatch)
    site.parts()
    assert [layout for chapter, layout in seen if chapter == 1] == ["strip"], (
        "the waiting chapter is scanned again under the new layout and decided from that scan in the same run"
    )


@budget(load="db", seconds=30)
def test_a_layout_change_sends_a_chapter_at_convert_back_to_junk_and_it_converts(site, monkeypatch):
    """A chapter at convert holding a page edit goes back to junk, then converts under the new layout."""
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    site.download("Alpha", 2, cbz(seed="v2", credit=True))
    site.parts()
    assert decide_junk(site.cfg, "junk") == 1
    with monkeypatch.context() as m:  # convert is out of reach this run: the chapter stays at convert
        m.setattr(convert_stage, "run", raise_unreachable)
        run_part(site.cfg, process)
    assert site.chapters() == [("convert", "ready", None)], "decided, its page edit waits at convert"
    set_override_layout(site, "Alpha", "strip")
    site.download("Alpha", 3, cbz(seed="v3"))
    seen = decided_from(monkeypatch)
    site.parts()
    ((stage, status, error),) = site.rows("SELECT stage, status, last_error FROM chapter WHERE id = 1")
    assert status != "needs_human", f"the chapter converts with no stale page edit: {error}"
    assert "chapter_edit" not in (error or ""), f"the chapter converts with no stale page edit: {error}"
    assert stage == "comicinfo", "the chapter passes convert"
    assert [layout for chapter, layout in seen if chapter == 1] == ["strip"], (
        "the chapter at convert is back at junk and decided from a strip scan in the same run"
    )


def raise_unreachable(_ctx, _chapter):
    service = "vship"
    raise errors.Unreachable(service, "test: convert out of reach")


@budget(load="db", seconds=20)
def test_a_layout_change_keeps_a_needs_human_chapters_candidate_open_until_decided_and_retried(site, monkeypatch):
    """A junk chapter held on an unsure candidate keeps it open; decided and retried, it is scanned again."""
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    site.download("Alpha", 2, cbz(seed="u2", credit=True))
    site.parts()
    (cand,) = open_candidates(site)
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [cand["id"]], "unsure", None, workflow.now(conn))
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "needs_human", None)], "unsure: a person decides"
    set_override_layout(site, "Alpha", "strip")
    site.download("Alpha", 3, cbz(seed="u3"))
    site.parts()
    assert site.chapters()[0] == ("junk", "needs_human", None), "a needs_human chapter is not reset"
    with closing(db.connect(site.cfg)) as conn:
        assert cand["id"] in [r["id"] for r in candidates.open_rows(conn)], "its candidate stays open"
        review.decide(conn, [cand["id"]], "keep", None, workflow.now(conn))
        workflow.retry_chapter(conn, 1)
    seen = decided_from(monkeypatch)
    run_part(site.cfg, process)
    assert [layout for chapter, layout in seen if chapter == 1] == ["strip"], (
        "decided and retried, the chapter is scanned again under strip"
    )


@budget(load="db", seconds=20)
def test_a_layout_change_leaves_a_converted_chapter_as_it_is(site, monkeypatch):
    """A chapter already converted (at comicinfo) keeps its stage and status."""
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")  # the real engine reads noise for seconds per block
    site.download("Alpha", 2, cbz(seed="p2"))
    site.parts()
    assert site.chapters() == [("comicinfo", "ready", None)], "converted, it waits for export"
    set_override_layout(site, "Alpha", "strip")
    site.download("Alpha", 3, cbz(seed="p3"))
    site.parts()
    assert site.chapters()[0] == ("comicinfo", "ready", None), "a converted chapter is published as converted"


@budget(load="db", seconds=20)
def test_an_override_naming_the_same_layout_resets_no_chapter(site):
    """An override that names the series' own layout changes only its origin: every scan stays current."""
    site.download("Alpha", 2, cbz(seed="s2", credit=True))
    site.parts()
    (before,) = site.rows("SELECT scan FROM junk_scan")
    set_override_layout(site, "Alpha", "pages")
    site.download("Alpha", 3, cbz(seed="s3"))
    site.parts()
    assert site.rows("SELECT layout, origin FROM series_conversion") == [("pages", "override")], "the origin changes"
    assert site.chapters()[0] == ("junk", "waiting", "junk_decision"), "the waiting chapter is not reset"
    assert site.rows("SELECT scan FROM junk_scan WHERE chapter_id = 1") == [before], "its scan is kept"
    with closing(db.connect(site.cfg)) as conn:
        chapter = present(conn.execute("SELECT * FROM chapter WHERE id = 1").fetchone())
        assert junk_stage.current_scan(conn, chapter) is not None, "its scan is still current"


def refill(site):
    """Return each chapter's junk refill record (junk_candidates, junk_new_series), in id order."""
    return site.rows("SELECT junk_candidates, junk_new_series FROM chapter ORDER BY id")


@budget(load="db", seconds=10)
def test_the_first_decide_after_a_scan_records_its_candidates_and_a_later_one_keeps_them(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    assert refill(site) == [(1, True)], "the first decide waits on 1 candidate; the series has no decision of its own"
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "decided, the chapter goes on"
    assert refill(site) == [(1, True)], "the decide that ends Done keeps the first decide's record"


@budget(load="db", seconds=10)
def test_a_new_scan_clears_the_record(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET junk_candidates = 99")
        conn.execute("DELETE FROM junk_scan")  # the scan is gone: the chapter is scanned again
        conn.execute("UPDATE chapter SET status = 'ready', waiting_on = NULL")
    run_part(site.cfg, process)
    assert refill(site) == [(1, True)], "a new scan clears the record; its first decide records it anew"


@budget(load="db", seconds=20)
def test_a_first_decide_held_on_unsure_candidates_records_them(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    (cand,) = open_candidates(site)
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [cand["id"]], "unsure", None, workflow.now(conn))
    site.download("Alpha", 3, cbz(credit=True))
    site.parts()
    got = site.rows("SELECT status, junk_candidates FROM chapter ORDER BY id")
    assert got[1] == ("needs_human", 1), "a first decide held on an unsure candidate records the candidates it held on"


@budget(load="db", seconds=10)
def test_a_series_whose_only_decision_is_retired_counts_as_new_in_the_refill(site):
    site.download("Alpha", 2, cbz(credit=True))
    run_part(site.cfg, intake)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        store.upsert(
            conn,
            [
                {
                    "series_key": "Alpha",
                    "kind": "page",
                    "key": "123456789abcdef0|0.7",
                    "verdict": "keep",
                    "decider": "human",
                    "decided_at": "2026-01-01T00:00:00+00:00",
                    "source": None,
                }
            ],
        )
        act = store.new_act(conn, "test", "2026-01-02T00:00:00+00:00")
        store.retire(conn, [one(conn, "SELECT id FROM junk_decision")], act)
    run_part(site.cfg, process)
    assert refill(site) == [(1, True)], "a retired decision is history: the series has none of its own in force"


# ---------------------------------------------------------------- chains and cuts (SPEC "Junk decisions")

CHAIN_PANELS = ["a", "b", "c", "d", "e", "f"]
FIX_JUNK = Path(__file__).parent / "fixtures_pages" / "junk"


def strip_rows(data):
    """Return a panels CBZ's strip (its pages stacked: 1 width, no resize) as an RGB array."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return np.vstack([np.asarray(Image.open(io.BytesIO(z.read(n))).convert("RGB")) for n in sorted(z.namelist())])


def span_segment(pixels, rows):
    """Return a cut segment of strip rows (start, end): their height in strip widths and their tiles."""
    s, e = rows
    tiles = present(anchored.signable(Image.fromarray(pixels[s:e]))).split("|")[0]
    return {"kind": "cut", "h": round((e - s) / pixels.shape[1], 4), "tiles": tiles}


def chain_text(side, *segs, src):
    return chains.text({side: [{"segments": list(segs), "ref": [["S", "c.cbz", 0]], "src": src}]})


def chained_strip(site, monkeypatch):
    """Scan 1 strip chapter of 6 panels; return (its CBZ bytes, its stored scan)."""
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    data = panels_cbz(CHAIN_PANELS)
    site.download("Long", 2, data)
    site.parts()
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    return data, json.loads(scan)


def counted_extracts(monkeypatch):
    """Count junk_stage.extract calls; return the list each call appends to."""
    calls = []
    real = junk_stage.extract

    def counting(cbz, known, work):
        calls.append(cbz)
        return real(cbz, known, work)

    monkeypatch.setattr(junk_stage, "extract", counting)
    return calls


@budget(load="db", seconds=30)
def test_a_cut_decision_removes_its_range_from_the_download_and_leaves_no_scratch(site, monkeypatch):
    data, sc = chained_strip(site, monkeypatch)
    last = tuple(sc["spans"][-1])
    cut = chain_text("below", span_segment(strip_rows(data), last), src={"file": "c.cbz", "sha256": "a" * 64})
    decision: store.Decision = {
        "series_key": "Long",
        "kind": "cut",
        "key": chains.key_span(chains.parse(cut)),
        "verdict": "junk",
        "decider": "import",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "source": "test",
        "chain": cut,
        "credit": False,
    }
    imported(site.cfg, decision)
    decide_junk(site.cfg, "keep")
    calls = counted_extracts(monkeypatch)
    run_part(site.cfg, process)
    ((cut_id,),) = site.rows("SELECT id FROM junk_decision WHERE kind = 'cut'")
    (edit,) = edits(site)
    assert {"start": last[0], "end": last[1], "why": "cut", "decision": cut_id} in edit["strip_removed"], edit
    assert len(calls) == 1, "the download is extracted once, at decide"
    scratch = config.abs_path(site.cfg["paths"]["scratch"])
    assert not list(scratch.rglob("junk")), "the decide's scratch folder is gone"


@budget(load="db", seconds=30)
def test_a_series_without_chains_or_cuts_extracts_nothing_at_decide(site, monkeypatch):
    chained_strip(site, monkeypatch)
    decide_junk(site.cfg, "keep")
    calls = counted_extracts(monkeypatch)
    run_part(site.cfg, process)
    assert all(c[0] != "junk" for c in site.chapters()), "kept blocks free the chapter"
    assert calls == [], "no chain, no cut, no new candidate: decide reads no pixels"


@budget(load="db", seconds=30)
def test_a_card_that_does_not_match_holds_its_chapter_for_a_person(site, monkeypatch):
    data, sc = chained_strip(site, monkeypatch)
    pixels = strip_rows(data)
    band = sc["blocks"][-2]  # panel e, in the tail half
    first = tuple(sc["spans"][0])  # panel a's tiles where panel f lies: no match
    card = chain_text("below", {**span_segment(pixels, first), "h": round(400 / 300, 4)}, src=None)
    decision: store.Decision = {
        "series_key": "Long",
        "kind": "block",
        "key": band["sig"],
        "verdict": "junk",
        "decider": "import",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "source": "test",
        "chain": card,
    }
    imported(site.cfg, decision)
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    ((status, reason),) = site.rows("SELECT status, last_error FROM chapter")
    ((block_id,),) = site.rows("SELECT id FROM junk_decision WHERE kind = 'block' AND decider = 'import'")
    assert status == "needs_human", "a card that should apply and does not: a person decides"
    assert f"junk decision {block_id}'s card (below) does not match beside rows {band['start']}-{band['end']}" in (
        reason
    ), reason


def import_decision(site, kind, key, chain, *, credit=False):
    decision: store.Decision = {
        "series_key": "Long",
        "kind": kind,
        "key": key,
        "verdict": "junk",
        "decider": "import",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "source": "test",
        "chain": chain,
    }
    imported(site.cfg, {**decision, "credit": True} if credit else decision)
    ((row_id,),) = site.rows("SELECT id FROM junk_decision WHERE decider = 'import'")
    return row_id


@budget(load="db", seconds=30)
def test_a_chained_blocks_edge_card_removes_every_row_to_the_strip_end(site, monkeypatch):
    _, sc = chained_strip(site, monkeypatch)
    band = sc["blocks"][-2]  # panel e, in the tail half
    card = chain_text("below", {"kind": "edge"}, src=None)
    block_id = import_decision(site, "block", band["sig"], card)
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    (edit,) = edits(site)
    assert {"start": band["start"], "end": band["end"], "why": "match", "decision": block_id, "hash": band["sig"]} in (
        edit["strip_removed"]
    ), edit
    assert {"start": band["end"], "end": sc["height"], "why": "card", "decision": block_id} in edit["strip_removed"], (
        "the card: every row from the block to the strip end, its decision named, no hash"
    )


@budget(load="db", seconds=30)
def test_44_a_credit_cut_on_the_real_card_removes_its_notice_lines_through_the_stage(site, monkeypatch):
    """#44 as #38's version: I Was the Real Head of the House c0003's D&C card (its crop, 1 page).

    [keep (c)][keep logo][cut][cut][cut] from the gap seed under the (c) line, credit: the 3 notice lines are cut.
    """
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    png = (FIX_JUNK / "s5_2_real_head_c0003" / "strip.png").read_bytes()
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("001.png", png)
    site.download("Long", 2, data.getvalue())
    site.parts()
    pixels = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    o = 111380  # the crop's first row in the chapter's strip
    rows = [(111830, 111847), (111751, 111787), (111665, 111684), (111638, 111657), (111612, 111630)]
    segs = [
        {**span_segment(pixels, (s - o, e - o)), "kind": kind}
        for kind, (s, e) in zip(("keep", "keep", "cut", "cut", "cut"), rows, strict=True)
    ]
    cut = chain_text("above", *segs, src={"file": "Gamma_Chapter 3.cbz", "sha256": "a" * 64})
    cut_id = import_decision(site, "cut", chains.key_span(chains.parse(cut)), cut, credit=True)
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    (edit,) = edits(site)
    got = [(r["start"] + o, r["end"] + o) for r in edit["strip_removed"] if r["why"] == "cut"]
    assert got == [(111612, 111630), (111638, 111657), (111665, 111684)], edit["strip_removed"]
    assert all(r.get("decision") == cut_id and "hash" not in r for r in edit["strip_removed"] if r["why"] == "cut")


@budget(load="db", seconds=30)
def test_a_decide_that_renders_and_writes_candidate_images_extracts_once(site, monkeypatch):
    """A cut decision known before the scan: the decide renders for it and writes candidate images, 1 extraction."""
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    data = panels_cbz(CHAIN_PANELS)
    last = (2400 + 700, 2400 + 1100)  # panel f: page 3's second panel
    cut = chain_text("below", span_segment(strip_rows(data), last), src={"file": "c.cbz", "sha256": "a" * 64})
    import_decision(site, "cut", chains.key_span(chains.parse(cut)), cut)
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, data)
    calls = counted_extracts(monkeypatch)
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "its blocks are offered"
    assert open_candidates(site), "their images are written"
    assert len(calls) == 2, "1 extraction to scan, 1 to decide: rendering and the images share it"
    assert not list(config.abs_path(site.cfg["paths"]["scratch"]).rglob("junk")), "no scratch folder is left"


@budget(load="db", seconds=30)
def test_a_decide_failing_after_its_extraction_leaves_no_scratch_folder(site, monkeypatch):
    data, sc = chained_strip(site, monkeypatch)
    last = tuple(sc["spans"][-1])
    cut = chain_text("below", span_segment(strip_rows(data), last), src={"file": "c.cbz", "sha256": "a" * 64})
    import_decision(site, "cut", chains.key_span(chains.parse(cut)), cut)
    decide_junk(site.cfg, "keep")

    def broken(render):
        render(0, 10)  # the download is extracted
        msg = "a fault while matching"
        raise RuntimeError(msg)

    monkeypatch.setattr(anchored, "span_sigs", broken)
    run_part(site.cfg, process)
    ((attempts, reason),) = site.rows("SELECT attempts, last_error FROM chapter")
    assert attempts == 1, "the chapter fails"
    assert "a fault while matching" in reason, reason
    assert not list(config.abs_path(site.cfg["paths"]["scratch"]).rglob("junk")), "the decide's folder is removed"


@budget(load="db", seconds=60)
def test_a_held_card_decides_again_after_its_fix_and_holds_again_after_junk_undo(site, monkeypatch, tmp_path):
    data, sc = chained_strip(site, monkeypatch)
    band = sc["blocks"][-2]
    first = tuple(sc["spans"][0])
    card = chain_text("below", {**span_segment(strip_rows(data), first), "h": round(400 / 300, 4)}, src=None)
    block_id = import_decision(site, "block", band["sig"], card)
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    ((chapter_id, status),) = site.rows("SELECT id, status FROM chapter")
    assert status == "needs_human"
    path = str(config_file(tmp_path, site.cfg))
    assert cli.main(["--config", path, "junk", "redecide", str(block_id), "keep"]) == 0, "the fix: a plain keep"
    ((act,),) = site.rows("SELECT max(id) FROM junk_act")
    assert cli.main(["--config", path, "retry", str(chapter_id)]) == 0
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "decided again, the chapter goes on"
    assert cli.main(["--config", path, "junk", "undo", str(act)]) == 0, "the chained decision is back"
    assert site.rows("SELECT chain FROM junk_decision WHERE id = %s", block_id) == [(card,)]


# ---------------------------------------------------------------- rest of card (W5, SPEC "Junk decisions")


def bundled_strip(site, monkeypatch):
    """Wait chapter 2 of strip series Long on its 3 head blocks and its rest of card: 2 tail pieces.

    Its 4th panel, an imported junk block decision, starts the credit; the 2 panels after it are the bundle. Returns
    the 2 pieces' candidate ids, in strip order.
    """
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, panels_cbz(["a2", "b2", "c2", "credit", "p1", "p2"]))
    site.parts()
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    credit = next(b["sig"] for b in json.loads(scan)["blocks"] if 1850 <= b["start"] <= 1950)
    decision: store.Decision = {
        "series_key": "Long",
        "kind": "block",
        "key": credit,
        "verdict": "junk",
        "decider": "import",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "source": "test",
    }
    imported(site.cfg, decision)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")]
    return [r[0] for r in site.rows("SELECT id FROM junk_candidate WHERE kind = 'piece' ORDER BY id")]


@budget(load="pool", seconds=60)
def test_a_split_bundle_is_recorded_marked_and_noted_mixed_in_status(site, monkeypatch, capsys, tmp_path):
    p1, p2 = bundled_strip(site, monkeypatch)
    ((bundles,),) = site.rows("SELECT bundles FROM junk_scan")
    assert json.loads(bundles) == [{"ids": [p1, p2], "size": 2}], "the wait records the bundle with its candidates"
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [p1], "junk", None, workflow.now(conn))
        review.decide(conn, [p2], "keep", None, workflow.now(conn))
    assert site.rows("SELECT DISTINCT bundle FROM junk_decision WHERE kind = 'piece'") == [(True,)], "both marked"
    decide_kinds(site.cfg, {"block": "keep"})
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "a mixed bundle holds nothing"
    ((notes,),) = site.rows("SELECT notes FROM chapter")
    (note,) = json.loads(notes)
    assert note.startswith("rest of card mixed: rows "), note
    assert cli.main(["--config", str(config_file(tmp_path, site.cfg)), "status"]) == 0
    assert note in capsys.readouterr().out, "status shows the chapter's note"
    with closing(db.connect(site.cfg)) as conn:
        kept = one(conn, "SELECT id FROM junk_decision WHERE kind = 'piece' AND verdict = 'keep'")
        redecide.redecide(conn, [kept], "junk", workflow.now(conn))
        with db.transaction(conn):  # the chapter is junk-checked again (as a layout change sends it back)
            conn.execute("UPDATE chapter SET stage = 'junk', status = 'ready'")
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "decided again: both members junk now"
    assert site.rows("SELECT notes FROM chapter") == [(None,)], "no longer mixed: its note is removed"


def piece_keep(site, candidate_id, *, bundle):
    """Write an imported piece keep on a candidate's key, marked or not, beside the imported credit decision."""
    ((key,),) = site.rows("SELECT key FROM junk_candidate WHERE id = %s", candidate_id)
    keep: store.Decision = {
        "series_key": "Long",
        "kind": "piece",
        "key": key,
        "verdict": "keep",
        "decider": "import",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "source": "test keep",
        "bundle": bundle,
    }
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        store.upsert(conn, [keep])


@budget(load="pool", seconds=60)
@pytest.mark.parametrize("marked", [False, True], ids=["unmarked-keep", "marked-keep"])
def test_an_unmarked_piece_keep_protects_its_gap_and_a_marked_one_does_not(site, monkeypatch, marked):
    """p1 kept, p2 undecided, 1 gap: an unmarked keep is a judgement beside an unjudged piece, so no bundle."""
    p1, p2 = bundled_strip(site, monkeypatch)
    piece_keep(site, p1, bundle=marked)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "it waits on p2 and the head blocks"
    ((ids, bundles),) = site.rows("SELECT candidates, bundles FROM junk_scan")
    assert p2 in json.loads(ids), "p2 is still a candidate"
    want = [{"ids": [p2], "size": 2}] if marked else []
    assert json.loads(bundles) == want, "a marked keep protects no gap; an unmarked one does"


@budget(load="pool", seconds=60)
def test_the_cutover_clears_a_chapters_record_and_process_decides_it_again_with_its_bundle(
    site, monkeypatch, tmp_path, capsys
):
    p1, p2 = bundled_strip(site, monkeypatch)
    before = site.rows("SELECT stage, status, waiting_on, updated_at FROM chapter")
    assert w5_cutover.main(["--config", str(config_file(tmp_path, site.cfg))]) == 0
    assert "chapters: 1 (waiting 1, needs_human 0) cleared" in capsys.readouterr().out
    assert site.rows("SELECT candidates, bundles FROM junk_scan") == [("[]", "[]")], "both records cleared"
    assert site.rows("SELECT stage, status, waiting_on, updated_at FROM chapter") == before, "no status written"
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "decided again: it waits on its candidates"
    ((ids, bundles),) = site.rows("SELECT candidates, bundles FROM junk_scan")
    assert {p1, p2} <= set(json.loads(ids)), "its piece candidates again"
    assert json.loads(bundles) == [{"ids": [p1, p2], "size": 2}], "now with its bundle record"


@budget(load="pool", seconds=60)
def test_a_held_bundle_member_records_the_bundle_names_it_and_a_rescan_drops_a_stale_record(site, monkeypatch):
    p1, p2 = bundled_strip(site, monkeypatch)
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, [p1], "unsure", None, workflow.now(conn))
    run_part(site.cfg, process)
    ((reason,),) = site.rows("SELECT last_error FROM chapter")
    assert f"rest of card {p1} {p2}: judge them together (junk decide {p1} {p2} junk|keep)" in reason, reason
    ((bundles,),) = site.rows("SELECT bundles FROM junk_scan")
    assert json.loads(bundles) == [{"ids": [p1, p2], "size": 2}], "the hold records the bundle too"
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("""UPDATE junk_scan SET content_digest = 'stale', bundles = '[{"ids": [999], "size": 5}]'""")
        conn.execute("UPDATE junk_candidate SET verdict = NULL")
    with closing(db.connect(site.cfg)) as conn:
        workflow.retry_chapter(conn, site.rows("SELECT id FROM chapter")[0][0])
    run_part(site.cfg, process)
    ((bundles,),) = site.rows("SELECT bundles FROM junk_scan")
    assert json.loads(bundles) == [{"ids": [p1, p2], "size": 2}], "rescanned: the stale record is gone"


# ---------------------------------------------------------------- R19: page aspects from every live chapter


def sized_cbz(size, pages, seed):
    """Return a CBZ of `pages` noise pages of this (width, height)."""
    rng = np.random.default_rng(seed)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        for n in range(1, pages + 1):
            im = Image.fromarray(rng.integers(0, 255, (size[1], size[0]), dtype=np.uint8), "L")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            z.writestr(f"{n:03}.png", buf.getvalue())
    return data.getvalue()


def stored_aspect(site):
    ((text,),) = site.rows("SELECT page_aspect FROM series_conversion")
    return None if text is None else json.loads(text)


def page_series(site, first, origin="computed", aspect=None, aspect_origin=None):
    """Download chapter 1 (CBZ `first`), run intake, store the series as a page series of these origins and aspect.

    origin: the layout's; aspect_origin: the aspect's (computed by default when an aspect is given).
    """
    site.download("Alpha", 1, first)
    run_part(site.cfg, intake)
    if aspect is not None and aspect_origin is None:
        aspect_origin = "computed"
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute(
            "UPDATE series_conversion SET layout = 'pages', origin = %s, page_aspect = %s, aspect_origin = %s",
            (origin, None if aspect is None else json.dumps(aspect), aspect_origin),
        )


@budget(load="db", seconds=20)
def test_the_page_aspect_follows_the_series_past_an_atypical_first_chapter(site):
    page_series(site, sized_cbz((100, 150), 4, seed=1))
    run_part(site.cfg, process)
    assert stored_aspect(site) == [1.5], "the first chapter's shape, as before"
    for n in (2, 3):
        site.download("Alpha", n, sized_cbz((100, 142), 6, seed=n))
    site.parts()
    assert present(stored_aspect(site))[0] == 1.42, "R19: the shape of every live chapter's pages, most common first"


@budget(load="db", seconds=20)
def test_a_dropped_chapters_pages_are_not_counted(site):
    page_series(site, sized_cbz((100, 150), 4, seed=1))
    run_part(site.cfg, process)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):  # as chapter drop leaves it, download removed
        conn.execute("UPDATE chapter SET status = 'done', last_error = %s", (workflow.DROPPED_BY_PERSON,))
        ((path,),) = conn.execute("SELECT download_path FROM chapter").fetchall()
    config.to_local(site.cfg, path).unlink()
    for n in (2, 3):
        site.download("Alpha", n, sized_cbz((100, 142), 6, seed=n))
    site.parts()
    assert stored_aspect(site) == [1.42], "the dropped chapter's 1.5 pages are no candidate"


@pytest.mark.parametrize(
    ("origin", "stored", "aspect_origin", "expected"),
    [
        ("record", [1.5], "record", [1.5]),
        ("override", [1.5], "record", [1.5]),  # an override file names a layout: the record's aspect still stands
        ("override", None, None, [1.42]),
        ("record", [1.5], "computed", [1.42]),  # filled at junk for a record that held none: computed again
    ],
)
@budget(load="db", seconds=20)
def test_a_records_page_aspect_stands_whatever_the_layouts_origin(site, origin, stored, aspect_origin, expected):
    page_series(site, sized_cbz((100, 142), 6, seed=1), origin=origin, aspect=stored, aspect_origin=aspect_origin)
    run_part(site.cfg, process)
    assert stored_aspect(site) == expected
    computed = site.rows("SELECT aspect_origin FROM series_conversion")
    assert computed == [("record" if expected == stored and aspect_origin == "record" else "computed",)]


@budget(load="db", seconds=20)
def test_a_chapter_with_no_page_like_page_leaves_the_stored_aspect(site):
    page_series(site, sized_cbz((100, 400), 4, seed=1), aspect=[1.42])  # 4.0: stitched, no page-like page
    run_part(site.cfg, process)
    assert stored_aspect(site) == [1.42]


@pytest.mark.parametrize(
    ("stage", "error", "counted"),
    [("publish", None, True), ("confirm", None, True), ("confirm", workflow.DROPPED_BY_PERSON, False)],
)
@budget(load="db", seconds=20)
def test_a_published_chapter_counts_at_publish_or_confirm_and_one_dropped_there_does_not(site, stage, error, counted):
    page_series(site, sized_cbz((100, 150), 4, seed=1))
    run_part(site.cfg, process)
    # a conversion ends at publish, a download at confirm; the download has left either way
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET status = 'done', stage = %s, last_error = %s", (stage, error))
        ((path,),) = conn.execute("SELECT download_path FROM chapter").fetchall()
    config.to_local(site.cfg, path).unlink()
    for n in (2, 3):
        site.download("Alpha", n, sized_cbz((100, 142), 6, seed=n))
    site.parts()
    assert (stored_aspect(site) == [1.42, 1.5]) is counted, "chapter 1's 4 pages at 1.5: a 2nd candidate when counted"


def png_cbz(images):
    """Return a CBZ of these images (numpy arrays) as PNG pages 001.png, 002.png, ..."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n, a in enumerate(images, 1):
            out = io.BytesIO()
            Image.fromarray(a).save(out, "PNG")
            z.writestr(f"{n:03}.png", out.getvalue())
    return data.getvalue()


@budget(load="db", seconds=20)
def test_junk_records_a_re_sliced_chapters_page_count_in_its_edit(site):
    site.download("Resliced", 1, png_cbz([reslice_page(100 + i) for i in range(12)]))
    site.download("Resliced", 2, png_cbz([reslice_page(200 + i) for i in range(12)]))
    site.download("Resliced", 3, png_cbz(recut(PAGES_28, [224] * 21)))
    site.parts()
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    ((text,),) = site.rows("SELECT e.edit FROM chapter_edit e JOIN chapter c ON c.id = e.chapter_id WHERE c.number = 3")
    edit = json.loads(text)
    assert (edit["split"], edit["split_pages"]) == (True, 28), "junk recorded chapter 3's 28 pages in its edit"


# ---------------------------------------------------------------- agreement of other series (plan phase-10-6, step 2)


def agree(cfg, kind, key, verdict="junk", chain=None):
    """Write 1 decision on kind and key in each of 2 other series, X (with chain) and Y; return their ids."""
    rows: list[store.Decision] = [
        {
            "series_key": s,
            "kind": kind,
            "key": key,
            "verdict": verdict,
            "decider": "claude",
            "decided_at": "2026-11-01T00:00:00+00:00",
            "source": "test",
            "chain": chain if s == "X" else None,
        }
        for s in ("X", "Y")
    ]
    with closing(db.connect(cfg)) as conn, db.transaction(conn):
        store.upsert(conn, rows)
        return [r[0] for r in conn.execute("SELECT id FROM junk_decision WHERE series_key IN ('X', 'Y') ORDER BY id")]


@budget(load="db", seconds=15)
def test_an_agreed_unit_is_no_candidate_and_ends_a_wait(site):
    site.download("Alpha", 2, cbz(seed="g2", credit=True))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "the chapter waits on its credit page"
    (c,) = open_candidates(site)
    ids = agree(site.cfg, c["kind"], c["key"])
    assert open_candidates(site) == [], "the agreement closes the candidate"
    with closing(db.connect(site.cfg)) as conn, pytest.raises(review.Refused) as refused:
        review.get_open(conn, c["id"], None)
    assert f"decisions {ids[0]}, {ids[1]} of other series agree it is junk" in str(refused.value), refused.value
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "the wait ends and the agreed junk decides the chapter"


def agreed_watermark(site, monkeypatch):
    """Scan and decide strip chapter 2 of Long, ending with the same band twice; X and Y then junk the band.

    Return the stand-in's id (the newer of the 2 agreeing decisions).
    """
    monkeypatch.setattr(ocr, "text", lambda _im, _device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, panels_cbz(["a2", "b2", "c2", "d2", "mark", "mark"]))
    site.parts()
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    return agree(site.cfg, "block", json.loads(scan)["blocks"][-1]["sig"])


@budget(load="pool", seconds=60)
def test_an_agreed_removal_remembers_no_watermark(site, monkeypatch):
    """The band twice at 1 tail is a watermark; removed by agreement, it is not remembered (no row written)."""
    ids = agreed_watermark(site, monkeypatch)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "its other blocks are still undecided"
    assert site.rows("SELECT key FROM junk_watermark") == [], "not remembered while it waits"
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    (edit,) = edits(site)
    agreed = [r for r in edit["strip_removed"] if r.get("decision") == ids[-1]]
    assert len(agreed) == len(["mark", "mark"]), "both bands are removed, named by the stand-in's (real) decision"
    assert site.rows("SELECT key FROM junk_watermark") == [], "nor when the chapter is done"


@budget(load="pool", seconds=60)
def test_a_chapter_logs_its_agreed_units_when_a_decide_ends_it_and_not_while_waiting(site, monkeypatch, caplog):
    ids = agreed_watermark(site, monkeypatch)
    caplog.set_level("INFO", logger=junk_stage.__name__)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "a waiting decide"
    assert not [r for r in caplog.records if "agreement" in r.getMessage()], "a waiting decide logs nothing"
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    (line,) = [r.getMessage() for r in caplog.records if "agreement" in r.getMessage()]
    (edit,) = edits(site)
    agreed = [r for r in edit["strip_removed"] if r.get("decision") == ids[-1]]
    assert agreed, "the bands are removed by the stand-in's decision"
    for r in agreed:
        assert f"block rows {r['start']}-{r['end']} junk by decisions {ids[0]} {ids[1]}" in line, line
    assert "keep" not in line, "the kept blocks are the series' own decisions: not named"


@budget(load="db", seconds=30)
def test_an_agreed_chained_decision_removes_no_card_and_holds_nothing(site, monkeypatch):
    """X's chained band decision and Y's plain one agree: the band goes, its card is not tried, nothing holds."""
    data, sc = chained_strip(site, monkeypatch)
    band = sc["blocks"][-2]  # panel e, in the tail half
    first = tuple(sc["spans"][0])  # panel a's tiles where panel f lies: a card that would miss
    card = chain_text("below", {**span_segment(strip_rows(data), first), "h": round(400 / 300, 4)}, src=None)
    ids = agree(site.cfg, "block", band["sig"], chain=card)
    for _ in range(2):  # the band removed, panel f below it is a tail piece, offered and kept in turn
        decide_junk(site.cfg, "keep")
        run_part(site.cfg, process)
        assert site.chapters()[0][1] != "needs_human", "an agreed chain never holds a chapter"
    (edit,) = edits(site)
    assert {"start": band["start"], "end": band["end"], "why": "match", "decision": ids[-1], "hash": band["sig"]} in (
        edit["strip_removed"]
    ), edit
    assert not [r for r in edit["strip_removed"] if r["why"] == "card"], "no card is removed"


def standin(row_id, key, verdict="junk"):
    """Return a stand-in row of an agreement of decisions 1 and row_id (row_id the newest)."""
    return db.row(
        id=row_id,
        series_key="X",
        kind="block",
        key=key,
        verdict=verdict,
        decider=store.AGREEMENT,
        chain=None,
        agreeing=(1, row_id),
    )


def own_row(row_id, key, verdict="junk"):
    return db.row(id=row_id, series_key="S", kind="block", key=key, verdict=verdict, decider="claude", chain=None)


@budget()
def test_a_stored_row_on_the_agreed_key_that_removed_nothing_does_not_bring_the_watermark_back():
    removed: list[plan.RemovedRange] = [{"start": 0, "end": 10, "why": "match", "decision": 7, "hash": "h1"}]
    memo: junk_stage.Memo = {("block", "h1"): standin(7, "K"), ("block", "h2"): own_row(9, "K")}
    assert junk_stage.own_watermarks(["K"], removed, memo) == [], "only the agreement removed K: not remembered"
    removed.append({"start": 20, "end": 30, "why": "match", "decision": 9, "hash": "h2"})
    assert junk_stage.own_watermarks(["K"], removed, memo) == ["K"], "a stored row removed K too: remembered"


def strip_of_blocks(*blocks) -> chapter_scan.StoredStripScan:
    """Return a stored strip scan of these edge blocks [(start, end, sig)], nothing else read."""
    return {
        "layout": "strip",
        "width": 300,
        "height": 4000,
        "gaps": [],
        "spans": [],
        "blocks": [{"start": a, "end": b, "where": "head", "sig": h, "joins": []} for a, b, h in blocks],
        "measured": {},
        "middle": [],
        "marks": [],
    }


def applied_of(removed, problem=None):
    edit: plan.ChapterEdit = {
        "drop_pages": [],
        "bands": [],
        "join": [],
        "split": False,
        "split_pages": None,
        "strip_removed": removed,
        "blank": [],
    }
    return chapter_scan.Applied(edit, [], [], problem, [])


@budget()
def test_the_log_names_agreed_junk_of_a_refused_edit_and_only_kept_blocks_left_whole():
    sc = strip_of_blocks((0, 100, "h1"), (200, 300, "h2"), (400, 500, "h3"))
    memo: junk_stage.Memo = {
        ("block", "h1"): standin(7, "K1"),
        ("block", "h2"): standin(8, "K2", "keep"),
        ("block", "h3"): standin(9, "K3", "keep"),
    }
    refused = junk_stage.agreed_units(sc, applied_of([], "junk removal would leave 0 px"), memo)
    assert refused == ["block (edit refused) junk by decisions 1 7"], "the agreed junk behind a refusal, no keep"
    removed: list[plan.RemovedRange] = [
        {"start": 0, "end": 100, "why": "match", "decision": 7, "hash": "h1"},
        {"start": 450, "end": 500, "why": "card", "decision": 3},
    ]
    got = junk_stage.agreed_units(sc, applied_of(removed), memo)
    assert got == ["block rows 0-100 junk by decisions 1 7", "block rows 200-300 keep by decisions 1 8"], (
        "a kept block a card range cuts into is not left whole: not named"
    )
