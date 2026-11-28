"""The names of a series' open chapters (SPEC "Publishing").

Loads the series' published files and open chapters as the resolver's rows (metadata/naming.py), with its override file
and stored MangaDex aggregate, and resolves them.
Every caller that needs a new chapter's volume goes through here: comicinfo, the library-copy check, validate's
mislabel check and the volume-cover supply (D7.21).
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from manga_pipeline import comicinfo, config, db, errors, names, volumes, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import naming, platforms
from manga_pipeline.metadata.records import MANGADEX_LIST

OPEN = ("ready", "claimed", "waiting")  # an unnamed needs_human chapter may never be published: no number
# A series' open download chapters, the resolver's open rows (params: series, open = list(OPEN)); a chapter at confirm
# is its published file's row already (publish wrote it), so it is not counted twice.
OPEN_WHERE = sql.SQL(
    """series_id = %(series)s AND kind = 'download' AND stage NOT IN ('discover', 'confirm')
    AND published_file_id IS NULL
    AND (status = ANY(%(open)s) OR (status = 'needs_human' AND naming_kind IS NOT NULL))"""
)
type Inputs = tuple[
    list[naming.Row[naming.RowKey]], volumes.Overrides | None, naming.MangaDex, platforms.PlatformList | None
]


def resolve(
    ctx: workflow.Context, series: db.Row, downloads: Mapping[int, str] | None = None, *, edited: bool = True
) -> dict[naming.RowKey, naming.Naming]:
    """Return the names of the series' open download chapters, with the series' hand edits.

    {chapter id: naming.Naming}; edited=False: as the sources alone name them. downloads: {chapter id: stored path} read
    instead of the chapter's own download (validate's re-download candidate). VolumeError (volumes.OverrideError
    included) for a malformed override file, an `override` basis without one, or a volume that is not whole.
    """
    rows, overrides, mangadex, platform = inputs(ctx, series, downloads)
    found = edits(ctx.conn, series["id"]) if edited else None
    return naming.resolve(series["naming_basis"], rows, overrides, mangadex, platform, edits=found)


def edits(conn: db.Connection, series_id: int) -> dict[naming.EditKey, naming.Edit]:
    """{naming.edit_key: naming.Edit} of the series' hand edits (SPEC "Retroactive updates", hand edits)."""
    return {
        naming.edit_key(names.SIDE if r["side"] else names.MAIN, r["season"], r["number"]): naming.Edit(
            r["title"], r["volume"]
        )
        for r in conn.execute(
            "SELECT season, side, number, title, volume FROM naming_edit WHERE series_id = %s", (series_id,)
        )
    }


def choose_basis(ctx: workflow.Context, series: db.Row, downloads: Mapping[int, str] | None = None) -> str:
    """Choose and store a new series' volume basis over its chapters known now (SPEC "Publishing").

    Kept when another run stored one first. downloads: as resolve's. Returns the series' basis. VolumeError for a
    malformed override file.
    """
    rows, overrides, mangadex, platform = inputs(ctx, series, downloads)
    basis = naming.choose(rows, overrides, mangadex, platform)
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE series SET naming_basis = %s WHERE id = %s AND naming_basis IS NULL", (basis, series["id"])
        )
        return db.one_row(ctx.conn, "SELECT naming_basis FROM series WHERE id = %s", (series["id"],))[0]


def inputs(
    ctx: workflow.Context,
    series: db.Row,
    downloads: Mapping[int, str] | None = None,
    *,
    planned: Sequence[naming.Row[naming.RowKey]] = (),
) -> Inputs:
    """(rows, override mapping or None, stored MangaDex data, stored platform list or None) of the series.

    planned: published rows not stored yet (an adoption's files), added to the stored ones.
    """
    overrides = volumes.override_file(config.abs_path(ctx.cfg["paths"]["naming_overrides"]), series["rules_key"])
    mangadex = mangadex_data(ctx.conn, series["id"])
    platform = platform_list(ctx.conn, series["id"])
    # ponytail: each call reads every open unnamed chapter's ComicInfo.xml; a series with hundreds of open chapters
    # (a back catalogue) costs that many reads per chapter; store the read fields at validate if that bites
    rows = [*_published(ctx.conn, series["id"]), *planned, *_open(ctx, series["id"], downloads or {})]
    return rows, overrides, mangadex, platform


def mangadex_data(conn: db.Connection, series_id: int) -> naming.MangaDex:
    """Return the series' stored MangaDex volumes and English titles (none of either when not stored)."""
    aggregate = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND source = 'mangadex' AND kind = 'aggregate'", (series_id,)
    ).fetchone()
    source, kind = MANGADEX_LIST
    chapters = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND source = %s AND kind = %s", (series_id, source, kind)
    ).fetchone()
    return naming.MangaDex(
        naming.mangadex_volumes(aggregate["payload"] if aggregate else None),
        naming.mangadex_titles(chapters["payload"] if chapters else None),
    )


def platform_list(conn: db.Connection, series_id: int) -> platforms.PlatformList | None:
    """Return the series' stored platform list, None when it has none."""
    row = conn.execute(
        "SELECT payload FROM fetched WHERE series_id = %s AND kind = %s", (series_id, platforms.KIND)
    ).fetchone()
    return row["payload"] if row else None


def _published(conn: db.Connection, series_id: int) -> list[naming.Row[naming.RowKey]]:
    out: list[naming.Row[naming.RowKey]] = []
    for r in conn.execute("SELECT * FROM published_file WHERE series_id = %s ORDER BY id", (series_id,)):
        if r["comicinfo_number"] is None:
            continue
        _, season, side, _, _ = names.read_identity(r["identity"])
        out.append(
            naming.Row(
                key=("published", r["id"]),
                settled=True,
                kind=r["kind"] or (names.SIDE if side else names.MAIN),  # a pipeline row from before step 0054
                number=float(r["comicinfo_number"]),
                season=None if season is None else int(float(season)),
                src_volume=None,
                volume=names.to_volume(r["comicinfo_volume"]),
                volume_source=r["volume_source"],
                marker_title=r["source_name"] or "",
                title=r["comicinfo_title"] or "",
                date=r["comicinfo_date"],
                order=r["id"],
            )
        )
    return out


def _open(ctx: workflow.Context, series_id: int, downloads: Mapping[int, str]) -> list[naming.Row[naming.RowKey]]:
    out: list[naming.Row[naming.RowKey]] = []
    chapters = ctx.conn.execute(
        sql.SQL("SELECT * FROM chapter WHERE {open} ORDER BY id").format(open=OPEN_WHERE),
        {"series": series_id, "open": list(OPEN)},
    )
    for c in chapters:
        if c["naming_kind"] is not None:  # comicinfo named it: fixed from then on (held or not, it keeps the name)
            _, season, _, _, _ = names.read_identity(c["identity"])
            out.append(
                naming.Row(
                    key=c["id"],
                    settled=True,
                    kind=c["naming_kind"],
                    number=float(c["comicinfo_number"]),
                    season=None if season is None else int(float(season)),
                    src_volume=None,
                    volume=names.to_volume(c["comicinfo_volume"]),
                    volume_source=c["naming_volume_source"],
                    marker_title=Path(c["download_path"]).stem,
                    title=c["comicinfo_title"] or "",
                    date=c["comicinfo_date"],
                    order=c["id"],
                )
            )
            continue
        path = config.to_local(ctx.cfg, downloads.get(c["id"], c["download_path"]))
        try:
            fields, item = comicinfo.read_download(path)
        except FileNotFoundError as e:  # a chapter between a rejected download and its re-download: its name counts
            if errors.dropped_share(e):
                raise
            fields, item = {}, names.parse(path.stem)
        number = item.num
        if item.kind not in (names.MAIN, names.SIDE) or number is None:
            continue
        out.append(
            naming.Row(
                key=c["id"],
                settled=False,
                kind=item.kind,
                number=number,
                season=int(item.season) if item.season else None,
                src_volume=item.volume if item.volume is not None else fields.get("Volume"),
                volume=None,
                volume_source=None,
                # the bulk converter's marker title: the ComicInfo Title without its own-number label, else the name's
                marker_title=names.title_from_comicinfo(fields.get("Title") or "", number) or item.title,
                title=names.chapter_title(item, fields.get("Title")),
                date=names.ci_date(fields),
                order=c["id"],
            )
        )
    return out


def download_volume(cfg: config.Config, download: str) -> int | float | None:
    """Return the volume a download gives itself (its name's, else its ComicInfo Volume) when it is a main chapter.

    Else None (a side story's volume is assigned). A ComicInfo.xml that cannot be read gives none of its own.
    VolumeError for a volume that is not whole.
    """
    path = config.to_local(cfg, download)
    fields, item = comicinfo.read_download(path)
    if item.kind != names.MAIN:
        return None
    volume = names.to_volume(item.volume if item.volume is not None else fields.get("Volume"))
    if volume is not None and volume != int(volume):
        msg = f"{path.name!r}: volume {names.fmt_num(volume)} is not a whole number"
        raise volumes.VolumeError(msg)
    return volume


def same_chapter(ours: Callable[[], float | None], theirs: str | None, theirs_source: str | None) -> bool:
    """Return whether a published file of a download's key (season, side label, number) is the same chapter.

    SPEC "Library copies", D7.21, user 2026-11-15: 2 volumes that both come from the files, the download's own (ours: a
    callable giving it, read only when compared) and a published file's from its file name, must not differ; a volume a
    source or rule assigned (MangaDex, an override, seasons) never splits a chapter, since a source may move a chapter
    between volumes later.
    """
    volume = names.to_volume(theirs)
    if volume is None or theirs_source != naming.FILENAME:
        return True
    mine = ours()
    return mine is None or mine == volume
