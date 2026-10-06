import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Database, Loader2, Play, Power, RefreshCw, Search, Settings as SettingsIcon, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { inputCls, SectionCard, StatusPill } from '@/components/dvr-shared'
import api from '@/lib/api'
import { askConfirm, ConfirmDialogHost } from '@/lib/confirm'
import { toast } from '@/lib/toast'

type MediaType = 'movie' | 'tv'

interface StoreSettings {
  enabled: boolean
  burst_size: number
  bursts_per_day: number
  prefill_top: number
  daily_budget: number
  concurrency: number
}
interface TypeStats {
  media_type: MediaType
  entries: number
  export_ids: number
  with_release_date: number
  with_poster: number
  with_cast: number
  with_overview: number
  with_rating: number
  stale: number
  last_fetched_at: number | null
  export_date: string | null
}
interface StoreStatus {
  has_api_key: boolean
  active: boolean
  settings: StoreSettings
  stats: TypeStats[]
  requests_today: number
  next_burst_at: number | null
  db_size_bytes: number
  job: { running: boolean; phase: string | null; done: number; total: number; last_result: Record<string, unknown> | null; last_error: string | null }
}
interface LookupResult {
  tmdb_id: number
  title: string | null
  original_title: string | null
  year: number | null
  poster_url: string | null
  overview: string | null
  vote_average: number | null
  content_rating: string | null
  top_cast: string | null
  genres: string | null
}

const fmtNum = (n: number) => n.toLocaleString()
const fmtTime = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : '—')
const fmtBytes = (b: number) => (b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`)

function pct(part: number, whole: number) {
  return whole ? `${fmtNum(part)} (${Math.round((part / whole) * 100)}%)` : '0'
}

const SETTING_FIELDS: { key: Exclude<keyof StoreSettings, 'enabled'>; label: string; hint: string }[] = [
  { key: 'burst_size', label: 'Titles per burst', hint: 'Detail requests per fill burst (split across movies and shows).' },
  { key: 'bursts_per_day', label: 'Bursts per day', hint: 'How many fill bursts run each day.' },
  { key: 'prefill_top', label: 'Pre-fill top N', hint: 'Only pre-fill the N most popular titles of each type.' },
  { key: 'daily_budget', label: 'Daily request budget', hint: 'Fill bursts stop once this many TMDB requests were made today.' },
  { key: 'concurrency', label: 'Parallel requests', hint: 'Concurrent requests during a burst — keep low to stay light on bandwidth.' },
]

export default function TmdbStore() {
  const qc = useQueryClient()
  const statusQuery = useQuery<StoreStatus>({
    queryKey: ['tmdb-store-status'],
    queryFn: () => api.get('/tmdb-store/status').then((r) => r.data),
    refetchInterval: (q) => (q.state.data?.job.running ? 3000 : 30000),
  })
  const status = statusQuery.data

  const [form, setForm] = useState<StoreSettings | null>(null)
  useEffect(() => {
    if (status && !form) setForm(status.settings)
  }, [status, form])

  const saveSettings = useMutation({
    mutationFn: (s: StoreSettings) =>
      api.put('/tmdb-store/settings', Object.fromEntries(SETTING_FIELDS.map((f) => [f.key, s[f.key]]))).then((r) => r.data),
    onSuccess: (data: StoreSettings) => {
      setForm(data)
      toast.success('Settings saved')
      qc.invalidateQueries({ queryKey: ['tmdb-store-status'] })
    },
    onError: () => toast.error('Could not save settings'),
  })
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.put('/tmdb-store/settings', { enabled }).then((r) => r.data as StoreSettings),
    onSuccess: (data) => {
      setForm((f) => (f ? { ...f, enabled: data.enabled } : data))
      toast.success(data.enabled ? 'Local TMDB library turned on' : 'Local TMDB library turned off')
      qc.invalidateQueries({ queryKey: ['tmdb-store-status'] })
    },
    onError: () => toast.error('Could not change the setting'),
  })
  const clearStore = useMutation({
    mutationFn: () => api.post('/tmdb-store/clear').then((r) => r.data),
    onSuccess: () => toast.success('Stored TMDB data deleted'),
    onError: (err: { response?: { data?: { detail?: string } } }) =>
      toast.error(err.response?.data?.detail ?? 'Could not delete stored data'),
    onSettled: () => qc.invalidateQueries({ queryKey: ['tmdb-store-status'] }),
  })
  const runJob = useMutation({
    mutationFn: (job: 'burst' | 'changes' | 'export') => api.post(`/tmdb-store/run/${job}`).then((r) => r.data),
    onSettled: () => qc.invalidateQueries({ queryKey: ['tmdb-store-status'] }),
  })

  const [query, setQuery] = useState('')
  const [mediaType, setMediaType] = useState<MediaType>('movie')
  const [year, setYear] = useState('')
  const lookup = useMutation({
    mutationFn: () =>
      api
        .get('/tmdb-store/search', { params: { q: query.trim(), type: mediaType, year: year ? Number(year) : undefined } })
        .then((r) => r.data as { source: 'local' | 'tmdb'; results: LookupResult[] }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['tmdb-store-status'] }),
  })

  const runError = (runJob.error as { response?: { data?: { detail?: string } } } | null)?.response?.data?.detail
  const enabled = status?.settings.enabled ?? true
  const canRun = !!status?.has_api_key && enabled && !status?.job.running

  return (
    <div className="space-y-4">
      <ConfirmDialogHost />
      {status && (
        <SectionCard title="Use local TMDB library" icon={<Power size={14} />}>
          <div className="flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-md border border-border overflow-hidden text-xs">
              {[true, false].map((on) => (
                <button
                  key={String(on)}
                  type="button"
                  disabled={toggle.isPending}
                  onClick={() => on !== enabled && toggle.mutate(on)}
                  className={`px-3 py-1.5 font-medium ${on === enabled ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                >
                  {on ? 'On' : 'Off'}
                </button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground flex-1 min-w-[16rem]">
              {enabled
                ? 'On: TMDB details are saved locally and reused, and a light background fill adds popular titles.'
                : 'Off: nothing is stored; every lookup goes straight to TMDB. Use this if disk space is tight.'}
            </p>
            {!enabled && status.db_size_bytes > 0 && (
              <Button
                size="sm"
                variant="outline"
                disabled={clearStore.isPending || status.job.running}
                onClick={() =>
                  askConfirm(
                    `Delete all stored TMDB data (${fmtBytes(status.db_size_bytes)})? It is rebuilt from TMDB if the library is turned back on.`,
                    () => clearStore.mutate(),
                  )
                }
              >
                {clearStore.isPending ? <Loader2 size={12} className="animate-spin mr-1" /> : <Trash2 size={12} className="mr-1" />}
                Delete stored data ({fmtBytes(status.db_size_bytes)})
              </Button>
            )}
          </div>
        </SectionCard>
      )}
      <SectionCard title="TMDB Library" icon={<Database size={14} />}>
        <p className="text-xs text-muted-foreground">
          A local copy of TMDB movie and TV details. Every title this app looks up is kept here, and a light background
          fill adds the most popular titles a few times a day, so most lookups are answered locally instead of calling
          TMDB. Starts automatically once a TMDB API key is saved under Configuration → API Keys.
        </p>
        {statusQuery.isLoading || !status ? (
          <Loader2 size={14} className="animate-spin" />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {status.active ? (
                <StatusPill label="Active" tone="success" />
              ) : (
                <StatusPill label={status.has_api_key ? 'Off' : 'Inactive — no TMDB API key'} tone="warning" />
              )}
              <span className="text-muted-foreground">
                Requests today: <b>{fmtNum(status.requests_today)}</b> / {fmtNum(status.settings.daily_budget)}
              </span>
              <span className="text-muted-foreground">Size: <b>{fmtBytes(status.db_size_bytes)}</b></span>
              <span className="text-muted-foreground">Next burst: <b>{fmtTime(status.next_burst_at)}</b></span>
            </div>
            {status.job.running && (
              <p className="text-xs flex items-center gap-1.5">
                <Loader2 size={12} className="animate-spin" /> {status.job.phase}
                {status.job.total > 0 && ` — ${fmtNum(status.job.done)} / ${fmtNum(status.job.total)}`}
              </p>
            )}
            {status.job.last_error && <p className="text-xs text-destructive">{status.job.last_error}</p>}
            {runError && <p className="text-xs text-destructive">{runError}</p>}

            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr className="text-left border-b border-border">
                    <th className="py-1.5 pr-3 font-medium">Type</th>
                    <th className="py-1.5 pr-3 font-medium">Entries</th>
                    <th className="py-1.5 pr-3 font-medium">Release date</th>
                    <th className="py-1.5 pr-3 font-medium">Poster</th>
                    <th className="py-1.5 pr-3 font-medium">Cast</th>
                    <th className="py-1.5 pr-3 font-medium">Overview</th>
                    <th className="py-1.5 pr-3 font-medium">Rating</th>
                    <th className="py-1.5 pr-3 font-medium">Changed on TMDB</th>
                    <th className="py-1.5 pr-3 font-medium">TMDB IDs known</th>
                    <th className="py-1.5 pr-3 font-medium">Last refreshed</th>
                  </tr>
                </thead>
                <tbody>
                  {status.stats.map((s) => (
                    <tr key={s.media_type} className="border-b border-border/50">
                      <td className="py-1.5 pr-3 font-medium">{s.media_type === 'movie' ? 'Movies' : 'TV Shows'}</td>
                      <td className="py-1.5 pr-3">{fmtNum(s.entries)}</td>
                      <td className="py-1.5 pr-3">{pct(s.with_release_date, s.entries)}</td>
                      <td className="py-1.5 pr-3">{pct(s.with_poster, s.entries)}</td>
                      <td className="py-1.5 pr-3">{pct(s.with_cast, s.entries)}</td>
                      <td className="py-1.5 pr-3">{pct(s.with_overview, s.entries)}</td>
                      <td className="py-1.5 pr-3">{pct(s.with_rating, s.entries)}</td>
                      <td className="py-1.5 pr-3">{fmtNum(s.stale)}</td>
                      <td className="py-1.5 pr-3">
                        {fmtNum(s.export_ids)}
                        {s.export_date && <span className="text-muted-foreground"> ({s.export_date})</span>}
                      </td>
                      <td className="py-1.5 pr-3">{fmtTime(s.last_fetched_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap gap-1.5">
              <Button size="sm" variant="outline" disabled={!canRun} onClick={() => runJob.mutate('burst')}>
                <Play size={12} className="mr-1" /> Run fill burst now
              </Button>
              <Button size="sm" variant="outline" disabled={!canRun} onClick={() => runJob.mutate('changes')}>
                <RefreshCw size={12} className="mr-1" /> Check TMDB changes
              </Button>
              <Button size="sm" variant="outline" disabled={!canRun} onClick={() => runJob.mutate('export')}>
                <Database size={12} className="mr-1" /> Refresh TMDB ID list
              </Button>
            </div>
          </>
        )}
      </SectionCard>

      <SectionCard title="Lookup" icon={<Search size={14} />}>
        <p className="text-xs text-muted-foreground">
          Search by name or TMDB ID. The local library is checked first; on a miss TMDB is searched and the results are
          saved here.
        </p>
        <form
          className="flex flex-wrap items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault()
            if (query.trim()) lookup.mutate()
          }}
        >
          <input className={inputCls('w-64')} placeholder="Title or TMDB ID" value={query} onChange={(e) => setQuery(e.target.value)} />
          <select className={inputCls('w-28')} value={mediaType} onChange={(e) => setMediaType(e.target.value as MediaType)}>
            <option value="movie">Movie</option>
            <option value="tv">TV Show</option>
          </select>
          <input className={inputCls('w-20')} placeholder="Year" inputMode="numeric" value={year} onChange={(e) => setYear(e.target.value.replace(/\D/g, '').slice(0, 4))} />
          <Button size="sm" type="submit" disabled={!query.trim() || lookup.isPending}>
            {lookup.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Search'}
          </Button>
        </form>
        {lookup.isError && <p className="text-xs text-destructive">Lookup failed — check the TMDB API key.</p>}
        {lookup.data && (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              {lookup.data.results.length} result(s) — {lookup.data.source === 'local' ? 'from the local library' : enabled ? 'from TMDB (now saved locally)' : 'from TMDB (not saved — library off)'}
            </p>
            {lookup.data.results.map((r) => (
              <div key={r.tmdb_id} className="flex gap-3 rounded-md border border-border p-2">
                {r.poster_url ? (
                  <img src={r.poster_url} alt="" className="w-12 h-[72px] object-cover rounded shrink-0" loading="lazy" />
                ) : (
                  <div className="w-12 h-[72px] rounded bg-muted shrink-0" />
                )}
                <div className="min-w-0 text-xs space-y-0.5">
                  <p className="font-semibold text-sm">
                    {r.title}
                    {r.year && <span className="text-muted-foreground font-normal"> ({r.year})</span>}
                  </p>
                  <p className="text-muted-foreground">
                    TMDB {r.tmdb_id}
                    {r.content_rating && ` · ${r.content_rating}`}
                    {r.vote_average ? ` · ★ ${r.vote_average.toFixed(1)}` : ''}
                    {r.genres && ` · ${r.genres}`}
                    {r.original_title && r.original_title !== r.title && ` · ${r.original_title}`}
                  </p>
                  {r.top_cast && <p className="text-muted-foreground truncate">{r.top_cast}</p>}
                  {r.overview && <p className="line-clamp-2">{r.overview}</p>}
                </div>
              </div>
            ))}
          </div>
        )}
      </SectionCard>

      {form && (
        <SectionCard title="Background fill settings" icon={<SettingsIcon size={14} />}>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {SETTING_FIELDS.map((f) => (
              <label key={f.key} className="text-xs space-y-1">
                <span className="font-medium">{f.label}</span>
                <input
                  className={inputCls('w-full')}
                  inputMode="numeric"
                  value={form[f.key]}
                  onChange={(e) => setForm({ ...form, [f.key]: Number(e.target.value.replace(/\D/g, '')) || 0 })}
                />
                <span className="block text-muted-foreground">{f.hint}</span>
              </label>
            ))}
          </div>
          <Button size="sm" disabled={saveSettings.isPending} onClick={() => saveSettings.mutate(form)}>
            {saveSettings.isPending ? <Loader2 size={12} className="animate-spin" /> : 'Save settings'}
          </Button>
        </SectionCard>
      )}
    </div>
  )
}
