import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, Clock, Film, HardDriveDownload, ListVideo, Loader2, LogOut, Mail, Play, Plus, Search as SearchIcon, Trash2, User, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Chip, inputCls, KpiTile, QuotaBar, SectionCard, StatusPill } from '@/components/dvr-shared'
import portalApi from '@/lib/portalApi'
import { askConfirm, ConfirmDialogHost, notify, NotifyDialogHost } from '@/lib/confirm'

type PortalTab = 'scheduler' | 'recordings' | 'upcoming' | 'usage' | 'library' | 'account'

interface GuideProgram {
  id: number; title: string; sub_title: string | null; start_time: string; end_time: string; tvg_id: string | null
  is_new?: boolean; season?: number | null; episode?: number | null; onscreen_episode?: string | null
}
interface GuideChannel {
  channel: { id: number; name: string | null; channel_number: number | null }
  programs: GuideProgram[]
}

interface RecordingRule {
  id: number
  label: string
  title: string
  tvg_id: string | null
  mode: string
  channel_id: number | null
  created_at: string
}
interface SingleRecording {
  id: number
  title: string
  sub_title: string | null
  channel_id: number | null
  start_time: string
  end_time: string
}
interface MyRecordings { series: RecordingRule[]; singles: SingleRecording[] }
interface UpcomingRecording {
  id: number
  channel: number
  start_time: string
  end_time: string
  custom_properties?: {
    season?: number | null; episode?: number | null
    program?: { title?: string; sub_title?: string | null }
  }
}
interface UsageResponse {
  actual_bytes: number
  virtual_bytes: number
  total_bytes: number
  disk_quota_bytes: number | null
  stream_reserve: number
  stream_limit: number | null
}
interface LibraryMovie {
  id: number; name: string; year: number | null; poster_url: string | null; duration_secs: number | null
  description: string | null; file_size_bytes: number | null; added_at: string; category_name: string | null
}
interface LibraryEpisode {
  id: number; name: string; description: string | null; season_number: number; episode_number: number; duration_secs: number | null
  file_size_bytes: number | null; added_at: string
  series_id: number; series_name: string; series_poster_url: string | null; series_description: string | null
  category_name: string | null
}
interface Me { username: string; dispatcharr_username: string | null; provider_name: string | null; email: string | null }

function streamUrl(kind: 'movie' | 'episode', id: number) {
  const token = localStorage.getItem('vodmanager-portal-session') ?? ''
  return `/api/portal/library/${kind}/${id}/stream/?token=${encodeURIComponent(token)}`
}

function queryErrorMessage(err: any): string {
  return err?.response?.data?.detail ?? err?.message ?? 'Something went wrong.'
}

// Deterministic per-title gradient so the Library list reads like a real
// poster wall instead of a flat row of identical play icons -- same idea as
// the approved concept render's colored poster blocks, just picked from a
// fixed palette (keyed by title, not random) so the same show always gets
// the same color across renders/reloads.
const POSTER_GRADIENTS = [
  'from-violet-500 to-violet-800', 'from-amber-500 to-amber-800', 'from-teal-400 to-teal-700',
  'from-blue-400 to-blue-800', 'from-rose-500 to-rose-800', 'from-emerald-400 to-emerald-700',
  'from-fuchsia-500 to-fuchsia-800', 'from-orange-500 to-orange-800',
]
function posterGradient(seed: string): string {
  let hash = 0
  for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) | 0
  return POSTER_GRADIENTS[Math.abs(hash) % POSTER_GRADIENTS.length]
}

function formatBytes(n: number | null): string {
  if (!n) return '—'
  const gb = n / 1024 ** 3
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(n / 1024 ** 2).toFixed(0)} MB`
}
// added_at is a Python time.time() string (seconds, not ms).
function formatAddedAt(addedAt: string): string {
  const ms = parseFloat(addedAt) * 1000
  if (!ms) return '—'
  return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}
// season_number=0 with a huge episode_number is dispatcharr_dvr_importer's
// synthetic identity for a show with no real EPG season/episode data (e.g.
// a soap or newsmagazine with no per-episode subtitle) -- episode_number in
// that case is a YYYYMMDDHHMM timestamp, not a real episode number, so
// "S0E202607282000" would be actively misleading. Show the recorded date
// instead whenever season is the synthetic 0.
function episodeTag(e: EpisodeItem): string {
  return e.season_number > 0 ? `S${e.season_number}E${e.episode_number}` : formatAddedAt(e.added_at ?? e.startTime ?? '')
}

// Unifies an actually-recorded episode (LibraryEpisode, has a file) and a
// scheduled-but-not-yet-aired one (from /upcoming/, no file yet) into one
// shape so a show's season list can render both together in one continuous
// timeline -- real requirement from the user, 2026-07-29: scheduling a
// series should show up in the Library immediately, past AND future
// episodes for the season, not just whatever's already recorded.
interface EpisodeItem {
  kind: 'recorded' | 'upcoming' | 'missing'
  key: string
  season_number: number
  episode_number: number
  name: string
  description: string | null
  file_size_bytes: number | null
  added_at: string | null
  libraryId?: number       // recorded only -- for play/remove
  recordingId?: number     // upcoming only -- for cancel
  startTime?: string       // upcoming only -- when it airs
}
function episodeFromLibrary(e: LibraryEpisode): EpisodeItem {
  return {
    kind: 'recorded', key: `r-${e.id}`, season_number: e.season_number, episode_number: e.episode_number,
    name: e.name, description: e.description, file_size_bytes: e.file_size_bytes, added_at: e.added_at, libraryId: e.id,
  }
}
function episodeFromUpcoming(u: UpcomingRecording): EpisodeItem {
  const cp = u.custom_properties ?? {}
  const season = cp.season ?? 0
  const episode = cp.episode ?? 0
  return {
    kind: 'upcoming', key: `u-${u.id}`, season_number: season, episode_number: episode,
    name: cp.program?.sub_title || 'Upcoming recording', description: null, file_size_bytes: null,
    added_at: null, recordingId: u.id, startTime: u.start_time,
  }
}
interface CanonicalEpisode {
  season_number: number; episode_number: number; name: string | null; air_date: string | null
  status: 'recorded' | 'upcoming' | 'missing'
  library_episode_id: number | null; file_size_bytes: number | null
  recording_id: number | null; start_time: string | null
}
// TMDB's real, full history for the show -- every season/episode it's ever
// had, not just whatever VOD Manager happens to have recorded or scheduled.
// Real requirement from the user, 2026-07-29: "there is prior seasons and
// even prior episodes from this same season to see" -- the earlier version
// of this only ever showed what was locally known (recorded + upcoming),
// so a 63-season show with one recorded episode looked like it only had
// one season, period.
function episodeFromCanonical(ep: CanonicalEpisode): EpisodeItem {
  return {
    kind: ep.status, key: `c-${ep.season_number}-${ep.episode_number}`,
    season_number: ep.season_number, episode_number: ep.episode_number,
    name: ep.name || `Episode ${ep.episode_number}`, description: ep.air_date,
    file_size_bytes: ep.file_size_bytes, added_at: null,
    libraryId: ep.library_episode_id ?? undefined, recordingId: ep.recording_id ?? undefined,
    startTime: ep.start_time ?? undefined,
  }
}

interface ShowGroup {
  seriesId: number | null; seriesName: string; posterUrl: string | null; description: string | null
  categoryName: string | null; episodes: EpisodeItem[]
}
type LibraryDetail = { kind: 'show'; show: ShowGroup } | { kind: 'movie'; movie: LibraryMovie }

// Latest season first (matches Sonarr's own convention) -- episodes within
// a season sorted by episode number (recorded and upcoming interleaved in
// one continuous timeline), synthetic-identity episodes (season 0) sorted
// oldest-first by their timestamp-as-episode-number instead, which happens
// to fall out of the same numeric sort.
function groupSeasons(episodes: EpisodeItem[]) {
  const bySeason = new Map<number, EpisodeItem[]>()
  for (const e of episodes) {
    if (!bySeason.has(e.season_number)) bySeason.set(e.season_number, [])
    bySeason.get(e.season_number)!.push(e)
  }
  return [...bySeason.entries()]
    .map(([season, eps]) => ({ season, episodes: eps.sort((a, b) => a.episode_number - b.episode_number) }))
    .sort((a, b) => b.season - a.season)
}

// Provider poster_url values are plain http:// -- once the portal is served
// over https (a reverse proxy or Cloudflare Tunnel, the admin README's own
// recommended remote-access setup), the browser hard-blocks a http:// <img>
// on a https:// page as mixed content, so posters would silently render as
// nothing. Same fix as VodManager.tsx's posterSrc/PosterThumb, using the
// portal's own token/route since portal users aren't admins.
function portalPosterSrc(url: string): string {
  if (!url.startsWith('http://')) return url
  const token = localStorage.getItem('vodmanager-portal-session') ?? ''
  return `/api/portal/image-proxy?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}`
}

function PosterTile({ seed, posterUrl, size = 'w-full aspect-[2/3]' }: { seed: string; posterUrl?: string | null; size?: string }) {
  const [failed, setFailed] = useState(false)
  if (posterUrl && !failed) {
    return (
      <img
        src={portalPosterSrc(posterUrl)}
        alt=""
        loading="lazy"
        className={`${size} rounded-md object-cover shadow-sm`}
        onError={() => setFailed(true)}
      />
    )
  }
  return (
    <div className={`relative ${size} rounded-md bg-gradient-to-br ${posterGradient(seed)} flex items-center justify-center text-white font-bold text-2xl shadow-inner`}>
      {seed.slice(0, 1).toUpperCase()}
    </div>
  )
}

function ShowCard({ show, onOpen }: { show: ShowGroup; onOpen: () => void }) {
  const seasons = new Set(show.episodes.map((e) => e.season_number)).size
  const recorded = show.episodes.filter((e) => e.kind === 'recorded').length
  const upcoming = show.episodes.filter((e) => e.kind === 'upcoming').length
  return (
    <button onClick={onOpen} className="group text-left rounded-lg border border-border bg-card overflow-hidden shadow-sm hover:border-primary/40 transition-colors">
      <div className="relative">
        <PosterTile seed={show.seriesName} posterUrl={show.posterUrl} />
        {upcoming > 0 && (
          <span className="absolute top-1.5 right-1.5 text-[9px] font-bold uppercase tracking-wide bg-primary text-primary-foreground rounded-full px-1.5 py-0.5">
            {upcoming} upcoming
          </span>
        )}
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/25 transition-colors" />
      </div>
      <div className="p-2">
        <div className="text-xs font-semibold truncate">{show.seriesName}</div>
        <div className="text-[11px] text-muted-foreground">
          {seasons} season{seasons === 1 ? '' : 's'} · {recorded} recorded{upcoming > 0 ? ` · ${upcoming} upcoming` : ''}
        </div>
      </div>
    </button>
  )
}

function MovieCard({ movie, onOpen }: { movie: LibraryMovie; onOpen: () => void }) {
  return (
    <button onClick={onOpen} className="group text-left rounded-lg border border-border bg-card overflow-hidden shadow-sm hover:border-primary/40 transition-colors">
      <div className="relative">
        <PosterTile seed={movie.name} posterUrl={movie.poster_url} />
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/25 transition-colors" />
      </div>
      <div className="p-2">
        <div className="text-xs font-semibold truncate">{movie.name}</div>
        <div className="text-[11px] text-muted-foreground">{movie.year ?? formatBytes(movie.file_size_bytes)}</div>
      </div>
    </button>
  )
}

function LibraryStat({ value, label }: { value: string; label: string }) {
  return (
    <div className="px-2 py-2 text-center">
      <div className="text-sm font-bold truncate">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  )
}

function formatAirTime(startTime?: string): string {
  if (!startTime) return 'Scheduled'
  const d = new Date(startTime)
  const today = new Date()
  const isToday = d.toDateString() === today.toDateString()
  const dayLabel = isToday ? 'Today' : d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
  return `${dayLabel} · ${d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
}

// Pill selector, one per season -- picking a pill shows just that season's
// episodes below, Sonarr-style. Replaces an earlier checkbox+accordion
// design that technically also let you reach earlier seasons but wasn't
// what the user actually asked for ("a pill selector... if they wanted to
// see prior episodes") -- real correction, 2026-07-29.
function SeasonPills({
  seasons, selected, onSelect,
}: {
  seasons: { season: number; episodes: EpisodeItem[] }[]
  selected: number
  onSelect: (season: number) => void
}) {
  return (
    <div className="flex items-center gap-1.5 overflow-x-auto scrollbar-none pb-0.5">
      {seasons.map((s) => {
        const upcoming = s.episodes.filter((e) => e.kind === 'upcoming').length
        return (
          <button
            key={s.season}
            onClick={() => onSelect(s.season)}
            className={`shrink-0 text-xs font-semibold px-3 py-1.5 rounded-full border transition-colors ${
              selected === s.season
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-secondary/50 text-muted-foreground border-border hover:border-primary/40'
            }`}
          >
            {s.season > 0 ? `Season ${s.season}` : 'Recordings'}
            {upcoming > 0 && <span className="ml-1 opacity-75">· {upcoming}</span>}
          </button>
        )
      })}
    </div>
  )
}

function EpisodeRows({
  episodes, onPlay, onRemove, onCancelUpcoming,
}: {
  episodes: EpisodeItem[]
  onPlay: (kind: 'episode', id: number, title: string) => void
  onRemove: (v: { kind: 'episode'; id: number }) => void
  onCancelUpcoming: (recordingId: number) => void
}) {
  return (
    <div className="rounded-lg border border-border divide-y divide-border overflow-hidden">
      {episodes.map((e) => (
        <div key={e.key} className={`flex items-center gap-2 px-3 py-2 text-sm ${e.kind !== 'recorded' ? 'opacity-60' : ''}`}>
          <span className={`text-[10px] font-bold rounded px-1.5 py-0.5 shrink-0 min-w-[52px] text-center ${e.kind === 'recorded' ? 'text-primary bg-primary/10' : 'text-muted-foreground bg-secondary'}`}>
            {episodeTag(e)}
          </span>
          <div className="flex-1 min-w-0">
            <div className="font-medium truncate">{e.name}</div>
            {e.kind === 'upcoming' && <div className="text-[11px] text-muted-foreground truncate flex items-center gap-1"><Clock size={10} /> {formatAirTime(e.startTime)}</div>}
            {e.kind === 'recorded' && e.description && <div className="text-[11px] text-muted-foreground truncate">{e.description}</div>}
            {e.kind === 'missing' && <div className="text-[11px] text-muted-foreground truncate">{e.description ? `Aired ${e.description}` : 'Not recorded'}</div>}
          </div>
          {e.kind === 'recorded' && (
            <>
              <span className="text-[11px] text-muted-foreground shrink-0">{formatBytes(e.file_size_bytes)}</span>
              <button
                title="Play" className="w-7 h-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center shrink-0"
                onClick={() => onPlay('episode', e.libraryId!, `${e.season_number > 0 ? episodeTag(e) + ' — ' : ''}${e.name}`)}
              >
                <Play size={11} fill="currentColor" />
              </button>
              <button
                title="Remove from my library" className="text-muted-foreground hover:text-destructive p-1 shrink-0"
                onClick={() => askConfirm(`Remove "${e.name}" from your library?`, () => onRemove({ kind: 'episode', id: e.libraryId! }))}
              >
                <Trash2 size={12} />
              </button>
            </>
          )}
          {e.kind === 'upcoming' && (
            <button
              title="Cancel this recording" className="text-muted-foreground hover:text-destructive p-1 shrink-0"
              onClick={() => askConfirm('Cancel this upcoming recording?', () => onCancelUpcoming(e.recordingId!))}
            >
              <X size={13} />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

export default function Portal({ onLogout }: { onLogout: () => void }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<PortalTab>('recordings')
  const [nowPlaying, setNowPlaying] = useState<{ kind: 'movie' | 'episode'; id: number; title: string } | null>(null)

  const meQuery = useQuery<Me>({ queryKey: ['portal-me'], queryFn: () => portalApi.get('/me/').then((r) => r.data) })
  const [emailInput, setEmailInput] = useState('')
  useEffect(() => {
    if (meQuery.data) setEmailInput(meQuery.data.email ?? '')
  }, [meQuery.data?.email])
  const guideQuery = useQuery<GuideChannel[]>({
    queryKey: ['portal-guide'],
    queryFn:  () => portalApi.get('/guide/').then((r) => r.data),
    enabled:  tab === 'scheduler',
    staleTime: 60_000,
  })
  const [guideFilter, setGuideFilter] = useState('')
  const filteredGuide = (() => {
    const q = guideFilter.trim().toLowerCase()
    if (!q) return guideQuery.data ?? []
    // Word-boundary match on program titles, not a raw substring -- "my"
    // matching inside "Jimmy"/"Emmy"/"Academy"/"Enemy" (anywhere the two
    // letters appear, not just as a real word) is exactly why a search for
    // "MY" surfaced unrelated shows with no visible explanation why they
    // matched. \b(...) only matches at the start of a word, same as how a
    // real search box (VOD Manager's own catalog search, or Dispatcharr's)
    // behaves, and it's what "search for a channel or title" implies.
    const wordRe = new RegExp(`\\b${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'i')
    return (guideQuery.data ?? [])
      .map((row) => {
        // A channel matched by NAME shows its full program strip, not one
        // ALSO filtered by the same query -- searching a channel name and
        // getting that channel back with an empty/near-empty row (only
        // "matching" if a program title happened to also contain the text)
        // is exactly the "may or may not work" behavior reported; the two
        // filters were being applied together when they should be either/or.
        if (row.channel.name && wordRe.test(row.channel.name)) return row
        return { ...row, programs: row.programs.filter((p) => wordRe.test(p.title)) }
      })
      .filter((row) => (row.channel.name && wordRe.test(row.channel.name)) || row.programs.length > 0)
  })()
  // Scrolling one channel's program strip scrolls every other channel's
  // strip to the same horizontal position, matching a real time-grid guide
  // (Dispatcharr's own, or VOD Manager's admin guide) -- each row used to
  // be an independent overflow-x-auto div with no relationship to the
  // others, so scrolling forward in time on one channel left every other
  // channel showing whatever aired hours ago. isSyncingScroll guards
  // against the propagated scrollLeft writes below re-triggering this same
  // handler on each other row.
  const guideRowRefs = useRef(new Map<number, HTMLDivElement>())
  const isSyncingScroll = useRef(false)
  const syncGuideScroll = (sourceChannelId: number, scrollLeft: number) => {
    if (isSyncingScroll.current) return
    isSyncingScroll.current = true
    guideRowRefs.current.forEach((el, channelId) => {
      if (channelId !== sourceChannelId) el.scrollLeft = scrollLeft
    })
    isSyncingScroll.current = false
  }
  const myRecordingsQuery = useQuery<MyRecordings>({ queryKey: ['portal-my-recordings'], queryFn: () => portalApi.get('/my-recordings/').then((r) => r.data) })
  const upcomingQuery = useQuery<UpcomingRecording[]>({
    queryKey: ['portal-upcoming'],
    queryFn:  () => portalApi.get('/upcoming/').then((r) => r.data),
  })
  // Always loaded (not just on the Usage tab) -- the KPI row at the top of
  // every tab shows storage used, so this needs to be available from the
  // first paint, not lazily fetched only once someone clicks into Usage.
  const usageQuery = useQuery<UsageResponse>({
    queryKey: ['portal-usage'],
    queryFn:  () => portalApi.get('/usage/').then((r) => r.data),
  })
  // Recording rules/upcoming recordings only ever store a bare Dispatcharr
  // channel_id -- fetched once and looked up by id everywhere a channel
  // needs to be shown, so the UI never has to show "ch 32568" again.
  const channelsQuery = useQuery<Record<number, { name: string | null; channel_number: number | null }>>({
    queryKey: ['portal-channels'],
    queryFn:  () => portalApi.get('/channels/').then((r) => r.data),
  })
  function channelLabel(channelId: number | null | undefined): string {
    if (!channelId) return ''
    const ch = channelsQuery.data?.[channelId]
    if (!ch?.name) return `channel ${channelId}`
    return ch.channel_number != null ? `${ch.channel_number} · ${ch.name}` : ch.name
  }
  const libraryQuery = useQuery<{ movies: LibraryMovie[]; episodes: LibraryEpisode[] }>({
    queryKey: ['portal-library'],
    queryFn:  () => portalApi.get('/library/').then((r) => r.data),
    enabled:  tab === 'library',
  })
  // Grouped into category sections (matching Dispatcharr's own VOD catalog
  // categories, e.g. "Emby Movies"/"Emby TV Shows") and, for episodes, into
  // one card per show -- approved design, 2026-07-28, replacing the earlier
  // flat per-episode list.
  const movieSections = (() => {
    const byCategory = new Map<string, LibraryMovie[]>()
    for (const m of libraryQuery.data?.movies ?? []) {
      const key = m.category_name ?? 'Movies'
      if (!byCategory.has(key)) byCategory.set(key, [])
      byCategory.get(key)!.push(m)
    }
    return [...byCategory.entries()].map(([name, movies]) => ({ name, movies }))
  })()
  // Keyed by normalized title, not series_id -- a freshly-scheduled series
  // rule has no recorded episodes yet, so no VOD Manager series_id exists
  // for it at all (that only gets created once something actually
  // completes and imports). Title is the only identity both a recorded
  // LibraryEpisode and a raw /upcoming/ Dispatcharr recording share. Real
  // requirement from the user, 2026-07-29: scheduling a series should show
  // up in the Library immediately with its upcoming episodes, not just
  // silently wait for the first one to finish recording.
  const seriesSections = (() => {
    const byCategory = new Map<string, Map<string, ShowGroup>>()
    const getShow = (catKey: string, titleKey: string, seed: () => ShowGroup) => {
      if (!byCategory.has(catKey)) byCategory.set(catKey, new Map())
      const shows = byCategory.get(catKey)!
      if (!shows.has(titleKey)) shows.set(titleKey, seed())
      return shows.get(titleKey)!
    }
    for (const e of libraryQuery.data?.episodes ?? []) {
      const catKey = e.category_name ?? 'TV Shows'
      const show = getShow(catKey, e.series_name.toLowerCase(), () => ({
        seriesId: e.series_id, seriesName: e.series_name, posterUrl: e.series_poster_url,
        description: e.series_description, categoryName: e.category_name, episodes: [],
      }))
      show.episodes.push(episodeFromLibrary(e))
    }
    for (const u of upcomingQuery.data ?? []) {
      const title = u.custom_properties?.program?.title
      if (!title) continue
      // Match into an existing category if this title already has recorded
      // episodes somewhere; otherwise it's a brand-new series with nothing
      // recorded yet -- seed a fresh card for it under the generic "TV
      // Shows" bucket (no category placement exists yet either, since
      // nothing's been imported to place).
      let target: ShowGroup | null = null
      for (const shows of byCategory.values()) {
        const existing = shows.get(title.toLowerCase())
        if (existing) { target = existing; break }
      }
      if (!target) {
        target = getShow('TV Shows', title.toLowerCase(), () => ({
          seriesId: null, seriesName: title, posterUrl: null, description: null, categoryName: null, episodes: [],
        }))
      }
      target.episodes.push(episodeFromUpcoming(u))
    }
    return [...byCategory.entries()].map(([name, shows]) => ({ name, shows: [...shows.values()] }))
  })()
  const [libraryDetail, setLibraryDetail] = useState<LibraryDetail | null>(null)
  const [selectedSeason, setSelectedSeason] = useState<number | null>(null)
  function openLibraryDetail(detail: LibraryDetail) {
    setSelectedSeason(null) // re-derived from whichever seasons list actually renders (canonical or local-only)
    setLibraryDetail(detail)
  }
  // TMDB's full episode history for the open show, diffed against what
  // this person actually has -- see backend portal_show_canonical_episodes.
  // Only fetched once a show's modal is open and it has a real series_id
  // (a freshly-scheduled series with nothing recorded yet has none). Falls
  // back to local-only (recorded + upcoming) data below when this hasn't
  // loaded yet, errored, or the show has no tmdb_id (canonical: false).
  const openShowSeriesId = libraryDetail?.kind === 'show' ? libraryDetail.show.seriesId : null
  const canonicalEpisodesQuery = useQuery<{ canonical: boolean; episodes: CanonicalEpisode[] }>({
    queryKey: ['portal-show-episodes', openShowSeriesId],
    queryFn:  () => portalApi.get(`/library/shows/${openShowSeriesId}/episodes/`).then((r) => r.data),
    enabled:  openShowSeriesId != null,
  })
  function playLibraryItem(kind: 'movie' | 'episode', id: number, title: string) {
    setLibraryDetail(null)
    setNowPlaying({ kind, id, title })
  }

  // Bottom-sheet scheduling flow, opened from a tap on any Scheduler grid
  // program -- Record this episode (one-off, POST /schedule-single/, no
  // recurring rule) vs Record the series (POST /recording-rules/, creates a
  // dvr_recording_profiles row that keeps discovering new episodes).
  const [schedulingItem, setSchedulingItem] = useState<{ channel: GuideChannel['channel']; program: GuideProgram } | null>(null)
  const [scheduleChoice, setScheduleChoice] = useState<'once' | 'series' | null>(null)
  const [scheduleMode, setScheduleMode] = useState<'new' | 'all'>('new')
  const [scheduleLabel, setScheduleLabel] = useState('')
  const [scheduleError, setScheduleError] = useState<string | null>(null)
  // vod_manager-8p1.2: opt-in bulk backfill of already-aired episodes for a
  // brand-new series rule -- reuses whatever's already in the pool from a
  // regular provider, or this show's own known channel's EPG, same
  // conservative two-tier cascade the admin's own Missing Episodes resolve
  // already uses (never guesses a channel for an ambiguous match).
  const [scheduleBackfillPastSeasons, setScheduleBackfillPastSeasons] = useState(false)

  function openScheduling(channel: GuideChannel['channel'], program: GuideProgram) {
    setSchedulingItem({ channel, program })
    setScheduleChoice(null)
    setScheduleMode('new')
    setScheduleLabel('')
    setScheduleError(null)
    setScheduleBackfillPastSeasons(false)
  }
  function closeScheduling() { setSchedulingItem(null) }

  const scheduleSingle = useMutation({
    mutationFn: () => {
      const { channel, program } = schedulingItem!
      return portalApi.post('/schedule-single/', {
        channel_id: channel.id, program_id: program.id, title: program.title,
        sub_title: program.sub_title, tvg_id: program.tvg_id,
        start_time: program.start_time, end_time: program.end_time,
        season: program.season ?? null, episode: program.episode ?? null, onscreen_episode: program.onscreen_episode ?? null,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
      qc.invalidateQueries({ queryKey: ['portal-my-recordings'] })
      closeScheduling()
    },
    onError: (e: any) => setScheduleError(e?.response?.data?.detail ?? e.message ?? 'Failed to schedule.'),
  })
  const scheduleSeries = useMutation({
    mutationFn: () => {
      const { channel, program } = schedulingItem!
      return portalApi.post('/recording-rules/', {
        label: scheduleLabel.trim() || program.title, title: program.title,
        tvg_id: program.tvg_id || null, mode: scheduleMode, channel_id: channel.id,
        backfill_past_seasons: scheduleBackfillPastSeasons,
      })
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['portal-my-recordings'] })
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
      qc.invalidateQueries({ queryKey: ['portal-library'] })
      closeScheduling()
      // A rule with 0 matches right now is a legitimate, common outcome
      // (nothing in the 7-day EPG window yet -- it'll pick up the next
      // airing automatically), but staying silent about it looks
      // indistinguishable from the rule having failed or vanished. Real
      // confusion reported live, 2026-07-28.
      if (res.data?.scheduled_now === 0) {
        notify(`Saved "${res.data.label}" as a recording rule, but nothing matches it in the guide right now -- it'll start recording automatically the next time a matching episode airs (within the next 7 days).`)
      }
      const backfill = res.data?.past_seasons_backfill
      if (backfill?.available) {
        const already = backfill.episodes.filter((e: { status: string }) => e.status === 'already_in_pool').length
        const scheduled = backfill.episodes.filter((e: { status: string }) => e.status === 'scheduled').length
        const notFound = backfill.episodes.filter((e: { status: string }) => e.status === 'not_found').length
        notify(
          `Past seasons: ${already} already in your library, ${scheduled} newly scheduled` +
          (notFound ? `, ${notFound} not found yet (still watching for them)` : '') + '.'
        )
      } else if (backfill && !backfill.available && scheduleBackfillPastSeasons) {
        notify("Past seasons: not available for this show yet (it hasn't been catalogued with full episode info).")
      }
    },
    onError: (e: any) => setScheduleError(e?.response?.data?.detail ?? e.message ?? 'Failed to schedule.'),
  })
  function confirmScheduling() {
    if (scheduleChoice === 'once') scheduleSingle.mutate()
    else if (scheduleChoice === 'series') scheduleSeries.mutate()
  }

  const deleteRule = useMutation({
    mutationFn: (id: number) => portalApi.delete(`/recording-rules/${id}/`),
    onSuccess:  () => {
      qc.invalidateQueries({ queryKey: ['portal-my-recordings'] })
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
    },
  })
  const cancelUpcoming = useMutation({
    mutationFn: (id: number) => portalApi.delete(`/upcoming/${id}/`),
    onSuccess:  () => {
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
      qc.invalidateQueries({ queryKey: ['portal-my-recordings'] })
    },
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Failed to cancel.'),
  })
  // Removes only from THIS person's own Library -- if someone else also has
  // the same recording (e.g. two people's profiles matched the same
  // airing), it stays intact for them; the file on disk is only actually
  // deleted once nobody has it left (see the backend's reference-counted
  // remove_movie/episode_library_owner). Real requirement from the user,
  // 2026-07-28.
  const removeFromLibrary = useMutation({
    mutationFn: ({ kind, id }: { kind: 'movie' | 'episode'; id: number }) =>
      portalApi.delete(`/library/${kind === 'movie' ? 'movies' : 'episodes'}/${id}/`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['portal-library'] }),
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Failed to remove.'),
  })

  // Self-service email -- where THIS person's own DVR quota warnings go
  // (in addition to whatever admin recipients are configured), if the
  // admin has set up SMTP. Real requirement from the user, 2026-07-28
  // ('Both' -- admin always warned, the person themselves also does if
  // they've set an email). emailInput itself is declared near meQuery,
  // synced via useEffect once /me/ loads.
  const updateEmail = useMutation({
    mutationFn: (email: string | null) => portalApi.put('/me/email/', { email }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['portal-me'] }),
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Failed to save.'),
  })

  // Convert single<->series -- opened from a small "Make this a series" /
  // "Make this a single episode" action on each My Recordings row.
  const [converting, setConverting] = useState<
    { kind: 'toSeries'; recording: SingleRecording } | { kind: 'toSingle'; rule: RecordingRule } | null
  >(null)
  const [convertMode, setConvertMode] = useState<'new' | 'all'>('new')
  const [convertKeep, setConvertKeep] = useState<'next' | 'all'>('next')
  const [convertError, setConvertError] = useState<string | null>(null)

  const convertToSeries = useMutation({
    mutationFn: () => {
      const recording = (converting as { kind: 'toSeries'; recording: SingleRecording }).recording
      return portalApi.post(`/singles/${recording.id}/convert-to-series/`, { mode: convertMode })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portal-my-recordings'] })
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
      setConverting(null)
    },
    onError: (e: any) => setConvertError(e?.response?.data?.detail ?? e.message ?? 'Failed to convert.'),
  })
  const convertToSingle = useMutation({
    mutationFn: () => {
      const rule = (converting as { kind: 'toSingle'; rule: RecordingRule }).rule
      return portalApi.post(`/recording-rules/${rule.id}/convert-to-single/`, { keep: convertKeep })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portal-my-recordings'] })
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
      setConverting(null)
    },
    onError: (e: any) => setConvertError(e?.response?.data?.detail ?? e.message ?? 'Failed to convert.'),
  })
  function openConvert(item: typeof converting) {
    setConverting(item)
    setConvertMode('new')
    setConvertKeep('next')
    setConvertError(null)
  }

  const upcomingByDay = (() => {
    const groups = new Map<string, UpcomingRecording[]>()
    const sorted = [...(upcomingQuery.data ?? [])].sort((a, b) => a.start_time.localeCompare(b.start_time))
    for (const r of sorted) {
      const day = new Date(r.start_time).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
      if (!groups.has(day)) groups.set(day, [])
      groups.get(day)!.push(r)
    }
    return [...groups.entries()]
  })()

  function handleLogout() {
    portalApi.post('/auth/logout/').finally(() => {
      localStorage.removeItem('vodmanager-portal-session')
      onLogout()
    })
  }

  const NAV: { key: PortalTab; label: string; icon: React.ReactNode }[] = [
    { key: 'scheduler', label: 'Scheduler', icon: <SearchIcon size={14} /> },
    { key: 'recordings', label: 'My Recordings', icon: <CalendarDays size={14} /> },
    { key: 'upcoming', label: 'Upcoming', icon: <CalendarDays size={14} /> },
    { key: 'usage', label: 'Usage', icon: <HardDriveDownload size={14} /> },
    { key: 'library', label: 'Library', icon: <Film size={14} /> },
    { key: 'account', label: 'Account', icon: <User size={14} /> },
  ]

  const actualGB = usageQuery.data ? usageQuery.data.actual_bytes / 1024 ** 3 : null
  const virtualGB = usageQuery.data ? usageQuery.data.virtual_bytes / 1024 ** 3 : null
  const quotaGB = usageQuery.data?.disk_quota_bytes != null ? usageQuery.data.disk_quota_bytes / 1024 ** 3 : null

  const nextUp = upcomingByDay[0]?.[1]?.[0]
  const activeRuleCount = myRecordingsQuery.data?.series.length ?? 0
  const upcomingCount = upcomingQuery.data?.length ?? 0

  return (
    <div className="min-h-screen flex flex-col bg-background">
      <header className="sticky top-0 z-10 border-b border-border bg-card">
        <div className="flex items-center gap-3 px-5 py-3 max-w-5xl w-full mx-auto">
          <div className="w-9 h-9 rounded-xl shrink-0 bg-primary/85 border border-primary/30 flex items-center justify-center">
            <img src="/favicon.svg" width={18} height={18} alt="" className="brightness-0 invert opacity-90" />
          </div>
          <div className="hidden sm:block">
            <div className="text-base font-bold leading-tight tracking-tight">VOD & DVR Manager</div>
            {meQuery.data && (
              <div className="text-xs text-muted-foreground leading-tight">
                {meQuery.data.provider_name ?? 'DVR portal'}
              </div>
            )}
          </div>
          <div className="flex-1" />
          <div className="flex items-center gap-0.5 rounded-lg border border-border p-0.5 bg-background/60">
            {NAV.map((n) => (
              <button
                key={n.key}
                onClick={() => setTab(n.key)}
                title={n.label}
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-sm border transition-colors ${
                  tab === n.key
                    ? 'bg-primary/85 text-foreground border-primary/30 font-bold'
                    : 'text-muted-foreground border-transparent hover:text-foreground hover:bg-accent font-medium'
                }`}
              >
                {n.icon}<span className="hidden sm:inline">{n.label}</span>
              </button>
            ))}
          </div>
          {meQuery.data && (
            <div className="flex items-center gap-2 pl-2 border-l border-border">
              <div className="w-7 h-7 rounded-full bg-primary/85 flex items-center justify-center text-xs font-bold text-foreground shrink-0">
                {(meQuery.data.dispatcharr_username ?? meQuery.data.username).slice(0, 1).toUpperCase()}
              </div>
              <span className="text-sm font-semibold hidden sm:inline">{meQuery.data.dispatcharr_username ?? meQuery.data.username}</span>
            </div>
          )}
          <button className="text-muted-foreground hover:text-foreground p-1.5 rounded-md hover:bg-accent" title="Sign out" onClick={handleLogout}>
            <LogOut size={15} />
          </button>
        </div>
      </header>

      <main className="flex-1 p-4 max-w-5xl w-full mx-auto space-y-4">

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
          <KpiTile
            icon={<CalendarDays />} label="Upcoming"
            value={upcomingCount}
            note={nextUp ? `next: ${new Date(nextUp.start_time).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })}` : 'nothing scheduled'}
          />
          <KpiTile
            icon={<ListVideo />} label="Active rules"
            value={activeRuleCount}
            note={activeRuleCount ? 'recording automatically' : 'search below to add one'}
          />
          <KpiTile
            icon={<HardDriveDownload />} label="Storage used"
            value={actualGB != null ? `${(actualGB + (virtualGB ?? 0)).toFixed(1)} GB` : '—'}
            note={quotaGB != null ? `of ${quotaGB.toFixed(0)} GB quota` : 'no quota set'}
            noteTone={quotaGB != null && actualGB != null && virtualGB != null && (actualGB + virtualGB) / quotaGB > 0.9 ? 'warn' : 'default'}
          />
          <KpiTile
            icon={<Clock />} label="Stream budget"
            value={usageQuery.data ? Math.max(0, (usageQuery.data.stream_limit ?? 0) - usageQuery.data.stream_reserve) : '—'}
            note={usageQuery.data ? `${usageQuery.data.stream_reserve} reserved of ${usageQuery.data.stream_limit ?? 'unlimited'}` : ''}
          />
        </div>

        {tab === 'scheduler' && (
          <SectionCard title="Scheduler" icon={<SearchIcon size={14} />}>
            <p className="text-sm text-muted-foreground">
              Everything airing in the next 24 hours across your lineup. Tap anything to schedule it.
            </p>
            <input
              className={inputCls('w-full')}
              placeholder="Filter by channel or title…"
              value={guideFilter}
              onChange={(e) => setGuideFilter(e.target.value)}
            />
            {guideQuery.isLoading && <p className="text-sm text-muted-foreground">Loading guide…</p>}
            {guideQuery.isError && (
              <p className="text-sm text-destructive">Couldn't load the guide: {queryErrorMessage(guideQuery.error)}</p>
            )}
            {guideQuery.data && !filteredGuide.length && (
              <p className="text-sm text-muted-foreground">
                {guideFilter ? 'Nothing matches that filter.' : 'No channels in your lineup right now.'}
              </p>
            )}
            <div className="space-y-2 max-h-[70vh] overflow-y-auto pr-1 scrollbar-none">
              {filteredGuide.map(({ channel, programs }) => (
                <div key={channel.id} className="rounded-lg border border-border bg-card p-2.5">
                  <div className="text-sm font-semibold mb-1.5 flex items-center gap-1.5">
                    <span className="font-mono text-muted-foreground text-xs w-8">{channel.channel_number ?? '?'}</span>
                    {channel.name}
                  </div>
                  <div
                    className="flex gap-1.5 overflow-x-auto scrollbar-none pb-0.5"
                    ref={(el) => {
                      if (el) guideRowRefs.current.set(channel.id, el)
                      else guideRowRefs.current.delete(channel.id)
                    }}
                    onScroll={(e) => syncGuideScroll(channel.id, e.currentTarget.scrollLeft)}
                  >
                    {programs.map((p) => (
                      <button
                        key={p.id}
                        onClick={() => openScheduling(channel, p)}
                        className="text-left shrink-0 w-32 rounded-md border border-border bg-background/60 hover:border-primary/40 hover:bg-accent transition-colors px-2 py-1.5"
                        title={p.sub_title ?? undefined}
                      >
                        <div className="text-xs font-semibold truncate flex items-center gap-1">
                          {p.is_new && <span className="w-1.5 h-1.5 rounded-full bg-primary shrink-0" title="New episode" />}
                          <span className="truncate">{p.title}</span>
                        </div>
                        <div className="text-[11px] font-mono text-muted-foreground tabular-nums">
                          {new Date(p.start_time).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </SectionCard>
        )}

        {tab === 'recordings' && (
          <SectionCard title="My Recordings" icon={<CalendarDays size={14} />}>
            <p className="text-sm text-muted-foreground">
              What's already scheduled. Head to Scheduler to add something new.
            </p>
            {myRecordingsQuery.isError && (
              <p className="text-sm text-destructive">Couldn't load your recordings: {queryErrorMessage(myRecordingsQuery.error)}</p>
            )}
            {myRecordingsQuery.data && !myRecordingsQuery.data.series.length && !myRecordingsQuery.data.singles.length && (
              <p className="text-sm text-muted-foreground">Nothing scheduled yet -- go to Scheduler to add one.</p>
            )}
            <div className="space-y-1.5">
              {myRecordingsQuery.data?.series.map((rule) => (
                <div key={`s-${rule.id}`} className="rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                  <div className="flex items-center gap-2 text-sm">
                    <div className="flex-1 min-w-0 truncate">
                      <span className="font-semibold">{rule.label}</span>{' '}
                      <span className="text-muted-foreground">— "{rule.title}"</span>
                    </div>
                    {rule.mode === 'new'
                      ? <StatusPill tone="info" label="New only" />
                      : <Chip>All episodes</Chip>}
                    {rule.channel_id && <Chip>{channelLabel(rule.channel_id)}</Chip>}
                    <button
                      title="Delete this recording rule"
                      className="text-muted-foreground hover:text-destructive p-1"
                      onClick={() => askConfirm(`Delete "${rule.label}"? This also cancels its future recordings.`, () => deleteRule.mutate(rule.id))}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                  <button
                    className="mt-1 text-xs text-muted-foreground hover:text-primary underline-offset-2 hover:underline"
                    onClick={() => openConvert({ kind: 'toSingle', rule })}
                  >
                    Make this a single episode →
                  </button>
                </div>
              ))}
              {myRecordingsQuery.data?.singles.map((rec) => (
                <div key={`r-${rec.id}`} className="rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                  <div className="flex items-center gap-2 text-sm">
                    <div className="flex-1 min-w-0 truncate">
                      <span className="font-semibold">{rec.title}</span>
                      {rec.sub_title && <span className="text-muted-foreground"> — {rec.sub_title}</span>}
                    </div>
                    <Chip>Single episode</Chip>
                    {rec.channel_id && <Chip>{channelLabel(rec.channel_id)}</Chip>}
                    <button
                      title="Delete this recording"
                      className="text-muted-foreground hover:text-destructive p-1"
                      onClick={() => askConfirm(`Delete "${rec.title}"?`, () => cancelUpcoming.mutate(rec.id))}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                  <button
                    className="mt-1 text-xs text-muted-foreground hover:text-primary underline-offset-2 hover:underline"
                    onClick={() => openConvert({ kind: 'toSeries', recording: rec })}
                  >
                    Make this a series →
                  </button>
                </div>
              ))}
            </div>
          </SectionCard>
        )}

        {tab === 'upcoming' && (
          <SectionCard title="Upcoming Recordings" icon={<CalendarDays size={14} />}>
            {upcomingQuery.isError && (
              <p className="text-sm text-destructive">Couldn't load upcoming recordings: {queryErrorMessage(upcomingQuery.error)}</p>
            )}
            {upcomingQuery.data && !upcomingQuery.data.length && (
              <p className="text-sm text-muted-foreground">Nothing scheduled right now.</p>
            )}
            <div className="space-y-3">
              {upcomingByDay.map(([day, items]) => {
                const isToday = day === new Date().toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
                return (
                  <div key={day}>
                    <div className="flex items-center gap-1.5 mb-1">
                      <p className="text-xs font-medium text-muted-foreground">{day}</p>
                      {isToday && <StatusPill tone="success" label="Today" />}
                    </div>
                    <div className="space-y-1">
                      {items.map((r) => (
                        <div key={r.id} className="flex items-center gap-2 text-sm rounded-md border border-border bg-card px-2.5 py-1.5">
                          <span className="font-mono text-muted-foreground w-16 shrink-0 tabular-nums">
                            {new Date(r.start_time).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
                          </span>
                          <span className="flex-1 min-w-0 truncate">
                            <span className="font-semibold">{r.custom_properties?.program?.title ?? '?'}</span>
                            {r.custom_properties?.program?.sub_title && (
                              <span className="text-muted-foreground"> — {r.custom_properties.program.sub_title}</span>
                            )}
                          </span>
                          <Chip>{channelLabel(r.channel)}</Chip>
                          <button
                            title="Cancel this recording"
                            className="text-muted-foreground hover:text-destructive p-1"
                            onClick={() => askConfirm(`Cancel "${r.custom_properties?.program?.title ?? 'this recording'}"?`, () => cancelUpcoming.mutate(r.id))}
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          </SectionCard>
        )}

        {tab === 'usage' && (
          <SectionCard title="Usage" icon={<HardDriveDownload size={14} />}>
            {usageQuery.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
            {usageQuery.isError && (
              <p className="text-sm text-destructive">Couldn't load usage: {queryErrorMessage(usageQuery.error)}</p>
            )}
            {usageQuery.data && (
              <>
                <QuotaBar actualGB={actualGB} virtualGB={virtualGB} quotaGB={quotaGB} />
                <div className="flex items-center gap-4 text-sm text-muted-foreground pt-1">
                  <span>Stream reserve: {usageQuery.data.stream_reserve}</span>
                  <span>Stream limit: {usageQuery.data.stream_limit ?? 'unlimited'}</span>
                </div>
              </>
            )}
          </SectionCard>
        )}

        {tab === 'library' && (
          <SectionCard title="Library" icon={<Film size={14} />}>
            {libraryQuery.isError && (
              <p className="text-sm text-destructive">Couldn't load your library: {queryErrorMessage(libraryQuery.error)}</p>
            )}
            {libraryQuery.data && !libraryQuery.data.movies.length && !libraryQuery.data.episodes.length && (
              <p className="text-sm text-muted-foreground">Nothing recorded yet.</p>
            )}
            {seriesSections.map((section) => (
              <div key={`tv-${section.name}`} className="space-y-2">
                <div className="flex items-center gap-2 pt-1">
                  <h3 className="text-sm font-semibold">{section.name}</h3>
                  <span className="text-xs text-muted-foreground">{section.shows.length} show{section.shows.length === 1 ? '' : 's'}</span>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
                  {section.shows.map((show) => (
                    <ShowCard key={show.seriesId} show={show} onOpen={() => openLibraryDetail({ kind: 'show', show })} />
                  ))}
                </div>
              </div>
            ))}
            {movieSections.map((section) => (
              <div key={`mv-${section.name}`} className="space-y-2">
                <div className="flex items-center gap-2 pt-1">
                  <h3 className="text-sm font-semibold">{section.name}</h3>
                  <span className="text-xs text-muted-foreground">{section.movies.length} movie{section.movies.length === 1 ? '' : 's'}</span>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
                  {section.movies.map((m) => (
                    <MovieCard key={m.id} movie={m} onOpen={() => openLibraryDetail({ kind: 'movie', movie: m })} />
                  ))}
                </div>
              </div>
            ))}
          </SectionCard>
        )}

        {libraryDetail && libraryDetail.kind === 'show' && (() => {
          const show = libraryDetail.show
          const canonical = canonicalEpisodesQuery.data?.canonical ? canonicalEpisodesQuery.data.episodes : null
          const seasons = groupSeasons(canonical ? canonical.map(episodeFromCanonical) : show.episodes)
          const recorded = show.episodes.filter((e) => e.kind === 'recorded')
          const upcomingCount = show.episodes.length - recorded.length
          const totalBytes = recorded.reduce((n, e) => n + (e.file_size_bytes ?? 0), 0)
          const lastRecorded = recorded.reduce((max, e) => ((e.added_at ?? '') > max ? (e.added_at ?? '') : max), '')
          return (
            <div className="fixed inset-0 z-50 bg-black/55 flex items-end sm:items-center justify-center" onClick={() => setLibraryDetail(null)}>
              <div className="w-full sm:max-w-xl sm:rounded-2xl rounded-t-2xl border border-border bg-card max-h-[88vh] overflow-y-auto scrollbar-none" onClick={(e) => e.stopPropagation()}>
                <div className="w-9 h-1 rounded-full bg-border mx-auto mt-2.5 mb-1 sm:hidden" />
                <div className={`relative p-4 pb-3 bg-gradient-to-br ${posterGradient(show.seriesName)} text-white`}>
                  <button className="absolute top-3 right-3 w-7 h-7 rounded-full bg-black/30 hover:bg-black/50 flex items-center justify-center" onClick={() => setLibraryDetail(null)}>
                    <X size={14} />
                  </button>
                  <div className="text-[10px] font-bold uppercase tracking-wide bg-white/20 inline-block px-2 py-0.5 rounded-full mb-1.5">
                    {show.categoryName ?? 'TV Shows'}
                  </div>
                  <h2 className="text-lg font-bold leading-tight pr-8">{show.seriesName}</h2>
                  <p className="text-xs text-white/75 mt-0.5">
                    {seasons.length} season{seasons.length === 1 ? '' : 's'}{upcomingCount > 0 ? ` · ${upcomingCount} upcoming` : ''}
                  </p>
                </div>
                <div className="grid grid-cols-4 divide-x divide-border border-b border-border">
                  <LibraryStat value={String(seasons.length)} label="Seasons" />
                  <LibraryStat value={String(recorded.length)} label="Recorded" />
                  <LibraryStat value={formatBytes(totalBytes)} label="Total size" />
                  <LibraryStat value={lastRecorded ? formatAddedAt(lastRecorded) : '—'} label="Last recorded" />
                </div>
                <div className="p-4 space-y-3">
                  {show.description && <p className="text-sm text-muted-foreground">{show.description}</p>}
                  {canonicalEpisodesQuery.isLoading && openShowSeriesId != null && (
                    <p className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <Loader2 size={11} className="animate-spin" /> Loading full season history…
                    </p>
                  )}
                  <SeasonPills seasons={seasons} selected={selectedSeason ?? seasons[0].season} onSelect={setSelectedSeason} />
                  <EpisodeRows
                    episodes={(seasons.find((s) => s.season === selectedSeason) ?? seasons[0]).episodes}
                    onPlay={playLibraryItem}
                    onRemove={removeFromLibrary.mutate}
                    onCancelUpcoming={cancelUpcoming.mutate}
                  />
                </div>
              </div>
            </div>
          )
        })()}

        {libraryDetail && libraryDetail.kind === 'movie' && (() => {
          const m = libraryDetail.movie
          return (
            <div className="fixed inset-0 z-50 bg-black/55 flex items-end sm:items-center justify-center" onClick={() => setLibraryDetail(null)}>
              <div className="w-full sm:max-w-md sm:rounded-2xl rounded-t-2xl border border-border bg-card max-h-[88vh] overflow-y-auto scrollbar-none" onClick={(e) => e.stopPropagation()}>
                <div className="w-9 h-1 rounded-full bg-border mx-auto mt-2.5 mb-1 sm:hidden" />
                <div className={`relative p-4 pb-3 bg-gradient-to-br ${posterGradient(m.name)} text-white`}>
                  <button className="absolute top-3 right-3 w-7 h-7 rounded-full bg-black/30 hover:bg-black/50 flex items-center justify-center" onClick={() => setLibraryDetail(null)}>
                    <X size={14} />
                  </button>
                  <div className="text-[10px] font-bold uppercase tracking-wide bg-white/20 inline-block px-2 py-0.5 rounded-full mb-1.5">
                    {m.category_name ?? 'Movies'}
                  </div>
                  <h2 className="text-lg font-bold leading-tight pr-8">{m.name}</h2>
                  <p className="text-xs text-white/75 mt-0.5">{m.year ?? ''}</p>
                </div>
                <div className="grid grid-cols-2 divide-x divide-border border-b border-border">
                  <LibraryStat value={formatBytes(m.file_size_bytes)} label="Size" />
                  <LibraryStat value={formatAddedAt(m.added_at)} label="Recorded" />
                </div>
                <div className="p-4 space-y-3">
                  {m.description && <p className="text-sm text-muted-foreground">{m.description}</p>}
                  <div className="flex items-center gap-1.5">
                    <Button className="flex-1" onClick={() => playLibraryItem('movie', m.id, m.name)}>
                      <Play size={13} className="mr-1.5" fill="currentColor" /> Play
                    </Button>
                    <button
                      title="Remove from my library"
                      className="text-muted-foreground hover:text-destructive p-2"
                      onClick={() => askConfirm(`Remove "${m.name}" from your library?`, () => { removeFromLibrary.mutate({ kind: 'movie', id: m.id }); setLibraryDetail(null) })}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )
        })()}

        {tab === 'account' && (
          <SectionCard title="Account" icon={<User size={14} />}>
            <p className="text-sm text-muted-foreground">
              Signed in as <span className="font-semibold text-foreground">{meQuery.data?.dispatcharr_username ?? meQuery.data?.username}</span> on{' '}
              {meQuery.data?.provider_name ?? 'this DVR'}.
            </p>
            <div className="space-y-1.5 pt-1">
              <label className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
                <Mail size={12} /> Email
              </label>
              <p className="text-xs text-muted-foreground">
                If the admin has set up notifications, storage quota warnings for your own account go here too
                (in addition to the admin). Leave blank if you'd rather not get them.
              </p>
              <div className="flex items-center gap-1.5">
                <input
                  className={inputCls('flex-1')}
                  type="email"
                  placeholder="you@example.com"
                  value={emailInput}
                  onChange={(e) => setEmailInput(e.target.value)}
                />
                <Button
                  size="sm"
                  disabled={updateEmail.isPending}
                  onClick={() => updateEmail.mutate(emailInput.trim() || null)}
                >
                  {updateEmail.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
                </Button>
              </div>
            </div>
          </SectionCard>
        )}

      </main>

      {converting && (
        <div className="fixed inset-0 z-50 bg-black/55 flex items-end sm:items-center justify-center" onClick={() => setConverting(null)}>
          <div
            className="w-full sm:max-w-sm sm:rounded-2xl rounded-t-2xl border border-border bg-card p-4 pb-6 sm:pb-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="w-9 h-1 rounded-full bg-border mx-auto mb-3.5 sm:hidden" />
            {converting.kind === 'toSeries' ? (
              <>
                <div className="text-sm font-bold mb-1">Make "{converting.recording.title}" a series?</div>
                <p className="text-xs text-muted-foreground mb-3">
                  The episode already scheduled stays as-is. New episodes get found and scheduled automatically from now on.
                </p>
                <p className="text-xs font-bold text-muted-foreground mb-1.5">Which episodes going forward?</p>
                <label className="flex items-center gap-2 text-sm mb-1">
                  <input type="radio" checked={convertMode === 'new'} onChange={() => setConvertMode('new')} /> New episodes only
                </label>
                <label className="flex items-center gap-2 text-sm mb-3">
                  <input type="radio" checked={convertMode === 'all'} onChange={() => setConvertMode('all')} /> All episodes (incl. reruns)
                </label>
                {convertError && <p className="text-sm text-destructive mb-2">{convertError}</p>}
                <div className="flex justify-end gap-2">
                  <Button size="sm" variant="outline" onClick={() => setConverting(null)}>Cancel</Button>
                  <Button size="sm" disabled={convertToSeries.isPending} onClick={() => convertToSeries.mutate()}>
                    {convertToSeries.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Make it a series'}
                  </Button>
                </div>
              </>
            ) : (
              <>
                <div className="text-sm font-bold mb-1">Make "{converting.rule.label}" a single episode?</div>
                <p className="text-xs text-muted-foreground mb-3">
                  This stops finding new episodes going forward. What should happen to episodes already scheduled?
                </p>
                <label className="flex items-center gap-2 text-sm mb-1">
                  <input type="radio" checked={convertKeep === 'next'} onChange={() => setConvertKeep('next')} /> Keep only the next one, cancel the rest
                </label>
                <label className="flex items-center gap-2 text-sm mb-3">
                  <input type="radio" checked={convertKeep === 'all'} onChange={() => setConvertKeep('all')} /> Keep all already-scheduled episodes
                </label>
                {convertError && <p className="text-sm text-destructive mb-2">{convertError}</p>}
                <div className="flex justify-end gap-2">
                  <Button size="sm" variant="outline" onClick={() => setConverting(null)}>Cancel</Button>
                  <Button size="sm" disabled={convertToSingle.isPending} onClick={() => convertToSingle.mutate()}>
                    {convertToSingle.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Make it a single episode'}
                  </Button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {schedulingItem && (
        <div className="fixed inset-0 z-50 bg-black/55 flex items-end sm:items-center justify-center" onClick={closeScheduling}>
          <div
            className="w-full sm:max-w-md sm:rounded-2xl rounded-t-2xl border border-border bg-card p-4 pb-6 sm:pb-4 max-h-[85vh] overflow-y-auto scrollbar-none"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="w-9 h-1 rounded-full bg-border mx-auto mb-3.5 sm:hidden" />
            <div className="text-base font-bold">{schedulingItem.program.title}</div>
            <div className="text-xs text-muted-foreground mt-0.5 mb-4">
              {schedulingItem.program.sub_title ? `${schedulingItem.program.sub_title} — ` : ''}
              {new Date(schedulingItem.program.start_time).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })}
              {' · '}{schedulingItem.channel.channel_number ?? '?'} · {schedulingItem.channel.name}
            </div>

            <p className="text-xs font-bold text-muted-foreground mb-1.5">What do you want to do?</p>
            <div className="grid grid-cols-2 gap-2 mb-3">
              <button
                onClick={() => setScheduleChoice('once')}
                className={`text-left rounded-lg border px-3 py-2 transition-colors ${scheduleChoice === 'once' ? 'border-primary bg-primary/15' : 'border-border bg-background'}`}
              >
                <div className="text-sm font-bold">Record this episode</div>
                <div className="text-xs text-muted-foreground">Just this one airing</div>
              </button>
              <button
                onClick={() => setScheduleChoice('series')}
                className={`text-left rounded-lg border px-3 py-2 transition-colors ${scheduleChoice === 'series' ? 'border-primary bg-primary/15' : 'border-border bg-background'}`}
              >
                <div className="text-sm font-bold">Record the series</div>
                <div className="text-xs text-muted-foreground">Every future episode</div>
              </button>
            </div>

            {scheduleChoice === 'series' && (
              <div className="rounded-lg border border-border bg-background p-2.5 mb-3 space-y-1.5">
                <p className="text-xs font-bold text-muted-foreground">Which episodes?</p>
                <label className="flex items-center gap-2 text-sm">
                  <input type="radio" checked={scheduleMode === 'new'} onChange={() => setScheduleMode('new')} /> New episodes only
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input type="radio" checked={scheduleMode === 'all'} onChange={() => setScheduleMode('all')} /> All episodes (incl. reruns)
                </label>
                <input
                  className={inputCls('w-full mt-1')}
                  placeholder={`Label (defaults to "${schedulingItem.program.title}")`}
                  value={scheduleLabel}
                  onChange={(e) => setScheduleLabel(e.target.value)}
                />
                <label className="flex items-center gap-2 text-sm pt-1 border-t border-border/50 mt-1.5">
                  <input
                    type="checkbox" checked={scheduleBackfillPastSeasons}
                    onChange={(e) => setScheduleBackfillPastSeasons(e.target.checked)}
                  />
                  Also grab past seasons
                </label>
                {scheduleBackfillPastSeasons && (
                  <p className="text-[11px] text-muted-foreground">
                    Uses whatever's already in the library, or this show's usual channel's guide -- won't guess a
                    different channel for you. Anything it can't find stays on the watch list for later.
                  </p>
                )}
              </div>
            )}

            {scheduleError && <p className="text-sm text-destructive mb-2">{scheduleError}</p>}

            <div className="flex justify-end gap-2">
              <Button size="sm" variant="outline" onClick={closeScheduling}>Cancel</Button>
              <Button
                size="sm" disabled={!scheduleChoice || scheduleSingle.isPending || scheduleSeries.isPending}
                onClick={confirmScheduling}
              >
                {(scheduleSingle.isPending || scheduleSeries.isPending)
                  ? <Loader2 size={12} className="animate-spin" />
                  : <><Plus size={12} className="mr-1" /> Schedule it</>}
              </Button>
            </div>
          </div>
        </div>
      )}

      {nowPlaying && (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4" onClick={() => setNowPlaying(null)}>
          <div className="w-full max-w-3xl space-y-2" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between text-white text-sm">
              <span>{nowPlaying.title}</span>
              <button onClick={() => setNowPlaying(null)} className="p-1 hover:bg-white/10 rounded"><X size={16} /></button>
            </div>
            <video
              key={`${nowPlaying.kind}-${nowPlaying.id}`}
              className="w-full max-h-[75vh] rounded-md bg-black"
              controls
              autoPlay
              src={streamUrl(nowPlaying.kind, nowPlaying.id)}
            />
          </div>
        </div>
      )}
      <ConfirmDialogHost />
      <NotifyDialogHost />
    </div>
  )
}
