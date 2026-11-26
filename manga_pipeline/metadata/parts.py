"""Split chapter runs (SPEC "Retroactive updates", Split chapters): what a run is, and the parts a joined file holds.

Pure: the callers read the published files and pass them in.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from manga_pipeline import names

type Key = tuple[str | None, str | None, str | None]  # validation.ChapterKey: (season, side label, number)

# A run of 2 or 3 parts beside a file N.5: the .5 is an extra chapter, not a part (the bulk converter's rule)
EXTRA = 5
EXTRA_BESIDE = (2, 3)
MIN_PARTS = 2  # a run joins 2 parts or more


@dataclass(frozen=True)
class File:
    """A published file of a series as the rule reads it.

    season and number as its identity holds them, side "" for a main chapter; volume its ComicInfo Volume text;
    joined the parts a join made it hold (None: not a joined file).
    """

    id: int
    season: str | None
    side: str | None
    number: str | None
    volume: str | None
    origin: str
    confirmed: bool
    joined: int | None


@dataclass(frozen=True)
class Run:
    """A run of a series: chapter N (whole, as its parts' numbers write it), its files in run order, its top part."""

    season: str | None
    whole: int
    members: tuple[int, ...]
    top: int


def _part(number: str | None) -> tuple[int, int] | None:
    """Return (N, j) of a part number N.j, j written without a leading 0 and with 1 reading; else None."""
    key = names.number_key(number)
    if key is None or not key[1] or key[1].startswith("0") or names.two_readings(number):
        return None
    return key[0], int(key[1])


def runs(files: Iterable[File], *, any_origin: bool = False) -> list[Run]:
    """Return the runs of 1 series' published files (every file of the series, confirmed or not).

    A run: main-chapter files numbered N.1 … N.k (k ≥ 2, every number from .1 to .k), of 1 season and 1 volume, with
    no other file numbered N but a joined file, which counts as its parts N.1 … N.m; 1 file per part; at least 1 file
    that is not the joined file. Beside 2 or 3 parts a file N.5 is an extra chapter and the run stands; any other part
    beyond .k leaves the numbers as they are (no run). Every file of the run is confirmed and of origin pipeline
    (any_origin: any origin, for the census).
    """
    groups: dict[tuple[str | None, int], list[tuple[File, str]]] = {}
    for f in files:
        key = None if f.side else names.number_key(f.number)
        if key is not None:
            groups.setdefault((f.season, key[0]), []).append((f, key[1]))
    out: list[Run] = []
    for (season, whole), group in sorted(groups.items(), key=lambda g: (g[0][0] or "", g[0][1])):
        run = _run(season, whole, group, any_origin=any_origin)
        if run is not None:
            out.append(run)
    return out


def _run(season: str | None, whole: int, group: list[tuple[File, str]], *, any_origin: bool) -> Run | None:
    """Return the run of chapter N's files (each with its number's decimal part), or None."""
    numbered = _numbered(group)
    if numbered is None:
        return None
    joined, by_part = numbered
    covered = set(range(1, (joined.joined or 0) + 1)) if joined else set[int]()
    top = _top(covered | set(by_part))
    new = [by_part[j] for j in range(1, top + 1) if j not in covered]
    members = [*([joined] if joined else []), *new]
    ok = (
        top >= MIN_PARTS
        and bool(new)  # a joined file alone has nothing to join
        and len({names.to_volume(f.volume) for f in members}) == 1
        and all(f.confirmed and (any_origin or f.origin == "pipeline") for f in members)
    )
    return Run(season, whole, tuple(f.id for f in members), top) if ok else None


def _numbered(group: list[tuple[File, str]]) -> tuple[File | None, dict[int, File]] | None:
    """Return chapter N's joined file (or None) and its part files by part; None when N has another file or a part 2.

    Another file: a file numbered N that is not a joined file, or 2 of them.
    """
    wholes = [f for f, decimal in group if not decimal]
    if len(wholes) > 1 or (wholes and wholes[0].joined is None):
        return None
    by_part: dict[int, list[File]] = {}
    for f, _ in group:
        if (p := _part(f.number)) is not None:
            by_part.setdefault(p[1], []).append(f)
    if not by_part or any(len(fs) > 1 for fs in by_part.values()):
        return None
    return (wholes[0] if wholes else None), {j: fs[0] for j, fs in by_part.items()}


def _top(held_parts: set[int]) -> int:
    """Return the run's top part k (every part 1..k held), 0 when a part beyond k leaves the numbers as they are.

    Beyond k only an extra .5 beside 2 or 3 parts is allowed.
    """
    top = 0
    while top + 1 in held_parts:
        top += 1
    beyond = held_parts - set(range(1, top + 1))
    return top if not beyond or (beyond == {EXTRA} and top in EXTRA_BESIDE) else 0


def held(season: str | None, number: str | None, joined: int | None) -> set[Key]:
    """Return the chapter keys of the parts a joined file numbered `number` holds (none for a file that is not)."""
    if not joined or number is None:
        return set()
    return {(season, "", f"{number}.{j}") for j in range(1, joined + 1)}
