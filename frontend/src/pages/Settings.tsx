import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, ArrowLeft, CheckCircle2, Clapperboard, KeyRound, Loader2, LogOut, Settings as SettingsIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import api from '@/lib/api'

interface Props {
  firstRun:       boolean
  fromEnv?:       boolean
  currentUrl?:    string
  hasCredentials: boolean
  onSaved:        () => void
  onBack?:        () => void
}

export default function Settings({ firstRun, fromEnv, currentUrl, hasCredentials, onSaved, onBack }: Props) {
  const queryClient = useQueryClient()
  const [url,   setUrl]   = useState(currentUrl ?? '')
  const [token, setToken] = useState('')
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)

  const [credUsername, setCredUsername] = useState('')
  const [credPassword, setCredPassword] = useState('')
  const [credConfirm,  setCredConfirm]  = useState('')
  const [credSaved,    setCredSaved]    = useState(false)
  const [credError,    setCredError]    = useState<string | null>(null)

  const testMutation = useMutation({
    mutationFn: () =>
      api.post('/settings/test/', { dispatcharr_url: url.trim(), dispatcharr_token: token.trim() })
        .then((r) => r.data),
    onSuccess: (data) => setTestResult(data),
    onError: () => setTestResult({ ok: false, message: 'Request failed — is VOD Manager running?' }),
  })

  const saveMutation = useMutation({
    mutationFn: () =>
      api.post('/settings/', { dispatcharr_url: url.trim(), dispatcharr_token: token.trim() })
        .then((r) => r.data),
    onSuccess: () => onSaved(),
    onError: (err: unknown) => {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setTestResult({ ok: false, message: msg ?? 'Save failed' })
    },
  })

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

  const disconnectMutation = useMutation({
    mutationFn: () => api.post('/settings/disconnect/').then((r) => r.data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['settings'] }); onSaved() },
  })

  const canTest = url.trim().length > 0 && token.trim().length > 0
  const canSave = canTest && testResult?.ok === true

  return (
    <div className="min-h-screen flex items-center justify-center p-6 bg-background">
      <div className="w-full max-w-md space-y-6">

        {/* Header */}
        <div className="text-center space-y-2">
          <div className="flex items-center justify-center gap-2">
            <Clapperboard size={28} className="text-primary" />
            <h1 className="text-2xl font-semibold">VOD Manager</h1>
          </div>
          {firstRun ? (
            <p className="text-sm text-muted-foreground">
              Connect to your Dispatcharr instance to get started.
            </p>
          ) : (
            <p className="text-sm text-muted-foreground flex items-center justify-center gap-1.5">
              <SettingsIcon size={13} /> Connection settings
            </p>
          )}
        </div>

        {/* Env var notice */}
        {fromEnv && (
          <div className="flex items-start gap-2 rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-sm text-yellow-400">
            <AlertCircle size={15} className="shrink-0 mt-0.5" />
            <span>
              Connection is configured via environment variables and cannot be changed here.
              {currentUrl && <><br /><span className="text-yellow-300/70 font-mono text-xs">{currentUrl}</span></>}
            </span>
          </div>
        )}

        {/* Connection form */}
        {!fromEnv && (
          <Card>
            <CardContent className="pt-6 space-y-4">

              <div className="space-y-1.5">
                <label className="text-sm font-medium">Dispatcharr URL</label>
                <Input
                  type="url"
                  placeholder="http://192.168.1.100:9191"
                  value={url}
                  onChange={(e) => { setUrl(e.target.value); setTestResult(null) }}
                  className="font-mono text-sm"
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-sm font-medium">API Token</label>
                <Input
                  type="password"
                  placeholder="Paste your Dispatcharr API token"
                  value={token}
                  onChange={(e) => { setToken(e.target.value); setTestResult(null) }}
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground">
                  Find this in Dispatcharr under{' '}
                  <span className="text-foreground font-medium">Settings → API Keys</span>.
                </p>
              </div>

              {testResult && (
                <div className={`flex items-center gap-2 text-sm rounded-md px-3 py-2 border ${
                  testResult.ok
                    ? 'text-green-400 bg-green-500/10 border-green-500/20'
                    : 'text-red-400 bg-red-500/10 border-red-500/20'
                }`}>
                  {testResult.ok
                    ? <CheckCircle2 size={14} className="shrink-0" />
                    : <AlertCircle size={14} className="shrink-0" />
                  }
                  {testResult.message}
                </div>
              )}

              <div className="flex items-center gap-2 pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!canTest || testMutation.isPending}
                  onClick={() => testMutation.mutate()}
                  className="gap-1.5"
                >
                  {testMutation.isPending
                    ? <><Loader2 size={13} className="animate-spin" /> Testing…</>
                    : 'Test Connection'
                  }
                </Button>

                <Button
                  size="sm"
                  disabled={!canSave || saveMutation.isPending}
                  onClick={() => saveMutation.mutate()}
                  className="gap-1.5 ml-auto"
                >
                  {saveMutation.isPending
                    ? <><Loader2 size={13} className="animate-spin" /> Saving…</>
                    : firstRun ? 'Connect' : 'Save'
                  }
                </Button>
              </div>

              {!testResult?.ok && canTest && (
                <p className="text-xs text-muted-foreground text-center">
                  Test the connection first, then save.
                </p>
              )}
            </CardContent>
          </Card>
        )}

        {/* Login credentials */}
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
                  : 'Protect VOD Manager with a username and password. Required before you can use the app.'}
              </p>
            </div>

            {!hasCredentials && (
              <div className="flex items-start gap-2 rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-400">
                <AlertCircle size={13} className="shrink-0 mt-0.5" />
                <span>No login credentials set. Set them below to enable authentication.</span>
              </div>
            )}

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

        {/* Back link */}
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

        {/* Disconnect */}
        {!firstRun && !fromEnv && (
          <div className="text-center pt-2 border-t border-border">
            <button
              className="text-xs text-muted-foreground hover:text-destructive transition-colors flex items-center gap-1.5 mx-auto disabled:opacity-50"
              disabled={disconnectMutation.isPending}
              onClick={() => disconnectMutation.mutate()}
            >
              {disconnectMutation.isPending
                ? <><Loader2 size={11} className="animate-spin" /> Disconnecting…</>
                : <><LogOut size={11} /> Disconnect from Dispatcharr</>
              }
            </button>
            <p className="text-[10px] text-muted-foreground mt-1">
              Clears the saved URL and token. You will be taken back to setup.
            </p>
          </div>
        )}

      </div>
    </div>
  )
}
