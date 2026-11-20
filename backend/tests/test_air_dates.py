"""Episode air dates and missing release dates: stored locally, served in the
XC get_series_info episode block, filled from the local TMDB store first and
TMDB second -- never from a provider call."""

import asyncio

import pytest

import tmdb_store
import tmdb_sync
import vod_importer
import xc_server


def _series(db, tmdb_id=None, release_date=None, name="Show"):
    sid = db.upsert_series(name, 2020)
    fields = {}
    if tmdb_id:
        fields["tmdb_id"] = tmdb_id
    if release_date:
        fields["release_date"] = release_date
    if fields:
        db.set_series_enrichment(sid, **fields)
    return sid


def _air(db, sid):
    return {(e["season_number"], e["episode_number"]): e["air_date"] for e in db.list_episodes(sid)}


def _provider(db):
    return db.upsert_provider("P", "http://p.invalid", "u", "p")


# ---- schema / helpers -------------------------------------------------------

def test_episodes_have_air_date_column_and_migration_is_idempotent(db):
    db.init_db()
    conn = db._connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
    finally:
        conn.close()
    assert "air_date" in cols


@pytest.mark.parametrize("raw,expected", [
    ("2020-03-04", "2020-03-04"),
    ("2020-03-04 10:11:12", "2020-03-04"),
    ("2020-03-04T00:00:00Z", "2020-03-04"),
    ("", None), (None, None), ("0000-00-00", None), ("03/04/2020", None), ("2020-13-45", None), (20200304, None),
])
def test_normalize_air_date(db, raw, expected):
    assert db.normalize_air_date(raw) == expected


# ---- provider air date captured in the existing episode write ---------------

def _ep(n, air=None, **kw):
    return {"season_number": 1, "episode_number": n, "name": f"E{n}", "provider_stream_id": f"s{n}",
            "air_date": air, **kw}


def test_episode_batch_stores_provider_air_date_and_never_overwrites(db):
    pid = _provider(db)
    sid = _series(db)
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, "2020-01-01"), _ep(2, None)])
    assert _air(db, sid) == {(1, 1): "2020-01-01", (1, 2): None}
    # A later pass with no date, or a different one, leaves a stored date alone.
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, None), _ep(2, "2020-01-08")])
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, "1999-09-09")])
    assert _air(db, sid) == {(1, 1): "2020-01-01", (1, 2): "2020-01-08"}


def test_provider_episode_info_air_date_is_captured_in_enrich_loop(db, monkeypatch):
    pid = _provider(db)
    sid = _series(db)
    info = {"episodes": {"1": [
        {"id": "11", "episode_num": 1, "title": "A", "season": 1, "info": {"releasedate": "2021-02-03"}},
        {"id": "12", "episode_num": 2, "title": "B", "season": 1, "info": {"air_date": "2021-02-10 00:00:00"}},
        {"id": "13", "episode_num": 3, "title": "C", "season": 1, "info": {"releasedate": "garbage"}},
    ]}}
    calls = []

    class FakeClient:
        def __init__(self, provider):
            pass

        async def get_series_info(self, psid):
            calls.append(psid)
            return info

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)
    source = {"provider_id": pid, "provider_series_id": "555", "id": 1}
    out = asyncio.run(vod_importer._enrich_one_series_source(sid, db.get_series(sid), source, force=True))
    assert out["fetched"] and calls == ["555"]  # still exactly one provider call
    assert _air(db, sid) == {(1, 1): "2021-02-03", (1, 2): "2021-02-10", (1, 3): None}


# ---- fill helpers never overwrite -------------------------------------------

def test_fill_episode_air_dates_only_fills_null(db):
    pid = _provider(db)
    sid = _series(db)
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, "2020-01-01"), _ep(2, None), _ep(3, None)])
    n = db.fill_episode_air_dates(
        sid, {(1, 1): "2000-01-01", (1, 2): "2020-01-08", (1, 9): "2020-02-02", (1, 3): "bad"})
    assert n == 1
    assert _air(db, sid) == {(1, 1): "2020-01-01", (1, 2): "2020-01-08", (1, 3): None}


def test_fill_missing_release_date_never_overwrites(db):
    mid = db.upsert_movie("Film", 2020)
    sid = _series(db)
    assert db.fill_missing_release_date("movie", mid, "2020-05-05") is True
    assert db.fill_missing_release_date("movie", mid, "1999-01-01") is False
    assert db.get_movie(mid)["release_date"] == "2020-05-05"
    db.set_series_enrichment(sid, release_date="  ")
    assert db.fill_missing_release_date("series", sid, "2021-06-06") is True
    assert db.get_series(sid)["release_date"] == "2021-06-06"
    assert db.fill_missing_release_date("series", sid, "bad") is False


# ---- XC output ---------------------------------------------------------------

def test_xc_get_series_info_air_date_present_only_when_set(db, monkeypatch):
    monkeypatch.setattr(xc_server, "_allowed_category_ids", lambda auth: None)
    monkeypatch.setattr(xc_server.vod_db, "get_series_export_row_by_export_id", lambda i: {
        "series_id": 1, "export_series_id": i, "name": "Show", "year": 2020, "category_id": 1,
        "genre": "", "description": "", "release_date": None, "name_suffix": ""})
    base = {"export_episode_id": 5, "season_number": 1, "description": None, "duration_secs": None,
            "container_extension": "mp4", "bitrate": None}
    monkeypatch.setattr(xc_server.vod_db, "get_episode_export_rows_for_series", lambda sid: [
        {**base, "episode_number": 1, "name": "A", "air_date": "2020-01-01"},
        {**base, "episode_number": 2, "name": "B", "air_date": None},
        {**base, "episode_number": 3, "name": "C"},
    ])
    out = xc_server._handle_player_api_action("get_series_info", {"series_id": "1"}, {})
    eps = out["episodes"]["1"]
    assert eps[0]["info"]["air_date"] == "2020-01-01"
    assert "air_date" not in eps[1]["info"] and "air_date" not in eps[2]["info"]
    assert out["info"]["releaseDate"] == ""


def test_export_rows_select_air_date(db):
    pid = _provider(db)
    sid = _series(db)
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, "2020-01-01")])
    rows = db.get_episode_export_rows_for_series(sid)
    assert rows[0]["air_date"] == "2020-01-01"


# ---- TMDB fill: local store first, TMDB only when still missing -------------

class _TmdbSpy:
    def __init__(self, monkeypatch, *, episodes=(), first_air="2019-09-09"):
        self.eps_calls, self.detail_calls = 0, 0
        self.episodes, self.first_air = list(episodes), first_air
        self.local_eps, self.local_first_air = [], None
        monkeypatch.setattr(tmdb_sync, "get_series_episode_list_local", self._local)
        monkeypatch.setattr(tmdb_sync, "get_series_episode_list", self._remote)
        monkeypatch.setattr(tmdb_sync, "get_series_full_details", self._detail)
        monkeypatch.setattr(tmdb_sync, "get_series_first_air_date_local", self._local_first, raising=False)

    async def _local(self, tmdb_id):
        return list(self.local_eps)

    async def _local_first(self, tmdb_id):
        return self.local_first_air

    async def _remote(self, tmdb_id, need_stills=False):
        self.eps_calls += 1
        self.need_stills_seen = need_stills
        return self.episodes

    async def _detail(self, tmdb_id):
        self.detail_calls += 1
        return {"release_date": self.first_air}


def _setup_series_with_missing(db):
    pid = _provider(db)
    sid = _series(db, tmdb_id="1396")
    db.enrich_series_episodes_batch(sid, pid, [_ep(1), _ep(2)])
    return sid


def test_series_fill_uses_local_store_and_makes_no_tmdb_call(db, monkeypatch):
    sid = _setup_series_with_missing(db)
    spy = _TmdbSpy(monkeypatch)
    spy.local_eps = [{"season_number": 1, "episode_number": 1, "air_date": "2008-01-20", "still_path": "/1.jpg"},
                     {"season_number": 1, "episode_number": 2, "air_date": "2008-01-27", "still_path": "/2.jpg"}]
    spy.local_first_air = "2008-01-20"
    asyncio.run(vod_importer.fill_series_air_dates(sid))
    assert _air(db, sid) == {(1, 1): "2008-01-20", (1, 2): "2008-01-27"}
    assert db.get_series(sid)["release_date"] == "2008-01-20"
    assert spy.eps_calls == 0 and spy.detail_calls == 0


def test_series_fill_falls_back_to_tmdb_when_local_lacks_it(db, monkeypatch):
    sid = _setup_series_with_missing(db)
    spy = _TmdbSpy(monkeypatch, episodes=[{"season_number": 1, "episode_number": 1, "air_date": "2008-01-20"}])
    asyncio.run(vod_importer.fill_series_air_dates(sid))
    assert _air(db, sid) == {(1, 1): "2008-01-20", (1, 2): None}  # NULL stays NULL
    assert db.get_series(sid)["release_date"] == "2019-09-09"
    assert spy.eps_calls == 1 and spy.detail_calls == 1


def test_series_fill_skips_when_nothing_missing_or_no_tmdb_id(db, monkeypatch):
    spy = _TmdbSpy(monkeypatch)
    pid = _provider(db)
    no_id = _series(db, name="NoId")
    db.enrich_series_episodes_batch(no_id, pid, [_ep(1)])
    complete = _series(db, tmdb_id="5", release_date="2020-01-01", name="Done")
    db.enrich_series_episodes_batch(complete, pid, [_ep(1, "2020-01-01", provider_stream_id="zz")])
    db.fill_episode_stills(complete, {(1, 1): "/done.jpg"})
    asyncio.run(vod_importer.fill_series_air_dates(no_id))
    asyncio.run(vod_importer.fill_series_air_dates(complete))
    assert spy.eps_calls == 0 and spy.detail_calls == 0


def test_series_fill_never_overwrites_release_date_and_survives_tmdb_failure(db, monkeypatch):
    sid = _setup_series_with_missing(db)
    db.set_series_enrichment(sid, release_date="2001-01-01")
    spy = _TmdbSpy(monkeypatch, first_air="2019-09-09")

    async def boom(tmdb_id):
        raise RuntimeError("tmdb down")

    monkeypatch.setattr(tmdb_sync, "get_series_episode_list", boom)
    asyncio.run(vod_importer.fill_series_air_dates(sid))  # must not raise
    assert db.get_series(sid)["release_date"] == "2001-01-01"
    assert spy.detail_calls == 0  # release date present -> no lookup for it


def test_movie_fill_tmdb_once_then_stays_set(db, monkeypatch):
    mid = db.upsert_movie("Film", 2020)
    db.set_movie_enrichment(mid, tmdb_id="603")
    payloads = []

    async def fake_payload(media_type, tmdb_id, **kw):
        payloads.append((media_type, tmdb_id))
        return {"release_date": "1999-03-31"}

    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", fake_payload)
    asyncio.run(vod_importer.fill_movie_release_date(mid))
    assert db.get_movie(mid)["release_date"] == "1999-03-31" and payloads == [("movie", "603")]
    asyncio.run(vod_importer.fill_movie_release_date(mid))  # already set: no second lookup
    assert payloads == [("movie", "603")]


def test_movie_fill_no_tmdb_id_or_tmdb_without_date_stays_null(db, monkeypatch):
    async def fake_payload(media_type, tmdb_id, **kw):
        return {"release_date": ""}

    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", fake_payload)
    a = db.upsert_movie("NoId", 2020)
    b = db.upsert_movie("NoDate", 2021)
    db.set_movie_enrichment(b, tmdb_id="7")
    asyncio.run(vod_importer.fill_movie_release_date(a))
    asyncio.run(vod_importer.fill_movie_release_date(b))
    assert not db.get_movie(a)["release_date"] and not db.get_movie(b)["release_date"]


def test_get_series_first_air_date_local_reads_store_only(db, monkeypatch):
    tmdb_store.init_db()
    tmdb_store.upsert_payload("tv", {"id": 1396, "name": "BB", "first_air_date": "2008-01-20", "seasons": []})
    monkeypatch.setattr(tmdb_sync, "store_enabled", lambda: True)

    async def no_network(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(tmdb_sync, "_tmdb_get", no_network)
    assert asyncio.run(tmdb_sync.get_series_first_air_date_local("1396")) == "2008-01-20"
    assert asyncio.run(tmdb_sync.get_series_first_air_date_local("999")) is None


# ---- GH #82: episode still images (movie_image) -------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("/abc123.jpg", "/abc123.jpg"), (" /a-b_c.png ", "/a-b_c.png"),
    ("", None), (None, None), ("abc.jpg", None), ("/a b.jpg", None), ("http://x/y.jpg", None), (5, None),
])
def test_normalize_still_path(db, raw, expected):
    assert db.normalize_still_path(raw) == expected


def test_episodes_have_still_path_column(db):
    db.init_db()
    conn = db._connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
    finally:
        conn.close()
    assert "still_path" in cols


def _stills(db, sid):
    return {(e["season_number"], e["episode_number"]): e.get("still_path") for e in db.list_episodes(sid)}


def test_fill_episode_stills_only_fills_null(db):
    pid = _provider(db)
    sid = _series(db)
    db.enrich_series_episodes_batch(sid, pid, [_ep(1), _ep(2), _ep(3)])
    assert db.fill_episode_stills(sid, {(1, 1): "/one.jpg", (1, 3): "bad", (1, 9): "/nine.jpg"}) == 1
    assert db.fill_episode_stills(sid, {(1, 1): "/other.jpg", (1, 2): "/two.jpg"}) == 1
    assert _stills(db, sid) == {(1, 1): "/one.jpg", (1, 2): "/two.jpg", (1, 3): None}
    assert db.count_episodes_missing_still(sid) == 1


def test_export_rows_select_still_path(db):
    pid = _provider(db)
    sid = _series(db)
    db.enrich_series_episodes_batch(sid, pid, [_ep(1)])
    db.fill_episode_stills(sid, {(1, 1): "/s.jpg"})
    assert db.get_episode_export_rows_for_series(sid)[0]["still_path"] == "/s.jpg"


def test_xc_get_series_info_serves_movie_image_and_releasedate(db, monkeypatch):
    monkeypatch.setattr(xc_server, "_allowed_category_ids", lambda auth: None)
    monkeypatch.setattr(xc_server.vod_db, "get_series_export_row_by_export_id", lambda i: {
        "series_id": 1, "export_series_id": i, "name": "Show", "year": 2020, "category_id": 1,
        "genre": "", "description": "", "release_date": None, "name_suffix": ""})
    base = {"export_episode_id": 5, "season_number": 1, "description": None, "duration_secs": None,
            "container_extension": "mp4", "bitrate": None}
    monkeypatch.setattr(xc_server.vod_db, "get_episode_export_rows_for_series", lambda sid: [
        {**base, "episode_number": 1, "name": "A", "air_date": "2020-01-01", "still_path": "/a.jpg"},
        {**base, "episode_number": 2, "name": "B", "air_date": None, "still_path": None},
    ])
    eps = xc_server._handle_player_api_action("get_series_info", {"series_id": "1"}, {})["episodes"]["1"]
    assert eps[0]["info"]["movie_image"] == "https://image.tmdb.org/t/p/w500/a.jpg"
    assert eps[0]["info"]["releasedate"] == "2020-01-01" == eps[0]["info"]["air_date"]
    for key in ("movie_image", "releasedate", "air_date"):
        assert key not in eps[1]["info"]


def test_series_fill_stills_local_first_then_tmdb_with_need_stills(db, monkeypatch):
    pid = _provider(db)
    sid = _series(db, tmdb_id="1396", release_date="2008-01-20")
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, "2008-01-20"), _ep(2, "2008-01-27")])
    spy = _TmdbSpy(monkeypatch, episodes=[{"season_number": 1, "episode_number": 2, "air_date": "2008-01-27",
                                           "still_path": "/two.jpg"}])
    spy.local_eps = [{"season_number": 1, "episode_number": 1, "air_date": "2008-01-20", "still_path": "/one.jpg"}]
    asyncio.run(vod_importer.fill_series_air_dates(sid))
    assert _stills(db, sid) == {(1, 1): "/one.jpg", (1, 2): "/two.jpg"}
    assert spy.eps_calls == 1 and spy.need_stills_seen is True


def test_series_fill_stills_complete_locally_makes_no_tmdb_call(db, monkeypatch):
    pid = _provider(db)
    sid = _series(db, tmdb_id="1396", release_date="2008-01-20")
    db.enrich_series_episodes_batch(sid, pid, [_ep(1, "2008-01-20")])
    spy = _TmdbSpy(monkeypatch)
    spy.local_eps = [{"season_number": 1, "episode_number": 1, "air_date": "2008-01-20", "still_path": "/one.jpg"}]
    asyncio.run(vod_importer.fill_series_air_dates(sid))
    assert _stills(db, sid) == {(1, 1): "/one.jpg"}
    assert spy.eps_calls == 0 and spy.detail_calls == 0


def test_backfill_selection_includes_series_missing_only_a_still(db):
    pid = _provider(db)
    only_still = _series(db, tmdb_id="7", name="OnlyStill")
    db.enrich_series_episodes_batch(only_still, pid, [_ep(1, "2020-01-01")])
    complete = _series(db, tmdb_id="8", name="Complete")
    db.enrich_series_episodes_batch(complete, pid, [_ep(1, "2020-01-01", provider_stream_id="zz")])
    db.fill_episode_stills(complete, {(1, 1): "/c.jpg"})
    assert db.list_series_ids_missing_air_dates() == [only_still]


# ---- tmdb_sync: season rows carry still_path; old cached rows are refetched once ----

def _season_fixture(monkeypatch, cached_rows):
    tmdb_store.init_db()
    monkeypatch.setattr(tmdb_sync, "store_enabled", lambda: True)
    monkeypatch.setattr(tmdb_sync, "get_tmdb_api_key", lambda: "k")
    shows = {"id": 1, "name": "S", "seasons": [{"season_number": 1}]}

    async def payload(kind, tmdb_id):
        return shows

    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", payload)
    tmdb_store.put_season(1, 1, cached_rows)
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"episodes": [{"episode_number": 1, "name": "E1", "air_date": "2020-01-01", "still_path": "/n.jpg"}]}

    async def get(url, params=None):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(tmdb_sync, "_tmdb_get", get)
    return calls


def test_season_fetch_keeps_still_path_and_old_cache_refetched_once_only_when_needed(db, monkeypatch):
    old = [{"season_number": 1, "episode_number": 1, "name": "E1", "air_date": "2020-01-01"}]  # no still_path key
    calls = _season_fixture(monkeypatch, old)
    # A caller that does not need stills is served from the old cache: no request.
    assert asyncio.run(tmdb_sync.get_series_episode_list("1")) == old
    assert calls == []
    # A caller that needs stills refetches once and the row is rewritten with the key.
    fresh = asyncio.run(tmdb_sync.get_series_episode_list("1", need_stills=True))
    assert fresh[0]["still_path"] == "/n.jpg" and len(calls) == 1
    asyncio.run(tmdb_sync.get_series_episode_list("1", need_stills=True))
    assert len(calls) == 1  # now cached with the key: never refetched again
