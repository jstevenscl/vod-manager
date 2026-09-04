# VOD & DVR Manager — User Guide

A complete walkthrough: install it, connect your real sources, wire it into
Dispatcharr (one instance or several), lock it down, and use the curation
tools day to day. For a quick technical reference instead of a guided
walkthrough, see [README.md](README.md).

> Screenshots in this guide have provider names, IP addresses, hostnames,
> and account identifiers replaced with placeholders (`Provider A`,
> `203.0.113.10`, `provider-a.example.com`, etc.) — your own screen will show
> your real provider names and data in their place.

## Table of contents

1. [What VOD & DVR Manager does](#1-what-vod-manager-does)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [First-run setup](#4-first-run-setup)
5. [Adding your first provider](#5-adding-your-first-provider)
6. [Connecting Dispatcharr](#6-connecting-dispatcharr)
7. [DVR recordings](#7-dvr-recordings)
8. [Security hardening](#8-security-hardening)
9. [Browsing and managing your catalog](#9-browsing-and-managing-your-catalog)
10. [AI-assisted features](#10-ai-assisted-features)
11. [Curation tools](#11-curation-tools)
12. [TMDB integration](#12-tmdb-integration)
13. [Backup and restore](#13-backup-and-restore)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What VOD & DVR Manager does

VOD & DVR Manager pulls movies and TV shows from whatever real sources you have —
one or more Xtream-Codes (XC) IPTV providers, a Plex server, an Emby or
Jellyfin server — and merges them into a single deduplicated catalog (the
*pool*). It then re-exposes that pool as its own XC-compatible server, so
Dispatcharr (or any other XC client) can pull it in exactly like it would
pull in a real provider.

```mermaid
flowchart LR
    subgraph Sources["Your real sources"]
        XC1["XC provider #1"]
        XC2["XC provider #2"]
        PLEX["Plex server"]
        EMBY["Emby / Jellyfin"]
    end

    subgraph VM["VOD & DVR Manager"]
        POOL["Import → Pool<br/>(dedupe, curate)"]
        XCS["Own XC server"]
    end

    subgraph Consumers["Consumers"]
        D1["Dispatcharr<br/>(instance 1)"]
        D2["Dispatcharr<br/>(instance 2)"]
    end

    XC1 --> POOL
    XC2 --> POOL
    PLEX --> POOL
    EMBY --> POOL
    POOL --> XCS
    XCS --> D1
    XCS --> D2
```

Why this matters in practice:

- **Same movie, multiple sources.** If the same title is available from two
  different IPTV resellers (or from a reseller *and* your own Plex), VOD
  Manager treats those as multiple *sources* of one pool entry, not two
  separate catalog items — and automatically fails over between them if one
  goes down or hits its connection limit.
- **Recommended deployment**: on the same host/stack as Dispatcharr, since
  the two talk to each other constantly. It's fully capable of running on
  its own separate host too — nothing about it requires colocation, it's
  just one network hop closer if it's local.

---

## 2. Prerequisites

- Docker + Docker Compose
- `ffmpeg` — already bundled in the image, nothing to install separately
- At least one real VOD source: an XC-type IPTV provider, a Plex server, or
  an Emby/Jellyfin server
- One or more [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)
  instances to pull the resulting catalog into
- Optional but recommended: a free
  [TMDB API key](https://www.themoviedb.org/settings/api) (v3 auth) — used
  for enrichment, duplicate/year disambiguation, and the missing-artwork
  queue. The app works without one; those specific features just won't.
- Optional: an API key from Anthropic, OpenAI, and/or Google (Gemini) if you
  want the AI-assisted features (§10)

---

## 3. Installation

Create a `docker-compose.yml`:

```yaml
services:
  vod-manager:
    image: ghcr.io/jstevenscl/vod-manager:latest
    container_name: vod-manager
    restart: unless-stopped
    ports:
      - "8282:8282"
    volumes:
      - vod_manager_data:/app/data

volumes:
  vod_manager_data:
```

Start it:

```bash
docker compose up -d
```

Building from source instead of pulling the published image — e.g. for
local development against this repo — works too:

```bash
docker build -t vod-manager:local .
```

The app listens on port `8282`. All persistent state (config, credentials,
the catalog database) lives in the `vod_manager_data` volume — safe across
image rebuilds and container recreation.

**Optional environment variables** (for initial/recovery admin login —
normally you'll just set a login through the UI on first run instead):

| Variable | Purpose |
|---|---|
| `VODMANAGER_ADMIN_USER` / `VODMANAGER_ADMIN_PASSWORD` | Overrides the stored login entirely while set — useful to regain access if you're ever locked out, or to provision a login via your deployment tooling instead of the UI. |
| `DATA_DIR` | Where persistent state is stored (default `/app/data`, matches the volume mount above — only change this if you're customizing the container layout). |

---

## 4. First-run setup

On first visit, VOD & DVR Manager asks you to set an admin username and password.

![Login / account settings screen](docs/screenshots/login-settings.png)

**Set a real login here.** There's also a "Skip for now — run without a
login" option for purely LAN/VPN-only deployments that don't need it, but
skipping means **every single feature is reachable with zero
authentication** by anyone who can reach the port — your provider
credentials, AI/TMDB API keys, and a full database backup download included.
The app will show you an explicit warning and ask you to confirm before
letting you skip, precisely because this is easy to click past without
thinking about it. If there's any chance this port is ever reachable from
outside a network you fully trust, set a login now — you can always change
it later from the gear icon → *Account settings*.

Password requirements: 6+ characters minimum. Passwords are hashed with
PBKDF2-HMAC-SHA256 (260,000 iterations) before being stored — never in
plain text, and not with a fast general-purpose hash either.

---

## 5. Adding your first provider

Go to the **Curation & Maintenance** tab → *Providers*. Pick a type
(Xtream-Codes, Plex, Emby, or Jellyfin), fill in its connection details, and
click **Add**.

![Providers table](docs/screenshots/providers.png)

Each provider row also has:

- **Priority** — lower numbers are preferred when the same title is
  available from more than one source; VOD & DVR Manager tries them in order and
  fails over automatically.
- **Max streams** — a hard cap on concurrent connections VOD & DVR Manager itself
  will open against this provider (`0` = unlimited).
- **Shared Limit / Live Accounts** — if this same real subscription also
  feeds a *live TV* account somewhere in Dispatcharr, link them here so VOD
  usage and live-TV usage draw from one accurately-tracked pool instead of
  silently exceeding your real connection limit. See your provider's actual
  plan for its real concurrent-stream cap. If your subscription is split
  into several Dispatcharr profiles, see *Multiple profiles on one
  subscription* below before setting this up.
- **User-Agent override** — some providers (a real example: one popular XC
  reseller) silently drop any request that doesn't look like it's coming
  from a browser. Leave this blank unless a specific provider needs it;
  VOD & DVR Manager already sends a normal desktop-browser User-Agent by default.

Once a provider is added, click **Import catalog** to pull its listing in
for the first time. This is a metadata-only pass (name/year/category/stream
ID) — poster art, cast, and descriptions are fetched lazily per-item after
that (see *Rich Metadata* at the top of the same tab for a manual bulk-fetch
button, or just let the background refresh schedule handle it — §6 in
[README.md](README.md#refresh-schedule)).

**Plex/Emby/Jellyfin: only Movies and TV Shows libraries are imported.** A
library's own **Content type** setting (in Plex/Emby/Jellyfin's own library
manager) is what tells VOD & DVR Manager whether it's a movie library, a TV
show library, or something else — a library added as "Mixed content" or
"Home Videos" reports neither, so none of its items can be imported. If a
library you expected to see content from doesn't show up after importing,
check that library's Content type is explicitly set to Movies or TV Shows;
the import result now calls out any library it couldn't classify by name so
this is visible instead of silently importing nothing.

### Excluding content on import

If a provider's catalog includes languages or categories you don't want in
your library at all — especially relevant for a provider with a very large
catalog, where manually cleaning up after the fact isn't practical — VOD
Manager can auto-archive matching titles the moment they're imported (or
re-imported), instead of only being able to filter them out after the fact.
Archived, never deleted: still fully browsable/playable/categorizable if you
ever change your mind, just out of the way by default.

**Language** (Curation & Maintenance → *Import Language Exclusion*) is
global — the same rule applies to every provider, since the languages you
don't want almost never depend on which provider a title came from. A
searchable checklist, not a typed list: it shows every language-style prefix
code actually seen across your pool right now (`AR`, `FR`, `EN`, and so on),
each with a friendly name where one's known and a live count of how many
titles currently carry it, so you're picking from what's really there instead
of guessing codes. Search, **Select visible** / **Deselect visible**, and
shift-click to select a range all work the same way as the provider category
picker below. Codes are recognized whether a provider tags titles with a pipe
(`AR| Movie Title`) or a colon (`AR: Movie Title`) — colon-style matching only
ever applies to a known language code, never any two-to-six-letter prefix, so
it won't misfire on a real title like *Kill Bill: Volume 1* or *CSI: Miami*.
There's also a toggle to exclude any title with non-Latin-script characters in
its name.

![Import Language Exclusion settings](docs/screenshots/import-language-exclusion.png)

**Category** (the **Exclude Categories** button on each provider row) is
per-provider, since available categories genuinely differ from one provider
to the next — the picker shows exactly what that provider itself calls its
categories, fetched live, not a guessed or fixed list. Same search/select-
visible/shift-click pattern as the language picker. A standalone
**Uncategorized** checkbox above the category list catches the case some
providers hit where an item is reported with no category at all — since
that has no name to match against, it can't be caught by picking specific
categories and needs its own switch.

![Exclude Categories picker for a provider](docs/screenshots/exclude-categories-modal.png)

If a provider's own category list has drifted since you last set exclusions
(a category renamed or removed on their end), the picker calls those out
separately — "N saved exclusions no longer reported by this provider" — with
a one-click **Remove stale** action, instead of silently folding them into
the selected count where they'd make the numbers look wrong.

**Archive new categories** (checkbox next to each provider, alongside
*Auto-create categories*) goes a step further: instead of a category you
have to notice and exclude by hand, any category a provider reports for the
first time gets auto-archived the moment it's discovered, same as
Dispatcharr's own "auto-archive new VOD categories" behavior. Off by
default, and turning it on never retroactively archives categories the
provider was already reporting before you enabled it — only ones that show
up for the first time on a later import.

Turning either of these on only affects **future** imports by default. If
you already have a large catalog and want the new rules applied
retroactively, click **Apply rules to existing catalog now** — this
re-imports every active provider to pick up the current rules across
everything already in your pool, which for a very large catalog can take a
while (the same cost as a normal full catalog import). Progress shows live
("Provider 2 of 5 — syncing Mega-OTT…"), and the final summary reports real
per-provider counts: how many titles were newly archived by the rules you
just set, not just how many providers finished.

**Language** exclusion applies to Plex/Emby/Jellyfin imports too, not just
Xtream-Codes (XC) providers. **Category** exclusion now does too: for a
Plex/Emby/Jellyfin provider, the picker lists that provider's own library
sections (Plex) or virtual folders (Emby/Jellyfin) instead of an XC-style
category list — e.g. exclude a "Music Videos" or "Home Videos" library the
same way you'd exclude an XC category. **Archive new categories** and
**Auto-create categories** remain XC-only for now — those need their own
design pass for what "newly discovered" means for a library-based source.

### Multiple profiles on one subscription

Some XC resellers split a subscription into several Dispatcharr "profiles"
so more than one connection can be open at once. This trips people up, so
here is the **one rule that decides everything else** — or skip the manual
counting below entirely and let VOD & DVR Manager read your real setup straight
from Dispatcharr; see *Discovering providers automatically* in §6:

> **Add exactly one provider row per distinct login (username+password
> pair) — never one row per Dispatcharr profile, and never one row per
> concurrent connection.** How many connections that one login is good for
> is a *setting* on its single provider row (Shared Limit + Live Accounts),
> not a reason to create more rows.

This is because VOD & DVR Manager's connection-limit pooling mirrors Dispatcharr's
own connection-fingerprint pooling exactly: it only pools two provider rows
together when their username **and** password match **exactly**. Rows with
different credentials are never pooled with each other, no matter how they're
labeled or grouped in Dispatcharr.

**Step 1 — count your distinct logins, not your profiles or connections.**
Ask your provider (or check what you were actually given): how many
different username/password pairs do you have? That number is exactly how
many provider rows you need. Ignore how many Dispatcharr profiles or total
concurrent connections you have — those numbers are irrelevant to this step.

**Step 2 — for each distinct login, set up its one provider row correctly:**

1. Add **one** provider row using that login's username/password.
2. Set **Max streams** and **Shared Limit** to that login's real connection
   cap (how many connections *this one login* — not your whole
   subscription — is allowed to have open at once; see your provider's
   plan).
3. Under **Live Accounts**, link this row to *every* Dispatcharr profile
   that was created using this same login. If this login only has one
   Dispatcharr profile, link that one; if it was split into several profiles
   for connection-management purposes, link all of them to this same single
   row.
4. Click **Import catalog** on this row.

**Step 3 — repeat Step 2 for every other distinct login**, but skip the
**Import catalog** click if that login serves the identical catalog as one
you already imported (the common case for multiple logins from the same
reseller). Importing the same catalog more than once creates real duplicate
rows in your pool — see *Duplicate Finder*, §11.

**Worked examples:**

| Situation | Distinct logins | Provider rows needed | Import catalog on |
|---|---|---|---|
| One login, split into 5 Dispatcharr profiles for 5 connections ("5x1") | 1 | **1**, Shared Limit = 5, linked to all 5 profiles | that 1 row |
| 5 logins, each with its own username/password, 1 connection each | 5 | **5**, each Max streams = 1, nothing linked between them | just 1 of the 5 |
| 5 logins, each *also* split into 5 Dispatcharr profiles ("5x5" = 25 connections total) | 5 | **5** (not 25 — one per login), each Shared Limit = 5, each linked to its own 5 profiles | just 1 of the 5 |

The number of Dispatcharr profiles or the total connection count never
determines the number of provider rows by itself — only the number of
distinct logins does.

### Native sub-accounts — one provider row per subscription, not per login

Everything above still works and is the safest description of the
underlying model, but if your logins all serve the **same catalog** (the
common case — multiple logins from the same reseller), you no longer need
a separate provider row for each one. A single provider can hold multiple
**sub-accounts**, each with its own real username, password, and
connection limit — the same idea as Dispatcharr's own M3U profiles, built
natively into VOD & DVR Manager.

Expand a provider row on the Providers page and use the **Sub-accounts**
panel:

1. Add each distinct login as a sub-account under the one provider, with
   its own **Max streams** (0 = unlimited).
2. Only import the catalog once, on the parent provider — sub-accounts
   share its content pool, they don't get their own.
3. VOD & DVR Manager tries active sub-accounts in order and uses the first
   one with a free slot when opening a stream, then fails over to the
   next — matching Dispatcharr's own default-then-next-profile behavior.
   Deactivate a sub-account (e.g. a login that's temporarily suspended)
   without touching the rest of the provider.

This replaces the "5 logins → 5 provider rows" pattern in the worked
examples above with "5 logins → 1 provider row, 5 sub-accounts" — fewer
rows to manage, one place to import/curate the catalog, same connection
accounting underneath.

**Already set up the old way?** Use **Merge Providers** (Providers page →
*Merge Providers* button) to fold your existing separate provider rows
into one. Pick a primary provider and the other rows to merge in as its
sub-accounts — content is never deleted, every source re-points to the
primary, and Dispatcharr live-account links move over automatically. If
the same piece of content genuinely exists on both providers (a real
collision, not a duplicate), it's left on the old row and reported back so
you can resolve it by hand; the old row is only removed once fully empty.

---

## 6. Connecting Dispatcharr

VOD & DVR Manager distinguishes two separate relationships with Dispatcharr, both
configured under **Configuration**:

- **Connected Instances** — *who's allowed to pull from VOD & DVR Manager.*
- **Dispatcharr Connections** — *who VOD & DVR Manager itself reaches out to*, to
  push connection-limit data and check live-TV viewer counts for the
  shared-limit coordination mentioned above.

A single Dispatcharr instance is usually both at once. They don't have to
match — you can have an instance that only pulls, and a connection VOD
Manager only reaches out to for coordination.

![Connected Instances and Dispatcharr Connections](docs/screenshots/configuration-dispatcharr.png)

**A fresh install starts with two categories already in place**: "All
Movies" and "All TV Shows", smart categories that automatically include
everything in your pool and stay current as new content is imported — no
manual step needed. This exists because Dispatcharr's VOD refresh aborts
entirely (rather than syncing an empty catalog) if it gets back zero
categories, so a brand-new instance always has something to sync against
from the start. The first time you see the app, you'll be asked once
whether 18+ content should be included in those two categories — it's
excluded by default until you answer. You can still build your own
categories (Manage Categories) on top of, or instead of, these two.

Separately from category placement, every movie's 18+ status (from the
Language Filter's category-name detection, or a manual toggle on the movie
itself) is also passed straight through to Dispatcharr on every VOD sync,
via the same field Dispatcharr (v0.29.0+) uses for its own per-profile
"Hide Mature Content" setting. This means a Dispatcharr profile with mature
content hidden won't see a flagged movie regardless of which category it's
placed in on the VOD & DVR Manager side — the two controls are independent,
and this one requires no setup here, it's automatic. Note this currently
covers **movies only**: Dispatcharr itself has no equivalent flag for
series yet.

### Before you start: remove existing provider VOD from Dispatcharr

If any of your providers are already connected directly in Dispatcharr with
VOD enabled, turn that off first. Otherwise you end up with the same movies
and series pulled in twice — once straight from the provider, once again
through VOD & DVR Manager's own pool — competing for the same groups.

Do this **one provider at a time**, not all at once — running it across
every provider simultaneously can cause database issues.

For each provider:

1. Open that provider's settings in Dispatcharr and go to **Groups → VOD -
   Movies**, click **Deselect Visible**, then switch to the **VOD - Series**
   tab and click **Deselect Visible** there too. Click **Save**, then
   refresh the provider and wait for it to finish refreshing its VOD before
   continuing.
2. Go back into that provider's settings and turn off **Enable VOD
   Scanning**.
3. Move on to the next provider and repeat steps 1–2. Don't start the next
   one until the current provider's refresh has fully finished.

Once every provider is done, open the **VODs** modal in Dispatcharr and
confirm both Movies and Series are empty. Only then are you ready to attach
VOD & DVR Manager as the new source.

### Connecting a single instance (the easy way)

Under *Dispatcharr Connections → Connect a new instance*, give it:

- A label of your choosing
- That Dispatcharr instance's own URL and an **admin API token** from it
- VOD & DVR Manager's own URL, **as reachable from that Dispatcharr instance** —
  this is not always the same URL you're viewing VOD & DVR Manager at yourself. A
  co-located instance (same Docker network/host) might use an internal
  hostname; a remote one needs your real public/VPN-reachable URL.

Click **Connect**. VOD & DVR Manager automatically:

1. Creates its own high-entropy client credentials
2. Creates the Dispatcharr-side XC M3U account for you, capped at 50
   concurrent streams at the account level (generous on purpose — your real
   per-provider limits are enforced separately, per source, not here), with
   its refresh interval set to 4 hours — Dispatcharr's own default for a
   new M3U account is 0 (disabled), which would otherwise mean it never
   auto-refreshes on its own

The only thing left is on Dispatcharr's own side: open the new M3U account,
enable VOD, and pick which groups/categories to turn on — the same setup
any other source needs.

### Connecting multiple instances

Repeat the same "Connect a new instance" flow for each additional
Dispatcharr instance — a household/production instance and a
testing/staging one, for example, or fully separate deployments for
different audiences. Each gets its own independent credential pair under
*Connected Instances*, so revoking or regenerating one never touches the
others.

**Per-instance category access control**: Dispatcharr has no per-user VOD
split of its own — everyone on a given Dispatcharr instance sees whatever
that instance's M3U account can see. To give one instance (or one end-user
IPTV app pointed straight at VOD & DVR Manager, bypassing Dispatcharr entirely) a
*restricted* catalog — a kids-only view, for example — set that instance's
*Category access* under *Connected Instances* to a specific set of
categories instead of leaving it at "— all —". This is enforced everywhere
that credential is used (catalog listing, detail lookups, and the actual
stream), not just hidden from a browse UI — a restricted client can't reach
disallowed content even with a raw copied stream URL.

### Discovering providers automatically

Once a **Dispatcharr Connection** exists (above), you don't have to type in
provider credentials by hand at all — click **Discover** on that
connection's row instead. This reads every real XC login Dispatcharr
already has configured on it — one entry per Dispatcharr "profile", since a
single Dispatcharr account can represent several separate real logins that
way (see *Multiple profiles on one subscription*, §5) — and shows you
exactly what it found: real base URL, username, and max streams, straight
from Dispatcharr, nothing to retype.

![Discover Providers modal, usernames masked](docs/screenshots/discover-providers.png)

Check the ones you want and click **Import selected**, or **Select all
valid logins** to grab everything in one click. A profile whose credential
rewrite pattern couldn't be cleanly parsed is listed but greyed out with an
explanation rather than guessed at — configure that one manually instead.
This is deliberately a review-and-import step, not a silent background
sync: nothing gets added as a provider until you choose it here.

**Re-check credentials** re-runs the same read against Dispatcharr and
updates any provider whose real password has since rotated on Dispatcharr's
side, without touching anything else you've set on that provider (priority,
category exclusions, shared limit, etc.). Already-imported profiles show as
"already imported" rather than being offered again.

---

## 7. DVR recordings

DVR isn't a provider you add — it's a capability you turn on for a
Dispatcharr connection you already have (§6). There's no separate catalog to
set up: enabling it just tells VOD & DVR Manager "also pull finished recordings
from this instance," and they show up in your pool alongside everything else.

Under **Configuration → Dispatcharr Connections**, each row has a DVR
button — **Enable DVR** if it's off, **DVR ✓** once it's on. Either opens the
same settings modal.

![DVR settings modal on a Dispatcharr connection](docs/screenshots/dvr-settings-modal.png)

### The path field — and why it's easy to get wrong

The **Local/NFS path** field is not a path on your host machine, and not a
path inside Dispatcharr's own container. It's a path **as seen from inside
the VOD & DVR Manager container itself**. That distinction is the single most
common way to misconfigure this.

**Leave it blank** and VOD & DVR Manager downloads each recording's file once,
over Dispatcharr's own API, into its own storage — no shared filesystem
needed at all. This always works, regardless of where either instance runs,
and is the right choice for a Dispatcharr instance on a different machine
with no shared/NFS mount. It costs one extra copy of each recording's bytes
and is a little slower to import than reading the file directly, but nothing
about setup or ongoing use requires touching Docker volumes at all.

**Set a path** only when VOD & DVR Manager's own container can read that
Dispatcharr instance's recordings directory directly off disk — which means
that directory has to be *mounted into VOD & DVR Manager's container*, not just
present somewhere on the host. Two ways to get there:

- **Same host, Dispatcharr also running in Docker** (the common case): mount
  the *same* volume Dispatcharr's own container already uses for its
  recordings into `vod-manager` as well. `docker inspect <dispatcharr
  container>` shows what that volume is and where Dispatcharr mounts it
  (typically `/data`, with recordings under `/data/recordings`). See the
  commented example in `docker-compose.yml` for the exact syntax — put your
  real values in a `docker-compose.override.yml` (gitignored) rather than
  editing the tracked file, so a `git pull` never clobbers your local setup.
- **Different host, reachable over NFS**: mount the NFS share at the host
  level first (plain Docker/OS NFS client config — VOD & DVR Manager itself never
  talks NFS), then bind-mount that host path into the container, same
  syntax as any other bind mount.

Either way, whatever path you land on **inside the container** is what goes
in the field — e.g. `/mnt/dvr/dispatch-test/recordings`, not
`/var/lib/docker/volumes/.../_data/recordings` and not Dispatcharr's own
internal `/data/recordings`. Point it at the `recordings` subfolder
specifically — Dispatcharr reports each file's path with a `/data/recordings`
prefix, and VOD & DVR Manager strips that prefix and re-joins the remainder onto
whatever you put here, so mounting one level too high or low silently
produces file-not-found on every recording.

If you're not sure whether the mount is right, leave the path blank and use
download mode first — it needs zero Docker configuration and proves DVR
import works end to end. Come back and wire up the local-path mount as a
later optimization once that's confirmed.

### Connection-level categories — the last-resort fallback, not the main path

**Movie category** / **TV category** on the connection's own DVR settings
modal are the *lowest-priority* fallback in a 4-step chain a completed
recording's category goes through on import:

1. The **Recording Rule** that matched it has its own target category set →
   use that.
2. Otherwise, whoever's rule matched it (or, for a one-off "record this
   episode" with no rule at all, whoever scheduled it) has their own
   personal DVR category set (see **Users**, below) → use that.
3. Otherwise, this connection-level category, if you set one here.
4. Otherwise, the recording still imports and still counts toward whoever
   owns it — it just isn't placed in any category, so it won't show up
   anywhere in the exported catalog until placed manually.

This connection-level setting is a safety net for admin-managed setups —
e.g. you're not using per-person Recording Rules at all and just want every
recording from this connection to land in one shared bucket. It is
**deliberately not available** to the self-service Portal: a Portal user can
only ever schedule into their own personal category (step 2), never fall
back to this one — see **Users** below for why.

### Deleting from Dispatcharr once safely copied

Neither ingestion mode ever cleaned up the original recording on
Dispatcharr's own side — Dispatcharr has no automatic retention of its own,
so left alone, its disk just fills up forever with content VOD & DVR Manager has
already absorbed. **Delete from Dispatcharr once safely copied**, on the
same DVR settings modal, fixes this — off by default, so it's always a
deliberate choice, never a surprise the moment you update.

What it actually does depends on which mode you're using:

- **Local/NFS path (shared-volume) mode** — normally just references
  Dispatcharr's own file directly, no bytes ever copied. With this on,
  a completed recording gets copied into VOD & DVR Manager's own storage first
  (independent of Dispatcharr's file), verified against the exact byte size
  Dispatcharr itself reports for that recording, and only *after* that
  verified copy exists does VOD & DVR Manager ask Dispatcharr to delete the
  original — which removes the underlying file too, not just Dispatcharr's
  own database record.
- **Download mode** — already makes an independent copy of every recording;
  this just adds the same verify-then-delete step afterward.

**Turning this on also gradually cleans up recordings you already imported
before this setting existed.** Every completed recording Dispatcharr reports
goes through the same check on every import pass, whether it's brand new or
was ingested months ago under the old reference-only behavior — so nothing
needs a separate migration step, it just catches up a few recordings at a
time as your normal import schedule runs.

A couple of things worth knowing:

- The size check only ever proceeds on a **confirmed match** — if
  Dispatcharr hasn't reported a size, or the copy doesn't match it, nothing
  gets deleted that pass; it's simply retried on the next one. Nothing is
  ever deleted without a verified independent copy already in place first.
- Dispatcharr's own file deletion is best-effort on its side (it happens in
  the background after the delete request is accepted), so don't expect
  Dispatcharr's disk usage to drop the instant an import pass finishes —
  give it a little time.

### Recording Rules

A **Recording Rule** (renamed from "Recording Profiles" — you may see the
old name in older screenshots) watches one EPG channel for anything matching
a title, and keeps discovering and scheduling new airings as Dispatcharr's
guide data updates — this is VOD & DVR Manager's own replacement for Dispatcharr's
built-in Series Rules, which have a channel-matching bug of their own for
this kind of setup. Create one from the DVR tab's **Scheduled Recordings**
page or the EPG search.

Each rule can set:

- Its own **target movie/series category** — takes priority over everything
  else in the resolution chain above. Leave blank to fall through to the
  rule owner's personal category instead.
- **Backfill mode** (optional) — before recording a new airing, check
  whether the same title already exists somewhere in your pool (from a
  regular provider, or another recording) and reuse it instead of recording
  again:
  - **Pointer** — no extra disk cost; just references the existing source's
    stream. The file stays wherever it already lived.
  - **Download-and-store** — makes a real local copy of the existing
    source's bytes under VOD & DVR Manager's own storage, same as a normal
    recording, but without needing Dispatcharr to record it again.
  - Either mode still counts the matched item toward the rule owner's disk
    quota (as virtual usage for pointer mode, real usage for download mode)
    and places it in the rule's own target category exactly like a fresh
    recording would.
- **Monitored** toggle — an unmonitored rule stops being checked for new
  episodes (e.g. a show you've finished collecting) without deleting its
  history or already-scheduled recordings.

![Scheduled Recordings — an existing Recording Rule and the "Add a rule" creation form](docs/screenshots/dvr-scheduled-recordings.png)

### The DVR tab's subpages

DVR is split into five subpages once you're actually using it day to day:

- **Scheduled Recordings** — your Recording Rules and what's currently
  upcoming/in-progress on Dispatcharr's side, plus the EPG search used to
  create new rules or one-off single recordings.
- **Users** — per-person configuration: stream-concurrency reserve (how many
  of this connection's total stream slots are held back for this person's
  own recordings), disk quota (with a choice of **hard fail**, block new
  recordings once they're at quota, or **delete oldest**, auto-evict their
  own oldest recordings to make room), retention policy (max age / max
  episodes per show, surfaced for manual review rather than auto-deleted),
  and — new — each person's own **DVR movie category** and **DVR TV
  category**.

  These two categories are the person's own, not a shared default: nothing
  stops two different people from being assigned the *same* category if you
  want that (e.g. a shared "Family Recordings" bucket), but each person only
  ever manages their *own* content in the Portal even when a category is
  shared — the Portal's Library is always scoped to what that person
  actually owns, never to everyone who happens to share the same category.
  What a category *does* determine is what shows up together when browsing
  it as a regular category in an IPTV player/Dispatcharr — that view has no
  concept of per-person ownership at all, same as any other category.

  ![DVR Users card showing a person with no category assigned yet](docs/screenshots/dvr-users-category-required.png)

  **A person can't schedule anything from the Portal until you've assigned
  them a category for that content type.** This is intentional, not a
  missing default: DVR categories share the same underlying table as every
  other VOD category (smart categories, TMDB Lists, provider-created ones),
  so an unmistakable, deliberately-assigned name is what keeps disk-quota
  accounting and the Portal's own display honest — a category name like
  "Emby TV Shows" left over from general catalog curation has nothing to do
  with any particular person's recordings and shouldn't be silently reused
  as if it did. When creating a person's category from this screen, name it
  something that says whose it is and what it's for — e.g. **"Steven DVR
  Movies"** / **"Steven DVR TV Shows"** — the quick-create button here
  pre-fills exactly that suggestion.

  ![DVR Users card with categories assigned, plus Portal Access below it](docs/screenshots/dvr-users-configured.png)

- **DVR Library** — browse, preview, and delete recordings directly
  (admin view of everything, not scoped to one person).

  ![DVR Library subpage](docs/screenshots/dvr-library.png)

- **Missing Episodes** — a Sonarr/Radarr-style view per show: episodes a
  monitored Recording Rule hasn't captured yet, with a find/record cascade
  (checks the show's own known channel first, falls back to a cross-channel
  EPG search).

  ![Missing Episodes subpage](docs/screenshots/dvr-missing-episodes.png)

- **Metrics** — rule health (is each rule's channel/title still matching
  anything real) and disk usage, split into actual bytes (real files this
  connection owns) vs. virtual bytes (pointer-backfilled content that lives
  elsewhere but still counts toward someone's quota).

  ![Metrics subpage — per-person usage, recording load by channel, rule health, and unresolved missing episodes](docs/screenshots/dvr-metrics.png)

### The self-service Portal

DVR also ships a separate, lightweight web app for end users — not admins —
to manage their own recordings without touching the main VOD & DVR Manager UI at
all. It has its own login (a **Portal account**, created per-person under
the Users page — separate from both the admin login and their Dispatcharr
credentials) and its own URL. Its first login always requires setting up an
authenticator app (Google Authenticator, Authy, 1Password, etc.) — mandatory,
not optional — before the account can sign in at all.

![Portal two-factor setup on first login — scan the QR/enter the key, then confirm a code](docs/screenshots/portal-mfa-setup.png)

From the Portal, a person can:

- Browse the EPG and schedule a single episode or a recurring series rule
  for anything on a channel visible to their Dispatcharr user

  ![Portal Scheduler tab — everything airing in the next 24 hours, tap anything to schedule it](docs/screenshots/portal-scheduler.png)

- See their own upcoming/in-progress recordings

  ![Portal Upcoming tab](docs/screenshots/portal-upcoming.png)

- Browse their own Library — everything they've recorded or been attached to
  (see below), grouped by their own assigned DVR category — and play, or
  remove, anything in it

  ![Portal Library tab](docs/screenshots/portal-library.png)

- See their own disk usage against their quota, and their stream-limit
  budget

  ![Portal Usage tab](docs/screenshots/portal-usage.png)

The landing tab (**My Recordings**) is the person's own dashboard —
upcoming count, active rules, storage used, and stream budget at a glance,
plus their own Recording Rules — and **Account** lets them set a
notification email.

![Portal landing tab — My Recordings, with the at-a-glance stat tiles every tab shares](docs/screenshots/portal-my-recordings.png)
![Portal Account tab](docs/screenshots/portal-account.png)

**Shared recordings, not duplicated ones.** If two people's rules both match
the same airing, or someone schedules something another person already has,
they share the one real file — each person's Library entry for it is
independent, so one person removing it from their own Library never affects
the other; the file itself is only actually deleted once nobody has it left.

**Nothing schedules without a category.** As covered under Users above, a
Portal account can't schedule a movie recording without their own DVR movie
category assigned, or a series recording without their own DVR TV category
— they'll see a clear message telling them to ask their admin, rather than
a recording silently succeeding with nowhere to be filed.

### Disabling DVR

**Disable DVR** on the connection's settings modal removes that connection's
recording rules, upcoming recordings, per-person limits, and portal
accounts — the same cascade a regular provider delete does today. It does
not touch the Dispatcharr connection itself, which stays fully usable for
its other job (pushing usage data, checking live-TV viewer counts).

---

## 8. Security hardening

If this is reachable beyond a network you fully trust — and especially if
it's reachable from the public internet at all — do these:

1. **Set a real login** (§4) and don't use the Skip option.
2. **Put TLS in front of it.** VOD & DVR Manager doesn't terminate TLS itself —
   use a reverse proxy (nginx, Caddy, Traefik) or a tunnel (Cloudflare
   Tunnel, Tailscale, WireGuard) if it's reachable from outside your LAN.
   This matters more than usual here: the XC protocol itself has no session
   concept beyond a username/password checked on every request, so an
   unencrypted connection exposes real streaming credentials on the wire.
3. **Leave the login lockout on** (Configuration → Security) — repeated
   failed admin-login or XC-client-login attempts from one address get
   temporarily locked out. Defaults are reasonable; tighten them for an
   internet-facing deployment.

![API Keys and Security settings](docs/screenshots/configuration-api-keys-security.png)

4. **Give every connected instance its own credential** (already the
   default — §6) rather than sharing one across multiple Dispatcharr
   instances, so a compromised credential is cheap to revoke without
   affecting anything else.
5. **Optional per-instance IP allowlist**, if a connected instance's source
   IP is known and stable. Leave it blank for anything behind CGNAT or a
   rotating IP — locking those would just break them, not add real
   security, since the address isn't a reliable identity signal for them.
6. Lockout state is in-memory and resets on container restart — this
   slows down a sustained automated attacker; it isn't a substitute for
   putting this behind a VPN/tunnel once it's reachable beyond your own
   network.
7. **Provider passwords, Dispatcharr tokens, and XC client secrets are
   encrypted at rest** in the database (not just hashed logins) — the
   encryption key lives in `config.json` so it travels with that file's own
   backup/restore lifecycle (§13). Existing plaintext values from before
   this was added upgrade automatically on next startup, no action needed.

---

## 9. Browsing and managing your catalog

Above the catalog itself, the dashboard always shows two live cards:

- **Activity** — what's playing right now, across every viewer, refreshed
  continuously. Empties out the moment playback stops; nothing here
  persists.
- **Failed Streams** — the opposite: a *persisted* log of playback attempts
  that failed outright (every source for that title was tried and none
  worked) or broke mid-stream after starting, surviving restarts unlike
  Activity above. Each row lists every provider that was actually tried and
  its own specific error — not just the last one — so you can tell "every
  source for this title is genuinely down" apart from "one specific
  provider keeps failing while the others are fine." Dismiss individual
  rows or **Clear all**; the log itself is capped at the most recent 500
  entries and prunes automatically. Where the failure's still resolvable to
  a real movie/episode, each row also shows **"Playing from"** — the source
  that would actually be tried first right now, using the same
  priority/failover ordering real playback uses, flagged if that source is
  itself currently failing — and, for a series, **"All series providers"**,
  every provider with a source anywhere in that series, not just the one
  episode that happened to fail.

![Failed Streams, showing a mid-stream crash and an every-source-exhausted failure](docs/screenshots/failed-streams.png)

The **Movies** and **TV Shows** tabs below that are the main catalog views,
each with a **list** or **grid** (poster wall) mode.

![Movies tab, grid view](docs/screenshots/movies-grid.png)

- **Search / provider filter / page size** — top toolbar.
- **Manage Categories**, **Needs Review**, **Missing Artwork**, **Language
  Filter** — open the curation tool modals covered in §11, scoped to
  whichever tab (movies vs. series) you opened them from.
- **Bulk actions** — check items individually, shift-click to select a range,
  or **Select all visible** to grab everything on the current page. **Place
  selected**/**Place all filtered** place into a category ("all filtered"
  covers everything matching the current search/filter, not just the current
  page, without paging through results manually); **Archive selected**
  (**Un-archive selected** when viewing the Archived toggle) applies the same
  archive action described below to the whole selection at once.
- **Rename / fix year** — every item's detail view (click a row, or a tile
  in grid mode) has this. Providers occasionally send a blank, garbled, or
  otherwise wrong title/year with no other way to correct it — this fixes
  that directly. If the corrected name+year now matches an existing pool
  entry exactly, the two are merged automatically instead of leaving a
  duplicate.

![Renaming a movie](docs/screenshots/rename-movie.png)

- **Use TMDB title** — appears next to *Rename / fix year* whenever the item
  already has a confirmed TMDB match. One click renames it to TMDB's own
  canonical title and year — useful after *Use TMDB title* or a Title &
  Metadata Rule has left the display name slightly different from what TMDB
  itself calls it. If the corrected title collides with an existing pool
  entry, the two merge (same as a manual rename above), and the confirmed
  TMDB id carries over to whichever row survives.
- **Clear TMDB match** — appears next to *Use TMDB title* whenever the item
  has a confirmed TMDB match (its id is also shown, e.g. "TMDB #623"). If a
  match turns out to be wrong (whether from the automatic matching TMDB
  Lists sync does or anything else), this breaks it — only the TMDB id is
  removed, name/year/sources/poster are untouched — so the item goes back
  to unmatched and can pick up a correct id on the next enrichment pass
  instead of staying confirmed-wrong. Note this can't undo a merge that
  already happened from a bad match (see *Use TMDB title* above) — that
  needs a Backup & Restore snapshot taken before the merge.
- **Revert to this** — every source records the provider's *original* name
  at import time even after a Title & Metadata Rule cleans it up for
  display. If a source's captured original name differs from the item's
  current name, a **"Provider's original name: ..."** line appears under
  that source with a one-click **Revert to this** button. Search also
  matches this original name, so a title is still findable by what the
  provider called it even after a rule has rewritten the display name.

![Use TMDB title and Revert to this, on a source whose display name was rewritten by a Title & Metadata Rule](docs/screenshots/revert-and-tmdb-title.png)

- **Apply TMDB Titles** (Movies/TV Shows toolbar) is the bulk version of
  *Use TMDB title* above — renames every item in the library that already
  has a confirmed TMDB match to TMDB's own title/year, wherever it currently
  differs, instead of clicking through one at a time. Still only ever
  touches items with an already-confirmed match; it doesn't go looking for
  new matches itself. Large libraries process in bounded batches, so this
  can take a little while — the button's label updates with a running "N
  renamed" count as it works.
- **Client Title Format** (Curation & Maintenance) is a separate, ongoing
  setting rather than a one-time rename: *Append year to titles served to
  clients* controls what Dispatcharr/TiviMate/etc. actually display for
  every title, e.g. "Movie Name (2024)" — the pool's own name/year fields,
  dedup matching, and Title & Metadata Rules are untouched by it. Combine
  with *Apply TMDB Titles* above to have clients see TMDB's own canonical
  title, with its year, instead of whatever a provider happened to send.

![Client Title Format toggle](docs/screenshots/client-title-format.png)
- **Archive** (the archive-box icon on each row) is a true archive: an
  archived item is immediately removed from every category placement (so
  Dispatcharr stops seeing it right away, not just eventually) and hidden
  from the normal Movies/TV Shows view — click the **Archived** toggle in the
  filter bar to see only what's archived. Nothing is deleted; sources,
  metadata, and history all stay intact, and restoring is one click.
- **Delete** only works on genuine orphans — an item with zero sources.
  Anything a provider still actively serves can't be deleted (the button is
  disabled with an explanation): a real provider will just re-import it on
  the next sync no matter how many times you delete it locally, so Archive is
  the only way to durably hide something that's still provider-backed. Use
  the **Orphan Checker** (§11) to find and clean up genuine orphans in bulk.

### Fixing a wrong match, and removing dead sources

Every movie or episode can have more than one **source** — one entry per
provider currently serving it. Expand any item to see its full source list;
each source line has two actions beyond the usual **Play** and **Copy
playable stream URL**:

- **Move to a different movie/episode** (the ↔ icon) — for when a
  provider's own listing was matched to the wrong title on import (a typo,
  a title collision, a garbled name). Search for the correct movie, or for
  an episode, search for the correct series and give it the right
  season/episode number — that episode is created if it doesn't exist yet —
  and the source moves there with its history intact, instead of you having
  to delete and re-add it. If the source you moved was the old item's only
  source, that now-empty item is cleaned up automatically.
- **Remove source** (the × icon) — deletes just that one source. If it's
  the item's only source, this deletes the item itself, since nothing would
  be left to serve it; if other sources remain, the item stays available
  from them.

![A movie's Sources list: a failing source, a source whose provider name doesn't match the movie ("Cinderella" under "The Crew"), and the Move/Remove actions](docs/screenshots/move-and-remove-source.png)

A source that keeps failing shows a warning right on its own line —
**"Failed Nx in a row, last &lt;time&gt; — likely dead, consider
removing"** — updated on every real playback attempt, not just when every
source for that title fails outright (see *Failed Streams* above). This
catches what Failed Streams alone can't: a title that *looks* healthy
because one provider covers it, while a second provider's copies are almost
all actually broken and simply never get tried because the first one
already succeeded. A source with a live failure streak is automatically
tried *last* during playback, behind every source without one, so it stops
being everyone's slow first (and doomed) attempt.

On a TV show, if more than one episode from the *same* provider is
currently failing, a **Failing sources** box appears at the top of that
show's Episodes list, grouped by provider — with a one-click **Remove all**
to strip every one of that provider's sources from the whole show at once,
instead of clearing them episode by episode.

![Failing sources box on a TV show, grouped by provider, with Remove all](docs/screenshots/failing-sources.png)

As with any source removal, **Remove all** can delete an episode (or, if
that provider was the only one covering it, the whole show) if nothing else
was serving it — the confirmation prompt warns before you commit.

---

## 10. AI-assisted features

An API key from **any** of Anthropic, OpenAI, or Google (Gemini) unlocks
the AI-assisted features — configure one or more under **Configuration →
API Keys**, then pick which one is active. Switching providers later is
just a click; nothing else about the features changes.

![Multi-provider AI configuration](docs/screenshots/configuration-api-keys-security.png)

None of these ever apply anything automatically — every one is a suggestion
you still review and confirm yourself:

- **Suggest a category with AI** (Categories modal) — describe a category
  in plain English; the AI proposes a structured filter rule using the same
  fields the manual rule builder uses (name, genre, year, country/language,
  director, is_adult).
- **AI Evaluate** (✨ on any category) — for criteria the rule fields can't
  express (mood, plot, audience fit), the AI judges actual titles against
  your description instead of matching fields. Runs over a bounded
  candidate set, never silently against the whole pool — the result always
  reports how many were actually considered.
- **Ask AI** (Needs Review, Missing Artwork) — when an item is ambiguous
  (no year, or no confident poster match), the AI picks the most likely
  correct match among the real TMDB search candidates already shown, with
  its reasoning and a confidence level. You still click a candidate
  yourself to apply it.

**Ask AI stays greyed out until at least one TMDB candidate is shown to
choose among** — it picks from that list, it doesn't search TMDB itself.
That candidate list depends on your **TMDB API key** (Configuration → API
Keys), which is a *separate* key from the AI provider key above — having an
AI provider configured isn't enough on its own if the TMDB key is missing
or the search for that title's stored name genuinely returns nothing. If
you see "No TMDB matches found for this name" under an item, that's why
the button is disabled for it: try a cleaned-up search term in the "search
TMDB as" box first, or set the year manually.

![Ask AI disabled on an item with zero TMDB candidates — the "No TMDB matches found" message is why](docs/screenshots/ask-ai-disabled.png)

Each provider has a model dropdown (Configuration → API Keys) with a
curated set of options, from cheapest/fastest to most capable — defaults to
the cheapest tier, since most of these features make many small requests
rather than needing flagship-level reasoning per call. Switching provider
resets the model choice to that provider's own default rather than carrying
over a model id that belongs to a different provider.

---

## 11. Curation tools

All of these live under the **Movies**/**TV Shows** toolbars or the
**Curation & Maintenance** tab, and follow the same philosophy throughout:
*scan or filter first, review what's found, then apply* — nothing runs
automatically against your whole library without you seeing what it found
first.

### Rich Metadata (enrichment)

Fetches detail — genre, poster, description, cast — from each item's own
source provider for every movie and series in the pool (**Curation &
Maintenance** tab). Runs in the background; safe to navigate away while it
works.

- **Bulk Enrich All** — enriches everything that hasn't been enriched yet,
  or has aged past the **Enrichment TTL** (Configuration → Refresh
  Schedule), skipping anything still fresh.
- **Force Re-Enrich All** — re-fetches every movie/series from its provider
  regardless of freshness, ignoring the TTL entirely. Use this once after
  an update adds a new field it captures (e.g. rating, release date,
  bitrate), so existing items backfill it right away instead of waiting
  out the normal freshness window.
- Progress tracks movies and series separately, each with its own running
  error count — a nonzero count usually means a source's own API rejected
  or timed out on some items, not that the whole run failed.
- If a provider starts throwing connection failures or 403/429/503
  responses, enrichment automatically **backs off just that provider**
  (an amber banner names it and shows the remaining cooldown) instead of
  continuing to hammer it — other providers keep enriching at full speed.
  Items skipped this way aren't errors and aren't lost; they're retried
  automatically once the cooldown ends or on the next run.
- Enrichment also happens lazily, per item, the moment it's actually
  needed (e.g. a movie/series detail modal's own **Fetch full detail**
  button) — so a freshly-imported series showing no episodes yet just
  hasn't been enriched yet, not necessarily broken (see Orphan Checker
  below).

### Managing categories

**Manage Categories** (Movies/TV Shows toolbar) is where you rename, reorder,
enable/disable, schedule, and delete your own categories — separate from the
provider-side Exclude Categories picker (§5), which controls what gets
imported in the first place.

- **Enable/disable** (power icon) is a soft on/off switch, not a delete: a
  disabled category stops being exported to Dispatcharr but keeps everything
  already in it, so a seasonal category (Halloween, Christmas) can be turned
  off after the season and back on next year without rebuilding it. You can't
  disable the last active category for movies or series — Dispatcharr's own
  VOD sync fails outright against an empty category list, so this is blocked
  with an explanation rather than letting you accidentally break it.
- **Annual schedule** (calendar icon) automates that same on/off switch —
  set a start and end date (month-day, e.g. `10-01` → `11-01`) and the
  category enables/disables itself on those dates every year going forward,
  no need to remember it each season.
- **Search, select, and bulk actions** — the same search bar, **Select
  visible**/**Deselect visible**, and shift-click range-select as the Exclude
  Categories picker, plus bulk **Enable selected**/**Disable selected**/
  **Delete selected** buttons once you've checked a few. Deleting a category
  only unplaces items from it — nothing in your pool is touched.

### Missing Artwork

Movies/series with no poster — usually because the source provider's own
catalog data just didn't include one. Search or filter (by language, see
below), then either pick a real TMDB match per item (with an AI-suggest
option) or blanket-apply one image to everything matching your filter at
once — useful for content that will never have a real per-title poster
(e.g. a batch of clips from the same creator/source).

![Missing Artwork queue](docs/screenshots/missing-artwork.png)

Also supports **archiving**: hide matching items from this queue (and
Needs Review, and Duplicate Finder) without deleting anything — still fully
browsable, playable, and usable in categories, just no longer flagged as
needing attention. Useful for content you've decided not to curate further
(e.g. a language you don't plan to add posters for).

### Language Filter

The same language-based filtering as Missing Artwork, but over your *whole*
library — a title with a real poster is just as much "not in your language"
as one without.

![Language Filter with a live archive preview](docs/screenshots/language-filter.png)

Two independent ways to isolate content by language:

- **Non-Latin script detection** — flags titles containing Arabic, Thai,
  Chinese/Japanese/Korean, Cyrillic, Greek, Hebrew, or Devanagari
  characters. Broad and automatic, no setup needed.
- **Language-prefix picker** — many providers tag dubbed/subtitled variants
  with a leading code like `AR|`, `FR|`, `EN|`. The picker shows exactly
  which codes are actually present in *your* catalog, with real counts —
  not a fixed guessed-in-advance list, so it adapts to whatever your
  providers actually use, including non-language category tags some
  providers reuse the same convention for (you'll see those too — just
  don't select ones that obviously aren't languages).

**Archiving here is sibling-aware by design**: type a code (or several) into
*"Keep a title if also available as"*, and a title only gets archived if a
copy also exists in a kept language (or with no language tag at all) —
never your only copy of something, just because it happens to not be in a
language you picked. The archive button shows a live preview
(`Archive all filtered (25 of 36 — 11 would be skipped)`) that updates as
you adjust the filter, so you can see exactly what will happen before you
commit to it.

### Duplicate Finder

Finds pool entries that look like the same real title split into two rows,
three ways at once:

- **Cosmetic punctuation** — a colon, a dash, quote style — the same title
  formatted slightly differently by different providers.
- **Adjacent-year mislabeling** — the same name with years one apart (a
  provider getting a release year wrong by one is a common, real pattern).
  A gap of two or more years never clusters — that's almost always two
  different films that happen to share a title, not a duplicate.
- **A shared TMDB id** — when two candidates carry the same TMDB id, that's
  confirmed proof they're the same real title, even across a bigger year
  gap than the rule above alone would allow. A *conflicting* TMDB id is
  treated the opposite way: proof they're genuinely different, so that pair
  is ruled out and never shown as a duplicate at all.

Each candidate shows its poster, a **same TMDB match** badge when a shared id
confirms the group, and a per-candidate **true match**/**year mismatch** badge
comparing that candidate's own year against TMDB's real release year for that
id — a shared id only proves the title matched, not that a given row's year
field is correct. An inline **Preview** (Direct/Transcoded/HLS, same as the
main player) lets you play more than one candidate side by side before
deciding. Pick which candidate to keep — the rest merge into it (sources,
categories, and episodes all move over automatically, nothing is lost) — or
**Ignore** a group that isn't actually a duplicate so it stops resurfacing on
future scans. The pre-selected candidate favors TMDB confirmation over raw
source count — a candidate with a confirmed, cross-checked TMDB match is
picked by default even if another candidate happens to have more sources,
since an unconfirmed candidate outranking a confirmed one by source count
alone was more often wrong than right. Still just a starting point — pick a
different one any time before merging.

**Check TMDB-confirmed matches** goes a step further: it checks every group in
the current scan against TMDB in the background (a real API call per
candidate id, so it can take a few minutes on a large scan — progress shows
live). A group counts as **confirmed** when every candidate shares the same
TMDB id — that alone is proof they're duplicates. The merge target is
whichever candidate's name matches TMDB's own title exactly, when one does;
otherwise the most-sourced candidate is used instead of dropping the group.
Confirmed groups are pulled out of the manual review list entirely and
offered as a single **Merge all confirmed matches** action — one click merges
the whole batch, since there's no real ambiguity left for a human to resolve.

A second, separate tier catches the case where only *one* candidate in a
group carries the shared TMDB id and the rest have no id at all — less
airtight than a corroborated match (no sibling confirms the id), so it's
never folded into the confirmed batch above. It's only offered when that
lone candidate's own year also matches TMDB's canonical year for that id
(the same self-consistency check behind the per-candidate **unconfirmed**
badge) — a candidate whose year *doesn't* match is never trusted here.
**Trust TMDB for these too** merges this second tier in one click, same as
the confirmed batch.

![Orphan Checker, Duplicate Finder, and TMDB Lists](docs/screenshots/curation-tools.png)
![Duplicate Finder with TMDB-confirmed matches](docs/screenshots/duplicate-finder.png)

### Needs Review

Items imported with no year, where more than one existing pool entry shares
the same name — too ambiguous to auto-merge, so they're held out of every
category until you (or the AI, as a suggestion) pick the right one, usually
from a real TMDB match rather than having to research it yourself.

### Orphan Checker

Finds dead rows a provider deletion (or a bug) can leave behind — a series
whose only source provider no longer exists, or movies/episodes with zero
sources at all. Run it periodically, especially after removing a provider.
It won't flag a series with no episodes yet — that's normal for anything
not yet lazily enriched, not broken. Once a scan finds anything, a **Delete
N orphans** button purges everything the scan found in one action — useful
when a provider's fully abandoned and its dead rows just need to go, rather
than investigating one at a time.

---

## 12. TMDB integration

A free [TMDB API key](https://www.themoviedb.org/settings/api) (v3 auth)
under Configuration → API Keys unlocks:

- Real TMDB search for the Needs Review and Missing Artwork flows above
- **TMDB Lists** (Curation & Maintenance) — link a public TMDB List (a
  personal watchlist, or a well-known curated list like IMDB's Top 250, for
  example) to auto-populate a category. A list can contain both movies and
  shows, so linking one creates a paired movie category and series category
  — kept separate since Dispatcharr's movie and TV catalogs are different
  endpoints. Only items already present in your pool ever get placed; this
  organizes existing content, it doesn't pull in anything new. Matching
  first tries each list entry's own TMDB id against your pool directly, then
  falls back to a forgiving title+year match (tolerating a provider
  mislabeling a release year by one) for pool items that don't have a
  confirmed TMDB id yet — a real hit backfills the id, so the match is
  instant on the next sync. Without this fallback, a list only ever matched
  the handful of items a provider happened to tag with their own TMDB id at
  import time, which is why a large curated list used to place almost
  nothing. The full list is fetched regardless of size — a list with
  hundreds of entries (e.g. IMDB's Top 250) is no longer capped at the
  first page TMDB returns.

---

## 13. Backup and restore

Configuration → Backup & Restore lets you download, restore, or reset each
piece of state independently — configuration, login sessions, and the
catalog database. Useful for resetting a corrupted database without losing
saved credentials, or rolling back just the config. Database downloads use
SQLite's `VACUUM INTO` for a consistent snapshot even while the app is
actively writing to it.

**Diagnostics.** Configuration → Diagnostics has a "Download Diagnostic
Logs" button — it exports the app's own log history with provider
credentials, hostnames, and IP addresses scrubbed, safe to attach to a bug
report or support request without exposing anything sensitive about your
setup. The version shown in the top header (hover it for the branch/tag it
was built from) is worth including too, especially when running a `:dev`
build rather than a tagged release.

---

## 14. Troubleshooting

**A provider's catalog won't import / times out.** Check the User-Agent
override (§5) — some providers reject requests that don't look
browser-like. Also confirm the base URL and credentials work with a
regular XC client first, to rule out a provider-side issue.

**Locked out of your own login.** Set `VODMANAGER_ADMIN_USER` and
`VODMANAGER_ADMIN_PASSWORD` as environment variables on the container and
restart — this overrides the stored login while set, letting you sign in
and set a new one from the UI. Remove the environment variables afterward.

**Dispatcharr says "Provider returned no VOD categories... aborting VOD
refresh."** This means VOD & DVR Manager currently has zero categories — normally
impossible since a fresh install auto-seeds "All Movies"/"All TV Shows"
(§6), but it can happen if every category was manually deleted. Create at
least one category (Manage Categories) and re-run the Dispatcharr sync.

**A Dispatcharr instance can't reach VOD & DVR Manager.** Double check the URL
you gave it during "Connect a new instance" is reachable *from that
instance's own network position*, not just from your browser — a
Docker-internal hostname won't resolve from a remote instance, and vice
versa.

**Movies/series show up as duplicates.** Run Duplicate Finder (§11) — most
duplication is either a punctuation difference between providers (that
tool) or a language variant (Language Filter, §11). If neither explains
it, check Needs Review for an unresolved year ambiguity.

**A title has no poster.** Check Missing Artwork (§11) — it's usually
either genuinely unavailable from the source provider, or fixable with a
real TMDB search from there.

**Something's wrong with the database.** Configuration → Backup & Restore
lets you download a snapshot before troubleshooting further, and reset just
the database (keeping your saved login/config) if you need a clean slate.
