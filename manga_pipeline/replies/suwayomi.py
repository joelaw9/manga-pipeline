"""Suwayomi's replies (SPEC "Services"): the fields the program's GraphQL queries ask for.

A GraphQL reply carries every field its query asks for; nullable ones are as the schema declares them (local
library suwayomi-graphql.md). Type-only; the program reads each reply unchecked.
"""

from typing import TypedDict


class Source(TypedDict):
    """A download source: its id (a LongString), name, language and displayName ("Name (LANG)")."""

    id: str
    name: str
    lang: str
    displayName: str


class TrackRecord(TypedDict):
    """A manga's tracker entry: the tracker's id and the series' id there (a LongString)."""

    trackerId: int
    remoteId: str


class TrackRecords(TypedDict):
    """A manga's tracker entries."""

    nodes: list[TrackRecord]


class Manga(TypedDict):
    """A manga: source is null when its extension is not installed; sourceId is always there."""

    id: int
    title: str
    status: str
    url: str
    inLibrary: bool
    sourceId: str
    source: Source | None
    trackRecords: TrackRecords


class Chapter(TypedDict):
    """A chapter: uploadDate is epoch milliseconds as text; scanlator and realUrl can be null."""

    id: int
    mangaId: int
    name: str
    scanlator: str | None
    chapterNumber: float
    sourceOrder: int
    isDownloaded: bool
    pageCount: int
    uploadDate: str
    url: str
    realUrl: str | None


class QueuedChapter(TypedDict):
    """The chapter of a download queue entry."""

    id: int
    mangaId: int


class QueueEntry(TypedDict):
    """A download queue entry: its state (QUEUED, DOWNLOADING, FINISHED, ERROR), tries, progress, place and chapter."""

    state: str
    tries: int
    progress: float
    position: int
    chapter: QueuedChapter


class DownloadStatus(TypedDict):
    """The downloader's state (STARTED or STOPPED) and its queue."""

    state: str
    queue: list[QueueEntry]


class MangaAndChapters(TypedDict):
    """A manga and its chapter list, as fetchMangaAndChapters refreshed them."""

    manga: Manga
    chapters: list[Chapter]
