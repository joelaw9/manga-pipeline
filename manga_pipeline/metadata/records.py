"""A series' source records (SPEC "Metadata checks"): what a check fetches, by name, and where `fetched` keeps each.

A record a source does not have (no id, not found, a novel's) is None. Types and the stored names only.
"""

from typing import Literal, TypedDict

from manga_pipeline.replies.sources import (
    AniListMedia,
    MangaBakaSeries,
    MangaDexAggregate,
    MangaDexCover,
    MangaDexManga,
    MangaUpdatesSeries,
)


class SeriesRecords(TypedDict):
    """The records a series' fields come from (metadata/merge.py, metadata/tags.py)."""

    mangabaka: MangaBakaSeries | None
    mangaupdates: MangaUpdatesSeries | None
    mangadex: MangaDexManga | None
    anilist: AniListMedia | None


class Records(SeriesRecords):
    """Every record a check fetches: the series' records, and MangaDex's aggregate and cover list."""

    mangadex_aggregate: MangaDexAggregate | None
    mangadex_covers: list[MangaDexCover] | None


# Each source's own record (SeriesRecords' fields), in the order a check reports them: a check reports a source found
# by it.
type SourceRecord = Literal["mangabaka", "mangaupdates", "mangadex", "anilist"]
SOURCE_RECORDS: tuple[SourceRecord, ...] = ("mangabaka", "mangaupdates", "mangadex", "anilist")
# {record name: (source, kind)} it is stored under in `fetched`.
STORED = {
    "mangabaka": ("mangabaka", "series"),
    "mangaupdates": ("mangaupdates", "series"),
    "mangadex": ("mangadex", "manga"),
    "mangadex_aggregate": ("mangadex", "aggregate"),
    "mangadex_covers": ("mangadex", "covers"),
    "anilist": ("anilist", "media"),
}
