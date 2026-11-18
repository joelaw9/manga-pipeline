"""The bulk converter's series record, `.optimizer.json` (SPEC "State", baseline): read and validated, never written.

A record that parses is a finished series. Copied from the bulk converter's optimizer/record.py (`load`, `parse`,
`check`); the check of the schema-4 junk decision rows comes with Phase 5, which imports them.
"""

import json
from pathlib import Path
from typing import NotRequired, TypedDict

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
QUALITY_MAX = 100  # the top calibration quality


class RecordFolder(TypedDict):
    """A folder the series had: its name and the run that named it (checked)."""

    name: str
    from_run: str


class RecordLayout(TypedDict):
    """series.layout: the series' layout ("strip" or "pages"; not checked) and its page aspects (h / w)."""

    layout: str
    page_aspect: NotRequired[list[float]]


class RecordNaming(TypedDict):
    """series.naming: the series name and the restart flag (checked), and where a new chapter's volume comes from."""

    series_name: str
    restart: bool
    basis: NotRequired[str]


class CalibrationGroup(TypedDict):
    """A calibration group: its quality (an integer 0-100, checked), its probes and why it was chosen."""

    q: int
    probes: NotRequired[list[object]]
    why: NotRequired[str]


class RecordCalibration(TypedDict):
    """series.calibration: {encode group: its calibration} (checked when present)."""

    groups: NotRequired[dict[str, CalibrationGroup]]


class CoverRef(TypedDict):
    """A volume cover's place: the output holding it (not checked)."""

    chapter: str


class RecordCovers(TypedDict):
    """series.covers: {volume: its cover's place} (not checked)."""

    volumes: dict[str, CoverRef]


class RecordDecisionRow(TypedDict):
    """A junk decision row a run used: its row number, series (None: global), decision, the time it was added.

    decision: "approve" or "keep"; added: SQLite CURRENT_TIMESTAMP text (UTC).
    """

    row: int
    series: str | None
    decision: str
    added: str


class RecordJunkRow(RecordDecisionRow):
    """A junk table row: its origin and its key, the JSON array [tiles text, aspect] read by position."""

    origin: str
    key: tuple[str, float]


class FrameVector(TypedDict):
    """A frame's stored vector, base64."""

    b64: str


class RecordFrameRow(RecordDecisionRow):
    """A frames table row: its key, the JSON array [vector, aspect] read by position."""

    key: tuple[FrameVector, float]


class TemplateRef(TypedDict):
    """A marks row's template: its name in series.decisions.templates."""

    tpl: str


class RecordMarkRow(RecordDecisionRow):
    """A marks table row: its key, the JSON array [template, width, height] read by position."""

    key: tuple[TemplateRef, int, int]


class DecisionRows(TypedDict):
    """The decision rows the runs used, by the bulk converter's table."""

    junk: list[RecordJunkRow]
    frames: list[RecordFrameRow]
    marks: list[RecordMarkRow]


class DecisionBlock(TypedDict):
    """series.decisions of a schema-4 record: its decision rows and {template name: RGBA PNG, base64}."""

    rows: DecisionRows
    templates: dict[str, str]


class RecordSeries(TypedDict):
    """The record's series blocks (each an object, checked).

    decisions: the junk decision rows the runs used; not checked, so object (decisions reads it as a DecisionBlock).
    """

    layout: RecordLayout
    naming: RecordNaming
    calibration: RecordCalibration
    covers: RecordCovers
    decisions: object


class RecordSource(TypedDict):
    """A source file: its name, sha256, fate (checked), output, content digest and trash place (together, checked)."""

    file: str
    sha256: str
    fate: str
    output: NotRequired[str | None]
    pages_digest: NotRequired[str]
    trash_run: NotRequired[str | None]
    trash_folder: NotRequired[str | None]


class RecordRange(TypedDict):
    """Strip rows [start, end) a reshape removed, and why."""

    start: int
    end: int
    why: str


class RecordReshape(TypedDict, total=False):
    """A chapter's reshape: the strip rows it removed."""

    removed: list[RecordRange]


class RecordEdits(TypedDict, total=False):
    """A chapter's edits (not checked): its dropped pages and its reshape (None: not reshaped)."""

    drops: list[object]
    reshape: RecordReshape | None


class RecordChapter(TypedDict):
    """A chapter (CHAPTER_KEYS checked present; values not checked) and its edits (an older record keeps none)."""

    output: str
    file: str
    sources: list[str]
    kind: str
    volume: int | float | None
    volume_source: str | None
    number: str | None
    title: str
    pages_out: int
    edits: NotRequired[RecordEdits]


class Record(TypedDict):
    """A bulk-converter series record (`.optimizer.json`): every top key of its type (TOP, checked).

    kavita, runs, errors and flags are not read beyond their type.
    """

    schema: int
    stages: list[str]
    kavita: dict[str, object]
    folders: list[RecordFolder]
    runs: list[object]
    errors: list[object]
    flags: list[object]
    series: RecordSeries
    sources: list[RecordSource]
    chapters: list[RecordChapter]


class RecordError(ValueError):
    """The series record cannot be used."""


class PartialRecordError(RecordError):
    """A record written with partial --stages: the series is not finished."""


def load(folder: Path) -> Record:
    """Return the parsed record of a series folder.

    FileNotFoundError when it has none; RecordError when it does not parse.
    """
    path = folder / RECORD_NAME
    text = path.read_text(encoding="utf-8")
    try:
        record = json.loads(text)
    except json.JSONDecodeError as e:
        msg = f"{path}: not JSON: {e}"
        raise RecordError(msg) from None
    return parse(record)


def decisions(record: Record) -> DecisionBlock:
    """Return a record's series.decisions block as a DecisionBlock, copied through JSON.

    Unchecked: check does not read the block; a malformed one fails where it is read.
    """
    return json.loads(json.dumps(record["series"]["decisions"]))


def parse(rec):
    """Validate a record dict and return it. Raises PartialRecordError or RecordError."""
    check(rec)
    missing = sorted(set(STAGES) - set(rec["stages"]))
    if missing:
        msg = f"record written without stage(s) {missing}"
        raise PartialRecordError(msg)
    return rec


def check(rec):
    """Validate a record's structure.

    Schema 3 or 4, every top key and series block, runs, folders, the naming restart flag, the calibration groups'
    shape, unique outputs and chapter files, and sources that name existing outputs. Raises RecordError.
    """
    _check_top(rec)
    _check_blocks(rec)
    _check_naming(rec["series"]["naming"])
    _check_calibration(rec["series"]["calibration"].get("groups", {}))
    outputs = _check_chapters(rec["chapters"])
    _check_sources(rec["sources"], outputs)
    _check_chapter_sources(rec["chapters"], rec["sources"])


def _check_top(rec):
    """Check that the record is an object of schema 3 or 4 holding every top key with its type."""
    if not isinstance(rec, dict):
        msg = f"record is a {type(rec).__name__}, not an object"
        raise RecordError(msg)
    if rec.get("schema") not in (FROZEN, SCHEMA) or isinstance(rec.get("schema"), bool):
        msg = f"record schema {rec.get('schema')!r}: only schema {FROZEN} and {SCHEMA} are read"
        raise RecordError(msg)
    for k, t in TOP.items():
        if not isinstance(rec.get(k), t):
            msg = f"record: {k!r} missing or not a {t.__name__}"
            raise RecordError(msg)


def _check_blocks(rec):
    """Every series block an object, at least 1 run, and folders of [{name, from_run}]."""
    for k in SERIES:
        if not isinstance(rec["series"].get(k), dict):
            msg = f"record: series.{k} missing or not an object"
            raise RecordError(msg)
    if not rec["runs"]:
        msg = "record: no runs"
        raise RecordError(msg)
    if not rec["folders"] or any(
        not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("from_run"), str)
        for f in rec["folders"]
    ):
        msg = "record: folders missing or not [{name, from_run}]"
        raise RecordError(msg)


def _check_naming(naming):
    """series.naming: a restart bool and a series name."""
    if not isinstance(naming.get("restart"), bool):
        msg = "record: series.naming.restart missing or not a bool"
        raise RecordError(msg)
    if not isinstance(naming.get("series_name"), str) or not naming["series_name"]:
        msg = "record: series.naming.series_name missing or empty"
        raise RecordError(msg)


def _check_calibration(groups):
    """series.calibration.groups: {group: {q: integer 0-100, probes: list}}."""
    if not isinstance(groups, dict) or any(
        not isinstance(g, dict)
        or type(g.get("q")) is not int
        or not 0 <= g["q"] <= QUALITY_MAX
        or not isinstance(g.get("probes", []), list)
        for g in groups.values()
    ):
        msg = "record: series.calibration.groups is not {group: {q: integer 0-100, probes: list}}"
        raise RecordError(msg)


def _check_chapters(chapters):
    """Every chapter's keys, its output and its file each listed once. Returns {output, lower case: chapter}."""
    outputs, files = {}, set()
    for c in chapters:
        absent = [k for k in CHAPTER_KEYS if k not in c]
        if absent:
            msg = f"record: chapter {c.get('output')!r} lacks {absent}"
            raise RecordError(msg)
        low = c["output"].lower()
        if low in outputs:
            msg = f"record: output {c['output']!r} listed twice"
            raise RecordError(msg)
        outputs[low] = c
        if c["file"] in files:
            msg = f"record: chapter file {c['file']!r} listed twice"
            raise RecordError(msg)
        files.add(c["file"])
    return outputs


def _check_sources(sources, outputs):
    """Every source well formed and listed once; a chapter or merged source names an output (outputs: lower case)."""
    seen = set()
    for s in sources:
        _check_source(s)
        if (s["file"], s["sha256"]) in seen:
            msg = f"record: source {s['file']!r} listed twice"
            raise RecordError(msg)
        seen.add((s["file"], s["sha256"]))
        if s["fate"] in (CHAPTER, MERGED) and (s.get("output") or "").lower() not in outputs:
            msg = f"record: source {s['file']!r} names output {s.get('output')!r}, not a chapter"
            raise RecordError(msg)


def _check_source(s):
    """1 source: file, sha256 and a known fate; trash_run and trash_folder together."""
    for k in ("file", "sha256", "fate"):
        if not isinstance(s.get(k), str):
            msg = f"record: source {s.get('file')!r}: {k!r} missing"
            raise RecordError(msg)
    if s["fate"] not in FATES:
        msg = f"record: source {s['file']!r}: unknown fate {s['fate']!r}"
        raise RecordError(msg)
    if ("trash_run" in s) != ("trash_folder" in s) or (s.get("trash_run") is None) != (s.get("trash_folder") is None):
        msg = f"record: source {s['file']!r}: trash_run and trash_folder go together"
        raise RecordError(msg)


def _check_chapter_sources(chapters, sources):
    """Every source a chapter lists is a chapter or merged source of that output."""
    by_name = {}
    for s in sources:
        by_name.setdefault(s["file"], []).append(s)
    for c in chapters:
        for f in c["sources"]:
            if not any(s["fate"] in (CHAPTER, MERGED) and s.get("output") == c["output"] for s in by_name.get(f, [])):
                msg = f"record: chapter {c['output']!r}: source {f!r} not in sources"
                raise RecordError(msg)
