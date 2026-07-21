import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { AlertCircle, ArrowLeft, CheckCircle2, Clapperboard, KeyRound, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import api from '@/lib/api'

interface Props {
  firstRun:       boolean
  hasCredentials: boolean
  onSaved:        () => void
  onBack?:        () => void
  onSkip?:        () => void
}

export default function Settings({ firstRun, hasCredentials, onSaved, onBack, onSkip }: Props) {
  const [credUsername, setCredUsername] = useState('')
  const [credPassword, setCredPassword] = useState('')
  const [credConfirm,  setCredConfirm]  = useState('')
  const [credSaved,    setCredSaved]    = useState(false)
  const [credError,    setCredError]    = useState<string | null>(null)

  const credMutation = useMutation({
    mutationFn: () =>
      api.post('/settings/credentials/', { username: credUsername.trim(), password: credPassword })
        .then((r) => r.data),
    onSuccess: () => {
      setCredSaved(true)
      setCredPassword('')
      setCredConfirm('')
      setTimeout(() => setCredSaved(false), 4000)
      onSaved()
    },
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setCredError(msg ?? 'Failed to save credentials')
    },
  })

  function handleCredSave() {
    setCredError(null)
    if (!credUsername.trim()) { setCredError('Username is required.'); return }
    if (credPassword.length < 6) { setCredError('Password must be at least 6 characters.'); return }
    if (credPassword !== credConfirm) { setCredError('Passwords do not match.'); return }
    credMutation.mutate()
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6 bg-background">
      <div className="w-full max-w-md space-y-6">

        {/* Header */}
        <div className="text-center space-y-2">
          <div className="flex items-center justify-center gap-2">
            <Clapperboard size={28} className="text-primary" />
            <h1 className="text-2xl font-semibold">VOD Manager</h1>
          </div>
          <p className="text-sm text-muted-foreground">
            {firstRun ? 'Set up an admin login to get started.' : 'Account settings'}
          </p>
        </div>

        <Card>
          <CardContent className="pt-6 space-y-4">
            <div>
              <h2 className="text-sm font-semibold flex items-center gap-1.5">
                <KeyRound size={13} className="text-primary" />
                {hasCredentials ? 'Change Login Credentials' : 'Set Up Login'}
              </h2>
              <p className="text-xs text-muted-foreground mt-0.5">
                {hasCredentials
                  ? 'Update the username and password used to sign in.'
                  : 'Protect VOD Manager with a username and password, or skip to run without a login.'}
              </p>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">Username</label>
              <Input
                autoComplete="username"
                value={credUsername}
                onChange={(e) => { setCredUsername(e.target.value); setCredError(null) }}
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">{hasCredentials ? 'New password' : 'Password'}</label>
              <Input
                type="password"
                autoComplete="new-password"
                value={credPassword}
                onChange={(e) => { setCredPassword(e.target.value); setCredError(null) }}
              />
              <p className="text-[10px] text-muted-foreground">Minimum 6 characters</p>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">Confirm password</label>
              <Input
                type="password"
                autoComplete="new-password"
                value={credConfirm}
                onChange={(e) => { setCredConfirm(e.target.value); setCredError(null) }}
                onKeyDown={(e) => e.key === 'Enter' && handleCredSave()}
              />
            </div>

            {credError && (
              <div className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
                <AlertCircle size={14} className="shrink-0" /> {credError}
              </div>
            )}

            <div className="flex items-center gap-2">
              <Button
                size="sm"
                disabled={credMutation.isPending || !credUsername.trim() || !credPassword || !credConfirm}
                onClick={handleCredSave}
                className="gap-1.5"
              >
                {credMutation.isPending
                  ? <><Loader2 size={13} className="animate-spin" /> Saving…</>
                  : hasCredentials ? 'Update Credentials' : 'Set Credentials'
                }
              </Button>
              {credSaved && (
                <span className="text-xs text-green-400 flex items-center gap-1">
                  <CheckCircle2 size={12} /> Saved
                </span>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Back / Skip */}
        {onBack && (
          <div className="text-center">
            <button
              className="text-sm text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1.5 mx-auto"
              onClick={onBack}
            >
              <ArrowLeft size={13} /> Back
            </button>
          </div>
        )}
        {onSkip && (
          <div className="space-y-2">
            <div className="flex items-start gap-2 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
              <AlertCircle size={14} className="shrink-0 mt-0.5" />
              <span>
                Skipping means VOD Manager runs with <strong>no login at all</strong> — anyone who can reach this
                app (e.g. if the port is exposed to the internet, not just your local network) has full access:
                your streaming credentials, provider logins, API keys, and the database backup download. Only skip
                if this instance is only reachable from a network you trust.
              </span>
            </div>
            <div className="text-center">
              <button
                className="text-sm text-muted-foreground hover:text-foreground transition-colors mx-auto"
                onClick={() => {
                  if (confirm('Run VOD Manager with no login? Anyone who can reach this app will have full access. Only continue if you\'re sure this instance is not exposed to the internet.')) {
                    onSkip()
                  }
                }}
              >
                Skip for now — run without a login
              </button>
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
