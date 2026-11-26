"""Chains on junk decisions (SPEC "Junk decisions", decisions): the format and which decisions carry one."""

import json
from contextlib import closing

import pytest

from manga_pipeline import db
from manga_pipeline.junk import chains, store

from tests.builders import adopted, one
from tests.timing import budget

AT = "2026-11-25T00:00:00+00:00"
TILES = "c1d84387cf25a7a5"  # a held cut row's key tile (signable)
OTHER = "0f0f0f0f0f0f0f0f"
SRC = {"file": "Gamma_Chapter 1.cbz", "sha256": "ab" * 32}
REF = [["S", "Gamma_Chapter 1.cbz", 0]]


def seg(kind="cut", h=0.6778, tiles=TILES, **extra):
    return {"kind": kind, "h": h, "tiles": tiles, **extra}


def version(*segments, src=None, ref=REF):
    return {"segments": list(segments), "ref": ref, "src": src}


EDGE_CARD = chains.text({"above": [version({"kind": "edge"})]})  # 17 held block rows' shape
CUT = chains.text({"below": [version(seg(), seg("keep", 0.1, OTHER), src=SRC)]})
EXACT_CUT = chains.text({"above": [version(seg(exact=True), seg("keep", 0.2, OTHER), src=SRC)]})  # rows 2882-2897
CUT_KEY = f"{TILES}|0.6778"


@budget(load="pure")
@pytest.mark.parametrize("stored", [EDGE_CARD, CUT, EXACT_CUT])
def test_a_canonical_chain_parses_and_prints_back_the_same(stored):
    again = json.dumps(chains.parse(stored), sort_keys=True, separators=(",", ":"))
    assert again == stored, "parse keeps every field; the text is canonical"


@budget(load="pure")
@pytest.mark.parametrize(
    ("value", "match"),
    [
        ({"above": [version(seg())], "none": [["S", "c", 0, 100, "above"]]}, "none exemptions are refused"),
        ({"above": [version({"kind": "skip", "min": 0.1, "max": 0.2}, seg())]}, "skip segments are refused"),
        ({"above": [version({"kind": "edge"}, seg())]}, "edge is the last segment only"),
        ({"above": [version(seg(exact=True))]}, "exact segment is not followed"),
        ({"above": [version(seg(exact=True), {"kind": "edge"})]}, "exact segment is not followed"),
        ({"above": [version(seg("keep"))]}, "holds no cut or edge"),
        ({"above": [version(seg(tiles="C1D84387CF25A7A5"))]}, "lowercase 16-hex"),
        ({"above": [version(seg(h=1))]}, "finite float > 0"),
        ({"above": [version(seg(h=0.12345))]}, "at most 4 decimals"),
        ({"above": [version(seg(), ref=[["S", "c", -1]])]}, "anchor start >= 0"),
        ({"above": [version(seg(), src={"file": "a.cbz", "sha256": "x"})]}, "sha256: 64 lowercase hex"),
        ({"above": [version(seg()), version(seg())]}, "repeats a version"),
        ({"left": [version(seg())]}, "keys are not 1 or 2 of"),
        ({}, "keys are not 1 or 2 of"),
        ({"above": [version(seg(extra=1))]}, "not {kind: cut|keep"),
        ({"above": [version(seg(exact=False), seg("keep"))]}, "exact is true or absent"),
        ({"above": []}, "not a non-empty list of versions"),
        ({"above": "x"}, "not a non-empty list of versions"),
        ({"above": [version(seg(), src={"sha256": "ab" * 32})]}, "sha256: 64 lowercase hex"),
        ({"above": [version(seg(), ref=[["S", "c"]])]}, "anchor start >= 0"),
    ],
)
def test_a_chain_breaking_the_format_is_refused(value, match):
    with pytest.raises(ValueError, match=match):
        chains.parse(chains.text(value))


@budget(load="pure")
def test_a_chain_not_in_canonical_text_or_not_json_is_refused():
    with pytest.raises(ValueError, match="not canonical"):
        chains.parse(EDGE_CARD.replace(":", ": ", 1))
    with pytest.raises(ValueError, match="not JSON"):
        chains.parse("{")


@budget(load="pure")
def test_a_chain_that_is_no_object_is_refused():
    with pytest.raises(ValueError, match="not an object"):
        chains.parse("[]")


@budget(load="pure")
def test_a_uniform_images_tiles_are_refused():
    uniform = "0" * 16  # unsignable: it matches any blank or flat rows
    with pytest.raises(ValueError, match="uniform"):
        chains.parse(chains.text({"above": [version(seg(tiles=uniform))]}))


@budget(load="pure")
def test_a_cut_key_is_its_first_cut_segment_as_signature_text():
    assert chains.key_span(chains.check_cut(CUT_KEY, CUT)) == CUT_KEY


def decision(**changes):
    return {
        "series_key": "S",
        "kind": "block",
        "key": f"{OTHER}|0.7",
        "verdict": "junk",
        "decider": "import",
        "decided_at": AT,
        "source": None,
        **changes,
    }


@pytest.fixture
def conn(tmp_path):
    with closing(db.connect(adopted(tmp_path))) as c:
        yield c


def written(conn, row):
    with db.transaction(conn):
        act = store.new_act(conn, "test", AT)
        return store.write(conn, [row], act)[0]


@budget(load="db")
def test_a_chained_block_and_a_cut_decision_are_stored_with_chain_and_credit(conn):
    block = written(conn, decision(chain=EDGE_CARD))
    cut = written(conn, decision(kind="cut", key=CUT_KEY, chain=CUT, credit=True))
    written(conn, decision(kind="probe", chain=EDGE_CARD))  # a probe decision may carry a card too
    assert one(conn, "SELECT chain FROM junk_decision WHERE id = %s", (block,)) == EDGE_CARD
    assert one(conn, "SELECT credit FROM junk_decision WHERE id = %s", (cut,)) is True
    assert one(conn, "SELECT credit FROM junk_decision WHERE id = %s", (block,)) is False, "credit defaults to false"


@budget(load="db")
@pytest.mark.parametrize(
    ("row", "match"),
    [
        (decision(kind="cut", key=CUT_KEY, chain=CUT, verdict="keep"), "series-scoped, junk"),
        (decision(kind="cut", key=CUT_KEY, chain=CUT, series_key=store.GLOBAL), "series-scoped, junk"),
        (decision(kind="cut", key=CUT_KEY), "series-scoped, junk"),
        (decision(kind="cut", key=f"{OTHER}|0.6778", chain=CUT), "not the chain's key span"),
        (decision(kind="cut", key=CUT_KEY, chain=chains.text({"below": [version(seg())]})), "without a source"),
        (
            decision(
                kind="cut", key=CUT_KEY, chain=chains.text({"below": [version(seg(), {"kind": "edge"}, src=SRC)]})
            ),
            "edge segment in a cut chain",
        ),
        (
            decision(
                kind="cut",
                key=CUT_KEY,
                chain=chains.text({"above": [version(seg(), src=SRC)], "below": [version(seg(), src=SRC)]}),
            ),
            "of 2 sides",
        ),
        (decision(credit=True), "credit on a block"),
        (decision(kind="cut", key=CUT_KEY, chain=CUT, credit=1), "not a bool"),
        (decision(chain=EDGE_CARD, verdict="keep"), "no series block or probe junk"),
        (decision(chain=EDGE_CARD, series_key=store.GLOBAL), "no series block or probe junk"),
        (decision(chain=EDGE_CARD, kind="piece"), "no series block or probe junk"),
        (decision(chain=chains.text({"above": [version(seg(), src=SRC)]})), "with a source"),
    ],
)
def test_a_decision_breaking_the_row_shapes_is_refused_and_nothing_is_written(conn, row, match):
    with pytest.raises(ValueError, match=match):
        written(conn, row)
    assert one(conn, "SELECT count(*) FROM junk_decision") == 0, "the act's transaction wrote nothing"


@budget(load="db")
def test_an_import_with_another_chain_or_credit_supersedes_the_same_import(conn):
    """An import on a key: the same row is unchanged; a chain or credit that differs supersedes it (store._keeps)."""
    other = chains.text({"below": [version(seg(), src=SRC), version(seg(), seg("keep", 0.3, OTHER), src=SRC)]})

    def row(chain: str, *, credit: bool) -> store.Decision:
        return {
            "series_key": "S",
            "kind": "cut",
            "key": CUT_KEY,
            "verdict": "junk",
            "decider": "import",
            "decided_at": AT,
            "source": None,
            "chain": chain,
            "credit": credit,
        }

    with db.transaction(conn):
        store.upsert(conn, [row(CUT, credit=False)])
        assert store.upsert(conn, [row(CUT, credit=False)]) == {"inserted": 0, "updated": 0, "unchanged": 1}
        assert store.upsert(conn, [row(CUT, credit=True)])["updated"] == 1, "credit differs"
        assert store.upsert(conn, [row(other, credit=True)])["updated"] == 1, "chain differs"
