"""Chapter names (SPEC "Bad downloads", "Publishing"): the file-name parser, a chapter's identity and the name a
published file gets. The parser is the bulk converter's optimizer/structure.py name part, copied (its series-level
structure pass is not); the published name, title and volume text are its optimizer/naming.py rules, copied.

A chapter name is free text written by scanlators, not a structured format: the patterns below are the heuristics the
bulk converter settled on over its library.
"""

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import overload

MAIN, SIDE, NON_CHAPTER, UNNUMBERED = "main", "side", "non-chapter", "unnumbered"

# Labels that precede a chapter number. Order matters: longer labels first.
SIDE_LABELS = [
    "special episode",
    "bonus episode",
    "bonus chapter",
    "side story",
    "after story",
    "extra episode",
    "special chapter",
    "spin-off",
    "spin off",
    "epilogue",
    "special",
    "bonus",
    "extra",
    "omake",
    "side",
]
MAIN_LABELS = ["chapter", "chap", "episode", "ch", "ep", "rem", "prologue", "story", "day", "page", "#"]
# labels that only count with a number after them ("Day 1", "# 10"); "Story of a Knight" is a title
NUMBER_ONLY_LABELS = {"story", "day", "page", "#"}
_LABEL = "|".join(re.escape(label).replace(r"\ ", r"\s+") for label in SIDE_LABELS + MAIN_LABELS)
# "Chapter 45-1" / "Episode 15-2" / "Chapter 172-5": hyphen part = decimal part (ComicInfo Number says 45.1)
_NUM = r"\d+(?:\.\d+)?(?:-\d+(?![\d.]))?"
NAME_RE = re.compile(
    rf"^(?:vol(?:ume)?\.?\s*(?P<vol>\d+(?:\.\d+)?)\s*[,.]?\s*)?(?P<label>{_LABEL})(?![a-z])\.?\s*#?\s*(?P<num>{_NUM})?"
    rf"(?P<rest>.*)$",
    re.IGNORECASE,
)
VOLUME_ONLY = re.compile(
    r"^vol(?:ume)?\.?\s*(?P<vol>\d+(?:\.\d+)?)(?P<rest>(?:\s*[-_:].*)?)$", re.IGNORECASE
)  # "Volume 1"
CI_NUMBER = re.compile(r"\d+(?:\.\d+)?")  # ComicInfo Number
NUM_DOT = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\.\s+(?P<rest>\S.*)$")  # "64. Title"
# leading season tags: "S1 - Chapter 1", "S1 Episode 29", "(S2) Episode 100", "[Season 2] Chapter 2"
SEASON_TAG = re.compile(
    r"^\s*(?:\[\s*season\s*(\d+)\s*\]|\(\s*s(?:eason)?\s*(\d+)\s*\)|s(\d+)(?:\s*-)?(?=\s)|"
    r"season\s*(\d+)\s*[-:]?(?=\s))\s*",
    re.IGNORECASE,
)
LEAD_TAG = re.compile(r"^\s*[\[({〖【][^\])}〗】]*[\])}〗】]\s*")
TITLE_SEP = re.compile(r"^\s*(?:[-_:]+\s*)+|\s*[-_:]+\s*$")
BRACKETS = re.compile(r"[\[({〖【][^\])}〗】]*[\])}〗】]")
# "[Side Story] Chapter 1" style tags mark side stories even under a "Ch." label: the tag is the side label itself
# (optionally numbered). "[Epilogue End]" is an end marker on a main chapter, not a side tag.
SIDE_MARKER = re.compile(
    r"^\s*[\[({〖【]\s*(?P<side>"
    + "|".join(re.escape(label) for label in SIDE_LABELS)
    + r")(?:\s*\d+(?:\.\d+)?)?\s*[\])}〗】]",
    re.IGNORECASE,
)
# rated editions: "[R15]" (censored), "[R19]", "[R19 version]", "[🔞]" (uncensored)
RATING = re.compile(r"[\[(]\s*(?:R-?(?P<age>\d{2})\+?(?:\s*version)?|(?P<adult>🔞))\s*[\])]", re.IGNORECASE)
END_MARKER = re.compile(r"[\[({〖【][^\])}〗】]*\b(?:end|finale)\b[^\])}〗】]*[\])}〗】]", re.IGNORECASE)
# a bare chapter label ("Chapter 18", "Ep. 3"): as a whole title it only repeats the number (naming, episodes)
BARE_LABEL = re.compile(r"(?:chapter|ch\.?|episode|ep\.?)\s*\d+(?:\.\d+)?", re.IGNORECASE)
MONTHS = {
    m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)
}
ADULT_AGE = 18  # a rating tag from this age up is uncensored
SAME_TITLE_SHARE = 0.6  # shared words over all words from which 2 titles are the same


@dataclass
class Item:
    file: str
    group: str = ""
    volume: str | None = None
    label: str = ""
    number: str | None = None
    kind: str = MAIN
    title: str = ""
    pages: int = 0
    width: int = 0
    bytes: int = 0
    date: str | None = None
    season: str | None = None  # leading season tag ("S1 - ", "(S2) ", "[Season 2] "), kept off the title
    error: bool = False  # unreadable CBZ: integrity's to judge, never trashed here
    notes: list = field(default_factory=list)

    @property
    def num(self):
        return float(self.number) if self.number is not None else None

    @property
    def key(self):
        return number_key(self.number)


def number_key(number):
    """Chapter number as parts, so 3.1 and 3.10 stay apart: "3.10" -> (3, "10"), "3" / "3.0" -> (3, "")."""
    if number is None:
        return None
    return _number_parts(number)


def _number_parts(number):
    """number_key of a number that is not None."""
    base, _, frac = str(number).strip().partition(".")
    return int(base), frac if frac.strip("0") else ""


def two_readings(number):
    """Why a chapter number has no single reading, or None: its decimal part ends in 0 without being all zeros
    ("26.10": 26.1, or part 10 of chapter 26). fmt_num, a file name and Kavita read such a number as the shorter
    decimal, so a download numbered so is held for a person (SPEC "Bad downloads")."""
    if number is None:
        return None
    whole, part = _number_parts(number)
    if not part.endswith("0"):
        return None
    return (
        f"number {whole}.{part} has no single reading: {whole}.{part.rstrip('0')}, or part {int(part)} of chapter "
        f"{whole}"
    )


@overload
def fmt_num(n: None) -> None: ...
@overload
def fmt_num(n: float | str | Decimal) -> str: ...
def fmt_num(n):
    """Chapter / volume number as text, every digit kept: 1.0 / "01" -> "1", 17.5 -> "17.5"; None -> None."""
    if n is None:
        return None
    return format(Decimal(str(n)).normalize(), "f")  # str(float): the shortest exact repr


def _peel(body):
    """Leading season tags and other bracket tags before the label: (season, tags text, rest)."""
    season, tags = None, ""
    while True:
        m = SEASON_TAG.match(body)
        if m and season is None:
            season, body = next(g for g in m.groups() if g), body[m.end() :]
            continue
        m = LEAD_TAG.match(body)
        if m:
            tags, body = tags + m.group(0), body[m.end() :]
            continue
        return season, tags.strip(), body


def _match(body):
    """(season, tags, kind of match, regex match) for a name body, or None. A label without a number counts only
    for labels that stand alone ("Epilogue", "Prologue", "Special Episode: Ivy")."""
    season, tags, rest = _peel(body.strip())
    for shape, rx in (("label", NAME_RE), ("volume", VOLUME_ONLY), ("numdot", NUM_DOT)):
        m = rx.match(rest)
        if not m:
            continue
        if (
            shape == "label"
            and m.group("num") is None
            and re.sub(r"\s+", " ", m.group("label").lower()) in NUMBER_ONLY_LABELS
        ):
            continue
        return season, tags, shape, m
    return None


def _numbered(body):
    hit = _match(body)
    return bool(hit) and hit[2] != "volume" and (hit[3].group("num") is not None)


def number_start(stem):
    """Index in `stem` where parse_name's chapter number starts ("Vol.1 Ch.3.1" -> the 3 after "Ch."), None when the
    file name carries no number (ComicInfo numbers the item)."""
    _, body = split_group(stem)
    hit = _match(body)
    if not hit or "num" not in hit[3].re.groupindex or hit[3].group("num") is None:
        return None
    stripped = body.strip()
    rest = hit[3].string  # the suffix of `stripped` the shape regex matched
    return len(stem) - len(body) + (len(body) - len(body.lstrip())) + len(stripped) - len(rest) + hit[3].start("num")


def split_group(stem):
    """'Easy Going Scans_Vol.1 Ch.1' -> ('Easy Going Scans', 'Vol.1 Ch.1');
    '_dumbcat__Chapter 45' -> ('_dumbcat_', ...)."""
    if _numbered(stem):
        return "", stem
    for k, ch in enumerate(stem):
        if ch == "_" and _numbered(stem[k + 1 :]):
            return stem[:k], stem[k + 1 :]
    m = re.match(rf"^(?P<group>.+?)_(?=(?:vol(?:ume)?\.?\s*\d|(?:{_LABEL})(?![a-z])))", stem, re.IGNORECASE)
    if m and not _match(stem):
        return m.group("group"), stem[m.end() :]
    m = re.match(r"^(?P<group>[^_]+)_(?P<rest>.+)$", stem)
    if m and not _match(stem):
        return m.group("group"), m.group("rest")
    return "", stem


def clean_title(rest):
    return TITLE_SEP.sub("", rest).strip()


def _hyphen_part(num):
    """'45-1' -> '45.1' (hyphen part = decimal part); other numbers unchanged."""
    return num.replace("-", ".", 1) if num and "-" in num else num


def parse_name(stem, ci=None, non_chapter_names=()):
    """Classify one chapter file from its filename stem; ComicInfo Number numbers it when the filename has none."""
    group, body = split_group(stem)
    item = Item(file=stem, group=group.strip("_ ") or (ci or {}).get("Translator", ""))
    hit = _match(body)
    if hit:
        _read_shape(item, stem, hit)
    else:
        item.title = clean_title(body)
    _number_from_comicinfo(item, hit[3].groupdict().get("num") if hit else None, (ci or {}).get("Number") or "")
    item.kind = _kind(item, [item.title] if hit else [item.title, body], non_chapter_names)
    return item


def _read_shape(item, stem, hit):
    """Fill item from its name's shape match (_match): season, label, volume, number and title. A prologue without a
    number is chapter 0; a hyphen part is the decimal part, and a decimal with a hyphen part ("045.5-1") has no
    single reading, so no number."""
    season, tags, shape, m = hit
    item.season = season
    if shape == "label":
        label = re.sub(r"\s+", " ", m.group("label").lower())
        item.label, item.volume, item.number = label, m.group("vol"), _hyphen_part(m.group("num"))
    elif shape == "volume":
        item.label, item.volume = "volume", m.group("vol")
    else:
        item.number = m.group("num")
    # other leading tags stay with the title ("[Side Story] Chapter 1" -> "[Side Story]")
    item.title = " ".join(x for x in (tags, clean_title(m.group("rest"))) if x)
    if item.label == "prologue" and item.number is None:
        item.number, item.notes = "0", ["prologue without number -> chapter 0"]
    if m.groupdict().get("num") and "-" in m.group("num"):
        if item.number is None:
            msg = f"{stem!r}: the num group set no number"
            raise ValueError(msg)
        if not CI_NUMBER.fullmatch(item.number):  # "045.5-1": a decimal with a hyphen part
            item.number = None
        else:
            item.notes.append(f"hyphen part {m.group('num')} -> {item.number}")


def _number_from_comicinfo(item, name_number, ci_num):
    """Number an item its file name left unnumbered from ComicInfo Number (ci_num, "" when absent); note a file
    name's hyphen-part number (name_number: the number as the name writes it) that ComicInfo disagrees with."""
    if item.number is None and ci_num:
        if CI_NUMBER.fullmatch(ci_num):  # not "-1" (Tachiyomi's unknown number), "1e3", "nan"
            item.number = ci_num
            item.notes.append("number from ComicInfo")
    elif item.number is not None and "-" in (name_number or "") and ci_num and number_key(ci_num) != item.key:
        item.notes.append(f"hyphen part {item.number} disagrees with ComicInfo Number {ci_num}")


def _kind(item, texts, non_chapter_names):
    """An item's kind: a non-chapter when one of texts (its title, and an unmatched name's whole body) is a
    non-chapter name, else unnumbered without a number, else a side story by its label or side tag, else main."""
    names = {normalize_title(n) for n in non_chapter_names}
    if any(normalize_title(t) in names for t in texts):
        return NON_CHAPTER
    if item.number is None:
        return UNNUMBERED
    return SIDE if item.label in SIDE_LABELS or SIDE_MARKER.match(item.title) else MAIN


def side_label(item):
    """The side-story series an item belongs to: its own label, or the side tag's ("[Side Story] Chapter 1")."""
    if item.label in SIDE_LABELS:
        return item.label.replace("spin off", "spin-off")
    m = SIDE_MARKER.match(item.title)
    return m.group("side").lower() if m else ""


def rating(title):
    """'uncensored' for [R19]/[R18]/[🔞], 'censored' for [R15]/[R12], None when untagged."""
    ages = [99 if m.group("adult") else int(m.group("age")) for m in RATING.finditer(title or "")]
    return None if not ages else ("uncensored" if max(ages) >= ADULT_AGE else "censored")


def normalize_title(t):
    """Lowercase words for comparison; bracketed markers ([END], [FIXED]) dropped, rating tags kept as tokens."""
    t = RATING.sub(lambda m: " rated_" + ("adult" if m.group("adult") else "r" + m.group("age")) + " ", t or "")
    t = BRACKETS.sub(" ", t).lower().replace("\u2019", "'")  # right single quotation mark
    return re.sub(r"[^a-z0-9'_]+", " ", t).strip()


def similar_titles(a, b):
    """Both untitled, equal, or token overlap >= 60% (ignoring bracketed markers like [END], not rating tags)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na and not nb:
        return True
    if not na or not nb:
        return False
    ta, tb = set(na.split()), set(nb.split())
    return na == nb or len(ta & tb) / len(ta | tb) >= SAME_TITLE_SHARE


DATE_PARTS = (("Year", 1, 9999), ("Month", 1, 12), ("Day", 1, 31))


def ci_date_parts(ci):
    """ComicInfo Year / Month / Day each on its own: ({part: int or None}, [(part, value)] of the values present but
    unusable). A part is an ASCII integer in range (DATE_PARTS); Month may be a name ("Mar", "March"); never clamped."""
    parts, bad = {}, []
    for name, lo, hi in DATE_PARTS:
        raw = str((ci or {}).get(name) or "").strip()
        v = None
        if raw:
            if raw.isascii() and raw.isdigit():
                v = int(raw)
            elif name == "Month":
                v = MONTHS.get(raw[:3].lower())
            if v is None or not lo <= v <= hi:
                bad.append((name, raw))
                v = None
        parts[name] = v
    return parts, bad


def ci_date(ci):
    """'YYYY-MM-DD' from ComicInfo Year/Month/Day (ci_date_parts; a missing or unusable Month / Day counts as 1);
    None without a usable Year."""
    p, _ = ci_date_parts(ci)
    if p["Year"] is None:
        return None
    return f"{p['Year']:04d}-{p['Month'] or 1:02d}-{p['Day'] or 1:02d}"


# --- identity and published file name


def parse(stem, ci_number=""):
    """The parse of a chapter file's stem ("<scanlator>_<chapter name>", or the name alone), its ComicInfo Number
    numbering it when the name has none."""
    return parse_name(stem, {"Number": ci_number} if ci_number else None)


def key(text):
    """Letters and digits only, lower-cased (after NFKC): ":" in a chapter name is "_" in its file name, and both
    must give 1 key."""
    return "".join(c for c in unicodedata.normalize("NFKC", text or "").casefold() if c.isalnum())


def identity(item):
    """A chapter's identity (SPEC "Bad downloads"): JSON text of [scanlator key, season, side label, number, title
    key]; numbers as fmt_num gives them, so "01" and "1" agree. A number with 2 readings (two_readings) stays as
    written ("26.10"), so it is never the identity of the chapter fmt_num would make of it (26.1)."""
    season = fmt_num(item.season) if item.season is not None else None
    number = "{}.{}".format(*_number_parts(item.number)) if two_readings(item.number) else fmt_num(item.number)
    return json.dumps([key(item.group), season, side_label(item), number, key(item.title)])


# --- the bulk converter's naming of a published file (its optimizer/naming.py), copied: SPEC "Publishing"

# Title markers stripped from a chapter title: the bulk converter's config naming.strip_title_markers, then its
# STATUS_MARKERS ("(END)", "{END}", "[End of Season 1]", "(Season 2 Finale)").
TITLE_MARKERS = (
    r"\[END\]",
    r"\[End of Main Story\]",
    r"[\(\{\[]\s*S(?:eason)?\s*\d+\s*-?\s*(?:END|START|Finale)\s*[\)\}\]]",
    r"\[Season \d+\]",
    r"[\(\[\{]\s*(?:END|End of Main Story|End of Season\s*\d+|Season\s*\d+\s*(?:END|Finale))\s*[\)\]\}]",
)
ILLEGAL = re.compile(r'[\\/:*?"<>|]')
MAX_TITLE = 80


def to_volume(v):
    """'2' -> 2, '0' -> 0, '2.5' -> 2.5. None, '', 'none', non-numeric ('Special') and negative values
    (Suwayomi/Kavita write -1 for "no volume") -> None."""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or f < 0 or f == float("inf"):
        return None
    return int(f) if f == int(f) else f


def title_from_comicinfo(ci_title, num=None):
    """'Chapter 4 : Where Is She?' -> 'Where Is She?'; 'Vol.1 Ch.1' -> ''. The leading label is dropped only when
    its number is the chapter's own (`num`): "Episode 7 Begins" on chapter 2 is the title itself."""
    if not ci_title:
        return ""
    it = parse_name(ci_title, None, ())
    if it.number is None or (num is not None and float(it.number) != float(num)):
        return ci_title
    return it.title


def clean_marker_title(title):
    """Strip status markers ([END], (END), (S1 END), {S2-END}, [End of Main Story], (End of Season 1)), unwrap
    decorative brackets (〖Main Story Finale〗 -> Main Story Finale) and tidy separators (" _ " was ":" before
    Suwayomi)."""
    for m in TITLE_MARKERS:
        title = re.sub(m, " ", title, flags=re.IGNORECASE)
    title = re.sub(r"[〖【]\s*([^〗】]*?)\s*[〗】]", r"\1", title)
    title = re.sub(r"\s+_\s+|\s+:\s*", ": ", title)
    title = re.sub(r"\s+", " ", title)
    # leftover separators go; a dash glued to the last word stays (MangaDex "Dreaming All Day -Revelation-")
    title = re.sub(r"^[\s\-_:]+|(?:\s+[\-_:]+|[\s_:]+)+$", "", title)
    # a bare label ("Chapter 18" left over from "[Season 2] Chapter 18") repeats the number: no title
    return "" if BARE_LABEL.fullmatch(title) else title


def chapter_title(item, ci_title):
    """A chapter's title as the bulk converter gives it: its ComicInfo Title (which keeps characters the file name
    lost) without a leading label of its own number, else the file name's title; markers stripped. "" for none."""
    return clean_marker_title(title_from_comicinfo(ci_title or "", item.num) or item.title)


def safe_title(title, limit=MAX_TITLE):
    """Filename-safe title: Windows-illegal characters replaced, trailing dots/spaces trimmed, length capped."""
    t = re.sub(r"\s*:\s+", " - ", re.sub(r"\s+", " ", title))  # "Side Story 1: Anxiety" reads as " - "
    t = ILLEGAL.sub("_", t).strip().rstrip(". ")
    return t if len(t) <= limit else t[: limit - 1].rstrip(" ._-") + "…"


def folder_name(series_name):
    """A new series' folder name (SPEC "Series identification"): Windows-illegal characters replaced, trailing dots
    and spaces trimmed; "" when nothing is left."""
    return ILLEGAL.sub("_", series_name).strip().rstrip(". ")


def file_name(number, title, volume=None):
    """The name a published file gets (SPEC "Publishing"): "Vol.<volume> Ch.<number> - <title>.cbz", without
    "Vol.<volume> " when the volume is None and without " - <title>" when the title is empty once made safe.
    ValueError for no number."""
    if number is None:
        msg = "a published file needs a chapter number"
        raise ValueError(msg)
    name = (f"Vol.{fmt_num(volume)} " if volume is not None else "") + f"Ch.{fmt_num(number)}"
    safe = safe_title(title) if title else ""
    return name + (f" - {safe}" if safe else "") + ".cbz"
