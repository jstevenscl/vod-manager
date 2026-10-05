"""Post-import sweeps (upstream PR #35 review): the language-archive sweep
must run on every import, including a no-op sync (a newly narrowed Enabled
Playback Languages setting changes nothing in the catalog), unscoped, and
exactly once; the orphan purge also runs once."""

import asyncio

import vod_importer


def test_noop_sync_runs_each_sweep_once_unscoped(db, monkeypatch):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")

    class FakeClient:
        def __init__(self, _provider):
            pass

        async def get_vod_categories(self):
            return [{"category_id": "1", "category_name": "Movies"}]

        async def get_series_categories(self):
            return []

        async def get_vod_streams(self):
            return [{"stream_id": "m1", "name": "Keep Movie (2020)", "category_id": "1"}]

        async def get_series(self):
            return []

    archive_calls = []
    orphan_calls = []
    real_orphans = vod_importer.vod_db.purge_orphans

    def fake_archive(*args):
        archive_calls.append(args)
        return {"movies_archived": 0, "series_archived": 0, "movies_unarchived": 0, "series_unarchived": 0}

    def counting_orphans():
        orphan_calls.append(1)
        return real_orphans()

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    monkeypatch.setattr(vod_importer, "schedule_post_import_enrichment", lambda: False)
    monkeypatch.setattr(vod_importer.vod_db, "archive_disabled_language_content", fake_archive)
    monkeypatch.setattr(vod_importer.vod_db, "purge_orphans", counting_orphans)

    first = asyncio.run(vod_importer.import_provider_catalog(provider_id))
    assert first["catalog_changed"] is True
    assert archive_calls == [()]
    assert len(orphan_calls) == 1
    archive_calls.clear()
    orphan_calls.clear()
    unchanged = asyncio.run(vod_importer.import_provider_catalog(provider_id))

    assert unchanged["catalog_changed"] is False
    assert archive_calls == [()]
    assert len(orphan_calls) == 1
