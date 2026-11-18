"""The metadata sources' records (SPEC "Source ids", "Metadata checks"): the fields the program reads.

MangaBaka, MangaUpdates, MangaDex and AniList records, fresh or stored as fetched (local library
manga-metadata/), and a platform list's episode. Type-only; the program reads each reply unchecked.
"""

from typing import NotRequired, TypedDict


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
    """A MangaBaka series record (GET /series/{id}; /source/{site}/{id} and /series/search list them).

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
    """A MangaDex manga record (GET /manga/{id} or /manga, cover art included): its UUID, attributes, relationships."""

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
    """An AniList manga's Media: the fields the program's Media query asks for, nullable as AniList's schema says."""

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
    """A media of AniList's title search: the fields the program's search query asks for."""

    id: int
    format: str | None
    title: AniListTitle | None
    synonyms: list[str] | None


# An episode of a platform's list: (site number, title, date as ISO text, the platform's id of it).
type Episode = tuple[int, str | None, str | None, int | None]
