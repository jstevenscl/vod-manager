## Summary

Batched rewrite of the VOD provider import write path, plus supporting changes for
safe rollout and benchmarking.

1. **Batch `bulk_import_movies`/`bulk_import_series`** — chunked bulk-fetch +
   in-memory diff + batched write, replacing per-item SQL round trips (`5e2f05e`)
2. **Add per-build image tag; split fetch vs. DB-write import timing** (`0945bc8`)

---

## 1. Batch `bulk_import_movies`/`bulk_import_series` for large VOD catalogs

**Commit:** `5e2f05e`
**Files modified:** `backend/vod_db.py`

### Problem
Provider catalog imports were slow at scale (see beads-otn). Both bulk-import
functions ran one SQL round trip (SELECT, then INSERT or UPDATE) per catalog item,
so import time scaled linearly with per-item DB latency rather than batch size.

### Root cause
`bulk_import_movies`/`bulk_import_series` looped over every item doing a live
per-item SELECT to find an existing row, then a per-item INSERT or UPDATE — the
same pattern Dispatcharr moved away from in its own bulk-import path.

### Fix
Rewrote both functions to process items in chunks of 1000:
- One bulk `SELECT ... WHERE x IN (...)` per chunk (batched further into
  sub-groups of 900 to stay under SQLite's variable-count limit) to prefetch
  everything the chunk could match against, instead of one SELECT per item.
- Per-item matching against the in-memory prefetch snapshot, preserving the
  exact original decision tree: primary match by provider stream/series id,
  blank-name items always flagged for review, exact name+year match, ambiguous
  same-name/no-year candidates flagged rather than guessed, otherwise insert.
- Newly-queued inserts are immediately registered into the in-memory lookup
  (via a negative placeholder id) so two new items sharing a name/year *within
  the same chunk* correctly match each other instead of both being inserted as
  separate duplicate rows — a regression risk specific to a snapshot-based
  rewrite that doesn't exist in the original per-item-live-query code, caught
  in review and fixed before merge.
- Batched `INSERT`/`UPDATE` execution per chunk instead of per item; the
  write-lock is released/reacquired once per chunk instead of once per 25 items.

No change to matching keys, dedup priority, or any upgrade-only field semantics
(`is_adult`, `tmdb_id`, archive/unarchive, `needs_year_review` flagging) — only
the write mechanics changed.

### Before / After

| | Before | After |
|---|---|---|
| DB round trips per item | ~2 (1 SELECT + 1 INSERT/UPDATE) | ~0.002 (batched 1000-wide) |
| 50,000-item synthetic create benchmark | _(not benchmarked — no equivalent old-path number captured)_ | ~1.60s (~31,200 items/sec) |
| 50,000-item synthetic re-import (all match, no-op) benchmark | _(not benchmarked)_ | ~0.92s (~54,300 items/sec) |
| **Real provider import — total wall-clock** | _TODO: capture pre-deploy baseline_ | _TODO: capture post-deploy result_ |
| **Real provider import — DB-write phase only** | _TODO_ | _TODO (see fix #2 below for the log line that reports this split)_ |

### Verification
Verified via a throwaway test script (schema-accurate, built through
`vod_db.init_db()` against a temp SQLite DB, not a hand-rolled mock) covering:
same-chunk name+year duplicate collapse, same-chunk ambiguous (year=None)
duplicate collapse, re-import matching via provider stream/series id,
blank-name-never-matches, ambiguous-candidate flagging, and archive/unarchive
in both directions — all passed. `bulk_import_series` additionally verified
for `import_provider_id` backfill on previously-orphaned rows and `tmdb_id`
coalesce-only behavior across repeated imports with different values.

**Not yet done**: functional validation against a real, live provider catalog
— pending the user adding 2 currently-unconfigured provider accounts.

---

## 2. Add per-build image tag; log fetch vs. DB-write import timing

**Commit:** `0945bc8`
**Files modified:** `.github/workflows/docker-build.yml`, `backend/vod_importer.py`

### Problem
`docker-build.yml` only tagged images by branch (`main`/`latest`), both of which
get overwritten on every push — there was no way to redeploy a specific prior
build if a new one turned out broken. Separately, there was no way to measure
fix #1's actual impact on a real import without conflating DB-write time with
unrelated provider network/fetch latency.

### Fix
- Every push to `main`/`dev` now also publishes a permanent
  `ghcr.io/knmplace/vod-manager:<branch>-<short-sha>` tag alongside the
  existing moving `main`/`latest` tags, so a known-good build stays pullable
  by tag even after later pushes move `main`/`latest` forward.
- `_import_movies_for_provider`/`_import_series_for_provider` in
  `vod_importer.py` now time the provider fetch and the `bulk_import_*` DB
  write separately and log both:
  `[vod_importer] provider=... movies: {...} (fetch=Xs db_write=Ys items=N)`

### Files changed
- `.github/workflows/docker-build.yml` — `Determine tags` step
- `backend/vod_importer.py` — `_import_movies_for_provider`,
  `_import_series_for_provider`

### Verification
`py_compile` clean. Not yet exercised against a live import run.

---

## Testing performed

- **Fix #1**: Synthetic verification scripts (schema-accurate temp DB) covering
  all dedup/matching/flagging edge cases plus a 50K-item performance benchmark.
  Real-provider before/after benchmark is the open item for this PR — see
  `fetch=`/`db_write=` log line added in fix #2.
- **Fix #2**: `py_compile` clean. Not yet exercised against a live import run.

## Not included in this PR

- Real-provider before/after benchmark numbers — to be filled in once deployed
  and run against the 2 pending provider accounts.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
