"""
Imports a real provider's VOD catalog into our own pool.

Two-phase, matching how Dispatcharr itself (and every XC client) actually
handles this: a cheap bulk list import (name/year/category/stream_id — one
call for the whole catalog) now, and expensive per-item detail enrichment
(genre, cast, tmdb_id, poster, description) fetched lazily on demand and
cached (see vod_db.ENRICHMENT_TTL_SECONDS). bulk_enrich_all() below covers
the whole pool at once, still one item at a time under the hood, just with
bounded concurrency instead of a human clicking one movie at a time.
"""

import asyncio
import inspect
import math
import json
import logging
import os
import re
import sqlite3
import time

import httpx

import config
import emby_vod_client
import tmdb_sync
import vod_db
from xc_server import _redact_upstream_url


async def _run_scoped_collision_sweep(sweep, item_ids: set[int]) -> list[dict]:
    """Run a collision sweep with the bounded IDs used by this workflow.

    The signature fallback keeps older integrations that monkeypatch the
    maintenance helper without parameters working; the real database helper
    always accepts the scoped ID set, so automatic production runs remain
    delta-scoped.
    """
    if not item_ids:
        return []
    try:
        inspect.signature(sweep).bind(item_ids)
    except (TypeError, ValueError):
        return await asyncio.to_thread(sweep) or []
    return await asyncio.to_thread(sweep, item_ids) or []


_SHARED_IMPORT_COUNT_KEYS = ("sources_changed", "total", "flagged_for_review", "errors")


def _merge_import_counts(movie_result: dict, series_result: dict) -> dict:
    """Merge movie/series import results without one overwriting the other.

    KNM: 2026-10-03 both results use the same count keys; keep per-type
    copies (movie_*/series_*) and report the combined keys as sums.
    """
    merged = {**movie_result, **series_result}
    for key in _SHARED_IMPORT_COUNT_KEYS:
        movie_value = movie_result.get(key, 0) or 0
        series_value = series_result.get(key, 0) or 0
        merged[f"movie_{key}"] = movie_value
        merged[f"series_{key}"] = series_value
        merged[key] = movie_value + series_value
    return merged


def _queue_catalog_enrichment_for_changes(
    *, changed_movie_ids: set[int], changed_series_ids: set[int], workflow_run_id: int | None = None,
) -> bool:
    """Queue delta-scoped enrichment while tolerating legacy test hooks."""
    kwargs = {
        "changed_movie_ids": changed_movie_ids,
        "changed_series_ids": changed_series_ids,
        "workflow_run_id": workflow_run_id,
    }
    try:
        inspect.signature(schedule_post_import_enrichment).bind(**kwargs)
    except (TypeError, ValueError):
        return schedule_post_import_enrichment()
    return schedule_post_import_enrichment(**kwargs)


def _current_lang_settings() -> dict:
    """Builds the `lang` dict _should_auto_archive/_row_excluded_by_rule
    read: enabled_languages (config.get_enabled_languages(), the include-list
    the language-prefix gate now checks membership against) merged with
    exclude_non_latin (still config.get_import_language_exclusion(), which
    also still backs that setting's own "Import Language Exclusion" UI card).
    Single shared builder so every caller (XC/vod_importer.py, Plex, Emby)
    stays in sync rather than re-deriving this merge independently."""
    return {
        "enabled_languages": config.get_enabled_languages(),
        "exclude_non_latin": config.get_import_language_exclusion()["exclude_non_latin"],
    }


def _should_auto_archive(
    name: str, provider_category_name: str | None = None, provider_exclude_categories: list[str] = (),
    exclude_uncategorized: bool = False, lang: dict | None = None, country: list[str] | None = None,
    raw_name: str | None = None,
) -> bool:
    """Import-time equivalent of the manual Language Filter archive tool --
    deliberately NOT sibling-safe (see USERGUIDE's Language Filter section
    for that tool's "don't archive the only copy" behavior): an explicit
    exclusion rule means "I don't want this content in my library at all",
    not "prefer another language's copy if one exists". Language rules are
    global (config.get_import_language_exclusion); category rules are
    per-provider (providers.import_exclude_categories), since available
    categories genuinely differ provider to provider.

    Despite the name (kept for existing callers), an excluded item is no
    longer archived. It used to still be fully
    imported and stored, just tagged auto_archive=True (hidden from review
    queues, but otherwise fully present -- see vod_db.bulk_set_review_
    excluded's "still fully browsable/playable/categorizable" archive
    semantics). Per user direction (2026-09-10, referencing Dispatcharr's
    VOD-group "unchecked = not imported" setting): a real exclusion should
    mean the content is never stored at all. Every caller now uses this to
    filter the item out of movie_items/series_items entirely, before it
    ever reaches bulk_import_movies/bulk_import_series, instead of tagging
    it for archive after the fact. Re-including a category (or removing a
    language-exclusion rule) then re-importing is what brings the content
    back -- see vod_db.purge_excluded_archived_content for the matching
    one-time cleanup of rows that were archived under the old behavior.

    provider_category_name/provider_exclude_categories default to no-ops
    for callers that only want language rules -- plex_importer.py/
    emby_vod_importer.py (GH#9) pass their own provider's library/
    collection-folder name in this same field, since a Plex library
    section (or Emby/Jellyfin virtual folder) is exactly what a user means
    by "category" for those provider types, even though it's not an
    XC-style flat category list under the hood.

    exclude_uncategorized (GH issue #7): a real provider was found shipping
    movies with no category attached at all -- the category-name check below
    can never catch that (there's no name to compare), so this is a
    dedicated switch, checked only when the item truly has no category,
    never as a substitute for an actual category-name match.

    raw_name (2026-09-12 fix -- discussed previously but never actually
    landed): the language-prefix check below MUST run against the
    provider's untouched raw name, not `name`. By the time callers compute
    `name`, it has already gone through parse_name_year and
    vod_db.apply_rules_to_value(movie_name_rules) -- and the built-in
    Title & Metadata Rules commonly include a rule stripping exactly this
    kind of leading "CODE - " language prefix for display purposes (e.g.
    "ES - 3 días en Malay (2023)" -> "3 días en Malay (2023)"). Checking
    the prefix code on the already-stripped `name` meant it always fell
    back to the "EN" default and passed the enabled_languages gate no
    matter what language the item actually was -- while vod_db.
    _source_language(raw_name) (run separately, later, when storing the
    row) correctly used the untouched raw_name and tagged the row's true
    language. Result: excluded-language content was tagged correctly in
    the DB yet was never actually excluded at import. Falls back to `name`
    when no raw_name is supplied (existing direct callers/tests).

    country (Import Country Exclusion): a trailing "(<country code>)" tag
    (vod_db._country_suffix_code), checked against the configured exclude
    list; callers pre-fetch it once per import, None reads config."""
    check_name = raw_name if raw_name is not None else name
    lang = lang if lang is not None else _current_lang_settings()
    # _source_language's default applies here too: a name with no recognized
    # prefix is untagged EN/ES-convention content, not "unknown" -- without
    # this fallback, an admin who leaves "EN" out of Enabled Playback
    # Languages wouldn't actually exclude the untagged titles that setting
    # counts as EN, since untagged names never match a literal prefix code.
    #
    # 2026-09-11: inverted from an explicit exclude-list membership test
    # (lang["exclude_prefixes"], a separate manually-maintained list that
    # could silently miss a language -- "IR" was a real gap) to an explicit
    # include-list membership test against lang["enabled_languages"]
    # (config.get_enabled_languages(), the same "Enabled Playback Languages"
    # list already used query-time by vod_db._enabled_languages_clause). Any
    # language not currently enabled for playback is now excluded at import
    # time too, with no separate exclude list to keep in sync.
    # Use the same classifier used when the source row is stored. This
    # includes trailing provider country tags such as "Title (DE)" and
    # "Title (PL)", which otherwise default to EN and are only corrected
    # later by language recompute.
    code = vod_db._source_language(raw_name if raw_name is not None else name)
    if code not in lang["enabled_languages"]:
        return True
    if lang["exclude_non_latin"] and vod_db._is_non_latin_name(raw_name if raw_name is not None else name):
        return True
    country = country if country is not None else config.get_import_country_exclusion()
    if country:
        code = vod_db._country_suffix_code(check_name)
        if code and code in country:
            return True
    if provider_category_name:
        if provider_category_name in provider_exclude_categories:
            return True
    elif exclude_uncategorized:
        return True
    return False


def _as_dict(value) -> dict:
    """get_vod_info/get_series_info are documented as returning an object,
    but at least one real provider returns a bare list (e.g. `[]`) instead
    of `{}` for "no data" -- either at the top level or nested under
    "info" -- which crashed every .get() downstream with 'list' object has
    no attribute 'get', silently failing that item's whole enrichment (and,
    for series, its episodes -- see enrich_series). Treat anything that
    isn't actually a dict as "no data" instead of raising."""
    return value if isinstance(value, dict) else {}

logger = logging.getLogger(__name__)

_YEAR_SUFFIX_RE = re.compile(
    # (?<!\d) keeps this from firing inside a genuine in-title year range like
    # "... (1987-1997)" or "Wartorn: 1861-2010" -- without it, the dash/paren
    # right before the second year in the range looks identical to a real
    # trailing year suffix and the title gets mangled.
    r"^(.*?)\s*(?<!\d)[-(]\s*(19\d{2}|20\d{2})\)?\s*(?:\[[^\]]*\]|[A-Z][A-Z\- ]{2,})?\s*$"
)

# Some real XC providers silently drop the connection -- no HTTP response
# at all -- for requests without a browser-like User-Agent,
# httpx's default ("python-httpx/x.y.z") included. A generic desktop-browser
# UA is enough to get a normal response.
_UPSTREAM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}


def _coerce_int(value) -> int | None:
    """Same reasoning as _coerce_year below, generalized -- bitrate in
    particular has been observed as a plain int from one real provider and
    there's no guarantee another sends it consistently."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# KNM: 2026-10-04 -- artwork and descriptive metadata come from TMDB after
# import, so provider image-URL rotation was flagging ~25K titles "changed"
# on one refresh and re-running enrichment for all of them. Only fields that
# decide playback, identity or placement count; last_modified stays because
# it is a series' new-episodes signal.
_CATALOG_FINGERPRINT_FIELDS = {
    "movie": ("stream_id", "name", "category_id", "container_extension", "tmdb", "tmdb_id"),
    "series": ("series_id", "name", "year", "category_id", "tmdb", "tmdb_id", "last_modified"),
}
_LEGACY_CATALOG_FINGERPRINT_FIELDS = {
    "movie": ("stream_id", "name", "category_id", "container_extension", "tmdb", "stream_icon", "cover", "cover_big", "movie_image"),
    "series": ("series_id", "name", "year", "category_id", "genre", "plot", "cast", "director", "cover", "rating", "releaseDate", "release_date", "tmdb", "tmdb_id", "last_modified"),
}


def _hash_catalog_fields(fields, item: dict, provider_category_name: str | None) -> str:
    payload = {field: item.get(field) for field in fields}
    payload["provider_category_name"] = provider_category_name
    return vod_db.fingerprint_hash(payload)


def _catalog_fingerprint(kind: str, item: dict, provider_category_name: str | None = None) -> str:
    """Hash only provider-owned list fields that can change catalog state."""
    return _hash_catalog_fields(_CATALOG_FINGERPRINT_FIELDS[kind], item, provider_category_name)


def _legacy_catalog_fingerprint(kind: str, item: dict, provider_category_name: str | None = None) -> str:
    """Pre-2026-10-04 fingerprint, used only to upgrade stored values in place."""
    return _hash_catalog_fields(_LEGACY_CATALOG_FINGERPRINT_FIELDS[kind], item, provider_category_name)


def _coerce_year(value) -> int | None:
    """Some XC providers send the series "year" field as a string (or an
    empty string, or junk like "N/A") rather than a number -- SQLite's
    INTEGER column affinity happens to silently coerce a clean numeric
    string on insert, which is exactly why this went unnoticed here, but
    anything that doesn't look like a plain year would still get stored
    as-is and quietly break every exact (name, year) match downstream
    (series import's own dedup lookup, needs_year_review, the duplicate
    finder's group scan)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean_tmdb_id(value) -> str | None:
    """Real user data found live 2026-09-05: at least one provider sends
    tmdb="0" as its own "no id known" sentinel rather than omitting the
    field -- a plain `value or None` check treats the non-empty string "0"
    as truthy, so that sentinel was getting stored as a real tmdb_id (149 of
    ~11k series in one real pool). Every tmdb_id extraction site should run
    through this instead of a bare truthiness check."""
    if value in (None, "", 0, "0"):
        return None
    return str(value)


def parse_name_year(raw_name: str) -> tuple[str, int | None]:
    """Real XC providers commonly bake the year into the title string itself,
    not always as a clean trailing "(YYYY)" -- also seen: "Title - YYYY",
    "Title (YYYY) [MULTI-SUB]", "Title (YYYY) HINDI", and even an unclosed
    "Title (YYYY". Some catalogs duplicate it (e.g. "1 1 (2018) (2018)") --
    strip every trailing year layer, not just one, or the leftover copy in
    the name doubles up with the year we display alongside it."""
    name = raw_name.strip()
    year = None
    while True:
        m = _YEAR_SUFFIX_RE.match(name)
        if not m:
            break
        new_name = m.group(1).strip()
        if new_name == name:
            break
        name, year = new_name, int(m.group(2))
    return name, year


class ProviderBackoffError(Exception):
    """Raised in place of an actual network call while a provider is under
    an active backoff cooldown (see _PROVIDER_BACKOFF below) -- lets a
    caller (bulk_enrich_all's per-item loop) skip the item cheaply, with no
    request sent, instead of piling more load onto a provider that's
    already shown signs of rate-limiting/blocking."""


# Per-provider backoff state, keyed by provider_id: {"failures": int,
# "until": float (time.time() timestamp, 0.0 if not currently backing off)}.
# Real user report 2026-09-02: bulk enrich against a large catalog reliably
# started throwing "Temporary failure in name resolution" (DNS-level, i.e.
# the provider or the network path to it stopped responding at all) after a
# sustained burst of requests, then 403s once it came back -- classic
# provider-side throttling/blocking under load, not anything the app was
# doing wrong per item. Bulk enrich has no per-provider rate limit of its
# own (just a flat concurrency=8 semaphore shared across every source), so
# once a provider starts rejecting/dropping connections, every one of those
# 8 concurrent slots kept hammering it in lockstep instead of backing off --
# maximizing exactly the load pattern that trips a provider's own rate
# limiter, and guaranteeing every retry attempt (this run or the next) would
# hit the exact same wall.
_PROVIDER_BACKOFF: dict[int, dict] = {}
_BACKOFF_FAILURE_THRESHOLD = 3
_BACKOFF_BASE_SECONDS = 15.0
_BACKOFF_MAX_SECONDS = 600.0
# Status codes and exception types that indicate the PROVIDER itself is
# throttling/blocking -- as opposed to e.g. a 404 for one missing/removed
# item, which says nothing about the provider's overall health and would
# otherwise trip the same backoff for every other item for no reason.
_BACKOFF_STATUS_CODES = {403, 429, 503}
_BACKOFF_EXCEPTION_TYPES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)


def _provider_backoff_remaining(provider_id: int) -> float:
    state = _PROVIDER_BACKOFF.get(provider_id)
    if not state:
        return 0.0
    return max(0.0, state["until"] - time.time())


def _record_provider_failure(provider_id: int, provider_name: str) -> None:
    state = _PROVIDER_BACKOFF.setdefault(provider_id, {"failures": 0, "until": 0.0})
    state["failures"] += 1
    if state["failures"] >= _BACKOFF_FAILURE_THRESHOLD:
        # Exponential in how many threshold-lengths of failures have piled
        # up, so a provider that keeps failing through one cooldown gets a
        # longer one next time instead of getting hammered again the moment
        # the short cooldown expires.
        tier = state["failures"] // _BACKOFF_FAILURE_THRESHOLD
        delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (tier - 1)))
        already_backing_off = _provider_backoff_remaining(provider_id) > 0
        state["until"] = time.time() + delay
        if not already_backing_off:
            logger.warning(
                "[vod_importer] provider=%s looks rate-limited/blocked (%d consecutive failures) -- "
                "backing off enrichment requests to it for %.0fs",
                provider_name, state["failures"], delay,
            )
        # Provider A case (2026-09-12): ~11K movie backoff-skips in one
        # run, cycling through repeated 3-failures/15s-backoff/recover. The
        # persistent per-provider client (_get_provider_client) was never
        # torn down across that cycle, so every retry after cooldown resumed
        # through the exact same connection pool/TLS session identity provider A
        # had just rate-limited seconds earlier -- indistinguishable from the
        # traffic that tripped the backoff in the first place if provider A's
        # blocking keys on anything beyond raw request rate. Evict it here
        # (once per trip, not on every failure) so the next call after
        # cooldown opens a genuinely fresh connection instead.
        stale_client = _PROVIDER_CLIENTS.pop(provider_id, None)
        if stale_client is not None:
            _CLIENTS_PENDING_CLOSE.append(stale_client)


def _record_provider_success(provider_id: int) -> None:
    state = _PROVIDER_BACKOFF.get(provider_id)
    if state and (state["failures"] or state["until"]):
        logger.info("[vod_importer] provider=%s recovered -- clearing backoff", provider_id)
    _PROVIDER_BACKOFF.pop(provider_id, None)


# Adaptive per-provider concurrency cap, layered on top of the binary
# backoff above -- raised in a 2026-09-04 discussion prompted by a user still
# seeing real provider rate-limiting: the binary backoff only ever goes from
# "full concurrency" to "fully paused", with no middle ground. This narrows a
# provider's own share of the concurrency budget gradually the moment it
# shows trouble (halves it, floor 1), then creeps back up by 1 after a run of
# clean calls -- an AIMD approach, same shape as TCP congestion control, so a
# genuinely-throttled provider settles near whatever concurrency it can
# actually sustain instead of alternating between "wide open" and "fully
# stopped". A healthy provider is never artificially slowed down -- every
# provider starts at _PROVIDER_MAX_CONCURRENCY (today's unrestricted
# behavior) and only narrows in response to an actual failure signal (the
# same _BACKOFF_STATUS_CODES/_BACKOFF_EXCEPTION_TYPES the binary backoff
# already watches for). In-memory only, same as _PROVIDER_BACKOFF -- resets
# on restart, which just means a provider gets to prove itself healthy again
# rather than staying permanently throttled from a stale prior run. This is
# independent of bulk_enrich_all's own `concurrency` semaphore (which caps
# TOTAL in-flight movie/series work across every provider combined) -- this
# caps one specific provider's share of that budget, which the shared
# semaphore alone can't do when several providers' items are interleaved in
# the same run.
_PROVIDER_MIN_CONCURRENCY = 1
_PROVIDER_MAX_CONCURRENCY = 8
_PROVIDER_RAMP_SUCCESSES = 25  # consecutive clean calls at the current cap before nudging it up by 1


class _AdaptiveLimiter:
    def __init__(self) -> None:
        self.cap = _PROVIDER_MAX_CONCURRENCY
        self._in_use = 0
        self._streak = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while self._in_use >= self.cap:
                await self._cond.wait()
            self._in_use += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_use -= 1
            self._cond.notify_all()

    async def note_failure(self, provider_name: str = "", reason: str = "") -> None:
        # Log cap reductions so a provider's visible throttling state has
        # evidence alongside the binary _PROVIDER_BACKOFF warning (which only
        # fires after its own consecutive-failure threshold).
        async with self._cond:
            self._streak = 0
            new_cap = max(_PROVIDER_MIN_CONCURRENCY, self.cap // 2)
            if new_cap != self.cap:
                logger.warning(
                    "[vod_importer] provider=%s reducing concurrency %d -> %d after %s",
                    provider_name or "?", self.cap, new_cap, reason or "unspecified error",
                )
            self.cap = new_cap

    async def note_success(self) -> None:
        async with self._cond:
            if self.cap >= _PROVIDER_MAX_CONCURRENCY:
                self._streak = 0
                return
            self._streak += 1
            if self._streak >= _PROVIDER_RAMP_SUCCESSES:
                self.cap += 1
                self._streak = 0
                self._cond.notify_all()  # newly-available slot(s) -- wake any acquire() waiters


_PROVIDER_LIMITERS: dict[int, _AdaptiveLimiter] = {}


def _get_provider_limiter(provider_id: int) -> _AdaptiveLimiter:
    limiter = _PROVIDER_LIMITERS.get(provider_id)
    if limiter is None:
        limiter = _AdaptiveLimiter()
        _PROVIDER_LIMITERS[provider_id] = limiter
    return limiter


# Persistent per-provider httpx.AsyncClient, keyed by provider_id -- kept
# alive for the life of the process instead of opening/closing a brand new
# TCP+TLS connection (httpx.AsyncClient(...) as a context manager, torn down
# at the end of every single _call) for every individual API request. Under
# bulk enrichment's 8-concurrent-per-kind load that handshake overhead was
# pure fixed cost paid on every one of thousands of calls; httpx's own
# connection pool handles reusing/keeping-alive the underlying sockets once
# the client itself is long-lived. Never explicitly closed -- these live for
# the process lifetime, same as _PROVIDER_LIMITERS/_PROVIDER_BACKOFF.
_PROVIDER_CLIENTS: dict[int, httpx.AsyncClient] = {}

# Clients evicted by _record_provider_failure on a backoff trip, waiting to be
# closed. Eviction happens from sync code (_record_provider_failure has sync
# callers), but httpx.AsyncClient.aclose() is a coroutine -- queuing here and
# draining from the async call path (_call, right before issuing the next
# request) avoids making _record_provider_failure async just to close a socket.
_CLIENTS_PENDING_CLOSE: list[httpx.AsyncClient] = []


async def _drain_closed_clients() -> None:
    while _CLIENTS_PENDING_CLOSE:
        await _CLIENTS_PENDING_CLOSE.pop().aclose()


async def evict_provider_client(provider_id: int) -> None:
    """Plan-doc follow-up "provider HTTP-client lifecycle" (2026-09-14):
    explicit cleanup for a single provider, called when that provider is
    deleted or its connection settings (base_url/username/password/
    custom_user_agent) change -- so a stale pooled client (old credentials/
    headers, or a provider that no longer exists) doesn't keep getting
    reused until an unrelated backoff trip happens to evict it (which might
    never happen for a deleted provider). Also drops the limiter/backoff
    in-memory state, since both are keyed by a provider_id that may now
    refer to nothing, or to a provider under a different identity."""
    client = _PROVIDER_CLIENTS.pop(provider_id, None)
    if client is not None:
        _CLIENTS_PENDING_CLOSE.append(client)
    _PROVIDER_LIMITERS.pop(provider_id, None)
    _PROVIDER_BACKOFF.pop(provider_id, None)
    await _drain_closed_clients()


async def close_all_provider_clients() -> None:
    """Plan-doc follow-up "provider HTTP-client lifecycle" (2026-09-14):
    called from main.py's lifespan shutdown so no pooled provider socket/TLS
    session outlives the process, instead of relying on OS process teardown
    to reclaim them."""
    for provider_id in list(_PROVIDER_CLIENTS):
        await evict_provider_client(provider_id)


def _get_provider_client(provider_id: int, headers: dict) -> httpx.AsyncClient:
    client = _PROVIDER_CLIENTS.get(provider_id)
    if client is None:
        # Pool sized to the adaptive limiter's CURRENT cap, not always
        # _PROVIDER_MAX_CONCURRENCY -- a provider that's already been halved
        # down to e.g. 4 by _AdaptiveLimiter.note_failure() should get a
        # physical pool that actually enforces that narrower ceiling too,
        # otherwise the pool would still allow more concurrent sockets than
        # the logical limiter is granting.
        cap = _PROVIDER_LIMITERS[provider_id].cap if provider_id in _PROVIDER_LIMITERS else _PROVIDER_MAX_CONCURRENCY
        client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=headers,
            limits=httpx.Limits(max_connections=cap, max_keepalive_connections=cap),
        )
        _PROVIDER_CLIENTS[provider_id] = client
    return client


class XCProviderClient:
    def __init__(self, provider: dict):
        self.provider = provider
        self.provider_id = provider.get("id")
        self.provider_name = provider.get("name") or str(self.provider_id)
        self.base_url = provider["base_url"].rstrip("/")
        self.username = provider["username"]
        self.password = provider["password"]
        custom_ua = provider.get("custom_user_agent")
        self.headers = {"User-Agent": custom_ua} if custom_ua else _UPSTREAM_HEADERS

    async def _call(self, action: str | None = None, **params) -> object:
        limiter = None
        if self.provider_id is not None:
            remaining = _provider_backoff_remaining(self.provider_id)
            if remaining > 0:
                raise ProviderBackoffError(
                    f"provider={self.provider_name} is backing off for another {remaining:.0f}s (looked rate-limited/blocked)"
                )
            limiter = _get_provider_limiter(self.provider_id)
            await limiter.acquire()
        query = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update(params)
        try:
            try:
                client = (
                    _get_provider_client(self.provider_id, self.headers) if self.provider_id is not None
                    else httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=self.headers)
                )
                r = await client.get(f"{self.base_url}/player_api.php", params=query)
                r.raise_for_status()
                # r.json() is a synchronous json.loads() under the hood --
                # for a large catalog's bulk get_vod_streams/get_series
                # response (tens of MB, hundreds of thousands of items)
                # that can block the event loop for real time, starving
                # every other in-flight coroutine (including a concurrent
                # sibling call on this same provider -- see
                # import_provider_catalog's movies/series asyncio.gather)
                # of the chance to keep reading their own response off
                # the socket, which can trip THEIR httpx timeout even
                # though nothing is actually wrong with their connection.
                # Confirmed live 2026-09-06: making movies+series import
                # concurrent for a ~240k-item provider (CRX) immediately
                # started failing with opaque timeouts that didn't happen
                # run sequentially -- moving the parse off the loop fixed it.
                result = await asyncio.to_thread(r.json)
            except _BACKOFF_EXCEPTION_TYPES as exc:
                if self.provider_id is not None:
                    _record_provider_failure(self.provider_id, self.provider_name)
                    await limiter.note_failure(provider_name=self.provider_name, reason=type(exc).__name__)
                raise
            except httpx.HTTPStatusError as exc:
                if self.provider_id is not None and exc.response.status_code in _BACKOFF_STATUS_CODES:
                    _record_provider_failure(self.provider_id, self.provider_name)
                    await limiter.note_failure(
                        provider_name=self.provider_name, reason=f"HTTP {exc.response.status_code}"
                    )
                raise
            else:
                if self.provider_id is not None:
                    _record_provider_success(self.provider_id)
                    await limiter.note_success()
                return result
        finally:
            if limiter is not None:
                await limiter.release()
            if _CLIENTS_PENDING_CLOSE:
                await _drain_closed_clients()

    async def auth(self) -> dict:
        return await self._call()

    async def get_vod_categories(self) -> list[dict]:
        return await self._call("get_vod_categories")

    async def get_vod_streams(self) -> list[dict]:
        return await self._call("get_vod_streams")

    async def get_vod_info(self, vod_id: str) -> dict:
        return await self._call("get_vod_info", vod_id=vod_id)

    async def get_series_categories(self) -> list[dict]:
        return await self._call("get_series_categories")

    async def get_series(self) -> list[dict]:
        return await self._call("get_series")

    async def get_series_info(self, series_id: str) -> dict:
        return await self._call("get_series_info", series_id=series_id)


async def _import_movies_for_provider(
    client: "XCProviderClient", provider: dict, provider_id: int,
    category_names: dict[str, str], exclude_categories: list[str], exclude_uncategorized: bool,
) -> tuple[dict, int, set[str]]:
    fetch_started = time.time()
    streams = await client.get_vod_streams()
    fetch_elapsed = time.time() - fetch_started
    movie_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "movie", "name")
    lang = _current_lang_settings()
    country = config.get_import_country_exclusion()
    movie_items, seen_stream_ids = await asyncio.to_thread(
        _build_movie_import_items, streams, category_names, exclude_categories,
        exclude_uncategorized, lang, movie_name_rules, country,
    )
    db_started = time.time()
    movie_result = await asyncio.to_thread(vod_db.bulk_import_movies, provider_id, movie_items)
    await asyncio.to_thread(vod_db.apply_provider_trailers, provider_id, "movie", movie_items)
    db_elapsed = time.time() - db_started
    logger.info(
        "[vod_importer] provider=%s movies: %s (fetch=%.2fs db_write=%.2fs items=%d)",
        provider["name"], movie_result, fetch_elapsed, db_elapsed, len(streams),
    )
    return movie_result, len(streams), seen_stream_ids


def _build_movie_import_items(streams, category_names, exclude_categories, exclude_uncategorized, lang, movie_name_rules, country=None):
    """CPU-only list normalization; deliberately runs outside FastAPI's loop."""
    movie_items = []
    seen_stream_ids = set()
    for s in streams:
        stream_id = str(s["stream_id"])
        seen_stream_ids.add(stream_id)
        name, year = parse_name_year(s.get("name") or "")
        name = vod_db.apply_rules_to_value(name, movie_name_rules)
        category_name = category_names.get(str(s.get("category_id")))
        if _should_auto_archive(
            name, category_name, exclude_categories, exclude_uncategorized, lang, country=country, raw_name=s.get("name") or "",
        ):
            continue
        movie_items.append({
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": s.get("container_extension") or "mp4",
            "provider_category_name": category_name,
            # The provider's own unstripped name, before parse_name_year and
            # Title & Metadata Rules clean it up -- "4K"/"UHD"-style quality
            # markers commonly live in exactly the prefix those rules are
            # meant to strip, and movies.name is shared across every source
            # of this movie (that's what makes them the same movie), so it
            # can never differentiate between two sources' quality on its
            # own. This is the real per-source signal a quality-based stream
            # priority feature would need (see vod_manager-ghi).
            "raw_name": s.get("name") or "",
            "catalog_fingerprint": _catalog_fingerprint("movie", s, category_name),
            "legacy_catalog_fingerprint": _legacy_catalog_fingerprint("movie", s, category_name),
            # Some providers' bulk get_vod_streams list already includes
            # this (confirmed live 2026-09-05: 3 of 5 real providers) --
            # capturing it lets enrich_movie's TMDB-first fallback kick in
            # from this movie's very first enrichment pass. No genre/cast/
            # plot in this endpoint though (unlike get_series), so nothing
            # else is worth capturing here.
            # XC panels are inconsistent here: some use ``tmdb`` while
            # others expose the same bulk-list identity as ``tmdb_id``.
            # Capture both so valid IDs do not fall into the expensive
            # provider-detail fallback queue.
            "tmdb_id": _clean_tmdb_id(s.get("tmdb")) or _clean_tmdb_id(s.get("tmdb_id")),
            "trailer": s.get("trailer") or s.get("youtube_trailer"),
            # XC movie-list responses conventionally expose their free bulk
            # artwork as stream_icon.
            # A few nonstandard panels use one of the later names instead,
            # so retain those as harmless fallbacks. This avoids an expensive
            # get_vod_info request for every movie merely to populate cards;
            # enrichment/TMDB still fills or improves any missing artwork.
            "poster_url": (
                s.get("stream_icon") or s.get("cover") or
                s.get("cover_big") or s.get("movie_image") or None
            ),
        })
    # Keep this raw snapshot separate from movie_items.  movie_items is
    # intentionally filtered by local policy, while reconciliation needs the
    # provider's complete advertised snapshot.
    return movie_items, seen_stream_ids


async def _import_series_for_provider(
    client: "XCProviderClient", provider: dict, provider_id: int,
    series_category_names: dict[str, str], exclude_categories: list[str], exclude_uncategorized: bool,
) -> tuple[dict, int, set[str]]:
    fetch_started = time.time()
    series_list = await client.get_series()
    fetch_elapsed = time.time() - fetch_started
    series_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
    # Most real XC panels' bulk get_series list already carries the same
    # detail fields enrich_series would otherwise pay a separate
    # get_series_info call per series to fetch (confirmed live 2026-09-05
    # against 5 of 6 real providers: plot, cast, director, genre, cover,
    # rating, tmdb all present). Rules fetched once here, not per item --
    # same reasoning as series_name_rules above, just extended to every
    # field bulk_import_series can now capture for free.
    detail_rules = {
        field: await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", field)
        for field in ("genre", "description", "cast_list", "director")
    }
    lang = _current_lang_settings()
    country = config.get_import_country_exclusion()
    series_items, seen_series_ids = await asyncio.to_thread(
        _build_series_import_items, series_list, series_category_names,
        exclude_categories, exclude_uncategorized, lang, series_name_rules, detail_rules, country,
    )
    db_started = time.time()
    series_result = await asyncio.to_thread(vod_db.bulk_import_series, provider_id, series_items)
    await asyncio.to_thread(vod_db.apply_provider_trailers, provider_id, "series", series_items)
    db_elapsed = time.time() - db_started
    logger.info(
        "[vod_importer] provider=%s series: %s (fetch=%.2fs db_write=%.2fs items=%d)",
        provider["name"], series_result, fetch_elapsed, db_elapsed, len(series_list),
    )
    return series_result, len(series_list), seen_series_ids


def _build_series_import_items(series_list, series_category_names, exclude_categories, exclude_uncategorized, lang, series_name_rules, detail_rules, country=None):
    """CPU-only list normalization; deliberately runs outside FastAPI's loop."""
    series_items = []
    seen_series_ids = set()
    for s in series_list:
        provider_series_id = str(s["series_id"])
        seen_series_ids.add(provider_series_id)
        name, year = parse_name_year(s.get("name") or "")
        name = vod_db.apply_rules_to_value(name, series_name_rules)
        category_name = series_category_names.get(str(s.get("category_id")))
        if _should_auto_archive(
            name, category_name, exclude_categories, exclude_uncategorized, lang, country=country, raw_name=s.get("name") or "",
        ):
            continue
        series_items.append({
            "name": name,
            "year": year or _coerce_year(s.get("year")),
            "provider_series_id": provider_series_id,
            "provider_category_name": category_name,
            # See movie_items' identical raw_name field above -- the
            # provider's own unstripped name, before parse_name_year and
            # Title & Metadata Rules clean it up.
            "raw_name": s.get("name") or "",
            "catalog_fingerprint": _catalog_fingerprint("series", s, category_name),
            "legacy_catalog_fingerprint": _legacy_catalog_fingerprint("series", s, category_name),
            "_has_detail": True,
            "genre": vod_db.apply_rules_to_value(s.get("genre") or None, detail_rules["genre"]),
            "description": vod_db.apply_rules_to_value(s.get("plot") or None, detail_rules["description"]),
            "cast_list": vod_db.apply_rules_to_value(s.get("cast") or None, detail_rules["cast_list"]),
            "director": vod_db.apply_rules_to_value(s.get("director") or None, detail_rules["director"]),
            "poster_url": s.get("cover") or None,
            "rating": s.get("rating") or None,
            "release_date": s.get("releaseDate") or s.get("release_date") or None,
            # Some providers send this under "tmdb", not "tmdb_id" -- see
            # enrich_series's identical comment for why both need checking.
            "tmdb_id": _clean_tmdb_id(s.get("tmdb")) or _clean_tmdb_id(s.get("tmdb_id")),
            "trailer": s.get("trailer") or s.get("youtube_trailer"),
            "provider_last_modified": s.get("last_modified") or None,
        })
    return series_items, seen_series_ids


# KNM: added 2026-09-15 -- imports and enrichment run server-side, so the
# sidebar needs shared runtime state instead of relying on the browser that
# happened to initiate a job.
_IMPORT_PROGRESS: dict = {
    "running": False, "queued": False, "queue_position": None,
    "provider_id": None, "provider_name": None,
    "started_at": None, "finished_at": None, "error": None,
}
# This is deliberately distinct from the immediate provider-import state
# above. A catalog import can finish its provider fetch while the automatic
# TMDB, provider-detail, and safe duplicate-reconciliation phases are still
# running. Keeping the whole lifecycle here gives every connected browser a
# single, durable answer to "is it safe to begin manual review yet?".
_CATALOG_WORKFLOW_PROGRESS: dict = {
    "state": "idle",  # idle | queued | running | ready | failed
    "phase": None,
    "provider_name": None,
    "run_id": None,
    "queued_at": None,
    "import_started_at": None,
    "import_finished_at": None,
    "reconciliation_started_at": None,
    "reconciliation_finished_at": None,
    "enrichment_started_at": None,
    "enrichment_finished_at": None,
    "ready_at": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_XC_IMPORT_LOCK = asyncio.Lock()
_CPU_SAMPLE: tuple[float, float] | None = None


def get_import_progress() -> dict:
    return dict(_IMPORT_PROGRESS)


def get_catalog_workflow_progress() -> dict:
    """Shared import-to-review lifecycle for the header and sidebar."""
    progress = dict(_CATALOG_WORKFLOW_PROGRESS)
    # The live lifecycle is intentionally in memory, but the last completed
    # report is durable.  After a container restart, hydrate the header from
    # that report instead of showing "duration pending" forever.
    latest = vod_db.get_latest_catalog_sync_run()
    should_hydrate_latest = (
        progress.get("state") == "idle"
        or (
            progress.get("state") in ("ready", "failed")
            and progress.get("run_id") in (None, latest.get("id") if latest else None)
        )
    )
    if should_hydrate_latest and latest and latest.get("status") in ("ready", "failed"):
        hydrated = {
            "state": latest["status"],
            "phase": "Catalog ready for review" if latest["status"] == "ready" else "Automatic work needs attention",
            "provider_name": latest.get("provider_name"),
            "run_id": latest.get("id"),
            "queued_at": latest.get("queued_at"),
            "import_started_at": latest.get("import_started_at"),
            "import_finished_at": latest.get("import_finished_at"),
            "reconciliation_started_at": latest.get("reconciliation_started_at"),
            "reconciliation_finished_at": latest.get("reconciliation_finished_at"),
            "enrichment_started_at": latest.get("enrichment_started_at"),
            "enrichment_finished_at": latest.get("enrichment_finished_at"),
            "ready_at": latest.get("ready_at"),
            "started_at": latest.get("import_started_at") or latest.get("queued_at"),
            "finished_at": latest.get("ready_at"),
            "error": latest.get("error"),
        }
        progress.update(hydrated)
        # Keep the current header authoritative even if the user deletes the
        # audit row that supplied this hydration.  A later import replaces it;
        # deleting history must not erase the live status of the last run.
        _CATALOG_WORKFLOW_PROGRESS.update(hydrated)
    for key in (
        "queued_at", "import_started_at", "import_finished_at",
        "reconciliation_started_at", "reconciliation_finished_at",
        "enrichment_started_at", "enrichment_finished_at", "ready_at",
        "started_at", "finished_at",
    ):
        if progress.get(key) is not None:
            try:
                progress[key] = float(progress[key])
            except (TypeError, ValueError):
                progress[key] = None
    return progress


def _start_catalog_workflow(provider_name: str | None, phase: str, *, queued: bool = False, run_id: int | None = None) -> None:
    now = time.time()
    _CATALOG_WORKFLOW_PROGRESS.update({
        "state": "queued" if queued else "running",
        "phase": phase,
        "provider_name": provider_name,
        "run_id": run_id,
        "queued_at": now,
        "import_started_at": None,
        "import_finished_at": None,
        "reconciliation_started_at": None,
        "reconciliation_finished_at": None,
        "enrichment_started_at": None,
        "enrichment_finished_at": None,
        "ready_at": None,
        "started_at": None if queued else now,
        "finished_at": None,
        "error": None,
    })


# KNM: 2026-10-03 per-run phase stamps. _CATALOG_WORKFLOW_PROGRESS is the
# single live header, and a newer import replaces its run_id while an older
# run's enrichment task is still going; stamps must follow the run, not the header.
_RUN_PHASE_STAMPS: dict[int, dict] = {}


def _workflow_stamps(run_id: int | None) -> tuple[int | None, dict, bool]:
    """Return (target run, its stamp dict, whether it owns the live header)."""
    current = _CATALOG_WORKFLOW_PROGRESS.get("run_id")
    target = run_id or current
    if target is None or target == current:
        return target, _CATALOG_WORKFLOW_PROGRESS, True
    return target, _RUN_PHASE_STAMPS.setdefault(target, {}), False


def _set_catalog_workflow_phase(phase: str, *, run_id: int | None = None) -> None:
    target, stamps, owns_header = _workflow_stamps(run_id)
    if owns_header:
        # A periodic reconciliation can start without a user-initiated provider
        # import. It still deserves an honest progress state, rather than an
        # unexplained idle header while it uses the worker pool.
        if _CATALOG_WORKFLOW_PROGRESS["started_at"] is None:
            _CATALOG_WORKFLOW_PROGRESS["started_at"] = time.time()
        _CATALOG_WORKFLOW_PROGRESS.update({"state": "running", "phase": phase, "finished_at": None, "error": None})
    now = str(time.time())
    # KNM: 2026-10-03 review starts after enrichment; stamping it at queue time
    # ("Preparing automatic catalog review") made it wrap the enrichment phase.
    if phase == "Preparing catalog review" and not stamps.get("reconciliation_started_at"):
        stamps["reconciliation_started_at"] = time.time()
        if target:
            vod_db.update_catalog_sync_run(target, reconciliation_started_at=now)
    elif phase == "Resolving known TMDB identities" and not stamps.get("enrichment_started_at"):
        stamps["enrichment_started_at"] = time.time()
        if target:
            vod_db.update_catalog_sync_run(target, enrichment_started_at=now)


def mark_catalog_workflow_ready(*, run_id: int | None = None) -> None:
    target, stamps, owns_header = _workflow_stamps(run_id)
    ready_at = time.time()
    if stamps.get("reconciliation_started_at") and not stamps.get("reconciliation_finished_at"):
        stamps["reconciliation_finished_at"] = ready_at
    if owns_header:
        if _CATALOG_WORKFLOW_PROGRESS["started_at"] is None:
            _CATALOG_WORKFLOW_PROGRESS["started_at"] = ready_at
        _CATALOG_WORKFLOW_PROGRESS.update({
            "state": "ready", "phase": "Catalog ready for review",
            "finished_at": ready_at, "ready_at": ready_at, "error": None,
        })
    if target:
        fields = {"ready_at": str(ready_at), "status": "ready"}
        if stamps.get("reconciliation_finished_at"):
            fields["reconciliation_finished_at"] = str(stamps["reconciliation_finished_at"])
        if stamps.get("enrichment_finished_at"):
            fields["enrichment_finished_at"] = str(stamps["enrichment_finished_at"])
        vod_db.update_catalog_sync_run(target, **fields)
    if target is not None:
        _RUN_PHASE_STAMPS.pop(target, None)


def _mark_catalog_workflow_failed(error: str, *, run_id: int | None = None) -> None:
    target, _, owns_header = _workflow_stamps(run_id)
    if owns_header:
        _CATALOG_WORKFLOW_PROGRESS.update({
            "state": "failed", "phase": "Automatic work needs attention",
            "finished_at": time.time(), "error": error,
        })
    if target:
        vod_db.update_catalog_sync_run(target, status="failed", error=error, ready_at=str(time.time()))
        _RUN_PHASE_STAMPS.pop(target, None)


def _catalog_sync_summary(result: dict) -> dict:
    """Keep report summaries compact while retaining every useful counter."""
    excluded = {"changed_movie_ids", "changed_series_ids", "created_movie_ids", "created_series_ids"}
    return {
        key: value for key, value in result.items()
        if key not in excluded and isinstance(value, (str, int, float, bool))
    }


async def _append_catalog_sync_events(run_id: int | None, events: list[dict], summary_updates: dict | None = None) -> None:
    if not run_id:
        return
    if events:
        await asyncio.to_thread(vod_db.record_catalog_sync_events, run_id, extra_events=events)
    if summary_updates:
        report = await asyncio.to_thread(vod_db.get_catalog_sync_run, run_id)
        summary = dict(report.get("summary") or {}) if report else {}
        summary.update(summary_updates)
        await asyncio.to_thread(
            vod_db.update_catalog_sync_run,
            run_id,
            summary_json=json.dumps(summary, separators=(",", ":")),
        )


def mark_import_queued(provider_id: int, provider_name: str, queue_position: int) -> None:
    """Expose a queued manual import before its worker starts it."""
    _IMPORT_PROGRESS.update({
        "running": False, "queued": True, "queue_position": queue_position,
        "provider_id": provider_id, "provider_name": provider_name,
        "started_at": None, "finished_at": None, "error": None,
    })
    _start_catalog_workflow(provider_name, "Waiting to import catalog", queued=True)


def mark_import_running(provider_id: int, provider_name: str) -> int:
    """Expose a non-XC manual import while its provider adapter is running.

    XC imports update this state inside import_provider_catalog.  Plex, Emby,
    Jellyfin, and DVR imports use different adapters, so the manual-import
    worker marks their common lifecycle here instead of leaving the sidebar
    permanently on "queued".
    """
    _IMPORT_PROGRESS.update({
        "running": True, "queued": False, "queue_position": None,
        "provider_id": provider_id, "provider_name": provider_name,
        "started_at": time.time(), "finished_at": None, "error": None,
    })
    run_id = vod_db.create_catalog_sync_run(provider_id, provider_name)
    _start_catalog_workflow(provider_name, "Importing provider catalog", run_id=run_id)
    now = str(time.time())
    _CATALOG_WORKFLOW_PROGRESS["import_started_at"] = time.time()
    vod_db.update_catalog_sync_run(run_id, import_started_at=now)
    return run_id


def merge_changed_ids(acc: set[int] | None, result: dict, key: str) -> set[int] | None:
    # KNM: 2026-10-04 -- Plex/Emby/library importers don't report changed ids;
    # None (unscoped) then wins, or post-import enrichment would be scoped to an
    # empty set and never queue their new items (upstream PR #35 review).
    if acc is None or key not in result:
        return None
    acc.update(result[key])
    return acc


def import_error_detail(exc: BaseException) -> str:
    # KNM: 2026-10-04 -- failed imports stored only the class name; keep the
    # message, redacted (httpx errors embed the provider login in the URL).
    return _redact_upstream_url(str(exc)) or type(exc).__name__


def mark_import_finished(
    provider_id: int, error: str | None = None, *, run_id: int | None = None, result: dict | None = None,
) -> None:
    """Finish a non-XC import without overwriting a newer job's state."""
    if _IMPORT_PROGRESS.get("provider_id") == provider_id:
        _IMPORT_PROGRESS.update({
            "running": False, "queued": False, "queue_position": None,
            "finished_at": time.time(), "error": error,
        })
    elif run_id is None:
        return
    run_id = run_id or _CATALOG_WORKFLOW_PROGRESS.get("run_id")
    if not run_id:
        return
    vod_db.update_catalog_sync_run(run_id, import_finished_at=str(time.time()))
    if error:
        _mark_catalog_workflow_failed(error, run_id=run_id)
        return
    # KNM: 2026-10-04 -- this used to leave the run at "running" on success;
    # with no catalog change nothing ever closed it.
    if result is not None:
        summary = _catalog_sync_summary(result)
        vod_db.record_catalog_sync_events(
            run_id,
            movie_ids=result.get("changed_movie_ids", []),
            series_ids=result.get("changed_series_ids", []),
            created_movie_ids=result.get("created_movie_ids", []),
            created_series_ids=result.get("created_series_ids", []),
            summary=summary,
        )
        vod_db.update_catalog_sync_run(run_id, summary_json=json.dumps(summary, separators=(",", ":")))
    mark_catalog_workflow_ready(run_id=run_id)


async def run_tracked_import(provider_id: int, importer) -> dict:
    """Run a Plex/Emby/Jellyfin/library import with a Sync History row.

    KNM: 2026-10-04 -- only XC imports and manual clicks wrote one, so the
    scheduled refresher and Apply rules left these providers out of history.
    """
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    run_id = mark_import_running(provider_id, provider.get("name") if provider else f"provider {provider_id}")
    try:
        result = await importer(provider_id)
    except Exception as exc:
        mark_import_finished(provider_id, import_error_detail(exc), run_id=run_id)
        raise
    mark_import_finished(provider_id, run_id=run_id, result=result)
    result["catalog_sync_run_id"] = run_id
    return result


def get_process_cpu_percent() -> float | None:
    """Best-effort CPU use for this container's Python process.

    Linux exposes process CPU time without an extra dependency.  The first
    sample establishes a baseline; later status polls return a one-core
    percentage, which can exceed 100 when worker threads are busy.
    """
    global _CPU_SAMPLE
    try:
        fields = open("/proc/self/stat", encoding="utf-8").read().split()
        cpu_seconds = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
        now = time.monotonic()
        previous = _CPU_SAMPLE
        _CPU_SAMPLE = (now, cpu_seconds)
        if previous is None or now <= previous[0]:
            return None
        return round(100 * (cpu_seconds - previous[1]) / (now - previous[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


async def import_provider_catalog(provider_id: int, *, schedule_enrichment: bool = True) -> dict:
    """Run one XC catalog import and expose its lifecycle to the UI."""
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    provider_name = provider.get("name") if provider else f"provider {provider_id}"
    run_id = await asyncio.to_thread(vod_db.create_catalog_sync_run, provider_id, provider_name)
    # SQLite has a single writer and catalog preparation itself is expensive.
    # This lock makes periodic and manual XC imports wait their turn rather
    # than competing for the writer and starving normal API reads.
    async with _XC_IMPORT_LOCK:
        _start_catalog_workflow(provider_name, "Importing provider catalog", run_id=run_id)
        _CATALOG_WORKFLOW_PROGRESS["import_started_at"] = time.time()
        await asyncio.to_thread(vod_db.update_catalog_sync_run, run_id, import_started_at=str(time.time()))
        _IMPORT_PROGRESS.update({
            "running": True, "queued": False, "queue_position": None,
            "provider_id": provider_id,
            "provider_name": provider.get("name") if provider else f"provider {provider_id}",
            "started_at": time.time(), "finished_at": None, "error": None,
        })
        try:
            result = await _import_provider_catalog_impl(provider_id)
        except Exception as exc:
            detail = import_error_detail(exc)
            _IMPORT_PROGRESS.update({"running": False, "finished_at": time.time(), "error": detail})
            await asyncio.to_thread(vod_db.update_catalog_sync_run, run_id, status="failed", error=detail)
            _mark_catalog_workflow_failed(detail)
            raise
        _IMPORT_PROGRESS.update({"running": False, "finished_at": time.time()})
        _CATALOG_WORKFLOW_PROGRESS["import_finished_at"] = time.time()
        await asyncio.to_thread(vod_db.update_catalog_sync_run, run_id, import_finished_at=str(time.time()))
    result["catalog_sync_run_id"] = run_id
    await asyncio.to_thread(
        vod_db.record_catalog_sync_events, run_id,
        movie_ids=result.get("changed_movie_ids", []),
        series_ids=result.get("changed_series_ids", []),
        created_movie_ids=result.get("created_movie_ids", []),
        created_series_ids=result.get("created_series_ids", []),
        summary=_catalog_sync_summary(result),
    )
    await asyncio.to_thread(
        vod_db.update_catalog_sync_run,
        run_id,
        summary_json=json.dumps(_catalog_sync_summary(result), separators=(",", ":")),
    )
    if schedule_enrichment and result["catalog_changed"]:
        result["post_import_enrichment_queued"] = _queue_catalog_enrichment_for_changes(
            changed_movie_ids=set(result.get("changed_movie_ids", [])),
            changed_series_ids=set(result.get("changed_series_ids", [])),
            workflow_run_id=run_id,
        )
        if not result["post_import_enrichment_queued"]:
            # Another provider's enrichment task already owns the shared
            # worker. This import is complete and must not remain "running"
            # until that unrelated task finishes. Its pending TMDB work is
            # still picked up by the normal scheduled pass.
            await asyncio.to_thread(
                vod_db.update_catalog_sync_run,
                run_id, status="ready", ready_at=str(time.time()),
            )
    else:
        # Nothing changed, so there is no enrichment/reconciliation phase to
        # await. Keep the handoff truthful instead of stranding it at the
        # completed provider-import phase.
        # KNM: 2026-10-04 -- also when the caller defers enrichment (scheduled
        # refresher, manual queue): nothing else closes this run, so it sat
        # at "running" in Sync History forever.
        mark_catalog_workflow_ready(run_id=run_id)
        await asyncio.to_thread(vod_db.update_catalog_sync_run, run_id, status="ready", ready_at=str(time.time()))
    return result


async def _import_provider_catalog_impl(provider_id: int) -> dict:
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    client = XCProviderClient(provider)

    exclude_categories = provider.get("import_exclude_categories") or []
    # KNM: 2026-10-04 (upstream #39 review) -- saved list only. exclude_categories
    # gains newly discovered categories below (archive_new_categories); purging
    # with that list deleted their just-archived content, which then came back
    # active on the next import.
    saved_exclude_categories = list(exclude_categories)
    exclude_uncategorized = bool(provider.get("import_exclude_uncategorized"))

    # Stripped for the same reason vod_routes.get_provider_available_categories
    # strips (GH#4 reopened): provider_exclude_categories is saved trimmed
    # (vod_db.set_provider_import_exclude_categories), so an untrimmed name
    # here would never match its own saved exclusion below -- a category
    # with stray whitespace in its provider-reported name could never
    # actually be excluded, no matter how many times it was re-selected.
    categories = await client.get_vod_categories()
    category_names = {str(c["category_id"]): (c.get("category_name") or "").strip() for c in categories}

    series_categories = await client.get_series_categories()
    series_category_names = {str(c["category_id"]): (c.get("category_name") or "").strip() for c in series_categories}

    # GH issue #5: archive a category the moment it's first seen on this
    # provider, same as Dispatcharr's own "auto-archive newly discovered VOD
    # provider categories" behavior, instead of it landing in the library
    # fully visible until an admin notices and hand-adds it to
    # import_exclude_categories. known_import_categories is always
    # refreshed below regardless of the setting, so turning this on later
    # never retroactively archives the whole existing category list.
    seen_category_names = {n for n in category_names.values() if n} | {n for n in series_category_names.values() if n}
    if provider.get("archive_new_categories"):
        known_categories = set(provider.get("known_import_categories") or [])
        new_categories = seen_category_names - known_categories
        if new_categories:
            exclude_categories = list(exclude_categories) + list(new_categories)
            logger.info("[vod_importer] provider=%s auto-archiving %d newly discovered categor(y/ies): %s",
                        provider["name"], len(new_categories), ", ".join(sorted(new_categories)))
    await asyncio.to_thread(
        vod_db.set_provider_known_import_categories, provider_id,
        sorted(seen_category_names | set(provider.get("known_import_categories") or [])),
    )

    # Tried running these two concurrently via asyncio.gather (2026-09-06,
    # CRX live test): it fetches both bulk lists in parallel fine, but
    # bulk_import_movies/series each run in their own asyncio.to_thread
    # worker thread and both hammer the same vod_db._WRITE_LOCK -- a plain
    # threading.RLock with no fairness guarantee. Confirmed live: series'
    # thread kept winning the lock and starved movies out completely for
    # 25+ minutes straight on a real ~240k-item catalog, worse than just
    # running them sequentially (which never contends with itself at all).
    # Reverted to sequential; the real fix would be giving movies and
    # series their own separate locks (they touch entirely different
    # tables) rather than trying to make one shared lock fair -- not worth
    # the added complexity unless this import path is ever shown to be a
    # real bottleneck for someone.
    movie_result, streams_total, seen_movie_stream_ids = await _import_movies_for_provider(
        client, provider, provider_id, category_names, exclude_categories, exclude_uncategorized,
    )
    series_result, series_total, seen_series_ids = await _import_series_for_provider(
        client, provider, provider_id, series_category_names, exclude_categories, exclude_uncategorized,
    )

    await asyncio.to_thread(vod_db.set_provider_import_totals, provider_id, streams_total, series_total)
    changed_movie_ids = set(movie_result.pop("changed_movie_ids", []))
    changed_series_ids = set(series_result.pop("changed_series_ids", []))
    created_movie_ids = set(movie_result.get("created_movie_ids", []))
    created_series_ids = set(series_result.get("created_series_ids", []))

    # Both list calls completed successfully, so these are authoritative full
    # catalog snapshots.  Remove only this provider's source rows that are no
    # longer advertised; canonical records survive whenever another provider
    # still has a source.  Do this before post-import enrichment so stale
    # sources cannot be selected as fallback work.
    reconcile_result = await asyncio.to_thread(
        vod_db.reconcile_provider_catalog_sources,
        provider_id,
        seen_movie_stream_ids=seen_movie_stream_ids,
        seen_series_ids=seen_series_ids,
        include_affected_ids=True,
    )
    changed_movie_ids.update(reconcile_result.pop("affected_movie_ids", []))
    changed_series_ids.update(reconcile_result.pop("affected_series_ids", []))
    reconcile_summary = dict(reconcile_result)
    if any(reconcile_result.values()):
        logger.info(
            "[vod_importer] provider=%s reconciled %d stale movie source(s), %d stale series source(s), %d stale episode source(s)",
            provider["name"],
            reconcile_result["movie_sources_removed"],
            reconcile_result["series_sources_removed"],
            reconcile_result["episode_sources_removed"],
        )

    # KNM: added 2026-10-03 -- reconcile above keeps sources the provider
    # still lists, including ones in categories excluded after they were
    # imported. Remove those now (see vod_db.purge_excluded_category_sources).
    category_purge = await asyncio.to_thread(
        vod_db.purge_excluded_category_sources, provider_id, saved_exclude_categories, exclude_uncategorized,
    )
    changed_movie_ids.update(category_purge.pop("affected_movie_ids"))
    changed_series_ids.update(category_purge.pop("affected_series_ids"))
    if category_purge["movie_sources_removed"] or category_purge["series_sources_removed"]:
        logger.warning(
            "[vod_importer] provider=%s removed %d movie/%d series source(s) in excluded categories; "
            "deleted %d movie(s)/%d series (e.g. %s). Preview with POST /providers/%s/purge-excluded-content/",
            provider["name"], category_purge["movie_sources_removed"], category_purge["series_sources_removed"],
            category_purge["movies_deleted"], category_purge["series_deleted"],
            ", ".join((category_purge["sample_movies"] + category_purge["sample_series"])[:5]) or "none deleted",
            provider_id,
        )

    catalog_changed = bool(changed_movie_ids or changed_series_ids or any(reconcile_result.values()))

    # Companion cleanup to the skip-at-import filtering above: content that
    # was imported-then-archived under the old behavior (before this
    # provider's exclusion rules were enforced at import time) is purged
    # now that a fresh scan has run, matching the new "never stored" model
    # (see vod_db.purge_excluded_archived_content).
    #
    # 2026-09-11: this used to be conditional on exclude_categories,
    # exclude_uncategorized, or a non-empty lang["exclude_prefixes"]/
    # lang["exclude_non_latin"] -- skipped entirely when a provider had no
    # exclusion rules configured, since an empty exclude_prefixes list could
    # never match anything. Now that the language gate is enabled_languages
    # based instead, there's no "empty means inactive" case: config.
    # get_enabled_languages() always has at least a default (["EN", "ES"]),
    # so any provider's catalog can always contain a language outside that
    # set. The purge scan now always runs (its own per-row work is cheap
    # when nothing currently matches any active rule).
    purge_summary = {"movies_deleted": 0, "series_deleted": 0}
    if catalog_changed:
        lang = _current_lang_settings()
        purge_result = await asyncio.to_thread(
            vod_db.purge_excluded_archived_content,
            {provider_id: (exclude_categories, exclude_uncategorized)}, lang,
        )
        purge_summary = dict(purge_result)
        if purge_result["movies_deleted"] or purge_result["series_deleted"]:
            logger.info("[vod_importer] provider=%s purged %d movie(s)/%d series matching current exclusion rules",
                        provider["name"], purge_result["movies_deleted"], purge_result["series_deleted"])

    # KNM: added 2026-09-17 -- source-first series matching prevents new
    # shadows; this catches the pre-fix rows after a successful full refresh.
    # It deletes only records with neither a series source nor any playable
    # episode source, never a newly imported series merely awaiting details.
    orphan_result = await asyncio.to_thread(vod_db.purge_orphans)
    if orphan_result["series_deleted"] or orphan_result["movies_deleted"] or orphan_result["episodes_deleted"]:
        logger.info(
            "[vod_importer] provider=%s purged %d source-less series, %d movie(s), %d episode(s)",
            provider["name"], orphan_result["series_deleted"], orphan_result["movies_deleted"], orphan_result["episodes_deleted"],
        )

    # Both list calls above completed successfully, so these are
    # authoritative full-catalog snapshots -- safe to remove only this
    # provider's source rows that are no longer advertised (canonical
    # movies/series survive whenever another provider still has a source).
    # Done before post-import enrichment so a stale source can't be picked
    # as fallback work. If either call above had raised, we'd never reach
    # here at all -- a provider outage (failed fetch) can't masquerade as
    # "every title got removed".
    reconcile_result = await asyncio.to_thread(
        vod_db.reconcile_provider_catalog_sources,
        provider_id,
        seen_movie_stream_ids=seen_movie_stream_ids,
        seen_series_ids=seen_series_ids,
    )
    if any(reconcile_result.values()):
        logger.info(
            "[vod_importer] provider=%s reconciled %d stale movie source(s), %d stale series source(s), %d stale episode source(s)",
            provider["name"],
            reconcile_result["movie_sources_removed"],
            reconcile_result["series_sources_removed"],
            reconcile_result["episode_sources_removed"],
        )

    if provider.get("auto_create_categories"):
        try:
            created = await asyncio.to_thread(
                _auto_create_categories_from_provider,
                set(category_names.values()), "movie", exclude_categories,
            ) + await asyncio.to_thread(
                _auto_create_categories_from_provider,
                set(series_category_names.values()), "series", exclude_categories,
            )
            if created:
                logger.info("[vod_importer] provider=%s auto-created %d categor(y/ies) from its own category list", provider["name"], created)
        except Exception as exc:
            logger.warning("[vod_importer] provider=%s auto-create-categories failed: %s", provider["name"], exc)

    # Catch-up companion to the auto-merge language gate (see
    # vod_db.archive_disabled_language_content's docstring) -- runs on every
    # import so a legacy disabled-language backlog (or content that only just
    # lost its only enabled-language source, e.g. after narrowing Enabled
    # Playback Languages) gets flagged for review without a separate manual
    # step. Not scoped to this provider_id -- it evaluates every movie/series
    # row's source languages regardless of which provider(s) they came from.
    # KNM: 2026-10-04 -- the 10-03 upstream merge left a second, catalog_changed-gated
    # scoped copy of this sweep plus a second purge_orphans in this function; a no-op
    # sync after narrowing Enabled Playback Languages must still sweep, so only this one stays.
    archive_result = await asyncio.to_thread(vod_db.archive_disabled_language_content)
    if archive_result["movies_archived"] or archive_result["series_archived"]:
        logger.info(
            "[vod_importer] archived %d movie(s)/%d series with no source in an enabled language",
            archive_result["movies_archived"], archive_result["series_archived"],
        )
    if archive_result["movies_unarchived"] or archive_result["series_unarchived"]:
        logger.info(
            "[vod_importer] un-archived %d movie(s)/%d series after a previously-disabled language was re-enabled",
            archive_result["movies_unarchived"], archive_result["series_unarchived"],
        )

    # Companion cleanup to _should_auto_archive: deletes rows that are
    # currently auto-archived (review_excluded=1, review_excluded_manual=0)
    # but ALSO still match a currently-active provider category/language
    # exclusion rule -- e.g. a category a provider used to expose got added
    # to that provider's import-exclude-categories list after the content
    # was already imported+archived under the old rule set. Not scoped to
    # this provider_id -- evaluates every archived row against every
    # currently-configured provider's exclusion rules, same as
    # archive_disabled_language_content above. Ported from knmplace's fork;
    # see vod_db.purge_excluded_archived_content's docstring for why this
    # still applies even though we haven't ported his bigger "skip at
    # import" behavior change.
    provider_exclusions = {
        p["id"]: (p.get("import_exclude_categories") or [], bool(p.get("import_exclude_uncategorized")))
        for p in vod_db.list_providers()
        if p.get("import_exclude_categories") or p.get("import_exclude_uncategorized")
    }
    if provider_exclusions:
        purge_lang = {
            "enabled_languages": config.get_enabled_languages(),
            "exclude_non_latin": config.get_import_language_exclusion()["exclude_non_latin"],
        }
        purge_result = await asyncio.to_thread(
            vod_db.purge_excluded_archived_content, provider_exclusions, purge_lang,
        )
        if purge_result["movies_deleted"] or purge_result["series_deleted"]:
            logger.info(
                "[vod_importer] purged %d movie(s)/%d series matching an active import-exclusion rule",
                purge_result["movies_deleted"], purge_result["series_deleted"],
            )

    return {
        "provider": provider["name"],
        "movie_categories": len(categories),
        "series_categories": len(series_categories),
        **_merge_import_counts(movie_result, series_result),
        **reconcile_summary,
        **purge_summary,
        "excluded_category_purge": category_purge,
        **archive_result,
        **{f"orphan_{key}": value for key, value in orphan_result.items() if key.endswith("deleted")},
        "catalog_changed": catalog_changed,
        "changed_movie_ids": list(changed_movie_ids),
        "changed_series_ids": list(changed_series_ids),
        "created_movie_ids": list(created_movie_ids),
        "created_series_ids": list(created_series_ids),
        "post_import_enrichment_queued": False,
    }


def _auto_create_categories_from_provider(category_names: set[str | None], content_type: str, exclude_categories: list[str]) -> int:
    """User request (Discord, KNM [BEES]/sjsteve, 2026-07-29): "the option at
    the provider connection to enable recreating the categories on import."
    Opt-in per provider (providers.auto_create_categories, default off).

    One VOD Manager smart category per distinct provider category name seen
    this import, matched via the provider_category rule field (see
    vod_db._SMART_CATEGORY_FIELDS) rather than scoped to just this one
    provider -- if two providers both have a category literally named
    "Comedy", they share the one VOD Manager "Comedy" category rather than
    creating "Comedy" and "Comedy (2)". upsert_category is itself an upsert
    keyed on (name, content_type), so re-running this on every import is a
    correct no-op for a category that already exists -- this only ever
    creates what's missing, never duplicates or overwrites an admin's own
    edits to an already-existing category with the same name.

    Evaluated immediately (not left for the next scheduled sweep) so the
    category shows real content right after the import that created it,
    same "auto-run on create" principle as vod_routes.upsert_category."""
    import json
    created = 0
    for name in category_names:
        name = (name or "").strip()
        if not name or name in exclude_categories:
            continue
        # Real bug caught live 2026-07-29, before this ever ran against real
        # data: upsert_category is a plain upsert that OVERWRITES rule_json
        # unconditionally on an existing row -- calling it here for a
        # category name that already exists (e.g. a user's own hand-built
        # "Music" category matched on genre) would have silently clobbered
        # their rule with this provider_category one. Skip entirely for an
        # existing category instead -- this only ever creates what's
        # missing, exactly as the module docstring always claimed but the
        # code didn't actually do until this fix.
        if vod_db.get_category_by_name(name, content_type):
            continue
        # Real race condition caught live 2026-07-29: the get_category_by_name
        # check above and upsert_category's own INSERT aren't atomic --
        # nothing here is unusual about that in isolation, except this
        # feature makes the SAME category name being auto-created from TWO
        # PROVIDERS AT ONCE a routine occurrence (e.g. the periodic catalog
        # refresher importing several providers back to back, more than one
        # with a "Comedy" category, or just two providers happening to
        # finish their own import right on top of each other) -- ordinary
        # sqlite3.IntegrityError on categories.name's UNIQUE constraint when
        # that happens, not exceptional. Catching it here and falling
        # through to re-fetch + evaluate (rather than letting it propagate
        # and abort every category still left in this loop) is what makes
        # this correct under real concurrent imports, not just a single one.
        try:
            category_id = vod_db.upsert_category(
                name, content_type, is_smart=True,
                rule_json=json.dumps({"match": "all", "conditions": [{"field": "provider_category", "op": "equals", "value": name}]}),
            )
            created += 1
        except sqlite3.IntegrityError:
            existing = vod_db.get_category_by_name(name, content_type)
            if not existing:
                # Lost the race in a way that isn't "someone else just made
                # it" (e.g. a genuinely different constraint) -- skip this
                # one name rather than crash the rest of the batch.
                logger.warning("[vod_importer] auto-create-categories: could not create or find %r (%s)", name, content_type)
                continue
            category_id = existing["id"]
        except sqlite3.OperationalError as exc:
            # Same reasoning as bulk_import_movies's own lock-retry handling
            # -- real concurrent-import contention observed live 2026-07-29
            # (heavy "database is locked" activity during simultaneous
            # imports). One category failing to create this pass isn't fatal
            # to the rest of the batch or the import itself; it'll pick up
            # on the next import/sweep.
            logger.warning("[vod_importer] auto-create-categories: %r (%s) hit %s, will retry next pass", name, content_type, exc)
            continue
        try:
            vod_db.evaluate_smart_category(category_id)
        except Exception as exc:
            logger.warning("[vod_importer] auto-created category=%s evaluate failed: %s", category_id, exc)
    return created


def _apply_field_rules(content_type: str, fields: dict) -> dict:
    """Applies each field's active metadata_rules (regex find/replace) to the
    freshly-fetched enrichment value before it's persisted."""
    result = {}
    for field, value in fields.items():
        rules = vod_db.get_active_rules_for_field(content_type, field)
        result[field] = vod_db.apply_rules_to_value(value, rules)
    return result


async def _tmdb_movie_enrichment_payload(movie_id: int, tmdb_id: str, *, track_not_found: bool = False) -> dict | None:
    """Fetch TMDB fields for a known movie identity without provider fallback."""
    try:
        tmdb_detail = await tmdb_sync.get_movie_full_details(tmdb_id)
    except tmdb_sync.TmdbNotFoundError:
        if track_not_found:
            await asyncio.to_thread(vod_db.record_tmdb_lookup_failure, "movie", movie_id, tmdb_id)
        return None
    if not tmdb_detail:
        return None
    if track_not_found:
        await asyncio.to_thread(vod_db.clear_tmdb_lookup_failure, "movie", movie_id)
    name_fields = {}
    if tmdb_detail.get("name"):
        name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "movie", "name")
        name_fields["name"] = vod_db.apply_rules_to_value(tmdb_detail["name"], name_rules)
    return {
        "movie_id": movie_id,
        "fields": {
            **name_fields,
            **_apply_field_rules("movie", {
                "genre": tmdb_detail.get("genre"),
                "description": tmdb_detail.get("description"),
                "cast_list": tmdb_detail.get("cast_list"),
                "director": tmdb_detail.get("director"),
                "country": tmdb_detail.get("country"),
            }),
            "tmdb_id": tmdb_id,
            "poster_url": tmdb_detail.get("poster_url"),
            "duration_secs": tmdb_detail.get("duration_secs"),
            "rating": tmdb_detail.get("rating"),
            "release_date": tmdb_detail.get("release_date"),
            "content_rating": tmdb_detail.get("content_rating"),
        },
        "source_id": None,
        "bitrate": None,
    }


async def enrich_movie(
    movie_id: int, *, force: bool = False, skip_auto_merge: bool = False, skip_write: bool = False,
) -> bool | dict:
    """Fetch get_vod_info for this movie's best source and persist detail
    fields. Returns False without a network call if already fresh (unless
    force=True) — the on-demand-and-cache pattern from the module docstring.
    skip_auto_merge=True (used only by bulk_enrich_all's per-provider
    orchestration) defers the tmdb_id auto-merge to that
    caller's own end-of-phase sweep instead of running it inline here.

    skip_write=True (used only by _run_provider_movie_phase)
    returns the computed {"movie_id", "fields", "source_id", "bitrate"}
    payload instead of writing it via vod_db.set_movie_enrichment/
    set_movie_source_bitrate -- the caller collects these across many
    concurrently-enriching movies and writes them via one
    vod_db.apply_movie_enrichment_batch call per chunk, instead of each
    movie independently acquiring vod_db._WRITE_LOCK. A single/on-demand
    call (skip_write's default, False) keeps writing inline immediately --
    same as enrich_series's unchanged single-item path. Still returns True/
    False (not a payload) when nothing needs enriching or no fields resulted
    (e.g. Plex's TTL-bump path skip_write=True hits below), since there's
    nothing for the caller's batch writer to do in that case."""
    if not force and not await asyncio.to_thread(vod_db.movie_needs_enrichment, movie_id):
        return False

    sources = await asyncio.to_thread(vod_db.list_movie_sources, movie_id)
    if not sources:
        return False
    source = sources[0]
    provider = await asyncio.to_thread(vod_db.get_provider, source["provider_id"])
    if not provider:
        return False

    if provider.get("provider_type") == "plex":
        # Plex's library listing already hands back full detail at import
        # time (see plex_importer.py) — nothing more to lazily fetch here,
        # just refresh the TTL stamp so the scheduler leaves it alone.
        if skip_write:
            return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}
        await asyncio.to_thread(vod_db.set_movie_enrichment, movie_id)
        return True

    if provider.get("provider_type") in ("emby", "jellyfin"):
        # Catalog import already supplies the fields used by the app. Do not
        # make a provider People request just to fill optional metadata.
        return False

    # Imported catalog rows already contain provider metadata, and movie
    # stream IDs are captured during catalog import. Known identities are
    # resolved through TMDB; never request per-movie provider metadata.
    movie_row = await asyncio.to_thread(vod_db.get_movie, movie_id)
    existing_tmdb_id = movie_row.get("tmdb_id") if movie_row else None
    if existing_tmdb_id:
        payload = await _tmdb_movie_enrichment_payload(movie_id, existing_tmdb_id)
        if payload:
            if skip_write:
                return payload
            await asyncio.to_thread(vod_db.set_movie_enrichment, movie_id, **payload["fields"])
            if not skip_auto_merge:
                await asyncio.to_thread(vod_db.auto_merge_movie_by_tmdb, movie_id)
            return True
        # A failed TMDB lookup stays in the manual review path; it must not
        # fall through to a provider metadata request.
        return False

    return False

async def enrich_series(series_id: int, *, force: bool = False, skip_auto_merge: bool = False) -> dict:
    """Fetch get_series_info -- this is the only source of episodes (most
    real XC panels' bulk get_series list already carries full series detail,
    see bulk_import_series, but never episodes), so this call is
    load-bearing even for a series whose detail fields are already fresh
    from the last bulk import. series_needs_enrichment gates the overall
    "is it worth touching this series at all" decision on the primary
    provider's own last_modified where available, not a blind TTL -- see
    its docstring -- so this mostly only actually runs for a series that's
    genuinely new or has reported a change.

    True multi-provider failover (vod_manager series/episode failover work,
    2026-09-09): a series can now be recorded against several providers at
    once via series_sources (see bulk_import_series), mirroring how
    movie_sources already lets one movie carry several playable sources.
    This loops over every one of them -- not just the legacy
    import_provider_id "primary" -- calling get_series_info on each and
    writing episode_sources rows per provider per episode via the
    unchanged add_episode_source (its ON CONFLICT upsert already handles
    the same episode being reported by multiple providers correctly, that
    part needed no changes). Each source is skipped once its own
    last_seen_at is fresh (series_source_needs_enrichment's simple
    per-source TTL -- deliberately not a full per-source last_modified
    migration, by explicit scope decision, 2026-09-09, to keep this first
    cut small) unless force is set. A source whose get_series_info call
    fails gets consecutive_failures/last_failed_at bumped instead of being
    retried every single pass forever, but never blocks the other sources
    for this series from being tried.

    Returns {"fetched": bool, "reason": str | None} rather than a bare bool
    -- every False outcome used to look identical (nothing happened, no
    error), which meant a real problem (the provider this series was
    imported from got deleted since) was indistinguishable from "already up
    to date, nothing to do" from the caller's side. A caller like the
    year-review panel's "fetch episodes to preview" button needs to tell
    those apart to show something better than a spinner that just resets.
    fetched=True here means "at least one source was actually attempted",
    not "every source succeeded" -- individual per-source failures are
    tracked on their own series_sources rows, not surfaced through this
    return value, matching how a single provider's transient failure never
    used to abort the whole call either.

    Every vod_db call in here (and in enrich_movie above) is offloaded via
    asyncio.to_thread — these are plain synchronous sqlite3 calls, and
    calling them directly on the event loop thread means any lock
    contention (very real: bulk_enrich_all runs 8 of these concurrently
    against the same db file) freezes the ENTIRE process, including
    unrelated concurrent work like a video stream relay. That's what was
    causing playback to stall mid-stream even though the network path to
    the source was fine."""
    series = await asyncio.to_thread(vod_db.get_series, series_id)
    if not series:
        return {"fetched": False, "reason": "series not found"}

    sources = await asyncio.to_thread(vod_db.list_series_sources, series_id)
    if not sources:
        return {"fetched": False, "reason": "no source provider recorded for this series"}

    # Detail metadata (genre/description/tmdb_id/etc) is only ever written
    # from the FIRST source below that's actually XC and actually fetched --
    # every provider is describing the same series, so there's no benefit to
    # (and real risk of flip-flopping fields from) applying it more than
    # once per call. Plex sources never reach this point needing detail
    # (see the Plex branch inside the loop), so this only ever fires for the
    # first successfully-fetched XC source.
    detail_written = False
    any_fetched = False
    last_reason = "already up to date"

    for source in sources:
        outcome = await _enrich_one_series_source(series_id, series, source, force=force, episodes_only=True)
        if outcome["fetched"]:
            any_fetched = True
            if outcome["detail_written"]:
                detail_written = True
        else:
            last_reason = outcome["reason"]

    if not any_fetched:
        return {"fetched": False, "reason": last_reason}

    if detail_written:
        # Mirrors enrich_movie's auto_merge_movie_by_tmdb call sites (see
        # those for the full design doc) -- fires once per series, after all
        # of this series' providers have been processed, and only when a
        # tmdb_id could actually have been (re)confirmed this pass (detail_
        # written implies the TMDB-bearing provider's detail fetch
        # succeeded). Gated on the same duplicate_finder_auto_merge_tmdb
        # config flag as movies. skip_auto_merge=True (bulk_enrich_all only)
        # defers this to that caller's end-of-phase sweep instead.
        if not skip_auto_merge:
            await asyncio.to_thread(vod_db.auto_merge_series_by_tmdb, series_id)

    return {"fetched": True, "reason": None}


async def _enrich_one_series_source(
    series_id: int, series: dict, source: dict, *, force: bool = False,
    write_queue: "asyncio.Queue | None" = None, episodes_only: bool = True,
) -> dict:
    """Fetches and persists exactly ONE series_sources row's episodes/detail.
    Extracted from enrich_series's original single-source-at-a-time loop body
    (plan-doc follow-up "make a provider lane fetch only that provider's
    series source", 2026-09-14) so the bulk path (enrich_series_source_only,
    via _run_provider_series_phase) can call it for ONE known provider's
    source without looping every other provider's source the way
    enrich_series itself still does for its on-demand/UI callers.

    Returns {"fetched": bool, "reason": str | None, "detail_written": bool}."""
    if not force and not await asyncio.to_thread(vod_db.series_source_needs_enrichment, source):
        return {"fetched": False, "reason": "already up to date", "detail_written": False}

    provider = await asyncio.to_thread(vod_db.get_provider, source["provider_id"])
    if not provider:
        return {
            "fetched": False,
            "reason": "a provider this series was imported from no longer exists",
            "detail_written": False,
        }

    if provider.get("provider_type") == "plex":
        # Same reasoning as enrich_movie: Plex already gave us full
        # detail and every episode at import time (plex_importer.py) —
        # episodes aren't lazily discovered here the way XC's are.
        await asyncio.to_thread(
            vod_db.set_series_source_enrichment, series_id, source["provider_id"], source["provider_series_id"],
        )
        return {"fetched": True, "reason": None, "detail_written": False}

    if provider.get("provider_type") == "library":
        # Episodes came from the files themselves at import time; what's left
        # is TMDB's series detail and real episode titles (a folder has no
        # get_series_info to call -- and its base_url is a path, so the XC
        # client below must never be reached). Best-effort: a TMDB miss just
        # leaves placeholders and is retried on the next TTL pass.
        tmdb_id = series.get("tmdb_id")
        if tmdb_id:
            try:
                detail = await tmdb_sync.get_series_full_details(str(tmdb_id))
                if detail:
                    await asyncio.to_thread(vod_db.set_series_enrichment, series_id, **_apply_field_rules("series", {
                        k: detail.get(k) for k in ("genre", "description", "cast_list")
                    }), poster_url=detail.get("poster_url"), rating=detail.get("rating"),
                        release_date=detail.get("release_date"))
                eps = await tmdb_sync.get_series_episode_list_cached(str(tmdb_id))
                names = {(e["season_number"], e["episode_number"]): e["name"] for e in eps if e.get("name")}
                if names:
                    await asyncio.to_thread(vod_db.fill_placeholder_episode_names, series_id, names)
            except Exception:
                logger.warning("[_enrich_one_series_source] TMDB enrichment failed for library series_id=%s", series_id, exc_info=True)
        await asyncio.to_thread(
            vod_db.set_series_source_enrichment, series_id, source["provider_id"], source["provider_series_id"],
        )
        return {"fetched": True, "reason": None, "detail_written": False}

    client = XCProviderClient(provider)
    try:
        info = _as_dict(await client.get_series_info(str(source["provider_series_id"])))
    except Exception:
        # Best-effort bookkeeping: a 'database is locked' here must not mask
        # the provider's own failure.
        try:
            await asyncio.to_thread(
                vod_db.record_series_source_failure, series_id, source["provider_id"], source["provider_series_id"],
            )
        except Exception:
            logger.exception(
                "[_enrich_one_series_source] failed to record failure for series_id=%s provider_id=%s",
                series_id, source["provider_id"],
            )
        return {
            "fetched": False,
            "reason": f"get_series_info failed for provider {provider.get('name') or provider['id']}",
            "detail_written": False,
            "failed": True,
        }

    # XC embeds optional series metadata in the same response, but it is not
    # consumed here. Catalog import and TMDB are authoritative for metadata;
    # this provider request exists only to obtain episode stream IDs.
    detail_written = False

    if False and not episodes_only:
        # See enrich_movie's identical comment -- safe as of 2026-07-29's
        # bulk_import_series rewrite, which now matches primarily by
        # (import_provider_id, import_provider_series_id), not by
        # re-deriving identity from (name, year) every pass.
        name_fields = {}
        # TMDB owns the visible title once the canonical pass has resolved
        # this series. Provider text stays in series_sources.raw_name.
        if detail.get("name") and not series.get("tmdb_metadata_enriched_at"):
            name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
            name_fields["name"] = vod_db.apply_rules_to_value(detail["name"], name_rules)

        # This provider sends the series' TMDB id under "tmdb", not
        # "tmdb_id" (unlike its own movie endpoint, which does use
        # "tmdb_id") -- check both since key naming isn't consistent
        # even within one provider, let alone across others.
        series_tmdb_id = _clean_tmdb_id(detail.get("tmdb")) or _clean_tmdb_id(detail.get("tmdb_id"))

        # This is only for a TMDB id first exposed by provider detail during
        # this call. Known IDs are handled earlier by the canonical pass.
        content_rating = None
        if series_tmdb_id and not series.get("tmdb_metadata_enriched_at"):
            content_rating = await tmdb_sync.get_tv_content_rating(str(series_tmdb_id))

        await asyncio.to_thread(
            vod_db.set_series_enrichment,
            series_id,
            **name_fields,
            **_apply_field_rules("series", {
                "genre": detail.get("genre") or None,
                "description": detail.get("plot") or None,
                "cast_list": detail.get("cast") or None,
                "director": detail.get("director") or None,
                "country": detail.get("country") or None,
            }),
            tmdb_id=series_tmdb_id,
            poster_url=detail.get("cover") or None,
            rating=detail.get("rating") or None,
            release_date=detail.get("releasedate") or None,
            content_rating=content_rating,
            # Snapshot of what bulk_import_series last saw for this
            # series -- series_needs_enrichment compares the two on the
            # next pass to know whether this (expensive, per-series)
            # call is worth making again.
            episodes_synced_last_modified=series.get("provider_last_modified"),
        )
        detail_written = True

    # get_series_info's "episodes" field is documented as {season_key: [ep, ...]}
    # (standard XC shape), but at least one real provider returns a plain
    # list of per-season lists instead — [[ep,...], [ep,...]].
    # Each episode also carries its own "season" field regardless of shape, so
    # trust that over the dict key / list index, falling back to the latter
    # only if a provider omits it.
    episodes_raw = info.get("episodes") or {}
    season_groups = episodes_raw.items() if isinstance(episodes_raw, dict) else enumerate(episodes_raw)

    # Collected in-memory and written in one batched transaction (see
    # vod_db.enrich_series_episodes_batch) instead of the old per-episode
    # add_episode/add_episode_source/set_episode_source_bitrate calls -- each
    # of those independently took the process-wide write lock and did a full
    # fsync-backed commit, which made a 20-episode series up to 40 serialized
    # lock+fsync round-trips contended against every other concurrently-
    # enriching series (series enriched ~37x slower than movies,
    # root-caused to exactly this).
    episode_batch = []
    for season_key, episodes in season_groups:
        for ep in episodes:
            season_number = ep.get("season", season_key)
            episode_batch.append({
                "season_number": int(season_number),
                "episode_number": int(ep.get("episode_num", 0)),
                "name": ep.get("title") or f"Episode {ep.get('episode_num', '?')}",
                "description": (ep.get("info") or {}).get("plot") or None,
                "duration_secs": (ep.get("info") or {}).get("duration_secs") or None,
                "provider_stream_id": str(ep["id"]),
                "container_extension": ep.get("container_extension") or "mp4",
                "raw_name": ep.get("title") or None,
                # XC only reports category at the series level, not per-
                # episode -- bulk_import_series stamped it onto this
                # source's series_sources row for exactly this moment,
                # since episodes weren't known yet back at that earlier,
                # cheap bulk-list stage. Real bug found live 2026-07-29:
                # this was never threaded through at all, so provider_
                # category-based series matching (evaluate_smart_
                # category, auto-create-categories) had no data to work
                # with for any provider, ever.
                "provider_category_name": source.get("provider_category_name"),
                "bitrate": _coerce_int((ep.get("info") or {}).get("bitrate")),
            })

    if write_queue is not None:
        done = asyncio.Event()
        await write_queue.put({
            "kind": "series", "series_id": series_id, "provider_id": provider["id"],
            "episodes": episode_batch, "done": done,
        })
        await done.wait()
    else:
        await asyncio.to_thread(vod_db.enrich_series_episodes_batch, series_id, provider["id"], episode_batch)

    await asyncio.to_thread(
        vod_db.set_series_source_enrichment, series_id, source["provider_id"], source["provider_series_id"],
    )
    return {"fetched": True, "reason": None, "detail_written": detail_written}


async def enrich_series_source_only(
    series_id: int, provider_id: int, *, force: bool = False, skip_auto_merge: bool = False,
    write_queue: "asyncio.Queue | None" = None, source_id: int | None = None,
    episodes_only: bool = True,
) -> dict:
    """Provider-scoped counterpart to enrich_series (plan-doc follow-up "make
    a provider lane fetch only that provider's series source", 2026-09-14).

    enrich_series(series_id) loops EVERY series_sources row for a series,
    which is correct for its on-demand/UI callers (a "refresh this item"
    button has no single provider in mind) but wrong for
    _run_provider_series_phase's bulk lane: that lane already selects
    series_ids scoped to ONE provider (list_all_series_ids(provider_id=...)),
    so handing each id to enrich_series let Provider A's lane also fetch
    Provider B/C's source for the same series -- weakening the per-provider
    request/concurrency isolation bulk_enrich_all's whole per-provider-phase
    design exists to provide, and risking duplicate get_series_info calls
    before each source's own freshness stamp is written.

    Fetches and persists ONLY the one series_sources row matching
    provider_id, via the same per-source logic enrich_series uses (shared
    through _enrich_one_series_source) -- so the fetch/write behavior for
    that single source is unchanged, only the "which sources does this call
    touch" scope is narrowed."""
    series = await asyncio.to_thread(vod_db.get_series, series_id)
    if not series:
        return {"fetched": False, "reason": "series not found"}

    sources = await asyncio.to_thread(vod_db.list_series_sources, series_id)
    source = next(
        (s for s in sources if s["provider_id"] == provider_id and (source_id is None or s["id"] == source_id)),
        None,
    )
    if not source:
        return {"fetched": False, "reason": "no source recorded for this series from this provider"}

    outcome = await _enrich_one_series_source(
        series_id, series, source, force=force, write_queue=write_queue,
        episodes_only=episodes_only,
    )

    if outcome["fetched"] and outcome["detail_written"] and not skip_auto_merge:
        await asyncio.to_thread(vod_db.auto_merge_series_by_tmdb, series_id)

    return {"fetched": outcome["fetched"], "reason": outcome["reason"], "failed": outcome.get("failed", False)}


# ── Bulk enrichment ──────────────────────────────────────────────────────────
# On-demand-and-cache (above) only ever touches one item per click. Bulk mode
# walks the whole pool with bounded concurrency so it doesn't hammer a real
# provider's API — progress is tracked in-process (single-instance app, no
# need for anything heavier) and polled from the UI rather than blocking a
# single request for what can be a multi-minute run across a large pool.

_TMDB_ENRICH_PROGRESS: dict = {
    "running": False, "total": 0, "done": 0, "errors": 0,
    "started_at": None, "finished_at": None,
}
_POST_IMPORT_ENRICH_TASK: asyncio.Task | None = None
_BACKGROUND_TMDB_TASK: asyncio.Task | None = None

# The first pass is deliberately bounded.  The remaining eligible records are
# handled by the low-priority continuation so a large provider catalog does
# not hold the catalog handoff open for hours.
_TMDB_INITIAL_FRACTION = 0.30
_TMDB_INITIAL_MOVIE_CAP = 15_000
_TMDB_INITIAL_SERIES_CAP = 3_000
_TMDB_BACKGROUND_BATCH = 500
_TMDB_BACKGROUND_DELAY_SECONDS = 5


def get_tmdb_enrich_progress() -> dict:
    return dict(_TMDB_ENRICH_PROGRESS)


async def bulk_enrich_tmdb_movies(
    concurrency: int = 8, limit: int | None = None, item_ids: set[int] | None = None,
) -> list[dict]:
    """Resolve imported movie TMDB IDs without opening provider connections.

    The import list already supplies identity for many movies.  Keeping this
    as a distinct bounded worker makes that cheap, provider-free work visible
    and prevents it from competing with episode discovery or fallback detail
    calls.  Results are committed in batches to avoid SQLite write churn.
    """
    if _TMDB_ENRICH_PROGRESS["running"]:
        return []
    ids = await asyncio.to_thread(vod_db.list_movie_ids_pending_tmdb_enrichment, limit, item_ids)
    _TMDB_ENRICH_PROGRESS.update({
        "running": True, "total": len(ids), "done": 0, "errors": 0,
        "started_at": time.time(), "finished_at": None,
    })
    pending: list[dict] = []
    succeeded: list[int] = []
    queue: asyncio.Queue[int] = asyncio.Queue()
    for movie_id in ids:
        queue.put_nowait(movie_id)

    async def worker() -> None:
        while True:
            try:
                movie_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                movie = await asyncio.to_thread(vod_db.get_movie, movie_id)
                payload = await _tmdb_movie_enrichment_payload(movie_id, movie["tmdb_id"], track_not_found=True) if movie else None
                if payload:
                    pending.append(payload)
                    succeeded.append(movie_id)
                else:
                    _TMDB_ENRICH_PROGRESS["errors"] += 1
            except Exception:
                logger.exception("[vod_importer] TMDB enrichment failed for movie_id=%s", movie_id)
                _TMDB_ENRICH_PROGRESS["errors"] += 1
            finally:
                _TMDB_ENRICH_PROGRESS["done"] += 1

    try:
        await asyncio.gather(*(worker() for _ in range(min(max(1, concurrency), len(ids) or 1))))
        for offset in range(0, len(pending), _MOVIE_BATCH_CHUNK_SIZE):
            await asyncio.to_thread(vod_db.apply_movie_enrichment_batch, pending[offset:offset + _MOVIE_BATCH_CHUNK_SIZE])
        merge_events = []
        if succeeded:
            merge_events.extend(await asyncio.to_thread(vod_db.auto_merge_movies_by_tmdb_batch, succeeded) or [])
        # A card can arrive with an already-valid TMDB ID and therefore skip
        # this detail queue entirely. Reconcile only this phase's delta so it
        # does not scan the full collision set on every refresh.
        collision_ids = item_ids if item_ids is not None else set(ids)
        if collision_ids:
            merge_events.extend(await _run_scoped_collision_sweep(
                vod_db.auto_merge_movie_tmdb_collisions, set(collision_ids),
            ))
        return merge_events
    finally:
        _TMDB_ENRICH_PROGRESS["running"] = False
        _TMDB_ENRICH_PROGRESS["finished_at"] = time.time()


async def bulk_enrich_tmdb_series_metadata(
    concurrency: int = 8, limit: int | None = None, item_ids: set[int] | None = None,
) -> list[dict]:
    """Normalize known-TMDB series cards before provider episode discovery."""
    pending_series = await asyncio.to_thread(vod_db.list_series_pending_tmdb_metadata_enrichment, limit, item_ids)
    # A multilingual catalog can deliberately retain several canonical cards
    # for one real TMDB title. They must stay separate for language-aware
    # playback/merge rules, but TMDB's title/rating is identical, so fetch it
    # once and fan that immutable result back out to every card.
    series_by_tmdb_id: dict[str, list[dict]] = {}
    for series in pending_series:
        series_by_tmdb_id.setdefault(str(series["tmdb_id"]), []).append(series)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for tmdb_id in series_by_tmdb_id:
        queue.put_nowait(tmdb_id)
    resolved: list[dict] = []
    merged_ids: list[int] = []

    async def worker() -> None:
        while True:
            try:
                tmdb_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                try:
                    detail = await tmdb_sync.get_tv_full_details(tmdb_id)
                except tmdb_sync.TmdbNotFoundError:
                    for series in series_by_tmdb_id[tmdb_id]:
                        await asyncio.to_thread(vod_db.record_tmdb_lookup_failure, "series", series["id"], tmdb_id)
                    continue
                if not detail:
                    continue
                name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
                fields = {
                    "name": vod_db.apply_rules_to_value(detail["name"], name_rules),
                }
                # A valid TMDB detail response is authoritative for the
                # first-air year.  Without carrying it over, an imported card
                # can have a confirmed ID, title, artwork, and episode data
                # yet remain incorrectly trapped in Metadata Review forever.
                if detail.get("year") is not None:
                    fields["year"] = detail["year"]
                    fields["needs_year_review"] = 0
                # A missing US rating must not erase a useful provider rating.
                if detail.get("content_rating"):
                    fields["content_rating"] = detail["content_rating"]
                for series in series_by_tmdb_id[tmdb_id]:
                    resolved.append({
                        "series_id": series["id"],
                        "fields": fields,
                    })
                    merged_ids.append(series["id"])
                    await asyncio.to_thread(vod_db.clear_tmdb_lookup_failure, "series", series["id"])
            except Exception:
                logger.exception("[vod_importer] TMDB series metadata failed for tmdb_id=%s", tmdb_id)

    await asyncio.gather(*(worker() for _ in range(min(max(1, concurrency), len(series_by_tmdb_id) or 1))))
    for offset in range(0, len(resolved), _MOVIE_BATCH_CHUNK_SIZE):
        await asyncio.to_thread(vod_db.apply_series_tmdb_metadata_batch, resolved[offset:offset + _MOVIE_BATCH_CHUNK_SIZE])
    merge_events = []
    if merged_ids:
        # Same TMDB id remains insufficient to merge different-language cards.
        merge_events.extend(await asyncio.to_thread(vod_db.auto_merge_series_by_tmdb_batch, merged_ids) or [])
    # The item list above is deliberately restricted to cards pending TMDB
    # metadata.  Finish with a cheap DB-derived collision sweep so a card
    # created while a coalesced import/enrichment run was already active
    # cannot be stranded merely because it was absent from that item list.
    pending_series_ids = {item["id"] for item in pending_series}
    collision_ids = item_ids if item_ids is not None else pending_series_ids
    if collision_ids:
        merge_events.extend(await _run_scoped_collision_sweep(
            vod_db.auto_merge_series_tmdb_collisions, set(collision_ids),
        ))
    return merge_events


async def bulk_enrich_series_episodes(
    concurrency: int = 6, limit: int | None = None, series_ids: set[int] | None = None,
    request_spacing: float = 0, max_consecutive_failures: int | None = None,
    yield_to_import: bool = False,
) -> list[dict]:
    """Fetch provider detail only for pending series episode sources.

    TMDB remains authoritative for series metadata; the provider call exists
    only because XC series detail supplies episode listings and stream IDs.
    """
    if _ENRICH_PROGRESS["running"]:
        logger.info("[vod_importer] episode enrichment already running; skipping overlapping request")
        return []
    providers = [p for p in await asyncio.to_thread(vod_db.list_providers)
                 if p.get("is_active", True)]
    _reset_enrich_done_ids()
    pending_provider_ids = []
    pending_by_provider: dict[int, list[dict]] = {}
    pending_series_ids: set[int] = set()
    source_count = 0
    for provider in providers:
        rows = await asyncio.to_thread(vod_db.list_pending_series_sources, provider["id"], limit, series_ids)
        if rows:
            pending_provider_ids.append(provider["id"])
            pending_by_provider[provider["id"]] = rows
            pending_series_ids.update(row["series_id"] for row in rows)
            source_count += len(rows)

    episode_before = await asyncio.to_thread(vod_db.get_series_episode_summaries, pending_series_ids)

    _ENRICH_PROGRESS.update({
        "running": True, "movies_total": 0, "movies_done": 0,
        "movies_errors": 0, "series_total": len(pending_series_ids), "series_done": 0,
        "series_sources_total": source_count, "series_sources_done": 0,
        "progress_phase": "series_episodes",
        "series_errors": 0, "series_backoff_skipped": 0,
        "started_at": time.time(), "finished_at": None,
        "cancelled": False, "providers_incomplete": [],
    })
    series_sem = _CapacitySemaphore(max(1, concurrency))
    write_queue: "asyncio.Queue" = asyncio.Queue(maxsize=32)
    writer_task = asyncio.create_task(_run_global_writer(write_queue))
    try:
        selected = [p for p in providers if p["id"] in pending_provider_ids]
        results = await asyncio.gather(*(
            _run_provider_series_phase(
                provider, series_sem, False, write_queue=write_queue,
                provider_count=len(selected), pending_only=True, episodes_only=True,
                pending_sources=pending_by_provider[provider["id"]],
                request_spacing=request_spacing, max_consecutive_failures=max_consecutive_failures,
                yield_to_import=yield_to_import,
            ) for provider in selected
        ))
        for provider, result in zip(selected, results):
            if not result[0]:
                _ENRICH_PROGRESS["providers_incomplete"].append({
                    "provider_id": provider["id"],
                    "provider_name": provider.get("name"), "phase": "series",
                })
        await write_queue.put(None)
        await writer_task
        merge_events = await asyncio.to_thread(vod_db.auto_merge_series_by_tmdb_batch, pending_series_ids) or []
        if pending_series_ids:
            merge_events.extend(await _run_scoped_collision_sweep(
                vod_db.auto_merge_series_tmdb_collisions, pending_series_ids,
            ))
        episode_after = await asyncio.to_thread(vod_db.get_series_episode_summaries, pending_series_ids)
        episode_events = []
        for series_id in sorted(pending_series_ids):
            before = episode_before.get(series_id) or {
                "episode_count": 0, "season_count": 0, "episode_source_count": 0,
            }
            after = episode_after.get(series_id)
            if not after:
                continue
            episode_events.append({
                "content_type": "series",
                "content_id": series_id,
                "title": after["title"],
                "year": after["year"],
                "action": "episodes_synced",
                "detail": {
                    "before": before,
                    "after": after,
                    "episodes_added": after["episode_count"] - before["episode_count"],
                    "episode_sources_added": after["episode_source_count"] - before["episode_source_count"],
                },
            })
        return merge_events + episode_events
    finally:
        if not writer_task.done():
            writer_task.cancel()
        _ENRICH_PROGRESS["running"] = False
        _ENRICH_PROGRESS["finished_at"] = time.time()


async def _post_import_enrichment(
    *, track_catalog_workflow: bool = True,
    changed_movie_ids: set[int] | None = None,
    changed_series_ids: set[int] | None = None,
    workflow_run_id: int | None = None,
) -> None:
    """Run the ordered metadata phases after import.

    Movie provider detail is never part of this handoff. Series provider
    detail is deliberately not part of this handoff either. Episode discovery
    is lazy on show open, with a small scheduled trickle handled separately.
    """
    # KNM: 2026-10-03 pin the run now; a later import replaces the header's run_id.
    pinned_run_id = workflow_run_id or _CATALOG_WORKFLOW_PROGRESS.get("run_id")
    try:
        if track_catalog_workflow:
            _set_catalog_workflow_phase("Resolving known TMDB identities", run_id=pinned_run_id)
        movie_count = await asyncio.to_thread(vod_db.count_movies_pending_tmdb_enrichment) if changed_movie_ids is None else await asyncio.to_thread(vod_db.count_movies_pending_tmdb_enrichment, changed_movie_ids)
        series_count = await asyncio.to_thread(vod_db.count_series_pending_tmdb_metadata_enrichment) if changed_series_ids is None else await asyncio.to_thread(vod_db.count_series_pending_tmdb_metadata_enrichment, changed_series_ids)
        movie_limit = min(_TMDB_INITIAL_MOVIE_CAP, max(1, math.ceil(movie_count * _TMDB_INITIAL_FRACTION))) if movie_count else 0
        series_limit = min(_TMDB_INITIAL_SERIES_CAP, max(1, math.ceil(series_count * _TMDB_INITIAL_FRACTION))) if series_count else 0
        logger.info(
            "[vod_importer] bounded initial pass: movies=%s/%s series_tmdb=%s/%s; episode discovery deferred",
            movie_limit, movie_count, series_limit, series_count,
        )
        movie_events = await bulk_enrich_tmdb_movies(limit=movie_limit or 1, item_ids=changed_movie_ids) or []
        series_events = await bulk_enrich_tmdb_series_metadata(limit=series_limit or 1, item_ids=changed_series_ids) or []
        await _append_catalog_sync_events(
            pinned_run_id,
            movie_events + series_events,
            {
                "automatic_merges": sum(event.get("action") == "merged" for event in movie_events + series_events),
                "series_episode_reports": 0,
            },
        )
        if track_catalog_workflow:
            pinned_run_id, stamps, _ = _workflow_stamps(pinned_run_id)
            stamps["enrichment_finished_at"] = time.time()
            if pinned_run_id:
                vod_db.update_catalog_sync_run(
                    pinned_run_id,
                    enrichment_finished_at=str(stamps["enrichment_finished_at"]),
                    status="ready",
                    ready_at=str(time.time()),
                )
        if track_catalog_workflow:
            _set_catalog_workflow_phase("Preparing catalog review", run_id=pinned_run_id)
        if track_catalog_workflow:
            mark_catalog_workflow_ready(run_id=pinned_run_id)
        _schedule_background_tmdb_work()
    except asyncio.CancelledError:
        # KNM: 2026-10-03 CancelledError is a BaseException, so cancellation skipped
        # the handler below and left the run "running" with leaked phase stamps.
        logger.warning("[vod_importer] post-import enrichment cancelled")
        if track_catalog_workflow:
            _mark_catalog_workflow_failed("automatic enrichment cancelled", run_id=pinned_run_id)
        raise
    except Exception:
        logger.exception("[vod_importer] post-import enrichment failed")
        if track_catalog_workflow:
            _mark_catalog_workflow_failed("automatic enrichment failed", run_id=pinned_run_id)


def schedule_post_import_enrichment(
    *, track_catalog_workflow: bool = True,
    changed_movie_ids: set[int] | None = None,
    changed_series_ids: set[int] | None = None,
    workflow_run_id: int | None = None,
) -> bool:
    """Queue one reconciliation pass; coalesce overlapping provider imports."""
    global _POST_IMPORT_ENRICH_TASK
    if _POST_IMPORT_ENRICH_TASK and not _POST_IMPORT_ENRICH_TASK.done():
        return False
    reset_enrichment_progress()
    _TMDB_ENRICH_PROGRESS.update({
        "running": False, "total": 0, "done": 0, "errors": 0,
        "started_at": None, "finished_at": None,
    })
    if track_catalog_workflow:
        _set_catalog_workflow_phase("Preparing automatic catalog review", run_id=workflow_run_id)
    _POST_IMPORT_ENRICH_TASK = asyncio.create_task(
        _post_import_enrichment(
            track_catalog_workflow=track_catalog_workflow,
            changed_movie_ids=changed_movie_ids,
            changed_series_ids=changed_series_ids,
            workflow_run_id=workflow_run_id,
        )
    )
    return True


async def _background_tmdb_enrichment() -> None:
    """Process one small low-priority scheduled TMDB batch.

    Episode discovery is not done here: run_episode_trickle_tick paces it on
    its own schedule, and a show opened by a user uses the on-demand path.
    """
    if await asyncio.to_thread(vod_db.count_movies_pending_tmdb_enrichment):
        await bulk_enrich_tmdb_movies(concurrency=2, limit=_TMDB_BACKGROUND_BATCH)
    if await asyncio.to_thread(vod_db.count_series_pending_tmdb_metadata_enrichment):
        await bulk_enrich_tmdb_series_metadata(concurrency=2, limit=_TMDB_BACKGROUND_BATCH)


# KNM: 2026-10-03 -- indirection so tests can observe the trickle's pacing.
_trickle_sleep = asyncio.sleep
_TRICKLE_MAX_CONSECUTIVE_FAILURES = 3


async def run_episode_trickle_tick(
    batch_size: int | None = None, spacing_seconds: float | None = None,
) -> list[dict]:
    """One paced episode-discovery pass: up to batch_size pending series
    sources per provider, one request at a time, spacing_seconds apart.
    A provider's lane stops early once it is in backoff or fails
    _TRICKLE_MAX_CONSECUTIVE_FAILURES times in a row; the rest stay pending
    for the next tick."""
    settings = config.get_refresh_settings()
    batch = int(batch_size if batch_size is not None else settings["episode_trickle_batch"])
    spacing = float(spacing_seconds if spacing_seconds is not None else settings["episode_trickle_spacing_seconds"])
    if batch <= 0:
        return []
    if _XC_IMPORT_LOCK.locked():
        logger.info("[episode_trickle] catalog import running; skipping this tick")
        return []
    return await bulk_enrich_series_episodes(
        concurrency=1, limit=batch, request_spacing=spacing,
        max_consecutive_failures=_TRICKLE_MAX_CONSECUTIVE_FAILURES,
        yield_to_import=True,
    )


def _schedule_background_tmdb_work() -> None:
    global _BACKGROUND_TMDB_TASK
    if _BACKGROUND_TMDB_TASK and not _BACKGROUND_TMDB_TASK.done():
        return
    _BACKGROUND_TMDB_TASK = asyncio.create_task(_background_tmdb_enrichment())


_ENRICH_PROGRESS: dict = {
    "running": False,
    "movies_total": 0, "movies_done": 0, "movies_errors": 0, "movies_backoff_skipped": 0,
    "series_total": 0, "series_done": 0, "series_errors": 0, "series_backoff_skipped": 0,
    "series_sources_total": 0, "series_sources_done": 0,
    "progress_phase": "catalog",
    "started_at": None, "finished_at": None,
    "cancelled": False,
    # Populated by bulk_enrich_all's per-provider sequencing
    # when a provider's movie or series phase never
    # succeeded even after the single allotted retry -- see that function's
    # docstring for the full sequencing/retry rules. Each entry:
    # {"provider_id": int, "provider_name": str, "phase": "movies"|"series"}.
    "providers_incomplete": [],
}
_ENRICH_CANCEL_REQUESTED = False


def cancel_bulk_enrichment() -> bool:
    """Request a cooperative stop for the active bulk enrichment job."""
    global _ENRICH_CANCEL_REQUESTED
    if not _ENRICH_PROGRESS["running"]:
        return False
    _ENRICH_CANCEL_REQUESTED = True
    logger.warning("[vod_importer] bulk enrichment cancellation requested")
    return True


def reset_enrichment_progress() -> None:
    """Discard the previous bulk snapshot before a new catalog workflow."""
    if _ENRICH_PROGRESS["running"]:
        return
    _ENRICH_PROGRESS.update({
        "movies_total": 0, "movies_done": 0, "movies_errors": 0, "movies_backoff_skipped": 0,
        "series_total": 0, "series_done": 0, "series_errors": 0, "series_backoff_skipped": 0,
        "series_sources_total": 0, "series_sources_done": 0, "progress_phase": "catalog",
        "started_at": None, "finished_at": None, "cancelled": False,
        "providers_incomplete": [],
    })

# Item ids already counted into movies_done/series_done this run. The
# single-retry pass (bulk_enrich_all) re-runs a failed provider's ENTIRE
# id list, not just the items that actually failed/backed off, which means
# an item that already succeeded on the first pass goes through
# _enrich_one again on retry -- without this dedup, its "done" would get
# counted twice, which is exactly how movies_done reached 101538 against a
# movies_total of 62854 on 2026-09-12 (live, provider A/B retry storm).
# Reset at the start of every bulk_enrich_all run.
_ENRICH_DONE_IDS: dict[str, set] = {"movie": set(), "series": set()}
_ENRICH_DONE_SOURCE_IDS: set = set()


def _reset_enrich_done_ids() -> None:
    # KNM: 2026-10-04 -- every run clears both sets together; clearing them at
    # different points let stale keys from a prior run skew *_done counts.
    _ENRICH_DONE_IDS["movie"].clear()
    _ENRICH_DONE_IDS["series"].clear()
    _ENRICH_DONE_SOURCE_IDS.clear()


def get_enrich_progress() -> dict:
    progress = dict(_ENRICH_PROGRESS)
    # Surfaces which provider(s), if any, enrichment is currently backing off
    # from and for how much longer -- otherwise a stalled-looking done-count
    # (see _record_provider_failure's docstring) has no visible explanation
    # in the UI beyond "it's slow".
    progress["providers_backing_off"] = [
        {"provider_id": pid, "seconds_remaining": round(remaining, 1)}
        for pid, remaining in ((pid, _provider_backoff_remaining(pid)) for pid in list(_PROVIDER_BACKOFF))
        if remaining > 0
    ]
    # Only surfaces a provider actually narrowed below the default -- most
    # runs never show anything here, same as providers_backing_off above.
    progress["providers_throttled"] = [
        {"provider_id": pid, "concurrency": limiter.cap, "max_concurrency": _PROVIDER_MAX_CONCURRENCY}
        for pid, limiter in list(_PROVIDER_LIMITERS.items())
        if limiter.cap < _PROVIDER_MAX_CONCURRENCY
    ]
    return progress


_PROGRESS_PREFIX = {"movie": "movies", "series": "series"}  # "series" pluralizes to itself, not "seriess"


# Returned by _enrich_one when a series source's provider fetch failed (truthy,
# so callers that only check `is False` keep their behavior).
_SOURCE_FETCH_FAILED = "source_fetch_failed"
# KNM: 2026-10-04 -- returned on ProviderBackoffError: a deliberate deferral,
# not a provider failure, so phases must not mark themselves failed for it
# (that triggered a full retry / skipped the series phase).
_BACKOFF_SKIPPED = "backoff_skipped"


class _CapacitySemaphore(asyncio.Semaphore):
    """Semaphore that remembers its configured size; _value is only the free
    permits, which is smaller while other providers' phases hold some."""

    def __init__(self, value: int = 1):
        super().__init__(value)
        self.capacity = value


def _semaphore_capacity(sem: asyncio.Semaphore) -> int:
    return getattr(sem, "capacity", None) or sem._value or 1


async def _enrich_one(
    kind: str, sem: asyncio.Semaphore, item_id: int, force: bool, *,
    skip_auto_merge: bool = False, movie_batch: list | None = None, provider_id: int | None = None,
    write_queue: "asyncio.Queue | None" = None, series_id: int | None = None,
    episodes_only: bool = True, source_id: int | None = None,
    progress_mode: str = "canonical",
) -> bool:
    """Returns True iff this item's enrichment call actually succeeded,
    _BACKOFF_SKIPPED when its provider is backing off (deferred, not failed),
    or False on a real error -- used by bulk_enrich_all's
    per-provider phase orchestration to tell "this provider's phase is
    genuinely failing" apart from "some items 404'd but the provider itself
    is fine", the same way a single _enrich_one call always could not.

    movie_batch (kind == "movie" only): when given, enrich_movie
    is called with skip_write=True and its returned payload (if any fields
    resulted) is appended here instead of being written immediately --
    _run_provider_movie_phase owns flushing this list via
    vod_db.apply_movie_enrichment_batch in chunks, so many concurrently-
    enriching movies share one write-lock acquisition per chunk instead of
    each independently acquiring it."""
    prefix = _PROGRESS_PREFIX[kind]
    async with sem:
        try:
            if kind == "movie":
                if movie_batch is not None:
                    result = await enrich_movie(item_id, force=force, skip_auto_merge=skip_auto_merge, skip_write=True)
                    if isinstance(result, dict):
                        movie_batch.append(result)
                else:
                    await enrich_movie(item_id, force=force, skip_auto_merge=skip_auto_merge)
            elif provider_id is not None:
                series_kwargs = {
                    "force": force, "skip_auto_merge": skip_auto_merge, "write_queue": write_queue,
                }
                if series_id is not None:
                    # item_id is the canonical series id used for progress
                    # accounting; source_id identifies the provider-specific
                    # series_sources row being fetched.
                    series_kwargs["source_id"] = source_id if source_id is not None else item_id
                result = await enrich_series_source_only(series_id or item_id, provider_id, **series_kwargs)
                # KNM: 2026-10-04 -- a provider fetch failure comes back as a
                # result, not an exception; surface it so the trickle's
                # consecutive-failure stop can see it.
                if isinstance(result, dict) and result.get("failed"):
                    return _SOURCE_FETCH_FAILED
            else:
                await enrich_series(item_id, force=force, skip_auto_merge=skip_auto_merge)
            return True
        except ProviderBackoffError:
            # Not a real failure -- deliberately skipped, no request sent,
            # because that item's provider is already known to be
            # rate-limited/blocked right now (see _record_provider_failure).
            # Doesn't touch last_enriched_at, so this item stays eligible
            # and gets picked up again on the next bulk-enrich run (or later
            # in this same run, once the provider's backoff expires).
            _ENRICH_PROGRESS[f"{prefix}_backoff_skipped"] += 1
            return _BACKOFF_SKIPPED
        except Exception as exc:
            # httpx.HTTPStatusError/ConnectError's own str() embeds the full
            # request URL -- real, working provider credentials included --
            # so this must go through the same redaction xc_server already
            # uses for stream URLs, or a paid-subscription login lands in
            # plaintext in container logs on every single failed lookup
            # (real user log 2026-09-05: hundreds of these per bulk-enrich
            # run, one per 404/429). tmdb_sync._redact is this same fix for
            # TMDB's own api_key query param.
            # str(exc) is often empty for httpx's timeout/connect exceptions
            # (they're frequently raised with no message) -- fall back to the
            # exception's class name so these lines stay diagnosable instead
            # of rendering as "failed: " with nothing after the colon (real
            # log 2026-09-12: provider A backoff investigation, ConnectTimeout/
            # ReadTimeout all logged blank).
            detail = _redact_upstream_url(str(exc)) or type(exc).__name__
            logger.warning("[vod_importer] bulk enrich %s=%s failed: %s", kind, item_id, detail)
            _ENRICH_PROGRESS[f"{prefix}_errors"] += 1
            return False
        finally:
            # Only count each item id once toward *_done, even if this same
            # id is enriched again later (the single-retry pass re-runs a
            # failed provider's FULL id list, so an already-succeeded item
            # can hit this function a second time) -- see _ENRICH_DONE_IDS.
            if kind == "series" and progress_mode == "source":
                source_key = source_id if source_id is not None else item_id
                if source_key not in _ENRICH_DONE_SOURCE_IDS:
                    _ENRICH_DONE_SOURCE_IDS.add(source_key)
                    _ENRICH_PROGRESS["series_sources_done"] += 1
            else:
                done_ids = _ENRICH_DONE_IDS[kind]
                if item_id not in done_ids:
                    done_ids.add(item_id)
                    _ENRICH_PROGRESS[f"{prefix}_done"] += 1


# One global writer serializes these transactions.  250 keeps commits bounded
# for SQLite readers/recovery while avoiding the overhead of 25-item commits
# on large provider imports.
_MOVIE_BATCH_CHUNK_SIZE = 250


async def _run_provider_movie_phase(
    provider: dict, sem: asyncio.Semaphore, force: bool, write_queue: "asyncio.Queue | None" = None,
    provider_count: int = 1, pending_only: bool = False,
) -> tuple[bool, list]:
    """Runs one provider's movie phase to completion. Returns (ok, movie_ids)
    where ok is True iff every item in this provider's movie phase completed
    without raising (ProviderBackoffError counts as non-raising/ok, same as
    _enrich_one's existing semantics -- a backed-off item isn't a provider
    failure, it's deliberately deferred).

    Movie writes are collected in-memory (movie_batch) across all
    of this provider's concurrently-enriching movies instead of each one
    independently acquiring vod_db._WRITE_LOCK via set_movie_enrichment --
    same class of lock contention for series via
    enrich_series_episodes_batch, just at movie-count scale. Flushed in
    _MOVIE_BATCH_CHUNK_SIZE-item chunks as enrichment completes (so one giant
    provider catalog doesn't hold one single enormous uncommitted
    transaction), with a final flush for whatever's left once every item has
    resolved -- this phase does not return until every movie's write has
    actually been committed, so the caller's end-of-run
    auto_merge_movie_by_tmdb sweep never runs against a tmdb_id that's still
    unwritten.

    provider_count: `sem` is ONE semaphore shared across every provider's
    movie phase running
    concurrently in bulk_enrich_all (created once, passed into all of them).
    worker_count used to be `sem._value` outright -- each provider's phase
    claiming the semaphore's FULL configured capacity as its OWN worker
    count, so with N providers running at once, N*sem._value long-lived
    workers all contended for the same sem._value slots. Each provider's own
    workers loop straight back to re-acquire sem the instant they release
    it, so whichever provider's workers start winning slots tends to keep
    winning them in a tight self-reinforcing burst -- unlike the old
    per-item create_task/gather pattern (see below) where task-creation
    order naturally interleaved slot wins across all providers. Passing the
    actual concurrently-running provider_count down lets this phase divide
    the shared semaphore's capacity fairly (min 1 worker) instead of
    claiming it whole; default of 1 preserves existing single-provider
    direct-call behavior/tests unchanged.

    write_queue (plan-doc follow-up "one global writer", 2026-09-14): when
    given, each chunk is put() on this run-wide queue and awaited via a
    per-chunk asyncio.Event instead of calling
    vod_db.apply_movie_enrichment_batch directly -- so this phase's writes
    are serialized against every OTHER concurrently-running provider phase's
    writes (movie or series) through the one _run_global_writer task
    draining the queue, instead of each phase independently reaching its own
    batch-commit point at the same time. When write_queue is None (no
    bulk_enrich_all run in progress), behavior is unchanged: this phase
    writes its own chunks directly.

    CPU-spike follow-up (2026-09-14, user-supplied analysis): this used to
    asyncio.create_task() one item PER movie id up front (a list
    comprehension over every id in the provider's catalog), even though
    `sem` only ever let `sem._value` of them run at once -- against a full
    catalog that's tens of thousands of live Task objects queued in the
    event loop for no added parallelism. Now a fixed pool of `sem`-sized
    long-lived workers pulls one id at a time off a plain asyncio.Queue,
    so at most `sem`'s original capacity worth of ids are ever "live" as
    real coroutines/Tasks; the rest sit as plain ints in the queue until
    their turn. Throughput/concurrency is unchanged -- only the up-front
    Task pile-up is removed."""
    movie_ids = await asyncio.to_thread(
        lambda: vod_db.list_movie_ids_pending_provider_enrichment(provider["id"])
        if pending_only and not force else vod_db.list_all_movie_ids(provider_id=provider["id"])
    )
    ok = True
    movie_batch: list = []
    # sem is shared across provider_count concurrently-running providers'
    # phases -- divide its capacity fairly instead of each phase
    # claiming sem._value (the semaphore's full capacity) for itself alone.
    worker_count = max(1, _semaphore_capacity(sem) // max(1, provider_count))

    async def _flush_chunk(chunk: list) -> None:
        if write_queue is not None:
            done = asyncio.Event()
            await write_queue.put({"kind": "movie", "items": chunk, "done": done})
            await done.wait()
        else:
            await asyncio.to_thread(vod_db.apply_movie_enrichment_batch, chunk)

    async def _flush_full_chunks() -> None:
        while len(movie_batch) >= _MOVIE_BATCH_CHUNK_SIZE:
            chunk, movie_batch[:_MOVIE_BATCH_CHUNK_SIZE] = movie_batch[:_MOVIE_BATCH_CHUNK_SIZE], []
            await _flush_chunk(chunk)

    queue: asyncio.Queue = asyncio.Queue()
    for mid in movie_ids:
        queue.put_nowait(mid)

    async def _worker() -> None:
        nonlocal ok
        while True:
            if _ENRICH_CANCEL_REQUESTED:
                return
            try:
                mid = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                outcome = await _enrich_one("movie", sem, mid, force, skip_auto_merge=True, movie_batch=movie_batch)
            except BaseException:
                outcome = False
            if outcome is False:
                ok = False
            await _flush_full_chunks()

    await asyncio.gather(*(_worker() for _ in range(min(worker_count, len(movie_ids) or 1))))

    if movie_batch:
        await _flush_chunk(movie_batch)

    return ok, movie_ids


async def _run_provider_series_phase(
    provider: dict, sem: asyncio.Semaphore, force: bool, write_queue: "asyncio.Queue | None" = None,
    provider_count: int = 1, pending_only: bool = False, episodes_only: bool = False,
    pending_sources: list[dict] | None = None,
    request_spacing: float = 0, max_consecutive_failures: int | None = None,
    yield_to_import: bool = False,
) -> tuple[bool, list]:
    """Runs one provider's series phase to completion. Same ok semantics as
    _run_provider_movie_phase. write_queue: see _run_provider_movie_phase's
    docstring -- threaded down to enrich_series_source_only/
    _enrich_one_series_source so this phase's episode-batch writes are
    serialized through the same run-wide writer as every other provider's
    movie/series writes. provider_count: see _run_provider_movie_phase's
    docstring -- same shared-semaphore fair-share fix, default 1 preserves
    existing single-provider direct-call behavior/tests unchanged.

    CPU-spike follow-up (2026-09-14, user-supplied analysis): this used to
    asyncio.gather() over a generator expression covering EVERY series id
    up front -- gather() fully materializes that generator into live
    Task/coroutine objects before any of them run, same up-front pile-up
    problem as the movie phase's old create_task list comprehension (see
    that phase's docstring). Now uses the same bounded worker-pool
    pattern: a fixed pool of `sem`-sized workers pulls one id at a time
    off a plain asyncio.Queue. Throughput/concurrency is unchanged."""
    if pending_only and pending_sources is None:
        pending_sources = await asyncio.to_thread(
            vod_db.list_pending_series_sources, provider["id"]
        )
    series_ids = [source["series_id"] for source in pending_sources] if pending_sources is not None else await asyncio.to_thread(
        vod_db.list_all_series_ids, provider_id=provider["id"]
    )
    ok = True
    worker_count = max(1, _semaphore_capacity(sem) // max(1, provider_count))

    queue: asyncio.Queue = asyncio.Queue()
    for item in pending_sources if pending_sources is not None else series_ids:
        queue.put_nowait(item)

    # KNM: 2026-10-03 -- paced trickle: optional gap between requests, and
    # stop this provider's lane once it is backing off or keeps failing.
    consecutive_failures = 0
    started = False

    async def _worker() -> None:
        nonlocal ok, consecutive_failures, started
        while True:
            if _ENRICH_CANCEL_REQUESTED:
                return
            if max_consecutive_failures and consecutive_failures >= max_consecutive_failures:
                return
            # KNM: 2026-10-04 -- a catalog import holds the SQLite writer for
            # minutes; episode writes queued behind it hit "database is locked".
            if yield_to_import and _XC_IMPORT_LOCK.locked():
                return
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if request_spacing and started:
                await _trickle_sleep(request_spacing)
            started = True
            try:
                if pending_sources is not None:
                    outcome = await _enrich_one(
                        "series", sem, item["series_id"], force, skip_auto_merge=True, provider_id=provider["id"],
                        write_queue=write_queue, series_id=item["series_id"],
                        source_id=item["id"], progress_mode="source" if episodes_only else "canonical",
                    )
                else:
                    outcome = await _enrich_one(
                        "series", sem, item, force, skip_auto_merge=True, provider_id=provider["id"], write_queue=write_queue,
                    )
            except BaseException:
                outcome = False
            if outcome == _BACKOFF_SKIPPED:
                # Provider is rate-limited; stop this trickle lane, don't fail the phase.
                if max_consecutive_failures:
                    consecutive_failures = max_consecutive_failures
            elif outcome is False:
                ok = False
                consecutive_failures += 1
                if max_consecutive_failures and _provider_backoff_remaining(provider["id"]) > 0:
                    consecutive_failures = max_consecutive_failures
            elif outcome == _SOURCE_FETCH_FAILED:
                # Already recorded on the source row; only the lane's stop counts it.
                consecutive_failures += 1
            else:
                consecutive_failures = 0

    await asyncio.gather(*(_worker() for _ in range(min(worker_count, len(series_ids) or 1))))

    return ok, list(dict.fromkeys(series_ids))


async def _run_provider_enrichment(
    provider: dict, movie_sem: asyncio.Semaphore, series_sem: asyncio.Semaphore, force: bool,
    write_queue: "asyncio.Queue | None" = None, provider_count: int = 1, pending_only: bool = False,
) -> dict:
    """Per-provider orchestrator implementing the per-provider
    sequencing/retry rules for exactly ONE provider: that provider's own
    movies enrich, then that SAME provider's own series enrich after -- but
    this coroutine never waits on any other provider, so provider A's slow
    or failing movie phase can never block provider B's series (the bug this
    redesign replaces: a flat single asyncio.gather() piled every provider's
    movies AND series onto shared semaphores with no per-provider isolation,
    so one provider misbehaving could trip another's shared backoff
    threshold).

    A failed movie phase does NOT retry inline here -- it's reported back to
    bulk_enrich_all as pending-retry, which the caller retries only after
    every OTHER provider has already finished, per the spec ("after ALL
    other providers finish, retry the failed provider's movies exactly
    once"). Returns a dict describing what still needs to happen:
      {"movie_ok": bool, "movie_ids": [...], "series_ran": bool,
       "series_ok": bool, "series_ids": [...]}

    write_queue: threaded to both phase calls -- see _run_global_writer.
    provider_count: how many providers are running this concurrently via the
    caller's asyncio.gather -- threaded to both phases so each divides
    movie_sem/series_sem's shared capacity fairly (see
    _run_provider_movie_phase's docstring)."""
    movie_kwargs = {"write_queue": write_queue, "provider_count": provider_count}
    if pending_only:
        movie_kwargs["pending_only"] = True
    movie_ok, movie_ids = await _run_provider_movie_phase(provider, movie_sem, force, **movie_kwargs)
    if not movie_ok:
        return {"movie_ok": False, "movie_ids": movie_ids, "series_ran": False, "series_ok": None, "series_ids": []}

    series_kwargs = {"write_queue": write_queue, "provider_count": provider_count}
    if pending_only:
        series_kwargs["pending_only"] = True
    series_ok, series_ids = await _run_provider_series_phase(provider, series_sem, force, **series_kwargs)
    return {"movie_ok": True, "movie_ids": movie_ids, "series_ran": True, "series_ok": series_ok, "series_ids": series_ids}


async def _run_global_writer(queue: "asyncio.Queue") -> None:
    """Plan-doc follow-up "one global writer" (2026-09-14): the ONLY coroutine
    that ever calls vod_db.apply_movie_enrichment_batch or
    vod_db.enrich_series_episodes_batch during a bulk_enrich_all run. Every
    concurrently-running provider lane (movie phase or series phase, any
    provider) put()s its batch payload onto this one run-wide queue instead
    of writing directly, so no two batch-commit calls to SQLite are ever in
    flight at the same time, regardless of how many provider lanes are
    enriching concurrently.

    Drains `queue` until it receives the shutdown sentinel (None). Each item
    is a dict with "kind" ("movie" or "series") plus that kind's payload and
    a "done" asyncio.Event the producer is awaiting -- set after the write
    commits so the producing phase knows its data is durably written before
    it returns (preserving the existing guarantee that a phase's writes are
    flushed before the caller's end-of-phase merge sweep runs)."""
    while True:
        item = await queue.get()
        if item is None:
            return
        if item["kind"] == "movie":
            await asyncio.to_thread(vod_db.apply_movie_enrichment_batch, item["items"])
        else:
            await asyncio.to_thread(
                vod_db.enrich_series_episodes_batch, item["series_id"], item["provider_id"], item["episodes"],
            )
        item["done"].set()


async def bulk_enrich_all(concurrency: int = 8, force: bool = False, pending_only: bool = False) -> None:
    """Enriches every movie and series in the pool, sequenced PER PROVIDER
    (2026-09-12 redesign): each provider's own movies
    enrich, then that same provider's own series -- but different providers
    never block each other, so provider A stuck on a slow/failing movie
    phase can't delay provider B's series at all.

    Replaces the old flat design (movies-then-series globally, or before
    that, all movies+series in one shared-semaphore gather()) after live
    incidents on provider A/B where one provider's failures piled onto a
    globally-shared backoff/semaphore budget and starved or throttled
    completely unrelated, healthy providers (see this module's test
    tests/test_bulk_enrich_provider_sequencing.py for the full spec this
    implements).

    Per-provider failure isolation, exactly one retry:
      - A provider's movie phase failing skips that provider entirely (no
        series either) while every OTHER provider continues unaffected.
      - Once every OTHER provider has finished, the failed provider's movies
        get retried exactly once. Retry succeeds -> that provider's series
        proceed normally. Retry fails again -> flag that provider's movie
        enrichment incomplete (_ENRICH_PROGRESS["providers_incomplete"]) and
        never attempt its series -- no third attempt.
      - A provider's series phase failing (movies already succeeded) is
        never retried -- just flagged incomplete. Never reruns movies, never
        blocks anything else.

    Auto-merge (auto_merge_movie_by_tmdb/auto_merge_series_by_tmdb) moves
    from per-item inline calls to two end-of-phase sweeps here: one movie
    sweep after every provider has resolved its movie phase (success or
    failed-after-retry), one series sweep after every provider has resolved
    its series phase. enrich_movie/enrich_series are always called with
    skip_auto_merge=True from this function so the inline per-item merge
    never double-runs during a bulk pass -- single on-demand enrich (outside
    bulk_enrich_all) keeps its immediate inline merge unchanged."""
    global _ENRICH_CANCEL_REQUESTED
    if _ENRICH_PROGRESS["running"] or (_BACKGROUND_TMDB_TASK and not _BACKGROUND_TMDB_TASK.done()):
        logger.info("[vod_importer] enrichment already running; skipping overlapping catalog run")
        return
    _ENRICH_CANCEL_REQUESTED = False

    providers = await asyncio.to_thread(vod_db.list_providers)
    configured_provider_count = len(providers)
    # Only providers that can actually perform work participate in the
    # fair-share calculation below.  Counting configured-but-empty providers
    # can turn concurrency=8 into one worker for the sole provider with
    # pending content (for example, five configured providers -> 8 // 5).
    # Each selected provider still keeps its own movie-then-series lane,
    # adaptive limiter, and backoff state.
    active_providers: list[dict] = []
    movie_ids_all: set[int] = set()
    series_ids_all: set[int] = set()
    for provider in providers:
        # A disabled provider must not participate in fallback enrichment,
        # even when old catalog source rows still reference it.  Imports and
        # playback already honor this flag; bulk detail enrichment must do so
        # as well or disabling a provider cannot stop its requests.
        if not provider.get("is_active", True):
            continue
        provider_id = provider["id"]
        provider_movie_ids = await asyncio.to_thread(
            lambda: vod_db.list_movie_ids_pending_provider_enrichment(provider_id)
            if pending_only and not force else vod_db.list_all_movie_ids(provider_id=provider_id)
        )
        movie_ids_all.update(provider_movie_ids)
        provider_series_rows = await asyncio.to_thread(
            vod_db.list_pending_series_sources, provider_id
        ) if pending_only and not force else None
        if provider_series_rows is not None:
            series_ids_all.update(row["series_id"] for row in provider_series_rows)
        else:
            series_ids_all.update(await asyncio.to_thread(vod_db.list_all_series_ids, provider_id=provider_id))
        provider_has_series_work = await asyncio.to_thread(
            vod_db.has_pending_series_source_enrichment, provider_id
        ) if pending_only and not force else bool(
            await asyncio.to_thread(vod_db.list_all_series_ids, provider_id=provider_id)
        )
        if provider_movie_ids or provider_has_series_work:
            active_providers.append(provider)
    providers = active_providers
    movie_ids_all = sorted(movie_ids_all)
    series_ids_all = sorted(series_ids_all)
    # Actual per-provider ids seen during this run (populated below) --
    # used for the end-of-phase merge sweeps instead of movie_ids_all/
    # series_ids_all, since a provider isn't guaranteed to have been listed
    # via the exact same query the global counts above came from (e.g. tests
    # mock list_all_movie_ids(provider_id=...) distinctly from the no-arg
    # call), and a provider added mid-run should still get merged.
    merged_movie_ids: set = set()
    merged_series_ids: set = set()
    _reset_enrich_done_ids()
    _ENRICH_PROGRESS.update({
        "running": True,
        "movies_total": len(movie_ids_all), "movies_done": 0, "movies_errors": 0, "movies_backoff_skipped": 0,
        "series_total": len(series_ids_all), "series_done": 0, "series_errors": 0, "series_backoff_skipped": 0,
        "series_sources_total": 0, "series_sources_done": 0, "progress_phase": "catalog",
        "started_at": time.time(), "finished_at": None,
        "cancelled": False,
        "providers_incomplete": [],
    })
    logger.info("[vod_importer] bulk enrich starting: %d movies, %d series, %d active/%d configured providers, concurrency=%d",
                len(movie_ids_all), len(series_ids_all), len(providers), configured_provider_count, concurrency)

    # Separate semaphores -- movies and series shouldn't compete with each
    # other for the same `concurrency` slots (that would just reproduce the
    # starvation this is fixing, only softer), each kind gets its own
    # provider-request budget. Shared across all providers, same as before --
    # only the SEQUENCING is now per-provider, not the concurrency budget.
    movie_sem = _CapacitySemaphore(concurrency)
    series_sem = _CapacitySemaphore(concurrency)

    # One run-wide queue + one background writer task (plan-doc follow-up
    # "one global writer", 2026-09-14) -- every provider lane's movie/series
    # batch write, across the WHOLE run, funnels through here instead of
    # each lane independently reaching its own commit point. Bounded so a
    # writer that falls behind applies backpressure to producers rather than
    # letting unbounded in-memory payloads pile up.
    write_queue: "asyncio.Queue" = asyncio.Queue(maxsize=32)
    writer_task = asyncio.create_task(_run_global_writer(write_queue))
    try:
        results = await asyncio.gather(
            *(
                _run_provider_enrichment(
                    p, movie_sem, series_sem, force, write_queue=write_queue,
                    provider_count=len(providers), pending_only=pending_only,
                )
                for p in providers
            ),
        )
        for result in results:
            merged_movie_ids.update(result["movie_ids"])
            if result["series_ran"]:
                merged_series_ids.update(result["series_ids"])

        if _ENRICH_CANCEL_REQUESTED:
            return
        # Retry pass: only for providers whose movie phase failed on the
        # first attempt, and only after every OTHER provider has already
        # finished both phases (spec: "after ALL other providers finish").
        needs_retry = [(p, r) for p, r in zip(providers, results) if not r["movie_ok"]]
        for provider, _first_result in needs_retry:
            if _ENRICH_CANCEL_REQUESTED:
                return
            retry_kwargs = {"write_queue": write_queue}
            if pending_only:
                retry_kwargs["pending_only"] = True
            movie_ok, movie_ids = await _run_provider_movie_phase(provider, movie_sem, force, **retry_kwargs)
            merged_movie_ids.update(movie_ids)
            if not movie_ok:
                _ENRICH_PROGRESS["providers_incomplete"].append(
                    {"provider_id": provider["id"], "provider_name": provider.get("name"), "phase": "movies"}
                )
                continue
            series_ok, series_ids = await _run_provider_series_phase(provider, series_sem, force, write_queue=write_queue)
            merged_series_ids.update(series_ids)
            if not series_ok:
                _ENRICH_PROGRESS["providers_incomplete"].append(
                    {"provider_id": provider["id"], "provider_name": provider.get("name"), "phase": "series"}
                )

        # A provider whose movies succeeded on the first attempt but whose
        # series phase then failed gets no retry at all -- just flagged.
        for provider, result in zip(providers, results):
            if result["movie_ok"] and result["series_ran"] and not result["series_ok"]:
                _ENRICH_PROGRESS["providers_incomplete"].append(
                    {"provider_id": provider["id"], "provider_name": provider.get("name"), "phase": "series"}
                )

        # Shut the writer down and wait for it to exit before the merge
        # sweeps below -- they must never read against data the writer
        # hasn't actually committed yet.
        await write_queue.put(None)
        await writer_task

        # End-of-phase auto-merge sweeps -- moved here from enrich_movie/
        # enrich_series's own inline per-item calls (which bulk_enrich_all
        # now suppresses via skip_auto_merge=True) so a bulk run merges once
        # per item after its whole phase resolves, not mid-phase per item.
        #
        # One asyncio.to_thread call each, not one per id: every merge is
        # already fully serialized by vod_db._WRITE_LOCK internally, so
        # fanning out via asyncio.gather bought no real parallelism -- against
        # a full-catalog run it meant thousands of OS threads submitted to
        # the executor at once, which is what drove host CPU to 1200%+/near-
        # total saturation during the 2026-09-14 dry-run. See
        # auto_merge_movies_by_tmdb_batch's docstring in vod_db.py.
        await asyncio.to_thread(vod_db.auto_merge_movies_by_tmdb_batch, merged_movie_ids)
        await asyncio.to_thread(vod_db.auto_merge_series_by_tmdb_batch, merged_series_ids)
        # Reconcile only the IDs touched by this bounded run.  A full collision
        # scan remains available only as an explicit maintenance call.
        if merged_movie_ids:
            await _run_scoped_collision_sweep(
                vod_db.auto_merge_movie_tmdb_collisions, merged_movie_ids,
            )
        if merged_series_ids:
            await _run_scoped_collision_sweep(
                vod_db.auto_merge_series_tmdb_collisions, merged_series_ids,
            )
    finally:
        was_cancelled = _ENRICH_CANCEL_REQUESTED
        if not writer_task.done():
            writer_task.cancel()
        _ENRICH_PROGRESS["running"] = False
        _ENRICH_PROGRESS["finished_at"] = time.time()
        _ENRICH_PROGRESS["cancelled"] = was_cancelled
        elapsed = _ENRICH_PROGRESS["finished_at"] - _ENRICH_PROGRESS["started_at"]
        logger.info(
            "[vod_importer] bulk enrich done in %.1fs: movies %d/%d (%d errors, %d backoff-skipped), "
            "series %d/%d (%d errors, %d backoff-skipped), %d provider phase(s) incomplete",
            elapsed,
            _ENRICH_PROGRESS["movies_done"], _ENRICH_PROGRESS["movies_total"], _ENRICH_PROGRESS["movies_errors"],
            _ENRICH_PROGRESS["movies_backoff_skipped"],
            _ENRICH_PROGRESS["series_done"], _ENRICH_PROGRESS["series_total"], _ENRICH_PROGRESS["series_errors"],
            _ENRICH_PROGRESS["series_backoff_skipped"],
            len(_ENRICH_PROGRESS["providers_incomplete"]),
        )


async def resweep_smart_categories(movie_ids: set[int] | None = None, series_ids: set[int] | None = None) -> None:
    """Re-evaluate every smart category with a rule configured (see
    vod_db.list_smart_category_ids_with_rules) so newly imported content
    actually shows up in them without a manual "evaluate" click -- broadened
    2026-07-29 from catch-all-only per user direction ("categories in
    general need some sort of sweep... on whatever the normal provider list
    update is"): rule-based evaluation is free (no external API call) and
    purely additive, so there's no real downside to keeping every rule
    fresh by default rather than requiring an explicit opt-in schedule for
    each one. (AI-assisted evaluation stays opt-in-only, see
    main.py._smart_category_scheduler -- that one has real recurring cost.)

    Shared by the periodic catalog refresher (main.py, fires right after a
    provider's own due catalog refresh -- "whatever the normal provider list
    update is"), the manual "Import catalog" button (vod_routes.py), the
    independent fixed-interval sweep (main.py._uncategorized_sweep_loop),
    and apply_exclusions_job.py -- calling it from all of them means none of
    those paths has to wait on another one to be reflected.

    Also retroactively purges any already-review_excluded item still sitting
    in a category (vod_db.purge_excluded_from_categories) -- covers an
    install that ran import-time exclusion before that bug was fixed, so
    already-wrongly-placed rows actually get cleaned up here rather than
    needing a separate one-off action."""
    # A maintenance/manual full sweep keeps the historical global behavior.
    # A provider delta refresh is intentionally scoped to changed IDs and
    # does not turn a few new sources into a full-pool curation job.
    is_full_sweep = movie_ids is None and series_ids is None
    if is_full_sweep:
        try:
            purge_result = await asyncio.to_thread(vod_db.purge_excluded_from_categories)
            if purge_result["movies_removed"] or purge_result["series_removed"]:
                logger.info("[vod_importer] purged already-excluded items from categories: %s", purge_result)
        except Exception as exc:
            logger.warning("[vod_importer] purge_excluded_from_categories failed: %s", exc)

    for category_id in await asyncio.to_thread(vod_db.list_smart_category_ids_with_rules):
        try:
            category = await asyncio.to_thread(vod_db.get_category, category_id)
            ids = None if is_full_sweep else (movie_ids if category and category["content_type"] == "movie" else series_ids)
            result = await asyncio.to_thread(vod_db.evaluate_smart_category, category_id, ids)
            logger.info("[vod_importer] smart category=%s: %s", category_id, result)
        except Exception as exc:
            logger.warning("[vod_importer] smart category=%s failed: %s", category_id, exc)
