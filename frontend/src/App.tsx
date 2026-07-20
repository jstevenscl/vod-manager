import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Clapperboard, Loader2, LogOut, Moon, Palette, Settings as SettingsIcon, Sun } from 'lucide-react'
import VodManager from '@/pages/VodManager'
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

export default function App() {
  const [showSettings, setShowSettings] = useState(false)
  const [authState, setAuthState]       = useState<AuthState>('checking')
  const [theme, setThemeState]          = useState<Theme>(initTheme)
  const queryClient = useQueryClient()

  function setTheme(t: Theme) {
    document.documentElement.setAttribute('data-theme', t)
    localStorage.setItem('vodmanager-theme', t)
    setThemeState(t)
  }

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
  }, [isLoading, settings?.has_credentials, settings?.configured])

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

  if (!settings?.configured || showSettings) {
    return (
      <Settings
        firstRun={!settings?.configured}
        fromEnv={settings?.from_env}
        currentUrl={settings?.dispatcharr_url}
        hasCredentials={settings?.has_credentials ?? false}
        onSaved={handleSettingsSaved}
        onBack={settings?.configured ? () => setShowSettings(false) : undefined}
      />
    )
  }

  if (authState === 'login') {
    return <Login onLogin={handleLogin} />
  }

  return (
    <div className="min-h-screen p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Clapperboard size={20} className="text-primary" />
        <h1 className="text-xl font-semibold">VOD Manager</h1>
        <div className="ml-auto flex items-center gap-3">
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
            className="text-muted-foreground hover:text-foreground transition-colors p-1 rounded hover:bg-accent"
            title="Connection settings"
            onClick={() => setShowSettings(true)}
          >
            <SettingsIcon size={15} />
          </button>
          {settings?.has_credentials && (
            <button
              className="text-muted-foreground hover:text-foreground transition-colors p-1 rounded hover:bg-accent"
              title="Sign out"
              onClick={handleLogout}
            >
              <LogOut size={15} />
            </button>
          )}
        </div>
      </div>
      <VodManager />
    </div>
  )
}
