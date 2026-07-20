import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, ChevronDown, ChevronUp, Copy, Download, Eye, Film, HardDriveDownload, Loader2, Play, Plus, RefreshCw, RotateCcw, Sparkles, Trash2, Tv, Upload, X, Zap } from 'lucide-react'
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
  dispatcharr_profile_id: number | null
  dispatcharr_live_account_id: number | null
  shared_connection_limit: number | null
  has_password: boolean
  movie_count: number
  series_count: number
  episode_count: number
}

interface XcCredentials { username: string; password: string }

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
}

interface NeedsReviewData {
  movies: NeedsReviewItem[]
  series: NeedsReviewItem[]
}

interface TmdbSuggestion {
  tmdb_id: string
  name: string
  year: number | null
  poster_url: string | null
}

interface XcClient {
  id: number
  label: string
  username: string
  password: string
  enabled: boolean
  ip_allowlist: string | null
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
// the direct preview above fails on a codec the browser can't decode.
function buildTranscodedPreviewSourceUrl(kind: 'movie' | 'series', sourceId: number, creds?: XcCredentials) {
  if (!creds) return null
  const path = kind === 'movie' ? 'movie-source-transcoded' : 'series-source-transcoded'
  return `${window.location.origin}/preview/${path}/${creds.username}/${creds.password}/${sourceId}.mp4`
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

function PlayButton({ url, transcodedUrl, title }: { url: string | null; transcodedUrl?: string | null; title: string }) {
  const [open, setOpen] = useState(false)
  if (!url) return null
  return (
    <>
      <button title="Play" className="hover:text-foreground" onClick={() => setOpen(true)}>
        <Play size={12} />
      </button>
      {open && <VodPlayer url={url} transcodedUrl={transcodedUrl} title={title} onClose={() => setOpen(false)} />}
    </>
  )
}

function VodPlayer({ url, transcodedUrl, title, onClose }: { url: string; transcodedUrl?: string | null; title: string; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [status, setStatus] = useState<'loading' | 'playing' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [usingTranscode, setUsingTranscode] = useState(false)
  const activeUrl = usingTranscode && transcodedUrl ? transcodedUrl : url

  return createPortal(
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80" onClick={onClose}>
      <div
        className="relative bg-card border border-border rounded-xl overflow-hidden w-full max-w-3xl mx-4 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
          <div className="flex items-center gap-2 min-w-0">
            <Play size={13} className="text-primary shrink-0" />
            <span className="text-sm font-medium truncate">{title}{usingTranscode && ' (transcoded)'}</span>
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
            {!usingTranscode && transcodedUrl ? (
              <>
                <p className="text-xs text-muted-foreground">
                  This is usually a codec this browser can't decode natively (e.g. AVI, DTS/AC-3 audio) — the file
                  itself relayed fine. Try a transcoded copy instead, or use Copy URL with an external player.
                </p>
                <Button size="sm" variant="outline" onClick={() => { setUsingTranscode(true); setStatus('loading'); setError(null) }}>
                  Try transcoded playback
                </Button>
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                The source provider may be down — failover already tried every active source for this item.
              </p>
            )}
          </div>
        )}

        <video
          ref={videoRef}
          src={activeUrl}
          controls
          autoPlay
          className={status === 'error' ? 'hidden' : 'w-full max-h-[70vh] bg-black'}
          onCanPlay={() => setStatus('playing')}
          onError={() => { setStatus('error'); setError('Playback failed — the file may be unreachable or use a codec this browser can\'t play.') }}
        />
        {status === 'loading' && (
          <div className="absolute inset-0 top-[41px] flex items-center justify-center gap-2 text-sm text-muted-foreground pointer-events-none">
            <Loader2 size={14} className="animate-spin" /> Loading…
          </div>
        )}
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

// One flagged item: no year, ambiguous against 2+ existing pool entries with
// the same name. TMDB suggestions are fetched on demand (only once expanded)
// rather than eagerly for every flagged item on page load.
function NeedsReviewRow({ contentType, item, qc }: {
  contentType: 'movie' | 'series'
  item: NeedsReviewItem
  qc: ReturnType<typeof useQueryClient>
}) {
  const [expanded, setExpanded] = useState(false)
  const [manualYear, setManualYear] = useState('')

  const suggestionsQuery = useQuery<TmdbSuggestion[]>({
    queryKey: ['vod-needs-review-suggestions', contentType, item.id],
    queryFn:  () => api.get(`/vod/needs-review/${contentType}/${item.id}/suggestions/`).then((r) => r.data),
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

  return (
    <li className="border-b border-border/50 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate">
          {item.name} {item.genre && <span className="text-muted-foreground">({item.genre})</span>}
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
          {suggestionsQuery.isLoading && <p className="text-muted-foreground">Searching TMDB…</p>}
          {suggestionsQuery.isError && <p className="text-destructive">TMDB search failed — check the API key in Rich Metadata settings.</p>}
          {!!suggestionsQuery.data?.length && (
            <div className="flex flex-wrap gap-2">
              {suggestionsQuery.data.map((s) => (
                <button
                  key={s.tmdb_id}
                  disabled={resolve.isPending}
                  className="flex items-center gap-2 border border-border rounded px-2 py-1 hover:bg-accent text-left"
                  onClick={() => resolve.mutate({ year: s.year ?? 0, tmdb_id: s.tmdb_id })}
                >
                  {s.poster_url
                    ? <img src={s.poster_url} alt="" className="w-8 h-12 object-cover rounded" />
                    : <div className="w-8 h-12 rounded bg-muted shrink-0" />}
                  <span>{s.name} {s.year ? `(${s.year})` : ''}</span>
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

function MovieRow({ movie, movieCategories, providers, qc, xcCredentials, selected, onToggleSelect }: {
  movie: Movie
  movieCategories: Category[]
  providers: Provider[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  selected: boolean
  onToggleSelect: () => void
}) {
  const [open, setOpen] = useState(false)
  const [sourceForm, setSourceForm] = useState({ provider_id: '', provider_stream_id: '', container_extension: 'mp4' })
  const [categoryPick, setCategoryPick] = useState('')

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
          {movie.poster_url && (
            <img src={movie.poster_url} alt="" className="w-24 rounded" loading="lazy" />
          )}
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
        </div>
      )}
      </div>
    </div>
  )
}

function SeriesRow({ series, seriesCategories, qc, xcCredentials, selected, onToggleSelect }: {
  series: Series
  seriesCategories: Category[]
  qc: ReturnType<typeof useQueryClient>
  xcCredentials?: XcCredentials
  selected: boolean
  onToggleSelect: () => void
}) {
  const [open, setOpen] = useState(false)
  const [categoryPick, setCategoryPick] = useState('')

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
          {series.poster_url && (
            <img src={series.poster_url} alt="" className="w-24 rounded" loading="lazy" />
          )}
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
        </div>
      )}
      </div>
    </div>
  )
}

export default function VodManager() {
  const qc = useQueryClient()

  // ── Activity (currently open stream relays) ──
  const activityQuery = useQuery<ActivitySession[]>({
    queryKey: ['vod-activity'],
    queryFn:  () => api.get('/vod/activity/').then((r) => r.data),
    refetchInterval: 3000,
  })

  // ── Settings (XC account id) ──
  const settingsQuery = useQuery({
    queryKey: ['vod-settings'],
    queryFn:  () => api.get('/vod/settings/').then((r) => r.data),
  })
  const [xcAccountId, setXcAccountId] = useState('')
  const saveSettings = useMutation({
    mutationFn: () => api.post('/vod/settings/', { xc_account_id: Number(xcAccountId) }),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['vod-settings'] }),
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
  const setConnectionSharing = useMutation({
    // 0 means "not set" on both sides (a real Dispatcharr account id or
    // connection limit of 0 is never meaningful), so always send actual
    // numbers rather than omitting params — avoids ambiguity around clearing
    // a previously-set value.
    mutationFn: ({ id, dispatcharr_live_account_id, shared_connection_limit }: {
      id: number; dispatcharr_live_account_id: number; shared_connection_limit: number
    }) => api.post(`/vod/providers/${id}/connection-sharing/`, null, {
      params: { dispatcharr_live_account_id, shared_connection_limit },
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vod-providers'] }),
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
    onSettled: () => setImportingId(null),
  })

  // ── Categories ──
  const categoriesQuery = useQuery<Category[]>({
    queryKey: ['vod-categories'],
    queryFn:  () => api.get('/vod/categories/').then((r) => r.data),
  })
  const [categoryForm, setCategoryForm] = useState({
    name: '', content_type: 'movie' as 'movie' | 'series', is_smart: false,
    rule_field: 'genre' as typeof RULE_FIELDS[number], rule_op: 'contains' as typeof RULE_OPS[number], rule_value: '',
  })
  const addCategory = useMutation({
    mutationFn: () => api.post('/vod/categories/', {
      name: categoryForm.name,
      content_type: categoryForm.content_type,
      is_smart: categoryForm.is_smart,
      rule_json: categoryForm.is_smart
        ? JSON.stringify({ match: 'all', conditions: [{ field: categoryForm.rule_field, op: categoryForm.rule_op, value: categoryForm.rule_value }] })
        : null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vod-categories'] })
      setCategoryForm({ name: '', content_type: 'movie', is_smart: false, rule_field: 'genre', rule_op: 'contains', rule_value: '' })
    },
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
  const [evaluateResult, setEvaluateResult] = useState<string | null>(null)
  const evaluateCategory = useMutation({
    mutationFn: (id: number) => api.post(`/vod/categories/${id}/evaluate/`),
    onSuccess: (r) => {
      setEvaluateResult(`Evaluated ${r.data.evaluated}: ${r.data.matched} matched, ${r.data.newly_placed} newly placed.`)
      qc.invalidateQueries({ queryKey: ['vod-movies'] })
      qc.invalidateQueries({ queryKey: ['vod-series'] })
    },
  })

  // ── Year review (ambiguous no-year duplicates held out of categories) ──
  const needsReviewQuery = useQuery<NeedsReviewData>({
    queryKey: ['vod-needs-review'],
    queryFn:  () => api.get('/vod/needs-review/').then((r) => r.data),
  })

  // ── Movies ──
  const [movieSearch, setMovieSearch] = useState('')
  const [movieOffset, setMovieOffset] = useState(0)
  const [movieCategoryFilter, setMovieCategoryFilter] = useState<number | null>(null)
  const [movieProviderFilter, setMovieProviderFilter] = useState<number | null>(null)
  const MOVIE_LIMIT = 25
  const moviesQuery = useQuery<Page<Movie>>({
    queryKey: ['vod-movies', movieSearch, movieOffset, movieCategoryFilter, movieProviderFilter],
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
  const SERIES_LIMIT = 25
  const seriesQuery = useQuery<Page<Series>>({
    queryKey: ['vod-series', seriesSearch, seriesOffset, seriesCategoryFilter, seriesProviderFilter],
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
  const plainMovieCategories  = movieCategories.filter((c) => !c.sync_source)
  const plainSeriesCategories = seriesCategories.filter((c) => !c.sync_source)
  const tmdbGroups = Object.values(
    (categoriesQuery.data ?? []).filter((c) => !!c.sync_source).reduce((acc, c) => {
      const key = c.sync_source as string
      if (!acc[key]) acc[key] = { sync_source: key, categories: [] as Category[] }
      acc[key].categories.push(c)
      return acc
    }, {} as Record<string, { sync_source: string; categories: Category[] }>)
  )

  return (
    <div className="space-y-4 max-w-5xl">
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
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </SectionCard>

      <SectionCard title="VOD Settings" icon={<CheckCircle2 size={14} />}>
        <p className="text-xs text-muted-foreground">
          The Dispatcharr M3U account (type XC) pointing back at VOD Manager's own catalog server.
          Create it once in Dispatcharr, then enter its account ID here.
        </p>
        <div className="flex items-center gap-1.5">
          <input
            className={inputCls()}
            placeholder={settingsQuery.data?.xc_account_id != null ? String(settingsQuery.data.xc_account_id) : 'Account ID'}
            value={xcAccountId}
            onChange={(e) => setXcAccountId(e.target.value)}
          />
          <Button size="sm" disabled={!xcAccountId || saveSettings.isPending} onClick={() => saveSettings.mutate()}>
            {saveSettings.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
          </Button>
          {settingsQuery.data?.xc_account_id != null && (
            <span className="text-xs text-muted-foreground">currently: {settingsQuery.data.xc_account_id}</span>
          )}
        </div>
        <p className="text-xs text-muted-foreground pt-2">
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
      </SectionCard>

      <SectionCard title="Connected Instances" icon={<Zap size={14} />}>
        <p className="text-xs text-muted-foreground">
          Each Dispatcharr instance (or other XC client) pulling from this pool gets its own credential pair —
          use <code className="bg-muted px-1 rounded">{window.location.origin}</code> as the server URL in that
          instance's XC-type M3U account, with the username/password below.
        </p>

        <table className="w-full text-xs">
          <thead>
            <tr className="text-muted-foreground text-left">
              <th className="pb-1 font-normal">Label</th>
              <th className="pb-1 font-normal">Credentials</th>
              <th className="pb-1 font-normal">IP allowlist</th>
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

      <SectionCard title="Providers" icon={<RefreshCw size={14} />}>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-muted-foreground text-left">
              <th className="pb-1 font-normal">Name</th>
              <th className="pb-1 font-normal">Base URL</th>
              <th className="pb-1 font-normal">Movies</th>
              <th className="pb-1 font-normal" title="Distinct series with at least one episode from this provider">Series</th>
              <th className="pb-1 font-normal" title="Total episode files from this provider — a different number than Series by design (one series can have many episodes)">Episodes</th>
              <th className="pb-1 font-normal" title="Higher number wins when multiple providers carry the same title">Priority</th>
              <th className="pb-1 font-normal">Max Streams</th>
              <th className="pb-1 font-normal">Dispatcharr Profile</th>
              <th className="pb-1 font-normal" title="If Dispatcharr also connects to this same real provider for live TV, set its account ID here plus the provider's true total connection limit — VOD will fail over to the next provider instead of exceeding it">Shares With (Live Acct / Limit)</th>
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
                <td className="py-1 pr-2 text-muted-foreground">{p.dispatcharr_profile_id ?? '—'}</td>
                <td className="py-1 pr-2">
                  <span className="flex items-center gap-1">
                    <input
                      className={inputCls('w-14')}
                      type="number"
                      placeholder="acct id"
                      defaultValue={p.dispatcharr_live_account_id ?? ''}
                      key={`live-${p.dispatcharr_live_account_id}`}
                      onBlur={(e) => {
                        const v = Number(e.target.value) || 0
                        if (v !== (p.dispatcharr_live_account_id ?? 0)) {
                          setConnectionSharing.mutate({ id: p.id, dispatcharr_live_account_id: v, shared_connection_limit: p.shared_connection_limit ?? 0 })
                        }
                      }}
                    />
                    <span className="text-muted-foreground">/</span>
                    <input
                      className={inputCls('w-14')}
                      type="number"
                      placeholder="limit"
                      defaultValue={p.shared_connection_limit ?? ''}
                      key={`limit-${p.shared_connection_limit}`}
                      onBlur={(e) => {
                        const v = Number(e.target.value) || 0
                        if (v !== (p.shared_connection_limit ?? 0)) {
                          setConnectionSharing.mutate({ id: p.id, dispatcharr_live_account_id: p.dispatcharr_live_account_id ?? 0, shared_connection_limit: v })
                        }
                      }}
                    />
                  </span>
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

      <SectionCard title="Categories" icon={<CheckCircle2 size={14} />}>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <p className="text-xs font-medium text-muted-foreground mb-1">Movie categories</p>
            <ul className="text-xs space-y-0.5">
              {plainMovieCategories.map((c) => (
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
                    {!!c.sync_source && (
                      <button title="Sync from TMDB now" className="text-muted-foreground hover:text-foreground" disabled={syncCategoryNow.isPending} onClick={() => syncCategoryNow.mutate(c.id)}>
                        <RefreshCw size={12} />
                      </button>
                    )}
                    <button
                      title="View movies in this category"
                      className={movieCategoryFilter === c.id ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}
                      onClick={() => { setMovieCategoryFilter(c.id); setMovieSearch(''); setMovieOffset(0) }}
                    >
                      <Eye size={12} />
                    </button>
                    <button title="Delete category" className="text-muted-foreground hover:text-destructive" onClick={() => { if (confirm(`Delete category "${c.name}"? Movies stay in the pool, just unplaced from this category.`)) deleteCategory.mutate(c.id) }}>
                      <Trash2 size={12} />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <p className="text-xs font-medium text-muted-foreground mb-1">Series categories</p>
            <ul className="text-xs space-y-0.5">
              {plainSeriesCategories.map((c) => (
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
                    {!!c.sync_source && (
                      <button title="Sync from TMDB now" className="text-muted-foreground hover:text-foreground" disabled={syncCategoryNow.isPending} onClick={() => syncCategoryNow.mutate(c.id)}>
                        <RefreshCw size={12} />
                      </button>
                    )}
                    <button
                      title="View series in this category"
                      className={seriesCategoryFilter === c.id ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}
                      onClick={() => { setSeriesCategoryFilter(c.id); setSeriesSearch(''); setSeriesOffset(0) }}
                    >
                      <Eye size={12} />
                    </button>
                    <button title="Delete category" className="text-muted-foreground hover:text-destructive" onClick={() => { if (confirm(`Delete category "${c.name}"? Series stay in the pool, just unplaced from this category.`)) deleteCategory.mutate(c.id) }}>
                      <Trash2 size={12} />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
        {evaluateResult && <p className="text-xs text-muted-foreground">{evaluateResult}</p>}
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <input
            className={inputCls()}
            placeholder="Category name"
            value={categoryForm.name}
            onChange={(e) => setCategoryForm({ ...categoryForm, name: e.target.value })}
          />
          <select
            className={inputCls()}
            value={categoryForm.content_type}
            onChange={(e) => setCategoryForm({ ...categoryForm, content_type: e.target.value as 'movie' | 'series' })}
          >
            <option value="movie">Movie</option>
            <option value="series">Series</option>
          </select>
          <label className="flex items-center gap-1 text-xs text-muted-foreground">
            <input type="checkbox" checked={categoryForm.is_smart} onChange={(e) => setCategoryForm({ ...categoryForm, is_smart: e.target.checked })} />
            Smart (rule-based)
          </label>
          <Button size="sm" disabled={!categoryForm.name || (categoryForm.is_smart && !categoryForm.rule_value) || addCategory.isPending} onClick={() => addCategory.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
        {categoryForm.is_smart && (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
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
      </SectionCard>

      <SectionCard title="Needs Year Review" icon={<AlertCircle size={14} />}>
        <p className="text-xs text-muted-foreground">
          Imported with no year, and ambiguous against 2+ existing pool entries with the same name — held out of every category until resolved.
        </p>
        {needsReviewQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
        {!!needsReviewQuery.data && !needsReviewQuery.data.movies.length && !needsReviewQuery.data.series.length && (
          <p className="text-xs text-muted-foreground">Nothing needs review right now.</p>
        )}
        {!!needsReviewQuery.data?.movies.length && (
          <div>
            <p className="text-xs font-medium text-muted-foreground mb-1">Movies ({needsReviewQuery.data.movies.length})</p>
            <ul className="text-xs">
              {needsReviewQuery.data.movies.map((m) => (
                <NeedsReviewRow key={m.id} contentType="movie" item={m} qc={qc} />
              ))}
            </ul>
          </div>
        )}
        {!!needsReviewQuery.data?.series.length && (
          <div>
            <p className="text-xs font-medium text-muted-foreground mb-1">TV Shows ({needsReviewQuery.data.series.length})</p>
            <ul className="text-xs">
              {needsReviewQuery.data.series.map((s) => (
                <NeedsReviewRow key={s.id} contentType="series" item={s} qc={qc} />
              ))}
            </ul>
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
            placeholder={`Name template, e.g. "Steven's ${TMDB_TOKEN} Picks"`}
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

      <SectionCard title="Movies" icon={<Film size={14} />}>
        <div className="flex items-center gap-1.5">
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
        <div className="space-y-2">
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
            />
          ))}
        </div>
        <div className="flex items-center gap-1.5 pt-1">
          <input className={inputCls()} placeholder="Movie name" value={movieForm.name} onChange={(e) => setMovieForm({ ...movieForm, name: e.target.value })} />
          <input className={inputCls('w-20')} type="number" placeholder="Year" value={movieForm.year} onChange={(e) => setMovieForm({ ...movieForm, year: e.target.value })} />
          <Button size="sm" disabled={!movieForm.name || addMovie.isPending} onClick={() => addMovie.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
      </SectionCard>

      <SectionCard title="TV Shows" icon={<Tv size={14} />}>
        <div className="flex items-center gap-1.5">
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
        <div className="space-y-2">
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
            />
          ))}
        </div>
        <div className="flex items-center gap-1.5 pt-1">
          <input className={inputCls()} placeholder="Series name" value={seriesForm.name} onChange={(e) => setSeriesForm({ ...seriesForm, name: e.target.value })} />
          <input className={inputCls('w-20')} type="number" placeholder="Year" value={seriesForm.year} onChange={(e) => setSeriesForm({ ...seriesForm, year: e.target.value })} />
          <Button size="sm" disabled={!seriesForm.name || addSeries.isPending} onClick={() => addSeries.mutate()}>
            <Plus size={12} className="mr-1" /> Add
          </Button>
        </div>
      </SectionCard>
    </div>
  )
}
