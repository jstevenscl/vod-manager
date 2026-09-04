"""
VOD pool database — canonical Movies/TV Shows library, provider sources, and
smart category placements.

Design: content lives in a flat pool (movies, series/episodes), deduped from
whichever real providers offer it. Categories are our own curated/smart
labels, not provider-supplied ones. A single pool item can be placed into
multiple categories; since Dispatcharr's XC ingestion collapses same-account
entries that share the same (name, year), each placement beyond the first is
exported with an invisible zero-width-space marker appended to the name so
it lands as its own distinct catalog entry in Dispatcharr while still
resolving back to the same real provider source.
"""

from contextlib import contextmanager
import datetime
import logging
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from config import DATA_DIR, get_config, get_refresh_settings, get_stream_priority_mode, get_vod_xc_account_id
from secrets_util import decrypt_value, encrypt_value, is_encrypted

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "vod_db.sqlite"

# Offset export stream_ids well clear of any real provider's own ID range.
_EXPORT_STREAM_ID_BASE = 900_000_000
_SERIES_EXPORT_BASE = 910_000_000
_EPISODE_EXPORT_BASE = 920_000_000
_ZW_MARKER = "​"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # WAL mode lets readers/writers proceed without blocking each other —
    # needed once something (e.g. a Plex library import) writes a real batch
    # while the background enrichment scheduler is also writing continuously;
    # under the default rollback-journal mode that contention raised "database
    # is locked". timeout=30 gives any remaining brief contention room to
    # retry instead of failing immediately.
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            max_streams INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            dispatcharr_profile_id INTEGER,
            dispatcharr_live_account_id INTEGER,
            shared_connection_limit INTEGER,
            provider_type TEXT NOT NULL DEFAULT 'xc',
            auto_create_categories INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            genre TEXT,
            description TEXT,
            duration_secs INTEGER,
            poster_url TEXT,
            cast_list TEXT,
            director TEXT,
            country TEXT,
            rating TEXT,
            release_date TEXT,
            is_adult INTEGER NOT NULL DEFAULT 0,
            is_adult_manual INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_enriched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS movie_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            provider_stream_id TEXT NOT NULL,
            container_extension TEXT NOT NULL DEFAULT 'mp4',
            provider_category_name TEXT,
            plex_rating_key TEXT,
            bitrate INTEGER,
            raw_name TEXT,
            added_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(provider_id, provider_stream_id)
        );

        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            genre TEXT,
            description TEXT,
            poster_url TEXT,
            cast_list TEXT,
            director TEXT,
            country TEXT,
            rating TEXT,
            release_date TEXT,
            is_adult INTEGER NOT NULL DEFAULT 0,
            is_adult_manual INTEGER NOT NULL DEFAULT 0,
            import_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
            import_provider_series_id TEXT,
            provider_category_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_enriched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            duration_secs INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episode_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            provider_stream_id TEXT NOT NULL,
            container_extension TEXT NOT NULL DEFAULT 'mp4',
            provider_category_name TEXT,
            plex_rating_key TEXT,
            bitrate INTEGER,
            raw_name TEXT,
            added_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(provider_id, provider_stream_id)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'series')),
            is_smart INTEGER NOT NULL DEFAULT 0,
            rule_json TEXT,
            sync_source TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(name, content_type)
        );

        CREATE TABLE IF NOT EXISTS metadata_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'series', 'both')),
            field TEXT NOT NULL,
            pattern TEXT NOT NULL,
            replacement TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movie_category_placements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            export_stream_id INTEGER NOT NULL UNIQUE,
            name_suffix TEXT NOT NULL DEFAULT '',
            UNIQUE(movie_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS series_category_placements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            export_series_id INTEGER NOT NULL UNIQUE,
            name_suffix TEXT NOT NULL DEFAULT '',
            UNIQUE(series_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS xc_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            ip_allowlist TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            last_seen_ip TEXT
        );

        CREATE TABLE IF NOT EXISTS dispatcharr_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            token TEXT NOT NULL,
            vod_relay_account_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_live_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_connection_id INTEGER NOT NULL REFERENCES dispatcharr_connections(id) ON DELETE CASCADE,
            dispatcharr_account_id INTEGER NOT NULL,
            dispatcharr_profile_id INTEGER,
            UNIQUE(provider_id, dispatcharr_connection_id)
        );

        -- Native multi-account-per-provider support (vod_manager-4dh): one
        -- provider entry can hold several separate real upstream logins
        -- (Dispatcharr calls these M3U "profiles" under one account -- a
        -- provider selling "5x1" single-connection accounts, for example),
        -- shown/managed as one thing instead of the old workaround of
        -- manually splitting into N separate provider rows. XC-only --
        -- Plex/Emby/Jellyfin have no equivalent multi-login concept.
        -- Deliberately NO aggregate/summed limit anywhere in this table or
        -- providers.shared_connection_limit when sub-accounts exist --
        -- confirmed live by reading Dispatcharr's own source
        -- (apps/channels/models.py Channel.get_stream(),
        -- apps/m3u/connection_pool.py) that IT tracks capacity
        -- independently per profile with automatic failover to the next
        -- profile when one is full, and never has an account-level total.
        -- xc_server's capacity/credential-selection logic mirrors that
        -- exactly: try each active sub-account in sort_order, first one
        -- with a free max_streams slot wins.
        CREATE TABLE IF NOT EXISTS provider_sub_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            max_streams INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        -- Sub-account equivalent of provider_live_accounts -- a separate
        -- table rather than adding a nullable sub_account_id column to
        -- provider_live_accounts and reworking its UNIQUE constraint,
        -- since SQLite can't alter an existing constraint in place without
        -- a full table rebuild (see _migrate_category_name_uniqueness for
        -- what that costs) and this table is already in real use.
        CREATE TABLE IF NOT EXISTS provider_sub_account_live_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_account_id INTEGER NOT NULL REFERENCES provider_sub_accounts(id) ON DELETE CASCADE,
            dispatcharr_connection_id INTEGER NOT NULL REFERENCES dispatcharr_connections(id) ON DELETE CASCADE,
            dispatcharr_account_id INTEGER NOT NULL,
            dispatcharr_profile_id INTEGER,
            UNIQUE(sub_account_id, dispatcharr_connection_id)
        );

        CREATE TABLE IF NOT EXISTS provider_sync_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_connection_id INTEGER NOT NULL REFERENCES dispatcharr_connections(id) ON DELETE CASCADE,
            dispatcharr_profile_id INTEGER NOT NULL,
            UNIQUE(provider_id, dispatcharr_connection_id)
        );

        CREATE TABLE IF NOT EXISTS duplicate_ignores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT NOT NULL,
            signature TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(content_type, signature)
        );

        -- Phase 2 DVR scheduling. channel_id is required for a new profile
        -- (see vod_routes.create_recording_profile) -- scheduling goes
        -- through dispatcharr_dvr_client.schedule_channel_recordings
        -- (channel-scoped EPG search + a direct Recording per airing), not
        -- Dispatcharr's own Series Rules feature, which is confirmed broken
        -- for channel-scoped matching (dispatch-test v0.27.2 and v0.28.2,
        -- 2026-07-26). tvg_id is still stored -- unused for scheduling now,
        -- but still the key match_recording_profiles uses to route a
        -- completed recording back to the profile(s) that scheduled it.
        CREATE TABLE IF NOT EXISTS dvr_recording_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            tvg_id TEXT,
            title TEXT NOT NULL,
            title_mode TEXT NOT NULL DEFAULT 'exact',
            description TEXT,
            description_mode TEXT NOT NULL DEFAULT 'contains',
            mode TEXT NOT NULL DEFAULT 'all',
            channel_id INTEGER,
            target_movie_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            target_series_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );

        -- Who has a given DVR-recorded item in their own portal Library --
        -- many-to-many, deliberately separate from movie_sources.
        -- recording_profile_id/dispatcharr_user_id (which stay as informal
        -- "who/what originally created this source" info, unchanged).
        -- Needed because a single physical recording can legitimately be
        -- shared: two people can each have their own recording profile
        -- matching the very same airing (match_recording_profiles' own
        -- docstring already documented this "fan-out" for category
        -- placement), or one person can schedule something another person
        -- already has. Real requirement from the user, 2026-07-28: if Bill
        -- schedules something Emby already recorded, Bill should see it in
        -- his own Library too (attached to the SAME file, not a duplicate
        -- recording) -- and if Emby then removes it from her Library, it
        -- must disappear from HER Library only, not Bill's, and the
        -- underlying file must only actually be deleted from disk once
        -- EVERY owner has removed it (see remove_movie_library_owner /
        -- remove_episode_library_owner). ON DELETE CASCADE so deleting the
        -- source row itself (e.g. the admin's own hard delete) cleans these
        -- up automatically rather than leaving orphans.
        CREATE TABLE IF NOT EXISTS movie_source_owners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_source_id INTEGER NOT NULL REFERENCES movie_sources(id) ON DELETE CASCADE,
            dispatcharr_user_id INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(movie_source_id, dispatcharr_user_id)
        );

        CREATE TABLE IF NOT EXISTS episode_source_owners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_source_id INTEGER NOT NULL REFERENCES episode_sources(id) ON DELETE CASCADE,
            dispatcharr_user_id INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(episode_source_id, dispatcharr_user_id)
        );

        -- Closes the one remaining gap in the shared-ownership model: Bill
        -- scheduling something Emby's recording of is STILL IN PROGRESS (no
        -- movie_sources/episode_sources row exists yet to attach him to --
        -- attach_portal_user_to_existing_recording can only work once one
        -- does). Rather than PATCHing Dispatcharr's own Recording object
        -- (untested against a live instance, real risk of malforming
        -- someone's actual in-progress recording), this is purely local:
        -- portal_schedule_single stores a claim keyed by the same
        -- (provider, channel, identity) triple find_existing_recording
        -- already uses to find the match in the first place, and
        -- dispatcharr_dvr_importer consumes (reads + deletes) any matching
        -- claims the moment that exact recording actually imports, adding
        -- each claimant as an owner alongside whoever the normal
        -- attribution pass already found. Real requirement from the user,
        -- 2026-07-28. identity_key is dispatcharr_dvr_client.
        -- episode_identity_key's own output -- see its docstring for what
        -- it folds in (season/episode, or onscreen_episode, or sub_title,
        -- or failing all of those the airing's own start/end time).
        CREATE TABLE IF NOT EXISTS pending_recording_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            channel_id INTEGER NOT NULL,
            identity_key TEXT NOT NULL,
            dispatcharr_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        -- Dedup for notifications.notify_quota_threshold -- one row per
        -- (person, threshold) they've already been warned about, so the
        -- importer's per-pass quota check doesn't re-email them every
        -- single import cycle. dispatcharr_dvr_importer clears a person's
        -- rows for any threshold their CURRENT usage no longer meets before
        -- checking for new ones, so dropping back under 90% and crossing it
        -- again later re-fires that warning -- this only suppresses
        -- re-sending while they're continuously still at/above a threshold
        -- already warned about.
        CREATE TABLE IF NOT EXISTS quota_warnings_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_user_id INTEGER NOT NULL,
            threshold_pct INTEGER NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(provider_id, dispatcharr_user_id, threshold_pct)
        );

        -- Per-person DVR resource limits, one row per (DVR provider, real
        -- Dispatcharr login user) -- both the stream-concurrency reserve and
        -- the disk quota live together since they're conceptually "this
        -- person's DVR allowance," confirmed 2026-07-26 that Dispatcharr's
        -- own real login Users (apps/accounts/models.py, User.stream_limit)
        -- are never checked for DVR recordings at all (only for authenticated
        -- live/VOD viewing sessions) -- so this is VOD Manager's own,
        -- necessarily predictive, best-effort enforcement, not something
        -- Dispatcharr does for us. dispatcharr_user_id is intentionally not a
        -- local FK -- the person themselves lives in Dispatcharr, VOD Manager
        -- only tracks the limit assigned to them. Opt-in: a person with no
        -- row here has no DVR limit enforced at all.
        CREATE TABLE IF NOT EXISTS dvr_user_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_user_id INTEGER NOT NULL,
            dispatcharr_username TEXT NOT NULL,
            stream_reserve INTEGER NOT NULL DEFAULT 0,
            disk_quota_bytes INTEGER,
            retention_max_age_days INTEGER,
            retention_max_episodes_per_show INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(provider_id, dispatcharr_user_id)
        );

        -- Turns Dispatcharr's real-time-only VOD connection stats (GET
        -- /proxy/stats/, confirmed live 2026-07-27 it carries a real
        -- per-person user_id on every active VOD connection, but
        -- Dispatcharr itself never persists it once the connection ends)
        -- into VOD Manager's own history -- see dispatcharr_dvr_importer.
        -- poll_watch_sessions, which upserts/closes these rows every poll.
        -- client_id is Dispatcharr's own per-connection identifier
        -- (confirmed live it's timestamp-prefixed, e.g.
        -- "vod_1785120140964_6853"), reused as the session key here rather
        -- than inventing a new one. Not DVR-specific -- covers any VOD
        -- content served through a connection's relay, DVR-recorded or
        -- not. dispatcharr_user_id is intentionally not a local FK, same
        -- reasoning as dvr_user_limits above.
        CREATE TABLE IF NOT EXISTS watch_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatcharr_connection_id INTEGER NOT NULL REFERENCES dispatcharr_connections(id) ON DELETE CASCADE,
            client_id TEXT NOT NULL,
            dispatcharr_user_id INTEGER,
            dispatcharr_username TEXT,
            content_type TEXT,
            content_name TEXT,
            content_uuid TEXT,
            client_ip TEXT,
            bytes_sent INTEGER NOT NULL DEFAULT 0,
            position_seconds REAL,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_at TEXT,
            UNIQUE(dispatcharr_connection_id, client_id)
        );

        -- The DVR Library's missing-episode view's "not found anywhere"
        -- outcome: a canonical (TMDB) episode that's neither already in the
        -- pool (find_pool_backfill_match) nor findable via an unscoped EPG
        -- title search. Per the user's own explicit request, 2026-07-27:
        -- "post it somewhere an admin can see on the backend" instead of
        -- the attempt silently vanishing -- an admin can then go look for
        -- it manually (Plex/Emby/Jellyfin, a different EPG source, etc.).
        -- One row per (series, season, episode); re-checking an already-
        -- flagged episode just refreshes checked_at rather than
        -- duplicating the row.
        CREATE TABLE IF NOT EXISTS dvr_unresolved_missing_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            episode_name TEXT,
            checked_at TEXT NOT NULL,
            UNIQUE(series_id, season_number, episode_number)
        );

        -- A DVR recording Dispatcharr scheduled, actually attempted, and
        -- genuinely failed (see dispatcharr_dvr_client.list_failed_recordings'
        -- docstring for the exact failure signature) -- Dispatcharr itself
        -- never retries these, so dispatcharr_dvr_importer.
        -- reschedule_failed_recordings is what looks for the same episode's
        -- next airing on any channel and schedules that instead. One row per
        -- (provider, Dispatcharr recording id): outcome 'unresolved' is
        -- re-attempted every poll cycle (a new EPG entry can enter the 7-day
        -- search horizon as time passes, same reason rescan_recording_profiles
        -- itself re-runs every cycle); outcome 'rescheduled' is done and never
        -- touched again. dispatcharr_recording_id is intentionally not a
        -- local FK -- the Recording lives in Dispatcharr, not here.
        CREATE TABLE IF NOT EXISTS dvr_recording_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_recording_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            season_number INTEGER,
            episode_number INTEGER,
            original_channel_id INTEGER,
            interrupted_reason TEXT,
            outcome TEXT NOT NULL,
            replacement_channel_id INTEGER,
            detected_at TEXT NOT NULL,
            UNIQUE(provider_id, dispatcharr_recording_id)
        );

        -- End-user self-service DVR portal login (backend/portal_auth.py,
        -- backend/portal_routes.py) -- deliberately a separate credential
        -- system from both the admin login (config.py's auth_username/
        -- auth_hash) and Dispatcharr's own login, so a portal account being
        -- compromised never exposes admin access or a real Dispatcharr
        -- password. One row per (DVR provider, real Dispatcharr login
        -- user); admin-provisioned only, no self-registration.
        -- dispatcharr_user_id is intentionally not a local FK, same
        -- reasoning as dvr_user_limits below -- the person lives in
        -- Dispatcharr. TOTP MFA is mandatory: totp_secret stays NULL
        -- (totp_enabled 0) until the account's first login completes
        -- enrollment; totp_secret is Fernet-encrypted at rest via
        -- secrets_util, same as every other stored secret in this app.
        CREATE TABLE IF NOT EXISTS portal_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_user_id INTEGER NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            totp_last_counter INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(provider_id, dispatcharr_user_id)
        );

        -- Real user request 2026-07-31: a failed playback attempt (every
        -- source exhausted, or a mid-stream relay crash) only ever showed up
        -- in the raw application log -- no way to notice "this user's
        -- stream just failed" without tailing logs. attempts is a JSON list
        -- of {provider, error} for every source tried, not just the last
        -- one, so an admin can tell "every provider is down" from "only
        -- this one flaky provider keeps failing." Deliberately no FK to
        -- movies/series -- a failure for something since renamed/deleted
        -- should still show what it was at the time, not vanish or point at
        -- the wrong row. Pruned to the most recent 500 rows on insert (see
        -- log_stream_failure) -- diagnostic history, not permanent record.
        CREATE TABLE IF NOT EXISTS vod_stream_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            username TEXT,
            attempts TEXT NOT NULL,
            final_reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_movies_name_year ON movies(name, year);
        CREATE INDEX IF NOT EXISTS idx_series_name_year ON series(name, year);
        CREATE INDEX IF NOT EXISTS idx_episodes_series_season_ep ON episodes(series_id, season_number, episode_number);
        -- SQLite never auto-indexes a foreign key column (only the referenced
        -- side's PRIMARY KEY gets one) -- movie_id/episode_id had no index of
        -- their own, only the unrelated (provider_id, provider_stream_id)
        -- UNIQUE constraint. Every _purge_if_sourceless_movie/episode call
        -- (delete_provider's cleanup loop runs one per affected item -- up to
        -- one per movie/episode a deleted provider ever touched) was a full
        -- table scan of movie_sources/episode_sources without these, turning
        -- a provider with a large catalog into a synchronous, unlogged,
        -- event-loop-blocking O(n*m) scan that looked like a hang.
        CREATE INDEX IF NOT EXISTS idx_movie_sources_movie_id ON movie_sources(movie_id);
        CREATE INDEX IF NOT EXISTS idx_episode_sources_episode_id ON episode_sources(episode_id);
        CREATE INDEX IF NOT EXISTS idx_dvr_recording_profiles_provider_id ON dvr_recording_profiles(provider_id);
        CREATE INDEX IF NOT EXISTS idx_dvr_user_limits_provider_id ON dvr_user_limits(provider_id);
        CREATE INDEX IF NOT EXISTS idx_watch_sessions_connection_open ON watch_sessions(dispatcharr_connection_id, ended_at);
        CREATE INDEX IF NOT EXISTS idx_watch_sessions_user ON watch_sessions(dispatcharr_user_id);
        CREATE INDEX IF NOT EXISTS idx_portal_accounts_provider_id ON portal_accounts(provider_id);
        CREATE INDEX IF NOT EXISTS idx_dvr_recording_failures_provider_id ON dvr_recording_failures(provider_id);
        CREATE INDEX IF NOT EXISTS idx_vod_stream_failures_created_at ON vod_stream_failures(created_at);
        CREATE INDEX IF NOT EXISTS idx_provider_sub_accounts_provider_id ON provider_sub_accounts(provider_id);
        CREATE INDEX IF NOT EXISTS idx_provider_sub_account_live_accounts_sub_account_id ON provider_sub_account_live_accounts(sub_account_id);
        CREATE INDEX IF NOT EXISTS idx_movie_source_owners_source_id ON movie_source_owners(movie_source_id);
        CREATE INDEX IF NOT EXISTS idx_movie_source_owners_user_id ON movie_source_owners(dispatcharr_user_id);
        CREATE INDEX IF NOT EXISTS idx_episode_source_owners_source_id ON episode_source_owners(episode_source_id);
        CREATE INDEX IF NOT EXISTS idx_episode_source_owners_user_id ON episode_source_owners(dispatcharr_user_id);
        CREATE INDEX IF NOT EXISTS idx_pending_recording_claims_lookup
            ON pending_recording_claims(provider_id, channel_id, identity_key);
        CREATE INDEX IF NOT EXISTS idx_quota_warnings_sent_lookup
            ON quota_warnings_sent(provider_id, dispatcharr_user_id);
    """)
    _commit_with_retry(conn)
    _migrate(conn)
    _migrate_category_name_uniqueness(conn)
    _backfill_source_owners(conn)
    # dispatcharr_connection_id only exists on `providers` from here on (it's
    # an ALTER TABLE in _migrate, not a base column -- providers predates
    # DVR support) -- this index has to wait until after that call on a
    # genuinely fresh database, not live in the executescript above with the
    # rest of the schema.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_providers_one_dvr_per_connection
            ON providers(dispatcharr_connection_id) WHERE provider_type='dispatcharr_dvr'
    """)
    # Same reasoning as the index above -- recording_profile_id/
    # dispatcharr_user_id are ALSO ALTER-added migration columns on
    # movie_sources/episode_sources, not base columns, so these have to
    # wait until after _migrate() too. Real bug found live 2026-07-29: these
    # 4 lines originally sat in the executescript block above (alongside
    # base-column indexes), which crashed init_db() outright on any
    # genuinely fresh database with "no such column: recording_profile_id"
    # -- never caught earlier because every test this session ran against a
    # copy of the already-migrated live DB, never a truly fresh install.
    # Without these, _backfill_source_owners' filtered scans/joins over
    # movie_sources/episode_sources (which can genuinely run into the
    # millions of rows across every provider, not just DVR -- confirmed
    # live 2026-07-28: 1.89M episode_sources rows, mostly ordinary catalog
    # content with these columns always NULL) degrade to full table scans
    # -- measured live at ~30s PER query on a real DB this size.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_movie_sources_recording_profile_id ON movie_sources(recording_profile_id);
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_movie_sources_dispatcharr_user_id ON movie_sources(dispatcharr_user_id);
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_episode_sources_recording_profile_id ON episode_sources(recording_profile_id);
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_episode_sources_dispatcharr_user_id ON episode_sources(dispatcharr_user_id);
    """)
    _commit_with_retry(conn)
    _migrate_primary_dispatcharr_connection(conn)
    _migrate_encrypt_plaintext_credentials(conn)
    _migrate_legacy_catchall_categories(conn)
    _seed_default_categories(conn)
    conn.close()


def _migrate_category_name_uniqueness(conn: sqlite3.Connection) -> None:
    """categories.name was originally a single-column UNIQUE constraint,
    which blocks a name like "Kids" or "Documentary" from ever existing as
    both a movie category and a series category at once -- a real
    limitation hit live 2026-07-29: Pink's provider auto-create-categories
    created "Kids" (movie) fine, then hit IntegrityError trying to create
    "Kids" (series), even though every application-level lookup
    (upsert_category, get_category_by_name) already scopes correctly by
    (name, content_type) and expected this to work. SQLite can't ALTER a
    UNIQUE constraint in place, so this recreates the table -- the first
    table-recreation migration in this codebase; every other migration so
    far (see _migrate above) is a plain ALTER TABLE ADD COLUMN, which can
    only add columns, not loosen an existing constraint. Runs immediately
    after _migrate(conn) so every categories.* column _migrate can add
    already exists by the time this copies the table -- and before
    _migrate_legacy_catchall_categories/_seed_default_categories, which
    both write to categories and should see the fixed schema."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='categories'").fetchone()
    if not row or "UNIQUE(name, content_type)" in (row["sql"] or ""):
        return  # fresh DB already has the new schema, or already migrated
    conn.commit()  # PRAGMA foreign_keys is a no-op inside a pending transaction
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript("""
        BEGIN;
        CREATE TABLE categories_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'series')),
            is_smart INTEGER NOT NULL DEFAULT 0,
            rule_json TEXT,
            sync_source TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            ai_description TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            schedule_start_mmdd TEXT,
            schedule_end_mmdd TEXT,
            schedule_interval_seconds INTEGER,
            last_evaluated_at TEXT,
            use_ai_evaluation INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name, content_type)
        );
        INSERT INTO categories_new (id, name, content_type, is_smart, rule_json, sync_source, sort_order,
                                     created_at, ai_description, is_active, schedule_start_mmdd, schedule_end_mmdd,
                                     schedule_interval_seconds, last_evaluated_at, use_ai_evaluation)
        SELECT id, name, content_type, is_smart, rule_json, sync_source, sort_order,
               created_at, ai_description, is_active, schedule_start_mmdd, schedule_end_mmdd,
               schedule_interval_seconds, last_evaluated_at, use_ai_evaluation
        FROM categories;
        DROP TABLE categories;
        ALTER TABLE categories_new RENAME TO categories;
        COMMIT;
    """)
    fk_check = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys=ON")
    if fk_check:
        raise RuntimeError(f"category name-uniqueness migration left dangling foreign key rows: {[dict(r) for r in fk_check]}")
    logger.info("[vod_db] migrated categories.name to a (name, content_type) composite unique constraint")


def _migrate_legacy_catchall_categories(conn: sqlite3.Connection) -> None:
    """One-time (but idempotent -- safe every startup): rewrites any smart
    category whose rule_json is functionally match-everything but predates
    the match_all convention -- a single condition {"field": "name", "op":
    "contains", "value": ""} always matches, since every string contains the
    empty string -- into the real match_all shape.

    Found live 2026-07-29 against a real production DB: its own "All
    Movies"/"All TV Shows" rows were created by an older version using
    exactly this pattern, before match_all existed. Two real consequences
    of leaving it unmigrated: (1) list_catchall_category_ids -- which the
    periodic catch-all sweep (vod_importer.refresh_catchall_categories) uses
    to find what to auto re-evaluate -- only ever recognizes match_all, so
    this category silently never got automatically kept current, only ever
    updating on a manual "Evaluate rule now" click; this is very likely a
    real contributor to "items aren't making it into All Movies" reports,
    not just the seeding regression _seed_default_categories' own docstring
    covers. (2) _seed_default_categories' match_all-only check (before this
    migration existed) didn't recognize this row as an existing catch-all
    either, and tried to INSERT a second category with the same name --
    reproduced live as a categories.name UNIQUE constraint crash that took
    the whole app down on startup. Migrating the existing row in place
    (same id, same placements, nothing re-imported) fixes both at the root
    instead of teaching every downstream check about this one legacy shape.

    exclude_adult is deliberately left OFF: the legacy rule matched
    literally everything including adult content, and this migration must
    not silently remove already-visible items from an admin's export --
    changing what's included is a decision for the admin, not a migration."""
    import json
    rows = conn.execute("SELECT id, rule_json FROM categories WHERE is_smart=1 AND rule_json IS NOT NULL").fetchall()
    for r in rows:
        try:
            rule = json.loads(r["rule_json"])
        except (ValueError, TypeError):
            continue
        if rule.get("match_all"):
            continue
        conditions = rule.get("conditions") or []
        is_legacy_match_all = (
            len(conditions) == 1
            and conditions[0].get("field") == "name"
            and conditions[0].get("op") == "contains"
            and conditions[0].get("value") == ""
        )
        if not is_legacy_match_all:
            continue
        conn.execute(
            "UPDATE categories SET rule_json=? WHERE id=?",
            (json.dumps({"match_all": True, "exclude_adult": False}), r["id"]),
        )
    _commit_with_retry(conn)


def _seed_default_categories(conn: sqlite3.Connection) -> None:
    """Runs on every startup (cheap -- one COUNT-ish scan per content_type),
    not just first-run: an empty category list isn't just unfriendly, it's a
    hard blocker -- Dispatcharr's VOD refresh aborts entirely rather than
    sync when its get_vod_categories call comes back empty (it reads that as
    "something's wrong upstream", not "genuinely no categories configured
    yet"). Seed a smart catch-all per content type so an instance is never
    left in that broken state.

    Checks for an existing match_all category OR an existing category with
    the exact catch-all name, NOT "any category of this content_type" --
    real regression found live 2026-07-29: the earlier version's plain "any
    category already exists, skip" guard meant any install that had even ONE
    custom category before this catch-all feature shipped (i.e. almost
    every real install) permanently never got a catch-all seeded, silently,
    on every subsequent startup -- read by multiple users as "some
    movies/shows never make it into All Movies/All TV Shows" and "something
    changed in this version." Checking for match_all specifically makes this
    self-healing: an upgrader's very next restart seeds the missing
    catch-all instead of staying broken forever.

    The exact-name check is a separate, necessary safety net, NOT redundant
    with match_all: confirmed live against a real production DB (2026-07-29)
    that its "All Movies"/"All TV Shows" rows predate the match_all
    convention entirely -- created by an older version as ordinary smart
    categories with rule_json {"match": "all", "conditions": [{"field":
    "name", "op": "contains", "value": ""}]} (functionally match-everything,
    but not flagged match_all). Checking match_all alone against that real
    row shape doesn't find it, so this function would try to INSERT a
    second category with the identical name and crash the whole app on
    startup on categories.name's UNIQUE constraint -- confirmed by
    reproducing this exact crash against a real backup before this
    exact-name check was added.

    Defaults to excluding 18+ content (safer default for something created
    without the admin's explicit input) -- the first-run prompt lets them
    flip that per category afterward. Marked with match_all in rule_json
    (see _rule_matches) so the periodic catalog refresh can find and
    re-evaluate just these two, keeping them current as new content is
    imported, without making ordinary user-created smart categories
    auto-evaluate too."""
    import json
    for content_type, name in (("movie", "All Movies"), ("series", "All TV Shows")):
        rows = conn.execute(
            "SELECT rule_json FROM categories WHERE content_type=? AND is_smart=1 AND rule_json IS NOT NULL",
            (content_type,),
        ).fetchall()
        has_catchall = False
        for r in rows:
            try:
                if json.loads(r["rule_json"]).get("match_all"):
                    has_catchall = True
                    break
            except (ValueError, TypeError):
                continue
        if not has_catchall:
            has_catchall = conn.execute(
                "SELECT 1 FROM categories WHERE name=? AND content_type=?", (name, content_type),
            ).fetchone() is not None
        if has_catchall:
            continue
        conn.execute(
            "INSERT INTO categories (name, content_type, is_smart, rule_json, sort_order, created_at) VALUES (?,?,?,?,?,?)",
            (name, content_type, 1, json.dumps({"match_all": True, "exclude_adult": True}), 0, _now()),
        )
    _commit_with_retry(conn)


def _migrate_primary_dispatcharr_connection(conn: sqlite3.Connection) -> None:
    """One-time: dispatcharr_connections used to be a single implicit
    connection (config.py's get_config() + get_vod_xc_account_id()) rather
    than a real list. If nothing's been added to the new table yet but that
    old single connection is configured, carry it over as the first row so
    existing setups (already-connected Dispatcharr instances) keep working
    exactly as before without the user needing to redo anything -- including
    each provider's already-synced dispatcharr_profile_id (providers.
    dispatcharr_profile_id was also a single implicit value; carried into
    provider_sync_profiles for this same connection, or the next sync would
    have re-POSTed a duplicate profile instead of PATCHing the existing one)."""
    existing = conn.execute("SELECT COUNT(*) c FROM dispatcharr_connections").fetchone()["c"]
    if existing > 0:
        return
    url, token = get_config()
    if not url or not token:
        return
    cur = conn.execute(
        "INSERT INTO dispatcharr_connections (label, url, token, vod_relay_account_id, created_at) VALUES (?,?,?,?,?)",
        ("Primary", url, encrypt_value(token), get_vod_xc_account_id(), _now()),
    )
    connection_id = cur.lastrowid
    for row in conn.execute("SELECT id, dispatcharr_profile_id FROM providers WHERE dispatcharr_profile_id IS NOT NULL").fetchall():
        conn.execute(
            "INSERT INTO provider_sync_profiles (provider_id, dispatcharr_connection_id, dispatcharr_profile_id) VALUES (?,?,?)",
            (row["id"], connection_id, row["dispatcharr_profile_id"]),
        )
    _commit_with_retry(conn)


def _migrate_encrypt_plaintext_credentials(conn: sqlite3.Connection) -> None:
    """One-time upgrade: encrypt any provider password / Dispatcharr token /
    XC client secret that predates encrypt-at-rest support. Every read path
    already tolerates plaintext via decrypt_value's InvalidToken fallback,
    so this isn't required for correctness -- it's what actually closes the
    gap for installs that already have real credentials sitting in
    plaintext on disk, not just new ones going forward."""
    for table, column in (
        ("providers", "password"),
        ("dispatcharr_connections", "token"),
        ("xc_clients", "password"),
    ):
        rows = conn.execute(f"SELECT id, {column} FROM {table}").fetchall()
        migrated = 0
        for row in rows:
            value = row[column]
            if not value or is_encrypted(value):
                continue
            conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (encrypt_value(value), row["id"]))
            migrated += 1
        if migrated:
            logger.info("[vod_db] encrypted %d pre-existing plaintext %s.%s value(s) at rest", migrated, table, column)
    _commit_with_retry(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to pre-existing tables that predate them. CREATE TABLE IF
    NOT EXISTS above only helps fresh databases."""
    migrations = [
        ("movies", "last_enriched_at", "TEXT"),
        ("movies", "cast_list", "TEXT"),
        ("movies", "director", "TEXT"),
        ("movies", "country", "TEXT"),
        ("series", "last_enriched_at", "TEXT"),
        ("series", "cast_list", "TEXT"),
        ("series", "director", "TEXT"),
        ("series", "country", "TEXT"),
        ("series", "import_provider_id", "INTEGER"),
        ("series", "import_provider_series_id", "TEXT"),
        ("movie_sources", "provider_category_name", "TEXT"),
        ("episode_sources", "provider_category_name", "TEXT"),
        ("providers", "priority", "INTEGER NOT NULL DEFAULT 0"),
        ("movies", "is_adult", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "is_adult", "INTEGER NOT NULL DEFAULT 0"),
        ("movies", "is_adult_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "is_adult_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("categories", "sync_source", "TEXT"),
        ("providers", "dispatcharr_live_account_id", "INTEGER"),
        ("providers", "shared_connection_limit", "INTEGER"),
        ("providers", "provider_type", "TEXT NOT NULL DEFAULT 'xc'"),
        ("movie_sources", "plex_rating_key", "TEXT"),
        ("episode_sources", "plex_rating_key", "TEXT"),
        ("movies", "needs_year_review", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "needs_year_review", "INTEGER NOT NULL DEFAULT 0"),
        ("providers", "custom_user_agent", "TEXT"),
        ("providers", "last_catalog_refresh_at", "TEXT"),
        ("xc_clients", "category_allowlist", "TEXT"),
        ("categories", "ai_description", "TEXT"),
        ("movies", "review_excluded", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "review_excluded", "INTEGER NOT NULL DEFAULT 0"),
        ("movies", "review_excluded_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "review_excluded_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("providers", "import_exclude_categories", "TEXT"),
        ("categories", "is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("categories", "schedule_start_mmdd", "TEXT"),
        ("categories", "schedule_end_mmdd", "TEXT"),
        ("providers", "dispatcharr_connection_id", "INTEGER"),
        ("providers", "dvr_local_path", "TEXT"),
        ("providers", "dvr_movie_category_id", "INTEGER"),
        ("providers", "dvr_series_category_id", "INTEGER"),
        ("providers", "dvr_remote_recordings_root", "TEXT"),
        ("movie_sources", "local_file_path", "TEXT"),
        ("episode_sources", "local_file_path", "TEXT"),
        ("dvr_recording_profiles", "dispatcharr_user_id", "INTEGER"),
        ("movie_sources", "file_size_bytes", "INTEGER"),
        ("episode_sources", "file_size_bytes", "INTEGER"),
        ("dvr_user_limits", "retention_max_age_days", "INTEGER"),
        ("dvr_user_limits", "retention_max_episodes_per_show", "INTEGER"),
        ("dvr_recording_profiles", "backfill_mode", "TEXT"),
        ("dvr_recording_profiles", "monitored", "INTEGER NOT NULL DEFAULT 1"),
        ("movie_sources", "recording_profile_id", "INTEGER"),
        ("episode_sources", "recording_profile_id", "INTEGER"),
        ("dvr_user_limits", "default_movie_category_id", "INTEGER"),
        ("dvr_user_limits", "default_series_category_id", "INTEGER"),
        ("movie_sources", "dispatcharr_user_id", "INTEGER"),
        ("episode_sources", "dispatcharr_user_id", "INTEGER"),
        ("portal_accounts", "email", "TEXT"),
        ("dvr_user_limits", "quota_policy", "TEXT NOT NULL DEFAULT 'hard_fail'"),
        ("movies", "rating", "TEXT"),
        ("movies", "release_date", "TEXT"),
        ("series", "rating", "TEXT"),
        ("series", "release_date", "TEXT"),
        ("movie_sources", "bitrate", "INTEGER"),
        ("episode_sources", "bitrate", "INTEGER"),
        ("provider_live_accounts", "dispatcharr_profile_id", "INTEGER"),
        ("categories", "schedule_interval_seconds", "INTEGER"),
        ("categories", "last_evaluated_at", "TEXT"),
        ("categories", "use_ai_evaluation", "INTEGER NOT NULL DEFAULT 0"),
        ("providers", "auto_create_categories", "INTEGER NOT NULL DEFAULT 0"),
        ("movie_sources", "raw_name", "TEXT"),
        ("episode_sources", "raw_name", "TEXT"),
        ("series", "provider_category_name", "TEXT"),
        ("providers", "dvr_delete_after_copy", "INTEGER NOT NULL DEFAULT 0"),
        ("providers", "last_movie_provider_total", "INTEGER"),
        ("providers", "last_series_provider_total", "INTEGER"),
        ("series", "raw_name", "TEXT"),
        # DEFAULT 1 (regex) only matters for rows that already exist -- those
        # are genuine hand-written regex from before this column existed.
        # New rules created after this migration get is_regex=0 (literal)
        # explicitly from create_metadata_rule's own default -- literal
        # matching is the safer default for the common case (e.g. stripping
        # a literal "EN| " prefix), since a missing regex escape (an
        # unescaped "|" turning that into "match EN OR a bare space") can't
        # happen if the pattern was never treated as regex in the first
        # place. Real bug found live 2026-07-31.
        ("metadata_rules", "is_regex", "INTEGER NOT NULL DEFAULT 1"),
        ("movie_sources", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("movie_sources", "last_failed_at", "TEXT"),
        ("episode_sources", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("episode_sources", "last_failed_at", "TEXT"),
        ("providers", "archive_new_categories", "INTEGER NOT NULL DEFAULT 0"),
        ("providers", "known_import_categories", "TEXT"),
        # Nullable, ON DELETE SET NULL -- foreign_keys=ON is enforced (see
        # _connect), so a plain REFERENCES without an ON DELETE action would
        # block deleting a movie/episode that still has an old failure row
        # pointing at it. See vod_stream_failures' CREATE TABLE comment: only
        # used to look up *live* "playing from" state while the row still
        # resolves; the stamped title/kind/attempts text is always the
        # source of truth for what a since-deleted item was.
        ("vod_stream_failures", "movie_id", "INTEGER REFERENCES movies(id) ON DELETE SET NULL"),
        ("vod_stream_failures", "episode_id", "INTEGER REFERENCES episodes(id) ON DELETE SET NULL"),
        # GH issue #7: some providers ship movies/series with no category at
        # all -- import_exclude_categories can never match those (it only
        # ever compares an actual category NAME against the exclude list),
        # so this is a separate on/off switch, not another entry in that
        # list. See vod_importer._should_auto_archive.
        ("providers", "import_exclude_uncategorized", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for table, column, coltype in migrations:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    _commit_with_retry(conn)


def _backfill_source_owners(conn: sqlite3.Connection) -> None:
    """One-time (but idempotent -- safe to run every startup) backfill of
    movie_source_owners/episode_source_owners from the legacy single-owner
    columns (recording_profile_id's own dispatcharr_user_id, or the direct
    dispatcharr_user_id column) that predate the owners tables above. Every
    source that already had exactly one known owner keeps that owner; goes
    from implicit/single to explicit/multi without changing who currently
    sees what. INSERT OR IGNORE against the UNIQUE(*_id, dispatcharr_user_id)
    constraint makes re-running this a no-op once caught up."""
    conn.execute("""
        INSERT OR IGNORE INTO movie_source_owners (movie_source_id, dispatcharr_user_id, added_at)
        SELECT ms.id, p.dispatcharr_user_id, ms.added_at
        FROM movie_sources ms JOIN dvr_recording_profiles p ON p.id = ms.recording_profile_id
        WHERE p.dispatcharr_user_id IS NOT NULL
    """)
    conn.execute("""
        INSERT OR IGNORE INTO movie_source_owners (movie_source_id, dispatcharr_user_id, added_at)
        SELECT ms.id, ms.dispatcharr_user_id, ms.added_at
        FROM movie_sources ms WHERE ms.dispatcharr_user_id IS NOT NULL
    """)
    conn.execute("""
        INSERT OR IGNORE INTO episode_source_owners (episode_source_id, dispatcharr_user_id, added_at)
        SELECT es.id, p.dispatcharr_user_id, es.added_at
        FROM episode_sources es JOIN dvr_recording_profiles p ON p.id = es.recording_profile_id
        WHERE p.dispatcharr_user_id IS NOT NULL
    """)
    conn.execute("""
        INSERT OR IGNORE INTO episode_source_owners (episode_source_id, dispatcharr_user_id, added_at)
        SELECT es.id, es.dispatcharr_user_id, es.added_at
        FROM episode_sources es WHERE es.dispatcharr_user_id IS NOT NULL
    """)
    _commit_with_retry(conn)


def _now() -> str:
    return str(time.time())


def _commit_with_retry(conn: sqlite3.Connection, retries: int = 5) -> None:
    """Retries a commit through transient 'database is locked' contention —
    needed once something writes a real batch (e.g. a Plex library import)
    while the background enrichment scheduler is also writing continuously.
    A single very long transaction is a bad neighbor to that scheduler's
    frequent short writes, so bulk import functions call this every N items
    rather than once at the very end (see bulk_import_plex_movies/series)."""
    for attempt in range(retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


_MAX_LOCK_RETRY_DEPTH = 3  # see bulk_import_movies/series's lock_retry_items handling

# SQLite allows exactly one write transaction at a time; under real
# contention (a bulk import running while bulk_enrich_all's ~8 concurrent
# workers are each writing their own item) that's arbitrated by sqlite3's own
# busy-handler, which retries but has no fairness guarantee across many
# competing connections -- confirmed live 2026-07-30: a provider's
# manually-triggered import collided with a concurrent enrichment pass and
# permanently lost ~48% of its items to "database is locked" even after
# every existing safety net (30s busy_timeout, periodic commits, the 3-pass
# lock_retry_items retry above). Reproduced in isolation and confirmed that
# shrinking the commit batch alone (200->25, see bulk_import_movies) did NOT
# fix it -- 8 workers polling for the lock every few ms can still starve a
# 9th out for a full 30s. This lock removes SQLite's own arbitration from the
# equation entirely for the writers actually heavy/frequent enough to matter
# (the bulk importers and the per-item enrichment writes they collide with):
# only one of them ever holds it, so the others simply queue in-process
# instead of racing sqlite3's busy-handler and sometimes losing. Deliberately
# NOT applied to every write function in this file -- rare, low-frequency
# writes (a user editing a rule, provider CRUD) are in no real danger of
# losing that race even without it, and locking them too would only add
# blocking for no measured benefit.
_WRITE_LOCK = threading.RLock()  # RLock: bulk_import_movies/series re-enter it on their own lock_retry_items recursion


@contextmanager
def _item_savepoint(conn: sqlite3.Connection):
    """Isolates one item's writes within a bulk_import_* loop's single shared
    transaction -- a real provider catalog is 1000s of items from a source we
    don't control, and one malformed row (a missing key, an unexpected type)
    must not either lose the whole batch (bulk_import_movies/series commit
    only once, at the very end) or silently leave a half-written item behind
    (an INSERT that succeeded followed by one that raised, in the
    multi-statement plex variants). ROLLBACK TO undoes just this item's
    writes and leaves the rest of the already-processed batch, still
    uncommitted, intact for the final commit."""
    conn.execute("SAVEPOINT item")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO item")
        raise
    finally:
        conn.execute("RELEASE item")


# ── Providers ────────────────────────────────────────────────────────────────

def upsert_provider(
    name: str, base_url: str, username: str, password: str, max_streams: int = 0, priority: int = 0,
    provider_type: str = "xc",
) -> int:
    """For real catalog sources (xc/plex/emby/jellyfin) only -- a DVR
    "provider" row is never created through this path, see
    enable_dvr_for_connection instead. base_url/username are meaningless for
    provider_type='plex' (blank username) and 'emby'/'jellyfin' (both
    blank), same convention every one of those importers already expects."""
    encrypted_password = encrypt_value(password)
    conn = _connect()
    row = conn.execute("SELECT id FROM providers WHERE name = ?", (name,)).fetchone()
    if row:
        conn.execute(
            """UPDATE providers SET base_url=?, username=?, password=?, max_streams=?, priority=?, provider_type=?,
               updated_at=? WHERE id=?""",
            (base_url, username, encrypted_password, max_streams, priority, provider_type, _now(), row["id"]),
        )
        provider_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO providers (name, base_url, username, password, max_streams, priority, provider_type,
               created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, base_url, username, encrypted_password, max_streams, priority, provider_type, _now()),
        )
        provider_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return provider_id


# ── DVR as a connection capability, not a provider type an admin picks ──────
# A dispatcharr_dvr providers row still has to exist underneath (the pool's
# multi-source failover/export queries -- list_movie_sources_for_streaming
# and friends -- JOIN providers for priority/is_active on every source type,
# DVR included, so that part of the schema stays), but nothing above this
# layer should ever require an admin to think in terms of "add a provider"
# for DVR. These three functions are the only way that row gets created,
# named after the connection itself so it reads naturally everywhere it
# already surfaces (DVR tab, Portal Access, Metrics).

def enable_dvr_for_connection(
    connection_id: int, dvr_local_path: str | None, dvr_movie_category_id: int | None,
    dvr_series_category_id: int | None, dvr_remote_recordings_root: str | None = None,
    priority: int = 0, dvr_delete_after_copy: bool = False,
) -> int:
    """Upserts the one providers row for this connection's DVR -- a second
    call with different settings edits the existing row rather than
    erroring, so the admin UI's enable form and edit form are the same
    submit action. The partial unique index on (dispatcharr_connection_id)
    WHERE provider_type='dispatcharr_dvr' is what actually enforces "one per
    connection" at the DB layer; this function's own SELECT-then-INSERT/
    UPDATE is just how it finds the existing row to update, not the
    enforcement itself.

    dvr_delete_after_copy: opt-in, default off -- real user requirement,
    2026-07-29: neither DVR ingestion mode ever cleaned up the original
    recording on Dispatcharr's own side, so its disk fills up forever with
    content VOD Manager has already safely absorbed. Defaults off so
    turning this on is always a deliberate admin choice, never a surprise
    the moment they update -- see dispatcharr_dvr_importer's own docstring
    for exactly what "safely absorbed" means before this deletes anything."""
    connection = get_dispatcharr_connection(connection_id)
    if not connection:
        raise ValueError(f"Dispatcharr connection {connection_id} not found")
    conn = _connect()
    row = conn.execute(
        "SELECT id FROM providers WHERE dispatcharr_connection_id=? AND provider_type='dispatcharr_dvr'",
        (connection_id,),
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE providers SET name=?, dvr_local_path=?, dvr_movie_category_id=?, dvr_series_category_id=?,
               dvr_remote_recordings_root=?, priority=?, dvr_delete_after_copy=?, is_active=1, updated_at=? WHERE id=?""",
            (connection["label"], dvr_local_path, dvr_movie_category_id, dvr_series_category_id,
             dvr_remote_recordings_root, priority, int(dvr_delete_after_copy), _now(), row["id"]),
        )
        provider_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO providers
               (name, base_url, username, password, max_streams, priority, provider_type, is_active,
                dispatcharr_connection_id, dvr_local_path, dvr_movie_category_id, dvr_series_category_id,
                dvr_remote_recordings_root, dvr_delete_after_copy, created_at)
               VALUES (?,'','','',0,?,'dispatcharr_dvr',1,?,?,?,?,?,?,?)""",
            (connection["label"], priority, connection_id, dvr_local_path, dvr_movie_category_id,
             dvr_series_category_id, dvr_remote_recordings_root, int(dvr_delete_after_copy), _now()),
        )
        provider_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return provider_id


def disable_dvr_for_connection(connection_id: int) -> None:
    """Deletes the DVR providers row for this connection -- the existing
    ON DELETE CASCADE on movie_sources/episode_sources/dvr_recording_profiles/
    dvr_user_limits/portal_accounts/dvr_recording_failures already tears
    everything else down, identically to today's admin "Delete provider"."""
    provider = get_dvr_provider_for_connection(connection_id)
    if not provider:
        return
    delete_provider(provider["id"])


def get_dvr_provider_for_connection(connection_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM providers WHERE dispatcharr_connection_id=? AND provider_type='dispatcharr_dvr'",
        (connection_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_movie_source_local_paths(provider_id: int, path_by_stream_id: dict[str, str]) -> dict[str, int]:
    """Applied as a separate pass after bulk_import_plex_movies (see
    dispatcharr_dvr_importer.py) -- local_file_path is DVR-specific and kept
    out of that shared Plex/Emby function on purpose. Matches movie_sources
    rows by the same (provider_id, provider_stream_id) key the import just
    wrote them under; entries with no matching row (e.g. a series episode's
    stream id, since callers pass one dict covering both movies and
    episodes) are silently skipped. Returns {stream_id: movie_id} rather
    than a bare list of touched ids -- Phase 2's per-recording-profile
    category routing needs to trace a specific recording back to the movie
    row it became, not just know which movies were touched in aggregate;
    callers that only need the touched set can take set(result.values())."""
    if not path_by_stream_id:
        return {}
    conn = _connect()
    movie_id_by_stream_id: dict[str, int] = {}
    for stream_id, local_path in path_by_stream_id.items():
        row = conn.execute(
            "SELECT movie_id FROM movie_sources WHERE provider_id=? AND provider_stream_id=?",
            (provider_id, stream_id),
        ).fetchone()
        if not row:
            continue
        conn.execute(
            "UPDATE movie_sources SET local_file_path=? WHERE provider_id=? AND provider_stream_id=?",
            (local_path, provider_id, stream_id),
        )
        movie_id_by_stream_id[stream_id] = row["movie_id"]
    _commit_with_retry(conn)
    conn.close()
    return movie_id_by_stream_id


def set_episode_source_local_paths(provider_id: int, path_by_stream_id: dict[str, str]) -> dict[str, int]:
    """Episode counterpart to set_movie_source_local_paths. Returns
    {stream_id: series_id} (not episode_id) -- DVR category placement
    targets a whole series, matching bulk_place_series_in_category's shape,
    the same as every other series import path in this codebase; see
    set_movie_source_local_paths's docstring for why this is a dict now
    rather than a bare list."""
    if not path_by_stream_id:
        return {}
    conn = _connect()
    series_id_by_stream_id: dict[str, int] = {}
    for stream_id, local_path in path_by_stream_id.items():
        row = conn.execute(
            """SELECT episodes.series_id AS series_id FROM episode_sources
               JOIN episodes ON episodes.id = episode_sources.episode_id
               WHERE episode_sources.provider_id=? AND episode_sources.provider_stream_id=?""",
            (provider_id, stream_id),
        ).fetchone()
        if not row:
            continue
        conn.execute(
            "UPDATE episode_sources SET local_file_path=? WHERE provider_id=? AND provider_stream_id=?",
            (local_path, provider_id, stream_id),
        )
        series_id_by_stream_id[stream_id] = row["series_id"]
    _commit_with_retry(conn)
    conn.close()
    return series_id_by_stream_id


def set_movie_source_recording_profile(provider_id: int, stream_id: str, recording_profile_id: int) -> None:
    """Attributes a just-imported DVR-recorded movie source to the profile
    (and therefore the person, via dvr_recording_profiles.dispatcharr_user_id)
    that scheduled it -- see dispatcharr_dvr_importer.import_dvr_recordings'
    Phase 2, where this is called once per (provider_id, stream_id) after
    set_movie_source_local_paths. A recording can match more than one
    profile (vod_db.match_recording_profiles' fan-out -- e.g. two different
    people each set up their own profile for the same show); this column
    only holds a single owner, so the caller picks one (currently: the
    first match) rather than this being a many-to-many attribution. Good
    enough for "whose portal library does this show up in" without a join
    table, at the cost of a shared recording only ever being attributed to
    one of its matching people."""
    conn = _connect()
    conn.execute(
        "UPDATE movie_sources SET recording_profile_id=? WHERE provider_id=? AND provider_stream_id=?",
        (recording_profile_id, provider_id, stream_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_episode_source_recording_profile(provider_id: int, stream_id: str, recording_profile_id: int) -> None:
    """Episode counterpart to set_movie_source_recording_profile -- same
    single-owner caveat applies."""
    conn = _connect()
    conn.execute(
        "UPDATE episode_sources SET recording_profile_id=? WHERE provider_id=? AND provider_stream_id=?",
        (recording_profile_id, provider_id, stream_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_movie_source_dispatcharr_user_id(provider_id: int, stream_id: str, dispatcharr_user_id: int) -> None:
    """Fallback attribution for a TRUE single (portal_routes.
    portal_schedule_single's 'Record this episode', no dvr_recording_profiles
    row at all) -- set_movie_source_recording_profile can't apply since
    there's no profile to point at, but the portal library still needs to
    know whose recording this is. Real gap found live 2026-07-28: every
    single ever recorded was invisible in every portal user's own Library,
    since list_portal_library_movies/episodes only ever joined through a
    profile. Only called when dispatcharr_dvr_importer's attribution pass
    found no profile match for this stream_id at all -- a profile-owned
    recording keeps using that path instead, unchanged."""
    conn = _connect()
    conn.execute(
        "UPDATE movie_sources SET dispatcharr_user_id=? WHERE provider_id=? AND provider_stream_id=?",
        (dispatcharr_user_id, provider_id, stream_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_episode_source_dispatcharr_user_id(provider_id: int, stream_id: str, dispatcharr_user_id: int) -> None:
    """Episode counterpart to set_movie_source_dispatcharr_user_id."""
    conn = _connect()
    conn.execute(
        "UPDATE episode_sources SET dispatcharr_user_id=? WHERE provider_id=? AND provider_stream_id=?",
        (dispatcharr_user_id, provider_id, stream_id),
    )
    _commit_with_retry(conn)
    conn.close()


def add_movie_source_owner(provider_id: int, stream_id: str, dispatcharr_user_id: int) -> None:
    """Registers one more person as having this recording in their own
    Library -- additive, not exclusive (see movie_source_owners' own table
    comment for why: two people's recording profiles can legitimately match
    the same airing, or one person schedules something another already has).
    INSERT OR IGNORE against the source's UNIQUE(movie_source_id,
    dispatcharr_user_id) constraint, so calling this again for someone
    who's already an owner is a harmless no-op. Silently does nothing if
    this stream_id has no movie_sources row yet (caller ordering issue,
    not this function's problem to raise on)."""
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO movie_source_owners (movie_source_id, dispatcharr_user_id, added_at)
           SELECT id, ?, ? FROM movie_sources WHERE provider_id=? AND provider_stream_id=?""",
        (dispatcharr_user_id, _now(), provider_id, stream_id),
    )
    _commit_with_retry(conn)
    conn.close()


def add_episode_source_owner(provider_id: int, stream_id: str, dispatcharr_user_id: int) -> None:
    """Episode counterpart to add_movie_source_owner."""
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO episode_source_owners (episode_source_id, dispatcharr_user_id, added_at)
           SELECT id, ?, ? FROM episode_sources WHERE provider_id=? AND provider_stream_id=?""",
        (dispatcharr_user_id, _now(), provider_id, stream_id),
    )
    _commit_with_retry(conn)
    conn.close()


def attach_portal_user_to_existing_recording(provider_id: int, stream_id: str, dispatcharr_user_id: int) -> bool:
    """portal_schedule_single's "someone else already recorded this" path --
    a Dispatcharr Recording with a matching identity already exists, so
    instead of creating a duplicate (Dispatcharr wouldn't allow it anyway)
    or flatly refusing, this attaches the calling person as an additional
    owner of whatever VOD Manager already imported for that same
    provider_stream_id, movie or episode, whichever it turns out to be.
    Returns False (does nothing) if this stream_id hasn't been imported
    yet -- still recording, or completed but not yet swept by the importer
    -- there's no source row to attach ownership to yet; the caller falls
    back to add_pending_recording_claim in that case, so the person still
    gets attached once it does import."""
    conn = _connect()
    movie_source = conn.execute(
        "SELECT id FROM movie_sources WHERE provider_id=? AND provider_stream_id=?", (provider_id, stream_id)
    ).fetchone()
    if movie_source:
        conn.execute(
            "INSERT OR IGNORE INTO movie_source_owners (movie_source_id, dispatcharr_user_id, added_at) VALUES (?,?,?)",
            (movie_source["id"], dispatcharr_user_id, _now()),
        )
        _commit_with_retry(conn)
        conn.close()
        return True
    episode_source = conn.execute(
        "SELECT id FROM episode_sources WHERE provider_id=? AND provider_stream_id=?", (provider_id, stream_id)
    ).fetchone()
    if episode_source:
        conn.execute(
            "INSERT OR IGNORE INTO episode_source_owners (episode_source_id, dispatcharr_user_id, added_at) VALUES (?,?,?)",
            (episode_source["id"], dispatcharr_user_id, _now()),
        )
        _commit_with_retry(conn)
        conn.close()
        return True
    conn.close()
    return False


def add_pending_recording_claim(provider_id: int, channel_id: int, identity_key: str, dispatcharr_user_id: int) -> None:
    """Records that this person also wants whatever recording eventually
    matches this (provider, channel, identity) triple -- see
    pending_recording_claims' own table comment. consume_pending_recording_
    claims (called from dispatcharr_dvr_importer once that recording
    actually imports) is what turns this into a real ownership row."""
    conn = _connect()
    conn.execute(
        "INSERT INTO pending_recording_claims (provider_id, channel_id, identity_key, dispatcharr_user_id, created_at) VALUES (?,?,?,?,?)",
        (provider_id, channel_id, identity_key, dispatcharr_user_id, _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def consume_pending_recording_claims(provider_id: int, channel_id: int, identity_key: str) -> list[int]:
    """Called by dispatcharr_dvr_importer for every recording it just
    imported -- returns every dispatcharr_user_id that claimed this exact
    (provider, channel, identity) triple via add_pending_recording_claim,
    and deletes those claim rows in the same call (single-use: once
    consumed here, the caller is about to add each of these as a real
    owner, so the claim has done its job)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, dispatcharr_user_id FROM pending_recording_claims WHERE provider_id=? AND channel_id=? AND identity_key=?",
        (provider_id, channel_id, identity_key),
    ).fetchall()
    if rows:
        conn.executemany("DELETE FROM pending_recording_claims WHERE id=?", [(r["id"],) for r in rows])
        _commit_with_retry(conn)
    conn.close()
    return [r["dispatcharr_user_id"] for r in rows]


def cleanup_stale_recording_claims(max_age_days: int = 7) -> int:
    """A claim whose target recording never actually completes/imports
    (cancelled, failed, or the person just gave up and never checked back)
    would otherwise sit here forever -- called once per dispatcharr_dvr_
    importer run, cheap (this table stays tiny in practice) and bounded."""
    conn = _connect()
    cutoff = str(time.time() - max_age_days * 86400)
    cur = conn.execute("DELETE FROM pending_recording_claims WHERE created_at < ?", (cutoff,))
    _commit_with_retry(conn)
    conn.close()
    return cur.rowcount


def list_owned_movies_oldest_first(provider_id: int, dispatcharr_user_id: int) -> list[dict]:
    """Ordered oldest-first by when THIS person became an owner (their own
    movie_source_owners.added_at), not necessarily the source row's own
    added_at -- for quota_policy='delete_oldest' eviction, "oldest" means
    oldest FOR THIS PERSON, since a shared recording's source could predate
    when they specifically were attached to it (see add_movie_source_owner/
    attach_portal_user_to_existing_recording)."""
    conn = _connect()
    rows = conn.execute("""
        SELECT m.id AS movie_id, ms.id AS source_id, mso.added_at
        FROM movie_source_owners mso
        JOIN movie_sources ms ON ms.id = mso.movie_source_id
        JOIN movies m ON m.id = ms.movie_id
        WHERE mso.dispatcharr_user_id = ? AND ms.provider_id = ?
        ORDER BY mso.added_at ASC
    """, (dispatcharr_user_id, provider_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_owned_episodes_oldest_first(provider_id: int, dispatcharr_user_id: int) -> list[dict]:
    """Episode counterpart to list_owned_movies_oldest_first."""
    conn = _connect()
    rows = conn.execute("""
        SELECT e.id AS episode_id, es.id AS source_id, eso.added_at
        FROM episode_source_owners eso
        JOIN episode_sources es ON es.id = eso.episode_source_id
        JOIN episodes e ON e.id = es.episode_id
        WHERE eso.dispatcharr_user_id = ? AND es.provider_id = ?
        ORDER BY eso.added_at ASC
    """, (dispatcharr_user_id, provider_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sync_quota_warnings_sent(provider_id: int, dispatcharr_user_id: int, currently_met_thresholds: set[int]) -> set[int]:
    """Reconciles quota_warnings_sent against which thresholds this
    person's CURRENT usage actually meets -- clears any previously-sent
    threshold no longer met (so crossing it again later re-warns instead of
    staying permanently suppressed) and records every currently-met
    threshold. Returns only the NEWLY crossed thresholds from this call;
    the caller (dispatcharr_dvr_importer) only sends a notification for
    those, not ones already warned about in an earlier pass."""
    conn = _connect()
    already = {r["threshold_pct"] for r in conn.execute(
        "SELECT threshold_pct FROM quota_warnings_sent WHERE provider_id=? AND dispatcharr_user_id=?",
        (provider_id, dispatcharr_user_id),
    ).fetchall()}
    for t in already - currently_met_thresholds:
        conn.execute(
            "DELETE FROM quota_warnings_sent WHERE provider_id=? AND dispatcharr_user_id=? AND threshold_pct=?",
            (provider_id, dispatcharr_user_id, t),
        )
    newly_crossed = currently_met_thresholds - already
    for t in newly_crossed:
        conn.execute(
            "INSERT OR IGNORE INTO quota_warnings_sent (provider_id, dispatcharr_user_id, threshold_pct, sent_at) VALUES (?,?,?,?)",
            (provider_id, dispatcharr_user_id, t, _now()),
        )
    _commit_with_retry(conn)
    conn.close()
    return newly_crossed


def remove_movie_library_owner(movie_id: int, provider_id: int, dispatcharr_user_id: int) -> dict:
    """The portal-facing 'remove from my Library' action -- removes only
    the calling person's own ownership row. The underlying movie_sources
    row (and its file on disk, if any) is only deleted once NO owner
    remains at all, i.e. reference-counted deletion: a recording shared by
    two people via add_movie_source_owner survives on disk for the one who
    keeps it after the other removes theirs. Real requirement from the
    user, 2026-07-28.

    The file is deliberately left alone if any OTHER source row (any
    movie/episode, any provider) still points at that exact path --
    defensive, given a real duplicate-row bug found live this same session
    where two different source rows ended up pointing at the same file; far
    better to leak a file than to delete one a different, unrelated row
    still depends on."""
    conn = _connect()
    source = conn.execute(
        "SELECT id, local_file_path FROM movie_sources WHERE movie_id=? AND provider_id=?",
        (movie_id, provider_id),
    ).fetchone()
    if not source:
        conn.close()
        return {"removed": False, "fully_deleted": False}
    source_id = source["id"]
    conn.execute(
        "DELETE FROM movie_source_owners WHERE movie_source_id=? AND dispatcharr_user_id=?",
        (source_id, dispatcharr_user_id),
    )
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM movie_source_owners WHERE movie_source_id=?", (source_id,)
    ).fetchone()["c"]
    file_path = None
    fully_deleted = False
    if remaining == 0:
        fully_deleted = True
        if source["local_file_path"]:
            other_ref = conn.execute(
                """SELECT 1 FROM movie_sources WHERE local_file_path=? AND id!=?
                   UNION SELECT 1 FROM episode_sources WHERE local_file_path=? LIMIT 1""",
                (source["local_file_path"], source_id, source["local_file_path"]),
            ).fetchone()
            if not other_ref:
                file_path = source["local_file_path"]
        conn.execute("DELETE FROM movie_sources WHERE id=?", (source_id,))
        _purge_if_sourceless_movie(conn, movie_id)
    _commit_with_retry(conn)
    conn.close()
    if fully_deleted:
        _delete_file_if_present(file_path)
    return {"removed": True, "fully_deleted": fully_deleted}


def remove_episode_library_owner(episode_id: int, provider_id: int, dispatcharr_user_id: int) -> dict:
    """Episode counterpart to remove_movie_library_owner."""
    conn = _connect()
    source = conn.execute(
        "SELECT id, local_file_path FROM episode_sources WHERE episode_id=? AND provider_id=?",
        (episode_id, provider_id),
    ).fetchone()
    if not source:
        conn.close()
        return {"removed": False, "fully_deleted": False}
    source_id = source["id"]
    conn.execute(
        "DELETE FROM episode_source_owners WHERE episode_source_id=? AND dispatcharr_user_id=?",
        (source_id, dispatcharr_user_id),
    )
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM episode_source_owners WHERE episode_source_id=?", (source_id,)
    ).fetchone()["c"]
    file_path = None
    fully_deleted = False
    episode_row = conn.execute("SELECT series_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if remaining == 0:
        fully_deleted = True
        if source["local_file_path"]:
            other_ref = conn.execute(
                """SELECT 1 FROM episode_sources WHERE local_file_path=? AND id!=?
                   UNION SELECT 1 FROM movie_sources WHERE local_file_path=? LIMIT 1""",
                (source["local_file_path"], source_id, source["local_file_path"]),
            ).fetchone()
            if not other_ref:
                file_path = source["local_file_path"]
        conn.execute("DELETE FROM episode_sources WHERE id=?", (source_id,))
        _purge_if_sourceless_episode(conn, episode_id)
        if episode_row:
            _purge_if_sourceless_series(conn, episode_row["series_id"])
    _commit_with_retry(conn)
    conn.close()
    if fully_deleted:
        _delete_file_if_present(file_path)
    return {"removed": True, "fully_deleted": fully_deleted}


# ── DVR recording profiles (Phase 2) ────────────────────────────────────────
# Per-person/per-schedule routing on top of a DVR provider's own default
# categories -- see dispatcharr_dvr_client.schedule_channel_recordings and
# dispatcharr_dvr_importer's profile-matching pass.

def create_recording_profile(
    provider_id: int, label: str, title: str,
    tvg_id: str | None = None, title_mode: str = "exact",
    description: str | None = None, description_mode: str = "contains",
    mode: str = "all", channel_id: int | None = None,
    target_movie_category_id: int | None = None, target_series_category_id: int | None = None,
    dispatcharr_user_id: int | None = None, backfill_mode: str | None = None,
) -> int:
    """backfill_mode is None/'off' (default -- always record via DVR even if
    the title already exists in the pool from another provider), 'pointer'
    (place the existing item into this rule's target category instead of
    recording, no new disk cost -- see vod_db.find_pool_backfill_match /
    dispatcharr_dvr_importer._try_backfill), or 'download' (same match, but
    also pulls a durable local copy from the source provider first, so the
    item survives that provider going down -- per the user's explicit
    reasoning, 2026-07-27, that a pointer-only "recording" a person believes
    is safe could otherwise vanish out from under them)."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO dvr_recording_profiles
           (provider_id, label, tvg_id, title, title_mode, description, description_mode,
            mode, channel_id, target_movie_category_id, target_series_category_id,
            dispatcharr_user_id, backfill_mode, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (provider_id, label, tvg_id or None, title, title_mode, description or None, description_mode,
         mode, channel_id, target_movie_category_id, target_series_category_id,
         dispatcharr_user_id, backfill_mode or None, _now()),
    )
    profile_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return profile_id


def list_recording_profiles(provider_id: int | None = None) -> list[dict]:
    conn = _connect()
    if provider_id is not None:
        rows = conn.execute(
            "SELECT * FROM dvr_recording_profiles WHERE provider_id=? ORDER BY label", (provider_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM dvr_recording_profiles ORDER BY label").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recording_profile(profile_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM dvr_recording_profiles WHERE id=?", (profile_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_recording_profile(profile_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM dvr_recording_profiles WHERE id=?", (profile_id,))
    _commit_with_retry(conn)
    conn.close()


def set_recording_profile_monitored(profile_id: int, monitored: bool) -> None:
    """Independent of whether the rule still actively schedules recordings
    -- monitored only controls whether this show's gaps surface on the
    dedicated Missing Episodes page. Unmonitoring a rule is how an admin
    opts a show out of that page's clutter without deleting (and thereby
    cancelling) the rule's own real Dispatcharr recordings."""
    conn = _connect()
    conn.execute("UPDATE dvr_recording_profiles SET monitored=? WHERE id=?", (1 if monitored else 0, profile_id))
    _commit_with_retry(conn)
    conn.close()


def match_recording_profiles(provider_id: int, title: str, tvg_id: str | None) -> list[dict]:
    """Matches a completed recording back to every profile that scheduled it
    -- plural, not singular: two different people can each set up their own
    profile for the same show (one scoped to a specific EPG channel, one
    left blank to match any channel, or simply two blank-tvg_id profiles),
    and a single recording can legitimately satisfy more than one of them.
    The caller places the recording into the union of every matched
    profile's target categories rather than picking just one winner.

    Dispatcharr's own Recording data carries no rule reference at all
    (confirmed via its OpenAPI schema and real captured recordings), but its
    series-rules resource is itself identified purely by (title, tvg_id) --
    its own DELETE endpoint takes exactly those two params, no id -- so
    that's the same pair used here. A profile with no tvg_id set matches
    across any channel (mirrors Dispatcharr's own "blank tvg_id = search all
    channels" behavior); a profile scoped to a specific tvg_id only matches
    a recording that actually aired on that channel.

    Note this is a purely local/VOD-Manager-side fan-out. Confirmed by
    reading Dispatcharr's own evaluate_series_rules_impl (apps/channels/
    tasks.py, dispatch-test v0.27.2, 2026-07-26): it builds ONE
    existing_program_keys set -- keyed by the airing's own (tvg_id,
    start_time, end_time), not by which rule matched it -- shared across
    every rule in the same evaluation pass. So even when both a
    channel-scoped rule and a channel-agnostic rule match the very same
    airing, whichever rule the loop reaches first creates the Recording and
    adds that key; the other rule's query hits the same key and is skipped.
    Exactly one physical recording is ever produced for a given airing
    regardless of how many rules (or VOD Manager profiles) matched it, so
    "multiple profiles matching" here really does mean multiple people
    sharing that one recording's copies-into-categories fan-out, not
    multiple independent recordings or duplicate downloads."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM dvr_recording_profiles WHERE provider_id=? AND title=?", (provider_id, title)
    ).fetchall()
    conn.close()
    candidates = [dict(r) for r in rows]
    return [c for c in candidates if not c["tvg_id"] or c["tvg_id"] == tvg_id]


# ── Multi-source ordering (failover priority) ────────────────────────────────
# Every query that picks/orders a movie's or episode's sources when more than
# one provider has it -- config.get_stream_priority_mode() is user-definable
# (Curation & Maintenance), default "provider" (today's original, unchanged
# behavior: highest provider.priority wins, recency as tiebreak).

_QUALITY_TIER_SQL_TEMPLATE = """CASE
    WHEN ({a}.raw_name LIKE '%4K%' OR {a}.raw_name LIKE '%UHD%' OR {a}.provider_category_name LIKE '%4K%' OR {a}.provider_category_name LIKE '%UHD%') THEN 3
    WHEN ({a}.raw_name LIKE '%FHD%' OR {a}.raw_name LIKE '%1080%' OR {a}.provider_category_name LIKE '%FHD%' OR {a}.provider_category_name LIKE '%1080%') THEN 2
    WHEN ({a}.raw_name LIKE '%HD%' OR {a}.raw_name LIKE '%720%' OR {a}.provider_category_name LIKE '%HD%' OR {a}.provider_category_name LIKE '%720%') THEN 1
    ELSE 0
END"""
# SQLite's LIKE is case-insensitive for ASCII by default, so this matches
# "4k"/"4K"/"UHD"/"uhd" etc. without needing UPPER()/LOWER(). Order matters:
# UHD/4K checked before the bare "HD"/"720" tier, since "HD" is a substring
# of "UHD" -- CASE stops at the first matching WHEN, so a raw_name containing
# "UHD" hits the tier-3 branch and never reaches the plain-HD check.


# A source that has failed this many times in a row (and hasn't succeeded
# since) is real dead weight -- still tried (a provider can come back), but
# last, after every source without that track record, so playback doesn't
# eat a slow doomed connect attempt first on every request. See
# _source_order_by, record_source_failure/record_source_success.
_FAILING_SOURCE_THRESHOLD = 3


def _source_order_by(source_alias: str, provider_alias: str) -> str:
    """Builds the ORDER BY clause for a multi-source query. source_alias is
    the movie_sources/episode_sources table alias (has raw_name,
    provider_category_name, last_seen_at, consecutive_failures);
    provider_alias is the joined providers table alias (has priority)."""
    quality = _QUALITY_TIER_SQL_TEMPLATE.format(a=source_alias)
    failing = f"(CASE WHEN {source_alias}.consecutive_failures >= {_FAILING_SOURCE_THRESHOLD} THEN 1 ELSE 0 END) ASC"
    mode = get_stream_priority_mode()
    if mode == "quality":
        return f"{failing}, {quality} DESC, {source_alias}.last_seen_at DESC"
    if mode == "quality_then_provider":
        return f"{failing}, {quality} DESC, {provider_alias}.priority DESC, {source_alias}.last_seen_at DESC"
    if mode == "provider_then_quality":
        return f"{failing}, {provider_alias}.priority DESC, {quality} DESC, {source_alias}.last_seen_at DESC"
    return f"{failing}, {provider_alias}.priority DESC, {source_alias}.last_seen_at DESC"


def find_pool_backfill_match(title: str, program: dict) -> dict | None:
    """Backfill support (opt-in per rule via dvr_recording_profiles.
    backfill_mode): before scheduling a fresh DVR recording, checks whether
    this exact episode/movie already sits in the pool from a regular
    (non-DVR) provider -- if so, the rule can place the existing item into
    its target category instead of re-recording it (see
    dispatcharr_dvr_importer._try_backfill).

    Matches on _normalize_title_for_dedup'd title, the same forgiving-but-
    bounded comparison the duplicate-detection pass already trusts
    elsewhere in this file -- exact string equality would miss "Show" vs
    "Show:", the same real-world mismatch that motivated
    _normalize_title_for_dedup in the first place.

    program carrying season/episode (custom_properties, same shape
    dispatcharr_dvr_client.episode_identity_key reads) means it's a series
    airing: matches a series by normalized name, then a specific episode by
    season+episode number within it. No season/episode means a movie:
    matches by normalized name alone -- EPG program data essentially never
    carries a reliable year, so year isn't part of this comparison.

    Only ever returns a source whose own provider is itself NOT a
    dispatcharr_dvr provider -- backfilling from another rule's own DVR
    recording isn't "already in the pool from elsewhere," it's just a
    different rule's output, so it's excluded to avoid one rule silently
    eating another's recording instead of doing its own job. Full-table
    scan + Python-side normalization, same tradeoff find_duplicate_groups
    already makes -- pool sizes here are in the thousands, not millions."""
    props = program.get("custom_properties") or {}
    season, episode = props.get("season"), props.get("episode")
    normalized = _normalize_title_for_dedup(title)
    conn = _connect()
    if season is not None and episode is not None:
        series_rows = conn.execute("SELECT id, name FROM series").fetchall()
        series_id = next((r["id"] for r in series_rows if _normalize_title_for_dedup(r["name"]) == normalized), None)
        if series_id is None:
            conn.close()
            return None
        ep_row = conn.execute(
            "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
            (series_id, season, episode),
        ).fetchone()
        if not ep_row:
            conn.close()
            return None
        source = conn.execute(f"""
            SELECT es.id, es.provider_id, es.provider_stream_id, es.container_extension, es.file_size_bytes, es.local_file_path
            FROM episode_sources es JOIN providers p ON p.id = es.provider_id
            WHERE es.episode_id=? AND p.provider_type != 'dispatcharr_dvr' AND p.is_active=1
            ORDER BY {_source_order_by('es', 'p')} LIMIT 1
        """, (ep_row["id"],)).fetchone()
        conn.close()
        return {"type": "series", "series_id": series_id, "episode_id": ep_row["id"], "source": dict(source)} if source else None
    movie_rows = conn.execute("SELECT id, name FROM movies").fetchall()
    movie_id = next((r["id"] for r in movie_rows if _normalize_title_for_dedup(r["name"]) == normalized), None)
    if movie_id is None:
        conn.close()
        return None
    source = conn.execute(f"""
        SELECT ms.id, ms.provider_id, ms.provider_stream_id, ms.container_extension, ms.file_size_bytes, ms.local_file_path
        FROM movie_sources ms JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id=? AND p.provider_type != 'dispatcharr_dvr' AND p.is_active=1
        ORDER BY {_source_order_by('ms', 'p')} LIMIT 1
    """, (movie_id,)).fetchone()
    conn.close()
    return {"type": "movie", "movie_id": movie_id, "source": dict(source)} if source else None


def find_series_by_title(title: str) -> dict | None:
    """Same normalized-title match find_pool_backfill_match already trusts,
    factored out as its own lookup for vod_manager-8p1.2's Portal past-
    seasons backfill: needs the full series row (specifically tmdb_id) up
    front to know whether a canonical episode list is even available to
    diff against, before doing any per-episode work."""
    normalized = _normalize_title_for_dedup(title)
    conn = _connect()
    row = next(
        (r for r in conn.execute("SELECT id, name FROM series").fetchall()
         if _normalize_title_for_dedup(r["name"]) == normalized),
        None,
    )
    conn.close()
    return get_series(row["id"]) if row else None


def find_recording_profile_for_title(provider_id: int, title: str) -> dict | None:
    """Missing-episode resolve's own backfill_mode/target-category source
    when there's no per-episode rule to ask -- an existing Recording Rule
    for this same show (any channel) is the closest signal for "how would
    the admin normally want this handled," so its own backfill_mode and
    target categories are reused rather than defaulting blind. None when no
    rule exists at all -- the caller falls back to the provider's own
    default dvr_movie_category_id/dvr_series_category_id in that case, same
    fallback the main import pass already uses."""
    normalized = _normalize_title_for_dedup(title)
    conn = _connect()
    rows = conn.execute("SELECT * FROM dvr_recording_profiles WHERE provider_id=?", (provider_id,)).fetchall()
    conn.close()
    return next((dict(r) for r in rows if _normalize_title_for_dedup(r["title"]) == normalized), None)


def record_unresolved_missing_episode(series_id: int, season_number: int, episode_number: int, episode_name: str | None) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO dvr_unresolved_missing_episodes (series_id, season_number, episode_number, episode_name, checked_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET
               episode_name=excluded.episode_name, checked_at=excluded.checked_at""",
        (series_id, season_number, episode_number, episode_name, _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def clear_unresolved_missing_episode(series_id: int, season_number: int, episode_number: int) -> None:
    conn = _connect()
    conn.execute(
        "DELETE FROM dvr_unresolved_missing_episodes WHERE series_id=? AND season_number=? AND episode_number=?",
        (series_id, season_number, episode_number),
    )
    _commit_with_retry(conn)
    conn.close()


def list_unresolved_missing_episodes(series_id: int | None = None) -> list[dict]:
    conn = _connect()
    if series_id is not None:
        rows = conn.execute(
            """SELECT u.*, s.name AS series_name FROM dvr_unresolved_missing_episodes u
               JOIN series s ON s.id = u.series_id WHERE u.series_id=?
               ORDER BY u.season_number, u.episode_number""",
            (series_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT u.*, s.name AS series_name FROM dvr_unresolved_missing_episodes u
               JOIN series s ON s.id = u.series_id
               ORDER BY u.checked_at DESC""",
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Failed DVR recording replacements ───────────────────────────────────────
# See dvr_recording_failures' CREATE TABLE comment above and
# dispatcharr_dvr_importer.reschedule_failed_recordings for the full cascade.

def upsert_recording_failure(
    provider_id: int, dispatcharr_recording_id: int, title: str,
    season_number: int | None, episode_number: int | None, original_channel_id: int | None,
    interrupted_reason: str | None, outcome: str, replacement_channel_id: int | None,
) -> None:
    """outcome='unresolved' rows are meant to be called again on a later
    poll cycle (see reschedule_failed_recordings) -- the ON CONFLICT branch
    lets a retry's fresh outcome/replacement_channel_id/detected_at
    overwrite the previous attempt's, so a row that goes unresolved ->
    rescheduled across cycles just updates in place rather than needing a
    separate transition path."""
    conn = _connect()
    conn.execute(
        """INSERT INTO dvr_recording_failures
           (provider_id, dispatcharr_recording_id, title, season_number, episode_number,
            original_channel_id, interrupted_reason, outcome, replacement_channel_id, detected_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(provider_id, dispatcharr_recording_id) DO UPDATE SET
               outcome=excluded.outcome, replacement_channel_id=excluded.replacement_channel_id,
               detected_at=excluded.detected_at""",
        (provider_id, dispatcharr_recording_id, title, season_number, episode_number,
         original_channel_id, interrupted_reason, outcome, replacement_channel_id, _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def list_recording_failures(provider_id: int | None = None) -> list[dict]:
    conn = _connect()
    if provider_id is not None:
        rows = conn.execute(
            "SELECT * FROM dvr_recording_failures WHERE provider_id=? ORDER BY detected_at DESC", (provider_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM dvr_recording_failures ORDER BY detected_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recording_failure(provider_id: int, dispatcharr_recording_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM dvr_recording_failures WHERE provider_id=? AND dispatcharr_recording_id=?",
        (provider_id, dispatcharr_recording_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Failed VOD stream playback attempts ─────────────────────────────────────
# See vod_stream_failures' CREATE TABLE comment above and
# xc_server._proxy_vod_stream, the only writer.

_MAX_STORED_STREAM_FAILURES = 500


def log_stream_failure(
    kind: str, title: str, username: str | None, attempts: list[dict], final_reason: str,
    movie_id: int | None = None, episode_id: int | None = None,
) -> None:
    import json
    conn = _connect()
    conn.execute(
        "INSERT INTO vod_stream_failures (kind, title, username, attempts, final_reason, created_at, movie_id, episode_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (kind, title, username, json.dumps(attempts), final_reason, _now(), movie_id, episode_id),
    )
    conn.execute(
        "DELETE FROM vod_stream_failures WHERE id NOT IN "
        "(SELECT id FROM vod_stream_failures ORDER BY created_at DESC, id DESC LIMIT ?)",
        (_MAX_STORED_STREAM_FAILURES,),
    )
    _commit_with_retry(conn)
    conn.close()


def record_source_failure(kind: str, source_id: int) -> None:
    """Bumps one specific source's own failure streak -- called on every
    per-source failure in xc_server._proxy_vod_stream's failover loop, not
    just when every source is exhausted. Real motivating case (2026-08-01):
    a series airing fine overall because one good provider (e.g. 4KLive)
    covers most episodes masks a second provider (e.g. Mega-OTT) whose
    copies are almost entirely broken -- vod_stream_failures alone would
    never surface that, since fallback to the good provider means no
    request ever actually failed outright. See _source_order_by, which
    reads this to try a source with a live failure streak last instead of
    first, and record_source_success, which clears it."""
    table = "movie_sources" if kind == "movie" else "episode_sources"
    conn = _connect()
    conn.execute(
        f"UPDATE {table} SET consecutive_failures = consecutive_failures + 1, last_failed_at = ? WHERE id = ?",
        (_now(), source_id),
    )
    _commit_with_retry(conn)
    conn.close()


def record_source_success(kind: str, source_id: int) -> None:
    """Clears a source's failure streak the moment it actually works again --
    a provider that was down can come back, and a source that's currently
    serving successfully shouldn't stay deprioritized on its past record."""
    table = "movie_sources" if kind == "movie" else "episode_sources"
    conn = _connect()
    conn.execute(
        f"UPDATE {table} SET consecutive_failures = 0, last_failed_at = NULL WHERE id = ? AND consecutive_failures != 0",
        (source_id,),
    )
    _commit_with_retry(conn)
    conn.close()


def _current_best_source(kind: str, movie_id: int | None, episode_id: int | None) -> dict | None:
    """The source that would actually be tried first right now for this
    specific movie/episode -- reuses list_movie/episode_sources_for_streaming,
    the exact same ordering xc_server._proxy_vod_stream's failover loop uses
    to pick source order, so this can never drift from what actually plays.
    Returns None if the row was since deleted (movie_id/episode_id NULL) or
    has no active-provider source at all."""
    if movie_id:
        sources = list_movie_sources_for_streaming(movie_id)
    elif episode_id:
        sources = list_episode_sources_for_streaming(episode_id)
    else:
        return None
    if not sources:
        return None
    top = sources[0]
    return {
        "provider_name": top["provider_name"],
        "is_failing": top["consecutive_failures"] >= _FAILING_SOURCE_THRESHOLD,
        "consecutive_failures": top["consecutive_failures"],
    }


def _series_providers(series_id: int) -> list[str]:
    """Distinct providers with ANY source across the whole series -- not just
    the one episode a given failure happened on. Providers only ever attach
    at the episode level, so without this an admin has no way to see the
    full picture without opening every episode individually."""
    conn = _connect()
    rows = conn.execute("""
        SELECT DISTINCT p.name AS provider_name
        FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        JOIN providers p ON p.id = es.provider_id
        WHERE e.series_id = ?
        ORDER BY p.name
    """, (series_id,)).fetchall()
    conn.close()
    return [r["provider_name"] for r in rows]


def list_stream_failures(limit: int = 200) -> list[dict]:
    import json
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM vod_stream_failures ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["attempts"] = json.loads(d["attempts"])
        except (TypeError, ValueError):
            d["attempts"] = []
        d["current_source"] = _current_best_source(d["kind"], d.get("movie_id"), d.get("episode_id"))
        d["series_providers"] = None
        if d["kind"] == "series" and d.get("episode_id"):
            episode = get_episode(d["episode_id"])
            if episode:
                d["series_providers"] = _series_providers(episode["series_id"])
        out.append(d)
    return out


def delete_stream_failure(failure_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM vod_stream_failures WHERE id=?", (failure_id,))
    _commit_with_retry(conn)
    conn.close()


def clear_stream_failures() -> None:
    conn = _connect()
    conn.execute("DELETE FROM vod_stream_failures")
    _commit_with_retry(conn)
    conn.close()


# ── DVR per-person resource limits ──────────────────────────────────────────
# Opt-in: a person (a real Dispatcharr login user, identified by their numeric
# id) with no row here has no DVR limit enforced at all. See
# dvr_recording_profiles' dispatcharr_user_id column and vod_routes.py's
# create_recording_profile for how the stream-concurrency check uses this.

def create_dvr_user_limit(
    provider_id: int, dispatcharr_user_id: int, dispatcharr_username: str,
    stream_reserve: int = 0, disk_quota_bytes: int | None = None,
    retention_max_age_days: int | None = None, retention_max_episodes_per_show: int | None = None,
    default_movie_category_id: int | None = None, default_series_category_id: int | None = None,
    quota_policy: str = "hard_fail",
) -> int:
    """quota_policy: 'hard_fail' (new recordings refused once at/over quota,
    see portal_routes.portal_schedule_single/portal_create_recording_rule)
    or 'delete_oldest' (dispatcharr_dvr_importer automatically evicts this
    person's own oldest owned recordings to make room -- reference-counted
    via remove_movie/episode_library_owner, so a recording someone ELSE
    also owns survives). Chosen once at creation per the user's own
    explicit call, 2026-07-28 -- editable later via update_dvr_user_limit
    like every other field here."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO dvr_user_limits
           (provider_id, dispatcharr_user_id, dispatcharr_username, stream_reserve, disk_quota_bytes,
            retention_max_age_days, retention_max_episodes_per_show, default_movie_category_id,
            default_series_category_id, quota_policy, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (provider_id, dispatcharr_user_id, dispatcharr_username, stream_reserve, disk_quota_bytes,
         retention_max_age_days, retention_max_episodes_per_show, default_movie_category_id,
         default_series_category_id, quota_policy, _now()),
    )
    limit_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return limit_id


def list_dvr_user_limits(provider_id: int | None = None) -> list[dict]:
    conn = _connect()
    if provider_id is not None:
        rows = conn.execute(
            "SELECT * FROM dvr_user_limits WHERE provider_id=? ORDER BY dispatcharr_username", (provider_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM dvr_user_limits ORDER BY dispatcharr_username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_dvr_user_limit(provider_id: int, dispatcharr_user_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM dvr_user_limits WHERE provider_id=? AND dispatcharr_user_id=?",
        (provider_id, dispatcharr_user_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_dvr_user_limit(
    limit_id: int, stream_reserve: int, disk_quota_bytes: int | None,
    retention_max_age_days: int | None = None, retention_max_episodes_per_show: int | None = None,
    default_movie_category_id: int | None = None, default_series_category_id: int | None = None,
    quota_policy: str | None = None,
) -> None:
    conn = _connect()
    if quota_policy is None:
        conn.execute(
            """UPDATE dvr_user_limits SET stream_reserve=?, disk_quota_bytes=?,
               retention_max_age_days=?, retention_max_episodes_per_show=?,
               default_movie_category_id=?, default_series_category_id=? WHERE id=?""",
            (stream_reserve, disk_quota_bytes, retention_max_age_days, retention_max_episodes_per_show,
             default_movie_category_id, default_series_category_id, limit_id),
        )
    else:
        conn.execute(
            """UPDATE dvr_user_limits SET stream_reserve=?, disk_quota_bytes=?,
               retention_max_age_days=?, retention_max_episodes_per_show=?,
               default_movie_category_id=?, default_series_category_id=?, quota_policy=? WHERE id=?""",
            (stream_reserve, disk_quota_bytes, retention_max_age_days, retention_max_episodes_per_show,
             default_movie_category_id, default_series_category_id, quota_policy, limit_id),
        )
    _commit_with_retry(conn)
    conn.close()


def delete_dvr_user_limit(limit_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM dvr_user_limits WHERE id=?", (limit_id,))
    _commit_with_retry(conn)
    conn.close()


# ── Portal accounts (end-user self-service DVR login) ──────────────────────
# See the portal_accounts CREATE TABLE comment above for the design
# rationale. Admin-provisioned only -- backend/vod_routes.py's admin-guarded
# CRUD routes call these; backend/portal_routes.py's login flow calls
# get_portal_account_by_username/set_portal_account_totp.

def create_portal_account(
    provider_id: int, dispatcharr_user_id: int, username: str,
    password_salt: str, password_hash: str, email: str | None = None,
) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO portal_accounts
           (provider_id, dispatcharr_user_id, username, password_salt, password_hash, email, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (provider_id, dispatcharr_user_id, username, password_salt, password_hash, email, _now()),
    )
    account_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return account_id


def set_portal_account_email(account_id: int, email: str | None) -> None:
    """Admin edit (Portal Access row) or the person's own self-service
    update (new portal Account tab) both call this -- same underlying
    field, only used for notifications.notify_quota_threshold today
    (see the user's 'Both' call, 2026-07-28: admin always gets warned,
    the person themselves also does if they've set an email here)."""
    conn = _connect()
    conn.execute("UPDATE portal_accounts SET email=? WHERE id=?", (email, account_id))
    _commit_with_retry(conn)
    conn.close()


def list_portal_accounts(provider_id: int | None = None) -> list[dict]:
    conn = _connect()
    if provider_id is not None:
        rows = conn.execute(
            "SELECT * FROM portal_accounts WHERE provider_id=? ORDER BY username", (provider_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM portal_accounts ORDER BY username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_portal_account(account_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM portal_accounts WHERE id=?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_portal_account_by_username(username: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM portal_accounts WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_portal_account_password(account_id: int, password_salt: str, password_hash: str) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE portal_accounts SET password_salt=?, password_hash=? WHERE id=?",
        (password_salt, password_hash, account_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_portal_account_totp(account_id: int, totp_secret: str | None, totp_enabled: bool) -> None:
    """Pass totp_secret=None, totp_enabled=False to revoke enrollment (e.g.
    admin-triggered MFA reset) -- the next login forces re-enrollment.
    Resets totp_last_counter -- a fresh/rotated secret invalidates whatever
    counter was tracked against the old one. Use enable_confirmed_totp
    instead when flipping totp_enabled on for the secret that was just
    verified (portal_routes.portal_confirm_mfa) -- that path must NOT wipe
    the counter that same verification just recorded, or the code that
    confirmed enrollment becomes replayable again immediately after."""
    conn = _connect()
    conn.execute(
        "UPDATE portal_accounts SET totp_secret=?, totp_enabled=?, totp_last_counter=NULL WHERE id=?",
        (encrypt_value(totp_secret) if totp_secret else None, int(totp_enabled), account_id),
    )
    _commit_with_retry(conn)
    conn.close()


def enable_confirmed_portal_totp(account_id: int) -> None:
    """Flips totp_enabled=1 for a secret that's already stored (set via
    set_portal_account_totp during enrollment) and already passed
    verification -- deliberately leaves totp_secret and totp_last_counter
    untouched, unlike set_portal_account_totp."""
    conn = _connect()
    conn.execute("UPDATE portal_accounts SET totp_enabled=1 WHERE id=?", (account_id,))
    _commit_with_retry(conn)
    conn.close()


def set_portal_account_totp_counter(account_id: int, counter: int) -> None:
    """Anti-replay: records the 30s time-step counter of the most recently
    accepted TOTP code so portal_routes._verify_totp_code can reject that
    exact code (or an earlier one) being submitted a second time within its
    own still-valid window -- otherwise a shoulder-surfed/intercepted code
    is usable twice, not just once, for up to ~30s."""
    conn = _connect()
    conn.execute("UPDATE portal_accounts SET totp_last_counter=? WHERE id=?", (counter, account_id))
    _commit_with_retry(conn)
    conn.close()


def delete_portal_account(account_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM portal_accounts WHERE id=?", (account_id,))
    _commit_with_retry(conn)
    conn.close()


# ── Portal library (completed recordings, scoped to one person) ────────────
# See set_movie_source_recording_profile's docstring for how
# movie_sources.recording_profile_id / episode_sources.recording_profile_id
# get populated (dispatcharr_dvr_importer's Phase 2) and its single-owner
# caveat when a recording matched more than one profile.

def list_portal_library_movies(provider_id: int, dispatcharr_user_id: int) -> list[dict]:
    """category_name is deliberately NOT "whichever VOD category this movie
    happens to sit in" -- a movie can be in the shared curated pool AND be
    this person's DVR recording/backfill at once, with its own unrelated
    category placements from that general import (e.g. an "Emby TV Shows"
    manual category from years ago). Grouping by any-placement-that-wins-a-
    sort_order-tie leaked exactly that kind of irrelevant, un-approved
    category into the portal -- real bug found live 2026-07-29, caught
    before ship. This resolves the actual DVR-relevant category instead:
    the recording rule that captured this specific source
    (dvr_recording_profiles.target_movie_category_id, joined via
    ms.recording_profile_id), or, for a backfilled/pointer source (which
    reuses an existing non-DVR provider's source row and so has no
    recording_profile_id of its own -- see dispatcharr_dvr_importer.
    _apply_pointer_backfill), this person's own configured default
    (dvr_user_limits.default_movie_category_id). Never falls through to the
    general movie_category_placements table at all."""
    conn = _connect()
    rows = conn.execute("""
        SELECT DISTINCT m.id, m.name, m.year, m.poster_url, m.duration_secs, m.description,
               ms.file_size_bytes, ms.added_at,
               COALESCE(
                   (SELECT c.name FROM dvr_recording_profiles p JOIN categories c ON c.id = p.target_movie_category_id
                    WHERE p.id = ms.recording_profile_id),
                   (SELECT c.name FROM dvr_user_limits ul JOIN categories c ON c.id = ul.default_movie_category_id
                    WHERE ul.provider_id = ? AND ul.dispatcharr_user_id = ?)
               ) AS category_name
        FROM movies m
        JOIN movie_sources ms ON ms.movie_id = m.id
        JOIN movie_source_owners mso ON mso.movie_source_id = ms.id
        WHERE mso.dispatcharr_user_id = ?
        ORDER BY m.name
    """, (provider_id, dispatcharr_user_id, dispatcharr_user_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_portal_library_episodes(provider_id: int, dispatcharr_user_id: int) -> list[dict]:
    """category_name: same reasoning and same bug fix as
    list_portal_library_movies -- resolves the DVR-relevant category
    (dvr_recording_profiles.target_series_category_id via
    es.recording_profile_id, falling back to this person's
    dvr_user_limits.default_series_category_id for a backfilled/pointer
    episode), never the general series_category_placements table."""
    conn = _connect()
    rows = conn.execute("""
        SELECT DISTINCT e.id, e.name, e.description, e.season_number, e.episode_number, e.duration_secs,
               es.file_size_bytes, es.added_at,
               s.id AS series_id, s.name AS series_name, s.poster_url AS series_poster_url, s.description AS series_description,
               COALESCE(
                   (SELECT c.name FROM dvr_recording_profiles p JOIN categories c ON c.id = p.target_series_category_id
                    WHERE p.id = es.recording_profile_id),
                   (SELECT c.name FROM dvr_user_limits ul JOIN categories c ON c.id = ul.default_series_category_id
                    WHERE ul.provider_id = ? AND ul.dispatcharr_user_id = ?)
               ) AS category_name
        FROM episodes e
        JOIN series s ON s.id = e.series_id
        JOIN episode_sources es ON es.episode_id = e.id
        JOIN episode_source_owners eso ON eso.episode_source_id = es.id
        WHERE eso.dispatcharr_user_id = ?
        ORDER BY s.name, e.season_number, e.episode_number
    """, (provider_id, dispatcharr_user_id, dispatcharr_user_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def movie_owned_by_portal_user(movie_id: int, dispatcharr_user_id: int) -> bool:
    conn = _connect()
    row = conn.execute("""
        SELECT 1 FROM movie_sources ms
        JOIN movie_source_owners mso ON mso.movie_source_id = ms.id
        WHERE ms.movie_id = ? AND mso.dispatcharr_user_id = ? LIMIT 1
    """, (movie_id, dispatcharr_user_id)).fetchone()
    conn.close()
    return row is not None


def episode_owned_by_portal_user(episode_id: int, dispatcharr_user_id: int) -> bool:
    conn = _connect()
    row = conn.execute("""
        SELECT 1 FROM episode_sources es
        JOIN episode_source_owners eso ON eso.episode_source_id = es.id
        WHERE es.episode_id = ? AND eso.dispatcharr_user_id = ? LIMIT 1
    """, (episode_id, dispatcharr_user_id)).fetchone()
    conn.close()
    return row is not None


def dvr_user_disk_usage_bytes(provider_id: int, dispatcharr_user_id: int) -> dict:
    """A person's usage = sum of file_size_bytes for every movie/episode
    currently placed in any category that any of their profiles targets --
    deliberately reuses movie_category_placements/series_category_placements
    (the same tables Manage Categories/TMDB Lists already read) instead of a
    separate placement-audit table, so usage is directly auditable: whatever
    an admin sees sitting in this person's category is exactly what's being
    summed. Per the fan-out design (c8c6139) and the user's own explicit
    call, a recording shared with someone else's profile still counts in
    full here -- no splitting.

    Splits into actual_bytes (local_file_path IS NOT NULL -- a real local
    copy VOD Manager is actually storing, DVR-recorded or downloaded-for-
    backfill) and virtual_bytes (local_file_path IS NULL -- a backfilled
    pointer into another provider's own stream, nothing stored locally,
    but the explicit design choice -- per the user, 2026-07-27 -- is to
    still count it against quota/retention exactly as if it were real, so
    a person's limit reflects how much library they're accumulating, not
    just how many bytes VOD Manager happens to be storing on their behalf.
    total_bytes (their sum) is what quota enforcement and retention should
    always compare against; actual/virtual are for display, so an admin
    can tell "how much disk is this really costing me" apart from "how
    much library does this person have" when the two diverge."""
    conn = _connect()
    profiles = conn.execute(
        "SELECT target_movie_category_id, target_series_category_id FROM dvr_recording_profiles "
        "WHERE provider_id=? AND dispatcharr_user_id=?",
        (provider_id, dispatcharr_user_id),
    ).fetchall()
    movie_cat_ids = {p["target_movie_category_id"] for p in profiles if p["target_movie_category_id"]}
    series_cat_ids = {p["target_series_category_id"] for p in profiles if p["target_series_category_id"]}
    # A true single (no profile at all) still lands in this person's own
    # default_movie/series_category_id (dispatcharr_dvr_importer's fallback
    # placement, added 2026-07-28) -- without also counting those categories
    # here, anyone who only ever records singles (e.g. emby, 0 profiles) has
    # movie_cat_ids/series_cat_ids permanently empty and usage always reads
    # 0 no matter how much content is actually sitting in their categories.
    # Real gap found live 2026-07-28 right after the placement fix shipped.
    limit_row = conn.execute(
        "SELECT default_movie_category_id, default_series_category_id FROM dvr_user_limits "
        "WHERE provider_id=? AND dispatcharr_user_id=?",
        (provider_id, dispatcharr_user_id),
    ).fetchone()
    if limit_row:
        if limit_row["default_movie_category_id"]:
            movie_cat_ids.add(limit_row["default_movie_category_id"])
        if limit_row["default_series_category_id"]:
            series_cat_ids.add(limit_row["default_series_category_id"])
    actual, virtual = 0, 0
    if movie_cat_ids:
        placeholders = ",".join("?" * len(movie_cat_ids))
        row = conn.execute(
            f"""SELECT
                    COALESCE(SUM(CASE WHEN ms.local_file_path IS NOT NULL THEN ms.file_size_bytes ELSE 0 END), 0) AS actual,
                    COALESCE(SUM(CASE WHEN ms.local_file_path IS NULL THEN ms.file_size_bytes ELSE 0 END), 0) AS virtual
                FROM movie_category_placements mcp
                JOIN movie_sources ms ON ms.movie_id = mcp.movie_id
                WHERE mcp.category_id IN ({placeholders})""",
            tuple(movie_cat_ids),
        ).fetchone()
        actual += row["actual"]
        virtual += row["virtual"]
    if series_cat_ids:
        placeholders = ",".join("?" * len(series_cat_ids))
        row = conn.execute(
            f"""SELECT
                    COALESCE(SUM(CASE WHEN es.local_file_path IS NOT NULL THEN es.file_size_bytes ELSE 0 END), 0) AS actual,
                    COALESCE(SUM(CASE WHEN es.local_file_path IS NULL THEN es.file_size_bytes ELSE 0 END), 0) AS virtual
                FROM series_category_placements scp
                JOIN episodes e ON e.series_id = scp.series_id
                JOIN episode_sources es ON es.episode_id = e.id
                WHERE scp.category_id IN ({placeholders})""",
            tuple(series_cat_ids),
        ).fetchone()
        actual += row["actual"]
        virtual += row["virtual"]
    conn.close()
    return {"actual_bytes": actual, "virtual_bytes": virtual, "total_bytes": actual + virtual}


def find_retention_candidates(provider_id: int, dispatcharr_user_id: int) -> dict:
    """Dry-run scan for a person's retention policy (dvr_user_limits.
    retention_max_age_days / retention_max_episodes_per_show, opt-in, both
    nullable) -- returns candidates, deletes nothing. See
    apply_retention_deletions for the actual delete step, a deliberately
    separate explicit action (matches the Orphan Checker's scan-then-delete
    pattern already established in this app) so an admin reviews exactly
    what would go before anything does.

    Only ever proposes a movie/episode whose ONLY source is this DVR
    provider -- one that's also available from another provider is left
    alone, since deleting it here would silently orphan this provider's
    placement while a different provider's copy became the sole survivor,
    not something a single person's retention policy should be able to
    trigger on content another provider still actively serves.

    Same category-resolution as dvr_user_disk_usage_bytes above -- a
    person's DVR-managed content is whatever's placed in the categories
    their own recording profiles target, not a separate tracked set."""
    limit_row = get_dvr_user_limit(provider_id, dispatcharr_user_id)
    if not limit_row:
        return {"movies": [], "episodes": []}
    max_age_days = limit_row.get("retention_max_age_days")
    max_episodes_per_show = limit_row.get("retention_max_episodes_per_show")
    if not max_age_days and not max_episodes_per_show:
        return {"movies": [], "episodes": []}

    conn = _connect()
    profiles = conn.execute(
        "SELECT target_movie_category_id, target_series_category_id FROM dvr_recording_profiles "
        "WHERE provider_id=? AND dispatcharr_user_id=?",
        (provider_id, dispatcharr_user_id),
    ).fetchall()
    movie_cat_ids = {p["target_movie_category_id"] for p in profiles if p["target_movie_category_id"]}
    series_cat_ids = {p["target_series_category_id"] for p in profiles if p["target_series_category_id"]}

    movie_candidates = []
    if max_age_days and movie_cat_ids:
        placeholders = ",".join("?" * len(movie_cat_ids))
        cutoff = str(time.time() - max_age_days * 86400)
        rows = conn.execute(
            f"""SELECT DISTINCT m.id, m.name, m.year, m.created_at,
                       (SELECT COUNT(*) FROM movie_sources WHERE movie_id=m.id) AS source_count,
                       (SELECT ms.id FROM movie_sources ms WHERE ms.movie_id=m.id LIMIT 1) AS source_id,
                       (SELECT ms.provider_id FROM movie_sources ms WHERE ms.movie_id=m.id LIMIT 1) AS source_provider_id
                FROM movies m
                JOIN movie_category_placements mcp ON mcp.movie_id = m.id
                WHERE mcp.category_id IN ({placeholders}) AND m.created_at < ?""",
            (*movie_cat_ids, cutoff),
        ).fetchall()
        for r in rows:
            if r["source_count"] == 1 and r["source_provider_id"] == provider_id:
                movie_candidates.append({
                    "movie_id": r["id"], "source_id": r["source_id"], "name": r["name"], "year": r["year"],
                    "created_at": r["created_at"], "reason": "age",
                })

    episode_candidates = []
    if series_cat_ids:
        placeholders = ",".join("?" * len(series_cat_ids))
        series_rows = conn.execute(
            f"SELECT DISTINCT series_id, name FROM series_category_placements scp "
            f"JOIN series s ON s.id = scp.series_id WHERE category_id IN ({placeholders})",
            tuple(series_cat_ids),
        ).fetchall()
        cutoff = str(time.time() - max_age_days * 86400) if max_age_days else None
        for sr in series_rows:
            series_id = sr["series_id"]
            eps = conn.execute(
                """SELECT e.id, e.name, e.season_number, e.episode_number, e.created_at,
                          (SELECT COUNT(*) FROM episode_sources WHERE episode_id=e.id) AS source_count,
                          (SELECT es.id FROM episode_sources es WHERE es.episode_id=e.id LIMIT 1) AS source_id,
                          (SELECT es.provider_id FROM episode_sources es WHERE es.episode_id=e.id LIMIT 1) AS source_provider_id
                   FROM episodes e WHERE e.series_id=? ORDER BY e.created_at DESC""",
                (series_id,),
            ).fetchall()
            eligible = [e for e in eps if e["source_count"] == 1 and e["source_provider_id"] == provider_id]
            to_delete_ids = set()
            if max_episodes_per_show and len(eligible) > max_episodes_per_show:
                to_delete_ids.update(e["id"] for e in eligible[max_episodes_per_show:])
            if cutoff:
                to_delete_ids.update(e["id"] for e in eligible if e["created_at"] < cutoff)
            for e in eligible:
                if e["id"] in to_delete_ids:
                    episode_candidates.append({
                        "episode_id": e["id"], "source_id": e["source_id"], "series_name": sr["name"],
                        "season_number": e["season_number"], "episode_number": e["episode_number"],
                        "name": e["name"], "created_at": e["created_at"],
                    })
    conn.close()
    return {"movies": movie_candidates, "episodes": episode_candidates}


def apply_retention_deletions(movies: list[dict], episodes: list[dict]) -> dict:
    """The confirm step after find_retention_candidates -- takes exactly the
    {movie_id, source_id} / {episode_id, source_id} pairs an admin reviewed
    and approved (never re-scans/re-decides here, so what gets deleted is
    exactly what was shown), and removes each one's DVR source via the same
    delete_movie_source/delete_episode_source already used elsewhere --
    both already purge the parent row once it's sourceless (see
    _purge_if_sourceless_movie/_purge_if_sourceless_episode), so there's no
    separate delete-the-row step needed here.

    Does NOT touch the underlying recording file on disk -- only removes
    it from VOD Manager's own catalog. Real filesystem cleanup is a
    separate, not-yet-built concern."""
    for m in movies:
        delete_movie_source(m["movie_id"], m["source_id"])
    for e in episodes:
        delete_episode_source(e["episode_id"], e["source_id"])
    return {"deleted_movies": len(movies), "deleted_episodes": len(episodes)}


def set_provider_priority(provider_id: int, priority: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET priority=?, updated_at=? WHERE id=?", (priority, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_name(provider_id: int, name: str) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET name=?, updated_at=? WHERE id=?", (name, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_base_url(provider_id: int, base_url: str) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET base_url=?, updated_at=? WHERE id=?", (base_url, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_max_streams(provider_id: int, max_streams: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET max_streams=?, updated_at=? WHERE id=?", (max_streams, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_shared_limit(provider_id: int, shared_connection_limit: int | None) -> None:
    """The real provider's true total connection cap, shared across every
    live-TV account on any Dispatcharr instance plus our own VOD streaming
    -- see xc_server.py's _try_reserve_capacity(). Which specific live-TV accounts
    count toward it is managed separately (provider_live_accounts, since a
    provider can have one on more than one Dispatcharr instance)."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET shared_connection_limit=?, updated_at=? WHERE id=?",
        (shared_connection_limit, _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_custom_user_agent(provider_id: int, custom_user_agent: str | None) -> None:
    """Overrides the default browser User-Agent (see vod_importer.py's
    _UPSTREAM_HEADERS) for just this provider -- most providers work fine
    with the shared default; this is only needed if one turns out to be
    pickier (blocks even a normal browser UA, or wants something else
    entirely). None/empty clears the override and falls back to the default."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET custom_user_agent=?, updated_at=? WHERE id=?",
        (custom_user_agent or None, _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_auto_create_categories(provider_id: int, enabled: bool) -> None:
    """Opt-in per provider (default off, unchanged behavior for anyone who
    doesn't touch this) -- see vod_importer.auto_create_categories_for_provider
    for what turning it on actually does on the next import."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET auto_create_categories=?, updated_at=? WHERE id=?",
        (int(enabled), _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_import_exclude_categories(provider_id: int, category_names: list[str], exclude_uncategorized: bool = False) -> None:
    """Provider category names (as this provider itself names them, e.g.
    "Movies - Spanish") to auto-archive on import -- unlike the language
    exclusion rules (config.get/save_import_language_exclusion), this is
    per-provider since available categories genuinely differ provider to
    provider. See vod_importer._should_auto_archive.

    exclude_uncategorized (GH issue #7) is a separate switch, not another
    category name -- some providers ship items with no category attached at
    all, which can never appear in category_names since there's no name to
    add."""
    import json
    conn = _connect()
    conn.execute(
        "UPDATE providers SET import_exclude_categories=?, import_exclude_uncategorized=?, updated_at=? WHERE id=?",
        (json.dumps([c.strip() for c in category_names if c.strip()]), int(exclude_uncategorized), _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_archive_new_categories(provider_id: int, enabled: bool) -> None:
    """Opt-in per provider (default off, mirrors auto_create_categories): a
    category name never seen before on this provider's own last-known set
    (providers.known_import_categories) gets auto-archived on the import
    that discovers it, same as Dispatcharr's own VOD provider category
    behavior (GH issue #5). See vod_importer.import_provider_catalog."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET archive_new_categories=?, updated_at=? WHERE id=?",
        (int(enabled), _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_known_import_categories(provider_id: int, category_names: list[str]) -> None:
    """Bookkeeping for archive_new_categories -- the full set of category
    names this provider has ever reported, so the next import can tell
    which ones are genuinely new. Written unconditionally every import
    (whether or not archive_new_categories is on) so turning the setting on
    later doesn't retroactively treat the entire existing category list as
    'new' and archive everything."""
    import json
    conn = _connect()
    conn.execute(
        "UPDATE providers SET known_import_categories=?, updated_at=? WHERE id=?",
        (json.dumps(sorted({c.strip() for c in category_names if c.strip()})), _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_dispatcharr_profile(provider_id: int, profile_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET dispatcharr_profile_id=?, updated_at=? WHERE id=?", (profile_id, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def _parse_json_list(value: str | None) -> list:
    import json
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def get_provider(provider_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["password"] = decrypt_value(d["password"])
    d["import_exclude_categories"] = _parse_json_list(d.get("import_exclude_categories"))
    d["known_import_categories"] = _parse_json_list(d.get("known_import_categories"))
    return d


def find_providers_sharing_credentials(provider_id: int) -> list[dict]:
    """Mirrors Dispatcharr's own credential-fingerprint connection pooling
    (confirmed via reading its real source, 2026-07-29:
    apps/m3u/connection_pool.py's server_group_connections:{group_id}:
    {fingerprint} key, where fingerprint = SHA-256(username, password) --
    Dispatcharr pools ITS OWN connection accounting across every M3U
    account/profile that shares one real login, since they're the same
    physical connection pool underneath no matter how many separate
    Dispatcharr-side objects represent it.

    VOD Manager needs the identical treatment for its own multi-provider
    workaround (splitting one real "5x1"-style account into 5 separate
    provider rows, each independently scoped to its own Dispatcharr
    profile, per set_provider_live_account) -- without pooling, each of
    those 5 providers tracks VOD Manager's own concurrent-stream usage
    (xc_server._active_vod_streams) independently, undercounting the real
    shared total the moment more than one of them streams at once, even
    though they're drawing on the exact same real upstream login.

    Fingerprint match is username+password only (not base_url), same scope
    Dispatcharr's own fingerprint uses -- deliberately not widened, so this
    stays a true mirror of behavior already proven live rather than a new
    design. Only returns other ACTIVE providers -- a deactivated sibling
    isn't drawing on the shared pool right now."""
    conn = _connect()
    rows = conn.execute("SELECT id, username, password FROM providers WHERE is_active=1").fetchall()
    conn.close()
    this = next((dict(r) for r in rows if r["id"] == provider_id), None)
    if not this:
        return []
    this_password = decrypt_value(this["password"])
    siblings = []
    for r in rows:
        if r["id"] == provider_id:
            continue
        if r["username"] != this["username"]:
            continue
        if decrypt_value(r["password"]) == this_password:
            siblings.append(r["id"])
    return [get_provider(pid) for pid in siblings]


_PROVIDER_COUNTS_CACHE: dict = {"counts": None, "computed_at": 0.0}
_PROVIDER_COUNTS_TTL_SECS = 60.0


def _provider_counts(conn) -> dict[int, dict]:
    """Per-provider content/link counts. series_count is DISTINCT series with
    at least one episode actually sourced from this provider (not "series
    this provider happened to create the row for" via series.import_
    provider_id, which undercounts any series a later provider's episodes
    merged into but didn't originally create) -- episode_count is the raw
    episode_sources row count, a different, larger number by design.

    series_count's join+COUNT(DISTINCT) has no selective filter to index
    against (every episode_sources row participates), so at real catalog
    size it's a multi-second hash join. Provider content counts don't change
    meaningfully faster than imports/enrichment runs do, so a short
    in-process TTL cache turns "every /vod/providers/ call recomputes this"
    into "at most once a minute" without restructuring the query."""
    now = time.time()
    if _PROVIDER_COUNTS_CACHE["counts"] is not None and now - _PROVIDER_COUNTS_CACHE["computed_at"] < _PROVIDER_COUNTS_TTL_SECS:
        return _PROVIDER_COUNTS_CACHE["counts"]

    movie_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM movie_sources GROUP BY provider_id"
    ).fetchall()}
    series_counts = {r["provider_id"]: r["c"] for r in conn.execute("""
        SELECT es.provider_id, COUNT(DISTINCT e.series_id) c
        FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        GROUP BY es.provider_id
    """).fetchall()}
    episode_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM episode_sources GROUP BY provider_id"
    ).fetchall()}
    synced_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM provider_sync_profiles GROUP BY provider_id"
    ).fetchall()}
    live_account_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM provider_live_accounts GROUP BY provider_id"
    ).fetchall()}
    sub_account_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM provider_sub_accounts GROUP BY provider_id"
    ).fetchall()}

    counts: dict[int, dict] = {}
    for pid in set(movie_counts) | set(series_counts) | set(episode_counts) | set(synced_counts) | set(live_account_counts) | set(sub_account_counts):
        counts[pid] = {
            "movie_count": movie_counts.get(pid, 0),
            "series_count": series_counts.get(pid, 0),
            "episode_count": episode_counts.get(pid, 0),
            "synced_connection_count": synced_counts.get(pid, 0),
            "live_account_count": live_account_counts.get(pid, 0),
            "sub_account_count": sub_account_counts.get(pid, 0),
        }
    _PROVIDER_COUNTS_CACHE["counts"] = counts
    _PROVIDER_COUNTS_CACHE["computed_at"] = now
    return counts


def list_providers() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY name").fetchall()]
    for r in rows:
        r["password"] = decrypt_value(r["password"])
        r["import_exclude_categories"] = _parse_json_list(r.get("import_exclude_categories"))
    counts = _provider_counts(conn)
    conn.close()
    for p in rows:
        c = counts.get(p["id"], {})
        p["movie_count"] = c.get("movie_count", 0)
        p["series_count"] = c.get("series_count", 0)
        p["episode_count"] = c.get("episode_count", 0)
        p["synced_connection_count"] = c.get("synced_connection_count", 0)
        p["live_account_count"] = c.get("live_account_count", 0)
        p["sub_account_count"] = c.get("sub_account_count", 0)
    return rows


def set_provider_active(provider_id: int, is_active: bool) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET is_active=?, updated_at=? WHERE id=?", (int(is_active), _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def _delete_file_if_present(file_path: str | None) -> bool:
    """Actually removes a DVR recording's file from disk -- nothing in this
    codebase did this before 2026-07-28 for ANY delete path, admin or
    portal, so files were silently orphaned on every delete, forever. Only
    ever called with a path a caller has already confirmed (within the same
    transaction) no other movie_sources/episode_sources row still
    references -- deleting the row is what makes this safe to call, never
    the other way around. missing_ok=True since a file can legitimately
    already be gone (a previous partial cleanup, or Phase 1a's shared-mount
    reference having been removed by Dispatcharr itself); a permission or
    other OS-level failure is logged, not raised -- the DB row is already
    gone by the time this runs, and failing the whole delete over a
    filesystem hiccup would leave the DB and disk in a WORSE mismatch, not
    a better one."""
    if not file_path:
        return False
    try:
        Path(file_path).unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("[vod_db] couldn't delete file %s: %s", file_path, exc)
        return False


def _purge_if_sourceless_movie(conn: sqlite3.Connection, movie_id: int) -> None:
    """A movie with zero sources from any provider can't actually be played,
    but would still show up as if it were real, available content in
    Dispatcharr's catalog and any downstream IPTV player -- worse than not
    listing it at all. Called after removing what might have been a movie's
    last source, whether that's one source, or a whole provider's worth."""
    remaining = conn.execute("SELECT COUNT(*) c FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["c"]
    if remaining == 0:
        conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))


def _purge_if_sourceless_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    """A specific episode can go sourceless (its one source deleted, or
    belonged to a now-deleted provider) while the series it belongs to
    still has other episodes with real sources from other providers -- the
    series survives, but this one episode is dead weight, same reasoning as
    _purge_if_sourceless_movie."""
    remaining = conn.execute("SELECT COUNT(*) c FROM episode_sources WHERE episode_id=?", (episode_id,)).fetchone()["c"]
    if remaining == 0:
        conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))


def _purge_if_sourceless_series(conn: sqlite3.Connection, series_id: int, orphaned_provider_id: int | None = None) -> None:
    """Series equivalent of _purge_if_sourceless_movie -- deletes the whole
    series only once none of its episodes have any source left at all (see
    _purge_if_sourceless_episode for the per-episode version). If the series
    survives (still has sources from other providers) but its cached
    import_provider_id -- the "ask this provider for episode details"
    reference used by enrich_series, a plain column with no real FK, not a
    real source record -- pointed at the provider that just lost its
    sources, clear it too, so a later enrich attempt fails cleanly instead
    of silently hitting a provider that's no longer there."""
    remaining = conn.execute("""
        SELECT COUNT(*) c FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        WHERE e.series_id=?
    """, (series_id,)).fetchone()["c"]
    if remaining == 0:
        conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    elif orphaned_provider_id is not None:
        conn.execute(
            "UPDATE series SET import_provider_id=NULL, import_provider_series_id=NULL WHERE id=? AND import_provider_id=?",
            (series_id, orphaned_provider_id),
        )


def delete_provider(provider_id: int) -> None:
    """Hard delete. movie_sources/episode_sources for this provider cascade
    via FK (ON DELETE CASCADE). Anything left with zero sources from any
    provider afterward is purged too -- see _purge_if_sourceless_movie/
    episode/series.

    series.import_provider_id is the one exception with no ON DELETE
    CASCADE/SET NULL on its FK (an existing schema gap -- fixing the
    constraint itself would need a full table rebuild, since SQLite can't
    ALTER a foreign key in place, too risky to do blindly against a live
    production database). Any series still pointing at this provider as its
    episode-detail source gets explicitly cleared first instead -- the same
    "no working way to fetch episode detail" state _purge_if_sourceless_series
    and bulk_import_series already know how to recover from when a later
    import assigns a new provider.

    Holds _WRITE_LOCK for the whole operation -- this deletes/purges `movies`
    rows, so it must not interleave with bulk_import_movies/bulk_import_series
    (which key their own writes off finding those rows still present). Found
    live 2026-08-19: this used to run lock-free, and a provider delete racing
    a concurrent import of the OTHER providers deleted a shared-title movie
    between the importer's match and its movie_sources insert, producing a
    flood of "FOREIGN KEY constraint failed" errors that silently dropped
    ~30% of that import."""
    with _WRITE_LOCK:
        conn = _connect()

        # Capture affected movies/episodes/series before the cascade delete
        # removes the only signal (their sources) that would tell us which ones
        # to check.
        affected_movie_ids = [r["movie_id"] for r in conn.execute(
            "SELECT DISTINCT movie_id FROM movie_sources WHERE provider_id=?", (provider_id,)
        ).fetchall()]
        affected_episode_rows = conn.execute("""
            SELECT DISTINCT e.id AS episode_id, e.series_id FROM episode_sources es
            JOIN episodes e ON e.id = es.episode_id
            WHERE es.provider_id=?
        """, (provider_id,)).fetchall()

        conn.execute(
            "UPDATE series SET import_provider_id=NULL, import_provider_series_id=NULL WHERE import_provider_id=?",
            (provider_id,),
        )
        conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))

        logger.info("[vod_db] delete_provider(%s): purging %d movie(s), %d episode(s)",
                    provider_id, len(affected_movie_ids), len(affected_episode_rows))
        for movie_id in affected_movie_ids:
            _purge_if_sourceless_movie(conn, movie_id)
        affected_series_ids = {r["series_id"] for r in affected_episode_rows}
        for row in affected_episode_rows:
            _purge_if_sourceless_episode(conn, row["episode_id"])
        for series_id in affected_series_ids:
            _purge_if_sourceless_series(conn, series_id, orphaned_provider_id=provider_id)

        _commit_with_retry(conn)
        conn.close()
    logger.info("[vod_db] delete_provider(%s): done", provider_id)


# ── Orphan checker ───────────────────────────────────────────────────────────
# Self-service version of the manual investigation that found the original
# bug this exists to prevent recurring: a provider getting deleted (or, more
# subtly, a source silently losing its movie/episode association -- see the
# ON CONFLICT fixes on the bulk_import_* functions above) can leave dead
# rows behind that _purge_if_sourceless_* would have caught at the time, but
# only if delete_provider/delete_movie_source/delete_episode_source was the
# path taken. This re-derives the same two categories from scratch across
# the whole pool, for whatever slips through (a bug elsewhere, a manual DB
# edit, an upgrade from before these functions existed) rather than assuming
# every future gap will go through the choke points already covered.
#
# Deliberately does NOT flag "series with zero episodes yet" as an orphan --
# that's the overwhelming majority of any freshly bulk-imported pool (XC
# episodes are fetched lazily per-series, on demand, by design) and is
# completely normal, not broken. Only a series whose cached
# import_provider_id points at a provider that no longer exists at all is
# genuinely unfixable and worth flagging.

def find_orphans() -> dict:
    conn = _connect()
    valid_provider_ids = {r["id"] for r in conn.execute("SELECT id FROM providers").fetchall()}

    orphaned_series = [dict(r) for r in conn.execute("SELECT id, name, import_provider_id FROM series").fetchall()
                        if r["import_provider_id"] is None or r["import_provider_id"] not in valid_provider_ids]
    sourceless_movies = [dict(r) for r in conn.execute("""
        SELECT m.id, m.name FROM movies m
        LEFT JOIN movie_sources ms ON ms.movie_id = m.id
        WHERE ms.id IS NULL
    """).fetchall()]
    sourceless_episodes = [dict(r) for r in conn.execute("""
        SELECT e.id, e.series_id, e.name FROM episodes e
        LEFT JOIN episode_sources es ON es.episode_id = e.id
        WHERE es.id IS NULL
    """).fetchall()]
    conn.close()

    return {
        "orphaned_series": {"count": len(orphaned_series), "sample": orphaned_series[:20]},
        "sourceless_movies": {"count": len(sourceless_movies), "sample": sourceless_movies[:20]},
        "sourceless_episodes": {"count": len(sourceless_episodes), "sample": sourceless_episodes[:20]},
    }


def find_uncategorized() -> dict:
    """A different concept from find_orphans above (that's sourceless rows;
    this is category-less rows) -- items with real sources that Dispatcharr
    still can't see, because get_movie_export_rows/get_series_export_rows
    inner-join through movie_category_placements/series_category_placements,
    so zero placements means zero export rows, invisible to any XC client
    no matter how many real sources the item has.

    Splits into two buckets because they need different remedies: items the
    catch-all sweep (vod_importer.refresh_catchall_categories) SHOULD have
    caught but hasn't yet (a real gap to fix or wait out), vs. items
    needs_year_review=1 blocks from every placement path entirely, including
    the catch-all, until a human resolves them in the review queue -- no
    sweep can or should silently override that safeguard."""
    conn = _connect()
    uncategorized_movies = [dict(r) for r in conn.execute("""
        SELECT m.id, m.name, m.year, m.needs_year_review FROM movies m
        LEFT JOIN movie_category_placements p ON p.movie_id = m.id
        WHERE p.id IS NULL AND m.review_excluded = 0
    """).fetchall()]
    uncategorized_series = [dict(r) for r in conn.execute("""
        SELECT s.id, s.name, s.year, s.needs_year_review FROM series s
        LEFT JOIN series_category_placements p ON p.series_id = s.id
        WHERE p.id IS NULL AND s.review_excluded = 0
    """).fetchall()]
    conn.close()

    movies_needing_review = [r for r in uncategorized_movies if r["needs_year_review"]]
    series_needing_review = [r for r in uncategorized_series if r["needs_year_review"]]
    movies_gap = [r for r in uncategorized_movies if not r["needs_year_review"]]
    series_gap = [r for r in uncategorized_series if not r["needs_year_review"]]

    return {
        "movies_needing_year_review": {"count": len(movies_needing_review), "sample": movies_needing_review[:20]},
        "series_needing_year_review": {"count": len(series_needing_review), "sample": series_needing_review[:20]},
        "movies_uncategorized": {"count": len(movies_gap), "sample": movies_gap[:20]},
        "series_uncategorized": {"count": len(series_gap), "sample": series_gap[:20]},
    }


def purge_orphans() -> dict:
    """Holds _WRITE_LOCK for the whole operation -- see delete_provider's
    docstring for why an unlocked `movies`/`series` delete here can race a
    concurrent import and cause its inserts to fail with a FOREIGN KEY error."""
    with _WRITE_LOCK:
        conn = _connect()
        valid_provider_ids = {r["id"] for r in conn.execute("SELECT id FROM providers").fetchall()}

        orphaned_series_ids = [r["id"] for r in conn.execute("SELECT id, import_provider_id FROM series").fetchall()
                                if r["import_provider_id"] is None or r["import_provider_id"] not in valid_provider_ids]
        sourceless_movie_ids = [r["id"] for r in conn.execute("""
            SELECT m.id FROM movies m LEFT JOIN movie_sources ms ON ms.movie_id = m.id WHERE ms.id IS NULL
        """).fetchall()]
        # Episodes belonging to a series about to be deleted anyway don't need a
        # separate delete -- ON DELETE CASCADE handles them. Only ones inside an
        # otherwise-healthy series need to be purged individually.
        sourceless_episode_ids = [r["id"] for r in conn.execute("""
            SELECT e.id FROM episodes e
            LEFT JOIN episode_sources es ON es.episode_id = e.id
            WHERE es.id IS NULL AND e.series_id NOT IN ({})
        """.format(",".join("?" * len(orphaned_series_ids)) if orphaned_series_ids else "NULL"),
            orphaned_series_ids,
        ).fetchall()]

        for sid in orphaned_series_ids:
            conn.execute("DELETE FROM series WHERE id=?", (sid,))
        for mid in sourceless_movie_ids:
            conn.execute("DELETE FROM movies WHERE id=?", (mid,))
        for eid in sourceless_episode_ids:
            conn.execute("DELETE FROM episodes WHERE id=?", (eid,))

        _commit_with_retry(conn)
        conn.close()
    return {
        "series_deleted": len(orphaned_series_ids),
        "movies_deleted": len(sourceless_movie_ids),
        "episodes_deleted": len(sourceless_episode_ids),
    }


# ── Duplicate finder ─────────────────────────────────────────────────────────
# Import matching (bulk_import_movies/bulk_import_series, upsert_movie) keys
# on exact name+year -- a provider that formats the same title slightly
# differently ("Title" vs "Title:") or mislabels the year by one creates a
# second real pool entry instead of matching the existing one (a real case:
# "#AMFAD All My Friends Are Dead" vs "#AMFAD: All My Friends Are Dead",
# both 2024, split into two rows). Same review-before-merge trust pattern as
# Orphan Checker/Needs Review, not an automatic pass -- punctuation
# normalization and year-proximity are high-confidence signals but not
# risk-free, and a bad auto-merge is much harder to notice/undo than a bad
# auto-delete. A confirmed tmdb_id conflict, on the other hand, is treated
# as proof rather than a signal -- see _split_by_tmdb_conflict.

_DUPLICATE_STRIP_RE = re.compile(r"[:;,.'\"’‘“”\-–—]")
_DUPLICATE_WS_RE = re.compile(r"\s+")


def _normalize_title_for_dedup(name: str) -> str:
    stripped = _DUPLICATE_STRIP_RE.sub("", name)
    return _DUPLICATE_WS_RE.sub(" ", stripped).strip().lower()


def _duplicate_ignore_signature(item_ids: list[int]) -> str:
    return ",".join(str(i) for i in sorted(item_ids))


def list_ignored_duplicate_signatures(content_type: str) -> list[str]:
    conn = _connect()
    rows = conn.execute("SELECT signature FROM duplicate_ignores WHERE content_type=?", (content_type,)).fetchall()
    conn.close()
    return [r["signature"] for r in rows]


def ignore_duplicate_group(content_type: str, item_ids: list[int]) -> None:
    """Dismisses a specific cluster (exact set of item ids) as reviewed and
    NOT actually duplicates -- e.g. two unrelated films that happen to
    share a generic title. Keyed on the id set, not just the name, so it
    stops resurfacing on rescan, but if the pool composition around it
    later changes (a new near-year item joins), that's a genuinely
    different cluster with its own signature and gets reviewed fresh
    rather than silently inheriting an old dismissal."""
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO duplicate_ignores (content_type, signature, created_at) VALUES (?,?,?)",
        (content_type, _duplicate_ignore_signature(item_ids), _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def _split_by_year_proximity(items: list[dict]) -> list[list[dict]]:
    """Same (normalized) name doesn't mean same real title if the years are
    too far apart -- two items 2+ years apart are almost always different
    films that happen to share a title, so they never cluster together at
    all. A 1-year gap still clusters (the real-world pattern: a provider
    mislabels a film's year by one). Every item here is guaranteed to have
    a real year -- find_duplicate_groups only ever feeds this year IS NOT
    NULL rows."""
    sorted_items = sorted(items, key=lambda i: i["year"])
    clusters: list[list[dict]] = []
    current: list[dict] = []
    for item in sorted_items:
        if current and abs(item["year"] - current[-1]["year"]) > 1:
            clusters.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        clusters.append(current)
    return clusters


def _split_by_tmdb_conflict(items: list[dict]) -> list[list[dict]]:
    """A confirmed DIFFERENT tmdb_id across two items is positive proof
    they're different real content -- not ambiguity for a human to review,
    so it splits a cluster apart rather than just getting flagged. Items
    with no tmdb_id at all stay genuinely ambiguous (no evidence either
    way) and fold into every id-having subgroup they could still plausibly
    belong to."""
    with_id = [i for i in items if i.get("tmdb_id")]
    without_id = [i for i in items if not i.get("tmdb_id")]
    by_id: dict[str, list[dict]] = {}
    for i in with_id:
        by_id.setdefault(i["tmdb_id"], []).append(i)
    if len(by_id) <= 1:
        return [items]
    return [group + without_id for group in by_id.values()]


def find_duplicate_groups(content_type: str) -> list[dict]:
    """Groups pool entries into candidate duplicate clusters, in three
    passes: (1) same name once cosmetic punctuation/whitespace is stripped
    ("Title" vs "Title:") -- import matching keys on exact name+year, so a
    provider formatting the same title slightly differently creates a
    second real pool entry instead of matching the existing one; (2) within
    that, same-year or adjacent-year, never 2+ years apart (see
    _split_by_year_proximity); (3) within that, split apart again by any
    CONFIRMED tmdb_id conflict (see _split_by_tmdb_conflict). Only years
    we're confident about (year IS NOT NULL) -- pairing on name alone would
    be a much weaker signal and belongs to needs_year_review instead, not
    this scan. A cluster a human already reviewed and dismissed (see
    duplicate_ignores) never resurfaces."""
    table = "movies" if content_type == "movie" else "series"
    id_col = "movie_id" if content_type == "movie" else "series_id"
    placements_table = "movie_category_placements" if content_type == "movie" else "series_category_placements"

    conn = _connect()
    rows = conn.execute(
        f"SELECT id, name, year, tmdb_id, poster_url FROM {table} WHERE year IS NOT NULL AND review_excluded=0"
    ).fetchall()

    by_name: dict[str, list[dict]] = {}
    for r in rows:
        key = _normalize_title_for_dedup(r["name"])
        by_name.setdefault(key, []).append({
            "id": r["id"], "name": r["name"], "year": r["year"],
            "tmdb_id": r["tmdb_id"], "poster_url": r["poster_url"],
        })

    ignored = set(list_ignored_duplicate_signatures(content_type))
    candidate_groups: list[list[dict]] = []
    for items in by_name.values():
        if len(items) < 2:
            continue
        for year_cluster in _split_by_year_proximity(items):
            for sub in _split_by_tmdb_conflict(year_cluster):
                if len(sub) < 2:
                    continue
                if _duplicate_ignore_signature([i["id"] for i in sub]) in ignored:
                    continue
                candidate_groups.append(sub)

    if not candidate_groups:
        conn.close()
        return []

    all_ids = [i["id"] for items in candidate_groups for i in items]
    placeholders = ",".join("?" for _ in all_ids)

    if content_type == "movie":
        src_counts = conn.execute(
            f"SELECT movie_id AS id, COUNT(*) c FROM movie_sources WHERE movie_id IN ({placeholders}) GROUP BY movie_id",
            all_ids,
        ).fetchall()
    else:
        src_counts = conn.execute(f"""
            SELECT e.series_id AS id, COUNT(*) c FROM episode_sources es
            JOIN episodes e ON e.id = es.episode_id
            WHERE e.series_id IN ({placeholders}) GROUP BY e.series_id
        """, all_ids).fetchall()
    src_count_by_id = {r["id"]: r["c"] for r in src_counts}

    cat_counts = conn.execute(
        f"SELECT {id_col} AS id, COUNT(*) c FROM {placements_table} WHERE {id_col} IN ({placeholders}) GROUP BY {id_col}",
        all_ids,
    ).fetchall()
    cat_count_by_id = {r["id"]: r["c"] for r in cat_counts}

    # A "duplicate" backed by 1 source from a single provider is a very
    # different trust level than one with several sources across several
    # providers -- invisible from source_count alone, so the reviewer sees
    # which providers actually back each candidate, not just how many.
    if content_type == "movie":
        provider_rows = conn.execute(f"""
            SELECT DISTINCT ms.movie_id AS id, p.name AS provider_name
            FROM movie_sources ms JOIN providers p ON p.id = ms.provider_id
            WHERE ms.movie_id IN ({placeholders})
        """, all_ids).fetchall()
    else:
        provider_rows = conn.execute(f"""
            SELECT DISTINCT e.series_id AS id, p.name AS provider_name
            FROM episode_sources es
            JOIN episodes e ON e.id = es.episode_id
            JOIN providers p ON p.id = es.provider_id
            WHERE e.series_id IN ({placeholders})
        """, all_ids).fetchall()
    provider_names_by_id: dict[int, list[str]] = {}
    for r in provider_rows:
        provider_names_by_id.setdefault(r["id"], []).append(r["provider_name"])
    conn.close()

    result = []
    for items in candidate_groups:
        for i in items:
            i["source_count"] = src_count_by_id.get(i["id"], 0)
            i["category_count"] = cat_count_by_id.get(i["id"], 0)
            i["provider_names"] = provider_names_by_id.get(i["id"], [])
        # Most-sourced/most-placed first -- the obvious default "keep" pick.
        items.sort(key=lambda i: (-i["source_count"], -i["category_count"]))
        result.append({"items": items})
    result.sort(key=lambda g: -sum(i["source_count"] for i in g["items"]))
    return result


def merge_duplicate_group(content_type: str, keep_id: int, merge_ids: list[int]) -> dict:
    merged = 0
    for mid in merge_ids:
        if mid == keep_id:
            continue
        if content_type == "movie":
            merge_movie(mid, keep_id)
        else:
            merge_series(mid, keep_id)
        merged += 1
    return {"kept_id": keep_id, "merged_count": merged}


def merge_duplicate_groups_bulk(content_type: str, groups: list[tuple[int, list[int]]], _retry_depth: int = 0) -> dict:
    """Batch counterpart to merge_duplicate_group -- used by the
    /duplicates/merge-confirmed/ bulk actions (GH issue #2's second pass and
    the original all-confirmed-matches merge), which can hand back hundreds
    to low-thousands of groups in one request.

    Real bug found live 2026-08-23, same shape as bulk_import_movies/series'
    own 2026-07-30/07-31 incidents (see _WRITE_LOCK's docstring):
    merge_duplicate_group's per-pair loop called merge_movie/merge_series,
    each of which acquires _WRITE_LOCK, opens its OWN connection, and commits
    individually -- fine for a handful of groups, but a 993-group request
    under concurrent bulk TMDB enrichment (both real concurrent-usage cases,
    not edge cases) took 8+ minutes and then hard-failed with an unhandled
    'database is locked' OperationalError, permanently losing every item
    after the failure point. A single one-connection/one-commit batch isn't
    the fix either -- enrichment's own writes don't go through _WRITE_LOCK,
    so a lone giant transaction is still exposed to the same mid-batch lock
    error, and holding the writer lock continuously for minutes starves
    every other writer exactly like the 2026-07-31 incident this function
    mirrors. So: one connection, one _WRITE_LOCK acquisition, but each
    individual merge wrapped in _item_savepoint (one bad/contended item
    can't lose the rest), periodic commit + lock release/reacquire every
    batch_size items (bounds how long any other writer is blocked), and
    transient 'database is locked' failures collected for one more retry
    pass instead of being treated as permanent losses."""
    items = [(keep_id, from_id) for keep_id, merge_ids in groups for from_id in merge_ids if from_id != keep_id]
    if not items:
        return {"merged_groups": len(groups), "merged_items": 0}

    batch_size = 25
    merged_items = 0
    lock_retry_items: list[tuple[int, int]] = []
    _WRITE_LOCK.acquire()
    try:
        conn = _connect()
        for i, (keep_id, from_id) in enumerate(items):
            try:
                with _item_savepoint(conn):
                    if content_type == "movie":
                        _merge_movie_row(conn, from_id, keep_id)
                    else:
                        _merge_series_row(conn, from_id, keep_id)
                merged_items += 1
            except sqlite3.OperationalError as exc:
                # Same transient-vs-permanent distinction as bulk_import_movies'
                # identical handling -- a lock collision here means another
                # writer (enrichment, a provider sync) briefly held the
                # engine-level write lock, not that this pair is bad.
                if "locked" in str(exc).lower() and _retry_depth < _MAX_LOCK_RETRY_DEPTH:
                    lock_retry_items.append((keep_id, from_id))
                else:
                    logger.warning("[vod_db] merge_duplicate_groups_bulk: skipped keep_id=%s from_id=%s: %s", keep_id, from_id, exc)
            except Exception as exc:
                logger.warning("[vod_db] merge_duplicate_groups_bulk: skipped keep_id=%s from_id=%s: %s", keep_id, from_id, exc)
            finally:
                if (i + 1) % batch_size == 0:
                    _commit_with_retry(conn)
                    _WRITE_LOCK.release()
                    _WRITE_LOCK.acquire()
        _commit_with_retry(conn)
        conn.close()
    finally:
        _WRITE_LOCK.release()

    if lock_retry_items:
        time.sleep(0.5 * (_retry_depth + 1))
        logger.info("[vod_db] merge_duplicate_groups_bulk: retrying %d item(s) after transient lock contention (pass %d/%d)",
                     len(lock_retry_items), _retry_depth + 1, _MAX_LOCK_RETRY_DEPTH)
        retry_groups = [(keep_id, [from_id]) for keep_id, from_id in lock_retry_items]
        retry_result = merge_duplicate_groups_bulk(content_type, retry_groups, _retry_depth=_retry_depth + 1)
        merged_items += retry_result["merged_items"]

    return {"merged_groups": len(groups), "merged_items": merged_items}


# ── XC clients ───────────────────────────────────────────────────────────────
# One credential pair per downstream Dispatcharr instance (or any other XC
# client) allowed to pull this pool. Auto-generated, high-entropy username/
# password rather than anything user-chosen — this is the only thing standing
# between the XC catalog and the open internet if this server is ever reached
# from outside a trusted network, so it needs to be as strong as a real API
# key, not a typed password. See xc_server.py's _authenticate.

def _generate_xc_username() -> str:
    return f"vm-{secrets.token_hex(4)}"


def _generate_xc_password() -> str:
    return secrets.token_urlsafe(32)


def create_xc_client(label: str, ip_allowlist: str | None = None) -> dict:
    username = _generate_xc_username()
    password = _generate_xc_password()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO xc_clients (label, username, password, enabled, ip_allowlist, created_at) VALUES (?,?,?,1,?,?)",
        (label, username, encrypt_value(password), ip_allowlist, _now()),
    )
    client_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return get_xc_client(client_id)


def list_xc_clients() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM xc_clients ORDER BY created_at ASC").fetchall()]
    conn.close()
    for r in rows:
        r["password"] = decrypt_value(r["password"])
    return rows


def list_enabled_xc_clients() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM xc_clients WHERE enabled=1 ORDER BY created_at ASC").fetchall()]
    conn.close()
    for r in rows:
        r["password"] = decrypt_value(r["password"])
    return rows


def get_xc_client(client_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM xc_clients WHERE id=?", (client_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["password"] = decrypt_value(d["password"])
    return d


def get_default_xc_client() -> dict | None:
    """The oldest enabled client -- used only where a single representative
    credential pair is needed (e.g. building a copy/preview URL in the UI),
    not for real auth decisions. Any enabled client's credentials work
    identically for that purpose since they all see the same pool."""
    conn = _connect()
    row = conn.execute("SELECT * FROM xc_clients WHERE enabled=1 ORDER BY created_at ASC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["password"] = decrypt_value(d["password"])
    return d


def update_xc_client(
    client_id: int, label: str | None = None, enabled: bool | None = None,
    ip_allowlist: str | None = None, clear_ip_allowlist: bool = False,
    category_allowlist: str | None = None, clear_category_allowlist: bool = False,
) -> None:
    conn = _connect()
    if label is not None:
        conn.execute("UPDATE xc_clients SET label=? WHERE id=?", (label, client_id))
    if enabled is not None:
        conn.execute("UPDATE xc_clients SET enabled=? WHERE id=?", (int(enabled), client_id))
    if clear_ip_allowlist:
        conn.execute("UPDATE xc_clients SET ip_allowlist=NULL WHERE id=?", (client_id,))
    elif ip_allowlist is not None:
        conn.execute("UPDATE xc_clients SET ip_allowlist=? WHERE id=?", (ip_allowlist, client_id))
    if clear_category_allowlist:
        conn.execute("UPDATE xc_clients SET category_allowlist=NULL WHERE id=?", (client_id,))
    elif category_allowlist is not None:
        conn.execute("UPDATE xc_clients SET category_allowlist=? WHERE id=?", (category_allowlist, client_id))
    _commit_with_retry(conn)
    conn.close()


def regenerate_xc_client_secret(client_id: int) -> dict:
    password = _generate_xc_password()
    conn = _connect()
    conn.execute("UPDATE xc_clients SET password=? WHERE id=?", (encrypt_value(password), client_id))
    _commit_with_retry(conn)
    conn.close()
    return get_xc_client(client_id)


def delete_xc_client(client_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM xc_clients WHERE id=?", (client_id,))
    _commit_with_retry(conn)
    conn.close()


def record_xc_client_seen(client_id: int, ip: str) -> None:
    conn = _connect()
    conn.execute("UPDATE xc_clients SET last_seen_at=?, last_seen_ip=? WHERE id=?", (_now(), ip, client_id))
    _commit_with_retry(conn)
    conn.close()


# ── Dispatcharr connections ─────────────────────────────────────────────────
# The other side of running against multiple Dispatcharr instances (see
# xc_clients above, which is who's allowed to *pull from* VOD Manager): this
# is who VOD Manager itself *reaches out to*, for two purposes that used to
# assume there was only ever one such instance --
#   1. vod_sync.py pushes each provider's max_streams into a Dispatcharr
#      account's connection-limit profiles (vod_relay_account_id: which
#      account on this connection is the one pointing back at VOD Manager).
#   2. xc_server.py's shared-connection-limit coordination (_try_reserve_capacity)
#      checks live-TV viewer counts against a real provider's total cap --
#      see provider_live_accounts below, since a single real provider can
#      have its own separate native live-TV account on more than one
#      Dispatcharr instance, all drawing from the same real connection pool.

def create_dispatcharr_connection(label: str, url: str, token: str) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO dispatcharr_connections (label, url, token, created_at) VALUES (?,?,?,?)",
        (label, url.rstrip("/"), encrypt_value(token), _now()),
    )
    connection_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return connection_id


def list_dispatcharr_connections() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM dispatcharr_connections ORDER BY created_at ASC").fetchall()]
    conn.close()
    for r in rows:
        r["token"] = decrypt_value(r["token"])
    return rows


def get_dispatcharr_connection(connection_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM dispatcharr_connections WHERE id=?", (connection_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["token"] = decrypt_value(d["token"])
    return d


def update_dispatcharr_connection(
    connection_id: int, label: str | None = None, url: str | None = None,
    token: str | None = None, vod_relay_account_id: int | None = None,
    clear_vod_relay_account_id: bool = False,
) -> None:
    conn = _connect()
    if label is not None:
        conn.execute("UPDATE dispatcharr_connections SET label=? WHERE id=?", (label, connection_id))
    if url is not None:
        conn.execute("UPDATE dispatcharr_connections SET url=? WHERE id=?", (url.rstrip("/"), connection_id))
    if token is not None:
        conn.execute("UPDATE dispatcharr_connections SET token=? WHERE id=?", (encrypt_value(token), connection_id))
    if clear_vod_relay_account_id:
        conn.execute("UPDATE dispatcharr_connections SET vod_relay_account_id=NULL WHERE id=?", (connection_id,))
    elif vod_relay_account_id is not None:
        conn.execute("UPDATE dispatcharr_connections SET vod_relay_account_id=? WHERE id=?", (vod_relay_account_id, connection_id))
    _commit_with_retry(conn)
    conn.close()


def delete_dispatcharr_connection(connection_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM dispatcharr_connections WHERE id=?", (connection_id,))
    _commit_with_retry(conn)
    conn.close()


# ── Provider live-TV accounts (for shared connection-limit coordination) ────

def list_provider_live_accounts_for_connection(connection_id: int) -> list[dict]:
    """Every provider already linked to ANY Dispatcharr account on this one
    connection -- one query instead of an admin-side N+1 over every
    provider, used by vod_sync's discovery flow to know which real
    Dispatcharr accounts are already imported (and by whom) before offering
    them again."""
    conn = _connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT pla.*, p.name AS provider_name FROM provider_live_accounts pla
        JOIN providers p ON p.id = pla.provider_id
        WHERE pla.dispatcharr_connection_id=?
    """, (connection_id,)).fetchall()]
    conn.close()
    return rows


def list_provider_live_accounts(provider_id: int) -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT pla.*, dc.label AS connection_label FROM provider_live_accounts pla
        JOIN dispatcharr_connections dc ON dc.id = pla.dispatcharr_connection_id
        WHERE pla.provider_id=?
        ORDER BY dc.label
    """, (provider_id,)).fetchall()]
    conn.close()
    return rows


def set_provider_live_account(provider_id: int, connection_id: int, account_id: int, profile_id: int | None = None) -> int:
    """Upsert -- one row per (provider, connection) pair; setting it again
    for the same connection just updates the account id.

    profile_id optionally scopes capacity coordination (xc_server._try_reserve_capacity)
    to ONE Dispatcharr M3U profile within that account instead of the whole
    account -- needed when a single Dispatcharr M3U source actually represents
    several separate real upstream logins (e.g. a provider selling "5x1"
    single-connection accounts, each added to Dispatcharr as its own profile
    under one source). Left null, behavior is unchanged: the whole account's
    viewer count is used, correct for the common case of one real login with
    N connections and no profile split."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO provider_live_accounts (provider_id, dispatcharr_connection_id, dispatcharr_account_id, dispatcharr_profile_id)
           VALUES (?,?,?,?)
           ON CONFLICT(provider_id, dispatcharr_connection_id) DO UPDATE SET
               dispatcharr_account_id=excluded.dispatcharr_account_id,
               dispatcharr_profile_id=excluded.dispatcharr_profile_id""",
        (provider_id, connection_id, account_id, profile_id),
    )
    _commit_with_retry(conn)
    row_id = conn.execute(
        "SELECT id FROM provider_live_accounts WHERE provider_id=? AND dispatcharr_connection_id=?",
        (provider_id, connection_id),
    ).fetchone()["id"]
    conn.close()
    return row_id


def remove_provider_live_account(link_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM provider_live_accounts WHERE id=?", (link_id,))
    _commit_with_retry(conn)
    conn.close()


# ── Provider sub-accounts (vod_manager-4dh) ──────────────────────────────────
# Multiple real logins under one provider entry, matching Dispatcharr's own
# M3U-account-with-profiles model. See provider_sub_accounts' CREATE TABLE
# comment for why capacity is tracked per sub-account with no aggregate --
# xc_server._select_upstream_credentials is where that's actually enforced.

def list_provider_sub_accounts(provider_id: int) -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM provider_sub_accounts WHERE provider_id=? ORDER BY sort_order, id", (provider_id,)
    ).fetchall()]
    conn.close()
    for r in rows:
        r["password"] = decrypt_value(r["password"])
    return rows


def get_provider_sub_account(sub_account_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM provider_sub_accounts WHERE id=?", (sub_account_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["password"] = decrypt_value(d["password"])
    return d


def create_provider_sub_account(
    provider_id: int, label: str, username: str, password: str,
    max_streams: int = 0, sort_order: int = 0,
) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO provider_sub_accounts (provider_id, label, username, password, max_streams, sort_order, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (provider_id, label, username, encrypt_value(password), max_streams, sort_order, _now()),
    )
    _commit_with_retry(conn)
    conn.close()
    return cur.lastrowid


def update_provider_sub_account(
    sub_account_id: int, label: str | None = None, username: str | None = None, password: str | None = None,
    max_streams: int | None = None, is_active: bool | None = None, sort_order: int | None = None,
) -> None:
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if username is not None:
        sets.append("username=?"); params.append(username)
    if password is not None:
        sets.append("password=?"); params.append(encrypt_value(password))
    if max_streams is not None:
        sets.append("max_streams=?"); params.append(max_streams)
    if is_active is not None:
        sets.append("is_active=?"); params.append(int(is_active))
    if sort_order is not None:
        sets.append("sort_order=?"); params.append(sort_order)
    if not sets:
        return
    conn = _connect()
    conn.execute(f"UPDATE provider_sub_accounts SET {', '.join(sets)} WHERE id=?", (*params, sub_account_id))
    _commit_with_retry(conn)
    conn.close()


def delete_provider_sub_account(sub_account_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM provider_sub_accounts WHERE id=?", (sub_account_id,))
    _commit_with_retry(conn)
    conn.close()


def list_provider_sub_account_live_accounts(sub_account_id: int) -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT psla.*, dc.label AS connection_label FROM provider_sub_account_live_accounts psla
        JOIN dispatcharr_connections dc ON dc.id = psla.dispatcharr_connection_id
        WHERE psla.sub_account_id=?
        ORDER BY dc.label
    """, (sub_account_id,)).fetchall()]
    conn.close()
    return rows


def set_provider_sub_account_live_account(sub_account_id: int, connection_id: int, account_id: int, profile_id: int | None = None) -> int:
    """Sub-account equivalent of set_provider_live_account -- see there for
    the upsert-by-(sub_account, connection) shape."""
    conn = _connect()
    conn.execute(
        """INSERT INTO provider_sub_account_live_accounts (sub_account_id, dispatcharr_connection_id, dispatcharr_account_id, dispatcharr_profile_id)
           VALUES (?,?,?,?)
           ON CONFLICT(sub_account_id, dispatcharr_connection_id) DO UPDATE SET
               dispatcharr_account_id=excluded.dispatcharr_account_id,
               dispatcharr_profile_id=excluded.dispatcharr_profile_id""",
        (sub_account_id, connection_id, account_id, profile_id),
    )
    _commit_with_retry(conn)
    row_id = conn.execute(
        "SELECT id FROM provider_sub_account_live_accounts WHERE sub_account_id=? AND dispatcharr_connection_id=?",
        (sub_account_id, connection_id),
    ).fetchone()["id"]
    conn.close()
    return row_id


def remove_provider_sub_account_live_account(link_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM provider_sub_account_live_accounts WHERE id=?", (link_id,))
    _commit_with_retry(conn)
    conn.close()


def merge_providers_into_subaccounts(primary_provider_id: int, other_provider_ids: list[int]) -> dict:
    """vod_manager-q78: migrates N pre-existing separately-created provider
    rows (the old manual workaround for a split "5x1"-style subscription --
    one row per real login, each independently profile-scoped -- from before
    vod_manager-4dh's native sub-accounts existed) into sub-accounts of one
    primary provider. Each other provider becomes one new sub-account under
    primary (label = its own name, its own username/password/
    shared_connection_limit as max_streams, same relative priority as
    sort_order). Its content (movie_sources/episode_sources) is RE-POINTED
    to primary_provider_id, never deleted -- this is a consolidation, not a
    purge, unlike delete_provider. Its provider_live_accounts links become
    provider_sub_account_live_accounts links on the new sub-account, so
    Dispatcharr live-viewer-count capacity coordination isn't lost in the
    move. The now-empty other provider row is then removed via the existing
    delete_provider -- safe to call at that point since everything it would
    otherwise cascade-affect has already been moved off it.

    A source whose provider_stream_id happens to collide with one already on
    the primary provider (astronomically unlikely across two genuinely
    different real logins on two different real subscriptions, but not
    provably impossible) is left in place on the OLD provider rather than
    silently dropped -- reported back in providers_partially_merged so an
    admin can look at what's left before deciding what to do with it. An
    other-provider with any leftover collided sources is NOT deleted."""
    conn = _connect()
    primary = conn.execute("SELECT id, name FROM providers WHERE id=?", (primary_provider_id,)).fetchone()
    if not primary:
        conn.close()
        raise ValueError(f"primary provider {primary_provider_id} not found")

    movie_sources_moved = 0
    episode_sources_moved = 0
    movie_source_collisions = 0
    episode_source_collisions = 0
    live_accounts_migrated = 0
    sub_accounts_created = 0
    providers_removed: list[int] = []
    providers_partially_merged: list[dict] = []

    for other_id in other_provider_ids:
        if other_id == primary_provider_id:
            continue
        other = conn.execute("SELECT * FROM providers WHERE id=?", (other_id,)).fetchone()
        if not other:
            continue
        other = dict(other)

        sub_account_id = create_provider_sub_account(
            primary_provider_id, other["name"], other["username"], decrypt_value(other["password"]),
            max_streams=other.get("shared_connection_limit") or 0, sort_order=other.get("priority") or 0,
        )
        sub_accounts_created += 1

        # Re-pointed one row at a time (not a bulk UPDATE) so a collision on
        # one row doesn't block every other row on this same provider from
        # moving -- see this function's own docstring for the collision path.
        collided_movie_ids = []
        for row in conn.execute("SELECT id FROM movie_sources WHERE provider_id=?", (other_id,)).fetchall():
            try:
                conn.execute("UPDATE movie_sources SET provider_id=? WHERE id=?", (primary_provider_id, row["id"]))
                movie_sources_moved += 1
            except sqlite3.IntegrityError:
                movie_source_collisions += 1
                collided_movie_ids.append(row["id"])
        collided_episode_ids = []
        for row in conn.execute("SELECT id FROM episode_sources WHERE provider_id=?", (other_id,)).fetchall():
            try:
                conn.execute("UPDATE episode_sources SET provider_id=? WHERE id=?", (primary_provider_id, row["id"]))
                episode_sources_moved += 1
            except sqlite3.IntegrityError:
                episode_source_collisions += 1
                collided_episode_ids.append(row["id"])

        for link in conn.execute("SELECT * FROM provider_live_accounts WHERE provider_id=?", (other_id,)).fetchall():
            conn.execute(
                """INSERT INTO provider_sub_account_live_accounts (sub_account_id, dispatcharr_connection_id, dispatcharr_account_id, dispatcharr_profile_id)
                   VALUES (?,?,?,?)
                   ON CONFLICT(sub_account_id, dispatcharr_connection_id) DO UPDATE SET
                       dispatcharr_account_id=excluded.dispatcharr_account_id,
                       dispatcharr_profile_id=excluded.dispatcharr_profile_id""",
                (sub_account_id, link["dispatcharr_connection_id"], link["dispatcharr_account_id"], link["dispatcharr_profile_id"]),
            )
            live_accounts_migrated += 1

        _commit_with_retry(conn)

        if collided_movie_ids or collided_episode_ids:
            providers_partially_merged.append({
                "provider_id": other_id, "name": other["name"],
                "movie_collisions": len(collided_movie_ids), "episode_collisions": len(collided_episode_ids),
            })
        else:
            providers_removed.append(other_id)

    conn.close()

    for pid in providers_removed:
        delete_provider(pid)

    return {
        "sub_accounts_created": sub_accounts_created,
        "movie_sources_moved": movie_sources_moved,
        "episode_sources_moved": episode_sources_moved,
        "movie_source_collisions": movie_source_collisions,
        "episode_source_collisions": episode_source_collisions,
        "live_accounts_migrated": live_accounts_migrated,
        "providers_removed": len(providers_removed),
        "providers_partially_merged": providers_partially_merged,
    }


# ── Provider sync profiles (per-connection Dispatcharr profile id) ──────────
# Which Dispatcharr profile object (on a given connection's VOD-relay
# account) represents this provider's max_streams -- needed per-connection
# since syncing to N Dispatcharr instances means N separate profile objects,
# not one shared id.

def get_provider_sync_profile(provider_id: int, connection_id: int) -> int | None:
    conn = _connect()
    row = conn.execute(
        "SELECT dispatcharr_profile_id FROM provider_sync_profiles WHERE provider_id=? AND dispatcharr_connection_id=?",
        (provider_id, connection_id),
    ).fetchone()
    conn.close()
    return row["dispatcharr_profile_id"] if row else None


def set_provider_sync_profile(provider_id: int, connection_id: int, profile_id: int) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO provider_sync_profiles (provider_id, dispatcharr_connection_id, dispatcharr_profile_id)
           VALUES (?,?,?)
           ON CONFLICT(provider_id, dispatcharr_connection_id) DO UPDATE SET dispatcharr_profile_id=excluded.dispatcharr_profile_id""",
        (provider_id, connection_id, profile_id),
    )
    _commit_with_retry(conn)
    conn.close()


# ── Categories ───────────────────────────────────────────────────────────────

def upsert_category(
    name: str, content_type: str, is_smart: bool = False, sort_order: int = 0,
    rule_json: str | None = None,
) -> int:
    conn = _connect()
    row = conn.execute("SELECT id FROM categories WHERE name = ? AND content_type = ?", (name, content_type)).fetchone()
    if row:
        category_id = row["id"]
        conn.execute(
            "UPDATE categories SET content_type=?, is_smart=?, sort_order=?, rule_json=? WHERE id=?",
            (content_type, int(is_smart), sort_order, rule_json, category_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO categories (name, content_type, is_smart, sort_order, rule_json, created_at) VALUES (?,?,?,?,?,?)",
            (name, content_type, int(is_smart), sort_order, rule_json, _now()),
        )
        category_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return category_id


def get_category(category_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_category_by_name(name: str, content_type: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM categories WHERE name=? AND content_type=?", (name, content_type)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_category(category_id: int) -> None:
    """Hard delete. movie_category_placements/series_category_placements for
    this category cascade via FK — the movies/series themselves are untouched,
    just no longer placed in this category."""
    conn = _connect()
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    _commit_with_retry(conn)
    conn.close()


def set_category_sort_order(category_id: int, sort_order: int) -> None:
    conn = _connect()
    conn.execute("UPDATE categories SET sort_order=? WHERE id=?", (sort_order, category_id))
    _commit_with_retry(conn)
    conn.close()


def set_category_name(category_id: int, name: str) -> None:
    conn = _connect()
    conn.execute("UPDATE categories SET name=? WHERE id=?", (name, category_id))
    _commit_with_retry(conn)
    conn.close()


def set_category_schedule_interval(category_id: int, interval_seconds: int | None, use_ai_evaluation: bool) -> None:
    """Per-rule recurring evaluation, opt-in (interval_seconds NULL = manual
    only, unchanged default behavior). use_ai_evaluation is a SEPARATE
    opt-in, default off -- rule-based evaluation is free (no external API
    call), so scheduling it has no real downside; AI-assisted evaluation
    costs real, recurring money against a whole catalog if left on by
    default, so a user must deliberately turn it on per-rule."""
    conn = _connect()
    conn.execute(
        "UPDATE categories SET schedule_interval_seconds=?, use_ai_evaluation=? WHERE id=?",
        (interval_seconds, int(use_ai_evaluation), category_id),
    )
    _commit_with_retry(conn)
    conn.close()


def mark_category_evaluated(category_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE categories SET last_evaluated_at=? WHERE id=?", (_now(), category_id))
    _commit_with_retry(conn)
    conn.close()


def categories_due_for_scheduled_evaluation() -> list[dict]:
    """Smart categories with a schedule_interval_seconds set, whose
    last_evaluated_at (any evaluation, manual or scheduled -- see
    evaluate_smart_category/mark_category_evaluated) is either null (never
    run) or older than their own configured interval. Same due-check shape
    as _is_stale/movie_needs_enrichment, just per-row instead of one global
    TTL, since each rule owns its own interval."""
    conn = _connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT * FROM categories
        WHERE is_smart=1 AND rule_json IS NOT NULL AND schedule_interval_seconds IS NOT NULL
    """).fetchall()]
    conn.close()
    now = time.time()
    return [
        r for r in rows
        if not r["last_evaluated_at"] or (now - float(r["last_evaluated_at"])) > r["schedule_interval_seconds"]
    ]


def set_category_ai_description(category_id: int, ai_description: str | None) -> None:
    """Persisted so a re-run of AI Evaluate (see ai_assist.py) doesn't require
    re-typing the description each time -- same pattern as sync_source for
    TMDB Lists categories."""
    conn = _connect()
    conn.execute("UPDATE categories SET ai_description=? WHERE id=?", (ai_description, category_id))
    _commit_with_retry(conn)
    conn.close()


def set_category_sync_source(category_id: int, sync_source: str | None) -> None:
    """sync_source e.g. 'tmdb_list:1234567' — see tmdb_sync.py for the actual
    fetch/match/place logic that reads this."""
    conn = _connect()
    conn.execute("UPDATE categories SET sync_source=? WHERE id=?", (sync_source, category_id))
    _commit_with_retry(conn)
    conn.close()


def list_sync_categories() -> list[dict]:
    """All categories with a sync_source configured — what the scheduled/manual sync walks."""
    conn = _connect()
    rows = conn.execute("SELECT * FROM categories WHERE sync_source IS NOT NULL AND sync_source != ''").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_by_tmdb_id(tmdb_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM movies WHERE tmdb_id=?", (str(tmdb_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_series_by_tmdb_id(tmdb_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM series WHERE tmdb_id=?", (str(tmdb_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_movies_with_tmdb_id(after_id: int, limit: int) -> list[dict]:
    """Cursor-paginated (by id, not OFFSET) so bulk_apply_tmdb_title_movies
    can keep calling this across a whole library in bounded batches without
    ever re-fetching a batch it already processed -- OFFSET would have kept
    returning the exact same first `limit` rows forever, since renaming a
    movie to already match TMDB's title doesn't remove it from a WHERE
    tmdb_id IS NOT NULL filter."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, name, year, tmdb_id FROM movies WHERE tmdb_id IS NOT NULL AND id > ? ORDER BY id LIMIT ?",
        (after_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_series_with_tmdb_id(after_id: int, limit: int) -> list[dict]:
    """See list_movies_with_tmdb_id's identical docstring."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, name, year, tmdb_id FROM series WHERE tmdb_id IS NOT NULL AND id > ? ORDER BY id LIMIT ?",
        (after_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_movie_by_title_year(title: str, year: int | None) -> dict | None:
    """Fallback for tmdb_sync's list matching: a pool movie only ever gets a
    tmdb_id at import time if the provider's own metadata happened to carry
    one (see vod_importer.py), so plenty of real pool items TMDB-list
    matching should hit sit there with tmdb_id NULL. Rather than reporting
    those as "not in pool" (the real complaint behind GH issue #3 -- a
    curated list like IMDB Top 250 matching "very few items"), fall back to
    the same normalized-title comparison find_pool_backfill_match already
    trusts, with a +/-1 year tolerance matching the Duplicate Finder's own
    (_split_by_year_proximity) since a provider-mislabeled release year is
    common and shouldn't sink an otherwise-exact title match."""
    normalized = _normalize_title_for_dedup(title)
    conn = _connect()
    rows = conn.execute("SELECT id, name, year FROM movies WHERE tmdb_id IS NULL").fetchall()
    conn.close()
    candidates = [r for r in rows if _normalize_title_for_dedup(r["name"]) == normalized]
    if not candidates:
        return None
    if year is not None:
        in_range = [r for r in candidates if r["year"] is not None and abs(r["year"] - year) <= 1]
        if in_range:
            candidates = in_range
    return dict(candidates[0])


def find_series_by_title_year(title: str, year: int | None) -> dict | None:
    """Series counterpart to find_movie_by_title_year -- see its docstring."""
    normalized = _normalize_title_for_dedup(title)
    conn = _connect()
    rows = conn.execute("SELECT id, name, year FROM series WHERE tmdb_id IS NULL").fetchall()
    conn.close()
    candidates = [r for r in rows if _normalize_title_for_dedup(r["name"]) == normalized]
    if not candidates:
        return None
    if year is not None:
        in_range = [r for r in candidates if r["year"] is not None and abs(r["year"] - year) <= 1]
        if in_range:
            candidates = in_range
    return dict(candidates[0])


def list_categories(content_type: str | None = None, active_only: bool = False) -> list[dict]:
    """active_only=True is for what actually gets exported to Dispatcharr
    (xc_server's get_vod_categories/get_series_categories) -- a disabled
    category (see set_category_active) still exists and is fully usable
    inside VOD Manager itself (still placeable, still evaluable if smart),
    it's just not offered to Dispatcharr while off. Every other caller
    (the admin Manage Categories UI, seeding checks, etc.) wants the
    default False -- disabled categories should still be visible/manageable
    there, not disappear."""
    conn = _connect()
    where = []
    params: list = []
    if content_type:
        where.append("content_type=?")
        params.append(content_type)
    if active_only:
        where.append("is_active=1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"SELECT * FROM categories {clause} ORDER BY sort_order, name", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_category_active(category_id: int, is_active: bool) -> None:
    """Soft on/off switch -- same pattern as providers.is_active. Disabling
    stops a category from being exported to Dispatcharr (list_categories
    active_only=True) without losing the category itself, its rule, or
    anything already placed in it -- e.g. a seasonal category (Halloween,
    Christmas) can be turned off after the season and back on next year
    without rebuilding it. Refuses to disable the last active category for
    a content_type, since that reproduces the exact "Dispatcharr aborts
    VOD refresh on an empty category list" bug _seed_default_categories
    exists to prevent -- this is the same failure reachable a different way."""
    conn = _connect()
    category = conn.execute("SELECT content_type FROM categories WHERE id=?", (category_id,)).fetchone()
    if not category:
        conn.close()
        raise ValueError(f"category {category_id} not found")
    if not is_active:
        active_count = conn.execute(
            "SELECT COUNT(*) c FROM categories WHERE content_type=? AND is_active=1 AND id!=?",
            (category["content_type"], category_id),
        ).fetchone()["c"]
        if active_count == 0:
            conn.close()
            raise ValueError(
                f"Can't disable the last active {category['content_type']} category -- "
                "at least 1 must stay active or Dispatcharr's VOD refresh will fail with an empty category list."
            )
    conn.execute("UPDATE categories SET is_active=? WHERE id=?", (int(is_active), category_id))
    _commit_with_retry(conn)
    conn.close()


def bulk_set_category_active(category_ids: list[int], is_active: bool) -> dict:
    """Bulk on/off toggle -- same last-active-category guard as
    set_category_active, but computed once across the whole batch instead
    of one row's stale count at a time, since disabling several categories
    from the same content_type in one action could otherwise pass a
    per-row check while still leaving that content_type with zero active
    categories once the whole batch lands."""
    if not category_ids:
        return {"changed": 0}
    conn = _connect()
    placeholders = ",".join("?" for _ in category_ids)
    if not is_active:
        rows = conn.execute(
            f"SELECT DISTINCT content_type FROM categories WHERE id IN ({placeholders})", category_ids,
        ).fetchall()
        for row in rows:
            ct = row["content_type"]
            remaining = conn.execute(
                f"SELECT COUNT(*) c FROM categories WHERE content_type=? AND is_active=1 AND id NOT IN ({placeholders})",
                (ct, *category_ids),
            ).fetchone()["c"]
            if remaining == 0:
                conn.close()
                raise ValueError(
                    f"Can't disable every active {ct} category -- at least 1 must stay active or "
                    "Dispatcharr's VOD refresh will fail with an empty category list."
                )
    conn.execute(f"UPDATE categories SET is_active=? WHERE id IN ({placeholders})", (int(is_active), *category_ids))
    _commit_with_retry(conn)
    conn.close()
    return {"changed": len(category_ids)}


def bulk_delete_categories(category_ids: list[int]) -> int:
    """Hard delete. movie_category_placements/series_category_placements
    for these categories cascade via FK -- the movies/series themselves
    are untouched, just no longer placed in these categories."""
    if not category_ids:
        return 0
    conn = _connect()
    placeholders = ",".join("?" for _ in category_ids)
    conn.execute(f"DELETE FROM categories WHERE id IN ({placeholders})", category_ids)
    _commit_with_retry(conn)
    conn.close()
    return len(category_ids)


_MMDD_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def set_category_schedule(category_id: int, start_mmdd: str | None, end_mmdd: str | None) -> None:
    """Annual recurring on/off schedule for a category -- e.g. a Halloween
    category set to "10-01" -> "11-01" auto-enables every Oct 1 and
    auto-disables every Nov 1, without anyone remembering to flip it by
    hand each year. MM-DD strings (not full dates) so the recurrence is
    implicit -- no separate "repeat yearly" flag needed, and lexicographic
    string comparison already sorts the same as chronological order within
    a year (see _mmdd_in_range), including wraparound ranges that cross
    New Year's (e.g. Christmas: "12-01" -> "01-02").

    Both null clears the schedule (pure manual control, unchanged from
    before this feature existed). Setting only one is rejected -- a
    schedule needs both ends.

    Also immediately applies today's correct state -- e.g. creating a
    Halloween category and setting its schedule while already in mid-
    October shouldn't have to wait until next year's Oct 1 transition to
    turn on for the first time."""
    if (start_mmdd is None) != (end_mmdd is None):
        raise ValueError("both a start and end date are required to set a schedule (or clear both to remove it)")
    if start_mmdd is not None and not _MMDD_RE.match(start_mmdd):
        raise ValueError(f"invalid start date {start_mmdd!r} -- expected MM-DD")
    if end_mmdd is not None and not _MMDD_RE.match(end_mmdd):
        raise ValueError(f"invalid end date {end_mmdd!r} -- expected MM-DD")
    conn = _connect()
    if not conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
        conn.close()
        raise ValueError(f"category {category_id} not found")
    conn.execute(
        "UPDATE categories SET schedule_start_mmdd=?, schedule_end_mmdd=? WHERE id=?",
        (start_mmdd, end_mmdd, category_id),
    )
    _commit_with_retry(conn)
    conn.close()
    if start_mmdd is not None and end_mmdd is not None:
        should_be_active = _mmdd_in_range(_today_mmdd(), start_mmdd, end_mmdd)
        try:
            set_category_active(category_id, should_be_active)
        except ValueError:
            pass  # e.g. would disable the last active category -- schedule is still saved, just can't apply yet


def _today_mmdd() -> str:
    """UTC, not local/container time -- avoids the schedule silently landing
    on a different calendar day depending on which timezone the container
    happens to run in (same reasoning as the EPG pipeline's own UTC
    handling elsewhere in this app)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%m-%d")


def _mmdd_in_range(today_mmdd: str, start_mmdd: str, end_mmdd: str) -> bool:
    if start_mmdd <= end_mmdd:
        return start_mmdd <= today_mmdd <= end_mmdd
    # Wraps around New Year's (e.g. "12-01" -> "01-02")
    return today_mmdd >= start_mmdd or today_mmdd <= end_mmdd


def apply_category_schedules(today_mmdd: str | None = None) -> list[dict]:
    """Called once a day (see main.py's periodic loop) -- fires the on/off
    transition ONLY on the exact calendar day it's due, not a continuous
    "enforce the schedule" check every time this runs. That's deliberate:
    it means a manual enable/disable in between a category's two scheduled
    dates just works and isn't fought by the scheduler until the next real
    transition -- same "manual action sticks until something explicit
    changes it" spirit as is_adult_manual/review_excluded_manual elsewhere
    in this file, just without needing a dedicated manual-override column
    here, since "not today" already means "leave it alone."""
    if today_mmdd is None:
        today_mmdd = _today_mmdd()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, name, content_type, is_active, schedule_start_mmdd, schedule_end_mmdd FROM categories "
        "WHERE schedule_start_mmdd IS NOT NULL AND schedule_end_mmdd IS NOT NULL"
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        if today_mmdd == row["schedule_start_mmdd"] and not row["is_active"]:
            try:
                set_category_active(row["id"], True)
                results.append({"id": row["id"], "name": row["name"], "action": "enabled"})
            except ValueError as exc:
                results.append({"id": row["id"], "name": row["name"], "action": "enable_failed", "error": str(exc)})
        elif today_mmdd == row["schedule_end_mmdd"] and row["is_active"]:
            try:
                set_category_active(row["id"], False)
                results.append({"id": row["id"], "name": row["name"], "action": "disabled"})
            except ValueError as exc:
                results.append({"id": row["id"], "name": row["name"], "action": "disable_failed", "error": str(exc)})
    return results


def purge_excluded_from_categories() -> dict:
    """One-time-per-call retroactive fix: removes every review_excluded=1
    movie/series from every category it's still placed in. Needed on top of
    evaluate_smart_category's own review_excluded filter (which only stops
    FUTURE placement, see its docstring) because this bug already shipped
    once -- an install that ran import-time exclusion before this fix could
    have thousands of already-wrongly-placed rows sitting there, and
    nothing else would ever clean those up on its own."""
    conn = _connect()
    movie_ids = [r["movie_id"] for r in conn.execute("""
        SELECT DISTINCT p.movie_id FROM movie_category_placements p
        JOIN movies m ON m.id = p.movie_id WHERE m.review_excluded=1
    """).fetchall()]
    series_ids = [r["series_id"] for r in conn.execute("""
        SELECT DISTINCT p.series_id FROM series_category_placements p
        JOIN series s ON s.id = p.series_id WHERE s.review_excluded=1
    """).fetchall()]
    if movie_ids:
        conn.execute(
            f"DELETE FROM movie_category_placements WHERE movie_id IN ({','.join('?' * len(movie_ids))})",
            movie_ids,
        )
    if series_ids:
        conn.execute(
            f"DELETE FROM series_category_placements WHERE series_id IN ({','.join('?' * len(series_ids))})",
            series_ids,
        )
    _commit_with_retry(conn)
    conn.close()
    return {"movies_removed": len(movie_ids), "series_removed": len(series_ids)}


def get_movie_category_ids(movie_id: int) -> list[int]:
    """Which categories a movie is placed in -- used by xc_server's
    per-client category allowlist to decide whether a restricted client may
    reach a movie via a route (e.g. preview) that isn't already filtered by
    a specific category placement's export id."""
    conn = _connect()
    rows = conn.execute("SELECT category_id FROM movie_category_placements WHERE movie_id=?", (movie_id,)).fetchall()
    conn.close()
    return [r["category_id"] for r in rows]


def get_series_category_ids(series_id: int) -> list[int]:
    """Series equivalent of get_movie_category_ids — see there."""
    conn = _connect()
    rows = conn.execute("SELECT category_id FROM series_category_placements WHERE series_id=?", (series_id,)).fetchall()
    conn.close()
    return [r["category_id"] for r in rows]


# ── Movies ───────────────────────────────────────────────────────────────────

def upsert_movie(name: str, year: int | None = None, **fields) -> int:
    conn = _connect()

    def _insert(needs_review: int = 0) -> int:
        cols = ["name", "year", "needs_year_review", *fields.keys()]
        vals = [name, year, needs_review, *fields.values()]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO movies ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)",
            (*vals, _now()),
        )
        return cur.lastrowid

    def _update(movie_id: int) -> None:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), movie_id))

    row = conn.execute("SELECT id FROM movies WHERE name = ? AND year IS ?", (name, year)).fetchone()
    if row:
        movie_id = row["id"]
        _update(movie_id)
    elif year is None:
        # No exact (name, NULL) row exists yet. Rather than blindly create a
        # fresh row that might just be an unlabeled duplicate of something
        # already in the pool, look for existing candidates by name alone.
        # Exactly one -> merge into it (almost certainly the same title,
        # just missing year metadata from this particular source). Two or
        # more -> can't tell which one it is, so create a new row but flag
        # it for a human to resolve rather than silently guessing wrong.
        candidates = conn.execute("SELECT id FROM movies WHERE name = ?", (name,)).fetchall()
        if len(candidates) == 1:
            movie_id = candidates[0]["id"]
            _update(movie_id)
        else:
            movie_id = _insert(needs_review=1 if candidates else 0)
    else:
        movie_id = _insert()

    _commit_with_retry(conn)
    conn.close()
    return movie_id


def _movie_filter_clause(
    search: str | None, category_id: int | None, provider_id: int | None = None, archived: bool = False,
) -> tuple[str, list]:
    where = ["m.review_excluded = ?"]
    params: list = [1 if archived else 0]
    if search:
        # Also matches each source's own raw_name -- the provider's
        # unmodified original title, captured before parse_name_year and
        # Title & Metadata Rules ran (see vod_importer.import_provider_
        # catalog). Real user request 2026-07-31: a rule that strips e.g. a
        # "EN|" prefix means the original text no longer appears in m.name
        # at all, so searching for it would otherwise find nothing even
        # though the movie is right there.
        where.append("(m.name LIKE ? OR m.id IN (SELECT movie_id FROM movie_sources WHERE raw_name LIKE ?))")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    if category_id is not None:
        where.append("m.id IN (SELECT movie_id FROM movie_category_placements WHERE category_id=?)")
        params.append(category_id)
    if provider_id is not None:
        where.append("m.id IN (SELECT movie_id FROM movie_sources WHERE provider_id=?)")
        params.append(provider_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def list_movies(
    limit: int = 50, offset: int = 0, search: str | None = None, category_id: int | None = None,
    provider_id: int | None = None, archived: bool = False,
) -> list[dict]:
    conn = _connect()
    clause, params = _movie_filter_clause(search, category_id, provider_id, archived)
    rows = conn.execute(
        f"SELECT m.* FROM movies m {clause} ORDER BY m.name LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_movies(
    search: str | None = None, category_id: int | None = None, provider_id: int | None = None, archived: bool = False,
) -> int:
    conn = _connect()
    clause, params = _movie_filter_clause(search, category_id, provider_id, archived)
    n = conn.execute(f"SELECT COUNT(*) c FROM movies m {clause}", params).fetchone()["c"]
    conn.close()
    return n


def list_all_movie_ids(
    search: str | None = None, category_id: int | None = None, provider_id: int | None = None, archived: bool = False,
) -> list[int]:
    conn = _connect()
    clause, params = _movie_filter_clause(search, category_id, provider_id, archived)
    rows = conn.execute(f"SELECT m.id FROM movies m {clause}", params).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def list_movie_sources_for_ids(movie_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk equivalent of list_movie_sources — one query for a whole page of
    movies instead of one query per movie (that N+1 pattern is what froze the
    app once the pool had thousands of real rows)."""
    if not movie_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in movie_ids)
    rows = conn.execute(f"""
        SELECT ms.*, p.name AS provider_name FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id IN ({placeholders})
        ORDER BY p.name
    """, movie_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {mid: [] for mid in movie_ids}
    for r in rows:
        grouped[r["movie_id"]].append(dict(r))
    return grouped


def list_movie_placements_for_ids(movie_ids: list[int]) -> dict[int, list[dict]]:
    if not movie_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in movie_ids)
    rows = conn.execute(f"""
        SELECT mcp.*, c.name AS category_name FROM movie_category_placements mcp
        JOIN categories c ON c.id = mcp.category_id
        WHERE mcp.movie_id IN ({placeholders})
        ORDER BY mcp.id
    """, movie_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {mid: [] for mid in movie_ids}
    for r in rows:
        grouped[r["movie_id"]].append(dict(r))
    return grouped


def get_movie(movie_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_movie_by_name_year(name: str, year: int | None) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM movies WHERE name=? AND year IS ?", (name, year)).fetchone()
    conn.close()
    return dict(row) if row else None


_refresh_settings_cache: dict | None = None
_refresh_settings_cache_at = 0.0
_REFRESH_SETTINGS_CACHE_TTL = 30


def _refresh_settings() -> dict:
    """Cached read of config.get_refresh_settings() -- _is_stale() runs once
    per item during a bulk_enrich_all pass over the whole pool (hundreds of
    thousands of rows), so this can't be a raw config-file read per call."""
    global _refresh_settings_cache, _refresh_settings_cache_at
    now = time.time()
    if _refresh_settings_cache is None or (now - _refresh_settings_cache_at) >= _REFRESH_SETTINGS_CACHE_TTL:
        _refresh_settings_cache = get_refresh_settings()
        _refresh_settings_cache_at = now
    return _refresh_settings_cache


def get_enrichment_ttl_seconds() -> int:
    return _refresh_settings()["enrichment_ttl_seconds"]


def get_catalog_refresh_interval_seconds(provider_type: str) -> int:
    key = f"catalog_refresh_seconds_{provider_type}" if provider_type in ("xc", "plex", "emby", "jellyfin") else "catalog_refresh_seconds_xc"
    return _refresh_settings()[key]


def get_tmdb_sync_interval_seconds() -> int | None:
    return _refresh_settings()["tmdb_sync_interval_seconds"]


def mark_provider_catalog_refreshed(provider_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET last_catalog_refresh_at=? WHERE id=?", (_now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_import_totals(provider_id: int, movie_total: int | None, series_total: int | None) -> None:
    """The provider's own raw reported catalog size for its most recent
    import pass -- movie_total/series_total are exactly len(streams)/
    len(series_list) from vod_importer.import_provider_catalog, before any
    dedup/matching logic runs. Real user request, 2026-07-30: the existing
    Movies/Series columns already show how much is actually IN the pool
    from this provider, but not what the provider itself claims to have --
    an admin has no quick way to spot "provider reports 50,000 movies, only
    45,000 ever made it into the pool" without digging through logs.
    None for either side that doesn't apply (e.g. Plex/Emby import provider
    counts separately and doesn't call this)."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET last_movie_provider_total=?, last_series_provider_total=? WHERE id=?",
        (movie_total, series_total, provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def _is_stale(last_enriched_at) -> bool:
    if not last_enriched_at:
        return True
    return (time.time() - float(last_enriched_at)) > get_enrichment_ttl_seconds()


def movie_needs_enrichment(movie_id: int) -> bool:
    movie = get_movie(movie_id)
    return bool(movie) and _is_stale(movie.get("last_enriched_at"))


def set_movie_enrichment(movie_id: int, **fields) -> None:
    """Persist detail-level fields fetched from a provider's get_vod_info, and
    stamp last_enriched_at so we don't re-fetch this movie again for
    ENRICHMENT_TTL_SECONDS — same throttling pattern Dispatcharr itself uses
    for provider detail lookups."""
    with _WRITE_LOCK:
        conn = _connect()
        fields["last_enriched_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE movies SET {sets} WHERE id=?", (*fields.values(), movie_id))
        _commit_with_retry(conn)
        conn.close()


def list_movie_sources(movie_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT ms.*, p.name AS provider_name FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id = ?
        ORDER BY p.name
    """, (movie_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_movie_placements(movie_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT mcp.*, c.name AS category_name FROM movie_category_placements mcp
        JOIN categories c ON c.id = mcp.category_id
        WHERE mcp.movie_id = ?
        ORDER BY mcp.id
    """, (movie_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_movie_source(
    movie_id: int, provider_id: int, provider_stream_id: str,
    container_extension: str = "mp4", provider_category_name: str | None = None,
    file_size_bytes: int | None = None, local_file_path: str | None = None,
) -> None:
    """file_size_bytes/local_file_path are optional and only ever passed by
    download-backfill (see dispatcharr_dvr_importer._apply_download_backfill)
    -- every other caller registers a plain provider-streamed source and
    leaves both null, unchanged from this function's original behavior."""
    conn = _connect()
    conn.execute(
        """INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, provider_category_name, file_size_bytes, local_file_path, added_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
               movie_id=excluded.movie_id, last_seen_at=excluded.last_seen_at, provider_category_name=excluded.provider_category_name,
               file_size_bytes=COALESCE(excluded.file_size_bytes, movie_sources.file_size_bytes),
               local_file_path=COALESCE(excluded.local_file_path, movie_sources.local_file_path)""",
        (movie_id, provider_id, provider_stream_id, container_extension, provider_category_name,
         file_size_bytes, local_file_path, _now(), _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def set_movie_source_file_size_bytes(source_id: int, file_size_bytes: int) -> None:
    """Pointer-mode backfill's virtual byte accounting -- a regular
    provider's movie_sources row never gets file_size_bytes populated at
    import time (only DVR-recorded sources do), so this fills it in from a
    one-time Content-Length probe once the pool item is backfilled into
    someone's category, without touching local_file_path -- it stays a
    pointer (virtual), not a local copy (actual)."""
    conn = _connect()
    conn.execute("UPDATE movie_sources SET file_size_bytes=? WHERE id=?", (file_size_bytes, source_id))
    _commit_with_retry(conn)
    conn.close()


def set_episode_source_file_size_bytes(source_id: int, file_size_bytes: int) -> None:
    conn = _connect()
    conn.execute("UPDATE episode_sources SET file_size_bytes=? WHERE id=?", (file_size_bytes, source_id))
    _commit_with_retry(conn)
    conn.close()


def set_movie_source_bitrate(source_id: int, bitrate: int | None) -> None:
    """Bitrate lives on the SOURCE row, not movies -- unlike genre/cast/etc,
    it's a property of a specific provider's specific stream for this title,
    not the title itself (two providers' copies of the same movie can be
    encoded at very different bitrates)."""
    with _WRITE_LOCK:
        conn = _connect()
        conn.execute("UPDATE movie_sources SET bitrate=? WHERE id=?", (bitrate, source_id))
        _commit_with_retry(conn)
        conn.close()


def set_episode_source_bitrate(source_id: int, bitrate: int | None) -> None:
    with _WRITE_LOCK:
        conn = _connect()
        conn.execute("UPDATE episode_sources SET bitrate=? WHERE id=?", (bitrate, source_id))
        _commit_with_retry(conn)
        conn.close()


def delete_movie(movie_id: int) -> None:
    """Hard delete -- only for genuine orphans (zero sources). A movie with
    an active source still exists at that provider, so the next catalog
    sync just re-imports it fresh (matched by name+year, since the deleted
    row's id is gone) -- a real bug reported live: deleting something a
    provider still serves doesn't actually stick, and worse, the fresh row
    starts with none of the old row's state (archived, categories, manual
    edits all gone, since it's not an UPDATE onto the old row, it's a brand
    new INSERT). Archive is the durable way to hide something that's still
    provider-backed; only a truly sourceless item is safe to hard-delete.
    movie_sources/movie_category_placements cascade via FK."""
    conn = _connect()
    source_count = conn.execute("SELECT COUNT(*) c FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["c"]
    if source_count > 0:
        conn.close()
        raise ValueError(
            f"Can't delete a movie with {source_count} active source(s) -- the next catalog sync would just "
            "re-import it fresh, with none of its archived/category state carried over. Archive it instead; "
            "only sourceless orphans (see Orphan Checker) can be deleted."
        )
    conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))
    _commit_with_retry(conn)
    conn.close()


def set_movie_adult(movie_id: int, is_adult: bool) -> None:
    """Manual override — also stamps is_adult_manual so future auto-detection
    passes (see resync_adult_flags) never silently revert this."""
    conn = _connect()
    conn.execute(
        "UPDATE movies SET is_adult=?, is_adult_manual=1, updated_at=? WHERE id=?",
        (int(is_adult), _now(), movie_id),
    )
    _commit_with_retry(conn)
    conn.close()


def delete_movie_source(movie_id: int, source_id: int) -> None:
    """Admin hard delete -- unlike remove_movie_library_owner, this isn't
    reference-counted against portal owners (an admin deleting a source
    removes it outright, cascading movie_source_owners via ON DELETE
    CASCADE regardless of how many people had it in their Library). Also
    now deletes the underlying file from disk, if any -- nothing did that
    before this (real gap found live 2026-07-28: DVR recording files were
    never cleaned up on delete, admin or portal, silently growing disk
    usage forever). Skipped if another source row still points at the
    exact same path, same defensive check as remove_movie_library_owner."""
    conn = _connect()
    row = conn.execute("SELECT local_file_path FROM movie_sources WHERE id=? AND movie_id=?", (source_id, movie_id)).fetchone()
    file_path = None
    if row and row["local_file_path"]:
        other_ref = conn.execute(
            """SELECT 1 FROM movie_sources WHERE local_file_path=? AND id!=?
               UNION SELECT 1 FROM episode_sources WHERE local_file_path=? LIMIT 1""",
            (row["local_file_path"], source_id, row["local_file_path"]),
        ).fetchone()
        if not other_ref:
            file_path = row["local_file_path"]
    conn.execute("DELETE FROM movie_sources WHERE id=? AND movie_id=?", (source_id, movie_id))
    _purge_if_sourceless_movie(conn, movie_id)
    _commit_with_retry(conn)
    conn.close()
    _delete_file_if_present(file_path)


def move_movie_source(source_id: int, movie_id: int, target_movie_id: int) -> None:
    """Re-points one mismatched source at the movie it actually belongs to,
    instead of deleting and losing it -- for when a provider's own listing
    (typo, wrong year, a title collision) got matched to the wrong movie on
    import. Keeps the source's own history (priority, raw_name,
    consecutive_failures) intact; only movie_id changes. The
    (provider_id, provider_stream_id) UNIQUE constraint doesn't involve
    movie_id, so this can't collide. If that was the old movie's last
    source, it gets purged same as a plain delete would."""
    conn = _connect()
    if not conn.execute("SELECT 1 FROM movies WHERE id=?", (target_movie_id,)).fetchone():
        conn.close()
        raise ValueError(f"target movie {target_movie_id} not found")
    conn.execute("UPDATE movie_sources SET movie_id=? WHERE id=? AND movie_id=?", (target_movie_id, source_id, movie_id))
    _purge_if_sourceless_movie(conn, movie_id)
    _commit_with_retry(conn)
    conn.close()


def remove_movie_from_category(movie_id: int, category_id: int) -> None:
    conn = _connect()
    conn.execute(
        "DELETE FROM movie_category_placements WHERE movie_id=? AND category_id=?",
        (movie_id, category_id),
    )
    _commit_with_retry(conn)
    conn.close()


def remove_movie_from_all_categories(movie_id: int) -> None:
    """Called the moment a movie becomes review_excluded=1 (see
    bulk_import_movies) -- removing it from every category it's currently
    placed in is what actually makes "archived" mean "not visible to
    Dispatcharr", since evaluate_smart_category's own review_excluded
    filter only stops FUTURE placement, it never un-places an existing
    match on its own."""
    conn = _connect()
    conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
    _commit_with_retry(conn)
    conn.close()


def place_movie_in_category(movie_id: int, category_id: int) -> int:
    """Assign a movie to a category, returning the export_stream_id to use in the XC feed.

    The first placement for a given movie uses the clean name; subsequent
    placements (for the same movie in additional categories) get an
    invisible zero-width-space marker appended so Dispatcharr's same-account
    (name, year) dedup treats each as a distinct catalog entry.
    """
    conn = _connect()
    flagged = conn.execute("SELECT needs_year_review FROM movies WHERE id=?", (movie_id,)).fetchone()
    if flagged and flagged["needs_year_review"]:
        conn.close()
        raise ValueError(f"movie {movie_id} needs year review before it can be placed in a category")
    existing = conn.execute(
        "SELECT export_stream_id FROM movie_category_placements WHERE movie_id=? AND category_id=?",
        (movie_id, category_id),
    ).fetchone()
    if existing:
        conn.close()
        return existing["export_stream_id"]

    placement_count = conn.execute(
        "SELECT COUNT(*) c FROM movie_category_placements WHERE movie_id=?", (movie_id,)
    ).fetchone()["c"]
    name_suffix = _ZW_MARKER * placement_count  # 0 suffixes for the 1st placement, 1 for the 2nd, ...

    # BEGIN IMMEDIATE around the read-then-insert: without it, two concurrent
    # placements (e.g. a scheduled refresh auto-placing while a user runs
    # "Place all filtered") can both read the same MAX(export_stream_id) and
    # try to insert the same value -- caught by the UNIQUE constraint, but as
    # an unhandled IntegrityError rather than being serialized cleanly. This
    # forces the write lock before the read, so the second caller blocks
    # (and retries via _commit_with_retry) instead of racing.
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        export_stream_id = _EXPORT_STREAM_ID_BASE + _next_placement_seq(conn)
        conn.execute(
            "INSERT INTO movie_category_placements (movie_id, category_id, export_stream_id, name_suffix) VALUES (?,?,?,?)",
            (movie_id, category_id, export_stream_id, name_suffix),
        )
        _commit_with_retry(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return export_stream_id


def _next_placement_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(export_stream_id), ?) m FROM movie_category_placements", (_EXPORT_STREAM_ID_BASE - 1,)).fetchone()
    return row["m"] - _EXPORT_STREAM_ID_BASE + 1


def _chunked(ids: list[int], size: int = 900) -> list[list[int]]:
    """Split an id list into SQLite-safe IN(...) chunks. 900 stays well under
    even the historically low SQLITE_MAX_VARIABLE_NUMBER default of 999 seen
    on some builds (this build's is 32766, but the smart-category catch-all
    path can match tens of thousands of rows in one call -- see
    bulk_place_movies_in_category -- so a fixed safe chunk size beats
    querying the runtime limit)."""
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def bulk_place_movies_in_category(movie_ids: list[int], category_id: int) -> int:
    """Batch equivalent of place_movie_in_category — one connection/transaction
    for the whole list instead of one round-trip per movie. Needed because
    smart-category evaluation (e.g. a catch-all rule matching the entire
    pool) can place tens of thousands of rows at once; the one-at-a-time
    version times out at that scale. Returns the count newly placed
    (already-placed movies are skipped, same semantics as the single version).

    IN (...) clauses are chunked (see _chunked) rather than binding the
    whole id list as one statement -- a catalog-wide catch-all category can
    match tens of thousands of movies, which exceeds SQLite's bound-parameter
    limit (32766 on this build; historically as low as 999 on some) in a
    single query."""
    if not movie_ids:
        return 0
    conn = _connect()
    already: set[int] = set()
    for chunk in _chunked(movie_ids):
        placeholders = ",".join("?" for _ in chunk)
        already.update(r["movie_id"] for r in conn.execute(
            f"SELECT movie_id FROM movie_category_placements WHERE category_id=? AND movie_id IN ({placeholders})",
            (category_id, *chunk),
        ).fetchall())
    flagged: set[int] = set()
    for chunk in _chunked(movie_ids):
        placeholders = ",".join("?" for _ in chunk)
        flagged.update(r["id"] for r in conn.execute(
            f"SELECT id FROM movies WHERE needs_year_review=1 AND id IN ({placeholders})", chunk,
        ).fetchall())
    if flagged:
        logger.info("[vod_db] skipping %d movie(s) still needing year review for category=%s", len(flagged), category_id)
    to_place = [mid for mid in movie_ids if mid not in already and mid not in flagged]
    if not to_place:
        conn.close()
        return 0

    counts: dict[int, int] = {}
    for chunk in _chunked(to_place):
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT movie_id, COUNT(*) c FROM movie_category_placements WHERE movie_id IN ({placeholders}) GROUP BY movie_id",
            chunk,
        ).fetchall():
            counts[r["movie_id"]] = r["c"]

    next_seq = _next_placement_seq(conn)
    rows = []
    for mid in to_place:
        name_suffix = _ZW_MARKER * counts.get(mid, 0)
        rows.append((mid, category_id, _EXPORT_STREAM_ID_BASE + next_seq, name_suffix))
        next_seq += 1

    conn.executemany(
        "INSERT INTO movie_category_placements (movie_id, category_id, export_stream_id, name_suffix) VALUES (?,?,?,?)",
        rows,
    )
    _commit_with_retry(conn)
    conn.close()
    return len(rows)


def _best_source_cte() -> str:
    """Was a module-level constant string -- turned into a function so the
    ORDER BY reflects config.get_stream_priority_mode() at query time, not
    whatever it was when this module first loaded."""
    return f"""
    WITH best_source AS (
        SELECT ms.*, ROW_NUMBER() OVER (
            PARTITION BY movie_id ORDER BY {_source_order_by('ms', 'pr')}
        ) AS rn
        FROM movie_sources ms
        JOIN providers pr ON pr.id = ms.provider_id
        WHERE pr.is_active = 1
    )
"""


def get_movie_export_rows() -> list[dict]:
    """One row per (movie, category placement) for the XC get_vod_streams export.

    Where a movie has sources from multiple providers, the highest-priority
    provider's source is used (recency as tiebreak) — see
    list_movie_sources_for_streaming for the full failover-ordered list.
    """
    conn = _connect()
    rows = conn.execute(_best_source_cte() + """
        SELECT
            m.id AS movie_id, m.name AS name, m.year AS year, m.genre AS genre,
            m.description AS description, m.duration_secs AS duration_secs, m.poster_url AS poster_url,
            m.cast_list AS cast_list, m.director AS director, m.country AS country,
            m.rating AS rating, m.release_date AS release_date, m.is_adult AS is_adult,
            p.export_stream_id AS export_stream_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name,
            ms.provider_id AS provider_id, ms.provider_stream_id AS provider_stream_id,
            ms.container_extension AS container_extension, ms.bitrate AS bitrate
        FROM movie_category_placements p
        JOIN movies m ON m.id = p.movie_id
        JOIN categories c ON c.id = p.category_id AND c.is_active = 1
        LEFT JOIN best_source ms ON ms.movie_id = m.id AND ms.rn = 1
        ORDER BY m.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_source_for_streaming(source_id: int) -> dict | None:
    """One specific movie_sources row, ready for _proxy_vod_stream — used by
    the per-source preview/play button, which forces exactly this provider's
    copy rather than the normal priority-order failover across all of them.
    Includes the parent movie's name/year/duration so the caller can build a
    title without a second lookup."""
    conn = _connect()
    row = conn.execute("""
        SELECT ms.provider_id, ms.provider_stream_id, ms.container_extension, ms.plex_rating_key, ms.local_file_path,
               m.id AS movie_id, m.name AS movie_name, m.year AS movie_year, m.duration_secs AS duration_secs
        FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        JOIN movies m ON m.id = ms.movie_id
        WHERE ms.id = ? AND p.is_active = 1
    """, (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_movie_sources_for_streaming(movie_id: int) -> list[dict]:
    """All active-provider sources for a movie, highest-priority-provider
    first (recency as tiebreak) — used by xc_server's stream proxy to fail
    over to another provider if the primary one is down. Unlike
    _BEST_SOURCE_CTE (metadata: one row only), this returns every candidate
    so the proxy can try them in order."""
    conn = _connect()
    rows = conn.execute(f"""
        SELECT ms.id AS source_id, ms.provider_id, ms.provider_stream_id, ms.container_extension,
               ms.plex_rating_key, ms.local_file_path, ms.consecutive_failures AS consecutive_failures,
               p.name AS provider_name
        FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id = ? AND p.is_active = 1
        ORDER BY {_source_order_by('ms', 'p')}
    """, (movie_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_export_row_by_stream_id(export_stream_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(_best_source_cte() + """
        SELECT
            m.id AS movie_id, m.name AS name, m.year AS year, m.genre AS genre,
            m.description AS description, m.duration_secs AS duration_secs, m.poster_url AS poster_url,
            m.cast_list AS cast_list, m.director AS director, m.country AS country,
            m.rating AS rating, m.release_date AS release_date, m.is_adult AS is_adult,
            p.export_stream_id AS export_stream_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name,
            ms.provider_id AS provider_id, ms.provider_stream_id AS provider_stream_id,
            ms.container_extension AS container_extension, ms.bitrate AS bitrate
        FROM movie_category_placements p
        JOIN movies m ON m.id = p.movie_id
        JOIN categories c ON c.id = p.category_id AND c.is_active = 1
        LEFT JOIN best_source ms ON ms.movie_id = m.id AND ms.rn = 1
        WHERE p.export_stream_id = ?
        LIMIT 1
    """, (export_stream_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Series / Episodes ────────────────────────────────────────────────────────

def upsert_series(name: str, year: int | None = None, **fields) -> int:
    conn = _connect()

    def _insert(needs_review: int = 0) -> int:
        cols = ["name", "year", "needs_year_review", *fields.keys()]
        vals = [name, year, needs_review, *fields.values()]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO series ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)",
            (*vals, _now()),
        )
        return cur.lastrowid

    def _update(series_id: int) -> None:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE series SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), series_id))

    row = conn.execute("SELECT id FROM series WHERE name = ? AND year IS ?", (name, year)).fetchone()
    if row:
        series_id = row["id"]
        _update(series_id)
    elif year is None:
        # Same reasoning as upsert_movie above.
        candidates = conn.execute("SELECT id FROM series WHERE name = ?", (name,)).fetchall()
        if len(candidates) == 1:
            series_id = candidates[0]["id"]
            _update(series_id)
        else:
            series_id = _insert(needs_review=1 if candidates else 0)
    else:
        series_id = _insert()

    _commit_with_retry(conn)
    conn.close()
    return series_id


def _series_filter_clause(
    search: str | None, category_id: int | None, provider_id: int | None = None, archived: bool = False,
) -> tuple[str, list]:
    where = ["s.review_excluded = ?"]
    params: list = [1 if archived else 0]
    if search:
        # Also matches s.raw_name -- see _movie_filter_clause's identical
        # comment. Series (unlike movies) have exactly one raw_name, not
        # one per source, since XC series don't carry a per-source stream_id
        # the way movies do (see bulk_import_series's docstring).
        where.append("(s.name LIKE ? OR s.raw_name LIKE ?)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    if category_id is not None:
        where.append("s.id IN (SELECT series_id FROM series_category_placements WHERE category_id=?)")
        params.append(category_id)
    if provider_id is not None:
        # At least one episode actually sourced from this provider — not
        # import_provider_id, which only reflects whoever created the series
        # row and undercounts providers that later merged episodes in.
        where.append("""s.id IN (
            SELECT e.series_id FROM episode_sources es JOIN episodes e ON e.id = es.episode_id
            WHERE es.provider_id=?
        )""")
        params.append(provider_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def list_series(
    limit: int = 50, offset: int = 0, search: str | None = None, category_id: int | None = None,
    provider_id: int | None = None, archived: bool = False,
) -> list[dict]:
    conn = _connect()
    clause, params = _series_filter_clause(search, category_id, provider_id, archived)
    rows = conn.execute(
        f"""SELECT s.*, p.name AS import_provider_name FROM series s
            LEFT JOIN providers p ON p.id = s.import_provider_id
            {clause} ORDER BY s.name LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_series(
    search: str | None = None, category_id: int | None = None, provider_id: int | None = None, archived: bool = False,
) -> int:
    conn = _connect()
    clause, params = _series_filter_clause(search, category_id, provider_id, archived)
    n = conn.execute(f"SELECT COUNT(*) c FROM series s {clause}", params).fetchone()["c"]
    conn.close()
    return n


def list_all_series_ids(
    search: str | None = None, category_id: int | None = None, provider_id: int | None = None, archived: bool = False,
) -> list[int]:
    conn = _connect()
    clause, params = _series_filter_clause(search, category_id, provider_id, archived)
    rows = conn.execute(f"SELECT s.id FROM series s {clause}", params).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def list_series_placements_for_ids(series_ids: list[int]) -> dict[int, list[dict]]:
    if not series_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(f"""
        SELECT scp.*, c.name AS category_name FROM series_category_placements scp
        JOIN categories c ON c.id = scp.category_id
        WHERE scp.series_id IN ({placeholders})
        ORDER BY scp.id
    """, series_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {sid: [] for sid in series_ids}
    for r in rows:
        grouped[r["series_id"]].append(dict(r))
    return grouped


def episode_export_id(episode_id: int) -> int:
    return _EPISODE_EXPORT_BASE + episode_id


def list_episodes_for_series_ids(series_ids: list[int]) -> dict[int, list[dict]]:
    if not series_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(
        f"SELECT * FROM episodes WHERE series_id IN ({placeholders}) ORDER BY season_number, episode_number",
        series_ids,
    ).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {sid: [] for sid in series_ids}
    for r in rows:
        d = dict(r)
        d["export_episode_id"] = episode_export_id(d["id"])
        grouped[r["series_id"]].append(d)
    return grouped


def get_series(series_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        """SELECT s.*, p.name AS import_provider_name FROM series s
           LEFT JOIN providers p ON p.id = s.import_provider_id
           WHERE s.id=?""",
        (series_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_series_by_name_year(name: str, year: int | None) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM series WHERE name=? AND year IS ?", (name, year)).fetchone()
    conn.close()
    return dict(row) if row else None


def series_needs_enrichment(series_id: int) -> bool:
    series = get_series(series_id)
    return bool(series) and _is_stale(series.get("last_enriched_at"))


def set_series_enrichment(series_id: int, **fields) -> None:
    with _WRITE_LOCK:
        conn = _connect()
        fields["last_enriched_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE series SET {sets} WHERE id=?", (*fields.values(), series_id))
        _commit_with_retry(conn)
        conn.close()


def get_episode(episode_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_episode(series_id: int, season_number: int, episode_number: int, name: str, **fields) -> int:
    with _WRITE_LOCK:
        conn = _connect()
        row = conn.execute(
            "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
            (series_id, season_number, episode_number),
        ).fetchone()
        if row:
            episode_id = row["id"]
            fields["name"] = name
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE episodes SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), episode_id))
        else:
            cols = ["series_id", "season_number", "episode_number", "name", *fields.keys()]
            vals = [series_id, season_number, episode_number, name, *fields.values()]
            placeholders = ", ".join("?" for _ in cols)
            cur = conn.execute(
                f"INSERT INTO episodes ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)",
                (*vals, _now()),
            )
            episode_id = cur.lastrowid
        _commit_with_retry(conn)
        conn.close()
        return episode_id


def list_episodes(series_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE series_id=? ORDER BY season_number, episode_number", (series_id,)
    ).fetchall()
    conn.close()
    episodes = [dict(r) for r in rows]
    for e in episodes:
        e["export_episode_id"] = episode_export_id(e["id"])
    return episodes


def list_episode_sources_for_episode_ids(episode_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk equivalent of a single-episode source lookup — mirrors
    list_movie_sources_for_ids, which movies already had and episodes never
    did (there was previously no way to see which provider an episode's
    file actually comes from)."""
    if not episode_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in episode_ids)
    rows = conn.execute(f"""
        SELECT es.*, p.name AS provider_name FROM episode_sources es
        JOIN providers p ON p.id = es.provider_id
        WHERE es.episode_id IN ({placeholders})
        ORDER BY p.name
    """, episode_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {eid: [] for eid in episode_ids}
    for r in rows:
        grouped[r["episode_id"]].append(dict(r))
    return grouped


def add_episode_source(
    episode_id: int, provider_id: int, provider_stream_id: str, container_extension: str = "mp4",
    file_size_bytes: int | None = None, local_file_path: str | None = None, raw_name: str | None = None,
    provider_category_name: str | None = None,
) -> int:
    """See add_movie_source's docstring -- file_size_bytes/local_file_path
    are download-backfill-only, null for every other caller. Returns the
    source row's own id (a plain SELECT after the upsert, since lastrowid
    isn't reliable across the ON CONFLICT DO UPDATE branch) -- needed by
    callers that set a per-source field afterward, e.g. vod_importer.
    enrich_series stamping bitrate onto the row this specific get_series_info
    call was actually about.

    provider_category_name: real bug found live 2026-07-29 -- this parameter
    didn't exist at all until now, so evaluate_smart_category's
    provider_category rule field (and auto-create-categories) had literally
    no series-side data to ever match against, for any provider. XC only
    reports category at the series level, not per-episode (see
    vod_importer.enrich_series, which reads it back off the series row --
    bulk_import_series is what stamps it there, since episodes aren't known
    yet at that earlier, cheap bulk-list stage)."""
    with _WRITE_LOCK:
        conn = _connect()
        conn.execute(
            """INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, file_size_bytes, local_file_path, raw_name, provider_category_name, added_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                   episode_id=excluded.episode_id, last_seen_at=excluded.last_seen_at,
                   file_size_bytes=COALESCE(excluded.file_size_bytes, episode_sources.file_size_bytes),
                   local_file_path=COALESCE(excluded.local_file_path, episode_sources.local_file_path),
                   raw_name=excluded.raw_name,
                   provider_category_name=excluded.provider_category_name""",
            (episode_id, provider_id, provider_stream_id, container_extension, file_size_bytes, local_file_path, raw_name, provider_category_name, _now(), _now()),
        )
        _commit_with_retry(conn)
        source_id = conn.execute(
            "SELECT id FROM episode_sources WHERE provider_id=? AND provider_stream_id=?",
            (provider_id, provider_stream_id),
        ).fetchone()["id"]
        conn.close()
        return source_id


def delete_series(series_id: int) -> None:
    """Hard delete -- only for genuine orphans (zero episode sources across
    the whole series). Same reasoning as delete_movie: a series with any
    active episode source still exists at that provider, so the next
    catalog sync just re-imports it fresh with none of its archived/
    category state carried over. Archive is the durable way to hide
    something that's still provider-backed. episodes cascade via FK, which
    in turn cascades episode_sources; series_category_placements cascade
    off series directly."""
    conn = _connect()
    source_count = conn.execute("""
        SELECT COUNT(*) c FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        WHERE e.series_id=?
    """, (series_id,)).fetchone()["c"]
    if source_count > 0:
        conn.close()
        raise ValueError(
            f"Can't delete a series with {source_count} active episode source(s) -- the next catalog sync "
            "would just re-import it fresh, with none of its archived/category state carried over. Archive "
            "it instead; only sourceless orphans (see Orphan Checker) can be deleted."
        )
    conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    _commit_with_retry(conn)
    conn.close()


def set_series_adult(series_id: int, is_adult: bool) -> None:
    """Manual override — also stamps is_adult_manual so future auto-detection
    passes (see resync_adult_flags) never silently revert this."""
    conn = _connect()
    conn.execute(
        "UPDATE series SET is_adult=?, is_adult_manual=1, updated_at=? WHERE id=?",
        (int(is_adult), _now(), series_id),
    )
    _commit_with_retry(conn)
    conn.close()


def delete_episode_source(episode_id: int, source_id: int) -> None:
    """Episode counterpart to delete_movie_source -- see its docstring for
    why this now also deletes the underlying file from disk, and why this
    isn't reference-counted against portal owners the way
    remove_episode_library_owner is."""
    conn = _connect()
    row = conn.execute("SELECT local_file_path FROM episode_sources WHERE id=? AND episode_id=?", (source_id, episode_id)).fetchone()
    file_path = None
    if row and row["local_file_path"]:
        other_ref = conn.execute(
            """SELECT 1 FROM episode_sources WHERE local_file_path=? AND id!=?
               UNION SELECT 1 FROM movie_sources WHERE local_file_path=? LIMIT 1""",
            (row["local_file_path"], source_id, row["local_file_path"]),
        ).fetchone()
        if not other_ref:
            file_path = row["local_file_path"]
    conn.execute("DELETE FROM episode_sources WHERE id=? AND episode_id=?", (source_id, episode_id))
    episode_row = conn.execute("SELECT series_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    _purge_if_sourceless_episode(conn, episode_id)
    if episode_row:
        _purge_if_sourceless_series(conn, episode_row["series_id"])
    _commit_with_retry(conn)
    conn.close()
    _delete_file_if_present(file_path)


def move_episode_source(source_id: int, episode_id: int, target_series_id: int, season_number: int, episode_number: int, name: str) -> int:
    """Re-points one mismatched source at the episode it actually belongs to
    -- movie_sources' move_movie_source, episode edition. The target episode
    is found-or-created on target_series_id via add_episode (a title
    collision can just as easily land on a season/episode slot the correct
    series hasn't been given a row for yet, e.g. if this was the only
    source anyone had imported for it). Returns the target episode's id."""
    conn = _connect()
    if not conn.execute("SELECT 1 FROM series WHERE id=?", (target_series_id,)).fetchone():
        conn.close()
        raise ValueError(f"target series {target_series_id} not found")
    conn.close()
    target_episode_id = add_episode(target_series_id, season_number, episode_number, name)

    conn = _connect()
    old_episode_row = conn.execute("SELECT series_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    conn.execute("UPDATE episode_sources SET episode_id=? WHERE id=? AND episode_id=?", (target_episode_id, source_id, episode_id))
    _purge_if_sourceless_episode(conn, episode_id)
    if old_episode_row:
        _purge_if_sourceless_series(conn, old_episode_row["series_id"])
    _commit_with_retry(conn)
    conn.close()
    return target_episode_id


def list_failing_episode_sources_for_series(series_id: int, min_failures: int = 1) -> list[dict]:
    """Surfaces the exact pattern a healthy-looking series can hide: one
    provider whose copies are almost all broken never shows up as an
    outright failure if a second, healthier provider covers the same
    episodes -- fallback succeeds, so vod_stream_failures never sees it (see
    record_source_failure). Grouping by provider here is what lets an admin
    spot "Mega-OTT's copies of this show are all dead" at a glance instead
    of noticing it one broken episode at a time."""
    conn = _connect()
    rows = conn.execute("""
        SELECT es.id AS source_id, es.provider_id, p.name AS provider_name,
               es.episode_id, e.season_number, e.episode_number,
               es.consecutive_failures, es.last_failed_at
        FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        JOIN providers p ON p.id = es.provider_id
        WHERE e.series_id = ? AND es.consecutive_failures >= ?
        ORDER BY p.name, e.season_number, e.episode_number
    """, (series_id, min_failures)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_provider_sources_from_series(series_id: int, provider_id: int) -> int:
    """Bulk version of delete_episode_source for the fix list_failing_episode_sources_for_series
    exists to enable: once a provider's copies of a series are confirmed
    dead, removing them one episode at a time is real toil for a 20+
    episode season. Same file-cleanup and sourceless-purge behavior as the
    single-source delete, just looped. Returns the number of sources
    removed."""
    conn = _connect()
    rows = conn.execute("""
        SELECT es.id AS source_id, es.episode_id, es.local_file_path
        FROM episode_sources es JOIN episodes e ON e.id = es.episode_id
        WHERE e.series_id = ? AND es.provider_id = ?
    """, (series_id, provider_id)).fetchall()
    conn.close()
    for row in rows:
        delete_episode_source(row["episode_id"], row["source_id"])
    return len(rows)


def remove_series_from_category(series_id: int, category_id: int) -> None:
    conn = _connect()
    conn.execute(
        "DELETE FROM series_category_placements WHERE series_id=? AND category_id=?",
        (series_id, category_id),
    )
    _commit_with_retry(conn)
    conn.close()


def remove_series_from_all_categories(series_id: int) -> None:
    """See remove_movie_from_all_categories -- same reasoning, series side."""
    conn = _connect()
    conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (series_id,))
    _commit_with_retry(conn)
    conn.close()


def place_series_in_category(series_id: int, category_id: int) -> int:
    """Same virtual-file mechanism as place_movie_in_category, scoped to series."""
    conn = _connect()
    flagged = conn.execute("SELECT needs_year_review FROM series WHERE id=?", (series_id,)).fetchone()
    if flagged and flagged["needs_year_review"]:
        conn.close()
        raise ValueError(f"series {series_id} needs year review before it can be placed in a category")
    existing = conn.execute(
        "SELECT export_series_id FROM series_category_placements WHERE series_id=? AND category_id=?",
        (series_id, category_id),
    ).fetchone()
    if existing:
        conn.close()
        return existing["export_series_id"]

    placement_count = conn.execute(
        "SELECT COUNT(*) c FROM series_category_placements WHERE series_id=?", (series_id,)
    ).fetchone()["c"]
    name_suffix = _ZW_MARKER * placement_count

    # See place_movie_in_category's matching comment -- same race, same fix.
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(export_series_id), ?) m FROM series_category_placements",
            (_SERIES_EXPORT_BASE - 1,),
        ).fetchone()
        export_series_id = max(row["m"] + 1, _SERIES_EXPORT_BASE)

        conn.execute(
            "INSERT INTO series_category_placements (series_id, category_id, export_series_id, name_suffix) VALUES (?,?,?,?)",
            (series_id, category_id, export_series_id, name_suffix),
        )
        _commit_with_retry(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return export_series_id


def bulk_place_series_in_category(series_ids: list[int], category_id: int) -> int:
    """Batch equivalent of place_series_in_category — see bulk_place_movies_in_category
    (including the _chunked rationale)."""
    if not series_ids:
        return 0
    conn = _connect()
    already: set[int] = set()
    for chunk in _chunked(series_ids):
        placeholders = ",".join("?" for _ in chunk)
        already.update(r["series_id"] for r in conn.execute(
            f"SELECT series_id FROM series_category_placements WHERE category_id=? AND series_id IN ({placeholders})",
            (category_id, *chunk),
        ).fetchall())
    flagged: set[int] = set()
    for chunk in _chunked(series_ids):
        placeholders = ",".join("?" for _ in chunk)
        flagged.update(r["id"] for r in conn.execute(
            f"SELECT id FROM series WHERE needs_year_review=1 AND id IN ({placeholders})", chunk,
        ).fetchall())
    if flagged:
        logger.info("[vod_db] skipping %d series still needing year review for category=%s", len(flagged), category_id)
    to_place = [sid for sid in series_ids if sid not in already and sid not in flagged]
    if not to_place:
        conn.close()
        return 0

    counts: dict[int, int] = {}
    for chunk in _chunked(to_place):
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT series_id, COUNT(*) c FROM series_category_placements WHERE series_id IN ({placeholders}) GROUP BY series_id",
            chunk,
        ).fetchall():
            counts[r["series_id"]] = r["c"]

    row = conn.execute(
        "SELECT COALESCE(MAX(export_series_id), ?) m FROM series_category_placements",
        (_SERIES_EXPORT_BASE - 1,),
    ).fetchone()
    next_id = max(row["m"] + 1, _SERIES_EXPORT_BASE)

    rows = []
    for sid in to_place:
        name_suffix = _ZW_MARKER * counts.get(sid, 0)
        rows.append((sid, category_id, next_id, name_suffix))
        next_id += 1

    conn.executemany(
        "INSERT INTO series_category_placements (series_id, category_id, export_series_id, name_suffix) VALUES (?,?,?,?)",
        rows,
    )
    _commit_with_retry(conn)
    conn.close()
    return len(rows)


def get_series_export_rows() -> list[dict]:
    """One row per (series, category placement) for the XC get_series export."""
    conn = _connect()
    rows = conn.execute("""
        SELECT
            s.id AS series_id, s.name AS name, s.year AS year, s.genre AS genre,
            s.description AS description, s.poster_url AS poster_url,
            s.cast_list AS cast_list, s.director AS director, s.country AS country,
            s.rating AS rating, s.release_date AS release_date,
            p.export_series_id AS export_series_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name
        FROM series_category_placements p
        JOIN series s ON s.id = p.series_id
        JOIN categories c ON c.id = p.category_id AND c.is_active = 1
        ORDER BY s.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_series_export_row_by_export_id(export_series_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("""
        SELECT
            s.id AS series_id, s.name AS name, s.year AS year, s.genre AS genre,
            s.description AS description, s.poster_url AS poster_url,
            s.cast_list AS cast_list, s.director AS director, s.country AS country,
            s.rating AS rating, s.release_date AS release_date,
            p.export_series_id AS export_series_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name
        FROM series_category_placements p
        JOIN series s ON s.id = p.series_id
        JOIN categories c ON c.id = p.category_id AND c.is_active = 1
        WHERE p.export_series_id = ?
        LIMIT 1
    """, (export_series_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _episode_best_source_cte() -> str:
    """See _best_source_cte's identical docstring -- episode equivalent."""
    return f"""
    WITH best_source AS (
        SELECT es.*, ROW_NUMBER() OVER (
            PARTITION BY episode_id ORDER BY {_source_order_by('es', 'pr')}
        ) AS rn
        FROM episode_sources es
        JOIN providers pr ON pr.id = es.provider_id
        WHERE pr.is_active = 1
    )
"""


def get_episode_source_for_streaming(source_id: int) -> dict | None:
    """Episode equivalent of get_movie_source_for_streaming — see there."""
    conn = _connect()
    row = conn.execute("""
        SELECT es.provider_id, es.provider_stream_id, es.container_extension, es.plex_rating_key, es.local_file_path,
               e.id AS episode_id, e.name AS episode_name, e.season_number AS season_number, e.episode_number AS episode_number,
               e.duration_secs AS duration_secs, s.id AS series_id, s.name AS series_name
        FROM episode_sources es
        JOIN providers p ON p.id = es.provider_id
        JOIN episodes e ON e.id = es.episode_id
        JOIN series s ON s.id = e.series_id
        WHERE es.id = ? AND p.is_active = 1
    """, (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_episode_sources_for_streaming(episode_id: int) -> list[dict]:
    """Episode equivalent of list_movie_sources_for_streaming — see there."""
    conn = _connect()
    rows = conn.execute(f"""
        SELECT es.id AS source_id, es.provider_id, es.provider_stream_id, es.container_extension,
               es.plex_rating_key, es.local_file_path, es.consecutive_failures AS consecutive_failures,
               p.name AS provider_name
        FROM episode_sources es
        JOIN providers p ON p.id = es.provider_id
        WHERE es.episode_id = ? AND p.is_active = 1
        ORDER BY {_source_order_by('es', 'p')}
    """, (episode_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_episode_export_row(episode_id: int) -> dict | None:
    """Episodes don't need the virtual-file dedup trick (only the parent series
    is placed into categories), so the export id is just a stable offset of
    the episode's own row id."""
    conn = _connect()
    row = conn.execute(_episode_best_source_cte() + """
        SELECT
            e.id AS episode_id, e.series_id AS series_id, e.season_number AS season_number,
            e.episode_number AS episode_number, e.name AS name, e.description AS description,
            e.duration_secs AS duration_secs,
            es.provider_id AS provider_id, es.provider_stream_id AS provider_stream_id,
            es.container_extension AS container_extension
        FROM episodes e
        LEFT JOIN best_source es ON es.episode_id = e.id AND es.rn = 1
        WHERE e.id = ?
    """, (episode_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["export_episode_id"] = _EPISODE_EXPORT_BASE + result["episode_id"]
    return result


def get_episode_export_row_by_export_id(export_episode_id: int) -> dict | None:
    return get_episode_export_row(export_episode_id - _EPISODE_EXPORT_BASE)


def get_episode_export_rows_for_series(series_id: int) -> list[dict]:
    """Bulk equivalent of calling get_episode_export_row once per episode --
    xc_server's get_series_info action used to do exactly that N+1 loop
    (list_episodes, then a separate query per episode), which is fine for a
    short series but opens one SQLite connection per episode synchronously
    inside an async request handler -- for a long-running show (hundreds of
    episodes) that's real blocking time on the single event-loop thread,
    confirmed live: a real Dispatcharr full-catalog sync hitting
    get_series_info for many series in a row froze the whole server for
    every other request until it finished."""
    conn = _connect()
    rows = conn.execute(_episode_best_source_cte() + """
        SELECT
            e.id AS episode_id, e.series_id AS series_id, e.season_number AS season_number,
            e.episode_number AS episode_number, e.name AS name, e.description AS description,
            e.duration_secs AS duration_secs,
            es.provider_id AS provider_id, es.provider_stream_id AS provider_stream_id,
            es.container_extension AS container_extension, es.bitrate AS bitrate
        FROM episodes e
        LEFT JOIN best_source es ON es.episode_id = e.id AND es.rn = 1
        WHERE e.series_id = ?
        ORDER BY e.season_number, e.episode_number
    """, (series_id,)).fetchall()
    conn.close()
    results = [dict(r) for r in rows]
    for r in results:
        r["export_episode_id"] = _EPISODE_EXPORT_BASE + r["episode_id"]
    return results


# ── Bulk import ──────────────────────────────────────────────────────────────
# List-level import from a real provider (cheap — name/year/category/stream_id
# only). Runs as a single transaction rather than the usual one-connection-per-
# call pattern, since a real catalog is thousands of rows.

_ADULT_KEYWORDS = ("adult", "xxx", "18+", "porn", "erotic")


def _looks_adult(*category_names) -> bool:
    """Best-effort auto-detect from the provider's own category naming —
    providers almost always segregate adult content into a distinctly-named
    category. Manual overrides (set_movie_adult/set_series_adult) always win;
    this only sets the initial value at creation, never on a later re-import,
    so it doesn't clobber a correction the user already made."""
    for name in category_names:
        if name and any(kw in name.lower() for kw in _ADULT_KEYWORDS):
            return True
    return False


def bulk_import_movies(provider_id: int, items: list[dict], _retry_depth: int = 0) -> dict:
    """items: [{name, year, provider_stream_id, container_extension, provider_category_name, auto_archive}, ...]

    Adult-content auto-detection runs on every import pass (not just first
    creation) so a provider re-categorizing something later still gets
    picked up on the next scheduled/manual refresh — but only ever upgrades
    is_adult to True from a matching category name, never downgrades, and
    never touches a row a human has manually corrected (is_adult_manual=1).

    auto_archive (see vod_importer._should_auto_archive) mirrors the
    is_adult/is_adult_manual upgrade-only pattern in BOTH directions: it can
    archive an item, and if the item is already archived but was archived
    automatically (review_excluded_manual=0), it can also un-archive it once
    no active rule matches it any more -- e.g. an admin removes a category
    from a provider's exclude list, then re-imports. A human's manual
    archive/restore (bulk_set_review_excluded, review_excluded_manual=1) is
    never touched in either direction -- that's what review_excluded_manual
    exists to protect.
    """
    _WRITE_LOCK.acquire()
    try:
        conn = _connect()
        now = _now()
        created = 0
        matched = 0
        flagged = 0
        errors = 0
        archived = 0
        unarchived = 0
        lock_retry_items = []
        # Committed every batch_size items rather than once at the very end (see
        # _commit_with_retry's docstring) -- a real XC catalog is thousands of
        # items, and holding one uncommitted transaction open for the whole loop
        # made this a bad neighbor to every other background writer (enrichment,
        # TMDB sync, category schedules, a concurrent provider's own import):
        # they'd block on the write lock for the full 30s connect timeout and
        # then fail with "database is locked" -- confirmed as the root cause of a
        # real user's flood of exactly that warning during import. 200 wasn't
        # small enough: the writer lock is held for the WHOLE batch (SAVEPOINTs
        # don't release it, only the periodic commit does), and 200 items' worth
        # of per-item SELECT/INSERT/UPDATE queries can itself run long enough to
        # outlast even a 30s busy_timeout under real load -- confirmed live
        # 2026-07-30: a provider's manually-triggered import collided with
        # bulk_enrich_all's 8 concurrent writers running against a different
        # provider and lost ~48% of its items to permanent lock errors even after
        # all 3 retry passes. Matches bulk_import_plex_series's existing 20.
        batch_size = 25
        for i, item in enumerate(items):
            try:
                with _item_savepoint(conn):
                    name = item["name"]
                    year = item.get("year")
                    category_looks_adult = _looks_adult(item.get("provider_category_name"))
                    should_archive = bool(item.get("auto_archive"))
                    # Counted locally and only folded into the real created/matched/
                    # flagged/archived totals once this item's last statement has
                    # actually succeeded -- incrementing the outer counters
                    # immediately would drift them out of sync with what's really
                    # in the DB if a later statement in this same item (e.g. the
                    # movie_sources insert below) goes on to raise and roll this
                    # item back.
                    did_create = did_match = did_flag = did_archive = did_unarchive = False
                    # Primary match: this exact provider+stream_id was already
                    # imported before -- reuse its established movie_id directly,
                    # UNCONDITIONALLY (checked before any name-based matching,
                    # not just for a blank name), rather than re-deriving identity
                    # from name/year on every single re-import. This is what makes
                    # it safe for enrichment (vod_importer.enrich_movie) to
                    # overwrite a raw-filename/placeholder name with the
                    # provider's own clean title without the NEXT re-import
                    # creating a duplicate orphaned row: a re-import's identity
                    # now comes from provider_stream_id, not from re-matching a
                    # (now-different) name string. Real gap closed here, found
                    # live 2026-07-29: enrichment already fetches a movie's clean
                    # title from get_vod_info but had nowhere safe to persist it,
                    # because every earlier version of this function re-derived
                    # identity from (name, year) on every single pass -- writing
                    # the clean name would have silently duplicated on the very
                    # next refresh.
                    existing_source = conn.execute(
                        "SELECT movie_id FROM movie_sources WHERE provider_id=? AND provider_stream_id=?",
                        (provider_id, item["provider_stream_id"]),
                    ).fetchone()
                    if existing_source:
                        movie_id = existing_source["movie_id"]
                        did_match = True
                        existing = conn.execute(
                            "SELECT is_adult, is_adult_manual, review_excluded, review_excluded_manual FROM movies WHERE id=?", (movie_id,)
                        ).fetchone()
                        if existing:
                            if category_looks_adult and not existing["is_adult"] and not existing["is_adult_manual"]:
                                conn.execute("UPDATE movies SET is_adult=1 WHERE id=?", (movie_id,))
                            if should_archive and not existing["review_excluded"] and not existing["review_excluded_manual"]:
                                conn.execute("UPDATE movies SET review_excluded=1 WHERE id=?", (movie_id,))
                                conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
                                did_archive = True
                            elif not should_archive and existing["review_excluded"] and not existing["review_excluded_manual"]:
                                # Mirror of the archive branch above -- no active
                                # rule matches this item any more (see this
                                # function's docstring), so lift an
                                # automatically-applied archive.
                                conn.execute("UPDATE movies SET review_excluded=0 WHERE id=?", (movie_id,))
                                did_unarchive = True
                    elif not name.strip():
                        # A blank provider-supplied name has no real identity to match
                        # on -- treating "" like any other string let unrelated titles
                        # silently collapse into one shared row (a real corruption found
                        # in production: 3 completely unrelated movies from the same
                        # provider, different genres, merged into a single blank-named
                        # entry because they all matched (name='', year=NULL) exactly).
                        # Never match a blank name against anything, including another
                        # blank one -- reaching this branch at all already means the
                        # existing_source check above found no established identity for
                        # this stream, so this is a genuinely new item.
                        placeholder = f"[Untitled] {(item.get('provider_category_name') or '').strip() or 'Unknown'} · stream {item['provider_stream_id']}"
                        cur = conn.execute(
                            "INSERT INTO movies (name, year, is_adult, needs_year_review, review_excluded, created_at) VALUES (?,?,?,?,?,?)",
                            (placeholder, year, int(category_looks_adult), 1, int(should_archive), now),
                        )
                        movie_id = cur.lastrowid
                        did_create = True
                        did_flag = True
                        did_archive = should_archive
                    else:
                        row = conn.execute(
                            "SELECT id, is_adult, is_adult_manual, review_excluded, review_excluded_manual FROM movies WHERE name=? AND year IS ?",
                            (name, year),
                        ).fetchone()
                        if row:
                            movie_id = row["id"]
                            did_match = True
                            if category_looks_adult and not row["is_adult"] and not row["is_adult_manual"]:
                                conn.execute("UPDATE movies SET is_adult=1 WHERE id=?", (movie_id,))
                            if should_archive and not row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE movies SET review_excluded=1 WHERE id=?", (movie_id,))
                                # Becoming archived doesn't just set a flag -- it has to
                                # actually remove any existing category placement, or
                                # Dispatcharr keeps seeing it via whatever category it
                                # was already in (the exact bug this whole block exists
                                # to fix -- see evaluate_smart_category's docstring).
                                conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
                                did_archive = True
                            elif not should_archive and row["review_excluded"] and not row["review_excluded_manual"]:
                                # Mirror of the archive branch above -- see this
                                # function's docstring for why an automatically
                                # applied archive can be automatically lifted too.
                                conn.execute("UPDATE movies SET review_excluded=0 WHERE id=?", (movie_id,))
                                did_unarchive = True
                        elif year is None:
                            # No exact (name, NULL) row, and no year to key an exact match
                            # on -- same reasoning as upsert_movie: exactly one same-named
                            # candidate means this is almost certainly it, just missing year
                            # metadata from this provider; two or more is genuinely
                            # ambiguous, flag rather than silently duplicate.
                            candidates = conn.execute(
                                "SELECT id, review_excluded, review_excluded_manual FROM movies WHERE name=?", (name,)
                            ).fetchall()
                            if len(candidates) == 1:
                                movie_id = candidates[0]["id"]
                                did_match = True
                                # Same archive-upgrade check as the exact (name, year)
                                # match above -- a null-year row matched this way is
                                # just as real a match, and must not silently skip
                                # becoming archived (a real bug: this branch used to
                                # apply no exclusion rules at all, so a null-year
                                # movie/series could never be caught by language/
                                # category import exclusion no matter how many times
                                # the catalog was re-imported).
                                if should_archive and not candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE movies SET review_excluded=1 WHERE id=?", (movie_id,))
                                    conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
                                    did_archive = True
                                elif not should_archive and candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE movies SET review_excluded=0 WHERE id=?", (movie_id,))
                                    did_unarchive = True
                            else:
                                cur = conn.execute(
                                    "INSERT INTO movies (name, year, is_adult, needs_year_review, review_excluded, created_at) VALUES (?,?,?,?,?,?)",
                                    (name, year, int(category_looks_adult), 1 if candidates else 0, int(should_archive), now),
                                )
                                movie_id = cur.lastrowid
                                did_create = True
                                did_archive = should_archive
                                if candidates:
                                    did_flag = True
                        else:
                            cur = conn.execute(
                                "INSERT INTO movies (name, year, is_adult, review_excluded, created_at) VALUES (?,?,?,?,?)",
                                (name, year, int(category_looks_adult), int(should_archive), now),
                            )
                            movie_id = cur.lastrowid
                            did_create = True
                            did_archive = should_archive
                    conn.execute(
                        """INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, provider_category_name, raw_name, added_at, last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                               movie_id=excluded.movie_id, last_seen_at=excluded.last_seen_at, provider_category_name=excluded.provider_category_name,
                               raw_name=excluded.raw_name""",
                        (movie_id, provider_id, item["provider_stream_id"], item.get("container_extension", "mp4"),
                         item.get("provider_category_name"), item.get("raw_name"), now, now),
                    )
                    created += did_create
                    matched += did_match
                    flagged += did_flag
                    archived += did_archive
                    unarchived += did_unarchive
            except sqlite3.OperationalError as exc:
                # A periodic commit (above) frees the write lock regularly, but a
                # concurrent writer can still grab it in the gap between this
                # item's statements and the next periodic commit -- that's
                # transient contention, not a bad item, so it gets one more pass
                # after this batch finishes instead of being counted as a
                # permanent failure (this is the exact "database is locked"
                # flood a real user hit during import -- items were being
                # silently and permanently dropped by this, not actually
                # malformed).
                if "locked" in str(exc).lower() and _retry_depth < _MAX_LOCK_RETRY_DEPTH:
                    lock_retry_items.append(item)
                else:
                    errors += 1
                    logger.warning("[vod_db] bulk_import_movies: skipped item name=%r stream_id=%r: %s",
                                    item.get("name"), item.get("provider_stream_id"), exc)
            except Exception as exc:
                errors += 1
                logger.warning("[vod_db] bulk_import_movies: skipped item name=%r stream_id=%r: %s",
                                item.get("name"), item.get("provider_stream_id"), exc)
            finally:
                # In a `finally` (not inline after the try/except) so this still
                # fires on the blank-name branch's `continue` -- that continue
                # jumps straight to the next loop iteration and would otherwise
                # skip this check entirely for that item's index.
                if (i + 1) % batch_size == 0:
                    _commit_with_retry(conn)
                    # Release+reacquire between batches -- holding the lock for the
                    # WHOLE function (a large catalog can take many minutes) blocked
                    # every other writer with no timeout, which could exhaust the
                    # async thread pool if enough piled up waiting -- confirmed live
                    # 2026-07-31 as the cause of Dispatcharr's VOD detail-refresh
                    # requests hanging/500ing during a large concurrent import, fixed
                    # by matching the lock's hold window to one batch's real SQLite
                    # write-transaction span instead of the whole import.
                    _WRITE_LOCK.release()
                    _WRITE_LOCK.acquire()
        _commit_with_retry(conn)
        conn.close()
    finally:
        _WRITE_LOCK.release()

    if lock_retry_items:
        time.sleep(0.5 * (_retry_depth + 1))
        logger.info("[vod_db] bulk_import_movies: retrying %d item(s) after transient lock contention (pass %d/%d)",
                     len(lock_retry_items), _retry_depth + 1, _MAX_LOCK_RETRY_DEPTH)
        retry_result = bulk_import_movies(provider_id, lock_retry_items, _retry_depth=_retry_depth + 1)
        created += retry_result["movies_created"]
        matched += retry_result["movies_matched"]
        archived += retry_result["movies_archived"]
        unarchived += retry_result["movies_unarchived"]
        flagged += retry_result["flagged_for_review"]
        errors += retry_result["errors"]

    return {"movies_created": created, "movies_matched": matched, "movies_archived": archived, "movies_unarchived": unarchived, "total": len(items), "flagged_for_review": flagged, "errors": errors}


def bulk_import_series(provider_id: int, items: list[dict], _retry_depth: int = 0) -> dict:
    """items: [{name, year, provider_series_id, provider_category_name}, ...]

    Series-level only (XC series don't carry a directly-playable stream_id —
    only their episodes do, which are fetched lazily via get_series_info,
    same as detail enrichment). import_provider_id/import_provider_series_id
    are stamped so enrich_series() can call straight back to the right
    provider instead of re-scanning every provider for a name match."""
    _WRITE_LOCK.acquire()
    try:
        conn = _connect()
        now = _now()
        created = 0
        matched = 0
        flagged = 0
        errors = 0
        archived = 0
        unarchived = 0
        lock_retry_items = []
        # See bulk_import_movies's identical comment -- same fix, same reason.
        batch_size = 25
        for i, item in enumerate(items):
            try:
                with _item_savepoint(conn):
                    name = item["name"]
                    year = item.get("year")
                    category_looks_adult = _looks_adult(item.get("provider_category_name"))
                    should_archive = bool(item.get("auto_archive"))
                    # See bulk_import_movies's identical did_create/did_match/did_flag
                    # comment -- folded into the real counters only after this item's
                    # last statement has actually succeeded.
                    did_create = did_match = did_flag = did_archive = did_unarchive = False
                    # Primary match: this exact provider+series_id was already
                    # imported before -- reuse its established identity directly,
                    # UNCONDITIONALLY (not just for a blank name), same reasoning
                    # as bulk_import_movies's identical hoist above (see its
                    # comment for the full explanation of why this is what makes
                    # it safe for enrichment to later overwrite a raw/placeholder
                    # name with the provider's clean title without the next
                    # re-import creating a duplicate orphaned row).
                    existing = conn.execute(
                        "SELECT id, is_adult, is_adult_manual, review_excluded, review_excluded_manual FROM series WHERE import_provider_id=? AND import_provider_series_id=?",
                        (provider_id, item.get("provider_series_id")),
                    ).fetchone()
                    if existing:
                        did_match = True
                        # Real bug found live 2026-07-29: this value was captured
                        # in `item` on every single import pass but never
                        # actually written anywhere -- episode_sources.
                        # provider_category_name (what evaluate_smart_category's
                        # provider_category rule field and auto-create-categories
                        # both actually read) was NULL for every episode of every
                        # series ever imported through this path, so series-side
                        # provider-category matching never worked for any
                        # provider, not just whichever one happened to be tested.
                        # Stored here (unconditionally overwritten -- provider-
                        # sourced, not user-editable) so enrich_series can read it
                        # back and stamp it onto each episode as episodes are
                        # discovered (episodes aren't known yet at this cheap
                        # bulk-list stage, only lazily via get_series_info).
                        conn.execute("UPDATE series SET provider_category_name=?, raw_name=? WHERE id=?", (item.get("provider_category_name"), item.get("raw_name"), existing["id"]))
                        if category_looks_adult and not existing["is_adult"] and not existing["is_adult_manual"]:
                            conn.execute("UPDATE series SET is_adult=1 WHERE id=?", (existing["id"],))
                        if should_archive and not existing["review_excluded"] and not existing["review_excluded_manual"]:
                            conn.execute("UPDATE series SET review_excluded=1 WHERE id=?", (existing["id"],))
                            conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (existing["id"],))
                            did_archive = True
                        elif not should_archive and existing["review_excluded"] and not existing["review_excluded_manual"]:
                            # Mirror of the archive branch above -- see
                            # bulk_import_movies's docstring for why an
                            # automatically applied archive can be
                            # automatically lifted too.
                            conn.execute("UPDATE series SET review_excluded=0 WHERE id=?", (existing["id"],))
                            did_unarchive = True
                    elif not name.strip():
                        # Same reasoning as bulk_import_movies's identical guard -- a
                        # blank name has no real identity to match on, so never match it
                        # against anything, including another blank one. Reaching this
                        # branch at all already means the existing-identity check above
                        # found no established row for this provider+series_id, so this
                        # is a genuinely new item.
                        placeholder = f"[Untitled] {(item.get('provider_category_name') or '').strip() or 'Unknown'} · series {item.get('provider_series_id')}"
                        conn.execute(
                            "INSERT INTO series (name, year, is_adult, needs_year_review, review_excluded, import_provider_id, import_provider_series_id, provider_category_name, raw_name, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (placeholder, year, int(category_looks_adult), 1, int(should_archive), provider_id, item.get("provider_series_id"), item.get("provider_category_name"), item.get("raw_name"), now),
                        )
                        did_create = True
                        did_flag = True
                        did_archive = should_archive
                    else:
                        row = conn.execute(
                            "SELECT id, is_adult, is_adult_manual, review_excluded, review_excluded_manual, import_provider_id FROM series WHERE name=? AND year IS ?",
                            (name, year),
                        ).fetchone()
                        if row:
                            did_match = True
                            # See the identical comment on the existing-identity
                            # match branch above -- same fix, same reasoning.
                            conn.execute("UPDATE series SET provider_category_name=?, raw_name=? WHERE id=?", (item.get("provider_category_name"), item.get("raw_name"), row["id"]))
                            if category_looks_adult and not row["is_adult"] and not row["is_adult_manual"]:
                                conn.execute("UPDATE series SET is_adult=1 WHERE id=?", (row["id"],))
                            if should_archive and not row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE series SET review_excluded=1 WHERE id=?", (row["id"],))
                                # See the identical comment in bulk_import_movies -- this
                                # is what actually makes "archived" hide it from
                                # Dispatcharr, not just the flag on its own.
                                conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (row["id"],))
                                did_archive = True
                            elif not should_archive and row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE series SET review_excluded=0 WHERE id=?", (row["id"],))
                                did_unarchive = True
                            if row["import_provider_id"] is None:
                                # This series previously had no working way to fetch episode
                                # detail (e.g. its only prior source's provider was later
                                # deleted) -- this provider can, so give it one rather than
                                # leaving it permanently stuck.
                                conn.execute(
                                    "UPDATE series SET import_provider_id=?, import_provider_series_id=? WHERE id=?",
                                    (provider_id, item.get("provider_series_id"), row["id"]),
                                )
                        elif year is None:
                            # Same reasoning as bulk_import_movies above.
                            candidates = conn.execute(
                                "SELECT id, import_provider_id, review_excluded, review_excluded_manual FROM series WHERE name=?",
                                (name,),
                            ).fetchall()
                            if len(candidates) == 1:
                                did_match = True
                                conn.execute("UPDATE series SET provider_category_name=?, raw_name=? WHERE id=?", (item.get("provider_category_name"), item.get("raw_name"), candidates[0]["id"]))
                                if candidates[0]["import_provider_id"] is None:
                                    conn.execute(
                                        "UPDATE series SET import_provider_id=?, import_provider_series_id=? WHERE id=?",
                                        (provider_id, item.get("provider_series_id"), candidates[0]["id"]),
                                    )
                                # Same archive-upgrade check as the exact (name, year)
                                # match above -- see the identical comment in
                                # bulk_import_movies for why this branch needs it too.
                                if should_archive and not candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE series SET review_excluded=1 WHERE id=?", (candidates[0]["id"],))
                                    conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (candidates[0]["id"],))
                                    did_archive = True
                                elif not should_archive and candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE series SET review_excluded=0 WHERE id=?", (candidates[0]["id"],))
                                    did_unarchive = True
                            else:
                                conn.execute(
                                    "INSERT INTO series (name, year, is_adult, needs_year_review, review_excluded, import_provider_id, import_provider_series_id, provider_category_name, raw_name, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (name, year, int(category_looks_adult), 1 if candidates else 0, int(should_archive), provider_id, item.get("provider_series_id"), item.get("provider_category_name"), item.get("raw_name"), now),
                                )
                                did_create = True
                                did_archive = should_archive
                                if candidates:
                                    did_flag = True
                        else:
                            conn.execute(
                                "INSERT INTO series (name, year, is_adult, review_excluded, import_provider_id, import_provider_series_id, provider_category_name, raw_name, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (name, year, int(category_looks_adult), int(should_archive), provider_id, item.get("provider_series_id"), item.get("provider_category_name"), item.get("raw_name"), now),
                            )
                            did_create = True
                            did_archive = should_archive
                    created += did_create
                    matched += did_match
                    flagged += did_flag
                    archived += did_archive
                    unarchived += did_unarchive
            except sqlite3.OperationalError as exc:
                # See bulk_import_movies's identical handler -- transient lock
                # contention gets a retry pass instead of a permanent, silent
                # data loss.
                if "locked" in str(exc).lower() and _retry_depth < _MAX_LOCK_RETRY_DEPTH:
                    lock_retry_items.append(item)
                else:
                    errors += 1
                    logger.warning("[vod_db] bulk_import_series: skipped item name=%r series_id=%r: %s",
                                    item.get("name"), item.get("provider_series_id"), exc)
            except Exception as exc:
                errors += 1
                logger.warning("[vod_db] bulk_import_series: skipped item name=%r series_id=%r: %s",
                                item.get("name"), item.get("provider_series_id"), exc)
            finally:
                # See bulk_import_movies's identical comment -- must be `finally`
                # so the blank-name branch's `continue` doesn't skip it.
                if (i + 1) % batch_size == 0:
                    _commit_with_retry(conn)
                    # Release+reacquire between batches -- holding the lock for the
                    # WHOLE function (a large catalog can take many minutes) blocked
                    # every other writer with no timeout, which could exhaust the
                    # async thread pool if enough piled up waiting -- confirmed live
                    # 2026-07-31 as the cause of Dispatcharr's VOD detail-refresh
                    # requests hanging/500ing during a large concurrent import, fixed
                    # by matching the lock's hold window to one batch's real SQLite
                    # write-transaction span instead of the whole import.
                    _WRITE_LOCK.release()
                    _WRITE_LOCK.acquire()
        _commit_with_retry(conn)
        conn.close()
    finally:
        _WRITE_LOCK.release()

    if lock_retry_items:
        time.sleep(0.5 * (_retry_depth + 1))
        logger.info("[vod_db] bulk_import_series: retrying %d item(s) after transient lock contention (pass %d/%d)",
                     len(lock_retry_items), _retry_depth + 1, _MAX_LOCK_RETRY_DEPTH)
        retry_result = bulk_import_series(provider_id, lock_retry_items, _retry_depth=_retry_depth + 1)
        created += retry_result["series_created"]
        matched += retry_result["series_matched"]
        archived += retry_result["series_archived"]
        unarchived += retry_result["series_unarchived"]
        flagged += retry_result["flagged_for_review"]
        errors += retry_result["errors"]

    return {"series_created": created, "series_matched": matched, "series_archived": archived, "series_unarchived": unarchived, "total": len(items), "flagged_for_review": flagged, "errors": errors}


_PLEX_DETAIL_FIELDS = ("genre", "description", "director", "cast_list", "poster_url", "last_enriched_at", "rating", "release_date")


def _plex_detail_update_sql(detail: dict, tmdb_id: str | None) -> tuple[str, list]:
    """SET clause/params for a Plex/Emby detail-refresh UPDATE. Every field
    in `detail` is overwritten unconditionally (Plex/Emby hand back
    authoritative full detail on every listing pass) except tmdb_id, which
    upgrades via COALESCE, same upgrade-only spirit as is_adult_manual/
    review_excluded_manual elsewhere -- a transient missing id in one pass
    (e.g. an unmatched item in the source library) must never erase an id
    already captured on a previous one."""
    sets = ", ".join(f"{k}=?" for k in detail)
    params = list(detail.values())
    if tmdb_id:
        sets += ", tmdb_id=COALESCE(tmdb_id, ?)"
        params.append(tmdb_id)
    return sets, params


def provider_stream_ids_with_episode_source(provider_id: int) -> set[str]:
    """Every provider_stream_id already registered as an episode for this
    provider -- dispatcharr_dvr_importer calls this before classifying a
    recording as a movie, so a stream_id already correctly living as an
    episode (from an earlier import pass, or a manual repair) never gets
    silently duplicated into a movie on a later pass just because that
    pass's season/episode resolution came up empty (e.g. a show with no
    EPG sub_title, whose Dispatcharr Recording object was created before a
    since-fixed capture bug and can never retroactively gain the data).
    Real bug found live 2026-07-28: the exact same General Hospital
    recording got reclassified as a duplicate movie on the very next import
    pass after being manually corrected, because nothing here checked
    first."""
    conn = _connect()
    rows = conn.execute(
        "SELECT provider_stream_id FROM episode_sources WHERE provider_id=?", (provider_id,)
    ).fetchall()
    conn.close()
    return {r["provider_stream_id"] for r in rows}


def bulk_import_plex_movies(provider_id: int, items: list[dict]) -> dict:
    """Plex counterpart to bulk_import_movies — one connection/transaction for
    the whole library instead of one round-trip per movie (same fix as
    bulk_place_movies_in_category: real-sized libraries time out otherwise).
    Plex hands back full detail up front, so this also writes genre/
    description/etc. in the same pass rather than needing a later enrichment
    step. items: [{name, year, provider_stream_id, container_extension, genre,
    description, director, cast_list, poster_url, last_enriched_at,
    auto_archive}, ...]

    auto_archive (see vod_importer._should_auto_archive, called with no
    category args -- Plex/Emby have no XC-style flat category list to filter
    on, only language rules apply here, see vod_manager-i4i) mirrors
    bulk_import_movies' upgrade-only-both-directions archive/unarchive
    semantics exactly -- see that function's docstring."""
    _WRITE_LOCK.acquire()
    try:
        conn = _connect()
        now = _now()
        created = 0
        matched = 0
        flagged = 0
        errors = 0
        archived = 0
        unarchived = 0
        # See bulk_import_movies's identical comment -- same fix, same reason.
        batch_size = 25
        for i, item in enumerate(items):
            try:
                with _item_savepoint(conn):
                    name = item["name"]
                    year = item.get("year")
                    should_archive = bool(item.get("auto_archive"))
                    detail = {k: item.get(k) for k in _PLEX_DETAIL_FIELDS}
                    # See bulk_import_movies's identical did_create/did_match/did_flag
                    # comment -- folded into the real counters only after this item's
                    # last statement has actually succeeded.
                    did_create = did_match = did_flag = did_archive = did_unarchive = False
                    # Primary match: this exact provider+stream_id was already imported
                    # before -- reuse its established movie_id directly, UNCONDITIONALLY
                    # (checked before any name-based matching, not just for a blank
                    # name). Mirrors the identical fix in bulk_import_movies (see its
                    # docstring) -- this function had the same gap until now: only the
                    # blank-name path checked provider_stream_id first, so a title
                    # changing between Plex syncs (e.g. Plex's own metadata refresh, or
                    # this function's own detail write below) could silently duplicate
                    # a movie on the very next import instead of recognizing it via its
                    # stable Plex source id.
                    existing_source = conn.execute(
                        "SELECT movie_id FROM movie_sources WHERE provider_id=? AND provider_stream_id=?",
                        (provider_id, item["provider_stream_id"]),
                    ).fetchone()
                    if existing_source:
                        movie_id = existing_source["movie_id"]
                        did_match = True
                        sets, set_params = _plex_detail_update_sql(detail, item.get("tmdb_id"))
                        conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*set_params, now, movie_id))
                        existing = conn.execute(
                            "SELECT review_excluded, review_excluded_manual FROM movies WHERE id=?", (movie_id,)
                        ).fetchone()
                        if existing:
                            if should_archive and not existing["review_excluded"] and not existing["review_excluded_manual"]:
                                conn.execute("UPDATE movies SET review_excluded=1 WHERE id=?", (movie_id,))
                                conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
                                did_archive = True
                            elif not should_archive and existing["review_excluded"] and not existing["review_excluded_manual"]:
                                conn.execute("UPDATE movies SET review_excluded=0 WHERE id=?", (movie_id,))
                                did_unarchive = True
                    elif not name.strip():
                        # Same guard as bulk_import_movies -- a blank name has no real
                        # identity to match on, so never match it against anything,
                        # including another blank one. Reaching this branch at all
                        # already means the existing_source check above found no
                        # established identity for this stream, so this is genuinely new.
                        placeholder = f"[Untitled] Plex · stream {item['provider_stream_id']}"
                        cur = conn.execute(
                            "INSERT INTO movies (name, year, needs_year_review, review_excluded, created_at) VALUES (?,?,?,?,?)",
                            (placeholder, year, 1, int(should_archive), now),
                        )
                        movie_id = cur.lastrowid
                        did_create = True
                        did_flag = True
                        did_archive = should_archive
                    else:
                        row = conn.execute(
                            "SELECT id, review_excluded, review_excluded_manual FROM movies WHERE name=? AND year IS ?",
                            (name, year),
                        ).fetchone()
                        if row:
                            movie_id = row["id"]
                            did_match = True
                            sets, set_params = _plex_detail_update_sql(detail, item.get("tmdb_id"))
                            conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*set_params, now, movie_id))
                            if should_archive and not row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE movies SET review_excluded=1 WHERE id=?", (movie_id,))
                                conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
                                did_archive = True
                            elif not should_archive and row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE movies SET review_excluded=0 WHERE id=?", (movie_id,))
                                did_unarchive = True
                        elif year is None:
                            # Same reasoning as bulk_import_movies. Still writes full detail
                            # even when flagged -- more info for whoever reviews it later.
                            candidates = conn.execute(
                                "SELECT id, review_excluded, review_excluded_manual FROM movies WHERE name=?", (name,)
                            ).fetchall()
                            if len(candidates) == 1:
                                movie_id = candidates[0]["id"]
                                did_match = True
                                sets, set_params = _plex_detail_update_sql(detail, item.get("tmdb_id"))
                                conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*set_params, now, movie_id))
                                if should_archive and not candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE movies SET review_excluded=1 WHERE id=?", (movie_id,))
                                    conn.execute("DELETE FROM movie_category_placements WHERE movie_id=?", (movie_id,))
                                    did_archive = True
                                elif not should_archive and candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE movies SET review_excluded=0 WHERE id=?", (movie_id,))
                                    did_unarchive = True
                            else:
                                cols = ["name", "year", "needs_year_review", "review_excluded", *detail.keys()]
                                vals = [name, year, 1 if candidates else 0, int(should_archive), *detail.values()]
                                if item.get("tmdb_id"):
                                    cols.append("tmdb_id")
                                    vals.append(item["tmdb_id"])
                                placeholders = ", ".join("?" for _ in cols)
                                cur = conn.execute(f"INSERT INTO movies ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
                                movie_id = cur.lastrowid
                                did_create = True
                                did_archive = should_archive
                                if candidates:
                                    did_flag = True
                        else:
                            cols = ["name", "year", "review_excluded", *detail.keys()]
                            vals = [name, year, int(should_archive), *detail.values()]
                            if item.get("tmdb_id"):
                                cols.append("tmdb_id")
                                vals.append(item["tmdb_id"])
                            placeholders = ", ".join("?" for _ in cols)
                            cur = conn.execute(f"INSERT INTO movies ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
                            movie_id = cur.lastrowid
                            did_create = True
                            did_archive = should_archive
                    conn.execute(
                        """INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, plex_rating_key, file_size_bytes, provider_category_name, added_at, last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                               movie_id=excluded.movie_id, last_seen_at=excluded.last_seen_at, plex_rating_key=excluded.plex_rating_key,
                               file_size_bytes=excluded.file_size_bytes, provider_category_name=excluded.provider_category_name""",
                        (movie_id, provider_id, item["provider_stream_id"], item.get("container_extension", "mp4"), item.get("plex_rating_key"), item.get("file_size_bytes"), item.get("provider_category_name"), now, now),
                    )
                    created += did_create
                    matched += did_match
                    flagged += did_flag
                    archived += did_archive
                    unarchived += did_unarchive
            except Exception as exc:
                errors += 1
                logger.warning("[vod_db] bulk_import_plex_movies: skipped item name=%r stream_id=%r: %s",
                                item.get("name"), item.get("provider_stream_id"), exc)
            if (i + 1) % batch_size == 0:
                _commit_with_retry(conn)
                # Release+reacquire between batches -- holding the lock for the
                # WHOLE function (a large catalog can take many minutes) blocked
                # every other writer with no timeout, which could exhaust the
                # async thread pool if enough piled up waiting -- confirmed live
                # 2026-07-31 as the cause of Dispatcharr's VOD detail-refresh
                # requests hanging/500ing during a large concurrent import, fixed
                # by matching the lock's hold window to one batch's real SQLite
                # write-transaction span instead of the whole import.
                _WRITE_LOCK.release()
                _WRITE_LOCK.acquire()
        _commit_with_retry(conn)
        conn.close()
    finally:
        _WRITE_LOCK.release()
    return {"movies_created": created, "movies_matched": matched, "total": len(items), "flagged_for_review": flagged,
            "archived": archived, "unarchived": unarchived, "errors": errors}


def bulk_import_plex_series(provider_id: int, items: list[dict]) -> dict:
    """Plex counterpart to bulk_import_series — same single-transaction fix,
    and also writes every episode (Plex's allLeaves gives us all of them up
    front, unlike XC's lazy per-series fetch) in the same pass. items:
    [{name, year, provider_series_id, genre, description, director,
    cast_list, poster_url, last_enriched_at, auto_archive, episodes: [{season_number,
    episode_number, name, description, duration_secs, provider_stream_id,
    container_extension}, ...]}, ...]

    auto_archive -- see bulk_import_plex_movies' identical docstring note."""
    _WRITE_LOCK.acquire()
    try:
        conn = _connect()
        now = _now()
        series_created = 0
        series_matched = 0
        episodes_total = 0
        errors = 0
        episode_errors = 0
        archived = 0
        unarchived = 0
        batch_size = 20
        for i, item in enumerate(items):
            try:
                with _item_savepoint(conn):
                    name = item["name"]
                    year = item.get("year")
                    should_archive = bool(item.get("auto_archive"))
                    detail = {k: item.get(k) for k in _PLEX_DETAIL_FIELDS}
                    # See bulk_import_movies's identical did_create/did_match comment --
                    # folded into the real counters only once the series-level
                    # statement that follows has actually succeeded.
                    did_create = did_match = did_archive = did_unarchive = False
                    # Primary match: this exact provider+series_id was already imported
                    # before -- reuse its established series_id directly, UNCONDITIONALLY
                    # (checked before any name-based matching, not just for a blank
                    # name). Mirrors the identical fix in bulk_import_series/
                    # bulk_import_plex_movies (see their docstrings) -- this function
                    # had the same gap until now: only the blank-name path checked
                    # import_provider_id/import_provider_series_id first, so a title
                    # changing between Plex syncs could silently duplicate a series on
                    # the very next import instead of recognizing it via its stable id.
                    existing = conn.execute(
                        "SELECT id, review_excluded, review_excluded_manual FROM series WHERE import_provider_id=? AND import_provider_series_id=?",
                        (provider_id, item.get("provider_series_id")),
                    ).fetchone()
                    if existing:
                        series_id = existing["id"]
                        sets, set_params = _plex_detail_update_sql(detail, item.get("tmdb_id"))
                        conn.execute(f"UPDATE series SET {sets}, updated_at=? WHERE id=?", (*set_params, now, series_id))
                        did_match = True
                        if should_archive and not existing["review_excluded"] and not existing["review_excluded_manual"]:
                            conn.execute("UPDATE series SET review_excluded=1 WHERE id=?", (series_id,))
                            conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (series_id,))
                            did_archive = True
                        elif not should_archive and existing["review_excluded"] and not existing["review_excluded_manual"]:
                            conn.execute("UPDATE series SET review_excluded=0 WHERE id=?", (series_id,))
                            did_unarchive = True
                    elif not name.strip():
                        # Same guard as bulk_import_series -- a blank name has no real
                        # identity to match on, so never match it against anything,
                        # including another blank one. Reaching this branch at all
                        # already means the existing check above found no established
                        # identity for this series, so this is genuinely new.
                        placeholder = f"[Untitled] Plex · series {item.get('provider_series_id')}"
                        cols = ["name", "year", "needs_year_review", "review_excluded", "import_provider_id", "import_provider_series_id", *detail.keys()]
                        vals = [placeholder, year, 1, int(should_archive), provider_id, item.get("provider_series_id"), *detail.values()]
                        if item.get("tmdb_id"):
                            cols.append("tmdb_id")
                            vals.append(item["tmdb_id"])
                        placeholders_sql = ", ".join("?" for _ in cols)
                        cur = conn.execute(f"INSERT INTO series ({', '.join(cols)}, created_at) VALUES ({placeholders_sql}, ?)", (*vals, now))
                        series_id = cur.lastrowid
                        did_create = True
                        did_archive = should_archive
                    else:
                        row = conn.execute(
                            "SELECT id, review_excluded, review_excluded_manual FROM series WHERE name=? AND year IS ?", (name, year),
                        ).fetchone()
                        if row:
                            series_id = row["id"]
                            sets, set_params = _plex_detail_update_sql(detail, item.get("tmdb_id"))
                            conn.execute(f"UPDATE series SET {sets}, updated_at=? WHERE id=?", (*set_params, now, series_id))
                            did_match = True
                            if should_archive and not row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE series SET review_excluded=1 WHERE id=?", (series_id,))
                                conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (series_id,))
                                did_archive = True
                            elif not should_archive and row["review_excluded"] and not row["review_excluded_manual"]:
                                conn.execute("UPDATE series SET review_excluded=0 WHERE id=?", (series_id,))
                                did_unarchive = True
                        elif year is None:
                            # Same reasoning as bulk_import_plex_movies's identical
                            # branch -- a null year (rare for Plex/Emby, which
                            # almost always report a real one, but the norm for
                            # DVR-sourced series, which have no year signal at all
                            # -- see dispatcharr_dvr_importer.py) still deserves an
                            # exactly-one-candidate check rather than silently
                            # falling straight through to a plain insert with no
                            # disambiguation at all, unlike every other null-year
                            # match path in this codebase.
                            candidates = conn.execute(
                                "SELECT id, review_excluded, review_excluded_manual FROM series WHERE name=?", (name,)
                            ).fetchall()
                            if len(candidates) == 1:
                                series_id = candidates[0]["id"]
                                sets, set_params = _plex_detail_update_sql(detail, item.get("tmdb_id"))
                                conn.execute(f"UPDATE series SET {sets}, updated_at=? WHERE id=?", (*set_params, now, series_id))
                                did_match = True
                                if should_archive and not candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE series SET review_excluded=1 WHERE id=?", (series_id,))
                                    conn.execute("DELETE FROM series_category_placements WHERE series_id=?", (series_id,))
                                    did_archive = True
                                elif not should_archive and candidates[0]["review_excluded"] and not candidates[0]["review_excluded_manual"]:
                                    conn.execute("UPDATE series SET review_excluded=0 WHERE id=?", (series_id,))
                                    did_unarchive = True
                            else:
                                cols = ["name", "year", "needs_year_review", "review_excluded", "import_provider_id", "import_provider_series_id", *detail.keys()]
                                vals = [name, year, 1 if candidates else 0, int(should_archive), provider_id, item.get("provider_series_id"), *detail.values()]
                                if item.get("tmdb_id"):
                                    cols.append("tmdb_id")
                                    vals.append(item["tmdb_id"])
                                placeholders = ", ".join("?" for _ in cols)
                                cur = conn.execute(f"INSERT INTO series ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
                                series_id = cur.lastrowid
                                did_create = True
                                did_archive = should_archive
                        else:
                            cols = ["name", "year", "review_excluded", "import_provider_id", "import_provider_series_id", *detail.keys()]
                            vals = [name, year, int(should_archive), provider_id, item.get("provider_series_id"), *detail.values()]
                            if item.get("tmdb_id"):
                                cols.append("tmdb_id")
                                vals.append(item["tmdb_id"])
                            placeholders = ", ".join("?" for _ in cols)
                            cur = conn.execute(f"INSERT INTO series ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
                            series_id = cur.lastrowid
                            did_create = True
                            did_archive = should_archive
                    series_created += did_create
                    series_matched += did_match
                    archived += did_archive
                    unarchived += did_unarchive

                    for ep in item.get("episodes", []):
                        # A separate inner savepoint -- one malformed episode
                        # (missing season/episode number, bad stream id) must not
                        # roll back the series row or its other, good episodes.
                        try:
                            with _item_savepoint(conn):
                                erow = conn.execute(
                                    "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
                                    (series_id, ep["season_number"], ep["episode_number"]),
                                ).fetchone()
                                if erow:
                                    episode_id = erow["id"]
                                    conn.execute(
                                        "UPDATE episodes SET name=?, description=?, duration_secs=?, updated_at=? WHERE id=?",
                                        (ep["name"], ep.get("description"), ep.get("duration_secs"), now, episode_id),
                                    )
                                else:
                                    cur = conn.execute(
                                        "INSERT INTO episodes (series_id, season_number, episode_number, name, description, duration_secs, created_at) VALUES (?,?,?,?,?,?,?)",
                                        (series_id, ep["season_number"], ep["episode_number"], ep["name"], ep.get("description"), ep.get("duration_secs"), now),
                                    )
                                    episode_id = cur.lastrowid
                                conn.execute(
                                    """INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, plex_rating_key, file_size_bytes, provider_category_name, added_at, last_seen_at)
                                       VALUES (?,?,?,?,?,?,?,?,?)
                                       ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                                           episode_id=excluded.episode_id, last_seen_at=excluded.last_seen_at, plex_rating_key=excluded.plex_rating_key,
                                           file_size_bytes=excluded.file_size_bytes, provider_category_name=excluded.provider_category_name""",
                                    # Unlike XC (per-episode category unknown until enrich_series
                                    # lazily fetches it, see that function's docstring), Plex/Emby
                                    # already know every episode up front -- the series-level
                                    # item's own category_name (GH#9) applies directly here, no
                                    # staging through series.provider_category_name needed.
                                    (episode_id, provider_id, ep["provider_stream_id"], ep.get("container_extension", "mp4"), ep.get("plex_rating_key"), ep.get("file_size_bytes"), item.get("provider_category_name"), now, now),
                                )
                                episodes_total += 1
                        except Exception as exc:
                            episode_errors += 1
                            logger.warning("[vod_db] bulk_import_plex_series: skipped episode series=%r s%re%r: %s",
                                            name, ep.get("season_number"), ep.get("episode_number"), exc)
            except Exception as exc:
                errors += 1
                logger.warning("[vod_db] bulk_import_plex_series: skipped item name=%r series_id=%r: %s",
                                item.get("name"), item.get("provider_series_id"), exc)
            if (i + 1) % batch_size == 0:
                _commit_with_retry(conn)
                # Release+reacquire between batches -- holding the lock for the
                # WHOLE function (a large catalog can take many minutes) blocked
                # every other writer with no timeout, which could exhaust the
                # async thread pool if enough piled up waiting -- confirmed live
                # 2026-07-31 as the cause of Dispatcharr's VOD detail-refresh
                # requests hanging/500ing during a large concurrent import, fixed
                # by matching the lock's hold window to one batch's real SQLite
                # write-transaction span instead of the whole import.
                _WRITE_LOCK.release()
                _WRITE_LOCK.acquire()
        _commit_with_retry(conn)
        conn.close()
    finally:
        _WRITE_LOCK.release()
    return {"series_created": series_created, "series_matched": series_matched, "episodes_imported": episodes_total,
            "archived": archived, "unarchived": unarchived, "errors": errors, "episode_errors": episode_errors}


# ── Metadata rewrite rules ───────────────────────────────────────────────────
# Regex find/replace applied to imported title text — e.g. stripping a
# provider's own "4K: " quality-tier prefix so it matches the plain title for
# dedup purposes. Rules are content-agnostic about WHERE they run (import vs.
# enrichment); vod_importer.py calls get_active_rules_for_field at each point
# a given field's value is actually set.

REWRITABLE_FIELDS = ("name", "genre", "description", "director", "cast_list", "country")


def create_metadata_rule(
    content_type: str, field: str, pattern: str, replacement: str = "", sort_order: int = 0, is_regex: bool = False,
) -> int:
    """is_regex defaults to False (literal-text matching) -- see this
    column's migration comment for why literal is the safer default."""
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO metadata_rules (content_type, field, pattern, replacement, sort_order, is_regex, created_at) VALUES (?,?,?,?,?,?,?)",
        (content_type, field, pattern, replacement, sort_order, int(is_regex), _now()),
    )
    rule_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return rule_id


def list_metadata_rules(content_type: str | None = None) -> list[dict]:
    conn = _connect()
    if content_type:
        rows = conn.execute(
            "SELECT * FROM metadata_rules WHERE content_type IN (?, 'both') ORDER BY sort_order, id",
            (content_type,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM metadata_rules ORDER BY sort_order, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_metadata_rule(rule_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM metadata_rules WHERE id=?", (rule_id,))
    _commit_with_retry(conn)
    conn.close()


def set_metadata_rule_active(rule_id: int, is_active: bool) -> None:
    conn = _connect()
    conn.execute("UPDATE metadata_rules SET is_active=? WHERE id=?", (int(is_active), rule_id))
    _commit_with_retry(conn)
    conn.close()


def get_active_rules_for_field(content_type: str, field: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM metadata_rules WHERE content_type IN (?, 'both') AND field=? AND is_active=1 ORDER BY sort_order, id",
        (content_type, field),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _effective_pattern(rule: dict) -> str:
    """A literal-text rule (the default -- see is_regex's migration
    comment) matches its pattern as plain text, escaped so no character in
    it is ever treated as regex syntax. Only a rule explicitly marked
    is_regex=1 gets its pattern used as raw regex."""
    return rule["pattern"] if rule.get("is_regex") else re.escape(rule["pattern"])



# Keyed by (id, pattern, is_regex) so a bad pattern logs once per distinct
# rule, not once per row it's applied to -- apply_rules_to_value runs inside
# per-item loops (bulk import, bulk enrich, preview_metadata_rule's pool
# scan), and a single invalid regex applied across a 195k-movie pool used
# to produce one warning per movie (200k+ log lines from a single bad
# preview click, real user report 2026-09-02). No TTL/eviction needed --
# process-lifetime is fine, and the set stays tiny (bounded by how many
# distinct bad patterns ever get tried).
_WARNED_BAD_PATTERNS: set[tuple] = set()


def apply_rules_to_value(value: str | None, rules: list[dict]) -> str | None:
    if not value or not rules:
        return value
    for r in rules:
        try:
            value = re.sub(_effective_pattern(r), r["replacement"], value)
        except re.error as exc:
            key = (r.get("id"), r["pattern"], bool(r.get("is_regex")))
            if key not in _WARNED_BAD_PATTERNS:
                _WARNED_BAD_PATTERNS.add(key)
                logger.warning("[vod_db] bad metadata rule pattern id=%s: %s", r.get("id"), exc)
    return value


def preview_metadata_rule(content_type: str, field: str, pattern: str, replacement: str, is_regex: bool, limit: int = 10) -> dict:
    """Ad-hoc preview of one candidate rule against the current pool --
    doesn't need to be saved as a real rule first. `field` is caller-
    controlled and gets interpolated into the SELECT below, so it's
    validated against REWRITABLE_FIELDS here rather than trusting the
    route layer alone -- this function has to be safe to call on its own.
    Real user request 2026-07-31, alongside is_regex/apply_metadata_rules_
    to_pool's blast-radius guard: a rule meant to strip a literal string
    had no way to check what it would actually match before committing it
    against the whole pool."""
    if field not in REWRITABLE_FIELDS:
        raise ValueError(f"field must be one of {REWRITABLE_FIELDS}")
    table = "movies" if content_type == "movie" else "series"
    fake_rule = {"pattern": pattern, "replacement": replacement, "is_regex": is_regex}

    conn = _connect()
    rows = conn.execute(f"SELECT id, {field} FROM {table}").fetchall()
    conn.close()

    samples = []
    match_count = 0
    for row in rows:
        old = row[field]
        new = apply_rules_to_value(old, [fake_rule])
        if new != old:
            match_count += 1
            if len(samples) < limit:
                samples.append({"id": row["id"], "before": old, "after": new})
    return {"total_pool": len(rows), "match_count": match_count, "samples": samples}


# A real "strip this prefix" rule should only ever touch a small slice of
# the pool -- anything past this fraction (floor'd so a small pool isn't
# blocked by a handful of legitimate matches) is disproportionate enough to
# warrant a second look before committing, not an automatic pass-through.
_POOL_APPLY_BLAST_RADIUS_FRACTION = 0.05
_POOL_APPLY_BLAST_RADIUS_FLOOR = 50


def apply_metadata_rules_to_pool(content_type: str, force: bool = False) -> dict:
    """Re-applies all active rules for this content_type against the whole
    already-imported pool — same 'fix what's already there' pattern as the
    year-dedup and enrichment bulk-runs. Movies/series only (episodes don't
    carry independently rewritable text beyond what their parent set).

    Real bug found live 2026-07-31: this used to commit unconditionally,
    with no preview and no confirmation of scale -- a rule meant to strip a
    literal string (e.g. "EN| ") but written with an unescaped regex
    metacharacter (a bare "|" meaning "EN" OR an empty string, not the
    literal text "EN|") would silently rewrite far more of the pool than
    intended, with the first sign of trouble being the result count after
    the fact. force=False (the default) blocks and returns samples instead
    of committing whenever the change set is disproportionately large;
    the caller re-calls with force=True to proceed anyway once they've seen
    the samples. See preview_metadata_rule for checking a rule BEFORE
    saving it in the first place, which is the safer path for anything
    correcting the earlier fix's own damage."""
    table = "movies" if content_type == "movie" else "series"
    rules_by_field: dict[str, list[dict]] = {}
    for field in REWRITABLE_FIELDS:
        rules = get_active_rules_for_field(content_type, field)
        if rules:
            rules_by_field[field] = rules
    if not rules_by_field:
        return {"blocked": False, "checked": 0, "changed": 0}

    conn = _connect()
    # Real bug found live 2026-07-31: a pool-wide rule apply overwrote name
    # with no record of what it replaced whenever raw_name hadn't already
    # been captured at import -- true for basically any pre-existing
    # catalog, since raw_name tracking is new. That's the exact case the
    # raw_name revert feature (built the same night) exists to protect
    # against, so this fetches raw_name (series only -- see below) to
    # backfill it from the about-to-be-overwritten name, and never touches
    # a raw_name that's already set.
    backfill_name = content_type == "series" and "name" in rules_by_field
    cols = ["id", *rules_by_field.keys()]
    if backfill_name and "raw_name" not in cols:
        cols.append("raw_name")
    rows = [dict(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()]

    pending: dict[int, dict] = {}
    samples = []
    for row in rows:
        updates = {}
        for field, rules in rules_by_field.items():
            new_val = apply_rules_to_value(row[field], rules)
            if new_val != row[field]:
                updates[field] = new_val
        if updates:
            if backfill_name and "name" in updates and not row.get("raw_name"):
                updates["raw_name"] = row["name"]
            pending[row["id"]] = updates
            if len(samples) < 10:
                samples.append({"id": row["id"], "changes": updates})

    threshold = max(_POOL_APPLY_BLAST_RADIUS_FLOOR, round(len(rows) * _POOL_APPLY_BLAST_RADIUS_FRACTION))
    if not force and len(pending) > threshold:
        conn.close()
        return {"blocked": True, "checked": len(rows), "changed": len(pending), "threshold": threshold, "samples": samples}

    # Movies have no single raw_name of their own -- it's captured per
    # SOURCE (a movie can have arrived from more than one provider under
    # different raw names), so the equivalent backfill for a movie whose
    # name is about to change is onto each of its sources that doesn't
    # already have its own raw_name, using the movie's current (pre-rule)
    # name as the best available record of what it was.
    if content_type == "movie" and "name" in rules_by_field:
        old_name_by_id = {row["id"]: row["name"] for row in rows}
        for row_id, updates in pending.items():
            if "name" in updates:
                conn.execute(
                    "UPDATE movie_sources SET raw_name=? WHERE movie_id=? AND raw_name IS NULL",
                    (old_name_by_id[row_id], row_id),
                )

    for row_id, updates in pending.items():
        sets = ", ".join(f"{f}=?" for f in updates)
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*updates.values(), row_id))
    _commit_with_retry(conn)
    conn.close()
    return {"blocked": False, "checked": len(rows), "changed": len(pending)}


# ── Merging duplicate pool entries ──────────────────────────────────────────
# Used both by the one-time null-year-duplicate cleanup and by the year-review
# resolve flow (a flagged item's year turns out to match an existing entry).
# `into_id`'s own row (name/year/genre/etc.) is left untouched -- it's treated
# as authoritative; `from_id` only ever contributes its sources/episodes/
# placements before being deleted, never overwrites into_id's metadata.

def _merge_movie_row(conn: sqlite3.Connection, from_id: int, into_id: int) -> None:
    """The actual reassign-then-delete SQL for one movie merge, against an
    already-open connection -- no lock/connect/commit/close of its own, so a
    caller merging many pairs in one request (see merge_duplicate_groups_bulk)
    can batch them all into a single transaction instead of paying a fresh
    connection + WAL fsync per pair. merge_movie (single-item) wraps this with
    its own lock/connect/commit for the ordinary one-at-a-time UI path."""
    from_row = conn.execute("SELECT name, year, tmdb_id FROM movies WHERE id=?", (from_id,)).fetchone()
    into_row = conn.execute("SELECT name, year, tmdb_id FROM movies WHERE id=?", (into_id,)).fetchone()
    # This permanently deletes `from_id` below (its sources/placements move
    # to `into_id` first) -- irreversible outside a DB backup, so a merge
    # triggered by a bad tmdb_id match (GH issue #6) leaves no trace to
    # diagnose without this. Logged as a warning, not info, since a merge
    # between two rows that don't actually share a tmdb_id is exactly the
    # signature of a false match, not a routine dedup.
    logger.warning(
        "[merge_movie] id=%s (%r, year=%s, tmdb_id=%s) merging into id=%s (%r, year=%s, tmdb_id=%s) -- from_id row will be deleted",
        from_id, from_row["name"] if from_row else None, from_row["year"] if from_row else None, from_row["tmdb_id"] if from_row else None,
        into_id, into_row["name"] if into_row else None, into_row["year"] if into_row else None, into_row["tmdb_id"] if into_row else None,
    )
    # movie_sources has no per-movie uniqueness (UNIQUE is (provider_id,
    # provider_stream_id) only) -- a plain reassignment can never collide.
    conn.execute("UPDATE movie_sources SET movie_id=? WHERE movie_id=?", (into_id, from_id))

    placements = conn.execute(
        "SELECT category_id FROM movie_category_placements WHERE movie_id=?", (from_id,)
    ).fetchall()
    for p in placements:
        target_has_it = conn.execute(
            "SELECT 1 FROM movie_category_placements WHERE movie_id=? AND category_id=?",
            (into_id, p["category_id"]),
        ).fetchone()
        if target_has_it:
            conn.execute(
                "DELETE FROM movie_category_placements WHERE movie_id=? AND category_id=?",
                (from_id, p["category_id"]),
            )
        else:
            conn.execute(
                "UPDATE movie_category_placements SET movie_id=? WHERE movie_id=? AND category_id=?",
                (into_id, from_id, p["category_id"]),
            )

    conn.execute("DELETE FROM movies WHERE id=?", (from_id,))


def merge_movie(from_id: int, into_id: int) -> None:
    """Holds _WRITE_LOCK for the whole operation -- see delete_provider's
    docstring for why an unlocked `movies` delete here can race a concurrent
    import and cause its inserts to fail with a FOREIGN KEY error."""
    if from_id == into_id:
        return
    with _WRITE_LOCK:
        conn = _connect()
        _merge_movie_row(conn, from_id, into_id)
        _commit_with_retry(conn)
        conn.close()


def _merge_series_row(conn: sqlite3.Connection, from_id: int, into_id: int) -> None:
    """The actual reassign-then-delete SQL for one series merge, against an
    already-open connection -- see _merge_movie_row's identical docstring for
    why this exists separately from merge_series (single-item)."""
    from_row = conn.execute("SELECT name, year, tmdb_id FROM series WHERE id=?", (from_id,)).fetchone()
    into_row = conn.execute("SELECT name, year, tmdb_id FROM series WHERE id=?", (into_id,)).fetchone()
    # See merge_movie's identical logging comment -- same irreversible-delete risk.
    logger.warning(
        "[merge_series] id=%s (%r, year=%s, tmdb_id=%s) merging into id=%s (%r, year=%s, tmdb_id=%s) -- from_id row will be deleted",
        from_id, from_row["name"] if from_row else None, from_row["year"] if from_row else None, from_row["tmdb_id"] if from_row else None,
        into_id, into_row["name"] if into_row else None, into_row["year"] if into_row else None, into_row["tmdb_id"] if into_row else None,
    )

    from_episodes = conn.execute(
        "SELECT id, season_number, episode_number FROM episodes WHERE series_id=?", (from_id,)
    ).fetchall()
    for ep in from_episodes:
        target_ep = conn.execute(
            "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
            (into_id, ep["season_number"], ep["episode_number"]),
        ).fetchone()
        if target_ep:
            # Both sides already have this episode -- move from's sources
            # onto into's existing episode row, then drop from's now-empty
            # episode (episode_sources cascades on the episodes delete).
            conn.execute(
                "UPDATE episode_sources SET episode_id=? WHERE episode_id=?",
                (target_ep["id"], ep["id"]),
            )
            conn.execute("DELETE FROM episodes WHERE id=?", (ep["id"],))
        else:
            # into doesn't have this episode yet -- just move it over wholesale.
            conn.execute("UPDATE episodes SET series_id=? WHERE id=?", (into_id, ep["id"]))

    placements = conn.execute(
        "SELECT category_id FROM series_category_placements WHERE series_id=?", (from_id,)
    ).fetchall()
    for p in placements:
        target_has_it = conn.execute(
            "SELECT 1 FROM series_category_placements WHERE series_id=? AND category_id=?",
            (into_id, p["category_id"]),
        ).fetchone()
        if target_has_it:
            conn.execute(
                "DELETE FROM series_category_placements WHERE series_id=? AND category_id=?",
                (from_id, p["category_id"]),
            )
        else:
            conn.execute(
                "UPDATE series_category_placements SET series_id=? WHERE series_id=? AND category_id=?",
                (into_id, from_id, p["category_id"]),
            )

    conn.execute("DELETE FROM series WHERE id=?", (from_id,))


def merge_series(from_id: int, into_id: int) -> None:
    """Holds _WRITE_LOCK for the whole operation -- see delete_provider's
    docstring for why an unlocked `series` delete here can race a concurrent
    import and cause its inserts to fail with a FOREIGN KEY error."""
    if from_id == into_id:
        return
    with _WRITE_LOCK:
        conn = _connect()
        _merge_series_row(conn, from_id, into_id)
        _commit_with_retry(conn)
        conn.close()


def list_needs_year_review(content_type: str | None = None) -> dict:
    conn = _connect()
    out: dict = {}
    if content_type in (None, "movie"):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM movies WHERE needs_year_review=1 AND review_excluded=0 ORDER BY name"
        ).fetchall()]
        for row in rows:
            # Transcoded fallback preview needs a specific movie_sources row
            # (that route is keyed by source, not movie -- see xc_server.py's
            # /preview/movie-source-transcoded/), for files whose codec the
            # browser can't decode natively (common for Plex-sourced .avi).
            src = conn.execute(
                "SELECT id FROM movie_sources WHERE movie_id=? LIMIT 1", (row["id"],),
            ).fetchone()
            row["sample_source_id"] = src["id"] if src else None
        out["movies"] = rows
    if content_type in (None, "series"):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM series WHERE needs_year_review=1 AND review_excluded=0 ORDER BY name"
        ).fetchall()]
        for row in rows:
            # A preview needs a specific episode (the XC preview route is keyed
            # by episode, not series -- see xc_server.py) so a reviewer can
            # actually watch a clip of the flagged item, not just read its name.
            ep = conn.execute(
                "SELECT id FROM episodes WHERE series_id=? ORDER BY season_number, episode_number LIMIT 1",
                (row["id"],),
            ).fetchone()
            row["sample_episode_id"] = ep["id"] if ep else None
            if ep:
                src = conn.execute(
                    "SELECT id FROM episode_sources WHERE episode_id=? LIMIT 1", (ep["id"],),
                ).fetchone()
                row["sample_episode_source_id"] = src["id"] if src else None
            else:
                row["sample_episode_source_id"] = None
            # Season/episode counts as a secondary signal alongside TMDB
            # suggestions -- if we've pulled 5 seasons/62 episodes and a
            # candidate's own TMDB counts are wildly different, that's a
            # useful hint even when the name/year alone are ambiguous. Not
            # every provider has a complete catalog, so this is corroborating
            # evidence, not proof either way.
            counts = conn.execute(
                "SELECT COUNT(DISTINCT season_number) seasons, COUNT(*) episodes FROM episodes WHERE series_id=?",
                (row["id"],),
            ).fetchone()
            row["imported_season_count"] = counts["seasons"]
            row["imported_episode_count"] = counts["episodes"]
        out["series"] = rows
    conn.close()
    return out


def resolve_year_review(content_type: str, item_id: int, year: int, tmdb_id: str | None = None) -> dict:
    """Sets the correct year (and tmdb_id, if known) on a flagged item and
    clears the flag. If that year now exactly matches an existing item of
    the same name, merges into it instead of leaving two rows around."""
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"{content_type} {item_id} not found")

    existing = conn.execute(
        f"SELECT id FROM {table} WHERE name=? AND year=? AND id != ?", (row["name"], year, item_id),
    ).fetchone()
    conn.close()

    if existing:
        if content_type == "movie":
            merge_movie(item_id, existing["id"])
        else:
            merge_series(item_id, existing["id"])
        return {"merged_into": existing["id"]}

    conn = _connect()
    fields = {"year": year, "needs_year_review": 0}
    if tmdb_id:
        fields["tmdb_id"] = tmdb_id
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE {table} SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), item_id))
    _commit_with_retry(conn)
    conn.close()
    return {"resolved_id": item_id}


# ── Missing artwork ──────────────────────────────────────────────────────────
# Poster/tmdb_id normally come straight from the XC provider's own metadata
# (see vod_importer.enrich_movie/enrich_series) -- when a provider's catalog
# just doesn't have artwork for something (or its title is mangled enough
# that the provider's own match failed), this is the browse-and-fix queue: a
# real TMDB search (tmdb_sync.search_title) a human or the AI picks from,
# same review-before-apply shape as needs_year_review above.

# Broad non-Latin-script detector -- not tied to any one language (Arabic,
# Thai, Chinese/Japanese/Korean, Cyrillic, Greek, Hebrew, Devanagari all
# match) so "filter out foreign-script titles" works the same way for any
# deployment's actual catalog mix, not just the language a given provider
# happens to include a lot of.
_NON_LATIN_RE = re.compile(
    "[؀-ۿݐ-ݿ"   # Arabic
    "֐-׿"                 # Hebrew
    "฀-๿"                 # Thai
    "一-鿿㐀-䶿"    # CJK
    "぀-ヿ"                 # Hiragana/Katakana
    "가-힯"                 # Hangul
    "Ѐ-ӿ"                 # Cyrillic
    "Ͱ-Ͽ"                 # Greek
    "ऀ-ॿ]"                # Devanagari
)


def _is_non_latin_name(name: str) -> bool:
    return bool(_NON_LATIN_RE.search(name))


# Some real XC providers tag language/dub/sub variants with a leading
# "XX|" code (e.g. "AR| Apex", "ALB| Apex", "IN| TELUGU| Apex") -- a much
# broader and more precise signal than script detection alone, since it
# also flags Latin-script variants (French, German, Spanish...) and, unlike
# a fixed language list, works for whatever codes a given deployment's
# providers actually use rather than ones picked in advance.
_LANG_PREFIX_PIPE_RE = re.compile(r"^([A-Z]{2,6})\|")
# Colon-style prefixes ("AR: Movie Title") are only ever stripped/detected
# when the code is a KNOWN language code, never any 2-6 capital letters --
# unlike "|", a colon is common in real titles ("Kill Bill: Volume 1",
# "CSI: Miami", "NCIS: New Orleans"), so a fuzzy match here would misdetect
# real titles as language-tagged across every feature that reuses this
# (duplicate-sibling detection, missing-artwork/library-language filtering).
_LANG_PREFIX_COLON_RE = re.compile(r"^([A-Z]{2,6}):\s")
_KNOWN_LANGUAGE_CODES = {
    "EN", "AR", "FR", "ES", "DE", "IT", "PT", "BR", "RU", "TR", "PL", "NL",
    "GR", "HU", "BG", "RO", "SE", "NO", "DK", "FI", "CZ", "SK", "HR", "SR",
    "SL", "UA", "IN", "HI", "ZH", "CN", "JA", "JP", "KO", "KR", "TH", "VI",
    "ID", "MY", "HE", "FA", "UR", "BN", "TA", "TE", "PK", "AF", "SW", "ALB",
    "EXYU", "LT", "LV", "EE", "GE", "AM", "AZ", "KZ", "SC",
}
# Real titles that happen to start with "<known code>: " -- checked against
# the colon match specifically (never the pipe match, which no real title
# ever collides with). Confirmed against TMDB: "IT: Chapter Two" (2019) is
# the only real title colliding with "IT" (Italian); add further entries
# here if another known code ever turns out to collide with a real title.
_LANG_PREFIX_COLON_EXCEPTIONS = {"it: chapter two"}


def _colon_prefix_code(name: str) -> str | None:
    m = _LANG_PREFIX_COLON_RE.match(name)
    if not m or m.group(1) not in _KNOWN_LANGUAGE_CODES:
        return None
    lowered = name.strip().lower()
    if any(lowered.startswith(exc) for exc in _LANG_PREFIX_COLON_EXCEPTIONS):
        return None
    return m.group(1)


def _name_prefix_code(name: str) -> str | None:
    m = _LANG_PREFIX_PIPE_RE.match(name)
    if m:
        return m.group(1)
    return _colon_prefix_code(name)


def _strip_one_lang_prefix(name: str) -> str | None:
    """One leading language-style prefix removed, or None if there isn't
    one -- see _name_prefix_code for why colon-matching is whitelist-only."""
    m = _LANG_PREFIX_PIPE_RE.match(name)
    if m:
        return name[m.end():].strip()
    if _colon_prefix_code(name):
        return _LANG_PREFIX_COLON_RE.sub("", name, count=1).strip()
    return None


def _strip_lang_prefixes(name: str) -> str:
    """Repeated -- some providers double-tag (e.g. "IN| TELUGU| Apex") --
    strip every leading language-style layer to get the bare title, used to
    match a language-tagged row against its same-content sibling in another
    language (see smart_bulk_exclude)."""
    while True:
        new_name = _strip_one_lang_prefix(name)
        if new_name is None or new_name == name:
            return name
        name = new_name


def _missing_artwork_clause(search: str | None, excluded: bool) -> tuple[str, list]:
    where = ["(poster_url IS NULL OR poster_url = '')", "review_excluded = ?"]
    params: list = [1 if excluded else 0]
    if search:
        where.append("name LIKE ?")
        params.append(f"%{search}%")
    return f"WHERE {' AND '.join(where)}", params


def _missing_artwork_rows(table: str, search: str | None, excluded: bool, script: str | None, prefixes: list[str] | None = None) -> list:
    """script/prefix filtering can't be expressed in SQL (SQLite has no
    Unicode script/category matching, and a leading-substring group-by is
    awkward in plain SQL), so when either is requested this fetches every
    matching row (already bounded by the poster/search/excluded filters,
    same as any other admin scan in this file) and filters in Python."""
    clause, params = _missing_artwork_clause(search, excluded)
    conn = _connect()
    rows = conn.execute(f"SELECT * FROM {table} {clause} ORDER BY name", params).fetchall()
    conn.close()
    if script == "non_latin":
        rows = [r for r in rows if _is_non_latin_name(r["name"])]
    if prefixes:
        wanted = set(prefixes)
        rows = [r for r in rows if _name_prefix_code(r["name"]) in wanted]
    return rows


def list_missing_artwork_prefixes(content_type: str, search: str | None = None, excluded: bool = False, script: str | None = None) -> list[dict]:
    """Distinct "XX|"-style language-prefix codes actually present in the
    current filter scope, with counts -- powers a picker showing real
    options instead of a fixed guessed-in-advance language list."""
    table = "movies" if content_type == "movie" else "series"
    rows = _missing_artwork_rows(table, search, excluded, script)
    counts: dict[str, int] = {}
    for r in rows:
        code = _name_prefix_code(r["name"])
        if code:
            counts[code] = counts.get(code, 0) + 1
    return sorted(({"code": c, "count": n} for c, n in counts.items()), key=lambda x: -x["count"])


def list_missing_artwork(
    content_type: str, limit: int = 50, offset: int = 0, search: str | None = None,
    excluded: bool = False, script: str | None = None, prefixes: list[str] | None = None,
) -> list[dict]:
    table = "movies" if content_type == "movie" else "series"
    rows = _missing_artwork_rows(table, search, excluded, script, prefixes)
    return [dict(r) for r in rows[offset:offset + limit]]


def count_missing_artwork(
    content_type: str, search: str | None = None, excluded: bool = False,
    script: str | None = None, prefixes: list[str] | None = None,
) -> int:
    table = "movies" if content_type == "movie" else "series"
    if script or prefixes:
        return len(_missing_artwork_rows(table, search, excluded, script, prefixes))
    clause, params = _missing_artwork_clause(search, excluded)
    conn = _connect()
    n = conn.execute(f"SELECT COUNT(*) c FROM {table} {clause}", params).fetchone()["c"]
    conn.close()
    return n


def list_missing_artwork_page(
    content_type: str, limit: int = 50, offset: int = 0, search: str | None = None,
    excluded: bool = False, script: str | None = None, prefixes: list[str] | None = None,
) -> dict:
    """Combined items+total in one pass -- calling list_missing_artwork and
    count_missing_artwork separately each re-runs the same full-table Python
    scan+filter whenever script/prefixes is set (once per call, so twice per
    page load, on top of the separate /prefixes/ scan), which is pure waste
    since it's the exact same row set both times."""
    table = "movies" if content_type == "movie" else "series"
    rows = _missing_artwork_rows(table, search, excluded, script, prefixes)
    return {"items": [dict(r) for r in rows[offset:offset + limit]], "total": len(rows)}


def list_missing_artwork_ids(
    content_type: str, search: str | None = None, excluded: bool = False,
    script: str | None = None, prefixes: list[str] | None = None,
) -> list[int]:
    """Every matching id, not just one page -- backs the "select all
    matching this search" bulk actions (apply-poster/exclude), same pattern
    as list_all_movie_ids for category bulk-place."""
    table = "movies" if content_type == "movie" else "series"
    if script or prefixes:
        return [r["id"] for r in _missing_artwork_rows(table, search, excluded, script, prefixes)]
    clause, params = _missing_artwork_clause(search, excluded)
    conn = _connect()
    rows = conn.execute(f"SELECT id FROM {table} {clause}", params).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def bulk_set_poster_url(content_type: str, ids: list[int], poster_url: str) -> int:
    """Blanket-apply one poster/placeholder image to many items at once --
    e.g. a generic logo for a whole batch of items a real per-title poster
    will never exist for (stock/creator content, local recordings)."""
    if not ids:
        return 0
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE {table} SET poster_url=?, updated_at=? WHERE id IN ({placeholders})",
        (poster_url, _now(), *ids),
    )
    _commit_with_retry(conn)
    conn.close()
    return len(ids)


def bulk_set_review_excluded(content_type: str, ids: list[int], excluded: bool) -> int:
    """Archives/unarchives items out of (or back into) every review queue --
    Missing Artwork, Needs Review, Duplicate Finder -- without touching the
    content itself: still fully browsable/playable/categorizable, just no
    longer flagged as something that needs attention. For content a given
    deployment doesn't care to curate (e.g. a foreign-language catalog a
    user has no interest in enriching) rather than something to delete.

    Every caller of this is a human clicking an archive/un-archive control,
    so this always stamps review_excluded_manual=1 too -- the signal that
    stops import-time auto-archive (see vod_importer._should_auto_archive)
    from silently re-archiving something a human deliberately restored, the
    same is_adult/is_adult_manual pattern already used for adult-content
    auto-detection."""
    if not ids:
        return 0
    table = "movies" if content_type == "movie" else "series"
    id_col = "movie_id" if content_type == "movie" else "series_id"
    placements_table = "movie_category_placements" if content_type == "movie" else "series_category_placements"
    conn = _connect()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE {table} SET review_excluded=?, review_excluded_manual=1, updated_at=? WHERE id IN ({placeholders})",
        (1 if excluded else 0, _now(), *ids),
    )
    if excluded:
        # Archiving must be a TRUE archive: pull it out of every category
        # placement (manual AND smart) immediately, not just flip the flag
        # and wait for the next smart-category re-evaluation -- a manually
        # placed item would otherwise keep exporting to Dispatcharr forever,
        # the same visibility bug already fixed for import-time auto-archive
        # (see evaluate_smart_category's review_excluded filter).
        conn.execute(f"DELETE FROM {placements_table} WHERE {id_col} IN ({placeholders})", ids)
    _commit_with_retry(conn)
    conn.close()
    return len(ids)


def smart_bulk_exclude(content_type: str, ids: list[int], keep_codes: list[str] | None, dry_run: bool = False) -> dict:
    """Archives each id UNLESS it's the only copy of that content in the
    pool -- e.g. don't archive "AR| Apex" if there's no "EN| Apex" (or
    unprefixed "Apex") to fall back on, since that would remove the only
    way to watch it at all, not just the non-preferred-language copy.
    Matches siblings by bare title (language prefix stripped) across the
    WHOLE table, not just the filtered candidate set -- the keeper sibling
    might already be fully enriched and outside whatever scope produced
    `ids` (e.g. it already has a poster, so it's not in a Missing Artwork
    scan). keep_codes: prefix codes that count as an acceptable sibling,
    in addition to no-prefix-at-all (always treated as the base/native
    listing). dry_run: compute the same archived/skipped counts without
    writing anything -- backs a live "this is what would happen" preview,
    since changing keep_codes has no visible effect otherwise until after
    you've already committed the archive."""
    if not ids:
        return {"archived": 0, "skipped": 0, "skipped_examples": []}
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    all_rows = conn.execute(f"SELECT id, name FROM {table}").fetchall()
    conn.close()

    by_id: dict[int, dict] = {}
    by_bare: dict[str, list[dict]] = {}
    for r in all_rows:
        code = _name_prefix_code(r["name"])
        entry = {"id": r["id"], "name": r["name"], "code": code}
        by_id[r["id"]] = entry
        by_bare.setdefault(_strip_lang_prefixes(r["name"]), []).append(entry)

    keep_set = set(keep_codes or [])
    to_archive = []
    skipped_names = []
    for item_id in ids:
        entry = by_id.get(item_id)
        if not entry:
            continue
        bare = _strip_lang_prefixes(entry["name"])
        has_keeper = any(
            sib["id"] != item_id and (sib["code"] is None or sib["code"] in keep_set)
            for sib in by_bare.get(bare, [])
        )
        if has_keeper:
            to_archive.append(item_id)
        else:
            skipped_names.append(entry["name"])

    if dry_run:
        return {"archived": len(to_archive), "skipped": len(skipped_names), "skipped_examples": skipped_names[:10]}
    archived = bulk_set_review_excluded(content_type, to_archive, True)
    return {"archived": archived, "skipped": len(skipped_names), "skipped_examples": skipped_names[:10]}


# ── Whole-library language filter ───────────────────────────────────────────
# Same script/prefix filtering as Missing Artwork, but over the entire pool
# rather than just the poster-missing subset -- lets a deployment curate its
# catalog by language broadly, not only where a poster happens to be
# missing (a title with a real poster is just as much "not in my language"
# as one without).

def _library_clause(search: str | None, excluded: bool | None) -> tuple[str, list]:
    where = []
    params: list = []
    if excluded is not None:
        where.append("review_excluded = ?")
        params.append(1 if excluded else 0)
    if search:
        where.append("name LIKE ?")
        params.append(f"%{search}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def _library_rows(table: str, search: str | None, excluded: bool | None, script: str | None, prefixes: list[str] | None) -> list:
    clause, params = _library_clause(search, excluded)
    conn = _connect()
    rows = conn.execute(f"SELECT * FROM {table} {clause} ORDER BY name", params).fetchall()
    conn.close()
    if script == "non_latin":
        rows = [r for r in rows if _is_non_latin_name(r["name"])]
    if prefixes:
        wanted = set(prefixes)
        rows = [r for r in rows if _name_prefix_code(r["name"]) in wanted]
    return rows


def list_library_filtered(
    content_type: str, limit: int = 50, offset: int = 0, search: str | None = None,
    excluded: bool | None = None, script: str | None = None, prefixes: list[str] | None = None,
) -> list[dict]:
    table = "movies" if content_type == "movie" else "series"
    if not script and not prefixes:
        clause, params = _library_clause(search, excluded)
        conn = _connect()
        rows = conn.execute(f"SELECT * FROM {table} {clause} ORDER BY name LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    rows = _library_rows(table, search, excluded, script, prefixes)
    return [dict(r) for r in rows[offset:offset + limit]]


def count_library_filtered(
    content_type: str, search: str | None = None, excluded: bool | None = None,
    script: str | None = None, prefixes: list[str] | None = None,
) -> int:
    table = "movies" if content_type == "movie" else "series"
    if not script and not prefixes:
        clause, params = _library_clause(search, excluded)
        conn = _connect()
        n = conn.execute(f"SELECT COUNT(*) c FROM {table} {clause}", params).fetchone()["c"]
        conn.close()
        return n
    return len(_library_rows(table, search, excluded, script, prefixes))


def list_library_page(
    content_type: str, limit: int = 50, offset: int = 0, search: str | None = None,
    excluded: bool | None = None, script: str | None = None, prefixes: list[str] | None = None,
) -> dict:
    """Combined items+total in one pass when script/prefixes forces the
    Python-side scan (same reasoning as list_missing_artwork_page) -- the
    plain search/excluded-only case still uses cheap SQL-native
    LIMIT/OFFSET + COUNT(*), not a full-table fetch, since that's the
    common case (no language filter engaged at all)."""
    table = "movies" if content_type == "movie" else "series"
    if not script and not prefixes:
        return {
            "items": list_library_filtered(content_type, limit, offset, search, excluded, script, prefixes),
            "total": count_library_filtered(content_type, search, excluded, script, prefixes),
        }
    rows = _library_rows(table, search, excluded, script, prefixes)
    return {"items": [dict(r) for r in rows[offset:offset + limit]], "total": len(rows)}


def list_library_ids(
    content_type: str, search: str | None = None, excluded: bool | None = None,
    script: str | None = None, prefixes: list[str] | None = None,
) -> list[int]:
    table = "movies" if content_type == "movie" else "series"
    if not script and not prefixes:
        clause, params = _library_clause(search, excluded)
        conn = _connect()
        rows = conn.execute(f"SELECT id FROM {table} {clause}", params).fetchall()
        conn.close()
        return [r["id"] for r in rows]
    return [r["id"] for r in _library_rows(table, search, excluded, script, prefixes)]


# Real pool data turns up name-prefix codes that match the language-tag
# pattern (2-6 capital letters + separator) but aren't a language at all --
# a provider's own content-category shorthand riding the same convention
# (confirmed live: "SOCCER|" tags sports content in a real test catalog).
# Excluded only from the Import Language Exclusion picker specifically --
# _name_prefix_code itself is unchanged, so duplicate-sibling detection and
# the other features that reuse it still see these codes normally.
_NON_LANGUAGE_PIPE_TAGS = {"SOCCER"}


def list_all_pool_prefixes() -> list[dict]:
    """Every distinct language-style name prefix ("AR|", "EN|", ...)
    actually present across the WHOLE pool right now (movies and series
    combined, no filters) -- these are arbitrary provider-chosen tags, not
    a fixed ISO list, so the only reliable source of "what codes exist" is
    the pool itself. Backs the Import Language Exclusion picker so an admin
    can select real, currently-seen codes instead of guessing/typing them
    blind."""
    conn = _connect()
    counts: dict[str, int] = {}
    for table in ("movies", "series"):
        rows = conn.execute(f"SELECT name FROM {table}").fetchall()
        for r in rows:
            code = _name_prefix_code(r["name"])
            if code and code not in _NON_LANGUAGE_PIPE_TAGS:
                counts[code] = counts.get(code, 0) + 1
    conn.close()
    return sorted(({"code": c, "count": n} for c, n in counts.items()), key=lambda x: -x["count"])


def list_library_prefixes(content_type: str, search: str | None = None, excluded: bool | None = None, script: str | None = None) -> list[dict]:
    table = "movies" if content_type == "movie" else "series"
    rows = _library_rows(table, search, excluded, script, None)
    counts: dict[str, int] = {}
    for r in rows:
        code = _name_prefix_code(r["name"])
        if code:
            counts[code] = counts.get(code, 0) + 1
    return sorted(({"code": c, "count": n} for c, n in counts.items()), key=lambda x: -x["count"])


def resolve_missing_artwork(
    content_type: str, item_id: int, poster_url: str,
    tmdb_id: str | None = None, name: str | None = None, year: int | None = None,
) -> dict:
    """Applies a chosen TMDB match (or a manually-entered poster URL) to a
    missing-artwork item. name/year are optional -- a corrected search query
    often reveals the *stored* name was the actual problem, so a reviewer or
    the AI can fix that at the same time, not just the poster. Same
    merge-on-collision safety as resolve_year_review: if the corrected
    name/year now matches an existing pool entry exactly, merge into it
    rather than leaving two rows with the same identity."""
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"{content_type} {item_id} not found")

    final_name = name.strip() if name and name.strip() else row["name"]
    final_year = year if year is not None else row["year"]

    existing = None
    if (final_name, final_year) != (row["name"], row["year"]):
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE name=? AND year IS ? AND id != ?", (final_name, final_year, item_id),
        ).fetchone()

    if existing:
        # Give the surviving row the poster/tmdb_id before folding this one
        # into it, in case it was ALSO missing artwork.
        conn.execute(
            f"UPDATE {table} SET poster_url=COALESCE(NULLIF(poster_url,''), ?), "
            f"tmdb_id=COALESCE(tmdb_id, ?), updated_at=? WHERE id=?",
            (poster_url, tmdb_id, _now(), existing["id"]),
        )
        _commit_with_retry(conn)
        conn.close()
        if content_type == "movie":
            merge_movie(item_id, existing["id"])
        else:
            merge_series(item_id, existing["id"])
        return {"merged_into": existing["id"]}

    fields = {"poster_url": poster_url, "name": final_name, "year": final_year}
    if tmdb_id:
        fields["tmdb_id"] = tmdb_id
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE {table} SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), item_id))
    _commit_with_retry(conn)
    conn.close()
    return {"resolved_id": item_id}


def backfill_tmdb_id_if_missing(content_type: str, item_id: int, tmdb_id: str) -> None:
    """Real bug found live 2026-07-31: apply_tmdb_title_movie/series calls
    rename_item with TMDB's own title, which can trigger rename_item's
    merge-on-collision (an existing row already has that exact name+year) --
    merge_movie/merge_series move sources/episodes/categories but never
    touch tmdb_id, so the confirmed match just fetched from TMDB was
    silently lost on the surviving row. Only backfills when the survivor
    has no tmdb_id of its own -- never overwrites an existing (possibly
    different) confirmed match."""
    table = "movies" if content_type == "movie" else "series"
    with _WRITE_LOCK:
        conn = _connect()
        conn.execute(f"UPDATE {table} SET tmdb_id=? WHERE id=? AND tmdb_id IS NULL", (tmdb_id, item_id))
        _commit_with_retry(conn)
        conn.close()


def clear_tmdb_id(content_type: str, item_id: int) -> dict:
    """Manual escape hatch (GH issue #6): a wrong tmdb_id -- however it got
    attached -- has no UI-visible undo today. Clearing it breaks the bad
    match (name/year/sources/poster untouched) so the item falls back to
    being unmatched instead of confirmed-wrong, and can pick up a correct
    id on the next enrichment pass or TMDB List sync. Does NOT touch
    anything a merge already did -- if the item was already merged into
    another row (see merge_movie/merge_series), the pre-merge row is gone
    and this can't bring it back; that needs a backup restore."""
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    row = conn.execute(f"SELECT id, name, tmdb_id FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"{content_type} {item_id} not found")
    conn.execute(f"UPDATE {table} SET tmdb_id=NULL, updated_at=? WHERE id=?", (_now(), item_id))
    _commit_with_retry(conn)
    conn.close()
    logger.info("[clear_tmdb_id] %s id=%s (%r) tmdb_id %s -> NULL", content_type, item_id, row["name"], row["tmdb_id"])
    return {"cleared_id": item_id}


def rename_item(content_type: str, item_id: int, name: str, year: int | None) -> dict:
    """Manually corrects a movie/series' own name/year -- the general
    escape hatch for whatever a provider's own catalog data got wrong (most
    commonly a blank/garbled title with no other tooling to fix it). Same
    merge-on-collision safety as resolve_missing_artwork/resolve_year_review:
    if the corrected name+year now matches an existing pool entry exactly,
    merge into it rather than leaving two rows with the same identity."""
    table = "movies" if content_type == "movie" else "series"
    name = name.strip()
    if not name:
        raise ValueError("name is required")

    conn = _connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"{content_type} {item_id} not found")

    existing = None
    if (name, year) != (row["name"], row["year"]):
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE name=? AND year IS ? AND id != ?", (name, year, item_id),
        ).fetchone()
    conn.close()

    if existing:
        if content_type == "movie":
            merge_movie(item_id, existing["id"])
        else:
            merge_series(item_id, existing["id"])
        return {"merged_into": existing["id"]}

    conn = _connect()
    conn.execute(
        f"UPDATE {table} SET name=?, year=?, needs_year_review=0, updated_at=? WHERE id=?",
        (name, year, _now(), item_id),
    )
    _commit_with_retry(conn)
    conn.close()
    return {"renamed_id": item_id}


# ── Smart categories ─────────────────────────────────────────────────────────
# rule_json shape: {"match": "all"|"any", "conditions": [{"field", "op", "value"}, ...]}
# field: name | genre | year | country | director (movies/series share these)
# op: contains | equals | starts_with | gte | lte

_SMART_CATEGORY_FIELDS = {"name", "genre", "year", "country", "language", "director", "is_adult", "provider_category"}
# "language" isn't a real column — providers report spoken language(s) in what
# we store as "country" (e.g. "English, Español"), so it's an alias onto that
# same data rather than a separate field. Named clearly for the UI since
# "country" reads as country-of-origin, not language.
# "provider_category" isn't a movies/series column either -- it's the
# provider's own category name(s), captured per-SOURCE at import time
# (movie_sources/episode_sources.provider_category_name), since a title can
# have sources from more than one provider with different category names.
# evaluate_smart_category's query below aggregates every distinct value seen
# across an item's own sources into one comma-joined string before rule
# matching, so "contains" works naturally against it like any other field.
_FIELD_ALIASES = {"language": "country"}


def _to_num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _condition_matches(row: dict, cond: dict) -> bool:
    field = cond.get("field")
    op    = cond.get("op")
    value = cond.get("value")
    if field not in _SMART_CATEGORY_FIELDS:
        return False
    actual = row.get(_FIELD_ALIASES.get(field, field))

    if op == "contains":
        return str(value or "").strip().lower() in str(actual or "").lower()
    if op == "starts_with":
        return str(actual or "").lower().startswith(str(value or "").strip().lower())
    if op == "equals":
        return str(actual if actual is not None else "").strip().lower() == str(value or "").strip().lower()
    if op in ("gte", "lte"):
        a, v = _to_num(actual), _to_num(value)
        if a is None or v is None:
            return False
        return a >= v if op == "gte" else a <= v
    return False


def _rule_matches(row: dict, rule: dict) -> bool:
    # match_all: true is a distinct opt-in mode (used by the built-in "All
    # Movies"/"All TV Shows" catch-all categories) -- deliberately separate
    # from conditions=[] below, which stays "matches nothing" so a
    # user-created smart category with no conditions configured yet doesn't
    # silently swallow the whole pool.
    if rule.get("match_all"):
        if rule.get("exclude_adult") and row.get("is_adult"):
            return False
        return True
    conditions = rule.get("conditions") or []
    if not conditions:
        return False
    checks = (_condition_matches(row, c) for c in conditions)
    return all(checks) if rule.get("match", "all") == "all" else any(checks)


def list_catchall_category_ids() -> list[int]:
    """The match_all smart categories from _seed_default_categories (and any
    a user has since built the same way) -- narrowly scoped to just these
    two, used ONLY by the first-run "include 18+?" prompt check
    (get_default_categories_prompt), which is specifically about the
    built-in catch-alls, not smart categories in general. For the periodic
    re-evaluation sweep, see list_smart_category_ids_with_rules below --
    broadened 2026-07-29 to cover every smart category, not just these two."""
    import json
    conn = _connect()
    rows = conn.execute("SELECT id, rule_json FROM categories WHERE is_smart=1 AND rule_json IS NOT NULL").fetchall()
    conn.close()
    ids = []
    for r in rows:
        try:
            if json.loads(r["rule_json"]).get("match_all"):
                ids.append(r["id"])
        except (ValueError, TypeError):
            continue
    return ids


def list_smart_category_ids_with_rules() -> list[int]:
    """Every smart category with a rule configured -- what the general
    re-evaluation sweep (vod_importer.resweep_smart_categories) keeps
    current, tied to the same triggers as a provider's own catalog refresh
    (see main.py's _vod_catalog_refresher due-check) rather than requiring
    each category to opt into its own explicit schedule. Broadened
    2026-07-29 from the original catch-all-only sweep, per user direction:
    rule-based evaluation is free (no external API call) and purely
    additive (evaluate_smart_category never un-places an existing match),
    so keeping every smart category fresh by default has no real downside
    the way scheduling AI-assisted evaluation by default would. A category
    with its own explicit schedule_interval_seconds (see
    categories_due_for_scheduled_evaluation) still gets that on top, for a
    faster/slower cadence than whatever a provider's own refresh triggers."""
    conn = _connect()
    rows = conn.execute("SELECT id FROM categories WHERE is_smart=1 AND rule_json IS NOT NULL").fetchall()
    conn.close()
    return [r["id"] for r in rows]


def set_catchall_include_adult(include_adult: bool) -> list[dict]:
    """Answers the first-run "include 18+ in the built-in All Movies/All TV
    Shows categories?" prompt -- flips exclude_adult on every match_all
    category (there are exactly two, seeded together) and re-evaluates them
    immediately so the change is visible right away instead of waiting for
    the next periodic refresh cycle."""
    import json
    conn = _connect()
    rows = conn.execute("SELECT id, rule_json FROM categories WHERE is_smart=1 AND rule_json IS NOT NULL").fetchall()
    results = []
    for r in rows:
        try:
            rule = json.loads(r["rule_json"])
        except (ValueError, TypeError):
            continue
        if not rule.get("match_all"):
            continue
        rule["exclude_adult"] = not include_adult
        conn.execute("UPDATE categories SET rule_json=? WHERE id=?", (json.dumps(rule), r["id"]))
        results.append(r["id"])
    _commit_with_retry(conn)
    conn.close()
    return [evaluate_smart_category(cid) for cid in results]


def evaluate_smart_category(category_id: int) -> dict:
    """Evaluate a smart category's rule_json against the whole pool (movies or
    series, per the category's content_type) and auto-place every match.
    Never un-places existing matches — same additive semantics as manual
    placement. Returns counts for the caller to surface in the UI.

    Excludes review_excluded=1 rows from the candidate pool entirely --
    without this, an archived item (see _should_auto_archive) still gets
    auto-placed into any smart category whose rule it happens to match,
    including the match_all catch-all categories, which defeats the whole
    point of archiving it: "archived" only means "hidden from VOD Manager's
    own review queues" at the row level (see bulk_set_review_excluded), it
    was never wired into category placement/export visibility on its own --
    this filter is what actually makes an archived item invisible to
    Dispatcharr, since visibility is governed by placement, not the flag."""
    category = get_category(category_id)
    if not category:
        raise ValueError(f"category {category_id} not found")
    if not category["is_smart"]:
        raise ValueError(f"category {category_id} is not a smart category")
    if not category["rule_json"]:
        raise ValueError(f"category {category_id} has no rule_json configured")

    import json
    rule = json.loads(category["rule_json"])

    conn = _connect()
    if category["content_type"] == "movie":
        rows = [dict(r) for r in conn.execute("""
            SELECT m.*, (
                SELECT GROUP_CONCAT(DISTINCT ms.provider_category_name) FROM movie_sources ms
                WHERE ms.movie_id = m.id AND ms.provider_category_name IS NOT NULL
            ) AS provider_category
            FROM movies m WHERE m.review_excluded=0
        """).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.*, (
                SELECT GROUP_CONCAT(DISTINCT es.provider_category_name) FROM episode_sources es
                JOIN episodes e ON e.id = es.episode_id
                WHERE e.series_id = s.id AND es.provider_category_name IS NOT NULL
            ) AS provider_category
            FROM series s WHERE s.review_excluded=0
        """).fetchall()]
    conn.close()

    matched_ids = [row["id"] for row in rows if _rule_matches(row, rule)]
    if category["content_type"] == "movie":
        newly_placed = bulk_place_movies_in_category(matched_ids, category_id)
    else:
        newly_placed = bulk_place_series_in_category(matched_ids, category_id)
    mark_category_evaluated(category_id)

    return {"evaluated": len(rows), "matched": len(matched_ids), "newly_placed": newly_placed}


def get_ai_candidate_rows(content_type: str, prefilter_rule_json: str | None, limit: int) -> tuple[list[dict], int]:
    """Bounded candidate pool for AI Evaluate (see ai_assist.py's
    evaluate_candidates_for_category) -- real per-item API cost means this
    can never run over the raw pool. Reuses the exact same rule_json
    pre-filter mechanism as rule-based smart categories (see
    evaluate_smart_category above) to narrow the field before applying the
    cap; without a pre-filter, it's just the first `limit` rows by id.
    Returns (candidates, total_before_cap) so the caller can tell the user
    how much was left out, rather than silently truncating."""
    import json
    conn = _connect()
    if content_type == "movie":
        rows = [dict(r) for r in conn.execute("SELECT * FROM movies").fetchall()]
    else:
        rows = [dict(r) for r in conn.execute("SELECT * FROM series").fetchall()]
    conn.close()

    if prefilter_rule_json:
        rule = json.loads(prefilter_rule_json)
        rows = [r for r in rows if _rule_matches(r, rule)]

    return rows[:limit], len(rows)


# ── Watch session history (per-person VOD viewing, from Dispatcharr's live
# connection stats) ──────────────────────────────────────────────────────
# See the watch_sessions table comment for why this exists and what
# client_id means here; dispatcharr_dvr_importer.poll_watch_sessions is the
# only caller.

def upsert_watch_session(
    dispatcharr_connection_id: int, client_id: str,
    dispatcharr_user_id: int | None, dispatcharr_username: str | None,
    content_type: str | None, content_name: str | None, content_uuid: str | None,
    client_ip: str | None, bytes_sent: int, position_seconds: float | None,
) -> None:
    """One poll's worth of a live VOD watch session. Reopens (clears
    ended_at on) a session that was previously marked ended if Dispatcharr's
    own client_id somehow reappears -- in practice this shouldn't happen
    given its embedded timestamp, but a reconnect reusing the same id
    should still be treated as the same session rather than silently
    dropped or duplicated."""
    conn = _connect()
    now = _now()
    existing = conn.execute(
        "SELECT id FROM watch_sessions WHERE dispatcharr_connection_id=? AND client_id=?",
        (dispatcharr_connection_id, client_id),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE watch_sessions SET last_seen_at=?, bytes_sent=?, position_seconds=?,
               dispatcharr_user_id=?, dispatcharr_username=?, ended_at=NULL WHERE id=?""",
            (now, bytes_sent, position_seconds, dispatcharr_user_id, dispatcharr_username, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO watch_sessions
               (dispatcharr_connection_id, client_id, dispatcharr_user_id, dispatcharr_username,
                content_type, content_name, content_uuid, client_ip, bytes_sent, position_seconds,
                started_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dispatcharr_connection_id, client_id, dispatcharr_user_id, dispatcharr_username,
             content_type, content_name, content_uuid, client_ip, bytes_sent, position_seconds,
             now, now),
        )
    _commit_with_retry(conn)
    conn.close()


def close_stale_watch_sessions(dispatcharr_connection_id: int, active_client_ids: list[str]) -> int:
    """Marks ended_at on any still-open session for this connection whose
    client_id wasn't in Dispatcharr's most recent /proxy/stats/ response --
    Dispatcharr only ever reports current state, so a session dropping out
    of that list is the only signal we get that it ended (no explicit
    'stopped' event to listen for)."""
    conn = _connect()
    now = _now()
    if active_client_ids:
        placeholders = ",".join("?" for _ in active_client_ids)
        cur = conn.execute(
            f"""UPDATE watch_sessions SET ended_at=? WHERE dispatcharr_connection_id=? AND ended_at IS NULL
                AND client_id NOT IN ({placeholders})""",
            (now, dispatcharr_connection_id, *active_client_ids),
        )
    else:
        cur = conn.execute(
            "UPDATE watch_sessions SET ended_at=? WHERE dispatcharr_connection_id=? AND ended_at IS NULL",
            (now, dispatcharr_connection_id),
        )
    _commit_with_retry(conn)
    conn.close()
    return cur.rowcount


def list_watch_sessions(
    dispatcharr_user_id: int | None = None, active_only: bool = False, limit: int = 500,
) -> list[dict]:
    conn = _connect()
    query = "SELECT * FROM watch_sessions WHERE 1=1"
    params: list = []
    if dispatcharr_user_id is not None:
        query += " AND dispatcharr_user_id=?"
        params.append(dispatcharr_user_id)
    if active_only:
        query += " AND ended_at IS NULL"
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
