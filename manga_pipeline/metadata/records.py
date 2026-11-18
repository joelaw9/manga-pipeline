"""A series' source records (SPEC "Metadata checks"): what a check fetches, by name, and where `fetched` keeps each.

A record a source does not have (no id, not found, a novel's) is None. Types and the stored names only.
"""

from typing import TypedDict

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


# {record name: (source, kind)} it is stored under in `fetched`. A record named for its source is that source's own:
# the one a check reports the source found by.
STORED = {
    "mangabaka": ("mangabaka", "series"),
    "mangaupdates": ("mangaupdates", "series"),
    "mangadex": ("mangadex", "manga"),
    "mangadex_aggregate": ("mangadex", "aggregate"),
    "mangadex_covers": ("mangadex", "covers"),
    "anilist": ("anilist", "media"),
}
