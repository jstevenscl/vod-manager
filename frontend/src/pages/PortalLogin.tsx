import { useState } from 'react'
import { AlertCircle, Loader2, Lock, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import portalApi from '@/lib/portalApi'

interface Props {
  onLogin: () => void
}

type Step = 'password' | 'enroll' | 'code'

export default function PortalLogin({ onLogin }: Props) {
  const [step, setStep] = useState<Step>('password')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [pendingToken, setPendingToken] = useState('')
  const [secret, setSecret] = useState('')
  const [otpauthUri, setOtpauthUri] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function handlePasswordSubmit() {
    if (!username.trim() || !password) return
    setLoading(true)
    setError(null)
    try {
      const { data } = await portalApi.post('/auth/login/', { username: username.trim(), password })
      setPendingToken(data.pending_token)
      if (data.enrollment_required) {
        const enroll = await portalApi.post('/auth/enroll-mfa/', { pending_token: data.pending_token })
        setSecret(enroll.data.secret)
        setOtpauthUri(enroll.data.otpauth_uri)
        setStep('enroll')
      } else {
        setStep('code')
      }
    } catch {
      setError('Invalid username or password.')
    } finally {
      setLoading(false)
    }
  }

  async function handleCodeSubmit() {
    if (code.length < 6) return
    setLoading(true)
    setError(null)
    try {
      const path = step === 'enroll' ? '/auth/confirm-mfa/' : '/auth/verify-mfa/'
      const { data } = await portalApi.post(path, { pending_token: pendingToken, code: code.trim() })
      localStorage.setItem('vodmanager-portal-session', data.token)
      onLogin()
    } catch {
      setError('Invalid or expired code.')
      setCode('')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6 bg-background">
      <div className="w-full max-w-sm space-y-6">

        <div className="flex items-center justify-center gap-3">
          <img src="/favicon.svg" width={84} height={84} alt="" className="rounded-lg" />
          <div className="text-left">
            <h1 className="text-2xl font-semibold">My Recordings</h1>
            <p className="text-sm text-muted-foreground flex items-center gap-1.5">
              <Lock size={12} /> {step === 'password' ? 'Sign in to continue' : 'Two-factor verification'}
            </p>
          </div>
        </div>

        <Card>
          <CardContent className="pt-6 space-y-4">
            {step === 'password' && (
              <>
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Username</label>
                  <Input
                    autoFocus
                    autoComplete="username"
                    value={username}
                    onChange={(e) => { setUsername(e.target.value); setError(null) }}
                    onKeyDown={(e) => e.key === 'Enter' && handlePasswordSubmit()}
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Password</label>
                  <Input
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => { setPassword(e.target.value); setError(null) }}
                    onKeyDown={(e) => e.key === 'Enter' && handlePasswordSubmit()}
                  />
                </div>
                {error && (
                  <div className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
                    <AlertCircle size={14} className="shrink-0" /> {error}
                  </div>
                )}
                <Button className="w-full gap-2" disabled={!username.trim() || !password || loading} onClick={handlePasswordSubmit}>
                  {loading ? <><Loader2 size={14} className="animate-spin" /> Signing in…</> : 'Sign In'}
                </Button>
              </>
            )}

            {step === 'enroll' && (
              <>
                <div className="flex items-center gap-2 text-sm font-medium">
                  <ShieldCheck size={15} className="text-primary" /> Set up two-factor authentication
                </div>
                <p className="text-sm text-muted-foreground">
                  This account needs an authenticator app (Google Authenticator, Authy, 1Password, etc.) before you can
                  sign in. Add a new account there using this key, then enter the 6-digit code it shows.
                </p>
                <div className="rounded-md border border-border bg-secondary/50 px-3 py-2 space-y-1">
                  <div className="text-[11px] text-muted-foreground">Secret key (enter manually)</div>
                  <div className="text-sm font-mono tracking-wider break-all">{secret}</div>
                </div>
                <details className="text-[11px] text-muted-foreground">
                  <summary className="cursor-pointer">Show setup URI</summary>
                  <div className="mt-1 break-all font-mono">{otpauthUri}</div>
                </details>
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">6-digit code</label>
                  <Input
                    autoFocus
                    inputMode="numeric"
                    maxLength={6}
                    value={code}
                    onChange={(e) => { setCode(e.target.value.replace(/\D/g, '')); setError(null) }}
                    onKeyDown={(e) => e.key === 'Enter' && handleCodeSubmit()}
                  />
                </div>
                {error && (
                  <div className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
                    <AlertCircle size={14} className="shrink-0" /> {error}
                  </div>
                )}
                <Button className="w-full gap-2" disabled={code.length < 6 || loading} onClick={handleCodeSubmit}>
                  {loading ? <><Loader2 size={14} className="animate-spin" /> Verifying…</> : 'Confirm & Sign In'}
                </Button>
              </>
            )}

            {step === 'code' && (
              <>
                <div className="flex items-center gap-2 text-sm font-medium">
                  <ShieldCheck size={15} className="text-primary" /> Enter your authenticator code
                </div>
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">6-digit code</label>
                  <Input
                    autoFocus
                    inputMode="numeric"
                    maxLength={6}
                    value={code}
                    onChange={(e) => { setCode(e.target.value.replace(/\D/g, '')); setError(null) }}
                    onKeyDown={(e) => e.key === 'Enter' && handleCodeSubmit()}
                  />
                </div>
                {error && (
                  <div className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
                    <AlertCircle size={14} className="shrink-0" /> {error}
                  </div>
                )}
                <Button className="w-full gap-2" disabled={code.length < 6 || loading} onClick={handleCodeSubmit}>
                  {loading ? <><Loader2 size={14} className="animate-spin" /> Verifying…</> : 'Sign In'}
                </Button>
              </>
            )}
          </CardContent>
        </Card>

      </div>
    </div>
  )
}
