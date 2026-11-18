"""JSON and YAML loads stay in their listed readers (ann-phase.md, "JSON and YAML boundaries").

A load (json.load, json.loads, json.JSONDecoder, a yaml loader, any .json() call) returns an untyped value; only the
readers listed here may hold one, so every other function gets a typed shape from a reader. A load is any reference to
a loader, called or not (`map(json.loads, rows)`, `parse = json.loads`), through any import alias. The list is kept at
function granularity: a listed function may hold any number of loads.
"""

import ast
from pathlib import Path

from tests.timing import budget

PROGRAM = Path(__file__).resolve().parent.parent / "manga_pipeline"
LOADS = {
    ("json", "load"),
    ("json", "loads"),
    ("json", "JSONDecoder"),
    ("yaml", "load"),
    ("yaml", "safe_load"),
    ("yaml", "full_load"),
    ("yaml", "unsafe_load"),
    ("yaml", "load_all"),
    ("yaml", "safe_load_all"),
    ("yaml", "full_load_all"),
    ("yaml", "unsafe_load_all"),
}

# (module path below manga_pipeline/, enclosing function) of every load allowed; "" for module level.
READERS = {
    ("baseline.py", "_fill_naming"),
    ("cli.py", "junk_show"),
    ("clients/kavita.py", "_authenticated"),
    ("clients/kavita.py", "_libraries"),
    ("clients/kavita.py", "_metadata"),
    ("clients/kavita.py", "_series"),
    ("clients/kavita.py", "_series_page"),
    ("clients/kavita.py", "_volumes"),
    ("clients/suwayomi.py", "_chapters"),
    ("clients/suwayomi.py", "_downloads"),
    ("clients/suwayomi.py", "_fetched"),
    ("clients/suwayomi.py", "_library_page"),
    ("clients/suwayomi.py", "_library_size"),
    ("clients/suwayomi.py", "_mangas"),
    ("clients/suwayomi.py", "_source_search"),
    ("clients/suwayomi.py", "_sources"),
    ("clients/suwayomi.py", "_unread"),
    ("config.py", "_convert"),
    ("config.py", "_read"),
    ("convert/calibrate.py", "_samples"),
    ("convert/plan.py", "check_edit"),
    ("convert/plan.py", "read_page_aspect"),
    ("convert/record_reader.py", "decisions"),
    ("convert/record_reader.py", "load"),
    ("fleet.py", "problem"),
    ("hand_edits.py", "load"),
    ("junk/review_run.py", "_reply"),
    ("metadata/importer.py", "read_overrides"),
    ("metadata/importer.py", "read_state"),
    ("metadata/sources.py", "_anilist_media"),
    ("metadata/sources.py", "_anilist_search"),
    ("metadata/sources.py", "_mangabaka_search"),
    ("metadata/sources.py", "_mangabaka_series"),
    ("metadata/sources.py", "_mangabaka_source"),
    ("metadata/sources.py", "_mangadex_aggregate"),
    ("metadata/sources.py", "_mangadex_covers"),
    ("metadata/sources.py", "_mangadex_feed"),
    ("metadata/sources.py", "_mangadex_manga"),
    ("metadata/sources.py", "_mangadex_search"),
    ("metadata/sources.py", "_mangaupdates_search"),
    ("metadata/sources.py", "_mangaupdates_series"),
    ("metadata/sources.py", "_tapas_page"),
    ("metadata/sources.py", "_webtoons_page"),
    ("metadata/tags.py", "load"),
    ("names.py", "read_identity"),
    ("notices.py", "judge"),
    ("redownload.py", "_parsed"),
    ("redownload.py", "load"),
    ("redownload.py", "pick_chapter"),
    ("rewrite.py", "comicinfo_run"),
    ("rewrite.py", "publish_run"),
    ("stages/convert.py", "read_output_pages"),
    ("stages/convert.py", "read_result"),
    ("stages/junk.py", "_aspects"),
    ("stages/junk.py", "decide"),
    ("stages/junk.py", "junk_decision_over"),
    ("stages/validate.py", "_check"),
    ("status.py", "_notes"),
    ("status.py", "_redownload"),
    ("validation.py", "chapter_key"),
    ("volumes.py", "override_file"),
    ("workflow.py", "read_redownload"),
}


def _aliases(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Return {local name: (module, function or "")} of every json / yaml import in a module, aliases included.

    `import json as jsonlib` gives {"jsonlib": ("json", "")}, `from json import loads` {"loads": ("json", "loads")}.
    """
    names: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"json", "yaml"}:
                    names[alias.asname or alias.name] = (alias.name, "")
        elif isinstance(node, ast.ImportFrom) and node.module in {"json", "yaml"}:
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module, alias.name)
    return names


def _is_load(node: ast.AST, aliases: dict[str, tuple[str, str]]) -> bool:
    """Return whether a node references a JSON or YAML loader (LOADS, through its import alias) or is a .json() call."""
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Attribute) and node.func.attr == "json"
    if isinstance(node, ast.Name):
        return aliases.get(node.id) in LOADS
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name) or node.value.id not in aliases:
        return False
    module, name = aliases[node.value.id]
    return not name and (module, node.attr) in LOADS


def sites(module: str, text: str) -> set[tuple[str, str]]:
    """Return the (module, enclosing function) of every load in a module's text; nested scopes are named a.b."""
    tree = ast.parse(text)
    aliases = _aliases(tree)
    found: set[tuple[str, str]] = set()

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{scope}.{child.name}" if scope else child.name)
                continue
            if _is_load(child, aliases):
                found.add((module, scope))
            visit(child, scope)

    visit(tree, "")
    return found


def program_sites() -> set[tuple[str, str]]:
    """Return the load sites of every module of the program."""
    found: set[tuple[str, str]] = set()
    for path in PROGRAM.rglob("*.py"):
        found |= sites(path.relative_to(PROGRAM).as_posix(), path.read_text(encoding="utf-8"))
    return found


@budget(load="fs")
def test_every_json_and_yaml_load_is_in_a_listed_reader():
    found = program_sites()
    assert found - READERS == set(), "a load outside the listed readers: read it through its shape's reader"
    assert READERS - found == set(), "a listed reader that loads nothing: take it off the list"


@budget(load="pure")
def test_every_loader_reference_is_found_through_aliases_and_nested_scopes():
    text = """\
import json as jsonlib
import yaml
from json import loads as parse

class Service:
    def json(self, r):
        return jsonlib.loads(r)

def reply(r):
    def inner():
        return r.json()
    return inner

def read(t):
    return yaml.safe_load(t)

def unsafe(t):
    return yaml.load(t)

def every(rows):
    return list(map(jsonlib.loads, rows))

def kept(t):
    parse_all = yaml.safe_load_all
    return parse_all(t)

def decoder():
    return jsonlib.JSONDecoder().decode

def dumps(v):
    return jsonlib.dumps(v), yaml.safe_dump(v)

TABLE = jsonlib.load(open("x"))

def imported(t):
    return parse(t)
"""
    assert sites("m.py", text) == {
        ("m.py", "Service.json"),
        ("m.py", "reply.inner"),
        ("m.py", "read"),
        ("m.py", "unsafe"),
        ("m.py", "every"),
        ("m.py", "kept"),
        ("m.py", "decoder"),
        ("m.py", ""),
        ("m.py", "imported"),
    }, "a loader referenced, called or not, through any import, and a .json() call are sites; a dump is not"
