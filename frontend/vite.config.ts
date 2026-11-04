import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'
import { version as fallbackVersion } from './package.json'
import { sourceReleaseInfo } from '../desktop/scripts/release.cjs'

/**
 * The built SPA is written straight into the Python package (`voxarium_studio/static`), which is
 * where `voxarium_studio.main.static_dir()` looks first and what `pyproject.toml` ships in the
 * wheel — so `npm run build` is all that stands between a source checkout and a
 * single-process, single-port deployment (spec §2.5).
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const backend = env.VITE_BACKEND ?? 'http://127.0.0.1:8765'
  const version = sourceReleaseInfo(fileURLToPath(new URL('..', import.meta.url)), fallbackVersion).version

  return {
    define: { __STUDIO_VERSION__: JSON.stringify(version) },
    plugins: [vue(), tailwindcss(), {
      name: 'studio-version',
      generateBundle() {
        // §2.5: the running backend checks the actual built SPA before restarting.
        this.emitFile({ type: 'asset', fileName: 'studio-version.json', source: JSON.stringify({ version }) })
      },
    }],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      // The backend's --dev CORS allowlist is hardcoded to this port, so keep it fixed
      // rather than letting Vite pick the next free one.
      port: 5173,
      strictPort: true,
      // Proxying keeps dev same-origin, so the API client, the WebSocket and the
      // dynamic import of plugin-supplied JS all use identical URLs in dev and prod.
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/ws': { target: backend, ws: true, changeOrigin: true },
        '/healthz': { target: backend, changeOrigin: true },
      },
    },
    build: {
      outDir: '../backend/voxarium_studio/static',
      emptyOutDir: true,
    },
  }
})
