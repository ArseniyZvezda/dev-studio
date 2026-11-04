import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

/**
 * Kept apart from `vite.config.ts` so the build config stays about the build.
 *
 * `environment: 'node'` is deliberate and is what the tests are shaped around: the Markdown
 * layer and the block model are plain data, and a suite that needs a DOM to ask "what does
 * this document serialise to" has the seam in the wrong place. The Vue plugin is here only
 * so the editor's *schema* can be imported — extensions reference node-view components, and
 * building a schema must not mean building a second, fake one.
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.spec.ts'],
  },
})
