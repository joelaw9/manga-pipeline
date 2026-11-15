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

from manga_pipeline import cli, config, db, identify, intake, process, state, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.convert import plan
from manga_pipeline.junk import candidates, importing, logo_units, ocr, review, store

from tests.builders import config_file, run_part
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
        run_part(self.cfg, intake)
        run_part(self.cfg, process)

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
        review.decide(conn, cand["id"], "unsure", "claude", workflow.now(conn))
        assert review.listing(conn, site.cfg) == [], "an unsure candidate is never listed to Claude again"
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "needs_human", None)], "unsure: a person decides"
    with closing(db.connect(site.cfg)) as conn:
        review.decide(conn, cand["id"], "keep", "human", workflow.now(conn))
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
    run_part(site.cfg, intake)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET content_digest = NULL")
    run_part(site.cfg, process)  # returns: a scan that could never be current would make drain spin
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
    run_part(site.cfg, process)
    assert all(c[0] != "junk" for c in site.chapters()), "kept blocks free every chapter"
    assert open_candidates(site) == [], "no candidate is offered again"


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
        "strip_removed": [],
        "blank": [],
    }, "the credit page (download index 3) is dropped; every key is present"


# ---------------------------------------------------------------- logos (SPEC "Junk decisions": units, chapter edit)


def decide_kinds(cfg, verdicts):
    """A person's verdict per candidate kind {kind: verdict} on every open candidate; returns the kinds decided."""
    with closing(db.connect(cfg)) as conn:
        rows = review.listing(conn, cfg, everything=True)
        for r in rows:
            review.decide(conn, r["id"], verdicts[r["kind"]], "human", workflow.now(conn))
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
    run_part(site.cfg, process)
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
                Image.open(io.BytesIO(logo_page(at=(100, 400))))  # at the top it would also be a band
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
    run_part(site.cfg, process)
    run_part(site.cfg, export)
    ((path, result),) = site.rows(
        "SELECT p.library_path, c.convert_result FROM published_file p JOIN chapter c ON c.identity = p.identity"
        " WHERE p.origin = 'pipeline'"
    )
    result = json.loads(result)
    assert result["outcome"] == "avif" and result["blanked"] == (["003.jpg"] if verdict == "junk" else []), result
    logo = logo_units.measure(2, Image.open(io.BytesIO(logo_page(at=(100, 400)))).convert("RGB"))
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
    """A page-format chapter of 600 x 852 art pages; page `on` (a download index) has the same WARNING band stacked
    over it behind a gutter in every chapter (as tests/test_junk_page.banded_chapter). 8 pages: 3 middle pages, so the
    edge pages have middle pages of their size beside them."""
    from tests import pages as fx

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
            verdict, who = ("unsure", "claude") if c["kind"] == "band" else ("junk", "human")
            review.decide(conn, c["id"], verdict, who, workflow.now(conn))
    run_part(site.cfg, process)
    assert site.chapters()[0][0] != "junk", "the page is dropped, so its band holds nothing: no person is asked"
    (edit,) = edits(site)
    assert (edit["drop_pages"], edit["bands"]) == ([0], []), "the page goes whole"


@budget(load="pool", seconds=60)
def test_a_candidate_deleted_under_a_waiting_chapter_ends_its_wait(site):
    """Schema step 0042 deletes candidates on unsignable keys: a chapter waiting on one and on another, still
    undecided, is decided again at once, and waits on what is undecided then."""
    site.download("Alpha", 2, band_cbz("g2", on=0))
    site.parts()
    ((before,),) = site.rows("SELECT candidates FROM junk_scan")
    page = next(c["id"] for c in open_candidates(site) if c["kind"] != "band")  # the edge page (or its frame)
    with closing(db.connect(site.cfg)) as conn:
        conn.execute("DELETE FROM junk_candidate WHERE id = %s", (page,))
    run_part(site.cfg, process)
    ((after,),) = site.rows("SELECT candidates FROM junk_scan")
    assert page in json.loads(before) and page not in json.loads(after), "decided again: the gone one is not listed"
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "it waits on the page and band offered again"


# ---------------------------------------------------------------- series-wide watermarks


def panels_cbz(panels):
    """3 tall white pages of a long strip with 2 noisy panels each; `panels` names the 6 panels' noise in reading
    order (the same name is the same panel)."""
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


@budget(load="pool", seconds=60)
def test_a_watermark_1_chapter_shows_is_known_to_the_series_next_chapters(site, monkeypatch):
    """Chapter 2 ends with the band twice: a watermark. Chapter 3 has it once at its tail, before a story panel: known
    as a watermark it starts no credit block, so the panel after it is no tail piece. The memory is by key: an
    import that rebuilds the decision under a new id changes nothing. Without the memory the panel is a tail piece."""
    monkeypatch.setattr(ocr, "text", lambda im, device: "")
    site.world.kavita_series("Long", layout="strip")
    site.mangas["Long"] = site.world.manga("Long")
    site.download("Long", 2, panels_cbz(["a2", "b2", "c2", "d2", "mark", "mark"]))
    site.parts()
    ((scan,),) = site.rows("SELECT scan FROM junk_scan")
    mark = json.loads(scan)["blocks"][-1]["sig"]
    row = {"series_key": "Long", "kind": "block", "key": mark, "verdict": "junk", "decider": "import"}
    source = ([{**row, "decided_at": "2026-11-01T00:00:00+00:00", "source": "test"}], {}, None)
    with closing(db.connect(site.cfg)) as conn:
        importing.run(conn, source)
    run_part(site.cfg, process)
    assert site.chapters() == [("junk", "waiting", "junk_decision")], "its other blocks are still undecided"
    assert site.rows("SELECT series_key, key FROM junk_watermark") == [("Long", mark)], "remembered while it waits"
    decide_junk(site.cfg, "keep")
    run_part(site.cfg, process)
    assert site.rows("SELECT series_key, key FROM junk_watermark") == [("Long", mark)], "twice at 1 tail: a watermark"
    with closing(db.connect(site.cfg)) as conn:
        importing.run(conn, source)
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
        identify.link(site.cfg, conn, world.suwayomi, world.kavita, kavita_id, manga_id=site.mangas["Alpha"])
    ((folder, key),) = site.rows("SELECT folder, rules_key FROM series")
    assert folder.endswith("Beta") and key == "Alpha", "the folder follows Kavita; the rules key stays"
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
    assert third[0] == 1 and "cleaned up meanwhile" in third[1], "the chapter fails once"
    monkeypatch.setattr(candidates, "join", real)
    with closing(db.connect(site.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET retry_after = NULL")  # the retry pause is over
    site.parts()
    assert [c[1:] for c in site.chapters()] == [("waiting", "junk_decision")] * 2, "both wait on the candidate again"
    (cand,) = open_candidates(site)
    images = config.abs_path(site.cfg["paths"]["candidates"]) / cand["folder"]
    assert (images / "unit.png").is_file(), "the retry wrote the candidate's images anew"
