"""Series fields from stored records (SPEC "Metadata", Series fields)."""

from typing import TYPE_CHECKING

import pytest

from manga_pipeline.metadata import merge, tags

from tests.builders import anilist_media, series_records
from tests.timing import budget

if TYPE_CHECKING:
    from manga_pipeline.replies.sources import AniListStaffConnection


@pytest.fixture(scope="module")
def rules():
    return tags.load()


@budget()
def test_the_summary_is_the_first_source_that_has_one_as_plain_text(rules):
    records = series_records(
        anilist=anilist_media(description=None),
        mangadex={"id": "uuid", "attributes": {"description": {"en": "**Bold** [link](http://x)\n\n---\nCredits"}}},
        mangabaka={"id": 5, "description": "Third"},
    )
    assert merge.fields(records, {}, rules).summary == "Bold link", "MangaDex next: Markdown read, cut at the rule"
    records["anilist"] = anilist_media(description="A &amp; B<br>\nC<br>\n(Source: X)")
    assert merge.fields(records, {}, rules).summary == "A & B\n\nC", "AniList first: HTML read, source note dropped"


@budget()
@pytest.mark.parametrize(
    ("rating", "comicinfo", "kavita"),
    [
        ("safe", "Everyone", 3),
        ("suggestive", "Teen", 8),
        ("erotica", "Mature 17+", 10),
        ("pornographic", "Adults Only 18+", 13),
    ],
)
def test_the_age_rating_maps_mangabakas_content_rating(rules, rating, comicinfo, kavita):
    got = merge.fields(series_records(mangabaka={"id": 5, "content_rating": rating}), {}, rules)
    assert (got.age_rating, got.age_rating_kavita) == (comicinfo, kavita)


@budget()
def test_people_language_status_and_links(rules):
    staff: AniListStaffConnection = {
        "edges": [
            {"role": "Story & Art", "node": {"name": {"full": "A"}}},
            {"role": "Art", "node": {"name": {"full": "B"}}},
            {"role": "Original Story", "node": {"name": {"full": "C"}}},
        ]
    }
    records = series_records(
        anilist=anilist_media(staff=staff),
        mangadex={"id": "uuid", "attributes": {"originalLanguage": "ko"}},
        mangabaka={"id": 5, "status": "hiatus", "canonical_url": "https://mangabaka.org/manhwa/5/x"},
    )
    agreed = {
        "anilist": "7",
        "mangadex": "uuid",
        "mangabaka": "5",
        "myanimelist": "9",
        "mangaupdates": str(int("4gtb06", 36)),
    }
    got = merge.fields(records, agreed, rules)
    assert (got.writers, got.pencillers) == (("A",), ("A", "B")), "roles exactly Story, Art, Story & Art"
    assert (got.language, got.status) == ("ko", 1)
    assert got.links == (
        "https://anilist.co/manga/7",
        "https://mangadex.org/title/uuid",
        "https://mangabaka.org/manhwa/5/x",
        "https://myanimelist.net/manga/9",
    ), "a source with no record (MangaUpdates here) gets no link"
    assert got.kavita_ids == {"aniListId": 7, "malId": 9, "mangaBakaId": 5}


@budget()
def test_a_markdown_summary_loses_its_closing_source_note(rules):
    records = series_records(mangabaka={"id": 5, "description": "Story here.\n\n*(Source: Yen Press)*"})
    assert merge.fields(records, {}, rules).summary == "Story here.", "the note goes, as from HTML"


@budget()
def test_no_mangabaka_link_without_a_url_or_an_agreed_id(rules):
    assert merge.fields(series_records(mangabaka={"id": 5, "status": "completed"}), {}, rules).links == ()
