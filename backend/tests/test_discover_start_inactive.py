"""Discover imports create providers inactive by default (like the add-provider
form), so a full unfiltered import can't start before Exclude Categories is set."""

import vod_sync


def _connection(db):
    return db.create_dispatcharr_connection("disp", "http://dispatcharr.example", "token")


def _profile(name="Account A", account=1, profile=10):
    return {"name": name, "server_url": "http://xc.example", "username": "u", "password": "p",
            "max_streams": 2, "dispatcharr_account_id": account, "dispatcharr_profile_id": profile}


def _is_active(db, provider_id):
    return bool(db.get_provider(provider_id)["is_active"])


def test_new_discovered_provider_starts_inactive_when_asked(db):
    provider = vod_sync.import_discovered_profile(_connection(db), _profile(), start_inactive=True)
    assert not _is_active(db, provider["id"])


def test_already_imported_provider_keeps_its_state_on_reimport(db):
    connection = _connection(db)
    first = vod_sync.import_discovered_profile(connection, _profile(), start_inactive=False)
    assert _is_active(db, first["id"])
    again = vod_sync.import_discovered_profile(connection, _profile(), start_inactive=True)
    assert again["id"] == first["id"] and _is_active(db, first["id"])


def test_subaccount_group_creates_its_new_provider_inactive(db):
    result = vod_sync.import_discovered_profiles_as_subaccounts(
        _connection(db), [_profile("A", 1, 10), _profile("B", 2, 20)], new_provider_name="Group", start_inactive=True)
    assert not _is_active(db, result["provider"]["id"])
    assert result["sub_accounts_created"] == 1


def test_same_named_provider_that_already_exists_is_not_deactivated(db):
    existing = db.upsert_provider("Account A", "http://xc.example", "u", "p")
    provider = vod_sync.import_discovered_profile(_connection(db), _profile(), start_inactive=True)
    assert provider["id"] == existing and _is_active(db, existing)
