"""Chains on junk decisions (SPEC "Junk decisions", decisions; chains and cuts): the format and the row shapes.

Ported from the bulk converter's optimizer/chain.py (the format: canonical JSON, sorted keys, no spaces) and its
junk.Library.check_row / _check_chain (which rows carry which chains), for the rows this program applies:

- a series block or probe junk decision may carry its card's extent: 1 or 2 sides (above, below), versions without a
  source;
- a cut decision (series-scoped, junk) carries a chain of exactly 1 side, every version with its source archive, no
  `edge` segment, its key the chain's key span (`key_span`), and may carry credit.

A chain is {"above": [VERSION, ...], "below": [VERSION, ...]} (a side omitted when it extends nothing). VERSION =
{"segments": [SEGMENT, ...], "ref": [[series, chapter key, anchor start], ...], "src": {"file", "sha256"} | null}. A
held row's (the bulk converter's) ref names its folder and archive and its src.sha256 is the archive's sha256; a row
`junk cut` makes names the series key and the chapter id, and its src is the download's file name and its pages digest
(chapter.content_digest).
SEGMENT = {"kind": "cut" | "keep", "h", "tiles", "exact"?: true} (h in strip widths, at most 4 decimals; exact: exactly
round(h x width) rows, followed by the non-exact rest of the span it splits) or {"kind": "edge"} (last only). A version
holds a cut or edge segment. A `none` exemption or a `skip` segment is refused (no command makes them); a global
decision or a keep carries no chain (no global rows are made).
"""

import json
import math
from collections.abc import Mapping, Sequence
from typing import Literal, NotRequired, TypedDict

from manga_pipeline import images
from manga_pipeline.replies import Json

SIDES = ("above", "below")
DECIMALS = 4  # a height carries at most this many decimals (another text of the same value would be another chain)
HEX = frozenset("0123456789abcdef")
TILE_HEX = 16  # hex digits of 1 tile hash
SHA256_HEX = 64  # hex digits of a source archive's sha256
REF_LEN = 3  # [series, chapter key, anchor start]
UNIT_KINDS = ("block", "probe")  # the unit decisions that may carry a card's extent


class Span(TypedDict):
    """A cut or keep segment: its height in strip widths, its tile hashes' text, exact when it splits a span."""

    kind: Literal["cut", "keep"]
    h: float
    tiles: str
    exact: NotRequired[bool]


class Edge(TypedDict):
    """An edge segment: every row from the anchor to the strip's end (a unit's chain only)."""

    kind: Literal["edge"]


type Segment = Span | Edge


class Source(TypedDict):
    """The archive a cut version was cut from: its file name and sha256 (a held row) or pages digest (junk cut's)."""

    file: str
    sha256: str


class Version(TypedDict):
    """1 version of a side: its segments outward from the anchor, the occurrences it was learned on, its source."""

    segments: list[Segment]
    ref: list[tuple[str, str, int]]
    src: Source | None


class Chain(TypedDict):
    """A chain's sides; at least 1 is present."""

    above: NotRequired[list[Version]]
    below: NotRequired[list[Version]]


def text(value: Json) -> str:
    """Return the canonical text of a chain value: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _height(x: Json) -> float:
    if not (type(x) is float and math.isfinite(x) and x > 0 and round(x, DECIMALS) == x):
        msg = f"height {x!r}: not a finite float > 0 with at most {DECIMALS} decimals"
        raise ValueError(msg)
    return x


def _tiles(x: Json) -> str:
    parts = x.split(":") if isinstance(x, str) else []
    if not parts or not all(len(p) == TILE_HEX and set(p) <= HEX for p in parts):
        msg = f"tiles {x!r}: not lowercase 16-hex hashes, ':'-separated"
        raise ValueError(msg)
    if images.unsignable(images.sig_from_str(f"{x}|1.0")):
        msg = f"tiles {x!r}: a uniform image's (unsignable: they match any blank or flat rows)"
        raise ValueError(msg)
    return str(x)


def _segment(s: Json) -> Segment:
    if not isinstance(s, Mapping):
        msg = f"segment {s!r}: not an object"
        raise ValueError(msg)
    kind = s.get("kind")
    if kind == "edge" and set(s) == {"kind"}:
        return {"kind": "edge"}
    if kind == "skip":
        msg = f"segment {s!r}: skip segments are refused (no command makes them)"
        raise ValueError(msg)
    if kind not in ("cut", "keep") or set(s) - {"exact"} != {"kind", "h", "tiles"}:
        msg = f"segment {s!r}: not {{kind: cut|keep, h, tiles, exact?}} or {{kind: edge}}"
        raise ValueError(msg)
    span: Span = {"kind": "cut" if kind == "cut" else "keep", "h": _height(s["h"]), "tiles": _tiles(s["tiles"])}
    if "exact" in s:
        if s["exact"] is not True:
            msg = f"segment {s!r}: exact is true or absent"
            raise ValueError(msg)
        span["exact"] = True
    return span


def _ref(o: Json) -> tuple[str, str, int]:
    if (
        isinstance(o, Sequence)
        and not isinstance(o, str)
        and len(o) == REF_LEN
        and isinstance(o[0], str)
        and isinstance(o[1], str)
        and type(o[2]) is int
        and o[2] >= 0
    ):
        return o[0], o[1], o[2]
    msg = f"ref {o!r}: not [series, chapter key, anchor start >= 0]"
    raise ValueError(msg)


def _source(s: Json) -> Source | None:
    if s is None:
        return None
    if (
        isinstance(s, Mapping)
        and set(s) == {"file", "sha256"}
        and isinstance(s["file"], str)
        and s["file"]
        and isinstance(s["sha256"], str)
        and len(s["sha256"]) == SHA256_HEX
        and set(s["sha256"]) <= HEX
    ):
        return {"file": s["file"], "sha256": s["sha256"]}
    msg = f"src {s!r}: not null or {{file, sha256: 64 lowercase hex}}"
    raise ValueError(msg)


def _version(v: Json) -> Version:
    if not (isinstance(v, Mapping) and set(v) == {"segments", "ref", "src"}):
        msg = f"version {v!r}: keys are not exactly segments, ref, src"
        raise ValueError(msg)
    raw, refs = v["segments"], v["ref"]
    if not (isinstance(raw, Sequence) and not isinstance(raw, str) and raw):
        msg = f"version {v!r}: segments is not a non-empty list"
        raise ValueError(msg)
    if not (isinstance(refs, Sequence) and not isinstance(refs, str) and refs):
        msg = f"version {v!r}: ref is not a non-empty list"
        raise ValueError(msg)
    segments = [_segment(s) for s in raw]
    kinds = [s["kind"] for s in segments]
    if "cut" not in kinds and "edge" not in kinds:
        msg = f"version {v!r}: holds no cut or edge segment"
        raise ValueError(msg)
    if "edge" in kinds[:-1]:
        msg = f"version {v!r}: edge is the last segment only"
        raise ValueError(msg)
    for s, nxt in zip(segments, [*segments[1:], None], strict=True):
        if s["kind"] != "edge" and s.get("exact") and (nxt is None or nxt["kind"] == "edge" or nxt.get("exact")):
            msg = f"version {v!r}: an exact segment is not followed by the non-exact rest of the span it splits"
            raise ValueError(msg)
    return {"segments": segments, "ref": [_ref(o) for o in refs], "src": _source(v["src"])}


def parse(stored: str) -> Chain:
    """Return the chain of a stored text; ValueError unless it is a valid chain in canonical text."""
    try:
        value: Json = json.loads(stored)
    except ValueError:
        msg = f"chain {stored!r}: not JSON"
        raise ValueError(msg) from None
    if not isinstance(value, Mapping):
        msg = f"chain {stored!r}: not an object"
        raise ValueError(msg)
    if "none" in value:
        msg = f"chain {stored!r}: none exemptions are refused (no command makes them)"
        raise ValueError(msg)
    if not (set(value) <= set(SIDES) and set(value)):
        msg = f"chain {stored!r}: keys are not 1 or 2 of {SIDES}"
        raise ValueError(msg)
    chain: Chain = {}
    for side in SIDES:
        if side not in value:
            continue
        raw = value[side]
        if not (isinstance(raw, Sequence) and not isinstance(raw, str) and raw):
            msg = f"chain {stored!r}: side {side} is not a non-empty list of versions"
            raise ValueError(msg)
        if len({text(v) for v in raw}) != len(raw):
            msg = f"chain {stored!r}: side {side} repeats a version"
            raise ValueError(msg)
        chain[side] = [_version(v) for v in raw]
    if text(value) != stored:
        msg = f"chain {stored!r}: not canonical (expected {text(value)!r})"
        raise ValueError(msg)
    return chain


def sides(chain: Chain) -> list[Literal["above", "below"]]:
    """Return the sides a chain extends, above first."""
    return [s for s in SIDES if s in chain]


def versions(chain: Chain) -> list[tuple[Literal["above", "below"], Version]]:
    """Return (side, version) of every version, sides above first, versions in stored order."""
    return [(side, v) for side in sides(chain) for v in chain.get(side, [])]


def key_span(chain: Chain) -> str:
    """Return a cut decision's key: its first side's first version's first cut segment, as signature text tiles|h."""
    first = versions(chain)[0][1]
    seg = next(s for s in first["segments"] if s["kind"] == "cut")  # check_cut: a cut chain's versions hold a cut
    return images.sig_to_str(images.sig_from_str(f"{seg['tiles']}|{seg['h']!r}"))


def check_unit(stored: str) -> Chain:
    """Return a block or probe decision's chain (its card's extent: versions without a source); ValueError otherwise."""
    chain = parse(stored)
    if any(v["src"] is not None for _, v in versions(chain)):
        msg = "a unit's chain version with a source (a unit's chain has none)"
        raise ValueError(msg)
    return chain


def check_cut(key: str, stored: str) -> Chain:
    """Return a cut decision's chain: 1 side, every version with its source, no edge, key = key span; ValueError."""
    chain = parse(stored)
    found = [v for _, v in versions(chain)]
    if len(sides(chain)) != 1:
        msg = f"a cut chain of {len(sides(chain))} sides (exactly 1)"
        raise ValueError(msg)
    if any(v["src"] is None for v in found):
        msg = "a cut chain version without a source"
        raise ValueError(msg)
    if any(s["kind"] == "edge" for v in found for s in v["segments"]):
        msg = "an edge segment in a cut chain (a unit's chain only)"
        raise ValueError(msg)
    span = key_span(chain)
    if key != span:
        msg = f"cut key {key!r} is not the chain's key span {span!r}"
        raise ValueError(msg)
    return chain
