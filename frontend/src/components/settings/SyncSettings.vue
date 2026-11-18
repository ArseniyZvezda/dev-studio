<script setup lang="ts">
/** Explicit project identity and gated catalog commands (remote-identity/studio-design §5.2–5.3, §9, §10.2).
 * I8 editor owners flush/ACK through the existing Ctx event stream. This form
 * never claims an editor is clean, writes Markdown or joins a room. Only the
 * explicit Enable confirmation (I13) enables; Create, Select, Preview, migration
 * steps (I15) and Disable all leave sync disabled.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { api, ApiError } from '@/api/client'
import type {
  ProjectRemoteMigrationPlan, ProjectRemoteMigrationProfile, ProjectRemoteMigrationResolution,
  ProjectRemoteArchiveRequest, ProjectRemotePrepareRequest, ProjectRemotePreview, ProjectRemoteRecoverRequest,
  ProjectRemoteRecovery, ProjectRemoteStatus,
} from '@/api/types'
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
/** I15: a project with no identity yet has no revision. Backend RemotePrepare/
 * RemoteCommit define what migrate sends then; this is the single place to change. */
const MIGRATION_REVISION_WITHOUT_IDENTITY = 0
const ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])/
type Intent = {
  readonly ticket: RemoteBindingTicket
  readonly currentSession: () => void
  readonly snapshot: Readonly<ProjectRemoteStatus>
  readonly operation: 'edit_id' | 'select' | 'disable' | 'migrate'
  readonly targetUrl: string
  readonly selected?: { profile: RemoteServerProfile; project: RemoteProject }
  /** CAS revision sent to prepare/commit; the commit must report exactly +1. */
  readonly expectedRevision: number
  /** Extra prepare fields of select/disable/migrate (never a browser-trusted target). */
  readonly extra: Partial<ProjectRemotePrepareRequest>
  /** Private binding the commit must report; null means none. */
  readonly binding: { profile_id: string; space_instance_id: string | null } | null
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
// Explicit recovery / checkpoint archive (§16.10, VS-188). Both are single,
// separately confirmed requests: nothing recovers, archives or retries on load.
type RecoveryReview = { readonly snapshot: Readonly<ProjectRemoteStatus>; readonly recovery: Readonly<ProjectRemoteRecovery> }
const recovery = computed(() => sync.remote?.recovery ?? null)
const recoveryReview = shallowRef<RecoveryReview | null>(null)
const recoveryStep = ref<'review' | 'recovering' | null>(null)
const recoveryError = ref<MessageKey | null>(null)
const recoveryNotice = ref<MessageKey | null>(null)
const archiveStep = ref<'review' | 'archiving' | null>(null)
/** The exact destination shown while the (consumed) preview is gone. */
const archiveTarget = shallowRef<{ persistent_id: string; url: string } | null>(null)
const valid = computed(() => ID_PATTERN.test(id.value))
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
  // A pending recovery refuses every other binding command (409 recovery_required).
  && !sync.remote.recovery && recoveryStep.value !== 'recovering' && archiveStep.value !== 'archiving'
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
const committedNotice: Record<Intent['operation'], MessageKey> = {
  edit_id: 'sync.identity.saved', select: 'sync.project.selected',
  disable: 'sync.project.disabled', migrate: 'sync.migration.applied',
}
const intentText: Record<Intent['operation'], { title: MessageKey; hint: MessageKey; confirm: MessageKey }> = {
  edit_id: { title: 'sync.identity.confirmTitle', hint: 'sync.identity.confirmHint', confirm: 'sync.identity.confirm' },
  select: { title: 'sync.project.selectTitle', hint: 'sync.project.selectHint', confirm: 'sync.project.selectConfirm' },
  disable: { title: 'sync.project.disableTitle', hint: 'sync.project.disableHint', confirm: 'sync.project.disableConfirm' },
  migrate: { title: 'sync.migration.applyTitle', hint: 'sync.migration.applyHint', confirm: 'sync.migration.applyConfirm' },
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
  if (intent.value || loading.value || recoveryStep.value === 'recovering') return
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
      operation: 'edit_id', initialEvent: sync.bindingChange, expectedRevision: sync.remote.identity_revision!,
      extra: {}, binding: null, changeId: null, phase: 'review', cancelled: false }
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
      operation: op.operation, persistent_id: op.target, url: op.targetUrl, ...op.extra,
      expected_revision: op.expectedRevision, config_fingerprint: op.snapshot.config_fingerprint,
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
      expected_revision: op.expectedRevision, expected_generation: fencedGeneration,
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
      || (op.binding ? result.profile_id !== op.binding.profile_id
        || (result.space_instance_id ?? null) !== op.binding.space_instance_id
        : result.profile_id != null)
      || result.active_rooms !== 0 || result.pending_writes !== 0 || typeof result.has_token !== 'boolean'
      || result.state !== 'disabled' || result.identity_revision !== op.expectedRevision + 1) {
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
    notice.value = committedNotice[op.operation]
    if (op.operation === 'migrate') resetMigration()
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
const selectedProfile = computed(() => servers.items.find(item => item.profile_id === profileId.value))
const panelIdle = computed(() => !catalogBusy.value && !loading.value && !intent.value && enableStep.value !== 'enabling'
  && archiveStep.value !== 'archiving' && recoveryStep.value !== 'recovering')
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
      expectedRevision: sync.remote.identity_revision!,
      extra: { profile_id: profile.profile_id, catalog_id: project.id, catalog_instance_id: project.storage_epoch },
      binding: { profile_id: profile.profile_id, space_instance_id: project.storage_epoch },
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
// ---------------------------------------------------------------- Preview / Enable (I11–I13)
// A preview is bound to the exact observation it was read from. Any change of
// revision, config, G, backend epoch, target or binding drops it; Enable sends
// only these captured values and never retries (studio-design §5.2, §10.2 item 6).
type PreviewRecord = {
  readonly result: Readonly<ProjectRemotePreview>
  readonly snapshot: Readonly<ProjectRemoteStatus>
  readonly initialEvent: typeof sync.bindingChange
  readonly expiresAt: number
}
const preview = shallowRef<PreviewRecord | null>(null)
const enableStep = ref<'review' | 'enabling' | null>(null)
const projectNotice = ref<MessageKey | null>(null)
let previewTimer: ReturnType<typeof setTimeout> | undefined
const COUNT_KEYS = ['unchanged', 'download', 'upload', 'conflict', 'delete_local', 'delete_remote'] as const
const countKeys: Record<string, MessageKey> = {
  unchanged: 'sync.project.count.unchanged', download: 'sync.project.count.download',
  upload: 'sync.project.count.upload', conflict: 'sync.project.count.conflict',
  delete_local: 'sync.project.count.delete_local', delete_remote: 'sync.project.count.delete_remote',
}
const baseKeys: Record<string, MessageKey> = {
  none: 'sync.project.base.none', matched: 'sync.project.base.matched',
  epoch_mismatch: 'sync.project.base.epoch_mismatch', cursor_rollback: 'sync.project.base.cursor_rollback',
}
const MAX_SHOWN_CONFLICTS = 50
const previewCounts = computed(() => {
  const counts = preview.value?.result.counts ?? {}
  const rows: { key: MessageKey; count: number }[] = COUNT_KEYS
    .filter(key => key in counts).map(key => ({ key: countKeys[key]!, count: counts[key]! }))
  const other = Object.entries(counts).filter(([key]) => !(key in countKeys)).reduce((sum, [, count]) => sum + count, 0)
  if (other) rows.push({ key: 'sync.project.count.unclassified', count: other })
  return rows
})
function dropPreview() {
  preview.value = null
  if (enableStep.value === 'review') enableStep.value = null
  if (archiveStep.value === 'review') { archiveStep.value = null; archiveTarget.value = null }
  if (previewTimer !== undefined) { clearTimeout(previewTimer); previewTimer = undefined }
}
function previewMatches(record: PreviewRecord) {
  const current = sync.remote
  const old = record.snapshot
  return !!current && !sync.remotePending && !sync.reloadRequired && sync.bindingChange === record.initialEvent
    && current.enabled === false && current.backend_epoch === old.backend_epoch
    && current.binding_generation === old.binding_generation && current.identity_revision === old.identity_revision
    && current.config_fingerprint === old.config_fingerprint && current.persistent_id === old.persistent_id
    && current.url === old.url && current.profile_id === old.profile_id
    && current.space_instance_id === old.space_instance_id
}
const canPreview = computed(() => panelIdle.value && !!remote.value && remote.value.enabled === false
  && !!remote.value.profile_id && !!remote.value.space_instance_id && !!remote.value.persistent_id
  && Number.isSafeInteger(remote.value.identity_revision) && remote.value.identity_revision! >= 1
  && !remote.value.recovery && !sync.remotePending && !sync.reloadRequired && !needsRefresh.value)
/** A checkpoint of another Remote incarnation / a rolled-back cursor: Enable is
 * refused (instance_mismatch / cursor_rollback); only Archive is offered. */
const archiveBase = computed(() => {
  const base = preview.value?.result.base
  return base === 'epoch_mismatch' || base === 'cursor_rollback' ? base : null
})
const canEnable = computed(() => panelIdle.value && !!preview.value && previewMatches(preview.value)
  && !needsRefresh.value && preview.value.expiresAt > Date.now() && !archiveBase.value && !sync.remote?.recovery)
const canArchive = computed(() => panelIdle.value && !!preview.value && previewMatches(preview.value)
  && !needsRefresh.value && preview.value.expiresAt > Date.now() && !!archiveBase.value && !sync.remote?.recovery
  && !!preview.value.snapshot.persistent_id)
function previewMessage(cause: unknown): MessageKey {
  const code = cause instanceof ApiError ? cause.code ?? '' : ''
  const known: Record<string, MessageKey> = {
    preview_stale: 'sync.project.previewStale', preview_not_found: 'sync.project.previewStale',
    snapshot_changed: 'sync.project.previewStale', preview_expired: 'sync.project.previewExpired',
    instance_mismatch: 'sync.project.instanceMismatch', project_not_found: 'sync.project.instanceMismatch',
    cursor_rollback: 'sync.project.cursorRollback', already_enabled: 'sync.project.alreadyEnabled',
    unauthorised: 'sync.project.unauthorised', confirmation_required: 'sync.project.confirmationMismatch',
    snapshot_too_large: 'sync.project.snapshotTooLarge', protocol_unavailable: 'sync.project.unavailable',
    insecure_blocked: 'sync.project.unavailable',
  }
  return known[code] ?? message(cause)
}
function expiry(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value * 1000
    : typeof value === 'string' ? Date.parse(value) : Number.NaN
}
async function readPreview() {
  if (!canPreview.value || !sync.remote) return
  // Captured before the first await; the request is the CAS of this observation.
  const snapshot = Object.freeze({ ...sync.remote })
  const initialEvent = sync.bindingChange
  const ticket = sync.capture()
  const body = { expected_revision: snapshot.identity_revision!, config_fingerprint: snapshot.config_fingerprint,
    backend_epoch: snapshot.backend_epoch }
  catalogBusy.value = true
  catalogError.value = null
  projectNotice.value = null
  dropPreview()
  try {
    const result = await sync.run(ticket, () => api.previewRemoteReconciliation(body))
    if (!alive) return
    const record: PreviewRecord = { result: Object.freeze({ ...result }), snapshot, initialEvent,
      expiresAt: expiry(result?.expires_at) }
    if (!previewMatches(record)) return
    if (!result || typeof result.preview_id !== 'string' || !/^[0-9a-f]{32}(?![\s\S])/.test(result.preview_id)
      || typeof result.fingerprint !== 'string' || !/^[0-9a-f]{64}(?![\s\S])/.test(result.fingerprint)
      || !result.counts || Array.isArray(result.counts) || typeof result.counts !== 'object'
      || Object.values(result.counts).some(count => !Number.isSafeInteger(count) || count < 0)
      || !Array.isArray(result.conflicts) || result.conflicts.some(path => typeof path !== 'string')
      || !(record.expiresAt > Date.now())
      || (result.base !== undefined && typeof result.base !== 'string')) throw new Error('invalid preview')
    if (result.incarnation !== undefined && (!result.incarnation || typeof result.incarnation !== 'object'
      || result.incarnation.space_instance_id !== snapshot.space_instance_id
      || !Number.isSafeInteger(result.incarnation.seq) || result.incarnation.seq < 0)) {
      throw new ApiError(409, '', 'instance_mismatch')
    }
    preview.value = record
    previewTimer = setTimeout(() => {
      if (preview.value === record) { dropPreview(); catalogError.value = 'sync.project.previewExpired' }
    }, Math.max(0, record.expiresAt - Date.now()))
  } catch (cause) { if (alive) catalogError.value = previewMessage(cause) }
  finally { if (alive) catalogBusy.value = false }
}
function reviewEnable() {
  const record = preview.value
  if (!record || !canEnable.value) return
  if (record.expiresAt <= Date.now()) { dropPreview(); catalogError.value = 'sync.project.previewExpired'; return }
  catalogError.value = null
  projectNotice.value = null
  enableStep.value = 'review'
}
function cancelEnable() {
  if (enableStep.value === 'review') enableStep.value = null
}
async function confirmEnable() {
  const record = preview.value
  if (enableStep.value !== 'review' || !record || !canEnable.value) return
  if (record.expiresAt <= Date.now()) { dropPreview(); catalogError.value = 'sync.project.previewExpired'; return }
  const s = record.snapshot
  let ticket: RemoteBindingTicket
  try {
    ticket = sync.capture()
    ticket.assertCurrent()
    if (ticket.backend_epoch !== s.backend_epoch || ticket.expected_binding_generation !== s.binding_generation) throw new Error('stale')
  } catch { dropPreview(); needsRefresh.value = true; catalogError.value = 'sync.project.previewStale'; return }
  // Every value is captured here, before the first await; nothing is re-read later.
  const body = {
    expected_revision: s.identity_revision!, expected_generation: s.binding_generation,
    preview_id: record.result.preview_id, fingerprint: record.result.fingerprint,
    confirmation: { url: s.url, persistent_id: s.persistent_id!, action: 'reconcile' as const },
    config_fingerprint: s.config_fingerprint, backend_epoch: s.backend_epoch,
  }
  const current = rendererSession.capture()
  enableStep.value = 'enabling'
  catalogError.value = null
  // Single use: success, refusal and an unknown outcome all require a new Preview.
  dropPreview()
  let sent = false
  try {
    sent = true
    const result = await api.enableProjectRemote(body)
    current()
    if (!alive) return
    if (!result || result.enabled !== true || result.persistent_id !== s.persistent_id || result.url !== s.url
      || result.backend_epoch !== s.backend_epoch) throw new Error('unconfirmed enable')
    const loaded = await sync.loadRemote()
    if (!alive) return
    if (!loaded || sync.remote?.enabled !== true) { needsRefresh.value = true; catalogError.value = 'sync.project.enableUnknown'; return }
    projectNotice.value = 'sync.project.enabled'
  } catch (cause) {
    if (!alive) return
    // No typed backend refusal (network error, gateway, contradictory reply): unknown.
    if (!(cause instanceof ApiError) || cause.status === 0 || (cause.status >= 500 && !cause.code)) {
      // Transport failure or contradictory reply after sending: the outcome is unknown.
      needsRefresh.value = sent
      catalogError.value = sent ? 'sync.project.enableUnknown' : 'sync.project.enableRefused'
      return
    }
    const code = cause.code ?? ''
    if (['stale_binding', 'recovery_required', 'reload_required'].includes(code)) needsRefresh.value = true
    const mapped = previewMessage(cause)
    catalogError.value = cause.status === 409 && mapped === 'sync.identity.conflict' ? 'sync.project.enableRefused' : mapped
  } finally { if (alive) enableStep.value = null }
}
// ---------------------------------------------------------------- Checkpoint archive (§16.10)
// Renames (keeps) the v2 checkpoint of another incarnation. Markdown is untouched;
// the preview is consumed by every outcome and a NEW Preview is required.
const archiveCodes: Record<string, MessageKey> = {
  confirmation_required: 'sync.project.archive.error.confirmation_required',
  preview_not_found: 'sync.project.archive.error.preview_stale', preview_stale: 'sync.project.archive.error.preview_stale',
  preview_expired: 'sync.project.archive.error.preview_expired',
  checkpoint_current: 'sync.project.archive.error.checkpoint_current', already_enabled: 'sync.project.alreadyEnabled',
  recovery_required: 'sync.identity.recoveryRequired', change_in_progress: 'sync.project.archive.error.change_in_progress',
  stale_binding: 'sync.project.archive.error.stale_binding', checkpoint_unavailable: 'sync.project.archive.error.checkpoint_unavailable',
}
function reviewArchive() {
  const record = preview.value
  if (!record || !canArchive.value || archiveStep.value) return
  if (record.expiresAt <= Date.now()) { dropPreview(); catalogError.value = 'sync.project.previewExpired'; return }
  catalogError.value = null
  projectNotice.value = null
  archiveTarget.value = Object.freeze({ persistent_id: record.snapshot.persistent_id!, url: record.snapshot.url })
  archiveStep.value = 'review'
}
function cancelArchive() {
  if (archiveStep.value === 'review') { archiveStep.value = null; archiveTarget.value = null }
}
async function confirmArchive() {
  const record = preview.value
  if (archiveStep.value !== 'review' || !record || !canArchive.value) return
  if (record.expiresAt <= Date.now()) { dropPreview(); catalogError.value = 'sync.project.previewExpired'; return }
  const s = record.snapshot
  try {
    const ticket = sync.capture()
    ticket.assertCurrent()
    if (ticket.backend_epoch !== s.backend_epoch || ticket.expected_binding_generation !== s.binding_generation) throw new Error('stale')
  } catch { dropPreview(); needsRefresh.value = true; catalogError.value = 'sync.project.archive.error.preview_stale'; return }
  // Every value is captured here, before the first await; nothing is re-read later.
  const body: ProjectRemoteArchiveRequest = {
    expected_revision: s.identity_revision!, expected_generation: s.binding_generation,
    preview_id: record.result.preview_id, fingerprint: record.result.fingerprint,
    confirmation: { url: s.url, persistent_id: s.persistent_id!, action: 'archive_checkpoint' },
    config_fingerprint: s.config_fingerprint, backend_epoch: s.backend_epoch,
  }
  const current = rendererSession.capture()
  archiveStep.value = 'archiving'
  catalogError.value = null
  projectNotice.value = null
  // Single use: success, refusal and an unknown outcome all require a new Preview.
  dropPreview()
  try {
    const result = await api.archiveProjectRemoteCheckpoint(body)
    current()
    if (!alive) return
    if (!result || result.archived !== true || result.backend_epoch !== s.backend_epoch) throw new Error('unconfirmed archive')
    projectNotice.value = 'sync.project.archive.done'
  } catch (cause) {
    if (!alive) return
    if (!(cause instanceof ApiError) || cause.status === 0 || (cause.status >= 500 && !cause.code)) {
      // Sent, but no typed answer: the outcome is unknown. Never repeated.
      needsRefresh.value = true
      catalogError.value = 'sync.project.archive.unknown'
      return
    }
    const code = cause.code ?? ''
    if (['stale_binding', 'recovery_required', 'reload_required'].includes(code)) needsRefresh.value = true
    catalogError.value = archiveCodes[code] ?? 'sync.project.archive.error.refused'
  } finally { if (alive) { archiveStep.value = null; archiveTarget.value = null } }
}
// ---------------------------------------------------------------- Recovery (§16.10)
// Resolves an interrupted binding change: back to the previous destination or
// finishing the sync-off change. Never enables, uploads, downloads or retries.
const recoveryOperations: Record<string, MessageKey> = {
  enable: 'sync.project.recovery.operation.enable', edit_id: 'sync.project.recovery.operation.edit_id',
  select: 'sync.project.recovery.operation.select', disable: 'sync.project.recovery.operation.disable',
  unbind: 'sync.project.recovery.operation.unbind', migrate: 'sync.project.recovery.operation.migrate',
}
const recoveryCodes: Record<string, MessageKey> = {
  confirmation_required: 'sync.project.recovery.error.confirmation_required',
  stale_binding: 'sync.project.recovery.error.stale_binding',
  recovery_conflict: 'sync.project.recovery.error.recovery_conflict',
  recovery_ambiguous: 'sync.project.recovery.error.recovery_ambiguous',
  unknown_change: 'sync.project.recovery.error.unknown_change',
  change_in_progress: 'sync.project.recovery.error.change_in_progress',
  stale_backend_epoch: 'sync.project.recovery.error.stale_backend_epoch',
  reload_required: 'sync.project.recovery.error.reload_required',
  binding_journal_unavailable: 'sync.project.recovery.error.binding_journal_unavailable',
  consumer_lifecycle_unavailable: 'sync.project.recovery.error.consumer_lifecycle_unavailable',
  config_store_unavailable: 'sync.project.recovery.error.config_store_unavailable',
  binding_store_unavailable: 'sync.project.recovery.error.binding_store_unavailable',
  recovery_failed: 'sync.project.recovery.error.recovery_failed',
}
const recoveryOperation = computed<MessageKey>(() =>
  recoveryOperations[recovery.value?.operation ?? ''] ?? 'sync.project.recovery.operation.unknown')
const recoveryWellFormed = computed(() => {
  const r = recovery.value
  return !!r && typeof r.change_id === 'string' && /^[0-9a-f]{32}(?![\s\S])/.test(r.change_id)
    && (r.outcome === 'previous' || r.outcome === 'commit') && !!r.target && typeof r.target === 'object'
    && typeof r.target.url === 'string' && (r.target.persistent_id === null || typeof r.target.persistent_id === 'string')
})
/** Why the Recover command is not offered now; null when it is. */
const recoveryBlocked = computed<MessageKey | null>(() => {
  const r = recovery.value
  if (!r) return null
  if (!recoveryWellFormed.value) return 'sync.project.recovery.error.invalid'
  if (r.available !== true) return recoveryCodes[r.code ?? ''] ?? 'sync.project.recovery.unavailable'
  return null
})
const canRecover = computed(() => !!recovery.value && !recoveryBlocked.value && !!sync.remote
  && !sync.remotePending && !sync.reloadRequired && !needsRefresh.value && panelIdle.value && !recoveryStep.value)
function recoveryMatches(review: RecoveryReview) {
  const current = sync.remote
  const r = current?.recovery
  const old = review.snapshot
  return !!current && !!r && r.available === true && !sync.remotePending && !sync.reloadRequired
    && current.backend_epoch === old.backend_epoch && current.binding_generation === old.binding_generation
    && current.identity_revision === old.identity_revision && current.config_fingerprint === old.config_fingerprint
    && r.change_id === review.recovery.change_id && r.outcome === review.recovery.outcome
    && r.target?.url === review.recovery.target.url && r.target?.persistent_id === review.recovery.target.persistent_id
}
function reviewRecovery() {
  const current = sync.remote
  const r = current?.recovery
  if (!canRecover.value || !current || !r) return
  recoveryReview.value = Object.freeze({ snapshot: Object.freeze({ ...current }),
    recovery: Object.freeze({ ...r, target: Object.freeze({ ...r.target }) }) })
  recoveryStep.value = 'review'
  recoveryError.value = null
  recoveryNotice.value = null
}
function cancelRecovery() {
  if (recoveryStep.value === 'review') { recoveryStep.value = null; recoveryReview.value = null }
}
async function confirmRecovery() {
  const review = recoveryReview.value
  if (recoveryStep.value !== 'review' || !review) return
  const s = review.snapshot
  const r = review.recovery
  try {
    if (!recoveryMatches(review)) throw new Error('stale')
    const ticket = sync.capture()
    ticket.assertCurrent()
    if (ticket.backend_epoch !== s.backend_epoch || ticket.expected_binding_generation !== s.binding_generation) throw new Error('stale')
  } catch {
    recoveryStep.value = null
    recoveryReview.value = null
    needsRefresh.value = true
    recoveryError.value = 'sync.project.recovery.error.stale_binding'
    return
  }
  // Every value is captured from the reviewed GET snapshot, before the first await.
  const body: ProjectRemoteRecoverRequest = {
    change_id: r.change_id, expected_revision: s.identity_revision ?? 0, config_fingerprint: s.config_fingerprint,
    confirmation: { url: r.target.url, persistent_id: r.target.persistent_id, action: 'recover' },
    backend_epoch: s.backend_epoch,
  }
  const current = rendererSession.capture()
  recoveryStep.value = 'recovering'
  recoveryError.value = null
  try {
    const result = await api.recoverProjectRemote(body)
    current()
    if (!alive) return
    if (!result || result.recovered !== true || result.change_id !== r.change_id || result.enabled !== false
      || result.backend_epoch !== s.backend_epoch || result.url !== r.target.url
      || (result.persistent_id ?? null) !== r.target.persistent_id) throw new Error('unconfirmed recovery')
    // The receipt is reported only through the owning store's observation.
    const loaded = await sync.loadRemote()
    if (!alive) return
    recoveryNotice.value = 'sync.project.recovery.done'
    needsRefresh.value = !loaded
    if (loaded) id.value = sync.remote?.persistent_id ?? ''
    else recoveryError.value = 'sync.identity.unavailable'
  } catch (cause) {
    if (!alive) return
    // Every failure needs an explicit refresh before another explicit attempt.
    needsRefresh.value = true
    recoveryError.value = !(cause instanceof ApiError) || cause.status === 0 || (cause.status >= 500 && !cause.code)
      ? 'sync.project.recovery.error.unknown'
      : recoveryCodes[cause.code ?? ''] ?? 'sync.project.recovery.error.refused'
  } finally { if (alive) { recoveryStep.value = null; recoveryReview.value = null } }
}
// ---------------------------------------------------------------- Disable (§7)
function reviewDisable() {
  const current = sync.remote
  if (!canChange.value || !current || current.enabled !== true || !current.persistent_id) return
  try {
    intent.value = { ticket: sync.capture(), currentSession: rendererSession.capture(),
      snapshot: Object.freeze({ ...current }), target: current.persistent_id, targetUrl: current.url,
      operation: 'disable', initialEvent: sync.bindingChange, expectedRevision: current.identity_revision!,
      extra: current.profile_id ? { profile_id: current.profile_id } : {},
      binding: current.profile_id ? { profile_id: current.profile_id, space_instance_id: current.space_instance_id ?? null } : null,
      changeId: null, phase: 'review', cancelled: false }
    phase.value = 'review'
    notice.value = null
    error.value = null
    projectNotice.value = null
  } catch { needsRefresh.value = true; error.value = 'sync.identity.conflict' }
}
// ---------------------------------------------------------------- Legacy migration (I15, §9)
// Each step is a separate click: plan (read-only, no network) → profile →
// resolve (read-only catalog) → prepare/quiesce/commit(migrate). Nothing retries.
type Migration = {
  readonly plan: Readonly<ProjectRemoteMigrationPlan>
  readonly profile: Readonly<ProjectRemoteMigrationProfile> | null
  readonly resolution: Readonly<ProjectRemoteMigrationResolution> | null
}
const migration = shallowRef<Migration | null>(null)
const migrationError = ref<MessageKey | null>(null)
const migrationProfileId = ref('')
const migrationName = ref('')
const migrationImport = ref(false)
const migrationUrlConfirmed = ref(false)
const showMigration = computed(() => remote.value?.state === 'migration_required' || !!migration.value)
const planStates: Record<string, MessageKey> = {
  needs_identity: 'sync.migration.state.needs_identity', needs_profile: 'sync.migration.state.needs_profile',
  identity_present: 'sync.migration.state.identity_present', migration_required: 'sync.migration.state.migration_required',
  migration_conflict: 'sync.migration.state.migration_conflict', invalid_config: 'sync.migration.state.invalid_config',
}
const planReasons: Record<string, MessageKey> = {
  legacy_resolution_required: 'sync.migration.reason.legacy_resolution_required',
  legacy_selector_missing: 'sync.migration.reason.legacy_selector_missing',
  identity_selector_conflict: 'sync.migration.reason.identity_selector_conflict',
  invalid_legacy_selector: 'sync.migration.reason.invalid_legacy_selector',
  invalid_legacy_url: 'sync.migration.reason.invalid_legacy_url', invalid_config: 'sync.migration.reason.invalid_config',
  missing_config: 'sync.migration.reason.missing_config', unreadable_config: 'sync.migration.reason.unreadable_config',
  guarded_cutover_required: 'sync.migration.reason.guarded_cutover_required',
  explicit_identity_required: 'sync.migration.reason.explicit_identity_required',
}
const credentialKeys: Record<ProjectRemoteMigrationProfile['credential'], MessageKey> = {
  stored: 'sync.migration.credential.stored', environment: 'sync.migration.credential.environment',
  missing: 'sync.migration.credential.missing',
}
const migrationReasons = computed(() => [...new Set((migration.value?.plan.reasons ?? [])
  .map(reason => planReasons[reason] ?? 'sync.migration.reason.unrecognised' as MessageKey))])
const planCurrent = computed(() => !!migration.value && !!remote.value
  && migration.value.plan.config_fingerprint === remote.value.config_fingerprint)
const matchingProfiles = computed(() => servers.items.filter(item => !!migration.value?.plan.legacy_url
  && item.url === migration.value.plan.legacy_url))
const migrationIdle = computed(() => panelIdle.value && !sync.remotePending && !sync.reloadRequired && !needsRefresh.value
  && !sync.remote?.recovery)
const canSaveProfile = computed(() => {
  const plan = migration.value?.plan
  return migrationIdle.value && planCurrent.value && !!plan && plan.state === 'needs_profile'
    && !!plan.legacy_url && !!plan.config_fingerprint && !migration.value!.profile && migrationUrlConfirmed.value
    && (migrationProfileId.value ? matchingProfiles.value.some(item => item.profile_id === migrationProfileId.value)
      : !!migrationName.value.trim())
})
const canResolve = computed(() => migrationIdle.value && planCurrent.value && !!migration.value?.profile
  && migration.value.profile.credential !== 'missing' && !migration.value.resolution)
const canApply = computed(() => {
  const m = migration.value
  const current = sync.remote
  return migrationIdle.value && planCurrent.value && !!m?.profile && !!m.resolution && !m.resolution.conflicts.length
    && !!m.plan.legacy_url && !!current && current.state !== 'quiescing' && current.state !== 'blocked'
    && Number.isSafeInteger(current.binding_generation + 2)
})
function migrationMessage(cause: unknown): MessageKey {
  if (!(cause instanceof ApiError)) return 'sync.migration.error.failed'
  const code = cause.code ?? ''
  const known: Record<string, MessageKey> = {
    credential_conflict: 'sync.migration.error.credentialConflict',
    credential_binding_conflict: 'sync.migration.error.credentialConflict',
    profile_url_collision: 'sync.migration.error.urlCollision',
    profile_revision_conflict: 'sync.migration.error.stale', config_revision_conflict: 'sync.migration.error.stale',
    metadata_conflict: 'sync.migration.error.stale', stale_binding: 'sync.migration.error.stale',
    stale_plan: 'sync.migration.error.stale', migration_plan_stale: 'sync.migration.error.stale',
    profile_not_found: 'sync.migration.error.stale',
    legacy_space_not_found: 'sync.migration.error.notFound', project_not_found: 'sync.migration.error.notFound',
    migration_conflict: 'sync.migration.error.conflict', identity_selector_conflict: 'sync.migration.error.conflict',
    instance_mismatch: 'sync.migration.error.conflict',
    credential_required: 'sync.migration.error.credentialMissing', credential_missing: 'sync.migration.error.credentialMissing',
    credential_binding_unproven: 'sync.migration.error.credentialMissing', unauthorised: 'sync.migration.error.credentialMissing',
    legacy_completeness_unproven: 'sync.migration.error.gate', release_gate_closed: 'sync.migration.error.gate',
    gate_closed: 'sync.migration.error.gate', migration_not_released: 'sync.migration.error.gate',
    // I15 backend codes (api/sync.py migration routes).
    consumer_lifecycle_unavailable: 'sync.migration.error.gate',
    remote_unauthorised: 'sync.migration.error.credentialMissing',
    profile_url_mismatch: 'sync.migration.error.urlCollision',
    legacy_mapping_mismatch: 'sync.migration.error.conflict', duplicate_local_binding: 'sync.migration.error.conflict',
    migration_not_applicable: 'sync.migration.error.stale', resolution_required: 'sync.migration.error.stale',
    resolution_expired: 'sync.migration.error.stale', resolution_stale: 'sync.migration.error.stale',
    confirmation_required: 'sync.migration.error.stale', change_in_progress: 'sync.migration.error.stale',
  }
  if (known[code]) return known[code]
  if (code.includes('gate')) return 'sync.migration.error.gate'
  if (cause.status === 404 || cause.status >= 500 || code.endsWith('_unavailable') || code.endsWith('_corrupt')) return 'sync.migration.error.unavailable'
  if (cause.status === 422) return 'sync.migration.error.invalid'
  return 'sync.migration.error.failed'
}
function resetMigration() {
  migration.value = null
  migrationProfileId.value = ''
  migrationName.value = ''
  migrationImport.value = false
  migrationUrlConfirmed.value = false
  migrationError.value = null
}
const nullableString = (value: unknown) => value === undefined || value === null || typeof value === 'string'
async function loadMigration() {
  if (!migrationIdle.value) return
  const current = rendererSession.capture()
  catalogBusy.value = true
  resetMigration()
  try {
    const plan = await api.remoteMigration()
    current()
    if (!alive) return
    if (!plan || typeof plan.migration_plan_id !== 'string' || !plan.migration_plan_id
      || !(plan.config_fingerprint === null || (typeof plan.config_fingerprint === 'string' && /^[0-9a-f]{64}(?![\s\S])/.test(plan.config_fingerprint)))
      || !nullableString(plan.persistent_id) || !nullableString(plan.legacy_url) || !nullableString(plan.legacy_selector)
      || !(plan.identity_revision == null || (Number.isSafeInteger(plan.identity_revision) && plan.identity_revision >= 1))
      || !(plan.has_legacy_key == null || typeof plan.has_legacy_key === 'boolean')
      || typeof plan.state !== 'string' || !Array.isArray(plan.reasons)
      || plan.reasons.some(reason => typeof reason !== 'string')) throw new Error('invalid plan')
    migration.value = { plan: Object.freeze({ ...plan, reasons: [...plan.reasons] }), profile: null, resolution: null }
  } catch (cause) { if (alive) migrationError.value = migrationMessage(cause) }
  finally { if (alive) catalogBusy.value = false }
}
async function saveMigrationProfile() {
  const m = migration.value
  if (!m || !canSaveProfile.value) return
  const plan = m.plan
  const chosen = migrationProfileId.value
  const body = {
    config_fingerprint: plan.config_fingerprint!,
    ...(chosen ? { profile_id: chosen } : { name: migrationName.value.trim() }),
    import_legacy_credential: plan.has_legacy_key === true && migrationImport.value,
    confirmation: { url: plan.legacy_url! },
  }
  const current = rendererSession.capture()
  catalogBusy.value = true
  migrationError.value = null
  try {
    const result = await api.selectRemoteMigrationProfile(plan.migration_plan_id, body)
    current()
    if (!alive || migration.value !== m) return
    if (!result || result.migration_plan_id !== plan.migration_plan_id || typeof result.profile_id !== 'string'
      || !result.profile_id || (chosen && result.profile_id !== chosen)
      || !Number.isSafeInteger(result.profile_revision) || result.profile_revision < 1
      || result.url !== plan.legacy_url || !(result.credential in credentialKeys)) throw new Error('invalid profile result')
    migration.value = { plan, profile: Object.freeze({ ...result }), resolution: null }
  } catch (cause) { if (alive) migrationError.value = migrationMessage(cause) }
  finally { if (alive) catalogBusy.value = false }
}
async function resolveMigration() {
  const m = migration.value
  if (!m?.profile || !canResolve.value) return
  const { plan, profile } = m
  const body = { profile_id: profile.profile_id, expected_profile_revision: profile.profile_revision,
    config_fingerprint: plan.config_fingerprint! }
  const current = rendererSession.capture()
  catalogBusy.value = true
  migrationError.value = null
  try {
    const result = await api.resolveRemoteMigration(plan.migration_plan_id, body)
    current()
    if (!alive || migration.value !== m) return
    if (!result || result.migration_plan_id !== plan.migration_plan_id || result.legacy_selector !== plan.legacy_selector
      || typeof result.catalog_id !== 'string' || !ID_PATTERN.test(result.catalog_id)
      || typeof result.space_instance_id !== 'string' || !result.space_instance_id
      || !Array.isArray(result.conflicts)) throw new Error('invalid resolution')
    migration.value = { plan, profile, resolution: Object.freeze({ ...result }) }
  } catch (cause) { if (alive) migrationError.value = migrationMessage(cause) }
  finally { if (alive) catalogBusy.value = false }
}
function reviewMigration() {
  const m = migration.value
  const current = sync.remote
  if (!m?.profile || !m.resolution || !current || !canApply.value) return
  try {
    intent.value = { ticket: sync.capture(), currentSession: rendererSession.capture(),
      snapshot: Object.freeze({ ...current }), target: m.resolution.catalog_id, targetUrl: m.plan.legacy_url!,
      operation: 'migrate', initialEvent: sync.bindingChange,
      expectedRevision: m.plan.identity_revision ?? MIGRATION_REVISION_WITHOUT_IDENTITY,
      extra: { migration_plan_id: m.plan.migration_plan_id, profile_id: m.profile.profile_id,
        catalog_id: m.resolution.catalog_id, catalog_instance_id: m.resolution.space_instance_id },
      binding: { profile_id: m.profile.profile_id, space_instance_id: m.resolution.space_instance_id },
      changeId: null, phase: 'review', cancelled: false }
    phase.value = 'review'
    notice.value = null
    error.value = null
    migrationError.value = null
  } catch { needsRefresh.value = true; error.value = 'sync.identity.conflict' }
}
watch(() => [sync.remote?.binding_generation, sync.remote?.identity_revision, sync.remote?.config_fingerprint,
  sync.remote?.backend_epoch, sync.remote?.persistent_id, sync.remote?.url, sync.remote?.enabled,
  sync.remote?.profile_id, sync.remote?.space_instance_id, sync.remotePending, sync.reloadRequired,
  sync.bindingChange, sync.remote?.recovery], () => {
  if (preview.value && !previewMatches(preview.value)) dropPreview()
  if (recoveryReview.value && recoveryStep.value === 'review' && !recoveryMatches(recoveryReview.value)) cancelRecovery()
})
onMounted(() => { sync.subscribe(); void refresh() })
onBeforeUnmount(() => {
  alive = false
  if (previewTimer !== undefined) clearTimeout(previewTimer)
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
      <dt class="text-muted">{{ t('sync.project.profileId') }}</dt>
      <dd data-testid="remote-project-profile-id" class="font-mono break-all">{{ remote?.profile_id || t('sync.project.none') }}</dd>
      <dt class="text-muted">{{ t('sync.project.instance') }}</dt>
      <dd data-testid="remote-project-instance" class="font-mono break-all">{{ remote?.space_instance_id || t('sync.project.none') }}</dd>
    </dl>
    <p v-if="remote?.reason" class="text-small text-warning">{{ t(message(new ApiError(409, '', remote.reason))) }}</p>
    <p v-if="offlineIdAttempt" data-testid="remote-id-offline-attempt" class="text-small text-warning">{{ t('sync.identity.offlineAttempt') }}</p>
    <section v-if="recovery" data-testid="remote-recovery" class="space-y-2 rounded-md border border-line p-3" :aria-label="t('sync.project.recovery.title')">
      <h3 class="font-medium">{{ t('sync.project.recovery.title') }}</h3>
      <p data-testid="remote-recovery-reason" class="text-small text-warning">{{ t('sync.project.recovery.reason', { operation: t(recoveryOperation) }) }}</p>
      <p data-testid="remote-recovery-outcome" class="text-small">{{ t(recovery.outcome === 'commit' ? 'sync.project.recovery.outcome.commit' : 'sync.project.recovery.outcome.previous') }}</p>
      <dl class="grid grid-cols-[8rem_1fr] gap-1">
        <dt class="text-muted">{{ t('sync.project.recovery.targetId') }}</dt>
        <dd data-testid="remote-recovery-id" class="font-mono break-all">{{ recovery.target?.persistent_id ?? t('sync.project.recovery.legacyId') }}</dd>
        <dt class="text-muted">{{ t('sync.settings.url') }}</dt>
        <dd data-testid="remote-recovery-url" class="font-mono break-all">{{ recovery.target?.url || t('sync.identity.noServer') }}</dd>
      </dl>
      <p v-if="recovery.phase" class="text-small text-faint break-all">{{ t('sync.project.recovery.phase', { phase: recovery.phase }) }}</p>
      <p v-if="recoveryBlocked" data-testid="remote-recovery-unavailable" role="alert" class="text-small text-warning">{{ t(recoveryBlocked) }}</p>
      <button v-else-if="!recoveryStep" data-testid="remote-recovery-start" type="button" :disabled="!canRecover" @click="reviewRecovery"
        class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('sync.project.recovery.recover') }}</button>
      <div v-if="recoveryStep && recoveryReview" data-testid="remote-recovery-review" class="space-y-2 rounded-md border border-line p-3">
        <p class="font-medium">{{ t('sync.project.recovery.reviewTitle') }}</p>
        <p class="text-small">{{ t(recoveryReview.recovery.outcome === 'commit' ? 'sync.project.recovery.outcome.commit' : 'sync.project.recovery.outcome.previous') }}</p>
        <dl class="grid grid-cols-[8rem_1fr] gap-1 font-mono break-all">
          <dt>{{ t('sync.project.recovery.targetId') }}</dt>
          <dd data-testid="remote-recovery-review-id">{{ recoveryReview.recovery.target.persistent_id ?? t('sync.project.recovery.legacyId') }}</dd>
          <dt>{{ t('sync.settings.url') }}</dt>
          <dd data-testid="remote-recovery-review-url">{{ recoveryReview.recovery.target.url || t('sync.identity.noServer') }}</dd>
        </dl>
        <p class="text-small">{{ t('sync.project.recovery.reviewHint') }}</p>
        <p v-if="recoveryStep === 'recovering'" role="status">{{ t('sync.project.recovery.recovering') }}</p>
        <div class="flex gap-2">
          <button data-testid="remote-recovery-confirm" type="button" :disabled="recoveryStep !== 'review'" @click="confirmRecovery"
            class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('sync.project.recovery.confirm') }}</button>
          <button data-testid="remote-recovery-cancel" type="button" :disabled="recoveryStep !== 'review'" @click="cancelRecovery"
            class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('common.cancel') }}</button>
        </div>
      </div>
    </section>
    <p v-if="recoveryError" data-testid="remote-recovery-error" role="alert" class="text-small text-danger">{{ t(recoveryError) }}</p>
    <p v-if="recoveryNotice" data-testid="remote-recovery-notice" role="status" class="text-small">{{ t(recoveryNotice) }}</p>
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
      <p class="font-medium">{{ t(intentText[intent.operation].title) }}</p>
      <p class="text-small">{{ t(intentText[intent.operation].hint) }}</p>
      <dl class="grid grid-cols-[8rem_1fr] gap-1 font-mono break-all">
        <dt>{{ t('sync.identity.current') }}</dt><dd>{{ intent.snapshot.persistent_id ?? t('sync.identity.missing') }}</dd>
        <dt>{{ t('sync.identity.newId') }}</dt><dd>{{ intent.target }}</dd>
        <dt>{{ t('sync.settings.url') }}</dt><dd>{{ intent.targetUrl || t('sync.identity.noServer') }}</dd>
      </dl>
      <p v-if="phase && phase !== 'review' && phase !== 'blocked'" role="status">{{ t(phase === 'committing' ? 'sync.identity.committing' : phase === 'cancelling' ? 'sync.identity.cancelling' : 'sync.identity.draining') }}</p>
      <div class="flex gap-2">
        <button v-if="phase === 'review'" data-testid="remote-id-confirm" type="button" @click="confirm"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover">{{ t(intentText[intent.operation].confirm) }}</button>
        <button data-testid="remote-id-cancel" type="button" :disabled="phase === 'committing' || phase === 'cancelling'" @click="cancel"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50">{{ t('common.cancel') }}</button>
      </div>
    </div>
    <p v-if="error" data-testid="remote-id-error" role="alert" class="text-small text-danger">{{ t(error) }}</p>
    <p v-if="notice" data-testid="remote-id-notice" role="status" class="text-small">{{ t(notice) }}</p>
    <button data-testid="remote-id-refresh" type="button" :disabled="!!intent || loading || recoveryStep === 'recovering'" @click="refresh"
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
      <button data-testid="remote-project-preview" type="button" :disabled="!canPreview" @click="readPreview"
        class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.preview') }}</button>
      <p v-if="!remote?.profile_id || !remote?.space_instance_id" class="text-small text-muted">{{ t('sync.project.previewGate') }}</p>
      <div v-if="preview" data-testid="remote-project-preview-result" role="status" class="space-y-1 rounded-md border border-line p-2">
        <p>{{ t('sync.project.previewResult', { conflicts: preview.result.conflicts.length }) }}</p>
        <p class="font-medium">{{ t('sync.project.countsTitle') }}</p>
        <ul class="text-small">
          <li v-for="row in previewCounts" :key="row.key">{{ t(row.key) }}: {{ row.count }}</li>
        </ul>
        <p v-if="preview.result.base && baseKeys[preview.result.base]" class="text-small">{{ t(baseKeys[preview.result.base]!) }}</p>
        <p v-if="preview.result.incarnation" class="text-small font-mono break-all">{{ t('sync.project.incarnation', {
          instance: preview.result.incarnation.space_instance_id, seq: preview.result.incarnation.seq }) }}</p>
      </div>
      <div v-if="archiveBase" data-testid="remote-project-archive-panel" class="space-y-2 rounded-md border border-line p-2">
        <p class="font-medium">{{ t('sync.project.archive.title') }}</p>
        <p data-testid="remote-project-archive-reason" class="text-small text-warning">{{ t(archiveBase === 'epoch_mismatch'
          ? 'sync.project.archive.epoch_mismatch' : 'sync.project.archive.cursor_rollback') }}</p>
        <p class="text-small">{{ t('sync.project.archive.hint') }}</p>
        <button data-testid="remote-project-archive" type="button" :disabled="!canArchive || !!archiveStep" @click="reviewArchive"
          class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.archive.archive') }}</button>
      </div>
      <div v-if="archiveStep && archiveTarget" data-testid="remote-project-archive-review" class="space-y-2 rounded-md border border-line p-3">
        <p class="font-medium">{{ t('sync.project.archive.reviewTitle') }}</p>
        <dl class="grid grid-cols-[8rem_1fr] gap-1 font-mono break-all">
          <dt>{{ t('sync.identity.current') }}</dt><dd data-testid="remote-project-archive-id">{{ archiveTarget.persistent_id }}</dd>
          <dt>{{ t('sync.settings.url') }}</dt><dd data-testid="remote-project-archive-url">{{ archiveTarget.url }}</dd>
        </dl>
        <p class="text-small">{{ t('sync.project.archive.reviewHint') }}</p>
        <p v-if="archiveStep === 'archiving'" role="status">{{ t('sync.project.archive.archiving') }}</p>
        <div class="flex gap-2">
          <button data-testid="remote-project-archive-confirm" type="button" :disabled="archiveStep !== 'review' || !canArchive"
            @click="confirmArchive" class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.archive.confirm') }}</button>
          <button data-testid="remote-project-archive-cancel" type="button" :disabled="archiveStep !== 'review'" @click="cancelArchive"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('common.cancel') }}</button>
        </div>
      </div>
      <button data-testid="remote-project-enable" type="button" :disabled="!canEnable || !!enableStep" @click="reviewEnable"
        class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.enable') }}</button>
      <p v-if="!canEnable && !enableStep && !archiveBase" class="text-small text-warning">{{ t('sync.project.enableGate') }}</p>
      <div v-if="enableStep" data-testid="remote-project-enable-review" class="space-y-2 rounded-md border border-line p-3">
        <p class="font-medium">{{ t('sync.project.enableTitle') }}</p>
        <p class="text-small">{{ t('sync.project.enableHint') }}</p>
        <dl v-if="preview" class="grid grid-cols-[8rem_1fr] gap-1 font-mono break-all">
          <dt>{{ t('sync.identity.current') }}</dt><dd data-testid="remote-project-enable-id">{{ preview.snapshot.persistent_id }}</dd>
          <dt>{{ t('sync.settings.url') }}</dt><dd data-testid="remote-project-enable-url">{{ preview.snapshot.url }}</dd>
          <dt>{{ t('sync.project.profileId') }}</dt><dd>{{ preview.snapshot.profile_id }}</dd>
          <dt>{{ t('sync.project.instance') }}</dt><dd>{{ preview.snapshot.space_instance_id }}</dd>
        </dl>
        <ul v-if="preview" class="text-small">
          <li v-for="row in previewCounts" :key="row.key">{{ t(row.key) }}: {{ row.count }}</li>
        </ul>
        <template v-if="preview?.result.conflicts.length">
          <p class="font-medium">{{ t('sync.project.conflictsTitle') }}</p>
          <ul data-testid="remote-project-enable-conflicts" class="text-small font-mono break-all">
            <li v-for="path in preview.result.conflicts.slice(0, MAX_SHOWN_CONFLICTS)" :key="path"><bdi>{{ path }}</bdi></li>
          </ul>
          <p v-if="preview.result.conflicts.length > MAX_SHOWN_CONFLICTS" class="text-small">{{ t('sync.project.moreConflicts', {
            count: preview.result.conflicts.length - MAX_SHOWN_CONFLICTS }) }}</p>
        </template>
        <p v-if="enableStep === 'enabling'" role="status">{{ t('sync.project.enabling') }}</p>
        <div class="flex gap-2">
          <button data-testid="remote-project-enable-confirm" type="button" :disabled="enableStep !== 'review' || !canEnable"
            @click="confirmEnable" class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.enableConfirm') }}</button>
          <button data-testid="remote-project-enable-cancel" type="button" :disabled="enableStep !== 'review'" @click="cancelEnable"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('common.cancel') }}</button>
        </div>
      </div>
      <button v-if="remote?.enabled" data-testid="remote-project-disable" type="button" :disabled="!canChange || !remote.persistent_id"
        @click="reviewDisable" class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.disable') }}</button>
      <p v-if="projectNotice" data-testid="remote-project-notice" role="status" class="text-small">{{ t(projectNotice) }}</p>
      <p v-if="catalogBusy" role="status">{{ t('sync.project.loading') }}</p>
      <p v-if="catalogError" data-testid="remote-project-error" role="alert" class="text-small text-danger">{{ t(catalogError) }}</p>
    </section>
    <section v-if="showMigration" data-testid="remote-migration" class="space-y-3 border-t border-line pt-3" :aria-label="t('sync.migration.title')">
      <h3 class="font-medium">{{ t('sync.migration.title') }}</h3>
      <p class="text-small text-faint">{{ t('sync.migration.hint') }}</p>
      <button data-testid="remote-migration-load" type="button" :disabled="!migrationIdle" @click="loadMigration"
        class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.migration.load') }}</button>
      <template v-if="migration">
        <dl class="grid grid-cols-[8rem_1fr] gap-1">
          <dt class="text-muted">{{ t('sync.migration.legacyUrl') }}</dt>
          <dd data-testid="remote-migration-url" class="font-mono break-all">{{ migration.plan.legacy_url || t('sync.project.none') }}</dd>
          <dt class="text-muted">{{ t('sync.migration.legacySelector') }}</dt>
          <dd data-testid="remote-migration-selector" class="font-mono break-all"><bdi>{{ migration.plan.legacy_selector || t('sync.project.none') }}</bdi></dd>
          <dt class="text-muted">{{ t('sync.migration.legacyKey') }}</dt>
          <dd>{{ t(migration.plan.has_legacy_key === true ? 'sync.migration.keyPresent'
            : migration.plan.has_legacy_key === false ? 'sync.migration.keyAbsent' : 'sync.migration.keyUnknown') }}</dd>
          <dt class="text-muted">{{ t('sync.migration.planState') }}</dt>
          <dd data-testid="remote-migration-state">{{ t(planStates[migration.plan.state] ?? 'sync.migration.state.unsupported') }}</dd>
        </dl>
        <ul class="text-small text-muted">
          <li v-for="reason in migrationReasons" :key="reason">{{ t(reason) }}</li>
        </ul>
        <p v-if="!planCurrent" data-testid="remote-migration-stale" class="text-small text-warning">{{ t('sync.migration.stale') }}</p>
        <form v-if="migration.plan.state === 'needs_profile'" class="space-y-2" @submit.prevent="saveMigrationProfile">
          <p class="font-medium">{{ t('sync.migration.profileStep') }}</p>
          <button data-testid="remote-migration-servers" type="button" :disabled="!migrationIdle || !!migration.profile" @click="loadServers"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.project.loadServers') }}</button>
          <label class="block">{{ t('sync.migration.profileExisting') }}
            <select v-model="migrationProfileId" data-testid="remote-migration-profile" :disabled="!migrationIdle || !!migration.profile"
              class="w-full rounded-md border border-line bg-inset px-2 py-1">
              <option value="">{{ t('sync.migration.profileNew') }}</option>
              <option v-for="profile in matchingProfiles" :key="profile.profile_id" :value="profile.profile_id">{{ profile.name }} — {{ profile.url }} — {{ profile.profile_id }}</option>
            </select>
          </label>
          <p v-if="!matchingProfiles.length" class="text-small text-muted">{{ t('sync.migration.noMatching') }}</p>
          <label v-if="!migrationProfileId" class="block">{{ t('sync.migration.profileName') }}
            <input v-model="migrationName" data-testid="remote-migration-name" :disabled="!migrationIdle || !!migration.profile"
              class="w-full rounded-md border border-line bg-inset px-2 py-1" autocomplete="off" /></label>
          <label class="flex items-center gap-2">
            <input v-model="migrationImport" data-testid="remote-migration-import" type="checkbox"
              :disabled="!migrationIdle || !!migration.profile || migration.plan.has_legacy_key !== true" />
            {{ t('sync.migration.importKey') }}</label>
          <label class="flex items-center gap-2">
            <input v-model="migrationUrlConfirmed" data-testid="remote-migration-url-confirm" type="checkbox"
              :disabled="!migrationIdle || !!migration.profile" />
            <span class="break-all">{{ t('sync.migration.confirmUrl', { url: migration.plan.legacy_url ?? '' }) }}</span></label>
          <button data-testid="remote-migration-profile-submit" type="submit" :disabled="!canSaveProfile"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.migration.profileSubmit') }}</button>
        </form>
        <template v-if="migration.profile">
          <dl data-testid="remote-migration-profile-result" class="grid grid-cols-[8rem_1fr] gap-1">
            <dt class="text-muted">{{ t('sync.project.profileId') }}</dt><dd class="font-mono break-all">{{ migration.profile.profile_id }}</dd>
            <dt class="text-muted">{{ t('sync.settings.url') }}</dt><dd class="font-mono break-all">{{ migration.profile.url }}</dd>
            <dt class="text-muted">{{ t('sync.migration.profileKey') }}</dt><dd>{{ t(credentialKeys[migration.profile.credential]) }}</dd>
          </dl>
          <p class="font-medium">{{ t('sync.migration.resolveStep') }}</p>
          <p v-if="migration.profile.credential === 'missing'" class="text-small text-warning">{{ t('sync.migration.resolveNeedsKey') }}</p>
          <button data-testid="remote-migration-resolve" type="button" :disabled="!canResolve" @click="resolveMigration"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.migration.resolve') }}</button>
        </template>
        <template v-if="migration.resolution">
          <dl data-testid="remote-migration-resolution" class="grid grid-cols-[8rem_1fr] gap-1 font-mono break-all">
            <dt>{{ t('sync.migration.legacySelector') }}</dt><dd><bdi>{{ migration.resolution.legacy_selector }}</bdi></dd>
            <dt>{{ t('sync.migration.catalogId') }}</dt><dd>{{ migration.resolution.catalog_id }}</dd>
            <dt>{{ t('sync.project.instance') }}</dt><dd>{{ migration.resolution.space_instance_id }}</dd>
            <dt>{{ t('sync.settings.url') }}</dt><dd>{{ migration.plan.legacy_url }}</dd>
          </dl>
          <p v-if="migration.resolution.conflicts.length" data-testid="remote-migration-conflicts" class="text-small text-warning">{{
            t('sync.migration.resolveConflicts', { count: migration.resolution.conflicts.length }) }}</p>
          <p class="font-medium">{{ t('sync.migration.applyStep') }}</p>
          <button data-testid="remote-migration-apply" type="button" :disabled="!canApply" @click="reviewMigration"
            class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50">{{ t('sync.migration.apply') }}</button>
        </template>
      </template>
      <p v-if="migrationError" data-testid="remote-migration-error" role="alert" class="text-small text-danger">{{ t(migrationError) }}</p>
    </section>
  </div>
</template>
