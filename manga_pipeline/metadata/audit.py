"""`metadata audit [--state PATH]` (SPEC "Metadata checks", Audit): a read-only check of the library's series' ids.

It runs over every series of the library whatever `series.only` says. Gathers each series' ids from every origin
itself (the database is not read: a test database holds few series) and prints the series whose ids disagree, the
sources no origin gives an id for, and the Suwayomi library manga no series matches by name.
"""

from collections.abc import Iterable

from manga_pipeline import config
from manga_pipeline.clients.kavita import Kavita
from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.clients.suwayomi import Suwayomi
from manga_pipeline.metadata import ids, importer, sources
from manga_pipeline.replies.kavita import Series
from manga_pipeline.replies.suwayomi import Manga


def run(
    cfg: config.Config,
    kavita: Kavita,
    suwayomi: Suwayomi,
    clients: sources.Sources,
    state: importer.OldState | None = None,
) -> None:
    """Print the audit.

    clients: the metadata sources' clients; state: the old tool's state file as importer.read_state gives it, or None.
    """
    listing = kavita.series_in_library(cfg["kavita"]["library_id"])
    manga = list(suwayomi.library_manga())
    by_title: dict[str, list[Manga]] = {}
    for m in manga:
        by_title.setdefault(normalize(m["title"]), []).append(m)
    matched: set[int] = set()
    disagree: list[str] = []
    lacking: dict[str, list[str]] = {}
    for kseries in listing:
        mine = list({m["id"]: m for n in series_names(kseries) for m in by_title.get(normalize(n), [])}.values())
        matched |= {m["id"] for m in mine}
        known = _known(kavita, clients, kseries, mine, state)
        agreed, problem = ids.resolve(known)
        if problem:
            disagree.append(f"{kseries['name']} (Kavita {kseries['id']}): {problem}")
        for source in ids.SOURCES:
            if source not in agreed:
                lacking.setdefault(source, []).append(kseries["name"])
    unmatched = [f"{m['title']} (manga {m['id']})" for m in manga if m["id"] not in matched]
    _print("series whose ids disagree", disagree)
    for source in ids.SOURCES:
        _print(f"series with no {source} id", lacking.get(source, []))
    _print("Suwayomi library manga no series matches by name", unmatched)


def _known(
    kavita: Kavita, clients: sources.Sources, kseries: Series, mangas: Iterable[Manga], state: importer.OldState | None
) -> list[ids.Known]:
    known: list[ids.Known] = [(s, "kavita", v) for s, v in ids.kavita_ids(kseries, kavita.metadata(kseries["id"]))]
    for m in mangas:
        known += [(s, "suwayomi", v) for s, v in ids.suwayomi_ids(m)]
    if state is not None:
        for key, value in (state["ids"].get(str(kseries["id"])) or {}).items():
            source = importer.OLD_SOURCES.get(key)
            if source is None or value is None:
                continue
            found = ids.form(source, value)
            if found:
                known.append((source, "import", found))
    agreed, _ = ids.resolve(known)
    if "mangabaka" in agreed and (record := clients.mangabaka.series(agreed["mangabaka"])) is not None:
        known += [(s, "mangabaka", v) for s, v in ids.mangabaka_ids(record) if s != "mangabaka"]
    if "mangadex" in agreed and (record := clients.mangadex.manga(agreed["mangadex"])) is not None:
        known += [(s, "mangadex", v) for s, v in ids.mangadex_ids(record) if s != "mangadex"]
    return known


def _print(title: str, lines: list[str]) -> None:
    print(f"{title}: {len(lines)}")
    for line in sorted(lines):
        print(f"  {line}")
