// @vitest-environment jsdom
import { createApp, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ApiBackendsSettings from './ApiBackendsSettings.vue'
import { apiConnections } from '@/lib/api-backends'

const mocks = vi.hoisted(() => ({ apiBackends: vi.fn(), saveApiBackend: vi.fn(), deleteApiBackend: vi.fn() }))
vi.mock('@/api/client', () => ({ api: mocks }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))
const first = { id: '00000000-0000-4000-8000-000000000001', name: 'First', base_url: 'https://one.test/v1', model: 'model-one', models: ['model-one', 'model-extra'], api_mode: 'auto' as const, context_window: 32768, icon: 'code', has_api_key: true }
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
    modelIds: string
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
  it('edits the protocol and a model list without changing the saved catalogue before Save', async () => {
    const state = await mount()
    state.edit(first)
    expect(state.modelIds).toBe('model-one\nmodel-extra')
    await nextTick()
    const textarea = host.querySelector('textarea')!
    textarea.value = ' model-new \n\nmodel-extra\r\nmodel-new'
    textarea.dispatchEvent(new Event('input'))
    await nextTick()
    const select = host.querySelector('select')!
    select.value = 'responses'
    select.dispatchEvent(new Event('change'))
    await nextTick()
    expect(apiConnections.value[0].models).toEqual(['model-one', 'model-extra'])
    await state.save()
    expect(mocks.saveApiBackend).toHaveBeenCalledWith(expect.objectContaining({
      models: ['model-new', 'model-extra'], api_mode: 'responses', api_key: undefined,
    }))
  })
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
