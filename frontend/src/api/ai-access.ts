/** Local browser boundary for Studio Remote AI (ACP/1). No Remote credentials or project selection. */
import { authHeaders } from './auth'

export interface AiDevice {
  registration_id: string
  user_id: string
  client_id: string
  display_name: string
  kind: string
}

export interface AiRegistration extends AiDevice {
  profile_id: string
  url: string
  server_instance_id: string
  publication_enabled: boolean
  revision: number
  has_device_token: boolean
}

export interface AiTarget {
  server_instance_id: string
  executor_registration_id: string
  export_id: string
  persistent_id: string
  storage_epoch: string
  export_revision: number
}

export type AiPermission = 'view_status' | 'view_transcript' | 'create_task' | 'edit_task' | 'run_task'
  | 'write_task' | 'cancel_task' | 'create_chat' | 'run_chat' | 'cancel_chat' | 'operate_existing'
export type AiResourceScope = 'created_by_this_grantee' | 'selected_resources' | 'all_resources_in_selected_teams'

interface AiGrantScope {
  operations: AiPermission[]
  team_ids: string[]
  member_ids: string[]
  expires_at: number
}

export interface AiTargetEntry {
  target: AiTarget
  display_name: string
  state: 'online' | 'offline'
  executor_epoch: string | null
  grant: AiGrantScope & { grant_id: string; revision: number; resource_scope: AiResourceScope }
}

export interface AiExport {
  target: AiTarget
  enabled: boolean
  display_name: string
  mirror_state: string
}

export interface AiExportInput {
  registration_id: string
  profile_id: string
  server_instance_id: string
  project_id: string
  project_epoch: string
  persistent_id: string
  storage_epoch: string
  identity_revision: number
  display_name: string
}

export interface AiPolicy {
  target: AiTarget
  team_id: string
  member_id: string
  execution_policy_digest: string
  backend: string
  permission_mode: string
  tools: { backend_native_tools_may_include_shell: boolean }
  mcp: { strict: boolean; project_server_count: number }
  limits: {
    max_turns: number | null
    max_run_continuations: number
    project_agent_slots: number
    remote_runs_per_export: number
    remote_runs_per_grant: number
  }
}

export interface AiGrantInput extends Omit<AiGrantScope, 'expires_at'> {
  grantee_user_id: string
  grantee_registration_id: string
  expected_export_revision: number
  execution_policy_digest: string
  resource_scope?: AiResourceScope
  resource_ids?: string[]
  expires_at?: number
}

export type AiGrantUpdate = Omit<AiGrantInput, 'grantee_user_id' | 'grantee_registration_id'> & {
  expected_revision: number
}

export interface AiGrant extends Omit<AiGrantInput, 'expected_export_revision'> {
  grant_id: string
  revision: number
  target: AiTarget
  issued_at: number
  expires_at: number
  revoked_at: number | null
  resource_scope: AiResourceScope
  resource_ids: string[]
}

export interface AiGrantReceipt {
  grant: AiGrant
  acked_revision: number | null
}

export interface AiRequest {
  target: AiTarget
  grant_id: string
  grant_revision: number
  expected_executor_epoch: string
  op: string
  payload: Record<string, unknown>
  command_id?: string
  issued_at?: number
  expected_resource_revision?: number
  expected_run_id?: string
}

export interface AiResponse<T = Record<string, unknown>> {
  target: AiTarget
  state: string
  command_id?: string
  code?: string
  result?: T
}

export interface AiReceipt {
  registration_id: string
  command_id: string
  op: string
  target: AiTarget
  saved_at: number
  expires_at: number
  state: string
  response: AiResponse | null
}

export interface AiEvent {
  kind: 'remote' | 'resync_required'
  data: Record<string, unknown>
}

export class AiAccessError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(code)
    this.name = 'AiAccessError'
  }
}

const prefix = '/api/ai-access'
function id(value: string): string {
  if (!value || value === '.' || value === '..') throw new AiAccessError(422, 'invalid_request')
  return encodeURIComponent(value)
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

async function failure(response: Response): Promise<AiAccessError> {
  const body: unknown = await response.json().catch(() => null)
  const detail = record(body) ? body.detail : null
  const code = record(detail) ? detail.code : detail
  return new AiAccessError(response.status,
    typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : 'remote_error')
}

async function localFetch(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(prefix + path, {
      ...init, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { ...init.headers, ...authHeaders() },
    })
  } catch (cause) {
    if (init.signal?.aborted) throw cause
    throw new AiAccessError(0, 'network')
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await localFetch(path, {
    method, headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) throw await failure(response)
  if (response.status === 204) return undefined as T
  return await response.json().catch(() => { throw new AiAccessError(response.status, 'invalid_response') }) as T
}

// One local fetch, no redirects, no URL credentials and no automatic reconnection. A reconnect
// needs a fresh snapshot and an explicit new subscription; an aborted read must release the reader.
async function events(signal: AbortSignal, onEvent: (event: AiEvent) => void, onReady?: () => void): Promise<void> {
  const response = await localFetch('/events', { method: 'GET', signal, headers: { Accept: 'text/event-stream' } })
  if (!response.ok) throw await failure(response)
  if (!response.body || response.headers.get('content-type')?.split(';')[0]?.trim() !== 'text/event-stream') {
    throw new AiAccessError(response.status, 'invalid_response')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const activityLimit = 30_000 // The server sends a heartbeat every 15s on idle.
  const frameLimit = 128 * 1024 // A projected ACP frame fits in 64 KiB before the local SSE wrapper.
  let kind = ''
  let data = ''
  let tail = ''
  const cancel = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', cancel, { once: true })
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    if (signal.aborted) return
    onReady?.()
    while (!signal.aborted) {
      const chunk = await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new AiAccessError(0, 'offline')), activityLimit)
        }),
      ]).finally(() => clearTimeout(timer))
      if (chunk.done || signal.aborted) break
      try {
        tail += decoder.decode(chunk.value, { stream: true })
      } catch {
        throw new AiAccessError(200, 'invalid_response')
      }
      let newline: number
      while ((newline = tail.indexOf('\n')) !== -1) {
        if (newline > frameLimit) throw new AiAccessError(200, 'invalid_response')
        const line = tail.slice(0, newline).replace(/\r$/, '')
        tail = tail.slice(newline + 1)
        if (line.startsWith('event:')) kind = line.slice(6).trim()
        else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '')
        else if (line === '') {
          if (kind === 'remote' || kind === 'resync_required') {
            let event: unknown
            try { event = JSON.parse(data) } catch { throw new AiAccessError(200, 'invalid_response') }
            if (!record(event)) throw new AiAccessError(200, 'invalid_response')
            onEvent({ kind, data: event })
          }
          kind = data = ''
        }
        if (data.length + kind.length > frameLimit) throw new AiAccessError(200, 'invalid_response')
        if (signal.aborted) break
      }
      if (tail.length > frameLimit) throw new AiAccessError(200, 'invalid_response')
    }
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', cancel)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

const reg = (value: string) => `/registrations/${id(value)}`
const exp = (value: string) => `/exports/${id(value)}`

export const aiAccessApi = {
  capabilities: (profileId: string) => call<{ kind: string; code: string | null; server_instance_id: string | null }>(
    'GET', `/profiles/${id(profileId)}/capabilities`),
  registrations: async () => (await call<{ registrations: AiRegistration[] }>('GET', '/registrations')).registrations,
  register: (body: { profile_id: string; server_instance_id: string; display_name: string }) =>
    call<AiRegistration>('POST', '/registrations', body),
  unregister: (registrationId: string, expected_revision: number) => call<void>('DELETE', reg(registrationId), { expected_revision }),
  publication: (registrationId: string, enabled: boolean, expected_revision: number) =>
    call<AiRegistration>('PUT', `${reg(registrationId)}/publication`, { enabled, expected_revision }),
  directory: async (registrationId: string) =>
    (await call<{ registrations: AiDevice[] }>('GET', `${reg(registrationId)}/directory`)).registrations,
  targets: (registrationId: string, after?: string) => call<{ targets: AiTargetEntry[]; next_after: string | null }>(
    'GET', `${reg(registrationId)}/targets${after === undefined ? '' : `?after=${id(after)}`}`),
  exports: async () => (await call<{ exports: AiExport[] }>('GET', '/exports')).exports,
  createExport: (body: AiExportInput) => call<AiExport>('POST', '/exports', body),
  publish: (exportId: string, enabled: boolean, expected_revision: number) =>
    call<AiExport>('PUT', `${exp(exportId)}/publication`, { enabled, expected_revision }),
  policy: (exportId: string, teamId: string, memberId: string) =>
    call<AiPolicy>('GET', `${exp(exportId)}/teams/${id(teamId)}/members/${id(memberId)}/execution-policy`),
  grants: async (exportId: string) => (await call<{ grants: AiGrantReceipt[] }>('GET', `${exp(exportId)}/grants`)).grants,
  createGrant: (exportId: string, body: AiGrantInput) => call<AiGrantReceipt>('POST', `${exp(exportId)}/grants`, body),
  updateGrant: (exportId: string, grantId: string, body: AiGrantUpdate) =>
    call<AiGrantReceipt>('PUT', `${exp(exportId)}/grants/${id(grantId)}`, body),
  revoke: (grantId: string, expected_revision: number) => call<AiGrantReceipt>('DELETE', `/grants/${id(grantId)}`, { expected_revision }),
  request: <T = Record<string, unknown>>(registration_id: string, request: AiRequest) =>
    call<AiResponse<T>>('POST', '/requests', { registration_id, request }),
  receipts: async () => (await call<{ receipts: AiReceipt[] }>('GET', '/receipts')).receipts,
  recover: (commandId: string, executor_epoch: string) =>
    call<AiResponse>('POST', `/receipts/${id(commandId)}/recover`, { executor_epoch, resend: false }),
  events,
}
