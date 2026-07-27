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
      localStorage.removeItem('vodmanager-portal-session')
      window.location.reload()
    }
    return Promise.reject(err)
  }
)

export default portalApi
