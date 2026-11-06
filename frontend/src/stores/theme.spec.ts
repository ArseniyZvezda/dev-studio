import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: { updateSettings: vi.fn(), updateApplicationSettings: vi.fn() },
}))
vi.mock('@/api/client', () => ({ api }))

import { DEFAULT_THEME } from '@/lib/appearance'
import { useThemeStore } from './theme'

let pinia: Pinia
beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  vi.resetAllMocks()
  vi.stubGlobal('document', {
    documentElement: { dataset: {}, style: { setProperty: vi.fn(), removeProperty: vi.fn() } },
    head: { querySelector: () => ({}) },
  })
  vi.stubGlobal('window', {
    matchMedia: () => ({ matches: false, addEventListener: vi.fn() }),
  })
})
afterEach(() => { disposePinia(pinia); vi.unstubAllGlobals() })

describe('theme persistence scope', () => {
  it('saves appearance globally and drops any legacy project accent in the patch', async () => {
    const theme = useThemeStore()
    theme.hydrate({ ...DEFAULT_THEME, accent: '#ff0000' })
    api.updateApplicationSettings.mockResolvedValue({
      theme: { ...DEFAULT_THEME, mode: 'light', accent: '#ff0000' },
    })
    const legacyPatch = { mode: 'light' as const, accent: '#00ff00' }
    await theme.update(legacyPatch)
    expect(api.updateApplicationSettings).toHaveBeenCalledExactlyOnceWith({ theme: { mode: 'light' } })
    expect(api.updateSettings).not.toHaveBeenCalled()
    expect(theme.theme).toMatchObject({ mode: 'light', accent: '#ff0000' })
  })

  it('saves only the accent to the project and keeps the shared appearance', async () => {
    const theme = useThemeStore()
    theme.hydrate({ ...DEFAULT_THEME, mode: 'light' })
    api.updateSettings.mockResolvedValue({
      theme: { ...DEFAULT_THEME, mode: 'light', accent: '#00ff00' },
    })
    await theme.updateAccent('#00ff00')
    expect(api.updateSettings).toHaveBeenCalledExactlyOnceWith({ theme: { accent: '#00ff00' } })
    expect(api.updateApplicationSettings).not.toHaveBeenCalled()
    expect(theme.theme).toMatchObject({ mode: 'light', accent: '#00ff00' })
  })

  it.each(['application', 'project'] as const)('rolls back an unsuccessful %s save', async (scope) => {
    const theme = useThemeStore()
    theme.hydrate(DEFAULT_THEME)
    api.updateApplicationSettings.mockRejectedValue(new Error('cannot save'))
    api.updateSettings.mockRejectedValue(new Error('cannot save'))
    const saving = scope === 'application' ? theme.update({ mode: 'light' }) : theme.updateAccent('#ff0000')
    await expect(saving).rejects.toThrow('cannot save')
    expect(theme.theme).toEqual(DEFAULT_THEME)
    expect(theme.saving).toBe(false)
  })
})
