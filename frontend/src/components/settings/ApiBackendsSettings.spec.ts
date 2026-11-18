// @vitest-environment jsdom
import { createApp, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ApiBackendsSettings from './ApiBackendsSettings.vue'
import { apiConnections } from '@/lib/api-backends'

const mocks = vi.hoisted(() => ({ apiBackends: vi.fn(), saveApiBackend: vi.fn(), deleteApiBackend: vi.fn() }))
vi.mock('@/api/client', () => ({ api: mocks }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))
const first = { id: '00000000-0000-4000-8000-000000000001', name: 'First', base_url: 'https://one.test/v1', model: 'model-one', context_window: 32768, icon: 'code', has_api_key: true }
const second = { ...first, id: '00000000-0000-4000-8000-000000000002', name: 'Second', model: 'model-two', icon: 'star' }
let app: ReturnType<typeof createApp>
let host: HTMLDivElement
async function mount() {
  host = document.createElement('div')
  app = createApp(ApiBackendsSettings)
  const instance = app.mount(host)
  for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() }
  return (instance.$ as unknown as { setupState: {
    edit: (item: typeof first) => void
    draft: typeof first & { api_key: string }
    clearKey: boolean
    save: () => Promise<void>
    remove: (id: string) => Promise<void>
  } }).setupState
}
beforeEach(() => {
  vi.clearAllMocks()
  apiConnections.value = []
  mocks.apiBackends.mockResolvedValue([first, second])
  mocks.saveApiBackend.mockResolvedValue(first)
  mocks.deleteApiBackend.mockResolvedValue({ deleted: true })
})
afterEach(() => app?.unmount())

describe('global API backend settings', () => {
  it('lists independent connections and keeps the saved key when only the label/icon changes', async () => {
    const state = await mount()
    expect(host.textContent).toContain('First')
    expect(host.textContent).toContain('Second')
    state.edit(first)
    state.draft.name = 'Renamed'
    state.draft.icon = 'star'
    state.draft.context_window = 65536
    await state.save()
    expect(mocks.saveApiBackend).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      id: first.id, name: 'Renamed', icon: 'star', context_window: 65536, api_key: undefined,
    }))
  })
  it('replaces and explicitly clears credentials', async () => {
    const state = await mount()
    state.edit(first)
    state.draft.api_key = 'replacement'
    await state.save()
    expect(mocks.saveApiBackend).toHaveBeenLastCalledWith(expect.objectContaining({ api_key: 'replacement' }))
    state.edit(first)
    state.clearKey = true
    await state.save()
    expect(mocks.saveApiBackend).toHaveBeenLastCalledWith(expect.objectContaining({ api_key: '' }))
  })
  it('removes only the selected connection', async () => {
    const state = await mount()
    await state.remove(second.id)
    expect(mocks.deleteApiBackend).toHaveBeenCalledExactlyOnceWith(second.id)
  })
})
