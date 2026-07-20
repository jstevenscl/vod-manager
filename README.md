# VOD Manager

Curates movies and TV shows from multiple real sources — Xtream-Codes (XC)
IPTV providers, Plex, and Emby/Jellyfin — into one deduplicated pool, then
re-exposes that pool as its own XC-compatible catalog server so one or more
Dispatcharr instances can pull it like any other provider.

Same real content is often available from several sources at once (a movie
on both an XC reseller and your own Plex library, or the same title from two
different resellers). VOD Manager treats those as multiple *sources* for one
pool entry rather than duplicate entries, and automatically fails over
between them if one goes down or hits its connection limit.

## Requirements

- Docker + Docker Compose
- `ffmpeg` (already included in the Docker image — nothing to install
  separately)
- At least one real VOD source: an XC-type IPTV provider, a Plex server, or
  an Emby/Jellyfin server
- One or more Dispatcharr instances to pull the resulting catalog into
- A free [TMDB](https://www.themoviedb.org/settings/api) API key (v3 auth)
  — used for enrichment and the year-review disambiguation flow, not
  required for basic operation

## Quick start

```bash
docker compose up -d
```

The app listens on port `8282`. First run walks you through:

1. **Connect Dispatcharr** — VOD Manager needs its own Dispatcharr
   connection (URL + API token) to push provider connection limits and
   check live-TV viewer counts. This can be a different instance from the
   ones actually pulling VOD content from it (see *Connecting multiple
   Dispatcharr instances* below).
2. **Set an admin username/password** — this is VOD Manager's own login,
   separate from Dispatcharr's.

From there, add your real providers (Providers section) and import their
catalogs.

## Connecting Dispatcharr instances

VOD Manager distinguishes two separate relationships with Dispatcharr:

- **Connected Instances** — *who's allowed to pull from VOD Manager.* Each
  Dispatcharr instance gets its own auto-generated, high-entropy
  username/password pair. Use VOD Manager's own URL as the `server_url` on
  an XC-type M3U account in that Dispatcharr instance, with the generated
  credentials.
- **Dispatcharr Connections** — *who VOD Manager itself reaches out to.*
  Used to push each provider's connection limit into Dispatcharr's own
  admission control, and to check real-time live-TV viewer counts for
  shared-connection-limit coordination (see below).

A single Dispatcharr instance is usually both at once (it pulls from VOD
Manager *and* VOD Manager pushes profile data back to it), but they don't
have to match — you can have Dispatcharr instances that only pull, and
connections VOD Manager only reaches out to for coordination.

**Adding a new instance** (Settings → Dispatcharr Connections → "Connect a
new instance"): give it that instance's own admin API token and VOD
Manager's own URL as reachable *from that instance* — this isn't always the
same URL you're viewing VOD Manager at yourself (a co-located instance
might use a Docker-internal hostname; a remote one needs your real public
URL). VOD Manager then automatically creates the client credentials and the
Dispatcharr-side M3U account for you. The only thing left is on
Dispatcharr's own side: enable VOD on the new account and pick which
groups/categories to turn on — normal setup for any source, regardless of
how the account got created.

## Per-instance category access control

Dispatcharr has no per-user/per-profile VOD split of its own — once content
is pulled in through a Connected Instance's M3U account, every Dispatcharr
user on that instance sees the identical catalog. For real per-audience
control (e.g. a kids-only client, or handing a limited catalog directly to
an end-user IPTV app like TiviMate or IPTV Smarters instead of routing it
through Dispatcharr at all), restrict a specific Connected Instance's
credential to a set of categories under Settings → Connected Instances →
*Category access*. Left as "— all —" (the default), a client sees the whole
pool, matching every existing credential's behavior today. Restricting it
is enforced everywhere that credential is used — catalog listing, info
lookups, and the actual stream — not just hidden from the browse UI, so a
restricted client can't reach disallowed content even with a direct/copied
stream URL.

## In-app test player

Movies, series episodes, and Needs Review items all have a Play button that
opens a lightweight in-app player — meant for verifying imports (matched the
right title, source actually plays, etc.), not real end-user viewing (real
viewers watch through Dispatcharr with an external player, which never goes
through this player at all). Direct playback works for anything a stock
`<video>` element can decode natively; two fallbacks cover the rest, both
re-encoding the source with ffmpeg on the fly rather than just relaying it:

- **Transcoded** — fast to start, but forward-only (no mid-stream
  scrubbing). "Jump to" buttons restart the stream partway in instead.
- **HLS (seekable)** — real seek support via a proper HLS playlist +
  segments, at the cost of a slower start (ffmpeg has to produce a first
  segment before anything plays) and using somewhat more CPU/disk while
  active. Backward seek works across everything encoded so far; seeking
  past the live edge is naturally blocked until ffmpeg catches up, the same
  limitation any in-progress live/event HLS stream has.

Both fallbacks (and every other player-facing route) are torn down when a
session ends — closing the player, an idle timeout with no further
requests, or a Kill from Activity below all release the encoder process and
any on-disk segments.

## Shared connection-limit coordination

If a real provider also has its own native live-TV account somewhere in
Dispatcharr (common — the same IPTV subscription usually serves both live
channels and VOD), live TV and VOD Manager's own usage draw from the same
real connection pool without either side knowing about the other by
default. Configure it under Providers → *Shared Limit / Live Accounts*:

- **Shared Limit** — the provider's real total connection cap (from your
  subscription).
- **Live accounts** — link the provider to its native live-TV account on
  each Dispatcharr connection that has one. A provider can have a different
  live-TV account on more than one Dispatcharr instance; all of them count
  toward the same real limit.

VOD Manager checks the current combined usage (its own active streams +
every linked live account's viewer count) before opening a new stream
against that provider, and fails over to the next available source instead
of exceeding the real limit.

## Security and deployment

- **Don't expose this to the public internet without TLS in front of it.**
  The app itself doesn't terminate TLS — put a reverse proxy or VPN/tunnel
  (Cloudflare Tunnel, WireGuard, etc.) in front if it needs to be reachable
  from outside your own network. This matters more than usual here: the XC
  protocol itself has no concept of session auth beyond a username/password
  in the URL, checked on every request — that's a real, if unavoidable,
  weak point once anything is internet-facing.
- **Every connected instance gets its own credential**, not a shared one —
  Settings → Connected Instances. Revoke or regenerate one without
  affecting the others if a specific instance's credential is ever
  compromised.
- **Per-IP lockout** on the XC login: repeated failed attempts from one
  address lock it out temporarily (configurable in Settings → Security;
  changes apply within ~30s). This slows down automated brute-forcing but
  doesn't replace putting this behind a VPN/tunnel if it's ever going to be
  reachable beyond a trusted network.
- **Optional per-instance IP allowlist** — if a specific connected
  instance's source IP is known and stable, you can lock its credential to
  that IP as an extra layer. Leave it blank for instances behind
  CGNAT/rotating IPs (locking those would just break them, not add real
  security, since the address isn't a reliable identity signal for them
  anyway).
- Lockout state is in-memory and resets on every container restart — this
  slows down a sustained automated attacker, but a restart-and-retry
  attacker is a much smaller threat than an internet-facing app with no
  lockout at all.

## Refresh schedule

Settings → Refresh Schedule controls how often background work runs:

- **Catalog refresh** — how often each provider *type* (XC, Plex, Emby,
  Jellyfin) gets automatically re-imported, each on its own interval.
  Plex/Emby libraries can take much longer to scan than a cheap XC catalog
  pull, so they're not forced onto the same cadence. Defaults to 6 hours for
  every type.
- **Enrichment TTL** — how long detail-level metadata (posters, cast, genre)
  is cached before a movie/series is eligible to be refetched. Defaults to
  24 hours.
- **TMDB Lists sync** — how often categories linked to a TMDB List
  auto-resync. Off (manual "Sync now" only) by default — enabling it adds
  new recurring TMDB API traffic.

## Backup and restore

Settings → Backup & Restore lets you download, restore, or reset each piece
of VOD Manager's state independently (config, sessions, the catalog
database) — e.g. reset a corrupted database without touching saved
credentials, or roll back just the config. Database downloads use SQLite's
`VACUUM INTO` for a consistent snapshot even while the app is actively
writing to it.

## Orphan Checker

Settings → Orphan Checker finds dead rows a provider deletion (or a bug)
can leave behind — a series whose only source provider no longer exists,
or movies/episodes with zero sources at all. Run a scan periodically,
especially after removing a provider, and purge what it finds. It won't
flag series with no episodes yet — that's normal for anything not yet
lazily enriched, not broken.
