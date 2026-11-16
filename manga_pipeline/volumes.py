"""A series' naming override file (SPEC "Publishing"): `<paths.naming_overrides>/<series rules key>.yaml`, read and
checked as the bulk converter accepts it (`volumes` ranges, `series_group`, `layout`), and the volume a range gives a
chapter.
The volume bases' names. Volumes themselves are resolved by metadata/naming.py.
"""

from pathlib import Path

import yaml

from manga_pipeline import errors, names

FILENAME, SEASON_TAGS, OVERRIDE = "filename", "season tags", "override"
SIDE_DEFAULT = "side stories need volumes: main story = Vol 1"
LAYOUTS = ("strip", "pages")


class VolumeError(ValueError):
    """The chapter's volume cannot be written: no further attempt changes that, a person must act."""


class OverrideError(VolumeError):
    """The series' override file is missing or not one the bulk converter would accept: a person must fix it."""


def layout(data):
    """`layout:` of an overrides mapping as the bulk converter reads it (case-folded, trimmed): "strip", "pages" or
    None. ValueError for any other value."""
    v = data.get("layout")
    v = str(v).strip().lower() if v is not None else None
    if v not in (None, *LAYOUTS):
        msg = f"overrides layout must be 'strip' or 'pages', not {v!r}"
        raise ValueError(msg)
    return v


def override_layout(overrides, folder_name):
    """The layout the series' override file names, None without the file or the key. OverrideError when the file is
    malformed."""
    return layout(override_file(overrides, folder_name) or {})


def series_group(overrides, folder_name):
    """ComicInfo SeriesGroup from the series' override file's `series_group` (a name, or a list joined with ", "),
    None without the file or the key. OverrideError when the file is malformed."""
    data = override_file(overrides, folder_name) or {}
    return data.get("series_group") and ", ".join(_group_names(data["series_group"]))


def _mapping(data):
    """data, when it is a mapping; ValueError otherwise."""
    if not isinstance(data, dict):
        msg = f"overrides must be a YAML mapping, not {type(data).__name__}"
        raise ValueError(msg)
    return data


def override_file(overrides, folder_name):
    """The series' override file as a checked mapping, None when it does not exist. OverrideError when malformed."""
    path = Path(overrides) / f"{folder_name}.yaml"
    if not errors.exists(path):
        return None
    try:
        data = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        check_volumes(data.get("volumes"))
        if "series_group" in data:
            _group_names(data["series_group"])
        layout(data)
    except (yaml.YAMLError, ValueError) as e:
        msg = f"{path}: {e}"
        raise OverrideError(msg) from None
    return data


def _group_names(value):
    """series_group's names: a non-empty name or a non-empty list of them, none holding a comma (Kavita splits
    SeriesGroup on commas, 1 collection each). ValueError otherwise."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        msg = f"series_group must be a name or a non-empty list of names, not {value!r}"
        raise ValueError(msg)
    for name in items:
        if not isinstance(name, str) or not name.strip():
            msg = f"series_group names must be non-empty strings, not {name!r}"
            raise ValueError(msg)
        if "," in name:
            msg = f"series_group name {name!r} has a comma: list the collections instead"
            raise ValueError(msg)
    return [name.strip() for name in items]


def check_volumes(volumes):
    """`volumes: {volume: range}`: each volume a whole number (ComicInfo Volume is an xs:int), each range one _range
    reads ("1-53", "54", "94-", "-53") and that does not run backwards. Raises ValueError otherwise."""
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


def override_volume(volumes, num):
    """The volume whose range holds chapter number num (the last such volume, as the bulk converter's loop leaves
    it), or None."""
    found = None
    for vol, rng in volumes.items():
        lo, hi = _range(rng)
        if lo <= num <= hi:
            found = names.to_volume(vol)
    return found


def _range(rng):
    """overrides range -> (lo, hi): "1-53", "54" (one chapter), "94-" (open end), "-53" (open start)."""
    lo, sep, hi = str(rng).strip().partition("-")
    lo = float(lo) if lo.strip() else float("-inf")
    hi = float(hi) if hi.strip() else (float("inf") if sep else lo)
    return lo, hi
