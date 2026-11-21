"""The metadata sources' records (SPEC "Source ids", "Metadata checks") and a platform list's episode.

MangaBaka, MangaUpdates and MangaDex records come from REST APIs: a field a record may lack is NotRequired, one it may
send as null X | None. AniList records come from GraphQL: a reply carries every field its query asks for, null where
AniList's schema allows (local library manga-metadata/). Each type holds a subset of its record's fields. Types only.
"""

from typing import NotRequired, TypedDict


class MangaBakaLink(TypedDict):
    """A site MangaBaka links a series to: its id there (an int, or MangaUpdates' base36 text), null when unknown."""

    id: NotRequired[int | str | None]


class MangaBakaLinks(TypedDict):
    """A MangaBaka series' `source`: the AniList, MangaUpdates and MyAnimeList links (the cross-link hub)."""

    anilist: NotRequired[MangaBakaLink | None]
    manga_updates: NotRequired[MangaBakaLink | None]
    my_anime_list: NotRequired[MangaBakaLink | None]


class MangaBakaTitle(TypedDict):
    """A secondary title of a MangaBaka series."""

    title: NotRequired[str | None]


class MangaBakaTag(TypedDict):
    """A weighted tag of a MangaBaka series: its name and weight (core, defining, recurrent, incidental, unweighted).

    is_genre marks a tag that is a genre; implied_by_tag_ids lists the tags that imply it (MangaBaka adds it for them).
    """

    name: str
    weight: NotRequired[str | None]
    is_genre: NotRequired[bool | None]
    implied_by_tag_ids: NotRequired[list[int] | None]


class MangaBakaSeries(TypedDict):
    """A MangaBaka series record (GET /series/{id}; /source/{site}/{id} and /series/search list them).

    state is active, merged or deleted; type manga, manhwa, manhua, novel, oel or other; genres are snake_case slugs;
    secondary_titles {language: [title]}; description is Markdown or HTML.
    """

    id: int
    state: NotRequired[str]
    type: NotRequired[str | None]
    status: NotRequired[str | None]
    content_rating: NotRequired[str | None]
    title: NotRequired[str | None]
    romanized_title: NotRequired[str | None]
    native_title: NotRequired[str | None]
    secondary_titles: NotRequired[dict[str, list[MangaBakaTitle] | None] | None]
    description: NotRequired[str | None]
    canonical_url: NotRequired[str | None]
    genres: NotRequired[list[str] | None]
    tags_v2: NotRequired[list[MangaBakaTag] | None]
    source: NotRequired[MangaBakaLinks | None]


class MangaUpdatesGenre(TypedDict):
    """A genre of a MangaUpdates series."""

    genre: str


class MangaUpdatesCategory(TypedDict):
    """A voted category of a MangaUpdates series."""

    category: str


class MangaUpdatesTitle(TypedDict):
    """An associated name of a MangaUpdates series."""

    title: NotRequired[str | None]


class MangaUpdatesSeries(TypedDict):
    """A MangaUpdates series record: its type (Manga, Novel, ...), names, HTML description, genres and categories."""

    type: NotRequired[str | None]
    title: NotRequired[str | None]
    associated: NotRequired[list[MangaUpdatesTitle] | None]
    description: NotRequired[str | None]
    genres: NotRequired[list[MangaUpdatesGenre] | None]
    categories: NotRequired[list[MangaUpdatesCategory] | None]


class MangaUpdatesHit(TypedDict):
    """The record of a MangaUpdates search result: the series' id, title and type."""

    series_id: int
    title: NotRequired[str | None]
    type: NotRequired[str | None]


class MangaUpdatesResult(TypedDict):
    """A MangaUpdates search result: its record and the title the search matched."""

    record: MangaUpdatesHit
    hit_title: NotRequired[str | None]


class MangaDexLinks(TypedDict):
    """A MangaDex manga's AniList, MangaUpdates (base36 or numeric) and MyAnimeList links.

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
    """A MangaDex manga's attributes: its links, original language, description per language and tags."""

    links: NotRequired[MangaDexLinks | None]
    originalLanguage: NotRequired[str | None]
    description: NotRequired[dict[str, str] | None]
    tags: NotRequired[list[MangaDexTag] | None]


class MangaDexCoverArt(TypedDict):
    """The attributes of an included cover_art relationship: the cover's file name."""

    fileName: str


class MangaDexRelationship(TypedDict):
    """A relationship of a MangaDex manga; attributes only for one included (cover_art)."""

    type: NotRequired[str]
    attributes: NotRequired[MangaDexCoverArt]


class MangaDexManga(TypedDict):
    """A MangaDex manga record (GET /manga/{id} or /manga, cover art included): its UUID, attributes, relationships."""

    id: str
    attributes: NotRequired[MangaDexAttributes | None]
    relationships: NotRequired[list[MangaDexRelationship] | None]


class MangaDexAggregateChapter(TypedDict):
    """A chapter of a MangaDex aggregate volume: its number as text."""

    chapter: NotRequired[str | None]


class MangaDexAggregateVolume(TypedDict):
    """A volume of a MangaDex aggregate: its number as text ("none": no volume) and its chapters, keyed or listed."""

    volume: NotRequired[str | None]
    chapters: NotRequired[dict[str, MangaDexAggregateChapter] | list[MangaDexAggregateChapter] | None]


class MangaDexAggregate(TypedDict):
    """A MangaDex manga's aggregate: its volumes, a mapping keyed by number or a list ([] when empty)."""

    volumes: NotRequired[dict[str, MangaDexAggregateVolume] | list[MangaDexAggregateVolume] | None]


class MangaDexCover(TypedDict):
    """A MangaDex cover's attributes: its volume as text (null for none), locale and file name."""

    volume: NotRequired[str | None]
    locale: NotRequired[str | None]
    fileName: str


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
    """An AniList manga's Media: ids, format, status, titles, description, genres, tags, cover, dates, staff, links."""

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
    """A media of AniList's title search: its id, format, titles and synonyms."""

    id: int
    format: str | None
    title: AniListTitle | None
    synonyms: list[str] | None


# An episode of a platform's list: (site number, title, date as ISO text, the platform's id of it).
type Episode = tuple[int, str | None, str | None, int | None]
