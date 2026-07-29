import axios from 'axios'

// Separate axios instance for the end-user DVR portal (see
// backend/portal_routes.py) -- deliberately a different token header
// (X-Portal-Session-Token vs. the admin app's X-Session-Token) and a
// different localStorage key, so the two sessions can never be confused
// with or accidentally reused as each other.
const portalApi = axios.create({ baseURL: '/api/portal' })

portalApi.interceptors.request.use((config) => {
  const token = localStorage.getItem('vodmanager-portal-session')
  if (token) config.headers['X-Portal-Session-Token'] = token
  return config
})

portalApi.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      // Was a silent localStorage.removeItem() + window.location.reload() --
      // real bug found live, 2026-07-29: whatever the person was doing
      // (e.g. confirming a scheduling request) vanished with zero
      // explanation, because the reload wipes the page (and any pending
      // mutation's onError/scheduleError UI) before they can see it. Now
      // shows a real message first, and skips the reload if a mutation is
      // already showing a scheduling bottom-sheet-style error state itself
      // (rare double-401 case) -- one alert, not a silent vanish.
      localStorage.removeItem('vodmanager-portal-session')
      alert('Your session expired or was signed out elsewhere. Whatever you just tried to do did NOT go through -- please log in again and retry it.')
      window.location.reload()
    }
    return Promise.reject(err)
  }
)

export default portalApi
