import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Clapperboard, Loader2, LogOut, Settings as SettingsIcon } from 'lucide-react'
import VodManager from '@/pages/VodManager'
import Login from '@/pages/Login'
import Settings from '@/pages/Settings'
import api from '@/lib/api'

type AuthState = 'checking' | 'login' | 'ready'

export default function App() {
  const [showSettings, setShowSettings] = useState(false)
  const [authState, setAuthState]       = useState<AuthState>('checking')
  const queryClient = useQueryClient()

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
