"""The 4 metadata sources' clients (SPEC "Source ids", "Metadata checks"): MangaBaka, MangaUpdates, MangaDex, AniList.

Each client is thin: a record by id (None when the source no longer has it), and a title search returning the raw
results; which result is accepted is `match`'s job. Every record is the source's own JSON, stored as fetched. 1 client
per source per run, each on its own `http.Service` with the source's minimum gap between requests.
"""

import urllib.parse

from manga_pipeline.clients import http

MANGABAKA = "https://api.mangabaka.org/v1"
MANGAUPDATES = "https://api.mangaupdates.com/v1"
MANGADEX = "https://api.mangadex.org"
MANGADEX_UPLOADS = "https://uploads.mangadex.org"
ANILIST = "https://graphql.anilist.co"
MANGADEX_RATINGS = [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
SEARCH_RESULTS = 10
IMAGE_TIMEOUT = 120  # seconds
COVER_PAGE = 100  # the largest page of covers MangaDex serves
ANILIST_MEDIA = """query ($id: Int) { Media(id: $id, type: MANGA) {
  id idMal format status title { romaji english native } synonyms description genres
  tags { name rank isMediaSpoiler } coverImage { extraLarge } startDate { year month day }
  staff { edges { role node { name { full } } } } } }"""
ANILIST_SEARCH = """query ($s: String) { Page(perPage: 8) { media(search: $s, type: MANGA) {
  id format title { romaji english native } synonyms } } }"""


class SourceError(Exception):
    """A source answered with an error that is not "not found" (AniList's GraphQL `errors`)."""


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
        self._service = http.Service("mangadex", 0.25)

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
