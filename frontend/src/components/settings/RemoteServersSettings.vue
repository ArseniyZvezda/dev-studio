<script setup lang="ts">
/** Profile metadata and credential intents (remote-identity/studio-design §5.1/§10.1).
 * The renderer owns the store, not this view: navigation must not dispose somebody
 * else's pending check or mutation. No local async continuations survive unmount.
 */
import { computed, onBeforeUnmount, onMounted, ref, watchEffect } from 'vue'
import { Loader2, RefreshCw } from 'lucide-vue-next'

import type { RemoteServerProfile, RemoteVerification } from '@/api/remote'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import RemoteProfileMetadataForm, { remoteProfileMetadataState } from './RemoteProfileMetadataForm.vue'
import RemoteProfileCredentialForm from './RemoteProfileCredentialForm.vue'
import RemoteProfileDeleteDialog from './RemoteProfileDeleteDialog.vue'
import { formatDate, t, type MessageKey } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'

const servers = useRemoteServersStore()
const metadata = remoteProfileMetadataState(servers)
type Binding = Pick<RemoteServerProfile, 'profile_id' | 'revision' | 'url'>
const confirmation = ref<Binding | null>(null)
// Separate metadata/key intents capture their own CAS; no implicit Verify or URL edit.
const editor = ref<{ profile?: RemoteServerProfile; mode?: 'rename' | 'url' } | null>(null)
const credentialEditor = ref<RemoteServerProfile | null>(null)
const deleteEditor = ref<RemoteServerProfile | null>(null)
const metadataNeedsRefresh = computed(() => metadata.refreshRequired)
let mounted = false

const statusKeys: Record<RemoteVerification['state'], MessageKey> = {
  unavailable: 'settings.remoteServers.status.unavailable',
  unchecked: 'settings.remoteServers.status.unchecked',
  checking: 'settings.remoteServers.status.checking',
  valid: 'settings.remoteServers.status.valid',
  unauthorised: 'settings.remoteServers.status.unauthorised',
  offline: 'settings.remoteServers.status.offline',
  incompatible: 'settings.remoteServers.status.incompatible',
  insecure_blocked: 'settings.remoteServers.status.insecure_blocked',
  error: 'settings.remoteServers.status.error',
}
const credentialKeys: Record<RemoteServerProfile['credential_source'], MessageKey> = {
  stored: 'settings.remoteServers.credential.stored',
  none: 'settings.remoteServers.credential.missing',
  environment: 'settings.remoteServers.credential.environment',
}
const anyBusy = computed(() => metadata.pending > 0 || Object.keys(servers.busy).length > 0)
const listUnavailable = computed(() => !!servers.error || !servers.loaded)
const refreshDisabled = computed(() => servers.loading || anyBusy.value || !!confirmation.value || !!editor.value || !!credentialEditor.value || !!deleteEditor.value)
const addDisabled = computed(() => refreshDisabled.value || listUnavailable.value || metadataNeedsRefresh.value)

function same(a: Binding, b: Binding): boolean {
  return a.profile_id === b.profile_id && a.revision === b.revision && a.url === b.url
}
function applicable(binding: Binding): boolean {
  const current = servers.items.find(item => item.profile_id === binding.profile_id)
  return !!current && same(current, binding) && !servers.loading && !listUnavailable.value
    && !servers.busy[binding.profile_id]
}
function verifyDisabled(profile: RemoteServerProfile): boolean {
  return !!confirmation.value || !!editor.value || !!credentialEditor.value || !!deleteEditor.value || metadata.pending > 0 || metadataNeedsRefresh.value || !applicable(profile)
}
function openCreate(): void {
  if (mounted && !addDisabled.value) editor.value = {}
}
function openRename(profile: RemoteServerProfile): void {
  if (mounted && !verifyDisabled(profile)) editor.value = { profile: { ...profile }, mode: 'rename' }
}
function openChangeUrl(profile: RemoteServerProfile): void {
  if (mounted && !verifyDisabled(profile)) editor.value = { profile: { ...profile }, mode: 'url' }
}
function openDelete(profile: RemoteServerProfile): void {
  if (mounted && !verifyDisabled(profile)) deleteEditor.value = { ...profile }
}
function closeEditor(): void { editor.value = null }
function openCredential(profile: RemoteServerProfile): void {
  if (mounted && !verifyDisabled(profile)) credentialEditor.value = { ...profile }
}
function closeCredential(): void { credentialEditor.value = null }
function closeDelete(): void { deleteEditor.value = null }
function checked(profile: RemoteServerProfile) {
  const value = servers.lastChecked[profile.profile_id]
  return value && same(value, profile) ? value.result : null
}
function checkedTime(value: string): string {
  if (!Number.isFinite(Date.parse(value))) return t('settings.remoteServers.unknownTime')
  return formatDate(new Date(value), {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    second: '2-digit', timeZoneName: 'short',
  })
}

/** Match the transport's literal loopback allowlist, never suffixes or DNS guesses.
 * Invalid/unknown schemes fail closed here too; the metadata boundary validates URLs.
 */
function needsConfirmation(value: string): boolean {
  try {
    const url = new URL(value)
    if (url.protocol === 'https:') return false
    // WHATWG canonicalizes e.g. 127.1/decimal IPv4 to 127.0.0.1. Do not
    // silently widen the backend's literal host exception through that repair.
    return url.protocol !== 'http:'
      || !/^http:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/.test(value)
  } catch { return true }
}
function refresh(): void {
  if (mounted && !refreshDisabled.value) {
    void servers.load().then(applied => {
      if (mounted && applied) metadata.refreshRequired = false
    })
  }
}
function verify(profile: RemoteServerProfile): void {
  if (!mounted || verifyDisabled(profile)) return
  if (needsConfirmation(profile.url)) {
    // Consent is bound to the exact saved URL and revision, never just a row id.
    confirmation.value = { profile_id: profile.profile_id, revision: profile.revision, url: profile.url }
  } else void servers.verify(profile.profile_id)
}
function confirmHttp(): void {
  const binding = confirmation.value
  confirmation.value = null
  if (mounted && binding && applicable(binding)) void servers.verify(binding.profile_id)
}
watchEffect(() => {
  if (confirmation.value && !applicable(confirmation.value)) confirmation.value = null
})
onMounted(() => {
  mounted = true
  // Reuse work already owned by this renderer. In particular, remount must not
  // race a shared pending Verify with a new metadata snapshot.
  if (!servers.loading && !anyBusy.value && !metadataNeedsRefresh.value) void servers.load()
})
onBeforeUnmount(() => {
  mounted = false
  confirmation.value = null
  editor.value = null
  credentialEditor.value = null
  deleteEditor.value = null
})
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center justify-between gap-3">
      <h2 class="font-medium">{{ t('settings.remoteServers.title') }}</h2>
      <div class="flex flex-wrap justify-end gap-2">
        <button
          type="button"
          data-testid="remote-add"
          class="vxs-btn-primary px-3 py-1.5 disabled:opacity-50"
          :disabled="addDisabled"
          @click="openCreate"
        >{{ t('settings.remoteServers.add') }}</button>
        <button
          type="button"
          data-testid="remote-refresh"
          class="flex items-center gap-2 rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
          :disabled="refreshDisabled"
          @click="refresh"
        >
          <RefreshCw class="size-3.5" aria-hidden="true" />
          {{ t('settings.remoteServers.refresh') }}
        </button>
      </div>
    </div>
    <p class="text-small text-faint">{{ t('settings.remoteServers.hint') }}</p>
    <p data-testid="remote-mutation-gate" class="text-small text-muted">{{ t('settings.remoteServers.mutationGate') }}</p>
    <p v-if="metadataNeedsRefresh" role="alert" class="text-small text-danger">
      {{ t('settings.remoteServers.metadata.stale') }}
    </p>
    <p v-if="servers.loading" role="status" class="flex items-center gap-2 text-muted">
      <Loader2 class="size-4 animate-spin" aria-hidden="true" />
      {{ t('settings.remoteServers.loading') }}
    </p>
    <p v-else-if="listUnavailable" role="alert" class="text-small text-danger">
      {{ t('settings.remoteServers.loadError') }}
    </p>
    <p v-else-if="!servers.items.length" role="status" class="text-small text-muted">
      {{ t('settings.remoteServers.empty') }}
    </p>

    <ul v-if="servers.items.length" class="space-y-3">
      <li
        v-for="profile in servers.items"
        :key="profile.profile_id"
        :data-profile-id="profile.profile_id"
        :aria-busy="!!servers.busy[profile.profile_id]"
        class="vxs-card space-y-2 rounded-lg border border-card bg-surface p-4 shadow-card"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0 space-y-1">
            <h3 class="font-medium break-words"><bdi>{{ profile.name }}</bdi></h3>
            <p class="font-mono text-small break-all"><bdi dir="ltr">{{ profile.url }}</bdi></p>
          </div>
          <div class="flex shrink-0 flex-wrap justify-end gap-2">
            <button
              type="button"
              data-testid="remote-credential"
              class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
              :disabled="verifyDisabled(profile)"
              @click="openCredential(profile)"
            >{{ t('settings.remoteServers.credentials.manage') }}</button>
            <button
              type="button"
              data-testid="remote-rename"
              class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
              :disabled="verifyDisabled(profile)"
              @click="openRename(profile)"
            >{{ t('settings.remoteServers.rename') }}</button>
            <button
              type="button"
              data-testid="remote-change-url"
              class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
              :disabled="verifyDisabled(profile)"
              @click="openChangeUrl(profile)"
            >{{ t('settings.remoteServers.changeUrl.open') }}</button>
            <button
              type="button"
              data-testid="remote-delete"
              class="rounded-md border border-line px-3 py-1.5 text-danger hover:bg-hover disabled:opacity-50"
              :disabled="verifyDisabled(profile)"
              @click="openDelete(profile)"
            >{{ t('settings.remoteServers.delete.open') }}</button>
            <button
              type="button"
              data-testid="remote-verify"
              class="shrink-0 rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
              :disabled="verifyDisabled(profile)"
              @click="verify(profile)"
            >
              {{ t(servers.busy[profile.profile_id] === 'verify' ? 'settings.remoteServers.verifying' : 'settings.remoteServers.verify') }}
            </button>
          </div>
        </div>
        <p class="text-small text-muted">{{ t(credentialKeys[profile.credential_source]) }}</p>
        <p class="text-small">{{ t(statusKeys[profile.verification.state]) }}</p>
        <div v-if="checked(profile)" class="space-y-1 text-small text-muted">
          <p>{{ t('settings.remoteServers.lastResult') }}: {{ t(statusKeys[checked(profile)!.state]) }}</p>
          <p>
            <time :datetime="Number.isFinite(Date.parse(checked(profile)!.checked_at)) ? checked(profile)!.checked_at : undefined">
              {{ t('settings.remoteServers.checkedAt', { time: checkedTime(checked(profile)!.checked_at) }) }}
            </time>
          </p>
          <p>
            {{ t('settings.remoteServers.checkedUser', { user: checked(profile)!.user?.display_name || checked(profile)!.user?.id || t('settings.remoteServers.unknownUser') }) }}
          </p>
        </div>
        <p v-else class="text-small text-faint">{{ t('settings.remoteServers.noChecked') }}</p>
        <p v-if="servers.busy[profile.profile_id]" role="status" class="flex items-center gap-2 text-small text-muted">
          <Loader2 class="size-3.5 animate-spin" aria-hidden="true" />
          {{ t(servers.busy[profile.profile_id] === 'verify' ? 'settings.remoteServers.verifying' : 'settings.remoteServers.busy') }}
        </p>
        <p v-if="servers.errors[profile.profile_id]" role="alert" class="text-small text-danger">
          {{ t('settings.remoteServers.verifyError') }}
        </p>
      </li>
    </ul>

    <RemoteProfileCredentialForm
      v-if="credentialEditor"
      :profile="credentialEditor"
      @saved="closeCredential"
      @close="closeCredential"
      @refresh-required="metadata.refreshRequired = true"
    />

    <RemoteProfileDeleteDialog
      v-if="deleteEditor"
      :profile="deleteEditor"
      @deleted="closeDelete"
      @close="closeDelete"
      @refresh-required="metadata.refreshRequired = true"
    />

    <RemoteProfileMetadataForm
      v-if="editor"
      :profile="editor.profile"
      :mode="editor.mode"
      @saved="closeEditor"
      @close="closeEditor"
      @refresh-required="metadata.refreshRequired = true"
    />

    <ModalDialog
      v-if="confirmation"
      :label="t('settings.remoteServers.insecureTitle')"
      panel-class="w-lg p-5 space-y-4"
      instant
      @close="confirmation = null"
    >
      <h3 class="font-medium">{{ t('settings.remoteServers.insecureTitle') }}</h3>
      <p class="text-small text-warning">{{ t('settings.remoteServers.insecureWarning') }}</p>
      <p class="font-mono text-small break-all"><bdi dir="ltr">{{ confirmation.url }}</bdi></p>
      <div class="flex flex-wrap justify-end gap-2">
        <button
          type="button"
          data-testid="remote-http-cancel"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
          @click="confirmation = null"
        >{{ t('settings.remoteServers.cancel') }}</button>
        <button
          type="button"
          data-testid="remote-http-confirm"
          class="vxs-btn-primary px-3 py-1.5"
          @click="confirmHttp"
        >{{ t('settings.remoteServers.insecureConfirm') }}</button>
      </div>
    </ModalDialog>
  </div>
</template>
