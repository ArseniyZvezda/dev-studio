import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AppConfig, SettingsUpdate } from '@/api/types'
import TeamSettings from '@/components/teams/TeamSettings.vue'
import AgentPrompts from './AgentPrompts.vue'

const mocks = vi.hoisted(() => ({
  updateSettings: vi.fn(),
  session: { project: null as unknown },
}))

vi.mock('@/api/client', () => ({ api: {
  promptSpecs: async () => ({ identity: {
    default: 'Default identity', required: false, placeholders: [],
  } }),
  updateSettings: mocks.updateSettings,
} }))
vi.mock('@/stores/session', async () => {
  const { reactive } = await import('vue')
  mocks.session = reactive(mocks.session)
  return { useSessionStore: () => mocks.session }
})
vi.mock('@/i18n', () => ({ t: (key: string) => key, hasMessage: () => false }))
vi.mock('@/stores/teams', () => ({ useTeamsStore: () => ({ options: null }) }))

// Mount the real setup/lifecycle with a headless renderer: input is a draft update and
// scheduleSave, exactly the two handlers attached to the textarea in the component.
interface HostNode {
  tag: string
  children: HostNode[]
  parent: HostNode | null
  text: string
}

function node(tag: string): HostNode {
  return { tag, children: [], parent: null, text: '' }
}

const renderer = createRenderer<HostNode, HostNode>({
  createElement: node,
  createText: (text) => ({ ...node('#text'), text }),
  createComment: (text) => ({ ...node('#comment'), text }),
  setText: (target, text) => { target.text = text },
  setElementText: (target, text) => { target.text = text; target.children = [] },
  parentNode: (target) => target.parent,
  nextSibling: (target) => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null,
  insert(target, parent, anchor) {
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(target)
    else parent.children.splice(index, 0, target)
  },
  remove(target) {
    if (target.parent) target.parent.children = target.parent.children.filter(child => child !== target)
  },
  patchProp: () => {},
})

interface PromptState {
  drafts: Record<string, string>
  failedKeys: Set<string>
  scheduleSave: (key: string) => void
}

function configuration(identity: string) {
  return { teams: { prompts: { identity } } }
}

async function settle(): Promise<void> {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

async function mount(saveSettings?: (patch: SettingsUpdate) => Promise<AppConfig>) {
  const root = node('root')
  const app = renderer.createApp({ ...AgentPrompts, render: () => null }, { autosave: true, saveSettings })
  app.provide(ssrContextKey, {})
  app.mount(root)
  await settle()
  const state = (app._instance as unknown as { setupState: PromptState }).setupState
  return { app, state }
}

interface DefaultsState {
  maxTurns: number
  permissionMode: string
  requiredSkills: string[]
  requiredDocuments: string[]
  saveError: string | null
  scheduleSave: (patch: { permission_mode?: string; max_turns?: number; required_skills?: string[]; required_documents?: string[] }, immediate?: boolean) => void
  updateSettings: (patch: SettingsUpdate) => Promise<AppConfig>
}

async function mountDefaults() {
  const app = renderer.createApp({ ...TeamSettings, render: () => null })
  app.provide(ssrContextKey, {})
  app.mount(node('root'))
  await settle()
  const state = (app._instance as unknown as { setupState: DefaultsState }).setupState
  return { app, state }
}

async function edit(state: PromptState, value: string) {
  state.drafts.identity = value
  state.scheduleSave('identity')
  await nextTick()
}

describe('Team agent prompt autosave', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.updateSettings.mockReset()
    mocks.session.project = { config: configuration('Default identity') }
    mocks.updateSettings.mockImplementation(async (patch) => configuration(patch.teams.prompts.identity))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('debounces typing and flushes the last edit when the page closes', async () => {
    const { app, state } = await mount()
    await edit(state, 'First')
    await vi.advanceTimersByTimeAsync(300)
    await edit(state, 'Last')
    await vi.advanceTimersByTimeAsync(599)
    expect(mocks.updateSettings).not.toHaveBeenCalled()
    app.unmount()
    await settle()
    expect(mocks.updateSettings).toHaveBeenCalledExactlyOnceWith({ teams: { prompts: { identity: 'Last' } } })
  })

  it('keeps newer typing while an older save resolves and sends changes serially', async () => {
    let finishFirst!: (value: ReturnType<typeof configuration>) => void
    mocks.updateSettings.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }))
    const { app, state } = await mount()
    await edit(state, 'First')
    await vi.advanceTimersByTimeAsync(600)
    await edit(state, 'Second')
    await vi.advanceTimersByTimeAsync(600)
    expect(mocks.updateSettings).toHaveBeenCalledTimes(1)
    finishFirst(configuration('First'))
    await settle()
    expect(state.drafts.identity).toBe('Second')
    expect(mocks.updateSettings).toHaveBeenCalledTimes(2)
    expect(mocks.updateSettings).toHaveBeenLastCalledWith({ teams: { prompts: { identity: 'Second' } } })
    app.unmount()
  })

  it('keeps a failed draft and offers retry without repeatedly sending it', async () => {
    mocks.updateSettings.mockRejectedValueOnce(new Error('Unavailable'))
    const { app, state } = await mount()
    await edit(state, 'Keep this draft')
    await vi.advanceTimersByTimeAsync(600)
    await settle()
    expect(state.drafts.identity).toBe('Keep this draft')
    expect(state.failedKeys.has('identity')).toBe(true)
    await vi.advanceTimersByTimeAsync(6_000)
    expect(mocks.updateSettings).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('flushes a valid numeric draft on navigation without requiring blur', async () => {
    mocks.updateSettings.mockResolvedValue({ teams: { max_turns: 80, permission_mode: 'plan', prompts: {} } })
    const { app, state } = await mountDefaults()
    state.maxTurns = 80
    state.scheduleSave({ max_turns: 80 })
    expect(mocks.updateSettings).not.toHaveBeenCalled()
    app.unmount()
    await settle()
    expect(mocks.updateSettings).toHaveBeenCalledExactlyOnceWith({ teams: { max_turns: 80 } })
  })

  it('serializes prompt and default saves while preserving newer numeric input', async () => {
    let finishPrompt!: (value: unknown) => void
    let finishDefault!: (value: unknown) => void
    mocks.updateSettings
      .mockImplementationOnce(() => new Promise(resolve => { finishPrompt = resolve }))
      .mockImplementationOnce(() => new Promise(resolve => { finishDefault = resolve }))
      .mockResolvedValue({ teams: { max_turns: 90, permission_mode: 'plan', prompts: { identity: 'Edited' } } })
    const defaults = await mountDefaults()
    const prompts = await mount(defaults.state.updateSettings)
    await edit(prompts.state, 'Edited')
    await vi.advanceTimersByTimeAsync(600)
    defaults.state.maxTurns = 80
    defaults.state.scheduleSave({ max_turns: 80 })
    await vi.advanceTimersByTimeAsync(600)
    expect(mocks.updateSettings).toHaveBeenCalledTimes(1)
    finishPrompt({ teams: { max_turns: 40, permission_mode: 'plan', prompts: { identity: 'Edited' } } })
    await settle()
    expect(defaults.state.maxTurns).toBe(80)
    expect(mocks.updateSettings).toHaveBeenLastCalledWith({ teams: { max_turns: 80 } })
    defaults.state.maxTurns = 90
    defaults.state.scheduleSave({ max_turns: 90 })
    defaults.app.unmount()
    prompts.app.unmount()
    finishDefault({ teams: { max_turns: 80, permission_mode: 'plan', prompts: { identity: 'Edited' } } })
    await settle()
    expect(mocks.updateSettings).toHaveBeenCalledTimes(3)
    expect(mocks.updateSettings).toHaveBeenLastCalledWith({ teams: { max_turns: 90 } })
    expect(defaults.state.maxTurns).toBe(90)
  })

  it('does not persist an earlier numeric value after it becomes invalid', async () => {
    const { app, state } = await mountDefaults()
    state.maxTurns = 80
    state.scheduleSave({ max_turns: 80 })
    state.maxTurns = 0
    state.scheduleSave({ max_turns: 0 })
    app.unmount()
    await settle()
    expect(mocks.updateSettings).not.toHaveBeenCalled()
  })

  it('serializes rapid required-skill additions and removals without restoring the old response', async () => {
    let finishFirst!: (value: unknown) => void
    mocks.updateSettings
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }))
      .mockResolvedValue({ teams: { required_skills: [], required_documents: ['design.md'], prompts: {} } })
    const { app, state } = await mountDefaults()
    state.requiredSkills = ['guide']
    state.scheduleSave({ required_skills: ['guide'] }, true)
    await settle()
    state.requiredSkills = []
    state.requiredDocuments = ['design.md']
    state.scheduleSave({ required_skills: [], required_documents: ['design.md'] }, true)
    expect(mocks.updateSettings).toHaveBeenCalledTimes(1)
    finishFirst({ teams: { required_skills: ['guide'], required_documents: [], prompts: {} } })
    await settle()
    expect(mocks.updateSettings).toHaveBeenLastCalledWith({ teams: { required_skills: [], required_documents: ['design.md'] } })
    expect(state.requiredSkills).toEqual([])
    expect(state.requiredDocuments).toEqual(['design.md'])
    app.unmount()
    const reopened = await mountDefaults()
    expect(reopened.state.requiredSkills).toEqual([])
    expect(reopened.state.requiredDocuments).toEqual(['design.md'])
    reopened.app.unmount()
  })

  it('retains failed requirements and retries them with the next settings edit', async () => {
    mocks.updateSettings.mockRejectedValueOnce(new Error('Unavailable')).mockResolvedValue({
      teams: { max_turns: 90, required_skills: ['guide'], required_documents: ['design.md'], prompts: {} },
    })
    const { app, state } = await mountDefaults()
    state.requiredSkills = ['guide']
    state.requiredDocuments = ['design.md']
    state.scheduleSave({ required_skills: ['guide'], required_documents: ['design.md'] }, true)
    await settle()
    expect(state.saveError).toBe('Unavailable')
    expect(state.requiredSkills).toEqual(['guide'])
    expect(state.requiredDocuments).toEqual(['design.md'])
    await vi.advanceTimersByTimeAsync(6_000)
    expect(mocks.updateSettings).toHaveBeenCalledTimes(1)
    state.maxTurns = 90
    state.scheduleSave({ max_turns: 90 }, true)
    await settle()
    expect(mocks.updateSettings).toHaveBeenLastCalledWith({ teams: {
      max_turns: 90, required_skills: ['guide'], required_documents: ['design.md'],
    } })
    expect(state.saveError).toBeNull()
    app.unmount()
  })

  it('merges a failed in-flight requirement with newer pending selections', async () => {
    let failFirst!: (error: Error) => void
    mocks.updateSettings
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirst = reject }))
      .mockResolvedValue({ teams: { required_skills: ['guide'], required_documents: ['new.md'], prompts: {} } })
    const { app, state } = await mountDefaults()
    state.requiredSkills = ['guide']
    state.requiredDocuments = ['old.md']
    state.scheduleSave({ required_skills: ['guide'], required_documents: ['old.md'] }, true)
    await settle()
    state.requiredDocuments = ['new.md']
    state.scheduleSave({ required_documents: ['new.md'] }, true)
    failFirst(new Error('Unavailable'))
    await settle()
    expect(mocks.updateSettings).toHaveBeenLastCalledWith({ teams: {
      required_skills: ['guide'], required_documents: ['new.md'],
    } })
    expect(state.saveError).toBeNull()
    expect(state.requiredDocuments).toEqual(['new.md'])
    app.unmount()
  })
})
