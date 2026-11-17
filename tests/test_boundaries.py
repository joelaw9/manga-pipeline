"""JSON and YAML loads stay in their listed readers (ann-phase.md, "JSON and YAML boundaries").

A load (json.load, json.loads, yaml.safe_load, any .json() call) returns an untyped value; only the readers listed
here may hold one, so every other function gets a typed shape from a reader.
"""

import ast
from pathlib import Path

from tests.timing import budget

PROGRAM = Path(__file__).resolve().parent.parent / "manga_pipeline"
LOADS = {("json", "load"), ("json", "loads"), ("yaml", "safe_load")}

# (module path below manga_pipeline/, enclosing function) of every load allowed; "" for module level.
READERS = {
    ("baseline.py", "_fill_naming"),
    ("chapter_names.py", "_open"),
    ("chapter_names.py", "_published"),
    ("cli.py", "junk_show"),
    ("clients/http.py", "Response.json"),
    ("clients/kavita.py", "Kavita._get_token"),
    ("clients/kavita.py", "Kavita.libraries"),
    ("clients/kavita.py", "Kavita.metadata"),
    ("clients/kavita.py", "Kavita.series"),
    ("clients/kavita.py", "Kavita.series_in_library"),
    ("clients/kavita.py", "Kavita.volumes"),
    ("clients/suwayomi.py", "_data"),
    ("config.py", "_convert"),
    ("config.py", "_read"),
    ("convert/calibrate.py", "add"),
    ("convert/calibrate.py", "rows"),
    ("convert/plan.py", "check_edit"),
    ("convert/record_reader.py", "load"),
    ("fleet.py", "problem"),
    ("hand_edits.py", "load"),
    ("junk/candidates.py", "chapter_order"),
    ("junk/review_run.py", "_claude"),
    ("junk/review_run.py", "child_config"),
    ("metadata/importer.py", "read_overrides"),
    ("metadata/importer.py", "read_state"),
    ("metadata/sources.py", "AniList._query"),
    ("metadata/sources.py", "MangaBaka._get"),
    ("metadata/sources.py", "MangaDex._get"),
    ("metadata/sources.py", "MangaUpdates.search"),
    ("metadata/sources.py", "MangaUpdates.series"),
    ("metadata/sources.py", "Tapas.episodes"),
    ("metadata/sources.py", "Webtoons.episodes"),
    ("metadata/tags.py", "load"),
    ("notices.py", "judge"),
    ("redownload.py", "_parsed"),
    ("redownload.py", "load"),
    ("redownload.py", "pick_chapter"),
    ("rewrite.py", "comicinfo_run"),
    ("rewrite.py", "publish_run"),
    ("stages/comicinfo.py", "run"),
    ("stages/convert.py", "_layout"),
    ("stages/junk.py", "_aspects"),
    ("stages/junk.py", "decide"),
    ("stages/junk.py", "junk_decision_over"),
    ("stages/publish.py", "row_values"),
    ("stages/validate.py", "_check"),
    ("status.py", "_notes"),
    ("status.py", "_redownload"),
    ("validation.py", "chapter_key"),
    ("volumes.py", "override_file"),
    ("workflow.py", "droppable"),
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


def _is_load(call: ast.Call, aliases: dict[str, tuple[str, str]]) -> bool:
    """Return whether a call is a JSON or YAML load: json.load(s), yaml.safe_load or any .json() call."""
    func = call.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id) in LOADS
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "json":
        return True
    if not isinstance(func.value, ast.Name) or func.value.id not in aliases:
        return False
    module, name = aliases[func.value.id]
    return not name and (module, func.attr) in LOADS


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
            if isinstance(child, ast.Call) and _is_load(child, aliases):
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
def test_aliased_and_from_imports_json_methods_and_nested_scopes_are_found():
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

TABLE = jsonlib.load(open("x"))

def imported(t):
    return parse(t)
"""
    assert sites("m.py", text) == {
        ("m.py", "Service.json"),
        ("m.py", "reply.inner"),
        ("m.py", "read"),
        ("m.py", ""),
        ("m.py", "imported"),
    }, "an aliased json import, a from-import, a .json() call, a nested function and module level are each a site"
