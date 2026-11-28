"""Official platforms of a series (SPEC "Metadata", platform lists): its platform page, and its list's volumes, titles.

Which Tapas or Webtoons page a series' links name, and what its stored episode list gives the resolver (volumes from
season breaks, chapter and side-story titles). Pure; the bulk converter's optimizer/naming.py `_platform_for`,
`platform_volumes`, `chapter_titles`, `side_titles`.
"""

import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import TypedDict, Unpack

from manga_pipeline import names
from manga_pipeline.metadata import episodes
from manga_pipeline.metadata.episodes import Coverage, EpisodeRecord, SeasonRange
from manga_pipeline.replies.sources import AniListMedia, Episode

TAPAS, WEBTOONS = "tapas", "webtoons"
BASES = {f"{TAPAS} seasons": TAPAS, f"{WEBTOONS} seasons": WEBTOONS}  # a recorded volume basis -> its platform
ANILIST_SITES = {"Tapas": TAPAS, "WEBTOON": WEBTOONS}
ENGLISH = "English"
KIND = "episodes"  # a stored platform list's kind in `fetched` (its source: tapas or webtoons)
SERIES_PATH_PARTS = 2  # /series/<name>


class _Extra(TypedDict, total=False):
    """What a platform's stored list holds beside the others: Tapas' series id."""

    series_id: str


class PlatformList(_Extra):
    """A stored platform list (_record): its platform, page ref, episode records, coverage and season ranges."""

    name: str
    ref: str
    episodes: list[EpisodeRecord]
    coverage: Coverage
    seasons: list[SeasonRange]


def tapas_ref(url: str | None) -> str | None:
    """Return the series slug of a Tapas series link (tapas.io/series/<slug>[/info], any host spelling), or None."""
    parts = urllib.parse.urlsplit(urllib.parse.unquote(url or "").strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host != "tapas.io" or len(path) < SERIES_PATH_PARTS or path[0] != "series":
        return None
    return path[1]


def webtoons_ref(url: str | None) -> str | None:
    """Return "<type>:<title_no>" of a Webtoons series link, or None.

    Type "canvas" for a /canvas/ or /challenge/ path and "webtoon" otherwise (the same number is another series under
    each). A link stored URL-encoded (list%3Ftitle_no=N) reads the same.
    """
    parts = urllib.parse.urlsplit(urllib.parse.unquote(url or "").strip())
    host = parts.netloc.lower()
    if not (host == "webtoons.com" or host.endswith(".webtoons.com")):
        return None
    query = {k.lower(): v for k, v in urllib.parse.parse_qs(parts.query).items()}
    numbers = query.get("title_no") or query.get("titleno")
    if not numbers or not numbers[0].isdigit():
        return None
    path = [p.lower() for p in parts.path.split("/") if p]
    kind = "canvas" if len(path) > 1 and path[1] in ("canvas", "challenge") else "webtoon"
    return f"{kind}:{numbers[0]}"


REFS: dict[str, Callable[[str | None], str | None]] = {TAPAS: tapas_ref, WEBTOONS: webtoons_ref}


def anilist_links(media: AniListMedia | None) -> list[str]:
    """Return the English Tapas and WEBTOON streaming links of an AniList media record."""
    return [
        url
        for link in (media or {}).get("externalLinks") or []
        if link.get("type") == "STREAMING"
        and link.get("site") in ANILIST_SITES
        and (url := link.get("url"))
        and link.get("language") in (ENGLISH, None)
        and _english_path(ANILIST_SITES[link["site"]], url)
    ]


def _english_path(platform: str, url: str) -> bool:
    """Tapas is English only; a Webtoons link's first path part is its language (/en/)."""
    if platform == TAPAS:
        return True
    path = [p for p in urllib.parse.urlsplit(urllib.parse.unquote(url)).path.split("/") if p]
    return bool(path) and path[0].lower() == "en"


def candidates(kavita_links: Sequence[str], anilist_links_: Sequence[str], basis: str | None) -> list[tuple[str, str]]:
    """Return [(platform, ref)] to try in order (SPEC "Metadata", platform lists).

    The Kavita links, else the AniList links; a recorded platform basis names its platform alone, else Tapas before
    Webtoons; 1 ref per platform.
    """
    links = kavita_links if any(ref(u) for u in kavita_links for ref in REFS.values()) else anilist_links_
    order = [BASES[basis]] if basis in BASES else [TAPAS, WEBTOONS]
    out: list[tuple[str, str]] = []
    for platform in order:
        ref = next((r for r in map(REFS[platform], links) if r), None)
        if ref:
            out.append((platform, ref))
    return out


def tapas_record(ref: str, items: Sequence[Episode], listed_total: int, series_id: str) -> PlatformList:
    """Build a stored Tapas list (record) from the site's items, its listed episode count and its series id.

    items: [(site number, title, date, id)]; listed_total: the number of episodes the series says it has.
    """
    eps = episodes.build(items)
    return _record(TAPAS, ref, eps, episodes.coverage(eps, listed_total), series_id=series_id)


def webtoons_record(ref: str, items: Sequence[Episode], *, app_only: bool | None) -> PlatformList:
    """Build a stored Webtoons list (record) from the site's items and whether the series keeps episodes to its app.

    items: [(site number, title, date, id)]; app_only None: its page was not read.
    """
    eps = episodes.build(items)
    cover = episodes.coverage(eps, app_only=bool(app_only), list_page_read=app_only is not None)
    return _record(WEBTOONS, ref, eps, cover)


def _record(
    platform: str, ref: str, eps: list[episodes.EpisodeRecord], cover: episodes.Coverage, **extra: Unpack[_Extra]
) -> PlatformList:
    """Build a stored platform list: {"name", "ref", "episodes", "coverage", "seasons"}.

    It also holds extra (e.g. Tapas' series id); built from the site's episode records (episodes.build) and their
    coverage (episodes.coverage).
    """
    return {
        "name": platform,
        "ref": ref,
        **extra,
        "episodes": eps,
        "coverage": cover,
        "seasons": episodes.season_breaks(eps),
    }


def main_episodes(platform: PlatformList | None) -> int:
    """Return how many main-story episodes a stored platform list holds; 0 for no list."""
    if platform is None:
        return 0
    return sum(1 for e in platform["episodes"] if e["kind"] == episodes.MAIN)


def volumes(platform: PlatformList | None, numbers: Mapping[int, float]) -> tuple[dict[int, int], set[int]]:
    """Return ({key: volume}, {key of a chapter past the main-story finale}) of main chapters ({key: number}).

    From a stored platform list's season ranges, matched by chapter number; empty when unusable: no ranges, or a list
    that is not complete while chapters lie outside its ranges. Volumes are 1..n in range order; a chapter in the range
    after "Main Story Finale", or past the last closed range of a list whose main story ended, is past the finale;
    another chapter past the list goes in the next volume; one before the first range in volume 1.
    """
    if platform is None:
        return {}, set()
    ranges = platform["seasons"]
    if not ranges or not numbers:
        return {}, set()

    def inside(n: float, b: episodes.SeasonRange) -> bool:
        return n >= float(b["first"]) and (b["last"] is None or n <= float(b["last"]))

    outside = [k for k, n in numbers.items() if not any(inside(n, b) for b in ranges)]
    if platform["coverage"]["status"] != "complete" and outside:
        return {}, set()
    story = [b for b in ranges if not b["side"]]
    main_ended = any((m := e["season_marker"]) is not None and m["type"] == "main_end" for e in platform["episodes"])
    last = story[-1]["last"] if story else None
    vols: dict[int, int] = {}
    side: set[int] = set()
    for k, n in numbers.items():
        b = next((b for b in ranges if inside(n, b)), None)
        if b is not None and b["side"]:
            side.add(k)
        elif b is not None:
            vols[k] = story.index(b) + 1
        elif last is not None and n > float(last):
            if main_ended or any(x["side"] for x in ranges):
                side.add(k)
            else:
                vols[k] = len(story) + 1
        else:  # before the first range: a prologue the site does not list
            vols[k] = 1
    return vols, side


def chapter_titles(platform: PlatformList | None) -> dict[str, str]:
    """{chapter number text: title} of the list's main episodes (the first titled one of a number), as chapters take it.

    A leading season tag and a label of the chapter's own number go ("(S2) Ep. 63 - X" on 63 -> "X"); a label of
    another number stays part of the title ("Episode 7 Begins" on 2); a title that is only a label gives none (the
    number is left out); the rest is cleaned as file titles are (names.clean_marker_title).
    """
    if platform is None:
        return {}
    seen: set[str] = set()
    out: dict[str, str] = {}
    for e in platform["episodes"]:
        if e["kind"] == episodes.MAIN and (chapter := e["chapter"]) is not None and e["title"] and chapter not in seen:
            seen.add(chapter)
            if title := entry_title(e["title"], chapter):
                out[chapter] = title
    return out


def entry_title(stored: str, chapter: str) -> str:
    """Return a list entry's title as a chapter takes it ("" for none): a platform list's and MangaDex's entries."""
    rest = names.SEASON_TAG.sub("", stored, count=1)
    if (label := episodes.LABEL.match(rest)) is not None:
        if not rest[label.end() :].strip():
            return ""
        if float(label.group(1) or label.group(2)) == float(chapter):
            stored = rest[label.end() :]
    return names.clean_marker_title(stored)


def side_titles(platform: PlatformList | None, side_count: int) -> dict[int, str]:
    """Return {side-story number: title} from the list's specials in site order.

    Only when the list has exactly as many specials as the series has side stories (published and open).
    """
    if platform is None:
        return {}
    specials = [e for e in platform["episodes"] if e["kind"] == episodes.SPECIAL]
    if not specials or len(specials) != side_count:
        return {}
    return {k: title for k, e in enumerate(specials, 1) if (title := e["title"])}
