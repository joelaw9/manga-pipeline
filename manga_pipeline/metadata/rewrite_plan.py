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
    for key, named in _reopened(basis, rows, empty, overrides, mangadex, platform).items():
        v = named.volume
        if named.kind == names.MAIN and v is not None and v == int(v):
            out[key[1]] = (int(v), named.volume_source)
    return out


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
    for key, named in _reopened(basis, rows, held, overrides, mangadex, platform).items():
        stored = next(r.volume for r in rows if r.key == key)
        if named.volume != stored:
            out[key[1]] = (stored, named.volume)
    return out


def _reopened(basis, rows, ids, overrides, mangadex, platform):
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
