"""A series' naming override file (SPEC "Publishing"): checked, and the volume its ranges or starts give a chapter.

The file is `<paths.naming_overrides>/<series rules key>.yaml`, read and checked as the bulk converter accepts it
(`volumes` ranges, `series_group`, `layout`), plus the pipeline's own `volume_starts` (each volume's first chapter,
unpadded: YAML reads `010` as octal 8).
The volume bases' names. Volumes themselves are resolved by metadata/naming.py.
"""

import itertools
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

import yaml

from manga_pipeline import errors, names
from manga_pipeline.config import Yaml

FILENAME, SEASON_TAGS, OVERRIDE = "filename", "season tags", "override"
SIDE_DEFAULT = "side stories need volumes: main story = Vol 1"
LAYOUTS = ("strip", "pages")


class VolumeError(ValueError):
    """The chapter's volume cannot be written: no further attempt changes that, a person must act."""


class OverrideError(VolumeError):
    """The series' override file is missing or not one the bulk converter would accept: a person must fix it."""


class Overrides(TypedDict, total=False):
    """A series' override file as override_file checks it.

    A key the bulk converter reads that the pipeline does not is kept, unchecked and untyped. volumes {volume: chapter
    range}, or null: a volume a whole number (an int, or a text of digits), a range a text or a number YAML read
    ("1-53", "54", "94-", "-53", 54); volume_starts {volume: first chapter}, or null (check_starts; not with volumes);
    series_group a name or a non-empty list of names; layout "strip" or "pages" in any case and spacing, or null; name
    the series' name, a non-empty text (SPEC "Retroactive updates", Series names); join_parts true or false (SPEC
    "Retroactive updates", Split chapters); source_titles true or false (SPEC "Publishing": false, its chapters take
    no title from its platform list or MangaDex). list_titles, its former name, is refused.
    """

    volumes: dict[int | str, str | int | float] | None
    volume_starts: dict[int, int | float] | None
    series_group: str | list[str]
    layout: str | None
    name: str
    join_parts: bool
    source_titles: bool


def layout(data: Overrides) -> str | None:
    """Return `layout:` of an overrides mapping as the bulk converter reads it (case-folded, trimmed).

    "strip", "pages" or None. ValueError for any other value.
    """
    v = data.get("layout")
    v = str(v).strip().lower() if v is not None else None
    if v not in (None, *LAYOUTS):
        msg = f"overrides layout must be 'strip' or 'pages', not {v!r}"
        raise ValueError(msg)
    return v


def override_layout(overrides: Path, folder_name: str) -> str | None:
    """Return the layout the series' override file names, None without the file or the key.

    OverrideError when the file is malformed.
    """
    return layout(override_file(overrides, folder_name) or {})


def series_group(overrides: Path, folder_name: str) -> str | None:
    """Return ComicInfo SeriesGroup from the series' override file's `series_group`.

    A name, or a list joined with ", "; None without the file or the key. OverrideError when the file is malformed.
    """
    data: Overrides = override_file(overrides, folder_name) or {}
    if "series_group" not in data:  # present, it is a non-empty name or list (override_file)
        return None
    return ", ".join(_group_names(data["series_group"]))


def override_name(overrides: Path, folder_name: str) -> str | None:
    """Return the series' name its override file gives, None without the file or the key.

    OverrideError when the file is malformed.
    """
    name = (override_file(overrides, folder_name) or {}).get("name")
    return name.strip() if name else None


def join_parts(overrides: Path, folder_name: str) -> bool:
    """Return whether the series' split chapter runs are joined: its override file's join_parts, true without it.

    OverrideError when the file is malformed.
    """
    return (override_file(overrides, folder_name) or {}).get("join_parts", True)


def _check_bool(key: str, value: Yaml) -> None:
    """Raise ValueError unless value is true or false."""
    if not isinstance(value, bool):
        msg = f"overrides {key} must be true or false, not {value!r}"
        raise ValueError(msg)


def _no_old_keys(data: Mapping[str, Yaml]) -> None:
    """Raise ValueError for a key renamed since: list_titles is source_titles (titles from the list and MangaDex)."""
    if "list_titles" in data:
        msg = "overrides list_titles is now source_titles (titles from the platform list and MangaDex)"
        raise ValueError(msg)


def _check_name(value: Yaml) -> None:
    """Raise ValueError unless value is a non-empty text."""
    if not isinstance(value, str) or not value.strip():
        msg = f"overrides name must be a non-empty text, not {value!r}"
        raise ValueError(msg)


def _mapping(data: Yaml) -> None:
    """Raise ValueError unless data is a mapping."""
    if not isinstance(data, dict):
        msg = f"overrides must be a YAML mapping, not {type(data).__name__}"
        raise ValueError(msg)


def override_file(overrides: Path, folder_name: str) -> Overrides | None:
    """Return the series' override file as a checked mapping, None when it does not exist.

    OverrideError when malformed.
    """
    path = Path(overrides) / f"{folder_name}.yaml"
    if not errors.exists(path):
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data:  # an empty file: no overrides
            return {}
        _mapping(data)
        check_volumes(data.get("volumes"))
        check_starts(data.get("volume_starts"))
        _one_volume_format(data)
        if "series_group" in data:
            _group_names(data["series_group"])
        if "name" in data:
            _check_name(data["name"])
        _no_old_keys(data)
        for key in ("join_parts", "source_titles"):
            if key in data:
                _check_bool(key, data[key])
        layout(data)
    except (yaml.YAMLError, ValueError) as e:
        msg = f"{path}: {e}"
        raise OverrideError(msg) from None
    return data


def _one_volume_format(data: Mapping[str, Yaml]) -> None:
    """Raise ValueError when the file gives its volumes both as ranges and as starts."""
    if data.get("volumes") is not None and data.get("volume_starts") is not None:
        msg = "volumes and volume_starts cannot both be given: write the volumes one way"
        raise ValueError(msg)


def _group_names(value: Yaml) -> list[str]:
    """Return series_group's names: a non-empty name or a non-empty list of them, none holding a comma.

    Kavita splits SeriesGroup on commas, 1 collection each. ValueError otherwise.
    """
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        msg = f"series_group must be a name or a non-empty list of names, not {value!r}"
        raise ValueError(msg)
    out: list[str] = []
    for name in items:
        if not isinstance(name, str) or not name.strip():
            msg = f"series_group names must be non-empty strings, not {name!r}"
            raise ValueError(msg)
        if "," in name:
            msg = f"series_group name {name!r} has a comma: list the collections instead"
            raise ValueError(msg)
        out.append(name.strip())
    return out


def check_volumes(volumes: Yaml) -> None:
    """Check `volumes: {volume: range}`: each volume a whole number, each range one _range reads, never backwards.

    Each volume a whole number (ComicInfo Volume is an xs:int), each range one _range reads ("1-53", "54", "94-",
    "-53") and that does not run backwards. Raises ValueError otherwise.
    """
    if volumes is None:
        return
    if not isinstance(volumes, dict):
        msg = f"volumes must be a mapping of volume: chapter range, not {volumes!r}"
        raise ValueError(msg)
    for vol, rng in volumes.items():
        if (
            isinstance(vol, bool)
            or not (isinstance(vol, int) or (isinstance(vol, str) and vol.isascii() and vol.isdigit()))
            or int(vol) < 0
        ):
            msg = f"volumes key {vol!r} is not a volume number (a whole number)"
            raise ValueError(msg)
        try:
            lo, hi = _range(rng)
        except ValueError:
            msg = f"volumes {vol}: range {rng!r} is not a chapter range (1-53, 54, 94-, -53)"
            raise ValueError(msg) from None
        if lo > hi:
            msg = f"volumes {vol}: range {rng!r} runs backwards"
            raise ValueError(msg)


def check_starts(starts: Yaml) -> None:
    """Check `volume_starts: {volume: first chapter}` (SPEC "Publishing"). Raises ValueError otherwise.

    Each volume a plain YAML integer from 0, the lowest 0 or 1, none skipped; each start a plain number from 0 (no
    bool, text, nan or inf), rising with the volume. Plain integer keys only, so 1 and "1" never both appear.
    """
    if starts is None:
        return
    if not isinstance(starts, dict) or not starts:
        msg = f"volume_starts must be a mapping of volume: first chapter, not {starts!r}"
        raise ValueError(msg)
    checked: dict[int, float] = {}
    for vol, start in starts.items():
        if isinstance(vol, bool) or not isinstance(vol, int) or vol < 0:
            msg = f"volume_starts key {vol!r} is not a volume number (a whole number from 0, unquoted)"
            raise ValueError(msg)
        if isinstance(start, bool) or not isinstance(start, int | float) or not 0 <= start < float("inf"):
            msg = f"volume_starts {vol}: start {start!r} is not a chapter number (a number from 0, unquoted)"
            raise ValueError(msg)
        checked[vol] = start
    _check_start_order(checked)


def _check_start_order(starts: dict[int, float]) -> None:
    """Raise ValueError unless the volumes begin at 0 or 1, skip none, and their starts rise with them."""
    vols = sorted(starts)
    # Each gap as a span (first, last): a typo'd huge volume names 1 span, never every number in it
    gaps = [
        (lower + 1, upper - 1) for lower, upper in itertools.pairwise([min(vols[0], 1) - 1, *vols]) if upper > lower + 1
    ]
    if gaps:
        msg = f"volume_starts skips volume(s) {', '.join(_span(*g) for g in gaps)}: give each volume's first chapter"
        raise ValueError(msg)
    for lower, upper in itertools.pairwise(vols):
        if not starts[lower] < starts[upper]:
            msg = f"volume_starts {upper}: start {starts[upper]} does not come after volume {lower}'s {starts[lower]}"
            raise ValueError(msg)


def _span(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


def override_volume(overrides: Overrides, num: float) -> int | float | None:
    """Return the volume the override file gives chapter number num, or None.

    volume_starts: the volume with the highest start at or below num. volumes: the volume whose range holds num, the
    last such one, as the bulk converter's loop leaves it.
    """
    starts = overrides.get("volume_starts")
    if starts is not None:
        held = [vol for vol, start in starts.items() if start <= num]
        return max(held) if held else None
    found = None
    for vol, rng in (overrides.get("volumes") or {}).items():
        lo, hi = _range(rng)
        if lo <= num <= hi:
            found = names.to_volume(vol)
    return found


def _range(rng: Yaml) -> tuple[float, float]:
    """Parse an overrides range into (lo, hi): "1-53", "54" (one chapter), "94-" (open end), "-53" (open start)."""
    lo, sep, hi = str(rng).strip().partition("-")
    lo = float(lo) if lo.strip() else float("-inf")
    hi = float(hi) if hi.strip() else (float("inf") if sep else lo)
    return lo, hi
