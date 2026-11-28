"""`junk show` (SPEC "Junk decisions", review): what a reviewer needs to decide 1 open candidate, as lines.

Its kind, series, reason and OCR text; where its opening occurrence sits and the removed ranges its context shows; the
decisions its reason cites; the chapters waiting on it (with the rows of the units matching it, for a strip block,
probe or piece); the chapters it was seen in; its image paths. Read at call time: no state of its own. A list longer
than 2 SHOWN items is cut to its first and last SHOWN, with its total.
"""

from collections.abc import Sequence

from manga_pipeline import config, db, images, strip
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, chapter_scan, strip_units
from manga_pipeline.stages import junk as junk_stage

SHOWN = 5  # a list's first and last items shown
STRIP_KINDS = ("block", "probe", "piece")  # the kinds shown with their rows in each waiting chapter


def lines(cfg: config.Config, conn: db.Connection, row: db.Row) -> list[str]:
    """Return the lines `junk show` prints for open candidate `row` (a junk_candidate row)."""
    folder = candidates.folder(cfg, row["folder"])
    info = candidates.read_info(folder)
    return [
        f"candidate {row['id']}: {row['kind']} of series {row['series_key']}",
        f"why: {row['reason']}",
        f"text: {info['text'] or '-'}",
        position_line(info),
        *removed_lines(info),
        *cited_lines(cfg, conn, info),
        waiting_line(conn, row),
        seen_line(conn, row),
        f"unit: {folder / 'unit.png'}",
        f"context: {folder / 'context.png'}",
    ]


def position_line(info: candidates.CandidateInfo) -> str:
    """Where the opening occurrence sits: its chapter and rows, page or box; `not recorded` for a folder before 10.8."""
    pos = info.get("position")
    if pos is None or "chapter" not in info:
        return "position: not recorded"
    parts = [f"chapter {info['chapter']}"]
    if "where" in pos:
        parts.append(pos["where"])
    if "page" in pos:
        parts.append(f"page {pos['page']}")
    if "box" in pos:
        parts.append("box " + ",".join(map(str, pos["box"])))
    if "rows" in pos:
        a, b = pos["rows"]
        parts.append(f"rows {a}:{b}" + (f" of {pos['height']}" if "height" in pos else ""))
    return "position: " + " ".join(parts)


def removed_lines(info: candidates.CandidateInfo) -> list[str]:
    """Return the removed ranges its context shows, labelled as its context labels them."""
    return [junk_stage.removed_label(r) for r in info.get("removed", [])]


def cited_lines(cfg: config.Config, conn: db.Connection, info: candidates.CandidateInfo) -> list[str]:
    """Return each decision its reason cites: id, series, verdict, decider, kept images, and whether it still stands.

    A decision is never deleted (a retired or superseded one keeps its row), so each cited id has its row.
    """
    out: list[str] = []
    for decision in info.get("cited", []):
        d = db.one_row(
            conn,
            """SELECT id, series_key, verdict, decider, images, retired_by, superseded_by
            FROM junk_decision WHERE id = %s""",
            (decision,),
        )
        scope = d["series_key"] or "global"
        kept = candidates.kept_images(cfg, d["images"])
        status = (
            f"retired by act {d['retired_by']}; "
            if d["retired_by"] is not None
            else f"superseded by decision {d['superseded_by']}; "
            if d["superseded_by"] is not None
            else ""
        )
        out.append(f"cited decision {d['id']} ({scope}): {d['verdict']} by {d['decider']}; {status}images: {kept}")
    return out


def waiting_line(conn: db.Connection, row: db.Row) -> str:
    """Return the chapters waiting on the candidate (candidates.WAITING_SCANS) by id.

    For a strip block, probe or piece, each shown chapter with the rows of its units matching the candidate; only the
    shown chapters' scans are read.
    """
    query = sql.SQL("SELECT js.chapter_id FROM {} AND js.candidates::jsonb @> to_jsonb(%s::bigint) ORDER BY 1")
    ids = [int(r[0]) for r in conn.execute(query.format(candidates.WAITING_SCANS), (row["id"],))]
    listed = shown(ids)
    rows: dict[int, list[strip.Rows]] = {}
    if row["kind"] in STRIP_KINDS:
        for r in conn.execute("SELECT chapter_id, scan FROM junk_scan WHERE chapter_id = ANY(%s)", (listed,)):
            sc = junk_stage.read_scan(r["scan"])
            if sc["layout"] == "strip":  # a piece cut inside a block at decide time is in no scan: no rows
                found = (
                    piece_units(sc, row["key"])
                    if row["kind"] == "piece"
                    else strip_units.matching_units(sc, row["key"])
                )
                rows[int(r["chapter_id"])] = found
    items = [f"{c}" + (" rows " + ",".join(f"{a}:{b}" for a, b in rows[c]) if rows.get(c) else "") for c in listed]
    return f"waiting chapters {len(ids)}: {joined(items, len(ids))}"


def piece_units(sc: strip_units.StripScan, key: str) -> list[strip.Rows]:
    """Return the scan's measured tail pieces whose signature matches key, sorted."""
    want = images.sig_from_str(key)
    out: list[strip.Rows] = []
    for span, m in sc["measured"].items():
        if (sig := m["sig"]) is not None and images.matches(want, images.sig_from_str(sig)):
            a, b = span.split(":")
            out.append((int(a), int(b)))
    return sorted(out)


def seen_line(conn: db.Connection, row: db.Row) -> str:
    """Return how many chapters of the series showed a matching unit, with their ids; a kind junk_seen records."""
    if row["kind"] not in chapter_scan.SEEN_KINDS:
        return f"seen: not recorded for a {row['kind']}"
    identities = candidates.identities_showing(conn, row["series_key"], row["kind"], row["key"])
    held = dict(
        conn.execute(
            """SELECT ch.identity, max(ch.id) FROM chapter ch JOIN series s ON s.id = ch.series_id
            WHERE s.rules_key = %s AND ch.identity = ANY(%s) AND ch.download_path IS NOT NULL
            AND ch.content_digest IS NOT NULL GROUP BY ch.identity""",
            (row["series_key"], sorted(identities)),
        ).fetchall()
    )
    ids = sorted(int(held[i]) for i in identities if i in held)
    none = len(identities) - len(ids)
    return f"seen in {len(identities)} chapters: {joined([str(i) for i in shown(ids)], len(ids))}" + (
        f"; {none} with no chapter" if none else ""
    )


def shown(items: Sequence[int]) -> list[int]:
    """Return the items a list shows: all of them, or its first and last SHOWN when it has more than 2 SHOWN."""
    return list(items) if len(items) <= 2 * SHOWN else [*items[:SHOWN], *items[-SHOWN:]]


def joined(items: Sequence[str], total: int) -> str:
    """Return a list's shown items (shown) joined, how many of its `total` are left out between its first and last."""
    if not items:
        return "none"
    if total == len(items):
        return ", ".join(items)
    return ", ".join([*items[:SHOWN], f"... {total - len(items)} more ...", *items[SHOWN:]])
