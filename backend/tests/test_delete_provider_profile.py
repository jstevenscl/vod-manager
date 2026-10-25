"""Deleting a provider removes the Dispatcharr M3U profile its sync created on the VOD-relay account, and nothing else."""

import asyncio

import vod_sync


class _FakeClient:
    def __init__(self, profiles, fail=False):
        self.profiles = profiles
        self.fail = fail
        self.deleted = []

    async def get(self, path, params=None):
        if self.fail:
            raise RuntimeError("Dispatcharr is down")
        return self.profiles

    async def delete(self, path):
        self.deleted.append(path)


def _setup(db, monkeypatch, profiles, fail=False):
    provider_id = db.upsert_provider("Doomed", "http://example.com", "u", "pw")
    connection_id = db.create_dispatcharr_connection("dispatch-test", "http://gluetun:9191", "token")
    db.update_dispatcharr_connection(connection_id, vod_relay_account_id=31)
    db.set_provider_sync_profile(provider_id, connection_id, 62)
    fake = _FakeClient(profiles, fail)
    monkeypatch.setattr(vod_sync, "DispatcharrClient", lambda url, token: fake)
    return provider_id, fake


def test_the_providers_synced_profile_is_removed(db, monkeypatch):
    provider_id, fake = _setup(db, monkeypatch, [{"id": 62, "name": "Doomed"}, {"id": 63, "name": "Other"}])
    refs = db.list_provider_sync_profiles(provider_id)
    assert [r["profile_id"] for r in refs] == [62]
    db.delete_provider(provider_id)
    asyncio.run(vod_sync.remove_provider_profiles(refs))
    assert fake.deleted == ["/api/m3u/accounts/31/profiles/62/"]


def test_a_default_profile_is_never_deleted(db, monkeypatch):
    provider_id, fake = _setup(db, monkeypatch, [{"id": 62, "name": "Default", "is_default": True}])
    asyncio.run(vod_sync.remove_provider_profiles(db.list_provider_sync_profiles(provider_id)))
    assert fake.deleted == []


def test_a_profile_already_gone_or_dispatcharr_down_is_not_an_error(db, monkeypatch):
    provider_id, fake = _setup(db, monkeypatch, [])
    asyncio.run(vod_sync.remove_provider_profiles(db.list_provider_sync_profiles(provider_id)))
    assert fake.deleted == []
    provider_id, fake = _setup(db, monkeypatch, [], fail=True)
    asyncio.run(vod_sync.remove_provider_profiles(db.list_provider_sync_profiles(provider_id)))  # must not raise
