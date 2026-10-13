"""List-sync matching must not rescan the whole pool per list entry, and must
run off the event loop (a big MDBList/TMDB list used to freeze the server)."""

import asyncio

import vod_db
import vod_list_sync


def _make_movies(db, n):
    conn = vod_db._connect()
    conn.executemany(
        "INSERT INTO movies (name, year, created_at) VALUES (?,?,?)",
        [(f"Pool Movie {i}", 2000 + i % 20, "0") for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_title_year_index_matches_old_rule(db):
    _make_movies(db, 50)
    idx = vod_db.build_title_year_index("movie")
    hit = vod_db.lookup_title_year_index(idx, "Pool Movie 7", 2007)
    assert hit and hit["name"] == "Pool Movie 7"
    assert vod_db.lookup_title_year_index(idx, "Nope", 2007) is None
    # +/-1 year tolerance, same as find_movie_by_title_year
    assert vod_db.lookup_title_year_index(idx, "Pool Movie 7", 2008)["name"] == "Pool Movie 7"
    assert vod_db.find_movie_by_title_year("Pool Movie 7", 2007)["id"] == hit["id"]


def test_sync_matches_by_title_and_builds_index_once(db, monkeypatch):
    _make_movies(db, 200)
    cat = db.upsert_category("L", "movie")
    assert cat
    cid = cat if isinstance(cat, int) else cat["id"]
    builds = []
    real = vod_db.build_title_year_index
    monkeypatch.setattr(vod_db, "build_title_year_index", lambda k: (builds.append(k), real(k))[1])

    async def fake_fetch(source):
        return [{"media_type": "movie", "tmdb_id": 9000 + i, "title": f"Pool Movie {i}", "year": 2000 + i % 20}
                for i in range(150)] + [{"media_type": "movie", "tmdb_id": 99999, "title": "Missing", "year": 1999}]

    monkeypatch.setattr(vod_list_sync, "_fetch_normalized_items", fake_fetch)
    vod_db.set_category_sync_sources(cid, ["mdblist:1"])
    res = asyncio.run(vod_list_sync.sync_category(cid))
    assert res["found_in_pool"] == 150 and res["not_in_pool"] == 1 and res["newly_placed"] == 150
    assert builds == ["movie"]
