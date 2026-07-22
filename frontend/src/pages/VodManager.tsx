import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Hls from 'hls.js'
import { AlertCircle, CheckCircle2, ChevronDown, ChevronUp, Copy, Download, Eye, Film, HardDriveDownload, ImageOff, LayoutGrid, List, Loader2, Play, Plus, RefreshCw, RotateCcw, Settings, ShieldCheck, Sparkles, Stethoscope, Trash2, Tv, Upload, Wrench, X, Zap } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import api from '@/lib/api'

interface Provider {
  id: number
  name: string
  base_url: string
  username: string
  max_streams: number
  is_active: number
  priority: number
  provider_type: 'xc' | 'plex' | 'emby' | 'jellyfin'
  shared_connection_limit: number | null
  custom_user_agent: string | null
  has_password: boolean
  movie_count: number
  series_count: number
  episode_count: number
  synced_connection_count: number
  live_account_count: number
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
  provider_type: 'xc' | 'plex' | 'emby' | 'jellyfin'
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
  source_count: number
  category_count: number
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
}

const PROVIDER_TYPE_LABELS: Record<'xc' | 'plex' | 'emby' | 'jellyfin', string> = {
  xc: 'Xtream-Codes', plex: 'Plex', emby: 'Emby', jellyfin: 'Jellyfin',
}
const RULE_FIELDS = ['name', 'genre', 'year', 'language', 'director', 'is_adult'] as const
const RULE_OPS = ['contains', 'equals', 'starts_with', 'gte', 'lte'] as const
const REWRITABLE_FIELDS = ['name', 'genre', 'description', 'director', 'cast_list', 'country'] as const

interface MovieSource { id: number; provider_id: number; provider_stream_id: string; container_extension: string; provider_name: string; provider_category_name?: string }
interface MoviePlacement { id: number; category_id: number; export_stream_id: number; name_suffix: string; category_name: string }
interface Movie {
  id: number
  name: string
  year: number | null
  genre: string | null
  description: string | null
  poster_url: string | null
  is_adult: number
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

interface EpisodeSource { id: number; provider_id: number; provider_stream_id: string; container_extension: string; provider_name: string }
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
  import_provider_name: string | null
  episodes: Episode[]
  placements: SeriesPlacement[]
}

interface EnrichProgress {
  running: boolean
  movies_total: number; movies_done: number; movies_errors: number
  series_total: number; series_done: number; series_errors: number
  started_at: number | null; finished_at: number | null
}

interface Page<T> { items: T[]; total: number; limit: number; offset: number }

function SectionCard({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="space-y-3">
        <h2 className="text-sm font-semibold flex items-center gap-1.5">{icon}{title}</h2>
        {children}
      </CardContent>
    </Card>
  )
}

function inputCls(extra = '') {
  return `h-7 px-2 rounded border border-border bg-background text-xs outline-none focus:ring-1 focus:ring-primary ${extra}`
}

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

function DuplicateGroupRow({ group, onMerge, isPending }: {
  group: DuplicateGroup
  onMerge: (keepId: number, mergeIds: number[]) => void
  isPending: boolean
}) {
  // Backend already sorts most-sourced/most-placed first -- the obvious
  // default "keep" pick, but still a human decision the reviewer can override.
  const [keepId, setKeepId] = useState(group.items[0].id)
  return (
    <div className="border border-border rounded px-2 py-1.5 space-y-1">
      {group.items.map((item) => (
        <label key={item.id} className="flex items-center gap-2 cursor-pointer">
          <input type="radio" checked={keepId === item.id} onChange={() => setKeepId(item.id)} />
          <span className={keepId === item.id ? 'font-medium' : ''}>{item.name}</span>
          <span className="text-muted-foreground">
            {item.source_count} source{item.source_count === 1 ? '' : 's'} · {item.category_count} categor{item.category_count === 1 ? 'y' : 'ies'}
          </span>
        </label>
      ))}
      <Button
        size="sm"
        disabled={isPending}
        onClick={() => onMerge(keepId, group.items.filter((i) => i.id !== keepId).map((i) => i.id))}
      >
        {isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
        Merge into selected
      </Button>
    </div>
  )
}

function MovieRow({ movie, movieCategories, providers, qc, xcCredentials, selected, onToggleSelect, mode = 'list' }: {
  movie: Movie
  movieCategories: Category[]
  providers: Provider[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  selected: boolean
  onToggleSelect: () => void
  mode?: 'list' | 'grid'
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
      <div className="rounded border border-border/50 overflow-hidden hover:border-primary/50 transition-colors relative">
        <div className="absolute top-1 left-1 z-10" onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={onToggleSelect} title="Select for bulk placement" className="w-3.5 h-3.5" />
        </div>
        {!!movie.is_adult && (
          <span className="absolute top-1 right-1 z-10 text-destructive text-[10px] font-semibold bg-background/80 rounded px-1">18+</span>
        )}
        <button className="block w-full text-left" onClick={() => setOpen(true)}>
          {movie.poster_url ? (
            <img src={movie.poster_url} alt="" className="w-full aspect-[2/3] object-cover" loading="lazy" />
          ) : (
            <div className="w-full aspect-[2/3] bg-muted flex items-center justify-center">
              <Film size={24} className="text-muted-foreground" />
            </div>
          )}
          <div className="p-1.5 text-xs">
            <p className="font-medium truncate">{movie.name}</p>
            <p className="text-muted-foreground">{movie.year ?? ''}</p>
          </div>
        </button>
        {movie.sources.length > 0 && (
          <div className="absolute bottom-8 right-1 z-10 bg-background/80 rounded" onClick={(e) => e.stopPropagation()}>
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
    <div className="rounded border border-border/50 p-2 text-xs flex gap-2">
      <input type="checkbox" className="mt-0.5 shrink-0" checked={selected} onChange={onToggleSelect} title="Select for bulk placement" />
      {movie.poster_url && (
        <img src={movie.poster_url} alt="" className="w-8 h-12 object-cover rounded shrink-0" loading="lazy" />
      )}
      <div className="flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <span className="font-medium flex items-center gap-1 cursor-pointer" onClick={() => setOpen(!open)}>
          {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {movie.name}{movie.year ? ` (${movie.year})` : ''}
          {!!movie.is_adult && <span className="text-destructive text-[10px] font-semibold">18+</span>}
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
            title="Delete movie"
            className="text-muted-foreground hover:text-destructive"
            onClick={() => { if (confirm(`Delete "${movie.name}"? This removes all its sources and category placements.`)) deleteMovie.mutate() }}
          >
            <Trash2 size={12} />
          </button>
        </span>
      </div>
      {movie.placements.length > 0 && (
        <div className="text-muted-foreground mt-0.5">{movie.placements.map((p) => p.category_name).join(', ')}</div>
      )}
      {movie.genre && <div className="text-muted-foreground mt-0.5">genre: {movie.genre}</div>}

      {open && (
        <div className="mt-2 pt-2 border-t border-border/50 space-y-2">
          {detailContent}
        </div>
      )}
      </div>
    </div>
  )
}

function SeriesRow({ series, seriesCategories, qc, xcCredentials, selected, onToggleSelect, mode = 'list' }: {
  series: Series
  seriesCategories: Category[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  selected: boolean
  onToggleSelect: () => void
  mode?: 'list' | 'grid'
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
      <div className="rounded border border-border/50 overflow-hidden hover:border-primary/50 transition-colors relative">
        <div className="absolute top-1 left-1 z-10" onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={onToggleSelect} title="Select for bulk placement" className="w-3.5 h-3.5" />
        </div>
        {!!series.is_adult && (
          <span className="absolute top-1 right-1 z-10 text-destructive text-[10px] font-semibold bg-background/80 rounded px-1">18+</span>
        )}
        <button className="block w-full text-left" onClick={() => setOpen(true)}>
          {series.poster_url ? (
            <img src={series.poster_url} alt="" className="w-full aspect-[2/3] object-cover" loading="lazy" />
          ) : (
            <div className="w-full aspect-[2/3] bg-muted flex items-center justify-center">
              <Tv size={24} className="text-muted-foreground" />
            </div>
          )}
          <div className="p-1.5 text-xs">
            <p className="font-medium truncate">{series.name}</p>
            <p className="text-muted-foreground">{series.year ?? ''}</p>
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
    <div className="rounded border border-border/50 p-2 text-xs flex gap-2">
      <input type="checkbox" className="mt-0.5 shrink-0" checked={selected} onChange={onToggleSelect} title="Select for bulk placement" />
      {series.poster_url && (
        <img src={series.poster_url} alt="" className="w-8 h-12 object-cover rounded shrink-0" loading="lazy" />
      )}
      <div className="flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <span className="font-medium flex items-center gap-1 cursor-pointer" onClick={() => setOpen(!open)}>
          {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {series.name}{series.year ? ` (${series.year})` : ''}
          {!!series.is_adult && <span className="text-destructive text-[10px] font-semibold">18+</span>}
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
            title="Delete series"
            className="text-muted-foreground hover:text-destructive"
            onClick={() => { if (confirm(`Delete "${series.name}"? This removes all its episodes and category placements.`)) deleteSeries.mutate() }}
          >
            <Trash2 size={12} />
          </button>
        </span>
      </div>
      {series.genre && <div className="text-muted-foreground mt-0.5">genre: {series.genre}</div>}
      {series.import_provider_name && <div className="text-muted-foreground mt-0.5">matched from: {series.import_provider_name}</div>}

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
        <ul className="space-y-0.5">
          {categories.map((c) => (
            <li key={c.id} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1 min-w-0">
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
                <button title="Delete category" className="text-muted-foreground hover:text-destructive" onClick={() => { if (confirm(`Delete category "${c.name}"? Items stay in the pool, just unplaced from this category.`)) deleteCategory.mutate(c.id) }}>
                  <Trash2 size={12} />
                </button>
              </span>
            </li>
          ))}
          {categories.length === 0 && <p className="text-muted-foreground">No categories yet.</p>}
        </ul>
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

export default function VodManager() {
  const qc = useQueryClient()

  // ── Page tabs + view modes, persisted across reloads ──
  const [activeTab, setActiveTabState] = useState<'movies' | 'series' | 'curation' | 'config'>(() => {
    const saved = localStorage.getItem('vodmanager-tab')
    return saved === 'movies' || saved === 'series' || saved === 'curation' || saved === 'config' ? saved : 'movies'
  })
  function setActiveTab(t: typeof activeTab) {
    localStorage.setItem('vodmanager-tab', t)
    setActiveTabState(t)
  }
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
  })
  const [revealedConnId, setRevealedConnId] = useState<number | null>(null)

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
    provider: 'anthropic' | 'openai' | 'gemini'
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
      name: providerForm.name, base_url: providerForm.base_url,
      username: providerForm.provider_type === 'xc' ? providerForm.username : '',
      password: providerForm.password, max_streams: Number(providerForm.max_streams) || 0,
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
      setImportResult(`Imported: ${r.data.movies_created} new movies (${r.data.movies_matched} already known), ${r.data.series_created} new series (${r.data.series_matched} already known).`)
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

  // ── Duplicate finder (same-year entries differing only by punctuation) ──
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

  // ── Movies ──
  const [movieSearch, setMovieSearch] = useState('')
  const [movieOffset, setMovieOffset] = useState(0)
  const [movieCategoryFilter, setMovieCategoryFilter] = useState<number | null>(null)
  const [movieProviderFilter, setMovieProviderFilter] = useState<number | null>(null)
  const [MOVIE_LIMIT, setMovieLimitState] = useState(
    () => Number(localStorage.getItem('vodmanager-movies-limit')) || 25
  )
  function setMovieLimit(n: number) {
    localStorage.setItem('vodmanager-movies-limit', String(n))
    setMovieLimitState(n)
    setMovieOffset(0)
  }
  const moviesQuery = useQuery<Page<Movie>>({
    queryKey: ['vod-movies', movieSearch, movieOffset, movieCategoryFilter, movieProviderFilter, MOVIE_LIMIT],
    queryFn:  () => api.get('/vod/movies/', { params: { search: movieSearch || undefined, limit: MOVIE_LIMIT, offset: movieOffset, category_id: movieCategoryFilter ?? undefined, provider_id: movieProviderFilter ?? undefined } }).then((r) => r.data),
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
  const toggleMovieSelected = (id: number) => setSelectedMovieIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
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

  // ── Series ──
  const [seriesSearch, setSeriesSearch] = useState('')
  const [seriesOffset, setSeriesOffset] = useState(0)
  const [seriesCategoryFilter, setSeriesCategoryFilter] = useState<number | null>(null)
  const [seriesProviderFilter, setSeriesProviderFilter] = useState<number | null>(null)
  const [SERIES_LIMIT, setSeriesLimitState] = useState(
    () => Number(localStorage.getItem('vodmanager-series-limit')) || 25
  )
  function setSeriesLimit(n: number) {
    localStorage.setItem('vodmanager-series-limit', String(n))
    setSeriesLimitState(n)
    setSeriesOffset(0)
  }
  const seriesQuery = useQuery<Page<Series>>({
    queryKey: ['vod-series', seriesSearch, seriesOffset, seriesCategoryFilter, seriesProviderFilter, SERIES_LIMIT],
    queryFn:  () => api.get('/vod/series/', { params: { search: seriesSearch || undefined, limit: SERIES_LIMIT, offset: seriesOffset, category_id: seriesCategoryFilter ?? undefined, provider_id: seriesProviderFilter ?? undefined } }).then((r) => r.data),
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
  const toggleSeriesSelected = (id: number) => setSelectedSeriesIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
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

      <div className="flex items-center gap-0.5 rounded border border-border p-0.5 w-fit">
        {([
          { key: 'movies' as const, label: 'Movies', icon: <Film size={12} /> },
          { key: 'series' as const, label: 'TV Shows', icon: <Tv size={12} /> },
          { key: 'curation' as const, label: 'Curation & Maintenance', icon: <Wrench size={12} /> },
          { key: 'config' as const, label: 'Configuration', icon: <Settings size={12} /> },
        ]).map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`flex items-center gap-1 px-2.5 py-1 rounded text-xs transition-colors ${
              activeTab === t.key
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent'
            }`}
          >
            {t.icon}{t.label}
          </button>
        ))}
      </div>

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
        <div className="flex items-center gap-1.5">
          <input
            className={inputCls()}
            placeholder={aiSettingsQuery.data?.model ? `Model (default: ${aiSettingsQuery.data.model})` : 'Model override'}
            value={aiModelInput}
            onChange={(e) => setAiModelInput(e.target.value)}
          />
          <Button
            size="sm"
            disabled={!aiSettingsQuery.data || saveAiProvider.isPending}
            onClick={() => saveAiProvider.mutate({ provider: aiSettingsQuery.data!.provider, model: aiModelInput || undefined })}
          >
            {saveAiProvider.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Set Model'}
          </Button>
        </div>
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
              <th className="pb-1 font-normal"></th>
            </tr>
          </thead>
          <tbody>
            {dispatcharrConnectionsQuery.data?.map((c) => (
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
            ))}
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
          {enrichProgress && (enrichProgress.running || enrichProgress.finished_at) && (
            <span className="text-xs text-muted-foreground">
              movies {enrichProgress.movies_done}/{enrichProgress.movies_total}
              {enrichProgress.movies_errors > 0 ? ` (${enrichProgress.movies_errors} errors)` : ''}
              {' · '}
              series {enrichProgress.series_done}/{enrichProgress.series_total}
              {enrichProgress.series_errors > 0 ? ` (${enrichProgress.series_errors} errors)` : ''}
              {!enrichProgress.running && enrichProgress.started_at && enrichProgress.finished_at
                ? ` · took ${Math.round(enrichProgress.finished_at - enrichProgress.started_at)}s`
                : ''}
            </span>
          )}
        </div>
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

      {activeTab === 'curation' && (
      <>
      <SectionCard title="Providers" icon={<RefreshCw size={14} />}>
        <div className="overflow-x-auto">
        <table className="w-full text-xs min-w-[1100px]">
          <thead>
            <tr className="text-muted-foreground text-left">
              <th className="pb-1 font-normal">Name</th>
              <th className="pb-1 font-normal">Base URL</th>
              <th className="pb-1 font-normal">Movies</th>
              <th className="pb-1 font-normal" title="Distinct series with at least one episode from this provider">Series</th>
              <th className="pb-1 font-normal" title="Total episode files from this provider — a different number than Series by design (one series can have many episodes)">Episodes</th>
              <th className="pb-1 font-normal" title="Higher number wins when multiple providers carry the same title">Priority</th>
              <th className="pb-1 font-normal">Max Streams</th>
              <th className="pb-1 font-normal" title="How many Dispatcharr connections have a synced profile for this provider">Synced</th>
              <th className="pb-1 font-normal" title="Real total connection cap for this provider, shared across every linked live-TV account (on any Dispatcharr instance) plus our own VOD usage — VOD will fail over to the next provider instead of exceeding it">Shared Limit / Live Accounts</th>
              <th className="pb-1 font-normal" title="Most providers work fine with the default browser User-Agent. Only set this if one blocks even that.">User-Agent Override</th>
              <th className="pb-1 font-normal"></th>
            </tr>
          </thead>
          <tbody>
            {providersQuery.data?.map((p) => (
              <tr key={p.id} className={`border-t border-border/50 ${!p.is_active ? 'opacity-50' : ''}`}>
                <td className="py-1 pr-2">
                  <span className="flex items-center gap-1">
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
                    {p.provider_type !== 'xc' && <span className="text-muted-foreground">({PROVIDER_TYPE_LABELS[p.provider_type]})</span>}
                    {!p.is_active && <span className="text-muted-foreground">(inactive)</span>}
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
            disabled={!providerForm.name || !providerForm.base_url || !providerForm.password || (providerForm.provider_type === 'xc' && !providerForm.username) || addProvider.isPending}
            onClick={() => addProvider.mutate()}
          >
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
      </SectionCard>

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
          <div className="text-xs text-muted-foreground space-y-1">
            <p>Orphaned series (dead provider reference): {orphansQuery.data.orphaned_series.count}
              {!!orphansQuery.data.orphaned_series.sample.length && ` — e.g. ${orphansQuery.data.orphaned_series.sample.slice(0, 5).map((s) => s.name).join(', ')}`}
            </p>
            <p>Sourceless movies: {orphansQuery.data.sourceless_movies.count}
              {!!orphansQuery.data.sourceless_movies.sample.length && ` — e.g. ${orphansQuery.data.sourceless_movies.sample.slice(0, 5).map((s) => s.name).join(', ')}`}
            </p>
            <p>Sourceless episodes (in otherwise-healthy series): {orphansQuery.data.sourceless_episodes.count}</p>
          </div>
        )}
      </SectionCard>

      <SectionCard title="Duplicate Finder" icon={<Copy size={14} />}>
        <p className="text-xs text-muted-foreground">
          Finds same-year pool entries whose names only differ by cosmetic punctuation (a colon, a dash, quote
          style) — real providers sometimes format the same title slightly differently, splitting what should be
          one pool entry with multiple sources into two "duplicates". Pick which spelling to keep for each group;
          the rest merge into it (sources, categories, and episodes move over, nothing is lost).
        </p>
        <div className="flex items-center gap-1.5">
          <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
            <button
              className={`px-2 py-0.5 rounded text-xs ${duplicatesContentType === 'movie' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              onClick={() => { setDuplicatesContentType('movie'); setDuplicatesOffset(0) }}
            >
              Movies
            </button>
            <button
              className={`px-2 py-0.5 rounded text-xs ${duplicatesContentType === 'series' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              onClick={() => { setDuplicatesContentType('series'); setDuplicatesOffset(0) }}
            >
              TV Shows
            </button>
          </div>
          <Button size="sm" variant="outline" disabled={duplicatesQuery.isFetching} onClick={() => { setDuplicatesOffset(0); duplicatesQuery.refetch() }}>
            {duplicatesQuery.isFetching ? <Loader2 size={12} className="animate-spin mr-1" /> : <RefreshCw size={12} className="mr-1" />}
            Scan
          </Button>
          {duplicatesQuery.data && duplicatesQuery.data.length === 0 && (
            <span className="text-xs text-muted-foreground">Clean — nothing found.</span>
          )}
          {!!duplicatesQuery.data?.length && (
            <>
              <span className="text-xs text-muted-foreground">{duplicatesQuery.data.length} group{duplicatesQuery.data.length === 1 ? '' : 's'} found</span>
              <Pager total={duplicatesQuery.data.length} limit={DUPLICATES_PAGE_SIZE} offset={duplicatesOffset} onOffset={setDuplicatesOffset} />
            </>
          )}
          {duplicatesMergeResult && <span className="text-xs text-destructive">{duplicatesMergeResult}</span>}
        </div>
        {!!duplicatesQuery.data?.length && (
          // Client-side slice, not a second network round-trip -- the scan
          // already walked the whole pool in one query; thousands of groups
          // in the DOM at once (not just in memory) is what actually made
          // the page unusably slow, so only render one page's worth.
          <div className="text-xs space-y-1.5">
            {duplicatesQuery.data.slice(duplicatesOffset, duplicatesOffset + DUPLICATES_PAGE_SIZE).map((group) => (
              <DuplicateGroupRow
                key={group.items.map((i) => i.id).join('-')}
                group={group}
                isPending={mergeDuplicateGroup.isPending}
                onMerge={(keepId, mergeIds) => mergeDuplicateGroup.mutate({ keep_id: keepId, merge_ids: mergeIds })}
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
                <div key={c.id} className="flex items-center justify-between gap-2">
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
                    <button title="Delete category" className="text-muted-foreground hover:text-destructive" onClick={() => { if (confirm(`Delete category "${c.name}"? Items stay in the pool, just unplaced from this category.`)) deleteCategory.mutate(c.id) }}>
                      <Trash2 size={12} />
                    </button>
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
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
          <span className="text-xs text-muted-foreground">{selectedMovieIds.size} selected</span>
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
          {bulkMovieResult && <span className="text-xs text-muted-foreground">{bulkMovieResult}</span>}
        </div>
        <div className={movieViewMode === 'grid' ? 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 gap-2' : 'space-y-2'}>
          {moviesQuery.isFetching && <p className="text-xs text-muted-foreground">Loading…</p>}
          {moviesQuery.data?.items.map((m) => (
            <MovieRow
              key={m.id}
              movie={m}
              movieCategories={movieCategories}
              providers={providersQuery.data ?? []}
              qc={qc}
              xcCredentials={xcCredentialsQuery.data}
              selected={selectedMovieIds.has(m.id)}
              onToggleSelect={() => toggleMovieSelected(m.id)}
              mode={movieViewMode}
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
          <span className="text-xs text-muted-foreground">{selectedSeriesIds.size} selected</span>
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
          {bulkSeriesResult && <span className="text-xs text-muted-foreground">{bulkSeriesResult}</span>}
        </div>
        <div className={seriesViewMode === 'grid' ? 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 gap-2' : 'space-y-2'}>
          {seriesQuery.isFetching && <p className="text-xs text-muted-foreground">Loading…</p>}
          {seriesQuery.data?.items.map((s) => (
            <SeriesRow
              key={s.id}
              series={s}
              seriesCategories={seriesCategories}
              qc={qc}
              xcCredentials={xcCredentialsQuery.data}
              selected={selectedSeriesIds.has(s.id)}
              onToggleSelect={() => toggleSeriesSelected(s.id)}
              mode={seriesViewMode}
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
