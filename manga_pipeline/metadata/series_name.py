"""A series' name and romanized name, the free-name test, what a rename must do (SPEC "Retroactive updates").

Pure: callers read the records, Kavita's names and the folders. The rule is the old metadata tool's (kavita_enrich):
a person's name first (the override file's `name`, then `series new --name`); else AniList's English title less
" @COMIC"; else the series' own Kavita name, then its localized name, that MangaDex's English alternative titles, else
MangaBaka's English titles, else MangaDex's main English title list, holds, that no romanized title is and that is not
AniList's romaji; else the ComicInfo name as it is. Names are compared under Kavita's normalising.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from manga_pipeline import names
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.metadata.records import SeriesRecords
from manga_pipeline.replies.sources import AniListMedia

ANILIST_SUFFIX = " @COMIC"  # AniList's mark on a title also used by an anime (KE:1257, either case)
ROMANIZED = ("ja-ro", "ko-ro", "zh-ro")  # MangaDex's romanized title languages


@dataclass(frozen=True)
class Own:
    """A series' own names: Kavita's name, localized and original name (as last synced) and its ComicInfo name."""

    name: str
    localized: str | None
    original: str | None
    comicinfo: str


@dataclass(frozen=True)
class Chosen:
    """The rule's name and romanized name ("" when none), and where the name came from."""

    name: str
    romaji: str
    source: str


@dataclass(frozen=True)
class Due:
    """What a rename must change: the files' Series, Kavita's name, Kavita's localized name, the folder."""

    files: bool
    kavita_name: bool
    localized: bool
    folder: bool

    def any(self) -> bool:
        """Return whether anything is due."""
        return self.files or self.kavita_name or self.localized or self.folder


def _less_suffix(title: str | None) -> str:
    title = (title or "").strip()
    if title.casefold().endswith(ANILIST_SUFFIX.casefold()):
        title = title[: -len(ANILIST_SUFFIX)].strip()
    return title


def anilist_english(media: AniListMedia | None) -> str:
    """Return an AniList record's English title less the suffix, "" when it has none."""
    return _less_suffix(((media or {}).get("title") or {}).get("english"))


def anilist_romaji(media: AniListMedia | None) -> str:
    """Return an AniList record's romaji title less the suffix, "" when it has none."""
    return _less_suffix(((media or {}).get("title") or {}).get("romaji"))


def _romanized_keys(records: SeriesRecords) -> set[str]:
    md = (records["mangadex"] or {}).get("attributes") or {}
    titles = [md.get("title") or {}, *(md.get("altTitles") or [])]
    found = [t[lang] for t in titles for lang in ROMANIZED if t.get(lang)]
    mb = records["mangabaka"] or {}
    secondary = mb.get("secondary_titles") or {}
    found += [t.get("title") or "" for lang, ts in secondary.items() if lang.endswith("-ro") for t in ts or []]
    found.append(mb.get("romanized_title") or "")
    return {normalize(t) for t in found} - {""}


def _english_lists(records: SeriesRecords) -> list[tuple[str, list[str]]]:
    md = (records["mangadex"] or {}).get("attributes") or {}
    secondary = (records["mangabaka"] or {}).get("secondary_titles") or {}
    return [
        ("mangadex-alt-en", [t["en"] for t in md.get("altTitles") or [] if t.get("en")]),
        ("mangabaka-en", [t.get("title") or "" for t in secondary.get("en") or []]),
        ("mangadex-title-en", [(md.get("title") or {}).get("en") or ""]),
    ]


def chosen(own: Own, records: SeriesRecords, person: str | None) -> Chosen:
    """Return the series' name and romanized name by the rule; person: the override file's or admission's name."""
    romanized = _romanized_keys(records)
    al_romaji = anilist_romaji(records["anilist"])
    if person:
        name, source = person, "person"
    elif english := anilist_english(records["anilist"]):
        name, source = english, "anilist"
    else:
        name, source = own.comicinfo, "comicinfo"
        excluded = romanized | {normalize(al_romaji)}
        for list_source, titles in _english_lists(records):
            keys = {normalize(t) for t in titles} - {""} - excluded
            found = next((n for n in (own.name, own.localized) if n and normalize(n) in keys), None)
            if found:
                name, source = found, list_source
                break
    if normalize(name) == normalize(own.comicinfo):
        name = own.comicinfo  # another spelling of the same title is no change (KE:1281-1284)
    romaji = al_romaji or next(
        (n for n in (own.name, own.localized, own.original) if n and normalize(n) in romanized), ""
    )
    if normalize(romaji) == normalize(name):
        romaji = ""
    return Chosen(name, romaji, source)


def taken(name: str, others: Mapping[str, str]) -> str | None:
    """Return why the name is not free, or None. others: {normalised name: who holds it} of every other series."""
    key = normalize(name)
    if not key:
        return "its name has no letter or digit Kavita keeps"
    return others.get(key)


def folder_taken(name: str, folders: Iterable[str]) -> str | None:
    """Return the folder (of folders, those in the parent but the series' own) holding the name, or None.

    A folder holds it when its name is the name made safe ignoring case, or equal under Kavita's normalising.
    """
    safe, key = names.folder_name(name), normalize(name)
    return next((f for f in folders if f.casefold() == safe.casefold() or normalize(f) == key), None)


def due(own: Own, kavita_localized: str | None, folder: str, chosen_: Chosen) -> Due:
    """Return what a rename must change. folder: the series' stored folder; kavita_localized: Kavita's now."""
    key = normalize(chosen_.name)
    return Due(
        files=normalize(own.comicinfo) != key,
        kavita_name=normalize(own.name) != key,
        localized=bool(chosen_.romaji) and normalize(kavita_localized or "") != normalize(chosen_.romaji),
        folder=PurePosixPath(folder).name != names.folder_name(chosen_.name),
    )
