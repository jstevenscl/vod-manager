"""Use TMDB episode titles: fill-gaps vs replace, dry run, and the name lock
that keeps a provider re-sync from reverting a deliberately-set name."""


def _series_with_episodes(db, names):
    provider = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    series_id = db.upsert_series("Example Show", 2020)
    for number, name in enumerate(names, start=1):
        db.add_episode(series_id, 1, number, name)
    return provider, series_id


def _names(db, series_id):
    return {e["episode_number"]: e["name"] for e in db.list_episodes(series_id)}


TMDB = {(1, 1): "Pilot", (1, 2): "Cat's in the Bag", (1, 3): "And the Bag's in the River"}


def test_fill_only_replaces_placeholders(db):
    _, sid = _series_with_episodes(db, ["Episode 1", "Provider Named Two", ""])

    result = db.apply_tmdb_episode_titles(sid, TMDB, mode="fill")

    assert result["renamed"] == 2 and result["skipped_existing"] == 1
    assert _names(db, sid) == {1: "Pilot", 2: "Provider Named Two", 3: "And the Bag's in the River"}


def test_fill_treats_provider_formatted_names_with_an_episode_code_as_placeholders(db):
    _, sid = _series_with_episodes(db, ["Example Show (2020) - S01E01 - Pilot", "Real Provider Title", "Example Show - s01e03"])

    result = db.apply_tmdb_episode_titles(sid, TMDB, mode="fill")

    assert result["renamed"] == 2 and result["skipped_existing"] == 1
    assert _names(db, sid) == {1: "Pilot", 2: "Real Provider Title", 3: "And the Bag's in the River"}


def test_replace_overwrites_every_matched_name(db):
    _, sid = _series_with_episodes(db, ["Episode 1", "Provider Named Two", "Three"])

    result = db.apply_tmdb_episode_titles(sid, TMDB, mode="replace")

    assert result["renamed"] == 3
    assert _names(db, sid)[2] == "Cat's in the Bag"


def test_dry_run_writes_nothing(db):
    _, sid = _series_with_episodes(db, ["Episode 1", "Episode 2", "Episode 3"])

    result = db.apply_tmdb_episode_titles(sid, TMDB, mode="fill", dry_run=True)

    assert result["renamed"] == 3 and result["dry_run"] is True and result["examples"]
    assert _names(db, sid) == {1: "Episode 1", 2: "Episode 2", 3: "Episode 3"}


def test_unmatched_episodes_are_left_alone(db):
    _, sid = _series_with_episodes(db, ["Episode 1", "Episode 2"])
    db.add_episode(sid, 5, 9, "Episode 9")

    result = db.apply_tmdb_episode_titles(sid, TMDB, mode="replace")

    assert result["unmatched"] == 1
    assert {(e["season_number"], e["episode_number"]): e["name"] for e in db.list_episodes(sid)}[(5, 9)] == "Episode 9"


def test_fill_skips_a_locked_name_but_replace_overrides_it(db):
    _, sid = _series_with_episodes(db, ["Episode 1", "Episode 2", "Episode 3"])
    ep = next(e for e in db.list_episodes(sid) if e["episode_number"] == 1)
    db.update_episode(ep["id"], name="Episode 1")  # manual edit locks it (even to a placeholder-looking value)

    fill = db.apply_tmdb_episode_titles(sid, TMDB, mode="fill")
    assert fill["renamed"] == 2 and _names(db, sid)[1] == "Episode 1"

    db.apply_tmdb_episode_titles(sid, TMDB, mode="replace")
    assert _names(db, sid)[1] == "Pilot"


def test_provider_resync_does_not_revert_a_locked_name(db):
    provider, sid = _series_with_episodes(db, ["Episode 1"])
    db.apply_tmdb_episode_titles(sid, {(1, 1): "Pilot"}, mode="fill")

    db.enrich_series_episodes_batch(sid, provider, [
        {"season_number": 1, "episode_number": 1, "name": "Provider Rename", "provider_stream_id": "s1"},
    ])

    assert _names(db, sid)[1] == "Pilot"


def test_unlocked_names_still_follow_the_provider(db):
    provider, sid = _series_with_episodes(db, ["Episode 1"])

    db.enrich_series_episodes_batch(sid, provider, [
        {"season_number": 1, "episode_number": 1, "name": "Provider Rename", "provider_stream_id": "s1"},
    ])

    assert _names(db, sid)[1] == "Provider Rename"


def test_bad_mode_rejected(db):
    _, sid = _series_with_episodes(db, ["Episode 1"])
    try:
        db.apply_tmdb_episode_titles(sid, TMDB, mode="nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_bulk_route_renames_selected_series_and_counts_the_rest(db, monkeypatch):
    import asyncio

    import tmdb_sync
    import vod_routes

    provider = db.upsert_provider("prov", "http://example.com", "u", "p")
    with_tmdb = db.upsert_series("Show A", 2020)
    db.backfill_tmdb_id_if_missing("series", with_tmdb, "111")
    ep = db.add_episode(with_tmdb, 1, 1, "Episode 1")
    db.add_episode_source(ep, provider, "a1", "mp4", raw_name="x")
    without_tmdb = db.upsert_series("Show B", 2021)

    async def fake_list(tmdb_id):
        return [{"season_number": 1, "episode_number": 1, "name": "Pilot"}]

    monkeypatch.setattr(tmdb_sync, "get_series_episode_list_cached", fake_list)
    body = vod_routes.BulkEpisodeTitlesRequest(ids=[with_tmdb, without_tmdb, 999999], dry_run=True)

    preview = asyncio.run(vod_routes.bulk_apply_tmdb_episode_titles(body))

    assert preview["series_checked"] == 2 and preview["series_changed"] == 1
    assert preview["episodes_renamed"] == 1 and preview["series_without_tmdb"] == 1
    assert db.get_series(with_tmdb)  # dry run changed nothing
    body.dry_run = False
    applied = asyncio.run(vod_routes.bulk_apply_tmdb_episode_titles(body))
    assert applied["episodes_renamed"] == 1
    conn = db._connect()
    try:
        assert conn.execute("SELECT name FROM episodes WHERE id=?", (ep,)).fetchone()["name"] == "Pilot"
    finally:
        conn.close()


def test_bulk_route_limits_batch_size(db):
    import asyncio

    import pytest
    from fastapi import HTTPException

    import vod_routes

    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.bulk_apply_tmdb_episode_titles(vod_routes.BulkEpisodeTitlesRequest(ids=list(range(51)))))
    assert exc.value.status_code == 400
