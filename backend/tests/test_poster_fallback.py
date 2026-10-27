"""A card whose stored poster fails to load swaps to the TMDB poster, and bulk
enrichment's end-of-run collision sweep only covers the series that run touched.

Seen on a live library: ~600 series / ~790 movies with a TMDB id still pointed at
a dead provider image host (or a bare TMDB base URL), so cards rendered blank."""

import asyncio

import pytest

import tmdb_sync
import vod_importer

TMDB_POSTER = "https://image.tmdb.org/t/p/w500/abc.jpg"


@pytest.fixture()
def poster_env(monkeypatch):
    items = {
        ("series", 1): {"id": 1, "tmdb_id": "89844", "poster_url": "http://dead.invalid/x.jpg"},
        ("series", 2): {"id": 2, "tmdb_id": None, "poster_url": "http://dead.invalid/y.jpg"},
        ("movie", 3): {"id": 3, "tmdb_id": "550", "poster_url": "https://image.tmdb.org/t/p/w600_and_h900_bestv2"},
    }
    saved = []
    fetched = []

    async def details(tmdb_id):
        fetched.append(tmdb_id)
        return {"poster_url": TMDB_POSTER}

    monkeypatch.setattr(vod_importer.vod_db, "get_series", lambda i: items.get(("series", i)))
    monkeypatch.setattr(vod_importer.vod_db, "get_movie", lambda i: items.get(("movie", i)))
    monkeypatch.setattr(vod_importer.vod_db, "bulk_set_poster_url",
                        lambda kind, ids, url: saved.append((kind, ids, url)) or len(ids))
    monkeypatch.setattr(tmdb_sync, "get_series_full_details", details)
    monkeypatch.setattr(tmdb_sync, "get_movie_full_details", details)
    return {"saved": saved, "fetched": fetched}


def test_broken_series_poster_swaps_to_tmdb_and_is_saved(poster_env):
    assert asyncio.run(vod_importer.replace_broken_poster("series", 1)) == TMDB_POSTER
    assert poster_env["fetched"] == ["89844"]
    assert poster_env["saved"] == [("series", [1], TMDB_POSTER)]


def test_broken_movie_poster_swaps_to_tmdb(poster_env):
    assert asyncio.run(vod_importer.replace_broken_poster("movie", 3)) == TMDB_POSTER
    assert poster_env["saved"] == [("movie", [3], TMDB_POSTER)]


def test_no_tmdb_id_leaves_poster_alone(poster_env):
    assert asyncio.run(vod_importer.replace_broken_poster("series", 2)) is None
    assert poster_env["fetched"] == [] and poster_env["saved"] == []


def test_tmdb_without_poster_leaves_poster_alone(poster_env, monkeypatch):
    async def no_poster(tmdb_id):
        return {"poster_url": None}

    monkeypatch.setattr(tmdb_sync, "get_series_full_details", no_poster)
    assert asyncio.run(vod_importer.replace_broken_poster("series", 1)) is None
    assert poster_env["saved"] == []


def test_failing_tmdb_poster_is_not_resaved(poster_env, monkeypatch):
    vod_importer.vod_db.get_series(1)["poster_url"] = TMDB_POSTER
    assert asyncio.run(vod_importer.replace_broken_poster("series", 1)) is None
    assert poster_env["saved"] == []


def test_tmdb_not_found_leaves_poster_alone(poster_env, monkeypatch):
    async def gone(tmdb_id):
        raise tmdb_sync.TmdbNotFoundError(tmdb_id)

    monkeypatch.setattr(tmdb_sync, "get_series_full_details", gone)
    assert asyncio.run(vod_importer.replace_broken_poster("series", 1)) is None
    assert poster_env["saved"] == []


def test_unknown_item_returns_none(poster_env):
    assert asyncio.run(vod_importer.replace_broken_poster("series", 999)) is None


def _series_with_tmdb(db, pairs):
    provider = db.upsert_provider("XC", "http://xc.invalid", "u", "p")
    db.bulk_import_series(provider, [
        {"name": name, "year": 2020, "provider_series_id": name} for name, _ in pairs
    ])
    ids = {s["name"]: s["id"] for s in db.list_series(limit=50)}
    conn = db._connect()
    for name, tmdb_id in pairs:
        conn.execute("UPDATE series SET tmdb_id=? WHERE id=?", (tmdb_id, ids[name]))
    conn.commit()
    conn.close()
    return ids


def test_collision_sweep_scoped_to_given_series(db, monkeypatch):
    ids = _series_with_tmdb(db, [("A", "1"), ("A2", "1"), ("B", "2"), ("B2", "2"), ("C", "3")])
    merged = []
    monkeypatch.setattr(db, "get_duplicate_finder_auto_merge_tmdb", lambda: True)
    monkeypatch.setattr(db, "auto_merge_series_by_tmdb_batch", lambda batch: merged.append(sorted(batch)))
    db.auto_merge_series_tmdb_collisions({ids["A"], ids["C"]})
    db.auto_merge_series_tmdb_collisions(set())
    db.auto_merge_series_tmdb_collisions()
    assert merged == [
        sorted([ids["A"], ids["A2"]]),
        sorted([ids["A"], ids["A2"], ids["B"], ids["B2"]]),
    ]


def test_bulk_enrich_sweeps_only_the_series_it_enriched(monkeypatch):
    swept = []

    async def due(movie_ids, series_ids):
        return movie_ids, [s for s in series_ids if s != 2]

    async def enrich(kind, sem, item_id, force, queue):
        return None

    async def writer(queue):
        while await queue.get() is not None:
            pass

    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda: [])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda: [1, 2, 3])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_tmdb_collisions",
                        lambda series_ids=None: swept.append(series_ids))
    monkeypatch.setattr(vod_importer, "_only_due", due)
    monkeypatch.setattr(vod_importer, "_enrich_one", enrich)
    monkeypatch.setattr(vod_importer, "_run_global_writer", writer)
    monkeypatch.setitem(vod_importer._ENRICH_PROGRESS, "running", False)
    asyncio.run(vod_importer.bulk_enrich_all())
    assert swept == [{1, 3}]


def _owned_episode(db, user_id):
    provider = db.upsert_provider("XC", "http://xc.invalid", "u", "p")
    db.bulk_import_series(provider, [{"name": "Show", "year": 2020, "provider_series_id": "1"}])
    series_id = db.list_series(limit=1)[0]["id"]
    conn = db._connect()
    ep = conn.execute("INSERT INTO episodes (series_id, season_number, episode_number, name, created_at) "
                      "VALUES (?, 1, 1, 'Pilot', '')", (series_id,)).lastrowid
    src = conn.execute("INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, added_at, last_seen_at) "
                       "VALUES (?, ?, 's1', '', '')", (ep, provider)).lastrowid
    conn.execute("INSERT INTO episode_source_owners (episode_source_id, dispatcharr_user_id, added_at) "
                 "VALUES (?, ?, '')", (src, user_id))
    conn.commit()
    conn.close()
    return series_id


def test_series_owned_by_portal_user(db):
    series_id = _owned_episode(db, user_id=7)
    assert db.series_owned_by_portal_user(series_id, 7) is True
    assert db.series_owned_by_portal_user(series_id, 8) is False


@pytest.fixture()
def portal_env(monkeypatch):
    import portal_routes
    owned = {("movie", 3), ("series", 1)}
    swapped = []

    async def replace(kind, item_id):
        swapped.append((kind, item_id))
        return TMDB_POSTER

    monkeypatch.setattr(portal_routes.vod_db, "movie_owned_by_portal_user", lambda i, u: ("movie", i) in owned)
    monkeypatch.setattr(portal_routes.vod_db, "series_owned_by_portal_user", lambda i, u: ("series", i) in owned)
    monkeypatch.setattr(portal_routes.vod_importer, "replace_broken_poster", replace)
    return portal_routes, swapped


ACCOUNT = {"provider_id": 1, "dispatcharr_user_id": 7}


def test_portal_fallback_swaps_owned_items(portal_env):
    routes, swapped = portal_env
    assert asyncio.run(routes.portal_poster_fallback("series", 1, ACCOUNT)) == {"poster_url": TMDB_POSTER}
    assert asyncio.run(routes.portal_poster_fallback("movie", 3, ACCOUNT)) == {"poster_url": TMDB_POSTER}
    assert swapped == [("series", 1), ("movie", 3)]


def test_portal_fallback_refuses_items_not_in_library(portal_env):
    from fastapi import HTTPException
    routes, swapped = portal_env
    for kind, item_id in (("movie", 99), ("series", 99), ("episode", 1)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes.portal_poster_fallback(kind, item_id, ACCOUNT))
        assert exc.value.status_code == 404
    assert swapped == []
