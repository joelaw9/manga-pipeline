"""Tag rules (SPEC "Tag rules"): a series' genres and tags worked out from its stored records by the rule files.

The golden cases are real records of 11 series (tests/fixtures_api/tags, only the fields the rules read), each with
the lists the new rules give and, for review, what the old metadata tool wrote to Kavita.
"""

import json
import re
import shutil
from pathlib import Path

import pytest

from manga_pipeline.metadata import tags
from manga_pipeline.replies.sources import MangaBakaSeries, MangaBakaTag, MangaDexManga, MangaUpdatesSeries

from tests.builders import anilist_media, series_records
from tests.timing import budget

GOLDEN = Path(__file__).parent / "fixtures_api" / "tags"


def records(fixture):
    """Return a golden fixture's records ("<source>.<kind>" keys) as a check stores them."""
    return series_records(
        mangabaka=fixture.get("mangabaka.series"),
        mangaupdates=fixture.get("mangaupdates.series"),
        mangadex=fixture.get("mangadex.manga"),
        anilist=fixture.get("anilist.media"),
    )


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
    assert tags.compute(series_records(), rules) == ([], []), "no record, no list"


@budget()
def test_mangaupdates_plural_markers_and_a_final_s_are_one_name():
    assert tags.term_key("Concubine/s") == tags.term_key("Concubines") == tags.term_key("concubine"), "1 name"
    assert tags.term_key("Elf/ves") == tags.term_key("Elf"), "1 name"
    assert tags.display_name("Angel/s") == "Angel", "1 name"


@budget()
def test_a_top_up_adds_only_allowlisted_names_and_never_an_excluded_one(rules):
    assert tags.term_key("Full Color") in rules.allow_tags, "the case needs an excluded name that is also allowed"
    mb: MangaBakaSeries = {
        "id": 1,
        "genres": [],
        "tags_v2": [{"name": n, "weight": "core"} for n in ("Full Color", "Nobility", "Made-Up Tag")],
    }
    _, tag_list = tags.compute(series_records(mangabaka=mb), rules)
    assert "Nobility" in tag_list, "an allowlisted tag fills a short list"
    assert "Full Color" not in tag_list, "an excluded name is never added, allowed or not"
    assert "Made-Up Tag" not in tag_list, "a name in no allowlist is never added"


def mb_with(*named, genres=()) -> MangaBakaSeries:
    """Return a MangaBaka record with at least 10 tags (not sparse): the named (name, weight) ones plus fillers."""
    filler: list[MangaBakaTag] = [{"name": f"Filler {i}", "weight": "incidental"} for i in range(10)]
    tags_v2: list[MangaBakaTag] = [{"name": n, "weight": w} for n, w in named]
    return {"id": 1, "genres": list(genres), "tags_v2": tags_v2 + filler}


@budget()
def test_a_promoted_tag_needs_its_minimum_mangabaka_weight(rules):
    weak = tags.compute(series_records(mangabaka=mb_with(("Revenge", "incidental"))), rules)[1]
    strong = tags.compute(series_records(mangabaka=mb_with(("Revenge", "recurrent"))), rules)[1]
    assert "Revenge" not in weak, "promote_tags.txt: Revenge: recurrent"
    assert "Revenge" in strong, "promote_tags.txt: Revenge: recurrent"


@budget()
def test_a_mangaupdates_category_mangabaka_repeats_keeps_mangabakas_weight(rules):
    mb: MangaBakaSeries = {"id": 1, "genres": [], "tags_v2": [{"name": "Revenge", "weight": "incidental"}]}
    mu: MangaUpdatesSeries = {"genres": [], "categories": [{"category": "Revenge"}]}
    _, tag_list = tags.compute(series_records(mangabaka=mb, mangaupdates=mu), rules)
    assert "Revenge" not in tag_list, "MangaBaka weighs MangaUpdates' low-vote category: incidental is too weak"
    mu = {"genres": [], "categories": [{"category": "Revenge/s"}]}
    _, tag_list = tags.compute(series_records(mangabaka=mb, mangaupdates=mu), rules)
    assert "Revenge" in tag_list, "spelt otherwise, the category is another source's term: any weight"


@budget()
def test_without_mangabaka_mangaupdates_gives_the_genres_and_rule_terms(rules):
    mu: MangaUpdatesSeries = {
        "genres": [{"genre": "Fantasy"}, {"genre": "Romance"}],
        "categories": [{"category": "Revenge"}],
    }
    genres, tag_list = tags.compute(series_records(mangaupdates=mu), rules)
    assert genres[:2] == ["Fantasy", "Romance"], "MangaUpdates' genres stand in for MangaBaka's"
    assert "Revenge" in tag_list, "its categories are rule terms"


@budget()
def test_a_genre_short_series_is_topped_up_from_mangabaka_genres(rules):
    al = anilist_media(genres=["Action"], tags=[])
    mb = mb_with(("Martial Artist", "core"), genres=["action"])
    genres, _ = tags.compute(series_records(mangabaka=mb, anilist=al), rules)
    assert "Martial Arts" in genres, "a MangaBaka tag the table makes a genre fills a genre-short series"


@budget()
def test_null_anilist_fields_read_as_empty(rules):
    media = anilist_media(genres=None, tags=[{"name": "Magic", "rank": None, "isMediaSpoiler": None}])
    assert tags.compute(series_records(anilist=media), rules) == ([], []), "a null list or rank is none"


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


@budget()
def test_top_up_takes_last_names_last(rules):
    held = anilist_media(genres=[], tags=[{"name": f"Held {i}", "rank": 90, "isMediaSpoiler": False} for i in range(5)])
    both = mb_with(("Female Protagonist", "core"), ("Time Loop", "unweighted"))
    _, tag_list = tags.compute(series_records(mangabaka=both, anilist=held), rules)
    assert "Time Loop" in tag_list, "top_up_last.txt: a story tag fills the last place before a generic one"
    assert "Female Protagonist" not in tag_list, "top_up_last.txt: a generic tag comes after every other candidate"
    alone = mb_with(("Female Protagonist", "core"))
    _, tag_list = tags.compute(series_records(mangabaka=alone, anilist=held), rules)
    assert "Female Protagonist" in tag_list, "top_up_last.txt: with no other candidate the generic tag still fills"


@budget(load="fs")
def test_empty_last_file_keeps_order(tmp_path):
    folder = tmp_path / "rules"
    shutil.copytree(tags.RULES, folder)
    (folder / "top_up_last.txt").write_bytes(b"")
    empty = tags.load(folder)
    held = anilist_media(genres=[], tags=[{"name": f"Held {i}", "rank": 90, "isMediaSpoiler": False} for i in range(5)])
    record = series_records(
        mangabaka=mb_with(("Female Protagonist", "core"), ("Time Loop", "unweighted")), anilist=held
    )
    _, tag_list = tags.compute(record, empty)
    assert "Female Protagonist" in tag_list, "an empty top_up_last.txt leaves the top-up in weight order, core first"
    assert "Time Loop" not in tag_list, "an empty top_up_last.txt leaves the top-up in weight order, core first"


@budget(load="fs")
def test_last_name_in_allowlist_is_refused(tmp_path):
    folder = tmp_path / "rules"
    shutil.copytree(tags.RULES, folder)
    (folder / "top_up_last.txt").write_bytes(b"Time Loop\n")
    with pytest.raises(ValueError, match=re.escape("'Time Loop' is also in tag_allowlist.txt")):
        tags.load(folder)


@budget(load="fs")
def test_missing_rule_file_raises(tmp_path):
    folder = tmp_path / "rules"
    shutil.copytree(tags.RULES, folder)
    (folder / "top_up_last.txt").unlink()
    with pytest.raises(FileNotFoundError, match=re.escape("top_up_last.txt")):
        tags.load(folder)


def genre_tag(name, weight="unweighted", *, implied=False) -> MangaBakaTag:
    return {"name": name, "weight": weight, "is_genre": True, "implied_by_tag_ids": [7] if implied else []}


@budget()
def test_mangabaka_is_genre_tags_are_genres(rules):
    def genres_of(rating, *genre_tags, slugs=("action", "drama", "comedy", "romance")):
        # 4 genres already: the top-up adds none, so only the genre-tag reading can add one
        mb: MangaBakaSeries = {"id": 1, "genres": list(slugs), "content_rating": rating, "tags_v2": list(genre_tags)}
        return tags.compute(series_records(mangabaka=mb), rules)[0]

    assert "Fantasy" in genres_of("safe", genre_tag("Fantasy")), "a genre tag no tag implies is a genre"
    assert "Sci-Fi" not in genres_of("safe", genre_tag("Sci-Fi", implied=True)), "an implied genre tag is not"
    assert "Tragedy" not in genres_of("safe", genre_tag("Tragedy", "incidental")), "an incidental one is not"
    assert "Hentai" not in genres_of("safe", genre_tag("Smut")), "Smut counts only on an adult record"
    assert "Hentai" in genres_of("erotica", genre_tag("Smut")), "Smut on an erotica record is Hentai"
    assert "Hentai" in genres_of("safe", slugs=["smut"]), "MangaBaka's genres list is read as before, smut included"


@budget()
def test_allowlisted_library_name_tops_up(rules):
    _, tag_list = tags.compute(series_records(mangabaka=mb_with(("Time Skip", "defining"))), rules)
    assert "Time Skip" in tag_list, "a story tag kavita_enrich's library held is a top-up candidate (allowlist)"


@budget()
def test_spelling_variants_have_one_spelling(rules):
    md: MangaDexManga = {
        "id": "x",
        "attributes": {"tags": [{"attributes": {"group": "genre", "name": {"en": "Girls' Love"}}}]},
    }
    mb: MangaBakaSeries = {"id": 1, "genres": [], "tags_v2": []}  # MangaDex's spelling alone: the identity row
    genres, _ = tags.compute(series_records(mangabaka=mb, mangadex=md), rules)
    assert [g for g in genres if "love" in g.lower()] == ["Girls Love"], "1 spelling per name: Kavita's Girls Love"
