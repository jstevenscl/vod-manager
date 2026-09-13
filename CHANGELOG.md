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
