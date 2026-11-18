/** Studio's global Remote boundary (remote-identity/studio-design §5.1/§6).
 * No Remote transport, credential storage, selection, cache or implicit Verify here.
 */
import { authHeaders } from './auth'
import { ApiError } from './client'
import { scopedApiUrl, type Query } from './project-scope'
import { ProjectSession, rendererSession, StaleProjectResponseError } from '@/session/project-session'
import { t } from '@/i18n'

export interface RemoteUser {
  id: string
  display_name: string
  color: string
}

export interface VerificationDTO {
  state: 'valid' | 'unauthorised' | 'offline' | 'incompatible' | 'insecure_blocked' | 'error'
  checked_at: string
  user: RemoteUser | null
  wire_protocol: number | null
  catalog_version: number | null
  error_code: string | null
}

export type VerificationResult = VerificationDTO
export type RemoteVerification = VerificationResult
  | { state: 'unchecked' } | { state: 'checking' } | { state: 'unavailable' }

export interface RemoteServerProfile {
  profile_id: string
  name: string
  url: string
  revision: number
  has_token: boolean
  credential_source: 'stored' | 'environment' | 'none'
  verification: RemoteVerification
}

export interface RemoteServers { items: RemoteServerProfile[] }
export interface RemoteProject {
  id: string
  display_name: string
  storage_epoch: string
  metadata_revision: number
  created_at: string
}
export interface RemoteProjects { items: RemoteProject[]; next_cursor: string | null }
export type RemoteCreateUnknownCode = 'create_unknown' | 'transport_unavailable' | 'invalid_response'
  | 'version_mismatch' | 'redirect_refused' | 'temporarily_unavailable' | 'remote_error'
export type RemoteCreateRejectedCode = 'invalid_id' | 'invalid_display_name' | 'invalid_request'
  | 'unauthorised' | 'project_not_found' | 'metadata_conflict' | 'snapshot_changed'
  | 'payload_too_large' | 'remote_error'
export type RemoteCreateResult =
  | { state: 'created' | 'found'; project: RemoteProject; error_code: null }
  | { state: 'conflict'; project: null; error_code: 'project_exists' }
  | { state: 'unknown'; project: null; error_code: RemoteCreateUnknownCode }
  | { state: 'rejected'; project: null; error_code: RemoteCreateRejectedCode }
  | { state: 'absent'; project: null; error_code: 'project_not_found' }
export type RemoteObserveResult = Extract<RemoteCreateResult, { state: 'absent' }>
  | { state: 'found'; project: RemoteProject; error_code: null }
export interface RemoteError { code: string; details: Record<string, string> }

// Separate intents: no composite PATCH or implicit confirmation/split/retry (§5.1).
export interface CreateRemoteProfileInput { name: string; url: string }
export interface RemoteRevisionInput { expected_revision: number }
export interface CreateRemoteProjectInput extends RemoteRevisionInput {
  id: string
  display_name: string
  operation_id: string
}
export interface RenameRemoteProfileInput extends RemoteRevisionInput { name: string }
export interface ChangeRemoteProfileUrlInput extends RemoteRevisionInput { url: string; clear_credential: true }
export interface RemoteCredentialInput extends RemoteRevisionInput { token: string }
export interface RecoverRemoteProfileInput extends RemoteRevisionInput {
  confirmation: { profile_id: string; action: 'discard_uncertain_credential' }
}
export interface RecoveredRemoteProfile { profile: RemoteServerProfile | null }

/** Gate L census (dev spec §16): projects this Studio knows use a profile/URL.
 * `known` only after the user explicitly confirmed this exact URL and list; never inferred.
 */
export type RemoteProfileConsumerOperation = 'verify' | 'read' | 'create'
export interface RemoteProfileConsumers {
  profile_id: string
  url: string
  revision: number
  state: 'known' | 'unknown'
  confirmed_at: string | null
  bound_roots: string[]
  active_roots: string[]
  operations: RemoteProfileConsumerOperation[]
}
/** Exactly the URL and project list shown to the user; the backend refuses a changed inventory. */
export interface ConfirmRemoteProfileConsumersInput extends RemoteRevisionInput {
  url: string
  bound_roots: string[]
}

/** Preserve the local router's error envelope, including Studio 401 semantics. */
export class RemoteApiError extends ApiError implements RemoteError {
  constructor(status: number, readonly code: string, readonly details: Record<string, string> = {}) {
    super(status, code)
    this.name = 'RemoteApiError'
  }
}

const canonicalProfileId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/
function profilePath(profileId: string): string {
  if (typeof profileId !== 'string' || !canonicalProfileId.test(profileId)) {
    throw new RemoteApiError(422, 'invalid_request')
  }
  return `/remote/servers/${profileId}`
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return Object.keys(value).length === fields.length
    && fields.every(field => Object.prototype.hasOwnProperty.call(value, field))
}

const canonicalProjectId = /^[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])/
const operationId = /^[A-Za-z0-9._-]{1,128}(?![\s\S])/
function usableOperationId(value: unknown): value is string {
  // Dot segments cannot be addressed by the exact observe URL. Reject before
  // reserving on Create too; never encode, replace or generate another ID.
  return typeof value === 'string' && operationId.test(value) && value !== '.' && value !== '..'
}
const createUnknownCodes = new Set<string>([
  'create_unknown', 'transport_unavailable', 'invalid_response', 'version_mismatch',
  'redirect_refused', 'temporarily_unavailable', 'remote_error',
])
const createRejectedCodes = new Set<string>([
  'invalid_id', 'invalid_display_name', 'invalid_request', 'unauthorised', 'project_not_found',
  'metadata_conflict', 'snapshot_changed', 'payload_too_large', 'remote_error',
])

/** Catalog metadata is literal, including migrated labels (studio-design §6). */
function project(value: unknown, expectedId?: string): RemoteProject {
  if (!object(value) || !exactFields(value, ['id', 'display_name', 'storage_epoch', 'metadata_revision', 'created_at'])
    || typeof value.id !== 'string' || !canonicalProjectId.test(value.id)
    || (expectedId !== undefined && value.id !== expectedId)
    || typeof value.display_name !== 'string'
    || typeof value.storage_epoch !== 'string' || !/^[0-9a-f]{32}(?![\s\S])/.test(value.storage_epoch)
    || !Number.isSafeInteger(value.metadata_revision) || (value.metadata_revision as number) < 1
    || typeof value.created_at !== 'string' || !value.created_at) {
    throw new RemoteApiError(502, 'invalid_response')
  }
  return value as unknown as RemoteProject
}

/** C0/C9: outcomes are not boundary exceptions, ownership or permission to retry. */
function createResult(value: unknown, expectedId?: string, observation = false): RemoteCreateResult {
  if (object(value) && exactFields(value, ['state', 'project', 'error_code'])) {
    if ((value.state === 'created' && !observation || value.state === 'found') && value.error_code === null) {
      return { state: value.state, project: project(value.project, expectedId), error_code: null }
    }
    if (value.project === null && typeof value.error_code === 'string') {
      if (value.state === 'absent' && value.error_code === 'project_not_found') {
        return { state: 'absent', project: null, error_code: 'project_not_found' }
      }
      if (!observation && (value.state === 'conflict' && value.error_code === 'project_exists'
        || value.state === 'unknown' && createUnknownCodes.has(value.error_code)
        || value.state === 'rejected' && createRejectedCodes.has(value.error_code))) {
        return value as unknown as RemoteCreateResult
      }
    }
  }
  throw new RemoteApiError(502, 'invalid_response')
}

// Mirror VerificationDTO's sanitized_status vocabulary in backend/api/remote.py.
const verificationErrorCodes = new Set([
  'invalid_request', 'invalid_id', 'profile_not_found', 'profile_revision_conflict',
  'credential_binding_conflict', 'profile_recovery_required', 'credential_required',
  'invalid_credential', 'profile_store_unavailable', 'application_store_unavailable',
  'credential_store_unavailable', 'metadata_unavailable', 'invalid_profile_metadata',
  'invalid_response', 'invalid_server_url', 'remote_unauthorised', 'insecure_blocked',
  'transport_unavailable', 'catalog_unavailable', 'version_mismatch', 'capabilities_missing',
  'redirect_refused', 'remote_error', 'temporarily_unavailable', 'verification_failed',
  'invalid_clock', 'service_unavailable',
  'profile_url_collision', 'profile_in_use', 'profile_consumers_not_drained',
  'credential_clear_confirmation_required', 'profile_recovery_confirmation_conflict',
  'consumer_lifecycle_unavailable', 'profile_recovery_unavailable', 'profile_committed_metadata_unavailable',
  'invalid_display_name', 'invalid_operation_id', 'invalid_intent', 'operation_not_found',
  'operation_conflict', 'operation_in_progress', 'credential_binding_unproven',
  'intent_store_unavailable', 'intent_store_invalid', 'invalid_intent_path', 'invalid_intent_mode',
  'invalid_catalog_readers',
  'identity_mismatch', 'project_not_found', 'metadata_conflict', 'snapshot_changed', 'payload_too_large',
])

// Gate L envelope codes accepted from the local router (never inside a VerificationDTO).
const consumerErrorCodes = new Set(['consumer_inventory_changed', 'consumer_confirmation_unavailable'])

function stringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string')
}
function consumers(value: unknown, expectedId: string): RemoteProfileConsumers {
  if (!object(value) || !exactFields(value, [
    'profile_id', 'url', 'revision', 'state', 'confirmed_at', 'bound_roots', 'active_roots', 'operations',
  ]) || value.profile_id !== expectedId || !profileUrl(value.url)
    || !Number.isSafeInteger(value.revision) || (value.revision as number) < 1
    || !(value.state === 'known' || value.state === 'unknown')
    || !(value.confirmed_at === null || (typeof value.confirmed_at === 'string' && !!value.confirmed_at))
    || (value.state === 'known' && value.confirmed_at === null)
    || !stringList(value.bound_roots) || !stringList(value.active_roots)
    || !stringList(value.operations) || !value.operations.every(item => ['verify', 'read', 'create'].includes(item))) {
    throw new RemoteApiError(502, 'invalid_response')
  }
  return {
    profile_id: value.profile_id, url: value.url, revision: value.revision as number, state: value.state,
    confirmed_at: value.confirmed_at as string | null, bound_roots: [...value.bound_roots],
    active_roots: [...value.active_roots], operations: [...value.operations] as RemoteProfileConsumerOperation[],
  }
}

function checkedVerification(value: unknown): VerificationDTO {
  if (!object(value) || !exactFields(value, [
    'state', 'checked_at', 'user', 'wire_protocol', 'catalog_version', 'error_code',
  ]) || !['valid', 'unauthorised', 'offline', 'incompatible', 'insecure_blocked', 'error'].includes(value.state as string)
    || typeof value.checked_at !== 'string'
    || !(value.user === null || (object(value.user) && exactFields(value.user, ['id', 'display_name', 'color'])
      && typeof value.user.id === 'string' && typeof value.user.display_name === 'string' && typeof value.user.color === 'string'))
    || !(value.wire_protocol === null || (typeof value.wire_protocol === 'number' && Number.isInteger(value.wire_protocol)))
    || !(value.catalog_version === null || (typeof value.catalog_version === 'number' && Number.isInteger(value.catalog_version)))
    || !(value.error_code === null || (typeof value.error_code === 'string' && verificationErrorCodes.has(value.error_code)))) {
    throw new RemoteApiError(502, 'invalid_response')
  }
  return value as unknown as VerificationDTO
}

function verification(value: unknown): RemoteVerification {
  // Unknown history requires explicit unavailable, never a default (metadata contract §4).
  if (object(value) && exactFields(value, ['state'])) {
    switch (value.state) {
      case 'unchecked': case 'checking': case 'unavailable': return { state: value.state }
    }
  }
  return checkedVerification(value)
}

function revisionInput(value: unknown, fields: readonly string[]): asserts value is RemoteRevisionInput {
  if (!object(value) || !exactFields(value, fields)
    || !Number.isSafeInteger(value.expected_revision) || (value.expected_revision as number) < 1) {
    throw new RemoteApiError(422, 'invalid_request')
  }
}

function profileUrl(value: unknown): value is string {
  // Backend output is already normalized http(s), never a bare host/userinfo/query/fragment.
  // Validate without rewriting the committed URL (or making any request to it).
  if (typeof value !== 'string') return false
  const authority = value.split('/')[2]
  // WHATWG URL repairs a missing authority (https:///host); the backend does not.
  if (!/^https?:\/\//.test(value) || !authority || /[?#]/.test(value)
    || value !== value.trim() || /[@\\\s]/.test(authority)) return false
  try {
    const url = new URL(value)
    return !!url.hostname && !url.username && !url.password
  } catch { return false }
}

/** Strict, secret-free mutation response. Historical labels are intentionally not trimmed. */
function serverProfile(value: unknown): RemoteServerProfile {
  if (!object(value) || !exactFields(value, [
    'profile_id', 'name', 'url', 'revision', 'has_token', 'credential_source', 'verification',
  ]) || typeof value.profile_id !== 'string' || !canonicalProfileId.test(value.profile_id)
    || typeof value.name !== 'string' || !profileUrl(value.url)
    || !Number.isSafeInteger(value.revision) || (value.revision as number) < 1
    || typeof value.has_token !== 'boolean'
    || !['stored', 'environment', 'none'].includes(value.credential_source as string)
    || value.has_token !== (value.credential_source !== 'none')) {
    throw new RemoteApiError(502, 'invalid_response')
  }
  return { ...value, verification: verification(value.verification) } as unknown as RemoteServerProfile
}

function remoteRequest(session: ProjectSession) {
  async function request<T>(method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE', path: string,
    query?: Query, body?: unknown, expectedStatus?: 200 | 201 | 204 | 'create'): Promise<T> {
    const current = session.capture()
    try {
      let response: Response
      try {
        response = await fetch(scopedApiUrl(session, path, query), {
          method,
          headers: { ...authHeaders(), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
          cache: 'no-store',
          body: body === undefined ? undefined : JSON.stringify(body),
        })
      } catch (cause) {
        current()
        if (cause instanceof StaleProjectResponseError) throw cause
        // A failed credential request must not echo a fetch implementation's raw input.
        throw new ApiError(0, expectedStatus !== undefined ? t('api.networkError')
          : cause instanceof Error ? cause.message : t('api.networkError'))
      }
      current()
      if (response.ok && expectedStatus !== undefined && (expectedStatus === 'create'
        ? ![200, 201].includes(response.status) : response.status !== expectedStatus)) {
        throw new RemoteApiError(502, 'invalid_response')
      }
      // DELETE profile is exactly 204, not a JSON/null envelope. Still fenced by finally.
      if (expectedStatus === 204 && response.status === 204) return undefined as T
      let payload: unknown
      try { payload = await response.json() }
      catch {
        current()
        throw new RemoteApiError(response.ok ? 502 : response.status, 'invalid_response')
      }
      current()
      if (!response.ok) {
        if (object(payload) && typeof payload.code === 'string' && object(payload.details)
          && (expectedStatus === undefined || (exactFields(payload, ['code', 'details'])
            && (verificationErrorCodes.has(payload.code) || consumerErrorCodes.has(payload.code)
              || ['studio_unauthorised', 'studio_forbidden', 'method_not_allowed'].includes(payload.code))))
          && Object.values(payload.details).every(value => typeof value === 'string')) {
          throw new RemoteApiError(response.status, payload.code, payload.details as Record<string, string>)
        }
        throw new RemoteApiError(response.status, 'invalid_response')
      }
      if (expectedStatus === 'create' && response.status !== (object(payload) && payload.state === 'created' ? 201 : 200)) {
        throw new RemoteApiError(502, 'invalid_response')
      }
      return payload as T
    } finally { current() }
  }
  return request
}

export function createRemoteApiClient(session: ProjectSession = rendererSession) {
  const request = remoteRequest(session)

  return {
    async createProject(profileId: string, input: CreateRemoteProjectInput): Promise<RemoteCreateResult> {
      const path = profilePath(profileId)
      revisionInput(input, ['id', 'display_name', 'operation_id', 'expected_revision'])
      if (typeof input.id !== 'string' || !canonicalProjectId.test(input.id)
        || typeof input.display_name !== 'string' || !usableOperationId(input.operation_id)) {
        throw new RemoteApiError(422, 'invalid_request')
      }
      // Capture the original ID before the await; caller mutation cannot rebind the response.
      const expectedId = input.id
      return createResult(await request<unknown>('POST', `${path}/projects`, undefined, input, 'create'), expectedId)
    },
    async observe(profileId: string, operation: string, input: RemoteRevisionInput): Promise<RemoteObserveResult> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision'])
      if (!usableOperationId(operation)) {
        throw new RemoteApiError(422, 'invalid_request')
      }
      // Only the backend's reservation knows the original project ID. No client journal or implicit GET.
      return createResult(await request<unknown>('POST', `${path}/projects/operations/${operation}/observe`,
        undefined, input, 200), undefined, true) as RemoteObserveResult
    },
    async createProfile(input: CreateRemoteProfileInput): Promise<RemoteServerProfile> {
      if (!object(input) || !exactFields(input, ['name', 'url'])
        || typeof input.name !== 'string' || typeof input.url !== 'string') {
        throw new RemoteApiError(422, 'invalid_request')
      }
      return serverProfile(await request<unknown>('POST', '/remote/servers', undefined, input, 201))
    },
    async renameProfile(profileId: string, input: RenameRemoteProfileInput): Promise<RemoteServerProfile> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision', 'name'])
      if (typeof input.name !== 'string') throw new RemoteApiError(422, 'invalid_request')
      return serverProfile(await request<unknown>('PATCH', path, undefined, input, 200))
    },
    async changeProfileUrl(profileId: string, input: ChangeRemoteProfileUrlInput): Promise<RemoteServerProfile> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision', 'url', 'clear_credential'])
      if (typeof input.url !== 'string' || input.clear_credential !== true) throw new RemoteApiError(422, 'invalid_request')
      return serverProfile(await request<unknown>('PATCH', path, undefined, input, 200))
    },
    async setCredential(profileId: string, input: RemoteCredentialInput): Promise<RemoteServerProfile> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision', 'token'])
      if (typeof input.token !== 'string' || !input.token.trim()) throw new RemoteApiError(422, 'invalid_request')
      return serverProfile(await request<unknown>('PUT', `${path}/credential`, undefined, input, 200))
    },
    async clearCredential(profileId: string, input: RemoteRevisionInput): Promise<RemoteServerProfile> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision'])
      return serverProfile(await request<unknown>('DELETE', `${path}/credential`, undefined, input, 200))
    },
    async deleteProfile(profileId: string, input: RemoteRevisionInput): Promise<void> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision'])
      return request<void>('DELETE', path, undefined, input, 204)
    },
    async recoverProfile(profileId: string, input: RecoverRemoteProfileInput): Promise<RecoveredRemoteProfile> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision', 'confirmation'])
      if (!object(input.confirmation) || !exactFields(input.confirmation, ['profile_id', 'action'])
        || typeof input.confirmation.profile_id !== 'string' || !canonicalProfileId.test(input.confirmation.profile_id)
        || typeof input.confirmation.action !== 'string') throw new RemoteApiError(422, 'invalid_request')
      const result = await request<unknown>('POST', `${path}/recover`, undefined, input, 200)
      if (!object(result) || !exactFields(result, ['profile'])) throw new RemoteApiError(502, 'invalid_response')
      return { profile: result.profile === null ? null : serverProfile(result.profile) }
    },
    async listServers(): Promise<RemoteServers> {
      const result = await request<unknown>('GET', '/remote/servers')
      if (!object(result) || !exactFields(result, ['items']) || !Array.isArray(result.items)) {
        throw new RemoteApiError(502, 'invalid_response')
      }
      return { items: result.items.map(profile => {
        if (!object(profile)) throw new RemoteApiError(502, 'invalid_response')
        return { ...profile, verification: verification(profile.verification) } as unknown as RemoteServerProfile
      }) }
    },
    async verify(profileId: string, expectedRevision: number): Promise<VerificationResult> {
      const path = profilePath(profileId)
      if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) throw new RemoteApiError(422, 'invalid_request')
      // POST's envelope is only a checked DTO, never a metadata-only state.
      return checkedVerification(await request<unknown>('POST', `${path}/verify`, undefined, { expected_revision: expectedRevision }))
    },
    async listProjects(profileId: string, cursor?: string | null): Promise<RemoteProjects> {
      return request<RemoteProjects>('GET', `${profilePath(profileId)}/projects`, { cursor })
    },
  }
}

/** Gate L census client (dev spec §16), separate from profile intents: reading it never
 * confirms, and confirming is a distinct explicit call with exactly what the user saw.
 */
export function createRemoteConsumersClient(session: ProjectSession = rendererSession) {
  const request = remoteRequest(session)
  return {
    /** Read-only census; never confirms anything by itself. */
    async profileConsumers(profileId: string): Promise<RemoteProfileConsumers> {
      const path = profilePath(profileId)
      return consumers(await request<unknown>('GET', `${path}/consumers`, undefined, undefined, 200), profileId)
    },
    /** Only after the user's explicit confirmation of exactly this URL and list. */
    async confirmProfileConsumers(profileId: string, input: ConfirmRemoteProfileConsumersInput): Promise<RemoteProfileConsumers> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision', 'url', 'bound_roots'])
      if (typeof input.url !== 'string' || !input.url || !stringList(input.bound_roots)) {
        throw new RemoteApiError(422, 'invalid_request')
      }
      // Capture before the await: later caller mutation cannot change what was confirmed.
      const body = { expected_revision: input.expected_revision, url: input.url, bound_roots: [...input.bound_roots] }
      return consumers(await request<unknown>('POST', `${path}/consumers/confirmation`, undefined, body, 200), profileId)
    },
    async withdrawProfileConsumers(profileId: string, input: RemoteRevisionInput): Promise<RemoteProfileConsumers> {
      const path = profilePath(profileId)
      revisionInput(input, ['expected_revision'])
      return consumers(await request<unknown>('DELETE', `${path}/consumers/confirmation`, undefined,
        { expected_revision: input.expected_revision }, 200), profileId)
    },
  }
}
export type RemoteConsumersClient = ReturnType<typeof createRemoteConsumersClient>
