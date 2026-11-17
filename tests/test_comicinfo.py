"""ComicInfo v2.1 (comicinfo; SPEC "Metadata", "Publishing"): what Suwayomi writes vs what the metadata stage writes,
every violation kind the validator reports, the value types, `field_ok`, `read_fields`, `read_all` and `build`. Ported
from the bulk converter's tests/test_comicinfo.py.
"""

import xml.etree.ElementTree as ET

import pytest

from manga_pipeline import comicinfo

from tests import pages as fx
from tests.timing import budget

TY = "{http://www.w3.org/2001/XMLSchema}"  # the namespace Suwayomi puts its Tachiyomi status tag in


def doc(body):
    """A ComicInfo document around the given child elements."""
    return f'<?xml version="1.0" encoding="utf-8"?><ComicInfo>{body}</ComicInfo>'.encode()


@pytest.fixture
def part_one():
    """A built ComicInfo with Kavita's LocalizedSeries extension, BlackAndWhite, ScanInformation and full Page
    entries (A Fairytale for the Demon Lord, part 1).
    """
    fields = {
        "Series": "A Fairytale for the Demon Lord",
        "LocalizedSeries": "Mawangeul Wihan Donghwa",
        "Number": "1",
        "BlackAndWhite": "No",
        "ScanInformation": "Easy Going Scans",
        "PageCount": 2,
    }
    pages = [
        {"Image": 0, "Type": "FrontCover", "ImageSize": 52011, "ImageWidth": 800, "ImageHeight": 1200},
        {"Image": 1, "DoublePage": "true", "ImageSize": 98304, "ImageWidth": 1600, "ImageHeight": 1200},
    ]
    return comicinfo.build(fields, pages)


# validate


@budget(load="fs")
def test_suwayomi_layout_is_invalid_for_known_reasons():
    assert comicinfo.validate(fx.suwayomi_comicinfo()) == [
        f"unknown element {TY}PublishingStatusTachiyomi",
        "element Day out of schema order",
        "element Month out of schema order",
        "element Year out of schema order",
    ], "Suwayomi's Tachiyomi status tag and its Day/Month/Year after Web are the only 4 violations"


@budget(load="pure")
def test_pipeline_layout_is_valid():
    assert comicinfo.validate(fx.pipeline_comicinfo(volume="2", cover=True)) == [], (
        "the schema-ordered layout with a front-cover Page entry has no violation"
    )


@budget(load="pure")
def test_side_story_volume_must_be_integer():
    assert comicinfo.validate(fx.pipeline_comicinfo(volume="1.5")) == ["Volume='1.5' is not a valid xs:int"], (
        "Kavita accepts 1.5 volumes, the v2.1 schema does not"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("xml", "want"),
    [
        (b"", "parse-error: "),
        (b"<?xml version='1.0' encoding='bogus'?><ComicInfo/>", "parse-error: "),
        (b"<Comic><Title>t</Title></Comic>", "root element is 'Comic', expected 'ComicInfo'"),
    ],
    ids=["zero-byte", "bogus-encoding", "wrong-root"],
)
def test_unreadable_documents_report_one_error(xml, want):
    errors = comicinfo.validate(xml)
    assert (len(errors), errors[0][: len(want)]) == (1, want), (
        "a document that does not parse, or whose root is not ComicInfo, gives exactly 1 error saying so"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("<Title>a</Title><Title>b</Title>", ["duplicate element Title"]),
        ("<Pages><Pg Image='0'/></Pages>", ["unknown element Pages/Pg"]),
        ("<Pages><Page Type='Story'/></Pages>", ["Page missing required Image attribute"]),
        ("<Pages><Page Image='0' Colour='red'/></Pages>", ["unknown Page attribute Colour"]),
        ("<Pages><Page Image='x'/></Pages>", ["Page/@Image='x' is not a valid xs:int"]),
        ("<Pages><Page Image='0' DoublePage='yes'/></Pages>", ["Page/@DoublePage='yes' is not a valid xs:boolean"]),
        ("<Pages><Page Image='0' Type='Cover'/></Pages>", ["Page/@Type='Cover' is not a valid ComicPageType"]),
        ("<AgeRating>PG-13</AgeRating>", ["AgeRating='PG-13' is not a valid AgeRating"]),
    ],
    ids=[
        "duplicate",
        "pages-child",
        "page-no-image",
        "page-attribute",
        "page-int",
        "page-boolean",
        "page-type",
        "enum",
    ],
)
def test_violation_kinds(body, want):
    assert comicinfo.validate(doc(body)) == want, "each schema rule reports its own violation"


@budget(load="pure")
@pytest.mark.parametrize(
    "body",
    [
        "<Pages><Page Image='0' DoublePage='1'/><Page Image='1' DoublePage='false'/></Pages>",
        "<Pages><Page Image='0' Type='FrontCover InnerCover'/></Pages>",
        "<Volume>-1</Volume>",
        "<Count> 12 </Count>",
        "<Summary></Summary>",
        "<CommunityRating>0</CommunityRating>",
        "<CommunityRating>5.0</CommunityRating>",
    ],
    ids=["booleans", "page-type-list", "negative-int", "padded-int", "empty-value", "rating-floor", "rating-ceiling"],
)
def test_valid_values(body):
    assert comicinfo.validate(doc(body)) == [], (
        "xs:boolean 1/false, a ComicPageType list, signed and padded integers, an empty element, a Rating 0..5 pass"
    )


@budget(load="pure")
@pytest.mark.parametrize("value", ["5.1", "-0.5", "five"], ids=["over-5", "negative", "not-a-number"])
def test_rating_outside_0_to_5_is_invalid(value):
    assert comicinfo.validate(doc(f"<CommunityRating>{value}</CommunityRating>")) == [
        f"CommunityRating='{value}' is not a valid Rating"
    ], "CommunityRating is a decimal in 0..5"


# field_ok


@budget(load="pure")
@pytest.mark.parametrize(
    ("name", "value", "want"),
    [
        ("Series", "Anything at all", True),
        ("Volume", "2", True),
        ("Volume", "1.5", False),
        ("Manga", "YesAndRightToLeft", True),
        ("Manga", "Maybe", False),
        ("CommunityRating", "4.5", True),
        ("CommunityRating", "6", False),
        ("BlackAndWhite", "No", True),
        ("Pages", "", False),
        ("PublishingStatusTachiyomi", "Completed", False),
    ],
    ids=[
        "string",
        "int",
        "int-decimal",
        "enum",
        "enum-unknown",
        "rating",
        "rating-over-5",
        "yes-no",
        "pages",
        "unknown-element",
    ],
)
def test_field_ok(name, value, want):
    assert comicinfo.field_ok(name, value) is want, (
        "a ComicInfo element other than Pages, with a value valid for its schema type"
    )


# build


@budget(load="pure")
def test_built_part_one_is_valid(part_one):
    assert comicinfo.validate(part_one) == [], "every element given builds into a valid document"


@budget(load="pure")
def test_localized_series_sits_right_after_series(part_one):
    assert part_one.index(b"<Series>") < part_one.index(b"<LocalizedSeries>") < part_one.index(b"<Number>"), (
        "Kavita's LocalizedSeries extension is placed between Series and Number"
    )


@budget(load="pure")
def test_page_entries_keep_their_attributes(part_one):
    assert b'<Page Image="1" DoublePage="true" ImageSize="98304" ImageWidth="1600" ImageHeight="1200" />' in part_one, (
        "a Page entry is written with every attribute given, in the given order"
    )


@budget(load="pure")
def test_series_after_pages_is_out_of_order(part_one):
    bad = part_one.replace(b"<Series>A Fairytale for the Demon Lord</Series>", b"").replace(
        b"</ComicInfo>", b"<Series>A Fairytale for the Demon Lord</Series></ComicInfo>"
    )
    assert "element Series out of schema order" in comicinfo.validate(bad), "the validator enforces the schema order"


@budget(load="pure")
def test_black_and_white_must_be_yes_no(part_one):
    assert comicinfo.validate(part_one.replace(b">No<", b">Maybe<")) == [
        "BlackAndWhite='Maybe' is not a valid YesNo"
    ], "BlackAndWhite is the YesNo enumeration"


@budget(load="pure")
def test_build_skips_empty_fields():
    xml = comicinfo.build({"Series": "S", "Title": "", "Volume": None, "Number": 3})
    assert comicinfo.read_fields(xml, ("Series", "Title", "Volume", "Number")) == {
        "Series": "S",
        "Title": "",
        "Volume": "",
        "Number": "3",
    }, "the given values are written"
    assert (b"<Title" in xml, b"<Volume" in xml, b"<Pages" in xml) == (False, False, False), (
        "empty fields and absent pages leave no element"
    )


@budget(load="pure")
def test_build_refuses_unknown_elements():
    with pytest.raises(ValueError, match=r"not ComicInfo elements: \['PublishingStatusTachiyomi'\]"):
        comicinfo.build({"Series": "S", "PublishingStatusTachiyomi": "Completed"})


@budget(load="pure")
def test_build_refuses_an_invalid_value():
    with pytest.raises(ValueError, match=r"built ComicInfo is invalid: \[\"Volume='1\.5' is not a valid xs:int\"\]"):
        comicinfo.build({"Series": "S", "Volume": "1.5"})


# read_fields, read_all


@budget(load="pure")
def test_read_fields_of_a_suwayomi_file():
    assert comicinfo.read_fields(fx.suwayomi_comicinfo(title="Chapter 4 : Where Is She?", number="4")) == {
        "Series": "Some Series",
        "Title": "Chapter 4 : Where Is She?",
        "Number": "4",
        "Volume": "",
        "Count": "",
    }, "the default fields; absent ones are empty strings"


@budget(load="pure")
def test_read_all_of_a_suwayomi_file():
    assert comicinfo.read_all(fx.suwayomi_comicinfo()) == {
        "Title": "Chapter 1",
        "Series": "Some Series",
        "Number": "1",
        "Summary": "A summary.",
        "Writer": "Writer",
        "Penciller": "Artist",
        "Genre": "Action, Fantasy",
        "Web": "https://example.org/series/chapter/1",
        f"{TY}PublishingStatusTachiyomi": "Completed",
        "Day": "23",
        "Month": "9",
        "Year": "2024",
    }, "every non-empty top-level element as written, unknown elements included"


@budget(load="pure")
def test_read_all_skips_pages_and_empty_elements():
    assert comicinfo.read_all(fx.pipeline_comicinfo(title="", cover=True)) == {
        "Series": "Some Series",
        "Number": "1",
        "Year": "2024",
        "Month": "9",
        "Day": "23",
        "Translator": "Group",
        "Manga": "Yes",
    }, "Pages and empty elements are left out"


@budget(load="pure")
def test_read_all_refuses_xml_that_does_not_parse():
    with pytest.raises(ET.ParseError):
        comicinfo.read_all(b"<ComicInfo>")
