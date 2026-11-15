"""The metadata sources' clients (SPEC "Source ids", "Metadata checks"): MangaBaka, MangaUpdates, MangaDex, AniList,
and the official platforms' episode lists, Tapas and Webtoons (SPEC "Metadata", platform lists).

Each client is thin: a record by id (None when the source no longer has it), and a title search returning the raw
results; which result is accepted is `match`'s job. Every record is the source's own JSON, stored as fetched. 1 client
per source per run, each on its own `http.Service` with the source's minimum gap between requests.
"""

import urllib.parse
from datetime import UTC, datetime
from html.parser import HTMLParser

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


class SourceError(Exception):
    """A source answered with an error that is not "not found" (AniList's GraphQL `errors`)."""


def _gone(call, statuses=(404,)):
    """The call's result, or None when the site answers that the page is gone."""
    try:
        return call()
    except http.HttpError as e:
        if e.status in statuses:
            return None
        raise


def _unless_gone(call):
    """The call's result, or None when the source answers 404 (the entry is gone or merged)."""
    try:
        return call()
    except http.HttpError as e:
        if e.status == 404:
            return None
        raise


class MangaBaka:
    def __init__(self):
        self._service = http.Service("mangabaka", 0.5)
        self._search = http.Service("mangabaka", 2.1)  # search has its own, slower limit

    def series(self, mb_id):
        return _unless_gone(lambda: self._get(self._service, f"/series/{mb_id}"))

    def by_source(self, source, source_id):
        """The 1 active series MangaBaka holds for an AniList ("anilist") or MangaUpdates ("manga-updates") id; None
        for none or several."""
        data = _unless_gone(lambda: self._get(self._service, f"/source/{source}/{source_id}")) or {}
        active = [s for s in data.get("series") or [] if s.get("state") == "active"]
        return active[0] if len(active) == 1 else None

    def search(self, name):
        query = urllib.parse.urlencode({"q": name})
        results = self._get(self._search, f"/series/search?{query}") or []
        return [r for r in results if r.get("state", "active") == "active"]

    @staticmethod
    def _get(service, path):
        return service.request("GET", MANGABAKA + path).json()["data"]


class MangaUpdates:
    def __init__(self):
        self._service = http.Service("mangaupdates", 0.5)

    def series(self, mu_id):
        return _unless_gone(lambda: self._service.request("GET", f"{MANGAUPDATES}/series/{mu_id}").json())

    def search(self, name):
        """The search's results: each {"record": {...}, "hit_title": ...}."""
        body = {"search": name, "perpage": SEARCH_RESULTS}
        reply = self._service.request("POST", f"{MANGAUPDATES}/series/search", json=body).json()
        return reply.get("results") or []


class MangaDex:
    def __init__(self):
        self._service = http.Service("mangadex", 0.25, MANGADEX_USER_AGENT)

    def manga(self, md_id):
        query = urllib.parse.urlencode([("includes[]", "cover_art")])
        return _unless_gone(lambda: self._get(f"/manga/{md_id}?{query}")["data"])

    def aggregate(self, md_id):
        return _unless_gone(lambda: self._get(f"/manga/{md_id}/aggregate"))

    def covers(self, md_id):
        """Every cover's attributes (volume, locale, fileName), in volume order, page by page."""
        out, offset = [], 0
        while True:
            query = urllib.parse.urlencode(
                [("manga[]", md_id), ("order[volume]", "asc"), ("limit", COVER_PAGE), ("offset", offset)]
            )
            page = self._get(f"/cover?{query}")
            out += [c["attributes"] for c in page["data"]]
            offset += len(page["data"])
            if not page["data"] or offset >= page["total"]:
                return out

    def latest_chapter_date(self, md_id):
        """The publish time (ISO text) of the manga's newest chapter in any language, or None."""
        query = urllib.parse.urlencode(
            [("limit", 1), ("order[publishAt]", "desc"), *MANGADEX_RATINGS, ("includeFuturePublishAt", 0)]
        )
        chapters = self._get(f"/manga/{md_id}/feed?{query}")["data"]
        return chapters[0]["attributes"]["publishAt"] if chapters else None

    def search(self, name):
        query = urllib.parse.urlencode(
            [("title", name), ("limit", SEARCH_RESULTS), ("includes[]", "cover_art"), *MANGADEX_RATINGS]
        )
        return self._get(f"/manga?{query}")["data"]

    def cover_image(self, md_id, file_name):
        url = f"{MANGADEX_UPLOADS}/covers/{md_id}/{urllib.parse.quote(file_name)}"
        return self._service.request("GET", url, headers={"Accept": "image/*"}, timeout=IMAGE_TIMEOUT).body

    def _get(self, path):
        return self._service.request("GET", MANGADEX + path).json()


class AniList:
    def __init__(self):
        self._service = http.Service("anilist", 2.1)

    def media(self, al_id):
        data = _unless_gone(lambda: self._query(ANILIST_MEDIA, {"id": int(al_id)}))
        return None if data is None else data["Media"]

    def search(self, name):
        return (self._query(ANILIST_SEARCH, {"s": name})["Page"] or {}).get("media") or []

    def cover_image(self, url):
        """The media's `coverImage.extraLarge` image (served by AniList's image host)."""
        return self._service.request("GET", url, headers={"Accept": "image/*"}, timeout=IMAGE_TIMEOUT).body

    def _query(self, query, variables):
        reply = self._service.request("POST", ANILIST, json={"query": query, "variables": variables}).json()
        if reply.get("errors"):
            raise SourceError("AniList: " + "; ".join(str(e.get("message")) for e in reply["errors"]))
        return reply["data"]


class Tapas:
    """Tapas' own site endpoints (local library tapas.md). A 403 is Tapas blocking: unreachable for the run."""

    GONE = (400, 404)

    def __init__(self):
        self._service = http.Service("tapas", 0.5)

    def series_id(self, slug):
        """The numeric series id from the series' /info page (its `tapastic://series/<id>` meta; the page carries it
        only when asked for HTML), or None when the page is gone or carries none."""
        url = f"{TAPAS}/series/{urllib.parse.quote(slug)}/info"
        reply = _gone(lambda: self._get(url, "text/html"), self.GONE)
        if reply is None:
            return None
        finder = _MetaFinder("tapastic://series/")
        finder.feed(reply.body.decode("utf-8", "replace"))
        return finder.found.split("/")[0] if finder.found and finder.found.split("/")[0].isdigit() else None

    def episodes(self, series_id):
        """([(site number, title, date, id)] in site order, the count Tapas says it has), or None when the list is
        gone. Episodes are kept once each, by id."""
        items, seen, total, page = [], set(), 0, 1
        while True:
            url = f"{TAPAS}/series/{series_id}/episodes?page={page}&sort=OLDEST&max_limit={TAPAS_PAGE}"
            # a gone first page is no list; a page gone later is an error: the stored list is kept
            reply = _gone(lambda url=url: self._get(url), self.GONE if page == 1 else ())
            if reply is None:
                return None
            data = reply.json()["data"]
            for e in data["episodes"]:
                if e["id"] not in seen:
                    seen.add(e["id"])
                    items.append((e["scene"], e["title"], (e.get("publish_date") or "")[:10] or None, e["id"]))
            total = max(total, data["pagination"].get("total") or 0)
            following = data["pagination"].get("page") or 0  # Tapas gives the next page's number
            if not data["pagination"]["has_next"] or not data["episodes"] or following <= page:
                return items, total
            page = following

    def _get(self, url, accept="application/json"):
        try:
            return self._service.request("GET", url, headers={"Accept": accept})
        except http.HttpError as e:
            if e.status == 403:
                raise self._service.mark_unreachable(f"HTTP 403 from {http.redact_url(url)}: blocked") from None
            raise


class Webtoons:
    """Webtoons' own endpoints (local library webtoons.md); a ref is "<webtoon|canvas>:<title_no>"."""

    def __init__(self):
        self._service = http.Service("webtoons", 0.5)

    def episodes(self, ref):
        """[(site number, title, date, None)] in site order, or None when Webtoons has no such series
        (`success` false, or a page that is gone)."""
        kind, number = ref.split(":")
        items, cursor = [], None
        while True:
            url = f"{WEBTOONS_API}/{kind}/{number}/episodes?pageSize={WEBTOONS_PAGE}"
            url += f"&cursor={cursor}" if cursor else ""
            # a gone first page is no list; a page gone later is an error: the stored list is kept
            reply = _gone(lambda url=url: self._service.request("GET", url), (400, 404) if cursor is None else ())
            if reply is None:
                return None
            data = reply.json()
            if not data.get("success"):
                if cursor is None:
                    return None
                raise SourceError(f"Webtoons: {kind} {number} failed after cursor {cursor}")
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

    def app_only(self, ref):
        """Whether the web list page says episodes are only on the app (its `detail_install_app` block); None when the
        page cannot be read."""
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


class _MetaFinder(HTMLParser):
    """The rest of the first <meta content> that starts with prefix."""

    def __init__(self, prefix):
        super().__init__()
        self.prefix, self.found = prefix, None

    def handle_starttag(self, tag, attrs):
        content = dict(attrs).get("content") or ""
        if tag == "meta" and self.found is None and content.startswith(self.prefix):
            self.found = content[len(self.prefix) :]


class _AttributeFinder(HTMLParser):
    """Whether any element carries the name as a class or as its id."""

    def __init__(self, name):
        super().__init__()
        self.name, self.found = name, False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.name in (values.get("class") or "").split() or values.get("id") == self.name:
            self.found = True
