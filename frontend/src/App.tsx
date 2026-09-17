import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, CalendarDays, CheckCircle2, CircleAlert, ClipboardCheck, Film, Flame, Globe2, HardDriveDownload, LayoutGrid, Loader2, LogOut, Moon,
  Palette, RefreshCw, Search, Settings as SettingsIcon, Sun, Tv, Users, Wrench,
} from 'lucide-react'
import VodManager, { type DvrSubTab, type VodManagerTab } from '@/pages/VodManager'
import Login from '@/pages/Login'
import Settings from '@/pages/Settings'
import api from '@/lib/api'

export const THEMES = ['dark', 'mid', 'light', 'mono', 'warm'] as const
export type Theme = typeof THEMES[number]

const THEME_META: Record<Theme, { label: string; icon: React.ReactNode }> = {
  dark:  { label: 'Dark',  icon: <Moon size={11} /> },
  mid:   { label: 'Mid',   icon: <Palette size={11} /> },
  light: { label: 'Light', icon: <Sun size={11} /> },
  mono:  { label: 'Mono',  icon: <span className="text-[10px] font-bold leading-none">M</span> },
  warm:  { label: 'Warm',  icon: <Flame size={11} /> },
}

function initTheme(): Theme {
  const saved = localStorage.getItem('vodmanager-theme') as Theme | null
  const t: Theme = (saved && (THEMES as readonly string[]).includes(saved)) ? saved as Theme : 'dark'
  document.documentElement.setAttribute('data-theme', t)
  return t
}

type AuthState = 'checking' | 'login' | 'ready'

interface NavItem {
  label: string
  icon: React.ReactNode
  tab: VodManagerTab
  dvrSubTab?: DvrSubTab
}
interface NavGroup {
  label: string
  items: NavItem[]
}
interface RuntimeStatus {
  import: { running: boolean; queued?: boolean; queue_position?: number | null; provider_name: string | null; error: string | null }
  enrichment: { running: boolean; movies_done: number; movies_total: number; series_done: number; series_total: number }
  tmdb: { running: boolean; done: number; total: number }
  bulk_ai: { running: boolean; done: number; total: number; jobs: number }
  catalog_workflow: {
    state: 'idle' | 'queued' | 'running' | 'ready' | 'failed'
    phase: string | null
    provider_name: string | null
    started_at: number | null
    finished_at: number | null
    error: string | null
  }
  review_summary: {
    missing_identity: { movies: number; series: number }
    invalid_tmdb: { movies: number; series: number }
  }
  process_cpu_percent: number | null
}
const NAV_GROUPS: NavGroup[] = [
  { label: 'VOD Library', items: [
    { label: 'Movies', icon: <Film size={15} />, tab: 'movies' },
    { label: 'TV Shows', icon: <Tv size={15} />, tab: 'series' },
  ] },
  { label: 'DVR', items: [
    { label: 'Scheduled', icon: <CalendarDays size={15} />, tab: 'dvr', dvrSubTab: 'scheduled' },
    { label: 'Users', icon: <Users size={15} />, tab: 'dvr', dvrSubTab: 'users' },
    { label: 'Library', icon: <HardDriveDownload size={15} />, tab: 'dvr', dvrSubTab: 'library' },
    { label: 'Missing', icon: <Search size={15} />, tab: 'dvr', dvrSubTab: 'missing' },
    { label: 'Metrics', icon: <LayoutGrid size={15} />, tab: 'dvr', dvrSubTab: 'metrics' },
  ] },
  { label: 'Operations', items: [
    { label: 'Providers', icon: <RefreshCw size={15} />, tab: 'providers' },
    { label: 'Metadata Review', icon: <Search size={15} />, tab: 'metadata' },
    { label: 'Stream Recovery', icon: <Activity size={15} />, tab: 'recovery' },
    { label: 'Curation & Maintenance', icon: <Wrench size={15} />, tab: 'curation' },
  ] },
  { label: 'System', items: [
    { label: 'Configuration', icon: <SettingsIcon size={15} />, tab: 'config' },
  ] },
]

export default function App() {
  const [showSettings, setShowSettings] = useState(false)
  const [authState, setAuthState]       = useState<AuthState>('checking')
  const [theme, setThemeState]          = useState<Theme>(initTheme)
  const queryClient = useQueryClient()

  const [activeTab, setActiveTabState] = useState<VodManagerTab>(() => {
    const saved = localStorage.getItem('vodmanager-tab')
    return saved === 'movies' || saved === 'series' || saved === 'metadata' || saved === 'recovery' || saved === 'curation' || saved === 'config' || saved === 'dvr' ? saved : 'movies'
  })
  function setActiveTab(t: VodManagerTab) {
    localStorage.setItem('vodmanager-tab', t)
    setActiveTabState(t)
  }
  const [dvrSubTab, setDvrSubTab] = useState<DvrSubTab>(() => {
    const saved = localStorage.getItem('vodmanager-dvr-subtab')
    return saved === 'scheduled' || saved === 'users' || saved === 'library' || saved === 'metrics' ? saved : 'scheduled'
  })
  function setDvrSubTabPersisted(t: DvrSubTab) {
    localStorage.setItem('vodmanager-dvr-subtab', t)
    setDvrSubTab(t)
  }
  function goto(item: NavItem) {
    setActiveTab(item.tab)
    if (item.dvrSubTab) setDvrSubTabPersisted(item.dvrSubTab)
  }

  function setTheme(t: Theme) {
    document.documentElement.setAttribute('data-theme', t)
    localStorage.setItem('vodmanager-theme', t)
    setThemeState(t)
  }

  const [firstRunDismissed, setFirstRunDismissed] = useState(
    () => localStorage.getItem('vodmanager-firstrun-dismissed') === '1'
  )

  const versionQuery = useQuery<{ version: string; commit: string; ref: string }>({
    queryKey: ['app-version'],
    queryFn:  () => api.get('/version/').then((r) => r.data),
    staleTime: Infinity,
  })

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn:  () => api.get('/settings/').then((r) => r.data),
    staleTime: 30_000,
    retry: false,
  })

  const hideDvrTabQuery = useQuery<{ hidden: boolean }>({
    queryKey: ['hide-dvr-tab'],
    queryFn:  () => api.get('/vod/hide-dvr-tab/').then((r) => r.data),
    enabled: authState === 'ready',
    staleTime: 30_000,
  })
  const runtimeStatusQuery = useQuery<RuntimeStatus>({
    queryKey: ['vod-runtime-status'],
    queryFn: () => api.get('/vod/runtime-status/').then((r) => r.data),
    enabled: authState === 'ready',
    refetchInterval: (query) => {
      const status = query.state.data
      return status?.import.running || status?.enrichment.running || status?.tmdb.running || status?.bulk_ai.running ? 2000 : 10_000
    },
    retry: false,
  })
  const externalIpQuery = useQuery<{ ip: string | null; available: boolean }>({
    queryKey: ['external-ip'],
    queryFn: () => api.get('/external-ip/').then((r) => r.data),
    enabled: authState === 'ready',
    staleTime: 5 * 60_000,
    retry: false,
  })
  const workflow = runtimeStatusQuery.data?.catalog_workflow
  const reviewSummary = runtimeStatusQuery.data?.review_summary
  const workflowIsActive = workflow?.state === 'queued' || workflow?.state === 'running'
  const workflowIsReady = workflow?.state === 'ready'
  const workflowHasFailed = workflow?.state === 'failed'
  const workflowDurationSeconds = workflow?.started_at && workflow?.finished_at
    ? Math.max(0, Math.round(workflow.finished_at - workflow.started_at))
    : null
  const missingIdentitySummary = reviewSummary
    ? `${reviewSummary.missing_identity.movies} movie${reviewSummary.missing_identity.movies === 1 ? '' : 's'} · ${reviewSummary.missing_identity.series} TV show${reviewSummary.missing_identity.series === 1 ? '' : 's'} need identity review`
    : 'Loading review summary…'
  const invalidTmdbTotal = reviewSummary
    ? reviewSummary.invalid_tmdb.movies + reviewSummary.invalid_tmdb.series
    : 0
  const activeWorkflowDetail = runtimeStatusQuery.data?.tmdb.running
    ? `TMDB identities ${runtimeStatusQuery.data.tmdb.done}/${runtimeStatusQuery.data.tmdb.total}`
    : runtimeStatusQuery.data?.enrichment.running
      ? `Details: ${runtimeStatusQuery.data.enrichment.movies_done}/${runtimeStatusQuery.data.enrichment.movies_total} movies · ${runtimeStatusQuery.data.enrichment.series_done}/${runtimeStatusQuery.data.enrichment.series_total} series`
      : workflow?.phase ?? 'Working on catalog…'
  const navGroups = hideDvrTabQuery.data?.hidden ? NAV_GROUPS.filter((g) => g.label !== 'DVR') : NAV_GROUPS

  useEffect(() => {
    if (isLoading) return
    if (!settings?.has_credentials) {
      setAuthState('ready')
      return
    }
    const token = localStorage.getItem('vodmanager-session')
    if (!token) { setAuthState('login'); return }
    api.get('/auth/verify/')
      .then((r) => setAuthState(r.data.valid ? 'ready' : 'login'))
      .catch(() => setAuthState('login'))
  }, [isLoading, settings?.has_credentials])

  useEffect(() => {
    // If DVR was hidden (by this admin or another) while it was the active
    // tab -- including on load, from a stale localStorage value -- land
    // somewhere that's still actually in the nav instead of an empty pane.
    if (hideDvrTabQuery.data?.hidden && activeTab === 'dvr') {
      setActiveTab('movies')
    }
  }, [hideDvrTabQuery.data?.hidden, activeTab])

  function handleSkipFirstRun() {
    localStorage.setItem('vodmanager-firstrun-dismissed', '1')
    setFirstRunDismissed(true)
  }

  function handleLogin() {
    setAuthState('ready')
  }

  function handleLogout() {
    api.post('/auth/logout/').finally(() => {
      localStorage.removeItem('vodmanager-session')
      setAuthState('login')
    })
  }

  function handleSettingsSaved() {
    queryClient.invalidateQueries({ queryKey: ['settings'] })
    setShowSettings(false)
  }

  if (isLoading || authState === 'checking') {
    return (
      <div className="flex items-center justify-center min-h-screen text-muted-foreground gap-2">
        <Loader2 size={16} className="animate-spin" />
        <span className="text-sm">Loading…</span>
      </div>
    )
  }

  const needsFirstRun = !settings?.has_credentials && !firstRunDismissed

  if (needsFirstRun || showSettings) {
    return (
      <Settings
        firstRun={needsFirstRun}
        hasCredentials={settings?.has_credentials ?? false}
        onSaved={handleSettingsSaved}
        onBack={!needsFirstRun ? () => setShowSettings(false) : undefined}
        onSkip={needsFirstRun ? handleSkipFirstRun : undefined}
      />
    )
  }

  if (authState === 'login') {
    return <Login onLogin={handleLogin} />
  }

  return (
    <div className="min-h-screen grid grid-cols-[240px_1fr]">
      <aside className="sticky top-0 h-screen flex flex-col border-r border-border bg-card px-2.5 py-4 overflow-y-auto">
        <div className="flex items-center gap-2 px-1.5 pb-4">
          <img src="/favicon.svg" width={26} height={26} alt="" className="rounded-md flex-shrink-0" />
          <div className="min-w-0">
            <div className="text-sm font-bold tracking-tight leading-tight">VOD & DVR Manager - KNM</div>
            {versionQuery.data && (
              <div className="text-[10px] text-muted-foreground font-mono truncate" title={`ref: ${versionQuery.data.ref}`}>
                v{versionQuery.data.version} · {versionQuery.data.commit}
              </div>
            )}
          </div>
        </div>
        <nav className="flex-1 space-y-3.5">
          {navGroups.map((group) => (
            <div key={group.label}>
              <div className="px-2 pb-1 text-[10px] font-bold uppercase tracking-wider text-muted-foreground/70">{group.label}</div>
              {group.items.map((item) => {
                const isActive = activeTab === item.tab && (!item.dvrSubTab || dvrSubTab === item.dvrSubTab)
                return (
                  <button
                    key={item.label}
                    onClick={() => goto(item)}
                    className={`w-full flex items-center gap-2.5 px-2 py-1.5 rounded-md text-[13px] font-medium transition-colors ${
                      isActive
                        ? 'bg-primary/10 text-foreground border border-primary/30'
                        : 'text-muted-foreground border border-transparent hover:text-foreground hover:bg-accent'
                    }`}
                  >
                    <span className={isActive ? 'text-primary [&_svg]:w-[15px] [&_svg]:h-[15px]' : 'opacity-80 [&_svg]:w-[15px] [&_svg]:h-[15px]'}>{item.icon}</span>
                    {item.label}
                  </button>
                )
              })}
            </div>
          ))}
          <div className="rounded-md border border-border bg-background/60 px-2.5 py-2 text-[11px] text-muted-foreground">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Activity size={13} className={workflowIsActive || runtimeStatusQuery.data?.bulk_ai.running ? 'text-primary animate-pulse' : workflowIsReady ? 'text-emerald-400' : workflowHasFailed ? 'text-destructive' : 'text-muted-foreground'} />
              Catalog status
            </div>
            {runtimeStatusQuery.data?.import.queued ? (
              <p className="mt-1">{runtimeStatusQuery.data.import.provider_name ?? 'Provider'} import queued{runtimeStatusQuery.data.import.queue_position && runtimeStatusQuery.data.import.queue_position > 1 ? ` (${runtimeStatusQuery.data.import.queue_position} ahead)` : ''}â€¦</p>
            ) : runtimeStatusQuery.data?.import.running ? (
              <p className="mt-1">Importing {runtimeStatusQuery.data.import.provider_name ?? 'provider'}…</p>
            ) : runtimeStatusQuery.data?.bulk_ai.running ? (
              <p className="mt-1">AI review: {runtimeStatusQuery.data.bulk_ai.done}/{runtimeStatusQuery.data.bulk_ai.total}</p>
            ) : runtimeStatusQuery.data?.tmdb.running ? (
              <p className="mt-1">TMDB: {runtimeStatusQuery.data.tmdb.done}/{runtimeStatusQuery.data.tmdb.total}</p>
            ) : runtimeStatusQuery.data?.enrichment.running ? (
              <p className="mt-1">Enriching: {runtimeStatusQuery.data.enrichment.movies_done}/{runtimeStatusQuery.data.enrichment.movies_total} movies · {runtimeStatusQuery.data.enrichment.series_done}/{runtimeStatusQuery.data.enrichment.series_total} series</p>
            ) : (
              <p className={workflowIsReady ? 'mt-1 text-emerald-400' : workflowHasFailed ? 'mt-1 text-destructive' : 'mt-1'}>
                {workflowIsReady ? 'Catalog ready for review' : workflowHasFailed ? (workflow?.error ?? 'Automatic catalog work needs attention.') : 'Idle'}
              </p>
            )}
            <p className="mt-1 text-[10px] text-muted-foreground/80">App CPU: {runtimeStatusQuery.data?.process_cpu_percent == null ? 'sampling…' : `${runtimeStatusQuery.data.process_cpu_percent}%`}</p>
            <p className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground/80" title="Public address seen by external services">
              <Globe2 size={11} /> External IP: {externalIpQuery.isLoading ? 'checking…' : externalIpQuery.data?.ip ?? 'unavailable'}
            </p>
          </div>
          {workflowIsReady && (
            <div className="rounded-md border border-emerald-500/25 bg-emerald-500/5 px-2.5 py-2 text-[11px]">
              <div className="flex items-center gap-1.5 font-semibold text-foreground">
                <ClipboardCheck size={13} className="text-emerald-400" />
                Review in this order
              </div>
              <button onClick={() => setActiveTab('metadata')} className="mt-1.5 block w-full text-left text-muted-foreground hover:text-foreground">
                <span className="font-semibold text-primary">1. Metadata Review</span><br />
                <span>{missingIdentitySummary}</span>
              </button>
              <button onClick={() => setActiveTab('metadata')} className="mt-1.5 block w-full text-left text-muted-foreground hover:text-foreground">
                <span className="font-semibold text-primary">2. Incorrect TMDB IDs</span><br />
                <span>{invalidTmdbTotal} item{invalidTmdbTotal === 1 ? '' : 's'} need a corrected ID</span>
              </button>
              <button onClick={() => setActiveTab('curation')} className="mt-1.5 block w-full text-left text-muted-foreground hover:text-foreground">
                <span className="font-semibold text-primary">3. Duplicate Finder</span><br />
                <span>Review only remaining ambiguous matches</span>
              </button>
              <button onClick={() => setActiveTab('movies')} className="mt-1.5 block w-full text-left text-muted-foreground hover:text-foreground">
                <span className="font-semibold text-primary">4. Missing artwork</span><br />
                <span>Finish visual cleanup after identity work</span>
              </button>
            </div>
          )}
        </nav>
      </aside>

      <div className="flex flex-col min-w-0">
        <header className="sticky top-0 z-10 relative flex items-center gap-3.5 px-5 py-2.5 border-b border-border bg-card">
          <div className="flex-1 max-w-[380px] flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground/70">
            <Search size={13} className="flex-shrink-0" />
            Search coming soon…
          </div>
          {workflow && workflow.state !== 'idle' && (
            <div className={`pointer-events-none absolute left-1/2 top-1/2 w-[min(42rem,46vw)] -translate-x-1/2 -translate-y-1/2 rounded-md border px-3 py-1 ${
              workflowIsReady ? 'border-emerald-500/35 bg-emerald-500/10' : workflowHasFailed ? 'border-destructive/40 bg-destructive/10' : 'border-primary/35 bg-primary/10'
            }`}>
              <div className="flex items-center justify-center gap-1.5 text-[11px] font-semibold">
                {workflowIsReady ? <CheckCircle2 size={14} className="text-emerald-400" /> : workflowHasFailed ? <CircleAlert size={14} className="text-destructive" /> : <Loader2 size={14} className="animate-spin text-primary" />}
                <span>{workflowIsReady ? 'Import complete · Catalog ready for review' : workflowHasFailed ? 'Automatic catalog work needs attention' : activeWorkflowDetail}</span>
              </div>
              {workflowIsReady && (
                <div className="mt-0.5 text-center text-[10px] text-muted-foreground">
                  {workflowDurationSeconds != null ? `Completed in ${workflowDurationSeconds}s · ` : ''}
                  Review: {missingIdentitySummary}{invalidTmdbTotal ? ` · ${invalidTmdbTotal} incorrect TMDB ID${invalidTmdbTotal === 1 ? '' : 's'}` : ''}
                </div>
              )}
              {workflowHasFailed && workflow.error && <div className="mt-0.5 text-center text-[10px] text-destructive">{workflow.error}</div>}
            </div>
          )}
          <div className="flex-1" />
          <div className="flex items-center gap-0.5 rounded border border-border p-0.5">
            {(THEMES as readonly Theme[]).map((t) => {
              const meta = THEME_META[t]
              return (
                <button
                  key={t}
                  title={meta.label}
                  onClick={() => setTheme(t)}
                  className={`flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors ${
                    theme === t
                      ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                  }`}
                >
                  {meta.icon}
                  <span>{meta.label}</span>
                </button>
              )
            })}
          </div>
          <button
            className="text-muted-foreground hover:text-foreground transition-colors p-1.5 rounded hover:bg-accent"
            title="Account settings"
            onClick={() => setShowSettings(true)}
          >
            <SettingsIcon size={15} />
          </button>
          {settings?.has_credentials && (
            <button
              className="text-muted-foreground hover:text-foreground transition-colors p-1.5 rounded hover:bg-accent"
              title="Sign out"
              onClick={handleLogout}
            >
              <LogOut size={15} />
            </button>
          )}
        </header>
        <main className="flex-1 min-w-0 p-4">
          <VodManager activeTab={activeTab} setActiveTab={setActiveTab} dvrSubTab={dvrSubTab} setDvrSubTabPersisted={setDvrSubTabPersisted} />
        </main>
      </div>
    </div>
  )
}
