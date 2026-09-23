import asyncio
import time

import xc_server


def test_series_episode_export_keeps_one_best_source_per_episode(db):
    preferred_provider = db.upsert_provider(
        "preferred", "http://preferred.example", "user", "pass", priority=10,
    )
    fallback_provider = db.upsert_provider(
        "fallback", "http://fallback.example", "user", "pass", priority=0,
    )
    series_id = db.upsert_series("Example Show", 2024)
    category_id = db.upsert_category("Example Shows", "series")
    db.place_series_in_category(series_id, category_id)

    first_episode = db.add_episode(series_id, 1, 1, "Pilot")
    second_episode = db.add_episode(series_id, 1, 2, "Second")
    db.add_episode_source(first_episode, preferred_provider, "preferred-1", raw_name="Pilot")
    db.add_episode_source(first_episode, fallback_provider, "fallback-1", raw_name="Pilot")
    db.add_episode_source(second_episode, fallback_provider, "fallback-2", raw_name="Second")

    rows = db.get_episode_export_rows_for_series(series_id)

    assert [(row["season_number"], row["episode_number"]) for row in rows] == [(1, 1), (1, 2)]
    assert [row["provider_stream_id"] for row in rows] == ["preferred-1", "fallback-2"]
    assert all(row["series_id"] == series_id for row in rows)


def test_client_seen_updates_are_debounced(monkeypatch):
    calls = []
    monkeypatch.setattr(
        xc_server.vod_db,
        "record_xc_client_seen",
        lambda client_id, client_ip: calls.append((client_id, client_ip)),
    )
    xc_server._seen_update_cache.clear()
    xc_server._last_seen_sweep_at = time.monotonic()

    async def exercise():
        await xc_server._record_client_seen_if_due(7, "192.0.2.10")
        await xc_server._record_client_seen_if_due(7, "192.0.2.10")

    asyncio.run(exercise())
    assert calls == [(7, "192.0.2.10")]

    xc_server._seen_update_cache[(7, "192.0.2.10")] = (
        time.monotonic() - xc_server._SEEN_UPDATE_TTL_SECONDS - 1
    )
    asyncio.run(exercise())
    assert calls == [(7, "192.0.2.10"), (7, "192.0.2.10")]
