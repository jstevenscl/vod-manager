"""
Syncs each VOD provider's max_streams into a corresponding Dispatcharr M3U
profile, so Dispatcharr's own per-profile connection accounting (proven to
be real admission control, not cosmetic) is always enforcing the actual
limit we know about for that provider -- on every connected Dispatcharr
instance that has a VOD-relay account configured, not just one. Each
instance gets its own separate profile object (tracked per-connection in
vod_db.provider_sync_profiles), since profile ids aren't shared across
separate Dispatcharr databases.
"""

import logging

from dispatcharr_client import DispatcharrClient
import vod_db

logger = logging.getLogger(__name__)


class VodXcAccountNotConfigured(Exception):
    pass


async def connect_dispatcharr_instance(label: str, url: str, token: str, vod_manager_public_url: str) -> dict:
    """One-shot automated setup for a new Dispatcharr instance: generates it
    its own XC client credentials, calls that instance's own API (using the
    admin token given here) to create an M3U account pointed back at VOD
    Manager, and saves the resulting connection -- the same steps that
    otherwise mean manually creating a client, then manually creating and
    wiring up the Dispatcharr-side account by hand. What's left afterward is
    purely Dispatcharr-side (enabling VOD on the new account, picking which
    groups/categories to turn on) -- normal setup for any source, same as
    it'd be no matter how the account was created, not something VOD
    Manager could do on its behalf.

    Rolls back the XC client it created if the Dispatcharr-side call fails,
    so a bad token/URL doesn't leave an orphaned, never-used client behind."""
    client_record = vod_db.create_xc_client(f"{label} (auto)")
    try:
        dispatcharr = DispatcharrClient(url, token)
        account = await dispatcharr.post("/api/m3u/accounts/", {
            "name": f"VOD Manager ({label})",
            "server_url": vod_manager_public_url.rstrip("/"),
            "account_type": "XC",
            "username": client_record["username"],
            "password": client_record["password"],
            "is_active": True,
            "max_streams": 1,
        })
    except Exception:
        vod_db.delete_xc_client(client_record["id"])
        raise

    connection_id = vod_db.create_dispatcharr_connection(label, url, token)
    vod_db.update_dispatcharr_connection(connection_id, vod_relay_account_id=account["id"])
    logger.info("[vod_sync] connected new instance label=%s dispatcharr_account_id=%s xc_client=%s",
                label, account["id"], client_record["username"])
    return {
        "connection": vod_db.get_dispatcharr_connection(connection_id),
        "xc_client": client_record,
        "dispatcharr_account": account,
    }


async def _sync_provider_to_connection(provider: dict, connection: dict) -> dict:
    account_id = connection["vod_relay_account_id"]
    client = DispatcharrClient(connection["url"], connection["token"])
    existing_profile_id = vod_db.get_provider_sync_profile(provider["id"], connection["id"])

    if existing_profile_id:
        # Dispatcharr requires search_pattern on PATCH too for non-default
        # profiles ("This field is required for non-default profiles."),
        # not just at creation — send the full profile shape every time.
        profile = await client.patch(
            f"/api/m3u/accounts/{account_id}/profiles/{existing_profile_id}/",
            {
                "name": provider["name"],
                "max_streams": provider["max_streams"],
                "search_pattern": "^(.*)$",
                "replace_pattern": "$1",
            },
        )
        logger.info("[vod_sync] connection=%s: updated profile %s for provider %s max_streams=%s",
                    connection["label"], profile["id"], provider["name"], provider["max_streams"])
        return profile

    profile = await client.post(
        f"/api/m3u/accounts/{account_id}/profiles/",
        {
            "name": provider["name"],
            "max_streams": provider["max_streams"],
            "is_active": True,
            "search_pattern": "^(.*)$",
            "replace_pattern": "$1",
        },
    )
    vod_db.set_provider_sync_profile(provider["id"], connection["id"], profile["id"])
    logger.info("[vod_sync] connection=%s: created profile %s for provider %s max_streams=%s",
                connection["label"], profile["id"], provider["name"], provider["max_streams"])
    return profile


async def sync_provider(provider_id: int) -> dict:
    """Pushes to every connection with a vod_relay_account_id configured.
    Returns per-connection results so a caller can surface a partial
    failure (one instance down) without losing the others that succeeded."""
    connections = [c for c in vod_db.list_dispatcharr_connections() if c.get("vod_relay_account_id")]
    if not connections:
        raise VodXcAccountNotConfigured("no Dispatcharr connection has a VOD-relay account configured")

    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    results = {}
    for connection in connections:
        try:
            results[connection["label"]] = await _sync_provider_to_connection(provider, connection)
        except Exception as exc:
            logger.warning("[vod_sync] connection=%s: sync failed for provider %s: %s",
                            connection["label"], provider["name"], exc)
            results[connection["label"]] = {"error": str(exc)}
    return results
