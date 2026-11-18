"""A series' source ids (SPEC "Source ids"): read from each origin's data, in 1 form per source, judged for agreement.

Pure: the caller fetches and stores.

Forms: AniList, MyAnimeList and MangaBaka ids are decimal text; a MangaUpdates id is the decimal of the number its
base-36 text spells (an all-digit MangaUpdates link is a retired legacy id and is ignored); a MangaDex id is its
lower-case UUID.
"""

import urllib.parse
import uuid
from collections.abc import Iterable

from manga_pipeline.replies.kavita import Series, SeriesMetadata
from manga_pipeline.replies.sources import AniListMedia, MangaBakaSeries, MangaDexManga
from manga_pipeline.replies.suwayomi import Manga

SOURCES = ("anilist", "mangaupdates", "mangadex", "mangabaka", "myanimelist")
TRACKER_ANILIST = 2  # Suwayomi's tracker ids
TRACKER_MANGAUPDATES = 7
KAVITA_ID_FIELDS = {"aniListId": "anilist", "malId": "myanimelist", "mangaBakaId": "mangabaka"}
UUID_TEXT_LEN = 36  # a UUID's 32 hex digits and 4 hyphens
KIND_ID_PARTS = 2  # a link path's /<kind>/<id>
# Sites whose series page is /<kind>/<id>: {host: (kind, source)}
KIND_ID_SITES = {
    "anilist.co": ("manga", "anilist"),
    "myanimelist.net": ("manga", "myanimelist"),
    "mangadex.org": ("title", "mangadex"),
}
type Pair = tuple[str, str]  # (source, id in its form)
type Known = tuple[str, str, str]  # (source, origin, id) of 1 series


def form(source: str, value: str | int | None) -> str | None:
    """Return the id in its source's form, or None when value is not one (ValueError for an unknown source)."""
    text = str(value).strip().lower() if value is not None else ""
    if source not in SOURCES:
        msg = f"unknown source {source!r}: one of {', '.join(SOURCES)}"
        raise ValueError(msg)
    if not text:
        return None
    if source == "mangadex":
        try:
            return str(uuid.UUID(text)) if len(text) == UUID_TEXT_LEN else None
        except ValueError:
            return None
    if source == "mangaupdates" and not text.isdigit():
        return str(int(text, 36)) if text.isascii() and text.isalnum() else None
    return str(int(text)) if text.isascii() and text.isdigit() and int(text) > 0 else None


def mangaupdates_link(text: str) -> str | None:
    """Return a MangaUpdates id from a link's base-36 text; None for an all-digit (legacy) one."""
    return None if text.isdigit() else form("mangaupdates", text)


def link_id(url: str) -> Pair | None:
    """(source, id) for a series page URL of a known site, else None."""
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host in KIND_ID_SITES:
        kind, source = KIND_ID_SITES[host]
        return _pair(source, path[1]) if len(path) >= KIND_ID_PARTS and path[0] == kind else None
    if host == "mangaupdates.com":
        return _mangaupdates_page(path, parts.query)
    if host in ("mangabaka.org", "mangabaka.dev"):
        # /<id>[/<slug>] or /<type>/<id>[/<slug>]: the first all-digit part of the first 2
        return next((_pair("mangabaka", p) for p in path[:2] if p.isascii() and p.isdigit()), None)
    return None


def _mangaupdates_page(path: list[str], query: str) -> Pair | None:
    """(mangaupdates, id) of a MangaUpdates series page: /series/<base-36 id>[/<slug>] or series.html?id=<id>."""
    if path[:1] == ["series"] and len(path) >= KIND_ID_PARTS:
        text = path[1]
    elif path[:1] == ["series.html"]:
        text = (urllib.parse.parse_qs(query).get("id") or [""])[0]
    else:
        return None
    found = mangaupdates_link(text)
    return ("mangaupdates", found) if found else None


def _pair(source: str, value: str | int | None) -> Pair | None:
    found = form(source, value)
    return (source, found) if found else None


def suwayomi_ids(manga: Manga) -> list[Pair]:
    """Return the ids of a Suwayomi manga.

    Its AniList and MangaUpdates tracker records, and the MangaDex id of a MangaDex source's manga URL
    ("/title/<uuid>").
    """
    found: list[Pair | None] = []
    for record in manga["trackRecords"]["nodes"]:
        source = {TRACKER_ANILIST: "anilist", TRACKER_MANGAUPDATES: "mangaupdates"}.get(record["trackerId"])
        if source and record.get("remoteId"):
            found.append(_pair(source, str(record["remoteId"])))
    if "mangadex" in ((manga.get("source") or {}).get("name") or "").lower():
        path = [p for p in urllib.parse.urlsplit(manga.get("url") or "").path.split("/") if p]
        if len(path) >= KIND_ID_PARTS and path[0] in ("title", "manga"):
            found.append(_pair("mangadex", path[1]))
    return [pair for pair in found if pair]


def kavita_ids(series: Series, metadata: SeriesMetadata) -> list[Pair]:
    """Ids of a Kavita series: its id fields (series DTO) and its web links (series metadata)."""
    found = [_pair(source, series.get(field)) for field, source in KAVITA_ID_FIELDS.items()]
    found += [link_id(url) for url in (metadata.get("webLinks") or "").split(",") if url.strip()]
    return [pair for pair in found if pair]


def mangabaka_ids(record: MangaBakaSeries) -> list[Pair]:
    """Return a MangaBaka record's own id and its cross-links (AniList, MangaUpdates, MyAnimeList)."""
    links = record.get("source") or {}

    def linked(key: str) -> int | str | None:
        return (links.get(key) or {}).get("id")

    mu = linked("manga_updates")
    found = [
        _pair("mangabaka", record.get("id")),
        _pair("anilist", linked("anilist")),
        _pair("myanimelist", linked("my_anime_list")),
        ("mangaupdates", mangaupdates_link(str(mu))) if mu and mangaupdates_link(str(mu)) else None,
    ]
    return [pair for pair in found if pair]


def mangadex_ids(record: MangaDexManga) -> list[Pair]:
    """Return a MangaDex record's own id and its cross-links (`links.al`, `links.mu`, `links.mal`)."""
    links = (record.get("attributes") or {}).get("links") or {}
    mu = links.get("mu")
    found = [
        _pair("mangadex", record.get("id")),
        _pair("anilist", links.get("al")),
        _pair("myanimelist", links.get("mal")),
        ("mangaupdates", mangaupdates_link(str(mu))) if mu and mangaupdates_link(str(mu)) else None,
    ]
    return [pair for pair in found if pair]


def anilist_ids(media: AniListMedia) -> list[Pair]:
    """Return an AniList record's own id and its MyAnimeList id (`idMal`)."""
    found = [_pair("anilist", media.get("id")), _pair("myanimelist", media.get("idMal"))]
    return [pair for pair in found if pair]


def resolve(rows: Iterable[Known]) -> tuple[dict[str, str], str | None]:
    """Resolve 1 series' ids into the agreed id per source.

    rows: (source, origin, id) of 1 series. Returns ({source: id}, problem): the agreed id per source, and None, or
    the disagreement as text. A `human` id is the source's id whatever the other origins hold.
    """
    by_source: dict[str, list[tuple[str, str]]] = {}
    for source, origin, value in dict.fromkeys(rows):
        by_source.setdefault(source, []).append((origin, value))
    agreed: dict[str, str] = {}
    problems: list[str] = []
    for source, held in sorted(by_source.items()):
        human = {value for origin, value in held if origin == "human"}
        values = human or {value for _, value in held}
        if len(values) == 1:
            agreed[source] = values.pop()
        else:
            shown = ", ".join(f"{value} ({origin})" for origin, value in sorted(held, key=lambda h: (h[1], h[0])))
            problems.append(f"{source} ids disagree: {shown}")
    return agreed, "; ".join(problems) or None
