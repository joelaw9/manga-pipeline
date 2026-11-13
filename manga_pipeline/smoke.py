"""The `smoke` command (SPEC "Services"): check Suwayomi and Kavita with reads only, print what they answer."""

from manga_pipeline.clients import kavita, suwayomi

SHOWN = 3  # chapter names and files printed


class Failed(Exception):
    """A smoke check failed; the entry point exits 1."""


def run(cfg):
    """Check both services, whatever the first one answers. Raises Failed naming every problem."""
    problems = []
    for name, check in (("suwayomi", _suwayomi), ("kavita", _kavita)):
        try:
            check(cfg)
        except Exception as e:  # the command's edge: whatever stops a check is that check's problem
            problems.append(f"{name}: {type(e).__name__}: {e}")
            print(f"{name}: FAILED: {type(e).__name__}: {e}")
    if problems:
        raise Failed("; ".join(problems))


def _suwayomi(cfg):
    client = suwayomi.from_config(cfg)
    print(f"suwayomi: {client.library_size()} manga in the library, {len(client.sources())} sources")
    manga = next(client.library_manga(), None)
    if manga is None:
        print("suwayomi: the library is empty")
        return
    chapters = client.chapters(manga["id"])
    source = (manga["source"] or {}).get("displayName", "source not installed")
    counts = f"{len(chapters)} chapters, {sum(c['isDownloaded'] for c in chapters)} downloaded"
    print(f'suwayomi: manga {manga["id"]} "{manga["title"]}" ({source}): {counts}')
    for chapter in chapters[:SHOWN]:
        print(f"  {suwayomi.chapter_file(chapter['scanlator'], chapter['name'])}")


def _kavita(cfg):
    client = kavita.from_config(cfg)
    libraries = client.libraries()
    listed = ", ".join(f'{library["id"]} "{library["name"]}"' for library in libraries)
    print(f"kavita: {len(libraries)} libraries: {listed}")
    library_id = cfg["kavita"]["library_id"]
    if library_id not in [library["id"] for library in libraries]:
        raise LookupError(f"kavita.library_id {library_id} is none of Kavita's libraries")
    series = client.series_in_library(library_id)
    print(f"kavita: library {library_id} holds {len(series)} series")
    if not series:
        return
    first = series[0]
    files = client.series_files(first["id"])
    try:
        folder = client.series_folder(first["id"])
    except kavita.FolderError as e:
        folder = f"no usable folder ({e})"
    print(f'kavita: series {first["id"]} "{first["name"]}": {len(files)} files, folder {folder}')
    for file in files[:SHOWN]:
        print(f"  {file['path']} ({file['bytes']} bytes, {'exists' if file['path'].exists() else 'MISSING'})")
    missing = sum(not file["path"].exists() for file in files)
    if missing:
        raise FileNotFoundError(
            f"{missing} of {len(files)} mapped paths do not exist: check paths.kavita_path_map and the share's mount"
        )
