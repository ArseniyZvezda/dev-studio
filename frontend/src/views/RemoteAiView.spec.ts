// @vitest-environment jsdom
import { createApp, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { aiAccessApi, type AiEvent, type AiPermission, type AiTarget } from '@/api/ai-access'
import { t } from '@/i18n'
import RemoteAiView from './RemoteAiView.vue'

const target: AiTarget = { server_instance_id: 'remote-instance', executor_registration_id: 'executor-registration',
  export_id: 'export-copy', persistent_id: 'remote-project', storage_epoch: 'storage-generation', export_revision: 2 }
const device = { registration_id: target.executor_registration_id, user_id: 'executor-user', client_id: 'executor-client',
  display_name: 'Executor PC', kind: 'studio' }
const reg = { ...device, registration_id: 'source-registration', user_id: 'requester-user', client_id: 'requester-client',
  display_name: 'Requester PC', profile_id: 'profile', url: 'https://remote.test/prefix', server_instance_id: 'remote-instance',
  publication_enabled: false, has_device_token: true, revision: 5 }
const snapshot = { resource_id: 'remote-task', title: 'Fresh remote item', kind: 'task', status: 'todo', team_id: 'team',
  assignee_label: 'Runner', revision: 4, is_running: false, waiting: false, run_id: null }
const statusMark = { stream_epoch: 'status-feed', seq: 0 }
const transcriptMark = { stream_epoch: 'transcript-feed', seq: 0 }

let root: HTMLElement
let app: App
let emit: (event: AiEvent) => void
const spyRequest = () => vi.spyOn(aiAccessApi, 'request')
let request: ReturnType<typeof spyRequest>

beforeEach(() => {
  root = document.createElement('div')
  document.body.append(root)
  app = createApp(RemoteAiView)
  vi.spyOn(aiAccessApi, 'registrations').mockResolvedValue([reg])
  vi.spyOn(aiAccessApi, 'directory').mockResolvedValue([device])
  vi.spyOn(aiAccessApi, 'receipts').mockResolvedValue([])
  vi.spyOn(aiAccessApi, 'events').mockImplementation((signal, onEvent, onReady) => {
    emit = onEvent
    onReady?.()
    return new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  })
  request = spyRequest().mockImplementation(async (_reg, envelope) => {
    const { op, command_id } = envelope
    const result = op === 'resource.list' ? { items: [{ ...snapshot, title: 'List preview' }], next_cursor: null }
      : op === 'resource.get' ? { ...snapshot, ...statusMark }
        : op === 'stream.subscribe' ? envelope.payload.topic === 'status'
          ? { ...statusMark, subscription_id: 'status-sub' } : { ...transcriptMark, subscription_id: 'transcript-sub' }
          : op === 'transcript.page' ? { ...transcriptMark, next_cursor: null, items: [{ resource_id: 'remote-task',
            message_id: 'msg', text: '<img src=x onerror=evil>', offset: 0, total_bytes: 24, role: 'assistant', content_omitted: false }] }
            : op === 'task.create' || op === 'task.start' ? { command_id, op, resource_kind: 'task', resource_id: 'remote-task',
              state: op === 'task.start' ? 'accepted' : 'completed' } : {}
    return { target, state: op === 'task.start' ? 'accepted' : 'completed', ...(command_id ? { command_id } : {}), result } as never
  })
})
afterEach(() => { app.unmount(); root.remove(); vi.restoreAllMocks() })

const find = <T extends HTMLElement = HTMLElement>(id: string) => root.querySelector<T>(`[data-testid="${id}"]`)
const button = (id: string) => find<HTMLButtonElement>(id)!
const operations = () => request.mock.calls.map(([, envelope]) => envelope.op)
async function flush() { for (let i = 0; i < 20; i++) { await Promise.resolve(); await nextTick() } }
function input(id: string, value: string, event = 'input') {
  const el = find<HTMLInputElement>(id)!
  el.value = value
  el.dispatchEvent(new Event(event, { bubbles: true }))
}
async function open(permissions: AiPermission[], includeIdentity = true) {
  vi.spyOn(aiAccessApi, 'targets').mockResolvedValue({ targets: [{ target, display_name: 'Working copy', state: 'online',
    executor_epoch: 'executor-generation', grant: { grant_id: 'grant-id', revision: 6, operations: permissions, team_ids: ['team'],
      member_ids: ['agent'], expires_at: Date.now() + 120_000, resource_scope: 'created_by_this_grantee' } }], next_after: null })
  if (!includeIdentity) vi.mocked(aiAccessApi.directory).mockResolvedValue([{ ...device, registration_id: 'same-name-wrong-id' }])
  app.mount(root)
  await flush()
  input('requester-registration', reg.registration_id, 'change')
  await flush()
  button('open-target').click()
  await flush()
}

describe('RemoteAiView', () => {
  it('renders authenticated identity and status alone for a view_status grant', async () => {
    await open(['view_status'])
    expect(root.textContent).toContain('executor-user')
    expect(root.textContent).toContain('executor-client')
    expect(root.textContent).toContain('source-registration')
    expect(root.textContent).toContain('remote-instance')
    expect(root.textContent).toContain('storage-generation')
    expect(root.textContent).toContain('executor-generation')
    expect(find('create')).toBeNull()
    button('open-resource').click()
    await flush()
    expect(find('resource-detail')?.textContent).toContain('Fresh remote item')
    expect(find('resource-detail')?.textContent).not.toContain('List preview')
    expect(find('resource-detail')?.textContent).toContain(t('aiAccess.requester.transcriptUnavailable'))
    expect(find('start')).toBeNull()
    expect(find('submit-message')).toBeNull()
    expect(operations()).toEqual(['resource.list', 'resource.get', 'stream.subscribe'])
  })

  it('does not open when only a same-label device exists', async () => {
    await open(['view_status'], false)
    expect(button('open-target').disabled).toBe(true)
    expect(root.textContent).toContain(t('aiAccess.requester.unidentified'))
    expect(request).not.toHaveBeenCalled()
  })

  it('requires distinct UI gestures to create, open and start', async () => {
    await open(['view_status', 'create_task', 'run_task'])
    input('create-team', 'team', 'change')
    input('create-member', 'agent', 'change')
    input('create-title', 'Draft')
    input('create-brief', 'Explicitly create only')
    await flush()
    expect(button('create').disabled).toBe(false)
    button('create').form!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(operations()).toEqual(['resource.list', 'task.create'])
    expect(find('start')).toBeNull()
    expect(find('created-resource')?.textContent).toContain('remote-task')
    button('open-created').click()
    await flush()
    expect(operations()).toEqual(['resource.list', 'task.create', 'resource.get', 'stream.subscribe'])
    expect(button('start').disabled).toBe(false)
    button('start').click()
    await flush()
    expect(operations()).toEqual(['resource.list', 'task.create', 'resource.get', 'stream.subscribe', 'task.start'])
    expect(request.mock.lastCall?.[1]).toMatchObject({ target, grant_id: 'grant-id', grant_revision: 6,
      expected_executor_epoch: 'executor-generation', expected_resource_revision: 4 })
    expect(button('start').disabled).toBe(true)
  })

  it('renders transcript only as plain text and removes it and controls when that subscription ends', async () => {
    await open(['view_status', 'view_transcript', 'run_task'])
    button('open-resource').click()
    await flush()
    expect(find('transcript')?.textContent).toContain('<img src=x onerror=evil>')
    expect(find('transcript')?.querySelector('img')).toBeNull()
    expect(find('start')).not.toBeNull()
    emit({ kind: 'resync_required', data: { subscription_id: 'transcript-sub' } })
    await flush()
    expect(find('transcript')).toBeNull()
    expect(find('resource-detail')).toBeNull()
    expect(find('start')).toBeNull()
    expect(find('open-target')).toBeNull()
    expect(find('availability')?.textContent).toContain(t('aiAccess.requester.resync'))
  })
})
