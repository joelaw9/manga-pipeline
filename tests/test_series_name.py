"""The series name rule, the free-name test and what a rename must do (SPEC "Retroactive updates", Series names)."""

import pytest

from manga_pipeline.metadata.series_name import Chosen, Due, Own, chosen, due, folder_taken, taken

from tests.timing import budget

OWN = Own("Ore Dake", None, None, "Ore Dake")


def records(**titles):
    """Return a series' records with the given titles (None for a source given nothing).

    titles: english, romaji (AniList); md_alt, md_title, md_romanized (MangaDex); mb_en, mb_romanized (MangaBaka).
    """
    english, romaji = titles.get("english"), titles.get("romaji")
    md_alt, md_title, md_romanized = titles.get("md_alt", ()), titles.get("md_title"), titles.get("md_romanized", ())
    mb_en, mb_romanized = titles.get("mb_en", ()), titles.get("mb_romanized")
    al = {"id": 1, "title": {"english": english, "romaji": romaji, "native": None}} if english or romaji else None
    md = None
    if md_alt or md_title or md_romanized:
        alt = [{"en": t} for t in md_alt] + [{"ja-ro": t} for t in md_romanized]
        md = {"id": "u", "attributes": {"title": {"en": md_title} if md_title else {}, "altTitles": alt}}
    mb = None
    if mb_en or mb_romanized:
        mb = {"id": 2, "secondary_titles": {"en": [{"title": t} for t in mb_en]}, "romanized_title": mb_romanized}
    return {"anilist": al, "mangadex": md, "mangabaka": mb, "mangaupdates": None}


@pytest.mark.parametrize(
    ("own", "recs", "person", "want"),
    [
        (
            OWN,
            records(english="Solo Leveling", romaji="Ore Dake"),
            None,
            Chosen("Solo Leveling", "Ore Dake", "anilist"),
        ),
        (OWN, records(english="Solo Leveling @comic"), None, Chosen("Solo Leveling", "", "anilist")),
        (OWN, records(english="X", romaji="Ore Dake @COMIC"), None, Chosen("X", "Ore Dake", "anilist")),
        (OWN, records(english="Solo Leveling"), "Mine", Chosen("Mine", "", "person")),
        (
            Own("Kavita Name", "Loc", None, "Old"),
            records(md_alt=["Loc"]),
            None,
            Chosen("Loc", "", "mangadex-alt-en"),
        ),
        (
            Own("Kavita Name", None, None, "Old"),
            records(mb_en=["kavita name"]),
            None,
            Chosen("Kavita Name", "", "mangabaka-en"),
        ),
        (
            Own("Kavita Name", None, None, "Old"),
            records(md_title="Kavita Name"),
            None,
            Chosen("Kavita Name", "", "mangadex-title-en"),
        ),
        (
            Own("Kavita Name", None, None, "Old"),
            records(md_alt=["Kavita Name"], md_romanized=["Kavita Name"]),
            None,
            Chosen("Old", "Kavita Name", "comicinfo"),
        ),
        (
            Own("Kavita Name", None, None, "Old"),
            records(romaji="Kavita Name", md_alt=["Kavita Name"]),
            None,
            Chosen("Old", "Kavita Name", "comicinfo"),
        ),
        (OWN, records(), None, Chosen("Ore Dake", "", "comicinfo")),
        (
            Own("Solo-Leveling", None, None, "Solo-Leveling"),
            records(english="solo leveling"),
            None,
            Chosen("Solo-Leveling", "", "anilist"),
        ),
        (OWN, records(english="Ore Dake", romaji="ore dake"), None, Chosen("Ore Dake", "", "anilist")),
        (
            Own("Kavita Name", "Romanized", None, "Old"),
            records(english="New", mb_romanized="romanized"),
            None,
            Chosen("New", "Romanized", "anilist"),
        ),
    ],
)
@budget(load="pure")
def test_the_name_rule_picks_in_order_and_keeps_the_old_tool_exclusions(own, recs, person, want):
    assert chosen(own, recs, person) == want, "SPEC 'Series names': person, AniList English, fallbacks, ComicInfo name"


@budget(load="pure")
def test_a_name_is_free_unless_another_series_holds_it_under_normalising():
    others = {"sololeveling": "Kavita series 3 (Solo Leveling) has that name"}
    assert taken("Solo-Leveling", others) == "Kavita series 3 (Solo Leveling) has that name", "normalised"
    assert taken("Other", others) is None, "free"
    assert taken("-", others) == "its name has no letter or digit Kavita keeps", "an empty key is never free"


@budget(load="pure")
def test_a_folder_holds_a_name_by_its_safe_name_in_any_case_or_normalised():
    assert folder_taken("Solo Leveling", ["solo leveling"]) == "solo leveling", "case ignored"
    assert folder_taken("Solo Leveling", ["Solo-Leveling"]) == "Solo-Leveling", "Kavita's normalising"
    assert folder_taken("Solo Leveling", ["Other"]) is None, "free"


@pytest.mark.parametrize(
    ("own", "localized", "folder", "want"),
    [
        (
            Own("Old", "Old", None, "Old"),
            "Romaji",
            "library/X/Old",
            Due(files=True, kavita_name=True, localized=False, folder=True),
        ),
        (
            Own("New", "Romaji", None, "New"),
            "Romaji",
            "library/X/Old",
            Due(files=False, kavita_name=False, localized=False, folder=True),
        ),
        (
            Own("Old", "Romaji", None, "New"),
            "Romaji",
            "library/X/New",
            Due(files=False, kavita_name=True, localized=False, folder=False),
        ),
        (
            Own("New", "Other", None, "New"),
            "Other",
            "library/X/New",
            Due(files=False, kavita_name=False, localized=True, folder=False),
        ),
        (
            Own("New", "Romaji", None, "New"),
            "romaji",
            "library/X/New",
            Due(files=False, kavita_name=False, localized=False, folder=False),
        ),
    ],
)
@budget(load="pure")
def test_due_names_each_fault_apart(own, localized, folder, want):
    assert due(own, localized, folder, Chosen("New", "Romaji", "anilist")) == want, (
        "files, Kavita name, localized, folder"
    )


@budget(load="pure")
def test_a_series_without_a_romaji_keeps_its_localized_name():
    got = due(Own("New", "Something", None, "New"), "Something", "library/X/New", Chosen("New", "", "anilist"))
    assert not got.any(), "the old tool never cleared a localized name"
