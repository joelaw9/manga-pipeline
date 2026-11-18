"""The dead-code check (CLAUDE.md "Core commands"): vulture over the program, less its TypedDict fields.

A TypedDict field is read by key (`cfg["logging"]["dir"]`), which vulture cannot see, so it reports every field as an
unused variable. This runs vulture over manga_pipeline/ with `[tool.vulture]` of pyproject.toml and drops only the
findings that sit on a field declaration in a TypedDict class body (found with ast: a class whose bases name TypedDict,
generic ones included, or another TypedDict of the program, a class or a functional `X = TypedDict(...)`); every other
finding is printed.

Usage: python scripts/vulture_check.py. Exit status: 0 when nothing is left, 1 when findings are left, vulture's own
status when it fails.
"""

import ast
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = "manga_pipeline"
VULTURE_FOUND_CODE = 3  # vulture's exit status when it reports unused code

type Location = tuple[Path, int]  # a file (resolved) and a line number


def _base_names(node: ast.ClassDef) -> set[str]:
    """Return the names a class's bases give: `TypedDict`, `typing.TypedDict` and `Parent` give their last part."""
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _called(call: ast.Call) -> str | None:
    """Return the name a call calls by a bare name (`TypedDict(...)` gives TypedDict); None for another call."""
    return call.func.id if isinstance(call.func, ast.Name) else None


def typeddict_fields(trees: Mapping[Path, ast.Module]) -> set[Location]:
    """Return the location of every field declared in a TypedDict class body of the trees.

    A class is a TypedDict when a base names TypedDict or a TypedDict of the trees (matched by name): a TypedDict class,
    or a name a functional `X = TypedDict("X", {...})` assigns (its fields are keys, not declarations).
    """
    classes = [
        (path, node) for path, tree in trees.items() for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    ]
    typed = {"TypedDict"} | {
        target.id
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _called(node.value) == "TypedDict"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    while True:
        found = {node.name for _, node in classes if _base_names(node) & typed}
        if found <= typed:
            break
        typed |= found
    return {
        (path, statement.lineno)
        for path, node in classes
        if _base_names(node) & typed
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }


def location(finding: str, root: Path) -> Location:
    """Return the file and line of a vulture report line ("path:line: message"), the path resolved under root."""
    path, _, rest = finding.partition(":")
    line, _, _ = rest.partition(":")
    return (root / path).resolve(), int(line)


def main() -> int:
    """Run vulture over the program and print its findings that are not TypedDict fields; see the module docstring."""
    trees = {
        path.resolve(): ast.parse(path.read_text(encoding="utf-8"), str(path))
        for path in sorted((ROOT / PROGRAM).rglob("*.py"))
    }
    fields = typeddict_fields(trees)
    command = [sys.executable, "-m", "vulture", PROGRAM]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if done.returncode not in (0, VULTURE_FOUND_CODE):
        sys.stderr.write(done.stdout + done.stderr)
        return done.returncode
    left = [line for line in done.stdout.splitlines() if line and location(line, ROOT) not in fields]
    sys.stdout.write("".join(f"{line}\n" for line in left))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
