// @vitest-environment jsdom
import { createApp, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ApiBackendsSettings from './ApiBackendsSettings.vue'
import { apiConnections } from '@/lib/api-backends'

const mocks = vi.hoisted(() => ({ apiBackends: vi.fn(), saveApiBackend: vi.fn(), deleteApiBackend: vi.fn() }))
vi.mock('@/api/client', () => ({ api: mocks }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))
const first = { id: '00000000-0000-4000-8000-000000000001', name: 'First', base_url: 'https://one.test/v1', model: 'model-one', models: ['model-one', 'model-extra'], api_mode: 'auto' as const, context_window: 32768, icon: 'code', has_api_key: true, use_limits: false, tpm: 1_000_000 }
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
    const useLimits = [...host.querySelectorAll<HTMLInputElement>('input[type=checkbox]')].at(-1)!
    useLimits.checked = true
    useLimits.dispatchEvent(new Event('change'))
    await nextTick()
    const tpm = host.querySelector<HTMLInputElement>('input[type=number][min="1"]')!
    expect(tpm.value).toBe('1000000')
    tpm.value = '50000'
    tpm.dispatchEvent(new Event('input'))
    await nextTick()
    expect(apiConnections.value[0].models).toEqual(['model-one', 'model-extra'])
    await state.save()
    expect(mocks.saveApiBackend).toHaveBeenCalledWith(expect.objectContaining({
      models: ['model-new', 'model-extra'], api_mode: 'responses', use_limits: true, tpm: 50000, api_key: undefined,
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
  it('still saves the key after TPM is emptied and limits are disabled', async () => {
    const state = await mount()
    state.edit({ ...first, use_limits: true, tpm: 23000 })
    state.draft.api_key = 'new-test-key'
    await nextTick()
    const tpm = host.querySelector<HTMLInputElement>('input[type=number][min="1"]')!
    tpm.value = ''
    tpm.dispatchEvent(new Event('input'))
    const useLimits = [...host.querySelectorAll<HTMLInputElement>('input[type=checkbox]')].at(-1)!
    useLimits.checked = false
    useLimits.dispatchEvent(new Event('change'))
    await state.save()
    expect(mocks.saveApiBackend).toHaveBeenCalledWith(expect.objectContaining({
      api_key: 'new-test-key', use_limits: false, tpm: undefined,
    }))
  })
  it('reopens the saved settings even if a stale catalogue read finishes and the refresh fails', async () => {
    const previous = { ...first, has_api_key: false }
    const saved = { ...first, use_limits: true, tpm: 23000 }
    let finishOldRead!: (items: (typeof first)[]) => void
    mocks.apiBackends.mockImplementationOnce(() => new Promise(resolve => { finishOldRead = resolve }))
      .mockRejectedValueOnce(new Error('refresh unavailable'))
    mocks.saveApiBackend.mockResolvedValueOnce(saved)
    const state = await mount()
    apiConnections.value = [previous, second]
    state.edit(previous)
    state.draft.api_key = 'new-test-key'
    state.draft.use_limits = true
    state.draft.tpm = 23000

    const saving = state.save()
    await nextTick()
    finishOldRead([previous, second])
    await saving
    for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() }

    expect(apiConnections.value).toEqual([saved, second])
    const edit = [...host.querySelectorAll('button')].find(button => button.textContent === 'common.edit')!
    edit.click()
    await nextTick()
    expect(host.textContent).toContain('settings.apiBackends.keySaved')
    expect(host.querySelector<HTMLInputElement>('input[type=password]')!.value).toBe('')
    expect(host.querySelector<HTMLInputElement>('input[type=number][min="1"]')!.value).toBe('23000')
  })
  it('removes only the selected connection', async () => {
    const state = await mount()
    await state.remove(second.id)
    expect(mocks.deleteApiBackend).toHaveBeenCalledExactlyOnceWith(second.id)
  })
})
