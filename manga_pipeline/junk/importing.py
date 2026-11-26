"""Junk decisions from a bulk-converter series record's decision rows (SPEC "Junk decisions", import).

The baseline reads a record's own decision rows (record_rows) and stores them as `import` decisions; the record is
never written. The bulk converter's store was imported once and is read no more.
"""

import base64
from collections.abc import Iterable, Sequence

from manga_pipeline import images
from manga_pipeline.convert import record_reader
from manga_pipeline.junk import frames, marks, store

VERDICT = {"approve": "junk", "keep": "keep"}  # the bulk converter's decision -> ours
BUNDLE_PREFIX = "rn"  # a bulk-converter review id of a rest-of-card bundle of more than 1 piece (junk.bundle)
UNIT_ORIGINS = ("block", "probe", "piece", "page", "band")  # the bulk converter's origins imported (not cut)


class SourceError(ValueError):
    """A record decision row cannot be imported."""


def bulk_time(added: str) -> str:
    """Return the bulk converter's `added` (SQLite CURRENT_TIMESTAMP, UTC) as our time text (workflow.now's format)."""
    return added.replace(" ", "T") + "+00:00"


def winners(rows: Iterable[store.Decision]) -> list[store.Decision]:
    """Pick, of several source rows on 1 (series key, kind, key), the one the bulk converter obeyed.

    A keep before a junk, then the newest. rows: dicts as upsert takes; returns them in input order, 1 per key.
    """
    best: dict[tuple[str, str, str], tuple[tuple[bool, str], store.Decision]] = {}
    for r in rows:
        k = (r["series_key"], r["kind"], r["key"])
        rank = (r["verdict"] == "keep", r["decided_at"])
        if k not in best or rank >= best[k][0]:
            best[k] = (rank, r)
    return [r for _, r in best.values()]


def signable(rows: Sequence[store.Decision]) -> tuple[list[store.Decision], int]:
    """Return (the rows a decision may be written from, how many were left out: on an unsignable key)."""
    kept = [r for r in rows if not store.unsignable(r["kind"], r["key"])]
    return kept, len(rows) - len(kept)


def logo_key(png: bytes, size: Sequence[int], source: str) -> str:
    """Return a logo decision's key from a bulk-converter template PNG (marks.Template.key_text).

    SourceError when it is no template or its size is not the recorded (w, h).
    """
    try:
        template = marks.Template.from_png(png)
    except (OSError, ValueError) as e:
        msg = f"{source}: template is not a logo template: {e}"
        raise SourceError(msg) from None
    if template.size != tuple(size):
        msg = f"{source}: template is {template.size}, recorded as {tuple(size)}"
        raise SourceError(msg)
    return template.key_text()


def _row(series: str | None, kind: str, key: str, r: record_reader.RecordDecisionRow, source: str) -> store.Decision:
    if r["decision"] not in VERDICT:
        msg = f"{source}: decision {r['decision']!r} is neither approve nor keep"
        raise SourceError(msg)
    return {
        "series_key": store.GLOBAL if series is None else series,
        "kind": kind,
        "key": key,
        "verdict": VERDICT[r["decision"]],
        "decider": "import",
        "decided_at": bulk_time(r["added"]),
        "source": source,
    }


def record_rows(record: record_reader.Record) -> list[store.Decision]:
    """Return the rows to upsert from a bulk-converter series record.

    Schema 4 carries the decision rows its runs used (series.decisions.rows; a marks row names its template in
    series.decisions.templates); a schema-3 record carries none it can give. Cut rows
    are left out and chains dropped. A junk row of a bundle's review id ("rn...") carries the bundle mark. RecordError
    on a malformed row.
    """
    if record["schema"] != record_reader.SCHEMA:
        return []
    try:
        block = record_reader.decisions(record)
        junk, frame_rows = block["rows"]["junk"], block["rows"]["frames"]
        out: list[store.Decision] = []
        for r in junk:  # its cut rows are left out by the reader
            if r["origin"] not in UNIT_ORIGINS:
                msg = f"origin {r['origin']!r}"
                raise SourceError(msg)
            tiles, aspect = r["key"]
            sig = images.sig_to_str((tuple(int(t, 16) for t in tiles.split(":")), float(aspect)))
            row = _row(r["series"], r["origin"], sig, r, f"record junk row {r['row']}")
            row["bundle"] = r["review_id"] is not None and r["review_id"].startswith(BUNDLE_PREFIX)
            out.append(row)
        for r in frame_rows:
            vec, aspect = r["key"]
            key = frames.key_text(frames.from_bytes(base64.b64decode(vec["b64"]), aspect))
            out.append(_row(r["series"], "frame", key, r, f"record frames row {r['row']}"))
        for r in block["rows"]["marks"]:
            tpl, w, h = r["key"]
            source = f"record marks row {r['row']}"
            png = base64.b64decode(block["templates"][tpl["tpl"]], validate=True)
            out.append(_row(r["series"], "logo", logo_key(png, (w, h), source), r, source))
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        why = f"{type(e).__name__}: {e}"
        msg = f"record series.decisions.rows: malformed row ({why})"
        raise record_reader.RecordError(msg) from None
    return out
