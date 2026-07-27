import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CalendarDays, Film, HardDriveDownload, LayoutGrid, Loader2, LogOut, Moon,
  Palette, Search, Settings as SettingsIcon, Sun, Tv, Users, Wrench,
} from 'lucide-react'
import VodManager, { type DvrSubTab, type VodManagerTab } from '@/pages/VodManager'
import Login from '@/pages/Login'
import Settings from '@/pages/Settings'
import api from '@/lib/api'

export const THEMES = ['dark', 'mid', 'light', 'mono'] as const
export type Theme = typeof THEMES[number]

const THEME_META: Record<Theme, { label: string; icon: React.ReactNode }> = {
  dark:  { label: 'Dark',  icon: <Moon size={11} /> },
  mid:   { label: 'Mid',   icon: <Palette size={11} /> },
  light: { label: 'Light', icon: <Sun size={11} /> },
  mono:  { label: 'Mono',  icon: <span className="text-[10px] font-bold leading-none">M</span> },
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
    return saved === 'movies' || saved === 'series' || saved === 'curation' || saved === 'config' || saved === 'dvr' ? saved : 'movies'
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
            <div className="text-sm font-bold tracking-tight leading-tight">VOD Manager</div>
            {versionQuery.data && (
              <div className="text-[10px] text-muted-foreground font-mono truncate" title={`ref: ${versionQuery.data.ref}`}>
                v{versionQuery.data.version} · {versionQuery.data.commit}
              </div>
            )}
          </div>
        </div>
        <nav className="flex-1 space-y-3.5">
          {NAV_GROUPS.map((group) => (
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
        </nav>
      </aside>

      <div className="flex flex-col min-w-0">
        <header className="sticky top-0 z-10 flex items-center gap-3.5 px-5 py-2.5 border-b border-border bg-card">
          <div className="flex-1 max-w-[380px] flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground/70">
            <Search size={13} className="flex-shrink-0" />
            Search coming soon…
          </div>
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
