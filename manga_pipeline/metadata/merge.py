"""A series' fields from its stored records (SPEC "Metadata"): what goes to Kavita through its API and into every
`ComicInfo.xml` written. `fields` is pure: the same records, ids and rules give the same fields; `stored` reads them
from the database.

From the old metadata tool's build_metadata (summary order, links, status), with the fields it never wrote (decision
D7.11): age rating from MangaBaka, language from MangaDex, writers and pencillers from AniList's staff.
"""

import html.parser
from dataclasses import dataclass

from markdown_it import MarkdownIt

from manga_pipeline.metadata import ids, tags

# MangaBaka status -> Kavita's publication status (0 Ongoing, 1 Hiatus, 2 Completed, 3 Cancelled; Ended is never set)
KAVITA_STATUS = {"releasing": 0, "upcoming": 0, "hiatus": 1, "completed": 2, "cancelled": 3}
# MangaBaka content rating -> (ComicInfo AgeRating, Kavita's age rating value)
AGE_RATING = {
    "safe": ("Everyone", 3),
    "suggestive": ("Teen", 8),
    "erotica": ("Mature 17+", 10),
    "pornographic": ("Adults Only 18+", 13),
}
WRITER_ROLES = {"Story", "Story & Art"}  # AniList staff roles, exactly
PENCILLER_ROLES = {"Art", "Story & Art"}
DIGITS36 = "0123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class SeriesFields:
    summary: str | None
    genres: tuple
    tags: tuple
    status: int | None  # Kavita publication status
    age_rating: str | None  # ComicInfo AgeRating
    age_rating_kavita: int | None
    language: str | None  # ISO code
    writers: tuple
    pencillers: tuple
    links: tuple  # series page URLs, 1 per source
    kavita_ids: dict  # {"aniListId", "malId", "mangaBakaId"}: ints, None when unknown


def fields(records, agreed, rules):
    """The series' fields from its records ({(source, kind): payload or None}) and agreed ids ({source: id})."""
    mb, mu = records.get(("mangabaka", "series")), records.get(("mangaupdates", "series"))
    md, al = records.get(("mangadex", "manga")), records.get(("anilist", "media"))
    md_attr = (md or {}).get("attributes") or {}
    genres, tag_list = tags.compute(records, rules)
    rating = AGE_RATING.get(((mb or {}).get("content_rating") or "").lower())
    staff = [(e.get("role") or "", ((e.get("node") or {}).get("name") or {}).get("full")) for e in _edges(al)]
    return SeriesFields(
        summary=_summary(al, md_attr, mb, mu),
        genres=tuple(genres),
        tags=tuple(tag_list),
        status=KAVITA_STATUS.get(((mb or {}).get("status") or "").lower()),
        age_rating=rating[0] if rating else None,
        age_rating_kavita=rating[1] if rating else None,
        language=md_attr.get("originalLanguage") or None,
        writers=tuple(dict.fromkeys(name for role, name in staff if role in WRITER_ROLES and name)),
        pencillers=tuple(dict.fromkeys(name for role, name in staff if role in PENCILLER_ROLES and name)),
        links=tuple(_links(agreed, mb, mu, md, al)),
        kavita_ids={
            "aniListId": _int(agreed.get("anilist")) if al else None,
            "malId": _int(agreed.get("myanimelist")),
            "mangaBakaId": _int(agreed.get("mangabaka")) if mb else None,
        },
    )


def stored(conn, series_id, rules):
    """The series' fields from its stored records and agreed ids."""
    records = {
        (r["source"], r["kind"]): r["payload"]
        for r in conn.execute("SELECT source, kind, payload FROM fetched WHERE series_id = %s", (series_id,))
    }
    known = conn.execute(
        "SELECT source, origin, source_id FROM series_source_id WHERE series_id = %s", (series_id,)
    ).fetchall()
    agreed, _ = ids.resolve([tuple(r) for r in known])
    return fields(records, agreed, rules)


def _edges(al):
    return ((al or {}).get("staff") or {}).get("edges") or []


def _summary(al, md_attr, mb, mu):
    """The first non-empty of AniList (HTML), MangaDex English (Markdown), MangaBaka (Markdown and HTML), MangaUpdates
    (HTML)."""
    found = [
        html_text((al or {}).get("description")),
        markdown_text((md_attr.get("description") or {}).get("en")),
        markdown_text((mb or {}).get("description")),
        html_text((mu or {}).get("description")),
    ]
    return next((text for text in found if text), None)


def _links(agreed, mb, mu, md, al):
    """1 page per source whose record was found (a record a check dropped as a novel's gives none)."""
    found = []
    if al and agreed.get("anilist"):
        found.append(f"https://anilist.co/manga/{agreed['anilist']}")
    if mu and agreed.get("mangaupdates"):
        found.append(f"https://www.mangaupdates.com/series/{_base36(int(agreed['mangaupdates']))}")
    if md and agreed.get("mangadex"):
        found.append(f"https://mangadex.org/title/{agreed['mangadex']}")
    if mb and (mb.get("canonical_url") or agreed.get("mangabaka")):
        found.append(mb.get("canonical_url") or f"https://mangabaka.org/{agreed['mangabaka']}")
    if agreed.get("myanimelist"):
        found.append(f"https://myanimelist.net/manga/{agreed['myanimelist']}")
    return found


def _int(value):
    return int(value) if value is not None else None


def _base36(number):
    out = ""
    while number:
        number, rest = divmod(number, 36)
        out = DIGITS36[rest] + out
    return out or "0"


# --- text


class _Text(html.parser.HTMLParser):
    """An HTML fragment's text: a line break for <br>, <p> and </p>, every other tag dropped, entities decoded."""

    BREAKS = {"br", "p"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_text(text):
    """Plain text of an HTML summary: paragraphs kept (at most 1 blank line between), a trailing "(Source: ...)"
    note dropped."""
    if not text:
        return None
    parser = _Text()
    parser.feed(text)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    while lines and (not lines[-1] or (lines[-1].lower().startswith("(source:") and lines[-1].endswith(")"))):
        lines.pop()
    return _paragraphs(lines)


def markdown_text(text):
    """Plain text of a Markdown summary, cut at its first horizontal rule (MangaDex appends links and credits under
    one); links keep their text, emphasis marks go."""
    if not text:
        return None
    md = MarkdownIt("commonmark")
    tokens = md.parse(text)
    cut = next((i for i, token in enumerate(tokens) if token.type == "hr"), len(tokens))
    return html_text(md.renderer.render(tokens[:cut], md.options, {}))


def _paragraphs(lines):
    out, blank = [], False
    for line in lines:
        if line:
            if blank and out:
                out.append("")
            out.append(line)
            blank = False
        else:
            blank = True
    return "\n".join(out) or None
