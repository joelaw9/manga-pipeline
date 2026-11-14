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
from manga_pipeline.convert import plan
from manga_pipeline.junk import logo_units, ocr, review, store

from tests.builders import config_file
from tests.fakes.suwayomi import page as fake_page
from tests.timing import budget
from tests.world import World, cbz, decide_junk, logo_cbz, logo_page


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
        conn.execute("UPDATE junk_candidate SET reviews = %s", (site.cfg["junk"]["review_limit"],))
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
    assert json.loads(text) == {
        "drop_pages": [3],
        "bands": [],
        "join": [],
        "split": False,
        "strip_removed": [],
        "blank": [],
    }, "the credit page (download index 3) is dropped; every key is present"


# ---------------------------------------------------------------- logos (SPEC "Junk decisions": units, chapter edit)


def decide_kinds(cfg, verdicts):
    """A person's verdict per candidate kind {kind: verdict} on every open candidate; returns the kinds decided."""
    with closing(db.connect(cfg)) as conn:
        rows = review.listing(conn, cfg, everything=True)
        for r in rows:
            review.decide(conn, r["id"], verdicts[r["kind"]], "human", workflow.now())
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
    process.run(site.cfg)
    assert all(c[0] != "junk" for c in site.chapters()), "1 decision frees both"
    for edit in edits(site):
        assert [b["page"] for b in edit["blank"]] == [3] and edit["drop_pages"] == [], "the badge page is blanked"
        assert plan.check_edit(json.dumps(edit)) == edit, "convert accepts the blank"
        x0, y0, x1, y1 = edit["blank"][0]["box"]
        assert 0 <= x0 < x1 <= 1200 and 0 <= y0 < y1 <= 1800, "the box lies in the page"
        assert all(245 <= v <= 255 for v in edit["blank"][0]["fill"]), "the fill is the white margin around it"


@budget(load="db", seconds=10)
def test_a_kept_badge_gives_no_blank(site):
    site.download("Alpha", 2, logo_cbz("k2"))
    site.parts()
    assert decide_junk(site.cfg, "keep") == 1
    process.run(site.cfg)
    assert [e["blank"] for e in edits(site)] == [[]], "a kept badge stays"


@budget(load="db", seconds=10)
def test_a_scan_made_before_logos_is_scanned_again(site):
    site.download("Alpha", 2, logo_cbz("o2"))
    site.parts()
    with closing(db.connect(site.cfg)) as conn:
        conn.execute("UPDATE junk_scan SET scan = (scan::jsonb - 'marks')::text, candidates = '[]'")
        conn.execute("DELETE FROM junk_candidate")
    process.run(site.cfg)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "the chapter is held again"
    (scan,) = site.rows("SELECT scan FROM junk_scan")
    assert [g["page"] for g in json.loads(scan[0])["marks"]] == [3], "its new scan holds the badge"


def strip_logo_cbz(seed):
    """4 white strip pages of 1200 x 1800, each with a gray noisy panel of its own (a gray chapter: a coloured panel
    would make the badge page colour), the third also with the badge."""
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
    monkeypatch.setattr(ocr, "text", lambda im, device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, strip_logo_cbz("s2"))
    site.parts()
    assert site.chapters() == [("junk", "waiting", "junk_decision")]
    kinds = decide_kinds(site.cfg, {"block": "keep", "probe": "keep", "piece": "keep", "logo": "junk"})
    assert kinds.count("logo") == 1, kinds
    process.run(site.cfg)
    (edit,) = edits(site)
    assert [b["page"] for b in edit["blank"]] == [2] and edit["strip_removed"] == [], "download page 2 is blanked"
    assert plan.check_edit(json.dumps(edit)) == edit, "convert accepts a strip chapter's blank"


def jpeg_logo_cbz(seed):
    """6 gray JPEG pages of 1200 x 1800 (AVIF outcome: a blank applies), the third a white page with the badge."""
    from tests.convert_pages import gray, jpeg

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(6):
            im = (
                Image.open(io.BytesIO(logo_page()))
                if n == 2
                else gray((1200, 1800), seed=zlib.crc32(f"{seed}{n}".encode()))
            )
            z.writestr(f"{n + 1:03}.jpg", jpeg(im))
    return data.getvalue()


@budget(load="pool", seconds=60)
@pytest.mark.parametrize("verdict", ["junk", "keep"])
def test_a_badge_is_painted_over_in_the_published_chapter_only_when_junk(site, verdict):
    from manga_pipeline import export

    site.download("Alpha", 2, jpeg_logo_cbz("p2"))
    site.parts()
    kinds = decide_kinds(site.cfg, {"page": "keep", "frame": "keep", "logo": verdict})
    assert kinds.count("logo") == 1, kinds
    process.run(site.cfg)
    export.run(site.cfg)
    ((path, result),) = site.rows(
        "SELECT p.library_path, c.convert_result FROM published_file p JOIN chapter c ON c.identity = p.identity"
        " WHERE p.origin = 'pipeline'"
    )
    result = json.loads(result)
    assert result["outcome"] == "avif" and result["blanked"] == (["003.jpg"] if verdict == "junk" else []), result
    logo = logo_units.measure(2, Image.open(io.BytesIO(logo_page())).convert("RGB"))
    x0, y0, x1, y1 = logo["box"]
    with zipfile.ZipFile(path) as z:
        name = sorted(n for n in z.namelist() if n != "ComicInfo.xml")[2]
        page = np.asarray(Image.open(io.BytesIO(z.read(name))).convert("L")).astype(int)
    spread = np.ptp(page[y0:y1, x0:x1])
    if verdict == "junk":
        assert spread <= 12, "the published page's badge box is 1 flat colour: the badge is painted over"
    else:
        assert spread > 100, "a kept badge is still on the published page"
