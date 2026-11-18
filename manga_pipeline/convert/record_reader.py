"""The bulk converter's series record, `.optimizer.json` (SPEC "State", baseline): read and validated, never written.

A record that parses is a finished series. Copied from the bulk converter's optimizer/record.py (`load`, `parse`,
`check`); the check of the schema-4 junk decision rows comes with Phase 5, which imports them.
"""

import json
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict

from manga_pipeline.replies import Json

RECORD_NAME = ".optimizer.json"
SCHEMA, FROZEN = 4, 3  # schema 3 is read too; its decisions are history
STAGES = ("structure", "image", "naming")  # a record written with fewer stages is partial
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
    """A junk table row: its origin and its key, (tiles text, aspect), built from its JSON array."""

    origin: str
    key: tuple[str, float]


class FrameVector(TypedDict):
    """A frame's stored vector, base64."""

    b64: str


class RecordFrameRow(RecordDecisionRow):
    """A frames table row: its key, (vector, aspect), built from its JSON array."""

    key: tuple[FrameVector, float]


class TemplateRef(TypedDict):
    """A marks row's template: its name in series.decisions.templates."""

    tpl: str


class RecordMarkRow(RecordDecisionRow):
    """A marks table row: its key, (template, width, height), built from its JSON array."""

    key: tuple[TemplateRef, int, int]


class DecisionRows(TypedDict):
    """The decision rows the runs used, by the bulk converter's table; the junk table's cut rows are left out."""

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


class _Top(NamedTuple):
    """The top keys of a record that the check reads further, each checked to be of its type."""

    stages: list[Json]
    folders: list[Json]
    runs: list[Json]
    series: dict[str, Json]
    sources: list[Json]
    chapters: list[Json]


class _Source(NamedTuple):
    """A checked source: its file name, sha256 and fate (each text) and the output it names (unchecked)."""

    file: str
    sha256: str
    fate: str
    output: Json


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
    parse(record)
    return record


def decisions(record: Record) -> DecisionBlock:
    """Return a record's series.decisions block as a DecisionBlock: its rows' read fields, each key a tuple.

    Unchecked: check does not read the block; a malformed one fails here (a key of another length included) or where
    it is read.
    """
    block = json.loads(json.dumps(record["series"]["decisions"]))
    junk: list[RecordJunkRow] = []
    frames: list[RecordFrameRow] = []
    marks: list[RecordMarkRow] = []
    for r in block["rows"]["junk"]:
        if r["origin"] == "cut":  # a cut is no decision on a unit's key: only its origin is read
            continue
        tiles, aspect = r["key"]
        junk.append(
            {
                "row": r["row"],
                "series": r["series"],
                "decision": r["decision"],
                "added": r["added"],
                "origin": r["origin"],
                "key": (tiles, aspect),
            }
        )
    for r in block["rows"]["frames"]:
        vec, aspect = r["key"]
        frames.append(
            {
                "row": r["row"],
                "series": r["series"],
                "decision": r["decision"],
                "added": r["added"],
                "key": (vec, aspect),
            }
        )
    for r in block["rows"]["marks"]:
        tpl, w, h = r["key"]
        marks.append(
            {"row": r["row"], "series": r["series"], "decision": r["decision"], "added": r["added"], "key": (tpl, w, h)}
        )
    return {"rows": {"junk": junk, "frames": frames, "marks": marks}, "templates": block.get("templates", {})}


def parse(rec: Json) -> None:
    """Validate a record (any JSON value): its structure (check), then that it was written with every stage.

    Raises PartialRecordError or RecordError.
    """
    top = _checked(rec)
    missing = sorted(set(STAGES).difference(top.stages))
    if missing:
        msg = f"record written without stage(s) {missing}"
        raise PartialRecordError(msg)


def check(rec: Json) -> None:
    """Validate a record's structure (rec: any JSON value).

    Schema 3 or 4, every top key and series block, runs, folders, the naming restart flag, the calibration groups'
    shape, chapters and sources that are objects, unique outputs (text) and chapter files, and sources that name
    existing outputs. Raises RecordError.
    """
    _checked(rec)


def _checked(rec: Json) -> _Top:
    """Run check's rules and return the record's checked top keys."""
    top = _check_top(rec)
    blocks = _check_blocks(top)
    _check_naming(blocks["naming"])
    _check_calibration(blocks["calibration"].get("groups", {}))
    outputs = _check_chapters(top.chapters)
    sources = _check_sources(top.sources, outputs)
    _check_chapter_sources(list(outputs.values()), sources)
    return top


def _missing(key: str, kind: str) -> str:
    return f"record: {key!r} missing or not a {kind}"


def _array(rec: dict[str, Json], key: str) -> list[Json]:
    value = rec.get(key)
    if not isinstance(value, list):
        msg = _missing(key, "list")
        raise RecordError(msg)
    return value


def _object(rec: dict[str, Json], key: str) -> dict[str, Json]:
    value = rec.get(key)
    if not isinstance(value, dict):
        msg = _missing(key, "dict")
        raise RecordError(msg)
    return value


def _check_top(rec: Json) -> _Top:
    """Check that the record is an object of schema 3 or 4 holding every top key with its type, in this order.

    schema an int; stages, folders, runs, errors, flags, sources and chapters lists; kavita and series objects.
    """
    if not isinstance(rec, dict):
        msg = f"record is a {type(rec).__name__}, not an object"
        raise RecordError(msg)
    if rec.get("schema") not in (FROZEN, SCHEMA) or isinstance(rec.get("schema"), bool):
        msg = f"record schema {rec.get('schema')!r}: only schema {FROZEN} and {SCHEMA} are read"
        raise RecordError(msg)
    if not isinstance(rec.get("schema"), int):
        msg = _missing("schema", "int")
        raise RecordError(msg)
    stages = _array(rec, "stages")
    _object(rec, "kavita")
    folders, runs = _array(rec, "folders"), _array(rec, "runs")
    _array(rec, "errors")
    _array(rec, "flags")
    series = _object(rec, "series")
    return _Top(stages, folders, runs, series, _array(rec, "sources"), _array(rec, "chapters"))


def _check_blocks(top: _Top) -> dict[str, dict[str, Json]]:
    """Every series block an object, at least 1 run, and folders of [{name, from_run}]. Returns {block: object}."""
    blocks: dict[str, dict[str, Json]] = {}
    for k in SERIES:
        block = top.series.get(k)
        if not isinstance(block, dict):
            msg = f"record: series.{k} missing or not an object"
            raise RecordError(msg)
        blocks[k] = block
    if not top.runs:
        msg = "record: no runs"
        raise RecordError(msg)
    if not top.folders or any(
        not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("from_run"), str)
        for f in top.folders
    ):
        msg = "record: folders missing or not [{name, from_run}]"
        raise RecordError(msg)
    return blocks


def _check_naming(naming: dict[str, Json]) -> None:
    """series.naming: a restart bool and a series name."""
    if not isinstance(naming.get("restart"), bool):
        msg = "record: series.naming.restart missing or not a bool"
        raise RecordError(msg)
    if not isinstance(naming.get("series_name"), str) or not naming["series_name"]:
        msg = "record: series.naming.series_name missing or empty"
        raise RecordError(msg)


def _check_calibration(groups: Json) -> None:
    """series.calibration.groups: {group: {q: integer 0-100, probes: list}}."""
    if not isinstance(groups, dict) or any(
        not isinstance(g, dict)
        or type(q := g.get("q")) is not int
        or not 0 <= q <= QUALITY_MAX
        or not isinstance(g.get("probes", []), list)
        for g in groups.values()
    ):
        msg = "record: series.calibration.groups is not {group: {q: integer 0-100, probes: list}}"
        raise RecordError(msg)


def _check_chapters(chapters: list[Json]) -> dict[str, dict[str, Json]]:
    """Every chapter an object with its keys, its output and its file (text) each listed once.

    Returns {output, lower case: chapter}, in the chapters' order.
    """
    outputs: dict[str, dict[str, Json]] = {}
    files: set[str] = set()
    for c in chapters:
        if not isinstance(c, dict):
            msg = f"record: chapter {c!r} is not an object"
            raise RecordError(msg)
        absent = [k for k in CHAPTER_KEYS if k not in c]
        if absent:
            msg = f"record: chapter {c.get('output')!r} lacks {absent}"
            raise RecordError(msg)
        output = c["output"]
        if not isinstance(output, str):
            msg = f"record: chapter output {output!r} is not text"
            raise RecordError(msg)
        low = output.lower()
        if low in outputs:
            msg = f"record: output {output!r} listed twice"
            raise RecordError(msg)
        outputs[low] = c
        file = c["file"]
        if not isinstance(file, str):
            msg = f"record: chapter {output!r}: file {file!r} is not text"
            raise RecordError(msg)
        if file in files:
            msg = f"record: chapter file {file!r} listed twice"
            raise RecordError(msg)
        files.add(file)
    return outputs


def _check_sources(sources: list[Json], outputs: dict[str, dict[str, Json]]) -> list[_Source]:
    """Every source well formed and listed once; a chapter or merged source names an output (outputs: lower case).

    Returns the checked sources.
    """
    seen: set[tuple[str, str]] = set()
    checked: list[_Source] = []
    for entry in sources:
        s = _check_source(entry)
        if (s.file, s.sha256) in seen:
            msg = f"record: source {s.file!r} listed twice"
            raise RecordError(msg)
        seen.add((s.file, s.sha256))
        if s.fate in (CHAPTER, MERGED):
            output = s.output or ""
            if not isinstance(output, str):
                msg = f"record: source {s.file!r} names output {output!r}, not text"
                raise RecordError(msg)
            if output.lower() not in outputs:
                msg = f"record: source {s.file!r} names output {s.output!r}, not a chapter"
                raise RecordError(msg)
        checked.append(s)
    return checked


def _text(s: dict[str, Json], key: str) -> str:
    """Return a source's text field; RecordError naming it when it is missing or not text."""
    value = s.get(key)
    if not isinstance(value, str):
        msg = f"record: source {s.get('file')!r}: {key!r} missing"
        raise RecordError(msg)
    return value


def _check_source(s: Json) -> _Source:
    """1 source: an object of file, sha256 and a known fate; trash_run and trash_folder together. Returns it checked."""
    if not isinstance(s, dict):
        msg = f"record: source {s!r} is not an object"
        raise RecordError(msg)
    file, sha256, fate = _text(s, "file"), _text(s, "sha256"), _text(s, "fate")
    if fate not in FATES:
        msg = f"record: source {file!r}: unknown fate {fate!r}"
        raise RecordError(msg)
    if ("trash_run" in s) != ("trash_folder" in s) or (s.get("trash_run") is None) != (s.get("trash_folder") is None):
        msg = f"record: source {file!r}: trash_run and trash_folder go together"
        raise RecordError(msg)
    return _Source(file, sha256, fate, s.get("output"))


def _check_chapter_sources(chapters: list[dict[str, Json]], sources: list[_Source]) -> None:
    """Every chapter's sources a list of text, each a chapter or merged source of that output."""
    by_name: dict[str, list[_Source]] = {}
    for s in sources:
        by_name.setdefault(s.file, []).append(s)
    for c in chapters:
        listed = c["sources"]
        if not isinstance(listed, list):
            msg = f"record: chapter {c['output']!r}: sources {listed!r} is not a list"
            raise RecordError(msg)
        for f in listed:
            if not isinstance(f, str):
                msg = f"record: chapter {c['output']!r}: source {f!r} is not text"
                raise RecordError(msg)
            if not any(s.fate in (CHAPTER, MERGED) and s.output == c["output"] for s in by_name.get(f, [])):
                msg = f"record: chapter {c['output']!r}: source {f!r} not in sources"
                raise RecordError(msg)
