"""Rest-of-card bundles (W5; SPEC "Junk decisions": units, decisions, review; plan phase-10-5).

The gap rule (strip_units.remnant_gaps, rest_members) on synthetic pieces and on decide's tail, judged keeps and the
bundle mark in the store, the bundle record's reader, the listing's bundles and batch cut, the mark on every writer,
and the mixed note.
"""

import json
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import cli, db, images
from manga_pipeline.junk import candidates, importing, redecide, review, store
from manga_pipeline.junk import strip_units as su
from manga_pipeline.stages import junk as junk_stage

from tests.builders import add_chapter, adopted, config_file, db_row, one
from tests.test_junk_batch import FAR, Series, key, with_batch
from tests.test_junk_store import decisions_of, record_with_rows
from tests.test_junk_strip import piece, tail_chapter, tsig
from tests.timing import budget

AT = "2026-01-01T00:00:00+00:00"


def rp(start, end, verdict=None):
    """Return a W5 piece of rows [start, end), its signature distinct per start."""
    return su.RestPiece(start=start, end=end, sig=tsig(f"p{start}"), verdict=verdict)


def rows_of(pieces):
    return [(p["start"], p["end"]) for p in pieces]


# ---------------------------------------------------------------- the gap rule


@budget(load="pure")
def test_gaps_are_split_at_bound_ends_and_a_hole_marks_its_gap():
    pieces = [rp(100, 200), rp(300, 400), rp(900, 950)]
    gaps = su.remnant_gaps(pieces, [(50, 100), (800, 900)], [(500, 600)])
    assert [(rows_of(g["pieces"]), g["hole"]) for g in gaps] == [
        ([(100, 200), (300, 400)], True),
        ([(900, 950)], False),
    ], "a bound's end opens a gap; a card hole's kept rows mark the gap they lie in"


@budget(load="pure")
def test_a_gap_holding_a_card_hole_is_protected():
    gaps = su.remnant_gaps([rp(100, 200), rp(900, 950)], [(800, 900)], [(300, 400)])
    assert rows_of(su.rest_members(gaps, lambda _sig: False)) == [(900, 950)], "the hole's gap gives no member"


@budget(load="pure")
def test_a_judged_keep_beside_an_unjudged_piece_protects_its_gap_and_an_all_judged_gap_does_not():
    kept = tsig("p100")
    mixed = su.remnant_gaps([rp(100, 200), rp(300, 400), rp(900, 950)], [(800, 900)], [])
    assert rows_of(su.rest_members(mixed, lambda sig: sig == kept)) == [(900, 950)], "judged beside not: protected"
    alone = su.remnant_gaps([rp(100, 200)], [], [])
    assert rows_of(su.rest_members(alone, lambda sig: sig == kept)) == [(100, 200)], "every piece judged: a member"


@budget(load="pure")
def test_pieces_of_2_gaps_are_1_bundle_and_a_decided_piece_stays_a_member():
    bands = [(1000, 1300, 1), (1500, 1700, 2)]
    sc, decide_unit = tail_chapter(bands, [piece(1300, 1500), piece(1700, 1900)])
    junk_sig = sc["measured"]["1300:1500"]["sig"]

    def with_junk(kind, sig):
        if kind == "piece" and sig == junk_sig:
            return db_row(id=8, verdict="junk", key=sig, chain=None, decided_at=AT)
        return decide_unit(kind, sig)

    got = su.decide(sc, with_junk)
    assert [rows_of(g["pieces"]) for g in got["rest"]] == [[(1300, 1500)], [(1700, 1900)]], "1 gap per band end"
    members = su.rest_members(got["rest"], lambda _sig: False)
    assert [(m["start"], m["verdict"]) for m in members] == [(1300, "junk"), (1700, None)], (
        "both gaps' pieces are the chapter's 1 bundle; the piece a junk decision drops is still a member"
    )


@budget(load="pure")
def test_a_chapter_with_no_credit_start_has_no_gap():
    got = su.decide(*tail_chapter([(0, 100, 1, "head")], [piece(100, 200)]))
    assert got["rest"] == [], "no tail pass: no W5 piece"


# ---------------------------------------------------------------- judged keeps and the mark


def decision_row(i, verdict, decider, *, bundle=False, kind="piece"):
    return db_row(
        id=i,
        series_key="S",
        kind=kind,
        key=key(FAR[0]),
        verdict=verdict,
        decider=decider,
        decided_at=AT,
        bundle=bundle,
        superseded_by=None,
        retired_by=None,
    )


def judged(*rows):
    decisions = store.Decisions.of_rows(store.index_of(rows), "S", [])
    return decisions, decisions.judged_keep(images.sig_from_str(key(FAR[0])))


@budget(load="pure")
def test_judged_keep_sees_a_keep_a_pipeline_junk_also_matches_and_never_a_marked_keep():
    decisions, seen = judged(decision_row(1, "keep", "import"), decision_row(2, "junk", "claude"))
    assert present_verdict(decisions) == "keep", "the series' imported keep decides the piece (A26: keeps first)"
    assert seen, "and it is a judgement"
    _, marked = judged(decision_row(1, "keep", "import", bundle=True))
    assert not marked, "a keep given as part of a bundle protects no gap"
    _, other = judged(decision_row(1, "keep", "import", kind="block"))
    assert not other, "only piece keeps judge"


def present_verdict(decisions):
    row = decisions.decide("piece", images.sig_from_str(key(FAR[0])))
    assert row is not None, "a decision decides the piece"
    return row["verdict"]


# ---------------------------------------------------------------- the record and its reader


@budget(load="pure")
@pytest.mark.parametrize(
    "text",
    ['{"ids": [1], "size": 1}', '[{"ids": [1]}]', '[{"ids": [1, "2"], "size": 2}]', '[{"ids": [1, 2], "size": 1}]'],
    ids=["no-list", "no-size", "a-text-id", "size-under-ids"],
)
def test_a_malformed_bundle_record_is_refused(text):
    with pytest.raises(ValueError, match=r"junk_scan\.bundles"):
        candidates.read_bundles(text)


@budget(load="pure")
def test_the_record_holds_the_members_candidates_and_counts_every_member():
    members = [rp(100, 200, "keep"), rp(300, 400), rp(500, 600)]
    picks: list[candidates.Candidate] = [
        {"kind": "piece", "key": "a", "unit": unit(300, 400), "reason": "r"},
        {"kind": "piece", "key": "b", "unit": unit(500, 600), "reason": "r"},
        {"kind": "piece", "key": "c", "unit": unit(700, 800), "reason": "r"},
    ]
    got = junk_stage.bundle_record(picks, [5, 5, 9], members)
    assert got == [{"ids": [5], "size": 3}], "2 members on 1 candidate, 1 decided, the non-member left out"
    assert junk_stage.bundle_record(picks, [5, 5, 9], []) == [], "no bundle: no record"


def unit(start, end) -> su.PieceUnit:
    return {"kind": "piece", "start": start, "end": end, "where": "tail", "sig": tsig(f"p{start}")}


@budget(load="pure")
def test_the_held_text_names_a_bundles_ids_to_judge_together():
    held = [db_row(id=5, kind="piece", verdict="unsure")]
    text = junk_stage.held_reason(held, 3, [{"ids": [5, 6], "size": 3}])
    assert "rest of card 5 6: judge them together (junk decide 5 6 junk|keep)" in text, text
    assert "junk cut" in text, "or a person cuts the junk among content out"
    assert "rest of card" not in junk_stage.held_reason(held, 3, [{"ids": [5], "size": 1}]), "a bundle of 1: none"


# ---------------------------------------------------------------- the mixed note


@budget(load="pure")
def test_a_mixed_bundle_gives_1_note_replaced_not_repeated():
    mixed = junk_stage.rest_note([rp(100, 200, "junk"), rp(300, 400, "keep")])
    assert mixed == "rest of card mixed: rows 100-200, 300-400"
    assert junk_stage.rest_note([rp(100, 200, "junk"), rp(300, 400, "junk")]) is None, "all alike: no note"
    assert junk_stage.rest_note([rp(100, 200, "junk"), rp(300, 400)]) is None, "an undecided member: no note"
    notes = junk_stage.with_rest_note(["kept notice"], mixed)
    again = junk_stage.with_rest_note(notes, "rest of card mixed: rows 100-200, 500-600")
    assert again == ["kept notice", "rest of card mixed: rows 100-200, 500-600"], "replaced, not repeated"
    assert junk_stage.with_rest_note(again, None) == ["kept notice"], "no longer mixed: removed"


# ---------------------------------------------------------------- the listing


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield cfg, conn


def bundled_chapter(s: Series, identity, ids, size):
    """Add a chapter of s's series waiting on ids, recorded as 1 bundle of `size` members; return its id."""
    sid = one(s.conn, "SELECT series_id FROM chapter WHERE id = %s", (s.chapter,))
    cid = add_chapter(s.conn, sid, identity, "junk", number=int(identity[1:]))
    with db.transaction(s.conn):
        s.conn.execute("UPDATE chapter SET status = 'waiting', waiting_on = 'junk_decision' WHERE id = %s", (cid,))
        s.conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates, bundles)
            VALUES (%s, %s, 'd', '{}', %s, %s, %s)""",
            (cid, sid, AT, json.dumps(ids), json.dumps([{"ids": ids, "size": size}])),
        )
    return cid


@budget(load="db")
def test_identical_bundles_are_1_and_a_shared_candidate_carries_both_labels(site, tmp_path, capsys, monkeypatch):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    a, b, c = (s.candidate(f, kind="piece") for f in FAR[:3])
    bundled_chapter(s, "c2", [a, b], 2)
    bundled_chapter(s, "c3", [b, a], 3)  # the same set: the same bundle
    bundled_chapter(s, "c4", [b, c], 2)
    bundled_chapter(s, "c5", [c], 1)  # 1 member: no bundle
    assert candidates.bundles(conn) == [[a, b], [b, c]], "identical id sets once; a bundle of 1 member is none"
    path = str(config_file(tmp_path, cfg))
    assert cli.main(["--config", path, "junk", "list", "--all"]) == 0
    lines = {int(line.split("\t")[0]): line for line in capsys.readouterr().out.strip().splitlines()}
    assert "\tbundle 1\t" in lines[a], lines[a]
    assert "bundle 2" not in lines[a], lines[a]
    assert "\tbundle 1\tbundle 2\t" in lines[b], "the shared candidate carries both labels"
    assert "\tbundle 2\t" in lines[c], lines[c]
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", path, "junk", "list"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert [line.split("\t")[:2] for line in out[-2:]] == [["bundle 1", f"ids {a},{b}"], ["bundle 2", f"ids {b},{c}"]]
    assert all(line.endswith(".png") for line in out[-2:]), "inside a review each bundle line names its sheets"


@budget(load="db")
def test_a_bundle_whose_member_is_unsure_is_shown_in_part(site, tmp_path, capsys, monkeypatch):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    a, b, c = (s.candidate(f, kind="piece") for f in FAR[:3])
    bundled_chapter(s, "c2", [a, b, c], 3)
    review.decide(conn, [b], "unsure", None, AT)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", str(config_file(tmp_path, cfg)), "junk", "list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [int(line.split("\t")[0]) for line in lines[:2]] == [a, c], "the unsure member is not listed"
    assert lines[2].split("\t")[:2] == ["bundle 1", f"ids {a},{c}"], "the bundle is shown with its listed members"


@budget(load="db")
def test_a_bundle_and_a_near_group_are_labelled_apart(site, tmp_path, capsys, monkeypatch):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    a, b = s.candidate(FAR[0], kind="piece"), s.candidate(FAR[0] ^ 1, kind="piece")
    bundled_chapter(s, "c2", [a, b], 2)
    monkeypatch.setenv(review.REVIEW_ID_VARIABLE, "r1")
    assert cli.main(["--config", str(config_file(tmp_path, cfg)), "junk", "list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert all("\tgroup 1\tbundle 1\t" in line for line in lines[:2]), "near and bundled: both labels"
    assert [line.split("\t")[0] for line in lines[2:]] == ["group 1", "bundle 1"], "1 line each, with sheets"


@budget(load="db")
def test_the_batch_cut_keeps_a_listed_bundle_whole(site):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    ids = [s.candidate(FAR[i % 5] ^ (i << 20), kind="piece") for i in range(6)]
    bundled_chapter(s, "c2", [ids[1], ids[4]], 2)
    assert listed(with_batch(cfg, 3), conn) == [ids[0], ids[1], ids[4]], "a member brings its bundle's other ids"
    assert listed(with_batch(cfg, 2), conn, "r2") == [ids[0]], "a bundle that does not fit the room left waits"


@budget(load="db")
def test_a_bundle_alone_over_the_batch_is_listed_whole_and_the_listing_stops(site):
    cfg, conn = site
    s, t = Series(cfg, conn, "A"), Series(cfg, conn, "B")
    ids = [s.candidate(FAR[i % 5] ^ (i << 20), kind="piece") for i in range(4)]
    t.candidate(FAR[0] ^ (9 << 20))
    bundled_chapter(s, "c2", ids[:3], 3)
    assert listed(with_batch(cfg, 2), conn) == ids[:3], "nothing listed yet: the whole bundle, then the listing stops"


def listed(cfg, conn, review_id="r1"):
    return [r["id"] for r in review.listing(conn, cfg, review_id)]


# ---------------------------------------------------------------- the mark on every writer


def marks(conn):
    return {r["source"]: r["bundle"] for r in conn.execute("SELECT source, bundle FROM junk_decision")}


@budget(load="db")
def test_junk_decide_and_keep_rest_mark_a_many_piece_bundles_members_only(site):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    a, b, c, d = (s.candidate(f, kind="piece") for f in FAR[:4])
    bundled_chapter(s, "c2", [a, b], 3)
    bundled_chapter(s, "c3", [d], 1)
    review.decide(conn, [a], "junk", None, AT)
    assert marks(conn) == {f"candidate {a}": True}, "a member of a bundle of more than 1 member: marked"
    listed(cfg, conn)
    review.decide_junk_keeping_rest(conn, [c], "r1", AT)
    want = {f"candidate {a}": True, f"candidate {c}": False, f"candidate {b}": True, f"candidate {d}": False}
    assert marks(conn) == want, "--keep-rest marks the kept member too; a bundle of 1 marks nothing"


@budget(load="db")
def test_redecide_carries_the_mark_to_the_decision_in_its_place(site):
    cfg, conn = site
    s = Series(cfg, conn, "A")
    a = s.candidate(FAR[0], kind="piece")
    bundled_chapter(s, "c2", [a], 2)
    review.decide(conn, [a], "keep", None, AT)
    redecide.redecide(conn, [one(conn, "SELECT id FROM junk_decision")], "junk", AT)
    assert one(conn, "SELECT bundle FROM junk_decision WHERE retired_by IS NULL"), "the junk in its place is marked"


@budget(load="db")
def test_an_import_marks_a_bundle_review_ids_row_and_a_later_import_compares_the_mark(site):
    _, conn = site
    rec = record_with_rows()
    rows = decisions_of(rec)["rows"]["junk"]
    rows[0]["origin"], rows[0]["review_id"] = "piece", "rn01ba7e5"
    got = importing.record_rows(rec)
    assert [(r["kind"], r.get("bundle")) for r in got if r["kind"] == "piece"] == [("piece", True)], "rn: marked"
    unmarked: store.Decision = {**got[0], "bundle": False}
    with db.transaction(conn):
        store.upsert(conn, [unmarked])
        counts = store.upsert(conn, [got[0]])
    assert counts["updated"] == 1, "the same import with its mark is no equal row: it supersedes the unmarked one"
    assert one(conn, "SELECT bundle FROM junk_decision WHERE superseded_by IS NULL"), "the mark stands"
    rows[0]["review_id"] = "r1abcdef"
    assert not importing.record_rows(rec)[0].get("bundle"), "an r1 row (a 1-piece rest) is no bundle"


# ---------------------------------------------------------------- the rule on the bulk converter's own pieces (D6 a)

W5_FIX = Path(__file__).parent / "fixtures_junk_w5" / "baby_sister.json"


def old_chapter(ch):
    """Return (W5 pieces, bounds) of a chapter from the bulk converter's cached tail pieces and its plan's ranges.

    Its pieces: no gap, no W1 blank (strip_units.inked), no W3 wedge (a "wedge" range on its rows), not wholly inside
    a late cut (a "cut" range over its pieces); its bounds: the steps 1-3 ranges (match, card, cut; no late cut).
    """
    spans = [(p["start"], p["end"]) for p in ch["pieces"]]
    late = [
        (r["start"], r["end"])
        for r in ch["removed"]
        if r["why"] == "cut" and any(r["start"] < e and s < r["end"] for s, e in spans)
    ]
    bounds = [(r["start"], r["end"]) for r in ch["removed"] if r["why"] in ("match", "card", "cut")]
    wedges = {(r["start"], r["end"]) for r in ch["removed"] if r["why"] == "wedge"}
    pieces = [
        rp_of(p)
        for p in ch["pieces"]
        if not p["gap"]
        and not p["blank"]
        and su.inked(p) is not None
        and (p["start"], p["end"]) not in wedges
        and not any(a <= p["start"] and p["end"] <= b for a, b in late)
    ]
    return pieces, [b for b in bounds if b not in late]


def rp_of(p):
    return su.RestPiece(start=p["start"], end=p["end"], sig=p["sig"], verdict=None)


@budget(load="fs")
def test_the_rule_on_the_bulk_converters_cached_pieces_gives_its_members():
    """The rule on My Beloved Baby Sister's run 20261114-035349-d077 gives that plan's members.

    5 chapters' cached tail pieces and plan ranges (no card holes), and the series' and global piece keeps active at
    the plan's checked_at that match them (an "rn" one marked). Chapters 1, 3 and 59 give the plan's members; 54 and
    79 are protected: none.
    """
    fx = json.loads(W5_FIX.read_bytes())
    rows = [
        db_row(
            id=k["id"],
            series_key=k["series"] or store.GLOBAL,
            kind="piece",
            key=k["key"],
            verdict="keep",
            decider="import",
            decided_at=AT,
            bundle=(k["review_id"] or "").startswith(importing.BUNDLE_PREFIX),
        )
        for k in fx["keeps"]
    ]
    decisions = store.Decisions.of_rows(store.index_of(rows), fx["series"], [])
    got, want = {}, {}
    for ch in fx["chapters"]:
        pieces, bounds = old_chapter(ch)
        gaps = su.remnant_gaps(pieces, bounds, [])
        members = su.rest_members(gaps, lambda sig: decisions.judged_keep(images.sig_from_str(sig)))
        got[ch["chapter"]] = rows_of(members)
        want[ch["chapter"]] = [tuple(m) for m in ch["members"]]
    assert got == want, "the pipeline's W5 rule gives the bulk converter's members per chapter"
    assert {c: len(m) for c, m in want.items()} == {
        "Chapter 1.cbz": 1,
        "Chapter 3.cbz": 6,
        "Chapter 54.cbz": 0,
        "Chapter 59.cbz": 6,
        "Chapter 79.cbz": 0,
    }, "members and protected gaps both"


@budget(load="pure")
def test_judged_keep_judges_no_unsignable_piece():
    """A keep within reach of a flat image's tile (0) judges a signable piece near it, never a flat one."""
    near_flat = f"{0x7:016x}|1.0"  # 3 bits from the flat tile
    row = db_row(
        id=1, series_key="S", kind="piece", key=near_flat, verdict="keep", decider="import", decided_at=AT, bundle=False
    )
    decisions = store.Decisions.of_rows(store.index_of([row]), "S", [])
    assert decisions.judged_keep(((0xF,), 1.0)), "a signable piece within reach is judged"
    assert not decisions.judged_keep(((0,), 1.0)), "a flat piece is judged by none, as decide decides it by none"


BAD_BUNDLE = {
    "series_key": "S",
    "kind": "piece",
    "key": key(FAR[0]),
    "verdict": "keep",
    "decider": "human",
    "decided_at": AT,
    "source": None,
    "bundle": 1,
}


@budget(load="db")
@pytest.mark.parametrize("row", [BAD_BUNDLE], ids=["bundle-1"])
def test_a_decision_whose_bundle_is_no_bool_is_refused(site, row):
    _, conn = site
    with pytest.raises(ValueError, match="bundle 1: not a bool"), db.transaction(conn):
        store.upsert(conn, [row])
    assert one(conn, "SELECT count(*) FROM junk_decision") == 0, "nothing is written"
