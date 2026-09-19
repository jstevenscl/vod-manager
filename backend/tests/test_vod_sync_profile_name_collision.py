"""Dispatcharr enforces a unique (account, name) constraint on M3U profiles
and 500s (not a clean 409) on a collision. _sync_provider_to_connection's
local provider_sync_profiles mapping can't always know about a Dispatcharr-
side profile that already carries this provider's name (a prior sync could
have created it before failing on a later step, or the provider was deleted
and recreated) -- real bug found live (GH#27/#32 follow-up) reproduced
against a real Dispatcharr instance. It must look up and adopt an existing
same-name profile instead of blindly POSTing into the collision.
"""

import asyncio

import vod_sync


class _FakeClient:
    def __init__(self, existing_profiles, post_should_fail=False):
        self.existing_profiles = existing_profiles
        self.post_should_fail = post_should_fail
        self.posted = []
        self.patched = []

    async def get(self, path, params=None):
        return self.existing_profiles

    async def post(self, path, data):
        if self.post_should_fail:
            raise RuntimeError("Server error '500 Internal Server Error' -- duplicate key value violates unique constraint")
        self.posted.append(data)
        return {"id": 999, **data}

    async def patch(self, path, data):
        self.patched.append(data)
        return {"id": int(path.rstrip("/").rsplit("/", 1)[-1]), **data}


def test_adopts_existing_same_name_profile_instead_of_posting(db, monkeypatch):
    provider_id = db.upsert_provider("Jellyfin Local Test", "http://jellyfin-local:8096", "", "key", provider_type="jellyfin")
    connection_id = db.create_dispatcharr_connection("dispatch-test", "http://gluetun:9191", "token")
    db.update_dispatcharr_connection(connection_id, vod_relay_account_id=31)

    fake_client = _FakeClient(existing_profiles=[{"id": 52, "name": "Jellyfin Local Test", "max_streams": 0}])
    monkeypatch.setattr(vod_sync, "DispatcharrClient", lambda url, token: fake_client)

    provider = db.get_provider(provider_id)
    connection = db.get_dispatcharr_connection(connection_id)
    result = asyncio.run(vod_sync._sync_provider_to_connection(provider, connection))

    # Adopted the existing Dispatcharr-side profile (id=52) rather than
    # attempting to create a duplicate.
    assert fake_client.posted == []
    assert db.get_provider_sync_profile(provider_id, connection_id) == 52
    assert result["id"] == 52


def test_still_creates_normally_when_no_name_collision(db, monkeypatch):
    provider_id = db.upsert_provider("Brand New Provider", "http://example.com", "", "key", provider_type="jellyfin")
    connection_id = db.create_dispatcharr_connection("dispatch-test", "http://gluetun:9191", "token")
    db.update_dispatcharr_connection(connection_id, vod_relay_account_id=31)

    fake_client = _FakeClient(existing_profiles=[{"id": 52, "name": "Some Other Provider", "max_streams": 0}])
    monkeypatch.setattr(vod_sync, "DispatcharrClient", lambda url, token: fake_client)

    provider = db.get_provider(provider_id)
    connection = db.get_dispatcharr_connection(connection_id)
    result = asyncio.run(vod_sync._sync_provider_to_connection(provider, connection))

    assert len(fake_client.posted) == 1
    assert result["id"] == 999
