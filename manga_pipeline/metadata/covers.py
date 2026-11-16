"""A series' cover (SPEC "Metadata checks", Covers): picked at each check by the old metadata tool's rule
(`mangadex-en-vol1-v1`), stored content-addressed under `<paths.covers>/series/`, recorded in `series_cover`. A covered
volume's cover (SPEC "Metadata", Volume covers): MangaDex's cover of exactly that volume (the bulk converter's
`volume_covers`), stored under `<paths.covers>/volume/`, recorded in `series_volume_cover`.
"""

import hashlib
import io
import logging
import time
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from manga_pipeline import config, db, errors, images, names, workflow
from manga_pipeline.clients import http
from manga_pipeline.metadata import sources

log = logging.getLogger(__name__)
WIDE_ENOUGH = 1500  # px: a Suwayomi thumbnail this wide is the cover
FOLDER = "series"  # below paths.covers
VOLUME_FOLDER = "volume"  # below paths.covers: covered volumes' covers
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
    _put(path, data)
    return Cover(source_url, config.to_stored(cfg, path), sha256)


def store(conn, series_id, cover):
    """Record the series' cover; inside the caller's transaction."""
    conn.execute(
        """INSERT INTO series_cover (series_id, source_url, path, sha256) VALUES (%s, %s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET source_url = excluded.source_url, path = excluded.path,
            sha256 = excluded.sha256""",
        (series_id, cover.source_url, cover.path, cover.sha256),
    )


def volume_cover(ctx, series_id, volume):
    """(path relative to paths.covers, sha256) of the series' cover of `volume`, fetched and stored the first time
    (and again when its file is missing or holds other bytes); None when MangaDex has no cover of that volume or the
    series has no MangaDex record."""
    row = ctx.conn.execute(
        "SELECT path, sha256 FROM series_volume_cover WHERE series_id = %s AND volume = %s", (series_id, volume)
    ).fetchone()
    root = config.abs_path(ctx.cfg["paths"]["covers"])
    if row is not None and _holds(root / row["path"], row["sha256"]):
        return row["path"], row["sha256"]
    records = {
        r["kind"]: r["payload"]
        for r in ctx.conn.execute(
            "SELECT kind, payload FROM fetched WHERE series_id = %s AND source = 'mangadex'", (series_id,)
        )
    }
    manga = records.get("manga")
    file_name = volume_choice(manga, records.get("covers") or [], volume) if manga else None
    if file_name is None:
        return None
    url = f"{sources.MANGADEX_UPLOADS}/covers/{manga['id']}/{file_name}"
    data = _gone_is_none(ctx.sources.mangadex.cover_image, manga["id"], file_name)
    if data is None:  # MangaDex no longer serves the file: no cover
        return None
    image = _image(url, data)
    if image is None:  # an error page served as the image: fails the attempt, a later run tries again
        raise ValueError(f"the volume {volume} cover from {url} is not an image")
    _, data, (_, ext) = image
    sha256 = hashlib.sha256(data).hexdigest()
    rel = f"{VOLUME_FOLDER}/{sha256}.{ext}"
    _put(root / rel, data, sha256)
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            """INSERT INTO series_volume_cover (series_id, volume, source_url, path, sha256)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (series_id, volume) DO UPDATE SET source_url = excluded.source_url, path = excluded.path,
                sha256 = excluded.sha256""",
            (series_id, volume, url, rel, sha256),
        )
    return rel, sha256


def fetch_wanted(ctx):
    """Fetch the volume cover images the retroactive comparison wants (SPEC "Retroactive updates"): each row below
    workflow.attempt_limit attempts, of an in-scope ok series, goes once it is fetched or MangaDex has none; a fetch
    whose answer is wrong counts 1 attempt and records its error. A share or database error, or MangaDex down,
    timing out or failing on its side (5xx), is raised: the caller's problem, no attempt spent."""
    rows = ctx.conn.execute(
        f"""SELECT w.series_id, w.volume FROM volume_cover_wanted w JOIN series s ON s.id = w.series_id
        WHERE w.attempts < %(limit)s AND s.status = 'ok' AND {workflow.SCOPE} ORDER BY w.series_id, w.volume""",
        {**workflow.scope_params(ctx.cfg), "limit": ctx.cfg["workflow"]["attempt_limit"]},
    ).fetchall()
    for r in rows:
        try:
            found = volume_cover(ctx, r["series_id"], r["volume"])
        except Exception as e:
            if (
                isinstance(e, (db.Error, errors.Unreachable, http.Timeout))
                or (isinstance(e, http.HttpError) and e.status >= 500)
                or (isinstance(e, OSError) and (errors.dropped_share(e) or e.errno in errors.SHARE_ERRNOS))
            ):
                raise
            with db.transaction(ctx.conn):
                ctx.conn.execute(
                    """UPDATE volume_cover_wanted SET attempts = attempts + 1, last_error = %s
                    WHERE series_id = %s AND volume = %s""",
                    (f"{type(e).__name__}: {e}", r["series_id"], r["volume"]),
                )
            log.warning("%s: series %s volume %s cover not fetched: %s", ctx.part, r["series_id"], r["volume"], e)
            continue
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "DELETE FROM volume_cover_wanted WHERE series_id = %s AND volume = %s", (r["series_id"], r["volume"])
            )
        log.info(
            "%s: series %s volume %s cover %s", ctx.part, r["series_id"], r["volume"],
            "fetched" if found else "not on MangaDex",
        )  # fmt: skip


def store_volume_image(cfg, data):
    """(path below paths.covers, sha256) of a volume cover image stored content-addressed under VOLUME_FOLDER, as a
    fetched one is (a better copy taking its file's own cover page: SPEC "Retroactive updates")."""
    sha256 = hashlib.sha256(data).hexdigest()
    rel = f"{VOLUME_FOLDER}/{sha256}{images.EXTENSION[images.detect(data)]}"
    _put(config.abs_path(cfg["paths"]["covers"]) / rel, data, sha256)
    return rel, sha256


def volume_choice(manga, covers, volume):
    """The MangaDex cover file name of exactly `volume`: English, else the manga's original language, else any
    locale (the first listed wins a tie); None when MangaDex has none of that volume."""
    original = (manga.get("attributes") or {}).get("originalLanguage")
    best = None
    for c in covers:
        if names.to_volume(c.get("volume")) != volume or not c.get("fileName"):
            continue
        rank = 0 if c.get("locale") == "en" else 1 if c.get("locale") == original else 2
        if best is None or rank < best[0]:
            best = (rank, c["fileName"])
    return best and best[1]


def _holds(path, sha256):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    except FileNotFoundError as e:
        if errors.dropped_share(e):
            raise
        return False


def _put(path, data, sha256=None):
    """Write the content-addressed file unless it is there; with sha256, also when it holds other bytes."""
    if not path.exists() or (sha256 is not None and not _holds(path, sha256)):
        path.parent.mkdir(exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_bytes(data)
        part.replace(path)


def clean(ctx):
    """Delete a cover file under paths.covers/series that no series names, and under paths.covers/volume that no
    series volume cover nor chapter names, once it is a day old (a check or validate that did not complete, a dropped
    chapter or a state reset may have left one)."""
    covers = config.abs_path(ctx.cfg["paths"]["covers"])
    series = {row[0] for row in ctx.conn.execute("SELECT path FROM series_cover")}
    volume = {
        config.to_stored(ctx.cfg, covers / row[0])
        for row in ctx.conn.execute("SELECT path FROM series_volume_cover UNION SELECT path FROM volume_cover")
    }
    for folder, named in ((FOLDER, series), (VOLUME_FOLDER, volume)):
        _clean(ctx, covers / folder, named)


def _clean(ctx, root, named):
    if not root.is_dir():
        return
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
