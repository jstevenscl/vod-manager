import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Hls from 'hls.js'
import { AlertCircle, Archive, ArchiveRestore, CalendarClock, CalendarDays, CheckCircle2, ChevronDown, ChevronUp, Copy, Download, Eye, EyeOff, Film, HardDriveDownload, ImageOff, LayoutGrid, List, Loader2, Play, Plus, Power, PowerOff, RefreshCw, RotateCcw, Search, Settings, ShieldCheck, Sparkles, Stethoscope, Trash2, Tv, Upload, Users, Wrench, X, Zap } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Chip, inputCls, KpiTile, QuotaBar, SectionCard, StatusPill } from '@/components/dvr-shared'
import api from '@/lib/api'

interface Provider {
  id: number
  name: string
  base_url: string
  username: string
  max_streams: number
  is_active: number
  priority: number
  provider_type: 'xc' | 'plex' | 'emby' | 'jellyfin' | 'dispatcharr_dvr'
  shared_connection_limit: number | null
  custom_user_agent: string | null
  has_password: boolean
  movie_count: number
  series_count: number
  episode_count: number
  synced_connection_count: number
  live_account_count: number
  import_exclude_categories: string[]
  dispatcharr_connection_id: number | null
  dvr_local_path: string | null
  dvr_movie_category_id: number | null
  dvr_series_category_id: number | null
}

interface RecordingProfile {
  id: number
  provider_id: number
  label: string
  tvg_id: string | null
  title: string
  title_mode: 'exact' | 'contains' | 'search' | 'regex'
  description: string | null
  description_mode: 'contains' | 'search' | 'regex'
  mode: 'all' | 'new'
  channel_id: number | null
  target_movie_category_id: number | null
  target_series_category_id: number | null
  dispatcharr_user_id: number | null
  backfill_mode: 'pointer' | 'download' | null
  monitored: number
  created_at: string
}

interface DispatcharrUser {
  id: number
  username: string
  stream_limit: number
  channel_profiles?: number[]
}

interface DispatcharrChannelProfile {
  id: number
  name: string
  channels: number[]
}

interface DvrUpcomingRecording {
  id: number
  channel: number
  start_time: string
  end_time: string
  custom_properties?: {
    program?: { title?: string; sub_title?: string | null }
    scheduled_by?: { dispatcharr_user_id?: number; dispatcharr_username?: string | null; profile_label?: string } | null
  }
}

interface EpgSearchProgram {
  id: number
  title: string
  sub_title: string | null
  start_time: string
  end_time: string
  tvg_id: string
  channels: { id: number; name: string; channel_number: number | null; channel_group: string | null; tvg_id: string }[]
}

interface DvrUserLimit {
  id: number
  provider_id: number
  dispatcharr_user_id: number
  dispatcharr_username: string
  stream_reserve: number
  disk_quota_bytes: number | null
  retention_max_age_days: number | null
  retention_max_episodes_per_show: number | null
  created_at: string
}

interface PortalAccount {
  id: number
  provider_id: number
  dispatcharr_user_id: number
  username: string
  totp_enabled: number
  totp_last_counter: number | null
  created_at: string
}

interface RetentionCandidateMovie {
  movie_id: number
  source_id: number
  name: string
  year: number | null
  created_at: string
  reason: string
}

interface WatchSession {
  id: number
  dispatcharr_connection_id: number
  client_id: string
  dispatcharr_user_id: number | null
  dispatcharr_username: string | null
  content_type: string | null
  content_name: string | null
  content_uuid: string | null
  client_ip: string | null
  bytes_sent: number
  position_seconds: number | null
  started_at: string
  last_seen_at: string
  ended_at: string | null
}

interface RetentionCandidateEpisode {
  episode_id: number
  source_id: number
  series_name: string
  season_number: number
  episode_number: number
  name: string
  created_at: string
}

interface DispatcharrConnection {
  id: number
  label: string
  url: string
  token: string
  vod_relay_account_id: number | null
  created_at: string
}

interface ProviderLiveAccount {
  id: number
  provider_id: number
  dispatcharr_connection_id: number
  dispatcharr_account_id: number
  connection_label: string
}

interface XcCredentials { username: string; password: string }

interface LockoutSettings {
  lockout_max_attempts: number
  lockout_window_seconds: number
  lockout_duration_seconds: number
}

interface RefreshSettings {
  catalog_refresh_seconds_xc: number
  catalog_refresh_seconds_plex: number
  catalog_refresh_seconds_emby: number
  catalog_refresh_seconds_jellyfin: number
  enrichment_ttl_seconds: number
  tmdb_sync_interval_seconds: number | null
}

interface BackupComponent {
  id: string
  label: string
  kind: 'json' | 'sqlite'
  exists: boolean
  size_bytes: number
  modified_at: number | null
}

interface ActivitySession {
  conn_id: string
  kind: 'movie' | 'series'
  title: string
  provider_name: string
  provider_type: 'xc' | 'plex' | 'emby' | 'jellyfin' | 'dispatcharr_dvr'
  started_at: number
  bytes_sent: number
  total_bytes: number
  duration_secs: number | null
  range_start_byte: number
}

interface NeedsReviewItem {
  id: number
  name: string
  year: number | null
  genre: string | null
  sample_episode_id?: number | null
  sample_source_id?: number | null
  sample_episode_source_id?: number | null
  imported_season_count?: number
  imported_episode_count?: number
}

interface NeedsReviewData {
  movies: NeedsReviewItem[]
  series: NeedsReviewItem[]
}

interface OrphanGroup {
  count: number
  sample: { id: number; name: string }[]
}

interface OrphanReport {
  orphaned_series: OrphanGroup
  sourceless_movies: OrphanGroup
  sourceless_episodes: OrphanGroup
}

interface TmdbSuggestion {
  tmdb_id: string
  name: string
  year: number | null
  poster_url: string | null
  overview: string | null
  vote_average: number | null
  season_count: number | null
  episode_count: number | null
  cast: string[]
}

interface MissingArtworkItem {
  id: number
  name: string
  year: number | null
}

interface DuplicateGroupItem {
  id: number
  name: string
  year: number
  tmdb_id: string | null
  poster_url: string | null
  source_count: number
  category_count: number
  provider_names: string[]
}

interface DuplicateGroup {
  items: DuplicateGroupItem[]
}

interface XcClient {
  id: number
  label: string
  username: string
  password: string
  enabled: boolean
  ip_allowlist: string | null
  category_allowlist: string | null
  created_at: string
  last_seen_at: string | null
  last_seen_ip: string | null
}

function formatElapsed(startedAt: number): string {
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - startedAt))
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

function buildStreamUrl(kind: 'movie' | 'series', exportId: number, ext: string, creds?: XcCredentials) {
  if (!creds) return null
  return `${window.location.origin}/${kind}/${creds.username}/${creds.password}/${exportId}.${ext}`
}

// Plays/copies a movie or episode directly by its own id — works even before
// it's placed in any category (placement-based export_stream_id only exists
// once placed; see xc_server.py's /preview/ routes for why).
function buildPreviewUrl(kind: 'movie' | 'series', itemId: number, ext: string, creds?: XcCredentials) {
  if (!creds) return null
  return `${window.location.origin}/preview/${kind}/${creds.username}/${creds.password}/${itemId}.${ext}`
}

// Forces one specific provider's copy — belongs on each Sources row (testing
// a particular provider's file), not on a category placement, which plays
// identically regardless of which category you look at it from.
function buildPreviewSourceUrl(kind: 'movie' | 'series', sourceId: number, ext: string, creds?: XcCredentials) {
  if (!creds) return null
  const path = kind === 'movie' ? 'movie-source' : 'series-source'
  return `${window.location.origin}/preview/${path}/${creds.username}/${creds.password}/${sourceId}.${ext}`
}

// Re-encodes to browser-compatible H.264/AAC on the fly — fallback for when
// the direct preview above fails on a codec the browser can't decode. No
// mid-stream seeking (single forward-only ffmpeg pipe, not HLS — see
// _transcode_vod_stream) — startSecs instead starts a fresh stream partway
// into the file (ffmpeg -ss before -i, a fast input-side seek), so jumping
// past an intro to verify a title doesn't mean watching the whole thing.
function buildTranscodedPreviewSourceUrl(kind: 'movie' | 'series', sourceId: number, creds?: XcCredentials, startSecs = 0) {
  if (!creds) return null
  const path = kind === 'movie' ? 'movie-source-transcoded' : 'series-source-transcoded'
  const url = `${window.location.origin}/preview/${path}/${creds.username}/${creds.password}/${sourceId}.mp4`
  return startSecs > 0 ? `${url}?start=${startSecs}` : url
}

// Same re-encode as above, but as a real HLS playlist (see xc_server.py's
// _serve_hls_playlist) instead of a single forward-only pipe -- gives the
// in-app player genuine seek support (backward across everything encoded so
// far; forward past the live edge is naturally blocked, same as any
// in-progress live/event HLS playlist). Slower to start than the plain
// transcode above (ffmpeg has to produce a first segment before anything
// plays), so this is offered as a separate choice, not a replacement.
function buildHlsPreviewSourceUrl(kind: 'movie' | 'series', sourceId: number, creds?: XcCredentials) {
  if (!creds) return null
  const path = kind === 'movie' ? 'movie-source-hls' : 'series-source-hls'
  return `${window.location.origin}/preview/${path}/${creds.username}/${creds.password}/${sourceId}/index.m3u8`
}

function CopyUrlButton({ url }: { url: string | null }) {
  const [copied, setCopied] = useState(false)
  if (!url) return null
  return (
    <button
      title="Copy playable stream URL"
      className="hover:text-foreground"
      onClick={() => { navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
    >
      {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
    </button>
  )
}

function PlayButton({ url, transcodedUrl, hlsUrl, title }: { url: string | null; transcodedUrl?: string | null; hlsUrl?: string | null; title: string }) {
  const [open, setOpen] = useState(false)
  if (!url) return null
  return (
    <>
      <button title="Play" className="hover:text-foreground" onClick={() => setOpen(true)}>
        <Play size={12} />
      </button>
      {open && <VodPlayer url={url} transcodedUrl={transcodedUrl} hlsUrl={hlsUrl} title={title} onClose={() => setOpen(false)} />}
    </>
  )
}

// Sonarr/Radarr-style gap view for one series -- every canonical (TMDB)
// episode not yet in the pool, with a "Find" action that runs the backend
// cascade the user specced 2026-07-27: pool backfill first (silent,
// instant), else EPG candidates to pick from (nothing auto-scheduled --
// picking a channel is a real decision, same reasoning as the Scheduled
// Recordings picker), else a flagged-for-review message.
function MissingEpisodesPanel({ series, providerId, qc }: {
  series: Series
  providerId: number
  qc: ReturnType<typeof useQueryClient>
}) {
  const missingQuery = useQuery<MissingEpisode[]>({
    queryKey: ['vod-missing-episodes', series.id],
    queryFn: () => api.get(`/vod/series/${series.id}/missing-episodes/`).then((r) => r.data),
  })
  const [resolving, setResolving] = useState<string | null>(null)
  const [resolveResult, setResolveResult] = useState<Record<string, { resolved: boolean; mode: string | null; candidates: EpgSearchProgram[]; message: string | null }>>({})

  async function findEpisode(ep: MissingEpisode) {
    const key = `${ep.season_number}-${ep.episode_number}`
    setResolving(key)
    try {
      const res = await api.post(`/vod/series/${series.id}/missing-episodes/resolve/`, {
        provider_id: providerId, season_number: ep.season_number, episode_number: ep.episode_number, episode_name: ep.name,
      })
      setResolveResult((prev) => ({ ...prev, [key]: res.data }))
      if (res.data.resolved) {
        qc.invalidateQueries({ queryKey: ['vod-missing-episodes', series.id] })
        qc.invalidateQueries({ queryKey: ['vod-dvr-library-series', providerId] })
        qc.invalidateQueries({ queryKey: ['vod-series'] })
      }
    } catch (e: any) {
      setResolveResult((prev) => ({ ...prev, [key]: { resolved: false, mode: null, candidates: [], message: e?.response?.data?.detail ?? e.message ?? 'Failed.' } }))
    } finally {
      setResolving(null)
    }
  }

  async function scheduleCandidate(ep: MissingEpisode, program: EpgSearchProgram, channelId: number | undefined) {
    if (!channelId) return
    const key = `${ep.season_number}-${ep.episode_number}`
    setResolving(key)
    try {
      await api.post(`/vod/series/${series.id}/missing-episodes/schedule/`, { provider_id: providerId, channel_id: channelId, program })
      setResolveResult((prev) => { const next = { ...prev }; delete next[key]; return next })
      qc.invalidateQueries({ queryKey: ['vod-missing-episodes', series.id] })
      qc.invalidateQueries({ queryKey: ['vod-dvr-upcoming', providerId] })
    } catch (e: any) {
      alert(e?.response?.data?.detail ?? e.message ?? 'Failed to schedule.')
    } finally {
      setResolving(null)
    }
  }

  if (missingQuery.isLoading) return <p className="text-[11px] text-muted-foreground px-1">Loading canonical episode list…</p>
  if (missingQuery.isError) return <p className="text-[11px] text-destructive px-1">{(missingQuery.error as any)?.response?.data?.detail ?? 'Failed to load TMDB episode list.'}</p>
  const missing = (missingQuery.data ?? []).filter((e) => !e.in_pool)
  if (!missing.length) return <p className="text-[11px] text-muted-foreground px-1">No gaps — every canonical episode is already in the pool.</p>

  const bySeason = new Map<number, MissingEpisode[]>()
  for (const ep of missing) {
    if (!bySeason.has(ep.season_number)) bySeason.set(ep.season_number, [])
    bySeason.get(ep.season_number)!.push(ep)
  }

  return (
    <div className="space-y-3">
      {[...bySeason.entries()].sort((a, b) => a[0] - b[0]).map(([season, eps]) => (
        <div key={season}>
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Season {season}</span>
            <span className="h-px flex-1 bg-border" />
          </div>
          <div className="space-y-1.5">
            {eps.sort((a, b) => a.episode_number - b.episode_number).map((ep) => {
              const key = `${ep.season_number}-${ep.episode_number}`
              const result = resolveResult[key]
              const busy = resolving === key
              return (
                <div key={key} className="rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-muted-foreground shrink-0 w-9 tabular-nums">E{ep.episode_number}</span>
                    <span className="flex-1 truncate">
                      <span className="font-semibold">{ep.name || 'Untitled'}</span>
                      {ep.air_date && <span className="text-muted-foreground"> ({ep.air_date})</span>}
                    </span>
                    {ep.flagged_unresolved && <StatusPill tone="warning" label="flagged" />}
                    <button
                      className="rounded-md border border-border bg-background px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:text-foreground hover:border-primary/40 flex items-center gap-1 shrink-0 disabled:opacity-50"
                      disabled={busy}
                      onClick={() => findEpisode(ep)}
                    >
                      {busy ? <Loader2 size={11} className="animate-spin" /> : <Search size={11} />} Find
                    </button>
                  </div>
                  {result?.resolved && (
                    <div className="mt-2 pt-2 border-t border-border">
                      <StatusPill
                        tone="success"
                        label={
                          result.mode === 'recorded'
                            ? "Found on this show's usual channel and recorded -- no manual pick needed."
                            : result.mode === 'already_scheduled'
                              ? 'Already has a real recording scheduled for this exact airing.'
                              : `Backfilled (${result.mode}) from the pool instead of recording.`
                        }
                      />
                    </div>
                  )}
                  {result && !result.resolved && result.candidates.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-border space-y-1">
                      <p className="text-muted-foreground text-[11px]">{result.message || 'Not in the pool -- pick a channel/airing to record:'}</p>
                      {result.candidates.slice(0, 8).map((c, i) => (
                        <button
                          key={i}
                          className="flex items-center gap-1.5 w-full text-left rounded-md border border-border bg-background hover:border-primary/40 hover:bg-accent px-2 py-1 disabled:opacity-50"
                          disabled={busy || !c.channels?.[0]?.id}
                          onClick={() => scheduleCandidate(ep, c, c.channels?.[0]?.id)}
                        >
                          <span className="flex-1 truncate">{c.channels?.[0]?.name ?? c.tvg_id} — {c.title}{c.sub_title ? `: ${c.sub_title}` : ''}</span>
                          <span className="text-muted-foreground shrink-0">{c.start_time ? new Date(c.start_time).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' }) : ''}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  {result && !result.resolved && result.candidates.length === 0 && result.message && (
                    <div className="mt-2 pt-2 border-t border-border">
                      <StatusPill tone="warning" label={result.message} />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

// Dedicated cross-rule Missing Episodes page -- covers every show with an
// active Recording Rule, not just ones this specific provider has already
// recorded an episode for (the old per-series toggle buried inside DVR
// Library only ever showed up for already-partially-recorded shows, real
// gap found live-testing 2026-07-27). Resolves each rule's own title to a
// pool series via the existing search endpoint (no new backend route
// needed), then reuses MissingEpisodesPanel unchanged -- same Find
// cascade, just surfaced somewhere any show with a rule can be found,
// recorded or not.
function RuleMissingBlock({ rule, providerId, qc }: {
  rule: RecordingProfile
  providerId: number
  qc: ReturnType<typeof useQueryClient>
}) {
  const [expanded, setExpanded] = useState(false)
  const seriesQuery = useQuery<{ items: Series[]; total: number }>({
    queryKey: ['vod-series-search-for-rule', rule.title],
    // limit=50, not a small number -- a common title (e.g. "This Is Us")
    // can have several regional/localized prefixed variants ("AR| This Is
    // Us") sorted ahead of the real exact-name match, confirmed live
    // 2026-07-27 that limit=5 missed it entirely even though the pool had
    // it. Cheap either way -- this is a handful of rules, not the whole
    // library.
    queryFn: () => api.get('/vod/series/', { params: { search: rule.title, limit: 50 } }).then((r) => r.data),
  })
  const matched = seriesQuery.data?.items.find((s) => s.name.trim().toLowerCase() === rule.title.trim().toLowerCase())
  // Same query key MissingEpisodesPanel itself uses -- react-query dedupes
  // this into one request and shares the cache, so expanding is instant
  // and the collapsed header's count never costs a second fetch.
  const missingQuery = useQuery<MissingEpisode[]>({
    queryKey: ['vod-missing-episodes', matched?.id],
    queryFn: () => api.get(`/vod/series/${matched!.id}/missing-episodes/`).then((r) => r.data),
    enabled: !!matched?.tmdb_id,
  })
  const gapCount = missingQuery.data?.filter((e) => !e.in_pool).length

  if (seriesQuery.isLoading) {
    return <div className="rounded-lg border border-border bg-card px-3 py-2.5 shadow-sm text-xs text-muted-foreground">Looking up "{rule.title}"…</div>
  }
  if (!matched) {
    return (
      <div className="rounded-lg border border-border bg-card px-3 py-2.5 shadow-sm">
        <div className="text-[13px] font-semibold">{rule.title}</div>
        <div className="text-[11px] text-muted-foreground mt-0.5">Not in the pool yet -- nothing to diff against until at least one episode has been recorded or backfilled.</div>
      </div>
    )
  }
  if (!matched.tmdb_id) {
    return (
      <div className="rounded-lg border border-border bg-card px-3 py-2.5 shadow-sm">
        <div className="text-[13px] font-semibold">{matched.name}</div>
        <div className="text-[11px] text-muted-foreground mt-0.5">No TMDB match yet -- nothing canonical to diff against.</div>
      </div>
    )
  }
  return (
    <div className="rounded-lg border border-border bg-card shadow-sm overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left hover:bg-accent/50"
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? <ChevronUp size={14} className="text-muted-foreground shrink-0" /> : <ChevronDown size={14} className="text-muted-foreground shrink-0" />}
        <span className="text-[13px] font-semibold flex-1 truncate">{matched.name}</span>
        {missingQuery.isLoading ? (
          <span className="text-[11px] text-muted-foreground">checking…</span>
        ) : gapCount === 0 ? (
          <StatusPill tone="success" label="no gaps" />
        ) : gapCount != null ? (
          <StatusPill tone="warning" label={`${gapCount} missing`} />
        ) : null}
      </button>
      {expanded && (
        <div className="px-3 pb-3 pt-1 border-t border-border">
          <MissingEpisodesPanel series={matched} providerId={providerId} qc={qc} />
        </div>
      )}
    </div>
  )
}

// Merges/overwrites a ?start=<secs> query param — used to restart the
// transcoded stream partway into the file (see buildTranscodedPreviewSourceUrl).
function withStartParam(url: string, startSecs: number): string {
  const u = new URL(url)
  if (startSecs > 0) u.searchParams.set('start', String(startSecs))
  else u.searchParams.delete('start')
  return u.toString()
}

const JUMP_MARKS_SECS = [0, 120, 300, 600, 1200]

function VodPlayer({ url, transcodedUrl, hlsUrl, title, onClose }: {
  url: string; transcodedUrl?: string | null; hlsUrl?: string | null; title: string; onClose: () => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [status, setStatus] = useState<'loading' | 'playing' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<'direct' | 'transcode' | 'hls'>('direct')
  const [jumpSecs, setJumpSecs] = useState(0)
  const activeUrl = mode === 'transcode' && transcodedUrl
    ? (jumpSecs > 0 ? withStartParam(transcodedUrl, jumpSecs) : transcodedUrl)
    : mode === 'hls' && hlsUrl
      ? hlsUrl
      : url

  function jumpTo(secs: number) {
    setJumpSecs(secs)
    setStatus('loading')
    setError(null)
  }

  // hls.js attaches to the <video> element itself rather than a plain `src`
  // (only Safari plays .m3u8 natively) — wire/tear down manually instead of
  // the plain src= attribute the direct/transcode modes use below.
  useEffect(() => {
    const video = videoRef.current
    if (!video || mode !== 'hls' || !hlsUrl) return
    if (Hls.isSupported()) {
      const hls = new Hls({ liveSyncDurationCount: 6 })
      hlsRef.current = hls
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data.fatal) {
          setStatus('error')
          setError('HLS playback failed — the transcode may have failed to start or the source is unreachable.')
        }
      })
      hls.loadSource(hlsUrl)
      hls.attachMedia(video)
      return () => { hls.destroy(); hlsRef.current = null }
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl  // Safari: native HLS, no hls.js needed
    } else {
      setStatus('error')
      setError('This browser has no HLS support.')
    }
  }, [mode, hlsUrl])

  return createPortal(
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80" onClick={onClose}>
      <div
        className="relative bg-card border border-border rounded-xl overflow-hidden w-full max-w-3xl mx-4 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
          <div className="flex items-center gap-2 min-w-0">
            <Play size={13} className="text-primary shrink-0" />
            <span className="text-sm font-medium truncate">
              {title}{mode === 'transcode' && ' (transcoded)'}{mode === 'hls' && ' (HLS, seekable)'}
            </span>
          </div>
          <button className="text-muted-foreground hover:text-foreground transition-colors p-1 rounded hover:bg-accent shrink-0 ml-2" onClick={onClose}>
            <X size={16} />
          </button>
        </div>

        {status === 'error' && error && (
          <div className="px-6 py-10 space-y-2 text-center">
            <div className="flex items-center justify-center gap-2 text-sm text-destructive">
              <AlertCircle size={14} className="shrink-0" />
              <span>{error}</span>
            </div>
            {mode === 'direct' && (transcodedUrl || hlsUrl) ? (
              <>
                <p className="text-xs text-muted-foreground">
                  This is usually a codec this browser can't decode natively (e.g. AVI, DTS/AC-3 audio) — the file
                  itself relayed fine. Try a transcoded copy instead, or use Copy URL with an external player.
                </p>
                <div className="flex items-center justify-center gap-2">
                  {transcodedUrl && (
                    <Button size="sm" variant="outline" onClick={() => { setMode('transcode'); setStatus('loading'); setError(null) }}>
                      Try transcoded playback
                    </Button>
                  )}
                  {hlsUrl && (
                    <Button size="sm" variant="outline" onClick={() => { setMode('hls'); setStatus('loading'); setError(null) }}>
                      Try HLS (seekable, slower start)
                    </Button>
                  )}
                </div>
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                The source provider may be down — failover already tried every active source for this item.
              </p>
            )}
          </div>
        )}

        {mode === 'hls' ? (
          <video
            ref={videoRef}
            controls
            autoPlay
            className={status === 'error' ? 'hidden' : 'w-full max-h-[70vh] bg-black'}
            onCanPlay={() => setStatus('playing')}
          />
        ) : (
          <video
            ref={videoRef}
            src={activeUrl}
            controls
            autoPlay
            className={status === 'error' ? 'hidden' : 'w-full max-h-[70vh] bg-black'}
            onCanPlay={() => setStatus('playing')}
            onError={() => { setStatus('error'); setError('Playback failed — the file may be unreachable or use a codec this browser can\'t play.') }}
          />
        )}
        {status === 'loading' && (
          <div className="absolute inset-0 top-[41px] flex items-center justify-center gap-2 text-sm text-muted-foreground pointer-events-none">
            <Loader2 size={14} className="animate-spin" /> Loading{mode === 'hls' && ' (starting transcode, first segment takes a few seconds)'}…
          </div>
        )}

        {mode === 'transcode' && status !== 'error' && (
          <div className="flex items-center gap-1.5 px-4 py-2 border-t border-border text-xs">
            <span className="text-muted-foreground">Jump to (no mid-stream scrubbing — starts a fresh stream):</span>
            {JUMP_MARKS_SECS.map((secs) => (
              <Button
                key={secs}
                size="sm"
                variant={jumpSecs === secs ? 'default' : 'outline'}
                onClick={() => jumpTo(secs)}
              >
                {secs === 0 ? 'Start' : `${Math.floor(secs / 60)}m`}
              </Button>
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

// Small reusable overlay wrapper -- createPortal + backdrop + centered card +
// corner close button, extracted from VodPlayer's inline pattern above so the
// per-content-type Categories/Needs Review modals (and grid-mode item detail)
// don't each duplicate that boilerplate. Purely a shell -- callers supply
// their own header/body content as children, including any title bar.
function Modal({ onClose, children, maxWidth = 'max-w-lg' }: { onClose: () => void; children: React.ReactNode; maxWidth?: string }) {
  return createPortal(
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80 p-4" onClick={onClose}>
      <div
        className={`relative bg-card border border-border rounded-xl overflow-hidden w-full ${maxWidth} shadow-2xl max-h-[85vh] flex flex-col`}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          className="absolute top-2 right-2 text-muted-foreground hover:text-foreground transition-colors p-1 rounded hover:bg-accent z-10"
          onClick={onClose}
        >
          <X size={16} />
        </button>
        {children}
      </div>
    </div>,
    document.body,
  )
}

interface Category {
  id: number
  name: string
  content_type: 'movie' | 'series'
  is_smart: number
  rule_json: string | null
  sync_source: string | null
  sort_order: number
  ai_description: string | null
  is_active: number
  schedule_start_mmdd: string | null
  schedule_end_mmdd: string | null
}

const PROVIDER_TYPE_LABELS: Record<'xc' | 'plex' | 'emby' | 'jellyfin' | 'dispatcharr_dvr', string> = {
  xc: 'Xtream-Codes', plex: 'Plex', emby: 'Emby', jellyfin: 'Jellyfin', dispatcharr_dvr: 'Dispatcharr DVR',
}

// Best-effort friendly names for provider name-prefix codes (e.g. "AR|",
// "EN|"). These are arbitrary provider-chosen tags, not a real standard --
// mostly ISO 639-1 language codes, but IPTV providers also commonly use
// country-style or regional codes (BR, EXYU, ALB, MULTI-LANG...) that have
// no ISO equivalent. Unknown codes just show their raw form, never hidden.
const LANGUAGE_CODE_NAMES: Record<string, string> = {
  EN: 'English', AR: 'Arabic', FR: 'French', ES: 'Spanish', DE: 'German',
  IT: 'Italian', PT: 'Portuguese', BR: 'Brazilian Portuguese', RU: 'Russian',
  TR: 'Turkish', PL: 'Polish', NL: 'Dutch', GR: 'Greek', HU: 'Hungarian',
  BG: 'Bulgarian', RO: 'Romanian', SE: 'Swedish', NO: 'Norwegian', DK: 'Danish',
  FI: 'Finnish', CZ: 'Czech', SK: 'Slovak', HR: 'Croatian', SR: 'Serbian',
  SL: 'Slovenian', UA: 'Ukrainian', IN: 'Hindi/Indian', HI: 'Hindi',
  ZH: 'Chinese', CN: 'Chinese', JA: 'Japanese', JP: 'Japanese', KO: 'Korean',
  KR: 'Korean', TH: 'Thai', VI: 'Vietnamese', ID: 'Indonesian', MY: 'Malay',
  HE: 'Hebrew', FA: 'Persian/Farsi', UR: 'Urdu', BN: 'Bengali', TA: 'Tamil',
  TE: 'Telugu', PK: 'Pakistani', AF: 'Afrikaans', SW: 'Swahili',
  ALB: 'Albanian', EXYU: 'Ex-Yugoslavia (regional)', LT: 'Lithuanian',
  LV: 'Latvian', EE: 'Estonian', GE: 'Georgian', AM: 'Armenian',
  AZ: 'Azerbaijani', KZ: 'Kazakh', 'MULTI-LANG': 'Multi-language/Undetermined',
  MULTI: 'Multi-language/Undetermined', SC: 'Subtitled/Sub-Content',
  IR: 'Iranian', KU: 'Kurdish', PH: 'Filipino', AL: 'Albanian',
  SOM: 'Somali', MT: 'Maltese', MA: 'Moroccan', IL: 'Israeli', GB: 'British English',
  CHI: 'Chinese', LAT: 'Latin America (regional)',
  // CH, BL, NF, INI, JK, ENN, UXYU, ENYU intentionally left unlabeled --
  // no confident real-world meaning (could be Chinese/Swiss/something
  // else entirely for CH, and the rest look like provider-specific
  // shorthand with no reliable interpretation). A wrong guess here is
  // worse than just showing the raw code.
}
type AiProvider = 'anthropic' | 'openai' | 'gemini'
const AI_PROVIDER_DEFAULT_MODELS: Record<AiProvider, string> = {
  anthropic: 'claude-haiku-4-5-20251001', openai: 'gpt-5-mini', gemini: 'gemini-2.5-flash',
}
const AI_PROVIDER_MODEL_OPTIONS: Record<AiProvider, { id: string; label: string }[]> = {
  anthropic: [
    { id: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5 — cheapest, fastest (default)' },
    { id: 'claude-sonnet-5', label: 'Claude Sonnet 5 — balanced' },
    { id: 'claude-opus-4-8', label: 'Claude Opus 4.8 — most capable, priciest' },
  ],
  openai: [
    { id: 'gpt-5-mini', label: 'GPT-5 Mini — cheapest, fastest (default)' },
    { id: 'gpt-5', label: 'GPT-5 — most capable, priciest' },
  ],
  gemini: [
    { id: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash — cheapest, fastest (default)' },
    { id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro — most capable, priciest' },
  ],
}
const RULE_FIELDS = ['name', 'genre', 'year', 'language', 'director', 'is_adult'] as const
const RULE_OPS = ['contains', 'equals', 'starts_with', 'gte', 'lte'] as const
const REWRITABLE_FIELDS = ['name', 'genre', 'description', 'director', 'cast_list', 'country'] as const

interface MovieSource { id: number; provider_id: number; provider_stream_id: string; container_extension: string; provider_name: string; provider_category_name?: string; file_size_bytes?: number | null }
interface MoviePlacement { id: number; category_id: number; export_stream_id: number; name_suffix: string; category_name: string }
interface Movie {
  id: number
  name: string
  year: number | null
  genre: string | null
  description: string | null
  poster_url: string | null
  is_adult: number
  review_excluded: number
  sources: MovieSource[]
  placements: MoviePlacement[]
}

interface MetadataRule {
  id: number
  content_type: 'movie' | 'series' | 'both'
  field: string
  pattern: string
  replacement: string
  is_active: number
  sort_order: number
}

interface EpisodeSource { id: number; provider_id: number; provider_stream_id: string; container_extension: string; provider_name: string; file_size_bytes?: number | null }
interface Episode { id: number; season_number: number; episode_number: number; name: string; export_episode_id: number; sources: EpisodeSource[] }
interface SeriesPlacement { id: number; category_id: number; export_series_id: number; name_suffix: string; category_name: string }
interface Series {
  id: number
  name: string
  year: number | null
  genre: string | null
  description: string | null
  poster_url: string | null
  is_adult: number
  review_excluded: number
  import_provider_name: string | null
  tmdb_id: string | null
  episodes: Episode[]
  placements: SeriesPlacement[]
}

interface MissingEpisode {
  season_number: number
  episode_number: number
  name: string | null
  air_date: string | null
  in_pool: boolean
  flagged_unresolved: boolean
}

interface UnresolvedMissingEpisode {
  id: number
  series_id: number
  series_name: string
  season_number: number
  episode_number: number
  episode_name: string | null
  checked_at: string
}

interface DvrRecordingFailure {
  id: number
  provider_id: number
  dispatcharr_recording_id: number
  title: string
  season_number: number | null
  episode_number: number | null
  original_channel_id: number | null
  interrupted_reason: string | null
  outcome: 'rescheduled' | 'unresolved'
  replacement_channel_id: number | null
  detected_at: string
}

interface EnrichProgress {
  running: boolean
  movies_total: number; movies_done: number; movies_errors: number
  series_total: number; series_done: number; series_errors: number
  started_at: number | null; finished_at: number | null
}

interface Page<T> { items: T[]; total: number; limit: number; offset: number }

// SectionCard/KpiTile/StatusPill/Chip/QuotaBar/inputCls moved to
// @/components/dvr-shared so the end-user portal (src/pages/Portal.tsx) can
// share them without importing this whole file.

function Pager({ total, limit, offset, onOffset }: { total: number; limit: number; offset: number; onOffset: (o: number) => void }) {
  if (total <= limit) return null
  const page = Math.floor(offset / limit) + 1
  const pages = Math.ceil(total / limit)
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}>Prev</Button>
      <span>page {page} of {pages} · {total} total</span>
      <Button size="sm" variant="outline" disabled={offset + limit >= total} onClick={() => onOffset(offset + limit)}>Next</Button>
    </div>
  )
}

const PAGE_SIZE_OPTIONS = [25, 50, 100, 200]

function PageSizeSelect({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <select className={inputCls()} value={value} onChange={(e) => onChange(Number(e.target.value))} title="Items per page">
      {PAGE_SIZE_OPTIONS.map((n) => <option key={n} value={n}>{n} / page</option>)}
    </select>
  )
}

// One flagged item: no year, ambiguous against 2+ existing pool entries with
// the same name. TMDB suggestions are fetched on demand (only once expanded)
// rather than eagerly for every flagged item on page load.
// Highlights whether our own imported season/episode counts line up with a
// TMDB candidate's — a useful secondary signal when the name/year alone
// don't settle it, though not proof either way: providers routinely have an
// incomplete catalog (missing seasons, gaps), so a mismatch just means
// "worth a second look," not "wrong."
function SeasonEpisodeMatch({ imported, candidate, label }: { imported?: number; candidate: number | null; label: string }) {
  if (imported == null || candidate == null) return null
  const close = Math.abs(imported - candidate) <= 1
  return (
    <span className={close ? 'text-green-600 dark:text-green-500' : 'text-muted-foreground'}>
      {label}: {imported} vs {candidate}{close ? ' ✓' : ''}
    </span>
  )
}

function NeedsReviewRow({ contentType, item, qc, xcCredentials }: {
  contentType: 'movie' | 'series'
  item: NeedsReviewItem
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
}) {
  const [expanded, setExpanded] = useState(false)
  const [manualYear, setManualYear] = useState('')

  // Movies preview directly off their own id; series need a specific episode
  // (see xc_server.py's /preview/series/ route) — sample_episode_id is the
  // first episode we've actually imported for this flagged series, if any.
  // Transcoded fallback needs the specific *source* row, not the movie/
  // episode id — required for anything the browser can't decode natively
  // (e.g. Plex-sourced .avi files, a real case hit in this exact panel).
  const previewUrl = contentType === 'movie'
    ? buildPreviewUrl('movie', item.id, 'mp4', xcCredentials)
    : item.sample_episode_id
      ? buildPreviewUrl('series', item.sample_episode_id, 'mp4', xcCredentials)
      : null
  const transcodedUrl = contentType === 'movie'
    ? (item.sample_source_id ? buildTranscodedPreviewSourceUrl('movie', item.sample_source_id, xcCredentials) : null)
    : (item.sample_episode_source_id ? buildTranscodedPreviewSourceUrl('series', item.sample_episode_source_id, xcCredentials) : null)
  const hlsUrl = contentType === 'movie'
    ? (item.sample_source_id ? buildHlsPreviewSourceUrl('movie', item.sample_source_id, xcCredentials) : null)
    : (item.sample_episode_source_id ? buildHlsPreviewSourceUrl('series', item.sample_episode_source_id, xcCredentials) : null)

  // Same content is sometimes released under a different title in a
  // different region -- the default search (this item's own stored name)
  // won't find a match TMDB's index doesn't already associate with that
  // exact string, so let the reviewer search a different title when they
  // suspect/know one. Empty means "use the stored name" (the default).
  const [searchOverride, setSearchOverride] = useState('')
  const suggestionsQuery = useQuery<TmdbSuggestion[]>({
    queryKey: ['vod-needs-review-suggestions', contentType, item.id, searchOverride],
    queryFn:  () => api.get(`/vod/needs-review/${contentType}/${item.id}/suggestions/`, {
      params: searchOverride ? { q: searchOverride } : {},
    }).then((r) => r.data),
    enabled:  expanded,
    retry:    false,
  })

  const resolve = useMutation({
    mutationFn: (body: { year: number; tmdb_id?: string }) =>
      api.post(`/vod/needs-review/${contentType}/${item.id}/resolve/`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-needs-review'] })
      qc.invalidateQueries({ queryKey: contentType === 'movie' ? ['vod-movies'] : ['vod-series'] })
    },
  })

  // Asks Claude to pick the most likely correct match among the same TMDB
  // candidates already shown above -- purely a hint (name + reasoning +
  // confidence) the reviewer weighs before still clicking Resolve
  // themselves; never applies anything on its own.
  const aiSuggest = useMutation({
    mutationFn: () => api.get(`/vod/needs-review/${contentType}/${item.id}/ai-suggest/`, {
      params: searchOverride ? { q: searchOverride } : {},
    }),
  })

  // Flagged series often have no episodes yet -- they were never placed in a
  // category, so they never went through normal enrichment (which is also
  // what fetches episode listings). Fetch on demand so there's something to
  // preview instead of just a name to guess from, and so the imported
  // season/episode counts below have something real to compare against.
  const [fetchEpisodesMessage, setFetchEpisodesMessage] = useState<string | null>(null)
  const fetchEpisodes = useMutation({
    mutationFn: () => api.post(`/vod/series/${item.id}/enrich/`, null, { params: { force: true } }),
    onSuccess: (r) => {
      if (r.data.fetched) {
        setFetchEpisodesMessage(null)
        qc.invalidateQueries({ queryKey: ['vod-needs-review'] })
      } else {
        // e.g. "the provider this series was originally imported from no
        // longer exists" — previously this looked identical to success
        // (button just went back to normal, nothing shown).
        setFetchEpisodesMessage(r.data.reason ?? 'Nothing fetched.')
      }
    },
    onError: (e: any) => setFetchEpisodesMessage(e?.response?.data?.detail ?? e.message),
  })

  return (
    <li className="border-b border-border/50 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate flex items-center gap-1.5">
          <PlayButton url={previewUrl} transcodedUrl={transcodedUrl} hlsUrl={hlsUrl} title={item.name} />
          {item.name} {item.genre && <span className="text-muted-foreground">({item.genre})</span>}
          {contentType === 'series' && !!item.imported_episode_count && (
            <span className="text-muted-foreground">
              — imported: {item.imported_season_count} season{item.imported_season_count === 1 ? '' : 's'}, {item.imported_episode_count} episode{item.imported_episode_count === 1 ? '' : 's'}
            </span>
          )}
          {contentType === 'series' && !item.sample_episode_id && (
            <button
              className="text-muted-foreground hover:text-foreground underline decoration-dotted shrink-0"
              disabled={fetchEpisodes.isPending}
              onClick={() => { setFetchEpisodesMessage(null); fetchEpisodes.mutate() }}
            >
              {fetchEpisodes.isPending ? 'fetching…' : 'fetch episodes to preview'}
            </button>
          )}
          {fetchEpisodesMessage && <span className="text-destructive">{fetchEpisodesMessage}</span>}
        </span>
        <button
          className="text-muted-foreground hover:text-foreground shrink-0"
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>

      {expanded && (
        <div className="mt-2 space-y-2">
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">search TMDB as:</span>
            <input
              className={inputCls('w-40')}
              placeholder={item.name}
              defaultValue={searchOverride}
              onKeyDown={(e) => { if (e.key === 'Enter') setSearchOverride((e.target as HTMLInputElement).value.trim()) }}
              onBlur={(e) => setSearchOverride(e.target.value.trim())}
              title="Same title is sometimes released under a different name in a different region — search a different one if you suspect that's the case here"
            />
            <Button size="sm" variant="outline" disabled={!suggestionsQuery.data?.length || aiSuggest.isPending} onClick={() => aiSuggest.mutate()}>
              {aiSuggest.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Sparkles size={12} className="mr-1" />Ask AI</>}
            </Button>
          </div>
          {aiSuggest.isError && (
            <p className="text-destructive">AI suggestion failed — check the AI provider/API key in API Keys settings.</p>
          )}
          {aiSuggest.data && (
            <p className="text-muted-foreground border border-border rounded px-2 py-1">
              <Sparkles size={11} className="inline mr-1" />
              {aiSuggest.data.data.best_match_index != null && suggestionsQuery.data?.[aiSuggest.data.data.best_match_index]
                ? <>AI suggests <strong className="text-foreground">{suggestionsQuery.data[aiSuggest.data.data.best_match_index].name}</strong> ({aiSuggest.data.data.confidence} confidence) — {aiSuggest.data.data.reasoning}</>
                : <>AI found no confident match — {aiSuggest.data.data.reasoning}</>}
            </p>
          )}
          {suggestionsQuery.isLoading && <p className="text-muted-foreground">Searching TMDB…</p>}
          {suggestionsQuery.isError && <p className="text-destructive">TMDB search failed — check the API key in Rich Metadata settings.</p>}
          {!!suggestionsQuery.data?.length && (
            <div className="space-y-1.5">
              {suggestionsQuery.data.map((s) => (
                <button
                  key={s.tmdb_id}
                  disabled={resolve.isPending}
                  className="flex items-start gap-2 w-full border border-border rounded px-2 py-1.5 hover:bg-accent text-left"
                  onClick={() => resolve.mutate({ year: s.year ?? 0, tmdb_id: s.tmdb_id })}
                >
                  {s.poster_url
                    ? <img src={s.poster_url} alt="" className="w-10 h-14 object-cover rounded shrink-0" />
                    : <div className="w-10 h-14 rounded bg-muted shrink-0" />}
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{s.name} {s.year ? `(${s.year})` : ''}</span>
                      {s.vote_average != null && <span className="text-muted-foreground">★ {s.vote_average.toFixed(1)}</span>}
                    </div>
                    {contentType === 'series' && (s.season_count != null || s.episode_count != null) && (
                      <div className="flex items-center gap-2 text-muted-foreground">
                        <SeasonEpisodeMatch imported={item.imported_season_count} candidate={s.season_count} label="seasons" />
                        <SeasonEpisodeMatch imported={item.imported_episode_count} candidate={s.episode_count} label="episodes" />
                      </div>
                    )}
                    {!!s.cast.length && <p className="text-muted-foreground">Cast: {s.cast.join(', ')}</p>}
                    {s.overview && <p className="text-muted-foreground line-clamp-2">{s.overview}</p>}
                  </div>
                </button>
              ))}
            </div>
          )}
          {suggestionsQuery.data && suggestionsQuery.data.length === 0 && (
            <p className="text-muted-foreground">No TMDB matches found for this name.</p>
          )}

          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">or set year manually:</span>
            <input
              className={inputCls('w-16')}
              type="number"
              placeholder="year"
              value={manualYear}
              onChange={(e) => setManualYear(e.target.value)}
            />
            <Button
              size="sm"
              disabled={!manualYear || resolve.isPending}
              onClick={() => resolve.mutate({ year: Number(manualYear) })}
            >
              Resolve
            </Button>
          </div>
        </div>
      )}
    </li>
  )
}

function MissingArtworkRow({ contentType, item, qc, selected, onToggleSelect }: {
  contentType: 'movie' | 'series'
  item: MissingArtworkItem
  qc: ReturnType<typeof useQueryClient>
  selected: boolean
  onToggleSelect: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [searchOverride, setSearchOverride] = useState('')
  const [manualPosterUrl, setManualPosterUrl] = useState('')

  // Same reasoning as NeedsReviewRow's search override -- a mangled/
  // punctuation-stripped stored title (the actual cause of most missing
  // artwork) often just doesn't find its real TMDB entry, so let the
  // reviewer try a cleaned-up query instead of the stored name verbatim.
  const suggestionsQuery = useQuery<TmdbSuggestion[]>({
    queryKey: ['vod-missing-artwork-suggestions', contentType, item.id, searchOverride],
    queryFn:  () => api.get(`/vod/missing-artwork/${contentType}/${item.id}/suggestions/`, {
      params: searchOverride ? { q: searchOverride } : {},
    }).then((r) => r.data),
    enabled:  expanded,
    retry:    false,
  })

  const resolve = useMutation({
    mutationFn: (body: { poster_url: string; tmdb_id?: string; name?: string; year?: number }) =>
      api.post(`/vod/missing-artwork/${contentType}/${item.id}/resolve/`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-missing-artwork'] })
      qc.invalidateQueries({ queryKey: contentType === 'movie' ? ['vod-movies'] : ['vod-series'] })
    },
  })

  // Same "purely a recommendation" contract as Needs Review's Ask AI --
  // whichever provider is configured in Settings picks among the same TMDB
  // candidates shown above; nothing is ever applied without an explicit click.
  const aiSuggest = useMutation({
    mutationFn: () => api.get(`/vod/missing-artwork/${contentType}/${item.id}/ai-suggest/`, {
      params: searchOverride ? { q: searchOverride } : {},
    }),
  })

  return (
    <li className="border-b border-border/50 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate flex items-center gap-1.5">
          <input type="checkbox" checked={selected} onChange={onToggleSelect} title="Select for bulk action" />
          <ImageOff size={12} className="text-muted-foreground shrink-0" />
          {item.name} {item.year && <span className="text-muted-foreground">({item.year})</span>}
        </span>
        <button className="text-muted-foreground hover:text-foreground shrink-0" onClick={() => setExpanded((e) => !e)}>
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>

      {expanded && (
        <div className="mt-2 space-y-2">
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">search TMDB as:</span>
            <input
              className={inputCls('w-40')}
              placeholder={item.name}
              defaultValue={searchOverride}
              onKeyDown={(e) => { if (e.key === 'Enter') setSearchOverride((e.target as HTMLInputElement).value.trim()) }}
              onBlur={(e) => setSearchOverride(e.target.value.trim())}
              title="Try a cleaned-up or differently-punctuated title if the stored name looks mangled — that's usually why the match failed"
            />
            <Button size="sm" variant="outline" disabled={!suggestionsQuery.data?.length || aiSuggest.isPending} onClick={() => aiSuggest.mutate()}>
              {aiSuggest.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Sparkles size={12} className="mr-1" />Ask AI</>}
            </Button>
          </div>
          {aiSuggest.isError && (
            <p className="text-destructive">AI suggestion failed — check the AI provider/API key in API Keys settings.</p>
          )}
          {aiSuggest.data && (
            <p className="text-muted-foreground border border-border rounded px-2 py-1">
              <Sparkles size={11} className="inline mr-1" />
              {aiSuggest.data.data.best_match_index != null && suggestionsQuery.data?.[aiSuggest.data.data.best_match_index]
                ? <>AI suggests <strong className="text-foreground">{suggestionsQuery.data[aiSuggest.data.data.best_match_index].name}</strong> ({aiSuggest.data.data.confidence} confidence) — {aiSuggest.data.data.reasoning}</>
                : <>AI found no confident match — {aiSuggest.data.data.reasoning}</>}
            </p>
          )}
          {suggestionsQuery.isLoading && <p className="text-muted-foreground">Searching TMDB…</p>}
          {suggestionsQuery.isError && <p className="text-destructive">TMDB search failed — check the TMDB API key in API Keys settings.</p>}
          {!!suggestionsQuery.data?.length && (
            <div className="space-y-1.5">
              {suggestionsQuery.data.map((s) => (
                <button
                  key={s.tmdb_id}
                  disabled={resolve.isPending || !s.poster_url}
                  title={s.poster_url ? undefined : "TMDB has no poster for this candidate either — try another match or enter one manually below"}
                  className="flex items-start gap-2 w-full border border-border rounded px-2 py-1.5 hover:bg-accent text-left disabled:opacity-50 disabled:hover:bg-transparent"
                  onClick={() => resolve.mutate({
                    poster_url: s.poster_url!,
                    tmdb_id: s.tmdb_id,
                    name: s.name !== item.name ? s.name : undefined,
                    year: s.year ?? undefined,
                  })}
                >
                  {s.poster_url
                    ? <img src={s.poster_url} alt="" className="w-10 h-14 object-cover rounded shrink-0" />
                    : <div className="w-10 h-14 rounded bg-muted shrink-0 flex items-center justify-center"><ImageOff size={14} className="text-muted-foreground" /></div>}
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{s.name} {s.year ? `(${s.year})` : ''}</span>
                      {s.vote_average != null && <span className="text-muted-foreground">★ {s.vote_average.toFixed(1)}</span>}
                    </div>
                    {!!s.cast.length && <p className="text-muted-foreground">Cast: {s.cast.join(', ')}</p>}
                    {s.overview && <p className="text-muted-foreground line-clamp-2">{s.overview}</p>}
                  </div>
                </button>
              ))}
            </div>
          )}
          {suggestionsQuery.data && suggestionsQuery.data.length === 0 && (
            <p className="text-muted-foreground">No TMDB matches found for this name.</p>
          )}

          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">or paste a poster URL manually:</span>
            <input
              className={inputCls('flex-1')}
              placeholder="https://..."
              value={manualPosterUrl}
              onChange={(e) => setManualPosterUrl(e.target.value)}
            />
            <Button
              size="sm"
              disabled={!manualPosterUrl.trim() || resolve.isPending}
              onClick={() => resolve.mutate({ poster_url: manualPosterUrl.trim() })}
            >
              Apply
            </Button>
          </div>
        </div>
      )}
    </li>
  )
}

// Non-modal preview, deliberately -- the point of this one (unlike the
// single-item VodPlayer) is that two candidates can be open side by side at
// once for real visual/audio comparison before merging. Same Direct/
// Transcoded/HLS fallback modes as VodPlayer, reusing the same URL builders
// (buildPreviewUrl/buildTranscodedPreviewSourceUrl/buildHlsPreviewSourceUrl)
// -- resolved client-side from item id + XC credentials, no extra backend
// round-trip needed.
function DuplicateInlinePreview({ kind, itemId, xcCredentials }: {
  kind: 'movie' | 'series'
  itemId: number
  xcCredentials?: XcCredentials
}) {
  const [mode, setMode] = useState<'direct' | 'transcode' | 'hls'>('direct')
  const [error, setError] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)

  const directUrl = buildPreviewUrl(kind, itemId, 'mp4', xcCredentials)
  const transcodedUrl = buildTranscodedPreviewSourceUrl(kind, itemId, xcCredentials)
  const hlsUrl = buildHlsPreviewSourceUrl(kind, itemId, xcCredentials)
  const activeUrl = mode === 'transcode' ? transcodedUrl : mode === 'hls' ? null : directUrl

  useEffect(() => {
    const video = videoRef.current
    if (!video || mode !== 'hls' || !hlsUrl) return
    setError(null)
    if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: false })
      hlsRef.current = hls
      hls.loadSource(hlsUrl)
      hls.attachMedia(video)
      hls.on(Hls.Events.MANIFEST_PARSED, () => { video.play().catch(() => {}) })
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data.fatal) setError('HLS playback failed — the transcode may have failed to start or the source is unreachable.')
      })
      return () => { hls.destroy(); hlsRef.current = null }
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl
    } else {
      setError('This browser has no HLS support.')
    }
  }, [mode, hlsUrl])

  return (
    <div className="mt-1 space-y-1">
      <div className="relative aspect-video bg-black rounded overflow-hidden">
        {mode === 'hls' ? (
          <video ref={videoRef} controls playsInline className="w-full h-full" />
        ) : (
          <video
            ref={videoRef} src={activeUrl ?? undefined} controls autoPlay playsInline className="w-full h-full"
            onError={() => setError(mode === 'direct' ? 'Playback failed — try Transcoded or HLS below.' : 'Playback failed.')}
          />
        )}
      </div>
      {!directUrl && <p className="text-[10px] text-destructive">Could not build a preview URL (no XC client configured yet).</p>}
      {error && <p className="text-[10px] text-destructive">{error}</p>}
      <div className="flex items-center gap-1">
        {(['direct', 'transcode', 'hls'] as const).map((m) => (
          <button
            key={m}
            className={`px-1.5 py-0.5 rounded text-[10px] border ${mode === m ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:text-foreground'}`}
            onClick={() => { setMode(m); setError(null) }}
          >
            {m === 'direct' ? 'Direct' : m === 'transcode' ? 'Transcoded' : 'HLS'}
          </button>
        ))}
      </div>
    </div>
  )
}

function DuplicateGroupRow({ group, contentType, xcCredentials, onMerge, isPending, onIgnore, isIgnorePending, tmdbDetails }: {
  group: DuplicateGroup
  contentType: 'movie' | 'series'
  xcCredentials?: XcCredentials
  onMerge: (keepId: number, mergeIds: number[]) => void
  isPending: boolean
  onIgnore: (itemIds: number[]) => void
  isIgnorePending: boolean
  tmdbDetails?: Record<string, { year: number | null; title: string | null }>
}) {
  // Backend already sorts most-sourced/most-placed first -- the obvious
  // default "keep" pick, but still a human decision the reviewer can override.
  const [keepId, setKeepId] = useState(group.items[0].id)
  const [previewIds, setPreviewIds] = useState<Set<number>>(new Set())
  const togglePreview = (id: number) => setPreviewIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  // Identical (non-null) poster art across 2+ candidates is strong
  // confirming evidence they're the same real release, not just a title
  // collision -- a known limitation: same image hosted on two different
  // source servers won't match by URL even if the pixels are identical.
  const posterCounts = new Map<string, number>()
  for (const item of group.items) {
    if (item.poster_url) posterCounts.set(item.poster_url, (posterCounts.get(item.poster_url) ?? 0) + 1)
  }
  const artworkMatches = group.items.some((item) => item.poster_url && (posterCounts.get(item.poster_url) ?? 0) > 1)

  // A shared tmdb_id across 2+ candidates means TMDB itself confirms
  // they're the same real title. A CONFLICTING (different) tmdb_id never
  // reaches this component at all -- that's positive proof they're
  // different content, so the backend splits it into separate groups
  // before this ever renders (see vod_db._split_by_tmdb_conflict).
  const tmdbIdCounts = new Map<string, number>()
  for (const item of group.items) {
    if (item.tmdb_id) tmdbIdCounts.set(item.tmdb_id, (tmdbIdCounts.get(item.tmdb_id) ?? 0) + 1)
  }
  const sameTmdbMatch = [...tmdbIdCounts.values()].some((c) => c > 1)

  return (
    <div className="border border-border rounded px-2 py-1.5 space-y-1.5">
      <div className="flex items-center gap-1.5 flex-wrap">
        {sameTmdbMatch && (
          <span className="text-[10px] font-normal px-1.5 py-0.5 rounded-full bg-blue-500/15 text-blue-400 border border-blue-500/30">
            same TMDB match
          </span>
        )}
        {artworkMatches && (
          <span className="text-[10px] font-normal px-1.5 py-0.5 rounded-full bg-green-500/15 text-green-400 border border-green-500/30">
            matching artwork
          </span>
        )}
      </div>
      {group.items.map((item) => {
        // trueYear is TMDB's own canonical release year for this item's
        // tmdb_id -- undefined while loading, null if the lookup failed, a
        // real year once resolved. Only a real year that equals this
        // item's OWN year field is the "true" match -- sharing the
        // tmdb_id alone just means the TITLE was matched correctly, not
        // that this row's year is.
        const trueYear = item.tmdb_id ? tmdbDetails?.[item.tmdb_id]?.year : undefined
        const isTrueYearMatch = trueYear != null && item.year === trueYear
        const isYearMismatch = trueYear != null && item.year !== trueYear
        // isTrueYearMatch on its own only says THIS candidate's own tmdb_id
        // is self-consistent -- it says nothing about whether any other
        // candidate in the group actually corroborates it. A lone
        // self-consistent id next to a sibling with no id at all (or the
        // sibling just hasn't loaded a poster/other evidence) used to render
        // an unqualified green "true match" that read as a confirmed pairing
        // when it's actually the weakest signal available -- split into
        // three real tiers instead: shared-and-confirmed (green),
        // self-consistent-but-uncorroborated (amber), and no-id-at-all-while-
        // a-sibling-has-one (red, the strongest negative signal short of an
        // actual conflicting id, which never reaches this component at all --
        // see _split_by_tmdb_conflict).
        const isCorroborated = item.tmdb_id != null && (tmdbIdCounts.get(item.tmdb_id) ?? 0) > 1
        const otherHasTmdbId = group.items.some((other) => other.id !== item.id && other.tmdb_id != null)
        return (
          <div key={item.id} className="flex gap-2">
            {item.poster_url && (
              <img src={item.poster_url} alt="" className="w-12 h-[72px] object-cover rounded shrink-0" loading="lazy" />
            )}
            <div className="flex-1 min-w-0">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="radio" checked={keepId === item.id} onChange={() => setKeepId(item.id)} />
                <span className={keepId === item.id ? 'font-medium' : ''}>{item.name} ({item.year})</span>
                <span className="text-muted-foreground">
                  {item.source_count} source{item.source_count === 1 ? '' : 's'} · {item.category_count} categor{item.category_count === 1 ? 'y' : 'ies'}
                  {!!item.provider_names.length && <> ({item.provider_names.join(', ')})</>}
                </span>
              </label>
              {!!item.tmdb_id ? (
                <div className="flex items-center gap-1 flex-wrap mt-0.5">
                  <span className="inline-block text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border" title={`TMDB id ${item.tmdb_id}`}>
                    TMDB #{item.tmdb_id}
                  </span>
                  {isTrueYearMatch && isCorroborated && (
                    <span className="inline-block text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-green-500/15 text-green-400 border border-green-500/30">
                      true match — TMDB confirms {trueYear}
                    </span>
                  )}
                  {isTrueYearMatch && !isCorroborated && (
                    <span
                      className="inline-block text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/30"
                      title="This candidate's own TMDB id checks out, but no other candidate in this group shares it -- not cross-confirmed, verify manually"
                    >
                      unconfirmed — TMDB confirms only this candidate ({trueYear})
                    </span>
                  )}
                  {isYearMismatch && (
                    <span className="inline-block text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-red-500/15 text-red-400 border border-red-500/30">
                      year mismatch — TMDB says {trueYear}
                    </span>
                  )}
                </div>
              ) : otherHasTmdbId && (
                <div className="flex items-center gap-1 flex-wrap mt-0.5">
                  <span
                    className="inline-block text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-red-500/15 text-red-400 border border-red-500/30"
                    title="Another candidate in this group has a confirmed TMDB id; this one has none at all -- a real duplicate almost always both match, so this is the biggest reason to doubt the pairing"
                  >
                    no TMDB match — unconfirmed
                  </span>
                </div>
              )}
              <button className="text-[11px] text-primary hover:underline mt-0.5" onClick={() => togglePreview(item.id)}>
                {previewIds.has(item.id) ? 'Hide preview' : 'Preview'}
              </button>
              {previewIds.has(item.id) && <DuplicateInlinePreview kind={contentType} itemId={item.id} xcCredentials={xcCredentials} />}
            </div>
          </div>
        )
      })}
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          disabled={isPending}
          onClick={() => onMerge(keepId, group.items.filter((i) => i.id !== keepId).map((i) => i.id))}
        >
          {isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
          Merge into selected
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={isIgnorePending}
          title="Not actually duplicates -- dismiss this group so it stops resurfacing"
          onClick={() => onIgnore(group.items.map((i) => i.id))}
        >
          {isIgnorePending ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
          Ignore
        </Button>
      </div>
    </div>
  )
}

function MovieRow({ movie, movieCategories, providers, qc, xcCredentials, selected, onToggleSelect, mode = 'list', onToggleArchived }: {
  movie: Movie
  movieCategories: Category[]
  providers: Provider[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  selected: boolean
  onToggleSelect: (shiftKey: boolean) => void
  mode?: 'list' | 'grid'
  onToggleArchived: () => void
}) {
  const [open, setOpen] = useState(false)
  const [sourceForm, setSourceForm] = useState({ provider_id: '', provider_stream_id: '', container_extension: 'mp4' })
  const [categoryPick, setCategoryPick] = useState('')
  const [renameForm, setRenameForm] = useState<{ name: string; year: string } | null>(null)

  const rename = useMutation({
    mutationFn: () => api.post(`/vod/movies/${movie.id}/rename/`, {
      name: renameForm!.name, year: renameForm!.year ? Number(renameForm!.year) : undefined,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      setRenameForm(null)
    },
  })

  const addSource = useMutation({
    mutationFn: () => api.post(`/vod/movies/${movie.id}/sources/`, {
      provider_id: Number(sourceForm.provider_id), provider_stream_id: sourceForm.provider_stream_id,
      container_extension: sourceForm.container_extension,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      setSourceForm({ provider_id: '', provider_stream_id: '', container_extension: 'mp4' })
    },
  })
  const addPlacement = useMutation({
    mutationFn: () => api.post(`/vod/movies/${movie.id}/categories/`, { category_id: Number(categoryPick) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      setCategoryPick('')
    },
  })
  const enrich = useMutation({
    mutationFn: () => api.post(`/vod/movies/${movie.id}/enrich/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-movies'] }),
  })
  const deleteMovie = useMutation({
    mutationFn: () => api.delete(`/vod/movies/${movie.id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-movies'] }),
    onError:    (e: any) => alert(e?.response?.data?.detail ?? 'Delete failed.'),
  })
  const toggleAdult = useMutation({
    mutationFn: (is_adult: boolean) => api.post(`/vod/movies/${movie.id}/adult/`, null, { params: { is_adult } }),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-movies'] }),
  })
  const deleteSource = useMutation({
    mutationFn: (sourceId: number) => api.delete(`/vod/movies/${movie.id}/sources/${sourceId}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-movies'] }),
  })
  const removePlacement = useMutation({
    mutationFn: (categoryId: number) => api.delete(`/vod/movies/${movie.id}/categories/${categoryId}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-movies'] }),
  })

  const detailContent = (
    <>
      {movie.poster_url && mode === 'list' && (
        <img src={movie.poster_url} alt="" className="w-24 rounded" loading="lazy" />
      )}
      <div>
        {renameForm ? (
          <div className="flex items-center gap-1.5 flex-wrap">
            <input
              className={inputCls('flex-1 min-w-32')}
              placeholder="Name"
              value={renameForm.name}
              onChange={(e) => setRenameForm({ ...renameForm, name: e.target.value })}
            />
            <input
              className={inputCls('w-20')}
              type="number"
              placeholder="Year"
              value={renameForm.year}
              onChange={(e) => setRenameForm({ ...renameForm, year: e.target.value })}
            />
            <Button size="sm" disabled={!renameForm.name.trim() || rename.isPending} onClick={() => rename.mutate()}>
              {rename.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setRenameForm(null)}>Cancel</Button>
          </div>
        ) : (
          <button
            className="text-muted-foreground hover:text-foreground underline decoration-dotted"
            onClick={() => setRenameForm({ name: movie.name, year: movie.year ? String(movie.year) : '' })}
          >
            Rename / fix year
          </button>
        )}
        {rename.isError && <p className="text-destructive">{(rename.error as any)?.response?.data?.detail ?? 'Rename failed'}</p>}
      </div>
      {movie.description && <p className="text-muted-foreground">{movie.description}</p>}

      <div>
        <p className="font-medium mb-1">Sources</p>
        {movie.sources.map((s) => (
          <div key={s.id} className="flex items-center justify-between text-muted-foreground">
            <span>{s.provider_name} → {s.provider_stream_id} ({s.container_extension}){s.provider_category_name ? ` · ${s.provider_category_name}` : ''}</span>
            <span className="flex items-center gap-1.5">
              <PlayButton
                url={buildPreviewSourceUrl('movie', s.id, s.container_extension, xcCredentials)}
                transcodedUrl={buildTranscodedPreviewSourceUrl('movie', s.id, xcCredentials)}
                hlsUrl={buildHlsPreviewSourceUrl('movie', s.id, xcCredentials)}
                title={`${movie.name}${movie.year ? ` (${movie.year})` : ''} — ${s.provider_name}`}
              />
              <CopyUrlButton url={buildPreviewSourceUrl('movie', s.id, s.container_extension, xcCredentials)} />
              <button title="Remove source" className="hover:text-destructive" onClick={() => deleteSource.mutate(s.id)}>
                <X size={12} />
              </button>
            </span>
          </div>
        ))}
        <div className="flex items-center gap-1.5 pt-1">
          <select className={inputCls()} value={sourceForm.provider_id} onChange={(e) => setSourceForm({ ...sourceForm, provider_id: e.target.value })}>
            <option value="">Provider…</option>
            {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <input className={inputCls()} placeholder="Provider stream ID" value={sourceForm.provider_stream_id} onChange={(e) => setSourceForm({ ...sourceForm, provider_stream_id: e.target.value })} />
          <input className={inputCls('w-16')} placeholder="ext" value={sourceForm.container_extension} onChange={(e) => setSourceForm({ ...sourceForm, container_extension: e.target.value })} />
          <Button size="sm" disabled={!sourceForm.provider_id || !sourceForm.provider_stream_id || addSource.isPending} onClick={() => addSource.mutate()}>
            <Plus size={12} className="mr-1" /> Add source
          </Button>
        </div>
      </div>

      <div>
        <p className="font-medium mb-1">Categories</p>
        {movie.placements.map((p) => (
          <div key={p.id} className="flex items-center justify-between text-muted-foreground">
            <span>{p.category_name}</span>
            <button title="Remove from category" className="hover:text-destructive" onClick={() => removePlacement.mutate(p.category_id)}>
              <X size={12} />
            </button>
          </div>
        ))}
        <div className="flex items-center gap-1.5 pt-1">
          <select className={inputCls()} value={categoryPick} onChange={(e) => setCategoryPick(e.target.value)}>
            <option value="">Category…</option>
            {movieCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <Button size="sm" disabled={!categoryPick || addPlacement.isPending} onClick={() => addPlacement.mutate()}>
            <Plus size={12} className="mr-1" /> Place in category
          </Button>
        </div>
      </div>

      <Button size="sm" variant="outline" disabled={enrich.isPending} onClick={() => enrich.mutate()}>
        {enrich.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : <Sparkles size={12} className="mr-1" />}
        Fetch full detail
      </Button>
    </>
  )

  if (mode === 'grid') {
    return (
      <div className="rounded-lg border border-border bg-card overflow-hidden shadow-sm hover:shadow-md hover:border-primary/40 transition-all relative">
        <div className="absolute top-1.5 left-1.5 z-10" onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={() => {}} onClick={(e) => onToggleSelect(e.shiftKey)} title="Select for bulk placement (shift-click to select a range)" className="w-3.5 h-3.5" />
        </div>
        {!!movie.is_adult && (
          <span className="absolute top-1.5 right-1.5 z-10 text-white text-[10px] font-bold bg-destructive/90 rounded px-1.5 py-0.5">18+</span>
        )}
        <button
          title={movie.review_excluded ? 'Restore from archive' : 'Archive (removes from every category and hides from the pool)'}
          className="absolute top-1.5 left-7 z-10 bg-background/85 rounded p-0.5 text-muted-foreground hover:text-foreground"
          onClick={(e) => { e.stopPropagation(); onToggleArchived() }}
        >
          {movie.review_excluded ? <ArchiveRestore size={12} /> : <Archive size={12} />}
        </button>
        <button className="block w-full text-left" onClick={() => setOpen(true)}>
          {movie.poster_url ? (
            <img src={movie.poster_url} alt="" className="w-full aspect-[2/3] object-cover" loading="lazy" />
          ) : (
            <div className="w-full aspect-[2/3] bg-secondary flex items-center justify-center">
              <Film size={24} className="text-muted-foreground" />
            </div>
          )}
          <div className="p-2 text-xs">
            <p className="font-semibold truncate leading-snug">{movie.name}</p>
            <p className="text-muted-foreground text-[11px] mt-0.5">{movie.year ?? ''}</p>
          </div>
        </button>
        {movie.sources.length > 0 && (
          <div className="absolute bottom-9 right-1.5 z-10 bg-background/85 rounded" onClick={(e) => e.stopPropagation()}>
            <PlayButton
              url={buildPreviewUrl('movie', movie.id, movie.sources[0]?.container_extension || 'mp4', xcCredentials)}
              transcodedUrl={movie.sources[0] ? buildTranscodedPreviewSourceUrl('movie', movie.sources[0].id, xcCredentials) : null}
              hlsUrl={movie.sources[0] ? buildHlsPreviewSourceUrl('movie', movie.sources[0].id, xcCredentials) : null}
              title={`${movie.name}${movie.year ? ` (${movie.year})` : ''}`}
            />
          </div>
        )}
        {open && (
          <Modal onClose={() => setOpen(false)} maxWidth="max-w-lg">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
              <span className="text-sm font-medium truncate pr-6">{movie.name}{movie.year ? ` (${movie.year})` : ''}</span>
            </div>
            <div className="p-4 text-xs space-y-2 overflow-y-auto">
              {detailContent}
            </div>
          </Modal>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-card p-2.5 text-xs flex gap-3 shadow-sm hover:border-primary/30 transition-colors">
      <input type="checkbox" className="mt-1 shrink-0" checked={selected} onChange={() => {}} onClick={(e) => onToggleSelect(e.shiftKey)} title="Select for bulk placement (shift-click to select a range)" />
      {movie.poster_url ? (
        <img src={movie.poster_url} alt="" className="w-10 h-14 object-cover rounded-md shrink-0" loading="lazy" />
      ) : (
        <div className="w-10 h-14 rounded-md shrink-0 bg-secondary flex items-center justify-center">
          <Film size={14} className="text-muted-foreground" />
        </div>
      )}
      <div className="flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <span className="font-semibold text-[13px] flex items-center gap-1.5 cursor-pointer" onClick={() => setOpen(!open)}>
          {open ? <ChevronUp size={12} className="text-muted-foreground" /> : <ChevronDown size={12} className="text-muted-foreground" />}
          {movie.name}{movie.year ? <span className="text-muted-foreground font-normal"> ({movie.year})</span> : ''}
          {!!movie.is_adult && <Chip tone="rec">18+</Chip>}
        </span>
        <span className="flex items-center gap-2 text-muted-foreground">
          {movie.sources.length} source{movie.sources.length === 1 ? '' : 's'} · {movie.placements.length} categor{movie.placements.length === 1 ? 'y' : 'ies'}
          {movie.sources.length > 0 && (
            <>
              <PlayButton
                url={buildPreviewUrl('movie', movie.id, movie.sources[0]?.container_extension || 'mp4', xcCredentials)}
                transcodedUrl={movie.sources[0] ? buildTranscodedPreviewSourceUrl('movie', movie.sources[0].id, xcCredentials) : null}
                hlsUrl={movie.sources[0] ? buildHlsPreviewSourceUrl('movie', movie.sources[0].id, xcCredentials) : null}
                title={`${movie.name}${movie.year ? ` (${movie.year})` : ''}`}
              />
              <CopyUrlButton url={buildPreviewUrl('movie', movie.id, movie.sources[0]?.container_extension || 'mp4', xcCredentials)} />
            </>
          )}
          <button
            title={movie.is_adult ? 'Unmark as adult content' : 'Mark as adult content'}
            className={movie.is_adult ? 'text-destructive' : 'text-muted-foreground hover:text-destructive'}
            onClick={() => toggleAdult.mutate(!movie.is_adult)}
          >
            18+
          </button>
          <button
            title={movie.review_excluded ? 'Restore from archive' : 'Archive (removes from every category and hides from the pool)'}
            className="text-muted-foreground hover:text-foreground"
            onClick={onToggleArchived}
          >
            {movie.review_excluded ? <ArchiveRestore size={12} /> : <Archive size={12} />}
          </button>
          <button
            title={movie.sources.length > 0
              ? `Has ${movie.sources.length} active source(s) — the next catalog sync would just re-import it fresh. Archive instead; only sourceless orphans can be deleted.`
              : 'Delete movie (sourceless orphan)'}
            className={movie.sources.length > 0 ? 'text-muted-foreground/40 cursor-not-allowed' : 'text-muted-foreground hover:text-destructive'}
            disabled={movie.sources.length > 0}
            onClick={() => { if (confirm(`Delete "${movie.name}"? This removes all its category placements. It has no sources, so nothing will bring it back on the next sync.`)) deleteMovie.mutate() }}
          >
            <Trash2 size={12} />
          </button>
        </span>
      </div>
      {(movie.placements.length > 0 || movie.genre) && (
        <div className="flex flex-wrap items-center gap-1 mt-1.5">
          {movie.placements.map((p) => <Chip key={p.id}>{p.category_name}</Chip>)}
          {movie.genre && <Chip>{movie.genre}</Chip>}
        </div>
      )}

      {open && (
        <div className="mt-2 pt-2 border-t border-border/50 space-y-2">
          {detailContent}
        </div>
      )}
      </div>
    </div>
  )
}

function SeriesRow({ series, seriesCategories, qc, xcCredentials, selected, onToggleSelect, mode = 'list', onToggleArchived }: {
  series: Series
  seriesCategories: Category[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  selected: boolean
  onToggleSelect: (shiftKey: boolean) => void
  mode?: 'list' | 'grid'
  onToggleArchived: () => void
}) {
  const [open, setOpen] = useState(false)
  const [categoryPick, setCategoryPick] = useState('')
  const [renameForm, setRenameForm] = useState<{ name: string; year: string } | null>(null)

  const rename = useMutation({
    mutationFn: () => api.post(`/vod/series/${series.id}/rename/`, {
      name: renameForm!.name, year: renameForm!.year ? Number(renameForm!.year) : undefined,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      setRenameForm(null)
    },
  })

  const addPlacement = useMutation({
    mutationFn: () => api.post(`/vod/series/${series.id}/categories/`, { category_id: Number(categoryPick) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      setCategoryPick('')
    },
  })
  const enrich = useMutation({
    mutationFn: () => api.post(`/vod/series/${series.id}/enrich/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-series'] }),
  })
  const deleteSeries = useMutation({
    mutationFn: () => api.delete(`/vod/series/${series.id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-series'] }),
    onError:    (e: any) => alert(e?.response?.data?.detail ?? 'Delete failed.'),
  })
  const toggleAdult = useMutation({
    mutationFn: (is_adult: boolean) => api.post(`/vod/series/${series.id}/adult/`, null, { params: { is_adult } }),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-series'] }),
  })
  const removePlacement = useMutation({
    mutationFn: (categoryId: number) => api.delete(`/vod/series/${series.id}/categories/${categoryId}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-series'] }),
  })

  const detailContent = (
    <>
      {series.poster_url && mode === 'list' && (
        <img src={series.poster_url} alt="" className="w-24 rounded" loading="lazy" />
      )}
      <div>
        {renameForm ? (
          <div className="flex items-center gap-1.5 flex-wrap">
            <input
              className={inputCls('flex-1 min-w-32')}
              placeholder="Name"
              value={renameForm.name}
              onChange={(e) => setRenameForm({ ...renameForm, name: e.target.value })}
            />
            <input
              className={inputCls('w-20')}
              type="number"
              placeholder="Year"
              value={renameForm.year}
              onChange={(e) => setRenameForm({ ...renameForm, year: e.target.value })}
            />
            <Button size="sm" disabled={!renameForm.name.trim() || rename.isPending} onClick={() => rename.mutate()}>
              {rename.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setRenameForm(null)}>Cancel</Button>
          </div>
        ) : (
          <button
            className="text-muted-foreground hover:text-foreground underline decoration-dotted"
            onClick={() => setRenameForm({ name: series.name, year: series.year ? String(series.year) : '' })}
          >
            Rename / fix year
          </button>
        )}
        {rename.isError && <p className="text-destructive">{(rename.error as any)?.response?.data?.detail ?? 'Rename failed'}</p>}
      </div>
      {series.description && <p className="text-muted-foreground">{series.description}</p>}

      <div>
        <p className="font-medium mb-1">Episodes</p>
        {series.episodes.length === 0 && (
          <p className="text-muted-foreground">No episodes yet — click "Fetch episodes &amp; detail" to pull them from the source provider.</p>
        )}
        {series.episodes.map((e) => (
          <div key={e.id} className="flex items-center justify-between text-muted-foreground">
            <span>
              S{e.season_number}E{e.episode_number} — {e.name}
              {e.sources.length > 0 && <span className="text-[10px]"> ({e.sources.map((s) => s.provider_name).join(', ')})</span>}
            </span>
            <span className="flex items-center gap-1.5">
              <PlayButton
                url={buildStreamUrl('series', e.export_episode_id, 'mp4', xcCredentials)}
                transcodedUrl={e.sources[0] ? buildTranscodedPreviewSourceUrl('series', e.sources[0].id, xcCredentials) : null}
                hlsUrl={e.sources[0] ? buildHlsPreviewSourceUrl('series', e.sources[0].id, xcCredentials) : null}
                title={`${series.name} S${e.season_number}E${e.episode_number} — ${e.name}`}
              />
              <CopyUrlButton url={buildStreamUrl('series', e.export_episode_id, 'mp4', xcCredentials)} />
            </span>
          </div>
        ))}
      </div>

      <div>
        <p className="font-medium mb-1">Categories</p>
        {series.placements.map((p) => (
          <div key={p.id} className="flex items-center justify-between text-muted-foreground">
            <span>{p.category_name}</span>
            <button title="Remove from category" className="hover:text-destructive" onClick={() => removePlacement.mutate(p.category_id)}>
              <X size={12} />
            </button>
          </div>
        ))}
        <div className="flex items-center gap-1.5">
          <select className={inputCls()} value={categoryPick} onChange={(e) => setCategoryPick(e.target.value)}>
            <option value="">Category…</option>
            {seriesCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <Button size="sm" disabled={!categoryPick || addPlacement.isPending} onClick={() => addPlacement.mutate()}>
            <Plus size={12} className="mr-1" /> Place in category
          </Button>
        </div>
      </div>

      <Button size="sm" variant="outline" disabled={enrich.isPending} onClick={() => enrich.mutate()}>
        {enrich.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : <Sparkles size={12} className="mr-1" />}
        Fetch episodes &amp; detail
      </Button>
    </>
  )

  if (mode === 'grid') {
    return (
      <div className="rounded-lg border border-border bg-card overflow-hidden shadow-sm hover:shadow-md hover:border-primary/40 transition-all relative">
        <div className="absolute top-1.5 left-1.5 z-10" onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={() => {}} onClick={(e) => onToggleSelect(e.shiftKey)} title="Select for bulk placement (shift-click to select a range)" className="w-3.5 h-3.5" />
        </div>
        {!!series.is_adult && (
          <span className="absolute top-1.5 right-1.5 z-10 text-white text-[10px] font-bold bg-destructive/90 rounded px-1.5 py-0.5">18+</span>
        )}
        <button
          title={series.review_excluded ? 'Restore from archive' : 'Archive (removes from every category and hides from the pool)'}
          className="absolute top-1.5 left-7 z-10 bg-background/85 rounded p-0.5 text-muted-foreground hover:text-foreground"
          onClick={(e) => { e.stopPropagation(); onToggleArchived() }}
        >
          {series.review_excluded ? <ArchiveRestore size={12} /> : <Archive size={12} />}
        </button>
        <button className="block w-full text-left" onClick={() => setOpen(true)}>
          {series.poster_url ? (
            <img src={series.poster_url} alt="" className="w-full aspect-[2/3] object-cover" loading="lazy" />
          ) : (
            <div className="w-full aspect-[2/3] bg-secondary flex items-center justify-center">
              <Tv size={24} className="text-muted-foreground" />
            </div>
          )}
          <div className="p-2 text-xs">
            <p className="font-semibold truncate leading-snug">{series.name}</p>
            <p className="text-muted-foreground text-[11px] mt-0.5">{series.year ?? ''}</p>
          </div>
        </button>
        {open && (
          <Modal onClose={() => setOpen(false)} maxWidth="max-w-lg">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
              <span className="text-sm font-medium truncate pr-6">{series.name}{series.year ? ` (${series.year})` : ''}</span>
            </div>
            <div className="p-4 text-xs space-y-2 overflow-y-auto">
              {detailContent}
            </div>
          </Modal>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-card p-2.5 text-xs flex gap-3 shadow-sm hover:border-primary/30 transition-colors">
      <input type="checkbox" className="mt-1 shrink-0" checked={selected} onChange={() => {}} onClick={(e) => onToggleSelect(e.shiftKey)} title="Select for bulk placement (shift-click to select a range)" />
      {series.poster_url ? (
        <img src={series.poster_url} alt="" className="w-10 h-14 object-cover rounded-md shrink-0" loading="lazy" />
      ) : (
        <div className="w-10 h-14 rounded-md shrink-0 bg-secondary flex items-center justify-center">
          <Tv size={14} className="text-muted-foreground" />
        </div>
      )}
      <div className="flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <span className="font-semibold text-[13px] flex items-center gap-1.5 cursor-pointer" onClick={() => setOpen(!open)}>
          {open ? <ChevronUp size={12} className="text-muted-foreground" /> : <ChevronDown size={12} className="text-muted-foreground" />}
          {series.name}{series.year ? <span className="text-muted-foreground font-normal"> ({series.year})</span> : ''}
          {!!series.is_adult && <Chip tone="rec">18+</Chip>}
        </span>
        <span className="flex items-center gap-2 text-muted-foreground">
          {series.episodes.length} episode{series.episodes.length === 1 ? '' : 's'}
          <button
            title={series.is_adult ? 'Unmark as adult content' : 'Mark as adult content'}
            className={series.is_adult ? 'text-destructive' : 'text-muted-foreground hover:text-destructive'}
            onClick={() => toggleAdult.mutate(!series.is_adult)}
          >
            18+
          </button>
          <button
            title={series.review_excluded ? 'Restore from archive' : 'Archive (removes from every category and hides from the pool)'}
            className="text-muted-foreground hover:text-foreground"
            onClick={onToggleArchived}
          >
            {series.review_excluded ? <ArchiveRestore size={12} /> : <Archive size={12} />}
          </button>
          {(() => {
            const seriesSourceCount = series.episodes.reduce((n, e) => n + e.sources.length, 0)
            return (
              <button
                title={seriesSourceCount > 0
                  ? `Has ${seriesSourceCount} active episode source(s) — the next catalog sync would just re-import it fresh. Archive instead; only sourceless orphans can be deleted.`
                  : 'Delete series (sourceless orphan)'}
                className={seriesSourceCount > 0 ? 'text-muted-foreground/40 cursor-not-allowed' : 'text-muted-foreground hover:text-destructive'}
                disabled={seriesSourceCount > 0}
                onClick={() => { if (confirm(`Delete "${series.name}"? This removes all its episodes and category placements. It has no sources, so nothing will bring it back on the next sync.`)) deleteSeries.mutate() }}
              >
                <Trash2 size={12} />
              </button>
            )
          })()}
        </span>
      </div>
      {(series.genre || series.import_provider_name) && (
        <div className="flex flex-wrap items-center gap-1 mt-1.5">
          {series.genre && <Chip>{series.genre}</Chip>}
          {series.import_provider_name && <Chip>matched: {series.import_provider_name}</Chip>}
        </div>
      )}

      {open && (
        <div className="mt-2 pt-2 border-t border-border/50 space-y-2">
          {detailContent}
        </div>
      )}
      </div>
    </div>
  )
}

// Category management scoped to one content type at a time -- opened from
// the Movies or TV Shows tab's own toolbar, so it always shows just the
// categories relevant to whatever you're already browsing (manual, smart,
// and TMDB-synced alike -- unlike the old unified card, TMDB-synced
// categories are included here too, so "View" works for them without a tab
// switch). Fully self-contained: declares its own copies of the category
// mutations (same endpoints as the ones vod_manager's "TMDB Lists" section
// still uses directly) rather than threading ~10 mutation objects down as
// props -- consistent with how every other row/item component in this file
// (MovieRow, SeriesRow, NeedsReviewRow) already owns its own mutations.
function CategoriesModal({ contentType, categories, qc, onView, onClose }: {
  contentType: 'movie' | 'series'
  categories: Category[]
  qc: ReturnType<typeof useQueryClient>
  onView: (categoryId: number) => void
  onClose: () => void
}) {
  const [categoryForm, setCategoryForm] = useState({
    name: '', is_smart: false,
    rule_field: 'genre' as typeof RULE_FIELDS[number], rule_op: 'contains' as typeof RULE_OPS[number], rule_value: '',
  })
  const addCategory = useMutation({
    mutationFn: () => api.post('/vod/categories/', {
      name: categoryForm.name,
      content_type: contentType,
      is_smart: categoryForm.is_smart,
      rule_json: categoryForm.is_smart
        ? JSON.stringify({ match: 'all', conditions: [{ field: categoryForm.rule_field, op: categoryForm.rule_op, value: categoryForm.rule_value }] })
        : null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
      setCategoryForm({ name: '', is_smart: false, rule_field: 'genre', rule_op: 'contains', rule_value: '' })
    },
  })
  const deleteCategory = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/categories/${id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const setCategorySortOrder = useMutation({
    mutationFn: ({ id, sort_order }: { id: number; sort_order: number }) =>
      api.post(`/vod/categories/${id}/sort-order/`, null, { params: { sort_order } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const renameCategory = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api.post(`/vod/categories/${id}/name/`, null, { params: { name } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const [categoryActiveError, setCategoryActiveError] = useState<string | null>(null)
  const setCategoryActive = useMutation({
    mutationFn: ({ id, is_active }: { id: number; is_active: boolean }) =>
      api.post(`/vod/categories/${id}/active/`, null, { params: { is_active } }),
    onSuccess: () => {
      setCategoryActiveError(null)
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
    onError: (e: any) => setCategoryActiveError(e?.response?.data?.detail ?? e.message),
  })
  const [categoryScheduleError, setCategoryScheduleError] = useState<string | null>(null)
  const setCategorySchedule = useMutation({
    mutationFn: ({ id, start_mmdd, end_mmdd }: { id: number; start_mmdd: string | null; end_mmdd: string | null }) =>
      api.post(`/vod/categories/${id}/schedule/`, null, { params: { start_mmdd: start_mmdd ?? undefined, end_mmdd: end_mmdd ?? undefined } }),
    onSuccess: () => {
      setCategoryScheduleError(null)
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
    onError: (e: any) => setCategoryScheduleError(e?.response?.data?.detail ?? e.message),
  })
  function promptSchedule(c: Category) {
    const start = window.prompt(
      `Annual auto-enable date for "${c.name}" (MM-DD, e.g. 10-01 for Oct 1). Leave blank to clear the schedule entirely.`,
      c.schedule_start_mmdd ?? '',
    )
    if (start === null) return
    if (!start.trim()) {
      setCategorySchedule.mutate({ id: c.id, start_mmdd: null, end_mmdd: null })
      return
    }
    const end = window.prompt(
      `Annual auto-disable date for "${c.name}" (MM-DD, e.g. 11-01 for Nov 1):`,
      c.schedule_end_mmdd ?? '',
    )
    if (end === null) return
    setCategorySchedule.mutate({ id: c.id, start_mmdd: start.trim(), end_mmdd: end.trim() })
  }

  // ── Multi-select (search + Select/Deselect visible + shift-click) ──
  // Same Dispatcharr-style pattern as the provider Exclude Categories
  // modal -- a generic selection, with separate bulk-action buttons below
  // consuming whatever's currently selected (not tied to one single action).
  const [categorySearch, setCategorySearch] = useState('')
  const [categoryShowFilter, setCategoryShowFilter] = useState<'all' | 'selected' | 'unselected'>('all')
  const [selectedCategoryIds, setSelectedCategoryIds] = useState<Set<number>>(new Set())
  const [categoryLastClickedIndex, setCategoryLastClickedIndex] = useState<number | null>(null)
  const visibleCategories = categories.filter((c) => {
    if (categorySearch && !c.name.toLowerCase().includes(categorySearch.toLowerCase())) return false
    if (categoryShowFilter === 'selected' && !selectedCategoryIds.has(c.id)) return false
    if (categoryShowFilter === 'unselected' && selectedCategoryIds.has(c.id)) return false
    return true
  })
  function toggleCategorySelected(id: number, index: number, shiftKey: boolean) {
    const willBeChecked = !selectedCategoryIds.has(id)
    const next = new Set(selectedCategoryIds)
    if (shiftKey && categoryLastClickedIndex != null) {
      const [start, end] = [categoryLastClickedIndex, index].sort((a, b) => a - b)
      for (let j = start; j <= end; j++) {
        const cid = visibleCategories[j]?.id
        if (cid == null) continue
        if (willBeChecked) next.add(cid); else next.delete(cid)
      }
    } else {
      if (willBeChecked) next.add(id); else next.delete(id)
    }
    setSelectedCategoryIds(next)
    setCategoryLastClickedIndex(index)
  }
  const [categoryBulkError, setCategoryBulkError] = useState<string | null>(null)
  const [categoryBulkResult, setCategoryBulkResult] = useState<string | null>(null)
  const bulkSetCategoriesActive = useMutation({
    mutationFn: (is_active: boolean) => api.post('/vod/categories/bulk-active/', { category_ids: [...selectedCategoryIds], is_active }),
    onSuccess: (r, is_active) => {
      setCategoryBulkError(null)
      setCategoryBulkResult(`${is_active ? 'Enabled' : 'Disabled'} ${r.data.changed} categor${r.data.changed === 1 ? 'y' : 'ies'}.`)
      setSelectedCategoryIds(new Set())
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
    onError: (e: any) => { setCategoryBulkResult(null); setCategoryBulkError(e?.response?.data?.detail ?? e.message) },
  })
  const bulkDeleteCategories = useMutation({
    mutationFn: () => api.post('/vod/categories/bulk-delete/', { category_ids: [...selectedCategoryIds] }),
    onSuccess: (r) => {
      setCategoryBulkError(null)
      setCategoryBulkResult(`Deleted ${r.data.deleted} categor${r.data.deleted === 1 ? 'y' : 'ies'}.`)
      setSelectedCategoryIds(new Set())
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
    onError: (e: any) => { setCategoryBulkResult(null); setCategoryBulkError(e?.response?.data?.detail ?? e.message) },
  })

  const [tmdbSyncResult, setTmdbSyncResult] = useState<string | null>(null)
  const syncCategoryNow = useMutation({
    mutationFn: (id: number) => api.post(`/vod/categories/${id}/sync-now/`),
    onSuccess: (r) => {
      setTmdbSyncResult(`List had ${r.data.list_total}: ${r.data.found_in_pool} in pool (${r.data.newly_placed} newly placed), ${r.data.not_in_pool} not in pool.`)
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
    onError: (e: any) => setTmdbSyncResult(`Sync failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  const [evaluateResult, setEvaluateResult] = useState<string | null>(null)
  const evaluateCategory = useMutation({
    mutationFn: (id: number) => api.post(`/vod/categories/${id}/evaluate/`),
    onSuccess: (r) => {
      setEvaluateResult(`Evaluated ${r.data.evaluated}: ${r.data.matched} matched, ${r.data.newly_placed} newly placed.`)
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
  })
  const [aiRuleDescription, setAiRuleDescription] = useState('')
  const [aiRuleSuggestion, setAiRuleSuggestion] = useState<{ name: string; match: string; conditions: { field: string; op: string; value: string }[] } | null>(null)
  const suggestAiRule = useMutation({
    mutationFn: () => api.post('/vod/ai/suggest-category-rule/', { description: aiRuleDescription, content_type: contentType }),
    onSuccess: (r) => setAiRuleSuggestion(r.data),
  })
  const createCategoryFromAiRule = useMutation({
    mutationFn: () => api.post('/vod/categories/', {
      name: aiRuleSuggestion!.name,
      content_type: contentType,
      is_smart: true,
      rule_json: JSON.stringify({ match: aiRuleSuggestion!.match, conditions: aiRuleSuggestion!.conditions }),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
      setAiRuleSuggestion(null)
      setAiRuleDescription('')
    },
  })
  const [aiEvaluateResult, setAiEvaluateResult] = useState<string | null>(null)
  const aiEvaluateCategory = useMutation({
    mutationFn: ({ id, description }: { id: number; description: string }) =>
      api.post(`/vod/categories/${id}/ai-evaluate/`, { description }),
    onSuccess: (r) => {
      const capNote = r.data.capped ? ` (capped at ${r.data.considered} of ${r.data.total_before_cap} candidates — narrow it with a rule pre-filter or run again)` : ''
      setAiEvaluateResult(`AI reviewed ${r.data.considered} candidate(s): ${r.data.matched} matched, ${r.data.newly_placed} newly placed.${capNote}`)
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
    onError: (e: any) => setAiEvaluateResult(`AI evaluation failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  function promptAiEvaluate(c: Category) {
    const description = window.prompt(
      `Describe what belongs in "${c.name}" in plain English (AI judges actual titles against this — good for criteria a field rule can't express, e.g. mood, plot, audience fit):`,
      c.ai_description ?? '',
    )
    if (description && description.trim()) aiEvaluateCategory.mutate({ id: c.id, description: description.trim() })
  }

  return (
    <Modal onClose={onClose} maxWidth="max-w-xl">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
        <span className="text-sm font-medium">{contentType === 'movie' ? 'Movie Categories' : 'TV Show Categories'}</span>
      </div>
      <div className="p-4 text-xs space-y-3 overflow-y-auto">
        {categories.length > 1 && (
          <>
            <div className="flex items-center gap-1.5">
              <input
                className={inputCls('flex-1')}
                placeholder="Search categories…"
                value={categorySearch}
                onChange={(e) => setCategorySearch(e.target.value)}
              />
              <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
                {(['all', 'selected', 'unselected'] as const).map((f) => (
                  <button
                    key={f}
                    className={`px-1.5 py-0.5 rounded text-[10px] transition-colors ${categoryShowFilter === f ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
                    onClick={() => setCategoryShowFilter(f)}
                  >
                    {f === 'all' ? 'All' : f === 'selected' ? 'Selected' : 'Unselected'}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <button
                className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                onClick={() => setSelectedCategoryIds(new Set([...selectedCategoryIds, ...visibleCategories.map((c) => c.id)]))}
              >
                Select visible ({visibleCategories.length})
              </button>
              <button
                className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                onClick={() => { const next = new Set(selectedCategoryIds); visibleCategories.forEach((c) => next.delete(c.id)); setSelectedCategoryIds(next) }}
              >
                Deselect visible ({visibleCategories.filter((c) => selectedCategoryIds.has(c.id)).length})
              </button>
              <span className="text-muted-foreground ml-auto">{selectedCategoryIds.size} selected total · shift-click to select a range</span>
            </div>
          </>
        )}
        {selectedCategoryIds.size > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 rounded border border-border/50 bg-muted/30 px-2 py-1.5">
            <Button size="sm" variant="outline" disabled={bulkSetCategoriesActive.isPending} onClick={() => bulkSetCategoriesActive.mutate(true)}>
              <Power size={12} className="mr-1" /> Enable selected
            </Button>
            <Button size="sm" variant="outline" disabled={bulkSetCategoriesActive.isPending} onClick={() => bulkSetCategoriesActive.mutate(false)}>
              <PowerOff size={12} className="mr-1" /> Disable selected
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={bulkDeleteCategories.isPending}
              onClick={() => { if (confirm(`Delete ${selectedCategoryIds.size} categor${selectedCategoryIds.size === 1 ? 'y' : 'ies'}? Items stay in the pool, just unplaced from these categories.`)) bulkDeleteCategories.mutate() }}
            >
              <Trash2 size={12} className="mr-1" /> Delete selected
            </Button>
            {categoryBulkError && <span className="text-destructive">{categoryBulkError}</span>}
            {categoryBulkResult && <span className="text-muted-foreground">{categoryBulkResult}</span>}
          </div>
        )}
        <ul className="space-y-0.5">
          {visibleCategories.map((c, i) => (
            <li key={c.id} className={`flex items-center justify-between gap-2 ${!c.is_active ? 'opacity-50' : ''}`}>
              <span className="flex items-center gap-1 min-w-0">
                <input
                  type="checkbox"
                  className="shrink-0"
                  checked={selectedCategoryIds.has(c.id)}
                  onChange={() => {}}
                  onClick={(e) => toggleCategorySelected(c.id, i, e.shiftKey)}
                />
                <input
                  className={inputCls('w-32')}
                  defaultValue={c.name}
                  key={c.name}
                  title="Rename category"
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v && v !== c.name) renameCategory.mutate({ id: c.id, name: v })
                  }}
                />
                {!!c.is_smart && <span className="text-muted-foreground"> (smart)</span>}
                {!!c.sync_source && <span className="text-muted-foreground"> (TMDB: {c.sync_source.replace('tmdb_list:', '')})</span>}
              </span>
              <span className="flex items-center gap-1.5">
                <input
                  className={inputCls('w-12')}
                  type="number"
                  title="Sort order (lower shows first in Dispatcharr)"
                  defaultValue={c.sort_order}
                  key={c.sort_order}
                  onBlur={(e) => {
                    const v = Number(e.target.value) || 0
                    if (v !== c.sort_order) setCategorySortOrder.mutate({ id: c.id, sort_order: v })
                  }}
                />
                {!!c.is_smart && (
                  <button title="Evaluate rule now" className="text-muted-foreground hover:text-foreground" disabled={evaluateCategory.isPending} onClick={() => evaluateCategory.mutate(c.id)}>
                    <Zap size={12} />
                  </button>
                )}
                <button
                  title={c.ai_description ? `AI Evaluate — "${c.ai_description}"` : 'AI Evaluate (judges actual titles against a plain-English description)'}
                  className="text-muted-foreground hover:text-foreground"
                  disabled={aiEvaluateCategory.isPending}
                  onClick={() => promptAiEvaluate(c)}
                >
                  <Sparkles size={12} />
                </button>
                {!!c.sync_source && (
                  <button title="Sync from TMDB now" className="text-muted-foreground hover:text-foreground" disabled={syncCategoryNow.isPending} onClick={() => syncCategoryNow.mutate(c.id)}>
                    <RefreshCw size={12} />
                  </button>
                )}
                <button
                  title={contentType === 'movie' ? 'View movies in this category' : 'View series in this category'}
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => onView(c.id)}
                >
                  <Eye size={12} />
                </button>
                <button
                  title={c.is_active ? 'Disable — stops exporting to Dispatcharr, keeps everything for later (e.g. a seasonal category)' : 'Enable — resumes exporting to Dispatcharr'}
                  className={c.is_active ? 'text-muted-foreground hover:text-foreground' : 'text-amber-500 hover:text-foreground'}
                  disabled={setCategoryActive.isPending}
                  onClick={() => setCategoryActive.mutate({ id: c.id, is_active: !c.is_active })}
                >
                  {c.is_active ? <Power size={12} /> : <PowerOff size={12} />}
                </button>
                <button
                  title={c.schedule_start_mmdd ? `Annual schedule: enable ${c.schedule_start_mmdd} → disable ${c.schedule_end_mmdd}` : 'Set an annual enable/disable schedule (e.g. a seasonal category)'}
                  className={c.schedule_start_mmdd ? 'text-primary hover:text-foreground' : 'text-muted-foreground hover:text-foreground'}
                  disabled={setCategorySchedule.isPending}
                  onClick={() => promptSchedule(c)}
                >
                  <CalendarClock size={12} />
                </button>
                <button title="Delete category" className="text-muted-foreground hover:text-destructive" onClick={() => { if (confirm(`Delete category "${c.name}"? Items stay in the pool, just unplaced from this category.`)) deleteCategory.mutate(c.id) }}>
                  <Trash2 size={12} />
                </button>
              </span>
            </li>
          ))}
          {categories.length === 0 && <p className="text-muted-foreground">No categories yet.</p>}
          {categories.length > 0 && visibleCategories.length === 0 && <p className="text-muted-foreground">No categories match.</p>}
        </ul>
        {categoryScheduleError && <p className="text-destructive">{categoryScheduleError}</p>}
        {categoryActiveError && <p className="text-destructive">{categoryActiveError}</p>}
        {evaluateResult && <p className="text-muted-foreground">{evaluateResult}</p>}
        {aiEvaluateResult && <p className="text-muted-foreground">{aiEvaluateResult}</p>}
        {tmdbSyncResult && <p className="text-muted-foreground">{tmdbSyncResult}</p>}

        <div className="border-t border-border/50 pt-2 flex flex-wrap items-center gap-1.5">
          <input
            className={inputCls()}
            placeholder="Category name"
            value={categoryForm.name}
            onChange={(e) => setCategoryForm({ ...categoryForm, name: e.target.value })}
          />
          <label className="flex items-center gap-1 text-muted-foreground">
            <input type="checkbox" checked={categoryForm.is_smart} onChange={(e) => setCategoryForm({ ...categoryForm, is_smart: e.target.checked })} />
            Smart (rule-based)
          </label>
          <Button size="sm" disabled={!categoryForm.name || (categoryForm.is_smart && !categoryForm.rule_value) || addCategory.isPending} onClick={() => addCategory.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
        {categoryForm.is_smart && (
          <div className="flex items-center gap-1.5 text-muted-foreground flex-wrap">
            <span>Rule: field</span>
            <select className={inputCls()} value={categoryForm.rule_field} onChange={(e) => setCategoryForm({ ...categoryForm, rule_field: e.target.value as typeof RULE_FIELDS[number] })}>
              {RULE_FIELDS.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <select className={inputCls()} value={categoryForm.rule_op} onChange={(e) => setCategoryForm({ ...categoryForm, rule_op: e.target.value as typeof RULE_OPS[number] })}>
              {RULE_OPS.map((op) => <option key={op} value={op}>{op}</option>)}
            </select>
            {categoryForm.rule_field === 'is_adult' ? (
              <select className={inputCls()} value={categoryForm.rule_value} onChange={(e) => setCategoryForm({ ...categoryForm, rule_value: e.target.value })}>
                <option value="">value…</option>
                <option value="1">Yes (adult)</option>
                <option value="0">No</option>
              </select>
            ) : (
              <input className={inputCls()} placeholder="value" value={categoryForm.rule_value} onChange={(e) => setCategoryForm({ ...categoryForm, rule_value: e.target.value })} />
            )}
          </div>
        )}

        <div className="border border-border rounded p-2 space-y-1.5">
          <p className="font-medium flex items-center gap-1"><Sparkles size={12} /> Suggest a category with AI</p>
          <p className="text-muted-foreground">
            Describe a category in plain English — Claude proposes a rule using only the fields/ops above (name,
            genre, year, country/language, director, is_adult). Review it before creating; nothing is saved until
            you click Create.
          </p>
          <div className="flex flex-wrap items-center gap-1.5">
            <input
              className={inputCls('flex-1 min-w-[14rem]')}
              placeholder='e.g. "90s action movies" or "kid-friendly animated films"'
              value={aiRuleDescription}
              onChange={(e) => setAiRuleDescription(e.target.value)}
            />
            <Button size="sm" variant="outline" disabled={!aiRuleDescription || suggestAiRule.isPending} onClick={() => suggestAiRule.mutate()}>
              {suggestAiRule.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Suggest'}
            </Button>
          </div>
          {suggestAiRule.isError && (
            <p className="text-destructive">{(suggestAiRule.error as any)?.response?.data?.detail ?? (suggestAiRule.error as any)?.message}</p>
          )}
          {aiRuleSuggestion && (
            <div className="space-y-1 border-t border-border/50 pt-1.5">
              <p><span className="text-muted-foreground">Name:</span> {aiRuleSuggestion.name}</p>
              <p className="text-muted-foreground">
                Match {aiRuleSuggestion.match.toUpperCase()} of:{' '}
                {aiRuleSuggestion.conditions.map((c, i) => (
                  <span key={i}>{i > 0 ? ', ' : ''}<code className="bg-muted px-1 rounded">{c.field} {c.op} "{c.value}"</code></span>
                ))}
              </p>
              <div className="flex items-center gap-1.5">
                <Button size="sm" disabled={createCategoryFromAiRule.isPending} onClick={() => createCategoryFromAiRule.mutate()}>
                  {createCategoryFromAiRule.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Create this category'}
                </Button>
                <Button size="sm" variant="outline" onClick={() => setAiRuleSuggestion(null)}>Discard</Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </Modal>
  )
}

// Same per-content-type scoping as CategoriesModal above, reusing
// NeedsReviewRow unchanged.
function NeedsReviewModal({ contentType, items, qc, xcCredentials, onClose }: {
  contentType: 'movie' | 'series'
  items: NeedsReviewItem[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  onClose: () => void
}) {
  return (
    <Modal onClose={onClose} maxWidth="max-w-2xl">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
        <span className="text-sm font-medium">
          Needs Review — {contentType === 'movie' ? 'Movies' : 'TV Shows'} ({items.length})
        </span>
      </div>
      <div className="p-4 text-xs overflow-y-auto">
        {items.length === 0 && <p className="text-muted-foreground">Nothing needs review right now.</p>}
        <ul>
          {items.map((item) => (
            <NeedsReviewRow key={item.id} contentType={contentType} item={item} qc={qc} xcCredentials={xcCredentials} />
          ))}
        </ul>
      </div>
    </Modal>
  )
}

// Unlike Needs Review (a small hand-curated flag list), missing-artwork can
// be thousands of items -- paginated server-side with its own search, same
// shape as the main Movies/TV Shows lists, rather than ever loading it whole.
function MissingArtworkModal({ contentType, qc, onClose }: {
  contentType: 'movie' | 'series'
  qc: ReturnType<typeof useQueryClient>
  onClose: () => void
}) {
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [showExcluded, setShowExcluded] = useState(false)
  // Free-text search can't isolate "everything in a foreign script" -- this
  // flags any title containing non-Latin-script characters (Arabic, Thai,
  // CJK, Cyrillic, Greek, Hebrew, Devanagari), so a whole language/region's
  // worth of titles can be archived in one "all filtered" action instead of
  // checking them off one at a time.
  const [nonLatinOnly, setNonLatinOnly] = useState(false)
  // Some providers tag language/dub variants with a leading "XX|" code
  // (e.g. "AR| Apex", "ALB| Apex") -- a more precise signal than script
  // detection alone since it also catches Latin-script variants (French,
  // German...), and works for whatever codes THIS deployment's providers
  // actually use rather than a fixed guessed-in-advance language list.
  const [selectedPrefixes, setSelectedPrefixes] = useState<Set<string>>(new Set())
  const prefixesParam = selectedPrefixes.size ? Array.from(selectedPrefixes).join(',') : undefined
  const LIMIT = 25
  const query = useQuery<{ items: MissingArtworkItem[]; total: number }>({
    queryKey: ['vod-missing-artwork', contentType, search, offset, showExcluded, nonLatinOnly, prefixesParam],
    queryFn:  () => api.get('/vod/missing-artwork/', {
      params: { content_type: contentType, search: search || undefined, limit: LIMIT, offset, excluded: showExcluded, script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam },
    }).then((r) => r.data),
  })
  const prefixesQuery = useQuery<{ code: string; count: number }[]>({
    queryKey: ['vod-missing-artwork-prefixes', contentType, search, showExcluded, nonLatinOnly],
    queryFn:  () => api.get('/vod/missing-artwork/prefixes/', {
      params: { content_type: contentType, search: search || undefined, excluded: showExcluded, script: nonLatinOnly ? 'non_latin' : undefined },
    }).then((r) => r.data),
  })
  function togglePrefix(code: string) {
    setSelectedPrefixes((prev) => {
      const next = new Set(prev)
      next.has(code) ? next.delete(code) : next.add(code)
      return next
    })
    setOffset(0)
  }

  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  function toggleSelected(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const [bulkPosterUrl, setBulkPosterUrl] = useState('')
  // Only used for archiving driven by the language/script filters above --
  // "archive all filtered" then only archives a title if a copy also
  // exists in one of these (or unprefixed), never your only copy of it.
  const [keepCodes, setKeepCodes] = useState('')
  const [bulkResult, setBulkResult] = useState<string | null>(null)
  const invalidateAfterBulk = () => {
    qc.invalidateQueries({ queryKey: ['vod-missing-artwork'] })
    qc.invalidateQueries({ queryKey: contentType === 'movie' ? ['vod-movies'] : ['vod-series'] })
    setSelectedIds(new Set())
  }
  const bulkApplyPoster = useMutation({
    mutationFn: (body: { ids?: number[]; search?: string }) =>
      api.post('/vod/missing-artwork/bulk-poster/', {
        content_type: contentType, poster_url: bulkPosterUrl.trim(), excluded: showExcluded,
        script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam, ...body,
      }),
    onSuccess: (r) => { setBulkResult(`Applied to ${r.data.applied}.`); setBulkPosterUrl(''); invalidateAfterBulk() },
    onError: (e: any) => setBulkResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  const bulkExclude = useMutation({
    mutationFn: (body: { set_excluded: boolean; ids?: number[]; search?: string }) =>
      api.post('/vod/missing-artwork/bulk-exclude/', {
        content_type: contentType, excluded: showExcluded,
        script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam,
        keep_codes: keepCodes.trim() || undefined, ...body,
      }),
    onSuccess: (r) => {
      const skipped = r.data.skipped as number | undefined
      setBulkResult(
        `${r.data.changed} updated.` +
        (skipped ? ` ${skipped} skipped (no copy in a kept language) — e.g. ${r.data.skipped_examples.slice(0, 3).join(', ')}` : '')
      )
      invalidateAfterBulk()
    },
    onError: (e: any) => setBulkResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })

  // Read-only preview of what archiving would actually do -- otherwise
  // changing "keep a title if also available as" has no visible effect
  // until after you've already committed, which looks like the field
  // isn't doing anything (only matters once a language/script filter is
  // active -- see the route's identical condition for when this applies).
  type ExcludePreview = { changed: number; skipped: number; skipped_examples: string[] }
  const previewAllFiltered = useQuery<ExcludePreview>({
    queryKey: ['vod-missing-artwork-preview-all', contentType, search, showExcluded, nonLatinOnly, prefixesParam, keepCodes],
    queryFn:  () => api.post('/vod/missing-artwork/bulk-exclude/', {
      content_type: contentType, excluded: showExcluded,
      script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam,
      keep_codes: keepCodes.trim() || undefined,
      set_excluded: !showExcluded, search: search || undefined, dry_run: true,
    }).then((r) => r.data),
    enabled: !showExcluded && !!query.data?.total,
  })
  const previewSelected = useQuery<ExcludePreview>({
    queryKey: ['vod-missing-artwork-preview-selected', contentType, showExcluded, nonLatinOnly, prefixesParam, keepCodes, Array.from(selectedIds).join(',')],
    queryFn:  () => api.post('/vod/missing-artwork/bulk-exclude/', {
      content_type: contentType, excluded: showExcluded,
      script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam,
      keep_codes: keepCodes.trim() || undefined,
      set_excluded: !showExcluded, ids: Array.from(selectedIds), dry_run: true,
    }).then((r) => r.data),
    enabled: !showExcluded && selectedIds.size > 0,
  })

  return (
    <Modal onClose={onClose} maxWidth="max-w-2xl">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border gap-2">
        <span className="text-sm font-medium shrink-0">
          Missing Artwork — {contentType === 'movie' ? 'Movies' : 'TV Shows'} ({query.data?.total ?? '…'})
        </span>
        <label className="flex items-center gap-1 text-xs text-muted-foreground shrink-0 cursor-pointer">
          <input type="checkbox" checked={showExcluded} onChange={(e) => { setShowExcluded(e.target.checked); setOffset(0); setSelectedIds(new Set()) }} />
          Show archived
        </label>
        <label className="flex items-center gap-1 text-xs text-muted-foreground shrink-0 cursor-pointer" title="Titles containing Arabic, Thai, CJK, Cyrillic, Greek, Hebrew, or Devanagari characters">
          <input type="checkbox" checked={nonLatinOnly} onChange={(e) => { setNonLatinOnly(e.target.checked); setOffset(0); setSelectedIds(new Set()) }} />
          Non-Latin script only
        </label>
        <input
          className={inputCls('w-36')}
          placeholder="Search…"
          defaultValue={search}
          onKeyDown={(e) => { if (e.key === 'Enter') { setSearch((e.target as HTMLInputElement).value.trim()); setOffset(0) } }}
          onBlur={(e) => { setSearch(e.target.value.trim()); setOffset(0) }}
        />
      </div>
      <div className="px-4 pt-3 text-xs space-y-1.5 border-b border-border pb-3">
        <p className="text-muted-foreground">
          {showExcluded
            ? 'Archived items are hidden from Missing Artwork, Needs Review, and Duplicate Finder — still fully browsable/playable, just not flagged as needing attention.'
            : 'Blanket-apply one image to many items at once (e.g. a generic logo for content that will never have a real per-title poster), or archive content you don\'t want flagged here.'}
        </p>
        {!!prefixesQuery.data?.length && (
          <div className="flex items-center gap-1 flex-wrap">
            <span className="text-muted-foreground shrink-0">Language prefix:</span>
            {prefixesQuery.data.map(({ code, count }) => (
              <button
                key={code}
                className={`px-1.5 py-0.5 rounded border text-xs ${selectedPrefixes.has(code) ? 'bg-primary text-primary-foreground border-primary' : 'border-border text-muted-foreground hover:text-foreground'}`}
                onClick={() => togglePrefix(code)}
              >
                {code} ({count})
              </button>
            ))}
            {!!selectedPrefixes.size && (
              <button className="text-muted-foreground hover:text-foreground underline decoration-dotted" onClick={() => { setSelectedPrefixes(new Set()); setOffset(0) }}>
                clear
              </button>
            )}
          </div>
        )}
        {!showExcluded && (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-muted-foreground shrink-0">Archiving by language keeps a title if also available as:</span>
            <input
              className={inputCls('w-28')}
              placeholder="e.g. EN (optional)"
              defaultValue={keepCodes}
              onKeyDown={(e) => { if (e.key === 'Enter') setKeepCodes((e.target as HTMLInputElement).value.trim()) }}
              onBlur={(e) => setKeepCodes(e.target.value.trim())}
            />
            {previewAllFiltered.isFetching && <Loader2 size={12} className="animate-spin text-muted-foreground" />}
          </div>
        )}
        <div className="flex items-center gap-1.5 flex-wrap">
          <input
            className={inputCls('flex-1 min-w-40')}
            placeholder="Poster URL to apply…"
            value={bulkPosterUrl}
            onChange={(e) => setBulkPosterUrl(e.target.value)}
          />
          <Button
            size="sm" variant="outline"
            disabled={!bulkPosterUrl.trim() || selectedIds.size === 0 || bulkApplyPoster.isPending}
            onClick={() => bulkApplyPoster.mutate({ ids: Array.from(selectedIds) })}
          >
            Apply to selected ({selectedIds.size})
          </Button>
          <Button
            size="sm" variant="outline"
            disabled={!bulkPosterUrl.trim() || !query.data?.total || bulkApplyPoster.isPending}
            onClick={() => bulkApplyPoster.mutate({ search: search || undefined })}
            title="Applies to every item matching the current search, not just this page"
          >
            Apply to all filtered ({query.data?.total ?? 0})
          </Button>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <Button
            size="sm" variant="outline"
            disabled={selectedIds.size === 0 || bulkExclude.isPending}
            onClick={() => bulkExclude.mutate({ set_excluded: !showExcluded, ids: Array.from(selectedIds) })}
          >
            {showExcluded
              ? `Un-archive selected (${selectedIds.size})`
              : previewSelected.data
                ? `Archive selected (${previewSelected.data.changed} of ${selectedIds.size}${previewSelected.data.skipped ? ` — ${previewSelected.data.skipped} would be skipped` : ''})`
                : `Archive selected (${selectedIds.size})`}
          </Button>
          <Button
            size="sm" variant="outline"
            disabled={!query.data?.total || bulkExclude.isPending}
            onClick={() => bulkExclude.mutate({ set_excluded: !showExcluded, search: search || undefined })}
            title="Applies to every item matching the current search, not just this page"
          >
            {showExcluded
              ? `Un-archive all filtered (${query.data?.total ?? 0})`
              : previewAllFiltered.data
                ? `Archive all filtered (${previewAllFiltered.data.changed} of ${query.data?.total ?? 0}${previewAllFiltered.data.skipped ? ` — ${previewAllFiltered.data.skipped} would be skipped` : ''})`
                : `Archive all filtered (${query.data?.total ?? 0})`}
          </Button>
          {bulkResult && <span className="text-muted-foreground">{bulkResult}</span>}
        </div>
      </div>
      <div className="p-4 text-xs overflow-y-auto">
        {query.data?.items.length === 0 && (
          <p className="text-muted-foreground">{showExcluded ? 'Nothing archived.' : 'Nothing missing artwork right now.'}</p>
        )}
        <ul>
          {query.data?.items.map((item) => (
            <MissingArtworkRow
              key={item.id} contentType={contentType} item={item} qc={qc}
              selected={selectedIds.has(item.id)}
              onToggleSelect={() => toggleSelected(item.id)}
            />
          ))}
        </ul>
        {query.data && <div className="pt-2"><Pager total={query.data.total} limit={LIMIT} offset={offset} onOffset={setOffset} /></div>}
      </div>
    </Modal>
  )
}

// Same script/prefix filtering as Missing Artwork, but over the WHOLE pool
// (a title with a real poster is just as much "not in my language" as one
// without) -- see vod_db.list_library_filtered's docstring. Archiving here
// always goes through the sibling check (vod_db.smart_bulk_exclude): a
// title only gets archived if a copy also exists in a kept language,
// never removing the only way to watch something.
function LibraryLanguageModal({ contentType, qc, onClose }: {
  contentType: 'movie' | 'series'
  qc: ReturnType<typeof useQueryClient>
  onClose: () => void
}) {
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [showExcluded, setShowExcluded] = useState(false)
  const [nonLatinOnly, setNonLatinOnly] = useState(false)
  const [selectedPrefixes, setSelectedPrefixes] = useState<Set<string>>(new Set())
  const [keepCodes, setKeepCodes] = useState('')
  const prefixesParam = selectedPrefixes.size ? Array.from(selectedPrefixes).join(',') : undefined
  const LIMIT = 25

  const query = useQuery<{ items: MissingArtworkItem[]; total: number }>({
    queryKey: ['vod-library-language', contentType, search, offset, showExcluded, nonLatinOnly, prefixesParam],
    queryFn:  () => api.get('/vod/library-language/', {
      params: { content_type: contentType, search: search || undefined, limit: LIMIT, offset, excluded: showExcluded, script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam },
    }).then((r) => r.data),
  })
  const prefixesQuery = useQuery<{ code: string; count: number }[]>({
    queryKey: ['vod-library-language-prefixes', contentType, search, showExcluded, nonLatinOnly],
    queryFn:  () => api.get('/vod/library-language/prefixes/', {
      params: { content_type: contentType, search: search || undefined, excluded: showExcluded, script: nonLatinOnly ? 'non_latin' : undefined },
    }).then((r) => r.data),
  })
  function togglePrefix(code: string) {
    setSelectedPrefixes((prev) => {
      const next = new Set(prev)
      next.has(code) ? next.delete(code) : next.add(code)
      return next
    })
    setOffset(0)
  }

  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  function toggleSelected(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const [bulkResult, setBulkResult] = useState<string | null>(null)
  const bulkExclude = useMutation({
    mutationFn: (body: { set_excluded: boolean; ids?: number[]; search?: string }) =>
      api.post('/vod/library-language/bulk-exclude/', {
        content_type: contentType, excluded: showExcluded,
        script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam,
        keep_codes: keepCodes.trim() || undefined, ...body,
      }),
    onSuccess: (r) => {
      const skipped = r.data.skipped as number | undefined
      setBulkResult(
        `${r.data.changed} updated.` +
        (skipped ? ` ${skipped} skipped (no copy in a kept language) — e.g. ${r.data.skipped_examples.slice(0, 3).join(', ')}` : '')
      )
      qc.invalidateQueries({ queryKey: ['vod-library-language'] })
      qc.invalidateQueries({ queryKey: contentType === 'movie' ? ['vod-movies'] : ['vod-series'] })
      setSelectedIds(new Set())
    },
    onError: (e: any) => setBulkResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })

  // Read-only preview of what "Archive all/selected filtered" would
  // actually do -- without this, changing "keep a title if also available
  // as" has no visible effect until after you've already committed the
  // archive, which looks like the field isn't doing anything.
  type ExcludePreview = { changed: number; skipped: number; skipped_examples: string[] }
  const previewAllFiltered = useQuery<ExcludePreview>({
    queryKey: ['vod-library-language-preview-all', contentType, search, showExcluded, nonLatinOnly, prefixesParam, keepCodes],
    queryFn:  () => api.post('/vod/library-language/bulk-exclude/', {
      content_type: contentType, excluded: showExcluded,
      script: nonLatinOnly ? 'non_latin' : undefined, prefixes: prefixesParam,
      keep_codes: keepCodes.trim() || undefined,
      set_excluded: !showExcluded, search: search || undefined, dry_run: true,
    }).then((r) => r.data),
    enabled: !showExcluded && !!query.data?.total,
  })
  const previewSelected = useQuery<ExcludePreview>({
    queryKey: ['vod-library-language-preview-selected', contentType, showExcluded, keepCodes, Array.from(selectedIds).join(',')],
    queryFn:  () => api.post('/vod/library-language/bulk-exclude/', {
      content_type: contentType, excluded: showExcluded,
      keep_codes: keepCodes.trim() || undefined,
      set_excluded: !showExcluded, ids: Array.from(selectedIds), dry_run: true,
    }).then((r) => r.data),
    enabled: !showExcluded && selectedIds.size > 0,
  })

  return (
    <Modal onClose={onClose} maxWidth="max-w-2xl">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border gap-2">
        <span className="text-sm font-medium shrink-0">
          Language Filter — {contentType === 'movie' ? 'Movies' : 'TV Shows'} ({query.data?.total ?? '…'})
        </span>
        <label className="flex items-center gap-1 text-xs text-muted-foreground shrink-0 cursor-pointer">
          <input type="checkbox" checked={showExcluded} onChange={(e) => { setShowExcluded(e.target.checked); setOffset(0); setSelectedIds(new Set()) }} />
          Show archived
        </label>
        <label className="flex items-center gap-1 text-xs text-muted-foreground shrink-0 cursor-pointer" title="Titles containing Arabic, Thai, CJK, Cyrillic, Greek, Hebrew, or Devanagari characters">
          <input type="checkbox" checked={nonLatinOnly} onChange={(e) => { setNonLatinOnly(e.target.checked); setOffset(0); setSelectedIds(new Set()) }} />
          Non-Latin script only
        </label>
        <input
          className={inputCls('w-36')}
          placeholder="Search…"
          defaultValue={search}
          onKeyDown={(e) => { if (e.key === 'Enter') { setSearch((e.target as HTMLInputElement).value.trim()); setOffset(0) } }}
          onBlur={(e) => { setSearch(e.target.value.trim()); setOffset(0) }}
        />
      </div>
      <div className="px-4 pt-3 text-xs space-y-1.5 border-b border-border pb-3">
        <p className="text-muted-foreground">
          {showExcluded
            ? 'Archived items are hidden from Missing Artwork, Needs Review, and Duplicate Finder — still fully browsable/playable, just not flagged as needing attention.'
            : 'Filters the whole library by language, not just items missing a poster. Archiving only removes a title from these queues if a copy also exists in a kept language -- your only copy of something is never archived this way.'}
        </p>
        {!!prefixesQuery.data?.length && (
          <div className="flex items-center gap-1 flex-wrap">
            <span className="text-muted-foreground shrink-0">Language prefix:</span>
            {prefixesQuery.data.map(({ code, count }) => (
              <button
                key={code}
                className={`px-1.5 py-0.5 rounded border text-xs ${selectedPrefixes.has(code) ? 'bg-primary text-primary-foreground border-primary' : 'border-border text-muted-foreground hover:text-foreground'}`}
                onClick={() => togglePrefix(code)}
              >
                {code} ({count})
              </button>
            ))}
            {!!selectedPrefixes.size && (
              <button className="text-muted-foreground hover:text-foreground underline decoration-dotted" onClick={() => { setSelectedPrefixes(new Set()); setOffset(0) }}>
                clear
              </button>
            )}
          </div>
        )}
        {!showExcluded && (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-muted-foreground shrink-0">Keep a title if also available as:</span>
            <input
              className={inputCls('w-28')}
              placeholder="e.g. EN (optional)"
              defaultValue={keepCodes}
              onKeyDown={(e) => { if (e.key === 'Enter') setKeepCodes((e.target as HTMLInputElement).value.trim()) }}
              onBlur={(e) => setKeepCodes(e.target.value.trim())}
            />
            {previewAllFiltered.isFetching && <Loader2 size={12} className="animate-spin text-muted-foreground" />}
          </div>
        )}
        <div className="flex items-center gap-1.5 flex-wrap">
          <Button
            size="sm" variant="outline"
            disabled={selectedIds.size === 0 || bulkExclude.isPending}
            onClick={() => bulkExclude.mutate({ set_excluded: !showExcluded, ids: Array.from(selectedIds) })}
          >
            {showExcluded
              ? `Un-archive selected (${selectedIds.size})`
              : previewSelected.data
                ? `Archive selected (${previewSelected.data.changed} of ${selectedIds.size}${previewSelected.data.skipped ? ` — ${previewSelected.data.skipped} would be skipped` : ''})`
                : `Archive selected (${selectedIds.size})`}
          </Button>
          <Button
            size="sm" variant="outline"
            disabled={!query.data?.total || bulkExclude.isPending}
            onClick={() => bulkExclude.mutate({ set_excluded: !showExcluded, search: search || undefined })}
            title="Applies to every item matching the current search/language filter, not just this page"
          >
            {showExcluded
              ? `Un-archive all filtered (${query.data?.total ?? 0})`
              : previewAllFiltered.data
                ? `Archive all filtered (${previewAllFiltered.data.changed} of ${query.data?.total ?? 0}${previewAllFiltered.data.skipped ? ` — ${previewAllFiltered.data.skipped} would be skipped` : ''})`
                : `Archive all filtered (${query.data?.total ?? 0})`}
          </Button>
          {bulkResult && <span className="text-muted-foreground">{bulkResult}</span>}
        </div>
      </div>
      <div className="p-4 text-xs overflow-y-auto">
        {query.data?.items.length === 0 && (
          <p className="text-muted-foreground">{showExcluded ? 'Nothing archived.' : 'No matches.'}</p>
        )}
        <ul>
          {query.data?.items.map((item) => (
            <li key={item.id} className="border-b border-border/50 py-1.5 flex items-center gap-1.5">
              <input type="checkbox" checked={selectedIds.has(item.id)} onChange={() => toggleSelected(item.id)} />
              <span className="min-w-0 truncate">{item.name} {item.year && <span className="text-muted-foreground">({item.year})</span>}</span>
            </li>
          ))}
        </ul>
        {query.data && <div className="pt-2"><Pager total={query.data.total} limit={LIMIT} offset={offset} onOffset={setOffset} /></div>}
      </div>
    </Modal>
  )
}

export type VodManagerTab = 'movies' | 'series' | 'curation' | 'providers' | 'config' | 'dvr'
export type DvrSubTab = 'scheduled' | 'users' | 'library' | 'missing' | 'metrics'

export default function VodManager({ activeTab, setActiveTab, dvrSubTab, setDvrSubTabPersisted }: {
  activeTab: VodManagerTab
  setActiveTab: (t: VodManagerTab) => void
  dvrSubTab: DvrSubTab
  setDvrSubTabPersisted: (t: DvrSubTab) => void
}) {
  const qc = useQueryClient()

  const [movieViewMode, setMovieViewModeState] = useState<'list' | 'grid'>(
    () => (localStorage.getItem('vodmanager-movies-view') === 'grid' ? 'grid' : 'list')
  )
  function setMovieViewMode(m: 'list' | 'grid') {
    localStorage.setItem('vodmanager-movies-view', m)
    setMovieViewModeState(m)
  }
  const [seriesViewMode, setSeriesViewModeState] = useState<'list' | 'grid'>(
    () => (localStorage.getItem('vodmanager-series-view') === 'grid' ? 'grid' : 'list')
  )
  function setSeriesViewMode(m: 'list' | 'grid') {
    localStorage.setItem('vodmanager-series-view', m)
    setSeriesViewModeState(m)
  }
  const [categoriesModalOpen, setCategoriesModalOpen] = useState<'movie' | 'series' | null>(null)
  const [needsReviewModalOpen, setNeedsReviewModalOpen] = useState<'movie' | 'series' | null>(null)
  const [missingArtworkModalOpen, setMissingArtworkModalOpen] = useState<'movie' | 'series' | null>(null)
  const [libraryLanguageModalOpen, setLibraryLanguageModalOpen] = useState<'movie' | 'series' | null>(null)

  // ── Activity (currently open stream relays) ──
  const activityQuery = useQuery<ActivitySession[]>({
    queryKey: ['vod-activity'],
    queryFn:  () => api.get('/vod/activity/').then((r) => r.data),
    refetchInterval: 3000,
  })
  const killSession = useMutation({
    mutationFn: (connId: string) => api.post(`/vod/activity/${connId}/kill/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-activity'] }),
  })


  // ── Connected Instances (per-instance XC credentials) ──
  const xcClientsQuery = useQuery<XcClient[]>({
    queryKey: ['vod-xc-clients'],
    queryFn:  () => api.get('/vod/clients/').then((r) => r.data),
  })
  const [newClientLabel, setNewClientLabel] = useState('')
  const [newClientIpAllowlist, setNewClientIpAllowlist] = useState('')
  const createXcClient = useMutation({
    mutationFn: () => api.post('/vod/clients/', { label: newClientLabel, ip_allowlist: newClientIpAllowlist || null }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-xc-clients'] })
      setNewClientLabel('')
      setNewClientIpAllowlist('')
    },
  })
  const toggleXcClient = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => api.patch(`/vod/clients/${id}/`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-xc-clients'] }),
  })
  const regenerateXcClient = useMutation({
    mutationFn: (id: number) => api.post(`/vod/clients/${id}/regenerate/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-xc-clients'] }),
  })
  const deleteXcClient = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/clients/${id}/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-xc-clients'] }),
  })
  const [revealedClientId, setRevealedClientId] = useState<number | null>(null)
  const [expandedCategoryAccessClientId, setExpandedCategoryAccessClientId] = useState<number | null>(null)
  const [categoryAccessForm, setCategoryAccessForm] = useState<Set<number> | null>(null)
  const setClientCategoryAllowlist = useMutation({
    mutationFn: ({ id, ids }: { id: number; ids: number[] | null }) =>
      api.patch(`/vod/clients/${id}/`, ids === null ? { clear_category_allowlist: true } : { category_allowlist: ids.join(',') }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-xc-clients'] })
      setExpandedCategoryAccessClientId(null)
      setCategoryAccessForm(null)
    },
  })

  // ── Dispatcharr connections (who VOD Manager reaches out to -- the other
  // side of xc_clients above, who's allowed to reach in) ──
  const dispatcharrConnectionsQuery = useQuery<DispatcharrConnection[]>({
    queryKey: ['vod-dispatcharr-connections'],
    queryFn:  () => api.get('/vod/dispatcharr-connections/').then((r) => r.data),
  })
  const [newConnLabel, setNewConnLabel] = useState('')
  const [newConnUrl, setNewConnUrl] = useState('')
  const [newConnToken, setNewConnToken] = useState('')
  const createDispatcharrConnection = useMutation({
    mutationFn: () => api.post('/vod/dispatcharr-connections/', { label: newConnLabel, url: newConnUrl, token: newConnToken }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-dispatcharr-connections'] })
      setNewConnLabel(''); setNewConnUrl(''); setNewConnToken('')
    },
  })
  const updateDispatcharrConnection = useMutation({
    mutationFn: ({ id, ...body }: { id: number; label?: string; url?: string; token?: string; vod_relay_account_id?: number; clear_vod_relay_account_id?: boolean }) =>
      api.patch(`/vod/dispatcharr-connections/${id}/`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-dispatcharr-connections'] }),
  })
  const deleteDispatcharrConnection = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/dispatcharr-connections/${id}/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-dispatcharr-connections'] }),
    onError:    (e: any) => alert(e?.response?.data?.detail ?? 'Delete failed.'),
  })
  const [revealedConnId, setRevealedConnId] = useState<number | null>(null)

  // DVR is a capability of a connection, not a separate "provider" the
  // admin adds elsewhere -- see backend/vod_db.py's enable_dvr_for_connection
  // docstring for why a providers row still exists underneath (the pool's
  // multi-source failover/export queries need it), even though nothing here
  // ever calls it that. dvrModalConnectionId drives the DVR settings Modal
  // below the Dispatcharr Connections table.
  const [dvrModalConnectionId, setDvrModalConnectionId] = useState<number | null>(null)
  const [dvrModalForm, setDvrModalForm] = useState({
    dvr_local_path: '', dvr_movie_category_id: '', dvr_series_category_id: '', priority: '0',
  })
  const enableDvrForConnection = useMutation({
    mutationFn: (connectionId: number) => api.post(`/vod/dispatcharr-connections/${connectionId}/dvr/`, {
      dvr_local_path: dvrModalForm.dvr_local_path.trim() || null,
      dvr_movie_category_id: dvrModalForm.dvr_movie_category_id ? Number(dvrModalForm.dvr_movie_category_id) : null,
      dvr_series_category_id: dvrModalForm.dvr_series_category_id ? Number(dvrModalForm.dvr_series_category_id) : null,
      priority: Number(dvrModalForm.priority) || 0,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
      setDvrModalConnectionId(null)
    },
  })
  const disableDvrForConnection = useMutation({
    mutationFn: (connectionId: number) => api.delete(`/vod/dispatcharr-connections/${connectionId}/dvr/`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
      setDvrModalConnectionId(null)
    },
  })

  // Automated one-shot: creates the XC client + Dispatcharr-side M3U
  // account + saved connection in one step instead of doing all three by
  // hand (see vod_sync.connect_dispatcharr_instance).
  const [connectLabel, setConnectLabel] = useState('')
  const [connectUrl, setConnectUrl] = useState('')
  const [connectToken, setConnectToken] = useState('')
  const [connectPublicUrl, setConnectPublicUrl] = useState(window.location.origin)
  const [connectResult, setConnectResult] = useState<string | null>(null)
  const connectInstance = useMutation({
    mutationFn: () => api.post('/vod/dispatcharr-connections/connect/', {
      label: connectLabel, url: connectUrl, token: connectToken, vod_manager_public_url: connectPublicUrl,
    }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['vod-dispatcharr-connections'] })
      qc.invalidateQueries({ queryKey: ['vod-xc-clients'] })
      setConnectResult(`Connected — Dispatcharr account #${r.data.dispatcharr_account.id} created, pointed at ${connectPublicUrl}. Go enable VOD and pick groups for it on that instance.`)
      setConnectLabel(''); setConnectUrl(''); setConnectToken('')
    },
    onError: (e: any) => setConnectResult(`Connect failed: ${e?.response?.data?.detail ?? e.message}`),
  })

  // ── Bulk enrichment ──
  const enrichProgressQuery = useQuery<EnrichProgress>({
    queryKey: ['vod-enrich-progress'],
    queryFn:  () => api.get('/vod/enrich-all/status/').then((r) => r.data),
    refetchInterval: (query) => (query.state.data?.running ? 2000 : false),
  })
  const xcCredentialsQuery = useQuery<XcCredentials>({
    queryKey: ['vod-xc-credentials'],
    queryFn:  () => api.get('/vod/xc-credentials/').then((r) => r.data),
    retry: false,
  })
  const tmdbSettingsQuery = useQuery<{ has_api_key: boolean }>({
    queryKey: ['vod-tmdb-settings'],
    queryFn:  () => api.get('/vod/tmdb-settings/').then((r) => r.data),
  })
  const [tmdbApiKeyInput, setTmdbApiKeyInput] = useState('')
  const saveTmdbApiKey = useMutation({
    mutationFn: () => api.post('/vod/tmdb-settings/', { api_key: tmdbApiKeyInput }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-tmdb-settings'] })
      setTmdbApiKeyInput('')
    },
  })
  const aiSettingsQuery = useQuery<{
    provider: AiProvider
    model: string
    has_anthropic_key: boolean
    has_openai_key: boolean
    has_gemini_key: boolean
  }>({
    queryKey: ['vod-ai-settings'],
    queryFn:  () => api.get('/vod/ai-settings/').then((r) => r.data),
  })
  const [aiModelInput, setAiModelInput] = useState('')
  const saveAiProvider = useMutation({
    mutationFn: (body: { provider: string; model?: string }) => api.post('/vod/ai-settings/', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-ai-settings'] })
      setAiModelInput('')
    },
  })
  const defaultCategoriesPromptQuery = useQuery<{ show: boolean }>({
    queryKey: ['vod-default-categories-prompt'],
    queryFn:  () => api.get('/vod/default-categories-prompt/').then((r) => r.data),
  })
  const answerDefaultCategoriesPrompt = useMutation({
    mutationFn: (includeAdult: boolean) => api.post('/vod/default-categories-prompt/', { include_adult: includeAdult }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-default-categories-prompt'] })
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
  })
  const importLanguageExclusionQuery = useQuery<{ exclude_prefixes: string[]; exclude_non_latin: boolean }>({
    queryKey: ['vod-import-language-exclusion'],
    queryFn:  () => api.get('/vod/import-language-exclusion/').then((r) => r.data),
  })
  const languagePrefixesQuery = useQuery<{ code: string; count: number }[]>({
    queryKey: ['vod-import-language-prefixes'],
    queryFn:  () => api.get('/vod/import-language-exclusion/prefixes/').then((r) => r.data),
  })
  const saveImportLanguageExclusion = useMutation({
    mutationFn: (body: { exclude_prefixes: string[]; exclude_non_latin: boolean }) =>
      api.post('/vod/import-language-exclusion/', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-import-language-exclusion'] }),
  })
  // Draft selection, committed only on Save -- codes are whatever a
  // provider happens to tag content with (not a fixed list), so the picker
  // is built from what's actually seen in the pool right now, unioned with
  // whatever's already saved (a previously-excluded code a provider no
  // longer uses should still show up, checked, not silently vanish).
  const [languageSearch, setLanguageSearch] = useState('')
  const [languageShowFilter, setLanguageShowFilter] = useState<'all' | 'selected' | 'unselected'>('all')
  const [languageDraft, setLanguageDraft] = useState<Set<string>>(new Set())
  const [languageLastClickedIndex, setLanguageLastClickedIndex] = useState<number | null>(null)
  const languageDraftInitialized = useRef(false)
  useEffect(() => {
    if (languageDraftInitialized.current || !importLanguageExclusionQuery.data) return
    languageDraftInitialized.current = true
    setLanguageDraft(new Set(importLanguageExclusionQuery.data.exclude_prefixes))
  }, [importLanguageExclusionQuery.data])
  const allLanguageCodes = (() => {
    const counts = new Map((languagePrefixesQuery.data ?? []).map((p) => [p.code, p.count]))
    for (const code of importLanguageExclusionQuery.data?.exclude_prefixes ?? []) {
      if (!counts.has(code)) counts.set(code, 0)
    }
    return [...counts.entries()]
      .map(([code, count]) => ({ code, count }))
      .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code))
  })()
  const visibleLanguageCodes = allLanguageCodes.filter((c) => {
    const label = `${c.code} ${LANGUAGE_CODE_NAMES[c.code] ?? ''}`.toLowerCase()
    if (languageSearch && !label.includes(languageSearch.toLowerCase())) return false
    if (languageShowFilter === 'selected' && !languageDraft.has(c.code)) return false
    if (languageShowFilter === 'unselected' && languageDraft.has(c.code)) return false
    return true
  })
  function toggleLanguageSelected(code: string, index: number, shiftKey: boolean) {
    const willBeChecked = !languageDraft.has(code)
    const next = new Set(languageDraft)
    if (shiftKey && languageLastClickedIndex != null) {
      const [start, end] = [languageLastClickedIndex, index].sort((a, b) => a - b)
      for (let j = start; j <= end; j++) {
        const c = visibleLanguageCodes[j]?.code
        if (c == null) continue
        if (willBeChecked) next.add(c); else next.delete(c)
      }
    } else {
      if (willBeChecked) next.add(code); else next.delete(code)
    }
    setLanguageDraft(next)
    setLanguageLastClickedIndex(index)
  }
  const [applyExclusionsJobId, setApplyExclusionsJobId] = useState<string | null>(null)
  const applyImportExclusionsNow = useMutation({
    mutationFn: () => api.post('/vod/import-exclusions/apply-now/'),
    onSuccess: (r) => setApplyExclusionsJobId(r.data.job_id),
  })
  type ApplyExclusionsProviderResult = {
    provider: string; error?: string
    movies_created?: number; movies_matched?: number; movies_archived?: number; movies_unarchived?: number
    series_created?: number; series_matched?: number; series_archived?: number; series_unarchived?: number
  }
  const applyExclusionsJobQuery = useQuery<{
    status: string; total: number; completed: number; current_provider: string | null
    results: ApplyExclusionsProviderResult[]; error: string | null
  }>({
    queryKey: ['vod-apply-exclusions-job', applyExclusionsJobId],
    queryFn:  () => api.get(`/vod/import-exclusions/apply-now/${applyExclusionsJobId}/`).then((r) => r.data),
    enabled:  !!applyExclusionsJobId,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1200 : false),
  })
  useEffect(() => {
    if (applyExclusionsJobQuery.data?.status === 'done') {
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applyExclusionsJobQuery.data?.status])
  const [aiKeyInputs, setAiKeyInputs] = useState<{ anthropic: string; openai: string; gemini: string }>({
    anthropic: '', openai: '', gemini: '',
  })
  const saveAiKey = useMutation({
    mutationFn: (provider: 'anthropic' | 'openai' | 'gemini') =>
      api.post('/vod/ai-settings/key/', { provider, api_key: aiKeyInputs[provider] }),
    onSuccess: (_res, provider) => {
      qc.invalidateQueries({ queryKey: ['vod-ai-settings'] })
      setAiKeyInputs((prev) => ({ ...prev, [provider]: '' }))
    },
  })
  const lockoutSettingsQuery = useQuery<LockoutSettings>({
    queryKey: ['vod-lockout-settings'],
    queryFn:  () => api.get('/vod/lockout-settings/').then((r) => r.data),
  })
  const [lockoutForm, setLockoutForm] = useState<LockoutSettings | null>(null)
  const lockoutValues = lockoutForm ?? lockoutSettingsQuery.data ?? null
  const saveLockoutSettings = useMutation({
    mutationFn: () => api.post('/vod/lockout-settings/', lockoutValues),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-lockout-settings'] })
      setLockoutForm(null)
    },
  })
  const refreshSettingsQuery = useQuery<RefreshSettings>({
    queryKey: ['vod-refresh-settings'],
    queryFn:  () => api.get('/vod/refresh-settings/').then((r) => r.data),
  })
  const [refreshForm, setRefreshForm] = useState<{
    catalog_refresh_hours_xc: string
    catalog_refresh_hours_plex: string
    catalog_refresh_hours_emby: string
    catalog_refresh_hours_jellyfin: string
    enrichment_ttl_hours: string
    tmdb_sync_hours: string
  } | null>(null)
  const secToHrStr = (s: number | null | undefined) => (s == null ? '' : String(s / 3600))
  const refreshValues = refreshForm ?? (refreshSettingsQuery.data ? {
    catalog_refresh_hours_xc:       secToHrStr(refreshSettingsQuery.data.catalog_refresh_seconds_xc),
    catalog_refresh_hours_plex:     secToHrStr(refreshSettingsQuery.data.catalog_refresh_seconds_plex),
    catalog_refresh_hours_emby:     secToHrStr(refreshSettingsQuery.data.catalog_refresh_seconds_emby),
    catalog_refresh_hours_jellyfin: secToHrStr(refreshSettingsQuery.data.catalog_refresh_seconds_jellyfin),
    enrichment_ttl_hours:           secToHrStr(refreshSettingsQuery.data.enrichment_ttl_seconds),
    tmdb_sync_hours:                secToHrStr(refreshSettingsQuery.data.tmdb_sync_interval_seconds),
  } : null)
  const saveRefreshSettings = useMutation({
    mutationFn: () => {
      const hrToSec = (v: string) => Math.round(Number(v) * 3600)
      return api.post('/vod/refresh-settings/', {
        catalog_refresh_seconds_xc:       hrToSec(refreshValues!.catalog_refresh_hours_xc),
        catalog_refresh_seconds_plex:     hrToSec(refreshValues!.catalog_refresh_hours_plex),
        catalog_refresh_seconds_emby:     hrToSec(refreshValues!.catalog_refresh_hours_emby),
        catalog_refresh_seconds_jellyfin: hrToSec(refreshValues!.catalog_refresh_hours_jellyfin),
        enrichment_ttl_seconds:           hrToSec(refreshValues!.enrichment_ttl_hours),
        tmdb_sync_interval_seconds:       refreshValues!.tmdb_sync_hours.trim() ? hrToSec(refreshValues!.tmdb_sync_hours) : null,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-refresh-settings'] })
      setRefreshForm(null)
    },
  })
  // ── Backup & Restore ──
  const backupComponentsQuery = useQuery<BackupComponent[]>({
    queryKey: ['backup-components'],
    queryFn:  () => api.get('/backup/components/').then((r) => r.data),
  })
  const [backupBusyId, setBackupBusyId] = useState<string | null>(null)
  async function downloadBackup(c: BackupComponent) {
    setBackupBusyId(c.id)
    try {
      const res = await api.get(`/backup/download/${c.id}/`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = c.id === 'database' ? 'vod_db.sqlite' : `${c.id}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } finally {
      setBackupBusyId(null)
    }
  }
  const restoreBackup = useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => {
      const form = new FormData()
      form.append('file', file)
      return api.post(`/backup/restore/${id}/`, form)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['backup-components'] }),
  })
  const [diagnosticsBusy, setDiagnosticsBusy] = useState(false)
  async function downloadDiagnostics() {
    setDiagnosticsBusy(true)
    try {
      const res = await api.get('/diagnostics/logs/', { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      a.download = `vod-manager-diagnostics-${stamp}.log`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } finally {
      setDiagnosticsBusy(false)
    }
  }
  const resetBackup = useMutation({
    mutationFn: (id: string) => api.post(`/backup/reset/${id}/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['backup-components'] }),
  })
  function formatBytes(n: number): string {
    if (n < 1024) return `${n} B`
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
    return `${(n / 1024 / 1024).toFixed(1)} MB`
  }
  const restoreFileInputRef = useRef<HTMLInputElement>(null)
  const [restoreTargetId, setRestoreTargetId] = useState<string | null>(null)
  function handleRestoreFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (file && restoreTargetId) restoreBackup.mutate({ id: restoreTargetId, file })
    setRestoreTargetId(null)
  }

  const startBulkEnrich = useMutation({
    mutationFn: () => api.post('/vod/enrich-all/'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-enrich-progress'] })
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
  })
  const enrichProgress = enrichProgressQuery.data
  const wasEnrichRunning = useRef(false)
  useEffect(() => {
    if (wasEnrichRunning.current && enrichProgress && !enrichProgress.running) {
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    }
    wasEnrichRunning.current = !!enrichProgress?.running
  }, [enrichProgress?.running])

  // ── Metadata rewrite rules ──
  const metadataRulesQuery = useQuery<MetadataRule[]>({
    queryKey: ['vod-metadata-rules'],
    queryFn:  () => api.get('/vod/metadata-rules/').then((r) => r.data),
  })
  const [ruleForm, setRuleForm] = useState({
    content_type: 'both' as 'movie' | 'series' | 'both',
    field: 'name' as typeof REWRITABLE_FIELDS[number],
    pattern: '', replacement: '',
  })
  const addRule = useMutation({
    mutationFn: () => api.post('/vod/metadata-rules/', ruleForm),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-metadata-rules'] })
      setRuleForm({ content_type: 'both', field: 'name', pattern: '', replacement: '' })
    },
  })
  const toggleRuleActive = useMutation({
    mutationFn: ({ id, active }: { id: number; active: boolean }) =>
      api.post(`/vod/metadata-rules/${id}/active/`, null, { params: { is_active: active } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-metadata-rules'] }),
  })
  const deleteRule = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/metadata-rules/${id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-metadata-rules'] }),
  })
  const [applyRulesResult, setApplyRulesResult] = useState<string | null>(null)
  const applyRules = useMutation({
    mutationFn: (content_type: 'movie' | 'series') => api.post('/vod/metadata-rules/apply/', null, { params: { content_type } }),
    onSuccess: (r, content_type) => {
      setApplyRulesResult(`${content_type}: checked ${r.data.checked}, changed ${r.data.changed}.`)
      qc.invalidateQueries({ queryKey: [content_type === 'movie' ? 'vod-movies' : 'vod-series'] })
    },
  })

  // ── Providers ──
  const providersQuery = useQuery<Provider[]>({
    queryKey: ['vod-providers'],
    queryFn:  () => api.get('/vod/providers/').then((r) => r.data),
  })
  const [providerForm, setProviderForm] = useState({
    name: '', base_url: '', username: '', password: '', max_streams: '0', priority: '0',
    provider_type: 'xc' as 'xc' | 'plex' | 'emby' | 'jellyfin',
  })
  const addProvider = useMutation({
    mutationFn: () => api.post('/vod/providers/', {
      name: providerForm.name,
      base_url: providerForm.base_url,
      username: providerForm.provider_type === 'xc' ? providerForm.username : '',
      password: providerForm.password,
      max_streams: Number(providerForm.max_streams) || 0,
      priority: Number(providerForm.priority) || 0, provider_type: providerForm.provider_type,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
      setProviderForm({ name: '', base_url: '', username: '', password: '', max_streams: '0', priority: '0', provider_type: 'xc' })
    },
  })
  const syncProvider = useMutation({
    mutationFn: (id: number) => api.post(`/vod/providers/${id}/sync/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const setProviderPriority = useMutation({
    mutationFn: ({ id, priority }: { id: number; priority: number }) =>
      api.post(`/vod/providers/${id}/priority/`, null, { params: { priority } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const setProviderName = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api.post(`/vod/providers/${id}/name/`, null, { params: { name } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const setProviderBaseUrl = useMutation({
    mutationFn: ({ id, base_url }: { id: number; base_url: string }) =>
      api.post(`/vod/providers/${id}/base-url/`, null, { params: { base_url } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const setProviderMaxStreams = useMutation({
    mutationFn: ({ id, max_streams }: { id: number; max_streams: number }) =>
      api.post(`/vod/providers/${id}/max-streams/`, null, { params: { max_streams } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const setProviderUserAgent = useMutation({
    mutationFn: ({ id, custom_user_agent }: { id: number; custom_user_agent: string }) =>
      api.post(`/vod/providers/${id}/user-agent/`, null, { params: { custom_user_agent } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const setProviderSharedLimit = useMutation({
    mutationFn: ({ id, shared_connection_limit }: { id: number; shared_connection_limit: number }) =>
      api.post(`/vod/providers/${id}/shared-limit/`, null, { params: { shared_connection_limit } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const [excludeCategoriesProviderId, setExcludeCategoriesProviderId] = useState<number | null>(null)
  const [excludeCategoriesDraft, setExcludeCategoriesDraft] = useState<Set<string>>(new Set())
  const [excludeCategoriesSearch, setExcludeCategoriesSearch] = useState('')
  const [excludeCategoriesShowFilter, setExcludeCategoriesShowFilter] = useState<'all' | 'selected' | 'unselected'>('all')
  const [excludeCategoriesLastClickedIndex, setExcludeCategoriesLastClickedIndex] = useState<number | null>(null)
  const [excludeCategoriesError, setExcludeCategoriesError] = useState<string | null>(null)
  const providerAvailableCategoriesQuery = useQuery<{ categories: string[] }>({
    queryKey: ['vod-provider-available-categories', excludeCategoriesProviderId],
    queryFn:  () => api.get(`/vod/providers/${excludeCategoriesProviderId}/available-categories/`).then((r) => r.data),
    enabled:  excludeCategoriesProviderId != null,
  })
  const setProviderImportExcludeCategories = useMutation({
    mutationFn: ({ id, category_names }: { id: number; category_names: string[] }) =>
      api.post(`/vod/providers/${id}/import-exclude-categories/`, { category_names }),
    onSuccess: async () => {
      // Awaited, not fire-and-forget -- closing the dialog before this
      // resolves let a reopen race the refetch and show stale (pre-save)
      // categories, which looked exactly like "my selection didn't stick".
      await qc.invalidateQueries({ queryKey: ['vod-providers'] })
      setExcludeCategoriesError(null)
      setExcludeCategoriesProviderId(null)
    },
    onError: (e: any) => setExcludeCategoriesError(e?.response?.data?.detail ?? e.message ?? 'Save failed.'),
  })
  // ── DVR recording profiles (Phase 2) ──
  const [recordingProfilesProviderId, setRecordingProfilesProviderId] = useState<number | null>(null)
  const dvrProviders = providersQuery.data?.filter((p) => p.provider_type === 'dispatcharr_dvr') ?? []
  // Auto-select the first DVR provider once the list loads, so the DVR tab
  // has something showing without requiring a click first -- most setups
  // only have one anyway. Only fires while nothing is selected yet or the
  // previously-selected provider disappeared (e.g. deleted), never
  // overriding a deliberate switch to a different DVR provider.
  useEffect(() => {
    if (dvrProviders.length && !dvrProviders.some((p) => p.id === recordingProfilesProviderId)) {
      setRecordingProfilesProviderId(dvrProviders[0].id)
    }
  }, [dvrProviders.map((p) => p.id).join(',')])
  const dvrUpcomingQuery = useQuery<DvrUpcomingRecording[]>({
    queryKey: ['vod-dvr-upcoming', recordingProfilesProviderId],
    queryFn:  () => api.get('/vod/dvr-upcoming/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled:  recordingProfilesProviderId != null,
  })
  const recordingProfilesQuery = useQuery<RecordingProfile[]>({
    queryKey: ['vod-dvr-recording-profiles', recordingProfilesProviderId],
    queryFn:  () => api.get('/vod/dvr-recording-profiles/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled:  recordingProfilesProviderId != null,
  })
  const blankRecordingProfileForm = {
    label: '', title: '', tvg_id: '', channel_id: '', channel_label: '',
    mode: 'all' as 'all' | 'new',
    target_movie_category_id: '', target_series_category_id: '', dispatcharr_user_id: '',
    backfill_mode: '' as '' | 'pointer' | 'download',
  }
  const dispatcharrUsersQuery = useQuery<DispatcharrUser[]>({
    queryKey: ['vod-dispatcharr-users', recordingProfilesProviderId],
    queryFn:  () => api.get('/vod/dispatcharr-users/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled:  recordingProfilesProviderId != null,
  })
  const channelProfilesQuery = useQuery<DispatcharrChannelProfile[]>({
    queryKey: ['vod-dvr-channel-profiles', recordingProfilesProviderId],
    queryFn:  () => api.get('/vod/channel-profiles/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled:  recordingProfilesProviderId != null,
  })
  const dvrUserLimitsQuery = useQuery<DvrUserLimit[]>({
    queryKey: ['vod-dvr-user-limits', recordingProfilesProviderId],
    queryFn:  () => api.get('/vod/dvr-user-limits/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled:  recordingProfilesProviderId != null,
  })
  const [dvrLimitForm, setDvrLimitForm] = useState({
    dispatcharr_user_id: '', stream_reserve: '0', disk_quota_gb: '',
    retention_max_age_days: '', retention_max_episodes_per_show: '',
  })
  const [dvrLimitError, setDvrLimitError] = useState<string | null>(null)
  const addDvrUserLimit = useMutation({
    mutationFn: () => {
      const user = dispatcharrUsersQuery.data?.find((u) => u.id === Number(dvrLimitForm.dispatcharr_user_id))
      return api.post('/vod/dvr-user-limits/', {
        provider_id: recordingProfilesProviderId,
        dispatcharr_user_id: Number(dvrLimitForm.dispatcharr_user_id),
        dispatcharr_username: user?.username ?? `user ${dvrLimitForm.dispatcharr_user_id}`,
        stream_reserve: Number(dvrLimitForm.stream_reserve) || 0,
        disk_quota_bytes: dvrLimitForm.disk_quota_gb ? Math.round(Number(dvrLimitForm.disk_quota_gb) * 1024 ** 3) : null,
        retention_max_age_days: dvrLimitForm.retention_max_age_days ? Number(dvrLimitForm.retention_max_age_days) : null,
        retention_max_episodes_per_show: dvrLimitForm.retention_max_episodes_per_show ? Number(dvrLimitForm.retention_max_episodes_per_show) : null,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-dvr-user-limits', recordingProfilesProviderId] })
      setDvrLimitForm({ dispatcharr_user_id: '', stream_reserve: '0', disk_quota_gb: '', retention_max_age_days: '', retention_max_episodes_per_show: '' })
      setDvrLimitError(null)
    },
    onError: (e: any) => setDvrLimitError(e?.response?.data?.detail ?? e.message ?? 'Save failed.'),
  })
  const deleteDvrUserLimit = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/dvr-user-limits/${id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-dvr-user-limits', recordingProfilesProviderId] }),
  })
  const updateDvrUserLimit = useMutation({
    mutationFn: (v: { id: number; stream_reserve: number; disk_quota_bytes: number | null; retention_max_age_days: number | null; retention_max_episodes_per_show: number | null }) =>
      api.post(`/vod/dvr-user-limits/${v.id}/`, {
        stream_reserve: v.stream_reserve, disk_quota_bytes: v.disk_quota_bytes,
        retention_max_age_days: v.retention_max_age_days, retention_max_episodes_per_show: v.retention_max_episodes_per_show,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-dvr-user-limits', recordingProfilesProviderId] }),
  })
  // Portal Access -- provisioning for the separate end-user DVR portal login
  // (backend/portal_routes.py, backend/portal_auth.py). Deliberately not the
  // same account/credentials as Dispatcharr or the admin login -- see
  // backend/vod_db.py's portal_accounts table comment.
  const portalAccountsQuery = useQuery<PortalAccount[]>({
    queryKey: ['vod-portal-accounts', recordingProfilesProviderId],
    queryFn:  () => api.get('/vod/portal-accounts/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled:  recordingProfilesProviderId != null,
  })
  const [portalAccountForm, setPortalAccountForm] = useState({ dispatcharr_user_id: '', username: '', password: '' })
  const [portalAccountError, setPortalAccountError] = useState<string | null>(null)
  const createPortalAccount = useMutation({
    mutationFn: () => api.post('/vod/portal-accounts/', {
      provider_id: recordingProfilesProviderId,
      dispatcharr_user_id: Number(portalAccountForm.dispatcharr_user_id),
      username: portalAccountForm.username,
      password: portalAccountForm.password,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-portal-accounts', recordingProfilesProviderId] })
      setPortalAccountForm({ dispatcharr_user_id: '', username: '', password: '' })
      setPortalAccountError(null)
    },
    onError: (e: any) => setPortalAccountError(e?.response?.data?.detail ?? e.message ?? 'Save failed.'),
  })
  const resetPortalAccountPassword = useMutation({
    mutationFn: (v: { id: number; password: string }) => api.post(`/vod/portal-accounts/${v.id}/reset-password/`, { password: v.password }),
  })
  const resetPortalAccountMfa = useMutation({
    mutationFn: (id: number) => api.post(`/vod/portal-accounts/${id}/reset-mfa/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-portal-accounts', recordingProfilesProviderId] }),
  })
  const deletePortalAccount = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/portal-accounts/${id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-portal-accounts', recordingProfilesProviderId] }),
  })
  // Retention: dry-run scan, then an explicit separate confirm step -- see
  // vod_db.find_retention_candidates/apply_retention_deletions' docstrings
  // for why this is two calls, not one (matches the Orphan Checker's own
  // scan-then-delete pattern already established in this app).
  const [retentionReviewLimitId, setRetentionReviewLimitId] = useState<number | null>(null)
  const retentionCandidatesQuery = useQuery<{ movies: RetentionCandidateMovie[]; episodes: RetentionCandidateEpisode[] }>({
    queryKey: ['vod-dvr-retention-candidates', retentionReviewLimitId],
    queryFn: () => api.get(`/vod/dvr-user-limits/${retentionReviewLimitId}/retention-candidates/`).then((r) => r.data),
    enabled: retentionReviewLimitId != null,
  })
  const applyRetention = useMutation({
    mutationFn: () => api.post(`/vod/dvr-user-limits/${retentionReviewLimitId}/apply-retention/`, {
      movies: retentionCandidatesQuery.data?.movies ?? [],
      episodes: retentionCandidatesQuery.data?.episodes ?? [],
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-dvr-user-usage'] })
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      setRetentionReviewLimitId(null)
    },
  })
  // DVR Library -- browse/delete this provider's own recorded media.
  // /vod/movies/ and /vod/series/ already support provider_id filtering
  // server-side (confirmed while scoping this), so no new backend was
  // needed -- just consuming what's already there. "Delete" removes this
  // provider's own source (DELETE .../sources/{id}/, the same operation
  // retention's apply step already uses), not a blanket row delete -- a
  // movie/episode also sourced elsewhere stays, just no longer via DVR.
  const [dvrLibraryTab, setDvrLibraryTab] = useState<'movies' | 'series'>('movies')
  const [expandedMissingSeriesId, setExpandedMissingSeriesId] = useState<number | null>(null)
  function formatFileSize(n: number): string {
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
    if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(0)} MB`
    return `${(n / 1024 ** 3).toFixed(2)} GB`
  }
  const dvrLibraryMoviesQuery = useQuery<{ items: Movie[]; total: number }>({
    queryKey: ['vod-dvr-library-movies', recordingProfilesProviderId],
    queryFn: () => api.get('/vod/movies/', { params: { provider_id: recordingProfilesProviderId, limit: 200, archived: false } }).then((r) => r.data),
    enabled: recordingProfilesProviderId != null && dvrSubTab === 'library',
  })
  const dvrLibrarySeriesQuery = useQuery<{ items: Series[]; total: number }>({
    queryKey: ['vod-dvr-library-series', recordingProfilesProviderId],
    queryFn: () => api.get('/vod/series/', { params: { provider_id: recordingProfilesProviderId, limit: 200, archived: false } }).then((r) => r.data),
    enabled: recordingProfilesProviderId != null && dvrSubTab === 'library',
  })
  const deleteDvrMovieSource = useMutation({
    mutationFn: (v: { movieId: number; sourceId: number }) => api.delete(`/vod/movies/${v.movieId}/sources/${v.sourceId}/`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-dvr-library-movies', recordingProfilesProviderId] })
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
    },
  })
  const deleteDvrEpisodeSource = useMutation({
    mutationFn: (v: { episodeId: number; sourceId: number }) => api.delete(`/vod/episodes/${v.episodeId}/sources/${v.sourceId}/`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-dvr-library-series', recordingProfilesProviderId] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
    },
  })
  // Metrics -- real numbers only, nothing simulated. Watch activity comes
  // from the watch_sessions history built by main.py's background poller
  // (see dispatcharr_dvr_importer.poll_watch_sessions) -- genuinely
  // per-person now, not the unattributed fallback originally planned,
  // since Dispatcharr's own /proxy/stats/ turned out to carry a real
  // user_id after all. Rule health is deliberately on-demand (a "Check"
  // button), not automatic -- reuses the same channel-scoped EPG search
  // the picker already uses, so it costs nothing new server-side and never
  // runs without being asked for.
  const watchSessionsQuery = useQuery<WatchSession[]>({
    queryKey: ['vod-watch-sessions'],
    queryFn: () => api.get('/vod/watch-sessions/').then((r) => r.data),
    enabled: dvrSubTab === 'metrics',
  })
  const unresolvedMissingEpisodesQuery = useQuery<UnresolvedMissingEpisode[]>({
    queryKey: ['vod-dvr-unresolved-missing-episodes'],
    queryFn: () => api.get('/vod/dvr-unresolved-missing-episodes/').then((r) => r.data),
    enabled: dvrSubTab === 'metrics',
  })
  const dvrRecordingFailuresQuery = useQuery<DvrRecordingFailure[]>({
    queryKey: ['vod-dvr-recording-failures', recordingProfilesProviderId],
    queryFn: () => api.get('/vod/dvr-recording-failures/', { params: { provider_id: recordingProfilesProviderId } }).then((r) => r.data),
    enabled: dvrSubTab === 'metrics' && recordingProfilesProviderId != null,
  })
  const [ruleHealth, setRuleHealth] = useState<Record<number, { matches: number; checking: boolean }>>({})
  const checkRuleHealth = async (rp: RecordingProfile) => {
    if (!rp.channel_id) return
    setRuleHealth((prev) => ({ ...prev, [rp.id]: { matches: prev[rp.id]?.matches ?? 0, checking: true } }))
    try {
      const res = await api.get<EpgSearchProgram[]>('/vod/epg-search/', { params: { provider_id: recordingProfilesProviderId, title: rp.title, channel_id: rp.channel_id } })
      setRuleHealth((prev) => ({ ...prev, [rp.id]: { matches: res.data.length, checking: false } }))
    } catch {
      setRuleHealth((prev) => ({ ...prev, [rp.id]: { matches: -1, checking: false } }))
    }
  }
  const dvrUserUsageQuery = useQuery<Record<number, { actual_bytes: number; virtual_bytes: number; total_bytes: number }>>({
    queryKey: ['vod-dvr-user-usage', recordingProfilesProviderId, dvrUserLimitsQuery.data?.map((l) => l.id).join(',')],
    queryFn: async () => {
      const entries = await Promise.all(
        (dvrUserLimitsQuery.data ?? []).map((lim) =>
          api.get(`/vod/dvr-user-limits/${lim.id}/usage/`).then((r) => [lim.id, {
            actual_bytes: r.data.actual_bytes, virtual_bytes: r.data.virtual_bytes, total_bytes: r.data.total_bytes,
          }] as const)
        )
      )
      return Object.fromEntries(entries)
    },
    enabled: !!dvrUserLimitsQuery.data?.length,
  })
  const [recordingProfileForm, setRecordingProfileForm] = useState(blankRecordingProfileForm)
  const [recordingProfileError, setRecordingProfileError] = useState<string | null>(null)
  const [recordingProfileResult, setRecordingProfileResult] = useState<{ scheduled_now: number; total_matches: number; skipped_conflicts: number; backfilled_now: number } | null>(null)
  const [epgSearchTitle, setEpgSearchTitle] = useState('')
  const epgSearch = useMutation({
    mutationFn: () => api.get<EpgSearchProgram[]>('/vod/epg-search/', {
      params: { provider_id: recordingProfilesProviderId, title: epgSearchTitle.trim() },
    }).then((r) => r.data),
    onError: (e: any) => setRecordingProfileError(e?.response?.data?.detail ?? e.message ?? 'Search failed.'),
  })
  const addRecordingProfile = useMutation({
    mutationFn: () => api.post('/vod/dvr-recording-profiles/', {
      provider_id: recordingProfilesProviderId,
      label: recordingProfileForm.label.trim() || recordingProfileForm.title,
      title: recordingProfileForm.title,
      tvg_id: recordingProfileForm.tvg_id || null,
      mode: recordingProfileForm.mode,
      channel_id: recordingProfileForm.channel_id ? Number(recordingProfileForm.channel_id) : null,
      target_movie_category_id: recordingProfileForm.target_movie_category_id ? Number(recordingProfileForm.target_movie_category_id) : null,
      target_series_category_id: recordingProfileForm.target_series_category_id ? Number(recordingProfileForm.target_series_category_id) : null,
      dispatcharr_user_id: recordingProfileForm.dispatcharr_user_id ? Number(recordingProfileForm.dispatcharr_user_id) : null,
      backfill_mode: recordingProfileForm.backfill_mode || null,
    }).then((r) => r.data),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['vod-dvr-recording-profiles', recordingProfilesProviderId] })
      qc.invalidateQueries({ queryKey: ['vod-dvr-upcoming', recordingProfilesProviderId] })
      setRecordingProfileForm(blankRecordingProfileForm)
      setEpgSearchTitle('')
      epgSearch.reset()
      setRecordingProfileError(null)
      setRecordingProfileResult({ scheduled_now: data.scheduled_now ?? 0, total_matches: data.total_matches ?? 0, skipped_conflicts: data.skipped_conflicts ?? 0, backfilled_now: data.backfilled_now ?? 0 })
    },
    onError: (e: any) => { setRecordingProfileError(e?.response?.data?.detail ?? e.message ?? 'Save failed.'); setRecordingProfileResult(null) },
  })
  // Real Dispatcharr Channel Profiles, cross-referenced against the selected
  // person's own membership -- a person doesn't always have the full channel
  // lineup (confirmed live: a real profile with 2395 total memberships but
  // only 81 enabled), so the EPG picker below marks channels outside their
  // lineup rather than hiding them (an admin may deliberately want to record
  // something outside a person's normal channels).
  const selectedDispatcharrUser = dispatcharrUsersQuery.data?.find((u) => String(u.id) === recordingProfileForm.dispatcharr_user_id)
  const visibleChannelIds = selectedDispatcharrUser
    ? new Set(
        (channelProfilesQuery.data ?? [])
          .filter((cp) => selectedDispatcharrUser.channel_profiles?.includes(cp.id))
          .flatMap((cp) => cp.channels),
      )
    : null
  const epgChannelGroups = (() => {
    const byChannel = new Map<number, { channel: EpgSearchProgram['channels'][number]; programs: EpgSearchProgram[] }>()
    for (const program of epgSearch.data ?? []) {
      for (const ch of program.channels ?? []) {
        if (!byChannel.has(ch.id)) byChannel.set(ch.id, { channel: ch, programs: [] })
        byChannel.get(ch.id)!.programs.push(program)
      }
    }
    return [...byChannel.values()].sort((a, b) => (a.channel.channel_number ?? 0) - (b.channel.channel_number ?? 0))
  })()
  const pickEpgChannel = (channel: EpgSearchProgram['channels'][number], programs: EpgSearchProgram[]) => {
    const first = programs[0]
    setRecordingProfileForm({
      ...recordingProfileForm,
      title: first.title,
      tvg_id: first.tvg_id,
      channel_id: String(channel.id),
      channel_label: `${channel.channel_number ?? '?'} · ${channel.name}`,
    })
    setEpgSearchTitle('')
    epgSearch.reset()
  }
  // Day-grouped agenda for the DVR tab's Upcoming Recordings section --
  // chosen over a full calendar-grid widget: answers "what's coming up and
  // when" without month navigation or click-to-expand cells, and needs no
  // new charting/calendar library.
  const upcomingByDay = (() => {
    const groups = new Map<string, DvrUpcomingRecording[]>()
    const sorted = [...(dvrUpcomingQuery.data ?? [])].sort((a, b) => a.start_time.localeCompare(b.start_time))
    for (const r of sorted) {
      const day = new Date(r.start_time).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
      if (!groups.has(day)) groups.set(day, [])
      groups.get(day)!.push(r)
    }
    return [...groups.entries()]
  })()
  const deleteRecordingProfile = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/dvr-recording-profiles/${id}/`),
    onSuccess:  () => {
      qc.invalidateQueries({ queryKey: ['vod-dvr-recording-profiles', recordingProfilesProviderId] })
      qc.invalidateQueries({ queryKey: ['vod-dvr-upcoming', recordingProfilesProviderId] })
    },
  })
  const setRecordingProfileMonitored = useMutation({
    mutationFn: (v: { id: number; monitored: boolean }) => api.post(`/vod/dvr-recording-profiles/${v.id}/monitored/`, null, { params: { monitored: v.monitored } }),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-dvr-recording-profiles', recordingProfilesProviderId] }),
  })
  const [expandedLiveAccountsProviderId, setExpandedLiveAccountsProviderId] = useState<number | null>(null)
  const providerLiveAccountsQuery = useQuery<ProviderLiveAccount[]>({
    queryKey: ['vod-provider-live-accounts', expandedLiveAccountsProviderId],
    queryFn:  () => api.get(`/vod/providers/${expandedLiveAccountsProviderId}/live-accounts/`).then((r) => r.data),
    enabled:  expandedLiveAccountsProviderId != null,
  })
  const [newLiveAccountConnId, setNewLiveAccountConnId] = useState('')
  const [newLiveAccountAcctId, setNewLiveAccountAcctId] = useState('')
  const setProviderLiveAccount = useMutation({
    mutationFn: ({ providerId, connectionId, accountId }: { providerId: number; connectionId: number; accountId: number }) =>
      api.post(`/vod/providers/${providerId}/live-accounts/`, { dispatcharr_connection_id: connectionId, dispatcharr_account_id: accountId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-provider-live-accounts'] })
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
      setNewLiveAccountConnId(''); setNewLiveAccountAcctId('')
    },
  })
  const removeProviderLiveAccount = useMutation({
    mutationFn: (linkId: number) => api.delete(`/vod/providers/live-accounts/${linkId}/`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-provider-live-accounts'] })
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
    },
  })
  const toggleProviderActive = useMutation({
    mutationFn: ({ id, active }: { id: number; active: boolean }) =>
      api.post(`/vod/providers/${id}/${active ? 'activate' : 'deactivate'}/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const deleteProvider = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/providers/${id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
  })
  const [importingId, setImportingId] = useState<number | null>(null)
  const [importResult, setImportResult] = useState<string | null>(null)
  const importCatalog = useMutation({
    mutationFn: (id: number) => { setImportingId(id); return api.post(`/vod/providers/${id}/import/`, null, { timeout: 180_000 }) },
    onSuccess: (r) => {
      const archived = (r.data.movies_archived ?? 0) + (r.data.series_archived ?? 0)
      const unarchived = (r.data.movies_unarchived ?? 0) + (r.data.series_unarchived ?? 0)
      setImportResult(
        `Imported: ${r.data.movies_created} new movies (${r.data.movies_matched} already known), ${r.data.series_created} new series (${r.data.series_matched} already known)`
        + (archived || unarchived ? ` — ${archived} archived, ${unarchived} restored by the current exclusion rules.` : '.')
      )
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
    },
    onError: (e: any) => setImportResult(`Import failed: ${e?.response?.data?.detail ?? e.message}`),
    onSettled: () => setImportingId(null),
  })

  // ── Categories ──
  const categoriesQuery = useQuery<Category[]>({
    queryKey: ['vod-categories'],
    queryFn:  () => api.get('/vod/categories/').then((r) => r.data),
  })
  // ── TMDB Lists (a list can hold both movies and shows; Dispatcharr keeps those
  // catalogs separate, so each list gets a paired movie + series category) ──
  const TMDB_TOKEN = '%'
  const buildTmdbPairName = (template: string, label: string) =>
    template.includes(TMDB_TOKEN) ? template.split(TMDB_TOKEN).join(label) : `${template} — ${label}`
  const [tmdbListForm, setTmdbListForm] = useState({ list_id: '', name_template: '', movie_label: 'Movies', tv_label: 'TV Shows' })
  const addTmdbList = useMutation({
    mutationFn: async () => {
      const syncSource = `tmdb_list:${tmdbListForm.list_id.trim()}`
      await api.post('/vod/categories/', { name: buildTmdbPairName(tmdbListForm.name_template, tmdbListForm.movie_label), content_type: 'movie', is_smart: false, sync_source: syncSource })
      await api.post('/vod/categories/', { name: buildTmdbPairName(tmdbListForm.name_template, tmdbListForm.tv_label), content_type: 'series', is_smart: false, sync_source: syncSource })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
      setTmdbListForm({ list_id: '', name_template: '', movie_label: 'Movies', tv_label: 'TV Shows' })
    },
  })
  const deleteCategory = useMutation({
    mutationFn: (id: number) => api.delete(`/vod/categories/${id}/`),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const setCategorySortOrder = useMutation({
    mutationFn: ({ id, sort_order }: { id: number; sort_order: number }) =>
      api.post(`/vod/categories/${id}/sort-order/`, null, { params: { sort_order } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const renameCategory = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api.post(`/vod/categories/${id}/name/`, null, { params: { name } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const [categoryActiveError2, setCategoryActiveError2] = useState<string | null>(null)
  const setCategoryActive2 = useMutation({
    mutationFn: ({ id, is_active }: { id: number; is_active: boolean }) =>
      api.post(`/vod/categories/${id}/active/`, null, { params: { is_active } }),
    onSuccess: () => {
      setCategoryActiveError2(null)
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
    onError: (e: any) => setCategoryActiveError2(e?.response?.data?.detail ?? e.message),
  })
  const [categoryScheduleError2, setCategoryScheduleError2] = useState<string | null>(null)
  const setCategorySchedule2 = useMutation({
    mutationFn: ({ id, start_mmdd, end_mmdd }: { id: number; start_mmdd: string | null; end_mmdd: string | null }) =>
      api.post(`/vod/categories/${id}/schedule/`, null, { params: { start_mmdd: start_mmdd ?? undefined, end_mmdd: end_mmdd ?? undefined } }),
    onSuccess: () => {
      setCategoryScheduleError2(null)
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
    },
    onError: (e: any) => setCategoryScheduleError2(e?.response?.data?.detail ?? e.message),
  })
  function promptSchedule2(c: Category) {
    const start = window.prompt(
      `Annual auto-enable date for "${c.name}" (MM-DD, e.g. 10-01 for Oct 1). Leave blank to clear the schedule entirely.`,
      c.schedule_start_mmdd ?? '',
    )
    if (start === null) return
    if (!start.trim()) {
      setCategorySchedule2.mutate({ id: c.id, start_mmdd: null, end_mmdd: null })
      return
    }
    const end = window.prompt(
      `Annual auto-disable date for "${c.name}" (MM-DD, e.g. 11-01 for Nov 1):`,
      c.schedule_end_mmdd ?? '',
    )
    if (end === null) return
    setCategorySchedule2.mutate({ id: c.id, start_mmdd: start.trim(), end_mmdd: end.trim() })
  }
  const setCategorySyncSource = useMutation({
    mutationFn: ({ id, sync_source }: { id: number; sync_source: string | null }) =>
      api.post(`/vod/categories/${id}/sync-source/`, null, { params: sync_source ? { sync_source } : {} }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-categories'] }),
  })
  const [tmdbSyncResult, setTmdbSyncResult] = useState<string | null>(null)
  const syncCategoryNow = useMutation({
    mutationFn: (id: number) => api.post(`/vod/categories/${id}/sync-now/`),
    onSuccess: (r) => {
      setTmdbSyncResult(`List had ${r.data.list_total}: ${r.data.found_in_pool} in pool (${r.data.newly_placed} newly placed), ${r.data.not_in_pool} not in pool.`)
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
    onError: (e: any) => setTmdbSyncResult(`Sync failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  // ── Year review (ambiguous no-year duplicates held out of categories) ──
  const needsReviewQuery = useQuery<NeedsReviewData>({
    queryKey: ['vod-needs-review'],
    queryFn:  () => api.get('/vod/needs-review/').then((r) => r.data),
  })

  // ── Missing artwork counts (badge only -- the modal paginates its own list) ──
  const missingArtworkCountsQuery = useQuery<{ movies: number; series: number }>({
    queryKey: ['vod-missing-artwork-counts'],
    queryFn: async () => {
      const [movies, series] = await Promise.all([
        api.get('/vod/missing-artwork/', { params: { content_type: 'movie', limit: 1 } }).then((r) => r.data.total),
        api.get('/vod/missing-artwork/', { params: { content_type: 'series', limit: 1 } }).then((r) => r.data.total),
      ])
      return { movies, series }
    },
  })

  // ── Orphan checker (dead rows a provider deletion, or a bug, can leave behind) ──
  const orphansQuery = useQuery<OrphanReport>({
    queryKey: ['vod-orphans'],
    queryFn:  () => api.get('/vod/orphans/').then((r) => r.data),
    enabled:  false,  // scan on demand only -- this walks the whole pool, not something to run on every page load
  })
  const purgeOrphans = useMutation({
    mutationFn: () => api.post('/vod/orphans/purge/'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-orphans'] })
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      qc.invalidateQueries({ queryKey: ['vod-providers'] })
    },
  })

  // ── Duplicate finder (punctuation variants + adjacent-year mislabeling) ──
  const [duplicatesContentType, setDuplicatesContentType] = useState<'movie' | 'series'>('movie')
  const [duplicatesOffset, setDuplicatesOffset] = useState(0)
  const DUPLICATES_PAGE_SIZE = 20
  const duplicatesQuery = useQuery<DuplicateGroup[]>({
    queryKey: ['vod-duplicates', duplicatesContentType],
    queryFn:  () => api.get('/vod/duplicates/', { params: { content_type: duplicatesContentType } }).then((r) => r.data),
    enabled:  false,  // scan on demand only -- this walks the whole pool, not something to run on every page load
  })
  const [duplicatesMergeResult, setDuplicatesMergeResult] = useState<string | null>(null)
  const mergeDuplicateGroup = useMutation({
    mutationFn: (body: { keep_id: number; merge_ids: number[] }) =>
      api.post('/vod/duplicates/merge/', { content_type: duplicatesContentType, ...body }),
    onSuccess: () => {
      setDuplicatesMergeResult(null)
      // duplicatesQuery is enabled:false (on-demand scan only) -- invalidateQueries
      // alone won't refetch a disabled query, so the merged group would keep
      // showing (now stale/wrong) until the next manual Scan without this.
      duplicatesQuery.refetch()
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
    onError: (e: any) => setDuplicatesMergeResult(`Merge failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  const ignoreDuplicateGroup = useMutation({
    mutationFn: (item_ids: number[]) => api.post('/vod/duplicates/ignore/', { content_type: duplicatesContentType, item_ids }),
    onSuccess: () => duplicatesQuery.refetch(),
  })
  const groupSignature = (items: { id: number }[]) => items.map((i) => i.id).sort((a, b) => a - b).join('-')

  // TMDB-confirmed-match check: a real catalog scan can surface thousands
  // of candidate tmdb_ids (one real GET per id, no bulk endpoint exists),
  // far too slow/rate-limit-risky to check inline -- runs as a polled
  // background job instead (see duplicate_confirm.py). Opt-in, separate
  // from Scan -- not everyone wants to wait minutes for this every time.
  const [duplicatesConfirmJobId, setDuplicatesConfirmJobId] = useState<string | null>(null)
  const startConfirmScan = useMutation({
    mutationFn: () => api.post('/vod/duplicates/confirm-scan/', null, { params: { content_type: duplicatesContentType } }),
    onSuccess: (r) => setDuplicatesConfirmJobId(r.data.job_id),
  })
  const confirmScanQuery = useQuery<{ status: string; checked: number; total: number; confirmed: { keep_id: number; merge_ids: number[]; matched_title: string; tmdb_id: string }[]; error: string | null }>({
    queryKey: ['vod-duplicates-confirm-scan', duplicatesConfirmJobId],
    queryFn:  () => api.get(`/vod/duplicates/confirm-scan/${duplicatesConfirmJobId}/`).then((r) => r.data),
    enabled:  !!duplicatesConfirmJobId,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1200 : false),
  })
  const cancelConfirmScan = useMutation({
    mutationFn: () => api.post(`/vod/duplicates/confirm-scan/${duplicatesConfirmJobId}/cancel/`),
  })
  const duplicatesConfirmed = confirmScanQuery.data?.status === 'done' ? confirmScanQuery.data.confirmed : []
  const duplicatesConfirmedKeys = new Set(duplicatesConfirmed.map((c) => groupSignature([{ id: c.keep_id }, ...c.merge_ids.map((id) => ({ id }))])))
  const duplicatesNeedsReview = (duplicatesQuery.data ?? []).filter((g) => !duplicatesConfirmedKeys.has(groupSignature(g.items)))

  const [duplicatesConfirmMergeResult, setDuplicatesConfirmMergeResult] = useState<string | null>(null)
  const mergeConfirmedDuplicates = useMutation({
    mutationFn: () => api.post('/vod/duplicates/merge-confirmed/', {
      content_type: duplicatesContentType,
      groups: duplicatesConfirmed.map((c) => ({ keep_id: c.keep_id, merge_ids: c.merge_ids })),
    }),
    onSuccess: (r) => {
      setDuplicatesConfirmMergeResult(`Merged ${r.data.merged_groups} confirmed group${r.data.merged_groups === 1 ? '' : 's'} (${r.data.merged_items} item${r.data.merged_items === 1 ? '' : 's'}).`)
      setDuplicatesConfirmJobId(null)
      duplicatesQuery.refetch()
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
    onError: (e: any) => setDuplicatesConfirmMergeResult(`Merge failed: ${e?.response?.data?.detail ?? e.message}`),
  })

  // Scoped to just the tmdb_ids actually visible on the current results page
  // (post-scan, post-pagination, post-confirmed-filter), not the whole scan
  // -- keeps this to a handful of real TMDB requests instead of one per
  // group in the pool.
  const duplicatesPageItems = duplicatesNeedsReview.slice(duplicatesOffset, duplicatesOffset + DUPLICATES_PAGE_SIZE)
  const duplicatesTmdbIdsKey = [...new Set(
    duplicatesPageItems.flatMap((g) => g.items.map((i) => i.tmdb_id).filter((id): id is string => !!id))
  )].sort().join(',')
  const duplicatesTmdbDetailsQuery = useQuery<Record<string, { year: number | null; title: string | null }>>({
    queryKey: ['vod-duplicates-tmdb-details', duplicatesContentType, duplicatesTmdbIdsKey],
    queryFn:  () => api.get('/vod/duplicates/tmdb-details/', { params: { content_type: duplicatesContentType, tmdb_ids: duplicatesTmdbIdsKey } }).then((r) => r.data),
    enabled:  !!duplicatesTmdbIdsKey,
  })

  // ── Movies ──
  const [movieSearch, setMovieSearch] = useState('')
  const [movieOffset, setMovieOffset] = useState(0)
  const [movieCategoryFilter, setMovieCategoryFilter] = useState<number | null>(null)
  const [movieProviderFilter, setMovieProviderFilter] = useState<number | null>(null)
  const [movieShowArchived, setMovieShowArchived] = useState(false)
  const [MOVIE_LIMIT, setMovieLimitState] = useState(
    () => Number(localStorage.getItem('vodmanager-movies-limit')) || 25
  )
  function setMovieLimit(n: number) {
    localStorage.setItem('vodmanager-movies-limit', String(n))
    setMovieLimitState(n)
    setMovieOffset(0)
  }
  const moviesQuery = useQuery<Page<Movie>>({
    queryKey: ['vod-movies', movieSearch, movieOffset, movieCategoryFilter, movieProviderFilter, movieShowArchived, MOVIE_LIMIT],
    queryFn:  () => api.get('/vod/movies/', { params: { search: movieSearch || undefined, limit: MOVIE_LIMIT, offset: movieOffset, category_id: movieCategoryFilter ?? undefined, provider_id: movieProviderFilter ?? undefined, archived: movieShowArchived } }).then((r) => r.data),
  })
  const toggleMovieArchived = useMutation({
    mutationFn: ({ id, archived }: { id: number; archived: boolean }) => api.post(`/vod/movies/${id}/archive/`, null, { params: { archived } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-movies'] }),
  })
  const [movieForm, setMovieForm] = useState({ name: '', year: '' })
  const addMovie = useMutation({
    mutationFn: () => api.post('/vod/movies/', { name: movieForm.name, year: movieForm.year ? Number(movieForm.year) : undefined }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      setMovieForm({ name: '', year: '' })
    },
  })
  const [selectedMovieIds, setSelectedMovieIds] = useState<Set<number>>(new Set())
  const [movieLastClickedIndex, setMovieLastClickedIndex] = useState<number | null>(null)
  const toggleMovieSelected = (id: number, index: number, shiftKey: boolean) => {
    setSelectedMovieIds((prev) => {
      const next = new Set(prev)
      const willBeChecked = !prev.has(id)
      if (shiftKey && movieLastClickedIndex != null) {
        const pageIds = moviesQuery.data?.items.map((m) => m.id) ?? []
        const [start, end] = [movieLastClickedIndex, index].sort((a, b) => a - b)
        for (let j = start; j <= end; j++) {
          if (willBeChecked) next.add(pageIds[j]); else next.delete(pageIds[j])
        }
      } else {
        if (willBeChecked) next.add(id); else next.delete(id)
      }
      return next
    })
    setMovieLastClickedIndex(index)
  }
  const [bulkMovieTargetCategory, setBulkMovieTargetCategory] = useState('')
  const [bulkMovieResult, setBulkMovieResult] = useState<string | null>(null)
  const bulkPlaceMovies = useMutation({
    mutationFn: (body: { category_id: number; ids?: number[]; search?: string; source_category_id?: number; source_provider_id?: number }) =>
      api.post('/vod/movies/bulk-place/', body),
    onSuccess: (r) => {
      setBulkMovieResult(`Matched ${r.data.matched} · newly placed ${r.data.newly_placed}.`)
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      setSelectedMovieIds(new Set())
    },
    onError: (e: any) => setBulkMovieResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  const bulkArchiveMovies = useMutation({
    mutationFn: (body: { ids: number[]; archived: boolean }) => api.post('/vod/bulk-archive/', { content_type: 'movie', ...body }),
    onSuccess: (r) => {
      setBulkMovieResult(`${movieShowArchived ? 'Un-archived' : 'Archived'} ${r.data.changed}.`)
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      setSelectedMovieIds(new Set())
    },
    onError: (e: any) => setBulkMovieResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })

  // ── Series ──
  const [seriesSearch, setSeriesSearch] = useState('')
  const [seriesOffset, setSeriesOffset] = useState(0)
  const [seriesCategoryFilter, setSeriesCategoryFilter] = useState<number | null>(null)
  const [seriesProviderFilter, setSeriesProviderFilter] = useState<number | null>(null)
  const [seriesShowArchived, setSeriesShowArchived] = useState(false)
  const [SERIES_LIMIT, setSeriesLimitState] = useState(
    () => Number(localStorage.getItem('vodmanager-series-limit')) || 25
  )
  function setSeriesLimit(n: number) {
    localStorage.setItem('vodmanager-series-limit', String(n))
    setSeriesLimitState(n)
    setSeriesOffset(0)
  }
  const seriesQuery = useQuery<Page<Series>>({
    queryKey: ['vod-series', seriesSearch, seriesOffset, seriesCategoryFilter, seriesProviderFilter, seriesShowArchived, SERIES_LIMIT],
    queryFn:  () => api.get('/vod/series/', { params: { search: seriesSearch || undefined, limit: SERIES_LIMIT, offset: seriesOffset, category_id: seriesCategoryFilter ?? undefined, provider_id: seriesProviderFilter ?? undefined, archived: seriesShowArchived } }).then((r) => r.data),
  })
  const toggleSeriesArchived = useMutation({
    mutationFn: ({ id, archived }: { id: number; archived: boolean }) => api.post(`/vod/series/${id}/archive/`, null, { params: { archived } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-series'] }),
  })
  const [seriesForm, setSeriesForm] = useState({ name: '', year: '' })
  const addSeries = useMutation({
    mutationFn: () => api.post('/vod/series/', { name: seriesForm.name, year: seriesForm.year ? Number(seriesForm.year) : undefined }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      setSeriesForm({ name: '', year: '' })
    },
  })
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<Set<number>>(new Set())
  const [seriesLastClickedIndex, setSeriesLastClickedIndex] = useState<number | null>(null)
  const toggleSeriesSelected = (id: number, index: number, shiftKey: boolean) => {
    setSelectedSeriesIds((prev) => {
      const next = new Set(prev)
      const willBeChecked = !prev.has(id)
      if (shiftKey && seriesLastClickedIndex != null) {
        const pageIds = seriesQuery.data?.items.map((s) => s.id) ?? []
        const [start, end] = [seriesLastClickedIndex, index].sort((a, b) => a - b)
        for (let j = start; j <= end; j++) {
          if (willBeChecked) next.add(pageIds[j]); else next.delete(pageIds[j])
        }
      } else {
        if (willBeChecked) next.add(id); else next.delete(id)
      }
      return next
    })
    setSeriesLastClickedIndex(index)
  }
  const [bulkSeriesTargetCategory, setBulkSeriesTargetCategory] = useState('')
  const [bulkSeriesResult, setBulkSeriesResult] = useState<string | null>(null)
  const bulkPlaceSeries = useMutation({
    mutationFn: (body: { category_id: number; ids?: number[]; search?: string; source_category_id?: number; source_provider_id?: number }) =>
      api.post('/vod/series/bulk-place/', body),
    onSuccess: (r) => {
      setBulkSeriesResult(`Matched ${r.data.matched} · newly placed ${r.data.newly_placed}.`)
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      setSelectedSeriesIds(new Set())
    },
    onError: (e: any) => setBulkSeriesResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })
  const bulkArchiveSeries = useMutation({
    mutationFn: (body: { ids: number[]; archived: boolean }) => api.post('/vod/bulk-archive/', { content_type: 'series', ...body }),
    onSuccess: (r) => {
      setBulkSeriesResult(`${seriesShowArchived ? 'Un-archived' : 'Archived'} ${r.data.changed}.`)
      qc.invalidateQueries({ queryKey: ['vod-series'] })
      setSelectedSeriesIds(new Set())
    },
    onError: (e: any) => setBulkSeriesResult(`Failed: ${e?.response?.data?.detail ?? e.message}`),
  })

  const movieCategories  = categoriesQuery.data?.filter((c) => c.content_type === 'movie')  ?? []
  const seriesCategories = categoriesQuery.data?.filter((c) => c.content_type === 'series') ?? []
  const tmdbGroups = Object.values(
    (categoriesQuery.data ?? []).filter((c) => !!c.sync_source).reduce((acc, c) => {
      const key = c.sync_source as string
      if (!acc[key]) acc[key] = { sync_source: key, categories: [] as Category[] }
      acc[key].categories.push(c)
      return acc
    }, {} as Record<string, { sync_source: string; categories: Category[] }>)
  )

  return (
    <div className="space-y-4 max-w-5xl xl:max-w-6xl 2xl:max-w-7xl mx-auto">
      {defaultCategoriesPromptQuery.data?.show && (
        <Modal onClose={() => answerDefaultCategoriesPrompt.mutate(false)} maxWidth="max-w-md">
          <div className="p-5 space-y-3">
            <h2 className="text-base font-semibold">Include 18+ content in the default categories?</h2>
            <p className="text-sm text-muted-foreground">
              VOD Manager just created two starting categories — "All Movies" and "All TV Shows" — so Dispatcharr has
              something to sync against right away. By default they exclude 18+ titles. You can change this later,
              per category, in Manage Categories.
            </p>
            <div className="flex justify-end gap-2 pt-2">
              <Button
                size="sm"
                variant="outline"
                disabled={answerDefaultCategoriesPrompt.isPending}
                onClick={() => answerDefaultCategoriesPrompt.mutate(false)}
              >
                Keep excluded
              </Button>
              <Button
                size="sm"
                disabled={answerDefaultCategoriesPrompt.isPending}
                onClick={() => answerDefaultCategoriesPrompt.mutate(true)}
              >
                {answerDefaultCategoriesPrompt.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Include 18+ content'}
              </Button>
            </div>
          </div>
        </Modal>
      )}
      <SectionCard title="Activity" icon={<Play size={14} />}>
        {!activityQuery.data?.length && <p className="text-xs text-muted-foreground">Nothing playing right now.</p>}
        {!!activityQuery.data?.length && (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-muted-foreground text-left">
                <th className="pb-1 font-normal">Title</th>
                <th className="pb-1 font-normal">Provider</th>
                <th className="pb-1 font-normal">Elapsed</th>
                <th className="pb-1 font-normal">Progress</th>
                <th className="pb-1 font-normal"></th>
              </tr>
            </thead>
            <tbody>
              {activityQuery.data.map((s) => {
                const playedBytes = s.range_start_byte + s.bytes_sent
                const pct = s.total_bytes ? Math.min(100, Math.round((playedBytes / s.total_bytes) * 100)) : null
                return (
                  <tr key={s.conn_id} className="border-t border-border/50">
                    <td className="py-1 pr-2">{s.title} <span className="text-muted-foreground">({s.kind})</span></td>
                    <td className="py-1 pr-2 text-muted-foreground">
                      {s.provider_name}{s.provider_type !== 'xc' && ` (${PROVIDER_TYPE_LABELS[s.provider_type]})`}
                    </td>
                    <td className="py-1 pr-2 text-muted-foreground">{formatElapsed(s.started_at)}</td>
                    <td className="py-1 pr-2 text-muted-foreground">{pct != null ? `${pct}%` : '—'}</td>
                    <td className="py-1">
                      <button
                        title="Force-close this stream"
                        className="text-muted-foreground hover:text-destructive"
                        disabled={killSession.isPending}
                        onClick={() => killSession.mutate(s.conn_id)}
                      >
                        <X size={12} />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </SectionCard>

      {activeTab === 'config' && (
      <>
      <SectionCard title="API Keys" icon={<CheckCircle2 size={14} />}>
        <p className="text-xs text-muted-foreground">
          TMDB API key — used to sync categories from public TMDB Lists (see Categories below).
        </p>
        <div className="flex items-center gap-1.5">
          <input
            className={inputCls()}
            type="password"
            placeholder={tmdbSettingsQuery.data?.has_api_key ? '••••••••••••••••' : 'TMDB API Key (v3 auth)'}
            value={tmdbApiKeyInput}
            onChange={(e) => setTmdbApiKeyInput(e.target.value)}
          />
          <Button size="sm" disabled={!tmdbApiKeyInput || saveTmdbApiKey.isPending} onClick={() => saveTmdbApiKey.mutate()}>
            {saveTmdbApiKey.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
          </Button>
          {tmdbSettingsQuery.data?.has_api_key && (
            <span className="text-xs text-muted-foreground flex items-center gap-1"><CheckCircle2 size={12} /> configured</span>
          )}
        </div>
        <p className="text-xs text-muted-foreground pt-2">
          AI provider — powers AI-assisted smart category suggestions, Needs Review disambiguation, and Missing
          Artwork matching (see Categories, Needs Year Review, and Missing Artwork below). Configure a key for
          any of these you have access to, then pick which one is active.
        </p>
        <div className="flex items-center gap-1.5 flex-wrap">
          {(['anthropic', 'openai', 'gemini'] as const).map((p) => (
            <Button
              key={p}
              size="sm"
              variant={aiSettingsQuery.data?.provider === p ? 'default' : 'outline'}
              disabled={saveAiProvider.isPending}
              onClick={() => saveAiProvider.mutate({ provider: p })}
            >
              {p === 'anthropic' ? 'Anthropic' : p === 'openai' ? 'OpenAI' : 'Gemini'}
            </Button>
          ))}
          <span className="text-xs text-muted-foreground">— active provider</span>
        </div>
        {aiSettingsQuery.data && (
          <div className="flex items-center gap-1.5">
            <select
              className={inputCls() + ' flex-1'}
              value={aiModelInput || aiSettingsQuery.data.model || AI_PROVIDER_DEFAULT_MODELS[aiSettingsQuery.data.provider]}
              onChange={(e) => setAiModelInput(e.target.value)}
            >
              {AI_PROVIDER_MODEL_OPTIONS[aiSettingsQuery.data.provider].map((m) => (
                <option key={m.id} value={m.id}>{m.label}</option>
              ))}
            </select>
            <Button
              size="sm"
              disabled={saveAiProvider.isPending}
              onClick={() => saveAiProvider.mutate({ provider: aiSettingsQuery.data!.provider, model: aiModelInput || undefined })}
            >
              {saveAiProvider.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Set Model'}
            </Button>
          </div>
        )}
        {([
          { key: 'anthropic' as const, label: 'Anthropic API Key', has: aiSettingsQuery.data?.has_anthropic_key },
          { key: 'openai' as const, label: 'OpenAI API Key', has: aiSettingsQuery.data?.has_openai_key },
          { key: 'gemini' as const, label: 'Google Gemini API Key', has: aiSettingsQuery.data?.has_gemini_key },
        ]).map(({ key, label, has }) => (
          <div key={key} className="flex items-center gap-1.5">
            <input
              className={inputCls()}
              type="password"
              placeholder={has ? '••••••••••••••••' : label}
              value={aiKeyInputs[key]}
              onChange={(e) => setAiKeyInputs((prev) => ({ ...prev, [key]: e.target.value }))}
            />
            <Button size="sm" disabled={!aiKeyInputs[key] || saveAiKey.isPending} onClick={() => saveAiKey.mutate(key)}>
              {saveAiKey.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
            </Button>
            {has && (
              <span className="text-xs text-muted-foreground flex items-center gap-1"><CheckCircle2 size={12} /> configured</span>
            )}
          </div>
        ))}
      </SectionCard>

      <SectionCard title="Security" icon={<ShieldCheck size={14} />}>
        <p className="text-xs text-muted-foreground">
          Per-IP lockout on the XC login (below Connected Instances) — repeated failed attempts from one
          address get temporarily locked out. Changes apply within ~30s (cached, not re-read on every request).
        </p>
        <div className="flex items-center gap-3 flex-wrap">
          <label className="flex items-center gap-1.5 text-xs">
            Max failed attempts
            <input
              className={inputCls('w-16')}
              type="number"
              min={1}
              value={lockoutValues?.lockout_max_attempts ?? ''}
              onChange={(e) => setLockoutForm({ ...(lockoutValues as LockoutSettings), lockout_max_attempts: Number(e.target.value) })}
            />
          </label>
          <label className="flex items-center gap-1.5 text-xs">
            Window (seconds)
            <input
              className={inputCls('w-20')}
              type="number"
              min={1}
              value={lockoutValues?.lockout_window_seconds ?? ''}
              onChange={(e) => setLockoutForm({ ...(lockoutValues as LockoutSettings), lockout_window_seconds: Number(e.target.value) })}
            />
          </label>
          <label className="flex items-center gap-1.5 text-xs">
            Lockout duration (seconds)
            <input
              className={inputCls('w-20')}
              type="number"
              min={1}
              value={lockoutValues?.lockout_duration_seconds ?? ''}
              onChange={(e) => setLockoutForm({ ...(lockoutValues as LockoutSettings), lockout_duration_seconds: Number(e.target.value) })}
            />
          </label>
          <Button
            size="sm"
            disabled={!lockoutForm || saveLockoutSettings.isPending}
            onClick={() => saveLockoutSettings.mutate()}
          >
            {saveLockoutSettings.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
          </Button>
          {lockoutForm && (
            <Button size="sm" variant="outline" onClick={() => setLockoutForm(null)}>Cancel</Button>
          )}
        </div>
      </SectionCard>

      <SectionCard title="Refresh Schedule" icon={<RefreshCw size={14} />}>
        <p className="text-xs text-muted-foreground">
          How often each provider type's catalog gets automatically re-imported, how long enrichment (posters,
          cast, genre) is cached before refetching, and how often TMDB Lists auto-sync. Plex/Emby libraries can
          take much longer to scan than a cheap XC catalog pull, so each provider type has its own interval.
        </p>
        {refreshValues && (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2">
            <label className="flex items-center gap-1.5 text-xs">
              XC refresh (hrs)
              <input
                className={inputCls('w-16')}
                type="number" min={0.02} step="0.5"
                value={refreshValues.catalog_refresh_hours_xc}
                onChange={(e) => setRefreshForm({ ...refreshValues, catalog_refresh_hours_xc: e.target.value })}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs">
              Plex refresh (hrs)
              <input
                className={inputCls('w-16')}
                type="number" min={0.02} step="0.5"
                value={refreshValues.catalog_refresh_hours_plex}
                onChange={(e) => setRefreshForm({ ...refreshValues, catalog_refresh_hours_plex: e.target.value })}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs">
              Emby refresh (hrs)
              <input
                className={inputCls('w-16')}
                type="number" min={0.02} step="0.5"
                value={refreshValues.catalog_refresh_hours_emby}
                onChange={(e) => setRefreshForm({ ...refreshValues, catalog_refresh_hours_emby: e.target.value })}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs">
              Jellyfin refresh (hrs)
              <input
                className={inputCls('w-16')}
                type="number" min={0.02} step="0.5"
                value={refreshValues.catalog_refresh_hours_jellyfin}
                onChange={(e) => setRefreshForm({ ...refreshValues, catalog_refresh_hours_jellyfin: e.target.value })}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs">
              Enrichment TTL (hrs)
              <input
                className={inputCls('w-16')}
                type="number" min={0.02} step="1"
                value={refreshValues.enrichment_ttl_hours}
                onChange={(e) => setRefreshForm({ ...refreshValues, enrichment_ttl_hours: e.target.value })}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs">
              TMDB Lists sync (hrs)
              <input
                className={inputCls('w-16')}
                type="number" min={0.02} step="1"
                placeholder="off"
                value={refreshValues.tmdb_sync_hours}
                onChange={(e) => setRefreshForm({ ...refreshValues, tmdb_sync_hours: e.target.value })}
              />
            </label>
          </div>
        )}
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            disabled={!refreshForm || saveRefreshSettings.isPending}
            onClick={() => saveRefreshSettings.mutate()}
          >
            {saveRefreshSettings.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
          </Button>
          {refreshForm && (
            <Button size="sm" variant="outline" onClick={() => setRefreshForm(null)}>Cancel</Button>
          )}
          <span className="text-xs text-muted-foreground">Leave TMDB Lists sync blank to keep it manual-only.</span>
        </div>
      </SectionCard>

      <SectionCard title="Connected Instances" icon={<Zap size={14} />}>
        <p className="text-xs text-muted-foreground">
          Each Dispatcharr instance (or other XC client) pulling from this pool gets its own credential pair —
          use <code className="bg-muted px-1 rounded">{window.location.origin}</code> as the server URL in that
          instance's XC-type M3U account, with the username/password below. Category access defaults to
          everything — restrict it per-client to give an end-user IPTV app (TiviMate, IPTV Smarters, etc.) its
          own limited catalog instead of the full pool, since Dispatcharr itself has no per-profile VOD split.
        </p>

        <table className="w-full text-xs">
          <thead>
            <tr className="text-muted-foreground text-left">
              <th className="pb-1 font-normal">Label</th>
              <th className="pb-1 font-normal">Credentials</th>
              <th className="pb-1 font-normal">IP allowlist</th>
              <th className="pb-1 font-normal">Category access</th>
              <th className="pb-1 font-normal">Last seen</th>
              <th className="pb-1 font-normal"></th>
            </tr>
          </thead>
          <tbody>
            {xcClientsQuery.data?.map((c) => (
              <tr key={c.id} className="border-t border-border/50 align-top">
                <td className="py-1 pr-2">
                  {c.label}
                  {!c.enabled && <span className="text-muted-foreground"> (disabled)</span>}
                </td>
                <td className="py-1 pr-2">
                  {revealedClientId === c.id ? (
                    <div className="space-y-0.5">
                      <div className="flex items-center gap-1">
                        {c.username}
                        <CopyUrlButton url={c.username} />
                      </div>
                      <div className="flex items-center gap-1">
                        {c.password}
                        <CopyUrlButton url={c.password} />
                      </div>
                    </div>
                  ) : (
                    <button className="text-muted-foreground hover:text-foreground flex items-center gap-1" onClick={() => setRevealedClientId(c.id)}>
                      <Eye size={12} /> reveal
                    </button>
                  )}
                </td>
                <td className="py-1 pr-2 text-muted-foreground">{c.ip_allowlist || '— any —'}</td>
                <td className="py-1 pr-2 text-muted-foreground align-top">
                  {expandedCategoryAccessClientId === c.id ? (
                    <div className="p-1.5 border border-border rounded space-y-1.5 w-56">
                      <div className="max-h-40 overflow-y-auto space-y-0.5">
                        <p className="text-[10px] uppercase text-muted-foreground">Movies</p>
                        {movieCategories.map((cat) => (
                          <label key={cat.id} className="flex items-center gap-1">
                            <input
                              type="checkbox"
                              checked={categoryAccessForm?.has(cat.id) ?? false}
                              onChange={(e) => {
                                const next = new Set(categoryAccessForm ?? [])
                                if (e.target.checked) next.add(cat.id); else next.delete(cat.id)
                                setCategoryAccessForm(next)
                              }}
                            />
                            <span className="truncate">{cat.name}</span>
                          </label>
                        ))}
                        <p className="text-[10px] uppercase text-muted-foreground pt-1">TV Shows</p>
                        {seriesCategories.map((cat) => (
                          <label key={cat.id} className="flex items-center gap-1">
                            <input
                              type="checkbox"
                              checked={categoryAccessForm?.has(cat.id) ?? false}
                              onChange={(e) => {
                                const next = new Set(categoryAccessForm ?? [])
                                if (e.target.checked) next.add(cat.id); else next.delete(cat.id)
                                setCategoryAccessForm(next)
                              }}
                            />
                            <span className="truncate">{cat.name}</span>
                          </label>
                        ))}
                      </div>
                      <div className="flex items-center gap-1 flex-wrap">
                        <Button
                          size="sm"
                          disabled={setClientCategoryAllowlist.isPending || (categoryAccessForm?.size ?? 0) === 0}
                          title={(categoryAccessForm?.size ?? 0) === 0 ? 'Select at least one category, or use Clear for full access' : undefined}
                          onClick={() => setClientCategoryAllowlist.mutate({ id: c.id, ids: Array.from(categoryAccessForm ?? []) })}
                        >
                          Save
                        </Button>
                        <Button
                          size="sm" variant="outline" disabled={setClientCategoryAllowlist.isPending}
                          onClick={() => setClientCategoryAllowlist.mutate({ id: c.id, ids: null })}
                        >
                          Clear (allow all)
                        </Button>
                        <Button
                          size="sm" variant="outline"
                          onClick={() => { setExpandedCategoryAccessClientId(null); setCategoryAccessForm(null) }}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <button
                      className="hover:text-foreground underline decoration-dotted"
                      onClick={() => {
                        setExpandedCategoryAccessClientId(c.id)
                        setCategoryAccessForm(new Set((c.category_allowlist ?? '').split(',').map((s) => s.trim()).filter(Boolean).map(Number)))
                      }}
                    >
                      {c.category_allowlist
                        ? `${c.category_allowlist.split(',').filter(Boolean).length} categor${c.category_allowlist.split(',').filter(Boolean).length === 1 ? 'y' : 'ies'}`
                        : '— all —'}
                    </button>
                  )}
                </td>
                <td className="py-1 pr-2 text-muted-foreground">
                  {c.last_seen_at ? `${new Date(Number(c.last_seen_at) * 1000).toLocaleString()} (${c.last_seen_ip})` : 'never'}
                </td>
                <td className="py-1">
                  <div className="flex items-center gap-1.5">
                    <button
                      title={c.enabled ? 'Disable' : 'Enable'}
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => toggleXcClient.mutate({ id: c.id, enabled: !c.enabled })}
                    >
                      {c.enabled ? <CheckCircle2 size={12} /> : <AlertCircle size={12} />}
                    </button>
                    <button
                      title="Regenerate secret (invalidates the old one immediately)"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => { if (confirm(`Regenerate the credential for "${c.label}"? The old one stops working immediately.`)) regenerateXcClient.mutate(c.id) }}
                    >
                      <RotateCcw size={12} />
                    </button>
                    <button
                      title="Delete"
                      className="text-muted-foreground hover:text-destructive"
                      onClick={() => { if (confirm(`Delete instance "${c.label}"? It will stop being able to authenticate immediately.`)) deleteXcClient.mutate(c.id) }}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="flex items-center gap-1.5 pt-2">
          <input className={inputCls('w-48')} placeholder="Label (e.g. Prod VPS 3)" value={newClientLabel} onChange={(e) => setNewClientLabel(e.target.value)} />
          <input className={inputCls('w-40')} placeholder="IP allowlist (optional)" value={newClientIpAllowlist} onChange={(e) => setNewClientIpAllowlist(e.target.value)} />
          <Button size="sm" disabled={!newClientLabel || createXcClient.isPending} onClick={() => createXcClient.mutate()}>
            {createXcClient.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Plus size={12} className="mr-1" />Add</>}
          </Button>
        </div>
      </SectionCard>

      <SectionCard title="Dispatcharr Connections" icon={<Zap size={14} />}>
        <p className="text-xs text-muted-foreground">
          Who VOD Manager itself reaches out to — the other side of Connected Instances above (who's allowed to
          reach in). Used to push each provider's stream limit into Dispatcharr's own connection accounting, and
          to check real-time live-TV viewer counts for the shared-connection-limit coordination below.
        </p>

        <div className="border border-border rounded p-2 space-y-1.5">
          <p className="text-xs font-medium">Connect a new instance (automated)</p>
          <p className="text-xs text-muted-foreground">
            Give it that instance's own admin API token — VOD Manager creates its client credentials and the
            Dispatcharr-side M3U account for you. All that's left afterward is on Dispatcharr's own side: enable
            VOD on the new account and pick which groups to turn on, same as any other source.
          </p>
          <div className="flex items-center gap-1.5 flex-wrap">
            <input className={inputCls('w-24')} placeholder="Label" value={connectLabel} onChange={(e) => setConnectLabel(e.target.value)} />
            <input className={inputCls('w-36')} placeholder="http://host:port" value={connectUrl} onChange={(e) => setConnectUrl(e.target.value)} />
            <input className={inputCls('w-36')} placeholder="Admin API token" value={connectToken} onChange={(e) => setConnectToken(e.target.value)} />
            <input
              className={inputCls('w-44')} placeholder="VOD Manager's URL, as reachable from that instance"
              value={connectPublicUrl} onChange={(e) => setConnectPublicUrl(e.target.value)}
              title="e.g. host.docker.internal:8282 for a co-located instance, or the public tunnel URL for a remote one — not always the same as what you're viewing this page at"
            />
            <Button
              size="sm"
              disabled={!connectLabel || !connectUrl || !connectToken || !connectPublicUrl || connectInstance.isPending}
              onClick={() => { setConnectResult(null); connectInstance.mutate() }}
            >
              {connectInstance.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : <Zap size={12} className="mr-1" />}
              Connect
            </Button>
          </div>
          {connectResult && <p className="text-xs text-muted-foreground">{connectResult}</p>}
        </div>

        <p className="text-xs font-medium pt-1">Manual / existing connections</p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-muted-foreground text-left">
              <th className="pb-1 font-normal">Label</th>
              <th className="pb-1 font-normal">URL</th>
              <th className="pb-1 font-normal">Token</th>
              <th className="pb-1 font-normal">VOD-relay account ID</th>
              <th className="pb-1 font-normal">DVR</th>
              <th className="pb-1 font-normal"></th>
            </tr>
          </thead>
          <tbody>
            {dispatcharrConnectionsQuery.data?.map((c) => {
              const dvrProvider = providersQuery.data?.find((p) => p.provider_type === 'dispatcharr_dvr' && p.dispatcharr_connection_id === c.id)
              return (
              <tr key={c.id} className="border-t border-border/50">
                <td className="py-1 pr-2">
                  <input
                    className={inputCls('w-24')} defaultValue={c.label} key={c.label}
                    onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== c.label) updateDispatcharrConnection.mutate({ id: c.id, label: v }) }}
                  />
                </td>
                <td className="py-1 pr-2">
                  <input
                    className={inputCls('w-40')} defaultValue={c.url} key={c.url}
                    onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== c.url) updateDispatcharrConnection.mutate({ id: c.id, url: v }) }}
                  />
                </td>
                <td className="py-1 pr-2">
                  {revealedConnId === c.id ? (
                    <div className="flex items-center gap-1">
                      {c.token}
                      <CopyUrlButton url={c.token} />
                    </div>
                  ) : (
                    <button className="text-muted-foreground hover:text-foreground flex items-center gap-1" onClick={() => setRevealedConnId(c.id)}>
                      <Eye size={12} /> reveal
                    </button>
                  )}
                </td>
                <td className="py-1 pr-2">
                  <div className="flex items-center gap-1">
                    <input
                      className={inputCls('w-20')} type="number" placeholder="acct id"
                      defaultValue={c.vod_relay_account_id ?? ''} key={c.vod_relay_account_id}
                      onBlur={(e) => {
                        const v = e.target.value.trim()
                        if (!v) { if (c.vod_relay_account_id != null) updateDispatcharrConnection.mutate({ id: c.id, clear_vod_relay_account_id: true }); return }
                        const n = Number(v)
                        if (n !== c.vod_relay_account_id) updateDispatcharrConnection.mutate({ id: c.id, vod_relay_account_id: n })
                      }}
                    />
                    {c.vod_relay_account_id == null && (
                      <span
                        className="text-destructive flex items-center gap-1"
                        title="No VOD-relay account set — this connection receives no provider syncs and no shared-connection-limit coordination. Enter the Dispatcharr-side M3U account ID above, or delete this and use 'Connect a new instance (automated)' instead."
                      >
                        <AlertCircle size={12} /> not syncing
                      </span>
                    )}
                  </div>
                </td>
                <td className="py-1 pr-2">
                  <Button
                    size="sm" variant="outline"
                    onClick={() => {
                      setDvrModalForm({
                        dvr_local_path: dvrProvider?.dvr_local_path ?? '',
                        dvr_movie_category_id: dvrProvider?.dvr_movie_category_id ? String(dvrProvider.dvr_movie_category_id) : '',
                        dvr_series_category_id: dvrProvider?.dvr_series_category_id ? String(dvrProvider.dvr_series_category_id) : '',
                        priority: dvrProvider ? String(dvrProvider.priority) : '0',
                      })
                      setDvrModalConnectionId(c.id)
                    }}
                  >
                    <CalendarClock size={12} className="mr-1" /> {dvrProvider ? 'DVR ✓' : 'Enable DVR'}
                  </Button>
                </td>
                <td className="py-1">
                  <button
                    title="Delete connection"
                    className="text-muted-foreground hover:text-destructive"
                    onClick={() => { if (confirm(`Delete Dispatcharr connection "${c.label}"? Provider sync/coordination against it will stop.`)) deleteDispatcharrConnection.mutate(c.id) }}
                  >
                    <Trash2 size={12} />
                  </button>
                </td>
              </tr>
              )
            })}
          </tbody>
        </table>
        <div className="flex items-center gap-1.5 pt-2">
          <input className={inputCls('w-28')} placeholder="Label" value={newConnLabel} onChange={(e) => setNewConnLabel(e.target.value)} />
          <input className={inputCls('w-40')} placeholder="http://host:port" value={newConnUrl} onChange={(e) => setNewConnUrl(e.target.value)} />
          <input className={inputCls('w-40')} placeholder="API token" value={newConnToken} onChange={(e) => setNewConnToken(e.target.value)} />
          <Button size="sm" disabled={!newConnLabel || !newConnUrl || !newConnToken || createDispatcharrConnection.isPending} onClick={() => createDispatcharrConnection.mutate()}>
            {createDispatcharrConnection.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Plus size={12} className="mr-1" />Add</>}
          </Button>
        </div>
      </SectionCard>

      {dvrModalConnectionId != null && (() => {
        const connection = dispatcharrConnectionsQuery.data?.find((c) => c.id === dvrModalConnectionId)
        const dvrProvider = providersQuery.data?.find((p) => p.provider_type === 'dispatcharr_dvr' && p.dispatcharr_connection_id === dvrModalConnectionId)
        return (
        <Modal onClose={() => setDvrModalConnectionId(null)} maxWidth="max-w-lg">
          <div className="p-5 space-y-3">
            <h2 className="text-base font-semibold">DVR — {connection?.label}</h2>
            <p className="text-sm text-muted-foreground">
              Pulls finished recordings from this Dispatcharr connection into the pool, and lets people schedule new
              ones (Scheduled tab, or their own self-service portal).
            </p>
            <div className="space-y-2">
              <input
                className={inputCls('w-full')}
                placeholder="Local/NFS path (optional -- e.g. /mnt/dispatcharr-recordings)"
                value={dvrModalForm.dvr_local_path}
                onChange={(e) => setDvrModalForm({ ...dvrModalForm, dvr_local_path: e.target.value })}
                title="Where this Dispatcharr instance's recordings folder is mounted inside this container -- a same-host shared volume or an NFS mount both work the same way here, since either just looks like a local path once mounted in. Leave blank for a Dispatcharr instance on a separate host with no shared mount: recordings are downloaded over its API instead, one copy per recording, a little slower but needs no shared filesystem at all."
              />
              <div className="flex items-center gap-1.5">
                <select
                  className={inputCls('flex-1')}
                  value={dvrModalForm.dvr_movie_category_id}
                  onChange={(e) => setDvrModalForm({ ...dvrModalForm, dvr_movie_category_id: e.target.value })}
                  title="Recorded movies are placed here automatically on import"
                >
                  <option value="">Movie category (optional)…</option>
                  {movieCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
                <select
                  className={inputCls('flex-1')}
                  value={dvrModalForm.dvr_series_category_id}
                  onChange={(e) => setDvrModalForm({ ...dvrModalForm, dvr_series_category_id: e.target.value })}
                  title="Recorded TV episodes' series are placed here automatically on import"
                >
                  <option value="">TV category (optional)…</option>
                  {seriesCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
                <input
                  className={inputCls('w-20')}
                  type="number"
                  placeholder="Priority"
                  value={dvrModalForm.priority}
                  onChange={(e) => setDvrModalForm({ ...dvrModalForm, priority: e.target.value })}
                  title="Only matters if the same movie/episode also comes from another provider -- higher priority streams first"
                />
              </div>
            </div>
            <div className="flex items-center justify-between pt-1">
              {dvrProvider ? (
                <button
                  className="text-xs text-destructive hover:underline flex items-center gap-1"
                  onClick={() => {
                    if (confirm(`Disable DVR for "${connection?.label}"? Its recording rules, upcoming recordings, per-person limits, and portal accounts will all be removed.`)) {
                      disableDvrForConnection.mutate(dvrModalConnectionId)
                    }
                  }}
                >
                  <Trash2 size={12} /> Disable DVR
                </button>
              ) : <span />}
              <div className="flex items-center gap-1.5">
                {dvrProvider && (
                  <Button
                    size="sm" variant="outline"
                    onClick={() => {
                      setRecordingProfilesProviderId(dvrProvider.id)
                      setRecordingProfileForm(blankRecordingProfileForm)
                      setEpgSearchTitle('')
                      epgSearch.reset()
                      setRecordingProfileResult(null)
                      setRecordingProfileError(null)
                      setActiveTab('dvr')
                      setDvrModalConnectionId(null)
                    }}
                  >
                    Manage Recordings
                  </Button>
                )}
                <Button
                  size="sm" disabled={enableDvrForConnection.isPending}
                  onClick={() => enableDvrForConnection.mutate(dvrModalConnectionId)}
                >
                  {enableDvrForConnection.isPending ? <Loader2 size={12} className="animate-spin" /> : (dvrProvider ? 'Save' : 'Enable DVR')}
                </Button>
              </div>
            </div>
          </div>
        </Modal>
        )
      })()}

      <SectionCard title="Backup & Restore" icon={<HardDriveDownload size={14} />}>
        <p className="text-xs text-muted-foreground">
          Each piece can be backed up, restored, or reset independently — e.g. wipe a corrupt
          database without touching saved credentials, or roll back just the config.
        </p>
        <input ref={restoreFileInputRef} type="file" className="hidden" onChange={handleRestoreFileChosen} />
        <table className="w-full text-xs">
          <tbody>
            {(backupComponentsQuery.data ?? []).map((c) => (
              <tr key={c.id} className="border-t border-border/50">
                <td className="py-1.5 pr-2">
                  <div>{c.label}</div>
                  <div className="text-muted-foreground">
                    {c.exists
                      ? `${formatBytes(c.size_bytes)}${c.modified_at ? ` · updated ${new Date(c.modified_at * 1000).toLocaleString()}` : ''}`
                      : 'not created yet'}
                  </div>
                </td>
                <td className="py-1.5 text-right whitespace-nowrap">
                  <Button
                    size="sm" variant="outline" className="gap-1"
                    disabled={!c.exists || backupBusyId === c.id}
                    onClick={() => downloadBackup(c)}
                  >
                    {backupBusyId === c.id ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
                    Download
                  </Button>
                  {' '}
                  <Button
                    size="sm" variant="outline" className="gap-1"
                    disabled={restoreBackup.isPending}
                    onClick={() => { setRestoreTargetId(c.id); restoreFileInputRef.current?.click() }}
                  >
                    <Upload size={12} /> Restore
                  </Button>
                  {' '}
                  <Button
                    size="sm" variant="outline" className="gap-1 text-destructive"
                    disabled={resetBackup.isPending}
                    onClick={() => {
                      if (confirm(`Reset "${c.label}" to a fresh empty state? The current file is moved to a timestamped backup on disk first, not deleted.`)) {
                        resetBackup.mutate(c.id)
                      }
                    }}
                  >
                    <RotateCcw size={12} /> Reset
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </SectionCard>

      <SectionCard title="Diagnostics" icon={<Stethoscope size={14} />}>
        <p className="text-xs text-muted-foreground">
          Downloads this app's own log history with provider credentials, hostnames, and IP addresses
          scrubbed — safe to share when reporting a bug or asking for help.
        </p>
        <Button size="sm" variant="outline" className="gap-1" disabled={diagnosticsBusy} onClick={downloadDiagnostics}>
          {diagnosticsBusy ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
          Download Diagnostic Logs
        </Button>
      </SectionCard>
      </>
      )}

      {activeTab === 'curation' && (
      <>
      <SectionCard title="Rich Metadata (posters, genre, cast)" icon={<Sparkles size={14} />}>
        <p className="text-xs text-muted-foreground">
          Fetches detail (genre, poster, description, cast) from each item's source provider for every movie
          and series in the pool. Runs in the background — safe to navigate away while it works.
        </p>
        <div className="flex items-center gap-1.5">
          <Button size="sm" disabled={!!enrichProgress?.running || startBulkEnrich.isPending} onClick={() => startBulkEnrich.mutate()}>
            {enrichProgress?.running ? <Loader2 size={12} className="animate-spin mr-1" /> : <Sparkles size={12} className="mr-1" />}
            {enrichProgress?.running ? 'Enriching…' : 'Bulk Enrich All'}
          </Button>
          {enrichProgress && !enrichProgress.running && enrichProgress.started_at && enrichProgress.finished_at && (
            <span className="text-xs text-muted-foreground">took {Math.round(enrichProgress.finished_at - enrichProgress.started_at)}s</span>
          )}
        </div>
        {enrichProgress && (enrichProgress.running || enrichProgress.finished_at) && (
          <div className="space-y-1.5">
            {(() => {
              const mPct = enrichProgress.movies_total ? Math.round((enrichProgress.movies_done / enrichProgress.movies_total) * 100) : 100
              const sPct = enrichProgress.series_total ? Math.round((enrichProgress.series_done / enrichProgress.series_total) * 100) : 100
              return (
                <>
                  <div>
                    <div className="flex items-center justify-between text-[11px] text-muted-foreground mb-0.5">
                      <span>Movies</span>
                      <span className="tabular-nums">{enrichProgress.movies_done.toLocaleString()} / {enrichProgress.movies_total.toLocaleString()}{enrichProgress.movies_errors > 0 && ` (${enrichProgress.movies_errors} errors)`}</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-secondary overflow-hidden"><div className="h-full bg-primary" style={{ width: `${mPct}%` }} /></div>
                  </div>
                  <div>
                    <div className="flex items-center justify-between text-[11px] text-muted-foreground mb-0.5">
                      <span>Series</span>
                      <span className="tabular-nums">{enrichProgress.series_done.toLocaleString()} / {enrichProgress.series_total.toLocaleString()}{enrichProgress.series_errors > 0 && ` (${enrichProgress.series_errors} errors)`}</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-secondary overflow-hidden"><div className="h-full bg-primary" style={{ width: `${sPct}%` }} /></div>
                  </div>
                </>
              )
            })()}
          </div>
        )}
      </SectionCard>
      </>
      )}

      {activeTab === 'config' && (
      <>
      <SectionCard title="Title & Metadata Rules" icon={<Zap size={14} />}>
        <p className="text-xs text-muted-foreground">
          Regex find/replace applied to imported text, e.g. stripping a provider's own quality-tier
          prefix ("4K: Movie" → "Movie"). Runs automatically on new imports/enrichment; use "Apply to pool"
          to re-run against everything already imported.
        </p>
        <ul className="text-xs space-y-1">
          {metadataRulesQuery.data?.map((r) => (
            <li key={r.id} className={`flex items-center justify-between gap-2 ${!r.is_active ? 'opacity-50' : ''}`}>
              <span className="font-mono">
                [{r.content_type}] {r.field}: /{r.pattern}/ → "{r.replacement}"
              </span>
              <span className="flex items-center gap-1.5 shrink-0">
                <button
                  className="text-muted-foreground hover:text-foreground"
                  title={r.is_active ? 'Disable rule' : 'Enable rule'}
                  onClick={() => toggleRuleActive.mutate({ id: r.id, active: !r.is_active })}
                >
                  {r.is_active ? 'On' : 'Off'}
                </button>
                <button className="text-muted-foreground hover:text-destructive" title="Delete rule" onClick={() => deleteRule.mutate(r.id)}>
                  <Trash2 size={12} />
                </button>
              </span>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <select className={inputCls()} value={ruleForm.content_type} onChange={(e) => setRuleForm({ ...ruleForm, content_type: e.target.value as typeof ruleForm.content_type })}>
            <option value="both">Movies & Series</option>
            <option value="movie">Movies only</option>
            <option value="series">Series only</option>
          </select>
          <select className={inputCls()} value={ruleForm.field} onChange={(e) => setRuleForm({ ...ruleForm, field: e.target.value as typeof ruleForm.field })}>
            {REWRITABLE_FIELDS.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
          <input className={inputCls('w-40')} placeholder="regex pattern, e.g. ^4K:\s*" value={ruleForm.pattern} onChange={(e) => setRuleForm({ ...ruleForm, pattern: e.target.value })} />
          <input className={inputCls('w-24')} placeholder="replacement" value={ruleForm.replacement} onChange={(e) => setRuleForm({ ...ruleForm, replacement: e.target.value })} />
          <Button size="sm" disabled={!ruleForm.pattern || addRule.isPending} onClick={() => addRule.mutate()}>
            <Plus size={12} className="mr-1" /> Add rule
          </Button>
        </div>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="outline" disabled={applyRules.isPending} onClick={() => applyRules.mutate('movie')}>Apply to movie pool</Button>
          <Button size="sm" variant="outline" disabled={applyRules.isPending} onClick={() => applyRules.mutate('series')}>Apply to series pool</Button>
          {applyRulesResult && <span className="text-xs text-muted-foreground">{applyRulesResult}</span>}
        </div>
      </SectionCard>
      </>
      )}

      {activeTab === 'providers' && (
      <>
      <SectionCard title="Providers" icon={<RefreshCw size={14} />}>
        <p className="text-sm text-muted-foreground">
          Every catalog source feeding the pool -- Xtream Codes, Plex, Emby, Jellyfin. (DVR is enabled per Dispatcharr
          connection in Configuration, not added here.)
        </p>
        <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs min-w-[1100px]">
          <thead>
            <tr className="text-muted-foreground text-left bg-secondary/50">
              <th className="py-2 px-2 font-semibold">Name</th>
              <th className="py-2 px-2 font-semibold">Base URL</th>
              <th className="py-2 px-2 font-semibold">Movies</th>
              <th className="py-2 px-2 font-semibold" title="Distinct series with at least one episode from this provider">Series</th>
              <th className="py-2 px-2 font-semibold" title="Total episode files from this provider — a different number than Series by design (one series can have many episodes)">Episodes</th>
              <th className="py-2 px-2 font-semibold" title="Higher number wins when multiple providers carry the same title">Priority</th>
              <th className="py-2 px-2 font-semibold">Max Streams</th>
              <th className="py-2 px-2 font-semibold" title="How many Dispatcharr connections have a synced profile for this provider">Synced</th>
              <th className="py-2 px-2 font-semibold" title="Real total connection cap for this provider, shared across every linked live-TV account (on any Dispatcharr instance) plus our own VOD usage — VOD will fail over to the next provider instead of exceeding it">Shared Limit / Live Accounts</th>
              <th className="py-2 px-2 font-semibold" title="Most providers work fine with the default browser User-Agent. Only set this if one blocks even that.">User-Agent Override</th>
              <th className="py-2 px-2 font-semibold"></th>
            </tr>
          </thead>
          <tbody>
            {providersQuery.data?.filter((p) => p.provider_type !== 'dispatcharr_dvr').map((p) => (
              <tr key={p.id} className={`border-t border-border ${!p.is_active ? 'opacity-50' : ''}`}>
                <td className="py-2 px-2">
                  <span className="flex items-center gap-1.5 flex-wrap">
                    <input
                      className={inputCls('w-24')}
                      defaultValue={p.name}
                      key={p.name}
                      title="Rename provider"
                      onBlur={(e) => {
                        const v = e.target.value.trim()
                        if (v && v !== p.name) setProviderName.mutate({ id: p.id, name: v })
                      }}
                    />
                    {p.provider_type !== 'xc' && <Chip>{PROVIDER_TYPE_LABELS[p.provider_type]}</Chip>}
                    <StatusPill tone={p.is_active ? 'success' : 'destructive'} label={p.is_active ? 'Active' : 'Inactive'} />
                  </span>
                </td>
                <td className="py-1 pr-2">
                  <input
                    className={inputCls('w-40')}
                    defaultValue={p.base_url}
                    key={p.base_url}
                    title="Base URL"
                    onBlur={(e) => {
                      const v = e.target.value.trim()
                      if (v && v !== p.base_url) setProviderBaseUrl.mutate({ id: p.id, base_url: v })
                    }}
                  />
                </td>
                <td className="py-1 pr-2 text-muted-foreground">{p.movie_count.toLocaleString()}</td>
                <td className="py-1 pr-2 text-muted-foreground">{p.series_count.toLocaleString()}</td>
                <td className="py-1 pr-2 text-muted-foreground">{p.episode_count.toLocaleString()}</td>
                <td className="py-1 pr-2">
                  <input
                    className={inputCls('w-14')}
                    type="number"
                    defaultValue={p.priority}
                    key={p.priority}
                    onBlur={(e) => {
                      const v = Number(e.target.value) || 0
                      if (v !== p.priority) setProviderPriority.mutate({ id: p.id, priority: v })
                    }}
                  />
                </td>
                <td className="py-1 pr-2">
                  <input
                    className={inputCls('w-14')}
                    type="number"
                    title="Max streams (0 = unlimited)"
                    defaultValue={p.max_streams}
                    key={p.max_streams}
                    onBlur={(e) => {
                      const v = Number(e.target.value) || 0
                      if (v !== p.max_streams) setProviderMaxStreams.mutate({ id: p.id, max_streams: v })
                    }}
                  />
                </td>
                <td className="py-1 pr-2 text-muted-foreground">{p.synced_connection_count || '—'}</td>
                <td className="py-1 pr-2">
                  <span className="flex items-center gap-1.5">
                    <input
                      className={inputCls('w-14')}
                      type="number"
                      placeholder="limit"
                      title="Real total connection cap for this provider, shared across every linked live-TV account plus our own VOD usage"
                      defaultValue={p.shared_connection_limit ?? ''}
                      key={`limit-${p.shared_connection_limit}`}
                      onBlur={(e) => {
                        const v = Number(e.target.value) || 0
                        if (v !== (p.shared_connection_limit ?? 0)) setProviderSharedLimit.mutate({ id: p.id, shared_connection_limit: v })
                      }}
                    />
                    <button
                      className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                      onClick={() => setExpandedLiveAccountsProviderId(expandedLiveAccountsProviderId === p.id ? null : p.id)}
                    >
                      {p.live_account_count} live acct{p.live_account_count === 1 ? '' : 's'}
                    </button>
                  </span>
                  {expandedLiveAccountsProviderId === p.id && (
                    <div className="mt-1 p-1.5 border border-border rounded space-y-1">
                      {providerLiveAccountsQuery.data?.map((la) => (
                        <div key={la.id} className="flex items-center gap-1.5">
                          <span>{la.connection_label}: acct #{la.dispatcharr_account_id}</span>
                          <button className="text-muted-foreground hover:text-destructive" onClick={() => removeProviderLiveAccount.mutate(la.id)}>
                            <X size={10} />
                          </button>
                        </div>
                      ))}
                      <div className="flex items-center gap-1">
                        <select
                          className={inputCls('w-24')}
                          value={newLiveAccountConnId}
                          onChange={(e) => setNewLiveAccountConnId(e.target.value)}
                        >
                          <option value="">connection…</option>
                          {dispatcharrConnectionsQuery.data?.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
                        </select>
                        <input className={inputCls('w-16')} type="number" placeholder="acct id" value={newLiveAccountAcctId} onChange={(e) => setNewLiveAccountAcctId(e.target.value)} />
                        <Button
                          size="sm"
                          disabled={!newLiveAccountConnId || !newLiveAccountAcctId || setProviderLiveAccount.isPending}
                          onClick={() => setProviderLiveAccount.mutate({ providerId: p.id, connectionId: Number(newLiveAccountConnId), accountId: Number(newLiveAccountAcctId) })}
                        >
                          Add
                        </Button>
                      </div>
                    </div>
                  )}
                </td>
                <td className="py-1 pr-2">
                  <input
                    className={inputCls('w-32')}
                    placeholder="default"
                    defaultValue={p.custom_user_agent ?? ''}
                    key={p.custom_user_agent}
                    title="Overrides the default browser User-Agent for this provider only"
                    onBlur={(e) => {
                      const v = e.target.value.trim()
                      if (v !== (p.custom_user_agent ?? '')) setProviderUserAgent.mutate({ id: p.id, custom_user_agent: v })
                    }}
                  />
                </td>
                <td className="py-1 flex items-center gap-1.5">
                  <Button size="sm" variant="outline" disabled={syncProvider.isPending} onClick={() => syncProvider.mutate(p.id)}>
                    Sync
                  </Button>
                  <Button size="sm" variant="outline" disabled={importingId === p.id} onClick={() => importCatalog.mutate(p.id)}>
                    {importingId === p.id ? <Loader2 size={12} className="animate-spin mr-1" /> : <Download size={12} className="mr-1" />}
                    Import catalog
                  </Button>
                  <Button
                    size="sm" variant="outline"
                    title="Categories to auto-archive on import, as this provider itself names them"
                    onClick={() => {
                      setExcludeCategoriesProviderId(p.id)
                      setExcludeCategoriesDraft(new Set(p.import_exclude_categories))
                      setExcludeCategoriesSearch('')
                      setExcludeCategoriesShowFilter('all')
                      setExcludeCategoriesLastClickedIndex(null)
                      setExcludeCategoriesError(null)
                    }}
                  >
                    Exclude Categories{p.import_exclude_categories.length ? ` (${p.import_exclude_categories.length})` : ''}
                  </Button>
                  <Button
                    size="sm" variant="outline" disabled={toggleProviderActive.isPending}
                    onClick={() => toggleProviderActive.mutate({ id: p.id, active: !p.is_active })}
                  >
                    {p.is_active ? 'Deactivate' : 'Activate'}
                  </Button>
                  <button
                    title="Delete provider"
                    className="text-muted-foreground hover:text-destructive p-1"
                    onClick={() => { if (confirm(`Delete provider "${p.name}"? Its sources for existing movies/episodes will be removed.`)) deleteProvider.mutate(p.id) }}
                  >
                    <Trash2 size={12} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {importResult && <p className="text-xs text-muted-foreground">{importResult}</p>}
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <select
            className={inputCls()}
            value={providerForm.provider_type}
            onChange={(e) => setProviderForm({ ...providerForm, provider_type: e.target.value as 'xc' | 'plex' | 'emby' | 'jellyfin' })}
          >
            <option value="xc">Xtream-Codes</option>
            <option value="plex">Plex</option>
            <option value="emby">Emby</option>
            <option value="jellyfin">Jellyfin</option>
          </select>
          <input className={inputCls()} placeholder="Name" value={providerForm.name} onChange={(e) => setProviderForm({ ...providerForm, name: e.target.value })} />
          <input
            className={inputCls()}
            placeholder={providerForm.provider_type === 'plex' ? 'Base URL (e.g. https://plex.example.com)' : providerForm.provider_type === 'xc' ? 'Base URL' : 'Base URL (e.g. http://host:8096)'}
            value={providerForm.base_url}
            onChange={(e) => setProviderForm({ ...providerForm, base_url: e.target.value })}
          />
          {providerForm.provider_type === 'xc' && (
            <input className={inputCls()} placeholder="Username" value={providerForm.username} onChange={(e) => setProviderForm({ ...providerForm, username: e.target.value })} />
          )}
          <input
            className={inputCls()}
            type="password"
            placeholder={providerForm.provider_type === 'plex' ? 'Plex token (X-Plex-Token)' : providerForm.provider_type === 'xc' ? 'Password' : 'API key'}
            value={providerForm.password}
            onChange={(e) => setProviderForm({ ...providerForm, password: e.target.value })}
          />
          <input className={inputCls('w-24')} type="number" placeholder="Max streams" value={providerForm.max_streams} onChange={(e) => setProviderForm({ ...providerForm, max_streams: e.target.value })} />
          <input className={inputCls('w-20')} type="number" placeholder="Priority" value={providerForm.priority} onChange={(e) => setProviderForm({ ...providerForm, priority: e.target.value })} />
          <Button
            size="sm"
            disabled={
              !providerForm.name || addProvider.isPending ||
              !providerForm.base_url || !providerForm.password || (providerForm.provider_type === 'xc' && !providerForm.username)
            }
            onClick={() => addProvider.mutate()}
          >
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
      </SectionCard>
      </>
      )}

      {activeTab === 'curation' && (
      <>
      <SectionCard title="Import Language Exclusion" icon={<Trash2 size={14} />}>
        <p className="text-xs text-muted-foreground">
          Auto-archives matching movies/series the moment they're imported (or re-imported) — global across every
          provider, since the languages you don't want almost never depend on which provider it came from. Per-provider
          category exclusion is the "Exclude Categories" button on each provider above. Same rule either way: it
          archives (still browsable/playable/categorizable if you change your mind), never deletes, and never
          overrides an item you've manually un-archived.
        </p>
        {allLanguageCodes.length > 0 ? (
          <>
            <div className="flex items-center gap-1.5">
              <input
                className={inputCls('flex-1')}
                placeholder="Search languages…"
                value={languageSearch}
                onChange={(e) => setLanguageSearch(e.target.value)}
              />
              <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
                {(['all', 'selected', 'unselected'] as const).map((f) => (
                  <button
                    key={f}
                    className={`px-1.5 py-0.5 rounded text-[10px] transition-colors ${languageShowFilter === f ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
                    onClick={() => setLanguageShowFilter(f)}
                  >
                    {f === 'all' ? 'All' : f === 'selected' ? 'Selected' : 'Unselected'}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-1.5 text-xs">
              <button
                className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                onClick={() => setLanguageDraft(new Set([...languageDraft, ...visibleLanguageCodes.map((c) => c.code)]))}
              >
                Select visible ({visibleLanguageCodes.length})
              </button>
              <button
                className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                onClick={() => { const next = new Set(languageDraft); visibleLanguageCodes.forEach((c) => next.delete(c.code)); setLanguageDraft(next) }}
              >
                Deselect visible ({visibleLanguageCodes.filter((c) => languageDraft.has(c.code)).length})
              </button>
              <span className="text-muted-foreground ml-auto">{languageDraft.size} selected total · shift-click to select a range</span>
            </div>
            <div className="max-h-48 overflow-y-auto space-y-0.5 border border-border rounded p-2 text-xs">
              {visibleLanguageCodes.map((c, i) => (
                <label key={c.code} className="flex items-center gap-1.5 select-none">
                  <input
                    type="checkbox"
                    checked={languageDraft.has(c.code)}
                    onChange={() => {}}
                    onClick={(e) => toggleLanguageSelected(c.code, i, e.shiftKey)}
                  />
                  <span className="font-mono">{c.code}</span>
                  {LANGUAGE_CODE_NAMES[c.code] && <span className="text-muted-foreground">— {LANGUAGE_CODE_NAMES[c.code]}</span>}
                  <span className="text-muted-foreground ml-auto">{c.count > 0 ? `${c.count} title${c.count === 1 ? '' : 's'}` : 'not currently in pool'}</span>
                </label>
              ))}
              {visibleLanguageCodes.length === 0 && <p className="text-muted-foreground">No languages match.</p>}
            </div>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">No language-tagged titles ("AR| ...", "EN| ...") found in the pool yet — import a catalog first.</p>
        )}
        <Button
          size="sm"
          disabled={saveImportLanguageExclusion.isPending}
          onClick={() => saveImportLanguageExclusion.mutate({
            exclude_prefixes: [...languageDraft],
            exclude_non_latin: importLanguageExclusionQuery.data?.exclude_non_latin ?? false,
          })}
        >
          {saveImportLanguageExclusion.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
          Save selected languages
        </Button>
        <label className="flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={importLanguageExclusionQuery.data?.exclude_non_latin ?? false}
            onChange={(e) => saveImportLanguageExclusion.mutate({
              exclude_prefixes: [...languageDraft],
              exclude_non_latin: e.target.checked,
            })}
          />
          Also exclude non-Latin-script titles (Arabic, Thai, Chinese/Japanese/Korean, Cyrillic, Greek, Hebrew, Devanagari)
        </label>
        <div className="flex items-center gap-1.5 pt-1">
          <Button
            size="sm" variant="outline"
            disabled={applyImportExclusionsNow.isPending || applyExclusionsJobQuery.data?.status === 'running'}
            onClick={() => { if (confirm('Re-import every active provider now to apply the current exclusion rules across your existing catalog? For a large catalog this can take a while.')) applyImportExclusionsNow.mutate() }}
          >
            {applyImportExclusionsNow.isPending || applyExclusionsJobQuery.data?.status === 'running' ? <Loader2 size={12} className="animate-spin mr-1" /> : <RefreshCw size={12} className="mr-1" />}
            Apply rules to existing catalog now
          </Button>
          {applyExclusionsJobQuery.data?.status === 'running' && (
            <span className="text-xs text-muted-foreground">
              Provider {applyExclusionsJobQuery.data.completed + 1} of {applyExclusionsJobQuery.data.total}
              {applyExclusionsJobQuery.data.current_provider ? ` — syncing ${applyExclusionsJobQuery.data.current_provider}…` : '…'}
            </span>
          )}
          {applyExclusionsJobQuery.data?.status === 'error' && (
            <span className="text-xs text-destructive">Failed: {applyExclusionsJobQuery.data.error}</span>
          )}
        </div>
        {applyExclusionsJobQuery.data?.status === 'done' && !!applyExclusionsJobQuery.data.results.length && (
          <div className="text-xs border border-border rounded p-2 space-y-1">
            <p className="text-muted-foreground">
              Done — {applyExclusionsJobQuery.data.results.length} provider(s), {
                applyExclusionsJobQuery.data.results.reduce((n, r) => n + (r.movies_archived ?? 0) + (r.series_archived ?? 0), 0)
              } newly archived and {
                applyExclusionsJobQuery.data.results.reduce((n, r) => n + (r.movies_unarchived ?? 0) + (r.series_unarchived ?? 0), 0)
              } restored (no longer matched) by the current rules.
            </p>
            {applyExclusionsJobQuery.data.results.map((r) => (
              <p key={r.provider} className="text-muted-foreground">
                {r.provider}: {r.error
                  ? <span className="text-destructive">{r.error}</span>
                  : <>{(r.movies_archived ?? 0) + (r.series_archived ?? 0)} archived · {(r.movies_unarchived ?? 0) + (r.series_unarchived ?? 0)} restored · {(r.movies_matched ?? 0) + (r.series_matched ?? 0)} matched · {(r.movies_created ?? 0) + (r.series_created ?? 0)} new</>}
              </p>
            ))}
          </div>
        )}
      </SectionCard>

      {excludeCategoriesProviderId != null && (() => {
        const allNames = providerAvailableCategoriesQuery.data?.categories ?? []
        const visible = allNames.filter((name) => {
          if (excludeCategoriesSearch && !name.toLowerCase().includes(excludeCategoriesSearch.toLowerCase())) return false
          if (excludeCategoriesShowFilter === 'selected' && !excludeCategoriesDraft.has(name)) return false
          if (excludeCategoriesShowFilter === 'unselected' && excludeCategoriesDraft.has(name)) return false
          return true
        })
        return (
        <Modal onClose={() => setExcludeCategoriesProviderId(null)} maxWidth="max-w-lg">
          <div className="p-5 space-y-3">
            <h2 className="text-base font-semibold">
              Exclude categories — {providersQuery.data?.find((p) => p.id === excludeCategoriesProviderId)?.name}
            </h2>
            <p className="text-sm text-muted-foreground">
              Movies/series in a checked category get auto-archived on import, using this provider's own category
              names. Archived, not deleted — still browsable if you change your mind.
            </p>
            {providerAvailableCategoriesQuery.isLoading && <p className="text-xs text-muted-foreground">Loading this provider's categories…</p>}
            {providerAvailableCategoriesQuery.data && !providerAvailableCategoriesQuery.data.categories.length && (
              <p className="text-xs text-muted-foreground">No categories reported by this provider.</p>
            )}
            {!!allNames.length && (
              <>
                <div className="flex items-center gap-1.5">
                  <input
                    className={inputCls('flex-1')}
                    placeholder="Search categories…"
                    value={excludeCategoriesSearch}
                    onChange={(e) => setExcludeCategoriesSearch(e.target.value)}
                  />
                  <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
                    {(['all', 'selected', 'unselected'] as const).map((f) => (
                      <button
                        key={f}
                        className={`px-1.5 py-0.5 rounded text-[10px] transition-colors ${excludeCategoriesShowFilter === f ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
                        onClick={() => setExcludeCategoriesShowFilter(f)}
                      >
                        {f === 'all' ? 'All' : f === 'selected' ? 'Selected' : 'Unselected'}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-1.5 text-xs">
                  <button
                    className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                    onClick={() => setExcludeCategoriesDraft(new Set([...excludeCategoriesDraft, ...visible]))}
                  >
                    Select visible ({visible.length})
                  </button>
                  <button
                    className="text-muted-foreground hover:text-foreground underline decoration-dotted"
                    onClick={() => { const next = new Set(excludeCategoriesDraft); visible.forEach((n) => next.delete(n)); setExcludeCategoriesDraft(next) }}
                  >
                    Deselect visible ({visible.filter((n) => excludeCategoriesDraft.has(n)).length})
                  </button>
                  <span className="text-muted-foreground ml-auto">{excludeCategoriesDraft.size} selected total · shift-click to select a range</span>
                </div>
              </>
            )}
            <div className="max-h-64 overflow-y-auto space-y-1 border border-border rounded p-2">
              {visible.map((name, i) => (
                <label key={name} className="flex items-center gap-1.5 text-xs select-none">
                  <input
                    type="checkbox"
                    checked={excludeCategoriesDraft.has(name)}
                    onChange={() => {}}
                    onClick={(e) => {
                      const willBeChecked = !excludeCategoriesDraft.has(name)
                      const next = new Set(excludeCategoriesDraft)
                      if (e.shiftKey && excludeCategoriesLastClickedIndex != null) {
                        const [start, end] = [excludeCategoriesLastClickedIndex, i].sort((a, b) => a - b)
                        for (let j = start; j <= end; j++) {
                          if (willBeChecked) next.add(visible[j]); else next.delete(visible[j])
                        }
                      } else {
                        if (willBeChecked) next.add(name); else next.delete(name)
                      }
                      setExcludeCategoriesDraft(next)
                      setExcludeCategoriesLastClickedIndex(i)
                    }}
                  />
                  {name}
                </label>
              ))}
              {!!allNames.length && !visible.length && <p className="text-muted-foreground">No categories match.</p>}
            </div>
            {excludeCategoriesError && <p className="text-xs text-destructive">{excludeCategoriesError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <Button size="sm" variant="outline" onClick={() => setExcludeCategoriesProviderId(null)}>Cancel</Button>
              <Button
                size="sm"
                disabled={setProviderImportExcludeCategories.isPending}
                onClick={() => setProviderImportExcludeCategories.mutate({ id: excludeCategoriesProviderId, category_names: Array.from(excludeCategoriesDraft) })}
              >
                {setProviderImportExcludeCategories.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
              </Button>
            </div>
          </div>
        </Modal>
        )
      })()}
      </>
      )}

      {activeTab === 'dvr' && (
      <>
        {!dvrProviders.length ? (
          <SectionCard title="DVR" icon={<CalendarClock size={14} />}>
            <p className="text-xs text-muted-foreground">
              DVR isn't enabled on any Dispatcharr connection yet. Go to Configuration → Dispatcharr Connections and click "Enable DVR" on the one you want to use.
            </p>
          </SectionCard>
        ) : (
          <>
            {dvrProviders.length > 1 && (
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-muted-foreground">Provider:</span>
                <select
                  className={inputCls()}
                  value={recordingProfilesProviderId ?? ''}
                  onChange={(e) => setRecordingProfilesProviderId(Number(e.target.value))}
                >
                  {dvrProviders.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
            )}

            {dvrSubTab === 'scheduled' && (
              <>
                <SectionCard title="Recording Rules" icon={<CalendarClock size={14} />}>
                  <p className="text-sm text-muted-foreground">
                    Each rule schedules real Dispatcharr recordings for a specific show on a specific channel, and
                    re-checks for new episodes automatically. When one finishes, it's routed into the categories
                    below instead of this provider's own default.
                  </p>

                  <div className="space-y-1.5">
                    {recordingProfilesQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
                    {recordingProfilesQuery.data && !recordingProfilesQuery.data.length && (
                      <p className="text-xs text-muted-foreground">No recording rules yet.</p>
                    )}
                    {recordingProfilesQuery.data?.map((rp) => (
                      <div key={rp.id} className="flex items-center gap-1.5 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                        <span className="flex-1">
                          <span className="font-semibold">{rp.label}</span>{' '}
                          <span className="text-muted-foreground">
                            — "{rp.title}", {rp.mode === 'all' ? 'all episodes' : 'new episodes only'}
                            {rp.channel_id ? `, channel ${rp.channel_id}` : ' (no channel -- won\'t schedule)'}
                            {' → '}
                            {[
                              movieCategories.find((c) => c.id === rp.target_movie_category_id)?.name,
                              seriesCategories.find((c) => c.id === rp.target_series_category_id)?.name,
                            ].filter(Boolean).join(' / ') || 'no category set'}
                            {rp.dispatcharr_user_id != null && (
                              <> · {dispatcharrUsersQuery.data?.find((u) => u.id === rp.dispatcharr_user_id)?.username ?? `user ${rp.dispatcharr_user_id}`}</>
                            )}
                          </span>
                        </span>
                        <button
                          title={rp.monitored ? 'Monitored -- shows up on the Missing Episodes page. Click to unmonitor.' : 'Unmonitored -- hidden from the Missing Episodes page. Click to monitor.'}
                          className={rp.monitored ? 'text-primary hover:text-primary/70 p-1' : 'text-muted-foreground/50 hover:text-foreground p-1'}
                          disabled={setRecordingProfileMonitored.isPending}
                          onClick={() => setRecordingProfileMonitored.mutate({ id: rp.id, monitored: !rp.monitored })}
                        >
                          {rp.monitored ? <Eye size={12} /> : <EyeOff size={12} />}
                        </button>
                        <button
                          title="Delete this recording rule (also cancels its future recordings on Dispatcharr)"
                          className="text-muted-foreground hover:text-destructive p-1"
                          onClick={() => { if (confirm(`Delete recording rule "${rp.label}"? This also cancels its future recordings on Dispatcharr.`)) deleteRecordingProfile.mutate(rp.id) }}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="border-t border-border pt-3 space-y-1.5">
                    <p className="text-xs font-medium">Add a rule</p>
                    <p className="text-[11px] text-muted-foreground">
                      Search the real guide and pick the exact channel to record — matching a title with no channel
                      picked records every affiliate carrying it as a separate duplicate, so a channel is required.
                    </p>

                    <div className="flex flex-wrap items-center gap-1.5">
                      <input
                        className={inputCls('flex-1 min-w-[8rem]')}
                        placeholder="Label (e.g. Bob's Seinfeld)"
                        value={recordingProfileForm.label}
                        onChange={(e) => setRecordingProfileForm({ ...recordingProfileForm, label: e.target.value })}
                      />
                      <select
                        className={inputCls()}
                        value={recordingProfileForm.dispatcharr_user_id}
                        onChange={(e) => setRecordingProfileForm({ ...recordingProfileForm, dispatcharr_user_id: e.target.value })}
                        title="Attributes this rule to a real Dispatcharr person -- if they have DVR limits configured under Users, this rule counts against their stream budget, and their own Channel Profile is used to flag channels outside their lineup below"
                      >
                        <option value="">Person (optional)…</option>
                        {dispatcharrUsersQuery.data?.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
                      </select>
                      <select
                        className={inputCls()}
                        value={recordingProfileForm.mode}
                        onChange={(e) => setRecordingProfileForm({ ...recordingProfileForm, mode: e.target.value as 'all' | 'new' })}
                        title="All episodes vs. new episodes only"
                      >
                        <option value="all">All episodes</option>
                        <option value="new">New episodes only</option>
                      </select>
                    </div>

                    {recordingProfileForm.channel_id ? (
                      <div className="flex items-center gap-1.5 text-xs border border-border rounded px-2 py-1.5 bg-muted/30">
                        <span className="flex-1">
                          <span className="font-medium">{recordingProfileForm.channel_label}</span>
                          <span className="text-muted-foreground"> — "{recordingProfileForm.title}"</span>
                        </span>
                        <button
                          className="text-muted-foreground hover:text-foreground underline decoration-dotted text-[11px] shrink-0"
                          onClick={() => setRecordingProfileForm({ ...recordingProfileForm, channel_id: '', tvg_id: '', channel_label: '' })}
                        >
                          change
                        </button>
                      </div>
                    ) : (
                      <div className="space-y-1.5">
                        <div className="flex items-center gap-1.5">
                          <input
                            className={inputCls('flex-1')}
                            placeholder="Search the guide (e.g. Seinfeld)"
                            value={epgSearchTitle}
                            onChange={(e) => setEpgSearchTitle(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter' && epgSearchTitle.trim()) epgSearch.mutate() }}
                          />
                          <Button
                            size="sm" variant="outline"
                            disabled={!epgSearchTitle.trim() || epgSearch.isPending}
                            onClick={() => epgSearch.mutate()}
                          >
                            {epgSearch.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Search'}
                          </Button>
                        </div>
                        {epgSearch.data && !epgChannelGroups.length && (
                          <p className="text-xs text-muted-foreground">No upcoming airings found for that title in the next 7 days.</p>
                        )}
                        {!!epgChannelGroups.length && (
                          <div className="max-h-48 overflow-y-auto space-y-0.5 border border-border rounded p-1.5">
                            {epgChannelGroups.map(({ channel, programs }) => {
                              const inLineup = visibleChannelIds == null || visibleChannelIds.has(channel.id)
                              const next = programs[0]
                              return (
                                <button
                                  key={channel.id}
                                  className={`w-full text-left text-xs px-2 py-1 rounded hover:bg-accent flex items-center gap-2 ${inLineup ? '' : 'opacity-50'}`}
                                  title={inLineup ? undefined : `Outside ${selectedDispatcharrUser?.username}'s Channel Profile -- they wouldn't normally see this channel`}
                                  onClick={() => pickEpgChannel(channel, programs)}
                                >
                                  <span className="font-mono text-muted-foreground w-8 shrink-0">{channel.channel_number ?? '—'}</span>
                                  <span className="flex-1 truncate">{channel.name}</span>
                                  {next?.sub_title && <span className="text-muted-foreground truncate max-w-[10rem]">{next.sub_title}</span>}
                                  <span className="text-muted-foreground shrink-0">{programs.length} upcoming</span>
                                  {!inLineup && <span className="text-[10px] text-muted-foreground shrink-0">not in lineup</span>}
                                </button>
                              )
                            })}
                          </div>
                        )}
                      </div>
                    )}

                    <div className="flex flex-wrap items-center gap-1.5">
                      <select
                        className={inputCls()}
                        value={recordingProfileForm.target_movie_category_id}
                        onChange={(e) => setRecordingProfileForm({ ...recordingProfileForm, target_movie_category_id: e.target.value })}
                        title="If a matching recording is classified as a movie, place it here"
                      >
                        <option value="">Movie category (optional)…</option>
                        {movieCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                      <select
                        className={inputCls()}
                        value={recordingProfileForm.target_series_category_id}
                        onChange={(e) => setRecordingProfileForm({ ...recordingProfileForm, target_series_category_id: e.target.value })}
                        title="If a matching recording is classified as a TV series, place its series here"
                      >
                        <option value="">TV category (optional)…</option>
                        {seriesCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                      <select
                        className={inputCls()}
                        value={recordingProfileForm.backfill_mode}
                        onChange={(e) => setRecordingProfileForm({ ...recordingProfileForm, backfill_mode: e.target.value as '' | 'pointer' | 'download' })}
                        title="If a matching episode/movie already exists in the pool from a regular provider, use it instead of recording a new copy via DVR"
                      >
                        <option value="">Backfill: off (always DVR-record)</option>
                        <option value="pointer">Backfill: pointer (use existing source, no new disk cost)</option>
                        <option value="download">Backfill: download &amp; store (durable local copy, survives that provider going down)</option>
                      </select>
                    </div>

                    {recordingProfileResult && (
                      <p className="text-xs text-muted-foreground">
                        Scheduled {recordingProfileResult.scheduled_now} of {recordingProfileResult.total_matches} upcoming episode
                        {recordingProfileResult.total_matches === 1 ? '' : 's'}
                        {recordingProfileResult.total_matches > 0 && recordingProfileResult.scheduled_now === 0 && ' (all already scheduled)'}.
                        {recordingProfileResult.skipped_conflicts > 0 && (
                          <> {recordingProfileResult.skipped_conflicts} skipped -- the guide listed more than one episode for the
                          same time slot on this channel (conflicting upstream EPG data), so only the first was kept.</>
                        )}
                        {recordingProfileResult.backfilled_now > 0 && (
                          <> {recordingProfileResult.backfilled_now} already in the pool from another provider -- placed directly instead of recording.</>
                        )}
                      </p>
                    )}
                    {recordingProfileError && <p className="text-xs text-destructive">{recordingProfileError}</p>}
                    <div className="flex justify-end gap-2 pt-1">
                      <Button
                        size="sm"
                        disabled={!recordingProfileForm.title.trim() || !recordingProfileForm.channel_id || addRecordingProfile.isPending}
                        onClick={() => addRecordingProfile.mutate()}
                      >
                        {addRecordingProfile.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Plus size={12} className="mr-1" /> Add rule</>}
                      </Button>
                    </div>
                  </div>
                </SectionCard>

                <SectionCard title="Upcoming Recordings" icon={<CalendarDays size={14} />}>
                  {dvrUpcomingQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
                  {dvrUpcomingQuery.data && !dvrUpcomingQuery.data.length && (
                    <p className="text-xs text-muted-foreground">Nothing scheduled right now.</p>
                  )}
                  <div className="space-y-3">
                    {upcomingByDay.map(([day, items]) => (
                      <div key={day}>
                        <p className="text-xs font-medium text-muted-foreground mb-1">{day}</p>
                        <div className="space-y-1">
                          {items.map((r) => {
                            const prog = r.custom_properties?.program
                            const who = r.custom_properties?.scheduled_by
                            const time = new Date(r.start_time).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
                            return (
                              <div key={r.id} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                                <span className="font-mono text-muted-foreground w-16 shrink-0 tabular-nums">{time}</span>
                                <span className="flex-1 truncate">
                                  <span className="font-semibold">{prog?.title ?? 'Unknown'}</span>
                                  {prog?.sub_title && <span className="text-muted-foreground"> — {prog.sub_title}</span>}
                                </span>
                                {who?.dispatcharr_username && <Chip>{who.dispatcharr_username}</Chip>}
                                <Chip>channel {r.channel}</Chip>
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                </SectionCard>
              </>
            )}

            {dvrSubTab === 'users' && (
              <>
              <SectionCard title="Users" icon={<Users size={14} />}>
                <p className="text-sm text-muted-foreground">
                  Opt-in per person -- only enforced for someone listed here. Stream reserve predicts whether a new
                  recording rule could require more simultaneous recordings than their Dispatcharr account allows.
                  Disk quota withholds new category placements once they're over (nothing existing is ever deleted
                  automatically). Retention lets you reclaim space explicitly -- review candidates and confirm before
                  anything is actually removed.
                </p>
                {dvrUserLimitsQuery.data && !dvrUserLimitsQuery.data.length && (
                  <p className="text-xs text-muted-foreground">No one has DVR limits configured yet.</p>
                )}
                <div className="space-y-2">
                  {dvrUserLimitsQuery.data?.map((lim) => {
                    const liveUser = dispatcharrUsersQuery.data?.find((u) => u.id === lim.dispatcharr_user_id)
                    const usage = dvrUserUsageQuery.data?.[lim.id]
                    const virtualGB = usage != null ? (usage.virtual_bytes / 1024 ** 3) : null
                    const quotaGB = lim.disk_quota_bytes != null ? (lim.disk_quota_bytes / 1024 ** 3) : null
                    const pushUpdate = (patch: Partial<{ stream_reserve: number; disk_quota_bytes: number | null; retention_max_age_days: number | null; retention_max_episodes_per_show: number | null }>) =>
                      updateDvrUserLimit.mutate({
                        id: lim.id,
                        stream_reserve: lim.stream_reserve, disk_quota_bytes: lim.disk_quota_bytes,
                        retention_max_age_days: lim.retention_max_age_days, retention_max_episodes_per_show: lim.retention_max_episodes_per_show,
                        ...patch,
                      })
                    const actualGB = usage != null ? (usage.actual_bytes / 1024 ** 3) : null
                    return (
                      <div key={lim.id} className="rounded-lg border border-border bg-card px-3 py-2.5 shadow-sm space-y-2">
                        <div className="flex items-center gap-3">
                          <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center text-[11px] font-bold text-primary-foreground shrink-0">
                            {lim.dispatcharr_username.slice(0, 1).toUpperCase()}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="text-[13px] font-semibold">{lim.dispatcharr_username}</div>
                            <div className="text-[11px] text-muted-foreground">
                              stream limit {liveUser?.stream_limit ?? '?'} (budget {liveUser ? Math.max(0, liveUser.stream_limit - lim.stream_reserve) : '?'})
                            </div>
                          </div>
                          <button
                            title="Remove this person's DVR limits (their existing recording rules are unaffected, just unconstrained again)"
                            className="text-muted-foreground hover:text-destructive p-1"
                            onClick={() => { if (confirm(`Remove DVR limits for "${lim.dispatcharr_username}"?`)) deleteDvrUserLimit.mutate(lim.id) }}
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                        <QuotaBar actualGB={actualGB} virtualGB={virtualGB} quotaGB={quotaGB} />
                        <div className="flex flex-wrap items-center gap-1.5">
                          <label className="text-[10px] text-muted-foreground flex items-center gap-1">
                            Reserve
                            <input
                              className={inputCls('w-16')} type="number" defaultValue={lim.stream_reserve} key={`res-${lim.id}-${lim.stream_reserve}`}
                              onBlur={(e) => { const v = Number(e.target.value) || 0; if (v !== lim.stream_reserve) pushUpdate({ stream_reserve: v }) }}
                            />
                          </label>
                          <label className="text-[10px] text-muted-foreground flex items-center gap-1">
                            Quota GB
                            <input
                              className={inputCls('w-16')} type="number" defaultValue={quotaGB ?? ''} key={`quota-${lim.id}-${lim.disk_quota_bytes}`}
                              placeholder="none"
                              onBlur={(e) => {
                                const v = e.target.value ? Math.round(Number(e.target.value) * 1024 ** 3) : null
                                if (v !== lim.disk_quota_bytes) pushUpdate({ disk_quota_bytes: v })
                              }}
                            />
                          </label>
                          <label className="text-[10px] text-muted-foreground flex items-center gap-1">
                            Max age (days)
                            <input
                              className={inputCls('w-16')} type="number" defaultValue={lim.retention_max_age_days ?? ''} key={`age-${lim.id}-${lim.retention_max_age_days}`}
                              placeholder="none"
                              onBlur={(e) => {
                                const v = e.target.value ? Number(e.target.value) : null
                                if (v !== lim.retention_max_age_days) pushUpdate({ retention_max_age_days: v })
                              }}
                            />
                          </label>
                          <label className="text-[10px] text-muted-foreground flex items-center gap-1">
                            Max episodes/show
                            <input
                              className={inputCls('w-16')} type="number" defaultValue={lim.retention_max_episodes_per_show ?? ''} key={`eps-${lim.id}-${lim.retention_max_episodes_per_show}`}
                              placeholder="none"
                              onBlur={(e) => {
                                const v = e.target.value ? Number(e.target.value) : null
                                if (v !== lim.retention_max_episodes_per_show) pushUpdate({ retention_max_episodes_per_show: v })
                              }}
                            />
                          </label>
                          {(lim.retention_max_age_days || lim.retention_max_episodes_per_show) && (
                            <Button size="sm" variant="outline" onClick={() => setRetentionReviewLimitId(lim.id)}>
                              Review retention
                            </Button>
                          )}
                        </div>
                        {retentionReviewLimitId === lim.id && (
                          <div className="border-t border-border pt-1.5 mt-1.5 space-y-1.5">
                            {retentionCandidatesQuery.isLoading && <p className="text-[11px] text-muted-foreground">Scanning…</p>}
                            {retentionCandidatesQuery.data && !retentionCandidatesQuery.data.movies.length && !retentionCandidatesQuery.data.episodes.length && (
                              <p className="text-[11px] text-muted-foreground">Nothing eligible for removal right now.</p>
                            )}
                            {retentionCandidatesQuery.data && (retentionCandidatesQuery.data.movies.length > 0 || retentionCandidatesQuery.data.episodes.length > 0) && (
                              <>
                                <p className="text-[11px] text-muted-foreground">
                                  {retentionCandidatesQuery.data.movies.length} movie(s), {retentionCandidatesQuery.data.episodes.length} episode(s) eligible -- only items whose sole source is this DVR provider:
                                </p>
                                <ul className="text-[11px] text-muted-foreground list-disc pl-4 max-h-32 overflow-y-auto">
                                  {retentionCandidatesQuery.data.movies.map((m) => (
                                    <li key={`m-${m.movie_id}`}>{m.name} {m.year ? `(${m.year})` : ''}</li>
                                  ))}
                                  {retentionCandidatesQuery.data.episodes.map((e) => (
                                    <li key={`e-${e.episode_id}`}>{e.series_name} S{e.season_number}E{e.episode_number} — {e.name}</li>
                                  ))}
                                </ul>
                              </>
                            )}
                            <div className="flex justify-end gap-2">
                              <Button size="sm" variant="outline" onClick={() => setRetentionReviewLimitId(null)}>Cancel</Button>
                              <Button
                                size="sm" className="text-destructive"
                                disabled={applyRetention.isPending || !retentionCandidatesQuery.data || (!retentionCandidatesQuery.data.movies.length && !retentionCandidatesQuery.data.episodes.length)}
                                onClick={() => { if (confirm('Delete these items from the VOD Manager pool? This cannot be undone.')) applyRetention.mutate() }}
                              >
                                {applyRetention.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Trash2 size={12} className="mr-1" /> Delete listed items</>}
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
                <div className="border-t border-border pt-2 flex flex-wrap items-center gap-1.5">
                  <select
                    className={inputCls()}
                    value={dvrLimitForm.dispatcharr_user_id}
                    onChange={(e) => setDvrLimitForm({ ...dvrLimitForm, dispatcharr_user_id: e.target.value })}
                  >
                    <option value="">Person…</option>
                    {dispatcharrUsersQuery.data?.map((u) => <option key={u.id} value={u.id}>{u.username} (limit {u.stream_limit})</option>)}
                  </select>
                  <input
                    className={inputCls('w-32')}
                    type="number"
                    placeholder="Reserve streams"
                    value={dvrLimitForm.stream_reserve}
                    onChange={(e) => setDvrLimitForm({ ...dvrLimitForm, stream_reserve: e.target.value })}
                    title="Kept free for this person's own live TV watching -- subtracted from their real stream limit to get their DVR budget"
                  />
                  <input
                    className={inputCls('w-32')}
                    type="number"
                    placeholder="Disk quota (GB)"
                    value={dvrLimitForm.disk_quota_gb}
                    onChange={(e) => setDvrLimitForm({ ...dvrLimitForm, disk_quota_gb: e.target.value })}
                    title="Optional -- leave blank for no disk quota. Once their recordings' categories hold this much, new category placements for them are withheld (nothing existing is ever deleted)."
                  />
                  <input
                    className={inputCls('w-28')}
                    type="number"
                    placeholder="Max age (days)"
                    value={dvrLimitForm.retention_max_age_days}
                    onChange={(e) => setDvrLimitForm({ ...dvrLimitForm, retention_max_age_days: e.target.value })}
                    title="Optional -- flag their recordings older than this many days as eligible for removal via Review retention. Nothing is ever deleted automatically."
                  />
                  <input
                    className={inputCls('w-32')}
                    type="number"
                    placeholder="Max episodes/show"
                    value={dvrLimitForm.retention_max_episodes_per_show}
                    onChange={(e) => setDvrLimitForm({ ...dvrLimitForm, retention_max_episodes_per_show: e.target.value })}
                    title="Optional -- keep only this many most-recent episodes per show, flagging the rest as eligible for removal via Review retention."
                  />
                  <Button
                    size="sm"
                    disabled={!dvrLimitForm.dispatcharr_user_id || addDvrUserLimit.isPending}
                    onClick={() => addDvrUserLimit.mutate()}
                  >
                    {addDvrUserLimit.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Plus size={12} className="mr-1" /> Add</>}
                  </Button>
                </div>
                {dvrLimitError && <p className="text-xs text-destructive">{dvrLimitError}</p>}
              </SectionCard>

              <SectionCard title="Portal Access" icon={<ShieldCheck size={14} />}>
                <p className="text-sm text-muted-foreground">
                  Lets a person log into their own DVR portal -- schedule recordings, see their upcoming recordings and
                  usage, browse what they've recorded -- through a login that's separate from both this admin panel and
                  Dispatcharr itself, with mandatory authenticator-app MFA. Create one per person below; they'll pick up
                  from there with the username/password you set (and enroll MFA themselves on first login).
                </p>
                {portalAccountsQuery.data && !portalAccountsQuery.data.length && (
                  <p className="text-xs text-muted-foreground">No portal accounts yet.</p>
                )}
                <div className="space-y-1.5">
                  {portalAccountsQuery.data?.map((acct) => {
                    const liveUser = dispatcharrUsersQuery.data?.find((u) => u.id === acct.dispatcharr_user_id)
                    return (
                      <div key={acct.id} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                        <div className="flex-1 min-w-0 truncate">
                          <span className="font-semibold">{acct.username}</span>{' '}
                          <span className="text-muted-foreground">— {liveUser?.username ?? `user ${acct.dispatcharr_user_id}`}</span>
                        </div>
                        <StatusPill
                          label={acct.totp_enabled ? 'MFA enrolled' : 'MFA not set up'}
                          tone={acct.totp_enabled ? 'success' : 'warning'}
                        />
                        <Button
                          size="sm" variant="outline"
                          onClick={() => {
                            const pw = prompt(`New password for "${acct.username}":`)
                            if (pw) resetPortalAccountPassword.mutate({ id: acct.id, password: pw })
                          }}
                        >
                          Reset password
                        </Button>
                        {!!acct.totp_enabled && (
                          <Button
                            size="sm" variant="outline"
                            onClick={() => { if (confirm(`Reset MFA for "${acct.username}"? They'll need to re-enroll on their next login.`)) resetPortalAccountMfa.mutate(acct.id) }}
                          >
                            Reset MFA
                          </Button>
                        )}
                        <button
                          title="Delete this portal account"
                          className="text-muted-foreground hover:text-destructive p-1"
                          onClick={() => { if (confirm(`Delete portal account "${acct.username}"?`)) deletePortalAccount.mutate(acct.id) }}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    )
                  })}
                </div>
                <div className="border-t border-border pt-2 flex flex-wrap items-center gap-1.5">
                  <select
                    className={inputCls()}
                    value={portalAccountForm.dispatcharr_user_id}
                    onChange={(e) => setPortalAccountForm({ ...portalAccountForm, dispatcharr_user_id: e.target.value })}
                  >
                    <option value="">Person…</option>
                    {dispatcharrUsersQuery.data?.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
                  </select>
                  <input
                    className={inputCls('w-32')}
                    placeholder="Portal username"
                    value={portalAccountForm.username}
                    onChange={(e) => setPortalAccountForm({ ...portalAccountForm, username: e.target.value })}
                  />
                  <input
                    className={inputCls('w-32')}
                    type="password"
                    placeholder="Initial password"
                    value={portalAccountForm.password}
                    onChange={(e) => setPortalAccountForm({ ...portalAccountForm, password: e.target.value })}
                  />
                  <Button
                    size="sm"
                    disabled={!portalAccountForm.dispatcharr_user_id || !portalAccountForm.username || !portalAccountForm.password || createPortalAccount.isPending}
                    onClick={() => createPortalAccount.mutate()}
                  >
                    {createPortalAccount.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Plus size={12} className="mr-1" /> Add</>}
                  </Button>
                </div>
                {portalAccountError && <p className="text-xs text-destructive">{portalAccountError}</p>}
              </SectionCard>
              </>
            )}

            {dvrSubTab === 'library' && (
              <SectionCard title="DVR Library" icon={<HardDriveDownload size={14} />}>
                <p className="text-sm text-muted-foreground">
                  Everything this provider has recorded and imported into the pool. Deleting here only removes this
                  provider's own copy -- if the same title is also sourced from somewhere else, that copy is
                  untouched.
                </p>
                <div className="flex items-center gap-0.5 rounded border border-border p-0.5 w-fit">
                  {(['movies', 'series'] as const).map((t) => (
                    <button
                      key={t}
                      onClick={() => setDvrLibraryTab(t)}
                      className={`px-2.5 py-1 rounded text-xs transition-colors capitalize ${
                        dvrLibraryTab === t ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                      }`}
                    >
                      {t}
                    </button>
                  ))}
                </div>

                {dvrLibraryTab === 'movies' ? (
                  <div className="space-y-1">
                    {dvrLibraryMoviesQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
                    {dvrLibraryMoviesQuery.data && !dvrLibraryMoviesQuery.data.items.length && (
                      <p className="text-xs text-muted-foreground">No movies from this provider yet.</p>
                    )}
                    {dvrLibraryMoviesQuery.data?.total != null && dvrLibraryMoviesQuery.data.total > dvrLibraryMoviesQuery.data.items.length && (
                      <p className="text-[11px] text-muted-foreground">Showing {dvrLibraryMoviesQuery.data.items.length} of {dvrLibraryMoviesQuery.data.total}.</p>
                    )}
                    {dvrLibraryMoviesQuery.data?.items.map((m) => {
                      const src = m.sources.find((s) => s.provider_id === recordingProfilesProviderId)
                      const ext = src?.container_extension || 'mp4'
                      return (
                        <div key={m.id} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                          {src && (
                            <PlayButton
                              url={buildPreviewSourceUrl('movie', src.id, ext, xcCredentialsQuery.data)}
                              transcodedUrl={buildTranscodedPreviewSourceUrl('movie', src.id, xcCredentialsQuery.data)}
                              hlsUrl={buildHlsPreviewSourceUrl('movie', src.id, xcCredentialsQuery.data)}
                              title={m.name}
                            />
                          )}
                          <span className="flex-1 truncate">
                            <span className="font-semibold">{m.name}</span>{' '}
                            <span className="text-muted-foreground">{m.year ? `(${m.year})` : ''}</span>
                          </span>
                          {src && <Chip>{ext.toUpperCase()}{src.file_size_bytes != null && ` · ${formatFileSize(src.file_size_bytes)}`}</Chip>}
                          {m.sources.length > 1 && <Chip>+{m.sources.length - 1} other source{m.sources.length > 2 ? 's' : ''}</Chip>}
                          <button
                            title="Remove this provider's copy from the pool"
                            className="text-muted-foreground hover:text-destructive p-1"
                            disabled={!src || deleteDvrMovieSource.isPending}
                            onClick={() => { if (src && confirm(`Remove "${m.name}" from the DVR pool? This can't be undone.`)) deleteDvrMovieSource.mutate({ movieId: m.id, sourceId: src.id }) }}
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <div className="space-y-2">
                    {dvrLibrarySeriesQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
                    {(() => {
                      const groups = (dvrLibrarySeriesQuery.data?.items ?? [])
                        .map((s) => ({
                          series: s,
                          rows: s.episodes.flatMap((e) => e.sources.filter((src) => src.provider_id === recordingProfilesProviderId).map((src) => ({ episode: e, source: src }))),
                        }))
                        .filter((g) => g.rows.length > 0)
                      if (dvrLibrarySeriesQuery.data && !groups.length) {
                        return <p className="text-xs text-muted-foreground">No episodes from this provider yet.</p>
                      }
                      return groups.map(({ series, rows }) => (
                        <div key={series.id} className="space-y-1">
                          {rows.map(({ episode, source }) => {
                            const ext = source.container_extension || 'mp4'
                            return (
                              <div key={`${episode.id}-${source.id}`} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                                <PlayButton
                                  url={buildPreviewSourceUrl('series', source.id, ext, xcCredentialsQuery.data)}
                                  transcodedUrl={buildTranscodedPreviewSourceUrl('series', source.id, xcCredentialsQuery.data)}
                                  hlsUrl={buildHlsPreviewSourceUrl('series', source.id, xcCredentialsQuery.data)}
                                  title={`${series.name} S${episode.season_number}E${episode.episode_number}`}
                                />
                                <span className="flex-1 truncate">
                                  <span className="font-semibold">{series.name}</span>{' '}
                                  <span className="text-muted-foreground">
                                    S{episode.season_number}E{episode.episode_number} — {episode.name}
                                  </span>
                                </span>
                                <Chip>{ext.toUpperCase()}{source.file_size_bytes != null && ` · ${formatFileSize(source.file_size_bytes)}`}</Chip>
                                {episode.sources.length > 1 && <Chip>+{episode.sources.length - 1} other source{episode.sources.length > 2 ? 's' : ''}</Chip>}
                                <button
                                  title="Remove this provider's copy from the pool"
                                  className="text-muted-foreground hover:text-destructive p-1"
                                  disabled={deleteDvrEpisodeSource.isPending}
                                  onClick={() => { if (confirm(`Remove "${series.name}" S${episode.season_number}E${episode.episode_number} from the DVR pool? This can't be undone.`)) deleteDvrEpisodeSource.mutate({ episodeId: episode.id, sourceId: source.id }) }}
                                >
                                  <Trash2 size={12} />
                                </button>
                              </div>
                            )
                          })}
                          {series.tmdb_id && (
                            <button
                              className="text-[10px] text-muted-foreground hover:text-foreground flex items-center gap-1 pl-1"
                              onClick={() => setExpandedMissingSeriesId(expandedMissingSeriesId === series.id ? null : series.id)}
                            >
                              {expandedMissingSeriesId === series.id ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                              Missing episodes
                            </button>
                          )}
                          {expandedMissingSeriesId === series.id && recordingProfilesProviderId != null && (
                            <MissingEpisodesPanel series={series} providerId={recordingProfilesProviderId} qc={qc} />
                          )}
                        </div>
                      ))
                    })()}
                  </div>
                )}
              </SectionCard>
            )}

            {dvrSubTab === 'missing' && (
              <SectionCard title="Missing Episodes" icon={<Search size={14} />}>
                <p className="text-sm text-muted-foreground">
                  Every gap across every monitored show with an active recording rule -- not just ones this provider
                  has already recorded an episode for. Unmonitor a rule (the eye icon under Scheduled) to keep a show
                  off this list without cancelling its recordings. Find runs the same cascade everywhere: pool
                  backfill first, then this show's own known channel, then a cross-channel search, then flags it for
                  review.
                </p>
                {(() => {
                  const monitoredRules = (recordingProfilesQuery.data ?? []).filter((rp) => rp.monitored)
                  if (recordingProfilesQuery.data && !recordingProfilesQuery.data.length) {
                    return <p className="text-xs text-muted-foreground">No recording rules yet -- add one under Scheduled first.</p>
                  }
                  if (recordingProfilesQuery.data && !monitoredRules.length) {
                    return <p className="text-xs text-muted-foreground">Every rule is unmonitored -- toggle one back on under Scheduled to see its gaps here.</p>
                  }
                  return (
                    <div className="space-y-2">
                      {monitoredRules.map((rp) => (
                        recordingProfilesProviderId != null && (
                          <RuleMissingBlock key={rp.id} rule={rp} providerId={recordingProfilesProviderId} qc={qc} />
                        )
                      ))}
                    </div>
                  )
                })()}
              </SectionCard>
            )}

            {dvrSubTab === 'metrics' && (
              <>
                <SectionCard title="People" icon={<Users size={14} />}>
                  <p className="text-sm text-muted-foreground">
                    Storage and real watch activity per person, from Dispatcharr's own live connection stats
                    (confirmed live it carries a real per-person identity, polled into history in the background).
                  </p>
                  {dvrUserLimitsQuery.data && !dvrUserLimitsQuery.data.length && (
                    <p className="text-xs text-muted-foreground">No one has DVR limits configured under Users yet -- nothing to show metrics for.</p>
                  )}
                  <div className="space-y-1">
                    {dvrUserLimitsQuery.data?.map((lim) => {
                      const usage = dvrUserUsageQuery.data?.[lim.id]
                      const usageGB = usage != null ? (usage.total_bytes / 1024 ** 3) : null
                      const virtualGB = usage != null ? (usage.virtual_bytes / 1024 ** 3) : null
                      const ruleCount = recordingProfilesQuery.data?.filter((rp) => rp.dispatcharr_user_id === lim.dispatcharr_user_id).length ?? 0
                      const sessions = (watchSessionsQuery.data ?? []).filter((w) => w.dispatcharr_user_id === lim.dispatcharr_user_id)
                      const lastSeen = sessions.length ? Math.max(...sessions.map((s) => Number(s.last_seen_at))) : null
                      return (
                        <div key={lim.id} className="flex items-center gap-1.5 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                          <span className="flex-1">
                            <span className="font-semibold">{lim.dispatcharr_username}</span>{' '}
                            <span className="text-muted-foreground">
                              — {usageGB != null ? `${usageGB.toFixed(1)}GB used` : 'usage unknown'}
                              {virtualGB != null && virtualGB > 0.01 && ` (${virtualGB.toFixed(1)}GB virtual)`}
                              {' · '}{ruleCount} recording rule{ruleCount === 1 ? '' : 's'}
                              {' · '}{sessions.length} watch session{sessions.length === 1 ? '' : 's'} logged
                              {lastSeen != null && <> · last watched {new Date(lastSeen * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}</>}
                            </span>
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </SectionCard>

                <SectionCard title="Recording Load by Channel" icon={<CalendarClock size={14} />}>
                  <p className="text-sm text-muted-foreground">How many active recording rules target each channel -- useful for spotting one channel carrying most of the load, or a rule pointed at the wrong channel.</p>
                  {(() => {
                    const byChannel = new Map<number, number>()
                    for (const rp of recordingProfilesQuery.data ?? []) {
                      if (!rp.channel_id) continue
                      byChannel.set(rp.channel_id, (byChannel.get(rp.channel_id) ?? 0) + 1)
                    }
                    const rows = [...byChannel.entries()].sort((a, b) => b[1] - a[1])
                    if (!rows.length) return <p className="text-xs text-muted-foreground">No channel-scoped recording rules yet.</p>
                    return (
                      <div className="space-y-1">
                        {rows.map(([channelId, count]) => (
                          <div key={channelId} className="flex items-center gap-1.5 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                            <span className="flex-1">channel {channelId}</span>
                            <Chip>{count} rule{count === 1 ? '' : 's'}</Chip>
                          </div>
                        ))}
                      </div>
                    )
                  })()}
                </SectionCard>

                <SectionCard title="Rule Health" icon={<CalendarDays size={14} />}>
                  <p className="text-sm text-muted-foreground">
                    On-demand check only -- re-searches each rule's own channel right now. 0 matches means either a
                    genuine hiatus/break or something worth a closer look, not necessarily broken.
                  </p>
                  {recordingProfilesQuery.data && !recordingProfilesQuery.data.length && (
                    <p className="text-xs text-muted-foreground">No recording rules yet.</p>
                  )}
                  <div className="space-y-1">
                    {recordingProfilesQuery.data?.map((rp) => {
                      const health = ruleHealth[rp.id]
                      return (
                        <div key={rp.id} className="flex items-center gap-1.5 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                          <span className="flex-1 truncate">
                            <span className="font-semibold">{rp.label}</span>{' '}
                            <span className="text-muted-foreground">— "{rp.title}"</span>
                          </span>
                          {health && !health.checking && (
                            health.matches < 0
                              ? <StatusPill tone="destructive" label="check failed" />
                              : health.matches === 0
                                ? <StatusPill tone="warning" label="no upcoming matches" />
                                : <StatusPill tone="success" label={`${health.matches} upcoming match${health.matches === 1 ? '' : 'es'}`} />
                          )}
                          <Button size="sm" variant="outline" disabled={!rp.channel_id || health?.checking} onClick={() => checkRuleHealth(rp)}>
                            {health?.checking ? <Loader2 size={12} className="animate-spin" /> : 'Check'}
                          </Button>
                        </div>
                      )
                    })}
                  </div>
                </SectionCard>

                <SectionCard title="Unresolved Missing Episodes" icon={<Search size={14} />}>
                  <p className="text-sm text-muted-foreground">
                    Episodes the DVR Library's "Find" action couldn't locate anywhere -- not already in the pool, and
                    no EPG airing found either. Worth checking manually against Plex/Emby/Jellyfin if configured.
                  </p>
                  {unresolvedMissingEpisodesQuery.data && !unresolvedMissingEpisodesQuery.data.length && (
                    <p className="text-xs text-muted-foreground">Nothing flagged.</p>
                  )}
                  <div className="space-y-1">
                    {unresolvedMissingEpisodesQuery.data?.map((u) => (
                      <div key={u.id} className="flex items-center gap-1.5 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                        <span className="flex-1 truncate">
                          <span className="font-semibold">{u.series_name}</span>{' '}
                          <span className="text-muted-foreground">
                            S{u.season_number}E{u.episode_number}{u.episode_name ? ` — ${u.episode_name}` : ''}
                          </span>
                        </span>
                        <Chip>flagged {new Date(Number(u.checked_at) * 1000).toLocaleDateString()}</Chip>
                      </div>
                    ))}
                  </div>
                </SectionCard>

                <SectionCard title="Failed Recording Replacements" icon={<CalendarClock size={14} />}>
                  <p className="text-sm text-muted-foreground">
                    Recordings Dispatcharr scheduled and attempted but that genuinely failed -- Dispatcharr never
                    retries these on its own. VOD Manager looks for the same episode's next airing on any channel in
                    that person's own lineup and reschedules it there automatically; "unresolved" is retried every
                    poll cycle rather than given up on.
                  </p>
                  {dvrRecordingFailuresQuery.data && !dvrRecordingFailuresQuery.data.length && (
                    <p className="text-xs text-muted-foreground">None detected.</p>
                  )}
                  <div className="space-y-1">
                    {dvrRecordingFailuresQuery.data?.map((f) => (
                      <div key={f.id} className="flex items-center gap-1.5 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                        <span className="flex-1 truncate">
                          <span className="font-semibold">{f.title}</span>{' '}
                          {f.season_number != null && f.episode_number != null && (
                            <span className="text-muted-foreground">S{f.season_number}E{f.episode_number}</span>
                          )}{' '}
                          <span className="text-muted-foreground">— was channel {f.original_channel_id ?? '?'}</span>
                        </span>
                        {f.outcome === 'rescheduled' ? (
                          <StatusPill label={`rescheduled -> channel ${f.replacement_channel_id}`} tone="success" />
                        ) : (
                          <StatusPill label="unresolved" tone="warning" />
                        )}
                        <Chip>detected {new Date(Number(f.detected_at) * 1000).toLocaleDateString()}</Chip>
                      </div>
                    ))}
                  </div>
                </SectionCard>
              </>
            )}
          </>
        )}
      </>
      )}

      {activeTab === 'curation' && (
      <>
      <SectionCard title="Orphan Checker" icon={<Trash2 size={14} />}>
        <p className="text-xs text-muted-foreground">
          Finds dead rows a provider deletion (or a bug) can leave behind: series whose only source provider no
          longer exists, and movies/episodes with zero sources at all. Doesn't flag series with no episodes yet —
          that's normal for anything not yet lazily enriched, not broken.
        </p>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="outline" disabled={orphansQuery.isFetching} onClick={() => orphansQuery.refetch()}>
            {orphansQuery.isFetching ? <Loader2 size={12} className="animate-spin mr-1" /> : <RefreshCw size={12} className="mr-1" />}
            Scan
          </Button>
          {!!orphansQuery.data && (
            <>
              {(() => {
                const total = orphansQuery.data.orphaned_series.count + orphansQuery.data.sourceless_movies.count + orphansQuery.data.sourceless_episodes.count
                return total === 0
                  ? <span className="text-xs text-muted-foreground">Clean — nothing found.</span>
                  : (
                    <Button
                      size="sm" variant="outline" className="text-destructive" disabled={purgeOrphans.isPending}
                      onClick={() => { if (confirm(`Delete ${total} orphaned/sourceless row(s)? This can't be undone.`)) purgeOrphans.mutate() }}
                    >
                      {purgeOrphans.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : <Trash2 size={12} className="mr-1" />}
                      Delete {total} orphan{total === 1 ? '' : 's'}
                    </Button>
                  )
              })()}
            </>
          )}
        </div>
        {!!orphansQuery.data && (
          <div className="space-y-1.5">
            <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-sm">
              <StatusPill tone={orphansQuery.data.orphaned_series.count ? 'warning' : 'success'} label="Orphaned series" />
              <span className="flex-1 text-muted-foreground truncate">
                {orphansQuery.data.orphaned_series.count} -- dead provider reference
                {!!orphansQuery.data.orphaned_series.sample.length && ` · e.g. ${orphansQuery.data.orphaned_series.sample.slice(0, 5).map((s) => s.name).join(', ')}`}
              </span>
            </div>
            <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-sm">
              <StatusPill tone={orphansQuery.data.sourceless_movies.count ? 'warning' : 'success'} label="Sourceless movies" />
              <span className="flex-1 text-muted-foreground truncate">
                {orphansQuery.data.sourceless_movies.count}
                {!!orphansQuery.data.sourceless_movies.sample.length && ` -- e.g. ${orphansQuery.data.sourceless_movies.sample.slice(0, 5).map((s) => s.name).join(', ')}`}
              </span>
            </div>
            <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-sm">
              <StatusPill tone={orphansQuery.data.sourceless_episodes.count ? 'warning' : 'success'} label="Sourceless episodes" />
              <span className="flex-1 text-muted-foreground truncate">{orphansQuery.data.sourceless_episodes.count} -- in otherwise-healthy series</span>
            </div>
          </div>
        )}
      </SectionCard>

      <SectionCard title="Duplicate Finder" icon={<Copy size={14} />}>
        <p className="text-xs text-muted-foreground">
          Finds pool entries that look like the same real title split into two rows: names that only differ by
          cosmetic punctuation (a colon, a dash, quote style), or the same name with years one apart (a provider
          mislabeling a release year). A shared TMDB id confirms a match even across a bigger year gap; a
          conflicting TMDB id rules a pair out entirely. Pick which candidate to keep for each group — the rest
          merge into it (sources, categories, and episodes move over, nothing is lost) — or Ignore a group that
          isn't actually a duplicate so it stops resurfacing.
        </p>
        <div className="flex items-center gap-1.5">
          <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
            <button
              className={`px-2 py-0.5 rounded text-xs ${duplicatesContentType === 'movie' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              onClick={() => { setDuplicatesContentType('movie'); setDuplicatesOffset(0); setDuplicatesConfirmJobId(null) }}
            >
              Movies
            </button>
            <button
              className={`px-2 py-0.5 rounded text-xs ${duplicatesContentType === 'series' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              onClick={() => { setDuplicatesContentType('series'); setDuplicatesOffset(0); setDuplicatesConfirmJobId(null) }}
            >
              TV Shows
            </button>
          </div>
          <Button size="sm" variant="outline" disabled={duplicatesQuery.isFetching} onClick={() => { setDuplicatesOffset(0); setDuplicatesConfirmJobId(null); duplicatesQuery.refetch() }}>
            {duplicatesQuery.isFetching ? <Loader2 size={12} className="animate-spin mr-1" /> : <RefreshCw size={12} className="mr-1" />}
            Scan
          </Button>
          {duplicatesQuery.data && duplicatesQuery.data.length === 0 && (
            <span className="text-xs text-muted-foreground">Clean — nothing found.</span>
          )}
          {!!duplicatesNeedsReview.length && (
            <>
              <span className="text-xs text-muted-foreground">{duplicatesNeedsReview.length} group{duplicatesNeedsReview.length === 1 ? '' : 's'} need review</span>
              <Pager total={duplicatesNeedsReview.length} limit={DUPLICATES_PAGE_SIZE} offset={duplicatesOffset} onOffset={setDuplicatesOffset} />
            </>
          )}
          {duplicatesMergeResult && <span className="text-xs text-destructive">{duplicatesMergeResult}</span>}
        </div>
        {!!duplicatesQuery.data?.length && (
          <div className="flex items-center gap-1.5 rounded border border-green-500/30 bg-green-500/5 px-2 py-1.5">
            {!duplicatesConfirmJobId ? (
              <>
                <span className="text-xs text-muted-foreground">Check every group in this scan against TMDB to find airtight matches (shared tmdb_id + exact title match) that can merge without a manual pick.</span>
                <Button size="sm" variant="outline" disabled={startConfirmScan.isPending} onClick={() => startConfirmScan.mutate()}>
                  {startConfirmScan.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
                  Check TMDB-confirmed matches
                </Button>
              </>
            ) : confirmScanQuery.data?.status === 'running' || !confirmScanQuery.data ? (
              <>
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <Loader2 size={12} className="animate-spin" />
                  Checking {confirmScanQuery.data?.checked ?? 0}/{confirmScanQuery.data?.total ?? '…'} against TMDB…
                </span>
                <Button size="sm" variant="outline" onClick={() => { cancelConfirmScan.mutate(); setDuplicatesConfirmJobId(null) }}>
                  Cancel
                </Button>
              </>
            ) : confirmScanQuery.data.status === 'error' ? (
              <span className="text-xs text-destructive">TMDB check failed: {confirmScanQuery.data.error}</span>
            ) : confirmScanQuery.data.status === 'cancelled' ? (
              <span className="text-xs text-muted-foreground">Cancelled.</span>
            ) : (
              <>
                <span className="text-xs text-green-400">✓ {duplicatesConfirmed.length} TMDB-confirmed match{duplicatesConfirmed.length === 1 ? '' : 'es'} found (of {confirmScanQuery.data.total} candidate{confirmScanQuery.data.total === 1 ? '' : 's'} checked)</span>
                <Button
                  size="sm"
                  disabled={!duplicatesConfirmed.length || mergeConfirmedDuplicates.isPending}
                  onClick={() => mergeConfirmedDuplicates.mutate()}
                  title="Every candidate here shares a confirmed TMDB id, and one name matches TMDB's own title exactly -- no manual pick needed"
                >
                  {mergeConfirmedDuplicates.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
                  Merge all confirmed matches ({duplicatesConfirmed.length})
                </Button>
              </>
            )}
            {duplicatesConfirmMergeResult && <span className="text-xs text-muted-foreground">{duplicatesConfirmMergeResult}</span>}
          </div>
        )}
        {!!duplicatesPageItems.length && (
          // Client-side slice, not a second network round-trip -- the scan
          // already walked the whole pool in one query; thousands of groups
          // in the DOM at once (not just in memory) is what actually made
          // the page unusably slow, so only render one page's worth.
          <div className="text-xs space-y-1.5">
            {duplicatesPageItems.map((group) => (
              <DuplicateGroupRow
                key={group.items.map((i) => i.id).join('-')}
                group={group}
                contentType={duplicatesContentType}
                xcCredentials={xcCredentialsQuery.data}
                isPending={mergeDuplicateGroup.isPending}
                onMerge={(keepId, mergeIds) => mergeDuplicateGroup.mutate({ keep_id: keepId, merge_ids: mergeIds })}
                onIgnore={(itemIds) => ignoreDuplicateGroup.mutate(itemIds)}
                isIgnorePending={ignoreDuplicateGroup.isPending}
                tmdbDetails={duplicatesTmdbDetailsQuery.data}
              />
            ))}
          </div>
        )}
      </SectionCard>

      <SectionCard title="TMDB Lists" icon={<RefreshCw size={14} />}>
        <p className="text-xs text-muted-foreground">
          Auto-populate categories from a public TMDB List (movie + TV watchlists). A list can hold both movies and
          shows, so each one gets a paired movie category and series category — kept separate because Dispatcharr's
          movie and TV catalogs are different endpoints.
        </p>
        {tmdbGroups.length === 0 && <p className="text-xs text-muted-foreground">No TMDB lists linked yet.</p>}
        <div className="space-y-2">
          {tmdbGroups.map((g) => (
            <div key={g.sync_source} className="rounded border border-border/50 p-2 text-xs space-y-1">
              <p className="text-muted-foreground">List ID: {g.sync_source.replace('tmdb_list:', '')}</p>
              {g.categories.map((c) => (
                <div key={c.id} className={`flex items-center justify-between gap-2 ${!c.is_active ? 'opacity-50' : ''}`}>
                  <span className="flex items-center gap-1 min-w-0">
                    <input
                      className={inputCls('w-40')}
                      defaultValue={c.name}
                      key={c.name}
                      title="Rename category"
                      onBlur={(e) => {
                        const v = e.target.value.trim()
                        if (v && v !== c.name) renameCategory.mutate({ id: c.id, name: v })
                      }}
                    />
                    <span className="text-muted-foreground">({c.content_type === 'movie' ? 'Movies' : 'TV Shows'})</span>
                  </span>
                  <span className="flex items-center gap-1.5">
                    <input
                      className={inputCls('w-12')}
                      type="number"
                      title="Sort order (lower shows first in Dispatcharr)"
                      defaultValue={c.sort_order}
                      key={c.sort_order}
                      onBlur={(e) => {
                        const v = Number(e.target.value) || 0
                        if (v !== c.sort_order) setCategorySortOrder.mutate({ id: c.id, sort_order: v })
                      }}
                    />
                    <button title="Sync from TMDB now" className="text-muted-foreground hover:text-foreground" disabled={syncCategoryNow.isPending} onClick={() => syncCategoryNow.mutate(c.id)}>
                      <RefreshCw size={12} />
                    </button>
                    <button
                      title={c.content_type === 'movie' ? 'View movies in this category' : 'View series in this category'}
                      className={(c.content_type === 'movie' ? movieCategoryFilter : seriesCategoryFilter) === c.id ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}
                      onClick={() => {
                        if (c.content_type === 'movie') { setMovieCategoryFilter(c.id); setMovieSearch(''); setMovieOffset(0) }
                        else { setSeriesCategoryFilter(c.id); setSeriesSearch(''); setSeriesOffset(0) }
                      }}
                    >
                      <Eye size={12} />
                    </button>
                    <button
                      title={c.is_active ? 'Disable — stops exporting to Dispatcharr, keeps everything for later (e.g. a seasonal category)' : 'Enable — resumes exporting to Dispatcharr'}
                      className={c.is_active ? 'text-muted-foreground hover:text-foreground' : 'text-amber-500 hover:text-foreground'}
                      disabled={setCategoryActive2.isPending}
                      onClick={() => setCategoryActive2.mutate({ id: c.id, is_active: !c.is_active })}
                    >
                      {c.is_active ? <Power size={12} /> : <PowerOff size={12} />}
                    </button>
                    <button
                      title={c.schedule_start_mmdd ? `Annual schedule: enable ${c.schedule_start_mmdd} → disable ${c.schedule_end_mmdd}` : 'Set an annual enable/disable schedule (e.g. a seasonal category)'}
                      className={c.schedule_start_mmdd ? 'text-primary hover:text-foreground' : 'text-muted-foreground hover:text-foreground'}
                      disabled={setCategorySchedule2.isPending}
                      onClick={() => promptSchedule2(c)}
                    >
                      <CalendarClock size={12} />
                    </button>
                    <button title="Delete category" className="text-muted-foreground hover:text-destructive" onClick={() => { if (confirm(`Delete category "${c.name}"? Items stay in the pool, just unplaced from this category.`)) deleteCategory.mutate(c.id) }}>
                      <Trash2 size={12} />
                    </button>
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
        {categoryScheduleError2 && <p className="text-xs text-destructive">{categoryScheduleError2}</p>}
        {categoryActiveError2 && <p className="text-xs text-destructive">{categoryActiveError2}</p>}
        {tmdbSyncResult && <p className="text-xs text-muted-foreground">{tmdbSyncResult}</p>}
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <input className={inputCls('w-28')} placeholder="TMDB List ID" value={tmdbListForm.list_id} onChange={(e) => setTmdbListForm({ ...tmdbListForm, list_id: e.target.value })} />
          <input
            className={inputCls()}
            placeholder={`Name template, e.g. "My ${TMDB_TOKEN} Picks"`}
            title={`Use ${TMDB_TOKEN} where the type name should be inserted. No ${TMDB_TOKEN}? We'll append " — <type>" to the end instead.`}
            value={tmdbListForm.name_template}
            onChange={(e) => setTmdbListForm({ ...tmdbListForm, name_template: e.target.value })}
          />
          <input className={inputCls('w-24')} placeholder="Movie label" value={tmdbListForm.movie_label} onChange={(e) => setTmdbListForm({ ...tmdbListForm, movie_label: e.target.value })} />
          <input className={inputCls('w-24')} placeholder="TV label" value={tmdbListForm.tv_label} onChange={(e) => setTmdbListForm({ ...tmdbListForm, tv_label: e.target.value })} />
          <Button size="sm" disabled={!tmdbListForm.list_id || !tmdbListForm.name_template || addTmdbList.isPending} onClick={() => addTmdbList.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
        {!!tmdbListForm.name_template && (
          <p className="text-xs text-muted-foreground">
            Will create "{buildTmdbPairName(tmdbListForm.name_template, tmdbListForm.movie_label || 'Movies')}" and "{buildTmdbPairName(tmdbListForm.name_template, tmdbListForm.tv_label || 'TV Shows')}".
          </p>
        )}
      </SectionCard>
      </>
      )}

      {activeTab === 'movies' && (
      <>
      <SectionCard title="Movies" icon={<Film size={14} />}>
        <div className="flex items-center gap-1.5 flex-wrap">
          <input
            className={inputCls('w-64')}
            placeholder="Search movies…"
            value={movieSearch}
            onChange={(e) => { setMovieSearch(e.target.value); setMovieOffset(0) }}
          />
          <select
            className={inputCls()}
            value={movieProviderFilter ?? ''}
            onChange={(e) => { setMovieProviderFilter(e.target.value ? Number(e.target.value) : null); setMovieOffset(0) }}
          >
            <option value="">All providers</option>
            {(providersQuery.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <Button
            size="sm"
            variant={movieShowArchived ? 'default' : 'outline'}
            onClick={() => { setMovieShowArchived((v) => !v); setMovieOffset(0) }}
            title={movieShowArchived ? 'Showing archived movies — click to return to the active pool' : 'Show only archived movies'}
          >
            <Archive size={12} className="mr-1" />Archived
          </Button>
          {movieCategoryFilter != null && (
            <span className="flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
              Viewing: {movieCategories.find((c) => c.id === movieCategoryFilter)?.name ?? movieCategoryFilter}
              <button title="Clear category filter" onClick={() => { setMovieCategoryFilter(null); setMovieOffset(0) }}>
                <X size={12} />
              </button>
            </span>
          )}
          {moviesQuery.data && <Pager total={moviesQuery.data.total} limit={MOVIE_LIMIT} offset={movieOffset} onOffset={setMovieOffset} />}
          <PageSizeSelect value={MOVIE_LIMIT} onChange={setMovieLimit} />
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <Button size="sm" variant="outline" onClick={() => setCategoriesModalOpen('movie')}>Manage Categories</Button>
          <Button size="sm" variant="outline" onClick={() => setNeedsReviewModalOpen('movie')}>
            Needs Review{needsReviewQuery.data?.movies.length ? ` (${needsReviewQuery.data.movies.length})` : ''}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setMissingArtworkModalOpen('movie')}>
            Missing Artwork{missingArtworkCountsQuery.data?.movies ? ` (${missingArtworkCountsQuery.data.movies})` : ''}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setLibraryLanguageModalOpen('movie')}>
            Language Filter
          </Button>
          <div className="flex items-center gap-0.5 rounded border border-border p-0.5 ml-auto">
            <button
              title="List view"
              className={`flex items-center p-1 rounded ${movieViewMode === 'list' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}`}
              onClick={() => setMovieViewMode('list')}
            >
              <List size={12} />
            </button>
            <button
              title="Grid view"
              className={`flex items-center p-1 rounded ${movieViewMode === 'grid' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}`}
              onClick={() => setMovieViewMode('grid')}
            >
              <LayoutGrid size={12} />
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 rounded border border-border/50 bg-muted/30 px-2 py-1.5">
          <span className="text-xs text-muted-foreground">{selectedMovieIds.size} selected · shift-click to select a range</span>
          <button
            className="text-xs text-muted-foreground hover:text-foreground underline decoration-dotted"
            onClick={() => setSelectedMovieIds(new Set((moviesQuery.data?.items ?? []).map((m) => m.id)))}
          >
            Select all visible ({moviesQuery.data?.items.length ?? 0})
          </button>
          <button
            className="text-xs text-muted-foreground hover:text-foreground underline decoration-dotted"
            onClick={() => setSelectedMovieIds(new Set())}
          >
            Clear
          </button>
          <select className={inputCls()} value={bulkMovieTargetCategory} onChange={(e) => setBulkMovieTargetCategory(e.target.value)}>
            <option value="">Place in category…</option>
            {movieCategories.filter((c) => !c.is_smart).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <Button
            size="sm"
            variant="outline"
            disabled={!bulkMovieTargetCategory || selectedMovieIds.size === 0 || bulkPlaceMovies.isPending}
            onClick={() => bulkPlaceMovies.mutate({ category_id: Number(bulkMovieTargetCategory), ids: Array.from(selectedMovieIds) })}
          >
            Place selected ({selectedMovieIds.size})
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!bulkMovieTargetCategory || !moviesQuery.data?.total || bulkPlaceMovies.isPending}
            onClick={() => bulkPlaceMovies.mutate({
              category_id: Number(bulkMovieTargetCategory),
              search: movieSearch || undefined,
              source_category_id: movieCategoryFilter ?? undefined,
              source_provider_id: movieProviderFilter ?? undefined,
            })}
            title="Places every movie matching the current search/category filter, not just this page"
          >
            Place all filtered ({moviesQuery.data?.total ?? 0})
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={selectedMovieIds.size === 0 || bulkArchiveMovies.isPending}
            onClick={() => bulkArchiveMovies.mutate({ ids: Array.from(selectedMovieIds), archived: !movieShowArchived })}
          >
            {movieShowArchived ? 'Un-archive' : 'Archive'} selected ({selectedMovieIds.size})
          </Button>
          {bulkMovieResult && <span className="text-xs text-muted-foreground">{bulkMovieResult}</span>}
        </div>
        <div className={movieViewMode === 'grid' ? 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 gap-2' : 'space-y-2'}>
          {moviesQuery.isFetching && <p className="text-xs text-muted-foreground">Loading…</p>}
          {moviesQuery.data?.items.map((m, i) => (
            <MovieRow
              key={m.id}
              movie={m}
              movieCategories={movieCategories}
              providers={providersQuery.data ?? []}
              qc={qc}
              xcCredentials={xcCredentialsQuery.data}
              selected={selectedMovieIds.has(m.id)}
              onToggleSelect={(shiftKey) => toggleMovieSelected(m.id, i, shiftKey)}
              mode={movieViewMode}
              onToggleArchived={() => toggleMovieArchived.mutate({ id: m.id, archived: !m.review_excluded })}
            />
          ))}
        </div>
        {moviesQuery.data && (
          <div className="flex items-center gap-1.5">
            <Pager total={moviesQuery.data.total} limit={MOVIE_LIMIT} offset={movieOffset} onOffset={setMovieOffset} />
            <PageSizeSelect value={MOVIE_LIMIT} onChange={setMovieLimit} />
          </div>
        )}
        <div className="flex items-center gap-1.5 pt-1">
          <input className={inputCls()} placeholder="Movie name" value={movieForm.name} onChange={(e) => setMovieForm({ ...movieForm, name: e.target.value })} />
          <input className={inputCls('w-20')} type="number" placeholder="Year" value={movieForm.year} onChange={(e) => setMovieForm({ ...movieForm, year: e.target.value })} />
          <Button size="sm" disabled={!movieForm.name || addMovie.isPending} onClick={() => addMovie.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
      </SectionCard>
      </>
      )}

      {activeTab === 'series' && (
      <>
      <SectionCard title="TV Shows" icon={<Tv size={14} />}>
        <div className="flex items-center gap-1.5 flex-wrap">
          <input
            className={inputCls('w-64')}
            placeholder="Search series…"
            value={seriesSearch}
            onChange={(e) => { setSeriesSearch(e.target.value); setSeriesOffset(0) }}
          />
          <select
            className={inputCls()}
            value={seriesProviderFilter ?? ''}
            onChange={(e) => { setSeriesProviderFilter(e.target.value ? Number(e.target.value) : null); setSeriesOffset(0) }}
          >
            <option value="">All providers</option>
            {(providersQuery.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <Button
            size="sm"
            variant={seriesShowArchived ? 'default' : 'outline'}
            onClick={() => { setSeriesShowArchived((v) => !v); setSeriesOffset(0) }}
            title={seriesShowArchived ? 'Showing archived series — click to return to the active pool' : 'Show only archived series'}
          >
            <Archive size={12} className="mr-1" />Archived
          </Button>
          {seriesCategoryFilter != null && (
            <span className="flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
              Viewing: {seriesCategories.find((c) => c.id === seriesCategoryFilter)?.name ?? seriesCategoryFilter}
              <button title="Clear category filter" onClick={() => { setSeriesCategoryFilter(null); setSeriesOffset(0) }}>
                <X size={12} />
              </button>
            </span>
          )}
          {seriesQuery.data && <Pager total={seriesQuery.data.total} limit={SERIES_LIMIT} offset={seriesOffset} onOffset={setSeriesOffset} />}
          <PageSizeSelect value={SERIES_LIMIT} onChange={setSeriesLimit} />
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <Button size="sm" variant="outline" onClick={() => setCategoriesModalOpen('series')}>Manage Categories</Button>
          <Button size="sm" variant="outline" onClick={() => setNeedsReviewModalOpen('series')}>
            Needs Review{needsReviewQuery.data?.series.length ? ` (${needsReviewQuery.data.series.length})` : ''}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setMissingArtworkModalOpen('series')}>
            Missing Artwork{missingArtworkCountsQuery.data?.series ? ` (${missingArtworkCountsQuery.data.series})` : ''}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setLibraryLanguageModalOpen('series')}>
            Language Filter
          </Button>
          <div className="flex items-center gap-0.5 rounded border border-border p-0.5 ml-auto">
            <button
              title="List view"
              className={`flex items-center p-1 rounded ${seriesViewMode === 'list' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}`}
              onClick={() => setSeriesViewMode('list')}
            >
              <List size={12} />
            </button>
            <button
              title="Grid view"
              className={`flex items-center p-1 rounded ${seriesViewMode === 'grid' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}`}
              onClick={() => setSeriesViewMode('grid')}
            >
              <LayoutGrid size={12} />
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 rounded border border-border/50 bg-muted/30 px-2 py-1.5">
          <span className="text-xs text-muted-foreground">{selectedSeriesIds.size} selected · shift-click to select a range</span>
          <button
            className="text-xs text-muted-foreground hover:text-foreground underline decoration-dotted"
            onClick={() => setSelectedSeriesIds(new Set((seriesQuery.data?.items ?? []).map((s) => s.id)))}
          >
            Select all visible ({seriesQuery.data?.items.length ?? 0})
          </button>
          <button
            className="text-xs text-muted-foreground hover:text-foreground underline decoration-dotted"
            onClick={() => setSelectedSeriesIds(new Set())}
          >
            Clear
          </button>
          <select className={inputCls()} value={bulkSeriesTargetCategory} onChange={(e) => setBulkSeriesTargetCategory(e.target.value)}>
            <option value="">Place in category…</option>
            {seriesCategories.filter((c) => !c.is_smart).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <Button
            size="sm"
            variant="outline"
            disabled={!bulkSeriesTargetCategory || selectedSeriesIds.size === 0 || bulkPlaceSeries.isPending}
            onClick={() => bulkPlaceSeries.mutate({ category_id: Number(bulkSeriesTargetCategory), ids: Array.from(selectedSeriesIds) })}
          >
            Place selected ({selectedSeriesIds.size})
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!bulkSeriesTargetCategory || !seriesQuery.data?.total || bulkPlaceSeries.isPending}
            onClick={() => bulkPlaceSeries.mutate({
              category_id: Number(bulkSeriesTargetCategory),
              search: seriesSearch || undefined,
              source_category_id: seriesCategoryFilter ?? undefined,
              source_provider_id: seriesProviderFilter ?? undefined,
            })}
            title="Places every series matching the current search/category filter, not just this page"
          >
            Place all filtered ({seriesQuery.data?.total ?? 0})
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={selectedSeriesIds.size === 0 || bulkArchiveSeries.isPending}
            onClick={() => bulkArchiveSeries.mutate({ ids: Array.from(selectedSeriesIds), archived: !seriesShowArchived })}
          >
            {seriesShowArchived ? 'Un-archive' : 'Archive'} selected ({selectedSeriesIds.size})
          </Button>
          {bulkSeriesResult && <span className="text-xs text-muted-foreground">{bulkSeriesResult}</span>}
        </div>
        <div className={seriesViewMode === 'grid' ? 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 gap-2' : 'space-y-2'}>
          {seriesQuery.isFetching && <p className="text-xs text-muted-foreground">Loading…</p>}
          {seriesQuery.data?.items.map((s, i) => (
            <SeriesRow
              key={s.id}
              series={s}
              seriesCategories={seriesCategories}
              qc={qc}
              xcCredentials={xcCredentialsQuery.data}
              selected={selectedSeriesIds.has(s.id)}
              onToggleSelect={(shiftKey) => toggleSeriesSelected(s.id, i, shiftKey)}
              mode={seriesViewMode}
              onToggleArchived={() => toggleSeriesArchived.mutate({ id: s.id, archived: !s.review_excluded })}
            />
          ))}
        </div>
        {seriesQuery.data && (
          <div className="flex items-center gap-1.5">
            <Pager total={seriesQuery.data.total} limit={SERIES_LIMIT} offset={seriesOffset} onOffset={setSeriesOffset} />
            <PageSizeSelect value={SERIES_LIMIT} onChange={setSeriesLimit} />
          </div>
        )}
        <div className="flex items-center gap-1.5 pt-1">
          <input className={inputCls()} placeholder="Series name" value={seriesForm.name} onChange={(e) => setSeriesForm({ ...seriesForm, name: e.target.value })} />
          <input className={inputCls('w-20')} type="number" placeholder="Year" value={seriesForm.year} onChange={(e) => setSeriesForm({ ...seriesForm, year: e.target.value })} />
          <Button size="sm" disabled={!seriesForm.name || addSeries.isPending} onClick={() => addSeries.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
      </SectionCard>
      </>
      )}

      {categoriesModalOpen && (
        <CategoriesModal
          contentType={categoriesModalOpen}
          categories={categoriesModalOpen === 'movie' ? movieCategories : seriesCategories}
          qc={qc}
          onView={(categoryId) => {
            if (categoriesModalOpen === 'movie') { setMovieCategoryFilter(categoryId); setMovieSearch(''); setMovieOffset(0) }
            else { setSeriesCategoryFilter(categoryId); setSeriesSearch(''); setSeriesOffset(0) }
            setCategoriesModalOpen(null)
          }}
          onClose={() => setCategoriesModalOpen(null)}
        />
      )}

      {needsReviewModalOpen && (
        <NeedsReviewModal
          contentType={needsReviewModalOpen}
          items={(needsReviewModalOpen === 'movie' ? needsReviewQuery.data?.movies : needsReviewQuery.data?.series) ?? []}
          qc={qc}
          xcCredentials={xcCredentialsQuery.data}
          onClose={() => setNeedsReviewModalOpen(null)}
        />
      )}
      {missingArtworkModalOpen && (
        <MissingArtworkModal
          contentType={missingArtworkModalOpen}
          qc={qc}
          onClose={() => setMissingArtworkModalOpen(null)}
        />
      )}
      {libraryLanguageModalOpen && (
        <LibraryLanguageModal
          contentType={libraryLanguageModalOpen}
          qc={qc}
          onClose={() => setLibraryLanguageModalOpen(null)}
        />
      )}
    </div>
  )
}
