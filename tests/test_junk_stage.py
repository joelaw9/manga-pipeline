"""The junk stage end to end on a world of fakes (SPEC "Junk decisions", "Chapter stages", "Needs human"): candidates,
the junk_decision wait, the junk commands, cleanup and state reset."""

import io
import json
import zipfile
import zlib
from contextlib import closing

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import cli, config, db, intake, process, state, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.junk import ocr, review, store

from tests.builders import config_file
from tests.fakes.suwayomi import page as fake_page
from tests.timing import budget
from tests.world import World, cbz, decide_junk


class Site:
    def __init__(self, tmp_path, monkeypatch):
        self.world = World(tmp_path)
        self.cfg = self.world.cfg
        self.mangas = {}
        monkeypatch.setattr(suwayomi_client, "from_config", lambda cfg: self.world.suwayomi)
        monkeypatch.setattr(kavita_client, "from_config", lambda cfg: self.world.kavita)

    def download(self, series, number, data):
        if series not in self.mangas:
            self.world.kavita_series(series)
            self.mangas[series] = self.world.manga(series)
        return self.world.download(self.mangas[series], f"Chapter {number}", data=data)[1]

    def parts(self):
        intake.run(self.cfg)
        process.run(self.cfg)

    def rows(self, sql, *params):
        with closing(db.connect(self.cfg)) as conn:
            return [tuple(r) for r in conn.execute(sql, params)]

    def chapters(self):
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
    """8 pages of the fake page size, the last a noisy banner page shared by every chapter (same bytes): no size or
    text rule offers it, only recurrence."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(1, 8):
            z.writestr(f"{n:03}.png", fake_page(n, seed))
        z.writestr("008.png", noise("banner"))
    return data.getvalue()


def strip_cbz(seed):
    """3 tall white pages of a long strip, each with 2 noisy panels: the chapter's own panels, but the last panel is
    the same credit panel in every chapter."""
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
    assert len(got) == 1 and got[0]["kind"] in ("page", "frame"), "the same page in 20 chapters is 1 candidate"
    assert decide_junk(site.cfg, "junk") == 1
    process.run(site.cfg)
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
    intake.run(site.cfg)
    bad.write_bytes(b"not a zip any more")  # the download breaks after discovery
    process.run(site.cfg)
    got = site.chapters()
    assert got[0][:2] == ("junk", "ready"), "the broken chapter failed (ready again, 1 attempt)"
    assert got[1][0] != "junk", "its scanned sibling is decided in the same run"


@budget(load="db", seconds=10)
def test_unsure_makes_the_chapter_needs_human_until_decided_and_retried(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    (cand,) = open_candidates(site)
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, cand["id"], "unsure", "claude", workflow.now())
        assert review.listing(conn, site.cfg) == [], "an unsure candidate is never listed to Claude again"
    process.run(site.cfg)
    assert site.chapters() == [("junk", "needs_human", None)], "unsure: a person decides"
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, cand["id"], "keep", "human", workflow.now())
        workflow.retry_chapter(conn, site.rows("SELECT id FROM chapter")[0][0])
    process.run(site.cfg)
    assert site.chapters()[0][0] != "junk", "decided and retried, the chapter goes on"


@budget(load="db", seconds=10)
def test_a_candidate_past_the_review_limit_makes_its_chapter_needs_human(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET reviews = ?", (site.cfg["junk"]["review_limit"],))
    process.run(site.cfg)
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
    assert (folder / "unit.png").is_file() and (folder / "context.png").is_file(), "its images are written"
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
                    "decided_at": workflow.now(),
                    "source": "t",
                }
            ],
        )
    assert open_candidates(site) == [], "a decision from any source closes the candidate"
    process.run(site.cfg)
    assert site.chapters()[0][0] != "junk", "its chapter goes on"
    assert not folder.exists(), "a closed candidate's folder is deleted by the next process"


@budget(load="db", seconds=10)
def test_state_reset_empties_candidates_and_their_folder(site):
    site.download("Alpha", 2, cbz(credit=True))
    site.parts()
    state.reset(site.cfg)
    assert site.rows("SELECT count(*) FROM junk_candidate") == [(0,)], "candidates are workflow state"
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
    assert line[0] == str(cand["id"]) and line[-1].endswith("context.png"), "list prints id, ..., image paths"
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
    assert cli.main(["--config", path, "junk", "decide", str(cand["id"]), "junk"]) == 0
    assert site.rows("SELECT decider, verdict FROM junk_decision") == [("claude", "junk")], "Claude's verdict"


@budget(load="db", seconds=10)
def test_a_chapter_without_a_content_digest_fails_instead_of_scanning_for_ever(site):
    site.download("Alpha", 2, cbz(seed="x"))
    intake.run(site.cfg)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET content_digest = NULL")
    process.run(site.cfg)  # returns: a scan that could never be current would make drain spin
    got = site.rows("SELECT stage, status, attempts, last_error FROM chapter")
    assert got[0][:3] == ("junk", "ready", 1) and "content digest" in got[0][3], "no digest: 1 failed attempt"
    assert site.rows("SELECT count(*) FROM junk_scan") == [(0,)], "no scan is written"


@budget(load="db", seconds=5)
def test_a_strip_chapter_waits_on_its_candidates_and_finishes_once_they_are_kept(site, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda im, device: "")  # the real engine reads noise for seconds per block
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    for n in (2, 3, 4):
        site.download("Long", n, strip_cbz(f"l{n}"))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")] * 3, "each strip chapter waits on its blocks"
    got = open_candidates(site)
    assert got and {c["kind"] for c in got} <= {"block", "probe"}, got
    assert decide_junk(site.cfg, "keep") == len(got)
    process.run(site.cfg)
    assert all(c[0] != "junk" for c in site.chapters()), "kept blocks free every chapter"
    assert open_candidates(site) == [], "no candidate is offered again"


@budget(load="db", seconds=10)
def test_a_chapter_leaving_junk_has_its_chapter_edit_with_every_key(site):
    site.download("Alpha", 2, cbz(seed="e2", credit=True))
    site.parts()
    assert decide_junk(site.cfg, "junk") == 1
    process.run(site.cfg)
    ((text,),) = site.rows("SELECT edit FROM chapter_edit")
    assert json.loads(text) == {"drop_pages": [3], "bands": [], "join": [], "split": False, "strip_removed": []}, (
        "the credit page (download index 3) is dropped; every key is present"
    )
