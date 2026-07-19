"""
Syncs each VOD provider's max_streams into a corresponding Dispatcharr M3U
profile, so Dispatcharr's own per-profile connection accounting (proven to
be real admission control, not cosmetic) is always enforcing the actual
limit we know about for that provider.
"""

import logging

from config import get_vod_xc_account_id
from dispatcharr_client import DispatcharrClient
import vod_db

logger = logging.getLogger(__name__)


class VodXcAccountNotConfigured(Exception):
    pass


async def sync_provider(provider_id: int) -> dict:
    account_id = get_vod_xc_account_id()
    if not account_id:
        raise VodXcAccountNotConfigured("vod_xc_account_id is not configured")

    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    client = DispatcharrClient()

    if provider["dispatcharr_profile_id"]:
        # Dispatcharr requires search_pattern on PATCH too for non-default
        # profiles ("This field is required for non-default profiles."),
        # not just at creation — send the full profile shape every time.
        profile = await client.patch(
            f"/api/m3u/accounts/{account_id}/profiles/{provider['dispatcharr_profile_id']}/",
            {
                "name": provider["name"],
                "max_streams": provider["max_streams"],
                "search_pattern": "^(.*)$",
                "replace_pattern": "$1",
            },
        )
        logger.info("[vod_sync] updated profile %s for provider %s max_streams=%s",
                    profile["id"], provider["name"], provider["max_streams"])
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
    vod_db.set_provider_dispatcharr_profile(provider_id, profile["id"])
    logger.info("[vod_sync] created profile %s for provider %s max_streams=%s",
                profile["id"], provider["name"], provider["max_streams"])
    return profile
