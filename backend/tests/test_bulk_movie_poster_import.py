"""XC movie-list artwork is retained without a detail request.

All URLs and provider details here are deliberately synthetic.
"""

import asyncio

import vod_importer


def _movie(stream_id: str, poster_url: str | None = None) -> dict:
    item = {
        "name": "Artwork Test",
        "year": 2024,
        "provider_stream_id": stream_id,
        "container_extension": "mp4",
        "raw_name": "Artwork Test (2024)",
    }
    if poster_url is not None:
        item["poster_url"] = poster_url
    return item


def test_bulk_movie_import_keeps_the_first_catalog_poster(db):
    provider_id = db.upsert_provider("test-provider", "http://example.invalid", "user", "pass")
    first_poster = "https://images.example/first.jpg"
    second_poster = "https://images.example/second.jpg"

    db.bulk_import_movies(provider_id, [_movie("one", first_poster)])
    db.bulk_import_movies(provider_id, [_movie("two", second_poster)])

    movie = db.get_movie_by_name_year("Artwork Test", 2024)
    assert movie["poster_url"] == first_poster
    assert len(db.list_movie_sources(movie["id"])) == 2


def test_bulk_movie_import_fills_a_missing_poster_from_a_later_source(db):
    provider_id = db.upsert_provider("test-provider", "http://example.invalid", "user", "pass")
    poster = "https://images.example/later.jpg"

    db.bulk_import_movies(provider_id, [_movie("one")])
    db.bulk_import_movies(provider_id, [_movie("two", poster)])

    movie = db.get_movie_by_name_year("Artwork Test", 2024)
    assert movie["poster_url"] == poster


class _FakeClient:
    async def get_vod_streams(self):
        return [{
            "name": "Artwork Test (2024)",
            "stream_id": "one",
            "category_id": "1",
            "container_extension": "mp4",
            "stream_icon": "https://images.example/stream-icon.jpg",
        }]


def test_movie_catalog_mapping_prefers_stream_icon(monkeypatch):
    captured = {}
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    monkeypatch.setattr(
        vod_importer.vod_db,
        "bulk_import_movies",
        lambda _provider_id, items: captured.setdefault("items", items) or {},
    )

    asyncio.run(vod_importer._import_movies_for_provider(
        _FakeClient(), {"id": 1, "name": "test-provider"}, 1,
        {"1": "Movies"}, [], False,
        {"exclude_prefixes": [], "exclude_non_latin": False}, [],
    ))

    assert captured["items"][0]["poster_url"] == "https://images.example/stream-icon.jpg"
