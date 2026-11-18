"""The metadata sources' clients: MangaBaka, MangaUpdates, MangaDex, AniList, and the Tapas and Webtoons episode lists.

SPEC "Source ids", "Metadata checks"; Tapas and Webtoons are the official platforms' episode lists (SPEC "Metadata",
platform lists).

Each client is thin: a record by id (None when the source no longer has it), and a title search returning the raw
results; which result is accepted is `match`'s job. Every record is the source's own JSON, stored as fetched. 1 client
per source per run, each on its own `http.Service` with the source's minimum gap between requests.
"""

import json
import urllib.parse
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from http import HTTPStatus
from typing import NotRequired, TypedDict, override

from manga_pipeline.clients import http
from manga_pipeline.replies.sources import (
    AniListHit,
    AniListMedia,
    Episode,
    MangaBakaSeries,
    MangaDexAggregate,
    MangaDexCover,
    MangaDexManga,
    MangaUpdatesResult,
    MangaUpdatesSeries,
)

MANGABAKA = "https://api.mangabaka.org/v1"
MANGABAKA_SEARCH_GAP = 2.1  # seconds before a MangaBaka search: search has its own, slower limit
MANGAUPDATES = "https://api.mangaupdates.com/v1"
MANGADEX = "https://api.mangadex.org"
MANGADEX_UPLOADS = "https://uploads.mangadex.org"
MANGADEX_USER_AGENT = "manga-pipeline"  # MangaDex answers a browser User-Agent with a bot check
ANILIST = "https://graphql.anilist.co"
TAPAS = "https://tapas.io"
TAPAS_PAGE = 20  # the largest page of episodes Tapas serves
WEBTOONS_API = "https://m.webtoons.com/api/v1"
WEBTOONS_WEB = "https://www.webtoons.com"
WEBTOONS_PAGE = 100
MANGADEX_RATINGS = [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
SEARCH_RESULTS = 10
IMAGE_TIMEOUT = 120  # seconds
COVER_PAGE = 100  # the largest page of covers MangaDex serves
ANILIST_MEDIA = """query ($id: Int) { Media(id: $id, type: MANGA) {
  id idMal format status title { romaji english native } synonyms description genres
  tags { name rank isMediaSpoiler } coverImage { extraLarge } startDate { year month day }
  staff { edges { role node { name { full } } } } externalLinks { site type url language } } }"""
ANILIST_SEARCH = """query ($s: String) { Page(perPage: 8) { media(search: $s, type: MANGA) {
  id format title { romaji english native } synonyms } } }"""


# The sources' records are typed in replies/sources.py; their envelopes and the platforms' pages are here. Each reply is
# read by its reader at the end of this module, unchecked.


class _MangaBakaSourceSeries(TypedDict):
    """The data of GET /source/{site}/{id}: the series MangaBaka holds for that id (main story and spin-offs)."""

    series: NotRequired[list[MangaBakaSeries] | None]


class _MangaBakaReply[T](TypedDict):
    """MangaBaka's reply envelope: the data."""

    data: T


class _MangaUpdatesSearch(TypedDict):
    """The reply of MangaUpdates' series search."""

    results: NotRequired[list[MangaUpdatesResult] | None]


class _MangaDexCoverEntry(TypedDict):
    """An entry of GET /cover."""

    attributes: MangaDexCover


class _MangaDexCovers(TypedDict):
    """A page of GET /cover: its entries and the total count."""

    data: list[_MangaDexCoverEntry]
    total: int


class _MangaDexPublished(TypedDict):
    """A feed chapter's attributes: its publish time (ISO text)."""

    publishAt: str


class _MangaDexFeedChapter(TypedDict):
    """A chapter of a manga's feed."""

    attributes: _MangaDexPublished


class _MangaDexReply[T](TypedDict):
    """MangaDex's reply envelope: the data."""

    data: T


class _AniListMediaData(TypedDict):
    """The data of ANILIST_MEDIA."""

    Media: AniListMedia


class _AniListPage(TypedDict):
    """A page of AniList's search."""

    media: list[AniListHit] | None


class _AniListSearchData(TypedDict):
    """The data of ANILIST_SEARCH."""

    Page: _AniListPage | None


class _AniListError(TypedDict):
    """An error of an AniList reply."""

    message: str


class _AniListReply[T](TypedDict):
    """An AniList GraphQL reply: the data and, when the query failed, its errors (data is read only when none)."""

    data: T
    errors: NotRequired[list[_AniListError]]


class _TapasEpisode(TypedDict):
    """An episode of a Tapas list page: its id, site number (scene), title and publish time (ISO text)."""

    id: int
    scene: int
    title: str
    publish_date: NotRequired[str | None]


class _TapasPagination(TypedDict):
    """A Tapas list page's pagination: whether a page follows, the next page's number and the episode count."""

    has_next: bool
    page: NotRequired[int | None]
    total: NotRequired[int | None]


class _TapasPage(TypedDict):
    """The data of a Tapas episode list page."""

    episodes: list[_TapasEpisode]
    pagination: _TapasPagination


class _TapasReply(TypedDict):
    """Tapas' reply envelope: the data."""

    data: _TapasPage


class _WebtoonsEpisode(TypedDict):
    """An episode of a Webtoons list page: its site number, title and when it went up (epoch milliseconds)."""

    episodeNo: int
    episodeTitle: NotRequired[str | None]
    exposureDateMillis: NotRequired[int | None]


class _WebtoonsResult(TypedDict):
    """A Webtoons list page: its episodes and the cursor of the next page (0 or null after the last)."""

    episodeList: NotRequired[list[_WebtoonsEpisode] | None]
    nextCursor: NotRequired[int | None]


class _WebtoonsReply(TypedDict):
    """Webtoons' reply: success false for no such series, when it carries no result."""

    success: NotRequired[bool]
    result: NotRequired[_WebtoonsResult | None]


class SourceError(Exception):
    """A source answered with an error that is not "not found" (AniList's GraphQL `errors`)."""


def _gone[T](call: Callable[[], T], statuses: Collection[int] = (404,)) -> T | None:
    """Return the call's result, or None when the site answers that the page is gone."""
    try:
        return call()
    except http.HttpError as e:
        if e.status in statuses:
            return None
        raise


def _unless_gone[T](call: Callable[[], T]) -> T | None:
    """Return the call's result, or None when the source answers 404 (the entry is gone or merged)."""
    try:
        return call()
    except http.HttpError as e:
        if e.status == HTTPStatus.NOT_FOUND:
            return None
        raise


class MangaBaka:
    """MangaBaka's API client: a series by id, the series of an AniList or MangaUpdates id, and a title search."""

    def __init__(self, service: http.Service | None = None) -> None:
        """Take the client's service; by default a new one with a 0.5 s gap (a search keeps MANGABAKA_SEARCH_GAP)."""
        self._service = service or http.Service("mangabaka", 0.5)

    def series(self, mb_id: str) -> MangaBakaSeries | None:
        """Return MangaBaka's series record of mb_id, or None when MangaBaka no longer has it."""
        return _unless_gone(lambda: self._get(f"/series/{mb_id}", _mangabaka_series))

    def by_source(self, source: str, source_id: str) -> MangaBakaSeries | None:
        """Return the 1 active series MangaBaka holds for an AniList ("anilist") or MangaUpdates ("manga-updates") id.

        None for none or several.
        """
        data = _unless_gone(lambda: self._get(f"/source/{source}/{source_id}", _mangabaka_source)) or {}
        active = [s for s in data.get("series") or [] if s.get("state") == "active"]
        return active[0] if len(active) == 1 else None

    def search(self, name: str) -> list[MangaBakaSeries]:
        """Return the active series among the results of MangaBaka's title search for name."""
        query = urllib.parse.urlencode({"q": name})
        results = self._get(f"/series/search?{query}", _mangabaka_search, MANGABAKA_SEARCH_GAP) or []
        return [r for r in results if r.get("state", "active") == "active"]

    def _get[T](self, path: str, read: Callable[[bytes], _MangaBakaReply[T]], min_gap: float | None = None) -> T:
        return self._service.request("GET", MANGABAKA + path, min_gap=min_gap).parse(read)["data"]


class MangaUpdates:
    """MangaUpdates' API client: a series by id and a title search."""

    def __init__(self, service: http.Service | None = None) -> None:
        """Take the client's service; by default a new one with a 0.5 s gap between requests."""
        self._service = service or http.Service("mangaupdates", 0.5)

    def series(self, mu_id: int | str) -> MangaUpdatesSeries | None:
        """Return MangaUpdates' series record of mu_id, or None when MangaUpdates no longer has it."""
        return _unless_gone(
            lambda: self._service.request("GET", f"{MANGAUPDATES}/series/{mu_id}").parse(_mangaupdates_series)
        )

    def search(self, name: str) -> list[MangaUpdatesResult]:
        """Return the search's results: each {"record": {...}, "hit_title": ...}."""
        body: dict[str, object] = {"search": name, "perpage": SEARCH_RESULTS}
        reply = self._service.request("POST", f"{MANGAUPDATES}/series/search", json=body).parse(_mangaupdates_search)
        return reply.get("results") or []


class MangaDex:
    """MangaDex's API client: a manga, its aggregate, covers and newest chapter's date, a title search, cover images."""

    def __init__(self, service: http.Service | None = None) -> None:
        """Take the client's service; by default a new one with a 0.25 s gap and the User-Agent MangaDex accepts."""
        self._service = service or http.Service("mangadex", 0.25, MANGADEX_USER_AGENT)

    def manga(self, md_id: str) -> MangaDexManga | None:
        """Return the manga's record with its cover art included, or None when MangaDex no longer has it."""
        query = urllib.parse.urlencode([("includes[]", "cover_art")])
        return _unless_gone(lambda: self._get(f"/manga/{md_id}?{query}", _mangadex_manga)["data"])

    def aggregate(self, md_id: str) -> MangaDexAggregate | None:
        """Return the manga's aggregate (its volumes and their chapters), or None when MangaDex no longer has it."""
        return _unless_gone(lambda: self._get(f"/manga/{md_id}/aggregate", _mangadex_aggregate))

    def covers(self, md_id: str) -> list[MangaDexCover]:
        """Every cover's attributes (volume, locale, fileName), in volume order, page by page."""
        out: list[MangaDexCover] = []
        offset = 0
        while True:
            query = urllib.parse.urlencode(
                [("manga[]", md_id), ("order[volume]", "asc"), ("limit", COVER_PAGE), ("offset", offset)]
            )
            page = self._get(f"/cover?{query}", _mangadex_covers)
            out += [c["attributes"] for c in page["data"]]
            offset += len(page["data"])
            if not page["data"] or offset >= page["total"]:
                return out

    def latest_chapter_date(self, md_id: str) -> str | None:
        """Return the publish time (ISO text) of the manga's newest chapter in any language, or None."""
        query = urllib.parse.urlencode(
            [("limit", 1), ("order[publishAt]", "desc"), *MANGADEX_RATINGS, ("includeFuturePublishAt", 0)]
        )
        chapters = self._get(f"/manga/{md_id}/feed?{query}", _mangadex_feed)["data"]
        return chapters[0]["attributes"]["publishAt"] if chapters else None

    def search(self, name: str) -> list[MangaDexManga]:
        """Return the manga records of MangaDex's title search for name: every content rating, cover art included."""
        query = urllib.parse.urlencode(
            [("title", name), ("limit", SEARCH_RESULTS), ("includes[]", "cover_art"), *MANGADEX_RATINGS]
        )
        return self._get(f"/manga?{query}", _mangadex_search)["data"]

    def cover_image(self, md_id: str, file_name: str) -> bytes:
        """Return the bytes of the manga's cover image file_name, from MangaDex's uploads host."""
        url = f"{MANGADEX_UPLOADS}/covers/{md_id}/{urllib.parse.quote(file_name)}"
        return self._service.request("GET", url, headers={"Accept": "image/*"}, timeout=IMAGE_TIMEOUT).body

    def _get[T](self, path: str, read: Callable[[bytes], T]) -> T:
        return self._service.request("GET", MANGADEX + path).parse(read)


class AniList:
    """AniList's GraphQL client: a manga's Media by id, a title search and cover images."""

    def __init__(self, service: http.Service | None = None) -> None:
        """Take the client's service; by default a new one with a 2.1 s gap between requests."""
        self._service = service or http.Service("anilist", 2.1)

    def media(self, al_id: int | str) -> AniListMedia | None:
        """Return the manga's Media record, or None when AniList no longer has it."""
        data = _unless_gone(lambda: self._query(ANILIST_MEDIA, {"id": int(al_id)}, _anilist_media))
        return None if data is None else data["Media"]

    def search(self, name: str) -> list[AniListHit]:
        """Return the manga media of AniList's title search for name (its first page)."""
        return (self._query(ANILIST_SEARCH, {"s": name}, _anilist_search)["Page"] or {}).get("media") or []

    def cover_image(self, url: str) -> bytes:
        """Return the media's `coverImage.extraLarge` image (served by AniList's image host)."""
        return self._service.request("GET", url, headers={"Accept": "image/*"}, timeout=IMAGE_TIMEOUT).body

    def _query[T](self, query: str, variables: dict[str, object], read: Callable[[bytes], _AniListReply[T]]) -> T:
        reply = self._service.request("POST", ANILIST, json={"query": query, "variables": variables}).parse(read)
        if errors := reply.get("errors"):
            raise SourceError("AniList: " + "; ".join(str(e.get("message")) for e in errors))
        return reply["data"]


class Tapas:
    """Tapas' own site endpoints (local library tapas.md). A 403 is Tapas blocking: unreachable for the run."""

    GONE = (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND)

    def __init__(self, service: http.Service | None = None) -> None:
        """Take the client's service; by default a new one with a 0.5 s gap between requests."""
        self._service = service or http.Service("tapas", 0.5)

    def series_id(self, slug: str) -> str | None:
        """Return the numeric series id from the series' /info page, or None when the page is gone or carries none.

        The id is the page's `tapastic://series/<id>` meta; the page carries it only when asked for HTML.
        """
        url = f"{TAPAS}/series/{urllib.parse.quote(slug)}/info"
        reply = _gone(lambda: self._get(url, "text/html"), self.GONE)
        if reply is None:
            return None
        finder = _MetaFinder("tapastic://series/")
        finder.feed(reply.body.decode("utf-8", "replace"))
        return finder.found.split("/")[0] if finder.found and finder.found.split("/")[0].isdigit() else None

    def episodes(self, series_id: str) -> tuple[list[Episode], int] | None:
        """Return the series' episodes and the count Tapas says it has, or None when the list is gone.

        The result is ([(site number, title, date, id)] in site order, the count). Episodes are kept once each, by id.
        """
        items: list[Episode] = []
        seen: set[int] = set()
        total, page = 0, 1
        while True:
            url = f"{TAPAS}/series/{series_id}/episodes?page={page}&sort=OLDEST&max_limit={TAPAS_PAGE}"
            # a gone first page is no list; a page gone later is an error: the stored list is kept
            reply = _gone(lambda url=url: self._get(url), self.GONE if page == 1 else ())
            if reply is None:
                return None
            data = reply.parse(_tapas_page)["data"]
            for e in data["episodes"]:
                if e["id"] not in seen:
                    seen.add(e["id"])
                    items.append((e["scene"], e["title"], (e.get("publish_date") or "")[:10] or None, e["id"]))
            total = max(total, data["pagination"].get("total") or 0)
            following = data["pagination"].get("page") or 0  # Tapas gives the next page's number
            if not data["pagination"]["has_next"] or not data["episodes"] or following <= page:
                return items, total
            page = following

    def _get(self, url: str, accept: str = "application/json") -> http.Response:
        try:
            return self._service.request("GET", url, headers={"Accept": accept})
        except http.HttpError as e:
            if e.status == HTTPStatus.FORBIDDEN:
                msg = f"HTTP 403 from {http.redact_url(url)}: blocked"
                raise self._service.mark_unreachable(msg) from None
            raise


class Webtoons:
    """Webtoons' own endpoints (local library webtoons.md); a ref is "<webtoon|canvas>:<title_no>"."""

    GONE = (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND)

    def __init__(self, service: http.Service | None = None) -> None:
        """Take the client's service; by default a new one with a 0.5 s gap between requests."""
        self._service = service or http.Service("webtoons", 0.5)

    def episodes(self, ref: str) -> list[Episode] | None:
        """Return [(site number, title, date, None)] in site order, or None when Webtoons has no such series.

        No such series is `success` false, or a page that is gone.
        """
        kind, number = ref.split(":")
        items: list[Episode] = []
        cursor: int | None = None
        while True:
            url = f"{WEBTOONS_API}/{kind}/{number}/episodes?pageSize={WEBTOONS_PAGE}"
            url += f"&cursor={cursor}" if cursor else ""
            # a gone first page is no list; a page gone later is an error: the stored list is kept
            reply = _gone(lambda url=url: self._service.request("GET", url), self.GONE if cursor is None else ())
            if reply is None:
                return None
            data = reply.parse(_webtoons_page)
            if not data.get("success"):
                if cursor is None:
                    return None
                msg = f"Webtoons: {kind} {number} failed after cursor {cursor}"
                raise SourceError(msg)
            result = data.get("result") or {}
            following = result.get("nextCursor")
            if cursor is not None and following and following <= cursor:
                return items  # the page asked for again: it does not move on, and its episodes are in already
            for e in result.get("episodeList") or []:
                millis = e.get("exposureDateMillis")
                date = datetime.fromtimestamp(millis / 1000, UTC).date().isoformat() if millis else None
                items.append((e["episodeNo"], e.get("episodeTitle"), date, None))
            if not following or not result.get("episodeList"):
                return items
            cursor = following

    def app_only(self, ref: str) -> bool | None:
        """Return whether the web list page says episodes are only on the app; None when the page cannot be read.

        The page says so with its `detail_install_app` block.
        """
        kind, number = ref.split(":")
        path = "/challenge/episodeList" if kind == "canvas" else "/episodeList"
        try:
            reply = self._service.request(
                "GET", f"{WEBTOONS_WEB}{path}?titleNo={number}", headers={"Accept": "text/html"}
            )
        except http.HttpError:
            return None
        finder = _AttributeFinder("detail_install_app")
        finder.feed(reply.body.decode("utf-8", "replace"))
        return finder.found


@dataclass
class Sources:
    """The run's source clients: the 4 metadata sources and the 2 platforms."""

    mangabaka: MangaBaka
    mangaupdates: MangaUpdates
    mangadex: MangaDex
    anilist: AniList
    tapas: Tapas
    webtoons: Webtoons


def new_sources() -> Sources:
    """Build the run's source clients, each with its own new service."""
    return Sources(MangaBaka(), MangaUpdates(), MangaDex(), AniList(), Tapas(), Webtoons())


class _MetaFinder(HTMLParser):
    """The rest of the first <meta content> that starts with prefix."""

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self.prefix, self.found = prefix, None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        content = dict(attrs).get("content") or ""
        if tag == "meta" and self.found is None and content.startswith(self.prefix):
            self.found = content[len(self.prefix) :]


class _AttributeFinder(HTMLParser):
    """Whether any element carries the name as a class or as its id."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name, self.found = name, False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self.name in (values.get("class") or "").split() or values.get("id") == self.name:
            self.found = True


# --- readers of the sources' replies, 1 per reply shape: each takes the reply's JSON as the shape the source sends,
# unchecked (an approved unchecked boundary: CLAUDE.md, "Code standards")


def _mangabaka_series(body: bytes) -> _MangaBakaReply[MangaBakaSeries]:
    """Read MangaBaka's GET /series/{id} reply; unchecked."""
    return json.loads(body)


def _mangabaka_source(body: bytes) -> _MangaBakaReply[_MangaBakaSourceSeries]:
    """Read MangaBaka's GET /source/{site}/{id} reply; unchecked."""
    return json.loads(body)


def _mangabaka_search(body: bytes) -> _MangaBakaReply[list[MangaBakaSeries] | None]:
    """Read MangaBaka's GET /series/search reply; unchecked."""
    return json.loads(body)


def _mangaupdates_series(body: bytes) -> MangaUpdatesSeries:
    """Read MangaUpdates' GET /series/{id} reply; unchecked."""
    return json.loads(body)


def _mangaupdates_search(body: bytes) -> _MangaUpdatesSearch:
    """Read MangaUpdates' POST /series/search reply; unchecked."""
    return json.loads(body)


def _mangadex_manga(body: bytes) -> _MangaDexReply[MangaDexManga]:
    """Read MangaDex's GET /manga/{id} reply; unchecked."""
    return json.loads(body)


def _mangadex_aggregate(body: bytes) -> MangaDexAggregate:
    """Read MangaDex's GET /manga/{id}/aggregate reply; unchecked."""
    return json.loads(body)


def _mangadex_covers(body: bytes) -> _MangaDexCovers:
    """Read a page of MangaDex's GET /cover reply; unchecked."""
    return json.loads(body)


def _mangadex_feed(body: bytes) -> _MangaDexReply[list[_MangaDexFeedChapter]]:
    """Read MangaDex's GET /manga/{id}/feed reply; unchecked."""
    return json.loads(body)


def _mangadex_search(body: bytes) -> _MangaDexReply[list[MangaDexManga]]:
    """Read MangaDex's GET /manga search reply; unchecked."""
    return json.loads(body)


def _anilist_media(body: bytes) -> _AniListReply[_AniListMediaData]:
    """Read AniList's ANILIST_MEDIA reply; unchecked."""
    return json.loads(body)


def _anilist_search(body: bytes) -> _AniListReply[_AniListSearchData]:
    """Read AniList's ANILIST_SEARCH reply; unchecked."""
    return json.loads(body)


def _tapas_page(body: bytes) -> _TapasReply:
    """Read a Tapas episode list page; unchecked."""
    return json.loads(body)


def _webtoons_page(body: bytes) -> _WebtoonsReply:
    """Read a Webtoons episode list page; unchecked."""
    return json.loads(body)
