import { computed, ref, shallowRef } from 'vue'

import {
  AiAccessError, aiAccessApi, type AiDevice, type AiEvent, type AiPermission, type AiRegistration,
  type AiRequest, type AiResponse, type AiTarget, type AiTargetEntry,
} from '@/api/ai-access'

const targetFields = ['server_instance_id', 'executor_registration_id', 'export_id', 'persistent_id',
  'storage_epoch', 'export_revision'] as const satisfies readonly (keyof AiTarget)[]
const targetKey = (target: AiTarget) => JSON.stringify(targetFields.map((key) => target[key]))
const entryKey = (entry: AiTargetEntry) => JSON.stringify([targetKey(entry.target), entry.grant.grant_id])
const sameTarget = (value: unknown, target: AiTarget) => object(value) && Object.keys(value).length === targetFields.length
  && targetFields.every((key) => value[key] === target[key])
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const bytes = (value: string) => new TextEncoder().encode(value).length
const invalid = () => new AiAccessError(200, 'invalid_response')
const codeOf = (cause: unknown) => cause instanceof AiAccessError ? cause.code : 'invalid_response'
const ambiguous = new Set(['network', 'outcome_unknown', 'invalid_response'])
const confirmed = new Set(['accepted', 'dispatching', 'running', 'completed', 'failed', 'cancelled', 'rejected', 'denied_before_dispatch'])
// Unexpected local HTTP failures are not evidence that a mutation never reached the executor.
const knownRejections = new Set(['denied', 'not_available', 'offline', 'stale_target', 'revision_conflict',
  'busy', 'expired', 'rate_limited', 'capacity_exceeded', 'invalid_request', 'auth_failure', 'disabled'])

export type AiResourceKind = 'task' | 'chat'
type Topic = 'status' | 'transcript'
type Action = 'start' | 'note' | 'send' | 'cancel'
type Availability = 'opening' | 'online' | 'offline' | 'stale' | 'revoked' | 'unavailable' | 'resync'
export interface AiResourceStatus {
  resource_id: string
  kind: AiResourceKind
  team_id: string
  title: string
  assignee_label: string
  status: string
  revision: number
  is_running: boolean
  waiting: boolean
  run_id: string | null
}
interface Watermark { stream_epoch: string; seq: number }
interface Subscription extends Watermark { id: string; topic: Topic }
interface Page<T> { items: T[]; next_cursor: string | null }
interface Message {
  id: string
  text: string
  role: string
  omitted: boolean
  next: number
  total: number
}
interface Chunk extends Message { offset: number }
interface ResourceScope { pin: AiPin; id: string; kind: AiResourceKind }
interface Command {
  id: string
  registrationId: string
  target: Readonly<AiTarget>
  op: string
}
export interface AiTargetRow {
  key: string
  entry: AiTargetEntry
  device: Readonly<AiDevice> | null
}
export interface AiPin {
  target: Readonly<AiTarget>
  registrationId: string
  url: string
  requester: Readonly<AiDevice>
  device: Readonly<AiDevice>
  executorEpoch: string
  grantId: string
  grantRevision: number
  expiresAt: number
  permissions: readonly AiPermission[]
  teams: readonly string[]
  members: readonly string[]
  resourceScope: AiTargetEntry['grant']['resource_scope']
  name: string
}

function identity(device: AiDevice): Readonly<AiDevice> {
  const { registration_id, client_id, user_id, display_name, kind } = device
  return Object.freeze({ registration_id, client_id, user_id, display_name, kind })
}

function status(value: unknown): AiResourceStatus {
  if (!object(value) || !nonempty(value.resource_id) || (value.kind !== 'task' && value.kind !== 'chat')
    || !nonempty(value.team_id) || typeof value.title !== 'string' || typeof value.assignee_label !== 'string'
    || !nonempty(value.status) || !integer(value.revision) || value.revision === 0
    || typeof value.is_running !== 'boolean' || typeof value.waiting !== 'boolean'
    || !(value.run_id === null || nonempty(value.run_id))) throw invalid()
  return {
    resource_id: value.resource_id, kind: value.kind, team_id: value.team_id, title: value.title,
    assignee_label: value.assignee_label, status: value.status, revision: value.revision,
    is_running: value.is_running, waiting: value.waiting, run_id: value.run_id,
  }
}

function watermark(result: Record<string, unknown>): Watermark {
  if (!nonempty(result.stream_epoch) || !integer(result.seq)) throw invalid()
  return { stream_epoch: result.stream_epoch, seq: result.seq }
}

function chunk(value: unknown, resourceId: string): Chunk {
  if (!object(value) || value.resource_id !== resourceId || !nonempty(value.message_id)
    || typeof value.text !== 'string' || typeof value.role !== 'string' || typeof value.content_omitted !== 'boolean'
    || !integer(value.total_bytes) || !integer(value.offset)) throw invalid()
  const next = value.offset + bytes(value.text)
  if (next > value.total_bytes || (value.content_omitted && (next !== 0 || value.text !== ''))
    || (!value.content_omitted && (value.total_bytes > 1024 * 1024 || (next === value.offset && next < value.total_bytes)))) throw invalid()
  return { id: value.message_id, text: value.text, role: value.role, omitted: value.content_omitted,
    offset: value.offset, next, total: value.total_bytes }
}

function page<T>(result: Record<string, unknown>, parse: (row: unknown) => T): Page<T> {
  if (!Array.isArray(result.items) || result.items.length > 50
    || !(result.next_cursor === null || nonempty(result.next_cursor))) throw invalid()
  return { items: result.items.map(parse), next_cursor: result.next_cursor }
}

export const validTitle = (value: string) => !!value.trim() && bytes(value.trim()) <= 480
export const validText = (value: string) => !!value.trim() && bytes(value) <= 16 * 1024
export const validPaths = (value: string[]) => value.length <= 64 && value.every((path) => bytes(path) <= 256)

/** One disposable, in-memory requester workspace. Not a local ProjectSession or a global task/chat store. */
export function createAiAccessStore() {
  const registrations = shallowRef<AiRegistration[]>([])
  const registrationId = ref('')
  const targets = shallowRef<AiTargetRow[]>([])
  const targetCursor = ref<string | null>(null)
  const directoryBusy = ref(false)
  const directoryError = ref('')
  const pin = shallowRef<AiPin | null>(null)
  const availability = ref<Availability>('opening')
  const actionError = ref('')
  const busy = ref(false)
  const resources = shallowRef<AiResourceStatus[]>([])
  const resourceCursor = ref<string | null>(null)
  const resource = shallowRef<AiResourceStatus | null>(null)
  const scope = shallowRef<ResourceScope | null>(null)
  const live = ref(false)
  const messages = shallowRef<Message[]>([])
  const transcriptCursor = ref<string | null>(null)
  const transcriptLimit = ref(false)
  const commandState = ref('')
  const cancellationRequested = ref(false)
  const created = shallowRef<{ id: string; kind: AiResourceKind } | null>(null)
  const receiptsReady = ref(false)
  const receiptError = ref('')
  const unknownCommands = shallowRef<Command[]>([])
  const owned = shallowRef<Set<string>>(new Set())
  const waitRevision = ref<number | null>(null)

  let disposed = false
  let browseGeneration = 0
  let workGeneration = 0
  let expiry: ReturnType<typeof setTimeout> | undefined
  let probe: ReturnType<typeof setTimeout> | undefined
  let stream: AbortController | undefined
  let transcriptSnapshot: Watermark | undefined
  // Subs are registered on the service before the HTTP reply reaches the browser.
  // Hold bounded frames during that window, then only apply an exact ID match.
  const subscriptions = new Map<string, Subscription>()
  let early: AiEvent[] | undefined

  const online = computed(() => pin.value !== null && availability.value === 'online')
  const permits = (permission: AiPermission) => pin.value?.permissions.includes(permission) ?? false
  const sameCommandTarget = (command: Command, current: AiPin) => command.registrationId === current.registrationId
    && sameTarget(command.target, current.target)
  const unknown = computed(() => pin.value ? unknownCommands.value.filter((command) => sameCommandTarget(command, pin.value!)) : [])
  const canWrite = computed(() => online.value && receiptsReady.value && !unknown.value.length && !busy.value)
  const canOperate = computed(() => !!resource.value && (permits('operate_existing')
    || pin.value?.resourceScope === 'created_by_this_grantee' || owned.value.has(resource.value.resource_id)))
  const canAction = (action: Action) => {
    const row = resource.value
    if (!row || !live.value || !canOperate.value || !online.value) return false
    if (action === 'cancel') return !!row.run_id && row.is_running
      && permits(row.kind === 'task' ? 'cancel_task' : 'cancel_chat')
    if (row.kind === 'task') return action === 'note' ? permits('write_task') : action === 'start'
      && permits('run_task') && !row.is_running && !row.waiting && ['todo', 'pending'].includes(row.status)
    return action === 'send' && permits('run_chat') && !row.is_running && ['idle', 'error'].includes(row.status)
  }

  function envelope(current: AiPin, op: string, payload: AiRequest['payload'], extra: Partial<AiRequest> = {}): AiRequest {
    return { ...extra, target: current.target, grant_id: current.grantId, grant_revision: current.grantRevision,
      expected_executor_epoch: current.executorEpoch, op, payload }
  }

  async function request(current: AiPin, op: string, payload: AiRequest['payload'], extra: Partial<AiRequest> = {}) {
    const response = await aiAccessApi.request(current.registrationId, envelope(current, op, payload, extra))
    if (!object(response) || !sameTarget(response.target, current.target) || response.command_id !== extra.command_id
      || (response.code !== undefined && !nonempty(response.code))) throw invalid()
    return response
  }

  async function read(current: AiPin, op: string, payload: AiRequest['payload']) {
    const response = await request(current, op, payload)
    if (response.code) throw new AiAccessError(200, response.code)
    if (response.state !== 'completed' || !object(response.result)) throw invalid()
    return response.result
  }

  function unsubscribe(current: AiPin, id: string) {
    // Cleanup only, using the original pin. Never reconnect by changing the epoch or grant.
    void aiAccessApi.request(current.registrationId, envelope(current, 'stream.unsubscribe', { subscription_id: id }))
      .catch(() => {})
  }

  function clearResource() {
    const previous = scope.value
    scope.value = null
    resource.value = null
    live.value = false
    stream?.abort()
    stream = undefined
    if (previous) for (const id of subscriptions.keys()) unsubscribe(previous.pin, id)
    subscriptions.clear()
    early = undefined
    messages.value = []
    transcriptSnapshot = undefined
    transcriptCursor.value = null
    transcriptLimit.value = false
    waitRevision.value = null
  }

  function clearTarget() {
    workGeneration++
    clearResource()
    clearTimeout(expiry)
    clearTimeout(probe)
    resources.value = []
    resourceCursor.value = null
    created.value = null
    owned.value = new Set()
    actionError.value = ''
    receiptError.value = ''
    receiptsReady.value = false
    commandState.value = ''
    cancellationRequested.value = false
    busy.value = false
  }

  function invalidate(reason: Availability) {
    clearTarget()
    availability.value = reason
    // A stale directory row cannot be accepted as a fresh explicit reopen.
    targets.value = []
    targetCursor.value = null
    browseGeneration++
    directoryBusy.value = false
  }

  function error(code: string, area: 'target' | 'list' | 'resource' = 'target') {
    if (['denied', 'auth_failure', 'disabled'].includes(code)) invalidate('revoked')
    else if (code === 'not_available') invalidate(area === 'target' ? 'revoked' : 'resync')
    else if (['network', 'offline'].includes(code)) invalidate('offline')
    else if (code === 'stale_target') invalidate('stale')
    else if (['invalid_response', 'resync_required'].includes(code)) invalidate('resync')
    else if (code === 'snapshot_changed' || code === 'revision_conflict') {
      if (area === 'resource') clearResource()
      else { resources.value = []; resourceCursor.value = null }
      actionError.value = 'snapshot_changed'
    } else actionError.value = code
  }

  function active(current: AiPin, generation = workGeneration) {
    if (disposed || pin.value !== current || generation !== workGeneration) return false
    if (Date.now() >= current.expiresAt) { invalidate('revoked'); return false }
    return true
  }

  function watchExpiry(current: AiPin, generation: number) {
    if (!active(current, generation)) return
    expiry = setTimeout(() => watchExpiry(current, generation), Math.min(2_147_483_647, current.expiresAt - Date.now()))
  }

  function watchAccess(current: AiPin, generation: number) {
    // The service only publishes subscription events; an idle resource list has no
    // stream. Reauthorize that visible snapshot periodically, never retarget it.
    if (!current.permissions.includes('view_status')) return
    probe = setTimeout(async () => {
      try {
        const result = await read(current, 'catalog.list', {})
        if (!sameTarget(result.target, current.target)) throw invalid()
        if (active(current, generation)) watchAccess(current, generation)
      } catch (cause) {
        if (active(current, generation)) {
          error(codeOf(cause))
          // Unlike an ordinary operation, an unsuccessful liveness probe cannot
          // leave an indefinitely cached snapshot visible.
          if (availability.value === 'online') invalidate('unavailable')
        }
      }
    }, 12_000)
  }

  async function fetchTargets(current: AiRegistration, generation: number, after?: string) {
    const [directory, result] = await Promise.all([
      aiAccessApi.directory(current.registration_id), aiAccessApi.targets(current.registration_id, after),
    ])
    if (generation !== browseGeneration || disposed) return
    if (!Array.isArray(directory) || !Array.isArray(result.targets)
      || !(result.next_after === null || (nonempty(result.next_after) && result.next_after !== after))) throw invalid()
    const devices = new Map<string, Readonly<AiDevice> | null>()
    for (const device of directory) {
      if (nonempty(device.registration_id) && nonempty(device.user_id) && nonempty(device.client_id)
        && typeof device.display_name === 'string' && ['studio', 'android'].includes(device.kind)) {
        devices.set(device.registration_id, devices.has(device.registration_id) ? null : identity(device))
      }
    }
    const executor = (reg: string) => {
      const device = devices.get(reg)
      return device?.kind === 'studio' ? device : null
    }
    const rows = after ? new Map(targets.value.map((row) => [row.key,
      { ...row, device: executor(row.entry.target.executor_registration_id) }])) : new Map<string, AiTargetRow>()
    for (const entry of result.targets) {
      const target = entry.target
      if (!sameTarget(target, target) || target.server_instance_id !== current.server_instance_id
        || !nonempty(target.executor_registration_id) || !nonempty(target.export_id)
        || !nonempty(target.persistent_id) || !nonempty(target.storage_epoch)
        || !integer(target.export_revision) || target.export_revision === 0 || typeof entry.display_name !== 'string'
        || (entry.state !== 'online' && entry.state !== 'offline')
        || (entry.state === 'online' ? !nonempty(entry.executor_epoch) : entry.executor_epoch !== null)
        || !nonempty(entry.grant.grant_id) || !integer(entry.grant.revision) || entry.grant.revision === 0
        || !integer(entry.grant.expires_at) || !Array.isArray(entry.grant.operations) || !entry.grant.operations.every(nonempty)
        || !Array.isArray(entry.grant.team_ids) || !entry.grant.team_ids.every(nonempty)
        || !Array.isArray(entry.grant.member_ids) || !entry.grant.member_ids.every(nonempty)
        || !['created_by_this_grantee', 'selected_resources', 'all_resources_in_selected_teams'].includes(entry.grant.resource_scope)) throw invalid()
      if (Date.now() >= entry.grant.expires_at) continue
      const key = entryKey(entry)
      const { display_name, state, executor_epoch, grant } = entry
      const { grant_id, revision, expires_at, operations, team_ids, member_ids, resource_scope } = grant
      const projected = { target: { ...target }, display_name, state, executor_epoch,
        grant: { grant_id, revision, expires_at, resource_scope, operations: [...operations], team_ids: [...team_ids], member_ids: [...member_ids] } }
      rows.set(key, { key, entry: projected, device: executor(target.executor_registration_id) })
    }
    targets.value = [...rows.values()]
    targetCursor.value = result.next_after
  }

  async function browse(id: string, reloadRegistrations = false, more = false) {
    if (disposed || (more && (!targetCursor.value || directoryBusy.value))) return
    const generation = ++browseGeneration
    const after = more ? targetCursor.value ?? undefined : undefined
    directoryBusy.value = true
    directoryError.value = ''
    if (!more) {
      targets.value = []
      targetCursor.value = null
      clearTarget()
      pin.value = null
      registrationId.value = id
    }
    try {
      if (reloadRegistrations) {
        const list = await aiAccessApi.registrations()
        if (generation !== browseGeneration || disposed) return
        registrations.value = list.filter((reg) => reg.has_device_token && reg.kind === 'studio')
      }
      const current = registrations.value.find((reg) => reg.registration_id === id)
      if (current) await fetchTargets(current, generation, after)
      else registrationId.value = ''
    } catch (cause) {
      if (generation !== browseGeneration || disposed) return
      targets.value = []
      targetCursor.value = null
      directoryError.value = codeOf(cause)
    } finally {
      if (generation === browseGeneration) directoryBusy.value = false
    }
  }

  function remember(command: Command) {
    if (!disposed && !unknownCommands.value.some((item) => item.id === command.id)) {
      unknownCommands.value = [...unknownCommands.value, command]
    }
  }
  function resolve(id: string) { unknownCommands.value = unknownCommands.value.filter((item) => item.id !== id) }

  function acceptReceipt(command: Command, state: string, result: unknown) {
    if (!confirmed.has(state)) return false
    const matched = object(result) && result.command_id === command.id && result.op === command.op && result.state === state
    if (state === 'completed' && (command.op === 'task.create' || command.op === 'chat.create')) {
      if (!matched || result.resource_kind !== command.op.split('.')[0] || !nonempty(result.resource_id)) return false
      owned.value = new Set([...owned.value, result.resource_id])
      return { id: result.resource_id, kind: result.resource_kind as AiResourceKind }
    }
    return matched || state === 'rejected' // Relay rejection can have no ledger result.
  }

  async function checkLocalReceipts(current: AiPin, generation: number) {
    try {
      const rows = await aiAccessApi.receipts()
      if (!active(current, generation)) return
      for (const row of rows) {
        const command = { id: row.command_id, registrationId: row.registration_id, target: row.target, op: row.op }
        if (!sameCommandTarget(command, current)) continue
        if (row.response?.command_id === command.id && sameTarget(row.response.target, current.target)
          && acceptReceipt(command, row.state, row.response.result)) resolve(command.id)
        else remember(command)
      }
      receiptsReady.value = true
      receiptError.value = ''
    } catch (cause) {
      if (active(current, generation)) { receiptsReady.value = false; receiptError.value = codeOf(cause) }
    }
  }

  async function list(current: AiPin, generation: number, cursor?: string) {
    const result = page(await read(current, 'resource.list', { ...(cursor ? { cursor } : {}) }), status)
    if (!active(current, generation)) return
    const rows = new Map((cursor ? resources.value : []).map((row) => [row.resource_id, row]))
    for (const row of result.items) {
      if (rows.has(row.resource_id) || !current.teams.includes(row.team_id)) throw invalid()
      rows.set(row.resource_id, row)
    }
    if (cursor && cursor === result.next_cursor) throw invalid()
    resources.value = [...rows.values()]
    resourceCursor.value = result.next_cursor
  }

  async function openTarget(key: string) {
    const row = targets.value.find((item) => item.key === key)
    const reg = registrations.value.find((item) => item.registration_id === registrationId.value)
    if (directoryBusy.value || busy.value || !row?.device || !reg || row.entry.state !== 'online'
      || !row.entry.executor_epoch || Date.now() >= row.entry.grant.expires_at) return
    const { entry } = row
    clearTarget()
    const current: AiPin = Object.freeze({ target: Object.freeze({ ...entry.target }),
      registrationId: reg.registration_id, requester: identity(reg), device: row.device, url: reg.url,
      executorEpoch: entry.executor_epoch!, grantId: entry.grant.grant_id, grantRevision: entry.grant.revision,
      expiresAt: entry.grant.expires_at, permissions: Object.freeze([...entry.grant.operations]),
      teams: Object.freeze([...entry.grant.team_ids]), members: Object.freeze([...entry.grant.member_ids]),
      resourceScope: entry.grant.resource_scope, name: entry.display_name,
    })
    pin.value = current
    availability.value = 'opening'
    const generation = workGeneration
    busy.value = true
    watchExpiry(current, generation)
    try {
      if (permits('view_status')) await list(current, generation)
      if (!active(current, generation)) return
      await checkLocalReceipts(current, generation)
      if (!active(current, generation)) return
      availability.value = 'online'
      watchAccess(current, generation)
    } catch (cause) {
      if (active(current, generation)) { invalidate('unavailable'); error(codeOf(cause), 'list') }
    } finally {
      if (generation === workGeneration) busy.value = false
    }
  }

  async function operate(work: (current: AiPin, generation: number) => Promise<void>, area: 'target' | 'list' | 'resource') {
    const current = pin.value
    const generation = workGeneration
    if (!current || !online.value || busy.value || !active(current, generation)) return
    busy.value = true
    actionError.value = ''
    try { await work(current, generation) }
    catch (cause) { if (active(current, generation)) error(codeOf(cause), area) }
    finally { if (generation === workGeneration) busy.value = false }
  }

  function loadResources(more = false) {
    if (!permits('view_status') || (more && !resourceCursor.value)) return
    return operate((current, generation) => list(current, generation, more ? resourceCursor.value! : undefined), 'list')
  }

  function addChunk(item: Chunk) {
    if (transcriptLimit.value) return
    const old = messages.value.find((message) => message.id === item.id)
    if ((!old && item.offset !== 0) || (old && (old.total !== item.total || old.omitted !== item.omitted || old.role !== item.role))) throw invalid()
    if (old && item.offset < old.next && item.next <= old.next) return // explicit chunk read overlapped SSE
    if (old && item.offset !== old.next) throw invalid()
    if ((!old && messages.value.length >= 512)
      || messages.value.reduce((sum, message) => sum + message.next, 0) + (item.next - item.offset) > 4 * 1024 * 1024) {
      transcriptLimit.value = true
      return
    }
    const next: Message = { id: item.id, role: item.role, omitted: item.omitted, total: item.total,
      next: item.next, text: old ? old.text + item.text : item.text }
    messages.value = old ? messages.value.map((message) => message === old ? next : message) : [...messages.value, next]
  }

  function eventFor(current: ResourceScope, event: AiEvent) {
    if (scope.value !== current || !active(current.pin)) return
    const data = event.data
    const sid = nonempty(data.subscription_id) ? data.subscription_id : undefined
    if (event.kind === 'resync_required' && sid === undefined) { invalidate('resync'); return }
    const sub = sid ? subscriptions.get(sid) : undefined
    if (!sub) {
      if (early && sid && (event.kind === 'resync_required'
        || (sameTarget(data.target, current.pin.target) && data.resource_id === current.id))) {
        if (early.length >= 32) invalidate('resync')
        else early.push(event)
      }
      return
    }
    if (event.kind === 'resync_required') { invalidate('resync'); return }
    if (!sameTarget(data.target, current.pin.target) || data.resource_id !== current.id) return
    if (data.stream_epoch !== sub.stream_epoch || !integer(data.seq) || data.seq > sub.seq + 1) {
      invalidate('resync'); return
    }
    if (data.seq <= sub.seq) return
    try {
      if (sub.topic === 'status') {
        const next = status(data.payload)
        if (next.resource_id !== current.id || next.kind !== current.kind || !current.pin.teams.includes(next.team_id)
          || next.revision !== data.resource_revision || next.revision < (resource.value?.revision ?? 0)) throw invalid()
        resource.value = next
        if (waitRevision.value !== null && next.revision > waitRevision.value) waitRevision.value = null
      } else addChunk(chunk(data.payload, current.id))
      sub.seq = data.seq
    } catch { invalidate('resync') }
  }

  async function startEvents(current: ResourceScope) {
    const controller = new AbortController()
    stream = controller
    await new Promise<void>((ready, reject) => {
      const timer = setTimeout(() => { reject(new AiAccessError(0, 'offline')); controller.abort() }, 10_000)
      const settle = () => clearTimeout(timer)
      controller.signal.addEventListener('abort', () => { settle(); reject(new AiAccessError(0, 'offline')) }, { once: true })
      void aiAccessApi.events(controller.signal, (event) => eventFor(current, event), () => { settle(); ready() })
        .then(() => { throw new AiAccessError(0, 'offline') })
        .catch((cause: unknown) => {
          settle()
          reject(cause)
          if (scope.value === current && !controller.signal.aborted) {
            error(codeOf(cause), 'resource')
            if (scope.value === current) invalidate('unavailable')
          }
        })
    })
  }

  async function subscribe(current: ResourceScope, topic: Topic, cursor: Watermark) {
    const buffer: AiEvent[] = []
    early = buffer
    try {
      const result = await read(current.pin, 'stream.subscribe', {
        resource_id: current.id, topic, stream_epoch: cursor.stream_epoch, after_seq: cursor.seq,
      })
      const ack = watermark(result)
      if (!nonempty(result.subscription_id)) throw invalid()
      if (scope.value !== current || !active(current.pin) || ack.seq !== cursor.seq || ack.stream_epoch !== cursor.stream_epoch) {
        unsubscribe(current.pin, result.subscription_id)
        if (scope.value === current) throw invalid()
        return
      }
      if (subscriptions.has(result.subscription_id)) throw invalid()
      subscriptions.set(result.subscription_id, { ...ack, id: result.subscription_id, topic })
      for (const event of buffer) if (event.data.subscription_id === result.subscription_id) eventFor(current, event)
    } finally { if (early === buffer) early = undefined }
  }

  async function transcript(current: ResourceScope, cursor?: string) {
    const result = await read(current.pin, 'transcript.page', { resource_id: current.id, ...(cursor ? { cursor } : {}) })
    if (scope.value !== current || !active(current.pin)) return
    const mark = watermark(result)
    if (cursor && (mark.stream_epoch !== transcriptSnapshot?.stream_epoch || mark.seq !== transcriptSnapshot.seq
      || cursor === result.next_cursor)) throw new AiAccessError(409, 'snapshot_changed')
    const parsed = page(result, (row) => chunk(row, current.id))
    for (const item of parsed.items) addChunk(item)
    transcriptSnapshot = mark
    transcriptCursor.value = parsed.next_cursor
    return mark
  }

  function openResource(id: string, kind: AiResourceKind) {
    if (!permits('view_status')) return
    return operate(async (current, generation) => {
      clearResource()
      const selected = Object.freeze({ pin: current, id, kind })
      scope.value = selected
      try {
        // No cached-row fallback, even when this ID was just returned by Create.
        const result = await read(current, 'resource.get', { resource_id: id })
        if (scope.value !== selected || !active(current, generation)) return
        const row = status(result)
        if (row.resource_id !== id || row.kind !== kind || !current.teams.includes(row.team_id)) throw invalid()
        await startEvents(selected)
        if (scope.value !== selected || !active(current, generation)) return
        await subscribe(selected, 'status', watermark(result))
        if (scope.value !== selected || !active(current, generation)) return
        // Don't overwrite replay that arrived before the subscribe HTTP ACK.
        resource.value ??= row
        if (permits('view_transcript')) {
          const mark = await transcript(selected)
          if (mark && scope.value === selected) await subscribe(selected, 'transcript', mark)
        }
        if (scope.value === selected && active(current, generation)) live.value = true
      } catch (cause) {
        if (scope.value === selected) { clearResource(); throw cause }
      }
    }, 'resource')
  }

  function moreTranscript() {
    const selected = scope.value
    if (!selected || !permits('view_transcript') || !transcriptCursor.value || transcriptLimit.value) return
    return operate(async () => { await transcript(selected, transcriptCursor.value!) }, 'resource')
  }

  function moreMessage(id: string) {
    const selected = scope.value
    const message = messages.value.find((item) => item.id === id)
    if (!selected || !permits('view_transcript') || transcriptLimit.value || !message || message.omitted || message.next >= message.total) return
    return operate(async (current, generation) => {
      const result = await read(current, 'transcript.page', { resource_id: selected.id, message_id: id, offset: message.next })
      if (scope.value !== selected || !active(current, generation)) return
      const mark = watermark(result)
      if (mark.stream_epoch !== transcriptSnapshot?.stream_epoch || mark.seq < transcriptSnapshot.seq) {
        throw new AiAccessError(409, 'snapshot_changed')
      }
      const item = chunk(result, selected.id)
      if (item.id !== id || item.offset !== message.next) throw invalid()
      addChunk(item)
    }, 'resource')
  }

  async function mutate(current: AiPin, generation: number, op: string, payload: AiRequest['payload'],
    preconditions: Partial<AiRequest> = {}) {
    const command = { id: crypto.randomUUID(), registrationId: current.registrationId, target: current.target, op }
    let response: AiResponse
    try {
      response = await request(current, op, payload, { ...preconditions, command_id: command.id, issued_at: Date.now() })
    } catch (cause) {
      const code = codeOf(cause)
      if (!knownRejections.has(code)) remember(command)
      throw cause
    }
    if (response.state === 'outcome_unknown' || (response.code && ambiguous.has(response.code))
      || (!confirmed.has(response.state) && !(response.code && knownRejections.has(response.code)))) remember(command)
    if (!active(current, generation)) return false
    if (response.code) throw new AiAccessError(200, response.code)
    const receipt = acceptReceipt(command, response.state, response.result)
    if (!receipt && response.state !== 'outcome_unknown') {
      remember(command)
      throw invalid()
    }
    if (receipt && typeof receipt === 'object') created.value = receipt
    commandState.value = response.state
    return ['accepted', 'dispatching', 'running', 'completed'].includes(response.state)
  }

  function create(kind: AiResourceKind, team: string, member: string, title: string, locale: string, brief = '', paths: string[] = []) {
    const current = pin.value
    if (!current || !canWrite.value || !permits(kind === 'task' ? 'create_task' : 'create_chat')
      || !current.teams.includes(team) || !current.members.includes(member) || !validTitle(title)
      || (kind === 'task' && (!validText(brief) || !validPaths(paths)))) return
    return operate(async (current, generation) => {
      commandState.value = ''
      cancellationRequested.value = false
      created.value = null
      await mutate(current, generation, `${kind}.create`, {
        title: title.trim(), team_id: team, locale,
        ...(kind === 'task' ? { assignee_id: member, brief, write_paths: paths } : { member_id: member }),
      })
    }, 'target')
  }

  function act(action: Action, text = '') {
    const row = resource.value
    const selected = scope.value
    if (!row || !selected || !canWrite.value || !canAction(action)
      || ((action === 'note' || action === 'send') && !validText(text))
      || (waitRevision.value !== null && (action === 'start' || action === 'send'))) return
    return operate(async (current, generation) => {
      const payload = { resource_id: selected.id, ...(['note', 'send'].includes(action) ? { text } : {}) }
      const preconditions = action === 'cancel' ? { expected_run_id: row.run_id! }
        : action === 'start' || action === 'send' ? { expected_resource_revision: row.revision } : {}
      commandState.value = ''
      cancellationRequested.value = false
      if (await mutate(current, generation, `${row.kind}.${action}`, payload, preconditions) && scope.value === selected) {
        if (action === 'cancel') cancellationRequested.value = true
        if ((action === 'start' || action === 'send') && (resource.value?.revision ?? 0) <= row.revision) {
          waitRevision.value = row.revision
        }
      }
    }, 'resource')
  }

  function checkReceipt(id: string) {
    const command = unknown.value.find((item) => item.id === id)
    if (!command) return
    return operate(async (current, generation) => {
      try {
        // This API hard-codes resend:false. Only a current, explicitly opened
        // executor generation for the exact original registration/export qualifies.
        const response = await aiAccessApi.recover(command.id, current.executorEpoch)
        if (!active(current, generation)) return
        if (!sameTarget(response.target, current.target) || response.command_id !== undefined) throw invalid()
        if (response.code) throw new AiAccessError(200, response.code)
        const result = response.result
        if (response.state !== 'completed' || !object(result) || result.command_id !== command.id || result.op !== command.op
          || !nonempty(result.state)) throw invalid()
        const receipt = acceptReceipt(command, result.state, result)
        if (receipt) resolve(command.id)
        if (receipt && typeof receipt === 'object') created.value = receipt
        commandState.value = result.state
      } catch (cause) {
        // A missing receipt is NOT proof that an attempt was never delivered.
        const code = codeOf(cause)
        if (['auth_failure', 'denied', 'disabled', 'stale_target', 'offline', 'network'].includes(code)) throw cause
        receiptError.value = code
      }
    }, 'target')
  }

  function dispose() {
    disposed = true
    browseGeneration++
    clearTarget()
    pin.value = null
    targets.value = []
    registrations.value = []
    unknownCommands.value = []
  }

  return { registrations, registrationId, targets, targetCursor, directoryBusy, directoryError,
    pin, availability, online, busy, actionError, resources, resourceCursor, resource, scope, live,
    messages, transcriptCursor, transcriptLimit, commandState, cancellationRequested, created,
    receiptsReady, receiptError, unknown, canWrite, waitRevision, permits, canAction,
    refresh: () => browse(registrationId.value, true), chooseRegistration: (id: string) => browse(id),
    moreTargets: () => browse(registrationId.value, false, true), openTarget, loadResources, openResource,
    closeResource: clearResource, moreTranscript, moreMessage, create, act, checkReceipt,
    reloadReceipts: () => operate(checkLocalReceipts, 'target'), dispose,
  }
}
