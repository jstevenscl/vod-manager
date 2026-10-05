import asyncio

import vod_sync


class _FakeClient:
    def __init__(self, existing_profiles):
        self.existing_profiles = existing_profiles
        self.posted = []
        self.patched = []

    async def get(self, path, params=None):
        return self.existing_profiles

    async def post(self, path, data):
        self.posted.append(data)
        return {"id": 999, **data}

    async def patch(self, path, data):
        self.patched.append(data)
        return {"id": int(path.rstrip("/").rsplit("/", 1)[-1]), **data}


def test_adopts_existing_same_name_profile_instead_of_posting(db, monkeypatch):
    provider_id = db.upsert_provider("Jellyfin Local Test", "http://jellyfin.example", "", "key", provider_type="jellyfin")
    connection_id = db.create_dispatcharr_connection("dispatch-test", "http://dispatcharr.example", "token")
    db.update_dispatcharr_connection(connection_id, vod_relay_account_id=31)
    fake_client = _FakeClient([{"id": 52, "name": "Jellyfin Local Test"}])
    monkeypatch.setattr(vod_sync, "DispatcharrClient", lambda url, token: fake_client)

    result = asyncio.run(vod_sync._sync_provider_to_connection(
        db.get_provider(provider_id), db.get_dispatcharr_connection(connection_id)
    ))

    assert fake_client.posted == []
    assert db.get_provider_sync_profile(provider_id, connection_id) == 52
    assert result["id"] == 52


def test_creates_profile_when_no_name_collision(db, monkeypatch):
    provider_id = db.upsert_provider("Brand New Provider", "http://provider.example", "", "key", provider_type="jellyfin")
    connection_id = db.create_dispatcharr_connection("dispatch-test", "http://dispatcharr.example", "token")
    db.update_dispatcharr_connection(connection_id, vod_relay_account_id=31)
    fake_client = _FakeClient([{"id": 52, "name": "Some Other Provider"}])
    monkeypatch.setattr(vod_sync, "DispatcharrClient", lambda url, token: fake_client)

    result = asyncio.run(vod_sync._sync_provider_to_connection(
        db.get_provider(provider_id), db.get_dispatcharr_connection(connection_id)
    ))

    assert len(fake_client.posted) == 1
    assert result["id"] == 999
