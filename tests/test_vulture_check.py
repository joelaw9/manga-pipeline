"""scripts/vulture_check.py: which findings are TypedDict fields, and where a vulture report line points."""

import ast
from pathlib import Path

from scripts import vulture_check

from tests.timing import budget

SOURCE = """\
from typing import TypedDict
import typing

class Plain(TypedDict):
    a: int
    b: str

class Generic[T](typing.TypedDict, total=False):
    c: T

class Child(Plain):
    d: int

class NotTyped:
    e: int

    def f(self) -> None:
        g: int = 1

def h() -> None:
    i: int = 2
"""


@budget(load="pure")
def test_the_fields_of_typeddicts_are_located_and_nothing_else():
    path = Path("m.py")
    assert vulture_check.typeddict_fields({path: ast.parse(SOURCE)}) == {(path, 5), (path, 6), (path, 9), (path, 12)}, (
        "a field of a TypedDict (plain, generic, through typing, or inheriting one) is located; a field of another "
        "class and an annotated variable in a function are not"
    )


@budget(load="pure")
def test_a_typeddict_inheriting_one_of_another_module_is_found():
    base, child = Path("base.py"), Path("child.py")
    trees = {
        base: ast.parse("from typing import TypedDict\nclass Base(TypedDict):\n    a: int\n"),
        child: ast.parse("from base import Base\nclass Child(Base):\n    b: int\n"),
    }
    assert vulture_check.typeddict_fields(trees) == {(base, 3), (child, 3)}, "a subclass of a TypedDict is one too"


@budget(load="fs")
def test_a_report_line_gives_its_file_resolved_under_the_root_and_its_line(tmp_path):
    line = "manga_pipeline/clients/x.py:12: unused variable 'a' (60% confidence)"
    assert vulture_check.location(line, tmp_path) == ((tmp_path / "manga_pipeline" / "clients" / "x.py").resolve(), 12)
