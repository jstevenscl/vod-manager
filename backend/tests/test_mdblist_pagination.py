"""A MDBList list longer than one page must be fetched in full -- the importer
used to request limit=500 once and drop everything after it."""

import asyncio

import mdblist_sync


class _Resp:
    def __init__(self, data, has_more):
        self._data = data
        self.headers = {"X-Has-More": "true" if has_more else "false"}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_client(total, page_size, calls):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls.append(dict(params))
            off, lim = params["offset"], params["limit"]
            ids = list(range(off, min(off + min(lim, page_size), total)))
            movies = [{"title": f"M{i}", "release_year": 2000, "ids": {"tmdb": i}} for i in ids if i % 2 == 0]
            shows = [{"title": f"S{i}", "release_year": 2001, "ids": {"tmdb": i}} for i in ids if i % 2 == 1]
            return _Resp({"movies": movies, "shows": shows}, off + len(ids) < total)

    return Client


def test_list_over_one_page_is_fetched_completely(monkeypatch):
    calls = []
    monkeypatch.setattr(mdblist_sync.httpx, "AsyncClient", _fake_client(1234, 500, calls))
    items = asyncio.run(mdblist_sync.fetch_list_items("key", "42"))
    assert len(items) == 1234
    assert len({i["tmdb_id"] for i in items}) == 1234
    assert [c["offset"] for c in calls] == [0, 500, 1000]


def test_short_list_makes_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(mdblist_sync.httpx, "AsyncClient", _fake_client(30, 500, calls))
    assert len(asyncio.run(mdblist_sync.fetch_list_items("key", "42"))) == 30
    assert len(calls) == 1


def test_server_page_cap_smaller_than_limit_still_completes(monkeypatch):
    calls = []
    monkeypatch.setattr(mdblist_sync.httpx, "AsyncClient", _fake_client(250, 100, calls))
    assert len(asyncio.run(mdblist_sync.fetch_list_items("key", "42"))) == 250
