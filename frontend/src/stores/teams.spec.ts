/**
 * The board's two live figures: what the finished work cost, and what each agent is doing.
 *
 * Both are derived rather than fetched, and the derivation is the part worth pinning. The
 * cost has to ignore work in flight, and the activity line has to come off the message
 * stream for *every* task rather than only the open one — which is the half a browser check
 * cannot reach without billing a real session, since nothing but a live agent emits those
 * events.
 *
 * The API and the socket are stubs, so what is under test is the store's decisions.
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentMessage, TaskDetail, Team, TeamDetail, TeamMember, TeamTask } from '@/api/types'
import { liveLine } from '@/lib/transcript'

const api = {
  teams: vi.fn(),
  team: vi.fn(),
  setAutostartTasks: vi.fn(),
  startAllTasks: vi.fn(),
  compressTask: vi.fn(),
  cancelTask: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
  deleteMember: vi.fn(),
  task: vi.fn(),
  addNote: vi.fn(),
}

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ApiError: actual.ApiError,
    api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
  }
})

/** Captured so a test can play an event the way the socket would. */
const handlers = new Map<string, (payload: never, envelope?: { project_id?: string; epoch?: string }) => void>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: (payload: never, envelope?: { project_id?: string; epoch?: string }) => void) => {
    handlers.set(type, handler)
  }),
}))

const mockSession = vi.hoisted(() => ({
  isLegacy: true,
  matches: (scope: { project_id?: string; epoch?: string }) => scope.project_id === 'project_a' && scope.epoch === 'epoch_a',
}))
vi.mock('@/session/project-session', () => ({ rendererSession: mockSession }))

const { useTeamsStore } = await import('./teams')

function task(overrides: Partial<TeamTask> = {}): TeamTask {
  return {
    id: 'task_1',
    number: 1,
    display_id: 'DP-01',
    team_id: 'team_1',
    assignee_id: 'member_1',
    created_by: null,
    title: 'Tune the dash',
    brief: '',
    write_paths: [],
    status: 'in_progress',
    depends_on: [],
    auto_start: true,
    resume_on_limit: false,
    limit_reset_at: null,
    limit_resume_at: null,
    model: null,
    effort: null,
    agent_session_id: null,
    remote_control_url: null,
    worktree_path: null,
    review_comment: null,
    result_summary: null,
    result_type: null,
    error: null,
    total_cost_usd: 0,
    num_turns: 0,
    total_tokens: 0,
    archived: false,
    created_at: '2026-08-24T10:00:00+00:00',
    started_at: null,
    finished_at: null,
    is_running: true,
    ...overrides,
  }
}

/** The event the backend publishes for every task, not only the one on screen. */
function messageEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: 'msg_1',
    task_id: 'task_1',
    member_id: 'member_1',
    role: 'assistant',
    kind: 'tool_use',
    text: 'Bash',
    data: { name: 'Bash', input: { command: 'npm test' } },
    created_at: '2026-08-24T10:00:01+00:00',
    ...overrides,
  }
}

async function storeWith(tasks: TeamTask[]) {
  const team: TeamDetail = {
    id: 'team_1',
    name: 'Default',
    description: '',
    autostart_tasks: false,
    created_at: '2026-08-24T09:00:00+00:00',
    members: [],
    tasks,
  }
  api.teams.mockResolvedValue([team as Team])
  api.team.mockResolvedValue(team)
  const store = useTeamsStore()
  await store.load()
  store.subscribe()
  return store
}

beforeEach(() => {
  mockSession.isLegacy = true
  setActivePinia(createPinia())
  handlers.clear()
  for (const stub of Object.values(api)) stub.mockReset()
})

describe('scoped gap recovery', () => {
  const scope = { project_id: 'project_a', epoch: 'epoch_a' }
  const gap = () => handlers.get('system.dropped')!({ count: 3, resync: true } as never, scope)
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
  const message = (id: string, second: number, kind = 'text'): AgentMessage => ({
    id, task_id: 'task_1', member_id: 'member_1', role: 'assistant', kind, text: id,
    data: {}, created_at: `2026-08-24T10:00:${String(second).padStart(2, '0')}+00:00`,
  })

  it('recovers roster, board and transcript while retaining the selected editor and its identity', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    store.teams[0]!.members = [{ id: 'member_1', name: 'Original' }] as TeamMember[]
    store.editingMemberId = 'member_1'
    const edited = store.editingMember
    store.openTask = { ...task(), messages: [], assignee: null }
    const refreshed = { ...store.team!, name: 'Recovered',
      members: [{ id: 'member_1', name: 'External name' }, { id: 'member_2' }] as TeamMember[],
      tasks: [task({ status: 'done' }), task({ id: 'task_2' })] }
    api.teams.mockResolvedValue([refreshed])
    api.team.mockResolvedValue(refreshed)
    api.task.mockResolvedValue({ ...store.openTask, status: 'done', messages: [messageEvent({ kind: 'text', text: 'Missed message' })] })
    gap()
    await settle()
    expect(store.team?.name).toBe('Recovered')
    expect(store.tasks).toHaveLength(2)
    expect(store.openTask?.messages[0]?.text).toBe('Missed message')
    expect(store.openTask?.status).toBe('done')
    expect(store.activeTeamId).toBe('team_1')
    expect(store.editingMemberId).toBe('member_1')
    expect(store.editingMember).toBe(edited)
    expect(store.openTask?.id).toBe('task_1')
  })

  it('does not reopen an old task and guards ordinary async task navigation too', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const original = { ...task(), messages: [], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    gap()
    await settle()
    api.task.mockResolvedValueOnce({ ...original, id: 'task_2' })
    await store.selectTask('task_2')
    resolve(original)
    await settle()
    expect(store.openTask?.id).toBe('task_2')

    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const selecting = store.selectTask('task_1')
    await store.selectTask(null)
    resolve(original)
    await selecting
    expect(store.openTask).toBeNull()
  })

  it('does not roll back a newer live task status with an older recovery snapshot', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const old = { ...store.team! }
    const latest = { ...old, tasks: [task({ status: 'done' })] }
    let finish!: (detail: typeof old) => void
    api.team.mockResolvedValue(latest).mockImplementationOnce(() => new Promise(done => { finish = done }))
    gap(); await settle()
    handlers.get('team.task.status_changed')!({ team_id: old.id, task_id: 'task_1', status: 'done', error: null } as never)
    await settle()
    expect(store.tasks[0]?.status).toBe('done')
    finish(old); await settle()
    expect(store.tasks[0]?.status).toBe('done')
  })

  it('keeps messages that arrive while a transcript recovery is in flight', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const original = { ...task(), messages: [], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    gap()
    await settle()
    handlers.get('team.member.message')!(messageEvent({ id: 'live', kind: 'text', text: 'Still here' }) as never)
    resolve(original)
    await settle()
    expect(store.openTask?.messages.map(message => message.id)).toEqual(['live'])
  })

  it('replaces missed compacted history, keeps live arrivals and ignores subsequent replay', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const old = message('removed', 1)
    const boundary = message('also-removed', 2)
    const summary = message('summary', 2, 'compressed summary')
    const original = { ...task(), messages: [old, boundary], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    gap(); await settle()
    // Deliberately out of order: arrival order is not transcript order.
    handlers.get('team.member.message')!(message('live', 4) as never)
    handlers.get('team.member.message')!(message('missed-tail', 3) as never)
    resolve({ ...original, messages: [summary] })
    await settle()
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'missed-tail', 'live'])
    handlers.get('team.member.message')!(old as never)
    handlers.get('team.member.message')!(boundary as never)
    handlers.get('team.member.message')!(summary as never)
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'missed-tail', 'live'])
    expect(store.openTask?.id).toBe(original.id)
    expect(store.activeTeamId).toBe('team_1')
  })

  it('does not restore pre-compaction rows from a late recovery snapshot', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const old = message('removed', 1)
    const original = { ...task(), messages: [old], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    gap(); await settle()
    handlers.get('team.member.message')!(message('summary', 2, 'context_summary') as never)
    handlers.get('team.member.message')!(message('live', 3) as never)
    resolve(original)
    await settle()
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'live'])
  })

  it('protects recovery from an older ordinary task snapshot still in flight', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const original = { ...task(), messages: [message('removed', 1)], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const selecting = store.selectTask(original.id)
    api.task.mockResolvedValueOnce({ ...original, messages: [message('summary', 2, 'compressed summary')] })
    gap(); await settle()
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary'])
    handlers.get('team.member.message')!(message('live', 3) as never)
    resolve(original)
    await selecting
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'live'])
  })

  it.each([false, true])('recovers the pending selection before its stale first snapshot, with another task open=%s', async otherOpen => {
    mockSession.isLegacy = false
    const store = await storeWith([task(), task({ id: 'task_2' })])
    if (otherOpen) store.openTask = { ...task({ id: 'task_2' }), messages: [message('other-task', 1)], assignee: null }
    const original = { ...task(), messages: [message('removed', 1)], assignee: null }
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const selecting = store.selectTask('task_1')
    api.task.mockResolvedValueOnce({ ...original, messages: [message('summary', 2, 'context_summary')] })
    gap(); await settle()
    expect(api.task).toHaveBeenLastCalledWith('team_1', 'task_1')
    // Recovery feeds the pending read without reopening/replacing the visible panel early.
    expect(store.openTask?.id ?? null).toBe(otherOpen ? 'task_2' : null)
    handlers.get('team.member.message')!(message('live', 3) as never)
    resolve(original)
    await selecting
    expect(store.openTask?.id).toBe('task_1')
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'live'])
  })

  it('keeps a REST note accepted during a detail read even when its WS delivery was missed', async () => {
    const store = await storeWith([task()])
    const original = { ...task(), messages: [message('summary', 2, 'context_summary')], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const selecting = store.selectTask('task_1')
    const note = { ...message('note', 3), data: { delivery: 'queued' } }
    api.addNote.mockResolvedValueOnce(note)
    expect(await store.messageTask('task_1', 'New note')).toBe(true)
    resolve(original)
    await selecting
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'note'])
  })

  it('keeps compaction and live messages received during an ordinary task refresh', async () => {
    const store = await storeWith([task()])
    const original = { ...task(), messages: [message('removed', 1)], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const selecting = store.selectTask(original.id)
    handlers.get('team.member.message')!(message('summary', 2, 'context_summary') as never)
    handlers.get('team.member.message')!(message('live', 3) as never)
    resolve(original)
    await selecting
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary', 'live'])
  })

  it('buffers only the newly selected task while ignoring the previous task recovery', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task(), task({ id: 'task_2' })])
    const original = { ...task(), messages: [message('old', 1)], assignee: null }
    store.openTask = original
    let finishRecovery!: (detail: typeof original) => void
    let finishSelection!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { finishRecovery = done }))
      .mockReturnValueOnce(new Promise(done => { finishSelection = done }))
    gap(); await settle()
    const selecting = store.selectTask('task_2')
    const selectedMessage = (id: string, second: number, kind = 'text') =>
      ({ ...message(id, second, kind), task_id: 'task_2' })
    handlers.get('team.member.message')!(selectedMessage('summary-2', 2, 'context_summary') as never)
    handlers.get('team.member.message')!(message('old-task-live', 5) as never)
    handlers.get('team.member.message')!(selectedMessage('live-2', 3) as never)
    finishSelection({ ...original, id: 'task_2', messages: [selectedMessage('removed-2', 1)] })
    await selecting
    finishRecovery({ ...original, messages: [message('summary-1', 4, 'compressed summary')] })
    await settle()
    expect(store.openTask?.id).toBe('task_2')
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['summary-2', 'live-2'])
  })

  it('still accepts an empty authoritative snapshot after a task reset', async () => {
    const store = await storeWith([task()])
    store.openTask = { ...task(), messages: [message('old-summary', 2, 'context_summary')], assignee: null }
    api.task.mockResolvedValueOnce({ ...store.openTask, messages: [] })
    await store.selectTask('task_1')
    expect(store.openTask?.messages).toEqual([])
  })

  it('runs another recovery for a gap during REST without rolling back the newest compaction', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const old = message('removed', 1)
    const first = message('first-summary', 2, 'compressed summary')
    const second = message('second-summary', 4, 'compressed summary')
    const original = { ...task(), messages: [old], assignee: null }
    store.openTask = original
    let resolveFirst!: (detail: typeof original) => void
    let resolveSecond!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolveFirst = done }))
      .mockReturnValueOnce(new Promise(done => { resolveSecond = done }))
    gap(); await settle()
    gap(); gap()
    resolveFirst({ ...original, messages: [first] })
    await settle()
    expect(api.task).toHaveBeenCalledTimes(2)
    handlers.get('team.member.message')!(second as never)
    handlers.get('team.member.message')!(message('live', 5) as never)
    resolveSecond({ ...original, messages: [old, first, message('removed-tail', 3)] })
    await settle()
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['second-summary', 'live'])
    handlers.get('team.member.message')!(first as never)
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['second-summary', 'live'])
  })

  it('ignores a compacted recovery for a selection that was closed and reopened', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const original = { ...task(), messages: [message('old', 1)], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    gap(); await settle()
    await store.selectTask(null)
    api.task.mockResolvedValueOnce({ ...original, messages: [message('new-summary', 3, 'context_summary')] })
    await store.selectTask(original.id)
    resolve({ ...original, messages: [message('old-summary', 2, 'compressed summary')] })
    await settle()
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['new-summary'])
  })

  it('keeps newest telemetry and chronological history when recovery overlaps WS', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    const original = { ...task(), messages: [message('state_usage', 1)], assignee: null }
    store.openTask = original
    let resolve!: (detail: typeof original) => void
    api.task.mockReturnValueOnce(new Promise(done => { resolve = done }))
    gap(); await settle()
    handlers.get('team.member.message')!(message('live', 5) as never)
    handlers.get('team.member.message')!(message('state_usage', 3) as never)
    resolve({ ...original, messages: [message('history', 2), message('state_usage', 4)] })
    await settle()
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['history', 'state_usage', 'live'])
    expect(store.openTask?.messages[1]?.created_at).toBe(message('state_usage', 4).created_at)
  })

  it('retains legacy live delivery and queued note replacement without enabling gap recovery', async () => {
    const store = await storeWith([task()])
    const queued = { ...message('note', 3), data: { delivery: 'queued' } }
    store.openTask = { ...task(), messages: [queued], assignee: null }
    handlers.get('team.member.message')!({ ...queued, data: { delivery: 'delivered' } } as never)
    handlers.get('team.member.message')!(message('earlier', 2) as never)
    expect(store.openTask?.messages.map(entry => entry.id)).toEqual(['earlier', 'note'])
    expect(store.openTask?.messages[1]?.data.delivery).toBe('delivered')
    api.task.mockClear()
    gap(); await settle()
    expect(api.task).not.toHaveBeenCalled()
  })

  it('ignores foreign, obsolete, unscoped and legacy gaps', async () => {
    await storeWith([])
    api.teams.mockClear()
    gap()
    mockSession.isLegacy = false
    const handler = handlers.get('system.dropped')!
    handler({ resync: true } as never, { ...scope, project_id: 'project_b' })
    handler({ resync: true } as never, { ...scope, epoch: 'old' })
    handler({ resync: true } as never)
    handler({ resync: false } as never, scope)
    await settle()
    expect(api.teams).not.toHaveBeenCalled()
  })

  it('does not surface an old transcript failure after navigating away', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([task()])
    store.openTask = { ...task(), messages: [], assignee: null }
    let reject!: (cause: Error) => void
    api.task.mockReturnValueOnce(new Promise((_done, fail) => { reject = fail }))
    gap()
    await settle()
    await store.selectTask(null)
    reject(new Error('old selection failed'))
    await settle()
    expect(store.error).toBeNull()
    expect(store.openTask).toBeNull()
  })

  it('coalesces bursts, handles a failed request and can recover again', async () => {
    mockSession.isLegacy = false
    const store = await storeWith([])
    api.teams.mockClear().mockRejectedValueOnce(new Error('offline'))
    gap(); gap(); gap()
    await settle()
    expect(api.teams).toHaveBeenCalledTimes(1)
    expect(store.error).toBe('offline')
    gap()
    await settle()
    expect(api.teams).toHaveBeenCalledTimes(2)
  })
})

describe('user task selection versus background WS refreshes (VS-34)', () => {
  const scope = { project_id: 'project_a', epoch: 'epoch_a' }
  const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve() }
  const deferred = () => {
    let resolve!: (detail: TaskDetail) => void
    const promise = new Promise<TaskDetail>(done => { resolve = done })
    return { promise, resolve }
  }
  const message = (taskId: string, id: string, second: number, kind = 'text'): AgentMessage => ({
    id, task_id: taskId, member_id: 'member_1', role: 'assistant', kind, text: id,
    data: {}, created_at: `2026-08-24T10:00:${String(second).padStart(2, '0')}+00:00`,
  })
  const detail = (id: string, messages: AgentMessage[] = []): TaskDetail =>
    ({ ...task({ id }), messages, assignee: null })
  const emitMessage = (entry: AgentMessage) =>
    handlers.get('team.member.message')!(entry as never, mockSession.isLegacy ? undefined : scope)
  const emitUpdate = (event: string) => handlers.get(event)!({
    team_id: 'team_1', task_id: 'task_a', status: 'needs_review', error: null,
  } as never, mockSession.isLegacy ? undefined : scope)
  const gap = () => handlers.get('system.dropped')!({ count: 3, resync: true } as never, scope)
  const events = ['team.task.status_changed', 'team.task.updated']

  describe.each([true, false].flatMap(legacy => events.map(event => ({ legacy, event }))))(
    'legacy=$legacy, $event', ({ legacy, event }) => {
      it.each([
        { refreshBeforeSelection: true, backgroundFirst: true },
        { refreshBeforeSelection: true, backgroundFirst: false },
        { refreshBeforeSelection: false, backgroundFirst: true },
        { refreshBeforeSelection: false, backgroundFirst: false },
      ])('preserves B and its WS buffer: refresh before selection=$refreshBeforeSelection, A resolves first=$backgroundFirst', async ({ refreshBeforeSelection, backgroundFirst }) => {
        mockSession.isLegacy = legacy
        const store = await storeWith([task({ id: 'task_a' }), task({ id: 'task_b' })])
        store.openTask = detail('task_a', [message('task_a', 'a-open-history', 1)])
        const background = deferred()
        const selection = deferred()
        // Dispatch by task id: skipping the obsolete A refresh during pending B is valid.
        api.task.mockImplementation((_teamId: string, taskId: string) =>
          taskId === 'task_a' ? background.promise : selection.promise)
        if (refreshBeforeSelection) {
          emitUpdate(event)
          await settle()
          expect(api.task).toHaveBeenCalledWith('team_1', 'task_a')
        }
        const selecting = store.selectTask('task_b')
        emitMessage(message('task_b', 'b-before-event', 3))
        if (!refreshBeforeSelection) emitUpdate(event)
        emitMessage(message('task_a', 'a-live', 4))
        await settle()
        // A may remain visible while B loads, but a WS event must not navigate back to A.
        expect(store.openTask?.id).toBe('task_a')
        const finishBackground = async () => {
          background.resolve(detail('task_a', [message('task_a', 'a-rest-history', 5)]))
          await settle()
        }
        if (backgroundFirst) await finishBackground()
        emitMessage(message('task_b', 'b-after-event', 6))
        selection.resolve(detail('task_b', [message('task_b', 'b-rest-history', 2)]))
        await selecting
        if (!backgroundFirst) await finishBackground()
        expect(store.openTask?.id).toBe('task_b')
        expect(store.openTask?.messages.map(entry => entry.id)).toEqual([
          'b-rest-history', 'b-before-event', 'b-after-event',
        ])
        expect(store.openTask?.messages.every(entry => entry.task_id === 'task_b')).toBe(true)
        emitMessage(message('task_a', 'a-late-live', 7))
        expect(store.openTask?.messages.map(entry => entry.id)).toEqual([
          'b-rest-history', 'b-before-event', 'b-after-event',
        ])
      })

      it.each(['reset', 'snapshot compaction', 'live compaction'])(
        'applies an authoritative current-task background %s without losing live arrivals', async mode => {
          mockSession.isLegacy = legacy
          const store = await storeWith([task({ id: 'task_a' })])
          store.openTask = detail('task_a', [
            message('task_a', 'old-history', 1), message('task_a', 'old-summary', 2, 'context_summary'),
          ])
          const background = deferred()
          api.task.mockReturnValueOnce(background.promise)
          emitUpdate(event)
          await settle()
          expect(api.task).toHaveBeenCalledExactlyOnceWith('team_1', 'task_a')
          const summary = message('task_a', 'new-summary', 3, 'compressed summary')
          if (mode === 'live compaction') emitMessage(summary)
          emitMessage(message('task_a', 'live', 4))
          background.resolve(detail('task_a', mode === 'snapshot compaction' ? [summary]
            : mode === 'live compaction' ? [message('task_a', 'stale-history', 1)] : []))
          await settle()
          expect(store.openTask?.id).toBe('task_a')
          expect(store.openTask?.messages.map(entry => entry.id)).toEqual(
            mode === 'reset' ? ['live'] : ['new-summary', 'live'],
          )
        },
      )
    },
  )

  describe.each(events)('repeated scoped gaps plus %s', event => {
    it.each([false, true])('retains pending B intent and invalidates old reads on close/reopen=%s', async close => {
      mockSession.isLegacy = false
      const store = await storeWith([task({ id: 'task_a' }), task({ id: 'task_b' })])
      store.openTask = detail('task_a', [message('task_a', 'a-history', 1)])
      const background = deferred()
      const selection = deferred()
      const firstRecovery = deferred()
      const secondRecovery = deferred()
      const bReads = [selection.promise, firstRecovery.promise, secondRecovery.promise]
      api.task.mockImplementation((_teamId: string, taskId: string) => {
        if (taskId === 'task_a') return background.promise
        const read = bReads.shift()
        if (!read) throw new Error(`Unexpected detail read for ${taskId}`)
        return read
      })
      emitUpdate(event)
      await settle()
      const selecting = store.selectTask('task_b')
      gap(); await settle()
      emitMessage(message('task_b', 'b-buffered', 2))
      emitUpdate(event)
      gap(); gap()
      firstRecovery.resolve(detail('task_b', [message('task_b', 'first-summary', 3, 'context_summary')]))
      await settle()
      expect(bReads).toHaveLength(0)
      expect(api.task.mock.calls.filter(([, id]) => id === 'task_b')).toHaveLength(3)
      expect(store.openTask?.id).toBe('task_a')
      emitMessage(message('task_b', 'b-live', 6))
      emitMessage(message('task_a', 'a-live', 7))

      if (close) {
        await store.selectTask(null)
        background.resolve(detail('task_a', [message('task_a', 'a-stale-summary', 8, 'context_summary')]))
        await settle()
        expect(store.openTask).toBeNull()
        // Same id, new user intent: neither old pending read nor either gap owns this panel.
        bReads.push(Promise.resolve(detail('task_b', [message('task_b', 'reopened', 9)])))
        await store.selectTask('task_b')
      }
      secondRecovery.resolve(detail('task_b', [message('task_b', 'second-summary', 5, 'compressed summary')]))
      await settle()
      selection.resolve(detail('task_b', [message('task_b', 'b-stale-history', 1)]))
      await selecting
      background.resolve(detail('task_a', [message('task_a', 'a-rest-history', 8)]))
      await settle()
      expect(store.openTask?.id).toBe('task_b')
      const expected = close ? ['reopened'] : ['second-summary', 'b-live']
      expect(store.openTask?.messages.map(entry => entry.id)).toEqual(expected)
      expect(store.openTask?.messages.every(entry => entry.task_id === 'task_b')).toBe(true)
      if (!close) {
        emitMessage(message('task_b', 'first-summary', 3, 'context_summary'))
        expect(store.openTask?.messages.map(entry => entry.id)).toEqual(expected)
      }
    })
  })
})

describe('explicit write-file declarations', () => {
  it('sends only the declared write files, despite shared read references in the brief', async () => {
    const store = await storeWith([])
    await store.createTask({
      assigneeId: 'member_1', title: 'Read docs/design.md',
      brief: 'Use README.md for context; change src/player.py.',
      writePaths: ['src/player.py'], autoStart: false,
    })
    expect(api.createTask).toHaveBeenCalledExactlyOnceWith('team_1', expect.objectContaining({
      write_paths: ['src/player.py'],
    }))
  })

  it('defaults to no write files and sends an explicit empty list when clearing an existing list', async () => {
    const store = await storeWith([task({ write_paths: ['src/player.py'], is_running: false })])
    await store.createTask({
      assigneeId: 'member_1', title: 'Review README.md', brief: 'Read src/player.py.', autoStart: false,
    })
    expect(api.createTask).toHaveBeenCalledExactlyOnceWith('team_1', expect.objectContaining({
      write_paths: [],
    }))
    expect(await store.updateTask('task_1', { write_paths: [] })).toBe(true)
    expect(api.updateTask).toHaveBeenCalledExactlyOnceWith('team_1', 'task_1', { write_paths: [], actor_id: null })
  })
})

describe('confirmed member removal', () => {
  it('transfers tasks only with explicit confirmation and refreshes the board', async () => {
    const assigned = task({ status: 'done', is_running: false, archived: true })
    const store = await storeWith([assigned])
    store.editingMemberId = 'member_1'
    api.team.mockResolvedValueOnce({
      ...store.team,
      tasks: [{ ...assigned, assignee_id: 'team_1__director' }],
    })
    expect(await store.removeMember('member_1', true)).toBe(true)
    expect(api.deleteMember).toHaveBeenCalledExactlyOnceWith('team_1', 'member_1', true)
    expect(store.tasks[0]?.assignee_id).toBe('team_1__director')
    expect(store.tasks[0]?.archived).toBe(true)
    expect(store.editingMemberId).toBeNull()
  })

  it('retains the member editor and tasks when deletion fails', async () => {
    const store = await storeWith([task()])
    store.editingMemberId = 'member_1'
    api.deleteMember.mockRejectedValueOnce(new Error('Cannot stop the running task'))
    expect(await store.removeMember('member_1')).toBe(false)
    expect(api.deleteMember).toHaveBeenCalledExactlyOnceWith('team_1', 'member_1', false)
    expect(store.editingMemberId).toBe('member_1')
    expect(store.tasks[0]?.assignee_id).toBe('member_1')
    expect(store.error).toBe('Cannot stop the running task')
    expect(store.busy).toBe(false)
  })
})

describe('context compression progress', () => {
  it('allows cancellation while Compress owns busy and prevents duplicate cancellation', async () => {
    const store = await storeWith([task({ status: 'pending', is_running: true, is_compacting: true })])
    let finishCompression!: () => void
    let finishCancellation!: () => void
    api.compressTask.mockImplementationOnce(() => new Promise<void>((resolve) => { finishCompression = resolve }))
    api.cancelTask.mockImplementationOnce(() => new Promise<void>((resolve) => { finishCancellation = resolve }))
    const compression = store.act('compress', 'task_1')
    const cancellation = store.cancelCompression('task_1')
    expect(store.busy).toBe(true)
    expect(store.cancellingCompressionTaskId).toBe('task_1')
    expect(api.cancelTask).toHaveBeenCalledExactlyOnceWith('team_1', 'task_1')
    expect(await store.cancelCompression('task_1')).toBe(false)
    finishCancellation()
    expect(await cancellation).toBe(true)
    expect(store.cancellingCompressionTaskId).toBeNull()
    expect(store.busy).toBe(true)
    expect(store.compressingTaskId).toBe('task_1')
    finishCompression()
    await compression
    expect(store.busy).toBe(false)
  })

  it('keeps compression active when cancellation fails', async () => {
    const store = await storeWith([task({ is_compacting: true })])
    api.cancelTask.mockRejectedValueOnce(new Error('Cannot cancel'))
    expect(await store.cancelCompression('task_1')).toBe(false)
    expect(store.error).toBe('Cannot cancel')
    expect(store.cancellingCompressionTaskId).toBeNull()
  })

  it.each([true, false])('stays busy throughout the request and clears on success=%s', async (success) => {
    const store = await storeWith([task({ status: 'pending', is_running: false })])
    let finish!: () => void
    api.compressTask.mockImplementationOnce(() => new Promise<void>((resolve, reject) => {
      finish = () => success ? resolve() : reject(new Error('Compression failed'))
    }))
    const request = store.act('compress', 'task_1')
    expect(store.compressingTaskId).toBe('task_1')
    expect(store.busy).toBe(true)
    await Promise.resolve()
    expect(store.compressingTaskId).toBe('task_1')
    finish()
    expect(await request).toBe(success)
    expect(store.compressingTaskId).toBeNull()
    expect(store.busy).toBe(false)
    expect(api.compressTask).toHaveBeenCalledWith('team_1', 'task_1', null)
  })
})

describe('board-wide release', () => {
  it('offers only idle agent tasks in Todo or Pending, including queued dependencies', async () => {
    const store = await storeWith([
      task({ id: 'todo', status: 'todo', is_running: false }),
      task({ id: 'queued', status: 'pending', is_running: false, depends_on: ['upstream'] }),
      task({ id: 'submitted', status: 'pending', is_running: true }),
      task({ id: 'human', status: 'todo', is_running: false, assignee_id: 'human' }),
      task({ id: 'review', status: 'needs_review', is_running: false }),
      task({ id: 'archived', status: 'done', is_running: false, archived: true }),
      task({ id: 'blocked', status: 'blocked', is_running: false }),
    ])
    store.teams[0]!.members = [
      { id: 'member_1', kind: 'agent' }, { id: 'human', kind: 'human' },
    ] as TeamMember[]
    expect(store.startableTasks.map((entry) => entry.id)).toEqual(['todo', 'queued'])
  })

  it('keeps the saved autostart value on failure and applies cross-window changes', async () => {
    const store = await storeWith([])
    api.setAutostartTasks.mockRejectedValueOnce(new Error('Cannot save'))
    expect(await store.setAutostartTasks(true)).toBe(false)
    expect(store.team?.autostart_tasks).toBe(false)
    expect(store.error).toBe('Cannot save')
    expect(store.busy).toBe(false)
    api.setAutostartTasks.mockResolvedValueOnce({ ...store.team, autostart_tasks: true })
    expect(await store.setAutostartTasks(true)).toBe(true)
    expect(api.setAutostartTasks).toHaveBeenLastCalledWith('team_1', true)
    expect(store.team?.autostart_tasks).toBe(true)
    handlers.get('team.updated')!({ team_id: 'other', autostart_tasks: false } as never)
    expect(store.team?.autostart_tasks).toBe(true)
    handlers.get('team.updated')!({ team_id: 'team_1', autostart_tasks: false } as never)
    expect(store.team?.autostart_tasks).toBe(false)
  })

  it('submits one bulk request and prevents another while it is in flight', async () => {
    const store = await storeWith([task({ status: 'todo', is_running: false })])
    let finish!: () => void
    api.startAllTasks.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve }))
    const request = store.startAllTasks()
    expect(store.busy).toBe(true)
    expect(await store.startAllTasks()).toBe(false)
    finish()
    expect(await request).toBe(true)
    expect(api.startAllTasks).toHaveBeenCalledExactlyOnceWith('team_1')
    expect(api.team).toHaveBeenCalledTimes(2)
    expect(store.busy).toBe(false)
    expect(store.team?.autostart_tasks).toBe(false)
  })
})

describe('doneCost', () => {
  it('counts what the results cost and nothing that is still in flight', async () => {
    const store = await storeWith([
      task({ id: 'a', status: 'done', total_cost_usd: 1.25 }),
      task({ id: 'b', status: 'done', total_cost_usd: 0.004 }),
      // Neither of these bought the project a result, so neither is in the figure: one is
      // still running and would move the total under the reader, the other was abandoned.
      task({ id: 'c', status: 'in_progress', total_cost_usd: 9 }),
      task({ id: 'd', status: 'cancelled', total_cost_usd: 3 }),
    ])
    expect(store.doneCost).toBeCloseTo(1.254, 6)
  })

  it('is zero on a board that has finished nothing', async () => {
    const store = await storeWith([task({ status: 'todo' })])
    expect(store.doneCost).toBe(0)
  })

  it('keeps counting a task that was archived off the board', async () => {
    // Archiving takes the card off the board and nothing off the books (§10.4): the
    // columns draw `boardTasks`, the figure sums `tasks`, and the archive is its own list.
    const store = await storeWith([
      task({ id: 'a', status: 'done', total_cost_usd: 1.0 }),
      task({ id: 'b', status: 'done', total_cost_usd: 0.5, archived: true }),
      task({ id: 'c', status: 'todo' }),
    ])
    expect(store.doneCost).toBeCloseTo(1.5, 6)
    expect(store.boardTasks.map((entry) => entry.id)).toEqual(['a', 'c'])
    expect(store.archivedTasks.map((entry) => entry.id)).toEqual(['b'])
  })
})

describe('the activity line', () => {
  it('updates lifetime token totals for a closed task without adding repeated snapshots', async () => {
    const store = await storeWith([task()])
    const message = messageEvent({ role: 'system', kind: 'system', text: 'usage_update', data: {
      task_total_tokens: 100, task_total_uncached_tokens: 30, task_total_cached_tokens: 70,
      task_total_input_tokens: 20, task_total_output_tokens: 10,
    } })
    handlers.get('team.member.message')?.(message as never)
    handlers.get('team.member.message')?.(message as never)
    expect(store.openTask).toBeNull()
    expect(store.tasks[0]).toMatchObject({ total_tokens: 100, total_uncached_tokens: 30,
      total_cached_tokens: 70, total_input_tokens: 20, total_output_tokens: 10 })
  })

  it('counts Studio-managed subagents without an open transcript and closes only the finished child', async () => {
    const store = await storeWith([task()])
    const emit = (text: string, data: Record<string, unknown>, kind = 'system') =>
      handlers.get('team.member.message')?.(messageEvent({ role: 'system', kind, text, data }) as never)
    emit('task_started', { task_id: 'codex_1', task_type: 'studio_agent', backend: 'codex' })
    emit('task_started', { task_id: 'qwen_1', task_type: 'studio_agent', backend: 'qwen_code' })
    expect(store.openTask).toBeNull()
    expect(store.runningSubagents['task_1']).toBe(2)
    emit('', { continued: true }, 'result')
    expect(store.runningSubagents['task_1']).toBe(2)
    emit('task_notification', { task_id: 'codex_1', task_type: 'studio_agent', status: 'completed' })
    expect(store.runningSubagents['task_1']).toBe(1)
    emit('task_notification', { task_id: 'qwen_1', task_type: 'studio_agent', status: 'stopped' })
    expect(store.runningSubagents['task_1']).toBeUndefined()
  })

  it('follows a task whose transcript is not open', async () => {
    const store = await storeWith([task()])
    expect(store.openTask).toBeNull()

    handlers.get('team.member.message')?.(messageEvent() as never)

    expect(store.activity['task_1']).toEqual({ text: 'Bash · npm test', seq: 1 })
  })

  it('counts every update, so the same tool twice still pulses twice', async () => {
    const store = await storeWith([task()])
    const message = handlers.get('team.member.message')

    message?.(messageEvent() as never)
    message?.(messageEvent({ id: 'msg_2' }) as never)

    // Same text, different seq — the card keys its animation on the number, not the words.
    expect(store.activity['task_1']).toEqual({ text: 'Bash · npm test', seq: 2 })
  })

  it('says what the task dialog’s last line says, whatever arrived', async () => {
    // A card used to hold "Bash · npm test" through the call's result, a note and every frame
    // a sub-agent sent, while the open task's line moved on. Both are the tail of the whole
    // feed now, so they are compared after every message, the counter behind the flash too.
    const store = await storeWith([task()])
    const message = handlers.get('team.member.message')
    const feed = [
      messageEvent(),
      messageEvent({
        id: 'msg_2',
        role: 'system',
        kind: 'tool_result',
        text: '12 passed\n0 failed',
        data: {},
      }),
      messageEvent({ id: 'msg_3', role: 'user', kind: 'note', text: 'try again', data: {} }),
      messageEvent({
        id: 'msg_4',
        role: 'system',
        kind: 'system',
        text: 'task_progress',
        data: {
          task_id: 'agent_1',
          task_type: 'local_agent',
          description: 'Reading group 1',
          last_tool_name: 'Grep',
        },
      }),
      // Nothing to say: the line stays where it was and does not flash again.
      messageEvent({ id: 'msg_5', kind: 'thinking', text: '', data: {} }),
    ] as AgentMessage[]

    feed.forEach((event, index) => {
      message?.(event as never)
      const dialog = liveLine(feed.slice(0, index + 1), 'default')
      expect(store.activity['task_1']).toEqual({ text: dialog.activity, seq: dialog.seq })
    })
    expect(store.activity['task_1']).toEqual({
      text: 'sub-agent · Reading group 1 · Grep',
      seq: 4,
    })
  })

  it('is dropped when the session ends, so a finished card claims nothing', async () => {
    const store = await storeWith([task()])
    handlers.get('team.member.message')?.(messageEvent() as never)

    handlers.get('team.task.status_changed')?.({
      task_id: 'task_1',
      team_id: 'team_1',
      assignee_id: 'member_1',
      status: 'needs_review',
      error: null,
    } as never)

    expect(store.activity['task_1']).toBeUndefined()
  })

  it('survives a status change that leaves the task running', async () => {
    const store = await storeWith([task({ status: 'pending' })])
    handlers.get('team.member.message')?.(messageEvent() as never)

    handlers.get('team.task.status_changed')?.({
      task_id: 'task_1',
      team_id: 'team_1',
      assignee_id: 'member_1',
      status: 'in_progress',
      error: null,
    } as never)

    expect(store.activity['task_1']?.text).toBe('Bash · npm test')
  })
})
