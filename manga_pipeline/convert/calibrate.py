"""AVIF quality per series and encode group (SPEC "Calibration").

The bulk converter's rule (optimizer/calibrate.py: the ladder, the pass rule, the choice), applied to samples gathered a
few chapters at a time: every sample page is encoded and scored at q80 and at each ladder step, the scores are stored,
and the quality is chosen from them once enough are stored.

Rows: series_calibration (schema 0030). A group with no row is open, with no samples.
"""

import json
import statistics
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from manga_pipeline import archive, db
from manga_pipeline.convert import image, plan, record_reader

CEILING = image.AVIF_QUALITY  # 80: an open group's quality, and the q a sample must pass to be counted
LADDER = (75, 70, 65, 60, 55, 50)  # probed qualities, highest first
PER_CHAPTER = 14  # sample pages a chapter adds per group (the bulk converter's 40 pages over 3 chapters)
MAX_SAMPLES = 40  # a group holding this many samples takes no more
MIN_COUNTED = 20  # samples passing at CEILING needed to fix a group, and to pass a probe
MIN_CHAPTERS = 3  # chapters the samples come from before a group is fixed
MEAN_MIN = 85  # a probe's mean score at least this


class Unit(plan.PageClass):
    """A page a plan's AVIF outcome encodes, as calibration samples it: its file, sha1, class and encode settings.

    rendered: a rendered PNG (path in the render folder), else a page file of the download.
    """

    path: Path
    sha1: str
    yuv: str
    fmt: str
    jpeg_feed: bool
    rendered: bool


# A sample's scores: {"<q>": [score, AVIF bytes]} at CEILING and every LADDER step.
type Scores = dict[str, list[float]]


class NewSample(TypedDict):
    """A probed unit as a chapter adds it: its sha1, whether it was rendered, its source bytes and its scores."""

    sha1: str
    rendered: bool
    bytes_in: int
    scores: Scores


class Sample(NewSample):
    """A stored sample: a NewSample and the chapter it came from."""

    chapter_id: int


class Group(TypedDict):
    """A stored calibration group: its quality, fixed (1) or open (0), origin ("record", "computed"), why, samples."""

    q: int
    fixed: int
    origin: str
    why: str
    samples: list[Sample]


def _samples(text: str) -> list[Sample]:
    """Read a series_calibration row's samples (JSON text); unchecked."""
    return json.loads(text)


def rows(conn: db.Connection, series_id: int) -> dict[str, Group]:
    """{group: {"q", "fixed", "origin", "why", "samples"}} of the series' stored groups."""
    out: dict[str, Group] = {}
    for r in conn.execute("SELECT * FROM series_calibration WHERE series_id = %s", (series_id,)):
        out[r["grp"]] = {
            "q": r["q"],
            "fixed": r["fixed"],
            "origin": r["origin"],
            "why": r["why"],
            "samples": _samples(r["samples"]),
        }
    return out


def quality(stored: Mapping[str, Group]) -> dict[str, int]:
    """{group: q}: a fixed group's quality, else CEILING."""
    return {g: stored[g]["q"] if g in stored and stored[g]["fixed"] else CEILING for g in plan.QUALITY_GROUPS}


def _spaced[T](units: Sequence[T], n: int) -> list[T]:
    """At most n of these units, evenly spaced in order."""
    if len(units) <= n:
        return list(units)
    return [units[(2 * i + 1) * len(units) // (2 * n)] for i in range(n)]


def units(chapter_plan: plan.ChapterPlan, x_dir: Path, rendered: Sequence[Path]) -> dict[str, list[Unit]]:
    """Return {group: [unit]} of the pages a plan's AVIF outcome encodes.

    Its page files in x_dir, or its rendered PNGs when it renders, minus cover-named pages and rendered pages made from
    them (as the bulk converter's calib_units): {"path", "sha1", "class", "yuv", "fmt", "jpeg_feed", "rendered"}. Empty
    when its first outcome is not AVIF.
    """
    if chapter_plan.outcomes[0]["outcome"] != "avif":
        return {}
    covers = {q["name"] for q in chapter_plan.pages if q["cover"]}
    out: dict[str, list[Unit]] = {g: [] for g in plan.QUALITY_GROUPS}
    if chapter_plan.render:
        for path, r in zip(rendered, chapter_plan.render, strict=True):
            if covers & set(r["src"]):
                continue
            sha1 = archive.sha1(Path(path).read_bytes())
            out[plan.quality_group(r["yuv"])].append(
                {
                    "path": Path(path),
                    "sha1": sha1,
                    "class": r["class"],
                    "yuv": r["yuv"],
                    "fmt": image.RENDERED_FMT,
                    "jpeg_feed": False,
                    "rendered": True,
                }
            )
        return out
    x_dir = Path(x_dir)
    for q in chapter_plan.kept():
        if q["cover"]:
            continue
        cls = chapter_plan.classes[q["name"]]
        yuv = plan.yuv_for(cls, q["chroma"])
        out[plan.quality_group(yuv)].append(
            {
                "path": x_dir / q["name"],
                "sha1": q["sha1"],
                "class": cls,
                "yuv": yuv,
                "fmt": q["format"],
                "jpeg_feed": plan.feeds_jpeg(q, yuv),
                "rendered": False,
            }
        )
    return out


def pick(group_units: Sequence[Unit], sampled: Collection[str]) -> list[Unit]:
    """Pick up to PER_CHAPTER units (fewer when the group nears MAX_SAMPLES), evenly spaced over the unsampled ones.

    Unsampled: those whose sha1 is not among the sampled ones; of pages with 1 sha1 (a repeated page) only the first
    counts.
    """
    room = min(PER_CHAPTER, MAX_SAMPLES - len(sampled))
    fresh: list[Unit] = []
    seen = set(sampled)
    for u in group_units:
        if u["sha1"] not in seen:
            seen.add(u["sha1"])
            fresh.append(u)
    return _spaced(fresh, room) if room > 0 else []


def probe(encoder: image.Encoder, picked: Sequence[Unit], work_dir: Path) -> dict[str, Scores]:
    """Encode each picked unit at CEILING and at every LADDER step, no retry, and score it.

    Returns {sha1: {"<q>": [score, bytes_out]}}. Work files in work_dir/q<q>/. encoder: the run's image.Encoder.
    """
    scores: dict[str, Scores] = {u["sha1"]: {} for u in picked}
    for q in (CEILING, *LADDER):
        tmp = Path(work_dir) / f"q{q}"
        tmp.mkdir(parents=True, exist_ok=True)
        tasks: list[image.PageTask] = [
            {
                "src": str(u["path"]),
                "out": str(tmp / f"{Path(u['path']).stem}.avif"),
                "tmp": str(tmp),
                "fmt": u["fmt"],
                "cls": u["class"],
                "yuv": u["yuv"],
                "jpeg_feed": u["jpeg_feed"],
                "q": q,
                "retry_q": None,
            }
            for u in picked
        ]
        recs, _ = encoder.avif_pages(tasks)
        for u, r in zip(picked, recs, strict=True):
            scores[u["sha1"]][str(q)] = [r["score"], r["bytes_out"]]
    return scores


def choose(samples: Sequence[Sample]) -> tuple[int, str] | None:
    """Return (q, why) once the samples can fix the group, else None.

    They can with MIN_COUNTED samples scoring at least the floor at CEILING (the others are left out of every probe),
    and samples, counted or not, from MIN_CHAPTERS chapters. Down the LADDER, a probe fails with fewer than MIN_COUNTED
    counted samples, a mean below MEAN_MIN or a minimum below the floor; the quality is the last step before the first
    failure, else CEILING.
    """
    floor = image.SCORE_FLOOR
    counted = [s for s in samples if s["scores"][str(CEILING)][0] >= floor]
    if len(counted) < MIN_COUNTED or len({s["chapter_id"] for s in samples}) < MIN_CHAPTERS:
        return None
    q = CEILING
    for step in LADDER:
        scores = [s["scores"][str(step)][0] for s in counted]
        mean = round(statistics.fmean(scores), 2)
        fails = [f"mean {mean} < {MEAN_MIN}"] * (mean < MEAN_MIN) + [f"min {min(scores)} < {floor}"] * (
            min(scores) < floor
        )
        if fails:
            return q, f"q{step} fails: {', '.join(fails)} ({len(counted)} samples)"
        q = step
    return q, f"ladder floor q{q} passes ({len(counted)} samples)"


def add(
    conn: db.Connection,
    chapter: db.Row,
    group: str,
    new: Iterable[NewSample],
    *,
    guard: Callable[[db.Connection], object],
) -> Group:
    """Store a chapter's samples of its series' open group and fix the group when choose allows.

    new: [{"sha1", "rendered", "bytes_in", "scores"}]. In 1 transaction that reads the row again: nothing is added when
    the group is fixed by now or the chapter has added samples before (a retried chapter, a second run). guard(conn)
    runs first in it and raises to add nothing (the stage's claim check). Returns the group's row as stored.
    """
    series_id, chapter_id = chapter["series_id"], chapter["id"]
    with db.transaction(conn):
        guard(conn)
        row = conn.execute(
            "SELECT q, fixed, origin, why, samples FROM series_calibration WHERE series_id = %s AND grp = %s",
            (series_id, group),
        ).fetchone()
        samples = _samples(row["samples"]) if row else []
        if row is not None and (row["fixed"] or any(s["chapter_id"] == chapter_id for s in samples)):
            return {
                "q": row["q"],
                "fixed": row["fixed"],
                "origin": row["origin"],
                "why": row["why"],
                "samples": samples,
            }
        known = {s["sha1"] for s in samples}
        samples.extend({**s, "chapter_id": chapter_id} for s in new if s["sha1"] not in known)
        chosen = choose(samples)
        q, fixed, why = (chosen[0], 1, chosen[1]) if chosen else (CEILING, 0, f"open: {len(samples)} samples")
        conn.execute(
            """INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)
            VALUES (%s, %s, %s, %s, 'computed', %s, %s)
            ON CONFLICT (series_id, grp) DO UPDATE SET q = excluded.q, fixed = excluded.fixed, why = excluded.why,
                samples = excluded.samples""",
            (series_id, group, q, fixed, why, json.dumps(samples)),
        )
        return {"q": q, "fixed": fixed, "origin": "computed", "why": why, "samples": samples}


def from_record(record: record_reader.Record) -> dict[str, tuple[int, str]]:
    """Return the fixed groups of a bulk-converter record ({group: (q, why)}): those it probed (non-empty probes).

    Whatever scorer it was made with. Unprobed and absent groups are left open.
    """
    groups = record["series"]["calibration"].get("groups", {})
    return {
        g: (v["q"], f"record: {v.get('why', '')}".rstrip(": "))
        for g, v in groups.items()
        if g in plan.QUALITY_GROUPS and v.get("probes")
    }
