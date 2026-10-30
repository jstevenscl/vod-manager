import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Languages, Loader2, RefreshCw, X } from 'lucide-react'
import api from '@/lib/api'
import { notify } from '@/lib/confirm'

type Mismatch = {
  kind: 'movie' | 'episode'
  source_id: number
  item_id: number
  title: string
  year: number | null
  tmdb_id: string | null
  provider_name: string
  configured_language: string | null
  observed_languages: string[]
  language_probe_confidence: string | null
  language_probed_at: string | null
  season_number?: number
  episode_number?: number
  episode_name?: string | null
  safe_to_apply: boolean
  proposed_language: string | null
  proposed_archive: boolean
}

type Stats = { eligible: { movies: number; series: number; total: number }; pending_review: number; wanted_languages: string[]; history: { day: string; probes: number; mismatches: number; not_needed: number; outside_wanted: number; wanted_only: number; errors: number }[]; provider_history: { provider: string; type: 'M' | 'S'; probes: number; mismatches: number; not_needed: number; outside_wanted: number; wanted_only: number; errors: number }[] }
type ProviderStatus = { provider_id: number; name: string; status: 'normal' | 'gating' | 'blocked'; effective_budget: number; global_budget: number; consecutive_errors: number; tier: number; gated_until: number | null; last_code: string | null; last_detail: string | null }
type CapacityHold = { provider_id: number; provider_name: string; kind: 'movie' | 'episode'; source_id: number; first_attempt_at: number; last_attempt_at: number; next_retry_at: number; hold_reason: string; attempt_count: number; title: string | null; year: number | null; season_number: number | null; episode_number: number | null; episode_name: string | null; retry_due: boolean }
type Report = { enabled_languages: string[]; count: number; results: Mismatch[]; errors?: { source_id: number }[]; capacity_holds?: CapacityHold[]; stats?: Stats; trickle?: { enabled: boolean; interval_seconds: number; daily_max: number | null; checked_today: number; attempts_today: number; deferred_today: number; errors_today: number; probes_per_cycle: number; language_window_max: number | null; language_window_seconds: number; providers?: ProviderStatus[] } }

const NAMES: Record<string, string> = {
  EN: 'English', HI: 'Hindi', FR: 'French', ES: 'Spanish', DE: 'German', IT: 'Italian',
  PT: 'Portuguese', JA: 'Japanese', ZH: 'Chinese', KO: 'Korean', AR: 'Arabic',
  RU: 'Russian', TA: 'Tamil', TE: 'Telugu', ML: 'Malayalam', TH: 'Thai',
}

function languageLabel(code: string | null | undefined) {
  if (!code) return 'Unknown'
  return `${code} — ${NAMES[code] ?? code}`
}

export default function LanguageMismatchReport() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [selectionAnchor, setSelectionAnchor] = useState<string | null>(null)
  const [providerFilters, setProviderFilters] = useState<string[]>([])
  const [observedFilters, setObservedFilters] = useState<string[]>([])
  const [capacityProviderFilter, setCapacityProviderFilter] = useState('')
  const [capacityKindFilter, setCapacityKindFilter] = useState<'all' | 'movie' | 'episode'>('all')
  const [capacityRetryFilter, setCapacityRetryFilter] = useState<'all' | 'waiting' | 'due'>('all')
  const [capacityReportOpen, setCapacityReportOpen] = useState(false)
  const [capacitySort, setCapacitySort] = useState<'provider_name' | 'title' | 'first_attempt_at' | 'last_attempt_at' | 'next_retry_at' | 'attempt_count' | 'hold_reason'>('next_retry_at')
  const [capacitySortAsc, setCapacitySortAsc] = useState(true)
  const report = useQuery<Report>({
    queryKey: ['language-mismatch-report'],
    queryFn: () => api.get('/vod/language-mismatch-report/').then((r) => r.data),
    refetchInterval: 30_000,
  })
  const rows = report.data?.results ?? []
  const errors = report.data?.errors ?? []
  const capacityHolds = report.data?.capacity_holds ?? []
  const filteredCapacityHolds = useMemo(() => capacityHolds
    .filter((hold) => !capacityProviderFilter || hold.provider_name === capacityProviderFilter)
    .filter((hold) => capacityKindFilter === 'all' || hold.kind === capacityKindFilter)
    .filter((hold) => capacityRetryFilter === 'all' || (capacityRetryFilter === 'due' ? hold.retry_due : !hold.retry_due))
    .slice()
    .sort((a, b) => {
      const left = a[capacitySort]
      const right = b[capacitySort]
      const compared = typeof left === 'number' && typeof right === 'number'
        ? left - right
        : String(left).localeCompare(String(right))
      return capacitySortAsc ? compared : -compared
    }), [capacityHolds, capacityProviderFilter, capacityKindFilter, capacityRetryFilter, capacitySort, capacitySortAsc])
  const sortCapacityBy = (column: typeof capacitySort) => {
    if (capacitySort === column) setCapacitySortAsc((asc) => !asc)
    else { setCapacitySort(column); setCapacitySortAsc(true) }
  }
  const capacitySortLabel = (column: typeof capacitySort, label: string) => <button className="text-left hover:text-white" onClick={() => sortCapacityBy(column)}>{label}{capacitySort === column ? (capacitySortAsc ? ' ▲' : ' ▼') : ''}</button>
  const capacityDate = (value: number) => new Date(value * 1000).toLocaleString()
  const key = (r: Mismatch) => `${r.kind}:${r.source_id}`
  const selectable = useMemo(() => rows.filter((r) => r.safe_to_apply).map(key), [rows])
  useEffect(() => {
    setSelected((old) => new Set([...old].filter((id) => rows.some((r) => key(r) === id))))
  }, [rows])

  const decide = useMutation({
    mutationFn: ({ row, decision }: { row: Mismatch; decision: 'approved' | 'rejected' }) =>
      api.post(`/vod/language-mismatch/${row.kind}/${row.source_id}/decision/`, { decision }).then((r) => r.data),
    onSuccess: (result) => {
      notify(result.action === 'language_updated_and_archived' ? 'Language updated and item archived.' : result.action === 'language_updated_and_restored' ? 'Language updated and item restored.' : result.decision === 'rejected' ? 'Mismatch ignored and will not be rechecked.' : 'Language updated; no archive was needed.')
      qc.invalidateQueries({ queryKey: ['language-mismatch-report'] })
      qc.invalidateQueries({ queryKey: ['vod-notices'] })
    },
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Could not record the language decision.'),
  })
  const runNow = useMutation({
    mutationFn: () => api.post('/vod/language-trickle/run/').then((r) => r.data),
    onSuccess: (result) => {
      notify(result.targets ? `Language check cycle started for ${result.targets} provider source${result.targets === 1 ? '' : 's'}. Results will appear here when complete.` : 'No eligible language sources are waiting to be checked.')
      qc.invalidateQueries({ queryKey: ['language-mismatch-report'] })
    },
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Could not start the language check cycle.'),
  })

  const runBulk = async (decision: 'approved' | 'rejected', all = false) => {
    const targets = all ? rows : rows.filter((r) => selected.has(key(r)))
    const safeTargets = decision === 'approved' ? targets.filter((r) => r.safe_to_apply) : targets
    if (!safeTargets.length) { notify(decision === 'approved' ? 'No single-language results are selected for approval.' : 'No mismatches are selected.'); return }
    for (const row of safeTargets) await decide.mutateAsync({ row, decision })
    setSelected(new Set())
  }

  if (report.isLoading) return <div className="flex items-center gap-2 text-muted-foreground"><Loader2 size={15} className="animate-spin" /> Loading language mismatch report…</div>
  if (report.isError) return <div className="text-destructive">Could not load the language mismatch report.</div>

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold flex items-center gap-2"><Languages size={18} /> Language Mismatch Report</h1>
          <p className="text-xs text-muted-foreground mt-1">Embedded audio tags are compared with the configured wanted-language policy. Approved single-language results update the source metadata and archive the parent when the observed language is outside that policy. Reject records the decision and prevents automatic re-checking.</p>
          <p className="text-xs text-muted-foreground mt-1">Wanted playback languages: {report.data?.enabled_languages.join(', ') || 'none'}</p>
          {report.data?.trickle && <p className="text-xs text-muted-foreground mt-1">Trickle: up to {report.data.trickle.global_concurrency} probes every {report.data.trickle.interval_seconds}s · {report.data.trickle.checked_today}/{report.data.trickle.daily_max} today · language cap {report.data.trickle.language_window_max} per {Math.round(report.data.trickle.language_window_seconds / 60)} minutes{report.data.trickle.blocked_providers ? ` · ${report.data.trickle.blocked_providers} provider paused` : ''}</p>}
        </div>
        <span className="flex items-center gap-2">
          {report.data?.trickle && <button className={`flex items-center gap-1.5 border rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50 ${report.data.trickle.enabled ? 'border-amber-500/40 text-amber-400' : 'border-emerald-500/40 text-emerald-400'}`} disabled={toggleEnabled.isPending} onClick={() => toggleEnabled.mutate(!report.data!.trickle!.enabled)}><Power size={12} /> {report.data.trickle.enabled ? 'Disable monitoring' : 'Enable monitoring'}</button>}
          <button className="flex items-center gap-1.5 border border-primary/40 text-primary rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50" disabled={runNow.isPending || !report.data?.trickle?.enabled} title={report.data?.trickle?.enabled ? 'Run one bounded monitoring cycle' : 'Enable monitoring before running a cycle'} onClick={() => runNow.mutate()}><Languages size={12} className={runNow.isPending ? 'animate-pulse' : ''} /> Run one cycle now</button>
          <button className="flex items-center gap-1.5 border border-amber-500/40 text-amber-300 rounded px-2 py-1.5 text-xs hover:bg-amber-500/10" onClick={() => window.dispatchEvent(new CustomEvent('vodmanager-goto', { detail: 'language-unknowns' }))}>Unknown language review</button>
          <button className="flex items-center gap-1.5 border border-amber-500/40 text-amber-300 rounded px-2 py-1.5 text-xs hover:bg-amber-500/10" onClick={() => window.dispatchEvent(new CustomEvent('vodmanager-goto', { detail: 'language-errors' }))}>Probe issues{errors.length ? ` (${errors.length})` : ''}</button>
          <button className="border border-border rounded px-2 py-1.5 text-xs hover:bg-accent" aria-expanded={capacityReportOpen} onClick={() => setCapacityReportOpen((open) => !open)}>Provider capacity holds ({capacityHolds.length})</button>
          <button className="flex items-center gap-1.5 border border-border rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50" disabled={report.isFetching} onClick={() => report.refetch()}><RefreshCw size={12} className={report.isFetching ? 'animate-spin' : ''} /> Refresh</button>
        </span>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-md border border-border bg-card px-3 py-2"><div className="text-[11px] text-muted-foreground">Eligible sources</div><div className="text-lg font-semibold">{report.data?.stats?.eligible.total.toLocaleString() ?? '—'}</div><div className="text-[11px] text-muted-foreground">Movies {report.data?.stats?.eligible.movies.toLocaleString() ?? '—'} · Episodes {report.data?.stats?.eligible.series.toLocaleString() ?? '—'}</div></div>
        <div className="rounded-md border border-border bg-card px-3 py-2"><div className="text-[11px] text-muted-foreground">Completed checks today</div><div className="text-lg font-semibold">{report.data?.trickle?.checked_today.toLocaleString() ?? '—'}</div><div className="text-[11px] text-muted-foreground">{report.data?.trickle ? `${report.data.trickle.attempts_today.toLocaleString()} attempts · ${report.data.trickle.deferred_today.toLocaleString()} deferred` : '—'}</div></div>
        <div className="rounded-md border border-border bg-card px-3 py-2"><div className="text-[11px] text-muted-foreground">Pending review</div><div className="text-lg font-semibold">{report.data?.stats?.pending_review.toLocaleString() ?? '—'}</div><div className="text-[11px] text-muted-foreground">Waiting for a decision</div></div>
        <div className="rounded-md border border-border bg-card px-3 py-2"><div className="text-[11px] text-muted-foreground">Estimated pace</div><div className="text-lg font-semibold">{report.data?.trickle ? `${Math.round(report.data.trickle.probes_per_cycle * 3600 / report.data.trickle.interval_seconds).toLocaleString()}/hr` : '—'}</div><div className="text-[11px] text-muted-foreground">Eligible count drops as sources are checked</div></div>
      </div>

      <details className="rounded-md border border-border bg-card px-3 py-3">
        <summary className="cursor-pointer text-xs font-medium">Monitoring controls</summary>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs">Probes per cycle<input className="block mt-1 w-28 rounded border border-border bg-background px-2 py-1" type="number" min="1" max="600" value={form.probes_per_cycle} onChange={(e) => setForm({ ...form, probes_per_cycle: Number(e.target.value) })} /></label>
          <label className="text-xs">Seconds between cycles<input className="block mt-1 w-32 rounded border border-border bg-background px-2 py-1" type="number" min="90" max="86400" value={form.interval_seconds} onChange={(e) => setForm({ ...form, interval_seconds: Number(e.target.value) })} /></label>
          <label className="text-xs">Daily cap<input className="block mt-1 w-28 rounded border border-border bg-background px-2 py-1" type="number" min="1" placeholder="None" value={form.daily_limit} onChange={(e) => setForm({ ...form, daily_limit: e.target.value })} /></label>
          <button className="border border-primary/40 text-primary rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50" disabled={saveSettings.isPending} onClick={() => saveSettings.mutate()}>Save pacing</button>
          <span className="text-[11px] text-muted-foreground">Leave Daily cap blank for no cap. Provider safeguards reduce or pause only providers that show repeated throttling.</span>
        </div>
        {!!report.data?.trickle?.providers?.length && <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">{report.data.trickle.providers.map((provider) => {
          const gated = provider.status === 'gating'
          const blocked = provider.status === 'blocked'
          const label = blocked ? 'Blocked' : gated ? `Tier ${provider.tier} gating` : 'Normal'
          const budget = blocked ? 'User action required' : gated && provider.gated_until ? `Until ${new Date(provider.gated_until * 1000).toLocaleTimeString()}` : provider.effective_budget < provider.global_budget ? `${provider.effective_budget}/cycle` : 'Full budget'
          const lastResponse = provider.last_code ? (/^\d+$/.test(provider.last_code) ? `Last response: HTTP ${provider.last_code}` : `Last response: ${provider.last_code}`) : null
          return <div className="rounded border border-border bg-background/40 px-2 py-2 text-[11px]" key={provider.provider_id}><div className="flex items-center justify-between gap-2"><span className="font-medium">{provider.name}</span><span className={blocked ? 'text-red-400' : gated ? 'text-amber-400' : 'text-emerald-400'}>{label}</span></div><div className="mt-1 text-muted-foreground">{budget}{lastResponse ? ` · ${lastResponse}` : ''}</div>{blocked && <button className="mt-2 border border-primary/40 rounded px-2 py-1 text-primary hover:bg-accent" disabled={unblockProvider.isPending} onClick={() => unblockProvider.mutate(provider.provider_id)}>Unblock</button>}</div>
        })}</div>}
      </details>

      {capacityReportOpen && <div className="rounded-md border border-border bg-card px-3 py-3">
        <h2 className="text-xs font-medium">Provider capacity holds</h2>
        <p className="mt-2 text-[11px] text-white">Read-only view of providers waiting for an available connection. A full provider is retried every two hours; if it is still full, the next retry is scheduled two hours after that attempt.</p>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-white">
          <label>Provider <select className="ml-1 rounded border border-border bg-background px-2 py-1 text-white" value={capacityProviderFilter} onChange={(e) => setCapacityProviderFilter(e.target.value)}>
            <option value="">All providers</option>
            {[...new Set(capacityHolds.map((hold) => hold.provider_name))].sort().map((name) => <option key={name} value={name}>{name}</option>)}
          </select></label>
          <label>Source type <select className="ml-1 rounded border border-border bg-background px-2 py-1 text-white" value={capacityKindFilter} onChange={(e) => setCapacityKindFilter(e.target.value as typeof capacityKindFilter)}><option value="all">All types</option><option value="movie">Movies</option><option value="episode">Episodes</option></select></label>
          <label>Retry status <select className="ml-1 rounded border border-border bg-background px-2 py-1 text-white" value={capacityRetryFilter} onChange={(e) => setCapacityRetryFilter(e.target.value as typeof capacityRetryFilter)}><option value="all">All statuses</option><option value="waiting">Waiting</option><option value="due">Due now</option></select></label>
          <span>{filteredCapacityHolds.length} shown of {capacityHolds.length}</span>
        </div>
        {filteredCapacityHolds.length === 0 ? <div className="mt-3 rounded border border-border bg-background/40 p-4 text-center text-xs text-white">{capacityHolds.length ? 'No capacity holds match this provider.' : 'No providers are currently waiting for capacity.'}</div> : (
          <div className="mt-3 overflow-x-auto rounded border border-border">
            <table className="w-full text-xs text-white">
              <thead className="bg-background/70"><tr className="text-left">
                <th className="p-2">{capacitySortLabel('provider_name', 'Provider')}</th>
                <th className="p-2">{capacitySortLabel('title', 'Source')}</th>
                <th className="p-2">{capacitySortLabel('first_attempt_at', 'First held')}</th>
                <th className="p-2">{capacitySortLabel('last_attempt_at', 'Last attempt')}</th>
                <th className="p-2">{capacitySortLabel('next_retry_at', 'Next retry')}</th>
                <th className="p-2">{capacitySortLabel('attempt_count', 'Capacity attempts')}</th>
                <th className="p-2">{capacitySortLabel('hold_reason', 'Hold reason')}</th>
              </tr></thead>
              <tbody>{filteredCapacityHolds.map((hold) => <tr className="border-t border-border/60 align-top" key={`${hold.kind}:${hold.source_id}`}>
                <td className="p-2">{hold.provider_name}</td>
                <td className="p-2">{hold.title || 'Source unavailable'}{hold.year ? ` (${hold.year})` : ''}{hold.kind === 'episode' ? ` · S${hold.season_number ?? '?'}E${hold.episode_number ?? '?'}${hold.episode_name ? ` · ${hold.episode_name}` : ''}` : ' · Movie'}</td>
                <td className="p-2 whitespace-nowrap">{capacityDate(hold.first_attempt_at)}</td>
                <td className="p-2 whitespace-nowrap">{capacityDate(hold.last_attempt_at)}</td>
                <td className="p-2 whitespace-nowrap"><span className={hold.retry_due ? 'text-emerald-300' : 'text-white'}>{hold.retry_due ? 'Due now' : capacityDate(hold.next_retry_at)}</span></td>
                <td className="p-2">{hold.attempt_count}</td>
                <td className="p-2">{hold.hold_reason}</td>
              </tr>)}</tbody>
            </table>
          </div>
        )}
      </div>}

      <details className="rounded-md border border-border bg-card px-3 py-3">
        <summary className="cursor-pointer text-xs font-medium">Provider probe history</summary>
      {!!report.data?.stats?.provider_history.length && <><div className="overflow-x-auto border border-border rounded-md"><table className="w-full text-xs"><thead className="bg-card text-muted-foreground"><tr className="text-left"><th className="p-2">Provider</th><th className="p-2">Type</th><th className="p-2">Probes</th><th className="p-2">Not needed</th><th className="p-2">Needed review</th><th className="p-2">Outside wanted</th><th className="p-2">Wanted only</th><th className="p-2">Errors/deferred</th></tr></thead><tbody>{report.data.stats.provider_history.map((h) => <tr className="border-t border-border/60" key={`${h.provider}:${h.type}`}><td className="p-2">{h.provider}</td><td className="p-2 font-semibold">{h.type}</td><td className="p-2">{h.probes.toLocaleString()}</td><td className="p-2">{h.not_needed.toLocaleString()}</td><td className="p-2">{h.mismatches.toLocaleString()}</td><td className="p-2">{h.outside_wanted.toLocaleString()}</td><td className="p-2">{h.wanted_only.toLocaleString()}</td><td className="p-2">{h.errors.toLocaleString()}</td></tr>)}</tbody></table></div><p className="text-[11px] text-muted-foreground">Each row reconciles as: Probes = Not needed + Needed review + Errors/deferred. “Not needed” means the probe completed without finding a mismatch.</p></>}

      </details>

      <div className="flex flex-wrap items-center gap-2 border border-border rounded-md bg-card px-3 py-2 text-xs">
        <span className="text-muted-foreground">{rows.length} pending mismatch{rows.length === 1 ? '' : 'es'}</span>
        <span className="text-muted-foreground">· {selected.size} selected</span>
        <button className="ml-auto border border-border rounded px-2 py-1 hover:bg-accent" onClick={() => setSelected(new Set(selectable))}>Select all approvable</button>
        <button className="border border-emerald-500/40 text-emerald-400 rounded px-2 py-1 hover:bg-emerald-500/10 disabled:opacity-50" disabled={decide.isPending || !selected.size} onClick={() => runBulk('approved')}>Approve selected</button>
        <button className="border border-amber-500/40 text-amber-400 rounded px-2 py-1 hover:bg-amber-500/10 disabled:opacity-50" disabled={decide.isPending || !selected.size} onClick={() => runBulk('rejected')}>Keep configured selected</button>
        <button className="border border-emerald-500/40 text-emerald-400 rounded px-2 py-1 hover:bg-emerald-500/10 disabled:opacity-50" disabled={decide.isPending || !rows.length} onClick={() => runBulk('approved', true)}>Approve all proposed</button>
        <button className="border border-amber-500/40 text-amber-400 rounded px-2 py-1 hover:bg-amber-500/10 disabled:opacity-50" disabled={decide.isPending || !rows.length} onClick={() => runBulk('rejected', true)}>Keep configured all</button>
      </div>

      <div className="flex flex-wrap gap-x-5 gap-y-1 border border-border/70 rounded-md bg-background/50 px-3 py-2 text-[11px] text-muted-foreground">
        <span><strong className="text-emerald-400">✓ Approve</strong> — apply the proposed observed language; archive the parent when it is outside the wanted-language policy.</span>
        <span><strong className="text-amber-400">× Keep configured / Ignore</strong> — leave the current language and catalog state unchanged; record the decision and do not recheck.</span>
        <span><strong className="text-foreground">Manual review</strong> — multiple or ambiguous audio tags; approval is disabled until the user decides.</span>
      </div>

      {rows.length === 0 ? <div className="border border-border rounded-md bg-card p-8 text-center text-sm text-muted-foreground">No pending language mismatches.</div> : (
        <div className="overflow-x-auto border border-border rounded-md">
          <table className="w-full text-xs">
            <thead className="bg-card text-muted-foreground"><tr className="text-left">
              <th className="p-2 w-8"></th><th className="p-2">Title</th><th className="p-2">Provider</th><th className="p-2">Configured</th><th className="p-2">Observed</th><th className="p-2">Proposed result</th><th className="p-2">Checked</th><th className="p-2">Decision</th>
            </tr></thead>
            <tbody>{rows.map((row) => {
              const id = key(row); const disabled = !row.safe_to_apply
              return <tr key={id} className="border-t border-border/60 align-top">
                <td className="p-2"><input type="checkbox" disabled={disabled} checked={selected.has(id)} onChange={(e) => setSelected((old) => { const n = new Set(old); e.target.checked ? n.add(id) : n.delete(id); return n })} /></td>
                <td className="p-2"><div className="font-medium">{row.title} {row.year ? `(${row.year})` : ''}</div><div className="text-muted-foreground">{row.kind === 'episode' ? `S${row.season_number}E${row.episode_number} · ${row.episode_name ?? ''}` : 'Movie'}{row.tmdb_id ? ` · TMDB ${row.tmdb_id}` : ''}</div></td>
                <td className="p-2">{row.provider_name}</td>
                <td className="p-2">{languageLabel(row.configured_language)}</td>
                <td className="p-2">{row.observed_languages.map(languageLabel).join(', ')}</td>
                <td className="p-2">{disabled ? 'Manual review required' : <>{languageLabel(row.proposed_language)}{row.proposed_archive && <div className="text-amber-400">Then archive</div>}</>}</td>
                <td className="p-2 text-muted-foreground">{row.language_probed_at ? new Date(row.language_probed_at).toLocaleString() : '—'}</td>
                <td className="p-2"><span className="flex gap-1"><button title="Approve: apply the proposed language and any archive result" disabled={decide.isPending || disabled} className="p-1 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40" onClick={() => decide.mutate({ row, decision: 'approved' })}><Check size={13} /></button><button title="Keep configured language and ignore this result; do not re-check" disabled={decide.isPending} className="p-1 rounded border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 disabled:opacity-40" onClick={() => decide.mutate({ row, decision: 'rejected' })}><X size={13} /></button></span></td>
              </tr>
            })}</tbody>
          </table>
        </div>
      )}
    </div>
  )
}
