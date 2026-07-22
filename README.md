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

**New here?** See [USERGUIDE.md](USERGUIDE.md) for a full walkthrough with
screenshots — installation, connecting Dispatcharr (single or multiple
instances), security hardening, and every curation tool. This README is a
concise technical reference for people already up and running.

## Requirements

- Docker + Docker Compose
- `ffmpeg` (already included in the Docker image — nothing to install
  separately)
- At least one real VOD source: an XC-type IPTV provider, a Plex server, or
  an Emby/Jellyfin server
- One or more Dispatcharr instances to pull the resulting catalog into
- A free [TMDB](https://www.themoviedb.org/settings/api) API key (v3 auth)
  — used for enrichment, the year-review/missing-artwork disambiguation
  flows, and TMDB List sync; not required for basic operation
- Optional: an API key from Anthropic, OpenAI, and/or Google (Gemini) for
  the AI-assisted features (any one is enough; more than one lets you
  switch providers without re-entering a key)

## Quick start

```bash
docker compose up -d
```

This pulls the published `ghcr.io/jstevenscl/vod-manager:latest` image (see
`docker-compose.yml`). Building from source instead — e.g. for local
development against this repo — works too:

```bash
docker build -t vod-manager:dev .
```

The app listens on port `8282`. First run asks you to set an admin
username/password (VOD Manager's own login, separate from Dispatcharr's) —
see [USERGUIDE.md](USERGUIDE.md#4-first-run-setup) for why you should set
one rather than skip it.

From there: add your real providers (Curation & Maintenance → Providers)
and import their catalogs, then connect Dispatcharr (below). Full
walkthrough with screenshots in [USERGUIDE.md](USERGUIDE.md).

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

**Adding a new instance** (Configuration → Dispatcharr Connections →
"Connect a new instance"): give it that instance's own admin API token and VOD
Manager's own URL as reachable *from that instance* — this isn't always the
same URL you're viewing VOD Manager at yourself (a co-located instance
might use a Docker-internal hostname; a remote one needs your real public
URL). VOD Manager then automatically creates the client credentials and the
Dispatcharr-side M3U account for you (with a 50-concurrent-stream account-
level cap — generous on purpose, since the real per-provider limits are
enforced separately; see below). The only thing left is on Dispatcharr's
own side: enable VOD on the new account and pick which groups/categories to
turn on — normal setup for any source, regardless of how the account got
created.

## Per-instance category access control

Dispatcharr has no per-user/per-profile VOD split of its own — once content
is pulled in through a Connected Instance's M3U account, every Dispatcharr
user on that instance sees the identical catalog. For real per-audience
control (e.g. a kids-only client, or handing a limited catalog directly to
an end-user IPTV app like TiviMate or IPTV Smarters instead of routing it
through Dispatcharr at all), restrict a specific Connected Instance's
credential to a set of categories under Configuration → Connected Instances
→ *Category access*. Left as "— all —" (the default), a client sees the whole
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

## AI-assisted categories, Needs Review, and Missing Artwork

An API key from **any** of Anthropic, OpenAI, or Google Gemini (Configuration
→ API Keys — configure as many as you have access to, then pick which one
is active) unlocks three assists, none of which ever apply anything
automatically — every one is a suggestion you still review and confirm:

- **Suggest a category with AI** (Categories) — describe a category in
  plain English and the AI proposes a structured filter rule using the
  same fields/ops the manual rule builder uses (name, genre, year,
  country/language, director, is_adult). Good for anything expressible as
  field conditions; review the proposed rule before creating it.
- **AI Evaluate** (✨ button on any category) — for criteria the rule
  fields genuinely can't express (mood, plot, audience fit), the AI judges
  actual titles against a plain-English description instead of matching
  fields. Real per-request API cost, so this always runs over a *bounded*
  candidate set (optionally narrowed first by a rule pre-filter) rather
  than the whole pool — the result always reports how many candidates were
  actually considered vs. left out by the cap, never a silent truncation.
- **Ask AI** (Needs Review, Missing Artwork) — when an item has no year, or
  no confident poster match, and multiple TMDB candidates are ambiguous,
  the AI picks the most likely correct match with its reasoning and a
  confidence level, as an extra hint alongside the normal TMDB suggestion
  list. You still click a candidate yourself to actually resolve it.

See [USERGUIDE.md](USERGUIDE.md#10-curation-tools) for the full set of
curation tools (Missing Artwork, Language Filter, Duplicate Finder, Needs
Review, Orphan Checker) with screenshots.

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

- **Set an admin login and don't skip it.** Until a login is configured,
  every API route is unauthenticated — a startup log warning and an in-UI
  confirmation are both there specifically to make this hard to overlook.
- **Don't expose this to the public internet without TLS in front of it.**
  The app itself doesn't terminate TLS — put a reverse proxy or VPN/tunnel
  (Cloudflare Tunnel, WireGuard, etc.) in front if it needs to be reachable
  from outside your own network. This matters more than usual here: the XC
  protocol itself has no concept of session auth beyond a username/password
  in the URL, checked on every request — that's a real, if unavoidable,
  weak point once anything is internet-facing.
- **Login passwords are hashed with PBKDF2-HMAC-SHA256** (260,000
  iterations), not a fast general-purpose hash — resistant to offline
  brute-forcing if the config file ever leaked.
- **Both the admin login and the XC (streaming) login have brute-force
  lockout** — repeated failed attempts from one address get temporarily
  locked out (XC lockout is configurable under Configuration → Security;
  changes apply within ~30s). This slows down automated brute-forcing but
  doesn't replace putting this behind a VPN/tunnel if it's ever going to be
  reachable beyond a trusted network. Lockout state is in-memory and resets
  on every container restart — a restart-and-retry attacker is a much
  smaller threat than an internet-facing app with no lockout at all.
- **Streaming credentials are never written to logs.** The XC protocol
  embeds them in the URL itself (its own convention, not ours) — both the
  app's own logging and the container's access log redact them before
  anything is written to stdout.
- **Every connected instance gets its own credential**, not a shared one —
  Configuration → Connected Instances. Revoke or regenerate one without
  affecting the others if a specific instance's credential is ever
  compromised.
- **Optional per-instance IP allowlist** — if a specific connected
  instance's source IP is known and stable, you can lock its credential to
  that IP as an extra layer. Leave it blank for instances behind
  CGNAT/rotating IPs (locking those would just break them, not add real
  security, since the address isn't a reliable identity signal for them
  anyway).

## Refresh schedule

Configuration → Refresh Schedule controls how often background work runs:

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

Configuration → Backup & Restore lets you download, restore, or reset each
piece of VOD Manager's state independently (config, sessions, the catalog
database) — e.g. reset a corrupted database without touching saved
credentials, or roll back just the config. Database downloads use SQLite's
`VACUUM INTO` for a consistent snapshot even while the app is actively
writing to it.

## Curation tools

Curation & Maintenance and the Movies/TV Shows toolbars host a set of
catalog-quality tools — **Missing Artwork** (bulk poster fixing, with a
language-aware filter and sibling-safe bulk archiving), **Language Filter**
(the same language filtering over your whole library, not just
poster-missing items), **Duplicate Finder** (merges same-year entries that
only differ by punctuation), **Needs Review** (resolves year-ambiguous
imports), and **Orphan Checker** (finds dead rows a provider deletion can
leave behind — a series whose only source provider no longer exists, or
movies/episodes with zero sources at all). Every movie/series can also be
manually renamed or have its year corrected from its own detail view, for
whatever a provider's own catalog data got wrong with no other way to fix
it. Full details and screenshots for each in
[USERGUIDE.md](USERGUIDE.md#10-curation-tools).
