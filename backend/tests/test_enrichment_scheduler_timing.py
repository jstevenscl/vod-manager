"""Enrichment scheduler timing: consecutive passes must START about one TTL
apart (not run-duration + TTL), so the per-item TTL check can actually skip
items enriched earlier. With a flat sleep after the finish time, a 9h pass
against a 24h TTL started every 33h and every item was past the TTL again."""

import asyncio

import main

HOUR = 3600
TTL = 24 * HOUR


class _Done(Exception):
    pass


def _run(monkeypatch, run_secs, ttl=TTL, cycles=3, run_raises=False, last_run=None):
    """Drives the real scheduler on a fake clock; returns (start gaps in hours,
    saved stamps relative to the first start, hours slept before the 1st pass)."""
    clock = {"t": 1_000_000.0}
    starts, stamps, sleeps = [], [], []

    async def bulk():
        starts.append(clock["t"])
        clock["t"] += run_secs
        if run_raises:
            raise RuntimeError("boom")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds
        if len(sleeps) > cycles:
            raise _Done()

    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main.vod_importer, "bulk_enrich_all", bulk)
    monkeypatch.setattr(main.vod_db, "get_enrichment_ttl_seconds", lambda: ttl)
    monkeypatch.setattr(main, "get_last_enrichment_run", lambda: last_run)
    monkeypatch.setattr(main, "save_last_enrichment_run", lambda ts: stamps.append(ts))
    try:
        asyncio.run(main._vod_enrichment_scheduler())
    except _Done:
        pass
    gaps = [round((b - a) / HOUR, 2) for a, b in zip(starts, starts[1:])]
    return gaps, [round((s - starts[0]) / HOUR, 2) for s in stamps], sleeps[0]


def test_pass_shorter_than_ttl_starts_one_ttl_apart(monkeypatch):
    gaps, stamps, _ = _run(monkeypatch, 9 * HOUR)
    assert gaps == [24.0, 24.0]  # was 33.0 (9h run + 24h TTL)
    assert stamps[:3] == [0.0, 24.0, 48.0]  # stamped with each run's START


def test_stamp_is_saved_even_when_the_run_raises(monkeypatch):
    gaps, stamps, _ = _run(monkeypatch, 5, run_raises=True)
    assert len(stamps) >= 3
    assert gaps[0] == round((TTL - 5 + 5) / HOUR, 2)  # failed attempts still pace at ~TTL


def test_pass_longer_than_ttl_still_rests_between_runs(monkeypatch):
    gaps, _, _ = _run(monkeypatch, 30 * HOUR)
    assert gaps == [30.25, 30.25]  # 30h run + the 15 min rest, never back-to-back


def test_short_ttl_and_long_pass_does_not_hammer_providers(monkeypatch):
    # TTL=1h, 3h pass: rest is the 15 min floor (never the old back-to-back 45 s)
    gaps, _, _ = _run(monkeypatch, 3 * HOUR, ttl=HOUR)
    assert gaps == [3.25, 3.25]
    # TTL=10 min: the rest is capped at the TTL itself
    gaps, _, _ = _run(monkeypatch, 3 * HOUR, ttl=10 * 60)
    assert gaps == [round((3 * HOUR + 600) / HOUR, 2)] * 2


def test_boot_catch_up_uses_the_start_stamp(monkeypatch):
    # last pass started 9h before boot: next one is due in TTL - 9h = 15h
    start = 1_000_000.0 - 9 * HOUR
    _, _, first_sleep = _run(monkeypatch, HOUR, last_run=start)
    assert round(first_sleep / HOUR, 2) == 15.0


def test_sleep_helper():
    assert main._enrichment_sleep_seconds(TTL, 9 * HOUR) == 15 * HOUR
    assert main._enrichment_sleep_seconds(TTL, 30 * HOUR) == 15 * 60
    assert main._enrichment_sleep_seconds(600, 3 * HOUR) == 600
