"""A new chapter's name next to its series' published chapters (SPEC "Publishing"): volume, number, title, kind and
whether it opens a covered volume. The bulk converter's settled path, ported: its optimizer/naming.py `_main_volumes`
with a recorded basis (`_basis_volumes`, `_fills`, `_gap_fill`), `_side_stories`, `season_ends` / `season_volumes`,
`_cover_confirmed`; covers.py `volume_cover_targets`; sources/episodes.py's season-marker reading; sources/mangadex.py
`volume_map`. Pure: the caller loads the rows, the override mapping and the stored MangaDex aggregate.

Season markers are read from free-form chapter titles by the bulk converter's patterns, kept as they were so a new
chapter gets the volume the converter would give it (flat text heuristics, not a structured format).
"""

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
FILL_PRIMARIES = (FILENAME, MANGADEX)  # the bases whose gaps a later source fills (seasons are not tankobon volumes)
SIDE_TITLE = "Side Story {n}"
MANGADEX_SHARE = 0.5  # choose: half volumed, half loose reads worse than no volumes

VolumeError = volumes.VolumeError  # a volume that cannot be written: a person must act


@dataclass(frozen=True)
class Row:
    """One chapter of the series. settled: a published file or a chapter whose name is already recorded (its kind,
    number, volume, volume_source and title are fixed); open: a chapter to name (volume and volume_source None)."""

    key: object
    settled: bool
    kind: str
    number: float
    season: int | None
    src_volume: str | None
    volume: int | None
    volume_source: str | None
    marker_title: str
    title: str
    date: str | None
    order: int


@dataclass(frozen=True)
class Naming:
    kind: str
    volume: int | None
    number: float
    title: str  # "" = untitled
    volume_source: str
    opens_covered_volume: bool


def resolve(basis, rows, overrides, mangadex, platform=None):
    """{row key: Naming} for the open rows of 1 series. basis: the record's series.naming.basis (None: no volume);
    overrides: the override file's mapping, or None when the series has none; mangadex: mangadex_volumes' map;
    platform: the stored platform list (metadata/platforms.py), or None.
    VolumeError for an `override` basis without an override file, and for an open chapter whose volume is not a
    whole number (ComicInfo Volume is an xs:int)."""
    if basis == OVERRIDE and overrides is None:
        raise VolumeError("the series' volume basis is its override file, and it has none")
    override = (overrides or {}).get("volumes") or {}
    main = sorted(
        (i for i, r in enumerate(rows) if r.kind == names.MAIN), key=lambda i: (rows[i].number, rows[i].order)
    )
    side = [i for i, r in enumerate(rows) if r.kind == names.SIDE]
    past = _past_finale(basis, rows, main, platform)
    main, side = [i for i in main if i not in past], side + sorted(past, key=lambda i: (rows[i].number, rows[i].order))
    vol = {i: (rows[i].volume, rows[i].volume_source) for i in main if rows[i].settled}
    vol.update(_open_main_volumes(basis, rows, main, override, mangadex, platform))
    for i in main:
        v = vol[i][0]
        if not rows[i].settled and v is not None and v != int(v):
            number, volume = names.fmt_num(rows[i].number), names.fmt_num(v)
            raise VolumeError(f"chapter {number}: volume {volume} ({vol[i][1]}) is not a whole number")
    out = {}
    covered = _opens_covered(basis, rows, main, vol, override, mangadex)
    titles = platforms.chapter_titles(platform)
    for i in main:
        if not rows[i].settled:
            v, source = vol[i]
            title = rows[i].title or names.clean_marker_title(titles.get(names.fmt_num(rows[i].number)) or "")
            out[rows[i].key] = Naming(names.MAIN, v, rows[i].number, title, source, i in covered)
    out.update(_side_stories(rows, side, [vol[i][0] for i in main], platform, past))
    return out


def _past_finale(basis, rows, main, platform):
    """Open main rows of a platform-basis series that its stored list puts past the main-story finale: side stories,
    as the bulk converter named them (SPEC "Publishing")."""
    if basis not in platforms.BASES or not platform or platform["name"] != platforms.BASES[basis]:
        return set()
    return platforms.volumes(platform, {i: rows[i].number for i in main if not rows[i].settled})[1]


def choose(rows, overrides, mangadex, platform=None):
    """A new series' volume basis (SPEC "Publishing"), chosen once over its chapters known then: the bulk converter's
    order for a series it meets first (`_main_volumes` with no record). Platform seasons (the stored platform list's
    season ranges) come after season markers. MangaDex is the basis only when it volumes half or more of the main
    chapters."""
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
    """{chapter number: volume or None} from a stored MangaDex aggregate payload (all languages): numbers normalised
    ("01" -> "1", "17.50" -> "17.5"), "none" -> no volume, a real volume beats none; `volumes` / `chapters` may be
    dicts keyed by number or lists ([] when empty)."""
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


def _open_main_volumes(basis, rows, main, override, mangadex, platform):
    """{row index: (volume, volume source)} of the open main rows: the basis's volume, else a gap fill, else none."""
    free = [i for i in main if not rows[i].settled]
    found = _basis_volumes(basis, rows, main, free, override, mangadex, platform)
    have = {i: rows[i].volume for i in main if rows[i].settled and rows[i].volume is not None}
    agree = {i: v for i, v in have.items() if rows[i].volume_source == basis}
    fills = _fills(basis, rows, main, free, {**have, **found}, {**agree, **found}, override, mangadex)
    out = {}
    for i in free:
        if i in found:
            out[i] = (found[i], DEFAULT if basis == SIDE_DEFAULT else basis)
        else:
            out[i] = fills.get(i, (None, NONE))
    return out


def _basis_volumes(basis, rows, main, free, override, mangadex, platform):
    """{row index: volume} of the free rows from the series' recorded basis only. A platform basis takes its stored
    list's season ranges (a list of another platform gives none; a chapter past the main-story finale is a side story,
    _past_finale). `none` and no basis give none."""
    if basis == FILENAME:
        vols = {i: names.to_volume(rows[i].src_volume) for i in free}
    elif basis == SEASON_TAGS:
        vols = {i: rows[i].season for i in free}
    elif basis == SEASON_MARKERS:
        vols = {i: v for i, v in season_volumes(rows, main).items() if not rows[i].settled}
    elif basis == MANGADEX:
        vols = _mangadex_volumes(mangadex, rows, free)
    elif basis == OVERRIDE:
        vols = _override_volumes(override, rows, free)
    elif basis == SIDE_DEFAULT:
        vols = {i: 1 for i in free}
    elif basis in platforms.BASES and platform and platform["name"] == platforms.BASES[basis]:
        vols = platforms.volumes(platform, {i: rows[i].number for i in free})[0]
    else:
        vols = {}
    return {i: v for i, v in vols.items() if v is not None}


def _mangadex_volumes(mangadex, rows, ids):
    vols = {i: names.to_volume(mangadex.get(names.fmt_num(rows[i].number))) for i in ids}
    return {i: v for i, v in vols.items() if v is not None}


def _override_volumes(override, rows, ids):
    vols = {i: volumes.override_volume(override, rows[i].number) for i in ids}
    return {i: v for i, v in vols.items() if v is not None}


def _fills(basis, rows, main, free, known, primary, override, mangadex):
    """Gap fill for a filename or MangaDex basis: the override first, then (filename only) MangaDex. A `none` basis
    takes the override only (user 2026-11-15: an override file written after a series' bulk run re-volumed its
    chapters, and its new chapters follow it; the bulk converter's own append gave them none)."""
    if basis not in (*FILL_PRIMARIES, NONE):
        return {}
    fillers = [(OVERRIDE, _override_volumes(override, rows, main))]
    if basis == FILENAME and mangadex:
        fillers.append((MANGADEX, _mangadex_volumes(mangadex, rows, main)))
    return _gap_fill(main, known, primary, set(free), fillers)


def _gap_fill(main, known, primary, targets, fillers):
    """{row index: (volume, filler)}. `known`: every volume given so far; `primary`: the part the basis gave (settled
    rows of the basis and the open rows' basis volumes); `targets`: the rows that may take a volume. A filler that
    gives a different volume than `primary` on any chapter both have fills nothing; a fill outside the volumes known
    around it (volumes never decrease along the numbers) is not applied; a filler whose fills are out of order among
    themselves is not used."""
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
        if any(a > b for a, b in zip(seq, seq[1:], strict=False)):
            continue
        known.update(added)
        fills.update({i: (v, name) for i, v in added.items()})
    return fills


def _bounds(order, vols):
    """(before, after) along `order`: before[k] the largest volume of a row before row k, after[k + 1] the smallest
    of a row after it (None: none)."""
    before, top = [], None
    for i in order:
        before.append(top)
        v = vols.get(i)
        top = top if v is None else v if top is None else max(top, v)
    after = [None] * (len(order) + 1)
    for k in range(len(order) - 1, -1, -1):
        v = vols.get(order[k])
        after[k] = after[k + 1] if v is None else v if after[k + 1] is None else min(v, after[k + 1])
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
    """{season: last main chapter number} from known ends ({season: end}, the record's closed volumes), the main rows'
    season markers (a marker without a season number is not used) and season tags (a start marker; a season starts at
    its lowest-numbered chapter). A start marker for season N closes N-1 at the chapter before it, unless an end
    already did."""
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
    """{row index: volume} of the main rows from season markers: volumes 1, 2, 3... in season order (a season without
    a marker leaves no number out); after the last end, the next. The settled chapters' recorded season-marker volumes
    count as ends too (each volume but the highest ends at its highest chapter): the bulk converter read markers its
    record does not keep (a download's ComicInfo Title), so the record's volumes are where those ends survive. The
    highest recorded volume stays open until a marker closes it."""
    recorded = {}
    for i in main:
        r = rows[i]
        if r.settled and r.volume is not None and r.volume_source == SEASON_MARKERS:
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


def _opens_covered(basis, rows, main, vol, override, mangadex):
    """Open rows that are the first (lowest-numbered) main chapter of a covered volume no settled chapter has: a volume
    whose first chapter's volume came from MangaDex or the override, or is a filename volume confirmed by them."""
    confirmed = _cover_confirmed(basis, rows, main, vol, override, mangadex)
    first = {}
    for i in main:  # in number order
        v = vol[i][0]
        if v is not None and v not in first:
            first[v] = i
    settled = {r.volume for r in rows if r.settled and r.volume is not None}
    return {
        i
        for v, i in first.items()
        if not rows[i].settled and v not in settled and (vol[i][1] in (MANGADEX, OVERRIDE) or i in confirmed)
    }


def _cover_confirmed(basis, rows, main, vol, override, mangadex):
    """Filename-volumed main rows whose volume an agreeing, ordered override or MangaDex map gives too."""
    if basis != FILENAME:
        return set()
    fname = {i: vol[i][0] for i in main if vol[i][0] is not None and vol[i][1] == FILENAME}
    confirmed = set()
    for cand in (_override_volumes(override, rows, main), _mangadex_volumes(mangadex, rows, main)):
        if not cand or any(fname[i] != cand[i] for i in fname if i in cand) or _unordered(main, cand):
            continue
        confirmed |= {i for i, v in fname.items() if cand.get(i) == v}
    return confirmed


def _side_stories(rows, side, main_volumes, platform, past=frozenset()):
    """{row key: Naming} of the open side stories: the settled side stories' volume, else the highest main volume + 1,
    else 2; numbered after the highest settled side number, in (source volume, number, date, order) order; titled
    "Side Story <n>: <title>", or "Side Story <n>" when the side marker and a bare chapter label leave no title. An
    untitled one takes the platform special's title for n when the list has as many specials as the series has side
    stories, published and open (platforms.side_titles); a chapter past the main-story finale (past) takes its own
    main episode's title first, as the bulk converter named it."""
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
        out[rows[i].key] = Naming(names.SIDE, side_volume, float(n), f"{base}: {t}" if t else base, source, False)
    return out
