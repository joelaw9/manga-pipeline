"""The bulk converter's series record, `.optimizer.json` (SPEC "State", baseline): read and validated, never written.
A record that parses is a finished series. Copied from the bulk converter's optimizer/record.py (`load`, `parse`,
`check`); the check of the schema-4 junk decision rows comes with Phase 5, which imports them.
"""

import json

RECORD_NAME = ".optimizer.json"
SCHEMA, FROZEN = 4, 3  # schema 3 is read too; its decisions are history
STAGES = ("structure", "image", "naming")  # a record written with fewer stages is partial
TOP = {
    "schema": int,
    "stages": list,
    "kavita": dict,
    "folders": list,
    "runs": list,
    "errors": list,
    "flags": list,
    "series": dict,
    "sources": list,
    "chapters": list,
}
SERIES = ("layout", "naming", "calibration", "covers", "decisions")
CHAPTER_KEYS = ("output", "file", "sources", "kind", "volume", "volume_source", "number", "title", "pages_out")
# source fates
CHAPTER, MERGED, DUPLICATE, DELETED, REPLACED = "chapter", "merged", "duplicate", "deleted", "replaced"
CARRIED, FAILED = "carried", "failed"
FATES = (CHAPTER, MERGED, DUPLICATE, DELETED, REPLACED, CARRIED, FAILED)


class RecordError(ValueError):
    """The series record cannot be used."""


class PartialRecord(RecordError):
    """A record written with partial --stages: the series is not finished."""


def load(folder):
    """The parsed record of a series folder. FileNotFoundError when it has none; RecordError when it does not parse."""
    path = folder / RECORD_NAME
    text = path.read_text(encoding="utf-8")
    try:
        record = json.loads(text)
    except json.JSONDecodeError as e:
        raise RecordError(f"{path}: not JSON: {e}") from None
    return parse(record)


def parse(rec):
    """Validate a record dict and return it. Raises PartialRecord or RecordError."""
    check(rec)
    missing = sorted(set(STAGES) - set(rec["stages"]))
    if missing:
        raise PartialRecord(f"record written without stage(s) {missing}")
    return rec


def check(rec):
    """The structural validation of a record: schema 3 or 4, every top key and series block, runs, folders, the
    naming restart flag, the calibration groups' shape, unique outputs and chapter files, and sources that name
    existing outputs. Raises RecordError."""
    if not isinstance(rec, dict):
        raise RecordError(f"record is a {type(rec).__name__}, not an object")
    if rec.get("schema") not in (FROZEN, SCHEMA) or isinstance(rec.get("schema"), bool):
        raise RecordError(f"record schema {rec.get('schema')!r}: only schema {FROZEN} and {SCHEMA} are read")
    for k, t in TOP.items():
        if not isinstance(rec.get(k), t):
            raise RecordError(f"record: {k!r} missing or not a {t.__name__}")
    for k in SERIES:
        if not isinstance(rec["series"].get(k), dict):
            raise RecordError(f"record: series.{k} missing or not an object")
    if not rec["runs"]:
        raise RecordError("record: no runs")
    if not rec["folders"] or any(
        not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("from_run"), str)
        for f in rec["folders"]
    ):
        raise RecordError("record: folders missing or not [{name, from_run}]")
    naming = rec["series"]["naming"]
    if not isinstance(naming.get("restart"), bool):
        raise RecordError("record: series.naming.restart missing or not a bool")
    if not isinstance(naming.get("series_name"), str) or not naming["series_name"]:
        raise RecordError("record: series.naming.series_name missing or empty")
    groups = rec["series"]["calibration"].get("groups", {})
    if not isinstance(groups, dict) or any(
        not isinstance(g, dict)
        or type(g.get("q")) is not int
        or not 0 <= g["q"] <= 100
        or not isinstance(g.get("probes", []), list)
        for g in groups.values()
    ):
        raise RecordError("record: series.calibration.groups is not {group: {q: integer 0-100, probes: list}}")
    outputs, files = {}, set()
    for c in rec["chapters"]:
        absent = [k for k in CHAPTER_KEYS if k not in c]
        if absent:
            raise RecordError(f"record: chapter {c.get('output')!r} lacks {absent}")
        low = c["output"].lower()
        if low in outputs:
            raise RecordError(f"record: output {c['output']!r} listed twice")
        outputs[low] = c
        if c["file"] in files:
            raise RecordError(f"record: chapter file {c['file']!r} listed twice")
        files.add(c["file"])
    seen = set()
    for s in rec["sources"]:
        for k in ("file", "sha256", "fate"):
            if not isinstance(s.get(k), str):
                raise RecordError(f"record: source {s.get('file')!r}: {k!r} missing")
        if s["fate"] not in FATES:
            raise RecordError(f"record: source {s['file']!r}: unknown fate {s['fate']!r}")
        if ("trash_run" in s) != ("trash_folder" in s) or (s.get("trash_run") is None) != (
            s.get("trash_folder") is None
        ):
            raise RecordError(f"record: source {s['file']!r}: trash_run and trash_folder go together")
        if (s["file"], s["sha256"]) in seen:
            raise RecordError(f"record: source {s['file']!r} listed twice")
        seen.add((s["file"], s["sha256"]))
        if s["fate"] in (CHAPTER, MERGED) and (s.get("output") or "").lower() not in outputs:
            raise RecordError(f"record: source {s['file']!r} names output {s.get('output')!r}, not a chapter")
    by_name = {}
    for s in rec["sources"]:
        by_name.setdefault(s["file"], []).append(s)
    for c in rec["chapters"]:
        for f in c["sources"]:
            if not any(s["fate"] in (CHAPTER, MERGED) and s.get("output") == c["output"] for s in by_name.get(f, [])):
                raise RecordError(f"record: chapter {c['output']!r}: source {f!r} not in sources")
