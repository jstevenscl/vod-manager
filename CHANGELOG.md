# Changelog

> ⚠️ **Beta fork — expect rough edges.** This is a community fork of
> [jstevenscl/vod-manager](https://github.com/jstevenscl/vod-manager), the
> actual upstream project. Changes here are still being validated in real
> deployments and some may not have been reviewed/merged upstream yet — treat
> this build as beta, keep backups of your data, and report anything odd.
>
> 🔗 For the original project, official releases, and the primary issue
> tracker, go to **[jstevenscl/vod-manager](https://github.com/jstevenscl/vod-manager)**.

All notable user-facing changes to this fork are documented here. Entries are
grouped by date and describe what changed in the deployed image — not every
internal commit, just additions and fixes worth knowing about if you're
running this build.

✅ = shipped and running in this fork only. Once work is proposed upstream,
add its second status marker and link to the pull request: 🔀 = open/under
review, 🔼 = included in an upstream release, ⛔ = closed without being
included.

### Upstream PR hygiene

Upstream submissions are prepared from a clean branch based on the current
upstream `main`, with one focused commit whenever possible. Never use the
fork's development `main` as a PR head. Before opening a PR, compare the
branch with upstream and remove fork-only changelog entries, internal issue or
bead references, AI/co-author attribution, private deployment/provider data,
hostnames, IPs, credentials, and unrelated tooling. Test data and URLs must be
synthetic placeholders. If upstream already contains equivalent or evolved
work, update the existing changelog entry with that PR/release reference
instead of opening a duplicate request.

## 2026-09-17

- ✅ Series refreshes now preserve the canonical row already attached to a
  provider's source ID, including when that provider is a secondary source.
  This fixes a regression that could reassign a source during a refresh and
  leave a duplicate zero-source series row in Metadata Review or Duplicate
  Finder. Successful imports now also purge existing rows with neither a
  provider source nor playable episode source; normal newly listed series
  awaiting episode detail are retained.

- ✅ Metadata Review now calls out likely existing catalog matches when a row is
  expanded. Same-title rows from another provider show their year, source
  count, and match reason. Choosing a candidate with a confirmed TMDB ID
  applies its identity/year and immediately uses the existing merge-safe
  resolver; no second Duplicate Finder action is required. Rows without an ID
  remain informational so title-only matches are never auto-merged.

- ✅ Automatic post-import processing is now **import-first and provider-safe**.
  Imported TMDB IDs continue through the bounded TMDB metadata pass; titles
  without an ID remain in Metadata Review instead of triggering automatic
  `get_vod_info` or `get_series_info` requests. Provider detail and episode
  discovery remain explicit, user-initiated actions. This prevents a normal
  catalog import from turning into thousands of provider calls.

- ✅ Bulk enrichment now excludes disabled providers at selection time, even
  when old source rows still reference them. Progress totals are scoped to
  active providers, so an inactive provider cannot continue receiving requests
  or inflate the displayed workload. Validated against a live disabled-provider
  cleanup and a separate multi-provider import test.

- ✅ Stale bulk-enrichment progress is cleared when a new import or automatic
  workflow starts, and cancelled runs no longer leave old totals and completion
  time visible. This prevents one provider's previous run from being mistaken
  for the current import.

- ✅ The sidebar now displays the manager host's current external IP using a
  cached, authenticated lookup. The value refreshes periodically and shows
  unavailable cleanly when the lookup service cannot be reached.

- ✅ Provider sources now record `provider_detail_deferred` explicitly. A
  source imported without a TMDB ID is marked for later review rather than
  being silently treated as an automatic enrichment failure; approving an ID
  or completing explicit provider detail clears the marker. This leaves a
  durable hook for future review and on-demand actions.

## 2026-09-15

- ✅ Bulk enrichment now has a cooperative **Cancel enrichment** action in the
  UI and API. Cancellation stops scheduling new provider-detail requests while
  allowing the current request to finish, so a large fallback pass can be
  halted without restarting the container. Added cancellation regression tests.

- ✅ XC movie imports now capture TMDB identity from either the provider's
  `tmdb` or `tmdb_id` bulk-list field. This prevents valid WarpTV-style IDs from
  being mistaken for missing identities and triggering thousands of unnecessary
  provider-detail requests.

- ✅🔀 Cross-provider matches now preserve an existing automatic archive by
  default. If a movie or series was archived and a later provider supplies the
  same title, that source is attached without resurrecting the catalog item;
  only an exact re-import of the already-known source can clear an automatic
  archive. Manual archive decisions remain protected. Regression coverage now
  includes both movies and series. Submitted as clean upstream PR
  [#26](https://github.com/jstevenscl/vod-manager/pull/26) after confirming the
  behavior is not covered by v0.2.15/v0.2.16.

- ✅ Undated cross-provider movie/series cards now inherit a confirmed TMDB
  identity when exactly one normalized-title candidate already exists in the
  pool. This prevents a second provider from creating a duplicate Metadata
  Review row for an already-fixed title; ambiguous candidates remain manual.
  Regression tests cover both safe inheritance and ambiguity protection.

- ✅⛔ Provider-supplied `trailer`/`youtube_trailer` values are now preserved
  during movie and series imports and exposed through Dispatcharr list/detail
  responses. This avoids unnecessary TMDB/YouTube requests and keeps the
  provider's own verified trailer reference. Synthetic movie/series fixtures
  cover persistence and exclusion of already-saved values from the review
  queue. PR [#25](https://github.com/jstevenscl/vod-manager/pull/25) was closed
  because the submitted branch contained the full fork history and unrelated
  files. Upstream will review the current behavior directly; no duplicate PR
  should be opened until a clean focused branch is prepared.
  Status: closed without inclusion in [#25](https://github.com/jstevenscl/vod-manager/pull/25).

- ✅ Provider imports now have one shared **Catalog workflow** handoff instead
  of leaving people to infer readiness from separate progress bars. The
  centered header advances through queued import, TMDB identity resolution,
  provider enrichment, and safe duplicate reconciliation; only after every
  automatic phase finishes does it show **Import complete · Catalog ready for
  review**. That ready banner includes the visible (non-adult) movie/TV
  identity-review counts and incorrect-TMDB-ID count. The sidebar now gives a
  clickable review order: Metadata Review, Incorrect TMDB IDs, remaining
  ambiguous duplicates, then missing artwork.

- ✅ Known-TMDB movie cards now receive the same final database collision
  sweep as series. A newly imported movie that already has a valid TMDB ID no
  longer misses auto-merge merely because it did not require provider-detail
  enrichment; the normal same-language and explicit-ignore safeguards still
  apply. This closes the live multi-provider import case where exact-TMDB
  movie pairs remained in Duplicate Finder after all progress bars completed.

- ✅🔀 Provider-free TMDB enrichment for known series IDs now also records TMDB's
  first-air year. A confirmed series no longer remains falsely held in
  **Metadata Review** merely because its provider omitted a year; the existing
  held records are safely picked up and backfilled on the next enrichment run.
  The final series reconciliation also discovers current exact-TMDB-ID
  collision groups directly from the database, so a card created during a
  coalesced import cannot be stranded in Duplicate Finder. It retains the
  existing same-language and explicit-ignore safeguards, so different-language
  variants and human decisions remain untouched. A reviewer-selected TMDB ID
  in Metadata Review now invokes that same safe merge path immediately rather
  than waiting for the next import.
  This was included, with upstream hardening, in v0.2.16 via
  [#24](https://github.com/jstevenscl/vod-manager/pull/24).

- ✅ Metadata Review now has an **Incorrect TMDB IDs** tab for titles whose
  stored ID receives a confirmed TMDB 404. It supports a pending-ID scan,
  per-title TMDB search and correction/clear actions, and high-confidence
  bulk AI correction. The issue and content-type controls are grouped with a
  plain-language key; adult filtering is shown only for Missing identity and
  states both the hidden-adult and visible-item counts. Adult titles are
  excluded from repeat pending-ID enrichment; Activity and Failed Streams no
  longer crowd the Metadata Review page.

- ✅ Manual Plex, Emby, Jellyfin, and DVR imports now update the shared
  sidebar lifecycle from queued to running and then finished/failed, matching
  XC imports. This makes a queued non-XC import visibly confirm that its
  background worker started instead of appearing to remain idle.

- ✅ Metadata Review now renders its queue in 50-item pages instead of
  mounting every review row at once. This keeps the page responsive when a
  provider has a large adult or unresolved-title queue; switching Movies/TV
  Shows and using Hide adult titles no longer creates thousands of row
  components in one browser frame. Selection is intentionally scoped to the
  visible page.

- ✅ XC provider refreshes now apply a source-level catalog delta after fetching
  the required full upstream snapshot. New, changed, and removed provider
  sources still follow the normal import/reconciliation workflow, but an
  unchanged source is no longer rewritten just to confirm it remains
  available. Smart-category evaluation and post-import enrichment now run
  only when a refresh actually changes the catalog, reducing SQLite writes,
  CPU work, and contention during ordinary daily refreshes.

- ✅ Provider catalog imports now queue in the background instead of holding
  the browser request open for the full catalog pass. Imports are serialized,
  their large record-normalization work no longer occupies the API event loop,
  and post-import enrichment waits until the manual import queue drains. The
  sidebar reports queued/running work, so Metadata Review and other pages stay
  usable while a provider refresh is underway.

- ✅ Provider catalog refreshes now reconcile removed content as well as new
  content: a source that is no longer advertised by a provider is removed,
  while the same movie or series remains available whenever another provider
  still has a source. Stale episode streams are also removed only after that
  provider has no remaining source for the series, preserving valid fallback
  playback. Validated with isolated catalog snapshots covering removed,
  retained alternate-provider, and stale-episode sources, plus a mocked
  provider-import flow; no live provider catalog was modified for the test.

## 2026-09-14

- ✅🔼 **Live on the server (`f0b9c0b`):** The sidebar Status card now
  reports active bulk AI review jobs (for example, `AI review: 13/55`) and
  switches to its live polling interval while they run, instead of incorrectly
  displaying Idle.

- ✅🔼 **Live on the server (`a77d9fc`):** Metadata Review now supports a
  Hide adult titles filter, filtered Select all, Archive selected, and an explicit
  Bulk resolve with AI action for both movies and TV shows. AI runs only on
  the titles the reviewer selected and writes a TMDB ID/year only for a
  high-confidence result; unresolved items remain for manual review. Each row
  also accepts a direct TMDB ID, with an optional year for the safe
  identity/merge path.

- ✅🔼 **Follow-up shipped in beta image (`4db93be`):** Corrected the initial
  Metadata Review filter to show only actionable no-identity records (both
  TMDB ID and release year absent), plus explicitly held ambiguous-year rows.
  A provider omitting only a release year is common and is not a repair queue;
  this prevents a normal catalog from producing thousands of false positives.

- ✅🔼 **Shipped in beta image (`e9c40de`):** Added a dedicated **Metadata
  Review** workspace under Operations, so TMDB
  corrections no longer need to live in Curation or a library modal. It lists
  active movies and TV shows missing both a TMDB ID and release year, plus the
  existing ambiguous-year hold queue. A reviewer searches TMDB and explicitly
  selects the result; that records its ID/year and safely merges
  sources/categories only when the corrected pool identity already exists.
  The queue's selection criteria and existing reconciliation/enrichment
  regressions were validated in 7 automated tests; the frontend production
  build also passed. Browser validation reached the first-run screen from a
  static build; the authenticated, data-backed queue must be checked after
  deployment.

- ✅🔼 **Shipped in beta image (`245f6a0`):** The sidebar now includes a compact
  live Status card below Configuration. It reports the importing provider,
  TMDB/enrichment progress, idle state, and app-process CPU sampling without
  requiring users to leave their current page. Python compilation, 6 focused
  backend regression tests, and the frontend production build passed; live
  workload states require an actual import/enrichment run to observe.

- ✅ Imports now run a TMDB-first canonical metadata pass for series with an
  imported TMDB ID before provider episode discovery. It uses one bounded,
  batched TMDB lookup per canonical series to set the user-visible TMDB title
  and US content rating, while retaining every provider's original title in
  its source row. Provider suffixes therefore stay available for provenance
  without replacing clean card titles, and the existing language-aware merge
  guard still keeps correctly classified EN/ES/IT siblings separate.

- ✅🔼 Bulk enrichment now divides its request-concurrency budget among only
  providers that actually have pending movies or series sources. Previously,
  empty configured providers could consume a fairness share: with five
  configured providers and only one needing work, an 8-request budget was
  reduced to one request at a time. Active providers still retain their own
  adaptive rate limiter, backoff handling, and isolated movie-then-series
  lanes.

- ✅🔼 Fixed automatic startup recovery for pending series episode discovery
  after the active-provider concurrency change. Providers with already
  completed or review-excluded series no longer count as pending work; the
  remaining source-level episode work resumes normally after a restart.

- ✅🔼 Automatic series episode discovery now tracks and processes every
  retained source variant, rather than stopping after the first source on a
  canonical series card. This preserves episode-stream fallbacks when one
  provider supplies multiple variants of the same show, and the progress
  counter now reflects pending sources rather than only canonical titles.

- ✅🔼 The Curation page now notices the automatic handoff from the TMDB-ID
  metadata pass to provider fallback/series episode work without requiring a
  manual browser refresh. While idle it checks for server-started enrichment
  every 10 seconds, then returns to its existing 2-second live progress
  updates once work begins.

- ✅ Bulk movie-enrichment writes now commit up to 250 completed movies per
  SQLite transaction (previously 25). The single global writer, FIFO queue,
  per-item savepoints, and bounded transactions remain in place, reducing
  commit/fsync overhead on large imports without allowing concurrent database
  writes or holding one catalog-sized transaction open.

- ✅ Imports now queue a provider-free TMDB metadata pass for movies that
  already include a TMDB ID in the catalog list. It has its own visible
  progress indicator and batch-writes results to reduce database contention.
  Provider detail calls are deferred to genuinely unmatched new movies, while
  series detail remains dedicated to episode discovery. Normal catalog
  refreshes no longer re-fetch stable movie metadata or previously discovered
  episode lists merely because a timer elapsed.

- ✅🔀 XC movie catalog imports now retain the provider's bulk artwork URL
  (`stream_icon`) immediately, matching the existing series-cover behavior.
  Movie cards no longer need an expensive per-title enrichment request merely
  to display a poster; enrichment and TMDB remain fallbacks for missing or
  improved artwork. The importer keeps the first usable poster for a
  canonical movie, so alternate source variants cannot cause artwork to
  flip on later refreshes.
  ([#23](https://github.com/jstevenscl/vod-manager/pull/23))

- ✅ Bulk enrichment no longer creates a task for every movie/series in a
  provider's catalog up front. It used to launch all of them at once (a
  full-catalog provider could mean tens of thousands queued simultaneously)
  even though only a handful actually run at a time — the rest just sat in
  memory adding scheduling overhead. Enrichment now uses a fixed-size pool
  of workers (matching the existing concurrency limit) that pull items one
  at a time from a queue. Same enrichment speed and same number of items
  running at once, just without the up-front pile-up.

- ✅ Fixed bulk enrichment writing every movie's enrichment result to the
  database in its own transaction, which could stall other providers'
  enrichment lanes with "database is locked" errors during a full bulk run.
  Movie writes are now batched (25 per transaction) instead of one write per
  movie. Verified with a full-catalog live dry-run (56,978 movies, 5
  providers, concurrency 8): zero lock errors, zero enrichment errors.
- ✅🔼 Fixed bulk series enrichment silently under-processing a provider's
  series: a newly imported series wasn't selected for that provider's
  enrichment phase until it had already been enriched by that provider at
  least once, which meant series that most needed enrichment could be
  permanently skipped by every bulk run. The per-provider series list now
  uses the provider-membership data recorded at import time instead of
  requiring prior episode data to already exist.
- ✅🔼 Fixed bulk enrichment's end-of-run duplicate-merge sweep spiking host
  CPU to 1200%+ (near-total saturation on a 14-core host) for the duration
  of the sweep. It was launching one background thread per affected title
  instead of merging sequentially, which added no real speed (every merge
  already had to wait its turn for the database anyway) but generated heavy
  thread-scheduling overhead at full-catalog scale. Merges now run
  sequentially in a single background task; total merge sweep work is
  unchanged, just without the thread pile-up.
- ✅🔼 Fixed a provider's bulk series-enrichment lane also fetching series
  metadata from OTHER providers whenever a series was carried by more than
  one provider (e.g. matched by name/year across two catalogs). This
  weakened each provider's intended request/concurrency isolation and
  backoff handling during a bulk run. Each provider's lane now fetches only
  its own series source.
- ✅🔼 Bulk enrichment now serializes every movie/series database write for
  the whole run through one background writer task instead of each
  provider's movie or series phase independently reaching its own
  batch-commit point. Since different providers' phases run concurrently by
  design, two providers could previously flush to SQLite at the same
  instant; now only one write is ever in flight at a time, regardless of
  how many provider lanes are enriching concurrently. Single/on-demand
  enrich calls outside a bulk run are unaffected.
- ✅🔼 Pooled per-provider HTTP connections are now explicitly closed at
  application shutdown, and whenever a provider is deleted or its connection
  settings (base URL, username/password, custom user agent) change. Before,
  a stale pooled connection could keep being reused under old credentials
  (or against a provider that no longer exists) until an unrelated failure
  happened to evict it, which might never happen for a deleted provider.

## 2026-09-13

- ✅🔼 Fixed a regression (introduced earlier today) where the automatic
  TMDB-ID merge could merge a movie or series with its different-language
  sibling (e.g. an EN card and its ES card) whenever that other language
  wasn't in your enabled playback languages. This is what was causing the
  "Movie language split" maintenance tool to keep finding thousands of
  movies to fix every single day even after running it — the auto-merge was
  quietly re-merging them right back. Existing content that was already
  incorrectly merged by this bug is not automatically un-merged; use the
  Duplicate Finder / language-split maintenance tool to split any titles
  that still show up mixed-language after updating.
  ([#21](https://github.com/jstevenscl/vod-manager/pull/21))
- ✅🔼 Movies/series whose content isn't in any of your enabled playback
  languages are now automatically archived (not deleted) instead of just
  quietly hidden from playback while still showing up everywhere else.
  This is a one-time catch-up for anyone who was already running before
  today's language-merge fix above — those titles had been accumulating
  without ever getting flagged for review. Runs automatically after each
  provider scan; re-enabling a language later automatically un-archives
  anything that qualifies again. A title you've manually archived or
  unarchived yourself is never touched by this.
  ([#22](https://github.com/jstevenscl/vod-manager/pull/22))
- ✅🔼 Movies and series that end up sharing the same TMDB ID after enrichment
  are now merged automatically, instead of sitting side-by-side as duplicates
  until someone merges them by hand in the Duplicate Finder.
  ([#20](https://github.com/jstevenscl/vod-manager/pull/20))
- ✅🔼 Series metadata lookups can now fail over between multiple providers
  instead of giving up when the primary provider doesn't have a match, the
  same way movie lookups already could.
  ([#19](https://github.com/jstevenscl/vod-manager/pull/19))

## 2026-09-10

- ✅🔼 Fixed a crash (`FOREIGN KEY constraint failure`) that could occur when
  auto-merge encountered a cycle of duplicate rows all sharing the same TMDB
  ID during a batch merge.
  ([#18](https://github.com/jstevenscl/vod-manager/pull/18))
- ✅🔼 Fixed archived movies/series occasionally re-appearing in normal category
  listings after being archived.
  ([#17](https://github.com/jstevenscl/vod-manager/pull/17))

## 2026-09-09 – 2026-09-10

- ✅🔼 Duplicate Finder: fixed matches being missed when one of the two
  candidate rows has no release year, and fixed a blank `()` showing in the
  UI when a candidate has no year.
  ([#16](https://github.com/jstevenscl/vod-manager/pull/16))
- ✅🔼 Duplicate Finder: improved name normalization to correctly strip
  language/quality prefixes (e.g. `FR -`, `RU -`) and country suffixes before
  comparing titles, so more real duplicates are found and fewer false
  positives are flagged.
  ([#16](https://github.com/jstevenscl/vod-manager/pull/16))
- ✅🔼 Fixed a rare false-archive of French/Russian-prefixed titles caused by
  the dash-prefix detector, and added a guard against hitting TMDB's rate
  limit during bulk lookups.
  ([#14](https://github.com/jstevenscl/vod-manager/pull/14))

## 2026-09-08

- ✅🔼 Added a "Bulk Apply TMDB Titles" action, plus better progress/resume
  visibility and clearer error reporting during bulk operations.
  ([#13](https://github.com/jstevenscl/vod-manager/pull/13))
- ✅🔼 Raised TMDB year-lookup concurrency for faster bulk enrichment.
  ([#12](https://github.com/jstevenscl/vod-manager/pull/12))
- ✅🔼 Fixed high CPU usage during import and improved movie/series duplicate
  matching accuracy.
  ([#11](https://github.com/jstevenscl/vod-manager/pull/11))
- ✅ Reused persistent HTTP connections for provider/TMDB calls, raised TMDB
  request concurrency further, and added automatic backoff when TMDB starts
  rate-limiting.

## Earlier

Everything before 2026-09-08 predates this changelog. See the git history for
the full record.
