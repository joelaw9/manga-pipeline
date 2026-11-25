"""`junk redecide`, `junk undo`, `junk rows` and `junk row --where` (SPEC "Junk decisions", decision commands).

Decisions are rows of junk_decision; sightings rows of junk_seen; series carry their rules key (their first folder's
name).
"""

from contextlib import closing

import pytest

from manga_pipeline import cli, config, db
from manga_pipeline.junk import candidates, redecide, review, store, where

from tests.builders import add_series, adopted, config_file, one
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
    with pytest.raises(review.Refused, match="are for global"):
        redecide.redecide_in(conn, [s], "keep", ["T"], AT)
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
    units = where.decided_by(conn, store.get(conn, g))
    assert units == [where.Unit("S", "c1", "block")], "the units the global decides now, by series and identity"
    assert where.decided_by(conn, store.get(conn, g), ["T"]) == [], "--series narrows a global's search"


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
