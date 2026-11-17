// @vitest-environment jsdom
import { createApp, nextTick, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AppConfig, MemberOptions, OrchestratorModels } from '@/api/types'
import OrchestratorSettings from './OrchestratorSettings.vue'

const mocks = vi.hoisted(() => ({ memberOptions: vi.fn(), updateApplicationSettings: vi.fn(), updateSettings: vi.fn() }))
vi.mock('@/api/client', () => ({ api: mocks }))
const session = reactive({ project: { config: {} as AppConfig } })
vi.mock('@/stores/session', () => ({ useSessionStore: () => session }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))

const mapping: OrchestratorModels = {
  codex: { '1': 'terra', '2': 'sol', '3': 'astra' },
  custom_backend: { '1': 'small', '2': 'large', '3': 'largest' },
}
const options: MemberOptions = {
  agent_backends: { codex: { available: true, reason: '' }, custom_backend: { available: false, reason: 'Not installed' } },
  suggested_models: [
    { id: 'sol', label: 'Sol', backend: 'codex', group: 'Models', note: '' },
    { id: 'large', label: 'Large', backend: 'custom_backend', group: 'Models', note: '' },
  ], permission_modes: [], effort_levels: [], common_tools: [], studio_tool_prefix: '',
}
interface State {
  draft: OrchestratorModels
  dirty: boolean
  valid: boolean
  saving: boolean
  choose: (backend: string, strength: '1' | '2' | '3', value: string) => void
  save: () => Promise<void>
}
let app: ReturnType<typeof createApp>
let host: HTMLDivElement
async function mount() {
  host = document.createElement('div')
  app = createApp(OrchestratorSettings)
  const instance = app.mount(host)
  await Promise.resolve()
  await nextTick()
  const state = (instance.$ as unknown as { setupState: State }).setupState
  return state
}
beforeEach(() => {
  vi.clearAllMocks()
  session.project.config = { orchestrator_models: JSON.parse(JSON.stringify(mapping)) } as AppConfig
  mocks.memberOptions.mockResolvedValue(options)
  mocks.updateApplicationSettings.mockImplementation(async (patch: Partial<AppConfig>) => ({ ...session.project.config, ...patch }))
})
afterEach(() => { app?.unmount() })

describe('global Orchestrator model mapping', () => {
  it('renders three strength rows and all registered backend columns using their model catalogs', async () => {
    const state = await mount()
    expect(host.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(host.querySelectorAll('tbody td')).toHaveLength(6)
    expect([...host.querySelectorAll('thead th')].map(cell => cell.textContent)).toEqual([
      'settings.orchestrator.strength', 'codex', 'custom_backend',
    ])
    const secondRow = host.querySelectorAll('tbody tr')[1]!
    expect(secondRow.children[1]!.textContent).toContain('Sol')
    expect(secondRow.children[1]!.textContent).not.toContain('Large')
    expect(secondRow.children[2]!.textContent).toContain('Large')
    expect(host.querySelector('option[value="auto"]')).toBeNull()
    expect(state.valid).toBe(true)
    expect(state.dirty).toBe(false)
    expect(mocks.updateApplicationSettings).not.toHaveBeenCalled()
  })

  it('saves only through application settings and preserves the other cells', async () => {
    const state = await mount()
    state.choose('codex', '2', 'future-sol')
    expect(session.project.config.orchestrator_models?.codex?.['2']).toBe('sol')
    await state.save()
    expect(mocks.updateApplicationSettings).toHaveBeenCalledExactlyOnceWith({
      orchestrator_models: { ...mapping, codex: { ...mapping.codex, '2': 'future-sol' } },
    })
    expect(mocks.updateSettings).not.toHaveBeenCalled()
    expect(session.project.config.orchestrator_models?.codex?.['2']).toBe('future-sol')
    expect(state.dirty).toBe(false)
  })

  it.each(['auto', '', '   '])('does not save an invalid or recursive mapping %s', async value => {
    const state = await mount()
    state.choose('codex', '1', value)
    expect(state.valid).toBe(false)
    await state.save()
    expect(mocks.updateApplicationSettings).not.toHaveBeenCalled()
  })

  it('keeps failed edits and shows the backend error without changing saved settings', async () => {
    const state = await mount()
    mocks.updateApplicationSettings.mockRejectedValueOnce(new Error('Save failed'))
    state.choose('codex', '3', 'next-astra')
    await state.save()
    await nextTick()
    expect(state.dirty).toBe(true)
    expect(state.saving).toBe(false)
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Save failed')
    expect(session.project.config.orchestrator_models?.codex?.['3']).toBe('astra')
  })
})
