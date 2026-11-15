"""A series' source ids (SPEC "Source ids"): read from each origin's data, in 1 form per source, and judged for
agreement. Pure: the caller fetches and stores.

Forms: AniList, MyAnimeList and MangaBaka ids are decimal text; a MangaUpdates id is the decimal of the number its
base-36 text spells (an all-digit MangaUpdates link is a retired legacy id and is ignored); a MangaDex id is its
lower-case UUID.
"""

import urllib.parse
import uuid

SOURCES = ("anilist", "mangaupdates", "mangadex", "mangabaka", "myanimelist")
ORIGINS = ("suwayomi", "kavita", "anilist", "mangabaka", "mangadex", "search", "import", "human")
TRACKER_ANILIST = 2  # Suwayomi's tracker ids
TRACKER_MANGAUPDATES = 7
KAVITA_ID_FIELDS = {"aniListId": "anilist", "malId": "myanimelist", "mangaBakaId": "mangabaka"}


def form(source, value):
    """The id in its source's form, or None when value is not one (ValueError for an unknown source)."""
    text = str(value).strip().lower() if value is not None else ""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}: one of {', '.join(SOURCES)}")
    if not text:
        return None
    if source == "mangadex":
        try:
            return str(uuid.UUID(text)) if len(text) == 36 else None
        except ValueError:
            return None
    if source == "mangaupdates" and not text.isdigit():
        return str(int(text, 36)) if text.isascii() and text.isalnum() else None
    return str(int(text)) if text.isascii() and text.isdigit() and int(text) > 0 else None


def mangaupdates_link(text):
    """A MangaUpdates id from a link's base-36 text; None for an all-digit (legacy) one."""
    return None if text.isdigit() else form("mangaupdates", text)


def link_id(url):
    """(source, id) for a series page URL of a known site, else None."""
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host == "anilist.co" and len(path) >= 2 and path[0] == "manga":
        return _pair("anilist", path[1])
    if host == "myanimelist.net" and len(path) >= 2 and path[0] == "manga":
        return _pair("myanimelist", path[1])
    if host == "mangadex.org" and len(path) >= 2 and path[0] == "title":
        return _pair("mangadex", path[1])
    if host == "mangaupdates.com" and path:
        if path[0] == "series" and len(path) >= 2:
            found = mangaupdates_link(path[1])
            return ("mangaupdates", found) if found else None
        if path[0] == "series.html":
            ids = urllib.parse.parse_qs(parts.query).get("id") or [""]
            found = mangaupdates_link(ids[0])
            return ("mangaupdates", found) if found else None
    if host in ("mangabaka.org", "mangabaka.dev"):
        # /<id>[/<slug>] or /<type>/<id>[/<slug>]: the first all-digit part of the first 2
        return next((_pair("mangabaka", p) for p in path[:2] if p.isascii() and p.isdigit()), None)
    return None


def _pair(source, value):
    found = form(source, value)
    return (source, found) if found else None


def suwayomi_ids(manga):
    """Ids of a Suwayomi manga: its AniList and MangaUpdates tracker records, and the MangaDex id of a MangaDex
    source's manga URL ("/title/<uuid>")."""
    found = []
    for record in manga["trackRecords"]["nodes"]:
        source = {TRACKER_ANILIST: "anilist", TRACKER_MANGAUPDATES: "mangaupdates"}.get(record["trackerId"])
        if source and record.get("remoteId"):
            found.append(_pair(source, str(record["remoteId"])))
    if "mangadex" in ((manga.get("source") or {}).get("name") or "").lower():
        path = [p for p in urllib.parse.urlsplit(manga.get("url") or "").path.split("/") if p]
        if len(path) >= 2 and path[0] in ("title", "manga"):
            found.append(_pair("mangadex", path[1]))
    return [pair for pair in found if pair]


def kavita_ids(series, metadata):
    """Ids of a Kavita series: its id fields (series DTO) and its web links (series metadata)."""
    found = [_pair(source, series.get(field)) for field, source in KAVITA_ID_FIELDS.items()]
    found += [link_id(url) for url in (metadata.get("webLinks") or "").split(",") if url.strip()]
    return [pair for pair in found if pair]


def mangabaka_ids(record):
    """A MangaBaka record's own id and its cross-links (AniList, MangaUpdates, MyAnimeList)."""
    links = record.get("source") or {}

    def linked(key):
        return (links.get(key) or {}).get("id")

    mu = linked("manga_updates")
    found = [
        _pair("mangabaka", record.get("id")),
        _pair("anilist", linked("anilist")),
        _pair("myanimelist", linked("my_anime_list")),
        ("mangaupdates", mangaupdates_link(str(mu))) if mu and mangaupdates_link(str(mu)) else None,
    ]
    return [pair for pair in found if pair]


def mangadex_ids(record):
    """A MangaDex record's own id and its cross-links (`links.al`, `links.mu`, `links.mal`)."""
    links = (record.get("attributes") or {}).get("links") or {}
    mu = links.get("mu")
    found = [
        _pair("mangadex", record.get("id")),
        _pair("anilist", links.get("al")),
        _pair("myanimelist", links.get("mal")),
        ("mangaupdates", mangaupdates_link(str(mu))) if mu and mangaupdates_link(str(mu)) else None,
    ]
    return [pair for pair in found if pair]


def anilist_ids(media):
    """An AniList record's own id and its MyAnimeList id (`idMal`)."""
    found = [_pair("anilist", media.get("id")), _pair("myanimelist", media.get("idMal"))]
    return [pair for pair in found if pair]


def resolve(rows):
    """rows: (source, origin, id) of 1 series. Returns ({source: id}, problem): the agreed id per source, and None,
    or the disagreement as text. A `human` id is the source's id whatever the other origins hold."""
    by_source = {}
    for source, origin, value in dict.fromkeys(rows):
        by_source.setdefault(source, []).append((origin, value))
    agreed, problems = {}, []
    for source, held in sorted(by_source.items()):
        human = {value for origin, value in held if origin == "human"}
        values = human or {value for _, value in held}
        if len(values) == 1:
            agreed[source] = values.pop()
        else:
            shown = ", ".join(f"{value} ({origin})" for origin, value in sorted(held, key=lambda h: (h[1], h[0])))
            problems.append(f"{source} ids disagree: {shown}")
    return agreed, "; ".join(problems) or None
