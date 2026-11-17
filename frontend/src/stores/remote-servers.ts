/** Renderer-local metadata only (remote-identity/studio-design §5.1, §10.1).
 * Forms call credential/mutation APIs directly: never pass their inputs through Pinia.
 * A ticket is captured BEFORE that call; only its secret-free confirmed DTO comes back.
 */
import { defineStore } from 'pinia'
import { onScopeDispose, ref } from 'vue'
import { ApiError } from '@/api/client'
import { createRemoteApiClient, type RemoteServerProfile, type RemoteVerification,
  type VerificationDTO } from '@/api/remote'
import { ProjectSession, rendererSession } from '@/session/project-session'

export interface CheckedRemoteServer {
  profile_id: string
  revision: number
  url: string
  result: VerificationDTO
}

/** Deliberately no raw message, exception, request, response or credential details. */
export interface RemoteServersError { kind: 'request_failed'; status: number | null }
declare const mutationTicket: unique symbol
export interface RemoteServerMutationTicket { readonly [mutationTicket]: true }
type Binding = Pick<RemoteServerProfile, 'profile_id' | 'revision' | 'url'>
type Mutation = { binding: Binding | null; current: () => void }
let definitionSequence = 0 // Definition identity only; never renderer data/session state.

function same(a: Binding, b: Binding): boolean {
  return a.profile_id === b.profile_id && a.revision === b.revision && a.url === b.url
}

// Project only known DTO fields, without retaining caller-owned object references.
function copyVerification(value: RemoteVerification): RemoteVerification {
  if (!('checked_at' in value)) return { state: value.state }
  return {
    state: value.state, checked_at: value.checked_at,
    user: value.user === null ? null : {
      id: value.user.id, display_name: value.user.display_name, color: value.user.color,
    },
    wire_protocol: value.wire_protocol, catalog_version: value.catalog_version, error_code: value.error_code,
  }
}

function copyProfile(value: RemoteServerProfile): RemoteServerProfile {
  // The GET client validates verification, but its metadata rows are not runtime-checked.
  // Reject the entire list rather than admitting partial/invalid profiles or object keys.
  if (typeof value?.profile_id !== 'string'
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/.test(value.profile_id)
    || typeof value.name !== 'string' || !Number.isSafeInteger(value.revision) || value.revision < 1
    || typeof value.url !== 'string' || value.url !== value.url.trim()
    || !/^https?:\/\/[^/\s@\\?#]+(?:\/[^?#]*)?$/.test(value.url)
    || typeof value.has_token !== 'boolean'
    || !['stored', 'environment', 'none'].includes(value.credential_source)
    || value.has_token !== (value.credential_source !== 'none')) throw new Error('Invalid metadata')
  const url = new URL(value.url)
  if (!url.hostname || url.username || url.password) throw new Error('Invalid metadata')
  return {
    profile_id: value.profile_id, revision: value.revision, url: value.url, name: value.name,
    has_token: value.has_token, credential_source: value.credential_source,
    verification: copyVerification(value.verification),
  }
}

function failure(cause?: unknown): RemoteServersError {
  return { kind: 'request_failed', status: cause instanceof ApiError ? cause.status : null }
}

/** One definition per renderer/Pinia root; no module-level mutable cache or persistence.
 * The session is immutable; this store does not select, bind or invalidate projects.
 */
export function createRemoteServersStore(session: ProjectSession = rendererSession) {
  return defineStore(`remote-servers-${++definitionSequence}`, () => {
    const api = createRemoteApiClient(session)
    const items = ref<RemoteServerProfile[]>([])
    const loaded = ref(false)
    const loading = ref(false)
    const error = ref<RemoteServersError | null>(null)
    const lastChecked = ref<Record<string, CheckedRemoteServer>>({})
    const busy = ref<Record<string, 'verify' | 'mutation'>>({})
    const errors = ref<Record<string, RemoteServersError>>({})
    let disposed = false
    let generation = 0
    let loadSequence = 0
    const verifies = new Map<string, object>()
    const mutations = new Map<RemoteServerMutationTicket, Mutation>()
    const profileMutations = new Map<string, RemoteServerMutationTicket>()
    const deleted = new Set<string>()

    const find = (id: string) => items.value.find(item => item.profile_id === id)
    function live(current: () => void): boolean {
      if (disposed) return false
      try { current(); return true } catch { return false }
    }
    function capture(): (() => void) | null {
      if (disposed) return null
      try { return session.capture() } catch { return null }
    }
    function rememberChecked(profile: RemoteServerProfile, explicit = false): void {
      const previous = lastChecked.value[profile.profile_id]
      if (previous && !same(previous, profile)) delete lastChecked.value[profile.profile_id]
      if ('checked_at' in profile.verification && (explicit || !previous || !same(previous, profile)
        || !Number.isFinite(Date.parse(previous.result.checked_at))
        || Date.parse(profile.verification.checked_at) >= Date.parse(previous.result.checked_at))) {
        lastChecked.value[profile.profile_id] = {
          profile_id: profile.profile_id, revision: profile.revision, url: profile.url,
          result: copyVerification(profile.verification) as VerificationDTO,
        }
      } else if ('checked_at' in profile.verification && previous && same(previous, profile)) {
        // A stale checked summary must not turn a newer failed check green again.
        // Metadata-only unavailable/unchecked/checking remains exactly that, not valid.
        profile.verification = copyVerification(previous.result)
      }
    }
    function forget(id: string): void {
      items.value = items.value.filter(item => item.profile_id !== id)
      deleted.add(id)
      verifies.delete(id)
      delete lastChecked.value[id]
      delete busy.value[id]
      delete errors.value[id]
    }

    /** Explicit metadata GET, never Verify. False means failed or superseded, not empty success.
     * Intervening local operations fence the whole snapshot; caller may explicitly load again.
     */
    async function load(): Promise<boolean> {
      const current = capture()
      if (!current) return false
      const sequence = ++loadSequence
      const start = generation
      const pending = mutations.size > 0
      loading.value = true
      error.value = null
      const applicable = () => live(current) && sequence === loadSequence
        && start === generation && !pending && mutations.size === 0
      try {
        const result = await api.listServers()
        if (!applicable()) return false
        // Never downgrade a known revision, even if a later GET returns an older snapshot.
        const profiles = result.items.map(copyProfile)
        if (new Set(profiles.map(item => item.profile_id)).size !== profiles.length) {
          throw new Error('Duplicate metadata')
        }
        const incoming = profiles.filter(item => !deleted.has(item.profile_id))
        const ids = new Set(incoming.map(item => item.profile_id))
        for (const old of items.value) if (!ids.has(old.profile_id)) forget(old.profile_id)
        items.value = incoming.map(item => {
          const old = find(item.profile_id)
          if (old && (old.revision > item.revision
            || (old.revision === item.revision && old.url !== item.url))) return old
          if (old && !same(old, item)) {
            verifies.delete(item.profile_id)
            delete busy.value[item.profile_id]
            delete errors.value[item.profile_id]
          }
          rememberChecked(item)
          return item
        })
        loaded.value = true
        generation += 1
        return true
      } catch (cause) {
        if (applicable()) error.value = failure(cause)
        return false
      } finally {
        if (live(current) && sequence === loadSequence) loading.value = false
      }
    }

    /** Verify uses only the already-saved profile/revision. Never accepts a credential. */
    async function verify(profileId: string): Promise<boolean> {
      const current = capture()
      const profile = find(profileId)
      if (!current || !profile || deleted.has(profileId) || profileMutations.has(profileId)) return false
      const binding: Binding = { profile_id: profileId, revision: profile.revision, url: profile.url }
      const request = {}
      verifies.set(profileId, request)
      generation += 1
      busy.value[profileId] = 'verify'
      delete errors.value[profileId]
      const applicable = () => live(current) && verifies.get(profileId) === request
        && !!find(profileId) && same(binding, find(profileId)!) && !deleted.has(profileId)
      try {
        const result = await api.verify(profileId, binding.revision)
        if (!applicable()) return false
        const next = copyProfile({ ...find(profileId)!, verification: result })
        items.value = items.value.map(item => item.profile_id === profileId ? next : item)
        rememberChecked(next, true) // Request order wins even if the backend clock moved back.
        generation += 1
        return true
      } catch (cause) {
        if (applicable()) { errors.value[profileId] = failure(cause); generation += 1 }
        return false
      } finally {
        if (applicable()) { verifies.delete(profileId); delete busy.value[profileId] }
      }
    }

    /** Capture before a direct form API call. Omit id only for keyless Create.
     * Null means DO NOT dispatch: same-profile mutations are serialized, not retried.
     * Otherwise a later failed CAS could hide an earlier confirmed deletion.
     */
    function beginMutation(profileId?: string): RemoteServerMutationTicket | null {
      const current = capture()
      const profile = profileId === undefined ? undefined : find(profileId)
      if (!current || (profileId !== undefined
        && (!profile || deleted.has(profileId) || profileMutations.has(profileId)))) return null
      const ticket = Object.freeze({}) as RemoteServerMutationTicket
      const binding = profile ? { profile_id: profile.profile_id, revision: profile.revision, url: profile.url } : null
      if (binding) {
        profileMutations.set(binding.profile_id, ticket)
        verifies.delete(binding.profile_id)
        busy.value[binding.profile_id] = 'mutation'
        delete errors.value[binding.profile_id]
      }
      mutations.set(ticket, { binding, current })
      generation += 1
      return ticket
    }
    function applicableMutation(ticket: RemoteServerMutationTicket): Mutation | null {
      const op = mutations.get(ticket)
      if (!op || !live(op.current)) return null
      if (op.binding && (profileMutations.get(op.binding.profile_id) !== ticket
        || !find(op.binding.profile_id) || !same(op.binding, find(op.binding.profile_id)!)
        || deleted.has(op.binding.profile_id))) return null
      return op
    }
    function retire(ticket: RemoteServerMutationTicket, op: Mutation): void {
      mutations.delete(ticket)
      if (op.binding && profileMutations.get(op.binding.profile_id) === ticket) {
        profileMutations.delete(op.binding.profile_id)
        delete busy.value[op.binding.profile_id]
      }
      generation += 1
    }
    /** Accept only a confirmed, secret-free API DTO, not form inputs or promises. */
    function applyMutation(ticket: RemoteServerMutationTicket, result: RemoteServerProfile): boolean {
      const op = applicableMutation(ticket)
      if (!op) return false
      const existing = find(result.profile_id)
      const accepted = !deleted.has(result.profile_id) && (op.binding
        ? result.profile_id === op.binding.profile_id && result.revision > op.binding.revision
        : !existing)
      let profile: RemoteServerProfile
      try { profile = copyProfile(result) } catch { retire(ticket, op); return false }
      retire(ticket, op)
      if (!accepted) return false
      items.value = [...items.value.filter(item => item.profile_id !== profile.profile_id), profile]
      rememberChecked(profile)
      return true
    }
    /** Only after successful DELETE 204 (or explicit recovery with profile:null). */
    function applyRemoval(ticket: RemoteServerMutationTicket): boolean {
      const op = applicableMutation(ticket)
      if (!op?.binding) return false
      retire(ticket, op)
      forget(op.binding.profile_id)
      return true
    }
    /** A rejected/cancelled direct form call: no retry, recovery or optimistic state change.
     * Raw errors stay in the form/API boundary, never Pinia action arguments.
     */
    function endMutation(ticket: RemoteServerMutationTicket, failed = true): boolean {
      const op = applicableMutation(ticket)
      if (!op) return false
      retire(ticket, op)
      if (failed && op.binding) errors.value[op.binding.profile_id] = failure()
      return true
    }
    function dispose(): void {
      disposed = true
      generation += 1
      mutations.clear(); profileMutations.clear(); verifies.clear(); deleted.clear()
      items.value = []; lastChecked.value = {}; busy.value = {}; errors.value = {}
      loading.value = false; loaded.value = false; error.value = null
    }
    onScopeDispose(dispose)
    return { items, loaded, loading, error, lastChecked, busy, errors,
      load, verify, beginMutation, applyMutation, applyRemoval, endMutation, dispose }
  })
}

export const useRemoteServersStore = createRemoteServersStore()
