"""A new chapter's volume (SPEC "Publishing"): the bulk converter's rule for a chapter next to recorded ones (its
optimizer/naming.py `_basis_volumes`), from the series' recorded volume basis, for the bases known without a network
source or the whole series: the file's own volume, its season tag, the series' override file (in
paths.naming_overrides, the bulk converter's folder), the side-story default. Any other basis, and a side story,
gives no volume (the metadata phase resolves those).
"""

from pathlib import Path
from xml.etree.ElementTree import ParseError

import yaml

from manga_pipeline import archive, comicinfo, names

FILENAME, SEASON_TAGS, OVERRIDE = "filename", "season tags", "override"
SIDE_DEFAULT = "side stories need volumes: main story = Vol 1"


class VolumeError(ValueError):
    """The chapter's volume cannot be written: no further attempt changes that, a person must act."""


class OverrideError(VolumeError):
    """The series' override file is missing or not one the bulk converter would accept: a person must fix it."""


def chapter_volume(series, item, ci_volume, overrides):
    """The volume of a main chapter by series["naming_basis"], or None. ci_volume: its download's ComicInfo Volume;
    overrides: the folder of override files (paths.naming_overrides).
    VolumeError for a file-name volume that is not a whole number; OverrideError for an override series whose file is
    missing or malformed."""
    if item.kind != names.MAIN:
        return None
    basis = series["naming_basis"]
    if basis == FILENAME:
        volume = names.to_volume(item.volume if item.volume is not None else ci_volume)
        if volume is not None and volume != int(volume):
            # the bulk converter's plan error too: ComicInfo Volume is an xs:int
            raise VolumeError(f"{item.file!r}: volume {names.fmt_num(volume)} is not a whole number")
        return volume
    if basis == SEASON_TAGS:
        return int(item.season) if item.season else None
    if basis == SIDE_DEFAULT:
        return 1
    if basis == OVERRIDE:
        return override_volume(override_volumes(overrides, series["rules_key"]), item.num)
    return None


def same_chapter(basis, kind, ours, theirs):
    """Whether a published file of a download's key (season, side label, number) is the same chapter (SPEC "Library
    copies"), the bulk converter's rule (its optimizer/structure.py `_same_scope`): in a series whose volume basis is
    `filename`, a main chapter's volume (ours) and the file's (theirs) must not differ where both have one. A side
    story's volume, and every volume under another basis, is assigned: there the key alone decides."""
    return basis != FILENAME or kind != names.MAIN or ours is None or theirs is None or ours == theirs


def same_as_published(series, download, published_volume):
    """same_chapter for a download and a published file of its key (published_volume: its ComicInfo Volume as
    written). The download is read only in a `filename` series, for the volume the comicinfo stage would write; a
    ComicInfo.xml that cannot be read gives no volume of its own (the name's still counts; the comicinfo stage reports
    it). VolumeError for a volume that is not a whole number."""
    if series["naming_basis"] != FILENAME:
        return True
    try:
        fields, item = comicinfo.read_download(download)
    except (*archive.UNREADABLE, ParseError):
        fields, item = {}, names.parse(Path(download).stem)
    ours = chapter_volume(series, item, fields.get("Volume"), None)  # a `filename` series reads no override file
    return same_chapter(FILENAME, item.kind, ours, names.to_volume(published_volume))


def override_volumes(overrides, folder_name):
    """The `volumes` mapping of the series' override file ({} when it has none). OverrideError when the file is
    missing or malformed."""
    path = Path(overrides) / f"{folder_name}.yaml"
    if not path.is_file():
        raise OverrideError(f"the series' volume basis is its override file, and {path} does not exist")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"overrides must be a YAML mapping, not {type(data).__name__}")
        check_volumes(data.get("volumes"))
    except (yaml.YAMLError, ValueError) as e:
        raise OverrideError(f"{path}: {e}") from None
    return data.get("volumes") or {}


def check_volumes(volumes):
    """`volumes: {volume: range}`: each volume a whole number (ComicInfo Volume is an xs:int), each range one _range
    reads ("1-53", "54", "94-", "-53") and that does not run backwards. Raises ValueError otherwise."""
    if volumes is None:
        return
    if not isinstance(volumes, dict):
        raise ValueError(f"volumes must be a mapping of volume: chapter range, not {volumes!r}")
    for vol, rng in volumes.items():
        if (
            isinstance(vol, bool)
            or not (isinstance(vol, int) or isinstance(vol, str) and vol.isascii() and vol.isdigit())
            or int(vol) < 0
        ):
            raise ValueError(f"volumes key {vol!r} is not a volume number (a whole number)")
        try:
            lo, hi = _range(rng)
        except ValueError:
            raise ValueError(f"volumes {vol}: range {rng!r} is not a chapter range (1-53, 54, 94-, -53)") from None
        if lo > hi:
            raise ValueError(f"volumes {vol}: range {rng!r} runs backwards")


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
