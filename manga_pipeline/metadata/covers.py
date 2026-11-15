"""A series' cover (SPEC "Metadata checks", Covers): picked at each check by the old metadata tool's rule
(`mangadex-en-vol1-v1`), stored content-addressed under `<paths.covers>/series/`, recorded in `series_cover`.
"""

import hashlib
import io
import logging
import time
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from manga_pipeline import config, errors
from manga_pipeline.clients import http

log = logging.getLogger(__name__)
WIDE_ENOUGH = 1500  # px: a Suwayomi thumbnail this wide is the cover
FOLDER = "series"  # below paths.covers (volume covers live beside it)
UNNAMED_KEEP_S = 24 * 3600


@dataclass(frozen=True)
class Cover:
    source_url: str
    path: str  # stored form
    sha256: str


def pick(ctx, clients, manga_rows, records):
    """The series' cover, written under paths.covers, or None when no source has one."""
    thumbnails = []  # (source url, bytes, (width, extension))
    for manga in manga_rows:
        found = _image(f"suwayomi:manga/{manga['id']}/thumbnail", ctx.suwayomi.thumbnail(manga["id"]))
        if found is not None:
            thumbnails.append(found)
            if found[2][0] >= WIDE_ENOUGH:
                return _write(ctx.cfg, *found)
    manga = records[("mangadex", "manga")]
    if manga is not None:
        file_name = mangadex_choice(manga, records[("mangadex", "covers")] or [])
        if file_name:
            found = _image(
                f"mangadex:{manga['id']}/{file_name}",
                _gone_is_none(clients.mangadex.cover_image, manga["id"], file_name),
            )
            if found is not None:
                return _write(ctx.cfg, *found)
    choices = list(thumbnails)
    media = records[("anilist", "media")]
    url = ((media or {}).get("coverImage") or {}).get("extraLarge")
    if url and (found := _image(url, _gone_is_none(clients.anilist.cover_image, url))) is not None:
        choices.append(found)
    if not choices:
        return None
    return _write(ctx.cfg, *max(choices, key=lambda c: c[2][0]))


def _gone_is_none(fetch, *args):
    """The image's bytes; None when its host answers 404 (a cover file deleted since the record was fetched)."""
    try:
        return fetch(*args)
    except http.HttpError as e:
        if e.status == 404:
            return None
        raise


def mangadex_choice(manga, covers):
    """The MangaDex cover file name: English volume 1, the lowest English volume, volume 1 in the original language,
    volume 1 in any language, else the series' main cover; None when it has none."""

    def volume(c):
        try:
            return float(c.get("volume") or "")
        except ValueError:
            return float("inf")

    english = sorted((c for c in covers if c.get("locale") == "en"), key=volume)
    first = [c for c in covers if volume(c) == 1]
    original = (manga.get("attributes") or {}).get("originalLanguage")
    for choices in (
        [c for c in english if volume(c) == 1],
        english,
        [c for c in first if c.get("locale") == original],
        first,
    ):
        if choices:
            return choices[0]["fileName"]
    main = [
        r["attributes"]["fileName"]
        for r in manga.get("relationships") or []
        if r.get("type") == "cover_art" and r.get("attributes")
    ]
    return main[0] if main else None


def _image(source_url, data):
    """(source url, bytes, (width, file extension)), or None for no bytes or bytes that are not an image Pillow reads
    (an error page served as the image): that candidate is skipped."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            found = (image.width, (image.format or "").lower().replace("jpeg", "jpg"))
    except (UnidentifiedImageError, Image.DecompressionBombError) as e:
        log.warning("cover from %s skipped: not an image (%s)", source_url, e)
        return None
    return (source_url, data, found) if found[1] else None


def _write(cfg, source_url, data, size):
    ext = size[1]
    sha256 = hashlib.sha256(data).hexdigest()
    path = config.abs_path(cfg["paths"]["covers"]) / FOLDER / f"{sha256}.{ext}"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_bytes(data)
        part.replace(path)
    return Cover(source_url, config.to_stored(cfg, path), sha256)


def store(conn, series_id, cover):
    """Record the series' cover; inside the caller's transaction."""
    conn.execute(
        """INSERT INTO series_cover (series_id, source_url, path, sha256) VALUES (%s, %s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET source_url = excluded.source_url, path = excluded.path,
            sha256 = excluded.sha256""",
        (series_id, cover.source_url, cover.path, cover.sha256),
    )


def clean(ctx):
    """Delete a cover file under paths.covers/series that no series names, once it is a day old (a check that did
    not complete may have left one)."""
    root = config.abs_path(ctx.cfg["paths"]["covers"]) / FOLDER
    if not root.is_dir():
        return
    named = {row[0] for row in ctx.conn.execute("SELECT path FROM series_cover")}
    cutoff = time.time() - UNNAMED_KEEP_S
    for entry in root.iterdir():
        if config.to_stored(ctx.cfg, entry) in named:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except FileNotFoundError as e:
            if errors.dropped_share(e):
                raise
            continue
        except OSError as e:
            if errors.dropped_share(e):
                raise
            log.warning("intake: cannot delete %s: %s", entry, e)
            continue
        log.info("intake: deleted %s: a cover no series names", entry)
