import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import PortalApp from './PortalApp'
import './index.css'

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
      {isPortal ? <PortalApp /> : <App />}
    </QueryClientProvider>
  </StrictMode>
)
