"""Tag rules (SPEC "Tag rules"): a series' genres and tags worked out from its stored records by the rule files.

The golden cases are real records of 11 series (tests/fixtures_api/tags, only the fields the rules read), each with
the lists the new rules give and, for review, what the old metadata tool wrote to Kavita.
"""

import json
import shutil
from pathlib import Path

import pytest

from manga_pipeline.metadata import tags

from tests.timing import budget

GOLDEN = Path(__file__).parent / "fixtures_api" / "tags"


def records(fixture):
    return {tuple(key.split(".")): fixture[key] for key in fixture if "." in key}


@pytest.fixture(scope="module")
def rules():
    return tags.load()


@budget()
@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*.json")), ids=lambda p: p.stem)
def test_golden_series_get_their_genres_and_tags(path, rules):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    genres, tag_list = tags.compute(records(fixture), rules)
    assert {"genres": genres, "tags": tag_list} == fixture["expected"], f"{fixture['series']}: the rules' output"


@budget(load="fs")
def test_a_rule_edit_changes_the_lists_and_the_fingerprint(tmp_path, rules):
    fixture = json.loads((GOLDEN / "a-kingdom-of-quartz.json").read_text(encoding="utf-8"))
    folder = tmp_path / "rules"
    shutil.copytree(tags.RULES, folder)
    with (folder / "consolidation.csv").open("a", encoding="utf-8", newline="\n") as f:
        f.write("Test,tag,Angels,,remove,\n")
    edited = tags.load(folder)
    assert edited.fingerprint != rules.fingerprint, "an edited rule file changes the fingerprint"
    before, after = tags.compute(records(fixture), rules)[1], tags.compute(records(fixture), edited)[1]
    assert "Angels" in before, "the edit reaches the series from its stored records alone"
    assert "Angels" not in after, "the edit reaches the series from its stored records alone"


@budget()
def test_a_series_no_source_knows_has_no_genres_or_tags(rules):
    assert tags.compute({}, rules) == ([], []), "no record, no list"


@budget()
def test_mangaupdates_plural_markers_and_a_final_s_are_one_name():
    assert tags.term_key("Concubine/s") == tags.term_key("Concubines") == tags.term_key("concubine"), "1 name"
    assert tags.term_key("Elf/ves") == tags.term_key("Elf"), "1 name"
    assert tags.display_name("Angel/s") == "Angel", "1 name"


@budget()
def test_a_top_up_adds_only_allowlisted_names_and_never_an_excluded_one(rules):
    assert tags.term_key("Full Color") in rules.allow_tags, "the case needs an excluded name that is also allowed"
    mb = {"genres": [], "tags_v2": [{"name": n, "weight": "core"} for n in ("Full Color", "Nobility", "Made-Up Tag")]}
    _, tag_list = tags.compute({("mangabaka", "series"): mb}, rules)
    assert "Nobility" in tag_list, "an allowlisted tag fills a short list"
    assert "Full Color" not in tag_list, "an excluded name is never added, allowed or not"
    assert "Made-Up Tag" not in tag_list, "a name in no allowlist is never added"


def mb_with(*named, genres=()):
    """Return a MangaBaka record with at least 10 tags (not sparse): the named (name, weight) ones plus fillers."""
    filler = [{"name": f"Filler {i}", "weight": "incidental"} for i in range(10)]
    return {"genres": list(genres), "tags_v2": [{"name": n, "weight": w} for n, w in named] + filler}


@budget()
def test_a_promoted_tag_needs_its_minimum_mangabaka_weight(rules):
    weak = tags.compute({("mangabaka", "series"): mb_with(("Revenge", "incidental"))}, rules)[1]
    strong = tags.compute({("mangabaka", "series"): mb_with(("Revenge", "recurrent"))}, rules)[1]
    assert "Revenge" not in weak, "promote_tags.txt: Revenge: recurrent"
    assert "Revenge" in strong, "promote_tags.txt: Revenge: recurrent"


@budget()
def test_a_mangaupdates_category_mangabaka_repeats_keeps_mangabakas_weight(rules):
    mb = {"genres": [], "tags_v2": [{"name": "Revenge", "weight": "incidental"}]}
    mu = {"genres": [], "categories": [{"category": "Revenge"}]}
    _, tag_list = tags.compute({("mangabaka", "series"): mb, ("mangaupdates", "series"): mu}, rules)
    assert "Revenge" not in tag_list, "MangaBaka weighs MangaUpdates' low-vote category: incidental is too weak"
    mu = {"genres": [], "categories": [{"category": "Revenge/s"}]}
    _, tag_list = tags.compute({("mangabaka", "series"): mb, ("mangaupdates", "series"): mu}, rules)
    assert "Revenge" in tag_list, "spelt otherwise, the category is another source's term: any weight"


@budget()
def test_without_mangabaka_mangaupdates_gives_the_genres_and_rule_terms(rules):
    mu = {"genres": [{"genre": "Fantasy"}, {"genre": "Romance"}], "categories": [{"category": "Revenge"}]}
    genres, tag_list = tags.compute({("mangaupdates", "series"): mu}, rules)
    assert genres[:2] == ["Fantasy", "Romance"], "MangaUpdates' genres stand in for MangaBaka's"
    assert "Revenge" in tag_list, "its categories are rule terms"


@budget()
def test_a_genre_short_series_is_topped_up_from_mangabaka_genres(rules):
    al = {"genres": ["Action"], "tags": []}
    mb = mb_with(("Martial Artist", "core"), genres=["action"])
    genres, _ = tags.compute({("mangabaka", "series"): mb, ("anilist", "media"): al}, rules)
    assert "Martial Arts" in genres, "a MangaBaka tag the table makes a genre fills a genre-short series"


@budget()
def test_null_anilist_fields_read_as_empty(rules):
    media = {"genres": None, "tags": [{"name": "Magic", "rank": None, "isMediaSpoiler": None}]}
    assert tags.compute({("anilist", "media"): media}, rules) == ([], []), "a null list or rank is none"


@budget(load="fs")
@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("promote_tags.txt", "Revenge: recurrant\n", "promote_tags.txt"),
        (
            "consolidation.csv",
            "group,from_kind,from,series,to_kind,to\nX,tag,Angels,,removed,\n",
            "consolidation.csv line 2",
        ),
        ("derived_tags.yaml", "tag: Not A List\n", "derived_tags.yaml"),
        ("derived_tags.yaml", "- tag: X\n  rule: [{all: [[A]]}]\n", "derived_tags.yaml"),
        ("derived_tags.yaml", "- tag: Everything\n  rules: [{all: []}]\n", "at least 1 group"),
        ("derived_tags.yaml", "- tag: 123\n  rules: [{all: [[A]]}]\n", "tag must be a name"),
    ],
)
def test_a_rule_file_that_is_not_valid_stops_the_load(tmp_path, name, text, problem):
    folder = tmp_path / "rules"
    shutil.copytree(tags.RULES, folder)
    (folder / name).write_bytes(text.encode("utf-8"))
    with pytest.raises(ValueError, match=problem):
        tags.load(folder)
