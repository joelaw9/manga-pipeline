"""The bulk converter's series record, read only (convert.record_reader; SPEC "State", baseline): load from a series
folder, parse (the stage rule) and check (the structural rules). Ported from the bulk converter's tests/test_record.py
(its read path)."""

import copy
import json

import pytest

from manga_pipeline.convert import record_reader as record

from tests.timing import budget

PILOT = "Akuyaku Kizoku to shite Hitsuyou na Sore"
CH1, CH2 = "Vol.1 Ch.1.cbz", "Vol.1 Ch.2 - Chris and Lady.cbz"
RUN_ID = "20261028-000000-0000"


def series_record(outputs, carried=None, folder="Series", schema=4):
    """A minimal valid record of written outputs (one source each, "source N.cbz") and carried files."""
    chapters, sources = [], []
    for i, out in enumerate(sorted(outputs), 1):
        src = f"source {i}.cbz"
        chapters.append(
            {
                "output": out,
                "file": src,
                "sources": [src],
                "kind": "main",
                "volume": None,
                "volume_source": "none",
                "number": str(i),
                "title": "",
                "pages_out": 10,
            }
        )
        sources.append(
            {
                "file": src,
                "sha256": f"{i:064x}",
                "fate": "chapter",
                "output": out,
                "trash_run": RUN_ID,
                "trash_folder": folder,
            }
        )
    sources += [{"file": n, "sha256": h, "fate": "carried"} for n, h in (carried or {}).items()]
    return {
        "schema": schema,
        "stages": ["image", "naming", "structure"],
        "kavita": {"series_id": None, "folder_status": None, "names": None},
        "folders": [{"name": folder, "from_run": RUN_ID}],
        "runs": [{"run_id": RUN_ID, "written": "2026-10-28T00:00:00+00:00", "outputs": sorted(outputs)}],
        "errors": [],
        "flags": [],
        "series": {
            "layout": {"layout": "pages"},
            "naming": {"basis": "none", "series_name": "S", "restart": False},
            "calibration": {},
            "covers": {},
            "decisions": {},
        },
        "sources": sources,
        "chapters": chapters,
    }


def pilot_record():
    """The pilot shaped minimally: two written chapters, a carried file."""
    return series_record([CH1, CH2], {"ComicInfo.xml": "c" * 64}, folder=PILOT)


def _without(key):
    return lambda r: {k: v for k, v in r.items() if k != key}


def _set(path, value):
    def f(r):
        node = r
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
        return r

    return f


def _delete(path):
    def f(r):
        node = r
        for k in path[:-1]:
            node = node[k]
        del node[path[-1]]
        return r

    return f


def _unpair_trash(r):
    del r["sources"][0]["trash_folder"]
    return r


def _null_trash_folder(r):
    r["sources"][0]["trash_folder"] = None
    return r


def _dup_output(r):
    r["chapters"].append(dict(r["chapters"][0], file="other.cbz"))
    return r


def _dup_output_other_case(r):
    r["chapters"].append(dict(r["chapters"][0], output=CH1.upper(), file="other.cbz"))
    return r


def _dup_file(r):
    r["chapters"][1]["file"] = r["chapters"][0]["file"]
    return r


def _orphan(r):
    r["chapters"][0]["sources"] = ["missing.cbz"]
    return r


def _source_of_other_output(r):
    r["chapters"][0]["sources"] = ["source 2.cbz"]
    return r


def _dangling(r):
    r["sources"][0]["output"] = "gone.cbz"
    return r


def _dup_source(r):
    r["sources"].append(dict(r["sources"][0]))
    return r


def write(folder, text):
    (folder / record.RECORD_NAME).write_text(text, encoding="utf-8")
    return folder


# load


@budget(load="fs")
def test_load_returns_the_parsed_record(tmp_path):
    rec = pilot_record()
    assert record.load(write(tmp_path, json.dumps(rec))) == rec, "a valid record loads as written"


@budget(load="fs")
def test_folder_without_a_record_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        record.load(tmp_path)


@budget(load="fs")
def test_record_that_is_not_json_is_refused(tmp_path):
    with pytest.raises(record.RecordError, match=r"\.optimizer\.json: not JSON: "):
        record.load(write(tmp_path, "{not json"))


@budget(load="fs")
@pytest.mark.parametrize(
    ("rec", "msg"),
    [
        pytest.param({"pipeline_version": 3, "naming": {}, "chapters": []}, "record schema None: ", id="v1-no-schema"),
        pytest.param(["not", "a", "record"], "record is a list, not an object", id="not-an-object"),
    ],
)
def test_unusable_file_is_refused(tmp_path, rec, msg):
    """A v1 record (no schema) or a non-object is refused, never treated as no record."""
    with pytest.raises(record.RecordError, match=msg):
        record.load(write(tmp_path, json.dumps(rec)))


# parse: every unusable record is refused


@budget(load="pure")
@pytest.mark.parametrize(
    ("change", "exc", "msg"),
    [
        pytest.param(_set(("schema",), 5), record.RecordError, "record schema 5: only schema 3 and 4", id="schema-5"),
        pytest.param(_set(("schema",), 2), record.RecordError, "record schema 2: only schema 3 and 4", id="schema-2"),
        pytest.param(_set(("schema",), value=True), record.RecordError, "record schema True", id="schema-bool"),
        pytest.param(
            _set(("folders",), []),
            record.RecordError,
            r"folders missing or not \[\{name, from_run\}\]",
            id="no-folders",
        ),
        pytest.param(
            _set(("folders",), [{"name": PILOT}]), record.RecordError, "folders missing or not", id="folder-without-run"
        ),
        pytest.param(
            _set(("folders",), [{"name": 1, "from_run": "r"}]),
            record.RecordError,
            "folders missing or not",
            id="folder-name-not-text",
        ),
        pytest.param(_set(("folders",), [PILOT]), record.RecordError, "folders missing or not", id="folder-not-object"),
        pytest.param(_without("sources"), record.RecordError, "'sources' missing or not a list", id="no-sources"),
        pytest.param(
            _set(("stages",), ["structure"]),
            record.PartialRecord,
            r"without stage\(s\) \['image', 'naming'\]",
            id="partial-stages",
        ),
        pytest.param(_dup_output, record.RecordError, "output 'Vol.1 Ch.1.cbz' listed twice", id="output-twice"),
        pytest.param(_dup_output_other_case, record.RecordError, "listed twice", id="output-twice-other-case"),
        pytest.param(_dup_file, record.RecordError, "chapter file 'source 1.cbz' listed twice", id="file-twice"),
        pytest.param(_orphan, record.RecordError, "source 'missing.cbz' not in sources", id="orphan-chapter-source"),
        pytest.param(
            _source_of_other_output,
            record.RecordError,
            "source 'source 2.cbz' not in sources",
            id="source-of-another-output",
        ),
        pytest.param(_dangling, record.RecordError, "names output 'gone.cbz', not a chapter", id="dangling-output"),
        pytest.param(
            _delete(("chapters", 0, "pages_out")), record.RecordError, r"lacks \['pages_out'\]", id="chapter-lacks-key"
        ),
        pytest.param(_delete(("series", "naming", "restart")), record.RecordError, "naming.restart", id="no-restart"),
        pytest.param(
            _delete(("series", "naming", "series_name")), record.RecordError, "series_name", id="no-series-name"
        ),
        pytest.param(
            _set(("series", "naming", "series_name"), ""), record.RecordError, "series_name", id="empty-series-name"
        ),
    ],
)
def test_every_unusable_record_is_refused_never_treated_as_no_record(change, exc, msg):
    with pytest.raises(exc, match=msg):
        record.parse(change(pilot_record()))


@budget(load="pure")
@pytest.mark.parametrize("change", [_unpair_trash, _null_trash_folder], ids=["trash-unpaired", "trash-folder-null"])
def test_trash_run_and_trash_folder_go_together(change):
    with pytest.raises(record.RecordError, match="trash_run and trash_folder go together"):
        record.parse(change(pilot_record()))


@budget(load="pure")
@pytest.mark.parametrize(
    ("change", "msg"),
    [
        pytest.param(_set(("runs",), []), "record: no runs", id="no-runs"),
        pytest.param(_delete(("series", "covers")), r"series\.covers missing or not an object", id="series-key"),
        pytest.param(_set(("sources", 0, "fate"), "lost"), "unknown fate 'lost'", id="unknown-fate"),
        pytest.param(_delete(("sources", 0, "sha256")), "source 'source 1.cbz': 'sha256' missing", id="source-no-sha"),
        pytest.param(_dup_source, "source 'source 1.cbz' listed twice", id="source-twice"),
    ],
)
def test_structural_refusals_name_the_broken_part(change, msg):
    with pytest.raises(record.RecordError, match=msg):
        record.check(change(pilot_record()))


@budget(load="pure")
def test_check_accepts_a_partial_stages_record_parse_does_not():
    partial = dict(pilot_record(), stages=["naming", "structure"])
    record.check(partial)
    with pytest.raises(record.PartialRecord, match=r"without stage\(s\) \['image'\]"):
        record.parse(partial)


@budget(load="pure")
def test_a_schema_3_record_parses():
    rec = series_record([CH1], schema=3)
    assert record.parse(copy.deepcopy(rec)) == rec, "a frozen schema-3 record is read too"


@budget(load="pure")
def test_decisions_of_any_object_shape_pass():
    rec = pilot_record()
    rec["series"]["decisions"] = {"rows": {"junk": [{"decision": "maybe"}]}}
    assert record.parse(rec) is rec, "the decision rows are not checked by the reader"


@budget(load="pure")
@pytest.mark.parametrize(
    "groups",
    [
        [],
        {"colour": "75"},
        {"colour": {"q": "75"}},
        {"colour": {"q": 75.0}},
        {"gray": {"q": 101}},
        {"gray": {"q": 80, "probes": {}}},
    ],
)
def test_a_malformed_calibration_is_a_broken_record(groups):
    rec = pilot_record()
    rec["series"]["calibration"] = {"groups": groups}
    with pytest.raises(record.RecordError, match="calibration.groups"):
        record.check(rec)
