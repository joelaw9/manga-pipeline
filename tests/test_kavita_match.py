"""clients.kavita_match: folder to Kavita series matching by file path (folder statuses, the record fields of each
status), `relevant`, `normalize` (Kavita's own rule), `series_names`, `chapter_files` (SPEC "Services")."""

import pytest

from manga_pipeline.clients.kavita_match import chapter_files, match_folders, normalize, relevant, series_names

from tests.timing import budget

ROOT = "/shared/manga/mangas/Local"  # library 4's Local folder, as Kavita sees it
LIB = "/shared/manga/mangas"  # library 4's root


def series(sid, name, low, localized=None, original=None):
    """A Kavita series DTO (only the fields matching reads)."""
    return {"id": sid, "name": name, "localizedName": localized, "originalName": original, "lowestFolderPath": low}


def vols(*paths):
    """An /api/Series/volumes result: one volume, one chapter per file path, chapter ids 0, 1, ..."""
    return [
        {
            "id": 1,
            "minNumber": 0,
            "chapters": [
                {"id": i, "pages": 10, "pagesRead": 0, "files": [{"filePath": p, "bytes": 1}]}
                for i, p in enumerate(paths)
            ],
        }
    ]


@pytest.fixture
def library():
    """match_folders over one folder of every status. Twice spans Local and the Weeb source (lowest = library root);
    Liz lives outside Local, so its volumes are not fetched but it is still a candidate by its localized name."""
    ss = [
        series(1, "Alone", f"{ROOT}/Alone"),
        series(2, "Twice", LIB),
        series(3, "Half", f"{ROOT}/Half"),
        series(4, "Liz", f"{LIB}/Violet/Liz", localized="Rize Shop"),
        series(5, "A", f"{ROOT}/Mixed"),
        series(6, "B", f"{ROOT}/Mixed"),
    ]
    vs = {
        1: vols(f"{ROOT}/Alone/c1.cbz", f"{ROOT}/Alone/c2.cbz"),
        2: vols(f"{ROOT}/Twice/c1.cbz", f"{LIB}/Weeb/Twice/c1.cbz"),
        3: vols(f"{ROOT}/Half/c1.cbz"),
        5: vols(f"{ROOT}/Mixed/a.cbz", f"{ROOT}/Mixed/b.cbz"),
        6: vols(f"{ROOT}/Mixed/c.cbz"),
    }
    folders = {
        "Alone": ["c1.cbz", "c2.cbz", "cover.jpg"],
        "Twice": ["c1.cbz"],
        "Half": ["c1.cbz", "c2.cbz"],
        "Rize": ["c1.cbz"],
        "Never": ["c1.cbz"],
        "Empty": [".noxml"],
        "Mixed": ["a.cbz", "b.cbz", "c.cbz"],
    }
    ci = {"Rize": {"Rize Shop"}, "Never": {"Nobody Has This"}}
    return match_folders(ROOT, folders, ss, vs, ci)


# Statuses (module docstring)


@budget(load="pure")
@pytest.mark.parametrize(
    ("folder", "status"),
    [
        ("Alone", "matched"),
        ("Twice", "shared"),
        ("Half", "partial"),
        ("Rize", "unmatched"),
        ("Never", "unmatched"),
        ("Empty", "empty"),
        ("Mixed", "split"),
    ],
    ids=["matched", "shared", "partial", "unmatched-by-name", "unmatched-no-name", "empty", "split"],
)
def test_folder_status(library, folder, status):
    """Each folder gets the status its archives' owners define (module docstring table)."""
    assert library[folder]["status"] == status


@budget(load="pure")
def test_matched_folder_carries_its_series_and_chapters(library):
    """Every archive of Alone belongs to series 1: its id and both its chapters are in the record."""
    rec = library["Alone"]
    assert (rec["series_id"], [c["id"] for c in rec["chapters"]]) == (1, [0, 1])


@budget(load="pure")
def test_only_archives_count_in_a_folder(library):
    """cover.jpg beside Alone's 2 CBZs is not an archive: 2 archives, both in Kavita."""
    assert (library["Alone"]["archives"], library["Alone"]["archives_in_kavita"]) == (2, 2)


@budget(load="pure")
def test_shared_folder_names_the_series_other_folders(library):
    """Twice also holds a file under Weeb: that folder (a Kavita path) is listed, not this one."""
    assert library["Twice"]["other_folders"] == [f"{LIB}/Weeb/Twice"]


@budget(load="pure")
def test_shared_folder_lists_only_its_own_chapters(library):
    """Of Twice's 2 chapters only chapter 0, whose file is in the Local folder, belongs to the folder record."""
    assert [c["id"] for c in library["Twice"]["chapters"]] == [0]


@budget(load="pure")
def test_partial_folder_counts_archives_kavita_lacks(library):
    """Half has 2 archives and Kavita holds 1 of them."""
    rec = library["Half"]
    assert (rec["archives"], rec["archives_in_kavita"], rec["series_id"]) == (2, 1, 3)


@budget(load="pure")
def test_unmatched_candidate_by_localized_name(library):
    """Rize's ComicInfo Series "Rize Shop" is Liz's localized name; Liz's volumes were not fetched, so its folder
    is its lowestFolderPath."""
    assert library["Rize"]["candidates"] == [{"id": 4, "name": "Liz", "folders": [f"{LIB}/Violet/Liz"]}]


@budget(load="pure")
def test_unmatched_without_a_name_match_has_no_candidates(library):
    """A ComicInfo Series no series carries gives no candidate and no series id."""
    assert (library["Never"]["candidates"], library["Never"]["series_id"]) == ([], None)


@budget(load="pure")
def test_empty_folder_has_no_series(library):
    """A folder without archives carries no series id and counts 0 archives."""
    assert (library["Empty"]["series_id"], library["Empty"]["archives"]) == (None, 0)


@budget(load="pure")
def test_split_folder_takes_the_series_holding_most_archives(library):
    """Mixed: series 5 holds 2 archives, series 6 holds 1; the record lists both, most archives first."""
    rec = library["Mixed"]
    assert (rec["series_id"], rec["series"]) == (
        5,
        [{"id": 5, "name": "A", "archives": 2}, {"id": 6, "name": "B", "archives": 1}],
    )


@budget(load="pure")
def test_split_folder_lists_the_chosen_series_chapters(library):
    """Mixed's chapters are series 5's two chapters in that folder."""
    assert [c["id"] for c in library["Mixed"]["chapters"]] == [0, 1]


@budget(load="pure")
def test_candidate_folders_come_from_its_files_when_fetched():
    """A candidate whose volumes were fetched lists the Kavita folders holding its files, sorted; a ComicInfo Series
    matches the original name too, whatever its case and punctuation."""
    ss = [series(7, "Nine", LIB, original="Kyuu-Bi")]
    vs = {7: vols(f"{ROOT}/Nine B/c1.cbz", f"{ROOT}/Nine A/c1.cbz")}
    m = match_folders(ROOT + "/", {"Loose": ["x.CBZ"]}, ss, vs, {"Loose": {"kyuu bi", ""}})
    assert m["Loose"]["candidates"] == [{"id": 7, "name": "Nine", "folders": [f"{ROOT}/Nine A", f"{ROOT}/Nine B"]}]


@budget(load="pure")
def test_archive_extensions_ignore_case():
    """An upper-case .CBZ in Kavita is still the folder's archive: the folder matches."""
    m = match_folders(ROOT, {"Up": ["C1.CBZ"]}, [series(8, "Up", f"{ROOT}/Up")], {8: vols(f"{ROOT}/Up/C1.CBZ")})
    assert m["Up"]["status"] == "matched"


# relevant


@budget(load="pure")
@pytest.mark.parametrize(
    ("low", "want"),
    [
        (f"{ROOT}/A", True),
        (ROOT, True),
        (LIB, True),
        (f"{LIB}/", True),
        (f"{LIB}/Weeb/A", False),
        (f"{ROOT}2/A", False),
        (None, False),
        ("", False),
    ],
    ids=[
        "inside-local",
        "local-itself",
        "library-root",
        "library-root-slash",
        "other-source",
        "sibling-prefix",
        "no-folder",
        "empty-folder",
    ],
)
def test_relevant(low, want):
    """A series can hold Local files when its lowestFolderPath is inside Local or an ancestor of it (a series spanning
    sources has the common parent); a folder that only shares Local's name prefix is neither."""
    assert relevant(series(1, "x", low), ROOT) is want


# normalize (Kavita v0.9.1.4 StringExtensions.ToNormalized: keep [\p{L}0-9+!\uff0a\uff01\uff0b], then lower-case)


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "Gal Maid & Villainess: Only Milady's Happy End Will Win!",
            "Gal Maid  Villainess: Only Miladys Happy End Will Win!",
        ),
        ("Swordmaster\u2019s Youngest Son", "Swordmaster's Youngest Son"),
        ("SOLO LEVELING", "solo leveling"),
        ("Cafe\u0301", "Cafe"),
    ],
    ids=["punctuation-spaces", "curly-apostrophe", "case", "combining-mark-dropped"],
)
def test_normalize_same_key(a, b):
    """Names differing only in spaces, punctuation (straight / curly apostrophes, &, :), case or a combining mark get
    the same key: Kavita drops all of them."""
    assert normalize(a) == normalize(b)


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Master Villainess the Invincible!", "Master Villainess the Invincible"),  # sample library
        ("Heavenly Demon Reborn!", "Heavenly Demon Reborn\uff01"),  # ASCII vs fullwidth "!"
        (
            "\uff33\uff4f\uff4c\uff4f \uff2c\uff45\uff56\uff45\uff4c\uff49\uff4e\uff47",
            "Solo Leveling",
        ),  # full-width; no NFKC folding
        ("Caf\u00e9", "Cafe\u0301"),  # precomposed letter kept, mark dropped
        ("Kill Me Now \uff12", "Kill Me Now 2"),
    ],  # only ASCII digits kept
    ids=[
        "exclamation-kept",
        "fullwidth-exclamation",
        "fullwidth-letters",
        "precomposed-vs-decomposed",
        "non-ascii-digit",
    ],
)
def test_normalize_different_key(a, b):
    """Kavita keeps "!" (I Failed to Oust the Villain!, Trash Belongs in the Trash Can!), fullwidth forms as they
    are, and precomposed letters; a ComicInfo Series differing there parses to another Kavita series."""
    assert normalize(a) != normalize(b)


@budget(load="pure")
@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("Kill Me Now 2", "killmenow2"),
        ("Trash Belongs in the Trash Can!", "trashbelongsinthetrashcan!"),
        ("A+B\uff0bC\uff0aD\uff01", "a+b\uff0bc\uff0ad\uff01"),
        ("Straße", "straße"),
        (None, ""),
        ("", ""),
        ("İstanbul", "istanbul"),
        ("ΟΔΟΣ", "οδοσ"),
        ("A\U0001d400B", "ab"),
    ],
    ids=[
        "name",
        "exclamation",
        "plus-and-fullwidth",
        "sharp-s",
        "none",
        "empty",
        "dotted-capital-i",
        "greek-sigma-per-char",
        "astral-letter-dropped",
    ],
)
def test_normalize_value(name, want):
    """The key is letters, ASCII digits, + ! and full-width * ! +, lower-cased (not case-folded: ß stays); a missing
    name is the empty key. Like .NET, each character is lowered on its own ("İ" gives "i", a final capital sigma
    "\u03c3", never the context-sensitive "ς"), and a letter above U+FFFF (a UTF-16 surrogate pair, not \\p{L} to .NET)
    is dropped."""
    assert normalize(name) == want


# series_names / chapter_files


@budget(load="pure")
def test_series_names_in_key_order_without_blanks():
    """name, localizedName, originalName in that order; empty and missing names are left out."""
    assert series_names({"name": "Liz", "localizedName": "", "originalName": "Rize"}) == ["Liz", "Rize"]


@budget(load="pure")
def test_series_names_of_no_series_is_empty():
    """A missing DTO has no names."""
    assert series_names(None) == []


@budget(load="pure")
def test_chapter_files_pairs_every_file_with_its_chapter():
    """Every file of every chapter of every volume, in order; a volume without chapters adds nothing."""
    v = [*vols("/a/1.cbz", "/a/2.cbz"), {"id": 2}]
    assert [(c["id"], p) for c, p in chapter_files(v)] == [(0, "/a/1.cbz"), (1, "/a/2.cbz")]
