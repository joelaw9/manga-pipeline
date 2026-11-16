"""Episode lists of the official platforms (SPEC "Metadata", platform lists): posts classified, titles cleaned, season
markers read; the bulk converter's optimizer/sources/episodes.py, ported whole. Pure.

Neither Tapas nor Webtoons has a season or volume field: seasons come only from finale and premiere markers in episode
titles. An episode record, in site order:
  {"number": the site's own index (counts notices too), "chapter": chapter number (text) of a main or special post,
   None for a non-chapter post, "raw_title", "title" (cleaned; None when nothing is left), "kind" (main / special /
   non-chapter), "season_marker": {"type": "end" | "start", "season": int} | {"type": "main_end", "season": None} |
   None, "date", "id"}
The season-marker patterns are also the resolver's for chapter titles (metadata/naming.py `season_ends`). Season
markers and labels are read from free-form titles by the bulk converter's patterns, kept as they were so a chapter gets
the volume the converter would give it (flat text heuristics, not a structured format).
"""

import re

from manga_pipeline import names

MAIN, SPECIAL, NON_CHAPTER = "main", "special", "non-chapter"

# "Ep. 1 - Memento Mortem", "Episode 30 (Season Finale)", "Chapter 4: X", "1. The Grand Sorcerer"
LABEL = re.compile(
    r"^\s*(?:(?:Episode|Ep|Chapter|Ch)\.?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\.(?=\s))\s*(?:[-\u2013\u2014:.|]\s*)?",
    re.IGNORECASE,
)
# "SE 1. He's Just a Puppy", "SP 2 - X", "Special Episode 3: X", "Side Story 1 - X", "Extra: The First Day"
SPECIAL_LABEL = re.compile(
    r"^\s*(?:(?:SE|SP)(?=\s*\d)|(?:Special(?:\s+Episode)?|Side\s+Story|Extra|Bonus(?:\s+Episode)?)\b)\s*\.?\s*"
    r"(\d+(?:\.\d+)?)?\s*(?:[-\u2013\u2014:.|]\s*)?",
    re.IGNORECASE,
)
SPECIAL_WORDS = re.compile(
    r"^\s*(?:SE|SP)\s*\d|^\s*(?:special|side\s+story|extra|bonus|epilogue|omake)\b", re.IGNORECASE
)
NON_CHAPTER_WORDS = re.compile(
    r"\b(?:announcements?|notice|hiatus|author'?s\s+notes?|creator'?s?\s+(?:notes?|interview)|interview|contests?|"
    r"giveaway|Q\s*&\s*A|poll|recap|teaser|preview|trailer|coming\s+soon|now\s+available|available\s+now|"
    r"schedule|life\s+update|merch|BTS|wallpapers?|character\s+profiles?|fan\s*art|happy\s+birthday)\b",
    re.IGNORECASE,
)

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d+|" + "|".join(WORDS) + r")"
END_MARKERS = [  # first group: the season number (digits or a word), absent for a bare "(Season Finale)"
    re.compile(r"(?<!Mid-)(?<!Mid )\bSeason\s*" + _NUM + r"?\s*(?:-\s*)?(?:Finale|END)\b", re.IGNORECASE),
    re.compile(r"\bEnd\s+of\s+Season\s*" + _NUM + r"?\b", re.IGNORECASE),
    re.compile(r"\bS(\d+)\s*-?\s*(?:END|Finale)\b", re.IGNORECASE),
]
START_MARKERS = [  # "Season 3 is Coming Soon!" / "Season 2 Teaser" posts sit between seasons: they start the next one
    re.compile(r"\bSeason\s*" + _NUM + r"?\s*-?\s*(?:Premiere|Begins|START)\b", re.IGNORECASE),
    re.compile(r"\bS(\d+)\s*-?\s*START\b", re.IGNORECASE),
    re.compile(r"\[Season\s*(\d+)\]", re.IGNORECASE),
    re.compile(r"\bSeason\s*" + _NUM + r"\s*(?:Teaser|Preview|Trailer|(?:is\s+)?Coming)\b", re.IGNORECASE),
]
MAIN_END = re.compile(r"\bMain\s+Story\s+Finale\b|\bEnd\s+of\s+(?:the\s+)?Main\s+Story\b", re.IGNORECASE)
# bracketed markers removed from titles once read: "(Season Finale)", "(Season 2 Premiere)", "[S1 END]",
# "(End of Season 1)", "(Main Story Finale)", "(Series Finale)", "(Season 1 Prequel Finale)", "(End)"
BRACKETED_MARKER = re.compile(
    r"\s*[(\[{]\s*(?:[^()\[\]{}]*\b(?:Finale|Premiere)|(?:Mid-?\s*)?Season\s*\w*\s*(?:END|Begins|START)|"
    r"End\s+of\s+(?:the\s+)?(?:Season\s*\w*|Main\s+Story)|S\d+\s*-?\s*(?:END|START)|"
    r"Season\s*\d+|END)\s*[)\]}]",
    re.IGNORECASE,
)
BARE_MARKER = re.compile(
    r"^\s*(?:(?:Mid-?\s*)?Season\s*\w*\s*(?:Finale|END|Premiere|Begins|START)|End\s+of\s+Season\s*\w*|"
    r"S\d+\s*-?\s*(?:END|Finale|START))\s*[!.]*\s*$",
    re.IGNORECASE,
)


def read_markers(title):
    """[(kind "end" | "start", season or None)] of every season marker in a title, end markers first."""
    out = []
    for kind, patterns in (("end", END_MARKERS), ("start", START_MARKERS)):
        for p in patterns:
            for m in p.finditer(title or ""):
                g = (m.group(1) or "").lower()
                out.append((kind, WORDS.get(g) or (int(g) if g else None)))
    return out


def read_marker(title):
    """The title's first season marker as an episode record holds it (end, start, then "Main Story Finale"), or
    None."""
    found = read_markers(title)
    if found:
        return {"type": found[0][0], "season": found[0][1]}
    return {"type": "main_end", "season": None} if MAIN_END.search(title or "") else None


def close_seasons(ends, starts):
    """A start marker for season N closes season N-1 at the last chapter before it, unless an end marker already
    closed N-1. ends: {season: last chapter}, updated in place and returned; starts: [(season, last chapter before its
    start marker, or None)] in the caller's order."""
    for season, before in starts:
        if season > 1 and season - 1 not in ends and before is not None:
            ends[season - 1] = before
    return ends


def norm_number(n):
    """'01' -> '1', '17.50' -> '17.5', '3.0' -> '3'; None / '' / 'none' -> None; non-numeric kept as is."""
    if n is None:
        return None
    n = str(n).strip()
    if not n or n.lower() == "none":
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", n):
        return n
    whole, _, frac = n.partition(".")
    whole, frac = whole.lstrip("0") or "0", frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _same(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _tidy(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return re.sub(r"^[\s\-\u2013\u2014:|.]+|[\s\-\u2013\u2014:|]+$", "", t) or None


def classify(raw_title):
    """(kind, stated number, rest of the title after its label). A stated chapter number ("Ep. 3 - X", "3. X") makes
    it a chapter; otherwise announcement-type words make it a non-chapter post."""
    t = raw_title or ""
    if SPECIAL_WORDS.match(t) and not NON_CHAPTER_WORDS.search(t):
        m = SPECIAL_LABEL.match(t)
        if m and m.group(1):
            return SPECIAL, m.group(1), t[m.end() :]
        if m and re.search(r"[-\u2013\u2014:|]\s*$", m.group(0)) and t[m.end() :].strip():
            return SPECIAL, None, t[m.end() :]
        return SPECIAL, None, t
    m = LABEL.match(t)
    if m:
        return MAIN, m.group(1) or m.group(2), t[m.end() :]
    if NON_CHAPTER_WORDS.search(t):
        return NON_CHAPTER, None, t
    return MAIN, None, t


def build(items):
    """items: [(site number, raw title, date, id)] in site order -> episode records (module doc). When most posts
    state a chapter number, an unnumbered post that is not a special is a non-chapter; in a series whose titles carry
    no numbers at all, every such post is a chapter."""
    parsed = [(n, (raw or "").strip(), date, eid) for n, raw, date, eid in items]
    kinds = [classify(raw) for _, raw, _, _ in parsed]
    numbered = sum(1 for k, stated, _ in kinds if k == MAIN and stated is not None)
    unnumbered_are_chapters = numbered < len(parsed) / 2
    out, prev_main, last_end, started = [], None, 0, None
    for (number, raw, date, eid), (kind, stated, rest) in zip(parsed, kinds, strict=True):
        chapter, title = None, raw
        if kind == MAIN and stated is None and not unnumbered_are_chapters:
            kind = NON_CHAPTER
        if kind == MAIN:
            expected = int(prev_main) + 1 if prev_main is not None else None
            # the label is the episode's own number when it repeats the site index or the running chapter count; in a
            # numbered series also on the first chapter and on small forward jumps (a chapter the site skipped);
            # otherwise ("Episode 7 Begins" as chapter 2) it is part of the title
            own = stated is not None and (
                _same(stated, number)
                or _same(stated, expected)
                or (not unnumbered_are_chapters and (prev_main is None or prev_main < float(stated) <= prev_main + 3))
                or (prev_main is None and float(stated) in (0, 1))
            )
            if own:
                chapter, title = norm_number(stated), rest
            else:
                chapter = str(expected if expected is not None else 1)
            prev_main = float(chapter)
        elif kind == SPECIAL:
            chapter = norm_number(stated)
            title = rest  # "SE 1. X" / "Bonus: X" -> "X"; a bare "Epilogue" kept
        marker = read_marker(raw)
        if marker and marker["type"] in ("end", "start") and marker["season"] is None:
            # "(Season Finale)" / "(Season Premiere)" without a number: the season in line
            marker["season"] = (started or last_end + 1) if marker["type"] == "end" else last_end + 1
        if marker and marker["type"] == "end":
            last_end, started = max(last_end, marker["season"]), None
        elif marker and marker["type"] == "start":
            started = marker["season"]
        title = BRACKETED_MARKER.sub("", title)
        if BARE_MARKER.match(title):
            title = ""
        title = _tidy(title)
        if title and kind == MAIN and names.BARE_LABEL.fullmatch(title):
            title = None  # a bare label left over carries no title
        out.append(
            {
                "number": number,
                "chapter": chapter,
                "raw_title": raw,
                "title": title,
                "kind": kind,
                "season_marker": marker,
                "date": date,
                "id": eid,
            }
        )
    return out


def season_breaks(episodes):
    """Season ranges over main chapters from the episodes' markers: [{"season": n, "first": chapter, "last": chapter
    or None (open: no end marker yet), "side": bool}]. An end marker closes its season at that episode (on a
    non-chapter post: at the last chapter before it); a start marker for season N closes N-1 one chapter earlier,
    unless an end marker already did. "Main Story Finale" closes the running season, and the chapters after it form 1
    more range with side true. [] when no episode carries a season marker."""
    ends, starts, main_end, last_main = {}, [], None, None
    for e in episodes:
        m = e.get("season_marker")
        here = float(e["chapter"]) if e["kind"] == MAIN and e.get("chapter") is not None else None
        at = here if here is not None else last_main
        if m and m["type"] == "end" and at is not None:
            ends[m["season"]] = max(ends.get(m["season"], at), at)
        elif m and m["type"] == "start":
            starts.append((m["season"], last_main))  # the last chapter before this post
        elif m and m["type"] == "main_end" and at is not None:
            main_end = at
        if here is not None:
            last_main = here
    close_seasons(ends, starts)
    if not ends and not starts and main_end is None:
        return []
    if main_end is not None and not any(end >= main_end for end in ends.values()):
        ends[max(ends, default=0) + 1] = main_end
    mains = sorted({float(e["chapter"]) for e in episodes if e["kind"] == MAIN and e.get("chapter") is not None})
    out, prev, last_season = [], None, 0
    for season, end in sorted(ends.items()):
        if prev is not None and end <= prev:
            continue
        first = next((c for c in mains if prev is None or c > prev), None)
        if first is None or first > end:
            continue
        out.append({"season": season, "first": names.fmt_num(first), "last": names.fmt_num(end), "side": False})
        prev, last_season = end, season
    rest = [c for c in mains if prev is None or c > prev]
    if rest:
        side = main_end is not None and main_end <= (prev or 0)
        out.append({"season": last_season + 1, "first": names.fmt_num(rest[0]), "last": None, "side": side})
    return out


def gaps(episodes, key="number"):
    """Site numbers (or chapter numbers) missing from a run expected to be 1..max; [] when contiguous."""
    nums = sorted(
        {
            int(float(e[key]))
            for e in episodes
            if e.get(key) is not None and (key == "number" or e["kind"] == MAIN) and float(e[key]) == int(float(e[key]))
        }
    )
    if not nums:
        return []
    have = set(nums)
    return [n for n in range(1, nums[-1] + 1) if n not in have]


def coverage(episodes, listed_total=None, app_only=False, list_page_read=True):
    """{"status": complete | partial | unknown, "reason"}: partial when the site lists fewer episodes than it says it
    has, when site numbers are missing, when none is listed, or when the platform keeps episodes to its app; unknown
    when the page that tells is unread."""
    reasons = []
    if listed_total and len(episodes) < listed_total:
        reasons.append(f"{len(episodes)} of {listed_total} episodes listed")
    missing = gaps(episodes)
    if missing:
        reasons.append(f"episode numbers missing: {missing[:10]}")
    if not episodes:
        reasons.append("no episodes listed")
    if app_only:
        reasons.append("episodes only on the app")
    if reasons:
        return {"status": "partial", "reason": "; ".join(reasons)}
    return {"status": "complete", "reason": None} if list_page_read else {"status": "unknown", "reason": "unread"}
