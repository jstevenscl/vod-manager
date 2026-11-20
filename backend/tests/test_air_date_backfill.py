"""One-time, user-started backfill of missing episode air dates: only series with a
TMDB id and a missing date are visited, stored dates are never overwritten, one
failing series never aborts the run, start is idempotent, state survives a restart,
and no provider is ever contacted."""

import asyncio

import pytest

import air_date_backfill
import tmdb_sync
import vod_importer
import vod_routes


def _ep(n, air=None):
    return {"season_number": 1, "episode_number": n, "name": f"E{n}", "provider_stream_id": f"s{n}", "air_date": air}


def _series(db, name, tmdb_id=None, air_dates=(None,)):
    pid = db.upsert_provider("P", "http://p.invalid", "u", "p")
    sid = db.upsert_series(name, 2020)
    if tmdb_id:
        db.set_series_enrichment(sid, tmdb_id=tmdb_id)
    db.enrich_series_episodes_batch(sid, pid, [_ep(i + 1, d) for i, d in enumerate(air_dates)])
    return sid


def _air(db, sid):
    return {e["episode_number"]: e["air_date"] for e in db.list_episodes(sid)}


class _FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # No real waiting in tests: the rate limiter's clock and sleep are faked.
    ft = _FakeTime()
    monkeypatch.setattr(air_date_backfill, "_clock", ft.clock)
    monkeypatch.setattr(air_date_backfill, "_sleep", ft.sleep)
    monkeypatch.setattr(pytest, "fake_time", ft, raising=False)
    monkeypatch.setattr(air_date_backfill, "_task", None)

    def boom(*a, **k):
        raise AssertionError("provider client must never be used by the air date backfill")
    monkeypatch.setattr(vod_importer, "XCProviderClient", boom)


@pytest.fixture()
def tmdb(monkeypatch):
    """Local library knows these dates; TMDB proper returns nothing."""
    class Spy:
        local = {}
        remote_calls = 0
    spy = Spy()

    async def local(tmdb_id):
        return [{"season_number": 1, "episode_number": n, "air_date": d, "still_path": f"/s{n}.jpg"}
                for n, d in spy.local.get(tmdb_id, {}).items()]

    async def remote(tmdb_id, need_stills=False):
        spy.remote_calls += 1
        return []

    async def none(tmdb_id):
        return None

    monkeypatch.setattr(tmdb_sync, "get_series_episode_list_local", local)
    monkeypatch.setattr(tmdb_sync, "get_series_episode_list", remote)
    monkeypatch.setattr(tmdb_sync, "get_series_first_air_date_local", none, raising=False)
    monkeypatch.setattr(tmdb_sync, "get_series_full_details", none)
    return spy


def _run():
    async def go():
        air_date_backfill.start()
        await air_date_backfill._task
    asyncio.run(go())


def test_default_state_is_pending(db):
    st = air_date_backfill.get_status()
    assert st["state"] == "pending"
    assert st["running"] is False
    assert (st["total"], st["processed"], st["filled"], st["skipped_no_tmdb_id"], st["errors"]) == (0, 0, 0, 0, 0)
    assert st["started_at"] is None and st["finished_at"] is None


def test_only_series_with_tmdb_id_and_missing_dates_are_processed(db, tmdb, monkeypatch):
    with_id = _series(db, "A", "100", (None, None))
    complete = _series(db, "B", "200", ("2020-01-01",))
    db.fill_episode_stills(complete, {(1, 1): "/kept.jpg"})  # has its date and its picture already
    no_id = _series(db, "C", None, (None,))
    tmdb.local = {"100": {1: "2008-01-20", 2: "2008-01-27"}, "200": {1: "1999-01-01"}}
    seen = []
    real = vod_importer.fill_series_air_dates

    async def spy(sid):
        seen.append(sid)
        await real(sid)
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", spy)
    _run()
    assert seen == [with_id]
    assert _air(db, with_id) == {1: "2008-01-20", 2: "2008-01-27"}
    assert _air(db, complete) == {1: "2020-01-01"}  # stored date untouched
    assert _air(db, no_id) == {1: None}
    st = air_date_backfill.get_status()
    assert (st["total"], st["processed"], st["filled"], st["skipped_no_tmdb_id"], st["errors"]) == (1, 1, 2, 1, 0)
    assert st["stills_filled"] == 2  # GH #82: the pictures are filled in the same pass


def test_already_filled_dates_are_never_overwritten(db, tmdb):
    sid = _series(db, "A", "100", ("2020-01-01", None))
    tmdb.local = {"100": {1: "1900-01-01", 2: "2020-01-08"}}
    _run()
    assert _air(db, sid) == {1: "2020-01-01", 2: "2020-01-08"}


def test_one_failing_series_does_not_abort_the_run(db, tmdb, monkeypatch):
    bad = _series(db, "Bad", "1", (None,))
    good = _series(db, "Good", "2", (None,))
    tmdb.local = {"2": {1: "2010-10-10"}}
    real = vod_importer.fill_series_air_dates

    async def flaky(sid):
        if sid == bad:
            raise RuntimeError("boom")
        await real(sid)
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", flaky)
    _run()
    assert _air(db, good) == {1: "2010-10-10"}
    st = air_date_backfill.get_status()
    assert st["state"] == "done" and st["errors"] == 1 and st["processed"] == 2


def test_state_transitions_pending_running_done(db, tmdb, monkeypatch):
    _series(db, "A", "100", (None,))
    tmdb.local = {"100": {1: "2008-01-20"}}
    states = []
    real = vod_importer.fill_series_air_dates

    async def watch(s):
        states.append(air_date_backfill.get_status()["state"])
        await real(s)
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", watch)

    async def go():
        assert air_date_backfill.get_status()["state"] == "pending"
        out = air_date_backfill.start()
        assert out["state"] == "running" and out["started_at"]
        await air_date_backfill._task
    asyncio.run(go())
    assert states == ["running"]
    st = air_date_backfill.get_status()
    assert st["state"] == "done" and st["finished_at"] and not st["running"]


def test_start_is_idempotent_while_running_and_after_done(db, tmdb, monkeypatch):
    _series(db, "A", "100", (None,))
    tmdb.local = {"100": {1: "2008-01-20"}}
    calls = []
    real = vod_importer.fill_series_air_dates

    async def counting(s):
        calls.append(s)
        await real(s)
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", counting)

    async def go():
        air_date_backfill.start()
        first = air_date_backfill._task
        air_date_backfill.start()  # ignored: already running
        assert air_date_backfill._task is first
        await first
        air_date_backfill.start()  # ignored: done
        assert air_date_backfill._task is first
    asyncio.run(go())
    assert len(calls) == 1
    assert air_date_backfill.get_status()["state"] == "done"


def test_resumes_after_restart_and_skips_finished_series(db, tmdb, monkeypatch):
    a = _series(db, "A", "100", (None,))
    b = _series(db, "B", "200", (None,))
    tmdb.local = {"100": {1: "2001-01-01"}, "200": {1: "2002-02-02"}}
    # Simulate a crash: persisted state says running, A is already done, no live task.
    air_date_backfill._save({**air_date_backfill.get_status(), "state": "running", "started_at": 111.0,
                             "processed": 1, "total": 2, "filled": 1})
    asyncio.run(vod_importer.fill_series_air_dates(a))
    seen = []
    real = vod_importer.fill_series_air_dates

    async def spy(s):
        seen.append(s)
        await real(s)
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", spy)

    async def go():
        air_date_backfill.resume_if_interrupted()
        await air_date_backfill._task
    asyncio.run(go())
    assert seen == [b]
    st = air_date_backfill.get_status()
    assert st["state"] == "done" and st["started_at"] == 111.0 and st["filled"] == 2


def test_resume_does_nothing_when_pending(db, tmdb):
    async def go():
        air_date_backfill.resume_if_interrupted()
        assert air_date_backfill._task is None
    asyncio.run(go())
    assert air_date_backfill.get_status()["state"] == "pending"


def test_no_provider_is_contacted_even_when_tmdb_has_nothing(db, tmdb):
    _series(db, "A", "100", (None,))
    _run()  # the XCProviderClient guard raises if a provider is ever built
    assert air_date_backfill.get_status()["state"] == "done"


def test_routes_expose_status_and_idempotent_start(db, tmdb):
    _series(db, "A", "100", (None,))
    tmdb.local = {"100": {1: "2008-01-20"}}

    async def go():
        assert (await vod_routes.get_air_date_backfill_status())["state"] == "pending"
        out = await vod_routes.start_air_date_backfill()
        assert out["state"] == "running"
        task = air_date_backfill._task
        await vod_routes.start_air_date_backfill()
        assert air_date_backfill._task is task
        await task
        return await vod_routes.get_air_date_backfill_status()
    st = asyncio.run(go())
    assert st["state"] == "done" and st["filled"] == 1




# ---- rate limiter, pacing, concurrency, write path ---------------------------

def _limiter(target=20.0, cap=25.0):
    ft = pytest.fake_time
    return air_date_backfill._RateLimiter(target, cap, clock=ft.clock, sleep=ft.sleep), ft


def test_default_target_is_20_and_hard_cap_25():
    assert air_date_backfill._TARGET_REQUESTS_PER_SECOND == 20.0
    assert air_date_backfill._HARD_CAP_REQUESTS_PER_SECOND == 25.0


def test_limiter_caps_rate_at_target():
    lim, ft = _limiter()

    async def go():
        t0 = ft.now
        for _ in range(41):
            await lim.acquire()
        return ft.now - t0
    assert asyncio.run(go()) == pytest.approx(2.0)  # 41 requests at 20/s


def test_limiter_never_exceeds_hard_cap_even_if_target_is_higher():
    lim, ft = _limiter(target=100.0, cap=25.0)

    async def go():
        t0 = ft.now
        for _ in range(51):
            await lim.acquire()
        return ft.now - t0
    assert asyncio.run(go()) >= 2.0 - 1e-9  # 50 gaps at >= 1/25 s


def test_limiter_concurrent_callers_are_spaced_out():
    lim, ft = _limiter()

    async def one(times):
        await lim.acquire()
        times.append(ft.now)

    async def go():
        times = []
        await asyncio.gather(*[one(times) for _ in range(30)])
        return sorted(times)
    times = asyncio.run(go())
    assert all(b - a >= 1 / 25 - 1e-9 for a, b in zip(times, times[1:]))


def test_429_halves_the_rate_then_recovers_gradually_and_bursts_count_once():
    lim, ft = _limiter()
    lim.slow_down()
    assert lim.rate == pytest.approx(10.0)
    lim.slow_down()  # same burst of 429s: no second halving
    assert lim.rate == pytest.approx(10.0)

    async def go():
        ft.now += air_date_backfill._RECOVER_AFTER
        await lim.acquire()
        first = lim.rate
        ft.now += air_date_backfill._RECOVER_AFTER
        await lim.acquire()
        return first, lim.rate
    first, second = asyncio.run(go())
    assert 10.0 < first < second <= 20.0
    # a later, separate 429 halves again
    ft.now += air_date_backfill._SLOWDOWN_COOLDOWN
    before = lim.rate
    lim.slow_down()
    assert lim.rate == pytest.approx(before / 2)


def test_tmdb_429_through_tmdb_get_slows_the_pacer(monkeypatch):
    class Resp:
        status_code = 429

    async def fake_get(url, params=None):
        return Resp()
    monkeypatch.setattr(tmdb_sync._tmdb_client, "get", fake_get)
    monkeypatch.setattr(tmdb_sync, "_tmdb_backoff_until", 0.0)
    lim, _ = _limiter()
    pacer = air_date_backfill._Pacer(lim)

    async def go():
        token = tmdb_sync._tmdb_pacing.set(pacer)
        try:
            await tmdb_sync._tmdb_get("http://x", {})
        finally:
            tmdb_sync._tmdb_pacing.reset(token)
    asyncio.run(go())
    assert lim.rate == pytest.approx(10.0)


def test_tmdb_get_without_a_pacer_is_untouched(monkeypatch):
    class Resp:
        status_code = 200

    async def fake_get(url, params=None):
        return Resp()
    monkeypatch.setattr(tmdb_sync._tmdb_client, "get", fake_get)
    assert tmdb_sync._tmdb_pacing.get() is None
    assert asyncio.run(tmdb_sync._tmdb_get("http://x", {})).status_code == 200


def test_local_only_series_incur_no_pacing_delay(db, tmdb):
    ft = pytest.fake_time
    for i in range(30):
        _series(db, f"S{i}", str(100 + i), (None,))
        tmdb.local[str(100 + i)] = {1: "2008-01-20"}
    _run()
    assert ft.sleeps == []
    assert air_date_backfill.get_status()["filled"] == 30


def test_remote_requests_are_paced_across_concurrent_workers(db, tmdb, monkeypatch):
    ft = pytest.fake_time
    for i in range(40):
        _series(db, f"S{i}", str(100 + i), (None,))
    stamps = []

    async def remote(tmdb_id, need_stills=False):  # one request per series, entering the pacer like the real season fetch
        async with tmdb_sync._paced_slot():
            stamps.append(ft.now)
        return [{"season_number": 1, "episode_number": 1, "air_date": "2010-01-01"}]
    monkeypatch.setattr(tmdb_sync, "get_series_episode_list", remote)
    _run()
    stamps.sort()
    assert len(stamps) == 40
    assert stamps[-1] - stamps[0] >= 39 / 25 - 1e-9          # never above the 25/s cap
    assert stamps[-1] - stamps[0] <= 39 / 20 + 0.05          # and it does reach ~20/s
    st = air_date_backfill.get_status()
    assert (st["processed"], st["total"], st["filled"], st["errors"]) == (40, 40, 40, 0)


def test_in_flight_requests_stay_under_the_backfill_share(db, tmdb, monkeypatch):
    for i in range(40):
        _series(db, f"S{i}", str(100 + i), (None,))
    now, peak = [0], [0]

    async def remote(tmdb_id, need_stills=False):
        async with tmdb_sync._paced_slot():
            now[0] += 1
            peak[0] = max(peak[0], now[0])
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            now[0] -= 1
        return []
    monkeypatch.setattr(tmdb_sync, "get_series_episode_list", remote)
    _run()
    assert 1 <= peak[0] <= air_date_backfill._MAX_IN_FLIGHT < tmdb_sync._GLOBAL_TMDB_CONCURRENCY


def test_counters_correct_under_concurrency_with_failures(db, tmdb, monkeypatch):
    ids = [_series(db, f"S{i}", str(100 + i), (None, None)) for i in range(40)]
    bad = set(ids[::4])
    real = vod_importer.fill_series_air_dates

    async def flaky(sid):
        await asyncio.sleep(0)
        if sid in bad:
            raise RuntimeError("boom")
        await real(sid)
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", flaky)
    tmdb.local = {str(100 + i): {1: "2001-01-01", 2: "2001-01-02"} for i in range(40)}
    _run()
    st = air_date_backfill.get_status()
    assert (st["total"], st["processed"], st["errors"], st["filled"]) == (40, 40, 10, 60)
    assert st["state"] == "done"


def test_progress_is_saved_every_batch_while_running(db, tmdb, monkeypatch):
    for i in range(60):
        _series(db, f"S{i}", str(100 + i), (None,))
    saved = []
    real_save = air_date_backfill._save

    def spy(st):
        saved.append(st["processed"])
        real_save(st)
    monkeypatch.setattr(air_date_backfill, "_save", spy)
    _run()
    assert 25 in saved and 50 in saved and saved[-1] == 60


def test_series_sharing_a_tmdb_id_do_not_run_at_the_same_time(db, tmdb, monkeypatch):
    a = _series(db, "A", "555", (None,))
    b = _series(db, "B", "555", (None,))
    tmdb.local = {"555": {1: "2009-09-09"}}
    active, peak = [0], [0]
    real = vod_importer.fill_series_air_dates

    async def watch(sid):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0)
        await real(sid)
        active[0] -= 1
    monkeypatch.setattr(vod_importer, "fill_series_air_dates", watch)
    _run()
    assert peak[0] == 1
    assert _air(db, a) == _air(db, b) == {1: "2009-09-09"}
    assert air_date_backfill.get_status()["errors"] == 0


def test_parallel_catalog_writes_lose_no_updates_and_never_lock(db):
    import threading
    ids = [_series(db, f"W{i}", str(900 + i), (None,) * 30) for i in range(12)]
    errors = []

    def work(sid, n):
        try:
            for _ in range(5):
                db.fill_episode_air_dates(sid, {(1, e): f"20{10 + n:02d}-01-{(e % 28) + 1:02d}" for e in range(1, 31)})
                db.fill_missing_release_date("series", sid, f"20{10 + n:02d}-05-05")
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(sid, n)) for n, sid in enumerate(ids)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    for n, sid in enumerate(ids):
        eps = _air(db, sid)
        assert len(eps) == 30 and all(eps.values())
        assert db.get_series(sid)["release_date"] == f"20{10 + n:02d}-05-05"


def test_parallel_tmdb_store_season_writes_never_lock(tmp_path, monkeypatch):
    import threading
    import tmdb_store
    monkeypatch.setattr(tmdb_store, "DB_PATH", tmp_path / "tmdb.sqlite")
    monkeypatch.setattr(tmdb_store, "_initialized_path", None)
    tmdb_store.init_db()
    errors = []

    def work(n):
        try:
            for season in range(1, 11):
                tmdb_store.put_season(n, season, [{"season_number": season, "episode_number": 1, "air_date": "2001-01-01"}])
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert all(tmdb_store.get_season(n, 10) for n in range(16))


def test_a_dates_only_run_from_before_images_is_offered_again(db):
    air_date_backfill._save({"state": "done", "total": 5, "processed": 5, "filled": 9, "skipped_no_tmdb_id": 1,
                             "errors": 0, "started_at": 1.0, "finished_at": 2.0})
    # simulate a state written by the older job: no stills_filled key on disk
    import config
    old = config.get_air_date_backfill()
    old.pop("stills_filled", None)
    config.save_air_date_backfill(old)
    st = air_date_backfill.get_status()
    assert st["state"] == "pending" and (st["total"], st["processed"], st["filled"]) == (0, 0, 0)
    # a run that did record the counter stays done
    air_date_backfill._save({"state": "done", "total": 5, "processed": 5, "filled": 9, "stills_filled": 4,
                             "skipped_no_tmdb_id": 0, "errors": 0, "started_at": 1.0, "finished_at": 2.0})
    assert air_date_backfill.get_status()["state"] == "done"
