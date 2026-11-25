"""Suwayomi GraphQL client and its download file names (SPEC "Services").

Field and input names are Suwayomi-Server's schema at v2.4.2366 (introspected). 1 object per run: it keeps whether
Suwayomi was found unreachable. Every call returns plain dicts shaped like the GraphQL reply.
"""

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from http import HTTPStatus
from typing import NotRequired, TypedDict

from manga_pipeline import config
from manga_pipeline.clients import http
from manga_pipeline.replies import suwayomi as replies
from manga_pipeline.replies.suwayomi import Chapter, ChapterPages, DownloadStatus, Manga, MangaAndChapters, Source

PAGE_SIZE = 50  # library manga per query: the whole library in 1 query times out
SOURCE_TIMEOUT = 120  # seconds; a source search or chapter fetch waits on the source's site
# Suwayomi's own calls: its library update starves its 6-connection database pool for minutes at a time (up to ~35 min
# seen, 2026-11-14), and it then answers late or not at all. A call that reaches a source keeps http.WAITS.
WAITS = (10, 20, 60, 180, 300)
MAX_NAME_BYTES = 240  # UTF-8 bytes
MANGAS_FOLDER = "mangas"  # the folder of downloads under Suwayomi's download root
REPLACED = frozenset('"*/:<>?\\|\x7f')
MANGA = (
    "id title status url inLibrary sourceId source { id name lang displayName }"
    " trackRecords { nodes { trackerId remoteId } }"
)
CHAPTER = "id mangaId name scanlator chapterNumber sourceOrder isDownloaded pageCount uploadDate url realUrl"
# the last code point UTF-8 writes in 1, 2 and 3 bytes
UTF8_1_BYTE_LAST, UTF8_2_BYTES_LAST, UTF8_3_BYTES_LAST = 0x7F, 0x7FF, 0xFFFF
FIRST_PRINTABLE = 0x20  # the first printable code point: the C0 control characters lie below it


# Suwayomi's replies are typed in replies/suwayomi.py; the GraphQL envelope and each query's data are here. Each reply
# is read by its reader at the end of this module, unchecked.


class ErrorEntry(TypedDict):
    """An error of a reply: its message, and the path of the field it came from when it arose while the query ran."""

    message: str
    path: NotRequired[list[str | int]]


class Reply[T](TypedDict):
    """A GraphQL reply: the query's data and, when it failed, its errors (data is read only when there are none)."""

    data: T
    errors: NotRequired[list[ErrorEntry]]


class _Count(TypedDict):
    """A connection's total count."""

    totalCount: int


class _LibrarySize(TypedDict):
    """The data of the library size query."""

    mangas: _Count


class _PageInfo(TypedDict):
    """Whether a connection has a page after this one."""

    hasNextPage: bool


class _MangaPage(TypedDict):
    """A page of manga."""

    nodes: list[Manga]
    pageInfo: _PageInfo


class _LibraryPage(TypedDict):
    """The data of the library page query."""

    mangas: _MangaPage


class _MangaNodes(TypedDict):
    """Manga of a connection."""

    nodes: list[Manga]


class _Mangas(TypedDict):
    """The data of the 1-manga query."""

    mangas: _MangaNodes


class _ChapterNodes(TypedDict):
    """Chapters of a connection."""

    nodes: list[Chapter]


class _Chapters(TypedDict):
    """The data of the chapters queries."""

    chapters: _ChapterNodes


class _Downloads(TypedDict):
    """The data of the download status query."""

    downloadStatus: DownloadStatus


class _SourceNodes(TypedDict):
    """Sources of a connection."""

    nodes: list[Source]


class _Sources(TypedDict):
    """The data of the sources query."""

    sources: _SourceNodes


class _FoundMangas(TypedDict):
    """The manga a source search found."""

    mangas: list[Manga]


class _SourceSearch(TypedDict):
    """The data of the fetchSourceManga mutation."""

    fetchSourceManga: _FoundMangas


class _Fetched(TypedDict):
    """The data of the fetchMangaAndChapters mutation."""

    fetchMangaAndChapters: MangaAndChapters


class _Pages(TypedDict):
    """The data of the fetchChapterPages mutation."""

    fetchChapterPages: ChapterPages


class GraphQLError(Exception):
    """Suwayomi answered a query with errors (HTTP 200 with `errors`)."""

    def __init__(self, errors: list[ErrorEntry]) -> None:
        """Store the reply's errors; the message joins their messages."""
        super().__init__("; ".join(str(e.get("message")) for e in errors))
        self.errors = errors


class SourceUnavailableError(Exception):
    """The download source a call went to cannot serve it now; Suwayomi itself answers."""


class Suwayomi:
    """A run's Suwayomi GraphQL client: its reads, writes and calls that reach a download source, over 1 Service."""

    def __init__(self, url: str, service: http.Service | None = None) -> None:
        """Store the server's URL and the Service its requests go through."""
        self.base = url.rstrip("/")
        self._service = service or http.Service("suwayomi")

    def graphql[T](
        self, query: str, read: Callable[[bytes], Reply[T]], variables: Mapping[str, object] | None = None
    ) -> T:
        """Return the `data` of 1 query or mutation, its reply read by `read`. GraphQLError when it carries errors."""
        reply = self._service.request("POST", f"{self.base}/api/graphql", json=_body(query, variables), waits=WAITS)
        return _data(reply, read)

    # --- reads

    def ping(self) -> None:
        """Send a query that reads Suwayomi's database: Unreachable when Suwayomi does not answer.

        A trivial query ({ __typename }) is answered while the database pool is starved; this one is not.
        """
        self.library_size()

    def library_size(self) -> int:
        """Return how many manga the library holds."""
        return self.graphql("{ mangas(condition: {inLibrary: true}) { totalCount } }", _library_size)["mangas"][
            "totalCount"
        ]

    def library_manga(self) -> Iterator[Manga]:
        """Yield every manga of the library, PAGE_SIZE per query, in id order.

        Each query starts after the last id read, so a manga added or removed while the caller works shifts nothing.
        `source` is None for a manga whose extension is not installed.
        """
        query = (
            "query ($first: Int!, $after: Int!) { mangas(condition: {inLibrary: true},"
            " filter: {id: {greaterThan: $after}}, order: [{by: ID}], first: $first)"
            f" {{ nodes {{ {MANGA} }} pageInfo {{ hasNextPage }} }} }}"
        )
        after = 0
        while True:
            page = self.graphql(query, _library_page, {"first": PAGE_SIZE, "after": after})["mangas"]
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"] or not page["nodes"]:
                return
            after = page["nodes"][-1]["id"]

    def manga(self, manga_id: int) -> Manga | None:
        """1 manga, in the library or not; None when Suwayomi holds no manga of that id."""
        query = f"query ($id: Int!) {{ mangas(condition: {{id: $id}}) {{ nodes {{ {MANGA} }} }} }}"
        nodes = self.graphql(query, _mangas, {"id": manga_id})["mangas"]["nodes"]
        return nodes[0] if nodes else None

    def chapters(self, manga_id: int) -> list[Chapter]:
        """Every chapter Suwayomi lists for 1 manga, in the source's order."""
        query = (
            "query ($id: Int!) { chapters(condition: {mangaId: $id}, order: [{by: SOURCE_ORDER}])"
            f" {{ nodes {{ {CHAPTER} }} }} }}"
        )
        return self.graphql(query, _chapters, {"id": manga_id})["chapters"]["nodes"]

    def chapter(self, chapter_id: int) -> Chapter | None:
        """1 chapter by its id (of any manga, in the library or not); None when Suwayomi holds no chapter of that id."""
        query = f"query ($id: Int!) {{ chapters(condition: {{id: $id}}) {{ nodes {{ {CHAPTER} }} }} }}"
        nodes = self.graphql(query, _chapters, {"id": chapter_id})["chapters"]["nodes"]
        return nodes[0] if nodes else None

    def thumbnail(self, manga_id: int) -> bytes | None:
        """Return the manga's thumbnail image, or None when Suwayomi cannot serve it.

        It cannot on a 500 (the source's image is gone) or a timeout (Suwayomi fetches it from the source's site, which
        hangs). Sent once: a retry would wait for the same answer, and a timeout here never marks Suwayomi unreachable.
        """
        try:
            reply = self._service.request(
                "GET",
                f"{self.base}/api/v1/manga/{int(manga_id)}/thumbnail",
                headers={"Accept": "image/*"},
                timeout=SOURCE_TIMEOUT,
                retry_timeout=False,
                waits=(),
            )
        except http.HttpTimeoutError:
            return None
        except http.HttpError as e:
            if e.status in (HTTPStatus.NOT_FOUND, HTTPStatus.INTERNAL_SERVER_ERROR):
                return None
            raise
        return reply.body

    def download_queue(self) -> DownloadStatus:
        """Return {state, queue}: the downloader's state and its entries.

        The state is STARTED or STOPPED; each entry has its state (QUEUED, DOWNLOADING, FINISHED, ERROR), tries,
        progress and chapter {id mangaId}. A finished download has left the queue.
        """
        query = "{ downloadStatus { state queue { state tries progress position chapter { id mangaId } } } }"
        return self.graphql(query, _downloads)["downloadStatus"]

    def sources(self) -> list[Source]:
        """Return every source Suwayomi has, each with its id, name, lang and displayName."""
        return self.graphql("{ sources { nodes { id name lang displayName } } }", _sources)["sources"]["nodes"]

    # --- writes

    def enqueue(self, chapter_ids: Iterable[int]) -> None:
        """Queue chapters for download. Suwayomi skips one already queued, and one whose file is in place."""
        query = "mutation ($ids: [Int!]!) { enqueueChapterDownloads(input: {ids: $ids}) { downloadStatus { state } } }"
        self.graphql(query, _unread, {"ids": list(chapter_ids)})

    def delete_downloaded(self, chapter_ids: Iterable[int]) -> None:
        """Delete the downloaded files of chapters and clear their downloaded flag."""
        query = "mutation ($ids: [Int!]!) { deleteDownloadedChapters(input: {ids: $ids}) { chapters { id } } }"
        self.graphql(query, _unread, {"ids": list(chapter_ids)})

    def dequeue(self, chapter_ids: Iterable[int]) -> None:
        """Remove chapters from the download queue (a chapter not queued is no error)."""
        query = "mutation ($ids: [Int!]!) { dequeueChapterDownloads(input: {ids: $ids}) { downloadStatus { state } } }"
        self.graphql(query, _unread, {"ids": list(chapter_ids)})

    def start_downloader(self) -> None:
        """Start Suwayomi's downloader."""
        self.graphql("mutation { startDownloader(input: {}) { downloadStatus { state } } }", _unread)

    def set_in_library(self, manga_id: int, *, in_library: bool) -> None:
        """Put a manga in Suwayomi's library, or take it out (its chapters and files stay)."""
        query = (
            "mutation ($id: Int!, $in: Boolean!)"
            " { updateManga(input: {id: $id, patch: {inLibrary: $in}}) { manga { id } } }"
        )
        self.graphql(query, _unread, {"id": manga_id, "in": in_library})

    # --- calls that make Suwayomi reach a download source

    def search_source(self, source_id: str, title: str) -> list[Manga]:
        """Return the first page of a source's search for a title.

        Suwayomi stores each hit as a manga outside the library. SourceUnavailableError when the source cannot serve it.
        """
        query = (
            "mutation ($source: LongString!, $query: String!) { fetchSourceManga(input:"
            f" {{source: $source, type: SEARCH, page: 1, query: $query}}) {{ mangas {{ {MANGA} }} }} }}"
        )
        return self._source_call(query, _source_search, {"source": source_id, "query": title})["fetchSourceManga"][
            "mangas"
        ]

    def fetch_manga_and_chapters(self, manga_id: int) -> MangaAndChapters:
        """Refresh 1 manga's chapter list from its source and return {manga, chapters}.

        Any manga, in the library or not; none of the updater's filters apply. SourceUnavailableError when the source
        cannot serve it.
        """
        query = (
            "mutation ($id: Int!) { fetchMangaAndChapters(input: {id: $id, fetchManga: false, fetchChapters: true})"
            f" {{ manga {{ {MANGA} }} chapters {{ {CHAPTER} }} }} }}"
        )
        return self._source_call(query, _fetched, {"id": manga_id})["fetchMangaAndChapters"]

    def chapter_pages(self, chapter_id: int) -> list[str]:
        """Return the page URLs Suwayomi's reader serves for a chapter, relative to the base URL, in reading order.

        Any chapter, of a manga in the library or not; nothing is downloaded. SourceUnavailableError when the source
        cannot serve it.
        """
        query = "mutation ($id: Int!) { fetchChapterPages(input: {chapterId: $id}) { pages } }"
        return self._source_call(query, _chapter_pages, {"id": chapter_id})["fetchChapterPages"]["pages"]

    def page(self, url: str) -> bytes:
        """Return a page's bytes: a page URL chapter_pages gave, fetched from the source by Suwayomi.

        SourceUnavailableError when the source cannot serve it (HTTP 500 after the retries, or a timeout while Suwayomi
        answers a ping). ValueError for a URL that is not a path.
        """
        if not url.startswith("/"):
            msg = f"suwayomi: a page URL is a path below the base URL, got {url!r}"
            raise ValueError(msg)
        try:
            return self._from_source("GET", f"{self.base}{url}", headers={"Accept": "image/*"}).body
        except http.HttpError as e:
            if e.status == HTTPStatus.INTERNAL_SERVER_ERROR:
                raise SourceUnavailableError(str(e)) from None
            raise

    def _source_call[T](self, query: str, read: Callable[[bytes], Reply[T]], variables: Mapping[str, object]) -> T:
        """Send a call Suwayomi answers from a source's site; return its `data`.

        An error raised while the query ran (it carries a `path`) is the source's; one without a path (the query is
        wrong) is a GraphQLError.
        """
        reply = self._from_source("POST", f"{self.base}/api/graphql", json=_body(query, variables))
        try:
            return _data(reply, read)
        except GraphQLError as e:
            if all("path" in error for error in e.errors):
                raise SourceUnavailableError(str(e)) from None
            raise

    def _from_source(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> http.Response:
        """Send a request Suwayomi answers from a source's site.

        A timeout is not retried: when Suwayomi still answers a ping, the source is what hangs (SourceUnavailableError);
        a ping that fails in any way is Suwayomi unreachable (its database pool starved, say), not the source.
        """
        try:
            return self._service.request(
                method, url, headers=headers, json=json, timeout=SOURCE_TIMEOUT, retry_timeout=False
            )
        except http.HttpTimeoutError as e:
            try:
                self.ping()
            except (GraphQLError, http.HttpError) as ping_error:
                msg = f"timed out, then a ping failed: {ping_error}"
                raise self._service.mark_unreachable(msg) from None
            raise SourceUnavailableError(str(e)) from None


def from_config(cfg: config.Config) -> Suwayomi:
    """Build the Suwayomi client from the suwayomi.url setting."""
    return Suwayomi(cfg["suwayomi"]["url"])


def _body(query: str, variables: Mapping[str, object] | None) -> dict[str, object]:
    return {"query": query, "variables": variables or {}}


def _data[T](reply: http.Response, read: Callable[[bytes], Reply[T]]) -> T:
    payload = reply.parse(read)
    if errors := payload.get("errors"):
        raise GraphQLError(errors)
    return payload["data"]


# --- readers of Suwayomi's replies, 1 per query's data shape: each takes the reply's JSON as the shape its query asks
# for, unchecked (an approved unchecked boundary: CLAUDE.md, "Code standards"); the page list's is checked


def _unread(body: bytes) -> Reply[object]:
    """Read the reply of a query whose data the program does not read (the mutations); unchecked."""
    return json.loads(body)


def _library_size(body: bytes) -> Reply[_LibrarySize]:
    """Read the library size query's reply; unchecked."""
    return json.loads(body)


def _library_page(body: bytes) -> Reply[_LibraryPage]:
    """Read a library page query's reply; unchecked."""
    return json.loads(body)


def _mangas(body: bytes) -> Reply[_Mangas]:
    """Read the 1-manga query's reply; unchecked."""
    return json.loads(body)


def _chapters(body: bytes) -> Reply[_Chapters]:
    """Read a chapters query's reply (a manga's chapters, or 1 chapter by id); unchecked."""
    return json.loads(body)


def _downloads(body: bytes) -> Reply[_Downloads]:
    """Read the download status query's reply; unchecked."""
    return json.loads(body)


def _sources(body: bytes) -> Reply[_Sources]:
    """Read the sources query's reply; unchecked."""
    return json.loads(body)


def _source_search(body: bytes) -> Reply[_SourceSearch]:
    """Read the fetchSourceManga mutation's reply; unchecked."""
    return json.loads(body)


def _fetched(body: bytes) -> Reply[_Fetched]:
    """Read the fetchMangaAndChapters mutation's reply; unchecked."""
    return json.loads(body)


def _chapter_pages(body: bytes) -> Reply[_Pages]:
    """Read the fetchChapterPages mutation's reply, checked (replies.suwayomi.check_chapter_pages)."""
    reply = json.loads(body)
    replies.check_chapter_pages(reply)
    return reply


# --- where Suwayomi writes a download: <source folder>/<manga folder>/<chapter file>


def source_folder(display_name: str) -> str:
    """Return the folder of a source, from its displayName ("Name (LANG)")."""
    return valid_filename(display_name)


def manga_folder(title: str) -> str:
    """Return the folder of a manga, from its title."""
    return valid_filename(title)


def download_folders(source: Source, title: str) -> tuple[str, str]:
    """(source folder, manga folder) a manga's downloads lie in under MANGAS_FOLDER: of its installed source and title.

    A manga with no source installed (Manga.source None) has no folder to rebuild.
    """
    return source_folder(source["displayName"]), manga_folder(title)


def chapter_file(scanlator: str | None, name: str) -> str:
    """Return the CBZ of a chapter: "<scanlator>_<name>" with a scanlator (an empty one counts), else its name."""
    return valid_filename(name if scanlator is None else f"{scanlator}_{name}") + ".cbz"


def valid_filename(name: str) -> str:
    """Return a name as Suwayomi's SafePath.buildValidFilename (v2.4.2366) makes it, exactly.

    Trim dots and spaces at both ends (nothing left is "(invalid)"); control characters and REPLACED become "_"; then
    whole characters are kept while the next fits in MAX_NAME_BYTES UTF-8 bytes. Nothing is trimmed after the cut.
    (Suwayomi also stops at 240 UTF-16 units; a character never takes fewer bytes than units, so the byte cap always
    cuts first.)
    """
    name = name.strip(". ")
    if not name:
        return "(invalid)"
    kept: list[str] = []
    size = 0
    for char in name:
        code = ord(char)
        char_bytes = (
            1 if code <= UTF8_1_BYTE_LAST else 2 if code <= UTF8_2_BYTES_LAST else 3 if code <= UTF8_3_BYTES_LAST else 4
        )
        if size + char_bytes > MAX_NAME_BYTES:
            break
        kept.append("_" if code < FIRST_PRINTABLE or char in REPLACED else char)
        size += char_bytes
    return "".join(kept)
