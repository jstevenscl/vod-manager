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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(air_date_backfill, "_PAUSE_SECONDS", 0)
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
        return [{"season_number": 1, "episode_number": n, "air_date": d} for n, d in spy.local.get(tmdb_id, {}).items()]

    async def remote(tmdb_id):
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
