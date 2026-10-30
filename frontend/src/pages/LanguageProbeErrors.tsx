import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, Check, Loader2, RefreshCw, RotateCcw, Trash2 } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '@/lib/api'
import { notify } from '@/lib/confirm'


type ProbeError = {
  kind: 'movie' | 'episode'
  source_id: number
  item_id: number
  title: string
  year: number | null
  provider_name: string
  configured_language: string | null
  category: string | null
  container_extension: string | null
  reason: string
  reason_code: string
  reason_detail: string
  attempts: number
  quarantined: boolean
  last_failed_at: string | null
}

type Report = { errors?: ProbeError[] }

export default function LanguageProbeErrors() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [selectionAnchor, setSelectionAnchor] = useState<string | null>(null)
  const [providerFilters, setProviderFilters] = useState<string[]>([])
  const report = useQuery<Report>({
    queryKey: ['language-mismatch-report'],
    queryFn: () => api.get('/vod/language-mismatch-report/').then((r) => r.data),
    refetchInterval: 30_000,
  })
  const action = useMutation({
    mutationFn: ({ row, value }: { row: ProbeError; value: 'approve' | 'retry' | 'delete' }) => api.post(`/vod/language-probe-errors/${row.kind}/${row.source_id}/action/`, { action: value }).then((r) => r.data),
    onSuccess: (result) => {
      notify(result.action === 'deleted' ? 'Source deleted.' : result.action === 'approved' ? 'Manual review approved; source marked checked.' : 'Source returned to the probe queue.')
      qc.invalidateQueries({ queryKey: ['language-mismatch-report'] })
    },
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Could not update the probe issue.'),
  })
  const errors = report.data?.errors ?? []
  const deferred = errors.filter((row) => !row.quarantined)
  const quarantined = errors.filter((row) => row.quarantined)
  const rowKey = (row: ProbeError) => `${row.kind}:${row.source_id}`
  const providerOptions = useMemo(() => [...new Set(errors.map((row) => row.provider_name))], [errors])
  const filteredErrors = useMemo(() => errors.filter((row) => !providerFilters.length || providerFilters.includes(row.provider_name)), [errors, providerFilters])
  const filteredDeferred = filteredErrors.filter((row) => !row.quarantined)
  const filteredQuarantined = filteredErrors.filter((row) => row.quarantined)
  const visibleRows = [...filteredDeferred, ...filteredQuarantined]
  useEffect(() => setSelected((old) => new Set([...old].filter((id) => errors.some((row) => rowKey(row) === id)))), [errors])
  const bulkAction = async (value: 'approve' | 'retry' | 'delete') => {
    const targets = errors.filter((row) => selected.has(rowKey(row)))
    if (!targets.length) { notify('No probe issues are selected.'); return }
    if (value === 'delete' && !window.confirm(`Delete ${targets.length} selected source${targets.length === 1 ? '' : 's'}? This cannot be undone.`)) return
    for (const row of targets) await action.mutateAsync({ row, value })
    setSelected(new Set())
    setSelectionAnchor(null)
  }
  const toggleRowSelection = (row: ProbeError, event: React.ChangeEvent<HTMLInputElement>) => {
    const id = rowKey(row)
    const shiftKey = (event.nativeEvent as Event & { shiftKey?: boolean }).shiftKey === true
    const index = visibleRows.findIndex((candidate) => rowKey(candidate) === id)
    setSelected((old) => {
      const next = new Set(old)
      const anchorIndex = shiftKey && selectionAnchor ? visibleRows.findIndex((candidate) => rowKey(candidate) === selectionAnchor) : -1
      if (anchorIndex >= 0) {
        const start = Math.min(anchorIndex, index)
        const end = Math.max(anchorIndex, index)
        for (let i = start; i <= end; i += 1) event.target.checked ? next.add(rowKey(visibleRows[i])) : next.delete(rowKey(visibleRows[i]))
      } else if (event.target.checked) next.add(id)
      else next.delete(id)
      return next
    })
    setSelectionAnchor(id)
  }
  const rows = (items: ProbeError[], locked: boolean) => items.map((row) => <tr className="border-t border-border/60 align-top" key={`${row.kind}:${row.source_id}`}>
    <td className="p-2"><input type="checkbox" aria-label={`Select ${row.title}`} checked={selected.has(rowKey(row))} onChange={(e) => toggleRowSelection(row, e)} /></td>
    <td className="p-2 text-white"><div className="font-medium flex items-center gap-1"><span className="mr-1 rounded border border-border px-1 text-[10px] text-white">{row.kind === 'episode' ? 'S' : 'M'}</span>{row.title}{row.year ? ` (${row.year})` : ''}</div><div className="text-[11px] text-white">{row.kind === 'episode' ? 'Episode source' : 'Movie source'}</div></td>
    <td className="p-2 text-white">{row.provider_name}</td><td className="p-2 text-white">{row.configured_language || 'Unknown'}</td><td className="p-2 text-white">{row.category || '—'}</td><td className="p-2 text-white">{row.attempts}</td><td className="p-2 break-words text-white"><div className="text-amber-300">{row.reason_detail}</div><div className="text-[10px] text-white">{/^\d+$/.test(row.reason_code) ? `HTTP ${row.reason_code}` : row.reason_code}</div></td>
    <td className="p-2"><span className="flex gap-1"><button title="Approve after manual playback review" disabled={action.isPending} className="p-1 rounded border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40" onClick={() => action.mutate({ row, value: 'approve' })}><Check size={13} /></button><button title="Retry this source" disabled={action.isPending} className="p-1 rounded border border-primary/40 text-primary hover:bg-accent disabled:opacity-40" onClick={() => action.mutate({ row, value: 'retry' })}><RotateCcw size={13} /></button><button title="Delete this source" disabled={action.isPending} className="p-1 rounded border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-40" onClick={() => window.confirm(`Delete the ${row.provider_name} source for ${row.title}? This cannot be undone.`) && action.mutate({ row, value: 'delete' })}><Trash2 size={13} /></button></span>{locked && <div className="text-[10px] text-red-300 mt-1">Review required</div>}</td>
  </tr>)
  const table = (items: ProbeError[], locked: boolean) => items.length ? <div className="overflow-x-auto border border-border rounded-md"><table className="w-full table-fixed text-xs text-white"><thead className="bg-card text-white"><tr className="text-left"><th className="p-2 w-8"><input type="checkbox" aria-label="Select all issues in this section" checked={items.every((row) => selected.has(rowKey(row)))} onChange={(e) => setSelected((old) => { const next = new Set(old); items.forEach((row) => e.target.checked ? next.add(rowKey(row)) : next.delete(rowKey(row))); return next })} /></th><th className="p-2 w-[34%]">Title / play</th><th className="p-2 w-[12%]">Provider</th><th className="p-2 w-[10%]">Configured</th><th className="p-2 w-[14%]">Category</th><th className="p-2 w-[7%]">Attempts</th><th className="p-2 w-[15%]">Reason</th><th className="p-2 w-[12%]">Actions</th></tr></thead><tbody>{rows(items, locked)}</tbody></table></div> : <div className="border border-border rounded-md bg-card p-6 text-center text-sm text-white">Nothing in this section.</div>

  if (report.isLoading) return <div className="flex items-center gap-2 text-white"><Loader2 size={15} className="animate-spin" /> Loading probe issues…</div>
  if (report.isError) return <div className="text-destructive">Could not load language probe issues.</div>
  return <div className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-lg font-semibold text-white">Language Probe Issues</h1><p className="text-xs text-white mt-1">Deferred sources stay in rotation after their first provider failure. Two provider failures quarantine a source from automatic probing and normal playback until you decide what to do.</p></div><span className="flex gap-2"><button className="flex items-center gap-1.5 border border-border rounded px-2 py-1.5 text-xs text-white hover:bg-accent" onClick={() => window.dispatchEvent(new CustomEvent('vodmanager-goto', { detail: 'language-report' }))}><ArrowLeft size={13} /> Mismatch report</button><button className="flex items-center gap-1.5 border border-border rounded px-2 py-1.5 text-xs text-white hover:bg-accent disabled:opacity-50" disabled={report.isFetching} onClick={() => report.refetch()}><RefreshCw size={13} className={report.isFetching ? 'animate-spin' : ''} /> Refresh</button></span></div>
    <div className="grid gap-3 md:grid-cols-3"><div className="rounded-md border border-border bg-card px-3 py-2 text-white"><div className="text-[11px] text-white">Deferred</div><div className="text-lg font-semibold">{deferred.length.toLocaleString()}</div><div className="text-[11px] text-white">First provider failure; still rotates</div></div><div className="rounded-md border border-amber-500/30 bg-card px-3 py-2 text-white"><div className="text-[11px] text-white">Quarantined</div><div className="text-lg font-semibold text-amber-300">{quarantined.length.toLocaleString()}</div><div className="text-[11px] text-white">Two failures; review required</div></div><div className="rounded-md border border-border bg-card px-3 py-2 text-white"><div className="text-[11px] text-white">Total issues</div><div className="text-lg font-semibold">{errors.length.toLocaleString()}</div><div className="text-[11px] text-white">Play, approve, retry, or delete</div></div></div>
    <div className="flex flex-wrap items-center gap-2 border border-border rounded-md bg-card px-3 py-2 text-xs text-white"><span>{selected.size} selected</span><details className="relative ml-auto"><summary className="cursor-pointer rounded border border-border px-2 py-1 text-[11px]">Provider{providerFilters.length ? ` (${providerFilters.length})` : ''}</summary><div className="absolute right-0 z-30 mt-1 max-h-60 min-w-48 overflow-auto rounded border border-border bg-card p-2 shadow-lg">{providerOptions.map((provider) => <label className="flex items-center gap-2 whitespace-nowrap py-1 text-[11px] text-white" key={provider}><input type="checkbox" checked={providerFilters.includes(provider)} onChange={() => setProviderFilters((old) => old.includes(provider) ? old.filter((value) => value !== provider) : [...old, provider])} />{provider}</label>)}{providerFilters.length > 0 && <button className="mt-1 text-[11px] text-primary" onClick={() => setProviderFilters([])}>Clear providers</button>}</div></details><button className="border border-border rounded px-2 py-1 hover:bg-accent" onClick={() => { setSelected((old) => old.size === filteredErrors.length ? new Set() : new Set(filteredErrors.map((row) => rowKey(row)))); setSelectionAnchor(null) }}>{selected.size === filteredErrors.length && filteredErrors.length ? 'Clear selection' : 'Select all shown'}</button><button className="border border-emerald-500/40 text-emerald-400 rounded px-2 py-1 hover:bg-emerald-500/10 disabled:opacity-50" disabled={action.isPending || !selected.size} onClick={() => bulkAction('approve')}>Approve selected</button><button className="border border-primary/40 text-primary rounded px-2 py-1 hover:bg-accent disabled:opacity-50" disabled={action.isPending || !selected.size} onClick={() => bulkAction('retry')}>Retry selected</button><button className="border border-red-500/40 text-red-400 rounded px-2 py-1 hover:bg-red-500/10 disabled:opacity-50" disabled={action.isPending || !selected.size} onClick={() => bulkAction('delete')}>Delete selected</button></div>
    <details className="space-y-2"><summary className="cursor-pointer text-sm font-semibold text-white">Deferred sources ({filteredDeferred.length})</summary><div className="pt-2"><p className="text-[11px] text-white">These sources failed once and remain eligible. Approve after playback review to mark them checked, retry them, or delete them.</p>{table(filteredDeferred, false)}</div></details>
    <details className="space-y-2"><summary className="cursor-pointer text-sm font-semibold text-amber-300">Quarantined sources ({filteredQuarantined.length})</summary><div className="pt-2"><p className="text-[11px] text-white">These sources failed twice and are excluded from automatic probing and normal playback. Manual review is required.</p>{table(filteredQuarantined, true)}</div></details>
  </div>
}
