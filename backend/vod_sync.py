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
import dispatcharr_dvr_client
import vod_db

logger = logging.getLogger(__name__)

_DEFAULT_ACCOUNT_MAX_STREAMS = 50
# Dispatcharr's own M3U account model defaults refresh_interval to 0 (hours)
# -- disabled -- and this call never included it, so every auto-created
# connection silently inherited that disabled default. Confirmed against
# Dispatcharr's actual source (apps/m3u/models.py, apps/m3u/signals.py):
# it's a plain hours value, directly scheduling a Celery refresh task at
# that cadence. Real gap found live 2026-07-30. 4 hours, not VOD Manager's
# own 6-hour default catalog-refresh cadence (Configuration -> Refresh
# Schedule) -- Dispatcharr pulling from VOD Manager somewhat more often
# than VOD Manager itself re-pulls from upstream providers is harmless (a
# no-op re-fetch when nothing changed) and keeps Dispatcharr from lagging
# behind whenever the pool does change, which matters more for DVR
# recordings appearing promptly than for the slower-moving general catalog.
_DEFAULT_ACCOUNT_REFRESH_INTERVAL_HOURS = 4


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
            # Account-level cap on how many concurrent streams Dispatcharr
            # will pull from VOD Manager through this account as a whole --
            # NOT the real per-provider limit (that's enforced separately,
            # per-provider, via the "profiles" _sync_provider_to_connection
            # below creates/updates). This just needs to be generous enough
            # to never be the practical bottleneck itself. Confirmed live
            # 2026-07-20: leaving this at 1 (the old value) meant a single
            # player's ordinary probe-then-play double-connect, or two
            # concurrent viewers, got hard-rejected with zero indication why
            # -- looked identical to a codec/client problem from the outside.
            "max_streams": _DEFAULT_ACCOUNT_MAX_STREAMS,
            "refresh_interval": _DEFAULT_ACCOUNT_REFRESH_INTERVAL_HOURS,
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


# ── Discovering real upstream accounts already configured in Dispatcharr ────
# Real user request 2026-07-31: the admin already told Dispatcharr about
# every real XC login when they set up live TV there -- no reason to make
# them retype the same username/password/base_url into a VOD Manager
# provider row by hand, or keep the two in sync manually if a login's
# password ever changes. Confirmed live 2026-07-31 against a real
# Dispatcharr instance: GET /api/m3u/accounts/ returns full plaintext
# username/password (not write-only), so this reads real credentials
# straight from Dispatcharr's own API using the same connection token
# already used everywhere else in this file.
#
# Real gap found live the same day, testing against real multi-login
# providers: an M3U "account" is not always one real login. Dispatcharr
# lets an admin represent several separate real upstream logins as
# distinct "profiles" under one account object, each with its own
# max_streams -- confirmed live, summing real profiles' max_streams
# reproduced the admin's actual known per-provider connection totals
# exactly. Each profile's search_pattern/replace_pattern is a plain
# "username/password" -> "username/password" regex swap applied to the
# account's own base credentials (confirmed live: a profile's
# replace_pattern tokens never equal the account's own base username/
# password when it's rewriting to a different login) -- except the
# always-present "Default" profile, whose pattern is the literal
# passthrough "^(.*)$" -> "$1" (no rewrite at all), meaning Default uses
# the account's own base credentials unchanged. So discovery has to work
# at the PROFILE level, not the account level: each profile (Default
# included) is its own real, independently-capped login candidate.

def _extract_profile_credentials(account: dict, profile: dict) -> tuple[str, str] | None:
    """The real username/password this one profile actually streams with.
    Default profile (or any pattern that's a no-op passthrough) uses the
    account's own base credentials unchanged. A real rewrite profile's
    replace_pattern is "username/password" -- split on the one slash. Any
    other shape is unparseable (some other, non-credential rewrite use of
    profiles this app hasn't seen) and this returns None rather than
    guessing wrong credentials that could break real streaming."""
    search_pattern = (profile.get("search_pattern") or "").strip()
    replace_pattern = (profile.get("replace_pattern") or "").strip()
    if search_pattern in ("^(.*)$", "") or replace_pattern in ("$1", ""):
        return (account.get("username") or "", account.get("password") or "")
    parts = replace_pattern.split("/")
    if len(parts) == 2 and all(parts):
        return (parts[0], parts[1])
    return None


async def list_discoverable_profiles(connection: dict) -> list[dict]:
    """Every real upstream XC login Dispatcharr already knows about on this
    connection, one entry per PROFILE (see module docstring above for why
    profile, not account) -- excluding the one Dispatcharr account this
    same connection's own vod_relay_account_id points at (that one is
    Dispatcharr pointing BACK at us, not a real upstream source) and any
    non-XC placeholder account (Dispatcharr's own "custom account" type has
    no real credentials -- confirmed live, username/password both empty).
    Each result carries whether it's already linked to a VOD Manager
    provider on this connection, so the picker can show that state without
    a second round trip. A profile whose rewrite pattern couldn't be parsed
    into credentials is still listed (so the admin can see it exists) but
    with credentials_unparseable=True and no username/password -- never
    silently guessed."""
    client = DispatcharrClient(connection["url"], connection["token"])
    data = await client.get("/api/m3u/accounts/")
    accounts = data if isinstance(data, list) else data.get("results", [])

    relay_account_id = connection.get("vod_relay_account_id")
    linked = {
        (link["dispatcharr_account_id"], link["dispatcharr_profile_id"])
        for link in vod_db.list_provider_live_accounts_for_connection(connection["id"])
    }

    candidates = []
    for a in accounts:
        if a["id"] == relay_account_id:
            continue
        if a.get("account_type") != "XC" or not a.get("username"):
            continue
        profiles = await dispatcharr_dvr_client.list_m3u_account_profiles(connection, a["id"])
        for p in profiles:
            if not p.get("is_active", True):
                continue
            creds = _extract_profile_credentials(a, p)
            candidates.append({
                "dispatcharr_account_id": a["id"],
                "dispatcharr_profile_id": p["id"],
                "name": f"{a['name']} ({p['name']})" if p["name"] != f"{a['name']} Default" else a["name"],
                "username": creds[0] if creds else None,
                "password": creds[1] if creds else None,
                "credentials_unparseable": creds is None,
                "server_url": a["server_url"],
                "max_streams": p.get("max_streams") or 0,
                "already_linked": (a["id"], p["id"]) in linked,
            })
    return candidates


def _find_provider_linked_to_profile(connection_id: int, account_id: int, profile_id: int) -> dict | None:
    for link in vod_db.list_provider_live_accounts_for_connection(connection_id):
        if link["dispatcharr_account_id"] == account_id and link["dispatcharr_profile_id"] == profile_id:
            return vod_db.get_provider(link["provider_id"])
    return None


def import_discovered_profile(connection_id: int, profile: dict) -> dict:
    """Creates (or refreshes, if this Dispatcharr profile was already
    imported before) one VOD Manager provider row from a discovered
    Dispatcharr profile. upsert_provider matches by name, so re-running
    this for a profile whose name hasn't changed on the Dispatcharr side
    just refreshes its credentials rather than creating a duplicate row.
    Preserves an already-imported provider's own priority rather than
    resetting it to the default -- an admin's manual adjustment there
    shouldn't get silently clobbered by a re-import. Refuses to import a
    profile whose credentials couldn't be parsed (see
    _extract_profile_credentials) rather than creating a provider with no
    real login."""
    if profile.get("credentials_unparseable") or not profile.get("username"):
        raise ValueError(f"couldn't determine real credentials for profile {profile['name']!r} -- not imported")
    existing = _find_provider_linked_to_profile(connection_id, profile["dispatcharr_account_id"], profile["dispatcharr_profile_id"])
    priority = existing["priority"] if existing else 0
    provider_id = vod_db.upsert_provider(
        name=profile["name"], base_url=profile["server_url"],
        username=profile["username"], password=profile["password"],
        max_streams=profile["max_streams"], priority=priority, provider_type="xc",
    )
    if profile["max_streams"]:
        vod_db.set_provider_shared_limit(provider_id, profile["max_streams"])
    vod_db.set_provider_live_account(provider_id, connection_id, profile["dispatcharr_account_id"], profile["dispatcharr_profile_id"])
    return vod_db.get_provider(provider_id)


def import_discovered_profiles_as_subaccounts(
    connection_id: int, profiles: list[dict], provider_id: int | None = None, new_provider_name: str | None = None,
) -> dict:
    """vod_manager-gd5: group N discovered Dispatcharr profiles into one
    provider's sub-accounts, instead of import_discovered_profile's one-row-
    per-profile default -- the Discover-flow equivalent of
    merge_providers_into_subaccounts (vod_manager-q78), but for profiles
    that were never separately imported as providers at all.

    Give exactly one of:
    - provider_id: an existing provider. Every usable profile becomes a new
      sub-account under it (its own top-level credentials are untouched).
    - new_provider_name: creates a new provider. The FIRST usable profile
      becomes that provider's own top-level login + live-account link
      (same as import_discovered_profile for a single profile); every
      remaining profile becomes a sub-account.

    Skips (not raises) any profile whose credentials couldn't be parsed,
    same as import_discovered_profile -- reported back in "skipped" rather
    than aborting the whole group."""
    if (provider_id is None) == (new_provider_name is None):
        raise ValueError("give exactly one of provider_id or new_provider_name")

    usable, skipped = [], []
    for profile in profiles:
        if profile.get("credentials_unparseable") or not profile.get("username"):
            skipped.append({"name": profile["name"], "reason": "couldn't determine real credentials"})
        else:
            usable.append(profile)
    if not usable:
        return {"provider": None, "sub_accounts_created": 0, "skipped": skipped}

    sub_account_profiles = usable
    if provider_id is not None:
        provider = vod_db.get_provider(provider_id)
        if not provider:
            raise ValueError(f"provider {provider_id} not found")
    else:
        first = usable[0]
        pid = vod_db.upsert_provider(
            name=new_provider_name, base_url=first["server_url"],
            username=first["username"], password=first["password"],
            max_streams=first["max_streams"], provider_type="xc",
        )
        if first["max_streams"]:
            vod_db.set_provider_shared_limit(pid, first["max_streams"])
        vod_db.set_provider_live_account(pid, connection_id, first["dispatcharr_account_id"], first["dispatcharr_profile_id"])
        provider = vod_db.get_provider(pid)
        sub_account_profiles = usable[1:]

    sub_accounts_created = 0
    for i, profile in enumerate(sub_account_profiles):
        sub_id = vod_db.create_provider_sub_account(
            provider["id"], profile["name"], profile["username"], profile["password"],
            max_streams=profile["max_streams"], sort_order=i,
        )
        vod_db.set_provider_sub_account_live_account(
            sub_id, connection_id, profile["dispatcharr_account_id"], profile.get("dispatcharr_profile_id"),
        )
        sub_accounts_created += 1

    return {"provider": vod_db.get_provider(provider["id"]), "sub_accounts_created": sub_accounts_created, "skipped": skipped}


async def recheck_discovered_credentials(connection: dict) -> dict:
    """Re-fetches every already-imported profile on this connection and
    updates VOD Manager's stored username/password if Dispatcharr's own
    values have since changed -- the actual "credentials rotate in
    Dispatcharr, VOD Manager picks it up" half of discovery, not just the
    one-time import. Only ever touches credentials (and base_url, which
    comes from the same Dispatcharr account); leaves every other admin-set
    field (priority, shared limit override, custom user-agent, category
    exclusions, max_streams, etc.) alone -- max_streams drift on the
    Dispatcharr side is a separate, deliberate re-import, not picked up
    silently by a credentials-only recheck."""
    profiles = await list_discoverable_profiles(connection)
    changed = []
    unchanged = 0
    for profile in profiles:
        if profile.get("credentials_unparseable"):
            continue
        provider = _find_provider_linked_to_profile(connection["id"], profile["dispatcharr_account_id"], profile["dispatcharr_profile_id"])
        if not provider:
            continue
        if provider["username"] != profile["username"] or provider["password"] != profile["password"]:
            vod_db.upsert_provider(
                name=provider["name"], base_url=profile["server_url"],
                username=profile["username"], password=profile["password"],
                max_streams=provider["max_streams"], priority=provider["priority"],
                provider_type=provider["provider_type"],
            )
            changed.append(provider["name"])
        else:
            unchanged += 1
    return {"changed": changed, "unchanged_count": unchanged}


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
