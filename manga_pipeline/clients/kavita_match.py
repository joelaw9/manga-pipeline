"""Map share folders to Kavita series by the files Kavita holds, and Kavita's own name normalising (SPEC "Services").

Not by folder name or lowestFolderPath. Copied from the bulk converter (optimizer/kavita_match.py).

A Kavita series can span folders (same series downloaded from several sources): its lowestFolderPath is
then the common parent (the library root), and a folder name says nothing about the series name
(ComicInfo Series / LocalizedName decide). Matching by file path is the only reliable link.

Folder statuses:
  matched    every archive belongs to one series, which holds no files elsewhere
  shared     every archive belongs to one series that also holds files in other folders (`other_folders`)
  partial    some archives belong to a series, others to none (Kavita dropped or never scanned them)
  split      archives belong to more than one series
  unmatched  archives, none in Kavita; `candidates` = series whose name equals a ComicInfo Series in the folder
  empty      no archives
Only matched/shared/partial/split carry a `series_id`; split takes the series holding most archives.
"""

import unicodedata
from collections import Counter, defaultdict

ARCHIVE_EXT = (".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z")
SERIES_NAME_KEYS = ("name", "localizedName", "originalName")  # the names a Kavita series DTO matches on
USABLE = frozenset({"matched", "shared"})  # folder statuses the pipeline works on
NORMALIZE_KEEP = frozenset("0123456789+!\uff0a\uff01\uff0b")  # kept by normalize besides letters (Kavita's class)
BMP_LAST = 0xFFFF  # the last code point of the Basic Multilingual Plane (1 C# char)


def series_names(series):
    """Return a Kavita series DTO's non-empty names (SERIES_NAME_KEYS order)."""
    return [series[k] for k in SERIES_NAME_KEYS if (series or {}).get(k)]


def normalize(name):
    r"""Return Kavita's series key of a name, exactly.

    Kavita v0.9.1.4 Kavita.Common/Extensions/StringExtensions.cs `ToNormalized`, which Parser.Normalize calls: keep
    Unicode letters (category L*), ASCII digits, + ! and full-width * ! +, drop everything else (spaces, punctuation,
    combining marks, other digits), then lower-case. No Unicode folding (NFKC, casefold). As .NET does: a letter above
    U+FFFF is a UTF-16 surrogate pair, not \p{L}, so it is dropped; each character is lowered on its own (never the
    whole string: Python lowers a final capital sigma to "ς" by context), and what lowering adds is filtered again ("İ"
    lowers to "i" + a combining dot: "i").
    """
    return "".join(low for c in name or "" if _kept(c) for low in c.lower() if _kept(low))


def _kept(c):
    """Return whether Kavita's normalize keeps c: a BMP letter, an ASCII digit, ASCII + ! and full-width * ! +."""
    return c in NORMALIZE_KEEP or (ord(c) <= BMP_LAST and unicodedata.category(c).startswith("L"))


def off_names(series_values, kavita_names):
    """Return the name guard: ComicInfo Series values (sorted, None as "") that normalize to none of Kavita's names."""
    allowed = {normalize(n) for n in kavita_names if n}
    return sorted({v or "" for v in series_values if normalize(v) not in allowed})


def series_values(comicinfos):
    """Return the values the name guard (off_names) checks in these ComicInfo field dicts.

    Every Series (a missing one as None, so it is caught) and every LocalizedSeries written (Kavita's scanner matches on
    both).
    """
    return [ci.get("Series") for ci in comicinfos] + [
        ci["LocalizedSeries"] for ci in comicinfos if ci.get("LocalizedSeries")
    ]


def series_titles(kseries):
    """Return (ComicInfo Series, LocalizedSeries) of a series.

    Kavita's Name, and its LocalizedName when that normalizes (normalize) to another name, else None. Kavita is the
    master: the files' own Series is never kept.
    A series without a Kavita Name (unmatched: the plan skips it at S0 before naming) raises.
    """
    name = (kseries or {}).get("name")
    if not name:
        msg = "naming: no Kavita series Name: ComicInfo Series follows Kavita's Name"
        raise ValueError(msg)
    localized = kseries.get("localizedName") or None
    if localized is not None and normalize(localized) == normalize(name):
        localized = None
    return name, localized


def is_archive(name):
    """Return whether a file name has an archive extension (ARCHIVE_EXT, any case)."""
    return name.lower().endswith(ARCHIVE_EXT)


def relevant(series, root):
    """Return the series that can hold files under `root`.

    Their lowestFolderPath is inside it, or an ancestor of it (a series spanning folders has the common parent, up to
    the library root).
    """
    low = (series.get("lowestFolderPath") or "").rstrip("/")
    return bool(low) and (low == root or low.startswith(root + "/") or root.startswith(low + "/"))


def chapter_files(volumes):
    """[(chapter, file path)] from /api/Series/volumes."""
    return [(c, f["filePath"]) for v in volumes for c in v.get("chapters", []) for f in c.get("files", [])]


def match_folders(root, folder_files, series, volumes_by_series, comicinfo_series=None):
    """Match each share folder under root to the Kavita series that hold its files.

    root: Kavita path of the share folder holding the series folders (e.g. /shared/manga/mangas/Local).
    folder_files: {folder name: [file names directly in it]}.
    series: every Kavita series of the library (names for candidates);
    volumes_by_series: {series id: /api/Series/volumes result}, needed for the `relevant` ones only.
    comicinfo_series: {folder name: set of ComicInfo Series values}, used for unmatched candidates.
    Returns {folder name: record}; folder paths in it are Kavita paths.
    """
    root = root.rstrip("/")
    by_id = {s["id"]: s for s in series}
    owner = {}  # Kavita file path -> series id
    folders_of = defaultdict(set)  # series id -> Kavita paths of the series folders holding its files
    chapters = defaultdict(dict)  # (series id, folder) -> {chapter id: chapter}
    for sid, vols in volumes_by_series.items():
        for ch, path in chapter_files(vols):
            owner[path] = sid
            if path.startswith(root + "/"):
                folder = path[len(root) + 1 :].split("/")[0]
                folders_of[sid].add(f"{root}/{folder}")
                chapters[sid, folder][ch["id"]] = ch
            else:
                folders_of[sid].add(path.rsplit("/", 1)[0])
    names = defaultdict(set)
    for s in series:
        for name in series_names(s):
            names[normalize(name)].add(s["id"])

    out = {}
    for folder, files in folder_files.items():
        archives = [f for f in files if is_archive(f)]
        hits = Counter(owner.get(f"{root}/{folder}/{f}") for f in archives)
        missing = hits.pop(None, 0)
        rec = {
            "archives": len(archives),
            "archives_in_kavita": len(archives) - missing,
            "series_id": None,
            "series": [],
            "other_folders": [],
            "candidates": [],
        }
        if not archives:
            rec["status"] = "empty"
        elif not hits:
            rec["status"] = "unmatched"
            ci = {normalize(n) for n in (comicinfo_series or {}).get(folder, ()) if n}
            rec["candidates"] = [
                {
                    "id": sid,
                    "name": by_id[sid]["name"],
                    "folders": sorted(folders_of[sid]) or [by_id[sid].get("lowestFolderPath")],
                }
                for sid in sorted({sid for n in ci for sid in names.get(n, ())})
            ]
        else:
            sid = hits.most_common(1)[0][0]
            others = sorted(folders_of[sid] - {f"{root}/{folder}"})
            rec["status"] = "split" if len(hits) > 1 else "partial" if missing else "shared" if others else "matched"
            rec["series_id"], rec["other_folders"] = sid, others
            rec["series"] = [{"id": s, "name": by_id[s]["name"], "archives": n} for s, n in hits.most_common()]
            rec["chapters"] = list(chapters[sid, folder].values())
        out[folder] = rec
    return out
