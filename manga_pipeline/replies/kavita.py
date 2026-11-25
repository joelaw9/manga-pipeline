"""Kavita's replies (SPEC "Services"), as Kavita v0.9.1.4 sends them (local library kavita.md).

Each type holds a subset of its reply's fields. A field a reply may lack is NotRequired; one it may send as null is
X | None. Types only.
"""

from typing import NotRequired, TypedDict


class Library(TypedDict):
    """A library of GET /api/Library/libraries: its id, name and folders (Kavita paths)."""

    id: int
    name: str
    folders: list[str]


class Series(TypedDict):
    """A series DTO (GET /api/Series/{id}, POST /api/Series/all-v2).

    Its id, the names Kavita matches files on (the localized and original names can be null), its library, its cover
    image's file name (null when it has none), the provider ids (0 when unset), and the other fields of the update DTO
    (POST /api/Series/update) as opaque values (object).
    """

    id: int
    name: str
    localizedName: NotRequired[str | None]
    originalName: NotRequired[str | None]
    libraryId: int
    coverImage: NotRequired[str | None]
    aniListId: NotRequired[int]
    malId: NotRequired[int]
    mangaBakaId: NotRequired[int]
    nameLocked: NotRequired[object]
    sortName: NotRequired[object]
    sortNameLocked: NotRequired[object]
    localizedNameLocked: NotRequired[object]
    coverImageLocked: NotRequired[object]
    cbrId: NotRequired[object]
    comicVineId: NotRequired[object]
    hardcoverId: NotRequired[object]
    metronId: NotRequired[object]
    metadataProviderOverride: NotRequired[object]


class Tag(TypedDict):
    """An entry of the metadata's genres or tags: Kavita's id (0 for one to add) and its title."""

    id: int
    title: str


class Person(TypedDict):
    """An entry of the metadata's writers or pencillers: Kavita's id (0 for one to add) and the name."""

    id: int
    name: str


class SeriesMetadata(TypedDict):
    """A series' metadata (GET /api/Series/metadata; POST /api/Series/metadata takes it back whole).

    seriesId (the series it describes), the summary, publication status, age rating and language, the genre, tag,
    writer and penciller lists, each with its lock flag, and webLinks, a comma-separated list of URLs.
    """

    seriesId: int
    summary: NotRequired[str | None]
    summaryLocked: NotRequired[bool]
    publicationStatus: NotRequired[int | None]
    publicationStatusLocked: NotRequired[bool]
    ageRating: NotRequired[int | None]
    ageRatingLocked: NotRequired[bool]
    language: NotRequired[str | None]
    languageLocked: NotRequired[bool]
    genres: NotRequired[list[Tag] | None]
    genresLocked: NotRequired[bool]
    tags: NotRequired[list[Tag] | None]
    tagsLocked: NotRequired[bool]
    writers: NotRequired[list[Person] | None]
    writerLocked: NotRequired[bool]
    pencillers: NotRequired[list[Person] | None]
    pencillerLocked: NotRequired[bool]
    webLinks: NotRequired[str | None]


class KavitaFile(TypedDict):
    """A file of a chapter (GET /api/Series/volumes): its id, Kavita path, size in bytes and page count."""

    id: int
    filePath: str
    bytes: int
    pages: int


class Chapter(TypedDict):
    """A chapter of a volume: its id, the API key's user's pages read, when a scan last re-read it, and its files."""

    id: int
    pagesRead: int
    lastModifiedUtc: str
    files: list[KavitaFile]


class Volume(TypedDict):
    """A volume of GET /api/Series/volumes: its id and chapters (chapters with no volume sit in volume -100000)."""

    id: int
    chapters: list[Chapter]


class Collection(TypedDict):
    """A collection of the API key's user (GET /api/Collection): its id and title."""

    id: int
    title: str
