import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AiAccessError, aiAccessApi, type AiEvent, type AiPermission, type AiTarget, type AiTargetEntry } from '@/api/ai-access'
import { createAiAccessStore } from './ai-access'

const target: AiTarget = { server_instance_id: 'server', executor_registration_id: 'pc-1', export_id: 'export-1',
  persistent_id: 'shared-project', storage_epoch: 'storage-1', export_revision: 3 }
const other = { ...target, executor_registration_id: 'pc-2', export_id: 'export-2' }
const permissions: AiPermission[] = ['view_status', 'view_transcript', 'create_task', 'run_task', 'write_task',
  'cancel_task', 'create_chat', 'run_chat', 'cancel_chat']
const device = (registration_id: string) => ({ registration_id, user_id: `${registration_id}-user`, client_id: `${registration_id}-client`,
  display_name: 'Same label', kind: 'studio' })
const registration = { ...device('requester'), profile_id: 'profile', url: 'https://remote.test/prefix', server_instance_id: 'server',
  publication_enabled: false, revision: 1, has_device_token: true }
const entry = (t = target, operations = permissions): AiTargetEntry => ({ target: { ...t }, display_name: 'Same export label', state: 'online',
  executor_epoch: 'generation-1', grant: { grant_id: `grant-${t.export_id}`, revision: 8, resource_scope: 'created_by_this_grantee',
    team_ids: ['team'], member_ids: ['agent'], operations: [...operations], expires_at: Date.now() + 120_000 } })
const row = (resource_id = 'task-1', kind = 'task', status = 'todo') => ({ resource_id, kind, status, team_id: 'team',
  title: 'GET title', assignee_label: 'Agent', revision: 5, is_running: false, waiting: false, run_id: null })
const chunk = (message_id: string, text: string, offset = 0, total_bytes = new TextEncoder().encode(text).length) => ({
  message_id, resource_id: 'task-1', role: 'assistant', text, offset, total_bytes, content_omitted: false,
})
const statusMark = { stream_epoch: 'status-epoch', seq: 2 }
const transcriptMark = { stream_epoch: 'transcript-epoch', seq: 10 }

let store: ReturnType<typeof createAiAccessStore>
let emit: (event: AiEvent) => void
let signal: AbortSignal
let targets: AiTargetEntry[]
const spyRequest = () => vi.spyOn(aiAccessApi, 'request')
let request: ReturnType<typeof spyRequest>

beforeEach(() => {
  store = createAiAccessStore()
  targets = [entry(), entry(other)]
  vi.spyOn(aiAccessApi, 'registrations').mockResolvedValue([registration])
  vi.spyOn(aiAccessApi, 'directory').mockResolvedValue([device('pc-2'), device('pc-1')])
  vi.spyOn(aiAccessApi, 'targets').mockImplementation(async () => ({ targets, next_after: null }))
  vi.spyOn(aiAccessApi, 'receipts').mockResolvedValue([])
  vi.spyOn(aiAccessApi, 'events').mockImplementation((abort, event, ready) => {
    emit = event; signal = abort; ready?.()
    return new Promise<void>((resolve) => abort.addEventListener('abort', () => resolve(), { once: true }))
  })
  request = spyRequest().mockImplementation(async (_registration, envelope) => {
    const { target, op, payload, command_id } = envelope
    const complete = (result: Record<string, unknown>, state = 'completed') => ({ target, state, ...(command_id ? { command_id } : {}), result })
    switch (op) {
      case 'resource.list': return complete({ items: payload.cursor ? [row('chat-1', 'chat', 'idle')]
        : [{ ...row(), title: 'Only a list row' }], next_cursor: payload.cursor ? null : 'resource-cursor' }) as never
      case 'resource.get': return complete({ ...statusMark, ...(payload.resource_id === 'chat-1' ? row('chat-1', 'chat', 'idle') : row()) }) as never
      case 'transcript.page': return complete({ ...transcriptMark, ...(payload.message_id ? chunk('snapshot-msg', 'x', 2, 3)
        : payload.cursor ? { items: [chunk('older', 'p2')], next_cursor: null }
          : { items: [chunk('snapshot-msg', 'é', 0, 3)], next_cursor: 'transcript-cursor' }) }) as never
      case 'stream.subscribe': return complete(payload.topic === 'status' ? { ...statusMark, subscription_id: 'status-sub' }
        : { ...transcriptMark, subscription_id: 'transcript-sub' }) as never
      case 'stream.unsubscribe': return complete({}) as never
      case 'catalog.list': return complete({ target }) as never
      default: {
        const state = op === 'task.start' || op === 'chat.send' ? 'accepted' : 'completed'
        return complete({ command_id, op, state, resource_kind: op.split('.')[0],
          resource_id: op === 'task.create' ? 'task-1' : op === 'chat.create' ? 'chat-1' : payload.resource_id }, state) as never
      }
    }
  })
})

afterEach(() => { store.dispose(); vi.useRealTimers(); vi.restoreAllMocks() })

async function open(index = 0) {
  await store.refresh()
  await store.chooseRegistration('requester')
  await store.openTarget(store.targets.value[index].key)
}
function operations() { return request.mock.calls.map(([, op]) => op.op) }
function remote(data: Record<string, unknown>) { emit({ kind: 'remote', data }) }

describe('remote workspace store', () => {
  it('joins exact device identity, freezes the six-field/grant/epoch address and pages without switching it', async () => {
    await open()
    const pin = store.pin.value!
    expect(pin.device).toEqual(device('pc-1'))
    expect(Object.isFrozen(pin)).toBe(true)
    expect(Object.isFrozen(pin.target)).toBe(true)
    targets[0].target.storage_epoch = 'new-storage'
    targets[0].grant.revision = 99
    targets[0].executor_epoch = 'new-generation'
    await store.loadResources(true)
    expect(store.resources.value.map((item) => item.resource_id)).toEqual(['task-1', 'chat-1'])
    expect(request).toHaveBeenLastCalledWith('requester', { target, expected_executor_epoch: 'generation-1',
      grant_id: 'grant-export-1', grant_revision: 8, op: 'resource.list', payload: { cursor: 'resource-cursor' } })
    expect(store.pin.value).toBe(pin)
    expect(store.registrationId.value).toBe('requester')
  })

  it('refuses an unidentified or offline target and does not use a same-name device', async () => {
    vi.mocked(aiAccessApi.directory).mockResolvedValue([device('pc-2')])
    targets[1] = { ...targets[1], state: 'offline', executor_epoch: null }
    await open()
    await store.openTarget(store.targets.value[1].key)
    expect(store.pin.value).toBeNull()
    expect(request).not.toHaveBeenCalled()
  })

  it('requires resource.get and never uses the list when GET rejects access', async () => {
    await open()
    request.mockResolvedValueOnce({ target, state: 'rejected', code: 'not_available' })
    await store.openResource('task-1', 'task')
    expect(operations()).toEqual(['resource.list', 'resource.get'])
    expect(store.resource.value).toBeNull()
    expect(store.resources.value).toEqual([])
    expect(store.availability.value).toBe('resync')
    expect(aiAccessApi.events).not.toHaveBeenCalled()
  })

  it('creates without executing, then starts/sends only by explicit separate commands with GET revisions', async () => {
    targets = [entry(target, permissions.filter((p) => p !== 'view_transcript'))]
    await open()
    await store.create('task', 'team', 'agent', 'Remote draft', 'en', 'Brief', ['docs/a.md'])
    expect(operations()).toEqual(['resource.list', 'task.create'])
    expect(store.created.value).toEqual({ id: 'task-1', kind: 'task' })
    expect(store.resource.value).toBeNull()
    const draft = request.mock.lastCall![1]
    expect(draft.payload).toEqual({ title: 'Remote draft', team_id: 'team', assignee_id: 'agent',
      brief: 'Brief', write_paths: ['docs/a.md'], locale: 'en' })
    await store.openResource('task-1', 'task')
    expect(store.resource.value?.title).toBe('GET title')
    await store.act('start')
    const run = request.mock.lastCall![1]
    expect(run).toMatchObject({ op: 'task.start', payload: { resource_id: 'task-1' }, expected_resource_revision: 5 })
    expect(run.command_id).not.toBe(draft.command_id)
    expect(store.waitRevision.value).toBe(5)
    await store.create('chat', 'team', 'agent', 'Remote chat', 'ru')
    const chat = request.mock.lastCall![1]
    expect(chat).toMatchObject({ op: 'chat.create', payload: { title: 'Remote chat', team_id: 'team', member_id: 'agent', locale: 'ru' } })
    expect(operations()).not.toContain('chat.send')
    await store.openResource('chat-1', 'chat')
    await store.act('send', 'Run once')
    expect(request.mock.lastCall![1]).toMatchObject({ op: 'chat.send', expected_resource_revision: 5, payload: { resource_id: 'chat-1', text: 'Run once' } })
  })

  it('applies a UTF-8 byte chunk and exact SSE events only, then clears data on a matching revocation/resync marker', async () => {
    await open()
    await store.openResource('task-1', 'task')
    expect(store.messages.value[0].text).toBe('é')
    await store.moreMessage('snapshot-msg')
    expect(request.mock.lastCall![1].payload).toEqual({ resource_id: 'task-1', message_id: 'snapshot-msg', offset: 2 })
    await store.moreTranscript()
    expect(store.messages.value.map((item) => item.text)).toEqual(['éx', 'p2'])
    const event = { ...transcriptMark, subscription_id: 'transcript-sub', target, resource_id: 'task-1', seq: 11,
      resource_revision: 5, payload: chunk('new', 'new text') }
    remote({ ...event, subscription_id: 'unrelated' })
    remote({ ...event, target: other })
    remote({ ...event, resource_id: 'unrelated' })
    expect(store.messages.value).toHaveLength(2)
    remote(event)
    remote(event)
    expect(store.messages.value.map((item) => item.text)).toEqual(['éx', 'p2', 'new text'])
    emit({ kind: 'resync_required', data: { subscription_id: 'unrelated' } })
    expect(store.live.value).toBe(true)
    emit({ kind: 'resync_required', data: { subscription_id: 'transcript-sub' } })
    expect(store.availability.value).toBe('resync')
    expect(store.messages.value).toEqual([])
    expect(store.resource.value).toBeNull()
    expect(store.targets.value).toEqual([])
    expect(signal.aborted).toBe(true)
    expect(store.canWrite.value).toBe(false)
    expect(operations().filter((op) => op === 'stream.subscribe')).toHaveLength(2)
  })

  it('treats a sequence gap as a new-snapshot requirement, not a replay or automatic reconnect', async () => {
    await open()
    await store.openResource('task-1', 'task')
    remote({ ...statusMark, seq: 4, resource_revision: 5, target, resource_id: 'task-1', subscription_id: 'status-sub', payload: row() })
    expect(store.availability.value).toBe('resync')
    expect(store.resource.value).toBeNull()
    expect(aiAccessApi.events).toHaveBeenCalledTimes(1)
  })

  it('separates note from run and sends cancel only with a matching live run ID', async () => {
    await open()
    await store.openResource('task-1', 'task')
    await store.act('note', 'Not a run')
    expect(request.mock.lastCall![1]).toMatchObject({ op: 'task.note', payload: { resource_id: 'task-1', text: 'Not a run' } })
    await store.act('cancel')
    expect(operations()).not.toContain('task.cancel')
    remote({ ...statusMark, seq: 3, resource_revision: 6, target, resource_id: 'task-1', subscription_id: 'status-sub',
      payload: { ...row(), revision: 6, status: 'in_progress', is_running: true, run_id: 'run-1' } })
    await store.act('cancel')
    expect(request.mock.lastCall![1]).toMatchObject({ op: 'task.cancel', expected_run_id: 'run-1', payload: { resource_id: 'task-1' } })
    expect(store.cancellationRequested.value).toBe(true)
  })

  it('clears on actual deny and does not use an old directory to reopen', async () => {
    await open()
    const previous = store.targets.value[0].key
    request.mockResolvedValueOnce({ target, state: 'rejected', code: 'denied' })
    await store.loadResources()
    expect(store.availability.value).toBe('revoked')
    expect(store.resources.value).toEqual([])
    await store.openTarget(previous)
    expect(operations()).toEqual(['resource.list', 'resource.list'])
  })

  it('fences unknown outcomes across reopen, checks that original receipt only, never resends', async () => {
    await open()
    await store.openResource('task-1', 'task')
    request.mockRejectedValueOnce(new AiAccessError(0, 'network'))
    await store.act('start')
    expect(store.availability.value).toBe('offline')
    const [command] = store.unknown.value
    expect(command.op).toBe('task.start')
    expect(store.resource.value).toBeNull()
    await open(1)
    expect(store.unknown.value).toEqual([])
    await open()
    expect(store.unknown.value[0].id).toBe(command.id)
    expect(store.canWrite.value).toBe(false)
    const recover = vi.spyOn(aiAccessApi, 'recover').mockResolvedValueOnce({ target, state: 'rejected', code: 'not_available' })
      .mockResolvedValueOnce({ target, state: 'completed', result: { command_id: command.id, op: 'task.start', state: 'completed' } })
    await store.checkReceipt(command.id)
    expect(store.unknown.value).toHaveLength(1)
    expect(recover).toHaveBeenLastCalledWith(command.id, 'generation-1')
    await store.checkReceipt(command.id)
    expect(store.unknown.value).toEqual([])
    expect(store.canWrite.value).toBe(true)
    expect(operations().filter((value) => value === 'task.start')).toHaveLength(1)
  })
})
