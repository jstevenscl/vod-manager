"""Automatic TMDB episode titles: opt-in on apply, due-list rules, and a pass
that fills new placeholder-named episodes without touching real/locked names."""

import asyncio
import time

import auto_episode_titles
import config
import tmdb_sync

TMDB = [
    {"season_number": 1, "episode_number": 1, "name": "Pilot"},
    {"season_number": 1, "episode_number": 2, "name": "Second"},
]


def _series(db, name="Show A", tmdb="111", names=("Episode 1", "Real Name")):
    sid = db.upsert_series(name, 2020)
    db.backfill_tmdb_id_if_missing("series", sid, tmdb)
    for n, ep_name in enumerate(names, start=1):
        db.add_episode(sid, 1, n, ep_name)
    return sid


def _fake_tmdb(monkeypatch, calls=None):
    async def fake(tmdb_id):
        if calls is not None:
            calls.append(tmdb_id)
        return TMDB
    monkeypatch.setattr(tmdb_sync, "get_series_episode_list_cached", fake)


def test_default_mode_is_applied_and_validated(db):
    assert config.get_auto_episode_titles_mode() == "applied"
    config.save_auto_episode_titles_mode("all")
    assert config.get_auto_episode_titles_mode() == "all"
    try:
        config.save_auto_episode_titles_mode("bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_applied_scope_only_includes_opted_in_series(db):
    a = _series(db, "Show A", "111")
    _series(db, "Show B", "222")
    assert db.list_series_due_for_auto_episode_titles(True, time.time(), 50) == []
    db.mark_series_auto_episode_titles([a])
    due = db.list_series_due_for_auto_episode_titles(True, time.time(), 50)
    assert [d["id"] for d in due] == [a]
    both = db.list_series_due_for_auto_episode_titles(False, time.time(), 50)
    assert len(both) == 2


def test_series_without_placeholder_or_with_locked_names_are_not_due(db):
    real = _series(db, "Real", "1", names=("Ep One", "Ep Two"))
    locked = _series(db, "Locked", "2", names=("Episode 1", "Episode 2"))
    db.mark_series_auto_episode_titles([real, locked])
    conn = db._connect()
    try:
        conn.execute("UPDATE episodes SET name_locked=1 WHERE series_id=?", (locked,))
        conn.commit()
    finally:
        conn.close()
    assert db.list_series_due_for_auto_episode_titles(True, time.time(), 50) == []


def test_recently_checked_series_wait_a_day(db):
    a = _series(db)
    db.mark_series_auto_episode_titles([a])
    db.mark_series_episode_titles_checked(a, time.time())
    assert db.list_series_due_for_auto_episode_titles(True, time.time() - 86400, 50) == []
    assert len(db.list_series_due_for_auto_episode_titles(True, time.time() + 1, 50)) == 1


def test_run_once_fills_placeholders_and_keeps_real_names(db, monkeypatch):
    a = _series(db)
    db.mark_series_auto_episode_titles([a])
    calls = []
    _fake_tmdb(monkeypatch, calls)

    result = asyncio.run(auto_episode_titles.run_once("applied"))

    assert result["series_checked"] == 1 and result["episodes_renamed"] == 1 and result["series_changed"] == 1
    names = {e["episode_number"]: e["name"] for e in db.list_episodes(a)}
    assert names == {1: "Pilot", 2: "Real Name"}  # provider's real name kept in fill mode
    # checked just now -> an immediate second pass makes no TMDB call
    asyncio.run(auto_episode_titles.run_once("applied"))
    assert calls == ["111"]


def test_run_once_off_does_nothing_and_survives_a_stale_id(db, monkeypatch):
    a = _series(db)
    db.mark_series_auto_episode_titles([a])
    assert asyncio.run(auto_episode_titles.run_once("off"))["series_checked"] == 0

    async def boom(tmdb_id):
        raise tmdb_sync.TmdbNotFoundError(tmdb_id)

    monkeypatch.setattr(tmdb_sync, "get_series_episode_list_cached", boom)
    result = asyncio.run(auto_episode_titles.run_once("applied"))
    assert result["failed"] == 1 and result["episodes_renamed"] == 0


# Local TMDB library first; TMDB only when local can't fill.
def _store_show(tmdb_id, season_eps, age=3 * 86400):
    import tmdb_store
    config.save_tmdb_store_settings({"enabled": True})
    tmdb_store.init_db()
    tmdb_store.upsert_payload("tv", {"id": tmdb_id, "name": "Show A", "seasons": [{"season_number": 1}]})
    tmdb_store.put_season(tmdb_id, 1, season_eps)
    with tmdb_store._conn() as conn:  # older than the 24h latest-season refresh window
        conn.execute("UPDATE seasons SET fetched_at=?", (time.time() - age,))


def test_run_once_uses_local_library_without_calling_tmdb(db, monkeypatch):
    a = _series(db)
    db.mark_series_auto_episode_titles([a])
    _store_show(111, TMDB)
    calls = []
    _fake_tmdb(monkeypatch, calls)

    result = asyncio.run(auto_episode_titles.run_once("applied"))

    assert calls == []
    assert result["episodes_renamed"] == 1 and result["from_local"] == 1
    assert {e["episode_number"]: e["name"] for e in db.list_episodes(a)} == {1: "Pilot", 2: "Real Name"}


def test_run_once_calls_tmdb_when_local_cannot_fill(db, monkeypatch):
    a = _series(db)
    db.mark_series_auto_episode_titles([a])
    _store_show(111, [{"season_number": 1, "episode_number": 2, "name": "Second"}])
    calls = []
    _fake_tmdb(monkeypatch, calls)

    result = asyncio.run(auto_episode_titles.run_once("applied"))

    assert calls == ["111"]
    assert result["episodes_renamed"] == 1 and result["from_local"] == 0
    assert {e["episode_number"]: e["name"] for e in db.list_episodes(a)}[1] == "Pilot"
