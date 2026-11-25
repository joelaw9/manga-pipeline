"""Suwayomi's replies (SPEC "Services"): manga, chapters, sources, the download queue and page lists of its GraphQL API.

A GraphQL reply carries every field its query asks for, nullable ones as the schema declares them (local library
suwayomi-graphql.md). Types, and the validator of the page list reply.
"""

from collections.abc import Mapping
from typing import TypedDict

from manga_pipeline.replies import Json

SHOWN = 200  # characters of a wrong reply an error shows


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
    """A manga: source is null when its extension is not installed; sourceId always there; description null or text."""

    id: int
    title: str
    status: str
    url: str
    description: str | None
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


class ChapterPages(TypedDict):
    """The data of fetchChapterPages: the chapter's page URLs, relative to Suwayomi's base URL, in reading order."""

    pages: list[str]


class ReplyShapeError(Exception):
    """A reply that is not the shape its query asks for."""


def check_chapter_pages(reply: Json) -> None:
    """Check a fetchChapterPages reply: errors (read by the caller), or data whose pages are paths starting with "/".

    ReplyShapeError naming what is wrong otherwise.
    """
    if isinstance(reply, Mapping) and "errors" in reply:
        return
    data = reply.get("data") if isinstance(reply, Mapping) else None
    call = data.get("fetchChapterPages") if isinstance(data, Mapping) else None
    pages = call.get("pages") if isinstance(call, Mapping) else None
    if not isinstance(pages, list) or not all(isinstance(p, str) and p.startswith("/") for p in pages):
        msg = f"fetchChapterPages reply holds no list of page paths: {str(reply)[:SHOWN]}"
        raise ReplyShapeError(msg)
