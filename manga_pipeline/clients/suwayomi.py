"""Suwayomi GraphQL client and its download file names (SPEC "Services").

Field and input names are Suwayomi-Server's schema at v2.4.2366 (introspected). 1 object per run: it keeps whether
Suwayomi was found unreachable. Every call returns plain dicts shaped like the GraphQL reply.
"""

from http import HTTPStatus

from manga_pipeline.clients import http

PAGE_SIZE = 50  # library manga per query: the whole library in 1 query times out
SOURCE_TIMEOUT = 120  # seconds; a source search or chapter fetch waits on the source's site
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


class GraphQLError(Exception):
    """Suwayomi answered a query with errors (HTTP 200 with `errors`)."""

    def __init__(self, errors):
        """Store the reply's errors; the message joins their messages."""
        super().__init__("; ".join(str(e.get("message")) for e in errors))
        self.errors = errors


class SourceUnavailableError(Exception):
    """The download source a call went to cannot serve it now; Suwayomi itself answers."""


class Suwayomi:
    """A run's Suwayomi GraphQL client: its reads, writes and calls that reach a download source, over 1 Service."""

    def __init__(self, url, service=None):
        """Store the server's URL and the Service its requests go through."""
        self.base = url.rstrip("/")
        self._service = service or http.Service("suwayomi")

    def graphql(self, query, variables=None):
        """Return the `data` of 1 query or mutation. GraphQLError when the reply carries errors."""
        reply = self._service.request("POST", f"{self.base}/api/graphql", json=_body(query, variables))
        return _data(reply)

    # --- reads

    def ping(self):
        """Send a trivial query: Unreachable when Suwayomi does not answer."""
        self.graphql("{ __typename }")

    def library_size(self):
        """Return how many manga the library holds."""
        return self.graphql("{ mangas(condition: {inLibrary: true}) { totalCount } }")["mangas"]["totalCount"]

    def library_manga(self):
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
            page = self.graphql(query, {"first": PAGE_SIZE, "after": after})["mangas"]
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"] or not page["nodes"]:
                return
            after = page["nodes"][-1]["id"]

    def manga(self, manga_id):
        """1 manga, in the library or not; None when Suwayomi holds no manga of that id."""
        query = f"query ($id: Int!) {{ mangas(condition: {{id: $id}}) {{ nodes {{ {MANGA} }} }} }}"
        nodes = self.graphql(query, {"id": manga_id})["mangas"]["nodes"]
        return nodes[0] if nodes else None

    def chapters(self, manga_id):
        """Every chapter Suwayomi lists for 1 manga, in the source's order."""
        query = (
            "query ($id: Int!) { chapters(condition: {mangaId: $id}, order: [{by: SOURCE_ORDER}])"
            f" {{ nodes {{ {CHAPTER} }} }} }}"
        )
        return self.graphql(query, {"id": manga_id})["chapters"]["nodes"]

    def chapter(self, chapter_id):
        """1 chapter by its id (of any manga, in the library or not); None when Suwayomi holds no chapter of that id."""
        query = f"query ($id: Int!) {{ chapters(condition: {{id: $id}}) {{ nodes {{ {CHAPTER} }} }} }}"
        nodes = self.graphql(query, {"id": chapter_id})["chapters"]["nodes"]
        return nodes[0] if nodes else None

    def thumbnail(self, manga_id):
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
                retries=0,
            )
        except http.HttpTimeoutError:
            return None
        except http.HttpError as e:
            if e.status in (HTTPStatus.NOT_FOUND, HTTPStatus.INTERNAL_SERVER_ERROR):
                return None
            raise
        return reply.body

    def download_queue(self):
        """Return {state, queue}: the downloader's state and its entries.

        The state is STARTED or STOPPED; each entry has its state (QUEUED, DOWNLOADING, FINISHED, ERROR), tries,
        progress and chapter {id mangaId}. A finished download has left the queue.
        """
        query = "{ downloadStatus { state queue { state tries progress position chapter { id mangaId } } } }"
        return self.graphql(query)["downloadStatus"]

    def sources(self):
        """Return every source Suwayomi has, each with its id, name, lang and displayName."""
        return self.graphql("{ sources { nodes { id name lang displayName } } }")["sources"]["nodes"]

    # --- writes

    def enqueue(self, chapter_ids):
        """Queue chapters for download. Suwayomi skips one already queued, and one whose file is in place."""
        query = "mutation ($ids: [Int!]!) { enqueueChapterDownloads(input: {ids: $ids}) { downloadStatus { state } } }"
        self.graphql(query, {"ids": list(chapter_ids)})

    def delete_downloaded(self, chapter_ids):
        """Delete the downloaded files of chapters and clear their downloaded flag."""
        query = "mutation ($ids: [Int!]!) { deleteDownloadedChapters(input: {ids: $ids}) { chapters { id } } }"
        self.graphql(query, {"ids": list(chapter_ids)})

    def dequeue(self, chapter_ids):
        """Remove chapters from the download queue (a chapter not queued is no error)."""
        query = "mutation ($ids: [Int!]!) { dequeueChapterDownloads(input: {ids: $ids}) { downloadStatus { state } } }"
        self.graphql(query, {"ids": list(chapter_ids)})

    def start_downloader(self):
        """Start Suwayomi's downloader."""
        self.graphql("mutation { startDownloader(input: {}) { downloadStatus { state } } }")

    # --- calls that make Suwayomi reach a download source

    def search_source(self, source_id, title):
        """Return the first page of a source's search for a title.

        Suwayomi stores each hit as a manga outside the library. SourceUnavailableError when the source cannot serve it.
        """
        query = (
            "mutation ($source: LongString!, $query: String!) { fetchSourceManga(input:"
            f" {{source: $source, type: SEARCH, page: 1, query: $query}}) {{ mangas {{ {MANGA} }} }} }}"
        )
        return self._source_call(query, {"source": str(source_id), "query": title})["fetchSourceManga"]["mangas"]

    def fetch_manga_and_chapters(self, manga_id):
        """Refresh 1 manga's chapter list from its source and return {manga, chapters}.

        Any manga, in the library or not; none of the updater's filters apply. SourceUnavailableError when the source
        cannot serve it.
        """
        query = (
            "mutation ($id: Int!) { fetchMangaAndChapters(input: {id: $id, fetchManga: false, fetchChapters: true})"
            f" {{ manga {{ {MANGA} }} chapters {{ {CHAPTER} }} }} }}"
        )
        return self._source_call(query, {"id": manga_id})["fetchMangaAndChapters"]

    def _source_call(self, query, variables):
        """Send a call Suwayomi answers from a source's site; return its `data`.

        An error raised while the query ran (it carries a `path`) is the source's; one without a path (the query is
        wrong) is a GraphQLError. A timeout is not retried: when Suwayomi still answers a ping, the source is what
        hangs.
        """
        try:
            reply = self._service.request(
                "POST",
                f"{self.base}/api/graphql",
                json=_body(query, variables),
                timeout=SOURCE_TIMEOUT,
                retry_timeout=False,
            )
        except http.HttpTimeoutError as e:
            self.ping()
            raise SourceUnavailableError(str(e)) from None
        try:
            return _data(reply)
        except GraphQLError as e:
            if all("path" in error for error in e.errors):
                raise SourceUnavailableError(str(e)) from None
            raise


def from_config(cfg):
    """Build the Suwayomi client from the suwayomi.url setting."""
    return Suwayomi(cfg["suwayomi"]["url"])


def _body(query, variables):
    return {"query": query, "variables": variables or {}}


def _data(reply):
    payload = reply.json()
    if payload.get("errors"):
        raise GraphQLError(payload["errors"])
    return payload["data"]


# --- where Suwayomi writes a download: <source folder>/<manga folder>/<chapter file>


def source_folder(display_name):
    """Return the folder of a source, from its displayName ("Name (LANG)")."""
    return valid_filename(display_name)


def manga_folder(title):
    """Return the folder of a manga, from its title."""
    return valid_filename(title)


def download_folders(manga):
    """(source folder, manga folder) a manga's downloads lie in under MANGAS_FOLDER; None with no source installed."""
    if manga["source"] is None:
        return None
    return source_folder(manga["source"]["displayName"]), manga_folder(manga["title"])


def chapter_file(scanlator, name):
    """Return the CBZ of a chapter: "<scanlator>_<name>" with a scanlator (an empty one counts), else its name."""
    return valid_filename(name if scanlator is None else f"{scanlator}_{name}") + ".cbz"


def valid_filename(name):
    """Return a name as Suwayomi's SafePath.buildValidFilename (v2.4.2366) makes it, exactly.

    Trim dots and spaces at both ends (nothing left is "(invalid)"); control characters and REPLACED become "_"; then
    whole characters are kept while the next fits in MAX_NAME_BYTES UTF-8 bytes. Nothing is trimmed after the cut.
    (Suwayomi also stops at 240 UTF-16 units; a character never takes fewer bytes than units, so the byte cap always
    cuts first.)
    """
    name = name.strip(". ")
    if not name:
        return "(invalid)"
    kept, size = [], 0
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
