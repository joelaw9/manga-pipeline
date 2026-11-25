"""Junk decision store and import (SPEC "Junk decisions": decisions, import)."""

import base64
import json
import shutil

import numpy as np
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
def test_near_finds_an_approved_signature_of_any_series_but_not_one_that_decides(conn):
    put(conn, decision("Other", "block", sig(), "junk"))
    hit = store.near(conn, "S", ((flip(T, 8),), 1.05))
    assert hit is not None, "another series' approval 8 bits away is near"
    assert hit[1] == 8, "another series' approval 8 bits away is near"
    assert store.near(conn, "S", ((flip(T, 9),), 1.0)) is None, "9 bits is not near"
    put(conn, decision("S", "block", sig((flip(T, 8),)), "junk"))
    assert store.near(conn, "S", ((flip(T, 8),), 1.0)) is None, "a decision that applies and matches decides: no near"


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
    assert one(conn, "SELECT verdict FROM junk_decision") == "keep", "the newer import stays"
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-06T00:00:00+00:00"))
    assert one(conn, "SELECT verdict FROM junk_decision") == "junk", "a newer import replaces it"


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
def test_a_pipeline_decision_wins_for_every_signature_it_covers(conn):
    put(conn, decision("S", "block", sig(), "junk", decider="claude", at="2026-11-01T00:00:00+00:00"))
    put(conn, decision("S", "block", sig((flip(T, 2),)), "keep", at="2026-12-01T00:00:00+00:00"))
    got = present(store.Decisions(conn, "S").decide("block", ((flip(T, 1),), 1.0)))
    assert (got["decider"], got["verdict"]) == ("claude", "junk"), (
        "an imported keep on a matching key never overrides Claude's junk"
    )


@budget()
def test_sig_text_round_trip():
    s = ((0x1234, 0xFFFFFFFFFFFFFFFF), 2.0123456789)
    assert images.sig_from_str(images.sig_to_str(s)) == s, "a signature survives its text form exactly"


def chapter(number):
    return names.identity(names.parse(f"Grp_Chapter {number}"))


IMPORTED_SEEN_AT = "1970-01-01T00:00:00+00:00"  # the bulk converter's sightings: first seen before any scan


def imported_sightings(conn, numbers):
    rows = [("S", "block", sig((flip(T, 40 + n),)), chapter(n), IMPORTED_SEEN_AT, "import") for n in numbers]
    with db.transaction(conn):
        candidates.write_seen(conn, rows)


@budget(load="db")
def test_the_first_chapters_of_imported_sightings_go_by_chapter_number(conn):
    imported_sightings(conn, (10, 2, 1, 3, 11))
    assert candidates.first_identities(conn, "S") == {chapter(1), chapter(2), chapter(3)}, (
        "sightings seen at the same time are ordered by chapter number, so 10 is not before 2"
    )


@budget(load="db")
def test_a_first_chapter_of_a_series_offers_every_unmatched_edge_block(conn):
    imported_sightings(conn, (1, 2, 3))
    block: strip_units.BlockUnit = {
        "kind": "block",
        "sig": sig((flip(T, 60),)),
        "start": 0,
        "end": 300,
        "where": "head",
        "probes": [],
    }
    words = word_lists()
    (pick,) = candidates.select(conn, "S", chapter(2), [block], words)
    assert pick["reason"] == "a first chapter of the series", pick
    assert candidates.select(conn, "S", chapter(4), [block], words) == [], "a later chapter offers nothing by rule (d)"
    put(conn, decision("S", "piece", sig((flip(T, 20),)), "keep"))
    (pick,) = candidates.select(conn, "S", chapter(2), [block], words)
    assert pick["reason"] == "a first chapter of the series", "the series' decisions do not turn (d) off"


@budget(load="db")
def test_the_rest_of_a_block_whose_edge_piece_is_junk_is_offered(conn):
    put(conn, decision("S", "piece", sig((flip(T, 20),)), "keep"))  # rule (d) aside: chapter 9 is no first chapter
    imported_sightings(conn, (1, 2, 3))
    rest: strip_units.BlockUnit = {
        "kind": "block",
        "sig": sig((flip(T, 60),)),
        "start": 120,
        "end": 300,
        "where": "tail",
        "probes": [],
    }
    words = word_lists()
    assert candidates.select(conn, "S", chapter(9), [rest], words) == [], "a plain block with no reason: not offered"
    (pick,) = candidates.select(conn, "S", chapter(9), [{**rest, "rest": True}], words)
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
    put(conn, decision("S", "piece", sig((flip(T, 20),)), "keep"))  # rule (d) off
    unit: page_units.BandUnit = {
        "kind": "band",
        "key": band,
        "page": 1,
        "where": "top",
        "box": [0, 0, 1, 1],
        "reasons": [],
    }
    words = word_lists()
    (pick,) = candidates.select(conn, "S", chapter(15), [unit], words)
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
    assert store.near(conn, "T", ((flip(FLAT, 4),), 1.0)) is not None, "a signable unit of another series is near"
    assert store.near(conn, "T", ((FLAT,), 1.0)) is None, "a flat unit is near nothing"


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
    assert candidates.select(conn, "S", chapter(2), [band], words) == [], "no decision could be written on it"
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
    (pick,) = candidates.select(conn, "S", chapter(2), [block], words)
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


def schema_without(tmp_path, monkeypatch, step):
    """db.SCHEMA replaced by a copy without `step` (monkeypatch.undo() brings the program's back)."""
    before = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, before)
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
    schema_without(tmp_path, monkeypatch, "0040_series_rules_key.sql")
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
