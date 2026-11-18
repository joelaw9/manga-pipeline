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

MANGABAKA = "https://api.mangabaka.org/v1"
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


# The sources' replies: the fields the program reads, fresh or from the stored record (local library manga-metadata/).
# Each reply is read by its reader at the end of this module, unchecked.


class MangaBakaLink(TypedDict):
    """A site MangaBaka links a series to: its id there (an int, or MangaUpdates' base36 text), null when unknown."""

    id: int | str | None


class MangaBakaLinks(TypedDict):
    """A MangaBaka series' `source`: the sites it links the series to that the program reads (the cross-link hub)."""

    anilist: MangaBakaLink | None
    manga_updates: MangaBakaLink | None
    my_anime_list: MangaBakaLink | None


class MangaBakaTitle(TypedDict):
    """A secondary title of a MangaBaka series."""

    title: str | None


class MangaBakaTag(TypedDict):
    """A weighted tag of a MangaBaka series: its name and weight (core, defining, recurrent, incidental, unweighted)."""

    name: str
    weight: str | None


class MangaBakaSeries(TypedDict):
    """A MangaBaka series record (series, by_source and search give it).

    state is active, merged or deleted; type manga, manhwa, manhua, novel, oel or other; genres are snake_case slugs;
    secondary_titles {language: [title]}; description is Markdown or HTML.
    """

    id: int
    state: str
    type: str | None
    status: str | None
    content_rating: str | None
    title: str | None
    romanized_title: str | None
    native_title: str | None
    secondary_titles: dict[str, list[MangaBakaTitle] | None] | None
    description: str | None
    canonical_url: str | None
    genres: list[str] | None
    tags_v2: list[MangaBakaTag] | None
    source: MangaBakaLinks | None


class _MangaBakaSourceSeries(TypedDict):
    """The data of GET /source/{site}/{id}: the series MangaBaka holds for that id (main story and spin-offs)."""

    series: list[MangaBakaSeries] | None


class _MangaBakaReply[T](TypedDict):
    """MangaBaka's reply envelope: the data."""

    data: T


class MangaUpdatesGenre(TypedDict):
    """A genre of a MangaUpdates series."""

    genre: str


class MangaUpdatesCategory(TypedDict):
    """A voted category of a MangaUpdates series."""

    category: str


class MangaUpdatesTitle(TypedDict):
    """An associated name of a MangaUpdates series."""

    title: str | None


class MangaUpdatesSeries(TypedDict):
    """A MangaUpdates series record: its type (Manga, Novel, ...), names, HTML description, genres and categories."""

    series_id: int
    type: str | None
    title: str | None
    associated: list[MangaUpdatesTitle] | None
    description: str | None
    genres: list[MangaUpdatesGenre] | None
    categories: list[MangaUpdatesCategory] | None


class MangaUpdatesHit(TypedDict):
    """The record of a MangaUpdates search result: the series' id, title and type."""

    series_id: int
    title: str | None
    type: str | None


class MangaUpdatesResult(TypedDict):
    """A MangaUpdates search result: its record and the title the search matched."""

    record: MangaUpdatesHit
    hit_title: str | None


class _MangaUpdatesSearch(TypedDict):
    """The reply of MangaUpdates' series search."""

    results: list[MangaUpdatesResult] | None


class MangaDexLinks(TypedDict):
    """The sites a MangaDex manga links to that the program reads: AniList, MangaUpdates (base36 or numeric), MAL.

    A key is there only for a site the manga links to.
    """

    al: NotRequired[str]
    mu: NotRequired[str]
    mal: NotRequired[str]


class MangaDexTagAttributes(TypedDict):
    """A MangaDex tag: its group (genre, theme, format, content) and its name per language."""

    group: str
    name: dict[str, str]


class MangaDexTag(TypedDict):
    """A tag of a MangaDex manga."""

    attributes: MangaDexTagAttributes


class MangaDexAttributes(TypedDict):
    """A MangaDex manga's attributes the program reads: its links, original language, description per language, tags."""

    links: MangaDexLinks | None
    originalLanguage: str | None
    description: dict[str, str] | None
    tags: list[MangaDexTag]


class MangaDexCoverArt(TypedDict):
    """The attributes of an included cover_art relationship: the cover's file name."""

    fileName: str


class MangaDexRelationship(TypedDict):
    """A relationship of a MangaDex manga; attributes only for one included (cover_art)."""

    type: str
    attributes: NotRequired[MangaDexCoverArt]


class MangaDexManga(TypedDict):
    """A MangaDex manga record (manga and search give it, cover art included): its UUID, attributes, relationships."""

    id: str
    attributes: MangaDexAttributes
    relationships: list[MangaDexRelationship]


class MangaDexAggregateChapter(TypedDict):
    """A chapter of a MangaDex aggregate volume: its number as text."""

    chapter: str | None


class MangaDexAggregateVolume(TypedDict):
    """A volume of a MangaDex aggregate: its number as text ("none": no volume) and its chapters, keyed or listed."""

    volume: str | None
    chapters: dict[str, MangaDexAggregateChapter] | list[MangaDexAggregateChapter]


class MangaDexAggregate(TypedDict):
    """A MangaDex manga's aggregate: its volumes, a mapping keyed by number or a list ([] when empty)."""

    volumes: dict[str, MangaDexAggregateVolume] | list[MangaDexAggregateVolume]


class MangaDexCover(TypedDict):
    """A MangaDex cover's attributes: its volume as text (null for none), locale and file name."""

    volume: str | None
    locale: str | None
    fileName: str


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


class AniListTitle(TypedDict):
    """An AniList media's title forms."""

    romaji: str | None
    english: str | None
    native: str | None


class AniListTag(TypedDict):
    """A tag of an AniList media: its name, rank (percent) and whether it is a spoiler."""

    name: str
    rank: int | None
    isMediaSpoiler: bool | None


class AniListCover(TypedDict):
    """An AniList media's cover image URLs."""

    extraLarge: str | None


class AniListDate(TypedDict):
    """A fuzzy date: each part null when unknown."""

    year: int | None
    month: int | None
    day: int | None


class AniListName(TypedDict):
    """A staff member's name."""

    full: str | None


class AniListStaff(TypedDict):
    """A staff member."""

    name: AniListName


class AniListStaffEdge(TypedDict):
    """A staff member of a media with their role (Story, Art, Story & Art, ...)."""

    role: str | None
    node: AniListStaff


class AniListStaffConnection(TypedDict):
    """A media's staff."""

    edges: list[AniListStaffEdge] | None


class AniListLink(TypedDict):
    """An external link of a media: its site, type (INFO, STREAMING, SOCIAL), URL and language."""

    site: str
    type: str | None
    url: str | None
    language: str | None


class AniListMedia(TypedDict):
    """An AniList manga's Media: the fields ANILIST_MEDIA asks for, each nullable as AniList's schema has it."""

    id: int
    idMal: int | None
    format: str | None
    status: str | None
    title: AniListTitle | None
    synonyms: list[str] | None
    description: str | None
    genres: list[str] | None
    tags: list[AniListTag] | None
    coverImage: AniListCover | None
    startDate: AniListDate | None
    staff: AniListStaffConnection | None
    externalLinks: list[AniListLink] | None


class AniListHit(TypedDict):
    """A media of AniList's title search: the fields ANILIST_SEARCH asks for."""

    id: int
    format: str | None
    title: AniListTitle | None
    synonyms: list[str] | None


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
    publish_date: str | None


class _TapasPagination(TypedDict):
    """A Tapas list page's pagination: whether a page follows, the next page's number and the episode count."""

    has_next: bool
    page: int | None
    total: int | None


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
    episodeTitle: str | None
    exposureDateMillis: int | None


class _WebtoonsResult(TypedDict):
    """A Webtoons list page: its episodes and the cursor of the next page (0 or null after the last)."""

    episodeList: list[_WebtoonsEpisode] | None
    nextCursor: int | None


class _WebtoonsReply(TypedDict):
    """Webtoons' reply: success false for no such series, when it carries no result."""

    success: bool
    result: NotRequired[_WebtoonsResult | None]


# An episode of a platform's list: (site number, title, date as ISO text, the platform's id of it).
type Episode = tuple[int, str | None, str | None, int | None]


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
        """Take the record calls' service (by default a new one with a 0.5 s gap); search gets its own, with 2.1 s."""
        self._service = service or http.Service("mangabaka", 0.5)
        self._search = http.Service("mangabaka", 2.1)  # search has its own, slower limit

    def series(self, mb_id: str) -> MangaBakaSeries | None:
        """Return MangaBaka's series record of mb_id, or None when MangaBaka no longer has it."""
        return _unless_gone(lambda: self._get(self._service, f"/series/{mb_id}", _mangabaka_series))

    def by_source(self, source: str, source_id: str) -> MangaBakaSeries | None:
        """Return the 1 active series MangaBaka holds for an AniList ("anilist") or MangaUpdates ("manga-updates") id.

        None for none or several.
        """
        data = _unless_gone(lambda: self._get(self._service, f"/source/{source}/{source_id}", _mangabaka_source)) or {}
        active = [s for s in data.get("series") or [] if s.get("state") == "active"]
        return active[0] if len(active) == 1 else None

    def search(self, name: str) -> list[MangaBakaSeries]:
        """Return the active series among the results of MangaBaka's title search for name."""
        query = urllib.parse.urlencode({"q": name})
        results = self._get(self._search, f"/series/search?{query}", _mangabaka_search) or []
        return [r for r in results if r.get("state", "active") == "active"]

    @staticmethod
    def _get[T](service: http.Service, path: str, read: Callable[[bytes], _MangaBakaReply[T]]) -> T:
        return service.request("GET", MANGABAKA + path).parse(read)["data"]


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
# unchecked (an approved unchecked boundary: ann-phase.md, "JSON and YAML boundaries")


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
