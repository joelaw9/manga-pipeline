"""Suwayomi GraphQL client and its download file names (SPEC "Services").

Field and input names are Suwayomi-Server's schema at v2.4.2366 (introspected). 1 object per run: it keeps whether
Suwayomi was found unreachable. Every call returns plain dicts shaped like the GraphQL reply.
"""

from manga_pipeline.clients import http

PAGE_SIZE = 50  # library manga per query: the whole library in 1 query times out
SOURCE_TIMEOUT = 120  # seconds; a source search or chapter fetch waits on the source's site
MAX_NAME_BYTES = 240  # UTF-8 bytes
MANGAS_FOLDER = "mangas"  # the folder of downloads under Suwayomi's download root
REPLACED = frozenset('"*/:<>?\\|\x7f')
MANGA = "id title status url inLibrary sourceId source { id name lang displayName }"
CHAPTER = "id mangaId name scanlator chapterNumber sourceOrder isDownloaded pageCount uploadDate url realUrl"


class GraphQLError(Exception):
    """Suwayomi answered a query with errors (HTTP 200 with `errors`)."""

    def __init__(self, errors):
        super().__init__("; ".join(str(e.get("message")) for e in errors))
        self.errors = errors


class SourceUnavailable(Exception):
    """The download source a call went to cannot serve it now; Suwayomi itself answers."""


class Suwayomi:
    def __init__(self, url, service=None):
        self.base = url.rstrip("/")
        self._service = service or http.Service("suwayomi")

    def graphql(self, query, variables=None):
        """The `data` of 1 query or mutation. GraphQLError when the reply carries errors."""
        reply = self._service.request("POST", f"{self.base}/api/graphql", json=_body(query, variables))
        return _data(reply)

    # --- reads

    def ping(self):
        """A trivial query: Unreachable when Suwayomi does not answer."""
        self.graphql("{ __typename }")

    def library_size(self):
        return self.graphql("{ mangas(condition: {inLibrary: true}) { totalCount } }")["mangas"]["totalCount"]

    def library_manga(self):
        """Every manga of the library, PAGE_SIZE per query, in id order; each query starts after the last id read,
        so a manga added or removed while the caller works shifts nothing. `source` is None for a manga whose
        extension is not installed."""
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

    def download_queue(self):
        """{state, queue}: the downloader's state (STARTED, STOPPED) and its entries, each with its state (QUEUED,
        DOWNLOADING, FINISHED, ERROR), tries, progress and chapter {id mangaId}. A finished download has left the
        queue."""
        query = "{ downloadStatus { state queue { state tries progress position chapter { id mangaId } } } }"
        return self.graphql(query)["downloadStatus"]

    def sources(self):
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

    def start_downloader(self):
        self.graphql("mutation { startDownloader(input: {}) { downloadStatus { state } } }")

    # --- calls that make Suwayomi reach a download source

    def search_source(self, source_id, title):
        """The first page of a source's search for a title. Suwayomi stores each hit as a manga outside the library.
        SourceUnavailable when the source cannot serve it."""
        query = (
            "mutation ($source: LongString!, $query: String!) { fetchSourceManga(input:"
            f" {{source: $source, type: SEARCH, page: 1, query: $query}}) {{ mangas {{ {MANGA} }} }} }}"
        )
        return self._source_call(query, {"source": str(source_id), "query": title})["fetchSourceManga"]["mangas"]

    def fetch_manga_and_chapters(self, manga_id):
        """Refresh 1 manga's chapter list from its source (any manga, in the library or not; none of the updater's
        filters apply) and return {manga, chapters}. SourceUnavailable when the source cannot serve it."""
        query = (
            "mutation ($id: Int!) { fetchMangaAndChapters(input: {id: $id, fetchManga: false, fetchChapters: true})"
            f" {{ manga {{ {MANGA} }} chapters {{ {CHAPTER} }} }} }}"
        )
        return self._source_call(query, {"id": manga_id})["fetchMangaAndChapters"]

    def _source_call(self, query, variables):
        """A call Suwayomi answers from a source's site. An error raised while the query ran (it carries a `path`)
        is the source's; one without a path (the query is wrong) is a GraphQLError. A timeout is not retried: when
        Suwayomi still answers a ping, the source is what hangs."""
        try:
            reply = self._service.request(
                "POST",
                f"{self.base}/api/graphql",
                json=_body(query, variables),
                timeout=SOURCE_TIMEOUT,
                retry_timeout=False,
            )
        except http.Timeout as e:
            self.ping()
            raise SourceUnavailable(str(e)) from None
        try:
            return _data(reply)
        except GraphQLError as e:
            if all("path" in error for error in e.errors):
                raise SourceUnavailable(str(e)) from None
            raise


def from_config(cfg):
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
    """The folder of a source, from its displayName ("Name (LANG)")."""
    return valid_filename(display_name)


def manga_folder(title):
    return valid_filename(title)


def chapter_file(scanlator, name):
    """The CBZ of a chapter: "<scanlator>_<name>" when the chapter has a scanlator (an empty one counts), else its
    name."""
    return valid_filename(name if scanlator is None else f"{scanlator}_{name}") + ".cbz"


def valid_filename(name):
    """Suwayomi's SafePath.buildValidFilename (v2.4.2366), exactly: trim dots and spaces at both ends (nothing left
    is "(invalid)"); control characters and REPLACED become "_"; then whole characters are kept while the next fits
    in MAX_NAME_BYTES UTF-8 bytes. Nothing is trimmed after the cut. (Suwayomi also stops at 240 UTF-16 units; a
    character never takes fewer bytes than units, so the byte cap always cuts first.)"""
    name = name.strip(". ")
    if not name:
        return "(invalid)"
    kept, size = [], 0
    for char in name:
        code = ord(char)
        char_bytes = 1 if code <= 0x7F else 2 if code <= 0x7FF else 3 if code <= 0xFFFF else 4
        if size + char_bytes > MAX_NAME_BYTES:
            break
        kept.append("_" if code < 0x20 or char in REPLACED else char)
        size += char_bytes
    return "".join(kept)
