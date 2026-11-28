"""Junk decision store and import (SPEC "Junk decisions": decisions, import)."""

import base64
import json
import math
import shutil

import numpy as np
import psycopg
import pytest

from manga_pipeline import config, db, images, names
from manga_pipeline.convert import record_reader
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, frames, importing, marks, page_units, store, strip_units

from tests.builders import db_row, one, present, site_cfg
from tests.timing import budget
from tests.world import bulk_record

T = 0x0123456789ABCDEF


def sig(tiles=(T,), aspect=1.0):
    return images.sig_to_str((tuple(tiles), aspect))


def flip(t, bits):
    """Return t with its lowest `bits` bits inverted (Hamming distance `bits`)."""
    return t ^ ((1 << bits) - 1)


def decision(  # noqa: PLR0913  # 1 junk-decision row; callers vary all 7 columns
    series_key, kind, key, verdict, decider="import", *, at="2026-11-01T00:00:00+00:00", source=None
) -> store.Decision:
    return {
        "series_key": series_key,
        "kind": kind,
        "key": key,
        "verdict": verdict,
        "decider": decider,
        "decided_at": at,
        "source": source,
    }


@pytest.fixture
def cfg(tmp_path):
    return site_cfg(tmp_path)


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg)
    yield c
    c.close()


def put(conn, *rows):
    with db.transaction(conn):
        return store.upsert(conn, rows)


# ---------------------------------------------------------------- lookup


@budget(load="db")
def test_lookup_order_is_series_keep_series_junk_global_keep_global_junk(conn):
    put(
        conn,
        decision("", "block", sig(), "junk"),
        decision("", "block", sig((flip(T, 1),)), "keep"),
        decision("S", "block", sig((flip(T, 2),)), "junk"),
        decision("S", "block", sig((flip(T, 3),)), "keep"),
    )
    got = present(store.Decisions(conn, "S").decide("block", ((T,), 1.0)))
    assert (got["series_key"], got["verdict"]) == ("S", "keep"), "the series' keep decides first"
    other = present(store.Decisions(conn, "Other").decide("block", ((T,), 1.0)))
    assert (other["series_key"], other["verdict"]) == ("", "keep"), "for another series, a global keep beats junk"


@budget(load="db")
def test_newest_decision_wins_within_its_rank(conn):
    put(
        conn,
        decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00", source="old"),
        decision("S", "block", sig((flip(T, 1),)), "junk", at="2026-11-02T00:00:00+00:00", source="new"),
    )
    assert present(store.Decisions(conn, "S").decide("block", ((T,), 1.0)))["source"] == "new", "newest first"


@budget(load="db")
@pytest.mark.parametrize("decider", ["claude", "import"])
def test_a_newer_junk_of_one_key_does_not_beat_an_older_keep_of_another(conn, decider):
    """Unit x is within the match distance of both b and a (b and a are not of each other).

    Keep b, then junk a: x stays kept (matching is not transitive; keeps first, the bulk converter's rule).
    """
    bits = (1 << images.PHASH_THRESHOLD) - 1
    b, x, a = T, T ^ bits, T ^ bits ^ (bits << images.PHASH_THRESHOLD)
    put(
        conn,
        decision("S", "block", sig((b,)), "keep", decider=decider, at="2026-11-01T00:00:00+00:00", source="keep b"),
        decision("S", "block", sig((a,)), "junk", decider=decider, at="2026-11-02T00:00:00+00:00", source="junk a"),
    )
    assert not images.matches(((b,), 1.0), ((a,), 1.0)), "b and a are apart"
    assert present(store.Decisions(conn, "S").decide("block", ((x,), 1.0)))["source"] == "keep b", "x stays kept"


@budget(load="db")
def test_a_unit_is_decided_only_by_the_kinds_that_act_on_it(conn):
    put(conn, decision("S", "block", sig(), "junk"))
    d = store.Decisions(conn, "S")
    assert d.decide("probe", ((T,), 1.0)) is not None, "block decisions decide probes"
    assert d.decide("piece", ((T,), 1.0)) is None, "block decisions never decide a tail piece"
    assert d.decide("block", ((flip(T, 7),), 1.0)) is None, "Hamming 7 is no match"
    assert d.decide("block", ((T,), 1.04)) is None, "an aspect 4 % off is no match"


@budget(load="db")
def test_frames_match_by_their_own_rule(conn):
    vec = np.full(frames.CELLS * 3, 100, np.uint8)
    put(conn, decision("S", "frame", frames.key_text(frames.Frame(vec, 1.5)), "junk"))
    d = store.Decisions(conn, "S")
    assert d.decide("frame", frames.Frame(vec + 10, 1.6)) is not None, "RMS 10, aspect 6 % off: a match"
    assert d.decide("frame", frames.Frame(vec + 20, 1.5)) is None, "RMS 20 is no match"


@budget(load="db")
@pytest.mark.parametrize(
    ("scope", "unit_kind", "cap"),
    [
        ("S", "block", 8),
        ("", "block", 8),
        ("Other", "block", 6),
        ("S", "probe", 4),
        ("", "probe", 4),
        ("Other", "probe", 4),
    ],
    ids=["series", "global", "other-series", "probe-series", "probe-global", "probe-other-series"],
)
def test_near_caps_8_for_the_series_and_global_6_for_another_series_4_for_a_probe(conn, scope, unit_kind, cap):
    """A junk decision is near a unit of series S within its cap, aspect within 10 %; 1 bit more is not near.

    Both units are 5 % off the decision's aspect: past the live rule's 3 %, so no decision of the series matches them
    and suppresses near; only the cap decides.
    """
    put(conn, decision(scope, "block", sig(), "junk"))
    hit = store.Decisions(conn, "S").near(((flip(T, cap),), 1.05), unit_kind)
    assert hit is not None, f"a {scope or 'global'} junk decision {cap} bits from a {unit_kind} is near"
    assert hit[1] == cap, f"its distance is {cap}"
    far = store.Decisions(conn, "S").near(((flip(T, cap + 1),), 1.05), unit_kind)
    assert far is None, f"{cap + 1} bits from a {unit_kind} is not near a {scope or 'global'} decision"


@budget(load="db")
def test_near_finds_a_decision_in_the_next_aspect_bin(conn):
    """A decision at aspect 0.99 and a unit at 1.05 (6 % apart) lie in adjacent bins of the index: still near."""
    bins = {math.floor(math.log(a) / store.ASPECT_BIN) for a in (0.99, 1.05)}
    assert len(bins) == len((0.99, 1.05)), "the 2 aspects are in different bins"
    put(conn, decision("S", "block", sig(aspect=0.99), "junk"))
    hit = store.Decisions(conn, "S").near(((flip(T, 2),), 1.05), "block")
    assert hit is not None, "a decision in the next aspect bin is near"


@budget()
def test_the_pipelines_decision_comes_before_an_imported_one_of_its_tier():
    """2 series junk decisions match the unit: the pipeline's, older and chained, decides before a newer import."""
    rows = [
        db_row(
            id=1,
            series_key="S",
            kind="block",
            key=sig(),
            verdict="junk",
            decider="claude",
            decided_at="2026-11-01T00:00:00+00:00",
            chain="{}",
        ),
        db_row(
            id=2,
            series_key="S",
            kind="block",
            key=sig((flip(T, 1),)),
            verdict="junk",
            decider="import",
            decided_at="2026-12-01T00:00:00+00:00",
            chain=None,
        ),
    ]
    got = present(store.Decisions.of_rows(store.index_of(rows), "S", []).decide("block", ((T,), 1.0)))
    assert (got["id"], got["chain"]) == (1, "{}"), "the pipeline's decision decides, its chain with it"


@budget(load="db")
def test_a_series_band_junk_still_suppresses_near_for_a_block_and_a_keep_does_not(conn):
    """Near is for the undecided: a junk decision of the series that matches at the live rule suppresses it.

    A band junk decides no block, yet it suppresses near for one (today's rule, kept); a keep never suppresses.
    """
    put(conn, decision("Other", "block", sig(), "junk"), decision("S", "band", sig((flip(T, 2),)), "keep"))
    unit = ((flip(T, 1),), 1.0)
    assert store.Decisions(conn, "S").near(unit, "block") is not None, "a matching keep of the series does not suppress"
    put(conn, decision("S", "band", sig((flip(T, 3),)), "junk"))
    decisions = store.Decisions(conn, "S")
    assert decisions.decide("block", unit) is None, "band decisions decide no block"
    assert decisions.near(unit, "block") is None, "a matching band junk of the series suppresses near"


# ---------------------------------------------------------------- upsert


@budget(load="db")
def test_a_pipeline_decision_is_never_overwritten_by_an_import(conn):
    put(conn, decision("S", "block", sig(), "keep", decider="claude", at="2026-11-01T00:00:00+00:00"))
    counts = put(conn, decision("S", "block", sig(), "junk", at="2026-12-01T00:00:00+00:00"))
    row = conn.execute("SELECT verdict, decider FROM junk_decision").fetchone()
    assert tuple(row) == ("keep", "claude"), "a newer import leaves claude's row"
    assert counts["unchanged"] == 1, "a newer import leaves claude's row"


@budget(load="db")
def test_an_older_import_never_undoes_a_newer_one(conn):
    put(conn, decision("S", "block", sig(), "keep", at="2026-11-05T00:00:00+00:00"))
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00"))
    assert (
        one(conn, "SELECT verdict FROM junk_decision WHERE superseded_by IS NULL AND retired_by IS NULL") == "keep"
    ), "the newer import stays"
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-06T00:00:00+00:00"))
    assert (
        one(conn, "SELECT verdict FROM junk_decision WHERE superseded_by IS NULL AND retired_by IS NULL") == "junk"
    ), "a newer import replaces it"


def logo(grey=200, w=40):
    """Return a logo template: a solid block of 1 colour (marks.Template), w x 20."""
    return marks.Template(np.full((20, w, 3), grey, np.uint8), np.ones((20, w), bool))


# ---------------------------------------------------------------- decision rows of a series record (baseline)


def record_with_rows(schema=4) -> record_reader.Record:
    rec = bulk_record("S", 7, [])
    rec["schema"] = schema
    vec = base64.b64encode(np.full(frames.CELLS * 3, 9, np.uint8).tobytes()).decode()
    decisions_of(rec)["rows"] = {
        "junk": [
            {
                "row": 1,
                "series": "S",
                "key": [f"{T:016x}", 1.0],
                "decision": "approve",
                "review_id": "j1",
                "note": None,
                "added": "2026-11-07 10:00:00",
                "origin": "block",
                "chain": None,
                "credit": False,
            },
            {
                "row": 2,
                "series": "S",
                "key": [f"{flip(T, 30):016x}", 1.0],
                "decision": "approve",
                "review_id": None,
                "note": None,
                "added": "2026-11-07 10:00:00",
                "origin": "cut",
                "chain": "{}",
                "credit": False,
            },
        ],
        "frames": [
            {
                "row": 3,
                "series": None,
                "key": [{"b64": vec}, 1.42],
                "decision": "approve",
                "review_id": "f1",
                "note": None,
                "added": "2026-11-07 10:00:01",
            }
        ],
        "marks": [
            {
                "row": 4,
                "series": None,
                "key": [{"tpl": "t1"}, 40, 20],
                "decision": "approve",
                "review_id": "m1",
                "note": None,
                "added": "2026-11-07 10:00:02",
            }
        ],
    }
    decisions_of(rec)["templates"] = {"t1": base64.b64encode(logo().to_png()).decode()}
    return json.loads(json.dumps(rec))  # as its record file reads


def decisions_of(rec) -> dict:
    """Return a record's series.decisions block, a JSON object."""
    block = rec["series"]["decisions"]
    assert isinstance(block, dict), "series.decisions is an object"
    return block


@budget(load="pure")
def test_of_2_record_rows_on_1_key_the_keep_wins_then_the_newest():
    junk = decision("S", "block", sig(), "junk", at="2026-11-09T00:00:00+00:00")
    keep = decision("S", "block", sig(), "keep", at="2026-11-01T00:00:00+00:00")
    newer = decision("S", "block", sig(), "keep", at="2026-11-05T00:00:00+00:00")
    assert importing.winners([junk, keep]) == [keep], "the bulk converter obeyed a keep before a junk, however new"
    assert importing.winners([keep, newer]) == [newer], "then the newest"


@budget(load="db")
def test_a_schema_4_records_decision_rows_land_as_import_rows(conn):
    put(conn, *importing.winners(importing.record_rows(record_with_rows())))
    rows = {
        (r["series_key"], r["kind"], r["verdict"], r["decider"]) for r in conn.execute("SELECT * FROM junk_decision")
    }
    want = {("S", "block", "junk", "import"), ("", "frame", "junk", "import"), ("", "logo", "junk", "import")}
    assert rows == want, "cut rows stay out; a marks row lands as a logo decision"
    assert importing.record_rows(record_with_rows(schema=3)) == [], "a schema-3 record gives no rows"


@budget(load="pure")
def test_a_cut_row_is_read_for_its_origin_alone():
    """A cut row is no decision on a key: one holding only its origin leaves the record's decisions importable."""
    rec = record_with_rows()
    decisions_of(rec)["rows"]["junk"][1] = {"origin": "cut"}  # no row, series, decision, added or key
    assert importing.record_rows(rec) == importing.record_rows(record_with_rows()), "the other rows import as before"


@budget(load="db")
def test_record_rows_never_undo_a_newer_imported_decision(conn):
    put(conn, decision("S", "block", sig(), "keep", at="2026-11-09T00:00:00+00:00"))
    put(conn, *importing.winners(importing.record_rows(record_with_rows())))
    row = conn.execute("SELECT verdict FROM junk_decision WHERE kind = 'block'").fetchone()
    assert row[0] == "keep", "an older record row never undoes a newer imported decision"


def _no_key(rows):
    del rows[0]["key"]


def _not_a_row(rows):
    rows[0] = "a row"


def _number_tiles(rows):
    rows[0]["key"][0] = 7


@budget(load="pure")
@pytest.mark.parametrize("damage", [_no_key, _not_a_row, _number_tiles])
def test_a_malformed_decision_row_is_a_record_that_does_not_parse(damage):
    rec = record_with_rows()
    damage(decisions_of(rec)["rows"]["junk"])
    with pytest.raises(record_reader.RecordError, match="malformed"):
        importing.record_rows(rec)


def _unknown_template(decisions):
    decisions["rows"]["marks"][0]["key"][0] = {"tpl": "nope"}


def _not_a_png(decisions):
    decisions["templates"]["t1"] = base64.b64encode(b"not a png").decode()


def _wrong_size(decisions):
    decisions["rows"]["marks"][0]["key"][1] = 41


@budget(load="pure")
@pytest.mark.parametrize("damage", [_unknown_template, _not_a_png, _wrong_size])
def test_a_malformed_marks_row_is_a_record_that_does_not_parse(damage):
    rec = record_with_rows()
    damage(decisions_of(rec))
    with pytest.raises(record_reader.RecordError, match="malformed"):
        importing.record_rows(rec)


@budget(load="db")
def test_a_series_imported_keep_beats_its_pipeline_junk_on_a_unit_both_match(conn):
    """A26: keeps before junk across the pipeline's and the imported decisions of a series."""
    put(conn, decision("S", "block", sig(), "junk", decider="claude", at="2026-12-01T00:00:00+00:00"))
    put(conn, decision("S", "block", sig((flip(T, 2),)), "keep", at="2026-11-01T00:00:00+00:00"))
    got = present(store.Decisions(conn, "S").decide("block", ((flip(T, 1),), 1.0)))
    assert (got["decider"], got["verdict"]) == ("import", "keep"), "the series' older imported keep beats Claude's junk"
    only_junk = T ^ (0b111111 << 2)  # 6 bits from the junk key, 8 from the keep's
    alone = present(store.Decisions(conn, "S").decide("block", ((only_junk,), 1.0)))
    assert (alone["decider"], alone["verdict"]) == ("claude", "junk"), "a unit only Claude's junk matches is junk"


@budget()
def test_sig_text_round_trip():
    s = ((0x1234, 0xFFFFFFFFFFFFFFFF), 2.0123456789)
    assert images.sig_from_str(images.sig_to_str(s)) == s, "a signature survives its text form exactly"


def chapter(number):
    return names.identity(names.parse(f"Grp_Chapter {number}"))


@budget(load="db")
def test_a_first_chapter_unit_with_no_other_reason_is_no_candidate(conn):
    block: strip_units.BlockUnit = {
        "kind": "block",
        "sig": sig((flip(T, 60),)),
        "start": 0,
        "end": 300,
        "where": "head",
        "probes": [],
    }
    words: config.JunkConfig = {**word_lists(), "keywords": ["discord"]}
    assert candidates.select(conn, "S", [block], words) == [], "a first chapter's plain block: no reason, not offered"
    (pick,) = candidates.select(conn, "S", [{**block, "text": "join our discord"}], words)
    assert pick["reason"].startswith("text 'discord'"), "a first chapter's block is offered by its other reasons"


@budget(load="db")
def test_a_block_near_a_junk_decision_cites_it(conn):
    put(conn, decision("S", "block", sig(), "junk"))
    decided = one(conn, "SELECT id FROM junk_decision")
    block: strip_units.BlockUnit = {
        "kind": "block",
        "sig": sig((flip(T, 7),)),
        "start": 0,
        "end": 300,
        "where": "head",
        "probes": [],
    }
    (pick,) = candidates.select(conn, "S", [block], word_lists())
    assert pick["reason"].startswith(f"near decision {decided} "), pick
    assert pick.get("cited") == [decided], "the decision its reason names, by id"
    # near, not matched: its aspect is past the live rule's 3 %
    probe: strip_units.Piece = {"start": 0, "end": 60, "sig": sig((flip(T, 2),), 1.06)}
    far: strip_units.BlockUnit = {**block, "sig": sig((~T & (2**64 - 1),)), "probes": [probe]}
    (pick,) = candidates.select(conn, "S", [far], word_lists())
    assert (pick["kind"], pick.get("cited")) == ("probe", [decided]), "a near probe offered for its block cites it too"


@budget(load="db")
def test_the_rest_of_a_block_whose_edge_piece_is_junk_is_offered(conn):
    rest: strip_units.BlockUnit = {
        "kind": "block",
        "sig": sig((flip(T, 60),)),
        "start": 120,
        "end": 300,
        "where": "tail",
        "probes": [],
    }
    words = word_lists()
    assert candidates.select(conn, "S", [rest], words) == [], "a plain block with no reason: not offered"
    (pick,) = candidates.select(conn, "S", [{**rest, "rest": True}], words)
    assert pick["reason"] == "the rest of a block whose edge piece is junk", pick


@budget(load="db")
def test_a_band_on_page_2_recurs_with_page_1_bands_of_other_chapters(conn):
    """Akuyaku ch12/13 carry a band on page 2, ch14/15 on page 1.

    Sightings name no page, so 1 band candidate for its recurrence in 4 chapters.
    """
    band = sig((flip(T, 33),), 3.2)
    with db.transaction(conn):
        candidates.write_seen(
            conn,
            [
                ("S", "band", band, chapter(n), f"2026-11-0{n - 11}T00:00:00+00:00", "pipeline")
                for n in (12, 13, 14, 15)
            ],
        )
    unit: page_units.BandUnit = {
        "kind": "band",
        "key": band,
        "page": 1,
        "where": "top",
        "box": [0, 0, 1, 1],
        "reasons": [],
    }
    words = word_lists()
    (pick,) = candidates.select(conn, "S", [unit], words)
    assert (pick["kind"], pick["reason"]) == ("band", "seen in 4 chapters"), pick


@budget(load="db")
def test_a_candidate_id_and_folder_follow_its_key_not_the_order_it_is_offered_in(conn):
    a, other = sig((flip(T, 1),)), sig((~T & (2**64 - 1),))
    pick: candidates.Candidate = {"kind": "block", "key": a, "unit": unit_of(a), "reason": "r", "folder": "f-1"}
    with db.transaction(conn):
        (first,) = candidates.join(conn, "S", [pick], "2026-11-01T00:00:00+00:00")
        both = candidates.join(
            conn,
            "S",
            [{**pick, "key": other, "folder": "f-2"}, {**pick, "key": sig((flip(T, 2),)), "folder": "f-3"}],
            "2026-11-02T00:00:00+00:00",
        )
    assert first in both, "a matching key joins its candidate, wherever it is listed"
    assert len(both) == 2, "a matching key joins its candidate, wherever it is listed"
    row = conn.execute("SELECT folder FROM junk_candidate WHERE id = %s", (first,)).fetchone()
    assert row[0] == "f-1", "the row names the folder its images were written to"
    assert candidates.folder_name("S", "block", a, db_row(id=7, claim_count=2)).endswith("-7-2"), (
        "a folder name carries the claim that wrote it"
    )


@budget(3.0, load="db")
def test_step_0021_keeps_decisions_and_candidate_ids_and_allows_logos(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path, built=False)
    before = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, before)
    (before / "0021_logo.sql").unlink()
    monkeypatch.setattr(db, "SCHEMA", before)
    old = db.connect(cfg)
    put(old, decision("S", "block", sig(), "junk"))
    for key in ("a", "b", "c"):
        old.execute(
            "INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)"
            " VALUES ('S', 'page', %s, 'r', 'f', 't')",
            (key,),
        )
    old.execute("DELETE FROM junk_candidate WHERE key = 'c'")
    old.close()
    monkeypatch.undo()
    conn = db.connect(cfg)
    assert [r[0] for r in conn.execute("SELECT kind FROM junk_decision")] == ["block"], "decisions are kept"
    put(conn, decision("S", "logo", logo().key_text(), "junk"))
    conn.execute(
        "INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)"
        " VALUES ('S', 'logo', 'd', 'r', 'f', 't')"
    )
    ids = [r[0] for r in conn.execute("SELECT id FROM junk_candidate ORDER BY id")]
    assert ids == [1, 2, 4], "a candidate id is never reused across the step"
    assert conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'junk_decision_scope'").fetchone(), "index kept"
    conn.close()


# ---------------------------------------------------------------- unsignable keys

FLAT = 0  # the pHash tile of a uniform image (images.uniform_tiles)


@budget(load="db")
def test_a_unit_with_an_unsignable_key_matches_no_decision_and_is_near_none(conn):
    """A junk decision within the match tolerance of a flat image decides a signable unit as near, never a flat one.

    That key would stand for any blank or flat rows.
    """
    put(conn, decision("S", "block", sig((flip(FLAT, 3),)), "junk"))
    decisions = store.Decisions(conn, "S")
    assert decisions.decide("block", ((flip(FLAT, 4),), 1.0)) is not None, "a signable unit within reach is decided"
    assert decisions.decide("block", ((FLAT,), 1.0)) is None, "a flat unit matches nothing"
    other = store.Decisions(conn, "T")
    assert other.near(((flip(FLAT, 4),), 1.0), "block") is not None, "a signable unit of another series is near"
    assert other.near(((FLAT,), 1.0), "block") is None, "a flat unit is near nothing"


@budget(load="db")
def test_a_decision_on_an_unsignable_key_is_refused(conn):
    with pytest.raises(ValueError, match="unsignable"):
        put(conn, decision("S", "band", sig((FLAT, 0x8000000000000000)), "junk", decider="human"))
    assert one(conn, "SELECT count(*) FROM junk_decision") == 0, "nothing is written"


@budget(load="pure")
def test_an_import_leaves_out_a_decision_on_an_unsignable_key_and_counts_it():
    rows = [decision("S", "block", sig((FLAT,)), "junk"), decision("S", "block", sig(), "junk")]
    kept, left_out = importing.signable(rows)
    assert left_out == 1, "the unsignable one is counted"
    assert [r["key"] for r in kept] == [sig()], "only the signable one is kept"


@budget(load="db")
def test_an_unsignable_unit_is_never_offered_nor_a_blocks_unsignable_probe(conn):
    words: config.JunkConfig = {**word_lists(), "keywords": ["scans"]}
    band: page_units.BandUnit = {
        "kind": "band",
        "key": sig((FLAT,), 3.2),
        "page": 3,
        "where": "top",
        "box": [0, 0, 1, 1],
        "reasons": ["band"],
    }
    assert candidates.select(conn, "S", [band], words) == [], "no decision could be written on it"
    flat: strip_units.Piece = {"sig": sig((FLAT,), 0.2), "start": 0, "end": 60, "text": "read at scans"}
    banner: strip_units.Piece = {"sig": sig((flip(T, 9),), 0.2), "start": 60, "end": 120, "text": "more scans"}
    block: strip_units.BlockUnit = {
        "kind": "block",
        "sig": sig((flip(T, 60),)),
        "start": 0,
        "end": 300,
        "where": "head",
        "probes": [flat, banner],
    }
    (pick,) = candidates.select(conn, "S", [block], words)
    assert (pick["kind"], pick["key"]) == ("probe", banner["sig"]), "the unsignable probe is passed over for the next"


@budget(load="db")
def test_a_unit_matching_a_decided_candidate_but_no_decision_gets_its_own_candidate(conn):
    """Matching is not transitive.

    Candidate T is covered by a decision 6 bits away on one side, the unit is 6 bits away on the other (12 from the
    decision). Joining the closed candidate would end the chapter's wait at once and start it again, for ever.
    """
    unit = sig((T ^ (0b111111 << 6),))
    pick: candidates.Candidate = {
        "kind": "block",
        "key": sig(),
        "unit": unit_of(sig()),
        "reason": "r",
        "folder": "f-old",
    }
    with db.transaction(conn):
        (old,) = candidates.join(conn, "S", [pick], "2026-11-01T00:00:00+00:00")
    put(conn, decision("S", "block", sig((flip(T, 6),)), "keep"))
    with db.transaction(conn):
        (new,) = candidates.join(conn, "S", [{**pick, "key": unit, "folder": "f-new"}], "2026-11-02T00:00:00+00:00")
    assert new != old, "the covered candidate is closed; the undecided unit opens its own"


def word_lists() -> config.JunkConfig:
    """Return the config's junk section with empty word lists: no unit has a keyword."""
    return {**config.cached()["junk"], "keywords": [], "weak": [], "paired": [], "suppress": []}


def unit_of(key) -> strip_units.BlockUnit:
    """Return an undecided edge block keyed `key` (a candidate's unit)."""
    return {"kind": "block", "sig": key, "start": 0, "end": 300, "where": "head", "probes": []}


def schema_without(tmp_path, monkeypatch, *steps):
    """db.SCHEMA replaced by a copy without `steps` (monkeypatch.undo() brings the program's back).

    A later step that reads what the tested step adds is left out with it: a real database never holds it without.
    """
    before = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, before)
    for step in steps:
        (before / step).unlink()
    monkeypatch.setattr(db, "SCHEMA", before)


@budget(3.0, load="db")
def test_step_0042_deletes_decisions_and_candidates_on_unsignable_keys(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path, built=False)
    schema_without(tmp_path, monkeypatch, "0042_unsignable_keys.sql")
    old = db.connect(cfg)
    flat, mixed = sig((FLAT, 0x8000000000000000), 2.0), sig((FLAT, T), 2.0)
    for kind, key in (("block", flat), ("band", mixed), ("frame", "f")):
        old.execute(
            "INSERT INTO junk_decision (series_key, kind, key, verdict, decider, decided_at)"
            " VALUES ('S', %s, %s, 'junk', 'human', 't')",
            (kind, key),
        )
        old.execute(
            "INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)"
            " VALUES ('S', %s, %s, 'r', 'f', 't')",
            (kind, key),
        )
    old.close()
    monkeypatch.undo()
    conn = db.connect(cfg)
    for table in ("junk_decision", "junk_candidate"):
        left = sorted(tuple(r) for r in conn.execute(sql.SQL("SELECT kind, key FROM {}").format(sql.Identifier(table))))
        assert left == [("band", mixed), ("frame", "f")], f"{table}: only the signature of uniform tiles alone goes"
    conn.close()
    step = (db.SCHEMA / "0042_unsignable_keys.sql").read_text(encoding="utf-8")
    tiles = images.uniform_tiles()
    assert len(tiles) == 2, "the step names the computed uniform tiles"
    assert all(f"{t:016x}" in step for t in tiles), "the step names the computed uniform tiles"


@budget(3.0, load="db")
def test_step_0040_names_each_series_by_its_first_folder_and_a_later_folder_leaves_it(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path, built=False)
    schema_without(tmp_path, monkeypatch, "0040_series_rules_key.sql", "0075_junk_refill.sql")  # 0075 reads rules_key
    old = db.connect(cfg)
    for name, folder in (("win", r"C:\lib\Solo Leveling"), ("posix", "/lib/Top/Omniscient Reader/"), ("held", None)):
        old.execute("INSERT INTO series (name, origin, folder) VALUES (%s, 'new', %s)", (name, folder))
    old.close()
    monkeypatch.undo()
    conn = db.connect(cfg)

    def keys():
        return dict(tuple(r) for r in conn.execute("SELECT name, rules_key FROM series"))

    assert keys() == {"win": "Solo Leveling", "posix": "Omniscient Reader", "held": None}, "the folder's last part"
    conn.execute("UPDATE series SET folder = '/lib/Renamed' WHERE name = 'posix'")
    conn.execute("UPDATE series SET folder = '/lib/First' WHERE name = 'held'")
    conn.execute("INSERT INTO series (name, origin, folder) VALUES ('fresh', 'new', '/lib/Fresh')")
    want = {"win": "Solo Leveling", "posix": "Omniscient Reader", "held": "First", "fresh": "Fresh"}
    assert keys() == want, "set with the first folder a series gets, never changed by a later one"
    conn.close()


# ---------------------------------------------------------------- history (decisions are never deleted)


def active(conn):
    """Return the active decisions' (series key, verdict, decider), in id order."""
    rows = conn.execute(
        "SELECT series_key, verdict, decider FROM junk_decision WHERE superseded_by IS NULL AND retired_by IS NULL"
        " ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


def retired_by_an_act(conn, decision_id):
    """Retire decision_id by a new act, as junk redecide's retirement does."""
    with db.transaction(conn):
        store.retire(conn, [decision_id], store.new_act(conn, "test", "2026-11-10T00:00:00+00:00"))


@budget(load="db")
def test_a_superseded_or_retired_decision_decides_nothing(conn):
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00"))
    put(conn, decision("S", "block", sig(), "keep", at="2026-11-02T00:00:00+00:00"))
    assert present(store.Decisions(conn, "S").decide("block", store.parse_key("block", sig())))["verdict"] == "keep", (
        "the superseding import decides; the superseded one is history"
    )
    retired_by_an_act(conn, one(conn, "SELECT id FROM junk_decision WHERE superseded_by IS NULL"))
    assert store.Decisions(conn, "S").decide("block", store.parse_key("block", sig())) is None, (
        "a retired row decides nothing"
    )


@budget(load="db")
def test_an_import_replacing_an_import_supersedes_it_and_keeps_it(conn):
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00"))
    counts = put(conn, decision("S", "block", sig(), "keep", at="2026-11-02T00:00:00+00:00"))
    rows = conn.execute("SELECT id, verdict, superseded_by FROM junk_decision ORDER BY id").fetchall()
    assert [(r["verdict"], r["superseded_by"]) for r in rows] == [("junk", rows[1]["id"]), ("keep", None)], (
        "the older import stays as history, superseded by the newer"
    )
    assert counts["updated"] == 1, "a superseded row counts as updated"


@budget(load="db")
def test_an_identical_reimport_is_unchanged(conn):
    row = decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00")
    put(conn, row)
    counts = put(conn, row)
    assert counts["unchanged"] == 1, "the same import again changes nothing"
    assert one(conn, "SELECT count(*) FROM junk_decision") == 1, "no history row for an identical import"


@budget(load="db")
def test_an_import_never_writes_a_key_an_act_retired(conn):
    put(conn, decision("", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00"))
    retired_by_an_act(conn, one(conn, "SELECT id FROM junk_decision"))
    counts = put(conn, decision("", "block", sig(), "junk", at="2026-11-20T00:00:00+00:00"))
    assert counts["unchanged"] == 1, "a baseline's global row does not revive what an act retired"
    assert active(conn) == [], "nothing is active on the key"


@budget(load="db")
def test_a_pipeline_decision_on_a_key_an_undone_act_left_inactive_is_written(conn):
    with db.transaction(conn):
        act = store.new_act(conn, "test", "2026-11-10T00:00:00+00:00")
        (written,) = store.write(conn, [decision("S", "block", sig(), "keep", "human")], act)
        store.retire(conn, [written], store.new_act(conn, "undo", "2026-11-11T00:00:00+00:00", undoes=act))
    counts = put(conn, decision("S", "block", sig(), "junk", "claude", at="2026-11-12T00:00:00+00:00"))
    assert counts["inserted"] == 1, "Claude's decision is written on a key with no active row"
    assert active(conn) == [("S", "junk", "claude")], "Claude's decision is the active one"


@budget(load="db")
def test_near_ignores_inactive_rows(conn):
    put(conn, decision("O", "block", sig((flip(T, 6),)), "junk"))
    assert store.Decisions(conn, "S").near(((T,), 1.0), "block") is not None, "an active junk of another series is near"
    retired_by_an_act(conn, one(conn, "SELECT id FROM junk_decision"))
    assert store.Decisions(conn, "S").near(((T,), 1.0), "block") is None, "a retired one is not"


# ---------------------------------------------------------------- the store index and its version


@budget(load="db")
def test_an_insert_a_retire_and_an_undo_each_change_the_store_version(conn):
    """Every writer moves the version (largest decision id, largest act id), and the next lookup sees the change."""
    unit = store.parse_key("block", sig())
    before = store.version(conn)
    put(conn, decision("S", "block", sig(), "junk"))
    inserted = store.version(conn)
    assert inserted != before, "an insert moves the version"
    assert store.Decisions(conn, "S").decide("block", unit) is not None, "the inserted decision decides"
    written = one(conn, "SELECT id FROM junk_decision")
    retired_by_an_act(conn, written)
    retired = store.version(conn)
    assert retired != inserted, "a retirement moves the version"
    assert store.Decisions(conn, "S").decide("block", unit) is None, "the retired decision decides nothing"
    with db.transaction(conn):
        act = one(conn, "SELECT max(id) FROM junk_act")
        store.restore(conn, [written], store.new_act(conn, "undo", "2026-11-11T00:00:00+00:00", undoes=act))
    assert store.version(conn) != retired, "an undo moves the version"
    assert store.Decisions(conn, "S").decide("block", unit) is not None, "the restored decision decides again"


@budget(load="db")
def test_a_write_in_another_connection_is_seen_by_the_next_decisions_built(cfg, conn):
    unit = store.parse_key("block", sig())
    assert store.Decisions(conn, "S").decide("block", unit) is None, "an empty store decides nothing"
    with db.connect(cfg) as other:
        put(other, decision("S", "block", sig(), "junk"))
    assert store.Decisions(conn, "S").decide("block", unit) is not None, "the other connection's decision is seen"


@budget(load="db")
def test_the_index_of_an_empty_store_decides_nothing_and_has_no_cuts(conn):
    unit = images.sig_from_str(sig())
    for decisions in (store.Decisions(conn, "S"), store.Decisions.of_rows(store.index_of([]), "S", [])):
        assert decisions.decide("block", unit) is None, "an empty store decides nothing"
        assert decisions.cuts() == [], "an empty store has no cuts"
        assert decisions.near(unit, "block") is None, "an empty store is near nothing"


def cut_decision(series_key, tiles, at) -> store.Decision:
    """Return a cut decision of 1 cut segment with tile pHash text `tiles` (its key span), decided at `at`."""
    chain = (
        f'{{"below":[{{"ref":[["{series_key}","c",0]],"segments":[{{"h":0.5,"kind":"cut","tiles":"{tiles}"}}],'
        f'"src":{{"file":"a.cbz","sha256":"{"ab" * 32}"}}}}]}}'
    )
    return {**decision(series_key, "cut", f"{tiles}|0.5", "junk", at=at), "chain": chain}


@budget(load="db")
def test_cuts_are_the_series_active_cut_rows_oldest_first(conn):
    put(
        conn,
        cut_decision("S", f"{T:016x}", "2026-11-03T00:00:00+00:00"),
        cut_decision("S", f"{flip(T, 20):016x}", "2026-11-01T00:00:00+00:00"),
        cut_decision("S", f"{flip(T, 30):016x}", "2026-11-02T00:00:00+00:00"),
        cut_decision("Other", f"{flip(T, 40):016x}", "2026-11-01T00:00:00+00:00"),
    )
    retired_by_an_act(conn, one(conn, "SELECT id FROM junk_decision WHERE key LIKE %s", (f"{flip(T, 30):016x}|%",)))
    got = [r["decided_at"][:10] for r in store.Decisions(conn, "S").cuts()]
    assert got == ["2026-11-01", "2026-11-03"], "the series' active cuts, oldest first; another series' left out"


def decided_in_a_rolled_back_write(conn, unit):
    """Write a junk decision in a transaction, look the unit up inside it, roll back; return that lookup's row."""
    with db.transaction(conn):
        store.upsert(conn, [decision("S", "block", sig(), "junk")])
        found = store.Decisions(conn, "S").decide("block", unit)
        raise psycopg.Rollback
    return found


@budget(load="db")
def test_index_inside_a_callers_transaction_reads_on_its_connection_and_sees_its_writes(conn):
    unit = store.parse_key("block", sig())
    assert decided_in_a_rolled_back_write(conn, unit) is not None, "the caller's own write is seen"
    assert store.Decisions(conn, "S").decide("block", unit) is None, "after the rollback the write is gone"
    put(conn, decision("S", "block", sig((flip(T, 1),)), "keep"))
    assert present(store.Decisions(conn, "S").decide("block", unit))["verdict"] == "keep", (
        "the next committed write is seen, not the rolled-back rows cached before it"
    )


@budget(load="db")
@pytest.mark.parametrize(
    ("kind", "key"),
    [("frame", "zz|1.0"), ("logo", "!!!"), ("block", f"{T:016x}|0.0")],
    ids=["frame", "logo", "aspect-0"],
)
def test_a_malformed_frame_or_logo_key_is_refused_on_write(conn, kind, key):
    with pytest.raises(ValueError, match="decision key"):
        put(conn, decision("S", kind, key, "junk"))
    assert one(conn, "SELECT count(*) FROM junk_decision") == 0, "nothing is written"


@budget(load="db")
def test_a_retirement_after_a_read_in_1_act_is_seen_by_the_next_read_and_after_commit(conn):
    """Within 1 act the retirement moves no id: a read before it must not leave its rows for the reads after."""
    unit = store.parse_key("block", sig())
    put(conn, decision("S", "block", sig(), "junk"))
    written = one(conn, "SELECT id FROM junk_decision")
    with db.transaction(conn):
        act = store.new_act(conn, "test", "2026-11-10T00:00:00+00:00")
        assert store.Decisions(conn, "S").decide("block", unit) is not None, "read before the retirement: decided"
        store.retire(conn, [written], act)
        assert store.Decisions(conn, "S").decide("block", unit) is None, "read after it, in the act: retired"
    assert store.Decisions(conn, "S").decide("block", unit) is None, "read after the commit: retired"


# ---------------------------------------------------------------- agreement (plan phase-10-6, step 2)

FAR_TILES = (0x0F0F0F0F0F0F0F0F, 0xF0F0F0F0F0F0F0F0, 0x00FF00FF00FF00FF)  # >= 32 bits apart: none match another


def stored(row_id, series_key, kind, key, verdict, **columns):
    """Return an active decision row as the store holds it: imported, of 2026-11-01, no chain, unless columns say."""
    defaults = {"decider": "import", "decided_at": "2026-11-01T00:00:00+00:00", "chain": None, "bundle": False}
    return db_row(id=row_id, series_key=series_key, kind=kind, key=key, verdict=verdict, **{**defaults, **columns})


def of(rows, series_key="S"):
    """Return series series_key's Decisions over an index of these rows."""
    return store.Decisions.of_rows(store.index_of(rows), series_key, [])


UNIT = ((T,), 1.0)  # a block unit every sig() and sig((flip(T, 1),)) row matches


@budget()
@pytest.mark.parametrize("verdict", ["junk", "keep"])
def test_2_other_series_agreeing_decide_a_unit_of_a_series_with_none(verdict):
    rows = [stored(1, "A", "block", sig(), verdict), stored(2, "B", "probe", sig((flip(T, 1),)), verdict)]
    got = present(of(rows).decide("block", UNIT))
    assert (got["verdict"], got["decider"], got["chain"]) == (verdict, store.AGREEMENT, None), (
        "2 other series of 1 verdict decide it, as a stand-in with no chain"
    )
    assert store.agreeing(got) == (1, 2), "the stand-in names both agreeing decisions"
    assert present(of(rows).decide("probe", UNIT))["verdict"] == verdict, "block and probe decisions agree on a probe"


@budget()
def test_1_other_series_decides_nothing():
    rows = [stored(1, "A", "block", sig(), "junk"), stored(2, "A", "probe", sig((flip(T, 1),)), "junk")]
    assert of(rows).decide("block", UNIT) is None, "2 decisions of 1 other series are no agreement"
    assert of(rows, "A").decide("block", UNIT) is not None, "in its own series the decision decides as ever"
    second = [*rows, stored(3, "B", "block", sig(), "junk")]
    assert present(of(second).decide("block", UNIT))["decider"] == store.AGREEMENT, "a 2nd series makes it agree"


@budget()
@pytest.mark.parametrize("kind", ["block", "probe"])
def test_a_contrary_decision_in_any_series_ends_the_agreement(kind):
    rows = [stored(1, "A", "block", sig(), "junk"), stored(2, "B", "block", sig(), "junk")]
    assert present(of(rows).decide("block", UNIT))["decider"] == store.AGREEMENT, "A and B agree"
    contrary = [*rows, stored(3, "C", kind, sig((flip(T, 1),)), "keep")]
    assert of(contrary).decide("block", UNIT) is None, f"a {kind} keep of a third series: no agreement"


@budget()
@pytest.mark.parametrize("own", ["junk", "keep"])
def test_the_series_own_decision_beats_an_agreement(own):
    other = "keep" if own == "junk" else "junk"
    rows = [
        stored(1, "A", "block", sig(), other, decider="claude", decided_at="2026-11-09T00:00:00+00:00"),
        stored(2, "B", "block", sig(), other, decider="claude", decided_at="2026-11-09T00:00:00+00:00"),
        stored(3, "S", "block", sig((flip(T, 1),)), own),  # imported and older
    ]
    got = present(of(rows).decide("block", UNIT))
    assert (got["id"], got["verdict"]) == (3, own), "the series' own decision ranks before any agreement"
    without = present(of(rows[:2]).decide("block", UNIT))
    assert (without["decider"], without["verdict"]) == (store.AGREEMENT, other), "without it, the agreement decides"


@budget()
def test_a_global_keep_beats_an_agreed_junk_and_an_agreed_keep_beats_a_global_junk():
    agreed_junk = [stored(1, "A", "block", sig(), "junk"), stored(2, "B", "block", sig(), "junk")]
    got = present(of([*agreed_junk, stored(3, "", "block", sig((flip(T, 1),)), "keep")]).decide("block", UNIT))
    assert (got["id"], got["verdict"]) == (3, "keep"), "a global keep ranks before agreement"
    agreed_keep = [stored(1, "A", "block", sig(), "keep"), stored(2, "B", "block", sig(), "keep")]
    got = present(of([*agreed_keep, stored(3, "", "block", sig((flip(T, 1),)), "junk")]).decide("block", UNIT))
    assert (store.agreeing(got), got["verdict"]) == ((1, 2), "keep"), "an agreed keep ranks before a global junk"
    ranked = store.in_lookup_order([stored(3, "", "block", sig(), "junk"), got, stored(4, "", "block", sig(), "keep")])
    assert [r["id"] for r in ranked] == [4, 2, 3], "global keep, agreed keep, global junk"


@budget()
def test_a_tail_piece_is_never_decided_by_agreement():
    rows = [stored(1, "A", "piece", sig(), "junk"), stored(2, "B", "piece", sig(), "junk")]
    assert of(rows).decide("piece", UNIT) is None, "pieces never agree (D4)"
    blocks = [stored(1, "A", "block", sig(), "junk"), stored(2, "B", "block", sig(), "junk")]
    assert of(blocks).decide("block", UNIT) is not None, "the same 2 series agree on a block of that key"


@budget()
def test_frames_and_logos_agree_only_on_junk():
    vec = np.full(frames.CELLS * 3, 100, np.uint8)
    frame = frames.key_text(frames.Frame(vec, 1.5))
    for verdict, want in (("junk", ["junk", "junk"]), ("keep", [None, None])):
        rows = [
            stored(1, "A", "frame", frame, verdict),
            stored(2, "B", "frame", frame, verdict),
            stored(3, "A", "logo", logo().key_text(), verdict),
            stored(4, "B", "logo", logo().key_text(), verdict),
        ]
        d = of(rows)
        got = [d.decide("frame", frames.Frame(vec + 5, 1.5)), d.decide("logo", logo())]
        assert [None if r is None else r["verdict"] for r in got] == want, f"a frame or logo {verdict} agreement"


@budget()
def test_the_stand_in_names_every_agreeing_decision():
    rows = [
        stored(5, "A", "block", sig(), "junk", decided_at="2026-11-01T00:00:00+00:00"),
        stored(9, "A", "probe", sig((flip(T, 1),)), "junk", decider="claude", decided_at="2026-11-03T00:00:00+00:00"),
        stored(7, "B", "block", sig((flip(T, 2),)), "junk", decided_at="2026-11-02T00:00:00+00:00"),
        stored(8, "C", "block", sig((flip(T, 3),)), "junk", decided_at="2026-11-02T00:00:00+00:00"),
        stored(6, "D", "block", sig(((FAR_TILES[0]),)), "junk"),  # another key: no part of it
    ]
    got = present(of(rows).decide("block", UNIT))
    assert store.agreeing(got) == (5, 7, 8, 9), "every matching decision of every other series, sorted"
    assert (got["id"], got["kind"], got["key"]) == (9, "probe", sig((flip(T, 1),))), "the newest agreeing row stands"
    assert store.agreeing(rows[0]) == (), "a stored row stands for no agreement"


@budget(load="db")
def test_a_retired_agreeing_decision_ends_the_agreement_for_the_next_decide(conn):
    put(conn, decision("A", "block", sig(), "junk"), decision("B", "block", sig(), "junk"))
    before = store.Decisions(conn, "S")
    assert present(before.decide("block", UNIT))["decider"] == store.AGREEMENT, "A and B agree"
    first = one(conn, "SELECT min(id) FROM junk_decision")
    with db.transaction(conn):
        store.retire(conn, [first], store.new_act(conn, "test", "2026-11-10T00:00:00+00:00"))
    assert store.Decisions(conn, "S").decide("block", UNIT) is None, "1 series left: the next decide has no agreement"
    assert before.decide("block", UNIT) is not None, "a lookup built before the act keeps what it read (known limit 3)"


@budget(load="db")
def test_an_agreed_probe_raises_its_blocks_rest_as_a_candidate(conn):
    """2 other series junk a block's edge piece: it is removed by agreement, and the rest is offered for it."""
    piece, rest, whole = sig((FAR_TILES[0],)), sig((FAR_TILES[1],)), sig((FAR_TILES[2],))
    block: strip_units.EdgeBlock = {
        "start": 0,
        "end": 200,
        "where": "head",
        "sig": whole,
        "joins": [
            {"start": 0, "end": 60, "sig": piece, "uniform": False, "rest": {"start": 60, "end": 200, "sig": rest}}
        ],
    }

    def junk_only(kind, text):
        row = store.Decisions(conn, "S").decide(kind, store.parse_key(kind, text))
        return row if row is not None and row["verdict"] == "junk" else None

    put(conn, decision("A", "probe", piece, "junk"))
    removed, left = strip_units.match_blocks([block], junk_only)
    assert removed == [], "1 other series: the piece is undecided, the block stays whole"
    put(conn, decision("B", "block", piece, "junk"))
    removed, left = strip_units.match_blocks([block], junk_only)
    assert [(r["start"], r["end"], r["why"]) for r in removed] == [(0, 60, "match")], "the agreed piece is removed"
    assert store.agreeing(removed[0]["decision"]), "removed by the agreement's stand-in"
    assert (left[0]["start"], left[0].get("rest")) == (60, True), "the block's rest is left"
    (pick,) = candidates.select(conn, "S", [{**left[0], "kind": "block"}], word_lists())
    assert pick["reason"] == "the rest of a block whose edge piece is junk", pick


@budget()
def test_1_other_series_and_a_global_decision_of_1_verdict_make_no_agreement():
    rows = [stored(1, "A", "block", sig(), "junk"), stored(2, "", "block", sig((flip(T, 1),)), "junk")]
    got = present(of(rows).decide("block", UNIT))
    assert (got["id"], store.agreeing(got)) == (2, ()), "a global decision is no other series: the global junk decides"
    with_b = present(of([*rows, stored(3, "B", "block", sig(), "junk")]).decide("block", UNIT))
    assert store.agreeing(with_b) == (1, 3), "a 2nd series agrees, the global row not among them"
