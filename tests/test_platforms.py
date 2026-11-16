"""Official platform lists (SPEC "Metadata", platform lists; "Publishing"): which page a series' links name, the Tapas
and Webtoons clients on replies recorded from the sites, and the platform-seasons basis and titles against the bulk
converter's records."""

import json
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import db, intake, names
from manga_pipeline.clients import http
from manga_pipeline.metadata import episodes, naming, platforms, sources

from tests.builders import present
from tests.fakes.http import CannedService, reply
from tests.test_naming_resolver import _rows
from tests.timing import budget
from tests.world import World

WT = "https://www.webtoons.com"
PLATFORMS = Path(__file__).parent / "fixtures_api" / "platforms"
RECORDS = Path(__file__).parent / "fixtures_api" / "records"


def tapas_service(slug):
    """A Tapas service answering from the recorded replies of 1 series."""
    html = (PLATFORMS / f"tapas-{slug}-info.html").read_bytes()
    series_id = html.decode().split("tapastic://series/")[1].split("/")[0]
    answers = {f"/series/{slug}/info": reply(html)}
    for path in sorted(PLATFORMS.glob(f"tapas-{slug}-episodes-*.json")):
        page = path.stem.rsplit("-", 1)[1]
        answers[f"/series/{series_id}/episodes?page={page}&"] = json.loads(path.read_text(encoding="utf-8"))
    return CannedService("tapas", answers)


def tapas_client(slug):
    return sources.Tapas(tapas_service(slug))


def webtoons_client(kind, number):
    answers = {
        f"/api/v1/{kind}/{number}/episodes": json.loads(
            (PLATFORMS / f"webtoons-{kind}-{number}-episodes-1.json").read_text(encoding="utf-8")
        ),
        "/episodeList": reply((PLATFORMS / f"webtoons-{kind}-{number}-list.html").read_bytes()),
    }
    return sources.Webtoons(CannedService("webtoons", answers))


def stored_list(platform, ref):
    """The platform list a check stores, built through the clients from the recorded replies."""
    if platform == platforms.TAPAS:
        client = tapas_client(ref)
        series_id = client.series_id(ref)
        items, total = present(client.episodes(series_id))
        return platforms.record(platform, ref, items, listed_total=total, series_id=series_id)
    client = webtoons_client(*ref.split(":"))
    app_only = client.app_only(ref)
    return platforms.record(platform, ref, client.episodes(ref), app_only=bool(app_only))


@budget(load="pure")
@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://tapas.io/series/brother-knows-best/info", "brother-knows-best"),
        ("https://www.tapas.io/series/brother-knows-best/", "brother-knows-best"),
        ("https://tapas.io/episode/123", None),
        ("https://www.mangaupdates.com/series/2c6248h/x", None),
    ],
)
def test_a_tapas_link_gives_its_series_slug(url, want):
    assert platforms.tapas_ref(url) == want


@budget(load="pure")
@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://www.webtoons.com/en/fantasy/regina-lena/list%3Ftitle_no=3920", "webtoon:3920"),
        ("https://www.webtoons.com/en/fantasy/x/list?title_no=3920", "webtoon:3920"),
        ("https://www.webtoons.com/en/canvas/x/list?title_no=3164", "canvas:3164"),
        ("https://www.webtoons.com/en/challenge/x/list?title_no=3164", "canvas:3164"),
        ("https://comic.naver.com/webtoon/list%3FtitleId=786910", None),
        ("https://www.webtoons.com/en/fantasy/x/list", None),
    ],
    ids=["encoded", "plain", "canvas", "challenge", "naver", "no-number"],
)
def test_a_webtoons_link_gives_its_type_and_number(url, want):
    assert platforms.webtoons_ref(url) == want, "the same number is another series under webtoon/ and canvas/"


@budget(load="pure")
def test_anilist_links_are_the_english_streaming_ones():
    media = {
        "externalLinks": [
            {"site": "WEBTOON", "type": "STREAMING", "url": f"{WT}/fr/x/list?title_no=9", "language": "French"},
            {"site": "WEBTOON", "type": "STREAMING", "url": f"{WT}/en/x/list?title_no=8", "language": "English"},
            {"site": "Tapas", "type": "STREAMING", "url": "https://tapas.io/series/x", "language": "English"},
            {"site": "Twitter", "type": "SOCIAL", "url": "https://twitter.com/x", "language": None},
        ]
    }  # fmt: skip
    assert platforms.anilist_links(media) == [
        "https://www.webtoons.com/en/x/list?title_no=8",
        "https://tapas.io/series/x",
    ]


@budget(load="pure")
@pytest.mark.parametrize(
    ("kavita", "anilist", "basis", "want"),
    [
        (["https://tapas.io/series/a/info"], ["https://tapas.io/series/b"], None, [("tapas", "a")]),
        ([], ["https://tapas.io/series/b"], None, [("tapas", "b")]),
        (
            ["https://tapas.io/series/a/info", "https://www.webtoons.com/en/x/list?title_no=7"],
            [],
            None,
            [("tapas", "a"), ("webtoons", "webtoon:7")],
        ),
        (
            ["https://tapas.io/series/a/info", "https://www.webtoons.com/en/x/list?title_no=7"],
            [],
            "webtoons seasons",
            [("webtoons", "webtoon:7")],
        ),
    ],
    ids=["kavita-first", "anilist-when-kavita-names-none", "tapas-then-webtoons", "recorded-basis-names-1"],
)
def test_the_platform_page_follows_the_links_and_the_recorded_basis(kavita, anilist, basis, want):
    assert platforms.candidates(kavita, anilist, basis) == want


@budget(load="pure")
def test_the_tapas_id_needs_the_html_page_and_the_list_is_read_whole():
    service = tapas_service("brother-knows-best")
    client = sources.Tapas(service)
    series_id = client.series_id("brother-knows-best")
    assert present(service.headers)["Accept"] == "text/html", "the JSON form of /info lacks the tapastic meta"
    items, total = present(client.episodes(series_id))
    assert series_id == "255099"
    assert len(items) == len({i[3] for i in items})
    assert total == 91


@budget(load="pure")
@pytest.mark.parametrize("status", [400, 404])
def test_a_gone_tapas_page_is_no_list(status):
    client = sources.Tapas(
        CannedService("tapas", {"/series/gone/info": http.HttpError(status, "https://tapas.io/series/gone/info", b"")})
    )
    assert client.series_id("gone") is None


@budget(load="pure")
def test_tapas_blocking_is_unreachable_for_the_run():
    client = sources.Tapas(
        CannedService("tapas", {"/series/a/info": http.HttpError(403, "https://tapas.io/series/a", b"")})
    )
    with pytest.raises(http.Unreachable):
        client.series_id("a")
    with pytest.raises(http.Unreachable):
        client.series_id("b")  # no request: the run stops calling Tapas


@budget(load="pure")
def test_an_unknown_webtoons_number_is_no_list():
    client = sources.Webtoons(
        CannedService("webtoons", {"/api/v1/": {"result": None, "message": None, "success": False}})
    )
    assert client.episodes("webtoon:1") is None


@budget(load="pure")
def test_the_webtoons_list_reads_its_coverage_from_the_web_page():
    client = sources.Webtoons(
        CannedService(
            "webtoons", {"/episodeList": reply(b'<div class="detail_install_app">Read more on the app!</div>')}
        )
    )
    assert client.app_only("webtoon:1") is True
    client = sources.Webtoons(
        CannedService("webtoons", {"/episodeList": http.HttpError(500, "https://www.webtoons.com/episodeList", b"")})
    )
    assert client.app_only("webtoon:1") is None, "an unread page is unknown coverage, not a failed check"


@budget(load="pure")
def test_a_side_story_title_does_not_depend_on_the_order_chapters_are_named_in():
    platform = {
        "episodes": [
            {"kind": episodes.SPECIAL, "chapter": "1", "title": "First"},
            {"kind": episodes.SPECIAL, "chapter": "2", "title": "Second"},
        ]
    }

    def row(key, number, title, *, settled):
        return naming.Row(
            key, settled, names.SIDE, number, None, None, 2 if settled else None, None, "", title, None, key
        )

    rows = [row(0, 1.0, "Side Story 1: First", settled=True), row(1, 2.0, "", settled=False)]
    got = naming.resolve(naming.NONE, rows, None, {}, platform)
    assert got[1].title == "Side Story 2: Second", "2 specials, 2 side stories (1 published): titles keyed by n"


@budget(load="pure")
@pytest.mark.parametrize("path", sorted(RECORDS.glob("platform-*.json")), ids=lambda p: p.stem)
def test_a_platform_series_last_chapters_resolve_as_the_bulk_converter_named_them(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    platform = stored_list(fixture["platform"]["name"], fixture["platform"]["ref"])
    got = naming.resolve(fixture["basis"], _rows(fixture), None, {}, platform)
    for k, c in enumerate(fixture["chapters"]):
        if c["open"]:
            want = (c["kind"], names.to_volume(c["volume"]), float(c["number"]), c["title"])
            have = (got[k].kind, got[k].volume, got[k].number, got[k].title)
            assert have == want, f"{fixture['series']}: {c['file']} named as the record holds it"


@budget(load="pure")
@pytest.mark.parametrize("path", sorted(RECORDS.glob("platform-*.json")), ids=lambda p: p.stem)
def test_a_new_series_whose_volumes_come_from_its_platform_gets_the_platform_basis(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    platform = stored_list(fixture["platform"]["name"], fixture["platform"]["ref"])
    rows = [r._replace(settled=False) if hasattr(r, "_replace") else r for r in _rows({**fixture, "chapters": [
        {**c, "open": True} for c in fixture["chapters"]]})]  # fmt: skip
    rows = [r for r in rows if r.src_volume is None and not r.season]  # nothing but the platform volumes them
    assert naming.choose(rows, None, {}, platform) == fixture["basis"]


# the check: storing the list, and the wait it holds a platform-basis chapter in


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


ITEMS = [(1, "Episode 1", None, 1), (2, "Episode 2 (Season 1 Finale)", None, 2), (3, "Episode 3", None, 3)]


def tapas_series(world, basis="tapas seasons"):
    kavita_id = world.kavita_series("Solo Leveling", bulk=(("Vol.1 Ch.1.cbz", "Tapas_Chapter 1", "1", 1),), basis=basis)
    world.kavita.metadata_rows[kavita_id] = {"webLinks": "https://tapas.io/series/solo/info"}
    world.sources.tapas.records[("series_id", "solo")] = "11"
    world.sources.tapas.records[("episodes", "11")] = (ITEMS, 3)
    return kavita_id


def stored(world):
    return world.rows("SELECT source, jsonb_array_length(payload -> 'episodes') FROM fetched WHERE kind = 'episodes'")


def due_again(world):
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("UPDATE series_check SET urgent = true")


@budget(load="db", seconds=2)
def test_a_check_stores_the_platform_list_and_a_shorter_one_does_not_replace_it(world):
    tapas_series(world)
    world.run(intake)
    assert stored(world) == [("tapas", 3)], "the list a series' Kavita link names, stored at its check"
    world.sources.tapas.records[("episodes", "11")] = (ITEMS[:1], 1)
    due_again(world)
    world.run(intake)
    assert stored(world) == [("tapas", 3)], "a list with fewer chapters keeps the stored one (cut back to the web)"
    del world.sources.tapas.records[("episodes", "11")]  # the stored series id is reused: its list is gone
    due_again(world)
    world.run(intake)
    assert stored(world) == [], "a page that is gone removes the list"


@budget(load="db", seconds=2)
def test_a_list_of_another_page_replaces_a_longer_stored_one(world):
    kavita_id = tapas_series(world)
    world.run(intake)
    world.kavita.metadata_rows[kavita_id] = {
        "webLinks": "https://tapas.io/series/right/info"
    }  # a person fixed the link
    world.sources.tapas.records[("series_id", "right")] = "12"
    world.sources.tapas.records[("episodes", "12")] = (ITEMS[:1], 1)
    due_again(world)
    world.run(intake)
    assert stored(world) == [("tapas", 1)], "the guard against a cut-short list holds for the same page only"


@budget(load="db", seconds=3)
def test_a_platform_series_chapter_waits_for_the_check_its_arrival_made_due(world, monkeypatch):
    tapas_series(world)
    world.run(intake)

    def blocked(_series_id):
        msg = "tapas"
        raise http.Unreachable(msg, "HTTP 403: blocked")

    monkeypatch.setattr(world.sources.tapas, "episodes", blocked)
    world.download(world.manga("Solo Leveling"), "Chapter 4")
    world.run(intake)
    assert world.rows("SELECT stage, status, waiting_on FROM chapter") == [("validate", "waiting", "metadata_check")], (
        "never named from a list older than its arrival"
    )
    monkeypatch.undo()
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], "the check completed: it goes on"


@budget(load="pure")
def test_a_list_that_does_not_move_on_ends_instead_of_looping():
    page = {
        "success": True,
        "result": {"episodeList": [{"episodeNo": 1, "episodeTitle": "Episode 1"}], "nextCursor": 5},
    }
    client = sources.Webtoons(CannedService("webtoons", {"/api/v1/": page}))  # every page answers cursor 5 again
    assert len(present(client.episodes("webtoon:1"))) == 1, "a page that does not move on ends the list, adding nothing"


@budget(load="pure")
def test_a_page_gone_after_the_first_is_an_error_not_no_list():
    first = {
        "data": {
            "episodes": [{"id": 1, "scene": 1, "title": "Episode 1"}],
            "pagination": {"page": 2, "has_next": True, "total": 2},
        }
    }
    gone = http.HttpError(404, "https://tapas.io/series/11/episodes", b"")
    client = sources.Tapas(
        CannedService("tapas", {"/series/11/episodes?page=1&": first, "/series/11/episodes?page=2&": gone})
    )
    with pytest.raises(http.HttpError):
        client.episodes("11")  # the check ends and the stored list is kept


@budget(load="pure")
def test_a_titled_episode_past_the_finale_names_its_side_story():
    platform = platforms.record(
        "tapas",
        "x",
        [(1, "1. Start (Main Story Finale)", None, 1), (2, "2. After Story", None, 2)],
        listed_total=2,
    )

    def row(key, number):
        return naming.Row(
            key=key,
            settled=False,
            kind=names.MAIN,
            number=number,
            season=None,
            src_volume=None,
            volume=None,
            volume_source=None,
            marker_title="",
            title="",
            date=None,
            order=key,
        )

    got = naming.resolve("tapas seasons", [row(0, 1.0), row(1, 2.0)], None, {}, platform)
    assert (got[1].kind, got[1].title) == (names.SIDE, "Side Story 1: After Story"), "as the bulk converter named it"
