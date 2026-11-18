"""The dead-code check (CLAUDE.md "Core commands"): vulture over the program, its TypedDict fields whitelisted.

A TypedDict field is read by key (`cfg["logging"]["dir"]`), which vulture cannot see. So every field declared in the
body of a class of manga_pipeline/ whose bases name TypedDict (generic ones included) goes into a whitelist written to
a temporary folder, and vulture runs over the program and that whitelist with `[tool.vulture]` of pyproject.toml.

Usage: python scripts/vulture_check.py. Exit status: vulture's (0 when it reports nothing).
"""

import ast
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = ROOT / "manga_pipeline"


def _is_typeddict(node: ast.ClassDef) -> bool:
    """Return whether a class's bases name TypedDict (as `TypedDict` or `typing.TypedDict`)."""
    return any(
        (isinstance(base, ast.Name) and base.id == "TypedDict")
        or (isinstance(base, ast.Attribute) and base.attr == "TypedDict")
        for base in node.bases
    )


def typeddict_fields(trees: Iterable[ast.Module]) -> set[str]:
    """Return the name of every field declared in a TypedDict class body of the trees."""
    return {
        statement.target.id
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _is_typeddict(node)
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }


def main() -> int:
    """Write the whitelist of the program's TypedDict fields, run vulture with it and return vulture's exit status."""
    trees = [ast.parse(path.read_text(encoding="utf-8"), str(path)) for path in sorted(PROGRAM.rglob("*.py"))]
    with tempfile.TemporaryDirectory() as folder:
        whitelist = Path(folder, "typeddict_fields.py")
        lines = [f"_.{name}  # a TypedDict field, read by key\n" for name in sorted(typeddict_fields(trees))]
        whitelist.write_text("".join(lines), encoding="utf-8", newline="\n")
        command = [sys.executable, "-m", "vulture", str(PROGRAM), str(whitelist)]
        return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
