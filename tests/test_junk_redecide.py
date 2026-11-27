"""`junk redecide`, `junk undo`, `junk rows` and `junk row --where` (SPEC "Junk decisions", decision commands).

Decisions are rows of junk_decision; sightings rows of junk_seen; series carry their rules key (their first folder's
name).
"""

import json
from contextlib import closing

import numpy as np
import pytest

from manga_pipeline import cli, config, db
from manga_pipeline.junk import candidates, frames, redecide, review, store, where

from tests.builders import add_chapter, add_series, adopted, config_file, one, present
from tests.timing import budget

AT = "2026-11-25T00:00:00+00:00"
FAR = (0x0F0F0F0F0F0F0F0F, 0xF0F0F0F0F0F0F0F0, 0x00FF00FF00FF00FF)  # >= 32 bits apart


def key(tile):
    """Return a 1-tile signature text; tiles 1 bit apart match, 2 of FAR do not."""
    return f"{tile:016x}|0.7"


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        for name in ("S", "T"):
            series = add_series(conn, name)
            with db.transaction(conn):
                conn.execute("UPDATE series SET folder = %s WHERE id = %s", (f"/lib/{name}", series))  # rules key
        yield cfg, conn


def decide(conn, series_key, tile, verdict, decider="claude"):
    """Write 1 decision on a block key; return its id."""
    with db.transaction(conn):
        store.upsert(
            conn,
            [
                {
                    "series_key": series_key,
                    "kind": "block",
                    "key": key(tile),
                    "verdict": verdict,
                    "decider": decider,
                    "decided_at": "2026-11-01T00:00:00+00:00",
                    "source": None,
                }
            ],
        )
    return one(conn, "SELECT max(id) FROM junk_decision")


def seen(conn, series_key, tile, identity):
    """Record a sighting of a block key in a chapter identity of a series."""
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
            VALUES (%s, 'block', %s, %s, %s, 'pipeline')""",
            (series_key, key(tile), identity, AT),
        )


def active(conn):
    """Return the active decisions, every column, in id order."""
    rows = conn.execute(
        "SELECT * FROM junk_decision WHERE superseded_by IS NULL AND retired_by IS NULL ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


def deciding(conn, series_key, tile):
    """Return the verdict deciding a block of tile in a series, or None."""
    row = store.Decisions(conn, series_key).decide("block", store.parse_key("block", key(tile)))
    return None if row is None else row["verdict"]


@budget(load="db")
@pytest.mark.parametrize("how", ["claude row", "import row", "global with series", "global by sightings", "retire"])
def test_undo_of_a_redecide_restores_the_rows_exactly(site, how):
    _cfg, conn = site
    if how == "claude row":
        row, act = decide(conn, "S", FAR[0], "junk"), None
        before = active(conn)
        act = redecide.redecide(conn, [row], "keep", AT)
    elif how == "import row":
        row = decide(conn, "S", FAR[0], "junk", "import")
        before = active(conn)
        act = redecide.redecide(conn, [row], "keep", AT)
    elif how == "global with series":
        row = decide(conn, "", FAR[0], "junk", "import")
        before = active(conn)
        act = redecide.redecide_in(conn, [row], "keep", ["S"], AT)
    elif how == "global by sightings":
        row = decide(conn, "", FAR[0], "junk", "import")
        seen(conn, "S", FAR[0] ^ 1, "c1")
        before = active(conn)
        act = redecide.redecide(conn, [row], "keep", AT)
    else:
        row = decide(conn, "", FAR[0], "junk", "import")
        before = active(conn)
        act = redecide.retire_global(conn, [row], "keep", AT)
    assert active(conn) != before, "the act changed the active decisions"
    redecide.undo(conn, act, AT)
    assert active(conn) == before, "undo restores the active rows exactly: ids, deciders and every column"


@budget(load="db")
def test_redecide_refuses_all_when_one_row_is_inactive(site):
    _cfg, conn = site
    first, second = decide(conn, "S", FAR[0], "junk"), decide(conn, "S", FAR[1], "junk")
    redecide.redecide(conn, [second], "keep", AT)
    before = active(conn)
    with pytest.raises(review.Refused, match=f"{second} is not active"):
        redecide.redecide(conn, [first, second], "keep", AT)
    assert active(conn) == before, "nothing is written"


@budget(load="db")
def test_redecide_refuses_when_another_row_of_the_other_verdict_still_decides_a_written_key(site):
    _cfg, conn = site
    first = decide(conn, "S", FAR[0], "keep")
    second = decide(conn, "S", FAR[0] ^ 1, "keep")  # near: it decides the first's key too, and keeps rank first
    before = active(conn)
    with pytest.raises(review.Refused, match=f"decision {second} \\(keep\\) still decides"):
        redecide.redecide(conn, [first], "junk", AT)
    assert active(conn) == before, "a re-decision that changes nothing is refused whole"


@budget(load="db")
def test_redecide_of_2_near_keeps_to_junk_together_is_accepted(site):
    _cfg, conn = site
    rows = [decide(conn, "S", FAR[0], "keep"), decide(conn, "S", FAR[0] ^ 1, "keep")]
    redecide.redecide(conn, rows, "junk", AT)
    assert deciding(conn, "S", FAR[0]) == "junk", "both re-decided together: junk decides"


@budget(load="db")
def test_redecide_global_with_series_writes_series_rows_and_keeps_the_global(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    redecide.redecide_in(conn, [g], "keep", ["S"], AT)
    assert deciding(conn, "S", FAR[0]) == "keep", "S opts out"
    assert deciding(conn, "T", FAR[0]) == "junk", "the global junk stays for T"


@budget(load="db")
def test_redecide_global_retire_reopens_its_units_as_candidates(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    redecide.retire_global(conn, [g], "keep", AT)
    assert deciding(conn, "S", FAR[0]) is None, "retired: nothing decides its units, so they become candidates"


@budget(load="db")
def test_redecide_global_junk_discovers_series_from_sightings(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    seen(conn, "S", FAR[0] ^ 1, "c1")
    redecide.redecide(conn, [g], "keep", AT)
    keeps = conn.execute(
        "SELECT series_key FROM junk_decision WHERE verdict = 'keep' AND retired_by IS NULL ORDER BY id"
    ).fetchall()
    assert [r[0] for r in keeps] == ["S"], "a keep in the series whose sightings it decided, none in T"
    assert deciding(conn, "T", FAR[0]) is None, "the global junk is retired"


@budget(load="db")
def test_redecide_global_with_no_sighting_is_refused(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    with pytest.raises(review.Refused, match="decides no sighted unit"):
        redecide.redecide(conn, [g], "keep", AT)


@budget(load="db")
def test_redecide_global_keep_to_junk_only_retires(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "keep", "import")
    redecide.redecide(conn, [g], "junk", AT)
    assert one(conn, "SELECT count(*) FROM junk_decision") == 1, "nothing is written"
    assert deciding(conn, "S", FAR[0]) is None, "the global keep is retired"


@budget(load="db")
def test_redecide_refuses_series_with_retire_and_options_on_a_series_row(site, tmp_path, capsys):
    cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    s = decide(conn, "S", FAR[1], "junk")
    path = str(config_file(tmp_path, cfg))
    args = ["--config", path, "junk", "redecide", str(g), "keep", "--series", "S", "--retire"]
    assert cli.main(args) == 2, "--series with --retire is refused"
    assert "go apart" in capsys.readouterr().err, "the refusal says why"
    with pytest.raises(review.Refused, match="own: re-decide it without --series"):
        redecide.redecide_in(conn, [s], "keep", ["S"], AT)
    with pytest.raises(review.Refused, match="no series has"):
        redecide.redecide_in(conn, [g], "keep", ["Nope"], AT)


@budget(load="db")
def test_undo_refuses_an_undone_act_an_undo_and_an_act_changed_since(site):
    _cfg, conn = site
    row = decide(conn, "S", FAR[0], "junk")
    act = redecide.redecide(conn, [row], "keep", AT)
    written = one(conn, "SELECT max(id) FROM junk_decision")
    later = redecide.redecide(conn, [written], "junk", AT)
    with pytest.raises(review.Refused, match="has changed since"):
        redecide.undo(conn, act, AT)
    undo = redecide.undo(conn, later, AT)
    with pytest.raises(review.Refused, match="undone already"):
        redecide.undo(conn, later, AT)
    with pytest.raises(review.Refused, match="is an undo"):
        redecide.undo(conn, undo, AT)
    redecide.undo(conn, act, AT)
    assert deciding(conn, "S", FAR[0]) == "junk", "with the later act undone, the first is undone too"


@budget(load="db")
def test_where_lists_the_identities_whose_sightings_a_row_decides(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    seen(conn, "S", FAR[0] ^ 1, "c1")
    seen(conn, "T", FAR[0], "c7")
    seen(conn, "T", FAR[1], "c8")  # another unit
    decide(conn, "T", FAR[0], "keep")  # T's own keep decides its sighting, not the global
    units = where.decided_by(conn, present(store.get(conn, g)))
    assert units == [where.Unit("S", "c1", "block")], "the units the global decides now, by series and identity"
    assert where.decided_by(conn, present(store.get(conn, g)), ["T"]) == [], "--series narrows a global's search"


@budget(load="db")
def test_rows_lists_history_with_states_and_acts(site, tmp_path, capsys):
    cfg, conn = site
    row = decide(conn, "S", FAR[0], "junk")
    act = redecide.redecide(conn, [row], "keep", AT)
    path = str(config_file(tmp_path, cfg))
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "rows", "S"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].split("\t")[7:9] == [f"retired by act {act}", f"acts: retired {act}"], (
        "the old row: retired, its act"
    )
    assert lines[1].split("\t")[7:9] == ["active", f"acts: wrote {act}"], "the new row: active, written by the act"


@budget(load="db")
def test_redecide_carries_the_images_and_row_prints_them(site, tmp_path, capsys):
    cfg, conn = site
    row = decide(conn, "S", FAR[0], "junk")
    with db.transaction(conn):
        conn.execute("UPDATE junk_decision SET images = 'S-a-1' WHERE id = %s", (row,))
    kept = config.abs_path(cfg["paths"]["candidates"]) / candidates.DECIDED / "S-a-1"
    kept.mkdir(parents=True)
    redecide.redecide(conn, [row], "keep", AT)
    new = one(conn, "SELECT max(id) FROM junk_decision")
    assert one(conn, "SELECT images FROM junk_decision WHERE id = %s", (new,)) == "S-a-1", "the new row keeps them"
    path = str(config_file(tmp_path, cfg))
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "row", str(new)]) == 0
    assert str(kept / "unit.png") in capsys.readouterr().out, "junk row prints the kept images' paths"


@budget(load="db")
def test_redecide_refuses_an_unknown_a_twice_named_or_a_same_verdict_decision(site):
    _cfg, conn = site
    row = decide(conn, "S", FAR[0], "junk")
    for ids, verdict, why in (
        ([999], "keep", "no junk decision 999"),
        ([row, row], "keep", "named twice"),
        ([row], "junk", "is junk already"),
    ):
        with pytest.raises(review.Refused, match=why):
            redecide.redecide(conn, ids, verdict, AT)
    g = decide(conn, "", FAR[1], "junk", "import")
    with pytest.raises(review.Refused, match="series is named twice"):
        redecide.redecide_in(conn, [g], "keep", ["S", "S"], AT)
    with pytest.raises(review.Refused, match="--retire is for a global one"):
        redecide.retire_global(conn, [row], "keep", AT)


@budget(load="db")
def test_redecide_in_refuses_a_series_with_its_own_decision_on_the_key(site):
    _cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    own = decide(conn, "S", FAR[0], "junk")
    with pytest.raises(review.Refused, match=f"its own decision {own}"):
        redecide.redecide_in(conn, [g], "keep", ["S"], AT)


@budget(load="db")
def test_undo_refuses_an_unknown_act(site):
    _cfg, conn = site
    with pytest.raises(review.Refused, match="no junk act 999"):
        redecide.undo(conn, 999, AT)


@budget(load="db")
def test_the_commands_through_the_cli(site, tmp_path, capsys):
    cfg, conn = site
    g = decide(conn, "", FAR[0], "junk", "import")
    seen(conn, "S", FAR[0] ^ 1, "c1")
    path = str(config_file(tmp_path, cfg))
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "row", str(g), "--where"]) == 0
    assert "  S\tc1\tblock" in capsys.readouterr().out, "--where names the series, identity and kind"
    assert cli.main(["--config", path, "junk", "row", str(g), "--series", "S"]) == 2, "--series goes with --where"
    assert cli.main(["--config", path, "junk", "row", str(g), "--where", "--series", "Nope"]) == 2, "unknown series"
    assert cli.main(["--config", path, "junk", "rows", "Nope"]) == 2, "rows of an unknown series is refused"
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "redecide", str(g), "keep"]) == 0
    act = capsys.readouterr().out.split()[2].rstrip(":")
    assert cli.main(["--config", path, "junk", "undo", act]) == 0
    assert deciding(conn, "S", FAR[0]) == "junk", "the act is undone through the CLI"
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "rows"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2, "every decision, history included"


@budget(load="db")
def test_row_says_when_no_images_are_kept_or_they_are_missing(site, tmp_path, capsys):
    cfg, conn = site
    row = decide(conn, "S", FAR[0], "junk")
    path = str(config_file(tmp_path, cfg))
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "row", str(row)]) == 0
    assert "no images kept" in capsys.readouterr().out, "a decision with no images says so"
    with db.transaction(conn):
        conn.execute("UPDATE junk_decision SET images = 'gone-1' WHERE id = %s", (row,))
    assert cli.main(["--config", path, "junk", "row", str(row)]) == 0
    assert "images missing: gone-1" in capsys.readouterr().out, "a folder gone from the share is named"


CARD = '{"above":[{"ref":[["S","c",0]],"segments":[{"kind":"edge"}],"src":null}]}'  # a held block row's card


@budget(load="db")
def test_a_chained_block_redecided_keep_has_no_chain_and_undo_brings_it_back(site):
    _cfg, conn = site
    with db.transaction(conn):
        act = store.new_act(conn, "test", AT)
        row = store.write(
            conn,
            [
                {
                    "series_key": "S",
                    "kind": "block",
                    "key": key(FAR[0]),
                    "verdict": "junk",
                    "decider": "import",
                    "decided_at": AT,
                    "source": None,
                    "chain": CARD,
                }
            ],
            act,
        )[0]
    keep = redecide.redecide(conn, [row], "keep", AT)
    assert one(conn, "SELECT chain FROM junk_decision WHERE written_by = %s", (keep,)) is None, "a keep has no chain"
    redecide.undo(conn, keep, AT)
    assert one(conn, "SELECT chain FROM junk_decision WHERE id = %s AND retired_by IS NULL", (row,)) == CARD


@budget(load="db")
def test_redecide_refuses_a_cut_decision(site, tmp_path, capsys):
    cfg, conn = site
    cut: store.Decision = {
        "series_key": "S",
        "kind": "cut",
        "key": "c1d84387cf25a7a5|0.5",
        "verdict": "junk",
        "decider": "import",
        "decided_at": AT,
        "source": None,
        "chain": '{"below":[{"ref":[["S","c",0]],"segments":[{"h":0.5,"kind":"cut","tiles":"c1d84387cf25a7a5"}],'
        '"src":{"file":"a.cbz","sha256":"' + "ab" * 32 + '"}}]}',
    }
    with db.transaction(conn):
        row = store.write(conn, [cut], store.new_act(conn, "test", AT))[0]
    args = ["--config", str(config_file(tmp_path, cfg)), "junk", "redecide", str(row), "keep"]
    assert cli.main(args) == 2, "a cut decision is refused"
    assert "is a cut decision" in capsys.readouterr().err, "the refusal says why"
    with pytest.raises(review.Refused, match="is a cut decision"):
        redecide.redecide(conn, [row], "junk", AT)  # named before "junk already"


# ---------------------------------------------------------------- agreement: its reach and D12 (b) (plan phase-10-6)


def more_series(conn, *names):
    """Add series with these rules keys."""
    for name in names:
        series = add_series(conn, name)
        with db.transaction(conn):
            conn.execute("UPDATE series SET folder = %s WHERE id = %s", (f"/lib/{name}", series))


@budget(load="db")
def test_junk_row_where_lists_the_series_an_agreement_reaches(site, tmp_path, capsys):
    cfg, conn = site
    more_series(conn, "U")
    s, _t = decide(conn, "S", FAR[0], "junk"), decide(conn, "T", FAR[0] ^ 1, "junk")
    seen(conn, "S", FAR[0], "c1")
    seen(conn, "T", FAR[0], "c2")  # T's own decision decides it: no agreement in T
    seen(conn, "U", FAR[0] ^ 1, "c3")  # S and T agree in U
    seen(conn, "U", FAR[1], "c4")  # another unit
    want = [where.Unit("S", "c1", "block"), where.Unit("U", "c3", "block")]
    assert where.decided_by(conn, present(store.get(conn, s))) == want, "its series, and U where it agrees"
    path = str(config_file(tmp_path, cfg))
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "row", str(s), "--where"]) == 0
    assert "  U\tc3\tblock" in capsys.readouterr().out, "--where names the series an agreement reaches"


@budget(load="db")
def test_redecide_series_writes_a_decision_in_another_series_and_undo_retires_it(site):
    _cfg, conn = site
    more_series(conn, "U", "V")
    s = decide(conn, "S", FAR[0], "junk")
    decide(conn, "T", FAR[0], "junk")
    assert deciding(conn, "U", FAR[0]) == "junk", "S and T agree in U"
    act = redecide.redecide_in(conn, [s], "keep", ["U"], AT)
    assert deciding(conn, "U", FAR[0]) == "keep", "U opts out with a keep of its own"
    assert deciding(conn, "S", FAR[0]) == "junk", "S's decision stays"
    assert deciding(conn, "V", FAR[0]) is None, "U's keep vetoes the agreement everywhere"
    redecide.undo(conn, act, AT)
    assert deciding(conn, "U", FAR[0]) == "junk", "undo retires U's decision: the agreement is back"


@budget(load="db")
def test_redecide_series_refuses_the_rows_own_series_a_piece_and_a_series_holding_the_key(site):
    _cfg, conn = site
    more_series(conn, "U")
    s = decide(conn, "S", FAR[0], "junk")
    held = decide(conn, "U", FAR[0], "junk")
    with db.transaction(conn):
        store.upsert(
            conn,
            [
                {
                    "series_key": "S",
                    "kind": "piece",
                    "key": key(FAR[1]),
                    "verdict": "junk",
                    "decider": "claude",
                    "decided_at": AT,
                    "source": None,
                }
            ],
        )
    piece = one(conn, "SELECT max(id) FROM junk_decision")
    before = active(conn)
    for ids, series, why in (
        ([s], ["S"], "own: re-decide it without --series"),
        ([piece], ["T"], "agreement never decides one"),
        ([s], ["U"], f"its own decision {held}"),
    ):
        with pytest.raises(review.Refused, match=why):
            redecide.redecide_in(conn, ids, "keep", series, AT)
    assert active(conn) == before, "nothing is written"


@budget(load="db")
def test_redecide_series_refuses_2_rows_on_1_key_for_1_series(site):
    _cfg, conn = site
    more_series(conn, "U")
    s, t = decide(conn, "S", FAR[0], "junk"), decide(conn, "T", FAR[0], "junk")
    before = active(conn)
    with pytest.raises(review.Refused, match=f"junk decisions {s} and {t} are on 1 key"):
        redecide.redecide_in(conn, [s, t], "keep", ["U"], AT)
    assert active(conn) == before, "refused before any write"


# ---------------------------------------------------------------- A24: page keeps over a frame junk


PAGE_SIG = key(FAR[2])
FRAME = frames.key_text(frames.Frame(np.full(frames.CELLS * 3, 100, np.uint8), 1.42))


def page_chapter(conn, series_key, identity):
    """Add a waiting junk chapter of series series_key with a stored page scan: edge page 5 is PAGE_SIG and FRAME."""
    (series_id,) = conn.execute("SELECT id FROM series WHERE rules_key = %s", (series_key,)).fetchone()
    chapter = add_chapter(conn, series_id, identity, "junk", number=1)
    edges = [{"page": i, "sig": None, "frame": None} for i in (0, 1, 3, 4)]
    scan = {"layout": "pages", "split": False, "edges": [*edges, {"page": 5, "sig": PAGE_SIG, "frame": FRAME}]}
    with db.transaction(conn):
        conn.execute("UPDATE chapter SET status = 'waiting', waiting_on = 'junk_decision' WHERE id = %s", (chapter,))
        conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at, candidates)
            VALUES (%s, %s, 'd', %s, %s, '[]')""",
            (chapter, series_id, json.dumps(scan), AT),
        )
    return chapter


def write(conn, series_key, kind, key_text, verdict):
    """Write 1 decision; return its id."""
    with db.transaction(conn):
        store.upsert(
            conn,
            [
                {
                    "series_key": series_key,
                    "kind": kind,
                    "key": key_text,
                    "verdict": verdict,
                    "decider": "import" if series_key == store.GLOBAL else "claude",  # globals come from the import
                    "decided_at": AT,
                    "source": None,
                }
            ],
        )
    return one(conn, "SELECT max(id) FROM junk_decision")


def frame_candidate(conn, series_key, chapter):
    """Add an open frame candidate on FRAME that chapter waits on; return its id."""
    with db.transaction(conn):
        cid = one(
            conn,
            """INSERT INTO junk_candidate (series_key, kind, key, reason, folder, created_at)
            VALUES (%s, 'frame', %s, 'recurs', 'f', %s) RETURNING id""",
            (series_key, FRAME, AT),
        )
        conn.execute("UPDATE junk_scan SET candidates = %s WHERE chapter_id = %s", (json.dumps([cid]), chapter))
    return cid


@budget(load="db")
@pytest.mark.parametrize("keep_rest", [False, True], ids=["decide", "keep-rest"])
def test_decide_and_keep_rest_warn_of_page_keeps_overriding_a_frame_junk(site, keep_rest):
    cfg, conn = site
    chapter = page_chapter(conn, "S", "c1")
    keep = write(conn, "S", "page", PAGE_SIG, "keep")
    cid = frame_candidate(conn, "S", chapter)
    if keep_rest:
        review.listing(conn, cfg, "r1")
        _kept, got = review.decide_junk_keeping_rest(conn, [cid], "r1", AT)
    else:
        got = review.decide(conn, [cid], "junk", None, AT)
    frame = one(conn, "SELECT id FROM junk_decision WHERE kind = 'frame'")
    assert got == [review.Override(frame, keep, "S", chapter, 5)], "the page keep that ranks first on page 5"
    assert "junk redecide" in review.override_text(got[0]), "the warning says how to lift it"


@budget(load="db")
def test_junk_row_lists_the_page_keeps_overriding_a_global_frame_junk_in_every_series(site, tmp_path, capsys):
    cfg, conn = site
    more_series(conn, "U")
    chapters = {s: page_chapter(conn, s, f"c-{s}") for s in ("S", "T", "U")}
    keeps = {s: write(conn, s, "page", PAGE_SIG, "keep") for s in ("S", "T")}  # U has none: S and T agree there
    g = write(conn, "", "frame", FRAME, "junk")
    got = review.page_keeps_over(conn, present(store.get(conn, g)))
    want = [review.Override(g, keeps[s], s, chapters[s], 5) for s in ("S", "T")]
    want.append(review.Override(g, keeps["T"], "U", chapters["U"], 5))  # the agreement's stand-in: the newest keep
    assert got == want, "each series' own page keep, and in U the agreed keep: each ranks before a global junk"
    path = str(config_file(tmp_path, cfg))
    capsys.readouterr()
    assert cli.main(["--config", path, "junk", "row", str(g)]) == 0
    out = capsys.readouterr().out
    assert all(review.override_text(o) in out for o in want), out
