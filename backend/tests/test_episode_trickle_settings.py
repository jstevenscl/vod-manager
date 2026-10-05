"""Episode trickle pacing is user-tunable from Settings, within safe bounds."""

import config

_BASE = dict(
    catalog_refresh_seconds_xc=3600, catalog_refresh_seconds_plex=3600,
    catalog_refresh_seconds_emby=3600, catalog_refresh_seconds_jellyfin=3600,
    enrichment_ttl_seconds=3600, tmdb_sync_interval_seconds=None,
)


def test_default_trickle_pause_is_15_minutes(db):
    assert config.get_refresh_settings()["episode_trickle_interval_seconds"] == 15 * 60


def test_trickle_settings_are_saved(db):
    config.save_refresh_settings(**_BASE, episode_trickle_batch=150,
                                 episode_trickle_interval_seconds=1200,
                                 episode_trickle_spacing_seconds=5)
    s = config.get_refresh_settings()
    assert (s["episode_trickle_batch"], s["episode_trickle_interval_seconds"],
            s["episode_trickle_spacing_seconds"]) == (150, 1200, 5)


def test_trickle_settings_are_clamped_to_safe_bounds(db):
    config.save_refresh_settings(**_BASE, episode_trickle_batch=10_000,
                                 episode_trickle_interval_seconds=10,
                                 episode_trickle_spacing_seconds=0)
    s = config.get_refresh_settings()
    assert s["episode_trickle_batch"] == 500
    assert s["episode_trickle_interval_seconds"] == 300
    assert s["episode_trickle_spacing_seconds"] == 1


def test_zero_batch_turns_trickle_off(db):
    config.save_refresh_settings(**_BASE, episode_trickle_batch=0)
    assert config.get_refresh_settings()["episode_trickle_batch"] == 0


def test_omitted_trickle_settings_are_kept(db):
    config.save_refresh_settings(**_BASE, episode_trickle_batch=150)
    config.save_refresh_settings(**_BASE)
    assert config.get_refresh_settings()["episode_trickle_batch"] == 150
