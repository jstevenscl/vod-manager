"""A new provider can be added inactive, so the scheduled catalog refresh
(which only imports active providers) can't pull its whole catalog before
category exclusions are set up."""

import asyncio

import vod_routes
import vod_sync


def _add(name, **kwargs):
    body = vod_routes.ProviderRequest(name=name, base_url="http://x.invalid", username="u", password="p", **kwargs)
    return asyncio.run(vod_routes.upsert_provider(body))


def _is_active(db, name):
    return next(p for p in db.list_providers() if p["name"] == name)["is_active"]


def _no_dispatcharr(monkeypatch):
    async def noop(provider_id):
        return None
    monkeypatch.setattr(vod_sync, "sync_provider", noop)


def test_new_provider_defaults_to_active(db, monkeypatch):
    _no_dispatcharr(monkeypatch)
    _add("Active One")
    assert _is_active(db, "Active One")


def test_new_provider_can_start_inactive(db, monkeypatch):
    _no_dispatcharr(monkeypatch)
    _add("Inactive One", is_active=False)
    assert not _is_active(db, "Inactive One")


def test_updating_an_existing_provider_does_not_change_its_active_state(db, monkeypatch):
    _no_dispatcharr(monkeypatch)
    _add("Existing")
    _add("Existing", is_active=False)  # same name = an update, not a new provider
    assert _is_active(db, "Existing")
