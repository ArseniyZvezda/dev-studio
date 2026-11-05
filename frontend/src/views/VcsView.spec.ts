import { renderToString } from '@vue/server-renderer'
import { createPinia } from 'pinia'
import { createSSRApp } from 'vue'
import { describe, expect, it } from 'vitest'

import { useVcsStore } from '@/stores/vcs'
import VcsView from './VcsView.vue'

describe('VCS settings while workspace status is loading', () => {
  it('renders saved settings immediately, including for an unavailable CLI', async () => {
    const pinia = createPinia()
    const store = useVcsStore(pinia)
    // load() publishes the overview before its potentially slow status request ends.
    // These fields must already contain their values when the Save button appears.
    store.loading = true
    store.overview = {
      active: null,
      configured: 'perforce',
      branch: null,
      revision: null,
      backends: [{
        id: 'perforce',
        name: 'Perforce',
        root: 'D:/game',
        available: false,
        reason: 'p4 is not on PATH',
        supports_isolation: false,
        detected: false,
        active: false,
        executable: 'C:/Perforce/p4.exe',
        program: 'p4',
        settings: {
          executable: 'C:/Perforce/p4.exe',
          port: 'ssl:depot.example.com:1666',
          user: 'artist',
          client: 'artist-game',
          charset: 'utf8',
        },
        settings_fields: ['executable', 'port', 'user', 'client', 'charset'].map((key) => ({
          key,
          type: key === 'executable' ? 'path' : 'string',
        })),
      }],
    }

    const html = await renderToString(createSSRApp(VcsView).use(pinia))
    for (const [key, value] of Object.entries(store.overview.backends[0]!.settings)) {
      expect(html).toMatch(new RegExp(`id="vcs-perforce-${key}"[^>]*value="${value}"`))
    }
    expect(html).toContain('type="submit"')
    expect(html).toContain('p4 is not on PATH')
  })
})
