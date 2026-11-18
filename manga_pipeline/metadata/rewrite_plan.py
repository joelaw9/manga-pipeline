"""Which published files' volumes should change (SPEC "Retroactive updates"): pure over the resolver's rows.

A published main chapter's volume changes only from none to a volume (the gap fill, or the series' basis newly giving
one) or to its override range's volume. A source moving or dropping a published volume changes nothing; `differences`
lists it for `naming diff`.
"""

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Literal

from manga_pipeline import names, volumes
from manga_pipeline.metadata import naming, platforms

HELD, STALE = "volume:", "stale:"  # cover_state prefixes: the volume's current image, an old one
# cover_changes' act on 1 file: an insert names its volume as text (fmt_num) and as its number
type CoverAct = tuple[Literal["insert"], str, float] | tuple[Literal["drop"], None]


def changes(
    basis: str | None,
    rows: Sequence[naming.Row[naming.RowKey]],
    overrides: volumes.Overrides | None,
    mangadex: Mapping[str, str | None],
    platform: platforms.PlatformList | None = None,
) -> dict[int, tuple[float, str]]:
    """Return {published file id: (volume, volume source)} for every published main row whose volume should change.

    rows: the resolver's rows of the series (published rows keyed ("published", id)). VolumeError as naming.resolve
    raises it.
    """
    override = (overrides or {}).get("volumes") or {}
    published = {i: fid for i, r in enumerate(rows) if (fid := _file_id(r)) is not None and r.kind == names.MAIN}
    out: dict[int, tuple[float, str]] = {}
    for i, v in naming.override_volumes(override, rows, published).items():
        if v != rows[i].volume:
            out[published[i]] = (v, naming.OVERRIDE)
    empty = [i for i, fid in published.items() if rows[i].volume is None and fid not in out]
    got = _reopened(basis, rows, overrides, mangadex, platform, ids=empty)
    for i, named in got.items():
        v = named.volume
        if named.kind == names.MAIN and v is not None and v == int(v):
            out[published[i]] = (int(v), named.volume_source)
    return out


def edit_changes(
    rows: Iterable[naming.Row[naming.RowKey]],
    edits: Mapping[naming.EditKey, naming.Edit],
    unedited: Mapping[int, naming.Unedited | None],
) -> dict[int, tuple[float | None, str | None, str, naming.Unedited | None]]:
    """Return {published file id: (volume, volume source, title, unedited)} of the files a hand edit changes.

    The published files (main and side) whose title or volume a hand edit, or a hand edit's removal, changes (SPEC
    "Retroactive updates", hand edits). edits: {naming.edit_key: naming.Edit}; unedited: {file id: the values its
    sources gave its edited fields}. An edited field takes the edit; a field whose edit is gone takes its unedited value
    back; the result's unedited is what the file keeps afterwards (None: nothing edited).
    """
    out: dict[int, tuple[float | None, str | None, str, naming.Unedited | None]] = {}
    for r in rows:
        fid = _file_id(r)
        if fid is None:
            continue
        old = unedited.get(fid)
        was: naming.Unedited = old.copy() if old else {}
        e = edits.get(naming.edit_key(r.kind, r.season, r.number))
        volume, source = _edited_volume(r, None if e is None else e.volume, was)
        title = _edited_title(r, None if e is None else e.title, was)
        if (volume, source, title) != (r.volume, r.volume_source, r.title):
            out[fid] = (volume, source, title, was or None)
    return out


def _edited_volume(
    row: naming.Row[naming.RowKey], edit: float | None, was: naming.Unedited
) -> tuple[float | None, str | None]:
    """Return (volume, volume source) a published row takes from its volume's edit.

    edit: the edited volume, None when not edited. The edit, its sources' volume back when the edit gives that or is
    gone, else its own. was (the file's unedited values) keeps what the edit replaced and loses what it gives back.
    """
    if edit is not None and "volume" in was and edit == names.to_volume(was["volume"]):
        return names.to_volume(was.pop("volume")), was.pop("volume_source")  # edited back: the sources'
    if edit is not None:
        if edit == row.volume:
            return row.volume, row.volume_source
        was.setdefault("volume", names.fmt_num(row.volume))
        was.setdefault("volume_source", row.volume_source)
        return edit, naming.EDIT
    if "volume" in was:
        return names.to_volume(was.pop("volume")), was.pop("volume_source")
    return row.volume, row.volume_source


def _edited_title(row: naming.Row[naming.RowKey], edit: str | None, was: naming.Unedited) -> str:
    """Return the title a published row takes from its title's edit.

    edit: the edited title, None when not edited. The edit, its sources' title back when the edit is gone, else its own.
    was (the file's unedited values) keeps what the edit replaced and loses what it gives back.
    """
    if edit is not None:
        want = naming.edited_title(row.kind, row.number, edit)
        if want != row.title:
            was.setdefault("title", row.title or None)
        return want
    if "title" in was:
        return was.pop("title") or ""
    return row.title


def held(
    rows: Iterable[naming.Row[naming.RowKey]],
    edits: Mapping[naming.EditKey, naming.Edit],
    unedited: Mapping[int, naming.Unedited | None],
) -> set[int]:
    """Return the published file ids whose volume a hand edit holds (edited, or to be restored).

    7e's own volume rules leave them alone.
    """
    return {
        fid
        for r in rows
        if (fid := _file_id(r)) is not None
        and (
            ((e := edits.get(naming.edit_key(r.kind, r.season, r.number))) is not None and e.volume is not None)
            or "volume" in (unedited.get(fid) or {})
        )
    }


def differences(
    basis: str | None,
    rows: Sequence[naming.Row[naming.RowKey]],
    overrides: volumes.Overrides | None,
    mangadex: Mapping[str, str | None],
    platform: platforms.PlatformList | None = None,
) -> dict[int, tuple[float | None, float | None]]:
    """Return {published file id: (stored volume, the volume the sources give now)} the sources would move.

    The published main rows holding a volume from a source that can be read again (MangaDex, a platform list) that the
    sources would move or drop, which 7e never applies (SPEC "Retroactive updates"). A volume from the files themselves
    cannot be read again: the record keeps no download.
    """
    override = (overrides or {}).get("volumes") or {}
    published = {i: fid for i, r in enumerate(rows) if (fid := _file_id(r)) is not None and r.kind == names.MAIN}
    ranged = naming.override_volumes(override, rows, published)
    rereadable = (naming.MANGADEX, *naming.platforms.BASES)
    held = [
        i for i in published if rows[i].volume is not None and i not in ranged and rows[i].volume_source in rereadable
    ]
    got = _reopened(basis, rows, overrides, mangadex, platform, ids=held)
    out: dict[int, tuple[float | None, float | None]] = {}
    for i, named in got.items():
        stored = rows[i].volume
        if named.volume != stored:
            out[published[i]] = (stored, named.volume)
    return out


def moved_rows(
    rows: Iterable[naming.Row[naming.RowKey]], moved: Mapping[int, tuple[float | None, str | None]]
) -> list[naming.Row[naming.RowKey]]:
    """Return the rows with each published file in moved at its new volume.

    moved: changes' result, {file id: (volume, volume source)}.
    """
    return [
        dataclasses.replace(r, volume=moved[fid][0], volume_source=moved[fid][1])
        if (fid := _file_id(r)) is not None and fid in moved
        else r
        for r in rows
    ]


def cover_changes(
    rows: Sequence[naming.Row[naming.RowKey]],
    covered: Iterable[float],
    *,
    states: Mapping[int, str],
    taken: AbstractSet[str],
    images: Mapping[str, str],
) -> dict[int, CoverAct]:
    """Return {published file id: ("insert", volume, number) | ("drop", None)} of the files whose cover should change.

    The published main files whose volume cover should change (SPEC "Retroactive updates"). rows: the resolver's rows as
    the volume changes leave them (moved_rows); covered: their covered volumes (naming.covered_volumes); states: {file
    id: cover_state} (every one known); taken: the volumes whose cover an open chapter took; images: {volume: sha256} of
    the series' stored cover images. A covered volume whose files hold none of its cover gets it in its lowest-numbered
    published file; a file holding another volume's cover drops it, unless it is that file; every other file keeps what
    it holds. A file holding an old image of a volume (stale: MangaDex had a new one) takes the new image when the
    volume is its own, else drops it. An insert recorded unfit for the volume's current image is not made again.
    """
    fids = {i: fid for i, r in enumerate(rows) if (fid := _file_id(r)) is not None and r.kind == names.MAIN}
    published = sorted(fids, key=lambda i: (rows[i].number, rows[i].order))
    volume = {fids[i]: _fmt(rows[i].volume) for i in published}
    number = {fids[i]: rows[i].volume for i in published}
    homes = _cover_homes(volume, covered, taken, states)
    out: dict[int, CoverAct] = {}
    for fid, vol in volume.items():
        act = _cover_act(states[fid], (vol, number[fid]), homes.get(fid), images)
        if act is not None:
            out[fid] = act
    return out


def _cover_homes(
    volume: Mapping[int, str], covered: Iterable[float], taken: AbstractSet[str], states: Mapping[int, str]
) -> dict[int, tuple[str, float]]:
    """Return {file id: (volume text, volume)} of the files that should get a covered volume's cover.

    The volume's first file (volume: {file id: volume text} in number order), unless an open chapter took the volume's
    cover or one of its files holds it already (its current image or an old one).
    """
    homes: dict[int, tuple[str, float]] = {}
    for n in covered:
        v = _fmt(n)
        files = [fid for fid, vol in volume.items() if vol == v]
        holds = (_cover_of(states[fid], prefix) for fid in files for prefix in (HELD, STALE))
        if v in taken or not files or v in holds:
            continue
        homes[files[0]] = (v, n)
    return homes


def _cover_act(
    state: str, own: tuple[str, float | None], home: tuple[str, float] | None, images: Mapping[str, str]
) -> CoverAct | None:
    """Return ("insert", volume, number) | ("drop", None) | None of 1 published file with cover state state.

    own: the file's volume (text, and its number: None for no volume); home: the covered volume whose cover it should
    get (_cover_homes), else None.
    """
    held, stale = _cover_of(state, HELD), _cover_of(state, STALE)
    volume, number = own
    if home is not None:
        unfit = home[0] in images and state == f"unfit:{images[home[0]]}"
        return ("insert", *home) if held != home[0] and not unfit else None
    # a file with no volume ("") has no image: images holds whole volumes' only
    if stale is not None and number is not None and stale == volume and stale in images:
        return ("insert", stale, number)  # its volume's new image replaces the old one
    if (held is not None and held != volume) or stale is not None:
        return ("drop", None)  # another volume's cover (an old image included)
    return None


def _cover_of(state: str, prefix: str) -> str | None:
    """Return the volume text of a cover state with prefix, else None.

    prefix: HELD, the volume's current image; STALE, an old one.
    """
    return state.removeprefix(prefix) if state.startswith(prefix) else None


def _fmt(volume: float | None) -> str:
    return "" if volume is None else names.fmt_num(volume)


def _reopened(  # noqa: PLR0913  # the resolver's inputs: 1 place resolves the reopened rows for changes and differences
    basis: str | None,
    rows: Sequence[naming.Row[naming.RowKey]],
    overrides: volumes.Overrides | None,
    mangadex: Mapping[str, str | None],
    platform: platforms.PlatformList | None,
    *,
    ids: Sequence[int],
) -> dict[int, naming.Naming]:
    """Return {row index: naming.Naming} of the rows with ids as the resolver names them reopened (no volume).

    Beside the others as they are: changes and differences read them the same way. Its inputs are the resolver's own
    (naming.resolve).
    """
    if not ids:
        return {}
    chosen = set(ids)
    opened = [
        dataclasses.replace(r, settled=False, volume=None, volume_source=None) if i in chosen else r
        for i, r in enumerate(rows)
    ]
    index = {rows[i].key: i for i in ids}
    named = naming.resolve(basis, opened, overrides, mangadex, platform, fill=index.keys())
    return {i: n for key, n in named.items() if (i := index.get(key)) is not None}


def _file_id(row: naming.Row[naming.RowKey]) -> int | None:
    """Return the published file id of a row keyed ("published", id), else None."""
    key = row.key
    return key[1] if isinstance(key, tuple) and key[0] == "published" else None
