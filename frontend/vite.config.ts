import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Proxy every backend route straight through to backend/main.py's uvicorn
// dev server so the browser only ever talks to one origin — no CORS setup
// needed, and cookies (the auth session) work exactly like they will in
// production behind a single reverse proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/auth': 'http://localhost:8000',
      '/profile': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
      '/chat': 'http://localhost:8000',
    },
  },
})
