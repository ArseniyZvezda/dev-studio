import { createRenderer, createSSRApp, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentChat, ProducerEffortMode, ProducerPolicy } from '@/api/types'
import ProducerControls from './ProducerControls.vue'

const mocks = vi.hoisted(() => ({
  chat: {
    view: 'chat', busy: false, newMemberId: null,
    activeChat: null as Partial<AgentChat> | null,
    activeMessages: [],
    producerAction: vi.fn(), setProducerPolicy: vi.fn(), setProducerEstimateTaskCost: vi.fn(),
    setProducerEffortMode: vi.fn(),
  },
}))

vi.mock('@/stores/chat', async () => {
  const { reactive } = await import('vue')
  mocks.chat = reactive(mocks.chat)
  return { useChatStore: () => mocks.chat }
})
vi.mock('@/stores/teams', () => ({ useTeamsStore: () => ({
  teams: [{ members: [{ id: 'producer', builtin: 'producer' }] }], memberById: new Map(),
}) }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))

interface HostNode {
  children: HostNode[]
  parent: HostNode | null
}

function node(): HostNode {
  return { children: [], parent: null }
}

// Run the real setup/lifecycle for policy clicks; markup uses the SFC's SSR renderer.
const renderer = createRenderer<HostNode, HostNode>({
  createElement: node,
  createText: node,
  createComment: node,
  setText: () => {},
  setElementText: () => {},
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null,
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

function markup(toolbar = true): Promise<string> {
  return renderToString(createSSRApp(ProducerControls, { toolbar }))
}

function button(html: string, label: string): { disabled: boolean; pressed: boolean } {
  const found = [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].find(match =>
    match[2]!.replace(/<[^>]*>/g, '').trim() === label)
  expect(found, `button ${label}`).toBeDefined()
  return { disabled: /\bdisabled(?:\s|=|$)/.test(found![1]!), pressed: found![1]!.includes('aria-pressed="true"') }
}

interface ControlsState {
  changePolicy: (policy: ProducerPolicy) => void
  changeEffortMode: (mode: ProducerEffortMode) => void
}

const apps: ReturnType<typeof renderer.createApp>[] = []
function mount(toolbar = true): ControlsState {
  const app = renderer.createApp({ ...ProducerControls, render: () => null }, { toolbar })
  app.provide(ssrContextKey, {})
  app.mount(node())
  apps.push(app)
  return (app._instance as unknown as { setupState: ControlsState }).setupState
}

describe('Producer controls before the first message', () => {
  beforeEach(() => {
    mocks.chat.view = 'chat'
    mocks.chat.busy = false
    mocks.chat.activeChat = {
      id: 'chat', member_id: 'producer', producer_workspace: true, producer_mode: true,
      producer_started: false, producer_paused: false, producer_state: 'discussing',
      producer_policy: 'careful', is_running: false,
    }
    mocks.chat.producerAction.mockReset()
    mocks.chat.setProducerPolicy.mockReset()
    mocks.chat.setProducerPolicy.mockImplementation(async (policy: ProducerPolicy) => {
      mocks.chat.activeChat!.producer_policy = policy
    })
    mocks.chat.setProducerEffortMode.mockReset()
    mocks.chat.setProducerEffortMode.mockImplementation(async (mode: ProducerEffortMode) => {
      mocks.chat.activeChat!.producer_effort_mode = mode
    })
  })

  afterEach(() => {
    for (const app of apps.splice(0)) app.unmount()
  })

  it.each([[true, false], [true, true], [false, false], [false, true]])(
    'shows Not started and saves either policy without starting (toolbar=%s, paused=%s)', async (toolbar, paused) => {
    // A stale/team pause flag cannot turn an empty conversation into a paused session.
    mocks.chat.activeChat!.producer_paused = paused
    const state = mount(toolbar)
    let html = await markup(toolbar)
    const statuses = [...html.matchAll(/role="status"[^>]*>([^<]*)</g)].map(match => match[1])
    expect(statuses).toEqual(['chat.producer.states.not_started'])
    expect(button(html, 'chat.producer.pause').disabled).toBe(true)
    expect(button(html, 'chat.producer.policies.careful').disabled).toBe(false)
    expect(button(html, 'chat.producer.policies.independent').disabled).toBe(false)

    state.changePolicy('independent')
    await nextTick()
    expect(mocks.chat.setProducerPolicy).toHaveBeenLastCalledWith('independent')
    html = await markup(toolbar)
    expect(button(html, 'chat.producer.policies.independent').pressed).toBe(true)
    state.changePolicy('careful')
    await nextTick()
    expect(mocks.chat.setProducerPolicy).toHaveBeenLastCalledWith('careful')
    expect(mocks.chat.producerAction).not.toHaveBeenCalled()
    expect(button(await markup(toolbar), 'chat.producer.pause').disabled).toBe(true)
  })

  it('enables Pause after the first message and retains started pause/resume controls', async () => {
    mocks.chat.activeChat!.producer_started = true
    mocks.chat.activeChat!.producer_state = 'awaiting_user'
    let html = await markup()
    expect(html).not.toContain('chat.producer.states.not_started')
    expect(button(html, 'chat.producer.pause').disabled).toBe(false)

    mocks.chat.activeChat!.producer_paused = true
    html = await markup()
    expect(html).toContain('chat.producer.pausedShort')
    expect(button(html, 'chat.producer.policies.independent').disabled).toBe(true)
    expect(button(html, 'chat.producer.resume').disabled).toBe(false)
  })

  it('keeps policy changes disabled during a running turn', async () => {
    mocks.chat.activeChat!.producer_started = true
    mocks.chat.activeChat!.is_running = true
    const html = await markup()
    expect(button(html, 'chat.producer.policies.careful').disabled).toBe(true)
    expect(button(html, 'chat.producer.policies.independent').disabled).toBe(true)
    expect(button(html, 'chat.producer.pause').disabled).toBe(false)
  })

  it('defaults effort to Agent and saves Auto without starting the conversation', async () => {
    const state = mount()
    expect(await markup()).toContain('<select value="agent"')
    state.changeEffortMode('auto')
    await nextTick()
    expect(mocks.chat.setProducerEffortMode).toHaveBeenCalledWith('auto')
    expect(await markup()).toContain('<select value="auto"')
    expect(mocks.chat.producerAction).not.toHaveBeenCalled()
  })

  it('locks effort during a turn but allows setting it while paused', async () => {
    mocks.chat.activeChat!.is_running = true
    let html = await markup()
    expect(html.match(/<select\b[^>]*>/)?.[0]).toMatch(/\sdisabled(?:\s|>|=)/)
    mocks.chat.activeChat!.is_running = false
    mocks.chat.activeChat!.producer_started = true
    mocks.chat.activeChat!.producer_paused = true
    html = await markup()
    expect(html.match(/<select\b[^>]*>/)?.[0]).not.toMatch(/\sdisabled(?:\s|>|=)/)
  })
})
