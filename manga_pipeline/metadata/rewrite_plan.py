"""Which published files' volumes should change (SPEC "Retroactive updates"): pure over the resolver's rows.

A published main chapter's volume changes only from none to a volume (the gap fill, or the series' basis newly giving
one) or to its override range's volume. A source moving or dropping a published volume changes nothing; `differences`
lists it for `naming diff`.
"""

import dataclasses

from manga_pipeline import names
from manga_pipeline.metadata import naming


def changes(basis, rows, overrides, mangadex, platform=None):
    """{published file id: (volume, volume source)} for every published main row whose volume should change. rows: the
    resolver's rows of the series (published rows keyed ("published", id)). VolumeError as naming.resolve raises it."""
    override = (overrides or {}).get("volumes") or {}
    published = [i for i, r in enumerate(rows) if _published(r) and r.kind == names.MAIN]
    out = {}
    for i, v in naming.override_volumes(override, rows, published).items():
        if v != rows[i].volume:
            out[rows[i].key[1]] = (v, naming.OVERRIDE)
    empty = [i for i in published if rows[i].volume is None and rows[i].key[1] not in out]
    for key, named in _reopened(basis, rows, empty, overrides, mangadex, platform=platform).items():
        v = named.volume
        if named.kind == names.MAIN and v is not None and v == int(v):
            out[key[1]] = (int(v), named.volume_source)
    return out


def edit_changes(rows, edits, unedited):
    """{published file id: (volume, volume source, title, unedited)} of the published files (main and side) whose
    title or volume a hand edit, or a hand edit's removal, changes (SPEC "Retroactive updates", hand edits). edits:
    {naming.edit_key: naming.Edit}; unedited: {file id: the values its sources gave its edited fields}. An edited field
    takes the edit; a field whose edit is gone takes its unedited value back; the result's unedited is what the file
    keeps afterwards (None: nothing edited)."""
    out = {}
    for r in rows:
        if not _published(r):
            continue
        fid = r.key[1]
        was = dict(unedited.get(fid) or {})
        e = edits.get(naming.edit_key(r.kind, r.season, r.number))
        volume, source, title = r.volume, r.volume_source, r.title
        if e is not None and e.volume is not None and "volume" in was and e.volume == names.to_volume(was["volume"]):
            volume, source = names.to_volume(was.pop("volume")), was.pop("volume_source")  # edited back: the sources'
        elif e is not None and e.volume is not None:
            if e.volume != r.volume:
                was.setdefault("volume", names.fmt_num(r.volume))
                was.setdefault("volume_source", r.volume_source)
                volume, source = e.volume, naming.EDIT
        elif "volume" in was:
            volume, source = names.to_volume(was.pop("volume")), was.pop("volume_source")
        if e is not None and e.title is not None:
            want = naming.edited_title(r.kind, r.number, e.title)
            if want != r.title:
                was.setdefault("title", r.title or None)
                title = want
        elif "title" in was:
            title = was.pop("title") or ""
        if (volume, source, title) != (r.volume, r.volume_source, r.title):
            out[fid] = (volume, source, title, was or None)
    return out


def held(rows, edits, unedited):
    """The published file ids whose volume a hand edit holds (edited, or to be restored): 7e's own volume rules leave
    them alone."""
    return {
        r.key[1]
        for r in rows
        if _published(r)
        and (
            ((e := edits.get(naming.edit_key(r.kind, r.season, r.number))) is not None and e.volume is not None)
            or "volume" in (unedited.get(r.key[1]) or {})
        )
    }


def differences(basis, rows, overrides, mangadex, platform=None):
    """{published file id: (stored volume, the volume the sources give now)} of the published main rows holding a
    volume from a source that can be read again (MangaDex, a platform list) that the sources would move or drop, which
    7e never applies (SPEC "Retroactive updates"). A volume from the files themselves cannot be read again: the record
    keeps no download."""
    override = (overrides or {}).get("volumes") or {}
    published = [i for i, r in enumerate(rows) if _published(r) and r.kind == names.MAIN]
    ranged = naming.override_volumes(override, rows, published)
    rereadable = (naming.MANGADEX, *naming.platforms.BASES)
    held = [
        i for i in published if rows[i].volume is not None and i not in ranged and rows[i].volume_source in rereadable
    ]
    out = {}
    for key, named in _reopened(basis, rows, held, overrides, mangadex, platform=platform).items():
        stored = next(r.volume for r in rows if r.key == key)
        if named.volume != stored:
            out[key[1]] = (stored, named.volume)
    return out


def cover_changes(basis, rows, overrides, mangadex, moved, *, states, taken, images):
    """{published file id: ("insert", volume) | ("drop", None)} of the published main files whose volume cover should
    change (SPEC "Retroactive updates"). moved: changes' result; states: {file id: cover_state} (every one known);
    taken: the volumes whose cover an open chapter took; images: {volume: sha256} of the series' stored cover images.
    A covered volume whose files hold none of its cover gets it in its lowest-numbered published file; a file holding
    another volume's cover drops it, unless it is that file; every other file keeps what it holds. A file holding an
    old image of a volume (stale: MangaDex had a new one) takes the new image when the volume is its own, else drops
    it. An insert recorded unfit for the volume's current image is not made again."""
    override = (overrides or {}).get("volumes") or {}
    now = list(rows)
    for i, r in enumerate(rows):
        if _published(r) and r.key[1] in moved:
            now[i] = dataclasses.replace(r, volume=moved[r.key[1]][0], volume_source=moved[r.key[1]][1])
    published = sorted(
        (i for i, r in enumerate(now) if _published(r) and r.kind == names.MAIN),
        key=lambda i: (now[i].number, now[i].order),
    )
    volume = {now[i].key[1]: _fmt(now[i].volume) for i in published}
    held = {fid: (states[fid] or "").removeprefix("volume:") for fid in volume if states[fid].startswith("volume:")}
    stale = {fid: states[fid].removeprefix("stale:") for fid in volume if states[fid].startswith("stale:")}
    chosen = {}
    for covered in naming.covered_volumes(basis, now, override, mangadex):
        v = _fmt(covered)
        files = [fid for fid in (now[i].key[1] for i in published) if volume[fid] == v]
        if v in taken or not files or any(v in (held.get(fid), stale.get(fid)) for fid in files):
            continue
        chosen[files[0]] = v
    out = {}
    for fid, vol in volume.items():
        if fid in chosen:
            v = chosen[fid]
            if held.get(fid) != v and not (v in images and states[fid] == f"unfit:{images[v]}"):
                out[fid] = ("insert", v)
        elif fid in stale and stale[fid] == vol and stale[fid] in images:
            out[fid] = ("insert", stale[fid])  # its volume's new image replaces the old one
        elif (fid in held and held[fid] != vol) or fid in stale:
            out[fid] = ("drop", None)  # another volume's cover (an old image included)
    return out


def _fmt(volume):
    return "" if volume is None else names.fmt_num(volume)


def _reopened(basis, rows, ids, overrides, mangadex, *, platform):
    """{key: naming.Naming} of the rows ids, named as open rows beside the others as they are."""
    if not ids:
        return {}
    chosen = set(ids)
    opened = [
        dataclasses.replace(r, settled=False, volume=None, volume_source=None) if i in chosen else r
        for i, r in enumerate(rows)
    ]
    keys = frozenset(rows[i].key for i in ids)
    got = naming.resolve(basis, opened, overrides, mangadex, platform, fill=keys)
    return {key: named for key, named in got.items() if key in keys}


def _published(row):
    return isinstance(row.key, tuple) and row.key[0] == "published"
