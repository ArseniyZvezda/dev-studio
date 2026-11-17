<script setup lang="ts">
/** Explicit project identity and gated catalog commands (remote-identity/studio-design §5.2–5.3).
 * I8 editor owners flush/ACK through the existing Ctx event stream. This form
 * never claims an editor is clean, writes Markdown, joins a room or Enables.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { api, ApiError } from '@/api/client'
import type { ProjectRemotePreview, ProjectRemoteStatus } from '@/api/types'
import { createRemoteApiClient, type RemoteCreateResult, type RemoteProject, type RemoteServerProfile } from '@/api/remote'
import { useRemoteServersStore } from '@/stores/remote-servers'
import { t, type MessageKey } from '@/i18n'
import { rendererSession } from '@/session/project-session'
import { useSyncStore, type RemoteBindingTicket } from '@/stores/sync'

const sync = useSyncStore()
const servers = useRemoteServersStore()
const catalogApi = createRemoteApiClient()
const id = ref('')
const error = ref<MessageKey | null>(null)
const notice = ref<MessageKey | null>(null)
const loading = ref(false)
const needsRefresh = ref(false)
const catalogBusy = ref(false)
type Intent = {
  readonly ticket: RemoteBindingTicket
  readonly currentSession: () => void
  readonly snapshot: Readonly<ProjectRemoteStatus>
  readonly operation: 'edit_id' | 'select'
  readonly targetUrl: string
  readonly selected?: { profile: RemoteServerProfile; project: RemoteProject }
  readonly target: string
  readonly initialEvent: typeof sync.bindingChange
  changeId: string | null
  phase: 'review' | 'preparing' | 'draining' | 'blocked' | 'committing' | 'cancelling'
  cancelled: boolean
  cancelPromise?: Promise<void>
}
const intent = shallowRef<Intent | null>(null)
let alive = true
const remote = computed(() => sync.remote)
const valid = computed(() => /^[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])/.test(id.value))
const stateKeys: Record<ProjectRemoteStatus['state'], MessageKey> = {
  disabled: 'sync.settings.state.disabled', unconfigured: 'sync.settings.state.unconfigured',
  connecting: 'sync.settings.state.connecting', syncing: 'sync.settings.state.syncing',
  connected: 'sync.settings.state.connected', offline: 'sync.settings.state.offline',
  unauthorised: 'sync.settings.state.unauthorised', error: 'sync.settings.state.error',
  binding_required: 'sync.identity.bindingRequired', migration_required: 'sync.identity.migrationRequired',
  quiescing: 'sync.identity.draining', blocked: 'sync.identity.blocked',
}
const canAttemptChange = computed(() => !!sync.remote && !sync.remotePending && !sync.reloadRequired && !loading.value
  && !needsRefresh.value && !intent.value && !catalogBusy.value
  && Number.isSafeInteger(sync.remote.binding_generation + 2)
  && Number.isSafeInteger(sync.remote.identity_revision! + 1)
  && Number.isSafeInteger(sync.remote.identity_revision) && sync.remote.identity_revision! >= 1
  && sync.remote.state !== 'migration_required' && sync.remote.state !== 'quiescing')
// A fresh renderer may observe network activation blocked after a completed
// local-ID journal. Idle/disabled observation permits ONLY a local command
// attempt, not Select/Enable or proof that backend recovery/CAS will admit it
// (studio-design §5.2–5.3). Never turn the old renderer's epoch latch into refresh.
const offlineIdAttempt = computed(() => !sync.remotePending && !sync.reloadRequired
  && sync.remote?.state === 'blocked' && sync.remote.enabled === false
  && sync.remote.active_rooms === 0 && sync.remote.pending_writes === 0
  && sync.remote.binding_change === null && sync.bindingChange === null
  && sync.remote.reason == null)
const canChange = computed(() => canAttemptChange.value && sync.remote?.state !== 'blocked')
const canReview = computed(() => canAttemptChange.value
  && (sync.remote?.state !== 'blocked' || offlineIdAttempt.value)
  && valid.value && id.value !== remote.value?.persistent_id)

// Keep the original object as ownership token; shallowRef updates must not replace it.
const phase = ref<Intent['phase'] | null>(null)
function setPhase(op: Intent, value: Intent['phase']) {
  op.phase = value
  if (alive && intent.value === op) phase.value = value
}
function applicable(op: Intent) {
  op.currentSession()
  if (!alive || sync.reloadRequired || intent.value !== op || op.cancelled) throw new Error('abandoned')
}
function assertBinding(op: Intent, ready = false) {
  applicable(op)
  const current = sync.remote
  const event = sync.bindingChange
  const old = op.ticket.expected_binding_generation
  // The barrier itself invalidates the old store ticket when it fences G+1.
  // Accept ONLY our command's known fence, not arbitrary latest metadata G.
  if (current && (current.backend_epoch !== op.ticket.backend_epoch
    || ![old, ...(ready ? [old + 1] : [])].includes(current.binding_generation)
    || current.identity_revision !== op.snapshot.identity_revision
    || current.config_fingerprint !== op.snapshot.config_fingerprint)
    || event && event !== op.initialEvent && (event.change_id !== op.changeId
      || ![old, ...(ready ? [old + 1] : [])].includes(event.binding_generation)
      || ['cancelled', 'expired', 'paused'].includes(event.state))) throw new Error('stale')
}
function message(cause: unknown): MessageKey {
  if (cause instanceof ApiError && (cause.status === 404 || cause.status === 503
    || ['catalog_unavailable', 'capabilities_missing', 'version_mismatch', 'service_unavailable'].includes(cause.code ?? ''))) return 'sync.project.unavailable'
  const code = cause instanceof ApiError ? cause.code : null
  if (code === 'pending_edits' || code === 'invalid_editor_ack' || code === 'editor_lease_expired'
    || code === 'room_unresolved') return 'sync.identity.pendingEdits'
  if (code === 'unknown_clients' || code === 'reload_required') return 'sync.identity.reloadRequired'
  if (code === 'migration_required') return 'sync.identity.migrationRequired'
  if (code === 'recovery_required' || code === 'resume_required') return 'sync.identity.recoveryRequired'
  if (code === 'duplicate_local_binding') return 'sync.identity.duplicate'
  if (cause instanceof ApiError && cause.status === 409) return 'sync.identity.conflict'
  if (cause instanceof ApiError && cause.status === 422) return 'sync.identity.invalid'
  return 'sync.identity.failed'
}
async function refresh() {
  if (intent.value || loading.value) return
  loading.value = true
  const current = rendererSession.capture()
  try {
    const loaded = await sync.loadRemote()
    current()
    if (!alive) return
    needsRefresh.value = !loaded
    error.value = loaded ? null : 'sync.identity.unavailable'
    if (loaded) {
      id.value = sync.remote?.persistent_id ?? ''
    }
  } catch {
    if (alive) { needsRefresh.value = true; error.value = 'sync.identity.unavailable' }
  } finally { if (alive) loading.value = false }
}
function review() {
  if (!canReview.value || !sync.remote) return
  error.value = null
  notice.value = null
  try {
    const op: Intent = { ticket: sync.capture(), currentSession: rendererSession.capture(),
      snapshot: Object.freeze({ ...sync.remote }), target: id.value, targetUrl: sync.remote.url,
      operation: 'edit_id', initialEvent: sync.bindingChange,
      changeId: null, phase: 'review', cancelled: false }
    intent.value = op
    phase.value = 'review'
  } catch { needsRefresh.value = true; error.value = 'sync.identity.conflict' }
}
async function cancelExact(op: Intent) {
  if (!op.changeId) return
  if (op.cancelPromise) return op.cancelPromise
  op.cancelPromise = (async () => {
    try {
      // Cancellation uses only the retained change ID/runtime binding, even if
      // G has advanced. It cannot commit or resume automatically after a fence.
      op.currentSession()
      const result: unknown = await api.cancelRemoteChange(op.changeId!)
      if (alive && intent.value === op) {
        notice.value = result && typeof result === 'object' && 'state' in result
          && result.state === 'paused' ? 'sync.identity.paused' : 'sync.identity.cancelled'
        intent.value = null
        phase.value = null
      }
    } catch {
      if (alive && intent.value === op) error.value = 'sync.identity.cancelFailed'
    } finally {
      if (alive && intent.value === op) setPhase(op, 'blocked')
    }
  })()
  await op.cancelPromise
  op.cancelPromise = undefined
}
async function cancel() {
  const op = intent.value
  if (!op || op.phase === 'committing' || op.phase === 'cancelling') return
  op.cancelled = true
  error.value = null
  needsRefresh.value = op.phase !== 'review'
  if (op.phase === 'review') { intent.value = null; phase.value = null; notice.value = 'sync.identity.cancelled'; return }
  setPhase(op, 'cancelling')
  notice.value = 'sync.identity.cancelling'
  if (op.changeId) await cancelExact(op)
  // A pending prepare still owns this intent; its late reply is DELETE-only.
}
async function confirm() {
  const op = intent.value
  if (!op || op.phase !== 'review') return
  let commitStarted = false
  setPhase(op, 'preparing')
  try {
    op.ticket.assertCurrent()
    assertBinding(op)
    const prepared = await api.prepareRemoteChange({
      backend_epoch: op.ticket.backend_epoch,
      operation: op.operation, persistent_id: op.target, url: op.targetUrl,
      ...(op.selected ? { profile_id: op.selected.profile.profile_id, catalog_id: op.selected.project.id,
        catalog_instance_id: op.selected.project.storage_epoch } : {}),
      expected_revision: op.snapshot.identity_revision!, config_fingerprint: op.snapshot.config_fingerprint,
    })
    op.changeId = prepared.change_id
    if (op.cancelled || !alive) { await cancelExact(op); return }
    assertBinding(op)
    if (!op.changeId || prepared.to.persistent_id !== op.target || prepared.to.url !== op.targetUrl
      || prepared.to.enabled !== false || prepared.conflicts.length) throw new Error('invalid preparation')
    setPhase(op, 'draining')
    const drained = await api.quiesceRemoteChange(op.changeId, {
      expected_generation: op.ticket.expected_binding_generation,
    })
    if (op.cancelled || !alive) return
    assertBinding(op, true)
    if (drained.state !== 'ready' || !Array.isArray(drained.missing_acks) || drained.missing_acks.length) {
      setPhase(op, 'blocked')
      error.value = !Array.isArray(drained.missing_acks) || drained.missing_acks.length ? 'sync.identity.pendingEdits'
        : message(new ApiError(409, '', drained.reason ?? null))
      return
    }
    // I5 ready advances exactly once. Never take a G from mutable sync.remote.
    const fencedGeneration = op.ticket.expected_binding_generation + 1
    if (!Number.isSafeInteger(fencedGeneration)) throw new Error('unrepresentable fence')
    commitStarted = true
    setPhase(op, 'committing')
    const result = await api.commitRemoteChange(op.changeId, {
      expected_revision: op.snapshot.identity_revision!, expected_generation: fencedGeneration,
      config_fingerprint: op.snapshot.config_fingerprint,
      confirmation: { url: op.targetUrl, persistent_id: op.target },
    })
    applicable(op)
    const event = sync.bindingChange
    const observed = sync.remote
    if (event && event !== op.initialEvent && (event.change_id !== op.changeId
      || ['paused', 'cancelled', 'expired'].includes(event.state))
      || observed && (observed.backend_epoch !== op.ticket.backend_epoch
        || ![op.ticket.expected_binding_generation, fencedGeneration, result.binding_generation].includes(observed.binding_generation)
        || ![op.snapshot.identity_revision, result.identity_revision].includes(observed.identity_revision))) {
      throw new Error('superseded commit')
    }
    if (result.committed !== true || result.change_id !== op.changeId
      || result.binding_change?.change_id !== op.changeId || result.binding_change?.state !== 'committed'
      || !rendererSession.matches(result) || result.backend_epoch !== op.ticket.backend_epoch
      || result.persistent_id !== op.target || result.url !== op.targetUrl || result.enabled !== false
      || typeof result.config_fingerprint !== 'string' || !/^[0-9a-f]{64}(?![\s\S])/.test(result.config_fingerprint)
      || result.config_fingerprint === op.snapshot.config_fingerprint
      || !Number.isSafeInteger(result.binding_generation) || result.binding_generation <= fencedGeneration
      || (op.selected && (result.profile_id !== op.selected.profile.profile_id
        || result.space_instance_id !== op.selected.project.storage_epoch))
      || (!op.selected && result.profile_id != null)
      || result.active_rooms !== 0 || result.pending_writes !== 0 || typeof result.has_token !== 'boolean'
      || result.state !== 'disabled' || result.identity_revision !== op.snapshot.identity_revision! + 1) {
      throw new Error('invalid commit response')
    }
    // A valid receipt is not a second, component-only binding. Publish success
    // only after the owning store admits the matching observation with its
    // safe-integer/monotonic/epoch guards. No latest-G repair or command retry.
    const loaded = await sync.loadRemote()
    applicable(op)
    const admitted = sync.remote
    if (!loaded || !admitted || sync.remotePending || admitted.backend_epoch !== result.backend_epoch
      || admitted.persistent_id !== result.persistent_id || admitted.url !== result.url
      || admitted.identity_revision !== result.identity_revision || admitted.enabled !== false
      || admitted.state !== 'disabled' || admitted.config_fingerprint !== result.config_fingerprint
      || admitted.binding_generation !== result.binding_generation
      || (admitted.profile_id !== undefined && admitted.profile_id !== (result.profile_id ?? null))
      || (admitted.space_instance_id !== undefined && admitted.space_instance_id !== (result.space_instance_id ?? null))) throw new Error('unconfirmed receipt')
    id.value = result.persistent_id!
    intent.value = null
    phase.value = null
    notice.value = op.operation === 'select' ? 'sync.project.selected' : 'sync.identity.saved'
    needsRefresh.value = false
  } catch (cause) {
    if (!alive || intent.value !== op) return
    if (op.cancelled) {
      if (op.changeId) await cancelExact(op)
      else { intent.value = null; phase.value = null; notice.value = 'sync.identity.cancelled' }
      return
    }
    needsRefresh.value = true
    error.value = message(cause)
    if (commitStarted) { intent.value = null; phase.value = null; error.value = 'sync.identity.commitUnknown' }
    else if (op.changeId) setPhase(op, 'blocked')
    else { intent.value = null; phase.value = null }
  }
}
// Explicit catalog/Create intents use the existing global metadata/client. Keys
// never enter this form or Pinia. Backend gates remain authoritative; Verify's
// green status alone does not release Catalog, Create, Preview or Enable.
const profileId = ref('')
const catalogError = ref<MessageKey | null>(null)
const catalog = shallowRef<{ profile: RemoteServerProfile; items: RemoteProject[]; next: string | null } | null>(null)
const createId = ref('')
const createName = ref('')
const createIntent = shallowRef<{ profile: RemoteServerProfile; id: string; name: string; operation: string } | null>(null)
const createResult = shallowRef<RemoteCreateResult | null>(null)
const preview = shallowRef<ProjectRemotePreview | null>(null)
const selectedProfile = computed(() => servers.items.find(item => item.profile_id === profileId.value))
const panelIdle = computed(() => !catalogBusy.value && !loading.value && !intent.value)
function sameProfile(profile: RemoteServerProfile) {
  const current = selectedProfile.value
  return current?.profile_id === profile.profile_id && current.revision === profile.revision
    && current.url === profile.url && !servers.loading && !servers.error && !servers.busy[profile.profile_id]
}
function catalogCurrent() { return !!catalog.value && sameProfile(catalog.value.profile) }
async function loadServers() {
  if (!panelIdle.value) return
  catalogBusy.value = true
  const current = rendererSession.capture()
  try {
    const loaded = await servers.load()
    current()
    if (alive) { catalog.value = null; catalogError.value = loaded ? null : 'sync.project.unavailable' }
  } catch { if (alive) catalogError.value = 'sync.project.unavailable' }
  finally { if (alive) catalogBusy.value = false }
}
async function loadCatalog(more = false) {
  const profile = selectedProfile.value
  if (!panelIdle.value || !profile || !sameProfile(profile) || !profile.has_token) return
  const previous = more ? catalog.value : null
  if (more && (!previous || !catalogCurrent() || !previous.next)) return
  const current = rendererSession.capture()
  const binding = Object.freeze({ ...profile })
  catalogBusy.value = true
  catalogError.value = null
  try {
    const page = await catalogApi.listProjects(binding.profile_id, previous?.next)
    current()
    if (!alive || !sameProfile(binding)) return
    // GET list is an older, unvalidated client surface. Do not turn an HTTP 200
    // malformed/duplicate row into selection authority (studio-design §6).
    const validId = (value: unknown): value is string => typeof value === 'string'
      && /^[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])/.test(value)
    if (!page || !Array.isArray(page.items) || !(page.next_cursor === null || validId(page.next_cursor))
      || page.items.some(item => !item || Object.keys(item).sort().join(',') !== 'created_at,display_name,id,metadata_revision,storage_epoch'
        || !validId(item.id) || typeof item.display_name !== 'string'
        || typeof item.storage_epoch !== 'string' || !/^[0-9a-f]{32}(?![\s\S])/.test(item.storage_epoch)
        || !Number.isSafeInteger(item.metadata_revision) || item.metadata_revision < 1
        || typeof item.created_at !== 'string' || !item.created_at)
      || new Set(page.items.map(item => item.id)).size !== page.items.length
      || (previous && page.next_cursor !== null && page.next_cursor <= previous.next!)
      || (previous && page.items.some(item => item.id <= previous.next!))) throw new Error('invalid catalog')
    const rows = new Map((previous?.items ?? []).map(item => [item.id, item]))
    for (const item of page.items) rows.set(item.id, Object.freeze({ id: item.id, display_name: item.display_name,
      storage_epoch: item.storage_epoch, metadata_revision: item.metadata_revision, created_at: item.created_at }))
    catalog.value = { profile: binding, items: [...rows.values()], next: page.next_cursor }
  } catch (cause) {
    if (alive) { catalog.value = null; catalogError.value = message(cause) }
  } finally { if (alive) catalogBusy.value = false }
}
function selectProject(project: RemoteProject) {
  if (!canChange.value || !sync.remote || !catalogCurrent()
    || !catalog.value!.items.includes(project)) return
  try {
    const profile = Object.freeze({ ...catalog.value!.profile })
    intent.value = { ticket: sync.capture(), currentSession: rendererSession.capture(),
      snapshot: Object.freeze({ ...sync.remote }), target: project.id, targetUrl: profile.url,
      selected: { profile, project }, operation: 'select', initialEvent: sync.bindingChange,
      changeId: null, phase: 'review', cancelled: false }
    phase.value = 'review'
    notice.value = null
    error.value = null
  } catch { needsRefresh.value = true; error.value = 'sync.identity.conflict' }
}
async function createProject(observe = false) {
  if (!panelIdle.value || !catalogCurrent()) return
  if (!observe && (createIntent.value || !/^[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])/.test(createId.value))) return
  if (observe && !createIntent.value) return
  const original = createIntent.value ?? Object.freeze({ profile: { ...catalog.value!.profile },
    id: createId.value, name: createName.value, operation: crypto.randomUUID() })
  if (!sameProfile(original.profile)) return
  createIntent.value = original // retained through exceptions/unknown; never retry POST implicitly.
  catalogBusy.value = true
  catalogError.value = null
  const current = rendererSession.capture()
  try {
    const result = observe
      ? await catalogApi.observe(original.profile.profile_id, original.operation, { expected_revision: original.profile.revision })
      : await catalogApi.createProject(original.profile.profile_id, { id: original.id, display_name: original.name,
        operation_id: original.operation, expected_revision: original.profile.revision })
    current()
    if (!alive || createIntent.value !== original || !sameProfile(original.profile)) return
    if (result.project && result.project.id !== original.id) throw new Error('mismatched observation')
    createResult.value = result
    // Deliberately no Select, refresh, Prepare, Preview, Enable or operation release.
  } catch (cause) { if (alive) catalogError.value = message(cause) }
  finally { if (alive) catalogBusy.value = false }
}
function newCreateIntent() {
  if (!panelIdle.value) return
  // Only an explicit new user intent gets another operation ID. The old durable
  // reservation is not removed or reinterpreted as permission to retry.
  createIntent.value = null
  createResult.value = null
  catalogError.value = null
}
async function readPreview() {
  if (!panelIdle.value || !sync.remote || sync.remote.enabled || !sync.remote.profile_id
    || !sync.remote.space_instance_id || sync.remotePending || needsRefresh.value) return
  const snapshot = sync.remote
  const initialEvent = sync.bindingChange
  const ticket = sync.capture()
  catalogBusy.value = true
  catalogError.value = null
  preview.value = null
  try {
    const result = await sync.run(ticket, () => api.previewRemoteReconciliation())
    if (!alive || sync.remotePending || sync.bindingChange !== initialEvent
      || sync.remote?.identity_revision !== snapshot.identity_revision
      || sync.remote?.config_fingerprint !== snapshot.config_fingerprint) return
    if (!result || typeof result.preview_id !== 'string' || !result.preview_id
      || typeof result.fingerprint !== 'string' || !result.fingerprint
      || !result.counts || Array.isArray(result.counts) || typeof result.counts !== 'object'
      || Object.values(result.counts).some(count => !Number.isSafeInteger(count) || count < 0)
      || !Array.isArray(result.conflicts)
      || !(typeof result.expires_at === 'number' ? result.expires_at * 1000 > Date.now()
        : typeof result.expires_at === 'string' && Date.parse(result.expires_at) > Date.now())) throw new Error('invalid preview')
    preview.value = result
  } catch (cause) { if (alive) catalogError.value = message(cause) }
  finally { if (alive) catalogBusy.value = false }
}
watch(() => [sync.remote?.binding_generation, sync.remote?.identity_revision,
  sync.remote?.config_fingerprint, sync.remotePending], () => { preview.value = null })
onMounted(() => { sync.subscribe(); void refresh() })
onBeforeUnmount(() => {
  alive = false
  const op = intent.value
  if (op && op.phase !== 'committing') { op.cancelled = true; void cancelExact(op) }
})
</script>

<template>
  <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
    <p class="text-small text-faint">{{ t('sync.identity.intro') }}</p>
    <dl class="grid grid-cols-[8rem_1fr] gap-1">
      <dt class="text-muted">{{ t(needsRefresh || sync.remotePending ? 'sync.identity.lastObserved' : 'sync.identity.current') }}</dt>
      <dd data-testid="remote-id-current" class="font-mono break-all">{{ remote?.persistent_id ?? t('sync.identity.missing') }}</dd>
      <dt class="text-muted">{{ t('sync.settings.url') }}</dt>
      <dd class="font-mono break-all">{{ remote?.url || t('sync.identity.noServer') }}</dd>
      <dt class="text-muted">{{ t('sync.identity.status') }}</dt>
      <dd>{{ remote ? t(stateKeys[remote.state]) : t('sync.identity.unavailable') }}</dd>
    </dl>
    <p v-if="remote?.reason" class="text-small text-warning">{{ t(message(new ApiError(409, '', remote.reason))) }}</p>
    <p v-if="offlineIdAttempt" data-testid="remote-id-offline-attempt" class="text-small text-warning">{{ t('sync.identity.offlineAttempt') }}</p>
    <form class="space-y-3" @submit.prevent="review">
      <label class="block">
        <span class="mb-1 block text-muted">{{ t('sync.identity.newId') }}</span>
        <input v-model="id" data-testid="remote-id-input" type="text" autocomplete="off" spellcheck="false"
          :disabled="!!intent || loading" :aria-invalid="!valid" aria-describedby="remote-id-hint"
          class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent" />
      </label>
      <p id="remote-id-hint" class="text-small text-faint">{{ t('sync.identity.invalid') }}</p>
      <button data-testid="remote-id-review" type="submit" :disabled="!canReview"
        class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('sync.identity.review') }}</button>
    </form>
    <div v-if="intent" class="space-y-3 rounded-md border border-line p-3">
      <p class="font-medium">{{ t(intent.operation === 'select' ? 'sync.project.selectTitle' : 'sync.identity.confirmTitle') }}</p>
      <p class="text-small">{{ t(intent.operation === 'select' ? 'sync.project.selectHint' : 'sync.identity.confirmHint') }}</p>
      <dl class="grid grid-cols-[8rem_1fr] gap-1 font-mono break-all">
        <dt>{{ t('sync.identity.current') }}</dt><dd>{{ intent.snapshot.persistent_id }}</dd>
        <dt>{{ t('sync.identity.newId') }}</dt><dd>{{ intent.target }}</dd>
        <dt>{{ t('sync.settings.url') }}</dt><dd>{{ intent.targetUrl || t('sync.identity.noServer') }}</dd>
      </dl>
      <p v-if="phase && phase !== 'review' && phase !== 'blocked'" role="status">{{ t(phase === 'committing' ? 'sync.identity.committing' : phase === 'cancelling' ? 'sync.identity.cancelling' : 'sync.identity.draining') }}</p>
      <div class="flex gap-2">
        <button v-if="phase === 'review'" data-testid="remote-id-confirm" type="button" @click="confirm"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover">{{ t(intent.operation === 'select' ? 'sync.project.selectConfirm' : 'sync.identity.confirm') }}</button>
        <button data-testid="remote-id-cancel" type="button" :disabled="phase === 'committing' || phase === 'cancelling'" @click="cancel"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('common.cancel') }}</button>
      </div>
    </div>
    <p v-if="error" data-testid="remote-id-error" role="alert" class="text-small text-danger">{{ t(error) }}</p>
    <p v-if="notice" data-testid="remote-id-notice" role="status" class="text-small">{{ t(notice) }}</p>
    <button data-testid="remote-id-refresh" type="button" :disabled="!!intent || loading" @click="refresh"
      class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('sync.identity.refresh') }}</button>
    <p class="text-small text-faint">{{ t('sync.identity.closed') }}</p>
    <section class="space-y-3 border-t border-line pt-3" :aria-label="t('sync.project.title')">
      <h3 class="font-medium">{{ t('sync.project.title') }}</h3>
      <p class="text-small text-faint">{{ t('sync.project.hint') }}</p>
      <button data-testid="remote-project-servers" type="button" :disabled="!panelIdle" @click="loadServers"
        class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.loadServers') }}</button>
      <label v-if="servers.loaded" class="block">
        {{ t('sync.project.server') }}
        <select v-model="profileId" data-testid="remote-project-profile" :disabled="!panelIdle"
          class="w-full rounded-md border border-line bg-inset px-2 py-1">
          <option value="">{{ t('sync.identity.noServer') }}</option>
          <option v-for="profile in servers.items" :key="profile.profile_id" :value="profile.profile_id">{{ profile.name }} — {{ profile.url }}</option>
        </select>
      </label>
      <button data-testid="remote-project-catalog" type="button"
        :disabled="!panelIdle || !selectedProfile?.has_token || !sameProfile(selectedProfile)" @click="loadCatalog()"
        class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.loadCatalog') }}</button>
      <p v-if="!catalogCurrent()" class="text-small text-muted">{{ t('sync.project.catalogGate') }}</p>
      <ul v-else class="space-y-2">
        <li v-for="project in catalog!.items" :key="project.id" class="rounded-md border border-line p-2">
          <p><bdi>{{ project.display_name }}</bdi></p>
          <p class="font-mono break-all"><bdi>{{ project.id }}</bdi> — {{ catalog!.profile.url }}</p>
          <button data-testid="remote-project-select" type="button" :disabled="!canChange" @click="selectProject(project)"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.select') }}</button>
        </li>
      </ul>
      <button v-if="catalogCurrent() && catalog?.next" data-testid="remote-project-more" type="button"
        :disabled="!panelIdle" @click="loadCatalog(true)" class="rounded-md border border-line px-3 py-1.5">{{ t('sync.project.more') }}</button>
      <form class="space-y-2" @submit.prevent="createProject()">
        <p class="font-medium">{{ t('sync.project.create') }}</p>
        <p class="text-small text-faint">{{ t('sync.project.createHint') }}</p>
        <label class="block">{{ t('sync.project.createId') }}
          <input v-model="createId" data-testid="remote-project-create-id" :disabled="!panelIdle || !catalogCurrent() || !!createIntent"
            class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono" autocomplete="off" spellcheck="false" /></label>
        <label class="block">{{ t('sync.project.createName') }}
          <input v-model="createName" data-testid="remote-project-create-name" :disabled="!panelIdle || !catalogCurrent() || !!createIntent"
            class="w-full rounded-md border border-line bg-inset px-2 py-1" autocomplete="off" /></label>
        <button data-testid="remote-project-create" type="submit"
          :disabled="!panelIdle || !catalogCurrent() || !!createIntent || !/^[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])/.test(createId)"
          class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.create') }}</button>
        <template v-if="createIntent">
          <p class="font-mono break-all">{{ createIntent.id }} — {{ createIntent.profile.url }}</p>
          <p v-if="createResult" data-testid="remote-project-create-result" role="status">{{ t(`sync.project.outcome.${createResult.state}`) }}</p>
          <button data-testid="remote-project-observe" type="button" :disabled="!panelIdle || !catalogCurrent() || !sameProfile(createIntent.profile)" @click="createProject(true)"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.observe') }}</button>
          <button data-testid="remote-project-new-create" type="button" :disabled="!panelIdle" @click="newCreateIntent"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.newCreate') }}</button>
        </template>
      </form>
      <button data-testid="remote-project-preview" type="button"
        :disabled="!panelIdle || !remote?.profile_id || !remote?.space_instance_id || remote.enabled || sync.remotePending || needsRefresh" @click="readPreview"
        class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.preview') }}</button>
      <p v-if="!remote?.profile_id || !remote?.space_instance_id" class="text-small text-muted">{{ t('sync.project.previewGate') }}</p>
      <p v-if="preview" data-testid="remote-project-preview-result" role="status">{{ t('sync.project.previewResult', { conflicts: preview.conflicts.length }) }}</p>
      <button data-testid="remote-project-enable" type="button" disabled
        class="rounded-md border border-line px-3 py-1.5 opacity-50">{{ t('sync.project.enable') }}</button>
      <p class="text-small text-warning">{{ t('sync.project.enableGate') }}</p>
      <p v-if="catalogBusy" role="status">{{ t('sync.project.loading') }}</p>
      <p v-if="catalogError" data-testid="remote-project-error" role="alert" class="text-small text-danger">{{ t(catalogError) }}</p>
    </section>
  </div>
</template>
