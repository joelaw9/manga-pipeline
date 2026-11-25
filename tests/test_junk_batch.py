"""Series batching, several ids, --keep-rest and group sheets of a junk review (SPEC "Junk decisions", review).

Candidates are rows made open by a junk chapter waiting on them; their images are small PNGs.
"""

from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import cli, config, db
from manga_pipeline.junk import candidates, groups, review

from tests.builders import add_chapter, add_series, adopted, config_file, db_row, one
from tests.timing import budget

AT = "2026-01-01T00:00:00+00:00"
FAR = (
    0x0F0F0F0F0F0F0F0F,
    0xF0F0F0F0F0F0F0F0,
    0x00FF00FF00FF00FF,
    0xFF00FF00FF00FF00,
    0x3333333333333333,
)  # >= 32 bits apart


def key(tile, aspect=0.7):
    """Return a 1-tile signature text: tiles 1 bit apart are near, 2 of FAR are not."""
    return f"{tile:016x}|{aspect}"


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


class Series:
    """A series with 1 junk chapter that waits on every candidate added to it."""

    def __init__(self, cfg, conn, name):
        """Add the series, its chapter at junk waiting on junk_decision, and the chapter's scan row."""
        self.cfg, self.conn, self.name = cfg, conn, name
        self.chapter = add_chapter(conn, add_series(conn, name), "c1", "junk", number=1)
        with db.transaction(conn):
            conn.execute(
                "UPDATE chapter SET status = 'waiting', waiting_on = 'junk_decision' WHERE id = %s", (self.chapter,)
            )
            conn.execute(
                """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates)
                SELECT id, series_id, 'd', '{}', %s, '[]' FROM chapter WHERE id = %s""",
                (AT, self.chapter),
            )

    def candidate(self, tile, kind="block", size=(200, 100)):
        """Add an open candidate of this series with images; return its id."""
        k = key(tile)
        with db.transaction(self.conn):
            cid = one(
                self.conn,
                """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)
                VALUES (%s, %s, %s, 'recurs', %s, %s) RETURNING id""",
                (self.name, kind, k, f"{self.name}-{tile:x}", AT),
            )
            self.conn.execute(
                """UPDATE junk_scan SET candidates = (candidates::jsonb || to_jsonb(%s::bigint))::text
                WHERE chapter_id = %s""",
                (cid, self.chapter),
            )
        im = Image.new("RGB", size, "gray")
        candidates.write_images(
            self.cfg,
            f"{self.name}-{tile:x}",
            im,
            im,
            {"series": self.name, "kind": kind, "reason": "recurs", "text": "", "chapters": 1},
        )
        return cid


def listed(cfg, conn, review_id="r1"):
    return [r["id"] for r in review.listing(conn, cfg, review_id)]


def decisions(conn):
    return conn.execute(
        "SELECT series_key, kind, key, verdict, decider, source FROM junk_decision ORDER BY id"
    ).fetchall()


def with_batch(cfg: config.Config, n: int) -> config.Config:
    return {**cfg, "junk": {**cfg["junk"], "review_batch": n}}


@budget(load="db")
def test_listing_takes_whole_series_until_the_batch(site):
    cfg, conn = site
    a, b, c = Series(cfg, conn, "A"), Series(cfg, conn, "B"), Series(cfg, conn, "C")
    ids_a = [a.candidate(0x1000 + i) for i in range(2)]
    ids_b = [b.candidate(0x2000 + i) for i in range(3)]
    [c.candidate(0x3000 + i) for i in range(2)]
    assert listed(with_batch(cfg, 5), conn) == ids_a + ids_b, "whole series, in order, until the batch is full; C waits"


@budget(load="db")
def test_listing_skips_a_series_that_does_not_fit_and_goes_on(site):
    cfg, conn = site
    a, b, c = Series(cfg, conn, "A"), Series(cfg, conn, "B"), Series(cfg, conn, "C")
    ids_a = [a.candidate(0x1000 + i) for i in range(2)]
    [b.candidate(0x2000 + i) for i in range(3)]
    ids_c = [c.candidate(0x3000 + i) for i in range(2)]
    assert listed(with_batch(cfg, 4), conn) == ids_a + ids_c, "B (3) does not fit the room left (2): C (2) does"


@budget(load="db")
def test_a_series_larger_than_the_batch_is_cut_at_the_batch(site):
    cfg, conn = site
    a, b = Series(cfg, conn, "A"), Series(cfg, conn, "B")
    ids_a = [a.candidate(FAR[0])]
    ids_b = [b.candidate(0x2000 + i) for i in range(5)]
    assert listed(with_batch(cfg, 3), conn) == ids_a + ids_b[:2], "a series over the batch fills the room left"


@budget(load="db")
def test_decide_of_several_ids_writes_the_rows_the_same_ids_decided_alone_write(tmp_path):
    rows = []
    for n, together in enumerate((True, False)):
        cfg = adopted(tmp_path / str(n))
        with closing(db.connect(cfg)) as conn:
            s = Series(cfg, conn, "A")
            ids = [s.candidate(FAR[0]), s.candidate(FAR[1])]
            if together:
                review.decide(conn, ids, "junk", None, AT)
            else:
                for i in ids:
                    review.decide(conn, [i], "junk", None, AT)
            rows.append([tuple(r) for r in decisions(conn)])
    assert rows[0] == rows[1], "1 decide of 2 ids writes what 2 decides of 1 id write"
    assert len(rows[0]) == 2, "a row per candidate"


@budget(load="db")
def test_decide_of_several_ids_refuses_all_and_writes_nothing_when_one_is_not_open(site):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    good = s.candidate(FAR[0])
    with pytest.raises(review.Refused, match="999"):
        review.decide(conn, [good, 999], "junk", None, AT)
    assert decisions(conn) == [], "nothing is written when 1 id is refused"


@budget(load="db")
def test_inside_a_review_decide_refuses_all_when_one_id_was_not_listed_by_it(site):
    cfg, conn = site
    a = Series(cfg, conn, "A")
    first = a.candidate(FAR[0])
    listed(with_batch(cfg, 1), conn)
    second = a.candidate(FAR[1])
    with pytest.raises(review.Refused, match="not listed by this review"):
        review.decide(conn, [first, second], "keep", "r1", AT)
    assert decisions(conn) == [], "nothing is written"


@budget(load="db")
def test_decide_refuses_a_candidate_named_twice(site):
    cfg, conn = site
    cid = Series(cfg, conn, "A").candidate(FAR[0])
    with pytest.raises(review.Refused, match="twice"):
        review.decide(conn, [cid, cid], "junk", None, AT)


@budget(load="db")
def test_unsure_on_several_ids_marks_each(site):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    ids = [s.candidate(FAR[0]), s.candidate(FAR[1])]
    review.decide(conn, ids, "unsure", None, AT)
    verdicts = conn.execute("SELECT verdict FROM junk_candidate ORDER BY id").fetchall()
    assert [v[0] for v in verdicts] == ["unsure", "unsure"], "every id is marked"
    assert decisions(conn) == [], "unsure writes no decision"


@budget(load="db")
def test_keep_rest_keeps_every_other_listed_open_candidate_of_the_series_but_unsure(site):
    cfg, conn = site
    a, b = Series(cfg, conn, "A"), Series(cfg, conn, "B")
    junk = a.candidate(FAR[0])
    rest = a.candidate(FAR[1])
    unsure = a.candidate(FAR[2])
    b.candidate(FAR[3])
    listed(cfg, conn)
    review.decide(conn, [unsure], "unsure", "r1", AT)
    kept = review.decide_junk_keeping_rest(conn, [junk], "r1", AT)
    assert [r["id"] for r in kept] == [rest], "the rest of A it listed, but the unsure one; B untouched"
    got = {(r["key"], r["verdict"]) for r in decisions(conn)}
    assert got == {(key(FAR[0]), "junk"), (key(FAR[1]), "keep")}, "junk on the named, keep on the rest"
    assert {r["series_key"] for r in decisions(conn)} == {"A"}, "B's candidate is not kept"


@budget(load="db")
def test_keep_rest_skips_a_probe_candidate_the_new_block_junk_covers(site):
    cfg, conn = site
    a = Series(cfg, conn, "A")
    block = a.candidate(FAR[0], kind="block")
    probe = a.candidate(FAR[0] ^ 1, kind="probe")  # a block decision decides probes too, and 1 bit is near
    listed(cfg, conn)
    kept = review.decide_junk_keeping_rest(conn, [block], "r1", AT)
    assert kept == [], "the probe the new junk covers is not kept"
    assert [r["verdict"] for r in decisions(conn)] == ["junk"], "only the junk row"
    row = conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (probe,)).fetchone()
    assert not candidates.is_open(conn, row), "the probe is decided by the block's junk"


@budget(load="db")
def test_keep_rest_refuses_2_series_another_verdict_and_a_person(site, tmp_path, capsys):
    cfg, conn = site
    a, b = Series(cfg, conn, "A"), Series(cfg, conn, "B")
    ia, ib = a.candidate(FAR[0]), b.candidate(FAR[1])
    listed(cfg, conn)
    with pytest.raises(review.Refused, match="1 series"):
        review.decide_junk_keeping_rest(conn, [ia, ib], "r1", AT)
    with pytest.raises(review.Refused, match="for a review"):
        review.decide_junk_keeping_rest(conn, [ia], None, AT)
    path = str(config_file(tmp_path, cfg))
    assert cli.main(["--config", path, "junk", "decide", str(ia), "keep", "--keep-rest"]) == 2, "keep is refused"
    assert "goes with junk" in capsys.readouterr().err, "the refusal says why"
    assert decisions(conn) == [], "nothing is written"


@budget(load="db")
def test_decide_of_a_covered_candidate_names_the_covering_row(site):
    cfg, conn = site
    a = Series(cfg, conn, "A")
    first = a.candidate(FAR[0])
    review.decide(conn, [first], "junk", None, AT)
    row = one(conn, "SELECT id FROM junk_decision")
    with pytest.raises(review.Refused, match=f"decision {row} \\(junk\\) covers it"):
        review.get_open(conn, first, None)


@budget(load="db")
def test_near_candidates_of_1_kind_form_a_group_with_sheets_within_the_bounds(site):
    cfg, conn = site
    a = Series(cfg, conn, "A")
    tall = [a.candidate(FAR[0] ^ (1 << i), size=(800, 4000)) for i in range(6)]  # each 1 bit from FAR[0]: 2 bits apart
    wide = [a.candidate(FAR[4] ^ (1 << i), size=(1000, 200)) for i in range(5)]
    alone = a.candidate(FAR[2])
    rows = review.listing(conn, cfg, "r1")
    near = groups.near_groups(rows)
    assert [[r["id"] for r in g] for g in near] == [tall, wide], "2 groups of near blocks; the far one in none"
    written = groups.write_sheets(cfg, "r1", near)
    assert [len(members) for members, _ in written] == [6, 5], "every member is on its group's sheets"
    for _, sheets in written:
        for sheet in sheets:
            with Image.open(sheet) as im:
                assert max(im.size) <= groups.SHEET_LONG_EDGE, "no sheet passes the long-edge bound"
                assert im.width * im.height <= groups.SHEET_PIXELS, "no sheet passes the pixel bound"
    assert alone not in {r["id"] for g in near for r in g}, "the far candidate is ungrouped"


@budget(load="pure")
def test_frames_and_logos_are_never_grouped():
    rows = [
        db_row(id=i, series_key="A", kind=kind, key="x") for i, kind in enumerate(("frame", "frame", "logo", "logo"))
    ]
    assert groups.near_groups(rows) == [], "frames and logos are never grouped"


@budget(load="db")
def test_candidates_of_2_series_or_2_kinds_are_never_grouped(site):
    cfg, conn = site
    a, b = Series(cfg, conn, "A"), Series(cfg, conn, "B")
    a.candidate(FAR[0], kind="block")
    a.candidate(FAR[0] ^ 1, kind="page")
    b.candidate(FAR[0] ^ 3, kind="block")
    assert groups.near_groups(review.listing(conn, cfg, "r1")) == [], "groups stay within 1 series and 1 kind"


@budget(load="db")
def test_a_review_lists_once_and_keep_rest_keeps_only_what_it_was_shown(site):
    cfg, conn = site
    x = Series(cfg, conn, "X")
    ids = [x.candidate(f) for f in FAR]  # 5 candidates
    small = with_batch(cfg, 3)
    first = [r["id"] for r in review.listing(conn, small, "r1")]
    assert first == ids[:3], "a series over the batch is cut to it"
    review.decide(conn, [first[0]], "keep", "r1", AT)
    again = [r["id"] for r in review.listing(conn, small, "r1")]
    assert again == first[1:], "a second list returns what the review listed and is still open, and nothing new"
    kept = review.decide_junk_keeping_rest(conn, [first[1]], "r1", AT)
    assert [r["id"] for r in kept] == [first[2]], "--keep-rest keeps only what the review was shown"
    assert one(conn, "SELECT count(*) FROM junk_candidate WHERE review_id = 'r1'") == 3, "nothing new is marked"


@budget(load="db")
def test_show_of_several_ids_prints_each(site, tmp_path, capsys):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    ids = [s.candidate(FAR[0]), s.candidate(FAR[1])]
    path = str(config_file(tmp_path, cfg))
    assert cli.main(["--config", path, "junk", "show", *map(str, ids)]) == 0
    out = capsys.readouterr().out
    assert all(f"candidate {i}:" in out for i in ids), "each candidate is shown"


@budget(load="db")
def test_list_inside_a_review_prints_the_groups_and_their_sheets(site, tmp_path, capsys, monkeypatch):
    cfg, conn = site
    a = Series(cfg, conn, "A")
    ids = [a.candidate(FAR[0]), a.candidate(FAR[0] ^ 1)]
    path = str(config_file(tmp_path, cfg))
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", path, "junk", "list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1].startswith(f"group 1\tids {ids[0]},{ids[1]}\t"), "a line per group names its ids and sheets"
    assert lines[-1].endswith(".png"), "the group line ends with its sheets"
    assert all("\tgroup 1\t" in line for line in lines[:2]), "each member's line names its group"


def queries_of_open_rows(conn, monkeypatch):
    """Return how many statements candidates.open_rows runs on conn."""
    calls = []
    execute = conn.execute

    def counted(*args, **kwargs):
        calls.append(args[0])
        return execute(*args, **kwargs)

    with monkeypatch.context() as patch:  # undo only this patch, not the fixture's others
        patch.setattr(conn, "execute", counted)
        candidates.open_rows(conn)
    return len(calls)


@budget(load="db", seconds=5)
def test_open_rows_issues_the_same_number_of_queries_for_3_and_30_candidates_in_3_and_30_series(tmp_path, monkeypatch):
    counts = []
    for n in (3, 30):
        cfg = adopted(tmp_path / str(n))
        with closing(db.connect(cfg)) as conn:
            for i in range(n):
                Series(cfg, conn, f"S{i}").candidate(FAR[i % len(FAR)])
            assert len(candidates.open_rows(conn)) == n, "every candidate is open"
            counts.append(queries_of_open_rows(conn, monkeypatch))
    assert counts[0] == counts[1], f"open_rows runs a fixed number of queries: {counts}"


@budget(load="db")
def test_a_decision_made_from_a_candidate_names_its_images(site):
    cfg, conn = site
    cid = Series(cfg, conn, "A").candidate(FAR[0])
    review.decide(conn, [cid], "junk", None, AT)
    folder = one(conn, "SELECT folder FROM junk_candidate WHERE id = %s", (cid,))
    assert one(conn, "SELECT images FROM junk_decision") == folder, "the decision keeps the candidate's images"
