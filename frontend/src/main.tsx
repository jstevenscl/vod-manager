import { Component, StrictMode, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import PortalApp from './PortalApp'
import './index.css'

class AppErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('VOD Manager UI error', error, info)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <main className="min-h-screen bg-background text-foreground p-8">
        <section className="max-w-2xl rounded border border-destructive/50 bg-card p-5 space-y-3">
          <h1 className="text-lg font-semibold">The page hit a UI error</h1>
          <p className="text-sm text-muted-foreground">Your data and background jobs are still on the server. Refresh the page and try the action again.</p>
          <pre className="text-xs whitespace-pre-wrap text-destructive">{this.state.error.message}</pre>
          <button className="rounded border px-3 py-1.5 text-sm" onClick={() => window.location.reload()}>Reload</button>
        </section>
      </main>
    )
  }
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1 } } })

// /portal (and anything under it) is the end-user self-service DVR portal --
// a separate small app/login from the admin App below, sharing this same
// build/deploy. No router involved: this is the only path-based branch in
// the whole frontend, matching the rest of the app's existing convention of
// plain useState-driven tabs rather than a router library.
const isPortal = window.location.pathname === '/portal' || window.location.pathname.startsWith('/portal/')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AppErrorBoundary>
        {isPortal ? <PortalApp /> : <App />}
      </AppErrorBoundary>
    </QueryClientProvider>
  </StrictMode>
)
