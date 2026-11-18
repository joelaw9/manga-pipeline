"""Volume covers of published files (SPEC "Retroactive updates"): their states, a rewrite's convert, its Page entries.

What cover each file holds (`fill_states`), a rewrite chapter's convert (`convert_run`: the library file's pages copied
byte for byte, its cover inserted or dropped) and the `Page` entries that follow (`shift_pages`).

A cover is encoded as a new chapter's is (convert/covers.py, image.Encoder.cover); no other page is re-encoded.
"""

import json
import logging
import shutil
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TypedDict

from manga_pipeline import archive, config, db, images, names, workflow
from manga_pipeline.convert import covers, plan, record_reader
from manga_pipeline.stages import convert

log = logging.getLogger(__name__)
NONE = "none"


class EncodedPage(covers.InsertedCover):
    """The cover page _encode made: what covers.insert wrote, its bytes and its size."""

    data: bytes
    width: int
    height: int


class InsertedPage(covers.InsertedCover):
    """The cover a rewrite's convert put in (its encoded page less its bytes) and the volume it is the cover of."""

    width: int
    height: int
    volume: str | None


class _Result(TypedDict):
    """What every rewrite convert_result holds: why no cover was put in, the dropped cover's index, the new state."""

    cover_skipped: str | None
    dropped_index: int | None
    cover_state: str


class CoverPut(_Result):
    """A rewrite convert_result whose convert put a cover in: the cover, and its index among the file's pages."""

    cover: InsertedPage
    inserted_index: int


class NoCoverPut(_Result):
    """A rewrite convert_result whose convert put no cover in."""

    cover: None
    inserted_index: None


type RewriteResult = CoverPut | NoCoverPut
"""A rewrite chapter's convert_result (JSON): the cover put in and its index are both there or both None."""


def read_result(text: str) -> RewriteResult:
    """Return a rewrite chapter's convert_result (JSON text). Unchecked."""
    return json.loads(text)


def held(state: str | None) -> str | None:
    """Return the volume (as text, "" for a file with no volume) whose cover a cover_state holds, or None."""
    return state.removeprefix("volume:") if state and state.startswith("volume:") else None


def cover_volume(state: str | None) -> str | None:
    """Return the volume (as text, "" for a file with no volume) whose cover page a cover_state says the file holds.

    The page is the volume's current image (volume:V) or an old one (stale:V: MangaDex had a new one since); None for
    none or unfit.
    """
    for prefix in ("volume:", "stale:"):
        if state and state.startswith(prefix):
            return state.removeprefix(prefix)
    return None


def current_state(conn: db.Connection, chapter_id: int, series_id: int, state: str) -> str:
    """Return a published chapter's cover_state (state: "volume:<V>" when it took volume V's cover).

    "stale:<V>" when the image it took is no longer the series' stored image of V (MangaDex had a new one since). Read
    in the caller's transaction, so a refresh marking the files of V is either before it or sees its file.
    """
    volume = held(state)
    v = names.to_volume(volume)
    if v is None or v != int(v):  # no volume, or not a whole one: no stored image to follow
        return state
    took = conn.execute("SELECT sha256 FROM volume_cover WHERE chapter_id = %s", (chapter_id,)).fetchone()
    now = conn.execute(
        "SELECT sha256 FROM series_volume_cover WHERE series_id = %s AND volume = %s", (series_id, int(v))
    ).fetchone()
    return f"stale:{volume}" if took and now and took[0] != now[0] else state


def is_cover(name: str) -> bool:
    """Return whether an archive member name is a cover page: its stem is archive.COVER_STEM, in any case."""
    return PurePosixPath(name).stem.lower() == archive.COVER_STEM


# --- what a published file holds


def fill_states(ctx: workflow.Context, series: db.Row) -> bool:
    """Fill the cover_state of the series' published files that have none.

    A bulk file's from its series' record (the output covers.volumes names for each volume), a pipeline file's from its
    member list (a page 000_cover is the cover of its volume). Returns whether every state is known; a record or file
    that cannot be read leaves them unknown and is logged (a share error raises Unreachable).
    """
    conn, cfg = ctx.conn, ctx.cfg
    rows = conn.execute(
        """SELECT id, library_path, origin, comicinfo_volume FROM published_file
        WHERE series_id = %s AND cover_state IS NULL ORDER BY id""",
        (series["id"],),
    ).fetchall()
    if not rows:
        return True
    try:
        with workflow.share_errors():
            states = _read_states(cfg, series, rows)
    except workflow.Unreachable:
        raise
    except Exception as e:  # this series' file or record: not compared this run; the run goes on
        log.warning("%s: series %s (%s): covers not read: %s", ctx.part, series["id"], series["name"], e)
        return False
    with db.transaction(conn):
        db.executemany(conn, "UPDATE published_file SET cover_state = %s WHERE id = %s AND cover_state IS NULL", states)
    return True


def _read_states(cfg: config.Config, series: db.Row, rows: list[db.Row]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    by_output: dict[str, str | None] | None = None
    for r in rows:
        if r["origin"] == "bulk":
            if by_output is None:
                record = record_reader.load(config.to_local(cfg, series["folder"]))
                volumes = record["series"]["covers"]["volumes"]
                by_output = {c["chapter"]: names.fmt_num(names.to_volume(v)) for v, c in volumes.items()}
            v = by_output.get(PurePosixPath(r["library_path"]).name)
        else:
            with zipfile.ZipFile(config.to_local(cfg, r["library_path"])) as z:
                has = any(is_cover(n) for n in archive.archive_pages(z.namelist()))
            v = (r["comicinfo_volume"] or "") if has else None
        out.append((NONE if v is None else f"volume:{v}", r["id"]))
    return out


# --- the rewrite chapter's convert


def convert_run(ctx: workflow.Context, chapter: db.Row) -> workflow.Done | workflow.Dropped:
    """Copy the library file's members in order byte for byte to staging, its cover left out, a new cover put in first.

    The new cover is encoded as a new chapter's is. An insert that cannot be made into a file that holds no cover and
    keeps its volume drops the chapter and records the image unfit.
    """
    cfg, conn = ctx.cfg, ctx.conn
    file = db.one_row(conn, "SELECT * FROM published_file WHERE id = %s", (chapter["published_file_id"],))
    source = config.to_local(cfg, file["library_path"])
    cover_row = conn.execute("SELECT * FROM volume_cover WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    with zipfile.ZipFile(source) as z:
        members = [(i.filename, z.read(i), i.CRC) for i in z.infolist()]
    page_names = archive.archive_pages([n for n, _, _ in members])
    # the file itself says what it holds (a record may not list an old cover): any cover page goes
    dropped = next((i for i, n in enumerate(page_names) if is_cover(n)), None)
    holds = dropped is not None
    kept = [m for m in members if not is_cover(m[0])]
    state = NONE if holds or cover_volume(file["cover_state"]) is not None else file["cover_state"]  # unfit stays
    inserted: EncodedPage | None = None
    why: str | None = None
    row_volume = None if cover_row is None else cover_row["volume"]
    if cover_row is not None:
        scratch = config.abs_path(cfg["paths"]["scratch"]) / workflow.claim_name(chapter)
        try:
            inserted, why = _encode(cfg, covers.load(cfg, cover_row), kept, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if inserted is None:
            state = f"unfit:{cover_row['sha256']}"
            if not holds and chapter["comicinfo_volume"] == file["comicinfo_volume"]:
                reason = f"volume {cover_row['volume']} cover unfit: {why}"
                log.warning("chapter %s: %s: %s", chapter["id"], file["library_path"], reason)
                return workflow.Dropped(reason, record=lambda c: _set_state(c, file["id"], state))
        else:
            state = f"volume:{cover_row['volume']}"
    staged = config.abs_path(cfg["paths"]["staging"]) / convert.staged_name(conn, chapter)
    entries = [*([(inserted["name"], inserted["data"])] if inserted else []), *((n, d) for n, d, _ in kept)]
    workflow.holds_claim(ctx, chapter)
    try:
        size = archive.write(staged, entries)
        _check(staged, kept, inserted)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    result: RewriteResult
    if inserted is None:
        result = {
            "cover": None,
            "cover_skipped": why,
            "dropped_index": dropped,
            "inserted_index": None,
            "cover_state": state,
        }
    else:
        result = {
            "cover": {
                "name": inserted["name"],
                "format": inserted["format"],
                "score": inserted["score"],
                "bytes": inserted["bytes"],
                "width": inserted["width"],
                "height": inserted["height"],
                "volume": row_volume,
            },
            "cover_skipped": why,
            "dropped_index": dropped,
            "inserted_index": archive.archive_pages([n for n, _ in entries]).index(inserted["name"]),
            "cover_state": state,
        }

    def record(c: db.Connection) -> None:
        c.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s, convert_result = %s WHERE id = %s",
            (config.to_stored(cfg, staged), size, json.dumps(result), chapter["id"]),
        )

    log.info(
        "chapter %s: %s: cover %s%s", chapter["id"], file["library_path"],
        "inserted" if inserted else "not inserted" if cover_row else "dropped", f" ({why})" if why else "",
    )  # fmt: skip
    return workflow.Done(record)


def _set_state(conn: db.Connection, file_id: int, state: str) -> None:
    conn.execute("UPDATE published_file SET cover_state = %s WHERE id = %s", (state, file_id))


def _encode(
    cfg: config.Config, data: bytes, kept: list[tuple[str, bytes, int]], scratch: Path
) -> tuple[EncodedPage, None] | tuple[None, str]:
    """Encode the cover page for the file's pages: (the page, None), or (None, why).

    The page is {name, data, format, score, width, height, bytes}. Scaled to the most common width of the pages, in the
    format they share; AVIF only through its gate.
    """
    data_of = {n: d for n, d, _ in kept}
    pages = [(images.detect(data_of[n]), _size(data_of[n])[0]) for n in archive.archive_pages(list(data_of))]
    if not pages:
        return None, "it has no pages"
    fmt = plan.cover_format({f for f, _ in pages})
    if fmt is None:
        return None, f"its pages ({', '.join(sorted({f for f, _ in pages}))}) share no cover format"
    width = Counter(w for _, w in pages).most_common(1)[0][0]
    taken: plan.OutcomeCover
    if fmt == images.AVIF:
        with convert.encoder(cfg) as encoder:
            avif = encoder.cover(data, width, scratch / "cover")
        if avif["ok"] is False:
            return None, f"its AVIF cover scored {avif['score']}, below the floor"
        taken = {"avif": avif}
    else:
        taken = {"format": fmt, "data": data, "width": width}
    out_dir = scratch / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    got = covers.insert(out_dir, taken)
    cover = (out_dir / got["name"]).read_bytes()
    w, h = _size(cover)
    return {**got, "data": cover, "width": w, "height": h}, None


def _size(data: bytes) -> tuple[int, int]:
    """(width, height) of an image's bytes: from its header, else by decoding it."""
    return images.dimensions(data) or images.open_image(data)[0].size


def _check(staged: Path, kept: list[tuple[str, bytes, int]], inserted: EncodedPage | None) -> None:
    """Every copied member's CRC is the source's; the cover decodes in its format."""
    with zipfile.ZipFile(staged) as z:
        got = {i.filename: i.CRC for i in z.infolist()}
        if any(got.get(n) != crc for n, _, crc in kept):
            msg = f"{staged}: a copied member's CRC differs from the library file's"
            raise RuntimeError(msg)
        if inserted and images.detect(z.read(inserted["name"])) != inserted["format"]:
            msg = f"{staged}: its cover does not decode as {inserted['format']}"
            raise RuntimeError(msg)


# --- the Page entries


def shift_pages(entries: list[dict[str, str]], result: RewriteResult) -> list[dict[str, str]]:
    """Return the old file's Page entries after convert's cover change (result: its convert_result).

    A dropped cover's entry goes and those after it take 1 off; an inserted cover adds a FrontCover entry at its place
    in reading order and 1 to every index from there. An old file with no entries gets none.
    """
    if not entries:
        return []
    out = [dict(e) for e in entries]
    gone = result["dropped_index"]
    if gone is not None:
        out = [e for e in out if int(e["Image"]) != gone]
        for e in out:
            if int(e["Image"]) > gone:
                e["Image"] = str(int(e["Image"]) - 1)
    match result:
        case {"cover": None}:
            pass
        case {"cover": cover, "inserted_index": at}:
            for e in out:
                if int(e["Image"]) >= at:
                    e["Image"] = str(int(e["Image"]) + 1)
            front = {"Image": str(at), "Type": "FrontCover", "ImageSize": str(cover["bytes"])}
            front |= {"ImageWidth": str(cover["width"]), "ImageHeight": str(cover["height"])}
            out.insert(next((i for i, e in enumerate(out) if int(e["Image"]) > at), len(out)), front)
    return out
