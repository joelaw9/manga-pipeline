"""Kavita's replies (SPEC "Services"): the fields the program reads, as Kavita v0.9.1.4 sends them.

Type-only (local library kavita.md); the program reads each reply unchecked.
"""

from typing import TypedDict


class Library(TypedDict):
    """A library of GET /api/Library/libraries: its id, name and folders (Kavita paths)."""

    id: int
    name: str
    folders: list[str]


class Series(TypedDict):
    """A series DTO (GET /api/Series/{id}, POST /api/Series/all-v2): the fields the program reads.

    The names Kavita matches files on (the localized and original names can be null), its library, its cover image's
    file name (null when it has none), the provider ids (0 when unset), and the other fields POST /api/Series/update
    reads, which the program resends as they came, unread (typed object); Kavita v0.9.1.4 sends each of them.
    """

    id: int
    name: str
    localizedName: str | None
    originalName: str | None
    libraryId: int
    coverImage: str | None
    aniListId: int
    malId: int
    mangaBakaId: int
    nameLocked: object
    sortName: object
    sortNameLocked: object
    localizedNameLocked: object
    coverImageLocked: object
    cbrId: object
    comicVineId: object
    hardcoverId: object
    metronId: object
    metadataProviderOverride: object


class Tag(TypedDict):
    """An entry of the metadata's genres or tags: Kavita's id (0 for one to add) and its title."""

    id: int
    title: str


class Person(TypedDict):
    """An entry of the metadata's writers or pencillers: Kavita's id (0 for one to add) and the name."""

    id: int
    name: str


class SeriesMetadata(TypedDict):
    """A series' metadata (GET /api/Series/metadata): the fields the program reads, each with its lock.

    Changed in place and posted back whole (POST /api/Series/metadata), so it carries every field Kavita sent,
    seriesId (the series it updates) among them. webLinks is a comma-separated list of URLs.
    """

    seriesId: int
    summary: str
    summaryLocked: bool
    publicationStatus: int
    publicationStatusLocked: bool
    ageRating: int
    ageRatingLocked: bool
    language: str
    languageLocked: bool
    genres: list[Tag]
    genresLocked: bool
    tags: list[Tag]
    tagsLocked: bool
    writers: list[Person]
    writerLocked: bool
    pencillers: list[Person]
    pencillerLocked: bool
    webLinks: str


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
