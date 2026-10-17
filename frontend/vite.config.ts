import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // In dev, proxy to uvicorn on :8001. In production the build is served
  // same-origin behind nginx, so /api just works.
  server: { proxy: { '/api': { target: 'http://localhost:8001', changeOrigin: true } } },
})
