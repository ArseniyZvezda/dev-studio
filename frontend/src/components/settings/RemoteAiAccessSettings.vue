<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { aiAccessApi, AiAccessError, type AiDevice, type AiExport, type AiExportInput,
  type AiGrant, type AiGrantInput, type AiGrantReceipt, type AiPermission, type AiPolicy,
  type AiRegistration, type AiResourceScope, type AiTarget } from '@/api/ai-access'
import { api } from '@/api/client'
import { createRemoteApiClient, type RemoteServerProfile } from '@/api/remote'
import type { ProjectRemoteStatus, Team } from '@/api/types'
import { formatDate, t } from '@/i18n'
import { rendererSession } from '@/session/project-session'

const props = defineProps<{ project?: boolean }>()
const profilesApi = createRemoteApiClient()
const profiles = shallowRef<RemoteServerProfile[]>([])
const registrations = shallowRef<AiRegistration[]>([])
const exports = shallowRef<AiExport[]>([])
const binding = shallowRef<ProjectRemoteStatus | null>(null)
const busy = ref(false)
const needsRefresh = ref(true)
const stale = () => new AiAccessError(409, 'stale_target')
const error = ref<'loadFailed' | 'failed' | 'offline' | 'unknown' | 'stale' | 'notAvailable' | null>(null)
let alive = true

const profileId = ref('')
const profile = computed(() => profiles.value.find(row => row.profile_id === profileId.value))
const checked = shallowRef<{ profile: RemoteServerProfile; server: string } | null>(null)
const checkResult = ref<'unsupported' | 'discoveryAuth' | 'discoveryUnavailable' | null>(null)
const deviceName = ref('')
const unregisterReview = shallowRef<AiRegistration | null>(null)
const draftName = ref('')
const executorId = ref('')
const draftConsent = ref(false)
// GET exports is device-wide and never identifies a local root. Keep the proof
// from this panel's POST only, and discard it on refresh/navigation.
const newDraftId = ref('')
const savedId = ref('')
const entry = computed(() => exports.value.find(row => row.target.export_id === (savedId.value || newDraftId.value)))
const entryRegistration = computed(() => registrations.value.find(row => row.registration_id === entry.value?.target.executor_registration_id
  && row.server_instance_id === entry.value.target.server_instance_id))

const directory = shallowRef<AiDevice[] | null>(null)
const grants = shallowRef<AiGrantReceipt[] | null>(null)
const teams = shallowRef<Team[]>([])
const edit = shallowRef<AiGrant | null>(null)
const deviceId = ref('')
const teamId = ref('')
const memberId = ref('')
const operations = ref<AiPermission[]>([])
const scope = ref<AiResourceScope>('created_by_this_grantee')
const resourceText = ref('')
const hours = ref(24)
const grantConsent = ref(false)
type Plan = {
  target: AiTarget
  actor: Pick<AiDevice, 'registration_id' | 'user_id'>
  options: Omit<AiGrantInput, 'grantee_user_id' | 'grantee_registration_id' | 'execution_policy_digest' | 'expires_at'>
  hours: number
  edit: { id: string; revision: number } | null
}
const preview = shallowRef<{ plan: Plan; policy: AiPolicy } | null>(null)
const permissions: AiPermission[] = ['view_status', 'view_transcript', 'create_task', 'edit_task', 'run_task',
  'write_task', 'cancel_task', 'create_chat', 'run_chat', 'cancel_chat', 'operate_existing']
const scopes: AiResourceScope[] = ['created_by_this_grantee', 'selected_resources', 'all_resources_in_selected_teams']
const lifetimeLabels = { 1: 'oneHour', 24: 'oneDay', 168: 'sevenDays' } as const
const members = computed(() => teams.value.find(team => team.id === teamId.value)?.members.filter(member => member.kind === 'agent') ?? [])
const ownScope = computed(() => !rendererSession.isLegacy && rendererSession.scope !== null)
const matching = computed(() => registrations.value.filter(row => row.profile_id === binding.value?.profile_id
  && row.url === binding.value.url && row.has_device_token))
const executor = computed(() => matching.value.find(row => row.registration_id === executorId.value))
const ready = computed(() => !busy.value && !needsRefresh.value)
const canAdministerGrant = computed(() => entry.value?.enabled && entry.value.mirror_state === 'effective'
  && entryRegistration.value?.publication_enabled && entryRegistration.value.has_device_token)

function isRevision(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1
}
function selected(status: ProjectRemoteStatus): boolean {
  return rendererSession.matches(status) && !!status.url && !!status.profile_id && !!status.persistent_id
    && !!status.space_instance_id && isRevision(status.identity_revision)
    && !status.binding_change && !status.recovery
    && !['migration_required', 'binding_required', 'quiescing', 'blocked', 'unconfigured'].includes(status.state)
}
function sameBinding(a: ProjectRemoteStatus, b: ProjectRemoteStatus): boolean {
  return selected(b) && a.project_id === b.project_id && a.epoch === b.epoch && a.backend_epoch === b.backend_epoch
    && a.url === b.url && a.profile_id === b.profile_id && a.persistent_id === b.persistent_id
    && a.space_instance_id === b.space_instance_id && a.identity_revision === b.identity_revision
    && a.binding_generation === b.binding_generation && a.config_fingerprint === b.config_fingerprint
}
function sameProfile(a: RemoteServerProfile, b: RemoteServerProfile): boolean {
  return a.profile_id === b.profile_id && a.url === b.url && a.revision === b.revision && b.has_token
}
function sameTarget(a: AiTarget, b: AiTarget, revision = true): boolean {
  return a.server_instance_id === b.server_instance_id && a.executor_registration_id === b.executor_registration_id
    && a.export_id === b.export_id && a.persistent_id === b.persistent_id && a.storage_epoch === b.storage_epoch
    && (!revision || a.export_revision === b.export_revision)
}
function validName(name: string): boolean {
  return !!name.trim() && new TextEncoder().encode(name.trim().normalize('NFC')).length <= 480
}
function invalidatePreview() { preview.value = null; grantConsent.value = false }
function resetGrant() {
  edit.value = null; deviceId.value = ''; teamId.value = ''; memberId.value = ''; operations.value = []
  scope.value = 'created_by_this_grantee'; resourceText.value = ''; hours.value = 24
  invalidatePreview()
}
function resetEntry() { directory.value = null; grants.value = null; teams.value = []; resetGrant() }
watch([executorId, draftName], () => { draftConsent.value = false }, { flush: 'sync' })
watch(profileId, () => { checked.value = null; checkResult.value = null }, { flush: 'sync' })
watch(teamId, () => { memberId.value = '' }, { flush: 'sync' })
watch([deviceId, teamId, memberId, operations, scope, resourceText, hours], invalidatePreview, { deep: true, flush: 'sync' })
watch(savedId, resetEntry, { flush: 'sync' })

async function perform(kind: 'read' | 'write' | 'refresh', action: (current: () => void) => Promise<void>) {
  if (!alive || busy.value || (kind !== 'refresh' && needsRefresh.value)) return
  busy.value = true
  error.value = null
  try {
    const assertSession = rendererSession.capture()
    await action(() => { assertSession(); if (!alive) throw stale() })
  } catch (cause) {
    if (!alive) return
    const code = cause instanceof AiAccessError ? cause.code : null
    const conflict = ['stale_target', 'revision_conflict', 'ai_access_conflict', 'server_instance_mismatch'].includes(code ?? '')
    error.value = conflict ? 'stale' : code === 'outcome_unknown' ? 'unknown'
      : ['offline', 'network', 'timeout', 'catalog_unavailable', 'unavailable', 'tls'].includes(code ?? '') ? 'offline'
      : code === 'not_available' ? 'notAvailable' : kind === 'refresh' ? 'loadFailed' : 'failed'
    if (kind !== 'read' || conflict) needsRefresh.value = true
    // A local change may already exist when the Remote ACK is lost. Never replay
    // a mutation or retain its consent. Explicit refresh is always read-only.
    checked.value = null; draftConsent.value = false; unregisterReview.value = null; invalidatePreview()
  } finally { if (alive) busy.value = false }
}
function refresh() {
  return perform('refresh', async current => {
    checked.value = null; checkResult.value = null; unregisterReview.value = null; draftConsent.value = false
    newDraftId.value = ''; savedId.value = ''; binding.value = null; resetEntry()
    const [regs, rows, status, available] = await Promise.all([
      aiAccessApi.registrations(), props.project ? aiAccessApi.exports() : Promise.resolve([]),
      props.project && ownScope.value ? api.projectRemote() : Promise.resolve(null),
      props.project ? Promise.resolve({ items: [] }) : profilesApi.listServers(),
    ])
    current()
    registrations.value = regs; exports.value = rows; profiles.value = available.items
    binding.value = status && selected(status) ? status : null
    executorId.value = ''; needsRefresh.value = false
  })
}
function discover() {
  const row = profile.value
  if (!row?.has_token) return
  return perform('read', async current => {
    checked.value = null; checkResult.value = null
    const result = await aiAccessApi.capabilities(row.profile_id)
    current()
    if (profile.value !== row) throw stale()
    if (result.kind === 'supported' && result.server_instance_id) checked.value = { profile: row, server: result.server_instance_id }
    else checkResult.value = result.kind === 'unsupported' ? 'unsupported'
      : result.kind === 'auth_failure' ? 'discoveryAuth' : 'discoveryUnavailable'
  })
}
function register() {
  const proof = checked.value
  const name = deviceName.value.trim().normalize('NFC')
  if (!proof || profile.value !== proof.profile || !validName(name)) return
  return perform('write', async current => {
    checked.value = null
    const { items } = await profilesApi.listServers()
    current()
    if (profile.value !== proof.profile || !items.some(row => sameProfile(proof.profile, row))) throw stale()
    const result = await aiAccessApi.register({ profile_id: proof.profile.profile_id, server_instance_id: proof.server, display_name: name })
    current()
    if (result.profile_id !== proof.profile.profile_id || result.url !== proof.profile.url
      || result.server_instance_id !== proof.server || result.publication_enabled) throw stale()
    registrations.value = [...registrations.value.filter(row => row.registration_id !== result.registration_id), result]
    deviceName.value = ''
  })
}
function profileMatches(reg: AiRegistration): boolean {
  return profiles.value.some(row => row.profile_id === reg.profile_id && row.url === reg.url && row.has_token)
}
function switchDevice(reg: AiRegistration, enable: boolean) {
  if (reg.publication_enabled === enable || !isRevision(reg.revision)
    || (enable && (!reg.has_device_token || !profileMatches(reg)))) return
  return perform('write', async current => {
    if (enable) {
      const row = profiles.value.find(item => item.profile_id === reg.profile_id)!
      const { items } = await profilesApi.listServers()
      current()
      if (!items.some(item => sameProfile(row, item))) throw stale()
    }
    const result = await aiAccessApi.publication(reg.registration_id, enable, reg.revision)
    current()
    if (result.registration_id !== reg.registration_id || result.server_instance_id !== reg.server_instance_id
      || result.url !== reg.url || result.user_id !== reg.user_id || result.client_id !== reg.client_id
      || result.profile_id !== reg.profile_id || result.publication_enabled !== enable || result.revision <= reg.revision) throw stale()
    registrations.value = registrations.value.map(row => row === reg ? result : row)
  })
}
function unregister() {
  const row = unregisterReview.value
  if (!row || !registrations.value.includes(row) || !isRevision(row.revision)) return
  return perform('write', async current => {
    unregisterReview.value = null
    await aiAccessApi.unregister(row.registration_id, row.revision)
    current()
    registrations.value = registrations.value.filter(reg => reg !== row)
  })
}
function createDraft() {
  const status = binding.value
  const reg = executor.value
  if (!status || !reg || !draftConsent.value || !validName(draftName.value)) return
  const input: AiExportInput = {
    registration_id: reg.registration_id, profile_id: reg.profile_id, server_instance_id: reg.server_instance_id,
    project_id: status.project_id, project_epoch: status.epoch, persistent_id: status.persistent_id!,
    storage_epoch: status.space_instance_id!, identity_revision: status.identity_revision!,
    display_name: draftName.value.trim().normalize('NFC'),
  }
  return perform('write', async current => {
    draftConsent.value = false
    const fresh = await api.projectRemote()
    current()
    if (!sameBinding(status, fresh)) throw stale()
    const result = await aiAccessApi.createExport(input)
    current()
    const target = result.target
    if (result.enabled || result.mirror_state !== 'unpublished' || target.persistent_id !== input.persistent_id
      || target.storage_epoch !== input.storage_epoch || target.executor_registration_id !== input.registration_id
      || target.server_instance_id !== input.server_instance_id || !isRevision(target.export_revision)) throw stale()
    exports.value = [...exports.value, result]
    savedId.value = ''; newDraftId.value = target.export_id; resetEntry(); draftName.value = ''
  })
}
function switchExport(enable: boolean) {
  const row = entry.value
  const reg = entryRegistration.value
  if (!row || !isRevision(row.target.export_revision) || row.enabled === enable
    || (enable && (!reg?.publication_enabled || !reg.has_device_token))) return
  return perform('write', async current => {
    const result = await aiAccessApi.publish(row.target.export_id, enable, row.target.export_revision)
    current()
    if (!sameTarget(row.target, result.target, false) || result.enabled !== enable
      || !isRevision(result.target.export_revision) || result.target.export_revision < row.target.export_revision) throw stale()
    resetEntry()
    exports.value = exports.value.map(item => item === row ? result : item)
  })
}
function loadGrants() {
  const row = entry.value
  if (!row) return
  return perform('read', async current => {
    grants.value = null; resetGrant()
    const result = await aiAccessApi.grants(row.target.export_id)
    current()
    if (entry.value !== row || result.some(item => !sameTarget(item.grant.target, row.target, false))) throw stale()
    grants.value = result
  })
}
function loadOptions() {
  const row = entry.value
  const reg = entryRegistration.value
  if (!row || !reg || !canAdministerGrant.value) return
  return perform('read', async current => {
    directory.value = null; resetGrant()
    const [devices, projectTeams] = await Promise.all([
      aiAccessApi.directory(reg.registration_id), !savedId.value && newDraftId.value === row.target.export_id ? api.teams() : Promise.resolve([]),
    ])
    current()
    if (entry.value !== row) throw stale()
    directory.value = devices.filter(device => device.registration_id !== reg.registration_id)
    teams.value = projectTeams
  })
}
function editGrant(grant: AiGrant) {
  if (!canAdministerGrant.value || !entry.value || !sameTarget(grant.target, entry.value.target)
    || grant.revoked_at !== null || !isRevision(grant.revision)) return
  resetGrant()
  edit.value = grant
}
const localId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const plan = computed<Plan | null>(() => {
  const row = entry.value
  const device = directory.value?.find(item => item.registration_id === deviceId.value)
  const existing = edit.value
  const actor = existing ? { registration_id: existing.grantee_registration_id, user_id: existing.grantee_user_id } : device
  const selectedOps = operations.value
  const team = teamId.value.trim()
  const member = memberId.value.trim()
  const ids = scope.value === 'selected_resources' ? [...new Set(resourceText.value.split(/[\s,]+/).filter(Boolean))] : []
  if (!row || !canAdministerGrant.value || !actor || !localId.test(team) || !localId.test(member)
    || (!savedId.value && !members.value.some(item => item.id === member))
    || !selectedOps.includes('view_status') || (selectedOps.includes('operate_existing')
      && (scope.value === 'created_by_this_grantee' || !selectedOps.some(op => !['view_status', 'view_transcript', 'operate_existing'].includes(op))))
    || (scope.value === 'selected_resources' && (!ids.length || ids.length > 1024 || ids.some(id => !localId.test(id))))
    || !Object.hasOwn(lifetimeLabels, hours.value) || !isRevision(row.target.export_revision)) return null
  return { target: { ...row.target }, actor: { registration_id: actor.registration_id, user_id: actor.user_id }, hours: hours.value,
    edit: existing ? { id: existing.grant_id, revision: existing.revision } : null,
    options: { expected_export_revision: row.target.export_revision, team_ids: [team], member_ids: [member],
      operations: [...selectedOps], resource_scope: scope.value, resource_ids: ids } }
})
function previewPolicy() {
  const intent = plan.value
  if (!intent) return
  return perform('read', async current => {
    invalidatePreview()
    const team = intent.options.team_ids[0]!
    const member = intent.options.member_ids[0]!
    const policy = await aiAccessApi.policy(intent.target.export_id, team, member)
    current()
    if (plan.value !== intent || !sameTarget(intent.target, policy.target) || policy.team_id !== team || policy.member_id !== member
      || !/^[0-9a-f]{64}$/.test(policy.execution_policy_digest)) throw stale()
    preview.value = { plan: intent, policy }
  })
}
function saveGrant() {
  const review = preview.value
  if (!review || !grantConsent.value || review.plan !== plan.value) return
  const intent = review.plan
  return perform('write', async current => {
    invalidatePreview()
    const body = { ...intent.options, execution_policy_digest: review.policy.execution_policy_digest,
      expires_at: Date.now() + intent.hours * 3_600_000 }
    const result = intent.edit
      ? await aiAccessApi.updateGrant(intent.target.export_id, intent.edit.id, { ...body, expected_revision: intent.edit.revision })
      : await aiAccessApi.createGrant(intent.target.export_id, { ...body,
          grantee_user_id: intent.actor.user_id, grantee_registration_id: intent.actor.registration_id })
    current()
    if (!sameTarget(result.grant.target, intent.target) || result.grant.grantee_user_id !== intent.actor.user_id
      || result.grant.grantee_registration_id !== intent.actor.registration_id || (intent.edit
        && (result.grant.grant_id !== intent.edit.id || result.grant.revision <= intent.edit.revision))) throw stale()
    retainGrant(result); resetGrant()
  })
}
function retainGrant(receipt: AiGrantReceipt) {
  grants.value = [...(grants.value ?? []).filter(row => row.grant.grant_id !== receipt.grant.grant_id), receipt]
}
function revoke(grant: AiGrant) {
  if (grant.revoked_at !== null || !isRevision(grant.revision)) return
  return perform('write', async current => {
    resetGrant()
    const result = await aiAccessApi.revoke(grant.grant_id, grant.revision)
    current()
    if (!sameTarget(result.grant.target, grant.target) || result.grant.grant_id !== grant.grant_id
      || result.grant.revision <= grant.revision || result.grant.revoked_at === null) throw stale()
    retainGrant(result)
  })
}
function expiry(expires: number) {
  return Number.isFinite(new Date(expires).valueOf())
    ? formatDate(new Date(expires), { dateStyle: 'medium', timeStyle: 'short' }) : t('aiAccess.owner.unknown')
}
function targetText(target: AiTarget) {
  return t('aiAccess.owner.targetIdentity', { server: target.server_instance_id, registration: target.executor_registration_id,
    export: target.export_id, project: target.persistent_id, epoch: target.storage_epoch, revision: target.export_revision })
}
function registrationText(reg: AiRegistration) {
  return t('aiAccess.owner.registrationIdentity', { url: reg.url, server: reg.server_instance_id, user: reg.user_id,
    registration: reg.registration_id, client: reg.client_id })
}
function deviceText(device: AiDevice) {
  return t('aiAccess.owner.deviceIdentity', { name: device.display_name, user: device.user_id, registration: device.registration_id, client: device.client_id })
}
onMounted(refresh)
onBeforeUnmount(() => { alive = false })
</script>

<template>
  <section class="space-y-3" :aria-busy="busy">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="font-medium">{{ t('aiAccess.owner.title') }}</h2>
      <button type="button" data-testid="ai-owner-refresh" class="vxs-btn" :disabled="busy" @click="refresh">{{ t('aiAccess.owner.refresh') }}</button>
    </div>
    <p class="text-small text-muted">{{ t(project ? 'aiAccess.owner.projectHint' : 'aiAccess.owner.globalHint') }}</p>
    <p v-if="error" role="alert" class="text-small text-danger">{{ t(`aiAccess.owner.${error}`) }}</p>
    <p v-if="busy" role="status" class="text-small text-muted">{{ t('aiAccess.owner.busy') }}</p>
    <fieldset v-if="!project" :disabled="!ready" class="space-y-3 disabled:opacity-60">
      <div class="flex flex-wrap items-end gap-2">
        <label class="grid min-w-0 gap-1">{{ t('aiAccess.owner.profile') }}
          <select v-model="profileId" data-testid="ai-owner-profile" class="vxs-input max-w-full">
            <option value="">{{ t('aiAccess.owner.chooseProfile') }}</option>
            <option v-for="row in profiles" :key="row.profile_id" :value="row.profile_id">{{ row.name }} — {{ row.url }}</option>
          </select>
        </label>
        <button type="button" data-testid="ai-owner-discover" class="vxs-btn" :disabled="!profile?.has_token" @click="discover">{{ t('aiAccess.owner.discover') }}</button>
      </div>
      <p v-if="checkResult" role="status" class="text-small text-muted">{{ t(`aiAccess.owner.${checkResult}`) }}</p>
      <div v-if="checked" class="space-y-2 rounded-md border border-line p-3">
        <p class="break-all text-small">{{ t('aiAccess.owner.supported', { id: checked.server }) }} <bdi>{{ checked.profile.url }}</bdi></p>
        <p class="text-small text-muted">{{ t('aiAccess.owner.accountAfterRegistration') }}</p>
        <label class="grid gap-1">{{ t('aiAccess.owner.deviceName') }}
          <input v-model="deviceName" data-testid="ai-owner-device-name" class="vxs-input" maxlength="160" />
        </label>
        <button type="button" data-testid="ai-owner-register" class="vxs-btn-primary" :disabled="!validName(deviceName)" @click="register">{{ t('aiAccess.owner.register') }}</button>
      </div>
      <h3 class="font-medium">{{ t('aiAccess.owner.registrations') }}</h3>
      <p v-if="!registrations.length && !needsRefresh" class="text-small text-muted">{{ t('aiAccess.owner.noRegistrations') }}</p>
      <ul class="space-y-2">
        <li v-for="reg in registrations" :key="reg.registration_id" class="space-y-1 rounded-md border border-line p-3">
          <h4><bdi>{{ reg.display_name }}</bdi></h4>
          <p class="break-all text-small"><bdi>{{ registrationText(reg) }}</bdi></p>
          <p class="text-small">{{ t('aiAccess.owner.registrationState', { state: t(reg.publication_enabled ? 'aiAccess.owner.enabled' : 'aiAccess.owner.disabled'), credential: t(reg.has_device_token ? 'aiAccess.owner.credentialPresent' : 'aiAccess.owner.credentialMissing') }) }}</p>
          <p v-if="!profileMatches(reg)" class="text-small text-muted">{{ t('aiAccess.owner.profileChanged') }}</p>
          <p v-else-if="!reg.publication_enabled" class="text-small text-muted">{{ t('aiAccess.owner.reenableNote') }}</p>
          <div class="flex flex-wrap gap-2">
            <button type="button" data-testid="ai-owner-device-switch" class="vxs-btn" :disabled="!reg.publication_enabled && (!reg.has_device_token || !profileMatches(reg)) || !!unregisterReview" @click="switchDevice(reg, !reg.publication_enabled)">{{ t(reg.publication_enabled ? 'aiAccess.owner.disablePublication' : 'aiAccess.owner.enablePublication') }}</button>
            <button v-if="unregisterReview !== reg" type="button" data-testid="ai-owner-unregister" class="vxs-btn text-danger" :disabled="!!unregisterReview" @click="unregisterReview = reg">{{ t('aiAccess.owner.unregister') }}</button>
          </div>
          <div v-if="unregisterReview === reg" class="space-y-1">
            <p class="text-small">{{ t('aiAccess.owner.unregisterNote') }}</p>
            <div class="flex gap-2">
              <button type="button" data-testid="ai-owner-confirm-unregister" class="vxs-btn text-danger" @click="unregister">{{ t('aiAccess.owner.confirmUnregister') }}</button>
              <button type="button" class="vxs-btn" @click="unregisterReview = null">{{ t('aiAccess.owner.cancel') }}</button>
            </div>
          </div>
        </li>
      </ul>
    </fieldset>

    <fieldset v-else :disabled="!ready" class="space-y-3 disabled:opacity-60">
      <p v-if="!ownScope" class="text-small text-muted">{{ t('aiAccess.owner.noScope') }}</p>
      <p v-else-if="!binding && !needsRefresh" class="text-small text-muted">{{ t('aiAccess.owner.noBinding') }}</p>
      <div v-else-if="binding && !newDraftId" class="space-y-2 rounded-md border border-line p-3">
        <p class="text-small text-muted">{{ t('aiAccess.owner.bindingNote') }}</p>
        <p class="break-all text-small">{{ t('aiAccess.owner.projectBinding', { project: binding.project_id, url: binding.url, profile: binding.profile_id ?? '', id: binding.persistent_id ?? '', epoch: binding.space_instance_id ?? '', revision: binding.identity_revision ?? '' }) }}</p>
        <p v-if="!matching.length" class="text-small text-muted">{{ t('aiAccess.owner.noExecutor') }}</p>
        <template v-else>
          <label class="grid gap-1">{{ t('aiAccess.owner.executor') }}
            <select v-model="executorId" data-testid="ai-owner-executor" class="vxs-input max-w-full">
              <option value="">{{ t('aiAccess.owner.chooseExecutor') }}</option>
              <option v-for="reg in matching" :key="reg.registration_id" :value="reg.registration_id">{{ deviceText(reg) }}</option>
            </select>
          </label>
          <template v-if="executor">
            <p class="break-all text-small" data-testid="ai-owner-consent-target"><bdi>{{ registrationText(executor) }}</bdi></p>
            <label class="grid gap-1">{{ t('aiAccess.owner.exportName') }}
              <input v-model="draftName" data-testid="ai-owner-draft-name" class="vxs-input" maxlength="160" />
            </label>
            <label class="flex items-start gap-2 text-small">
              <input v-model="draftConsent" data-testid="ai-owner-draft-consent" type="checkbox" class="mt-0.5" />
              {{ t('aiAccess.owner.draftConsent') }}
            </label>
            <button type="button" data-testid="ai-owner-create-draft" class="vxs-btn-primary" :disabled="!draftConsent || !validName(draftName)" @click="createDraft">{{ t('aiAccess.owner.createDraft') }}</button>
          </template>
        </template>
      </div>
      <div v-if="exports.some(row => row.target.export_id !== newDraftId)" class="space-y-2 rounded-md border border-line p-3">
        <p class="text-small text-muted">{{ t('aiAccess.owner.savedExportsNote') }}</p>
        <label class="grid gap-1">{{ t('aiAccess.owner.savedExports') }}
          <select v-model="savedId" data-testid="ai-owner-saved" class="vxs-input max-w-full">
            <option value="">{{ t(newDraftId ? 'aiAccess.owner.returnToNew' : 'aiAccess.owner.chooseExport') }}</option>
            <option v-for="row in exports.filter(item => item.target.export_id !== newDraftId)" :key="row.target.export_id" :value="row.target.export_id">{{ row.display_name }} — {{ row.target.persistent_id }} — {{ row.target.export_id }}</option>
          </select>
        </label>
      </div>
      <div v-if="entry" class="space-y-3 rounded-md border border-line p-3" data-testid="ai-owner-export">
        <h3 class="font-medium">{{ t(savedId ? 'aiAccess.owner.savedExport' : 'aiAccess.owner.newExport') }}: <bdi>{{ entry.display_name }}</bdi></h3>
        <p class="break-all text-small"><bdi>{{ targetText(entry.target) }}</bdi></p>
        <p v-if="entryRegistration" class="break-all text-small"><bdi>{{ registrationText(entryRegistration) }}</bdi></p>
        <p class="text-small">{{ t(entry.mirror_state === 'conflict' ? 'aiAccess.owner.mirrorConflict' : entry.mirror_state === 'pending' ? 'aiAccess.owner.mirrorPending' : entry.enabled && entry.mirror_state === 'effective' ? 'aiAccess.owner.published' : !entry.enabled && entry.mirror_state === 'unpublished' ? 'aiAccess.owner.draft' : 'aiAccess.owner.publicationUnknown') }}</p>
        <p v-if="!entryRegistration?.publication_enabled" class="text-small text-muted">{{ t('aiAccess.owner.enableDeviceFirst') }}</p>
        <div class="flex flex-wrap gap-2">
          <button type="button" data-testid="ai-owner-export-switch" class="vxs-btn" :disabled="!entry.enabled && (!entryRegistration?.publication_enabled || !entryRegistration.has_device_token)" @click="switchExport(!entry.enabled)">{{ t(entry.enabled ? 'aiAccess.owner.unpublish' : 'aiAccess.owner.publish') }}</button>
          <button type="button" data-testid="ai-owner-read-grants" class="vxs-btn" @click="loadGrants">{{ t('aiAccess.owner.readGrants') }}</button>
          <button type="button" data-testid="ai-owner-read-devices" class="vxs-btn" :disabled="!canAdministerGrant" @click="loadOptions">{{ t('aiAccess.owner.readDevices') }}</button>
        </div>

        <div v-if="directory !== null || edit" class="space-y-2 rounded-md border border-line p-3">
          <template v-if="edit">
            <p class="break-all text-small">{{ t('aiAccess.owner.editingGrant', { id: edit.grant_id, revision: edit.revision, user: edit.grantee_user_id, registration: edit.grantee_registration_id }) }}</p>
            <button type="button" class="vxs-btn" @click="resetGrant">{{ t('aiAccess.owner.cancel') }}</button>
          </template>
          <label v-else class="grid gap-1">{{ t('aiAccess.owner.grantDevice') }}
            <select v-model="deviceId" data-testid="ai-owner-grantee" class="vxs-input max-w-full">
              <option value="">{{ t('aiAccess.owner.chooseDevice') }}</option>
              <option v-for="device in directory" :key="device.registration_id" :value="device.registration_id">{{ deviceText(device) }}</option>
            </select>
          </label>
          <p v-if="!edit && !directory?.length" class="text-small text-muted">{{ t('aiAccess.owner.noDevices') }}</p>
          <template v-if="savedId">
            <p class="text-small text-muted">{{ t('aiAccess.owner.savedTeamNote') }}</p>
            <label class="grid gap-1">{{ t('aiAccess.owner.teamId') }}<input v-model="teamId" class="vxs-input" data-testid="ai-owner-team" maxlength="128" /></label>
            <label class="grid gap-1">{{ t('aiAccess.owner.memberId') }}<input v-model="memberId" class="vxs-input" data-testid="ai-owner-member" maxlength="128" /></label>
          </template>
          <template v-else-if="directory !== null">
            <label class="grid gap-1">{{ t('aiAccess.owner.team') }}
              <select v-model="teamId" class="vxs-input" data-testid="ai-owner-team">
                <option value="">{{ t('aiAccess.owner.chooseTeam') }}</option>
                <option v-for="team in teams" :key="team.id" :value="team.id">{{ t('aiAccess.owner.personIdentity', { name: team.name, id: team.id }) }}</option>
              </select>
            </label>
            <label class="grid gap-1">{{ t('aiAccess.owner.member') }}
              <select v-model="memberId" class="vxs-input" data-testid="ai-owner-member">
                <option value="">{{ t('aiAccess.owner.chooseMember') }}</option>
                <option v-for="member in members" :key="member.id" :value="member.id">{{ t('aiAccess.owner.personIdentity', { name: member.name, id: member.id }) }}</option>
              </select>
            </label>
          </template>
          <fieldset class="space-y-1">
            <legend>{{ t('aiAccess.owner.permissions') }}</legend>
            <p class="text-small text-muted">{{ t('aiAccess.owner.permissionHint') }}</p>
            <label v-for="op in permissions" :key="op" class="flex items-center gap-2 text-small">
              <input v-model="operations" :value="op" :data-testid="`ai-owner-op-${op}`" type="checkbox" />{{ t(`aiAccess.owner.${op}`) }}
            </label>
          </fieldset>
          <p v-if="operations.includes('view_transcript')" class="text-small text-muted">{{ t('aiAccess.owner.transcriptRisk') }}</p>
          <label class="grid gap-1">{{ t('aiAccess.owner.scope') }}
            <select v-model="scope" class="vxs-input" data-testid="ai-owner-scope">
              <option v-for="choice in scopes" :key="choice" :value="choice">{{ t(`aiAccess.owner.${choice}`) }}</option>
            </select>
          </label>
          <p v-if="scope !== 'created_by_this_grantee'" class="text-small text-muted">{{ t('aiAccess.owner.existingRisk') }}</p>
          <label v-if="scope === 'selected_resources'" class="grid gap-1">{{ t('aiAccess.owner.resources') }}
            <textarea v-model="resourceText" class="vxs-input" rows="2" />
          </label>
          <label class="grid gap-1">{{ t('aiAccess.owner.lifetime') }}
            <select v-model="hours" class="vxs-input">
              <option :value="1">{{ t('aiAccess.owner.oneHour') }}</option>
              <option :value="24">{{ t('aiAccess.owner.oneDay') }}</option>
              <option :value="168">{{ t('aiAccess.owner.sevenDays') }}</option>
            </select>
          </label>
          <button type="button" class="vxs-btn" data-testid="ai-owner-preview" :disabled="!plan" @click="previewPolicy">{{ t('aiAccess.owner.preview') }}</button>
          <div v-if="preview" class="space-y-1 break-words rounded-md border border-line p-3" data-testid="ai-owner-policy">
            <h4>{{ t('aiAccess.owner.policy') }}</h4>
            <p class="break-all text-small">{{ t('aiAccess.owner.grantIdentity', { user: preview.plan.actor.user_id, registration: preview.plan.actor.registration_id, team: preview.policy.team_id, member: preview.policy.member_id }) }}</p>
            <p class="text-small">{{ t('aiAccess.owner.policyModes', { backend: preview.policy.backend, mode: preview.policy.permission_mode }) }}</p>
            <p class="text-small">{{ t('aiAccess.owner.mcpPolicy', { strict: t(preview.policy.mcp.strict ? 'aiAccess.owner.enabled' : 'aiAccess.owner.disabled'), count: preview.policy.mcp.project_server_count }) }}</p>
            <p class="text-small">{{ t('aiAccess.owner.runLimits', { turns: preview.policy.limits.max_turns ?? t('aiAccess.owner.noExplicitLimit'), continuations: preview.policy.limits.max_run_continuations, slots: preview.policy.limits.project_agent_slots, export: preview.policy.limits.remote_runs_per_export, grant: preview.policy.limits.remote_runs_per_grant }) }}</p>
            <p class="break-all text-small">{{ t('aiAccess.owner.policyDigest', { digest: preview.policy.execution_policy_digest }) }}</p>
            <p class="text-small text-muted">{{ t('aiAccess.owner.runRisk') }}</p>
            <p v-if="preview.policy.tools.backend_native_tools_may_include_shell" class="text-small text-muted">{{ t('aiAccess.owner.shellRisk') }}</p>
            <label class="flex items-center gap-2 text-small"><input v-model="grantConsent" data-testid="ai-owner-grant-consent" type="checkbox" />{{ t('aiAccess.owner.consent') }}</label>
            <button type="button" class="vxs-btn-primary" data-testid="ai-owner-save-grant" :disabled="!grantConsent" @click="saveGrant">{{ t(edit ? 'aiAccess.owner.updateGrant' : 'aiAccess.owner.grant') }}</button>
          </div>
        </div>

        <div v-if="grants" class="space-y-2">
          <h4>{{ t('aiAccess.owner.grants') }}</h4>
          <p v-if="!grants.length" class="text-small text-muted">{{ t('aiAccess.owner.noGrants') }}</p>
          <ul class="space-y-2">
            <li v-for="{ grant, acked_revision } in grants" :key="grant.grant_id" class="space-y-1 rounded-md border border-line p-3">
              <p class="break-all text-small">{{ t('aiAccess.owner.grantIdentity', { user: grant.grantee_user_id, registration: grant.grantee_registration_id, team: grant.team_ids.join(', '), member: grant.member_ids.join(', ') }) }}</p>
              <p class="break-all text-small">{{ t('aiAccess.owner.grantRecord', { id: grant.grant_id, revision: grant.revision, expires: expiry(grant.expires_at) }) }}</p>
              <p class="text-small">{{ grant.operations.map(op => t(`aiAccess.owner.${op}`)).join(' · ') }}</p>
              <p class="break-words text-small">{{ t(`aiAccess.owner.${grant.resource_scope}`) }}<template v-if="grant.resource_ids.length">: {{ grant.resource_ids.join(', ') }}</template></p>
              <p class="text-small" role="status">{{ t(grant.revoked_at !== null ? (acked_revision === grant.revision ? 'aiAccess.owner.grantRevoked' : 'aiAccess.owner.grantRevokedLocal') : !sameTarget(entry.target, grant.target) ? 'aiAccess.owner.grantFenced' : grant.expires_at <= Date.now() ? 'aiAccess.owner.grantExpired' : acked_revision === grant.revision ? 'aiAccess.owner.grantAcknowledged' : 'aiAccess.owner.grantPending') }}</p>
              <div v-if="grant.revoked_at === null" class="flex flex-wrap gap-2">
                <button type="button" class="vxs-btn" data-testid="ai-owner-edit-grant" :disabled="!canAdministerGrant || !sameTarget(entry.target, grant.target) || (!savedId && directory === null)" @click="editGrant(grant)">{{ t('aiAccess.owner.editGrant') }}</button>
                <button type="button" class="vxs-btn text-danger" data-testid="ai-owner-revoke" @click="revoke(grant)">{{ t('aiAccess.owner.revoke') }}</button>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </fieldset>
    <p class="text-small text-muted">{{ t('aiAccess.owner.stopNote') }}</p>
  </section>
</template>
