/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev-only proxy: the FastAPI app (nvplan-serve) runs on :8000 with no CORS
// headers of its own (see nvplan/api/app.py — not a file this build owns, so
// no CORSMiddleware is added there). Proxying the exact route prefixes nvplan
// registers means the browser never makes a cross-origin request at all, and
// nothing here depends on the backend ever growing CORS support. Listed
// verbatim from nvplan/api/app.py's `_register`.
const BACKEND_PREFIXES = [
  '/health',
  '/ingest',
  '/plan',
  '/statements',
  '/trace',
  '/ai',
  '/assistant',
  '/backtest',
  '/deviation',
  '/brain',
  '/bridge',
]

// Overridable so a developer running `nvplan-serve --port <other>` (e.g. to keep a second
// instance out of the way of one already on :8000) doesn't have to edit this file.
const BACKEND_PORT = process.env.NVPLAN_BACKEND_PORT ?? '8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      BACKEND_PREFIXES.map((prefix) => [
        prefix,
        { target: `http://127.0.0.1:${BACKEND_PORT}`, changeOrigin: true },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
