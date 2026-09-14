"""Bulk XC movie artwork must be saved without waiting for enrichment.

Series already retain their free ``cover`` field from the cheap catalog list.
Real XC movie lists instead use ``stream_icon``.
"""

import asyncio

import vod_importer


def _movie(stream_id: str, poster_url: str | None = None) -> dict:
    item = {
        "name": "Artwork Test",
        "year": 2024,
        "provider_stream_id": stream_id,
        "container_extension": "mp4",
        "raw_name": "EN - Artwork Test (2024)",
    }
    if poster_url is not None:
        item["poster_url"] = poster_url
    return item


def test_bulk_movie_import_persists_initial_poster_and_does_not_replace_it(db):
    provider_id = db.upsert_provider("provider", "http://example.com", "user", "pass")
    first_poster = "https://images.example/first.jpg"
    second_poster = "https://images.example/second.jpg"

    db.bulk_import_movies(provider_id, [_movie("one", first_poster)])
    db.bulk_import_movies(provider_id, [_movie("two", second_poster)])

    movie = db.get_movie_by_name_year("Artwork Test", 2024)
    assert movie["poster_url"] == first_poster
    assert len(db.list_movie_sources(movie["id"])) == 2


def test_bulk_movie_import_fills_missing_poster_on_later_source(db):
    provider_id = db.upsert_provider("provider", "http://example.com", "user", "pass")
    poster = "https://images.example/later.jpg"

    db.bulk_import_movies(provider_id, [_movie("one")])
    db.bulk_import_movies(provider_id, [_movie("two", poster)])

    movie = db.get_movie_by_name_year("Artwork Test", 2024)
    assert movie["poster_url"] == poster


class _FakeClient:
    async def get_vod_streams(self):
        return [{
            "name": "EN - Artwork Test (2024)",
            "stream_id": "one",
            "category_id": "1",
            "container_extension": "mp4",
            "stream_icon": "https://images.example/stream-icon.jpg",
        }]


def test_movie_catalog_mapping_prefers_stream_icon(monkeypatch):
    captured = {}

    monkeypatch.setattr(vod_importer.config, "get_enabled_languages", lambda: ["EN"])
    monkeypatch.setattr(
        vod_importer.config, "get_import_language_exclusion",
        lambda: {"exclude_prefixes": [], "exclude_non_latin": False},
    )
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    monkeypatch.setattr(
        vod_importer.vod_db,
        "bulk_import_movies",
        lambda _provider_id, items: captured.setdefault("items", items) or {},
    )

    asyncio.run(vod_importer._import_movies_for_provider(
        _FakeClient(), {"id": 1, "name": "provider"}, 1,
        {"1": "Movies"}, [], False,
    ))

    assert captured["items"][0]["poster_url"] == "https://images.example/stream-icon.jpg"
