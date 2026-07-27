import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, Film, HardDriveDownload, Loader2, LogOut, Play, Plus, Search as SearchIcon, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Chip, inputCls, QuotaBar, SectionCard } from '@/components/dvr-shared'
import portalApi from '@/lib/portalApi'

type PortalTab = 'recordings' | 'upcoming' | 'usage' | 'library'

interface RecordingRule {
  id: number
  label: string
  title: string
  tvg_id: string | null
  mode: string
  channel_id: number | null
  created_at: string
}
interface EpgProgram {
  id: number
  title: string
  sub_title: string | null
  start_time: string
  end_time: string
  tvg_id: string
  channels: { id: number; name: string; channel_number: number | null; channel_group: string | null; tvg_id: string }[]
}
interface UpcomingRecording {
  id: number
  channel: number
  start_time: string
  end_time: string
  custom_properties?: { program?: { title?: string; sub_title?: string | null } }
}
interface UsageResponse {
  actual_bytes: number
  virtual_bytes: number
  total_bytes: number
  disk_quota_bytes: number | null
  stream_reserve: number
  stream_limit: number | null
}
interface LibraryMovie { id: number; name: string; year: number | null; poster_url: string | null; duration_secs: number | null }
interface LibraryEpisode {
  id: number; name: string; season_number: number; episode_number: number; duration_secs: number | null
  series_id: number; series_name: string; series_poster_url: string | null
}
interface Me { username: string; dispatcharr_username: string | null; provider_name: string | null }

function streamUrl(kind: 'movie' | 'episode', id: number) {
  const token = localStorage.getItem('vodmanager-portal-session') ?? ''
  return `/api/portal/library/${kind}/${id}/stream/?token=${encodeURIComponent(token)}`
}

function queryErrorMessage(err: any): string {
  return err?.response?.data?.detail ?? err?.message ?? 'Something went wrong.'
}

export default function Portal({ onLogout }: { onLogout: () => void }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<PortalTab>('recordings')
  const [nowPlaying, setNowPlaying] = useState<{ kind: 'movie' | 'episode'; id: number; title: string } | null>(null)

  const meQuery = useQuery<Me>({ queryKey: ['portal-me'], queryFn: () => portalApi.get('/me/').then((r) => r.data) })
  const rulesQuery = useQuery<RecordingRule[]>({ queryKey: ['portal-rules'], queryFn: () => portalApi.get('/recording-rules/').then((r) => r.data) })
  const upcomingQuery = useQuery<UpcomingRecording[]>({
    queryKey: ['portal-upcoming'],
    queryFn:  () => portalApi.get('/upcoming/').then((r) => r.data),
  })
  const usageQuery = useQuery<UsageResponse>({
    queryKey: ['portal-usage'],
    queryFn:  () => portalApi.get('/usage/').then((r) => r.data),
    enabled:  tab === 'usage',
  })
  const libraryQuery = useQuery<{ movies: LibraryMovie[]; episodes: LibraryEpisode[] }>({
    queryKey: ['portal-library'],
    queryFn:  () => portalApi.get('/library/').then((r) => r.data),
    enabled:  tab === 'library',
  })

  const [searchTitle, setSearchTitle] = useState('')
  const [ruleLabel, setRuleLabel] = useState('')
  const [ruleError, setRuleError] = useState<string | null>(null)
  const [picked, setPicked] = useState<{ title: string; tvg_id: string; channel_id: number; channel_label: string } | null>(null)

  const search = useMutation({
    mutationFn: () => portalApi.get<EpgProgram[]>('/epg-search/', { params: { title: searchTitle.trim() } }).then((r) => r.data),
    onError:    (e: any) => setRuleError(e?.response?.data?.detail ?? e.message ?? 'Search failed.'),
  })
  const channelGroups = (() => {
    const byChannel = new Map<number, { channel: EpgProgram['channels'][number]; programs: EpgProgram[] }>()
    for (const program of search.data ?? []) {
      for (const ch of program.channels ?? []) {
        if (!byChannel.has(ch.id)) byChannel.set(ch.id, { channel: ch, programs: [] })
        byChannel.get(ch.id)!.programs.push(program)
      }
    }
    return [...byChannel.values()].sort((a, b) => (a.channel.channel_number ?? 0) - (b.channel.channel_number ?? 0))
  })()
  function pickChannel(channel: EpgProgram['channels'][number], programs: EpgProgram[]) {
    const first = programs[0]
    setPicked({ title: first.title, tvg_id: first.tvg_id, channel_id: channel.id, channel_label: `${channel.channel_number ?? '?'} · ${channel.name}` })
    setSearchTitle('')
    search.reset()
  }

  const addRule = useMutation({
    mutationFn: () => portalApi.post('/recording-rules/', {
      label: ruleLabel.trim() || picked!.title,
      title: picked!.title,
      tvg_id: picked!.tvg_id || null,
      mode: 'all',
      channel_id: picked!.channel_id,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portal-rules'] })
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
      setPicked(null)
      setRuleLabel('')
      setRuleError(null)
    },
    onError: (e: any) => setRuleError(e?.response?.data?.detail ?? e.message ?? 'Save failed.'),
  })
  const deleteRule = useMutation({
    mutationFn: (id: number) => portalApi.delete(`/recording-rules/${id}/`),
    onSuccess:  () => {
      qc.invalidateQueries({ queryKey: ['portal-rules'] })
      qc.invalidateQueries({ queryKey: ['portal-upcoming'] })
    },
  })

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
    { key: 'recordings', label: 'My Recordings', icon: <CalendarDays size={14} /> },
    { key: 'upcoming', label: 'Upcoming', icon: <CalendarDays size={14} /> },
    { key: 'usage', label: 'Usage', icon: <HardDriveDownload size={14} /> },
    { key: 'library', label: 'Library', icon: <Film size={14} /> },
  ]

  const actualGB = usageQuery.data ? usageQuery.data.actual_bytes / 1024 ** 3 : null
  const virtualGB = usageQuery.data ? usageQuery.data.virtual_bytes / 1024 ** 3 : null
  const quotaGB = usageQuery.data?.disk_quota_bytes != null ? usageQuery.data.disk_quota_bytes / 1024 ** 3 : null

  return (
    <div className="min-h-screen flex flex-col bg-background">
      <header className="sticky top-0 z-10 flex items-center gap-3 px-5 py-2.5 border-b border-border bg-card">
        <img src="/favicon.svg" width={24} height={24} alt="" className="rounded-md" />
        <div className="text-sm font-bold">My Recordings</div>
        {meQuery.data && (
          <div className="text-xs text-muted-foreground">— {meQuery.data.dispatcharr_username ?? meQuery.data.username}</div>
        )}
        <div className="flex-1" />
        <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
          {NAV.map((n) => (
            <button
              key={n.key}
              onClick={() => setTab(n.key)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-xs transition-colors ${
                tab === n.key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-accent'
              }`}
            >
              {n.icon}{n.label}
            </button>
          ))}
        </div>
        <button className="text-muted-foreground hover:text-foreground p-1.5 rounded hover:bg-accent" title="Sign out" onClick={handleLogout}>
          <LogOut size={15} />
        </button>
      </header>

      <main className="flex-1 p-4 max-w-3xl w-full mx-auto space-y-4">

        {tab === 'recordings' && (
          <SectionCard title="My Recordings" icon={<CalendarDays size={14} />}>
            {rulesQuery.isError && (
              <p className="text-xs text-destructive">Couldn't load your recording rules: {queryErrorMessage(rulesQuery.error)}</p>
            )}
            {rulesQuery.data && !rulesQuery.data.length && (
              <p className="text-xs text-muted-foreground">You don't have any recording rules yet -- search below to add one.</p>
            )}
            <div className="space-y-1.5">
              {rulesQuery.data?.map((rule) => (
                <div key={rule.id} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                  <div className="flex-1 min-w-0 truncate">
                    <span className="font-semibold">{rule.label}</span>{' '}
                    <span className="text-muted-foreground">— "{rule.title}", {rule.mode === 'new' ? 'new episodes only' : 'all episodes'}</span>
                  </div>
                  {rule.channel_id && <Chip>channel {rule.channel_id}</Chip>}
                  <button
                    title="Delete this recording rule"
                    className="text-muted-foreground hover:text-destructive p-1"
                    onClick={() => { if (confirm(`Delete "${rule.label}"? This also cancels its future recordings.`)) deleteRule.mutate(rule.id) }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>

            <div className="border-t border-border pt-3 space-y-2">
              <p className="text-xs font-medium text-muted-foreground">Schedule a new recording</p>
              {!picked ? (
                <>
                  <div className="flex gap-1.5">
                    <input
                      className={inputCls('flex-1')}
                      placeholder="Search the guide (e.g. Seinfeld)"
                      value={searchTitle}
                      onChange={(e) => setSearchTitle(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter' && searchTitle.trim()) search.mutate() }}
                    />
                    <Button size="sm" disabled={!searchTitle.trim() || search.isPending} onClick={() => search.mutate()}>
                      {search.isPending ? <Loader2 size={12} className="animate-spin" /> : <SearchIcon size={12} />}
                    </Button>
                  </div>
                  {search.data && !channelGroups.length && (
                    <p className="text-xs text-muted-foreground">No upcoming airings found for that title.</p>
                  )}
                  {!!channelGroups.length && (
                    <div className="space-y-1 max-h-64 overflow-y-auto">
                      {channelGroups.map(({ channel, programs }) => (
                        <button
                          key={channel.id}
                          onClick={() => pickChannel(channel, programs)}
                          className="w-full text-left flex items-center gap-2 text-xs rounded-md border border-border bg-card px-2.5 py-1.5 hover:bg-accent transition-colors"
                        >
                          <span className="font-mono text-muted-foreground w-8 shrink-0">{channel.channel_number ?? '?'}</span>
                          <span className="flex-1 min-w-0 truncate">
                            <span className="font-semibold">{channel.name}</span>{' '}
                            <span className="text-muted-foreground">— {programs[0].title}{programs[0].sub_title ? ` — ${programs[0].sub_title}` : ''}</span>
                          </span>
                          <Chip>{programs.length} upcoming</Chip>
                        </button>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <div className="rounded-md border border-border bg-card p-2.5 space-y-2">
                  <div className="text-xs">
                    <span className="font-semibold">{picked.title}</span>{' '}
                    <span className="text-muted-foreground">on {picked.channel_label}</span>
                  </div>
                  <input
                    className={inputCls('w-full')}
                    placeholder={`Label (defaults to "${picked.title}")`}
                    value={ruleLabel}
                    onChange={(e) => setRuleLabel(e.target.value)}
                  />
                  <div className="flex justify-end gap-1.5">
                    <Button size="sm" variant="outline" onClick={() => setPicked(null)}>Cancel</Button>
                    <Button size="sm" disabled={addRule.isPending} onClick={() => addRule.mutate()}>
                      {addRule.isPending ? <Loader2 size={12} className="animate-spin" /> : <><Plus size={12} className="mr-1" /> Schedule it</>}
                    </Button>
                  </div>
                </div>
              )}
              {ruleError && <p className="text-xs text-destructive">{ruleError}</p>}
            </div>
          </SectionCard>
        )}

        {tab === 'upcoming' && (
          <SectionCard title="Upcoming Recordings" icon={<CalendarDays size={14} />}>
            {upcomingQuery.isError && (
              <p className="text-xs text-destructive">Couldn't load upcoming recordings: {queryErrorMessage(upcomingQuery.error)}</p>
            )}
            {upcomingQuery.data && !upcomingQuery.data.length && (
              <p className="text-xs text-muted-foreground">Nothing scheduled right now.</p>
            )}
            <div className="space-y-3">
              {upcomingByDay.map(([day, items]) => (
                <div key={day}>
                  <p className="text-[11px] font-medium text-muted-foreground mb-1">{day}</p>
                  <div className="space-y-1">
                    {items.map((r) => (
                      <div key={r.id} className="flex items-center gap-2 text-xs rounded-md border border-border bg-card px-2.5 py-1.5">
                        <span className="font-mono text-muted-foreground w-14 shrink-0">
                          {new Date(r.start_time).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
                        </span>
                        <span className="flex-1 min-w-0 truncate">
                          <span className="font-semibold">{r.custom_properties?.program?.title ?? '?'}</span>
                          {r.custom_properties?.program?.sub_title && (
                            <span className="text-muted-foreground"> — {r.custom_properties.program.sub_title}</span>
                          )}
                        </span>
                        <Chip>channel {r.channel}</Chip>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </SectionCard>
        )}

        {tab === 'usage' && (
          <SectionCard title="Usage" icon={<HardDriveDownload size={14} />}>
            {usageQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
            {usageQuery.isError && (
              <p className="text-xs text-destructive">Couldn't load usage: {queryErrorMessage(usageQuery.error)}</p>
            )}
            {usageQuery.data && (
              <>
                <QuotaBar actualGB={actualGB} virtualGB={virtualGB} quotaGB={quotaGB} />
                <div className="flex items-center gap-4 text-xs text-muted-foreground pt-1">
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
              <p className="text-xs text-destructive">Couldn't load your library: {queryErrorMessage(libraryQuery.error)}</p>
            )}
            {libraryQuery.data && !libraryQuery.data.movies.length && !libraryQuery.data.episodes.length && (
              <p className="text-xs text-muted-foreground">Nothing recorded yet.</p>
            )}
            <div className="space-y-1">
              {libraryQuery.data?.movies.map((m) => (
                <div key={`m-${m.id}`} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                  <button
                    className="w-7 h-7 rounded-full bg-primary/10 text-primary flex items-center justify-center shrink-0 hover:bg-primary/20"
                    onClick={() => setNowPlaying({ kind: 'movie', id: m.id, title: m.name })}
                  >
                    <Play size={12} />
                  </button>
                  <span className="flex-1 truncate">
                    <span className="font-semibold">{m.name}</span>{' '}
                    <span className="text-muted-foreground">{m.year ? `(${m.year})` : ''}</span>
                  </span>
                </div>
              ))}
              {libraryQuery.data?.episodes.map((e) => (
                <div key={`e-${e.id}`} className="flex items-center gap-2 text-xs rounded-lg border border-border bg-card px-3 py-2 shadow-sm">
                  <button
                    className="w-7 h-7 rounded-full bg-primary/10 text-primary flex items-center justify-center shrink-0 hover:bg-primary/20"
                    onClick={() => setNowPlaying({ kind: 'episode', id: e.id, title: `${e.series_name} S${e.season_number}E${e.episode_number}` })}
                  >
                    <Play size={12} />
                  </button>
                  <span className="flex-1 truncate">
                    <span className="font-semibold">{e.series_name}</span>{' '}
                    <span className="text-muted-foreground">S{e.season_number}E{e.episode_number} — {e.name}</span>
                  </span>
                </div>
              ))}
            </div>
          </SectionCard>
        )}

      </main>

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
    </div>
  )
}
