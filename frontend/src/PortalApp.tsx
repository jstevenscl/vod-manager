import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import PortalLogin from '@/pages/PortalLogin'
import Portal from '@/pages/Portal'
import portalApi from '@/lib/portalApi'

type AuthState = 'checking' | 'login' | 'ready'

export default function PortalApp() {
  const [authState, setAuthState] = useState<AuthState>('checking')

  useEffect(() => {
    // No per-portal-user theme switcher for v1 -- just pick a sane default,
    // same as the admin app's own initTheme default, rather than leaving it
    // to an unset prefers-color-scheme fallback.
    if (!document.documentElement.getAttribute('data-theme')) {
      document.documentElement.setAttribute('data-theme', 'dark')
    }
    const token = localStorage.getItem('vodmanager-portal-session')
    if (!token) { setAuthState('login'); return }
    portalApi.get('/auth/verify/')
      .then((r) => setAuthState(r.data.valid ? 'ready' : 'login'))
      .catch(() => setAuthState('login'))
  }, [])

  if (authState === 'checking') {
    return (
      <div className="flex items-center justify-center min-h-screen text-muted-foreground gap-2">
        <Loader2 size={16} className="animate-spin" />
        <span className="text-sm">Loading…</span>
      </div>
    )
  }

  if (authState === 'login') {
    return <PortalLogin onLogin={() => setAuthState('ready')} />
  }

  return <Portal onLogout={() => setAuthState('login')} />
}
