"""Convert's chapter notes (SPEC "Conversion", Result): the unusual cases of an encode, as status shows them.

Pure: built from the plan and image.process_chapter's record. The bulk converter's flags (optimizer/report.py,
optimizer/image.py, optimizer/plan.py, optimizer/pipeline.py), each as 1 note.
"""

import statistics
from collections.abc import Sequence
from typing import NamedTuple

from manga_pipeline import images
from manga_pipeline.convert import calibrate, image, plan

PREFIX = "convert: "  # every note of convert's starts so; a retried convert replaces its own (workflow.replace_notes)
DRIFT_MARGIN = 2  # quality-drift: a group's mean AVIF score under calibrate.MEAN_MIN by more than this
SIZE_SANITY = 0.10  # the staged CBZ under this share of the download CBZ
ANIMATED = (images.GIF, images.WEBP_ANIMATED)  # an AVIF of such a page holds its first frame only


class Sizes(NamedTuple):
    """The staged CBZ's bytes and the download CBZ's."""

    staged: int
    download: int


def notes(
    chapter_plan: plan.ChapterPlan,
    rec: image.ChapterRecord,
    *,
    sizes: Sizes,
    odd_chroma: Sequence[str],
    calibrated: Sequence[str],
) -> list[str]:
    """Return the chapter's convert notes, each starting with PREFIX ([] when nothing is unusual).

    odd_chroma: the kept colour JPEG pages an AVIF outcome encoded 4:4:4 because avifenc cannot keep their chroma
    subsampling (4:4:0, 4:1:1; the caller passes none for another outcome); a page an inserted volume cover replaced
    is left out of every note;
    calibrated: the lines of the calibration groups this chapter's samples fixed.
    """
    inserted = bool(rec["cover"])
    kept = [q for q in chapter_plan.kept() if not image.replaced_by_cover(q["name"], inserted=inserted)]
    odd_chroma = [n for n in odd_chroma if not image.replaced_by_cover(n, inserted=inserted)]
    found = [
        *_rejected(rec),
        *_drift(rec, inserted=inserted),
        *_irregular(chapter_plan),
        *_small(sizes),
        *_untouched_flag(rec),
        *_odd_chroma(odd_chroma),
        *_mixed_lossless(rec, kept),
        *_animated(rec, kept),
        *calibrated,
    ]
    return [PREFIX + n for n in found]


def _rejected(rec: image.ChapterRecord) -> list[str]:
    why = rec.get("avif_rejected")
    if why is None:
        return []
    what = "its rendered pages lossless" if rec["edits"] == "all" else rec["outcome"]
    return [f"AVIF rejected ({why}): published {what}"]


def _drift(rec: image.ChapterRecord, *, inserted: bool) -> list[str]:
    """Note each group whose mean AVIF score is under MEAN_MIN - DRIFT_MARGIN at a quality under the ceiling."""
    if rec["outcome"] != "avif":
        return []
    by_group: dict[str, list[float]] = {}
    for p in rec.get("avif_pages", []):
        if image.replaced_by_cover(p["name"], inserted=inserted):  # encoded, then replaced by the volume cover
            continue
        by_group.setdefault(plan.quality_group(p["yuv"]), []).append(p["score"])
    out: list[str] = []
    for group, scores in sorted(by_group.items()):
        q, mean = rec["quality"][group], round(statistics.fmean(scores), 2)
        if q < image.AVIF_QUALITY and mean < calibrate.MEAN_MIN - DRIFT_MARGIN:
            out.append(f"quality-drift: {group} AVIF mean score {mean} < {calibrate.MEAN_MIN - DRIFT_MARGIN} at q{q}")
    return out


def _irregular(chapter_plan: plan.ChapterPlan) -> list[str]:
    split = chapter_plan.edits["split"]
    if split is None:
        return []
    if split["resliced"] is not None:
        return [f"split: re-sliced into {split['resliced']} pages"]
    if not split["irregular"]:
        return []
    return [f"split: {len(split['irregular'])} irregular image(s): {', '.join(split['irregular'])}"]


def _small(sizes: Sizes) -> list[str]:
    if sizes.staged >= SIZE_SANITY * sizes.download:
        return []
    return [f"staged file {sizes.staged} bytes < {SIZE_SANITY:.0%} of the download's {sizes.download}"]


def _untouched_flag(rec: image.ChapterRecord) -> list[str]:
    if rec["route"] != "untouched-flag":
        return []
    return [f"untouched-flag: formats {'+'.join(sorted(rec['formats']))} mixed"]


def _odd_chroma(names: Sequence[str]) -> list[str]:
    if not names:
        return []
    return [f"JPEG chroma subsampling encoded as 4:4:4: {', '.join(names)}"]


def _mixed_lossless(rec: image.ChapterRecord, kept: Sequence[plan.PlanPage]) -> list[str]:
    fmts = {q["format"] for q in kept}
    if rec["outcome"] != "lossless" or rec["edits"] == "all" or len(fmts) <= 1:  # rendered pages: PNGs
        return []
    return [f"lossless chapter stays mixed-format: {'+'.join(sorted(fmts))}"]


def _animated(rec: image.ChapterRecord, kept: Sequence[plan.PlanPage]) -> list[str]:
    """Note the animated pages a volume cover took to AVIF (force_avif: the avif route, a cover inserted)."""
    names = [q["name"] for q in kept if q["format"] in ANIMATED]
    if rec["outcome"] != "avif" or rec["route"] != "avif" or not rec["cover"] or not names:
        return []
    return [f"animated pages converted to 1 frame for the volume cover: {', '.join(names)}"]
