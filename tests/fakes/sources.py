"""In-memory metadata sources (manga_pipeline.metadata.sources): records by id, search results by name. A source
holds nothing until a test adds it; every call is recorded in `calls`.
"""

from dataclasses import dataclass

from manga_pipeline.metadata import sources


class _Fake:
    def __init__(self, calls):
        self.records = {}  # (kind, id): payload
        self.results = {}  # search name: results
        self.images = {}  # url or (md id, file): bytes
        self._calls = calls

    def _get(self, kind, key):
        self._calls.append((type(self).__name__, kind, key))
        return self.records.get((kind, str(key)))

    def search(self, name):
        self._calls.append((type(self).__name__, "search", name))
        return list(self.results.get(name, []))


class FakeMangaBaka(_Fake):
    def series(self, mb_id):
        return self._get("series", mb_id)

    def by_source(self, source, source_id):
        return self._get(source, source_id)


class FakeMangaUpdates(_Fake):
    def series(self, mu_id):
        return self._get("series", mu_id)


class FakeMangaDex(_Fake):
    def manga(self, md_id):
        return self._get("manga", md_id)

    def aggregate(self, md_id):
        return self._get("aggregate", md_id)

    def covers(self, md_id):
        return self._get("covers", md_id) or []

    def latest_chapter_date(self, md_id):
        return self._get("latest", md_id)

    def cover_image(self, md_id, file_name):
        return self.images[(md_id, file_name)]


class FakeAniList(_Fake):
    def media(self, al_id):
        return self._get("media", al_id)

    def cover_image(self, url):
        return self.images[url]


class FakeTapas(_Fake):
    """records: ("series_id", slug) -> id; ("episodes", id) -> (items, listed total)."""

    def series_id(self, slug):
        return self._get("series_id", slug)

    def episodes(self, series_id):
        return self._get("episodes", series_id)


class FakeWebtoons(_Fake):
    """records: ("episodes", ref) -> items; ("app_only", ref) -> True, False or None (unread)."""

    def episodes(self, ref):
        return self._get("episodes", ref)

    def app_only(self, ref):
        return self.records.get(("app_only", ref), False)


@dataclass
class FakeSources(sources.Sources):
    """sources.Sources of fakes, and every call they were asked (calls)."""

    mangabaka: FakeMangaBaka
    mangaupdates: FakeMangaUpdates
    mangadex: FakeMangaDex
    anilist: FakeAniList
    tapas: FakeTapas
    webtoons: FakeWebtoons
    calls: list


def fake_sources():
    calls = []
    return FakeSources(
        FakeMangaBaka(calls),
        FakeMangaUpdates(calls),
        FakeMangaDex(calls),
        FakeAniList(calls),
        FakeTapas(calls),
        FakeWebtoons(calls),
        calls,
    )
