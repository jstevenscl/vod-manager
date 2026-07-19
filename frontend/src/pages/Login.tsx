import { useState } from 'react'
import { AlertCircle, Clapperboard, Loader2, Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import api from '@/lib/api'

interface Props {
  onLogin: () => void
}

export default function Login({ onLogin }: Props) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error,    setError]    = useState<string | null>(null)
  const [loading,  setLoading]  = useState(false)

  async function handleLogin() {
    if (!username.trim() || !password) return
    setLoading(true)
    setError(null)
    try {
      const { data } = await api.post('/auth/login/', { username: username.trim(), password })
      localStorage.setItem('vodmanager-session', data.token)
      onLogin()
    } catch {
      setError('Invalid username or password.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6 bg-background">
      <div className="w-full max-w-sm space-y-6">

        <div className="text-center space-y-2">
          <div className="flex items-center justify-center gap-2">
            <Clapperboard size={26} className="text-primary" />
            <h1 className="text-2xl font-semibold">VOD Manager</h1>
          </div>
          <p className="text-sm text-muted-foreground flex items-center justify-center gap-1.5">
            <Lock size={12} /> Sign in to continue
          </p>
        </div>

        <Card>
          <CardContent className="pt-6 space-y-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Username</label>
              <Input
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(e) => { setUsername(e.target.value); setError(null) }}
                onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">Password</label>
              <Input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => { setPassword(e.target.value); setError(null) }}
                onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
              />
            </div>

            {error && (
              <div className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
                <AlertCircle size={14} className="shrink-0" /> {error}
              </div>
            )}

            <Button
              className="w-full gap-2"
              disabled={!username.trim() || !password || loading}
              onClick={handleLogin}
            >
              {loading
                ? <><Loader2 size={14} className="animate-spin" /> Signing in…</>
                : 'Sign In'
              }
            </Button>
          </CardContent>
        </Card>

      </div>
    </div>
  )
}
