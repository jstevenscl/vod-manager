"""A TMDB built-in chart (Top Rated, Popular, ...) can be a list-sync source:
fetched page by page, normalized like any other list, capped at 25 pages."""

import asyncio

import pytest

import tmdb_sync
import vod_list_sync


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _patch(monkeypatch, total_pages, calls):
    monkeypatch.setattr(tmdb_sync, "get_tmdb_api_key", lambda: "k")

    async def fake_get(url, params=None):
        calls.append((url, params["page"]))
        p = params["page"]
        title_key = "title" if "/movie/" in url else "name"
        date_key = "release_date" if "/movie/" in url else "first_air_date"
        return _Resp({"total_pages": total_pages, "results": [
            {"id": p * 100 + i, title_key: f"T{p}-{i}", date_key: "2001-05-05"} for i in range(20)]})

    monkeypatch.setattr(tmdb_sync, "_tmdb_get", fake_get)


def test_movie_chart_is_paged_and_normalized(monkeypatch):
    calls = []
    _patch(monkeypatch, 3, calls)
    items = asyncio.run(tmdb_sync.fetch_chart_items("movie/top_rated"))
    assert len(items) == 60 and [c[1] for c in calls] == [1, 2, 3]
    assert items[0] == {"media_type": "movie", "tmdb_id": 100, "title": "T1-0", "year": 2001}


def test_tv_chart_uses_tv_media_type(monkeypatch):
    _patch(monkeypatch, 1, [])
    items = asyncio.run(tmdb_sync.fetch_chart_items("tv/popular"))
    assert items and all(i["media_type"] == "tv" and i["year"] == 2001 for i in items)


def test_chart_is_capped_at_25_pages(monkeypatch):
    calls = []
    _patch(monkeypatch, 500, calls)
    assert len(asyncio.run(tmdb_sync.fetch_chart_items("movie/popular"))) == 500
    assert len(calls) == 25


def test_unknown_chart_rejected(monkeypatch):
    with pytest.raises(ValueError):
        asyncio.run(tmdb_sync.fetch_chart_items("movie/../account"))


def test_list_sync_dispatches_tmdb_chart(monkeypatch):
    async def fake(chart):
        return [{"media_type": "movie", "tmdb_id": 1, "title": "x", "year": 2000}]
    monkeypatch.setattr(vod_list_sync.tmdb_sync, "fetch_chart_items", fake)
    assert asyncio.run(vod_list_sync._fetch_normalized_items("tmdb_chart:movie/top_rated"))[0]["tmdb_id"] == 1
