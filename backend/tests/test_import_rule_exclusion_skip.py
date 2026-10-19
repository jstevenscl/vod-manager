"""Language/country import-exclusion rules skip intake, like saved categories.

Before, a title matching an import language prefix, non-Latin or country rule
was stored archived and then deleted again by purge_excluded_archived_content
on every import (a create-then-purge cycle). It is now never created."""

import asyncio

import config
import vod_importer


class FakeXCClient:
    def __init__(self, _provider):
        pass

    async def get_vod_categories(self):
        return [{"category_id": "1", "category_name": "Movies"}]

    async def get_series_categories(self):
        return [{"category_id": "1", "category_name": "Shows"}]

    async def get_vod_streams(self):
        return [
            {"stream_id": "en-1", "name": "Kept Movie (2001)", "category_id": "1"},
            {"stream_id": "es-1", "name": "ES - Spanish Movie (2001)", "category_id": "1"},
            {"stream_id": "uk-1", "name": "Country Movie (2001) (UK)", "category_id": "1"},
            {"stream_id": "nl-1", "name": "Пример (2001)", "category_id": "1"},
        ]

    async def get_series(self):
        return [
            {"series_id": "en-s", "name": "Kept Show (2001)", "category_id": "1"},
            {"series_id": "es-s", "name": "ES - Spanish Show (2001)", "category_id": "1"},
        ]


def _import(db, monkeypatch):
    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeXCClient)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p", provider_type="xc")
    return asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))


def _names(db, table):
    conn = db._connect()
    try:
        return {r["name"] for r in conn.execute(f"SELECT name FROM {table}").fetchall()}
    finally:
        conn.close()


def test_language_prefix_rule_skips_movies_and_series(db, monkeypatch):
    config.save_import_language_exclusion(["ES"], False)

    result = _import(db, monkeypatch)

    assert not any("Spanish" in n for n in _names(db, "movies"))
    assert not any("Spanish" in n for n in _names(db, "series"))
    assert result["movies_skipped_excluded"] == 1
    assert result["series_skipped_excluded"] == 1
    assert result["series_archived"] == 0


def test_country_and_non_latin_rules_skip_movies(db, monkeypatch):
    config.save_import_language_exclusion([], True)
    config.save_import_country_exclusion(["UK"])

    result = _import(db, monkeypatch)

    movies = _names(db, "movies")
    assert not any("Country" in n for n in movies)
    assert "Пример" not in movies
    assert result["movies_skipped_excluded"] == 2
    assert result["movies_archived"] == 0


def test_no_rules_imports_everything(db, monkeypatch):
    config.save_import_language_exclusion([], False)
    config.save_import_country_exclusion([])

    result = _import(db, monkeypatch)

    assert result["movies_skipped_excluded"] == 0
    assert result["series_skipped_excluded"] == 0
    assert len(_names(db, "movies")) == 4
