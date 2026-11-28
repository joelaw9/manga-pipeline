"""`junk show` (SPEC "Junk decisions", review): what a reviewer needs to decide 1 open candidate, as lines.

Its kind, series, reason and OCR text; where its opening occurrence sits and the removed ranges its context shows; the
decisions its reason cites; the chapters waiting on it (with the rows of the units matching it, for a strip block,
probe or piece); the chapters it was seen in; its image paths. Read at call time: no state of its own. A list longer
than 2 SHOWN items is cut to its first and last SHOWN, with its total.
"""

from collections.abc import Sequence

from manga_pipeline import config, db, images, strip
from manga_pipeline.junk import candidates, strip_units
from manga_pipeline.stages import junk as junk_stage

SHOWN = 5  # a list's first and last items shown
SEEN_KINDS = ("block", "page", "frame", "band")  # the kinds junk_seen records (chapter_scan.seen_units)


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
    """Return the removed ranges its context shows, each with its decision or its rule."""
    return [
        f"removed {r['start']}:{r['end']} " + (f"decision {r['decision']}" if "decision" in r else r["why"])
        for r in info.get("removed", [])
    ]


def cited_lines(cfg: config.Config, conn: db.Connection, info: candidates.CandidateInfo) -> list[str]:
    """Each decision its reason cites: id, series, verdict, decider and its kept images (or why there are none)."""
    out: list[str] = []
    for decision in info.get("cited", []):
        d = conn.execute(
            "SELECT id, series_key, verdict, decider, images FROM junk_decision WHERE id = %s", (decision,)
        ).fetchone()
        if d is None:
            out.append(f"cited decision {decision}: gone")
            continue
        scope = d["series_key"] or "global"
        kept = candidates.kept_images(cfg, d["images"])
        out.append(f"cited decision {d['id']} ({scope}): {d['verdict']} by {d['decider']}; images: {kept}")
    return out


def waiting_line(conn: db.Connection, row: db.Row) -> str:
    """Return the chapters waiting on the candidate by id, with the rows of its matching units (strip units only)."""
    scans = conn.execute(
        "SELECT chapter_id, scan FROM junk_scan WHERE candidates::jsonb @> to_jsonb(%s::bigint) ORDER BY chapter_id",
        (row["id"],),
    ).fetchall()
    items = [_waiting_item(int(s["chapter_id"]), s["scan"], row["kind"], row["key"]) for s in scans]
    return f"waiting chapters {len(items)}: {_bounded(items)}"


def _waiting_item(chapter: int, scan_text: str, kind: str, key: str) -> str:
    sc = junk_stage.read_scan(scan_text)
    if sc["layout"] != "strip" or kind not in ("block", "probe", "piece"):
        return str(chapter)
    rows = (
        piece_units(sc, key) if kind == "piece" else strip_units.matching_units(sc, key)
    )  # a piece cut inside a block at decide time is in no scan: listed without rows
    return f"{chapter}" + (" rows " + ",".join(f"{a}:{b}" for a, b in rows) if rows else "")


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
    if row["kind"] not in SEEN_KINDS:
        return f"seen: not recorded for a {row['kind']}"
    identities = candidates.identities_showing(conn, row["series_key"], row["kind"], row["key"])
    held = dict(
        conn.execute(
            """SELECT ch.identity, max(ch.id) FROM chapter ch JOIN series s ON s.id = ch.series_id
            WHERE s.rules_key = %s AND ch.identity = ANY(%s) AND ch.download_path IS NOT NULL
            GROUP BY ch.identity""",
            (row["series_key"], sorted(identities)),
        ).fetchall()
    )
    ids = sorted(int(held[i]) for i in identities if i in held)
    none = len(identities) - len(ids)
    return f"seen in {len(identities)} chapters: {_bounded([str(i) for i in ids])}" + (
        f"; {none} with no chapter" if none else ""
    )


def _bounded(items: Sequence[str]) -> str:
    """Return items joined, cut to the first and last SHOWN with how many are left out between."""
    if not items:
        return "none"
    if len(items) <= 2 * SHOWN:
        return ", ".join(items)
    return ", ".join([*items[:SHOWN], f"... {len(items) - 2 * SHOWN} more ...", *items[-SHOWN:]])
