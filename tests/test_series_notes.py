"""status's "Series notes" (SPEC "Runtime", status): layout claims of a series' own genres, and open calibration."""

import json
from contextlib import closing

import pytest

from manga_pipeline import db, status
from manga_pipeline.metadata import records

from tests.builders import add_series, site_cfg
from tests.timing import budget


@pytest.fixture
def conn(tmp_path):
    with closing(db.connect(site_cfg(tmp_path))) as c:
        yield c


def series(conn, name, layout, mu_type):
    """Add a series stored with this layout whose MangaUpdates record gives this type (None: no record)."""
    series_id = add_series(conn, name)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, %s, 'computed')", (series_id, layout)
        )
        if mu_type is not None:
            source, kind = records.STORED["mangaupdates"]
            conn.execute(
                "INSERT INTO fetched (series_id, source, kind, payload, payload_hash, fetched_at)"
                " VALUES (%s, %s, %s, %s, 'x', '2026-11-26')",
                (series_id, source, kind, json.dumps({"type": mu_type})),
            )
    return series_id


def notes(conn):
    lines = status.report(conn)
    start = lines.index("Series notes:") + 1
    return [line for line in lines[start:] if line.startswith("  series ")]


@budget(load="db")
def test_a_series_stored_as_pages_whose_genres_claim_a_long_strip_is_listed(conn):
    manhwa = series(conn, "Paged Manhwa", "pages", "Manhwa")
    series(conn, "Paged Manga", "pages", "Manga")
    series(conn, "Strip Manga", "strip", "Manga")
    series(conn, "Strip Manhwa", "strip", "Manhwa")
    series(conn, "No Records", "pages", None)
    assert notes(conn) == [
        f"  series {manhwa} (Paged Manhwa): stored as pages, but its stored origin claims strip (Manhwa)"
    ], "D8 (b): only a strip claim against a series stored as pages"


@budget(load="db")
def test_open_computed_calibration_groups_are_listed_and_fixed_ones_are_not(conn):
    open_id = series(conn, "Open", "pages", None)
    fixed_id = series(conn, "Fixed", "pages", None)
    with db.transaction(conn):
        for series_id, fixed, why in ((open_id, 0, "open: 12 samples"), (fixed_id, 1, "q65 fails")):
            conn.execute(
                "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)"
                " VALUES (%s, 'colour', 80, %s, 'computed', %s, '[]')",
                (series_id, fixed, why),
            )
    assert notes(conn) == [f"  series {open_id} (Open): colour calibration open at q80 (open: 12 samples)"]


@budget(load="db")
def test_no_series_note_reads_none(conn):
    lines = status.report(conn)
    assert lines[lines.index("Series notes:") + 1] == "  none"
