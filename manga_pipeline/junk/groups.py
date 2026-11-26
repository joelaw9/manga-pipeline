"""Groups of near-identical candidates in a review's listing, and their sheets (SPEC "Junk decisions", review).

Within 1 series and 1 signature kind, candidates whose keys are near (images.matches at GROUP_DISTANCE and
GROUP_ASPECT) are linked, and the chains form groups (single link). A group of 2 or more is shown on sheets: grids of
its members' unit images, each labelled with its candidate id, every sheet within the model's image bounds
(SHEET_LONG_EDGE, SHEET_PIXELS) so it is read without being scaled down. Frames and logos are never grouped. A
rest-of-card bundle's listed members (review.listed_bundles) are shown on sheets the same way, written with the groups'.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import config, db, images
from manga_pipeline.junk import candidates, store

GROUP_DISTANCE = 10  # the bulk converter's review.group_threshold
GROUP_ASPECT = 0.10  # the bulk converter's review.group_aspect_tolerance
SHEET_LONG_EDGE = 1568  # px: the model reads an image this long on its long edge without scaling it down
SHEET_PIXELS = 1_150_000  # nor one of more pixels than this
PER_ROW = 4  # members side by side on a sheet
LABEL_PX = 28  # px of the label row under each member
FOLDER_PREFIX = "review-"  # a review's sheets folder: <paths.candidates>/review-<review id>


def near_groups(rows: Sequence[db.Row]) -> list[list[db.Row]]:
    """Return the groups of 2 or more near candidates among rows, each in the rows' order, in order of first member."""
    parent = list(range(len(rows)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    sigs = [images.sig_from_str(r["key"]) if r["kind"] in store.SIGNATURE_KINDS else None for r in rows]
    for i, (a, sa) in enumerate(zip(rows, sigs, strict=True)):
        for j in range(i + 1, len(rows)):
            b, sb = rows[j], sigs[j]
            if sa is None or sb is None or (a["series_key"], a["kind"]) != (b["series_key"], b["kind"]):
                continue
            if images.matches(sa, sb, GROUP_DISTANCE, GROUP_ASPECT):
                parent[root(j)] = root(i)
    members: dict[int, list[db.Row]] = {}
    for i, r in enumerate(rows):
        members.setdefault(root(i), []).append(r)
    return [g for g in members.values() if len(g) > 1]


def folder(cfg: config.Config, review_id: str) -> Path:
    """Return the folder of a review's sheets, in the candidates folder (Claude may read only there)."""
    return config.abs_path(cfg["paths"]["candidates"]) / f"{FOLDER_PREFIX}{review_id}"


def _cell(cfg: config.Config, row: db.Row, width: int, height: int) -> Image.Image:
    """Return a member's unit image scaled to fit width x height (never enlarged)."""
    with Image.open(candidates.folder(cfg, row["folder"]) / "unit.png") as im:
        unit = im.convert("RGB")
    scale = min(width / unit.width, height / unit.height, 1.0)
    return unit.resize((max(1, round(unit.width * scale)), max(1, round(unit.height * scale))))


def sheets(cfg: config.Config, review_id: str, name: str, members: Sequence[db.Row]) -> list[Path]:
    """Write the sheets of members named `name` (group-<n>, bundle-<n>), <name>-<m>.png; return their paths in order.

    Each sheet holds up to PER_ROW members a row, each under SHEET_LONG_EDGE / PER_ROW px wide and labelled with its
    id; a new sheet starts when the next row would pass SHEET_LONG_EDGE or SHEET_PIXELS.
    """
    cols = min(PER_ROW, len(members))
    cell_w = SHEET_LONG_EDGE // PER_ROW
    sheet_w = cols * cell_w
    max_h = min(SHEET_LONG_EDGE, SHEET_PIXELS // sheet_w)
    cells = [_cell(cfg, r, cell_w, max_h - LABEL_PX) for r in members]
    rows = [list(range(i, min(i + cols, len(members)))) for i in range(0, len(members), cols)]
    pages: list[list[list[int]]] = [[]]
    used = 0
    for row in rows:
        h = max(cells[i].height for i in row) + LABEL_PX
        if pages[-1] and used + h > max_h:
            pages.append([])
            used = 0
        pages[-1].append(row)
        used += h
    font = ImageFont.load_default(size=LABEL_PX - 8)
    dest = folder(cfg, review_id)
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for m, page in enumerate(pages, 1):
        heights = [max(cells[i].height for i in row) + LABEL_PX for row in page]
        sheet = Image.new("RGB", (sheet_w, sum(heights)), "white")
        draw = ImageDraw.Draw(sheet)
        top = 0
        for row, h in zip(page, heights, strict=True):
            for col, i in enumerate(row):
                sheet.paste(cells[i], (col * cell_w, top))
                draw.text((col * cell_w + 4, top + h - LABEL_PX + 4), f"id {members[i]['id']}", fill="red", font=font)
            top += h
        path = dest / f"{name}-{m}.png"
        images.save_png(sheet, path)
        out.append(path)
    return out


def write_sheets(
    cfg: config.Config, review_id: str, named: Sequence[tuple[str, Sequence[db.Row]]]
) -> list[tuple[Sequence[db.Row], list[Path]]]:
    """Write the sheets of every named member set (group-<n>, bundle-<n>) for a review, its folder emptied first.

    Returns each set's members with its sheets, in order.
    """
    shutil.rmtree(folder(cfg, review_id), ignore_errors=True)  # a second list in 1 review writes them anew
    return [(members, sheets(cfg, review_id, name, members)) for name, members in named]


def remove_all(cfg: config.Config) -> None:
    """Remove every review's sheets: junk review runs it at its start and end (1 review in the fleet at a time)."""
    for sheets_folder in config.abs_path(cfg["paths"]["candidates"]).glob(f"{FOLDER_PREFIX}*"):
        shutil.rmtree(sheets_folder, ignore_errors=True)
