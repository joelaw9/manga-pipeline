"""A library series' adoption (SPEC "State"): a series with no bulk-converter record takes its baseline from its files.

`prepare` does every read (Kavita's file list, each file's member list and `ComicInfo.xml`, the layout sample, the
volume basis) and writes nothing; `baseline.take` records its result in 1 transaction, and the census script reads it.
`plan` is the per-file rule, pure: a file read as a download is.
"""

import functools
import itertools
import os
import shutil
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from manga_pipeline import (
    archive,
    chapter_names,
    comicinfo,
    config,
    db,
    errors,
    kavita_link,
    names,
    preflight,
    rewrite_covers,
    strip,
    validation,
    volumes,
    workflow,
)
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients.kavita_match import normalize
from manga_pipeline.metadata import naming

SCRATCH_PREFIX = "adopt-"  # <paths.scratch>/adopt-<series id>-<pid>/: the layout sample
SCRATCH_KEEP_S = 86400  # a crashed adoption's sample folder is removed once a day old
SHOWN_PROBLEMS = 10  # problems a needs_human reason names; the rest are counted
CHECK_WAIT = "no baseline yet: it waits for its metadata check to complete"
DISCOVER_WAIT = "no baseline yet: it waits until none of its chapters is at discover"


class SampleError(ValueError):
    """A sampled page cannot be decoded."""


class UnreadableError(ValueError):
    """A sampled file cannot be read (not the share's fault)."""


@dataclass(frozen=True)
class Problem:
    """Why a series cannot be adopted (D7): `kind` groups it (the census), `text` names the file."""

    kind: str
    text: str


@dataclass(frozen=True)
class File:
    """An adopted file's published row, read as a download is (SPEC "State")."""

    library_path: str  # stored form
    identity: str
    kind: str  # names.MAIN or names.SIDE
    number: str  # ComicInfo text
    season: int | None
    volume: str | None  # its own volume, ComicInfo text
    title: str | None
    date: str | None
    pages_out: int  # its pages but a 000_cover page
    cover_state: str
    series: str | None  # its ComicInfo Series

    @property
    def source_name(self) -> str:
        """Its file stem, as a download's source name is."""
        return PurePosixPath(self.library_path).stem

    @property
    def volume_source(self) -> str | None:
        """`filename` for its own volume, else None."""
        return None if self.volume is None else naming.FILENAME

    def naming_row(self, order: int) -> naming.Row[naming.RowKey]:
        """Return the resolver's row of the file, a published one not stored yet."""
        return naming.Row(
            key=("adopted", order),
            settled=True,
            kind=self.kind,
            number=float(self.number),
            season=self.season,
            src_volume=self.volume,
            volume=names.to_volume(self.volume),
            volume_source=self.volume_source,
            marker_title=self.source_name,
            title=self.title or "",
            date=self.date,
            order=order,
        )


@dataclass(frozen=True)
class Prepared:
    """What an adoption records: its files, and the series' folder, layout and basis."""

    folder: str  # stored form
    name: str  # the series' ComicInfo name (series_name)
    files: list[File]  # the files no published row holds yet
    layout: tuple[str, str] | None  # (layout, series_conversion origin); None: the series has one
    basis: str | None  # None: the series has one


def waiting(conn: db.Connection, series_id: int) -> str | None:
    """Why the series is not adopted yet, or None: its metadata check has not completed, or a chapter is at discover.

    Database reads only, so every caller asks it before any file is read.
    """
    if not conn.execute(  # a completed check: metadata/check.py `completed`
        "SELECT 1 FROM series_check WHERE series_id = %s AND data_version > 0", (series_id,)
    ).fetchone():
        return CHECK_WAIT
    if conn.execute(
        "SELECT 1 FROM chapter WHERE series_id = %s AND stage = 'discover' AND status IN ('ready', 'claimed')",
        (series_id,),
    ).fetchone():
        return DISCOVER_WAIT
    return None


def reason(problems: Sequence[Problem]) -> str:
    """Return the needs_human reason naming a series' problems (at most SHOWN_PROBLEMS, the rest counted)."""
    shown = "; ".join(p.text for p in problems[:SHOWN_PROBLEMS])
    more = len(problems) - SHOWN_PROBLEMS
    return (
        f"no baseline can be taken: {shown}"
        + (f"; and {more} more" if more > 0 else "")
        + " -> fix them, rescan the library in Kavita"
    )


def prepare(ctx: workflow.Context, series: db.Row, folder: str | Problem) -> Prepared | list[Problem]:
    """Read everything the series' adoption records (steps 1-4), writing nothing; or the problems that hold it (D7).

    folder: folder_of's answer for the series, read once by the caller. A share error raises Unreachable, as does a
    missing file while the share is not mounted; a Kavita error reply raises as the client raises it.
    """
    _clean_scratch(ctx.cfg)
    with workflow.share_errors():
        listed = _listed(ctx, series, folder)
        if isinstance(listed, list):
            return listed
        folder, paths, problems = listed
        if series["folder"] is None:
            series = _with_folder(ctx.conn, series, folder)
        files = _read(ctx, paths, problems)
        problems += _same_chapters(files)
        name = series_name([f.series for f in files], series)
        if isinstance(name, Problem):
            return [*problems, name]
        if problems:
            return problems
        layout = _layout(ctx, series, files)
        if isinstance(layout, Problem):
            return [layout]
        basis = _basis(ctx, series, files)
        if isinstance(basis, Problem):
            return [basis]
    return Prepared(folder, name, files, layout, basis)


def _kavita_names(series: db.Row) -> list[str]:
    """Return the series' names Kavita lists: its name, localized name and original name, those it has."""
    return [n for n in (series["name"], series["localized_name"], series["original_name"]) if n]


def unlike_kavita(name: str, series: db.Row) -> str | None:
    """Return why a ComicInfo Series name would leave the series (none of its Kavita names under normalize), or None."""
    known = _kavita_names(series)
    if normalize(name) in {normalize(n) for n in known}:
        return None
    return f"{name!r} is none of Kavita's names ({', '.join(known)})"


def series_name(values: Sequence[str | None], series: db.Row) -> str | Problem:
    """Return the series' ComicInfo name from the Series its files carry (SPEC "State"), or why there is none.

    The value most files carry, by exact text; a tie: the one equal to Kavita's original name, else the first by sort.
    Values that differ under normalize, or a name none of Kavita's names, are a Problem; no value: Kavita's name.
    """
    counts = Counter(v for v in values if v)
    if not counts:
        return series["name"]
    if len({normalize(v) for v in counts}) > 1:
        return Problem(
            "series names", f"its files carry differing Series names: {', '.join(map(repr, sorted(counts)))}"
        )
    top = max(counts.values())
    tied = sorted(v for v, n in counts.items() if n == top)
    name = series["original_name"] if series["original_name"] in tied else tied[0]
    why = unlike_kavita(name, series)
    return name if why is None else Problem("series name", f"its files' Series name {why}")


def plan(path: str, members: list[str], xml: bytes | None) -> File | Problem:
    """Return a library file's published row, or why it cannot be adopted.

    path: its stored path; members: its archive's member names; xml: its ComicInfo.xml bytes or None. Its identity and
    number from its name and ComicInfo Number (the name first), its volume its own (the name's, else ComicInfo Volume),
    its title as a download's; a ComicInfo.xml that cannot be read gives no fields.
    """
    name = PurePosixPath(path).name
    try:
        fields = {k: v for k, v in comicinfo.read_all(xml).items() if comicinfo.field_ok(k, v)} if xml else {}
    except comicinfo.UNPARSEABLE:
        fields = {}
    item = names.parse(PurePosixPath(path).stem, fields.get("Number", ""))
    if item.number is None:
        return Problem("no number", f"{name}: no chapter number in its name or its ComicInfo Number")
    unclear = names.two_readings(item.number)
    if unclear:
        return Problem("two readings", f"{name}: {unclear}")
    pages = archive.archive_pages(members)
    content = [p for p in pages if not rewrite_covers.is_cover(p)]
    if not content:
        return Problem("no pages", f"{name}: no pages")
    own = names.to_volume(item.volume if item.volume is not None else fields.get("Volume"))
    volume = None if own is None else names.fmt_num(own)
    return File(
        library_path=path,
        identity=names.identity(item),
        kind=names.SIDE if names.side_label(item) else names.MAIN,
        number=names.fmt_num(item.number),
        season=int(float(item.season)) if item.season else None,
        volume=volume,
        title=names.chapter_title(item, fields.get("Title")) or None,
        date=names.ci_date(fields),
        pages_out=len(content),
        cover_state=rewrite_covers.member_state(members, volume),
        series=fields.get("Series"),
    )


def folder_of(ctx: workflow.Context, series: db.Row) -> str | Problem:
    """Return the series' folder in stored form: the stored one, else Kavita's (writing nothing); or why there is none.

    A Kavita error reply raises as the client raises it.
    """
    if series["folder"] is not None:
        return series["folder"]
    try:
        return kavita_link.library_folder(ctx.cfg, ctx.kavita.series_folder(series["kavita_series_id"]))
    except (kavita_client.FolderError, config.PathError, ValueError) as e:  # ValueError: a path under no map prefix
        return Problem("folder", str(e))


def _listed(
    ctx: workflow.Context, series: db.Row, folder: str | Problem
) -> tuple[str, list[str], list[Problem]] | list[Problem]:
    """Return (folder, the .cbz paths in it, the problems of the other listed files), or the series' own problem.

    folder: the stored one, else Kavita's (step 1: folder_of).
    """
    kavita_id = series["kavita_series_id"]
    if isinstance(folder, Problem):
        return [folder]
    try:
        listed = ctx.kavita.series_files(kavita_id)
    except ValueError as e:  # a path under no map prefix
        return [Problem("folder", str(e))]
    if not listed:
        return [Problem("no files", f"Kavita lists no file of series {kavita_id}")]
    paths: list[str] = []
    problems: list[Problem] = []
    for f in listed:
        try:
            stored = config.to_stored(ctx.cfg, f["path"])
        except config.PathError as e:
            problems.append(Problem("outside folder", str(e)))
            continue
        if str(PurePosixPath(stored).parent) != folder:
            problems.append(Problem("outside folder", f"{stored}: not in the series folder {folder}"))
        elif PurePosixPath(stored).suffix.lower() != ".cbz":
            problems.append(Problem("not cbz", f"{stored}: not a .cbz file"))
        else:
            paths.append(stored)
    return folder, paths, problems


def _with_folder(conn: db.Connection, series: db.Row, folder: str) -> db.Row:
    """Return the series row as it reads once its folder is stored, writing nothing.

    Its rules key the folder's name, as schema step 0040's trigger gives it, unless it has one. Its caller is the census
    (read only): baseline.take stores the folder before it adopts.
    """
    return db.one_row(
        conn,
        """SELECT (jsonb_populate_record(s, jsonb_build_object('folder', %s::text, 'rules_key', %s::text))).*
        FROM series s WHERE s.id = %s""",
        (folder, series["rules_key"] or PurePosixPath(folder).name, series["id"]),
    )


def _read(ctx: workflow.Context, paths: list[str], problems: list[Problem]) -> list[File]:
    """Plan each path no published row holds (step 2); a file's problem goes to problems."""
    held = {
        r[0] for r in ctx.conn.execute("SELECT library_path FROM published_file WHERE library_path = ANY(%s)", (paths,))
    }
    files: list[File] = []
    for path in sorted(set(paths) - held):
        local = config.to_local(ctx.cfg, path)
        try:
            with zipfile.ZipFile(local) as z:
                members = z.namelist()
                member = archive.comicinfo_member(members)
                xml = None if member is None else z.read(member)
        except OSError as e:
            _raise_if_share(ctx.cfg, e, path)
            if isinstance(e, FileNotFoundError):
                problems.append(Problem("missing", f"{path}: Kavita lists it, it is not on disk"))
            else:
                problems.append(Problem("unreadable", f"{path}: cannot be opened ({e})"))
            continue
        except archive.UNREADABLE as e:
            problems.append(Problem("unreadable", f"{path}: cannot be opened ({e})"))
            continue
        found = plan(path, members, xml)
        if isinstance(found, Problem):
            problems.append(found)
        else:
            files.append(found)
    return files


def _raise_if_share(cfg: config.Config, e: OSError, path: str) -> None:
    """Raise Unreachable("share") when an error reading a library file is the share's, not the file's.

    The share's: a dropped share, an errno or winerror of the share, or a missing file while the share is not mounted
    (Linux: its folders or owner file missing too).
    """
    if (
        errors.dropped_share(e)
        or e.errno in errors.SHARE_ERRNOS
        or getattr(e, "winerror", None) in errors.SHARE_WINERRORS
        or (isinstance(e, FileNotFoundError) and not _share_present(cfg))
    ):
        raise workflow.Unreachable(workflow.SHARE, f"{path}: {e}") from e


def _share_present(cfg: config.Config) -> bool:
    """Return whether the share is mounted: its folders there (preflight's share folders check) and its owner file."""
    return preflight.share_folders(cfg) is None and errors.exists(preflight.owner_file(cfg))


def _same_chapters(files: list[File]) -> list[Problem]:
    """Return a problem for each 2 files that are 1 chapter: 1 key, and volumes from the files that do not differ.

    As Library copies compares them: a side story has no volume of its own (chapter_names.download_volume), so its key
    alone tells it apart.
    """
    by_key: defaultdict[validation.ChapterKey, list[File]] = defaultdict(list)
    for f in files:
        by_key[validation.chapter_key(f.identity)].append(f)
    return [
        Problem("same chapter", f"{a.library_path} and {b.library_path} are 1 chapter")
        for group in by_key.values()
        for a, b in itertools.combinations(group, 2)
        if chapter_names.same_chapter(functools.partial(_own_volume, a), b.volume, b.volume_source)
    ]


def _own_volume(f: File) -> int | float | None:
    return names.to_volume(f.volume) if f.kind == names.MAIN else None


def _layout(ctx: workflow.Context, series: db.Row, files: list[File]) -> tuple[str, str] | Problem | None:
    """Return the layout to record (step 3): None when the series has one, its override file's, else measured."""
    if ctx.conn.execute("SELECT 1 FROM series_conversion WHERE series_id = %s", (series["id"],)).fetchone():
        return None
    overrides = config.abs_path(ctx.cfg["paths"]["naming_overrides"])
    try:
        wanted = volumes.override_layout(overrides, series["rules_key"])
    except ValueError as e:  # volumes.OverrideError, or a layout that is neither strip nor pages
        return Problem("override", f"override file of {series['rules_key']}: {e}")
    if wanted:
        return wanted, "override"
    if not files:
        return Problem("no files", "no file left to measure the layout over")
    return _measure(ctx, series["id"], files)


def _measure(ctx: workflow.Context, series_id: int, files: list[File]) -> tuple[str, str] | Problem:
    """Measure the layout over the content pages of the first, middle and last files by number (deduplicated).

    Only the pages the geometry check reads (the first strip.SAMPLE_PAGES content pages of each) are extracted, to a
    scratch folder removed in the end.
    """
    ordered = sorted(files, key=lambda f: (f.season or 0, float(f.number)))
    n = len(ordered)
    picked = list(dict.fromkeys(ordered[i].library_path for i in (0, n // 2, n - 1)))
    root = config.abs_path(ctx.cfg["paths"]["scratch"]) / f"{SCRATCH_PREFIX}{series_id}-{os.getpid()}"
    try:
        chapters = [_extract(ctx.cfg, path, root / str(k)) for k, path in enumerate(picked)]
        layout, _, _ = _classify(chapters)
    except UnreadableError as e:
        return Problem("unreadable", str(e))
    except (SampleError, *archive.UNREADABLE) as e:
        return Problem("undecodable", f"the layout sample ({', '.join(picked)}) cannot be decoded: {e}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return layout, "computed"


def _extract(cfg: config.Config, path: str, work: Path) -> list[Path]:
    """Write the library file's first content pages to work; return their paths in reading order.

    path: its stored path. UnreadableError for a file that cannot be read (gone since it was planned, say); a share
    error raises Unreachable, and an error writing to scratch raises as it is (the machine's, not the series').
    """
    try:
        with zipfile.ZipFile(config.to_local(cfg, path)) as z:
            pages = archive.archive_pages(z.namelist())
            read = [(i, z.read(pages[i])) for i in strip.content_indexes(pages)[: strip.SAMPLE_PAGES]]
    except OSError as e:
        _raise_if_share(cfg, e, path)
        msg = f"{PurePosixPath(path).name}: cannot be opened ({e})"
        raise UnreadableError(msg) from e
    work.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i, data in read:
        page = work / f"{i:04d}{PurePosixPath(pages[i]).suffix.lower()}"
        page.write_bytes(data)
        out.append(page)
    return out


def _classify(chapters: list[list[Path]]) -> tuple[str, str, list[str]]:
    try:
        return strip.classify_layout(chapters)
    except Exception as e:  # Pillow raises many types on a page it cannot read
        msg = str(e) or type(e).__name__
        raise SampleError(msg) from e


def _basis(ctx: workflow.Context, series: db.Row, files: list[File]) -> str | Problem | None:
    """Return the volume basis to record (step 4): None when the series has one, else chosen as a new series' is.

    Over its stored published files, the adopted ones and its open chapters.
    """
    if series["naming_basis"] is not None:
        return None
    try:
        rows, overrides, mangadex, platform = chapter_names.inputs(
            ctx, series, planned=[f.naming_row(i) for i, f in enumerate(files)]
        )
    except volumes.VolumeError as e:
        return Problem("override", f"override file of {series['rules_key']}: {e}")
    return naming.choose(rows, overrides, mangadex, platform)


def _clean_scratch(cfg: config.Config) -> None:
    """Remove the layout sample folders a day old: a crashed adoption's."""
    root = config.abs_path(cfg["paths"]["scratch"])
    if not root.is_dir():
        return
    old = time.time() - SCRATCH_KEEP_S
    for entry in root.iterdir():
        if entry.name.startswith(SCRATCH_PREFIX) and entry.is_dir() and entry.stat().st_mtime < old:
            shutil.rmtree(entry, ignore_errors=True)
