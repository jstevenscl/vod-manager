"""Regression coverage for responsive, serialized catalog imports."""

import asyncio

import vod_importer


def test_catalog_item_builders_keep_raw_snapshot_when_items_are_filtered(monkeypatch):
    monkeypatch.setattr(vod_importer, "_should_exclude_from_import", lambda name, *_args, **_kwargs: name == "Skip")

    movies, movie_ids = vod_importer._build_movie_import_items(
        [
            {"stream_id": "keep", "name": "Keep (2020)", "category_id": "1"},
            {"stream_id": "skip", "name": "Skip (2020)", "category_id": "1"},
        ],
        {"1": "Movies"}, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False}, [],
    )
    series, series_ids = vod_importer._build_series_import_items(
        [
            {"series_id": "keep", "name": "Keep (2020)", "category_id": "1"},
            {"series_id": "skip", "name": "Skip (2020)", "category_id": "1"},
        ],
        {"1": "Series"}, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False}, [],
        {field: [] for field in ("genre", "description", "cast_list", "director")},
    )

    assert [item["provider_stream_id"] for item in movies] == ["keep"]
    assert movie_ids == {"keep", "skip"}
    assert [item["provider_series_id"] for item in series] == ["keep"]
    assert series_ids == {"keep", "skip"}


def test_xc_imports_are_serialized(monkeypatch):
    active = 0
    peak = 0

    async def fake_impl(_provider_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True}

    monkeypatch.setattr(vod_importer.vod_db, "get_provider", lambda provider_id: {"id": provider_id, "name": str(provider_id)})
    monkeypatch.setattr(vod_importer, "_import_provider_catalog_impl", fake_impl)

    async def run():
        await asyncio.gather(
            vod_importer.import_provider_catalog(1, schedule_enrichment=False),
            vod_importer.import_provider_catalog(2, schedule_enrichment=False),
        )

    asyncio.run(run())
    assert peak == 1
