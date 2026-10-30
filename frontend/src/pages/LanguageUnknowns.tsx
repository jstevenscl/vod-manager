import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, Check, Loader2, RefreshCw, RotateCcw, Trash2 } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '@/lib/api'
import { notify } from '@/lib/confirm'


type UnknownRow = { kind: 'movie' | 'episode'; source_id: number; title: string; year: number | null; provider_name: string; configured_language: string | null; observed_languages: string[]; container_extension: string | null; language_probed_at: string | null; season_number?: number; episode_number?: number; episode_name?: string | null }
type UnknownReport = { results: UnknownRow[]; enabled_languages: string[]; primary_language: string }
type ActionValue = 'approve' | 'retry' | 'delete'

export default function LanguageUnknowns() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [selectionAnchor, setSelectionAnchor] = useState<string | null>(null)
  const [provider, setProvider] = useState('')
  const [sort, setSort] = useState<'checked' | 'title' | 'provider'>('checked')
  const [rowLanguages, setRowLanguages] = useState<Record<string, string>>({})
  const [bulkLanguage, setBulkLanguage] = useState('')
  const settings = useQuery<{ language_trickle_enabled: boolean }>({ queryKey: ['language-trickle-settings'], queryFn: () => api.get('/vod/language-trickle/settings/').then((r) => r.data) })
  const report = useQuery<UnknownReport>({ queryKey: ['language-unknown-report'], queryFn: () => api.get('/vod/language-unknown-report/').then((r) => r.data), refetchInterval: () => settings.data?.language_trickle_enabled ? 30_000 : false })
  const action = useMutation({
    mutationFn: ({ row, value, language }: { row: UnknownRow; value: ActionValue; language?: string }) => api.post(`/vod/language-probe-errors/${row.kind}/${row.source_id}/action/`, { action: value, ...(language ? { language } : {}) }).then((r) => r.data),
    onSuccess: (result) => { notify(result.action === 'deleted' ? 'Source deleted.' : result.action === 'approved' ? `${result.language || 'Language'} recorded as confirmed. This source will not be scanned again.` : 'Source returned to the probe queue.'); qc.invalidateQueries({ queryKey: ['language-unknown-report'] }); qc.invalidateQueries({ queryKey: ['language-mismatch-report'] }) },
    onError: (e: any) => notify(e?.response?.data?.detail ?? 'Could not update the unknown-language source.'),
  })
  const rows = report.data?.results ?? []
  const enabledLanguages = report.data?.enabled_languages ?? []
  const primaryLanguage = report.data?.primary_language ?? enabledLanguages[0] ?? ''
  useEffect(() => { if (primaryLanguage && !bulkLanguage) setBulkLanguage(primaryLanguage) }, [primaryLanguage, bulkLanguage])
  const visible = useMemo(() => [...rows].filter((row) => !provider || row.provider_name === provider).sort((a, b) => sort === 'title' ? a.title.localeCompare(b.title) : sort === 'provider' ? a.provider_name.localeCompare(b.provider_name) : (b.language_probed_at || '').localeCompare(a.language_probed_at || '')), [rows, provider, sort])
  const key = (row: UnknownRow) => `${row.kind}:${row.source_id}`
  const languageFor = (row: UnknownRow) => rowLanguages[key(row)] || (row.configured_language && enabledLanguages.includes(row.configured_language) ? row.configured_language : primaryLanguage)

  const toggleRowSelection = (row: UnknownRow, index: number, event: React.ChangeEvent<HTMLInputElement>) => {
    const id = key(row)
    const shiftKey = (event.nativeEvent as Event & { shiftKey?: boolean }).shiftKey === true
    setSelected((old) => {
      const next = new Set(old)
      const anchorIndex = shiftKey && selectionAnchor ? visible.findIndex((candidate) => key(candidate) === selectionAnchor) : -1
      if (anchorIndex >= 0) {
        for (let i = Math.min(anchorIndex, index); i <= Math.max(anchorIndex, index); i += 1) {
          const rangeId = key(visible[i])
          event.target.checked ? next.add(rangeId) : next.delete(rangeId)
        }
      } else if (event.target.checked) next.add(id)
      else next.delete(id)
      return next
    })
    setSelectionAnchor(id)
  }

  const run = (row: UnknownRow, value: ActionValue) => {
    const language = languageFor(row)
    const message = value === 'approve' ? `Record ${language} as the confirmed language for ${row.title}?\n\nThis saves the language choice, marks it validated, and stops future scans for this source. Nothing will be archived.` : value === 'retry' ? `Retry the language check for ${row.title}?` : `Delete the ${row.provider_name} source for ${row.title}? This cannot be undone.`
    if (value !== 'approve' || language) {
      if (window.confirm(message)) action.mutate({ row, value, ...(value === 'approve' ? { language } : {}) })
    } else notify('Enable at least one playback language before confirming a source.')
  }

  const bulk = (value: ActionValue) => {
    const targets = visible.filter((row) => selected.has(key(row)))
    if (!targets.length) { notify('No unknown-language sources are selected.'); return }
    if (value === 'approve' && !bulkLanguage) { notify('Choose an enabled language to confirm.'); return }
    const message = value === 'approve' ? `Record ${bulkLanguage} as the confirmed language for ${targets.length} selected source${targets.length === 1 ? '' : 's'}?\n\nThis saves the language choice, marks each source validated, and stops future scans for them. Nothing will be archived.` : value === 'retry' ? `Retry ${targets.length} selected language checks?` : `Delete ${targets.length} selected source${targets.length === 1 ? '' : 's'}? This cannot be undone.`
    if (!window.confirm(message)) return
    targets.reduce((promise, row) => promise.then(() => action.mutateAsync({ row, value, ...(value === 'approve' ? { language: bulkLanguage } : {}) })), Promise.resolve()).then(() => { setSelected(new Set()); setSelectionAnchor(null) })
  }

  if (report.isLoading) return <div className="flex items-center gap-2 text-muted-foreground"><Loader2 size={15} className="animate-spin" /> Loading unknown-language review…</div>
  if (report.isError) return <div className="text-destructive">Could not load unknown-language review.</div>
  return <div className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-lg font-semibold">Unknown Language Review</h1><p className="text-xs text-muted-foreground mt-1">Choose and confirm the language for files with no usable embedded language tag. Confirmed sources are recorded as validated and are not scanned again.</p></div><span className="flex gap-2"><button className="flex items-center gap-1.5 border border-border rounded px-2 py-1.5 text-xs hover:bg-accent" onClick={() => window.dispatchEvent(new CustomEvent('vodmanager-goto', { detail: 'language-report' }))}><ArrowLeft size={13} /> Mismatch report</button><button className="flex items-center gap-1.5 border border-border rounded px-2 py-1.5 text-xs" disabled={report.isFetching} onClick={() => report.refetch()}><RefreshCw size={12} /> Refresh</button></span></div>
    <div className="rounded-md border border-border bg-card px-3 py-2 text-xs text-white">{enabledLanguages.length === 1 ? `Confirming a source records ${enabledLanguages[0]} as its language, marks it validated, and prevents future scans.` : 'Choose from enabled playback languages. Your selection is saved as the source language, marks it validated, and prevents future scans.'} Unknown results are never archived by this action.</div>
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs"><span>{visible.length} shown · {selected.size} selected</span><span className="text-muted-foreground">Shift-click rows to select a range.</span><select className="ml-auto rounded border border-border bg-background px-2 py-1" value={provider} onChange={(e) => setProvider(e.target.value)}><option value="">All providers</option>{[...new Set(rows.map((row) => row.provider_name))].sort().map((name) => <option key={name}>{name}</option>)}</select><select className="rounded border border-border bg-background px-2 py-1" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}><option value="checked">Newest checked</option><option value="title">Title</option><option value="provider">Provider</option></select><select aria-label="Language to confirm for selected sources" className="rounded border border-border bg-background px-2 py-1" value={bulkLanguage} disabled={enabledLanguages.length <= 1} onChange={(e) => setBulkLanguage(e.target.value)}>{enabledLanguages.map((code) => <option key={code} value={code}>{code}</option>)}</select><button className="border border-border rounded px-2 py-1 hover:bg-accent" onClick={() => { setSelected((old) => old.size === visible.length ? new Set() : new Set(visible.map(key))); setSelectionAnchor(null) }}>{selected.size === visible.length && visible.length ? 'Clear selection' : 'Select all shown'}</button><button className="border border-emerald-500/40 text-emerald-400 rounded px-2 py-1 disabled:opacity-50" disabled={action.isPending || !selected.size || !bulkLanguage} onClick={() => bulk('approve')}>Confirm selected as {bulkLanguage || '—'}</button><button className="border border-primary/40 text-primary rounded px-2 py-1 disabled:opacity-50" disabled={action.isPending || !selected.size} onClick={() => bulk('retry')}>Retry selected</button><button className="border border-red-500/40 text-red-400 rounded px-2 py-1 disabled:opacity-50" disabled={action.isPending || !selected.size} onClick={() => bulk('delete')}>Delete selected</button></div>
    {visible.length ? <div className="overflow-x-auto border border-border rounded-md"><table className="w-full table-fixed text-xs"><thead className="bg-card"><tr className="text-left"><th className="p-2 w-8"><input type="checkbox" aria-label="Select all shown unknown-language sources" checked={visible.length > 0 && visible.every((row) => selected.has(key(row)))} onChange={(e) => { setSelected((old) => { const next = new Set(old); visible.forEach((row) => e.target.checked ? next.add(key(row)) : next.delete(key(row))); return next }); setSelectionAnchor(null) }} /></th><th className="p-2 w-[35%]">Title / play</th><th className="p-2 w-[15%]">Provider</th><th className="p-2 w-[12%]">Configured</th><th className="p-2 w-[10%]">Observed</th><th className="p-2 w-[20%]">Confirm language / actions</th></tr></thead><tbody>{visible.map((row, index) => <tr className="border-t border-border/60 align-top" key={key(row)}><td className="p-2"><input type="checkbox" aria-label={`Select ${row.title}`} checked={selected.has(key(row))} onChange={(e) => toggleRowSelection(row, index, e)} /></td><td className="p-2"><div className="font-medium flex items-center gap-1"><span className="rounded border border-border px-1 text-[10px]">{row.kind === 'episode' ? 'S' : 'M'}</span>{row.title}{row.year ? ` (${row.year})` : ''}</div><div className="text-[11px] text-muted-foreground">{row.kind === 'episode' ? `S${row.season_number}E${row.episode_number} · ${row.episode_name || ''}` : 'Movie'}</div></td><td className="p-2">{row.provider_name}</td><td className="p-2">{row.configured_language || 'Unknown'}</td><td className="p-2 text-amber-300">Unknown</td><td className="p-2"><div className="flex flex-wrap gap-1"><select aria-label={`Language to confirm for ${row.title}`} className="rounded border border-border bg-background px-1 py-1" value={languageFor(row)} disabled={enabledLanguages.length <= 1} onChange={(e) => setRowLanguages((old) => ({ ...old, [key(row)]: e.target.value }))}>{enabledLanguages.map((code) => <option key={code} value={code}>{code}</option>)}</select><button className="px-1.5 py-1 rounded border border-emerald-500/40 text-emerald-400" disabled={action.isPending || !languageFor(row)} onClick={() => run(row, 'approve')}><Check size={13} /> Confirm {languageFor(row) || ''}</button><button className="px-1.5 py-1 rounded border border-primary/40 text-primary" disabled={action.isPending} onClick={() => run(row, 'retry')}><RotateCcw size={13} /> Retry</button><button className="px-1.5 py-1 rounded border border-red-500/40 text-red-400" disabled={action.isPending} onClick={() => run(row, 'delete')}><Trash2 size={13} /> Delete</button></div></td></tr>)}</tbody></table></div> : <div className="border border-border rounded-md bg-card p-8 text-center text-sm text-muted-foreground">No unknown-language sources need review.</div>}
  </div>
}
