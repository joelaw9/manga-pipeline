"""Source review's samples (SPEC "Source review", Samples): `source sample`, which judges a manga's copies.

A sample's pages come through Suwayomi's reader API into a CBZ under <paths.scratch>/source/, never the downloads
folder; it is checked as validate checks a download, scanned and decided as the junk stage does with the series'
stored decisions, writing nothing, and judged by the better-copy rule. Returns the lines it prints.
"""

import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from manga_pipeline import (
    config,
    db,
    images,
    library_copy,
    measures,
    redownload,
    source_review,
    validation,
    workflow,
)
from manga_pipeline.clients import suwayomi
from manga_pipeline.junk import candidates, chapter_scan, ocr, store
from manga_pipeline.replies import suwayomi as suwayomi_replies

log = logging.getLogger(__name__)
FOLDER = "source"  # below paths.scratch: every sample's folder
# a page's file name extension by its format; an unknown format keeps an image name, so validation reports it
EXTENSIONS = {
    images.JPEG: ".jpg",
    images.PNG: ".png",
    images.GIF: ".gif",
    images.BMP: ".bmp",
    images.WEBP_LOSSY: ".webp",
    images.WEBP_LOSSLESS: ".webp",
    images.WEBP_ANIMATED: ".webp",
    images.AVIF: ".avif",
    images.JXL: ".jxl",
}


@dataclass(frozen=True)
class Sampling:
    """What every chapter of 1 sample is judged with: the series' stored state and the manga's chapters."""

    ctx: workflow.Context
    series: db.Row
    layout: str
    folder: Path  # this sample's folder
    files: list[db.Row]  # the series' confirmed published files, in chapter order
    listed: list[suwayomi_replies.Chapter]  # the manga's chapters
    known: list[validation.Published]  # what validation compares a sample with


def sample(ctx: workflow.Context, series_id: int, manga_id: int, n: int) -> list[str]:
    """Judge the manga's copies of the series' latest n files of a run; return the lines to print.

    Empties <paths.scratch>/source/ first (a killed run's files never stay) and deletes the sample's folder at the
    end, also after a failure.
    """
    if n < 1:
        msg = f"--chapters must be at least 1, got {n}"
        raise workflow.Refused(msg)
    series = source_review.reviewed(ctx, series_id)
    layout = source_review.layout(ctx.conn, series_id)
    if layout is None:
        msg = f"series {series_id} has no layout yet"
        raise workflow.Refused(msg)
    found, _ = source_review.manga(ctx, manga_id)
    files = source_review.published(ctx.conn, series_id)
    picked = source_review.sample_files(files, n)
    if not picked:
        msg = f"series {series_id} has no run of at least {source_review.MIN_RUN} published files to sample"
        raise workflow.Refused(msg)
    root = config.abs_path(ctx.cfg["paths"]["scratch"]) / FOLDER
    if root.exists():
        shutil.rmtree(root)
    folder = root / f"{series_id}-{manga_id}"
    folder.mkdir(parents=True)
    try:
        listed = source_review.chapters(ctx.suwayomi, found)
        s = Sampling(ctx, series, layout, folder, files, listed, _known(ctx.conn, series_id, files))
        out = [f"series {series_id} {series['name']} ({layout}) against manga {manga_id} {found['title']}"]
        for row in picked:
            out += judge(s, row)
    finally:
        shutil.rmtree(folder)
    out.append(
        "junk counts only what the series' stored decisions remove: a lower bound; weigh the units listed for review"
    )
    return out


def _known(conn: db.Connection, series_id: int, files: list[db.Row]) -> list[validation.Published]:
    """Return the series' published files as validation compares a download with them."""
    phashes = {
        r["id"]: r["phashes"]
        for r in conn.execute(
            """SELECT id, ARRAY(SELECT jsonb_array_elements_text(page_phashes::jsonb)) AS phashes
            FROM published_file WHERE series_id = %s AND page_phashes IS NOT NULL""",
            (series_id,),
        )
    }
    return [
        {
            "key": validation.chapter_key(r["identity"]),
            "page_phashes": phashes.get(r["id"]),
            "strip_length": r["strip_length"],
        }
        for r in files
    ]


def judge(s: Sampling, row: db.Row) -> list[str]:
    """Return the lines of 1 published file's sample: no match, a defect, or both copies and the rule's outcome."""
    head = f"file {row['id']} {source_review.key_text(row['identity'])}"
    ref = source_review.reference(s.files)
    hit = redownload.pick_chapter(row["identity"], source_review.number(row["identity"]) or 0.0, ref, s.listed)
    if hit is None:
        return [f"{head}: no match on the manga: left out"]
    head += f" / chapter {hit['id']} {hit['name']}"
    try:
        cbz, paths = _fetch(s, row["id"], hit["id"])
    except suwayomi.SourceUnavailableError as e:
        return [f"{head}: the source cannot serve it: {str(e).splitlines()[0]}: left out"]
    verdict = validation.check(cbz, validation.chapter_key(row["identity"]), s.known)
    if verdict.kind != "ok":
        return [f"{head}: {verdict.reason}: left out"]
    applied = _junk(s, paths, verdict.pages)
    if applied.problem:
        return [f"{head}: its junk edit is refused ({applied.problem}): a person decides"]
    candidate = library_copy.Copy(
        library_copy.measured(head, measures.summary(verdict.pages), measures.bits_per_pixel(verdict.pages)),
        library_copy.rating(suwayomi.chapter_file(hit["scanlator"], hit["name"]).removesuffix(".cbz")),
        measures.junk(applied.edit, s.layout, measures.summary(verdict.pages)["median_page_width"] or 0),
    )
    published = source_review.measure_file(s.ctx, row, s.layout)
    outcome = library_copy.decide(
        candidate, library_copy.Copy(published.measures, published.rating, published.junk), s.layout
    )
    out = [
        f"{head}: {_outcome(outcome)}",
        f"  candidate: {_copy_text(candidate.measures, candidate.junk, candidate.rating)}",
        f"  published: {_copy_text(published.measures, published.junk, published.rating)}",
        *(f"  note: {note}" for note in verdict.notes),
    ]
    picks = candidates.select(
        s.ctx.conn, store.series_key(s.series), row["identity"], applied.undecided, ocr.word_lists(s.ctx.cfg["junk"])
    )
    out += [f"  for review: {p['kind']} ({p['reason']})" for p in picks]
    return out


def _fetch(s: Sampling, file_id: int, chapter_id: int) -> tuple[Path, list[Path]]:
    """Write a chapter's pages, read through Suwayomi's reader API, as files and as a CBZ in the sample's folder.

    Returns (the CBZ, the page files in reading order).
    """
    work = s.folder / str(file_id)
    work.mkdir()
    paths: list[Path] = []
    for i, url in enumerate(s.ctx.suwayomi.chapter_pages(chapter_id)):
        data = s.ctx.suwayomi.page(url)
        path = work / f"{i + 1:04d}{EXTENSIONS.get(images.detect(data), '.jpg')}"
        path.write_bytes(data)
        paths.append(path)
    cbz = s.folder / f"{file_id}.cbz"
    with zipfile.ZipFile(cbz, "w") as z:
        for path in paths:
            z.write(path, path.name)
    log.info("sampled chapter %s: %s pages", chapter_id, len(paths))
    return cbz, paths


def _junk(s: Sampling, paths: list[Path], pages: list[images.MeasuredPage]) -> chapter_scan.Applied:
    """Scan the sample's pages and apply the series' stored decisions, as the junk stage does, writing nothing."""
    conn, key = s.ctx.conn, store.series_key(s.series)
    decisions = store.Decisions(conn, key)
    extracted = chapter_scan.Extracted(paths, [p.name for p in paths], [(m["width"], m["height"]) for m in pages])
    sc: chapter_scan.Scan
    if s.layout == "strip":
        sc = chapter_scan.scan_strip(extracted, decisions, s.ctx.cfg["junk"]["ocr_device"])
    else:
        aspects, _ = chapter_scan.page_aspects(conn, s.series["id"], extracted.sizes)
        sc = chapter_scan.scan_pages(extracted, [m["junk_signature"] is None for m in pages], aspects)

    def decide_unit(kind: str, text: str) -> db.Row | None:
        return decisions.decide(kind, store.parse_key(kind, text))

    known = conn.execute("SELECT key FROM junk_watermark WHERE series_key = %s", (key,)).fetchall()
    return chapter_scan.apply(sc, decide_unit, [r["key"] for r in known])


def _outcome(verdict: library_copy.Better | library_copy.Duplicate | library_copy.NotSameChapter | None) -> str:
    match verdict:
        case library_copy.Better():
            return "better: would replace"
        case library_copy.Duplicate(reason):
            return f"duplicate: would be dropped ({reason})"
        case library_copy.NotSameChapter(reason):
            return f"not the same chapter: a person decides ({reason})"
        case None:
            return "not decided: its junk is not known"


def _copy_text[B: float | None](m: library_copy.Measures[B], junk: float | None, rating: int) -> str:
    bpp = "-" if m.bpp is None else f"{m.bpp:.2f}"
    junk_text = "-" if junk is None else f"{junk:.4g}"
    return (
        f"pages {m.pages_in}, width {m.median_page_width}, strip length {m.strip_length}, bits per pixel {bpp}, "
        f"junk {junk_text}, rating {rating}"
    )
