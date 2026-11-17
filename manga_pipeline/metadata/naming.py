"""A new chapter's name next to its series' published chapters (SPEC "Publishing").

Its volume, number, title, kind and whether it opens a covered volume. The bulk converter's settled path, ported: its
optimizer/naming.py `_main_volumes` with a recorded basis (`_Sources`, `_gap_fill`), `_side_stories`, `season_ends` /
`season_volumes`, `_cover_confirmed`; covers.py `volume_cover_targets`; sources/episodes.py's season-marker reading;
sources/mangadex.py `volume_map`. Pure: the caller loads the rows, the override mapping and the stored MangaDex
aggregate.

Season markers are read from free-form chapter titles by the bulk converter's patterns, kept as they were so a new
chapter gets the volume the converter would give it (flat text heuristics, not a structured format).
"""

import dataclasses
import itertools
from collections.abc import Hashable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from manga_pipeline import names, volumes
from manga_pipeline.metadata import episodes, platforms

FILENAME, SEASON_TAGS, OVERRIDE, SIDE_DEFAULT = (
    volumes.FILENAME,
    volumes.SEASON_TAGS,
    volumes.OVERRIDE,
    volumes.SIDE_DEFAULT,
)
SEASON_MARKERS, MANGADEX = "season markers", "mangadex"
# chapter volume sources beside the bases: none found, the side-story default, a side story's volume
NONE, DEFAULT, SIDE_SOURCE = "none", "default", "side"
EDIT = "edit"  # a hand edit's volume (SPEC "Retroactive updates", hand edits): wins over every source
FILL_PRIMARIES = (FILENAME, MANGADEX)  # the bases whose gaps a later source fills (seasons are not tankobon volumes)
SIDE_TITLE = "Side Story {n}"
MANGADEX_SHARE = 0.5  # choose: half volumed, half loose reads worse than no volumes

VolumeError = volumes.VolumeError  # a volume that cannot be written: a person must act
TOKEN_PARTS = 2  # a chapter token with a prefix: "S2 10", "side 3"


@dataclass(frozen=True)
class Row:
    """One chapter of the series.

    settled: a published file or a chapter whose name is already recorded (its kind, number, volume, volume_source and
    title are fixed); open: a chapter to name (volume and volume_source None).
    """

    key: Hashable  # the caller's name for the row, opaque here: the key of resolve's result
    settled: bool
    kind: str
    number: float
    season: int | None
    src_volume: str | None
    volume: float | None  # an int for a whole volume; a published file may hold one like 2.5
    volume_source: str | None
    marker_title: str
    title: str
    date: str | None
    order: int


@dataclass(frozen=True)
class Naming:
    """A chapter's name as resolved: kind, volume, number, title, volume source, whether it opens a covered volume."""

    kind: str
    volume: float | None  # an int for a whole volume; a published file may hold one like 2.5
    number: float
    title: str  # "" = untitled
    volume_source: str
    opens_covered_volume: bool


@dataclass(frozen=True)
class Edit:
    """A hand edit of 1 chapter (SPEC "Retroactive updates", hand edits): None = that field not edited.

    title: a main chapter's title, or a side story's text after "Side Story <n>: ".
    """

    title: str | None
    volume: float | None  # an int for a whole volume; a published file may hold one like 2.5


def edit_key(kind, season, number):
    """Return the key a chapter's hand edit is stored under: (kind, season, number).

    A side story by its own side number and no season.
    """
    return (kind, None if kind == names.SIDE else season, float(number))


def edit_token(kind, season, number):
    """Return a chapter as a person names it in an edit file: "10", "S2 10", "side 3" (parse_token reads it back)."""
    n = names.fmt_num(number)
    return f"side {n}" if kind == names.SIDE else f"S{season} {n}" if season is not None else n


def parse_token(text):
    """edit_key of an edit file's chapter: "10", "S2 10" (season 2), "side 3" (Side Story 3). ValueError otherwise."""
    parts = str(text).split()
    if len(parts) == TOKEN_PARTS and parts[0].lower() == "side":
        kind, season, number = names.SIDE, None, parts[1]
    elif len(parts) == TOKEN_PARTS and parts[0][:1] in "sS" and parts[0][1:].isdigit():
        kind, season, number = names.MAIN, int(parts[0][1:]), parts[1]
    elif len(parts) == 1:
        kind, season, number = names.MAIN, None, parts[0]
    else:
        msg = f"chapter {text!r} is not a number, 'S<season> <number>' or 'side <number>'"
        raise ValueError(msg)
    n = names.to_volume(number)  # a non-negative number, or None
    if n is None:
        msg = f"chapter {text!r}: {number!r} is not a chapter number"
        raise ValueError(msg)
    return edit_key(kind, season, n)


def edited_title(kind, number, title):
    """Return the title a chapter's edited title gives it: a side story keeps its "Side Story <n>: " label."""
    return f"{SIDE_TITLE.format(n=names.fmt_num(number))}: {title}" if kind == names.SIDE else title


def unedited(named, plain):
    """Return what a chapter's own hand edit changed in its naming, as {published_file column-like field: value}.

    The change in its naming (named) from what the sources alone give it (plain): {"title": …} and/or {"volume": …,
    "volume_source": …} (volume as ComicInfo text, an untitled one's title None); None when the edit changed nothing.
    """
    out = {}
    if named.title != plain.title:
        out["title"] = plain.title or None
    if named.volume_source == EDIT and named.volume != plain.volume:
        out["volume"], out["volume_source"] = names.fmt_num(plain.volume), plain.volume_source
    return out or None


def apply_edit(row, edits):
    """Return a settled row as its hand edit makes it.

    An edited field that differs from the row's replaces it (an edited volume with source EDIT).
    """
    e = edits.get(edit_key(row.kind, row.season, row.number))
    if e is None:
        return row
    if e.volume is not None and e.volume != row.volume:
        row = dataclasses.replace(row, volume=e.volume, volume_source=EDIT)
    if e.title is not None:
        row = dataclasses.replace(row, title=edited_title(row.kind, row.number, e.title))
    return row


def resolve(basis, rows, overrides, mangadex, platform=None, *, fill=frozenset(), edits=None):  # noqa: PLR0913  # 7 inputs the resolver itself uses
    """Return {row key: Naming} for the open rows of 1 series.

    basis: the record's series.naming.basis (None: no volume); overrides: the override file's mapping, or None when
    the series has none; mangadex: mangadex_volumes' map; platform: the stored platform list (metadata/platforms.py),
    or None; fill: keys of open rows that are published files holding no volume, named only to fill it (SPEC
    "Retroactive updates": their caller takes a whole volume of a main chapter or nothing, so they raise no
    VolumeError).
    edits: {edit_key: Edit} of the series, applied over every source: the settled rows as their edits make them, an
    open row's edited volume before everything its volume decides (the volumes around it, covers, side-story
    volumes), an edited title over its own.
    VolumeError for an `override` basis without an override file, and for an open chapter whose volume is not a
    whole number (ComicInfo Volume is an xs:int).
    """
    edits = edits or {}
    rows = [apply_edit(r, edits) if r.settled else r for r in rows]
    if basis == OVERRIDE and overrides is None:
        msg = "the series' volume basis is its override file, and it has none"
        raise VolumeError(msg)
    override = (overrides or {}).get("volumes") or {}
    main = sorted(
        (i for i, r in enumerate(rows) if r.kind == names.MAIN), key=lambda i: (rows[i].number, rows[i].order)
    )
    side = [i for i, r in enumerate(rows) if r.kind == names.SIDE]
    seasons = _basis_list(basis, platform)
    past = _past_finale(rows, main, seasons)
    main, side = [i for i in main if i not in past], side + sorted(past, key=lambda i: (rows[i].number, rows[i].order))
    # the rows' numbers are the same in every pass below, so the outside sources' volumes are read once
    sources = _Sources(_override_volumes(override, rows, main), _mangadex_volumes(mangadex, rows, main), seasons)
    vol = {i: (rows[i].volume, rows[i].volume_source) for i in main if rows[i].settled}
    # An open chapter's edited volume is fixed first, as a settled one's: the volumes around it are named from it.
    pinned = {
        i: e.volume
        for i in main
        if not rows[i].settled
        and (e := edits.get(edit_key(names.MAIN, rows[i].season, rows[i].number))) is not None
        and e.volume is not None
    }
    fixed = [
        dataclasses.replace(r, settled=True, volume=pinned[i], volume_source=EDIT) if i in pinned else r
        for i, r in enumerate(rows)
    ]
    vol.update(_open_main_volumes(basis, fixed, main, sources))
    plain = _open_main_volumes(basis, rows, main, sources) if pinned else {}
    # an edit that gives the sources' own volume keeps their source (and whether it is covered)
    vol.update({i: plain[i] if plain[i][0] == v else (v, EDIT) for i, v in pinned.items()})
    for i in main:
        v = vol[i][0]
        if not rows[i].settled and rows[i].key not in fill and v is not None and v != int(v):
            number, volume = names.fmt_num(rows[i].number), names.fmt_num(v)
            msg = f"chapter {number}: volume {volume} ({vol[i][1]}) is not a whole number"
            raise VolumeError(msg)
    out = {}
    covered = _opens_covered(rows, _covered_firsts(basis, main, vol, (sources.override, sources.mangadex)))
    titles = platforms.chapter_titles(platform)
    for i in main:
        if not rows[i].settled:
            v, source = vol[i]
            title = rows[i].title or names.clean_marker_title(titles.get(names.fmt_num(rows[i].number)) or "")
            e = edits.get(edit_key(names.MAIN, rows[i].season, rows[i].number))
            title = e.title if e is not None and e.title is not None else title
            out[rows[i].key] = Naming(names.MAIN, v, rows[i].number, title, source, i in covered)
    for key, side_named in _side_stories(rows, side, [vol[i][0] for i in main], platform, past).items():
        named = side_named
        e = edits.get(edit_key(names.SIDE, None, named.number))
        if e is not None and e.title is not None:
            named = dataclasses.replace(named, title=edited_title(names.SIDE, named.number, e.title))
        if e is not None and e.volume is not None and e.volume != named.volume:
            named = dataclasses.replace(named, volume=e.volume, volume_source=EDIT)
        out[key] = named
    return out


def _basis_list(basis, platform):
    """Return the stored platform list when the series' basis is its seasons, else None."""
    if basis not in platforms.BASES or not platform or platform["name"] != platforms.BASES[basis]:
        return None
    return platform


def _past_finale(rows, main, seasons):
    """Return the open main rows of a platform-basis series that its stored list puts past the main-story finale.

    seasons: _basis_list. They are side stories, as the bulk converter named them (SPEC "Publishing").
    """
    if seasons is None:
        return set()
    return platforms.volumes(seasons, {i: rows[i].number for i in main if not rows[i].settled})[1]


def choose(rows, overrides, mangadex, platform=None):  # noqa: PLR0911  # 1 priority chain
    """Choose a new series' volume basis (SPEC "Publishing"), once, over its chapters known then.

    The bulk converter's order for a series it meets first (`_main_volumes` with no record). Platform seasons (the
    stored platform list's season ranges) come after season markers. MangaDex is the basis only when it volumes half or
    more of the main chapters.
    """
    override = (overrides or {}).get("volumes") or {}
    main = [i for i, r in enumerate(rows) if r.kind == names.MAIN]
    if any(names.to_volume(rows[i].src_volume) is not None for i in main):
        return FILENAME
    if main and all(rows[i].season for i in main):  # every file carries its season: "S2 - Chapter 1"
        return SEASON_TAGS
    if season_volumes(rows, main):
        return SEASON_MARKERS
    if platform and any(platforms.volumes(platform, {i: rows[i].number for i in main})):
        return f"{platform['name']} seasons"
    if main and len(_mangadex_volumes(mangadex, rows, main)) / len(main) >= MANGADEX_SHARE:
        return MANGADEX
    if _override_volumes(override, rows, main):
        return OVERRIDE
    if any(r.kind == names.SIDE for r in rows):
        return SIDE_DEFAULT
    return NONE


def mangadex_volumes(aggregate):
    """Return {chapter number: volume or None} from a stored MangaDex aggregate payload (all languages).

    Numbers normalised ("01" -> "1", "17.50" -> "17.5"), "none" -> no volume, a real volume beats none; `volumes` /
    `chapters` may be dicts keyed by number or lists ([] when empty).
    """
    out = {}
    for volume in _items((aggregate or {}).get("volumes")):
        v = episodes.norm_number(volume.get("volume"))
        for chapter in _items(volume.get("chapters")):
            n = episodes.norm_number(chapter.get("chapter"))
            if n is not None and out.get(n) is None:
                out[n] = v
    return out


def _items(x):
    return list(x.values()) if isinstance(x, dict) else list(x or [])


# --- main volumes


@dataclass(frozen=True)
class _Sources:
    """A series' volume sources beside its chapters' own files.

    The override file's and MangaDex's volume of each main row (by row index, any season) and the stored platform list
    when the basis is its seasons (_basis_list). It answers which volumes the recorded basis gives and which sources
    fill its gaps.
    """

    override: dict
    mangadex: dict
    seasons: dict | None

    def basis_volumes(self, basis, rows, main, free):
        """Return {row index: volume} of the free rows from the series' recorded basis only.

        A platform basis takes its stored list's season ranges (a list of another platform gives none; a chapter past
        the main-story finale is a side story, _past_finale). `none` and no basis give none.
        """
        if basis == FILENAME:
            vols = {i: names.to_volume(rows[i].src_volume) for i in free}
        elif basis == SEASON_TAGS:
            vols = {i: rows[i].season for i in free}
        elif basis == SEASON_MARKERS:
            vols = {i: v for i, v in season_volumes(rows, main).items() if not rows[i].settled}
        elif basis == MANGADEX:
            vols = {i: self.mangadex.get(i) for i in free}
        elif basis == OVERRIDE:
            vols = {i: self.override.get(i) for i in free}
        elif basis == SIDE_DEFAULT:
            vols = dict.fromkeys(free, 1)
        elif self.seasons is not None:
            vols = platforms.volumes(self.seasons, {i: rows[i].number for i in free})[0]
        else:
            vols = {}
        return {i: v for i, v in vols.items() if v is not None}

    def fillers(self, basis):
        """Return [(source, {row index: volume})] that fill a basis's gaps, in order.

        For a filename or MangaDex basis the override first, then (filename only) MangaDex. A `none` basis takes the
        override only (user 2026-11-15: an override file written after a series' bulk run re-volumed its chapters, and
        its new chapters follow it; the bulk converter's own append gave them none).
        """
        if basis not in (*FILL_PRIMARIES, NONE):
            return []
        fillers = [(OVERRIDE, self.override)]
        if basis == FILENAME:
            fillers.append((MANGADEX, self.mangadex))
        return fillers


def _open_main_volumes(basis, rows, main, sources):
    """Return {row index: (volume, volume source)} of the open main rows.

    The override range's volume (whatever the basis; only for a chapter that carries no season: a season's numbers
    restart), else the basis's volume, else a gap fill, else none.
    """
    free = [i for i in main if not rows[i].settled]
    ranged = _seasonless(rows, {i: sources.override[i] for i in free if i in sources.override})
    found = sources.basis_volumes(basis, rows, main, [i for i in free if i not in ranged])
    have = {i: rows[i].volume for i in main if rows[i].settled and rows[i].volume is not None}
    agree = {i: v for i, v in have.items() if rows[i].volume_source == basis}
    known = {**have, **ranged, **found}
    fills = _gap_fill(main, known, {**agree, **found}, set(free), sources.fillers(basis))
    out = {}
    for i in free:
        if i in ranged:
            out[i] = (ranged[i], OVERRIDE)
        elif i in found:
            out[i] = (found[i], DEFAULT if basis == SIDE_DEFAULT else basis)
        else:
            out[i] = fills.get(i, (None, NONE))
    return out


def override_volumes(override, rows, ids):
    """Return {row index: volume} of the rows among ids whose number lies in an override range.

    Only rows that carry no season, and whose number lies in a range of the override file's `volumes` mapping (the 1
    reader of ranges: volumes.override_volume).
    """
    return _seasonless(rows, _override_volumes(override, rows, ids))


def _seasonless(rows, vols):
    """Return the volumes of vols whose row carries no season.

    An override range never reaches a season's chapters (their numbers restart).
    """
    return {i: v for i, v in vols.items() if rows[i].season is None}


def _mangadex_volumes(mangadex, rows, ids):
    vols = {i: names.to_volume(mangadex.get(names.fmt_num(rows[i].number))) for i in ids}
    return {i: v for i, v in vols.items() if v is not None}


def _override_volumes(override, rows, ids):
    vols = {i: volumes.override_volume(override, rows[i].number) for i in ids}
    return {i: v for i, v in vols.items() if v is not None}


def _gap_fill(main, known, primary, targets, fillers):
    """Return {row index: (volume, filler)}: the gap fills of the target rows.

    `known`: every volume given so far; `primary`: the part the basis gave (settled rows of the basis and the open rows'
    basis volumes); `targets`: the rows that may take a volume. A filler that gives a different volume than `primary` on
    any chapter both have fills nothing; a fill outside the volumes known around it (volumes never decrease along the
    numbers) is not applied; a filler whose fills are out of order among themselves is not used.
    """
    known, fills = dict(known), {}
    for name, cand in fillers:
        want = {i for i in main if i in targets and i not in known and i in cand}
        if not want:
            continue
        if any(primary[i] != cand[i] for i in main if i in primary and i in cand):
            continue
        before, after = _bounds(main, known)
        added = {}
        for k, i in enumerate(main):
            if i in want:
                w = cand[i]
                if not ((before[k] is not None and w < before[k]) or (after[k + 1] is not None and w > after[k + 1])):
                    added[i] = w
        seq = [added[i] for i in main if i in added]
        if any(a > b for a, b in itertools.pairwise(seq)):
            continue
        known.update(added)
        fills.update({i: (v, name) for i, v in added.items()})
    return fills


def _bounds(order, vols):
    """Return (before, after) along `order`.

    before[k] the largest volume of a row before row k, after[k + 1] the smallest of a row after it (None: none).
    """
    before, top = [], None
    for i in order:
        before.append(top)
        v = vols.get(i)
        top = top if v is None else v if top is None else max(top, v)
    after: list[float | None] = [None] * (len(order) + 1)
    for k in range(len(order) - 1, -1, -1):
        v = vols.get(order[k])
        later = after[k + 1]
        after[k] = later if v is None else v if later is None else min(v, later)
    return before, after


def _unordered(main, vols):
    """Rows whose volume is lower than an earlier chapter's among `vols`."""
    out, top = [], None
    for i in main:
        v = vols.get(i)
        if v is None:
            continue
        if top is not None and v < top:
            out.append(i)
        top = v if top is None else max(top, v)
    return out


# --- season markers (metadata/episodes.py's reading)


def season_ends(rows, main, known=None):
    """Return {season: last main chapter number} from known ends, the main rows' season markers and season tags.

    known: {season: end}, the record's closed volumes. A marker without a season number is not used; a season tag is a
    start marker (a season starts at its lowest-numbered chapter). A start marker for season N closes N-1 at the
    chapter before it, unless an end already did.
    """
    ends, starts = dict(known or {}), {}
    for i in main:
        r = rows[i]
        marks = [(kind, season) for kind, season in episodes.read_markers(r.marker_title) if season is not None]
        marks += [("start", r.season)] if r.season else []
        for kind, season in marks:
            if kind == "end":
                ends[season] = max(ends.get(season, 0), r.number)
            else:
                starts[season] = min(starts.get(season, r.number), r.number)
    befores = [
        (season, max((rows[i].number for i in main if rows[i].number < first), default=None))
        for season, first in sorted(starts.items())
    ]
    return episodes.close_seasons(ends, befores)


def season_volumes(rows, main):
    """Return {row index: volume} of the main rows from season markers.

    Volumes 1, 2, 3... in season order (a season without a marker leaves no number out); after the last end, the next.
    The settled chapters' recorded season-marker volumes count as ends too (each volume but the highest ends at its
    highest chapter): the bulk converter read markers its record does not keep (a download's ComicInfo Title), so the
    record's volumes are where those ends survive. The highest recorded volume stays open until a marker closes it.
    """
    recorded = {}
    for i in main:
        r = rows[i]
        if r.settled and r.volume is not None and r.volume_source in (SEASON_MARKERS, EDIT):  # an edit ends one too
            recorded[r.volume] = max(recorded.get(r.volume, r.number), r.number)
    top = max(recorded, default=None)
    ends = set(season_ends(rows, main, {v: end for v, end in recorded.items() if v != top}).values())
    bounds = []
    for end in sorted(ends):
        if not bounds or end > bounds[-1]:
            bounds.append(end)
    if not bounds:
        return {}
    return {i: next((v for v, end in enumerate(bounds, 1) if rows[i].number <= end), len(bounds) + 1) for i in main}


# --- covered volumes and side stories


def _opens_covered(rows, firsts):
    """Return the open rows that are the first (lowest-numbered) main chapter of a covered volume no settled row has.

    firsts: _covered_firsts' result.
    """
    settled = {r.volume for r in rows if r.settled and r.volume is not None}
    return {i for v, i in firsts.items() if not rows[i].settled and v not in settled}


def covered_volumes(basis, rows, override, mangadex):
    """Return the covered volumes (SPEC "Volume covers") of rows as they stand.

    From each main row's volume and volume source (the retroactive comparison passes the published rows with the
    volumes they will hold).
    """
    main = sorted(
        (i for i, r in enumerate(rows) if r.kind == names.MAIN), key=lambda i: (rows[i].number, rows[i].order)
    )
    vol = {i: (rows[i].volume, rows[i].volume_source) for i in main}
    maps = (_override_volumes(override, rows, main), _mangadex_volumes(mangadex, rows, main))
    return set(_covered_firsts(basis, main, vol, maps))


def _covered_firsts(basis, main, vol, maps):
    """Return {volume: index of its first (lowest-numbered) main row} of the covered volumes.

    A covered volume: one whose first chapter's volume came from MangaDex, the override or a hand edit, or is a
    filename volume confirmed by them.
    """
    confirmed = _cover_confirmed(basis, main, vol, maps)
    first = {}
    for i in main:  # in number order
        v = vol[i][0]
        if v is not None and v not in first:
            first[v] = i
    return {v: i for v, i in first.items() if vol[i][1] in (MANGADEX, OVERRIDE, EDIT) or i in confirmed}


def _cover_confirmed(basis, main, vol, maps):
    """Return the filename-volumed main rows whose volume an agreeing, ordered map of maps gives too.

    maps: the override's, MangaDex's.
    """
    if basis != FILENAME:
        return set()
    fname = {i: vol[i][0] for i in main if vol[i][0] is not None and vol[i][1] == FILENAME}
    confirmed = set()
    for cand in maps:
        if not cand or any(fname[i] != cand[i] for i in fname if i in cand) or _unordered(main, cand):
            continue
        confirmed |= {i for i, v in fname.items() if cand.get(i) == v}
    return confirmed


def _side_stories(rows, side, main_volumes, platform, past: AbstractSet = frozenset()):
    """Return {row key: Naming} of the open side stories.

    Their volume: the settled side stories' volume, else the highest main volume + 1, else 2; numbered after the highest
    settled side number, in (source volume, number, date, order) order; titled "Side Story <n>: <title>", or "Side Story
    <n>" when the side marker and a bare chapter label leave no title. An untitled one takes the platform special's
    title for n when the list has as many specials as the series has side stories, published and open
    (platforms.side_titles); a chapter past the main-story finale (past) takes its own main episode's title first, as
    the bulk converter named it.
    """
    special_titles = platforms.side_titles(platform, len(side))
    episode_titles = platforms.chapter_titles(platform)
    done = sorted((i for i in side if rows[i].settled), key=lambda i: (rows[i].number, rows[i].order))
    present = [v for v in main_volumes if v is not None]
    side_volume = rows[done[0]].volume if done else int(max(present)) + 1 if present else 2
    start = int(max((rows[i].number for i in done), default=0))
    todo = sorted(
        (i for i in side if not rows[i].settled),
        key=lambda i: (names.to_volume(rows[i].src_volume) or 0, rows[i].number, rows[i].date or "", rows[i].order),
    )
    out = {}
    for n, i in enumerate(todo, start + 1):
        t = names.clean_marker_title(names.SIDE_MARKER.sub("", rows[i].title).strip())
        if not t and i in past:
            t = names.clean_marker_title(episode_titles.get(names.fmt_num(rows[i].number)) or "")
        t = t or names.clean_marker_title(special_titles.get(n) or "")
        base = SIDE_TITLE.format(n=n)
        source = SIDE_SOURCE if side_volume is not None else NONE
        out[rows[i].key] = Naming(
            names.SIDE, side_volume, float(n), f"{base}: {t}" if t else base, source, opens_covered_volume=False
        )
    return out
