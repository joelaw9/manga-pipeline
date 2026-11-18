"""The chapter plan (SPEC "Conversion"): what convert.image.process_chapter executes for 1 chapter.

A slim builder in place of the bulk converter's planner: its route table and render specs are copied from
optimizer/plan_routes.py (volume covers and force_avif included), its page entry from optimizer/plan.py page_entry.

Inputs: the download's measured pages (images.measure_pages), the extracted page files, the junk stage's edit
(chapter_edit, checked by check_edit), the series' layout and page aspect, and the AVIF quality per encode group.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, final

from manga_pipeline import archive, images, strip
from manga_pipeline.convert import pages as page_reads
from manga_pipeline.replies import Json

RENDERED = "rendered-avif"
AVIF_SOURCES = frozenset({images.JPEG, images.PNG, images.WEBP_LOSSLESS})  # chapters of only these: AVIF, else lossless
UNTOUCHED_SOURCES = frozenset({images.WEBP_LOSSY, images.GIF, images.AVIF, images.JXL})  # only these: untouched
GRAYISH = (images.GRAY, images.TINTED, images.GRAY_MARK)  # encoded without chroma
COVER_FORMATS = (images.AVIF, images.JPEG, images.PNG, images.WEBP_LOSSY)  # chapter formats a volume cover can match
QUALITY_GROUPS = ("gray", "colour")  # encode groups: 1 AVIF quality each
CLASS_RANK = {images.GRAY: 0, images.TINTED: 1, images.GRAY_MARK: 2, images.COLOUR: 3}  # rendered page = max
CHROMA_RANK = {"420": 0, "422": 1, "444": 2}  # rendered page = max
TURNED = range(2, 9)  # EXIF orientations readers apply: a rotation or mirror
EDIT_KEYS: dict[str, type[object]] = {
    "drop_pages": list,
    "strip_removed": list,
    "bands": list,
    "join": list,
    "split": bool,
    "blank": list,
}
BOX_SIDES = 4  # a box: [left, top, right, bottom]
PAIR_LEN = 2  # a join entry: [page, sliver]


class RemovedRange(strip.RowRange):
    """A strip_removed entry: strip rows [start, end) taken out, and why (the junk rule that removed them)."""

    why: str


class BandCrop(TypedDict):
    """A bands entry: a page (download index) cropped to its box [left, top, right, bottom], end-exclusive."""

    page: int
    box: list[int]


class Blank(TypedDict):
    """A blank entry: a logo's box [left, top, right, bottom] on a page (download index) filled with fill [r, g, b]."""

    page: int
    box: list[int]
    fill: list[int]


class PageLayoutEdit(TypedDict):
    """The page-layout keys of a chapter edit: split, the dropped pages, the band crops and the [page, sliver] joins."""

    split: bool
    drop_pages: list[int]
    bands: list[BandCrop]
    join: list[list[int]]


class ChapterEdit(PageLayoutEdit):
    """A chapter_edit (EDIT_KEYS, checked by check_edit): the page-layout keys, the strip rows taken out, the blanks."""

    strip_removed: list[RemovedRange]
    blank: list[Blank]


class PlanPage(TypedDict):
    """A plan's page (page_entry): its measured fields, what the plan reads from its file, its (w, h), cover-named.

    jpeg_feed and jpeg_gray (pages.jpeg_feed / jpeg_gray) are False for a page that is not a JPEG.
    """

    name: str
    index: int
    sha1: str
    format: str
    chroma: str
    jpeg_feed: bool
    jpeg_gray: bool
    size: strip.Size
    bytes: int
    cover: bool


class Drop(TypedDict):
    """A planned drop: the page (by name) left out."""

    page: str


class Crop(TypedDict):
    """A planned crop: a page (by name) cropped to its box [left, top, right, bottom], end-exclusive."""

    page: str
    box: list[int]


class Join(TypedDict):
    """A planned join: the sliver (by name) joined under its page."""

    page: str
    sliver: str


class PlannedBlank(TypedDict):
    """A planned blank: a logo's box [left, top, right, bottom] on a page (by name) filled with fill [r, g, b]."""

    page: str
    box: list[int]
    fill: list[int]


class StripSpec(strip.RenderStrip):
    """A strip or split spec: the render strip and the cuts its output pages are cut at (kept coordinates)."""

    cuts: list[int]


class PlannedEdits(TypedDict):
    """The edit in page names (the bulk converter's planned-edit shape); split and strip None when not planned."""

    drop: list[Drop]
    crop: list[Crop]
    join: list[Join]
    split: StripSpec | None
    strip: StripSpec | None
    blank: list[PlannedBlank]


# The "class" key (a keyword, so the functional syntax): a page class (images), the most colourful of its sources.
PageClass = TypedDict("PageClass", {"class": str})


class PartsPage(strip.PartsOutput, PageClass):
    """A rendered page stacked from its source pages' parts: its sources ("src"), class and avifenc --yuv."""

    src: list[str]
    yuv: str


class RowsPage(strip.RowsOutput, PageClass):
    """A rendered page of strip rows: its sources ("src"), class and avifenc --yuv."""

    src: list[str]
    yuv: str


type RenderPage = PartsPage | RowsPage


class PassedAvif(PageClass):
    """A volume cover's AVIF that passed the gate (image.Encoder.cover): its file, score, bytes, class and --yuv."""

    ok: Literal[True]
    path: Path
    score: float
    bytes: int
    yuv: str


class FailedAvif(PageClass):
    """A volume cover's AVIF that failed the AVIF gate (image.Encoder.cover): its score, class and --yuv; no file."""

    ok: Literal[False]
    score: float
    yuv: str


type CoverAvif = PassedAvif | FailedAvif


class VolumeCover(TypedDict):
    """A chapter's volume cover as routes reads it: its file's bytes, the page width it goes in at, its AVIF encode."""

    data: bytes
    width: int
    avif: CoverAvif


@final
class AvifCover(TypedDict):
    """An outcome's volume cover in AVIF: its AVIF that passed the gate, copied in."""

    avif: PassedAvif


@final
class EncodedCover(TypedDict):
    """An outcome's volume cover in its pages' format (JPEG, PNG or lossy WebP): encoded from data at width."""

    format: str
    data: bytes
    width: int


type OutcomeCover = AvifCover | EncodedCover


class Outcome(TypedDict):
    """A planned outcome: its kind, its gate (None: always stands), the edits it applies, its cover, pages.

    outcome "avif", "lossless" or "untouched"; gate "floor" or "floor+size"; edits "all" or "drops"; cover the volume
    cover it inserts (None: it takes none); pages_out its expected pages, the cover included.
    """

    outcome: str
    gate: str | None
    edits: str
    cover: OutcomeCover | None
    pages_out: int


class EditError(ValueError):
    """A chapter_edit whose keys or value types are wrong."""


class NeedsHuman(ValueError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """The chapter cannot be planned and no attempt can change that (the stage makes it needs_human)."""


@dataclass(frozen=True)
class SeriesLayout:
    """A series' conversion layout (series_conversion).

    Its kind, "strip" or "pages", and its page aspects (a list) or None.
    """

    kind: str
    page_aspect: list[float] | None


@dataclass
class ChapterPlan:
    """What process_chapter executes for 1 chapter: its pages, edits, page classes, render, route and outcomes."""

    name: str
    pages: list[PlanPage]
    edits: PlannedEdits
    classes: dict[str, str]  # {page name: page class}
    quality: dict[str, int] = field(default_factory=dict)  # {encode group: AVIF q}, set once calibration chose it
    render: list[RenderPage] = field(default_factory=list)  # rendered pages
    route: str | None = None
    outcomes: list[Outcome] = field(default_factory=list)  # tried in order; a measured failure moves to the next
    force_avif: bool = False  # the volume cover needs the chapter in AVIF (its AVIF cover passed)

    def kept(self) -> list[PlanPage]:
        """Return the plan's pages the edit does not drop, in order."""
        dropped = {d["page"] for d in self.edits["drop"]}
        return [q for q in self.pages if q["name"] not in dropped]


def check_edit(text: str) -> ChapterEdit:
    """Return the chapter_edit row's edit (JSON text) as a dict, checked.

    Checked: exactly EDIT_KEYS with their types, indexes ints, no page dropped twice, ranges ({start, end, why}, 0 <=
    start < end) and boxes of ints. Raises EditError naming what is wrong.
    """
    try:
        edit = json.loads(text)
    except (TypeError, ValueError) as e:
        msg = f"chapter_edit is not JSON: {e}"
        raise EditError(msg) from e
    _check_keys(edit)
    _check_drops(edit["drop_pages"])
    _check_strip_removed(edit["strip_removed"])
    _check_bands(edit["bands"])
    _check_joins(edit["join"])
    _check_blanks(edit["blank"])
    return edit


def _ints(values: Sequence[Json], what: str) -> list[int]:
    ints = [v for v in values if type(v) is int]
    if len(ints) < len(values):
        msg = f"chapter_edit {what} must hold integers, got {values!r}"
        raise EditError(msg)
    return ints


def _check_keys(edit: Json) -> None:
    """Check that the edit is a dict of exactly EDIT_KEYS, each of its type."""
    if not isinstance(edit, dict) or set(edit) != set(EDIT_KEYS):
        msg = f"chapter_edit keys must be {sorted(EDIT_KEYS)}, got {sorted(edit) if isinstance(edit, dict) else edit!r}"
        raise EditError(msg)
    for key, kind in EDIT_KEYS.items():
        if type(edit[key]) is not kind:
            msg = f"chapter_edit {key} must be a {kind.__name__}, got {edit[key]!r}"
            raise EditError(msg)


def _check_drops(drops: list[Json]) -> None:
    """drop_pages: integers, none twice."""
    _ints(drops, "drop_pages")
    if len(set(drops)) < len(drops):
        msg = f"chapter_edit drop_pages names a page twice: {drops!r}"
        raise EditError(msg)


def _check_strip_removed(ranges: list[Json]) -> None:
    """strip_removed: {start, end, why} ranges of integers, 0 <= start < end."""
    for r in ranges:
        if not isinstance(r, dict) or set(r) != {"start", "end", "why"} or not isinstance(r["why"], str):
            msg = f"chapter_edit strip_removed entries are {{start, end, why}}, got {r!r}"
            raise EditError(msg)
        start, end = _ints([r["start"], r["end"]], "strip_removed start / end")
        if not 0 <= start < end:
            msg = f"chapter_edit strip_removed rows {start}-{end} are not 0 <= start < end"
            raise EditError(msg)


def _check_bands(bands: list[Json]) -> None:
    """bands: {page, box: [l, t, r, b]} of integers."""
    for b in bands:
        if (
            not isinstance(b, dict)
            or set(b) != {"page", "box"}
            or not isinstance(b["box"], list)
            or len(b["box"]) != BOX_SIDES
        ):
            msg = f"chapter_edit bands entries are {{page, box: [l, t, r, b]}}, got {b!r}"
            raise EditError(msg)
        _ints([b["page"], *b["box"]], "bands page / box")


def _check_joins(joins: list[Json]) -> None:
    """join: [page, sliver] pairs of integers."""
    for pair in joins:
        if not isinstance(pair, list) or len(pair) != PAIR_LEN:
            msg = f"chapter_edit join entries are [page, sliver] pairs, got {pair!r}"
            raise EditError(msg)
        _ints(pair, "join")


def _check_blanks(blanks: list[Json]) -> None:
    """blank: {page, box: [l, t, r, b], fill: [r, g, b]} of integers."""
    for b in blanks:
        if (
            not isinstance(b, dict)
            or set(b) != {"page", "box", "fill"}
            or not isinstance(b["box"], list)
            or len(b["box"]) != BOX_SIDES
            or not isinstance(b["fill"], list)
            or len(b["fill"]) != images.RGB_CHANNELS
        ):
            msg = f"chapter_edit blank entries are {{page, box: [l, t, r, b], fill: [r, g, b]}}, got {b!r}"
            raise EditError(msg)
        _ints([b["page"], *b["box"], *b["fill"]], "blank page / box / fill")


def page_entry(page: images.MeasuredPage, path: Path) -> PlanPage:
    """Return a measured page (images.measure_pages) with what the plan adds from its file.

    It adds jpeg_feed, jpeg_gray (JPEG only), size, cover-named. Raises NeedsHuman for an EXIF orientation of 2-8 (a
    rotation or mirror readers apply).
    """
    data = Path(path).read_bytes()
    fmt = page["format"]
    if fmt in (images.JPEG, images.PNG, images.WEBP_LOSSY, images.WEBP_LOSSLESS):
        turn = images.exif_orientation(data)
        if turn in TURNED:
            msg = f"page {page['name']} has EXIF orientation {turn} (a rotation or mirror)"
            raise NeedsHuman(msg)
    is_jpeg = fmt == images.JPEG
    return {
        "name": Path(path).name,
        "index": page["index"],
        "sha1": page["sha1"],
        "format": fmt,
        "chroma": page["chroma"],
        "jpeg_feed": is_jpeg and page_reads.jpeg_feed(data),
        "jpeg_gray": is_jpeg and page_reads.jpeg_gray(data),
        "size": (page["width"], page["height"]),
        "bytes": page["bytes"],
        "cover": archive.is_cover_name(page["name"]),
    }


def yuv_for(cls: str, source_chroma: str) -> str:
    """Return avifenc's --yuv for a page of this class: 400 for gray / tinted / gray-mark, else the source's chroma."""
    return "400" if cls in GRAYISH else source_chroma


def feeds_jpeg(entry: PlanPage, yuv: str) -> bool:
    """Return whether the AVIF encode of this page at this --yuv feeds avifenc the JPEG itself.

    A colour (not 4:0:0) encode of a JPEG jpeg_feed allows; every other encode feeds the decoded PNG.
    """
    return entry["format"] == images.JPEG and yuv != "400" and entry["jpeg_feed"]


def quality_group(yuv: str) -> str:
    """Return the encode group of a page encoded at this avifenc --yuv: gray (4:0:0) or colour."""
    return "gray" if yuv == "400" else "colour"


def route(fmts: Iterable[str]) -> str:
    """Chapter route from the set of page formats. An animated WebP is GIF class."""
    fmts = {images.GIF if f == images.WEBP_ANIMATED else f for f in fmts}
    if fmts <= AVIF_SOURCES:
        return "avif"
    if fmts <= AVIF_SOURCES | {images.WEBP_LOSSY} and fmts & AVIF_SOURCES:
        return "avif-or-untouched"  # mixed with lossy WebP: no lossless form unifies it
    if fmts <= UNTOUCHED_SOURCES:
        return "untouched"
    return "untouched-flag"  # GIF / AVIF / JXL / unknown mixed with something else


def _edits(entries: list[PlanPage], edit: ChapterEdit, layout: SeriesLayout, files: list[Path]) -> PlannedEdits:
    """Return the edit in page names (the bulk converter's planned-edit shape).

    Drops and bands first, then join and split (page layout); a strip chapter's strip spec over its non-cover pages with
    the edit's removed rows taken out. Raises EditError for an edit the junk stage never writes (SPEC "The chapter
    edit"), NeedsHuman for removed rows that leave nothing of the strip.
    """
    by_index = {q["index"]: q for q in entries}

    def name(i: int, what: str) -> str:
        if i not in by_index:
            msg = f"chapter_edit {what} names page {i}, the chapter has pages 0-{len(entries) - 1}"
            raise EditError(msg)
        return by_index[i]["name"]

    content = [q for q in entries if not q["cover"]]
    out: PlannedEdits = {
        "drop": [],
        "crop": [],
        "join": [],
        "split": None,
        "strip": None,
        "blank": _blanks(entries, edit, name),
    }
    if layout.kind == "strip":
        if edit["drop_pages"] or edit["bands"] or edit["join"] or edit["split"]:
            msg = "chapter_edit of a strip chapter may hold strip_removed only"
            raise EditError(msg)
        if content:  # its pages have their files, all in 1 folder
            out["strip"] = _strip_spec(content, edit, files[0].parent)
        elif edit["strip_removed"]:
            msg = f"chapter_edit strip_removed ends past the strip's 0 rows: {edit['strip_removed']!r}"
            raise EditError(msg)
        return out
    if edit["strip_removed"]:
        msg = "chapter_edit of a page chapter holds strip_removed"
        raise EditError(msg)
    out["drop"] = [{"page": name(i, "drop_pages")} for i in edit["drop_pages"]]
    out["crop"] = [{"page": name(b["page"], "bands"), "box": b["box"]} for b in edit["bands"]]
    _check_banded(entries, edit, by_index)
    _check_joined(entries, edit)
    out["join"] = [{"page": name(a, "join"), "sliver": name(b, "join")} for a, b in edit["join"]]
    if edit["split"]:
        out["split"] = _split_spec(content, edit, out["drop"], layout.page_aspect)
    return out


def _strip_spec(content: list[PlanPage], edit: ChapterEdit, x_dir: Path) -> StripSpec:
    """Return a strip chapter's strip spec ({"width", "pages", "kept", "cuts"}) over its non-cover pages (1 or more).

    The edit's removed rows taken out; x_dir: the folder of the pages' files. Raises EditError for rows past the strip,
    NeedsHuman when nothing is kept.
    """
    sizes = [q["size"] for q in content]
    rows = [strip.page_rows(images.open_flat(x_dir / q["name"])[0]) for q in content]
    st = strip.make_strip([x_dir / q["name"] for q in content], sizes, rows)
    past = [r for r in edit["strip_removed"] if r["end"] > st.height]
    if past:
        msg = f"chapter_edit strip_removed ends past the strip's {st.height} rows: {past!r}"
        raise EditError(msg)
    cut = strip.cut_strip(st.height, strip.find_gaps(st), edit["strip_removed"])
    if not cut["kept"]:
        msg = "the chapter edit drops every page"
        raise NeedsHuman(msg)
    pages: list[strip.NamedRows] = [
        {"name": q["name"], "top": p["top"], "th": p["th"]} for q, p in zip(content, st.pages, strict=True)
    ]
    return {"width": st.width, "pages": pages, "kept": cut["kept"], "cuts": cut["cuts"]}


def _check_banded(entries: list[PlanPage], edit: ChapterEdit, by_index: dict[int, PlanPage]) -> None:
    """Check that drops and bands spare cover-named pages.

    Bands name a page once, never a dropped one, with the box inside it.
    """
    banded = [b["page"] for b in edit["bands"]]
    if {q["index"] for q in entries if q["cover"]} & {*edit["drop_pages"], *banded}:
        msg = "chapter_edit drops or crops a cover-named page"
        raise EditError(msg)
    if len(set(banded)) < len(banded) or set(banded) & set(edit["drop_pages"]):
        msg = f"chapter_edit bands name a page twice or a dropped page: {edit['bands']!r}"
        raise EditError(msg)
    for b in edit["bands"]:
        (left, top, right, bottom), (w, h) = b["box"], by_index[b["page"]]["size"]
        if not (0 <= left < right <= w and 0 <= top < bottom <= h):
            msg = f"chapter_edit band box {b['box']} is not inside page {b['page']} ({w} x {h})"
            raise EditError(msg)


def _check_joined(entries: list[PlanPage], edit: ChapterEdit) -> None:
    """Join pairs chain each page to 1 sliver, spare dropped and cover-named pages, and never form a cycle."""
    pages_, slivers = [a for a, _ in edit["join"]], [b for _, b in edit["join"]]
    if len(set(pages_)) < len(pages_) or len(set(slivers)) < len(slivers) or any(a == b for a, b in edit["join"]):
        msg = f"chapter_edit join pairs must chain each page to 1 sliver: {edit['join']!r}"
        raise EditError(msg)
    unjoinable = set(edit["drop_pages"]) | {q["index"] for q in entries if q["cover"]}
    if unjoinable & {i for pair in edit["join"] for i in pair}:
        msg = f"chapter_edit join names a dropped or cover-named page: {edit['join']!r}"
        raise EditError(msg)
    attach = dict(zip(pages_, slivers, strict=True))
    for start in attach:  # a chain must end: a cycle would never stop joining
        seen, at = {start}, start
        while at in attach:
            at = attach[at]
            if at in seen:
                msg = f"chapter_edit join pairs form a cycle: {edit['join']!r}"
                raise EditError(msg)
            seen.add(at)


def _split_spec(
    content: list[PlanPage], edit: ChapterEdit, drops: list[Drop], page_aspect: list[float] | None
) -> StripSpec:
    """Return a split chapter's spec ({"width", "pages", "kept", "cuts"}).

    Its non-cover pages not dropped, cut at the series' page aspect. Raises EditError when the edit holds another
    page-layout edit.
    """
    if edit["drop_pages"] or edit["bands"] or edit["join"]:
        msg = "chapter_edit of a split chapter holds another page-layout edit"
        raise EditError(msg)
    dropped = {d["page"] for d in drops}
    kept = [q for q in content if q["name"] not in dropped]
    pl = strip.plan_pages([q["name"] for q in kept], [q["size"] for q in kept], page_aspect)
    return {
        "width": pl["width"],
        "pages": [{"name": p["name"], "top": p["top"], "th": p["th"]} for p in pl["pages"]],
        "kept": [[0, pl["height"]]],
        "cuts": list(pl["cuts"]),
    }


def _blanks(entries: list[PlanPage], edit: ChapterEdit, name: Callable[[int, str], str]) -> list[PlannedBlank]:
    """Return the edit's logo blanks in page names, checked.

    At most 1 per page, never on a dropped or cover-named page, the box inside its page (end-exclusive) and the fill 1
    RGB colour.
    """
    by_index = {q["index"]: q for q in entries}
    out: list[PlannedBlank]
    seen: set[int]
    out, seen = [], set()
    for b in edit["blank"]:
        page = name(b["page"], "blank")
        q = by_index[b["page"]]
        if b["page"] in seen or b["page"] in edit["drop_pages"] or q["cover"]:
            msg = f"chapter_edit blank on page {b['page']}: a repeated, dropped or cover-named page"
            raise EditError(msg)
        seen.add(b["page"])
        (left, top, right, bottom), (w, h) = b["box"], q["size"]
        if not (0 <= left < right <= w and 0 <= top < bottom <= h):
            msg = f"chapter_edit blank box {b['box']} is not inside page {b['page']} ({w} x {h})"
            raise EditError(msg)
        if not all(0 <= v <= images.CHANNEL_MAX for v in b["fill"]):
            msg = f"chapter_edit blank fill {b['fill']} is not an RGB colour"
            raise EditError(msg)
        out.append({"page": page, "box": list(b["box"]), "fill": list(b["fill"])})
    return out


def render_pages(plan: ChapterPlan) -> list[RenderPage]:
    """Return the rendered outcome's output pages (strip.render specs) of a plan with crops, joins, a split or a strip.

    Cover-named pages first under their own stems, then 001, 002, ... (strip / split rows in kept coordinates, or
    page-format pages with their crop boxes and joined slivers). Each gets the class of the source pages it is made of
    (the most colourful), its avifenc yuv (a colour page: the fullest chroma of its colour sources) and its source
    page names ("src").
    """
    e = plan.edits
    dropped = {d["page"] for d in e["drop"]}
    by_name = {q["name"]: q for q in plan.pages}

    def classed(src: list[str]) -> tuple[str, str]:
        """Return (class, yuv) of a page made of these source pages."""
        cls = max((plan.classes[x] for x in src), key=CLASS_RANK.__getitem__)
        colour = [by_name[x]["chroma"] for x in src if plan.classes[x] == images.COLOUR]
        return cls, yuv_for(cls, max(colour, default="420", key=CHROMA_RANK.__getitem__))

    out: list[RenderPage] = []
    for q in plan.pages:
        if q["cover"] and q["name"] not in dropped:
            cls, yuv = classed([q["name"]])
            out.append(
                {
                    "name": Path(q["name"]).stem,
                    "parts": [(q["name"], None)],
                    "src": [q["name"]],
                    "class": cls,
                    "yuv": yuv,
                }
            )
    st = e["strip"] or e["split"]
    if st:
        for n, (a, b) in enumerate(zip(st["cuts"], st["cuts"][1:], strict=False), 1):
            src = [
                p["name"]
                for s, t in strip.kept_ranges(st["kept"], a, b)
                for p in st["pages"]
                if p["top"] < t and s < p["top"] + p["th"]
            ]
            src = list(dict.fromkeys(src))
            cls, yuv = classed(src)
            out.append({"name": strip.output_name(n), "rows": [a, b], "src": src, "class": cls, "yuv": yuv})
    else:
        boxes = {cr["page"]: cr["box"] for cr in e["crop"]}
        attach = {j["page"]: j["sliver"] for j in e["join"]}
        slivers, n = set(attach.values()), 0
        for q in plan.pages:
            if q["cover"] or q["name"] in dropped or q["name"] in slivers:
                continue
            chain = [q["name"]]
            while chain[-1] in attach:  # a page cut into 3+ pieces joins every piece
                chain.append(attach[chain[-1]])
            n += 1
            cls, yuv = classed(chain)
            out.append(
                {
                    "name": strip.output_name(n),
                    "parts": [(x, boxes.get(x)) for x in chain],
                    "src": chain,
                    "class": cls,
                    "yuv": yuv,
                }
            )
    return out


def needs_conversion(fmts: Iterable[str]) -> bool:
    """Return whether a volume cover cannot match every page of these formats.

    True for a format with no cover encoding, or more than 1 format.
    """
    fmts = set(fmts)
    return bool(fmts - set(COVER_FORMATS)) or len(fmts) > 1


def cover_format(fmts: Iterable[str]) -> str | None:
    """Return the volume cover's encoding for pages of these formats: the 1 format they share, or None.

    None when needs_conversion. No pages count as AVIF.
    """
    fmts = set(fmts)
    if needs_conversion(fmts):
        return None
    return next(iter(fmts)) if fmts else images.AVIF


def routes(plan: ChapterPlan, *, cover: VolumeCover | None = None) -> None:
    """Set the plan's route, force_avif and outcomes.

    Each outcome a measured attempt, a failure moving to the next, with the volume cover it inserts in it ("cover": its
    format and its passed AVIF or its data; None when it takes none) and its expected pages ("pages_out", the cover
    included). cover: None (the chapter takes no volume cover), else the volume cover with its AVIF encode. An AVIF
    cover that failed the gate goes in no AVIF outcome. A rendered plan: AVIF under the floor, else the original pages
    minus drops. A plan that is not rendered, whose AVIF cover passed and whose non-cover pages share no cover format:
    AVIF under the floor, else untouched (force_avif).
    """
    kept = plan.kept()
    all_fmts = sorted({q["format"] for q in kept})
    src = sorted({q["format"] for q in kept if not q["cover"]})
    has_cover_page = any(Path(q["name"]).stem == archive.COVER_STEM for q in kept)

    def taken(cover_fmts: Iterable[str]) -> OutcomeCover | None:
        fmt = cover_format(cover_fmts) if cover is not None else None
        if cover is None or fmt is None:
            return None
        if fmt != images.AVIF:
            return {"format": fmt, "data": cover["data"], "width": cover["width"]}
        avif = cover["avif"]
        if avif["ok"] is False:  # the discriminant: a failed AVIF goes in no outcome
            return None
        return {"avif": avif}

    def outcome(name: str, gate: str | None, edits: str, cover_fmts: Iterable[str], pages: int) -> Outcome:
        inserted = taken(cover_fmts)
        replaced = 1 if inserted and has_cover_page else 0
        return {
            "outcome": name,
            "gate": gate,
            "edits": edits,
            "cover": inserted,
            "pages_out": pages + bool(inserted) - replaced,
        }

    n = len(kept)
    untouched = outcome("untouched", None, "drops", src, n)
    base = route(all_fmts)
    plan.force_avif = not plan.render and cover is not None and cover["avif"]["ok"] and needs_conversion(src)
    if plan.render:
        plan.outcomes = [outcome("avif", "floor", "all", [images.AVIF], len(plan.render)), untouched]
    elif plan.force_avif:
        plan.outcomes = [outcome("avif", "floor", "drops", [images.AVIF], n), untouched]
    elif base == "avif":
        avif = outcome("avif", "floor+size", "drops", [images.AVIF], n)
        plan.outcomes = [avif, outcome("lossless", None, "drops", src, n)]
    elif base == "avif-or-untouched":
        plan.outcomes = [outcome("avif", "floor+size", "drops", [images.AVIF], n), untouched]
    else:
        plan.outcomes = [untouched]
    plan.route = RENDERED if plan.render else "avif" if plan.force_avif else base


def avif_groups(plan: ChapterPlan) -> list[str]:
    """Return the encode groups of the pages a plan's AVIF outcome encodes, sorted; [] when its first is not AVIF."""
    if plan.outcomes[0]["outcome"] != "avif":
        return []
    if plan.render:
        yuvs = {r["yuv"] for r in plan.render}
    else:
        yuvs = {yuv_for(plan.classes[q["name"]], q["chroma"]) for q in plan.kept()}
    return sorted({quality_group(y) for y in yuvs})


def build(
    name: str, measured: Sequence[images.MeasuredPage], files: Sequence[Path], edit: ChapterEdit, layout: SeriesLayout
) -> ChapterPlan:
    """Build the ChapterPlan of 1 chapter.

    measured: images.measure_pages of the download; files: its pages' extracted files (pages.extract), in the same
    order; edit: check_edit's dict; layout: the series' SeriesLayout. Raises NeedsHuman (an EXIF rotation, a strip edit
    that removes every row) and EditError.
    Route and outcomes are set after, by routes: they depend on the volume cover's AVIF encode; the quality is set by
    the caller once calibration chose it.
    """
    files = [Path(f) for f in files]
    entries = [page_entry(p, f) for p, f in zip(measured, files, strict=True)]
    plan = ChapterPlan(
        name=name,
        pages=entries,
        edits=_edits(entries, edit, layout, files),
        classes={q["name"]: p["colour_class"] for q, p in zip(entries, measured, strict=True)},
    )
    e = plan.edits
    if e["crop"] or e["join"] or e["split"] or e["strip"]:
        plan.render = render_pages(plan)
    return plan
