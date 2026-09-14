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

✅ = shipped and running in this fork's image. Where a change has also been
proposed to the upstream project, the entry links to that pull request —
🔀 = open/under review upstream, not yet merged.

## 2026-09-14

- ✅ Imports now queue a provider-free TMDB metadata pass for movies that
  already include a TMDB ID in the catalog list. It has its own visible
  progress indicator and batch-writes results to reduce database contention.
  Provider detail calls are deferred to genuinely unmatched new movies, while
  series detail remains dedicated to episode discovery. Normal catalog
  refreshes no longer re-fetch stable movie metadata or previously discovered
  episode lists merely because a timer elapsed.

- ✅ XC movie catalog imports now retain the provider's bulk artwork URL
  (`stream_icon`) immediately, matching the existing series-cover behavior.
  Movie cards no longer need an expensive per-title enrichment request merely
  to display a poster; enrichment and TMDB remain fallbacks for missing or
  improved artwork. The importer keeps the first usable poster for a
  canonical movie, so alternate source variants cannot cause artwork to
  flip on later refreshes.

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
- ✅ Fixed bulk series enrichment silently under-processing a provider's
  series: a newly imported series wasn't selected for that provider's
  enrichment phase until it had already been enriched by that provider at
  least once, which meant series that most needed enrichment could be
  permanently skipped by every bulk run. The per-provider series list now
  uses the provider-membership data recorded at import time instead of
  requiring prior episode data to already exist.
- ✅ Fixed bulk enrichment's end-of-run duplicate-merge sweep spiking host
  CPU to 1200%+ (near-total saturation on a 14-core host) for the duration
  of the sweep. It was launching one background thread per affected title
  instead of merging sequentially, which added no real speed (every merge
  already had to wait its turn for the database anyway) but generated heavy
  thread-scheduling overhead at full-catalog scale. Merges now run
  sequentially in a single background task; total merge sweep work is
  unchanged, just without the thread pile-up.
- ✅ Fixed a provider's bulk series-enrichment lane also fetching series
  metadata from OTHER providers whenever a series was carried by more than
  one provider (e.g. matched by name/year across two catalogs). This
  weakened each provider's intended request/concurrency isolation and
  backoff handling during a bulk run. Each provider's lane now fetches only
  its own series source.
- ✅ Bulk enrichment now serializes every movie/series database write for
  the whole run through one background writer task instead of each
  provider's movie or series phase independently reaching its own
  batch-commit point. Since different providers' phases run concurrently by
  design, two providers could previously flush to SQLite at the same
  instant; now only one write is ever in flight at a time, regardless of
  how many provider lanes are enriching concurrently. Single/on-demand
  enrich calls outside a bulk run are unaffected.
- ✅ Pooled per-provider HTTP connections are now explicitly closed at
  application shutdown, and whenever a provider is deleted or its connection
  settings (base URL, username/password, custom user agent) change. Before,
  a stale pooled connection could keep being reused under old credentials
  (or against a provider that no longer exists) until an unrelated failure
  happened to evict it, which might never happen for a deleted provider.

## 2026-09-13

- ✅ Fixed a regression (introduced earlier today) where the automatic
  TMDB-ID merge could merge a movie or series with its different-language
  sibling (e.g. an EN card and its ES card) whenever that other language
  wasn't in your enabled playback languages. This is what was causing the
  "Movie language split" maintenance tool to keep finding thousands of
  movies to fix every single day even after running it — the auto-merge was
  quietly re-merging them right back. Existing content that was already
  incorrectly merged by this bug is not automatically un-merged; use the
  Duplicate Finder / language-split maintenance tool to split any titles
  that still show up mixed-language after updating.
- ✅ Movies/series whose content isn't in any of your enabled playback
  languages are now automatically archived (not deleted) instead of just
  quietly hidden from playback while still showing up everywhere else.
  This is a one-time catch-up for anyone who was already running before
  today's language-merge fix above — those titles had been accumulating
  without ever getting flagged for review. Runs automatically after each
  provider scan; re-enabling a language later automatically un-archives
  anything that qualifies again. A title you've manually archived or
  unarchived yourself is never touched by this.
- ✅🔀 Movies and series that end up sharing the same TMDB ID after enrichment
  are now merged automatically, instead of sitting side-by-side as duplicates
  until someone merges them by hand in the Duplicate Finder.
  ([#20](https://github.com/jstevenscl/vod-manager/pull/20))
- ✅🔀 Series metadata lookups can now fail over between multiple providers
  instead of giving up when the primary provider doesn't have a match, the
  same way movie lookups already could.
  ([#19](https://github.com/jstevenscl/vod-manager/pull/19))

## 2026-09-10

- ✅ Fixed a crash (`FOREIGN KEY constraint failure`) that could occur when
  auto-merge encountered a cycle of duplicate rows all sharing the same TMDB
  ID during a batch merge.
- ✅ Fixed archived movies/series occasionally re-appearing in normal category
  listings after being archived.

## 2026-09-09 – 2026-09-10

- ✅ Duplicate Finder: fixed matches being missed when one of the two
  candidate rows has no release year, and fixed a blank `()` showing in the
  UI when a candidate has no year.
- ✅ Duplicate Finder: improved name normalization to correctly strip
  language/quality prefixes (e.g. `FR -`, `RU -`) and country suffixes before
  comparing titles, so more real duplicates are found and fewer false
  positives are flagged.
- ✅ Fixed a rare false-archive of French/Russian-prefixed titles caused by
  the dash-prefix detector, and added a guard against hitting TMDB's rate
  limit during bulk lookups.

## 2026-09-08

- ✅ Added a "Bulk Apply TMDB Titles" action, plus better progress/resume
  visibility and clearer error reporting during bulk operations.
- ✅ Raised TMDB year-lookup concurrency for faster bulk enrichment.
- ✅ Fixed high CPU usage during import and improved movie/series duplicate
  matching accuracy.
- ✅ Reused persistent HTTP connections for provider/TMDB calls, raised TMDB
  request concurrency further, and added automatic backoff when TMDB starts
  rate-limiting.

## Earlier

Everything before 2026-09-08 predates this changelog. See the git history for
the full record.
