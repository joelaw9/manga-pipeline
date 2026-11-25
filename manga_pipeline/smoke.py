"""The `smoke` command (SPEC "Services"): check Suwayomi and Kavita with reads only, report what they answer."""

from collections.abc import Iterator

from manga_pipeline import config
from manga_pipeline.clients import kavita, suwayomi

SHOWN = 3  # chapter names and files reported


class SmokeError(Exception):
    """A smoke check failed; the entry point exits 1."""


def run(cfg: config.Config) -> Iterator[str]:
    """Check both services, whatever the first one answers, yielding each line of the report as it is known.

    Raises SmokeError naming every problem once both are checked.
    """
    problems: list[str] = []
    for name, check in (("suwayomi", _suwayomi), ("kavita", _kavita)):
        try:
            yield from check(cfg)
        except Exception as e:  # noqa: BLE001  # the command's edge: whatever stops a check is that check's problem
            problems.append(f"{name}: {type(e).__name__}: {e}")
            yield f"{name}: FAILED: {type(e).__name__}: {e}"
    if problems:
        raise SmokeError("; ".join(problems))


def _suwayomi(cfg: config.Config) -> Iterator[str]:
    client = suwayomi.from_config(cfg)
    yield f"suwayomi: {client.library_size()} manga in the library, {len(client.sources())} sources"
    manga = next(client.library_manga(), None)
    if manga is None:
        yield "suwayomi: the library is empty"
        return
    chapters = client.chapters(manga["id"])
    source = (manga["source"] or {}).get("displayName", "source not installed")
    counts = f"{len(chapters)} chapters, {sum(c['isDownloaded'] for c in chapters)} downloaded"
    yield f'suwayomi: manga {manga["id"]} "{manga["title"]}" ({source}): {counts}'
    for chapter in chapters[:SHOWN]:
        yield f"  {suwayomi.chapter_file(chapter['scanlator'], chapter['name'])}"


def _kavita(cfg: config.Config) -> Iterator[str]:
    client = kavita.from_config(cfg)
    libraries = client.libraries()
    listed = ", ".join(f'{library["id"]} "{library["name"]}"' for library in libraries)
    yield f"kavita: {len(libraries)} libraries: {listed}"
    library_id = cfg["kavita"]["library_id"]
    if library_id not in [library["id"] for library in libraries]:
        msg = f"kavita.library_id {library_id} is none of Kavita's libraries"
        raise LookupError(msg)
    series = client.series_in_library(library_id)
    yield f"kavita: library {library_id} holds {len(series)} series"
    if not series:
        return
    first = series[0]
    files = client.series_files(first["id"])
    try:
        folder = client.series_folder(first["id"])
    except kavita.FolderError as e:
        folder = f"no usable folder ({e})"
    yield f'kavita: series {first["id"]} "{first["name"]}": {len(files)} files, folder {folder}'
    for file in files[:SHOWN]:
        yield f"  {file['path']} ({file['bytes']} bytes, {'exists' if file['path'].exists() else 'MISSING'})"
    missing = sum(not file["path"].exists() for file in files)
    if missing:
        msg = f"{missing} of {len(files)} mapped paths do not exist: check paths.kavita_path_map and the share's mount"
        raise FileNotFoundError(msg)
