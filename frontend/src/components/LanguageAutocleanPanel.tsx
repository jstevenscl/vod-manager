// KNM: 2026-11-05 - language auto-clean controls: default off, preview first, every change revertible.
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, Loader2, Power, Undo2 } from 'lucide-react'
import api from '@/lib/api'
import { askConfirm, notify } from '@/lib/confirm'

type Settings = {
  language_autoclean_enabled: boolean
  language_autoclean_batch_size: number
  language_autoclean_interval_seconds: number
  language_autoclean_cooldown_seconds: number
  status: { last_run_at: number | null; last_status: string | null; last_applied: number; busy_reason: string | null }
}
type Candidate = { kind: string; source_id: number; title: string; current_language: string | null; observed_languages: string[]; tmdb_original_language: string | null; tmdb_spoken_languages: string[] }
type Preview = { candidates: Candidate[]; skipped: Record<string, number> }
type LogRow = { id: number; kind: string; source_id: number; previous_language: string | null; new_language: string | null; applied_at: string; reverted_at: string | null }

const SKIP_LABELS: Record<string, string> = {
  probe_has_wanted: 'audio includes a wanted language',
  only_source: 'only feed for the title (left for manual review)',
  no_tmdb_data: 'no TMDB data stored',
  tmdb_original_language: 'TMDB original language is wanted',
  tmdb_spoken_language: 'TMDB lists a wanted spoken language',
}
const STATUS_LABELS: Record<string, string> = {
  disabled: 'off', paused_busy: 'paused, another job is running', cooldown: 'waiting for the cooldown after other jobs', applied: 'hid feeds last cycle', idle: 'nothing to do last cycle',
}

export default function LanguageAutocleanPanel() {
  const qc = useQueryClient()
  const settings = useQuery<Settings>({ queryKey: ['language-autoclean-settings'], queryFn: () => api.get('/vod/language-autoclean/settings/').then((r) => r.data), refetchInterval: 60_000 })
  const log = useQuery<{ results: LogRow[] }>({ queryKey: ['language-autoclean-log'], queryFn: () => api.get('/vod/language-autoclean/log/').then((r) => r.data) })
  const [preview, setPreview] = useState<Preview | null>(null)
  const [form, setForm] = useState({ batch_size: '25', interval_seconds: '300', cooldown_seconds: '900' })
  useEffect(() => {
    if (settings.data) setForm({ batch_size: String(settings.data.language_autoclean_batch_size), interval_seconds: String(settings.data.language_autoclean_interval_seconds), cooldown_seconds: String(settings.data.language_autoclean_cooldown_seconds) })
  }, [settings.data])
  const refresh = () => { qc.invalidateQueries({ queryKey: ['language-autoclean-settings'] }); qc.invalidateQueries({ queryKey: ['language-autoclean-log'] }); qc.invalidateQueries({ queryKey: ['language-mismatch-report'] }) }

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post('/vod/language-autoclean/settings/', body).then((r) => r.data),
    onSuccess: () => { refresh(); notify('Language auto-clean settings saved.') },
  })
  const runPreview = useMutation({
    mutationFn: () => api.get('/vod/language-autoclean/preview/').then((r) => r.data as Preview),
    onSuccess: (data) => setPreview(data),
  })
  const revert = useMutation({
    mutationFn: (id: number) => api.post(`/vod/language-autoclean/log/${id}/revert/`).then((r) => r.data),
    onSuccess: () => { refresh(); notify('Feed restored. Auto-clean will not touch it again.') },
    onError: () => notify('Could not revert: it was already reverted or the feed was changed since.'),
  })

  const enabled = !!settings.data?.language_autoclean_enabled
  const status = settings.data?.status
  const toggle = () => {
    if (enabled) return save.mutate({ enabled: false })
    askConfirm('Turn on language auto-clean? It hides a feed only when the audio check and TMDB both say it is not a wanted language, and only when another wanted feed keeps the title playable. Every change is logged and can be reverted.', () => save.mutate({ enabled: true }), 'Turn on')
  }

  return (
    <details className="rounded-md border border-border bg-card px-3 py-3">
      <summary className="cursor-pointer text-xs font-medium">Auto-clean unwanted-language feeds ({enabled ? 'on' : 'off'})</summary>
      <p className="text-[11px] text-muted-foreground mt-2">Runs small batches through the day, pauses while imports, syncs or enrichment run, then waits the cooldown. Never touches a feed someone already decided.{status?.last_status ? ` Last cycle: ${STATUS_LABELS[status.last_status] ?? status.last_status}${status.busy_reason ? ` (${status.busy_reason})` : ''}.` : ''}</p>
      <div className="flex flex-wrap items-end gap-3 mt-2">
        <button className={`flex items-center gap-1.5 border rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50 ${enabled ? 'border-amber-500/40 text-amber-400' : 'border-emerald-500/40 text-emerald-400'}`} disabled={save.isPending || !settings.data} onClick={toggle}>{save.isPending ? <Loader2 size={12} className="animate-spin" /> : <Power size={12} />} {enabled ? 'Turn off auto-clean' : 'Turn on auto-clean'}</button>
        <label className="text-xs">Feeds per batch<input className="block mt-1 w-24 rounded border border-border bg-background px-2 py-1" type="number" min="1" max="500" value={form.batch_size} onChange={(e) => setForm({ ...form, batch_size: e.target.value })} /></label>
        <label className="text-xs">Seconds between batches<input className="block mt-1 w-32 rounded border border-border bg-background px-2 py-1" type="number" min="60" max="86400" value={form.interval_seconds} onChange={(e) => setForm({ ...form, interval_seconds: e.target.value })} /></label>
        <label className="text-xs">Cooldown after other jobs (s)<input className="block mt-1 w-32 rounded border border-border bg-background px-2 py-1" type="number" min="0" max="86400" value={form.cooldown_seconds} onChange={(e) => setForm({ ...form, cooldown_seconds: e.target.value })} /></label>
        <button className="border border-primary/40 text-primary rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50" disabled={save.isPending} onClick={() => save.mutate({ batch_size: Number(form.batch_size), interval_seconds: Number(form.interval_seconds), cooldown_seconds: Number(form.cooldown_seconds) })}>Save pacing</button>
        <button className="flex items-center gap-1.5 border border-border rounded px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50" disabled={runPreview.isPending} onClick={() => runPreview.mutate()}>{runPreview.isPending ? <Loader2 size={12} className="animate-spin" /> : <Eye size={12} />} Preview what it would hide</button>
      </div>

      {preview && (
        <div className="mt-3 text-xs">
          <p className="font-medium">{preview.candidates.length.toLocaleString()} feed(s) would be hidden{preview.candidates.length >= 200 ? ' (first 200 shown)' : ''}.</p>
          <ul className="text-[11px] text-muted-foreground mt-1">{Object.entries(preview.skipped).filter(([, n]) => n > 0).map(([k, n]) => <li key={k}>Left alone, {SKIP_LABELS[k] ?? k}: {n.toLocaleString()}</li>)}</ul>
          {!!preview.candidates.length && <div className="mt-2 max-h-64 overflow-auto border border-border rounded"><table className="w-full text-[11px]"><thead><tr className="text-left text-muted-foreground"><th className="px-2 py-1">Title</th><th className="px-2 py-1">Type</th><th className="px-2 py-1">Label</th><th className="px-2 py-1">Audio</th><th className="px-2 py-1">TMDB</th></tr></thead><tbody>{preview.candidates.map((c) => <tr key={`${c.kind}-${c.source_id}`} className="border-t border-border"><td className="px-2 py-1">{c.title}</td><td className="px-2 py-1">{c.kind}</td><td className="px-2 py-1">{c.current_language ?? 'EN'}</td><td className="px-2 py-1">{c.observed_languages.join(', ')}</td><td className="px-2 py-1">{c.tmdb_original_language ?? '—'} / {c.tmdb_spoken_languages.join(', ') || '—'}</td></tr>)}</tbody></table></div>}
        </div>
      )}

      {!!log.data?.results.length && (
        <div className="mt-3 text-xs">
          <p className="font-medium">Recent auto-clean changes</p>
          <div className="mt-1 max-h-64 overflow-auto border border-border rounded"><table className="w-full text-[11px]"><thead><tr className="text-left text-muted-foreground"><th className="px-2 py-1">When</th><th className="px-2 py-1">Feed</th><th className="px-2 py-1">Label</th><th className="px-2 py-1"></th></tr></thead><tbody>{log.data.results.map((row) => <tr key={row.id} className="border-t border-border"><td className="px-2 py-1">{row.applied_at}</td><td className="px-2 py-1">{row.kind} #{row.source_id}</td><td className="px-2 py-1">{row.previous_language ?? 'EN'} → {row.new_language}</td><td className="px-2 py-1">{row.reverted_at ? <span className="text-muted-foreground">reverted</span> : <button className="inline-flex items-center gap-1 border border-border rounded px-1.5 py-0.5 hover:bg-accent disabled:opacity-50" disabled={revert.isPending} onClick={() => revert.mutate(row.id)}><Undo2 size={11} /> Revert</button>}</td></tr>)}</tbody></table></div>
        </div>
      )}
    </details>
  )
}
