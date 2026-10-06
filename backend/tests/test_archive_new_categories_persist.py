"""archive_new_categories: content in a newly discovered category must stay
archived, both through the same import's language sweep and on later imports.
known_import_categories is updated in the run that first sees the category,
so without persisting it the next import no longer treats it as new."""

import asyncio

import vod_importer


def _fake_client(categories, streams):
    class FakeClient:
        def __init__(self, _provider):
            pass

        async def get_vod_categories(self):
            return [{"category_id": cid, "category_name": name} for cid, name in categories.items()]

        async def get_series_categories(self):
            return []

        async def get_vod_streams(self):
            return list(streams)

        async def get_series(self):
            return []

    return FakeClient


def _patch(monkeypatch, client_cls):
    monkeypatch.setattr(vod_importer, "XCProviderClient", client_cls)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])


def test_new_category_stays_archived_across_imports(db, monkeypatch):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    db.set_provider_archive_new_categories(provider_id, True)

    _patch(monkeypatch, _fake_client({"1": "Movies"}, [
        {"stream_id": "m1", "name": "Known Movie (2020)", "category_id": "1"},
    ]))
    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert db.get_movie_by_name_year("Known Movie", 2020)["review_excluded"] == 0

    _patch(monkeypatch, _fake_client({"1": "Movies", "2": "Brand New"}, [
        {"stream_id": "m1", "name": "Known Movie (2020)", "category_id": "1"},
        {"stream_id": "m2", "name": "Fresh Movie (2021)", "category_id": "2"},
    ]))
    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert db.get_movie_by_name_year("Fresh Movie", 2021)["review_excluded"] == 1

    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert db.get_movie_by_name_year("Fresh Movie", 2021)["review_excluded"] == 1
    provider = db.get_provider(provider_id)
    assert provider["auto_archived_categories"] == ["Brand New"]
    assert "Brand New" not in provider["import_exclude_categories"]


def test_turning_setting_off_unarchives_on_next_import(db, monkeypatch):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    db.set_provider_archive_new_categories(provider_id, True)
    _patch(monkeypatch, _fake_client({"1": "Movies"}, []))
    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    _patch(monkeypatch, _fake_client({"1": "Movies", "2": "Brand New"}, [
        {"stream_id": "m2", "name": "Fresh Movie (2021)", "category_id": "2"},
    ]))
    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert db.get_movie_by_name_year("Fresh Movie", 2021)["review_excluded"] == 1

    db.set_provider_archive_new_categories(provider_id, False)
    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))

    assert db.get_movie_by_name_year("Fresh Movie", 2021)["review_excluded"] == 0


def test_first_import_does_not_archive_every_category(db, monkeypatch):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    db.set_provider_archive_new_categories(provider_id, True)

    _patch(monkeypatch, _fake_client({"1": "Movies"}, [
        {"stream_id": "m1", "name": "Known Movie (2020)", "category_id": "1"},
    ]))
    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))

    assert db.get_movie_by_name_year("Known Movie", 2020)["review_excluded"] == 0
    assert not db.get_provider(provider_id)["auto_archived_categories"]
