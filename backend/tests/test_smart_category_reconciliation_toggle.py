"""The 15-minute full-pool smart-category reconciliation pass can be turned off
without touching event-driven evaluation, and defaults to on."""

import asyncio

import config
import main
import vod_importer


class _Done(Exception):
    pass


def _run_one_cycle(monkeypatch):
    """Runs the real loop for exactly one iteration; returns resweep call count."""
    calls = []

    async def fake_resweep(*_a, **_k):
        calls.append(1)

    async def stop_after_first(_seconds):
        raise _Done

    monkeypatch.setattr(vod_importer, "resweep_smart_categories", fake_resweep)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)
    try:
        asyncio.run(main._uncategorized_sweep_loop())
    except _Done:
        pass
    return len(calls)


def _save(enabled):
    s = config.get_refresh_settings()
    s["smart_category_reconciliation_enabled"] = enabled
    config.save_refresh_settings(**s)


def test_enabled_by_default(db):
    assert config.get_refresh_settings()["smart_category_reconciliation_enabled"] is True


def test_loop_runs_resweep_when_enabled(db, monkeypatch):
    assert _run_one_cycle(monkeypatch) == 1


def test_loop_skips_resweep_when_disabled(db, monkeypatch):
    _save(False)
    assert _run_one_cycle(monkeypatch) == 0


def test_setting_round_trips(db):
    _save(False)
    assert config.get_refresh_settings()["smart_category_reconciliation_enabled"] is False
    _save(True)
    assert config.get_refresh_settings()["smart_category_reconciliation_enabled"] is True
