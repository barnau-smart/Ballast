import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import basicSsl from '@vitejs/plugin-basic-ssl'

// Real-Schwab link mode (opt-in via `--mode httpslink`, used by scripts/dev.sh
// when BALLAST_REAL_BROKER=1): serve the SPA over https on 127.0.0.1:443 so it
// lives at the EXACT callback URL Schwab already has registered
// (`https://127.0.0.1/callback`). That lets the real OAuth redirect land back IN
// the app — no URL change at Schwab, no code-paste helper. `/api` is proxied to
// the backend server-side, so the https page never makes a blocked mixed-content
// call to http and no CORS is involved. The API base is pinned to the same
// https origin here via `define` (not a .env file — the repo's .gitignore blocks
// committing .env.*), so the client calls https://127.0.0.1/api/... same-origin.
//
// Normal mode (plain `vite`) is unchanged: http on :5173 for offline/fake dev
// and the test runner.
//
// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const httpsMode = mode === 'httpslink'
  return {
    // Give the real-Schwab https mode its OWN dep-cache dir. That mode runs under
    // sudo (to bind :443), so Vite writes its cache as ROOT; sharing the default
    // `node_modules/.vite` with the normal (non-sudo) run then makes the next
    // plain `vite` crash with `EACCES: permission denied, unlink .../.vite/deps/...`
    // (bit us repeatedly 2026-08-14). Separate cache dirs means the root-owned
    // cache never collides with the user-owned one — no cross-mode permission clash.
    cacheDir: httpsMode ? 'node_modules/.vite-httpslink' : 'node_modules/.vite',
    plugins: [react(), ...(httpsMode ? [basicSsl()] : [])],
    // In real-link mode, force the client's API base to the same https origin so
    // apiFetch hits https://127.0.0.1/api/... (proxied to the backend) — never
    // the http://localhost:8000 default, which an https page would block as mixed
    // content. Overrides src/lib/session.js's import.meta.env read at build time.
    ...(httpsMode
      ? {
          define: {
            'import.meta.env.VITE_API_BASE_URL': JSON.stringify('https://127.0.0.1'),
          },
        }
      : {}),
    server: httpsMode
      ? {
          host: '127.0.0.1',
          // Port 443 = the default https port, i.e. what "no port" in
          // https://127.0.0.1/callback resolves to. Binding it needs sudo.
          port: 443,
          strictPort: true,
          proxy: {
            // Forward all backend calls to the http backend server-side. The
            // browser only ever talks https to Vite (same origin) → no mixed
            // content, no CORS preflight on the app's own API calls.
            '/api': { target: 'http://localhost:8000', changeOrigin: true },
          },
        }
      : {
          port: 5173,
        },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.js'],
      css: true,
    },
  }
})
