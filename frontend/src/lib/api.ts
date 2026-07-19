import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('vodmanager-session')
  if (token) config.headers['X-Session-Token'] = token
  return config
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('vodmanager-session')
      window.location.reload()
    }
    return Promise.reject(err)
  }
)

export default api
