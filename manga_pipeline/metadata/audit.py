"""`metadata audit [--state PATH]` (SPEC "Metadata checks", Audit): read-only, over every series of the library
whatever `series.only` says. Gathers each series' ids from every origin itself (the database is not read: a test
database holds few series) and prints the series whose ids disagree, the sources no origin gives an id for, and the
Suwayomi library manga no series matches by name.
"""

from manga_pipeline.clients.kavita_match import normalize, series_names
from manga_pipeline.metadata import ids, importer


def run(ctx, clients, state=None):
    """Print the audit; state: the old tool's state file as importer.read_state gives it, or None."""
    listing = ctx.kavita.series_in_library(ctx.cfg["kavita"]["library_id"])
    manga = list(ctx.suwayomi.library_manga())
    by_title = {}
    for m in manga:
        by_title.setdefault(normalize(m["title"]), []).append(m)
    matched = set()
    disagree, lacking = [], {}
    for kseries in listing:
        mine = list({m["id"]: m for n in series_names(kseries) for m in by_title.get(normalize(n), [])}.values())
        matched |= {m["id"] for m in mine}
        known = _known(ctx, clients, kseries, mine, state)
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


def _known(ctx, clients, kseries, mangas, state):
    known = [(s, "kavita", v) for s, v in ids.kavita_ids(kseries, ctx.kavita.metadata(kseries["id"]))]
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


def _print(title, lines):
    print(f"{title}: {len(lines)}")
    for line in sorted(lines):
        print(f"  {line}")
