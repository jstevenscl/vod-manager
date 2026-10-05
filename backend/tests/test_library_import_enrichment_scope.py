"""Plex/Emby/library importers don't report changed ids; a drain that
includes one must enrich unscoped (None), not scoped to an empty set that
silently queues nothing (upstream PR #35 review)."""

import asyncio

import vod_importer
import vod_routes


def test_merge_changed_ids():
    assert vod_importer.merge_changed_ids(set(), {"changed_movie_ids": [1]}, "changed_movie_ids") == {1}
    assert vod_importer.merge_changed_ids({1}, {"catalog_changed": True}, "changed_movie_ids") is None
    assert vod_importer.merge_changed_ids(None, {"changed_movie_ids": [2]}, "changed_movie_ids") is None


def test_queue_drain_with_plex_import_enriches_unscoped(monkeypatch):
    results = {1: {"catalog_changed": True, "changed_movie_ids": [5], "changed_series_ids": []},
               2: {"catalog_changed": True, "movies_imported": 3}}
    scheduled = []

    async def fake_import(provider_id):
        return results[provider_id]

    monkeypatch.setattr(vod_routes, "_run_provider_catalog_import", fake_import)
    monkeypatch.setattr(vod_importer, "schedule_post_import_enrichment", lambda **kw: scheduled.append(kw) or True)
    monkeypatch.setattr(vod_routes, "_MANUAL_IMPORT_QUEUE", [1, 2])

    asyncio.run(vod_routes._manual_import_worker())

    assert scheduled == [{"changed_movie_ids": None, "changed_series_ids": None}]
