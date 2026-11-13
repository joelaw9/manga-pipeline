"""Junk decision store and import (SPEC "Junk decisions": decisions, import)."""

import base64
import sqlite3

import numpy as np
import pytest

from manga_pipeline import cli, db, images, names
from manga_pipeline.junk import candidates, frames, importing, store

from tests.builders import config_file, site_cfg
from tests.timing import budget

T = 0x0123456789ABCDEF
PIPELINE_SIGHTING = ("S", "block", "x", "a pipeline chapter", "2026-11-12T00:00:00+00:00", "pipeline")


def sig(tiles=(T,), aspect=1.0):
    return images.sig_to_str((tuple(tiles), aspect))


def flip(t, bits):
    """t with its lowest `bits` bits inverted (Hamming distance `bits`)."""
    return t ^ ((1 << bits) - 1)


def decision(series_key, kind, key, verdict, decider="import", at="2026-11-01T00:00:00+00:00", source=None):
    return dict(
        series_key=series_key, kind=kind, key=key, verdict=verdict, decider=decider, decided_at=at, source=source
    )


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
    got = store.Decisions(conn, "S").decide("block", ((T,), 1.0))
    assert (got["series_key"], got["verdict"]) == ("S", "keep"), "the series' keep decides first"
    other = store.Decisions(conn, "Other").decide("block", ((T,), 1.0))
    assert (other["series_key"], other["verdict"]) == ("", "keep"), "for another series, a global keep beats junk"


@budget(load="db")
def test_newest_decision_wins_within_its_rank(conn):
    put(
        conn,
        decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00", source="old"),
        decision("S", "block", sig((flip(T, 1),)), "junk", at="2026-11-02T00:00:00+00:00", source="new"),
    )
    assert store.Decisions(conn, "S").decide("block", ((T,), 1.0))["source"] == "new", "newest first"


@budget(load="db")
@pytest.mark.parametrize("decider", ["claude", "import"])
def test_a_newer_junk_of_one_key_does_not_beat_an_older_keep_of_another(conn, decider):
    """Unit x is within the match distance of both b and a (b and a are not of each other): keep b, then junk a:
    x stays kept (matching is not transitive; keeps first, the bulk converter's rule)."""
    bits = (1 << images.PHASH_THRESHOLD) - 1
    b, x, a = T, T ^ bits, T ^ bits ^ (bits << images.PHASH_THRESHOLD)
    put(
        conn,
        decision("S", "block", sig((b,)), "keep", decider=decider, at="2026-11-01T00:00:00+00:00", source="keep b"),
        decision("S", "block", sig((a,)), "junk", decider=decider, at="2026-11-02T00:00:00+00:00", source="junk a"),
    )
    assert not images.matches(((b,), 1.0), ((a,), 1.0)), "b and a are apart"
    assert store.Decisions(conn, "S").decide("block", ((x,), 1.0))["source"] == "keep b", "x stays kept"


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
    assert hit is not None and hit[1] == 8, "another series' approval 8 bits away is near"
    assert store.near(conn, "S", ((flip(T, 9),), 1.0)) is None, "9 bits is not near"
    put(conn, decision("S", "block", sig((flip(T, 8),)), "junk"))
    assert store.near(conn, "S", ((flip(T, 8),), 1.0)) is None, "a decision that applies and matches decides: no near"


# ---------------------------------------------------------------- upsert


@budget(load="db")
def test_a_pipeline_decision_is_never_overwritten_by_an_import(conn):
    put(conn, decision("S", "block", sig(), "keep", decider="claude", at="2026-11-01T00:00:00+00:00"))
    counts = put(conn, decision("S", "block", sig(), "junk", at="2026-12-01T00:00:00+00:00"))
    row = conn.execute("SELECT verdict, decider FROM junk_decision").fetchone()
    assert tuple(row) == ("keep", "claude") and counts["unchanged"] == 1, "a newer import leaves claude's row"


@budget(load="db")
def test_an_older_import_never_undoes_a_newer_one(conn):
    put(conn, decision("S", "block", sig(), "keep", at="2026-11-05T00:00:00+00:00"))
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-01T00:00:00+00:00"))
    assert conn.execute("SELECT verdict FROM junk_decision").fetchone()[0] == "keep", "the newer import stays"
    put(conn, decision("S", "block", sig(), "junk", at="2026-11-06T00:00:00+00:00"))
    assert conn.execute("SELECT verdict FROM junk_decision").fetchone()[0] == "junk", "a newer import replaces it"


# ---------------------------------------------------------------- import from the bulk converter's store

BULK_SCHEMA = (
    """CREATE TABLE junk (id INTEGER PRIMARY KEY AUTOINCREMENT, series TEXT, decision TEXT NOT NULL,
       origin TEXT NOT NULL, review_id TEXT, tiles TEXT NOT NULL, aspect REAL NOT NULL, chain TEXT,
       credit INTEGER NOT NULL, note TEXT, added TIMESTAMP DEFAULT CURRENT_TIMESTAMP, superseded INTEGER)""",
    """CREATE TABLE frames (id INTEGER PRIMARY KEY AUTOINCREMENT, series TEXT, vec BLOB NOT NULL,
       aspect REAL NOT NULL, decision TEXT NOT NULL, review_id TEXT, note TEXT,
       added TIMESTAMP DEFAULT CURRENT_TIMESTAMP, superseded INTEGER)""",
    """CREATE TABLE marks (id INTEGER PRIMARY KEY AUTOINCREMENT, series TEXT, template BLOB NOT NULL,
       w INTEGER NOT NULL, h INTEGER NOT NULL, decision TEXT NOT NULL, review_id TEXT, note TEXT,
       added TIMESTAMP DEFAULT CURRENT_TIMESTAMP, superseded INTEGER)""",
    "CREATE TABLE meta (db_id TEXT NOT NULL)",
    """CREATE TABLE junk_seen (series TEXT NOT NULL, chapter TEXT NOT NULL, kind TEXT NOT NULL, tiles TEXT NOT NULL,
       ntiles INTEGER NOT NULL, abin INTEGER NOT NULL, aspect REAL NOT NULL, span TEXT, state TEXT NOT NULL,
       run_id TEXT)""",
)


def bulk_store(path):
    """A small bulk-converter store in WAL mode whose rows stay in the WAL (the open connection is returned)."""
    c = sqlite3.connect(path, autocommit=True)
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA wal_autocheckpoint = 0")
    for ddl in BULK_SCHEMA:
        c.execute(ddl)

    def junk(series, decision_, origin, tiles, added, chain=None, superseded=None):
        c.execute(
            "INSERT INTO junk (series, decision, origin, tiles, aspect, chain, credit, added, superseded)"
            " VALUES (?, ?, ?, ?, 1.0, ?, 0, ?, ?)",
            (series, decision_, origin, f"{tiles:016x}", chain, added, superseded),
        )

    junk("S", "approve", "block", T, "2026-11-06 10:00:00")
    junk(None, "approve", "probe", flip(T, 20), "2026-11-06 10:00:01")
    junk("S", "keep", "piece", flip(T, 30), "2026-11-06 10:00:02")
    junk("S", "approve", "cut", flip(T, 40), "2026-11-06 10:00:03")
    junk("S", "approve", "block", flip(T, 50), "2026-11-06 10:00:04", chain="{}")
    junk("S", "approve", "page", flip(T, 60), "2026-11-06 10:00:05", superseded=99)
    # 2 active rows on 1 key: the old lookup obeys the keep, though it is older
    junk("S", "keep", "band", flip(T, 10), "2026-11-06 09:00:00")
    junk("S", "approve", "band", flip(T, 10), "2026-11-06 11:00:00")
    vec = np.full(frames.CELLS * 3, 7, np.uint8).tobytes()
    c.execute(
        "INSERT INTO frames (series, vec, aspect, decision, added) VALUES (NULL, ?, 1.42, 'approve',"
        " '2026-11-06 10:00:06')",
        (vec,),
    )
    c.execute("INSERT INTO marks (series, template, w, h, decision) VALUES (NULL, x'00', 1, 1, 'approve')")
    for chapter, kind in (
        ("ryu_Episode 3.cbz", "block"),
        ("ryu_Episode 5.cbz", "block"),
        ("ryu_Episode 5.cbz", "span"),
    ):
        c.execute(
            "INSERT INTO junk_seen (series, chapter, kind, tiles, ntiles, abin, aspect, span, state)"
            " VALUES ('S', ?, ?, ?, 1, 10, 1.0, NULL, 'kept')",
            (chapter, kind, f"{T:016x}"),
        )
    return c


@budget(load="db")
def test_bulk_store_import_maps_verdicts_scope_and_kinds_and_reads_the_wal(conn, cfg, tmp_path):
    src = bulk_store(tmp_path / "junk.sqlite3")
    try:
        assert (tmp_path / "junk.sqlite3-wal").stat().st_size > 0, "the fixture's rows sit in the WAL"
        counts = importing.run(conn, tmp_path / "junk.sqlite3", cfg["database"]["path"])
    finally:
        src.close()
    chained_key = sig((flip(T, 50),))
    rows = {
        (r["series_key"], r["kind"]): r
        for r in conn.execute("SELECT * FROM junk_decision WHERE key != ?", (chained_key,))
    }
    assert counts["added"] == 6 and counts["skipped"] == {"cut": 1, "chain": 1, "marks": 1}, counts
    chained = conn.execute("SELECT verdict FROM junk_decision WHERE key = ?", (chained_key,)).fetchone()
    assert chained["verdict"] == "junk", "a chained block approve comes over as a plain decision, its chain dropped"
    assert rows[("S", "block")]["verdict"] == "junk", "approve -> junk"
    assert rows[("", "probe")]["series_key"] == "", "a global row stays global ('')"
    assert rows[("S", "piece")]["verdict"] == "keep", "keep -> keep"
    assert rows[("S", "band")]["verdict"] == "keep", "of 2 active rows on 1 key the keep wins, as the old lookup"
    assert rows[("", "frame")]["key"].endswith("|1.42"), "frames come over with their aspect"
    assert rows[("S", "block")]["decided_at"] == "2026-11-06T10:00:00+00:00", "the old time, in our format"
    assert all(r["decider"] == "import" for r in rows.values()), "decider import"


@budget(load="db")
def test_a_second_import_changes_nothing_and_leaves_pipeline_decisions(conn, cfg, tmp_path):
    bulk_store(tmp_path / "junk.sqlite3").close()
    importing.run(conn, tmp_path / "junk.sqlite3", cfg["database"]["path"])
    with db.transaction(conn):
        conn.execute("UPDATE junk_decision SET decider = 'claude', verdict = 'keep' WHERE key = ?", (sig(),))
        conn.execute("INSERT INTO junk_seen VALUES (?, ?, ?, ?, ?, ?)", PIPELINE_SIGHTING)

    def content():
        rows = conn.execute("SELECT series_key, kind, key, verdict, decider, decided_at, source FROM junk_decision")
        return sorted(map(tuple, rows)), sorted(map(tuple, conn.execute("SELECT * FROM junk_seen")))

    before = content()
    counts = importing.run(conn, tmp_path / "junk.sqlite3", cfg["database"]["path"])
    assert content() == before, "re-running the import changes no decision or sighting"
    assert (counts["added"], counts["removed"], counts["changed"]) == (0, 0, 0), counts
    assert counts["pipeline_kept"] == 1, "the source row on Claude's key is left out"


def change_store(path, sql, *params):
    c = sqlite3.connect(path, autocommit=True)
    c.execute(sql, params)
    c.close()


@budget(load="db")
def test_a_decision_the_bulk_converter_deleted_or_changed_is_followed(conn, cfg, tmp_path):
    store_path = tmp_path / "junk.sqlite3"
    bulk_store(store_path).close()
    importing.run(conn, store_path, cfg["database"]["path"])
    change_store(store_path, "UPDATE junk SET superseded = 1 WHERE origin = 'probe'")  # deleted there
    change_store(
        store_path,
        "UPDATE junk SET decision = 'keep', added = '2026-11-08 00:00:00' WHERE origin = 'block' AND chain IS NULL",
    )
    with db.transaction(conn):
        conn.execute("INSERT INTO junk_seen VALUES (?, ?, ?, ?, ?, ?)", PIPELINE_SIGHTING)
    counts = importing.run(conn, store_path, cfg["database"]["path"])
    rows = {
        (r["series_key"], r["kind"]): r["verdict"]
        for r in conn.execute("SELECT * FROM junk_decision WHERE key != ?", (sig((flip(T, 50),)),))
    }
    assert ("", "probe") not in rows and counts["removed"] == 1, "a decision the bulk converter dropped disappears"
    assert rows[("S", "block")] == "keep" and counts["changed"] == 1, "a changed one changes"
    origins = sorted(r[0] for r in conn.execute("SELECT origin FROM junk_seen"))
    assert origins == ["import", "import", "pipeline"], "the pipeline's own sightings survive the rebuild"


@budget(load="db")
def test_a_pipeline_source_brings_its_deciders_and_a_later_store_import_leaves_them(tmp_path):
    lxc = db.connect(site_cfg(tmp_path / "lxc"))
    win_cfg = site_cfg(tmp_path / "win")
    win = db.connect(win_cfg)
    bulk_store(tmp_path / "junk.sqlite3").close()
    importing.run(lxc, tmp_path / "junk.sqlite3", "unused")
    put(win, decision("S", "block", sig(), "keep", decider="claude", at="2026-11-10T00:00:00+00:00", source="r1"))
    win.close()
    importing.run(lxc, win_cfg["database"]["path"], "unused")
    row = lxc.execute("SELECT * FROM junk_decision WHERE series_key = 'S' AND kind = 'block'").fetchone()
    assert (row["verdict"], row["decider"], row["source"]) == ("keep", "claude", "r1"), "claude's row replaces import"
    importing.run(lxc, tmp_path / "junk.sqlite3", "unused")
    row = lxc.execute("SELECT * FROM junk_decision WHERE series_key = 'S' AND kind = 'block'").fetchone()
    assert row["decider"] == "claude", "a later store import leaves the pipeline-made row"
    lxc.close()


@budget(load="db")
def test_import_refuses_its_own_database_and_a_foreign_one(conn, cfg, tmp_path):
    with pytest.raises(importing.SourceError, match="own database"):
        importing.run(conn, cfg["database"]["path"], cfg["database"]["path"])
    other = sqlite3.connect(tmp_path / "other.sqlite3")
    other.execute("CREATE TABLE x (a)")
    other.close()
    with pytest.raises(importing.SourceError, match="neither"):
        importing.run(conn, tmp_path / "other.sqlite3", cfg["database"]["path"])
    with pytest.raises(importing.SourceError, match="no database"):
        importing.run(conn, tmp_path / "missing.sqlite3", cfg["database"]["path"])
    (tmp_path / "notes.txt").write_bytes(b"not a database, just text " * 40)
    with pytest.raises(importing.SourceError, match="not a readable SQLite database"):
        importing.run(conn, tmp_path / "notes.txt", cfg["database"]["path"])
    odd = sqlite3.connect(tmp_path / "odd.sqlite3")
    odd.execute("CREATE TABLE junk_decision (a)")
    odd.execute("CREATE TABLE schema_step (a)")
    odd.close()
    with pytest.raises(importing.SourceError, match="not the expected ones"):
        importing.run(conn, tmp_path / "odd.sqlite3", cfg["database"]["path"])


@budget(load="db")
def test_junk_import_command_prints_counts_and_refuses_with_exit_2(cfg, tmp_path, capsys):
    bulk_store(tmp_path / "junk.sqlite3").close()
    path = config_file(tmp_path, cfg)
    assert cli.main(["--config", str(path), "junk", "import", str(tmp_path / "junk.sqlite3")]) == 0
    assert "added 6" in capsys.readouterr().out, "the command reports what it imported"
    assert cli.main(["--config", str(path), "junk", "import", str(tmp_path / "missing.sqlite3")]) == 2


# ---------------------------------------------------------------- decision rows of a series record (baseline)


def record_with_rows(schema=4):
    from tests.world import bulk_record

    rec = bulk_record("S", 7, [])
    rec["schema"] = schema
    vec = base64.b64encode(np.full(frames.CELLS * 3, 9, np.uint8).tobytes()).decode()
    rec["series"]["decisions"]["rows"] = {
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
        "marks": [],
    }
    return rec


@budget(load="db")
def test_a_schema_4_records_decision_rows_land_as_import_rows(conn):
    put(conn, *importing.winners(importing.record_rows(record_with_rows())))
    rows = {
        (r["series_key"], r["kind"], r["verdict"], r["decider"]) for r in conn.execute("SELECT * FROM junk_decision")
    }
    assert rows == {("S", "block", "junk", "import"), ("", "frame", "junk", "import")}, "cut rows stay out"
    assert importing.record_rows(record_with_rows(schema=3)) == [], "a schema-3 record gives no rows"


@budget(load="db")
def test_record_rows_after_a_store_import_are_no_error_and_never_undo_a_newer_one(conn):
    put(conn, decision("S", "block", sig(), "keep", at="2026-11-09T00:00:00+00:00"))
    put(conn, *importing.winners(importing.record_rows(record_with_rows())))
    row = conn.execute("SELECT verdict FROM junk_decision WHERE kind = 'block'").fetchone()
    assert row[0] == "keep", "an older record row never undoes a newer store import"


def _no_key(rows):
    del rows[0]["key"]


def _not_a_row(rows):
    rows[0] = "a row"


def _number_tiles(rows):
    rows[0]["key"][0] = 7


@budget(load="pure")
@pytest.mark.parametrize("damage", [_no_key, _not_a_row, _number_tiles])
def test_a_malformed_decision_row_is_a_record_that_does_not_parse(damage):
    from manga_pipeline.convert import record_reader

    rec = record_with_rows()
    damage(rec["series"]["decisions"]["rows"]["junk"])
    with pytest.raises(record_reader.RecordError, match="malformed"):
        importing.record_rows(rec)


@budget(load="db")
def test_a_pipeline_decision_wins_for_every_signature_it_covers(conn):
    put(conn, decision("S", "block", sig(), "junk", decider="claude", at="2026-11-01T00:00:00+00:00"))
    put(conn, decision("S", "block", sig((flip(T, 2),)), "keep", at="2026-12-01T00:00:00+00:00"))
    got = store.Decisions(conn, "S").decide("block", ((flip(T, 1),), 1.0))
    assert (got["decider"], got["verdict"]) == ("claude", "junk"), (
        "an imported keep on a matching key never overrides Claude's junk"
    )


@budget()
def test_sig_text_round_trip():
    s = ((0x1234, 0xFFFFFFFFFFFFFFFF), 2.0123456789)
    assert images.sig_from_str(images.sig_to_str(s)) == s, "a signature survives its text form exactly"


@budget(load="db")
def test_the_stores_sightings_count_toward_recurrence_and_import_once(conn, cfg, tmp_path):
    from manga_pipeline import names
    from manga_pipeline.junk import candidates

    bulk_store(tmp_path / "junk.sqlite3").close()
    counts = importing.run(conn, tmp_path / "junk.sqlite3", cfg["database"]["path"])
    assert counts["seen"] == 2, "edge blocks, pages and bands come over; spans do not"
    assert candidates.chapters_showing(conn, "S", "block", sig()) == 2, "each chapter counts once for recurrence"
    got = {r[0] for r in conn.execute("SELECT identity FROM junk_seen")}
    assert got == {names.identity(names.parse("ryu_Episode 3")), names.identity(names.parse("ryu_Episode 5"))}, (
        "a sighting's chapter is the identity of its archive name"
    )
    first = sorted(map(tuple, conn.execute("SELECT * FROM junk_seen")))
    again = importing.run(conn, tmp_path / "junk.sqlite3", cfg["database"]["path"])
    assert again["seen"] == 2 and sorted(map(tuple, conn.execute("SELECT * FROM junk_seen"))) == first, (
        "a second import rebuilds the same sightings, first seen before any pipeline scan"
    )


def chapter(number):
    return names.identity(names.parse(f"Grp_Chapter {number}"))


def imported_sightings(conn, numbers):
    rows = [("S", "block", sig((flip(T, 40 + n),)), chapter(n), importing.IMPORTED_SEEN_AT, "import") for n in numbers]
    with db.transaction(conn):
        candidates.write_seen(conn, rows)


@budget(load="db")
def test_the_first_chapters_of_imported_sightings_go_by_chapter_number(conn):
    imported_sightings(conn, (10, 2, 1, 3, 11))
    assert candidates.first_identities(conn, "S") == {chapter(1), chapter(2), chapter(3)}, (
        "sightings seen at the same time are ordered by chapter number, so 10 is not before 2"
    )


@budget(load="db")
def test_a_first_chapter_of_a_series_without_decisions_offers_every_unmatched_edge_block(conn):
    imported_sightings(conn, (1, 2, 3))
    block = {"kind": "block", "sig": sig((flip(T, 60),)), "start": 0, "end": 300, "where": "head", "probes": []}
    words = {"keywords": [], "weak": [], "paired": [], "suppress": []}
    (pick,) = candidates.select(conn, "S", chapter(2), [block], words)
    assert pick["reason"] == "a first chapter of a series with no decisions", pick
    assert candidates.select(conn, "S", chapter(4), [block], words) == [], "a later chapter offers nothing by rule (d)"
    put(conn, decision("S", "piece", sig((flip(T, 20),)), "keep"))
    assert candidates.select(conn, "S", chapter(2), [block], words) == [], "once the series has a decision, (d) is off"


@budget(load="db")
def test_a_band_on_page_2_recurs_with_page_1_bands_of_other_chapters(conn):
    """Akuyaku ch12/13 carry a band on page 2, ch14/15 on page 1: sightings name no page, so 1 band candidate for
    its recurrence in 4 chapters."""
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
    unit = {"kind": "band", "key": band, "page": 1, "where": "top", "reasons": []}
    words = {"keywords": [], "weak": [], "paired": [], "suppress": []}
    (pick,) = candidates.select(conn, "S", chapter(15), [unit], words)
    assert (pick["kind"], pick["reason"]) == ("band", "seen in 4 chapters"), pick


@budget(load="db")
def test_a_candidate_id_and_folder_follow_its_key_not_the_order_it_is_offered_in(conn):
    a, other = sig((flip(T, 1),)), sig((~T & (2**64 - 1),))
    pick = {"kind": "block", "key": a, "reason": "r"}
    with db.transaction(conn):
        (first,) = candidates.join(conn, "S", [pick], "2026-11-01T00:00:00+00:00")
        both = candidates.join(
            conn, "S", [{**pick, "key": other}, {**pick, "key": sig((flip(T, 2),))}], "2026-11-02T00:00:00+00:00"
        )
    assert first in both and len(both) == 2, "a matching key joins its candidate, wherever it is listed"
    row = conn.execute("SELECT folder FROM junk_candidate WHERE id = ?", (first,)).fetchone()
    assert row[0] == candidates.folder_name("S", "block", a), "the folder is named by the key"
