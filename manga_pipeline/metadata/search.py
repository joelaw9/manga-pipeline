"""Title search for a source a series holds no id for (SPEC "Source ids"): which result, if any, is the series.

A result is accepted when exactly 1 result's title or alternative title equals a name under the lenient key
(`names.key`); MangaBaka also accepts its top result for the first name when it is close (`close`) and no other result
is; MangaUpdates also accepts exactly 1 of its top 3 results whose associated names hold a name. Novels never match: the
library is comics. Each function returns the found id in its source's form, or None.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet

from rapidfuzz import fuzz

from manga_pipeline import names
from manga_pipeline.metadata import ids, sources
from manga_pipeline.metadata.records import SeriesRecords
from manga_pipeline.replies.sources import AniListHit, MangaBakaSeries, MangaUpdatesSeries

CLOSE_RATIO = 90  # rapidfuzz ratio of the lenient keys (D7.18's threshold)
PREFIX_LENGTH = 15  # a lenient key at least this long that starts the other counts as close (a cut-off title)
NAMES_TRIED = 4  # names searched per source (MangaDex: 8, as MangaDex search matches less)
MANGADEX_NAMES_TRIED = 8
MANGAUPDATES_ASSOCIATED_CHECKED = 3
SUFFIXES_STRIPPED = 2
# A trailing bracketed suffix ("(Official)", "〘Official〙"): flat text, the old metadata tool's brackets
OPENING = "([«〘≪【{<" + chr(0xFF08)  # and the fullwidth left parenthesis
CLOSING = ")]»〙≫】}>" + chr(0xFF09)
TRAILING_BRACKETS = re.compile(
    rf"\s*[{re.escape(OPENING)}][^{re.escape(OPENING + CLOSING)}]*[{re.escape(CLOSING)}]\s*$"
)
TYPE_SUFFIX = re.compile(r"[(\[]\s*(manhwa|manhua|manga|webtoon)\s*[)\]]\s*$", re.IGNORECASE)


def close(a: str, b: str) -> bool:
    """Return whether 2 titles are close under the lenient key.

    Close: 1 key of at least PREFIX_LENGTH starts the other (a cut-off title), or their rapidfuzz ratio is at least
    CLOSE_RATIO. An empty key is never close.
    """
    ka, kb = names.key(a), names.key(b)
    if not ka or not kb:
        return False
    if min(len(ka), len(kb)) >= PREFIX_LENGTH and (ka.startswith(kb) or kb.startswith(ka)):
        return True
    return fuzz.ratio(ka, kb) >= CLOSE_RATIO


def unique(series_names: Iterable[str | None]) -> list[str]:
    """Return the names, 1 per lenient key (the first spelling kept), leaving out names with an empty key."""
    seen: dict[str, str] = {}
    for name in series_names:
        if name and (k := names.key(name)) and k not in seen:
            seen[k] = name
    return list(seen.values())


def variants(series_names: Sequence[str]) -> list[str]:
    """Return the names, then each name with up to 2 trailing bracketed suffixes removed, 1 at a time.

    "Title (Official)" and "Title 〘Official〙" also give "Title"; a name the removal would leave with no letters or
    digits stops there. 1 per lenient key (`unique`).
    """
    found = list(series_names)
    for name in series_names:
        stripped = name
        for _ in range(SUFFIXES_STRIPPED):
            shorter = TRAILING_BRACKETS.sub("", stripped)
            if shorter == stripped or not names.key(shorter):
                break
            found.append(shorter)
            stripped = shorter
    return unique(found)


def lookup_names(series_names: Sequence[str], records: SeriesRecords) -> list[str]:
    """Return the AniList and MangaDex search names (the old tool's order).

    The series' names, then MangaBaka's main title (MangaUpdates' when there is no MangaBaka record), then the
    suffix-stripped names (`variants`), then MangaBaka's (else MangaUpdates') other titles. 1 per lenient key.
    """
    mb, mu = records["mangabaka"], records["mangaupdates"]
    if mb is not None:
        titles = mangabaka_titles(mb)
    elif mu is not None:
        titles = [t for t in [mu.get("title"), *(a.get("title") for a in mu.get("associated") or [])] if t]
    else:
        titles = []
    return unique([*series_names, *titles[:1], *variants(series_names), *titles])


def _type_hint(series_names: Iterable[str]) -> set[str]:
    """Return the MangaUpdates types a name's `(Manhwa)`-style suffix allows; empty when no name has one."""
    hints = {m[1].casefold() for n in series_names if (m := TYPE_SUFFIX.search(n))}
    return {"manhwa", "manhua"} if "webtoon" in hints else hints


def _exactly_one[R](
    results: Iterable[R],
    wanted: AbstractSet[str],
    titles_of: Callable[[R], Iterable[str | None]],
    id_of: Callable[[R], int],
) -> int | None:
    hits = {id_of(r) for r in results if wanted & {names.key(t) for t in titles_of(r) if t}}
    return hits.pop() if len(hits) == 1 else None


def _wanted(series_names: Iterable[str]) -> set[str]:
    return {names.key(n) for n in series_names if names.key(n)}


def mangabaka_titles(record: MangaBakaSeries) -> list[str]:
    """Return a MangaBaka record's titles: its title, romanized and native titles, then its secondary titles."""
    titles = [record.get("title"), record.get("romanized_title"), record.get("native_title")]
    for group in (record.get("secondary_titles") or {}).values():
        titles += [t.get("title") for t in group or []]
    return [t for t in titles if t]


def mangabaka(client: sources.MangaBaka, series_names: Sequence[str], agreed: Mapping[str, str]) -> str | None:
    """Find the series' MangaBaka id: by the series' AniList id, else its MangaUpdates id.

    A title search only when it holds neither, or when they lead to a novel (a manga's ids pointing at its light
    novel).
    """
    found, search = _mangabaka_by_ids(client, agreed)
    return _mangabaka_by_title(client, series_names) if search else found


def _mangabaka_by_ids(client: sources.MangaBaka, agreed: Mapping[str, str]) -> tuple[str | None, bool]:
    """Return (the MangaBaka id the series' ids lead to, or None; whether to search by title instead).

    The id is the one the series' AniList id, else its MangaUpdates id, leads to. A search when the series holds
    neither id, or they lead only to novels.
    """
    lookups: list[tuple[str, str]] = []
    if "anilist" in agreed:
        lookups.append(("anilist", agreed["anilist"]))
    if "mangaupdates" in agreed:
        lookups.append(("manga-updates", _base36(int(agreed["mangaupdates"]))))
    novel = False
    for source, value in lookups:
        found = client.by_source(source, value)
        if found and not _novel(found):
            return ids.form("mangabaka", found["id"]), False
        novel = novel or bool(found)
    return None, novel or not lookups


def _mangabaka_by_title(client: sources.MangaBaka, series_names: Sequence[str]) -> str | None:
    """Return the 1 result whose title is a name, searched name by name.

    Else the first name's top result when it is close and no other result is.
    """
    wanted = _wanted(series_names)
    first: list[MangaBakaSeries] | None = None
    for name in series_names[:NAMES_TRIED]:
        results = [r for r in client.search(name) if not _novel(r)]
        if first is None:
            first = results
        hit = _exactly_one(results, wanted, mangabaka_titles, lambda r: r["id"])
        if hit is not None:
            return ids.form("mangabaka", hit)
    if first:
        closest = [any(close(n, t) for n in series_names for t in mangabaka_titles(r)) for r in first]
        if closest[0] and not any(closest[1:]):
            return ids.form("mangabaka", first[0]["id"])
    return None


def mangaupdates(client: sources.MangaUpdates, series_names: Sequence[str]) -> str | None:
    """Find the series' MangaUpdates id by title search, name by name.

    Exactly 1 series whose title or hit title is a name; else, when no result's title matches, exactly 1 of the top
    MANGAUPDATES_ASSOCIATED_CHECKED results whose associated names hold a name (each record fetched once, reused
    across names). Novels are left out; a name ending in `(Manhwa)`, `(Manhua)`, `(Manga)` or `(Webtoon)` keeps only
    results of that type (Webtoon: Manhwa or Manhua).
    """
    wanted = _wanted(series_names)
    types = _type_hint(series_names)
    fetched: dict[int, MangaUpdatesSeries | None] = {}
    for name in series_names[:NAMES_TRIED]:
        results = [
            r
            for r in client.search(name)
            if (kind := (r["record"].get("type") or "").casefold()) != "novel" and (not types or kind in types)
        ]
        hits = {
            r["record"]["series_id"]
            for r in results
            if wanted & {names.key(r["record"].get("title") or ""), names.key(r.get("hit_title") or "")}
        }
        if len(hits) == 1:
            return ids.form("mangaupdates", hits.pop())
        if hits:
            continue  # several series share this name: try the next name
        for r in results[:MANGAUPDATES_ASSOCIATED_CHECKED]:
            series_id = r["record"]["series_id"]
            if series_id not in fetched:
                fetched[series_id] = client.series(series_id)
            record = fetched[series_id] or {}
            associated = [record.get("title")] + [a.get("title") for a in record.get("associated") or []]
            if wanted & {names.key(t) for t in associated if t}:
                hits.add(series_id)
        if len(hits) == 1:
            return ids.form("mangaupdates", hits.pop())
    return None


def mangadex(client: sources.MangaDex, series_names: Iterable[str], agreed: Mapping[str, str]) -> str | None:
    """Return the MangaDex entry whose MangaUpdates link is the series' MangaUpdates id; none without one."""
    if "mangaupdates" not in agreed:
        return None
    for name in list(dict.fromkeys(series_names))[:MANGADEX_NAMES_TRIED]:
        for record in client.search(name):
            if ("mangaupdates", agreed["mangaupdates"]) in ids.mangadex_ids(record):
                return ids.form("mangadex", record["id"])
    return None


def anilist_titles(media: AniListHit) -> list[str | None]:
    """Return an AniList record's titles: each of its title forms, then its synonyms."""
    title = media.get("title")
    forms = [title["romaji"], title["english"], title["native"]] if title else []
    return [*forms, *(media.get("synonyms") or [])]


def anilist(client: sources.AniList, series_names: Sequence[str]) -> str | None:
    """Find the series' AniList id by title search: the 1 non-novel result whose title or synonym is a name."""
    wanted = _wanted(series_names)
    for name in series_names[:NAMES_TRIED]:
        results = [m for m in client.search(name) if m.get("format") != "NOVEL"]
        hit = _exactly_one(results, wanted, anilist_titles, lambda m: m["id"])
        if hit is not None:
            return ids.form("anilist", hit)
    return None


def _novel(record: MangaBakaSeries) -> bool:
    return (record.get("type") or "").lower() == "novel"


def _base36(number: int) -> str:
    """Return a MangaUpdates id as the 7 base-36 digits MangaBaka and MangaDex keep ("04gtb06")."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while number:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
    return (out or "0").zfill(7)
