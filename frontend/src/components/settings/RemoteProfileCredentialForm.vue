<script setup lang="ts">
/** Personal credential intents only (remote-identity/studio-design §5.1/§10.1).
 * A secret lives only in this field and the direct API request, never in Pinia.
 * Closing abandons the continuation, not its renderer-owned mutation fence.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Loader2 } from 'lucide-vue-next'

import { ApiError } from '@/api/client'
import { createRemoteApiClient, RemoteApiError, type RemoteServerProfile } from '@/api/remote'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t, type MessageKey } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'
import { remoteProfileMetadataState } from './RemoteProfileMetadataForm.vue'

const props = defineProps<{ profile: RemoteServerProfile }>()
const emit = defineEmits<{ saved: []; close: []; refreshRequired: [] }>()
const servers = useRemoteServersStore()
const metadata = remoteProfileMetadataState(servers)
const api = createRemoteApiClient()
// A new prop or a refreshed store must never silently upgrade this intent's CAS.
const initial = {
  profile_id: props.profile.profile_id, revision: props.profile.revision, url: props.profile.url,
  credential_source: props.profile.credential_source,
}
const credential = ref('')
const credentialField = ref<HTMLInputElement | null>(null)
const confirmingForget = ref(false)
const busy = ref(false)
const blocked = ref(false)
const error = ref<MessageKey | null>(null)
const disabled = computed(() => busy.value || blocked.value || metadata.pending > 0 || metadata.refreshRequired)
const sourceKeys: Record<RemoteServerProfile['credential_source'], MessageKey> = {
  stored: 'settings.remoteServers.credential.stored',
  environment: 'settings.remoteServers.credential.environment',
  none: 'settings.remoteServers.credential.missing',
}
let active = true
let completed = false

function clearField(): void {
  credential.value = ''
  // Vue's render is batched; remove the DOM value synchronously as well.
  if (credentialField.value) credentialField.value.value = ''
}
function abandon(): void {
  clearField()
  confirmingForget.value = false
  if (busy.value && !completed) metadata.refreshRequired = true
  active = false
}
onMounted(() => { void nextTick(() => { if (active) credentialField.value?.focus() }) })
onBeforeUnmount(abandon)
function close(): void {
  if (!active) return
  abandon()
  emit('close')
}
function same(value: RemoteServerProfile | undefined): boolean {
  return !!value && initial.profile_id === value.profile_id
    && initial.revision === value.revision && initial.url === value.url
}
function stale(): void {
  clearField()
  confirmingForget.value = false
  blocked.value = true
  metadata.refreshRequired = true
  error.value = 'settings.remoteServers.credentials.stale'
  emit('refreshRequired')
}
function showFailure(cause: unknown): void {
  // Never retain or render raw messages, bodies or details, including error codes.
  if (cause instanceof ApiError && cause.status === 409) {
    blocked.value = true
    metadata.refreshRequired = true
    error.value = cause instanceof RemoteApiError && cause.code === 'profile_in_use'
      ? 'settings.remoteServers.credentials.inUse'
      : cause instanceof RemoteApiError && cause.code === 'profile_consumers_not_drained'
        ? 'settings.remoteServers.credentials.notDrained' : 'settings.remoteServers.credentials.conflict'
    emit('refreshRequired')
  } else if (cause instanceof ApiError && cause.status === 422) {
    error.value = 'settings.remoteServers.credentials.invalid'
  } else if (cause instanceof ApiError && cause.status === 503) {
    error.value = 'settings.remoteServers.credentials.unavailable'
  } else {
    error.value = 'settings.remoteServers.credentials.failed'
  }
}
function askForget(): void {
  if (!active || disabled.value) return
  clearField()
  error.value = null
  confirmingForget.value = true
}
function cancelForget(): void {
  if (!active || busy.value) return
  confirmingForget.value = false
}
async function mutate(intent: 'save' | 'forget'): Promise<void> {
  if (!active || busy.value || blocked.value) return
  // Empty means keep, never DELETE. A form submit cannot confirm Forget.
  if (intent === 'save' ? confirmingForget.value || !credential.value.trim() : !confirmingForget.value) return
  if (metadata.pending > 0 || metadata.refreshRequired) { stale(); return }
  error.value = null
  if (!servers.loaded || servers.loading || servers.error || !same(props.profile)
    || !same(servers.items.find(item => item.profile_id === initial.profile_id))) {
    stale()
    return
  }
  const ticket = servers.beginMutation(initial.profile_id)
  if (!ticket) { stale(); return }
  metadata.pending += 1
  busy.value = true
  confirmingForget.value = false
  try {
    // Dispatch directly before clearing. No action, emitted event or persistent input cache.
    const request = intent === 'save'
      ? api.setCredential(initial.profile_id, { expected_revision: initial.revision, token: credential.value })
      : api.clearCredential(initial.profile_id, { expected_revision: initial.revision })
    clearField()
    const result = await request
    if (!active) return
    if (!same(props.profile)) { stale(); return }
    if (servers.applyMutation(ticket, result)) {
      blocked.value = true // Do not reuse the old snapshot before the parent unmounts.
      completed = true // saved may synchronously unmount; this is not abandonment.
      emit('saved')
    } else stale()
  } catch (cause) {
    if (active) showFailure(cause)
  } finally {
    clearField() // Also covers a synchronous boundary throw.
    servers.endMutation(ticket, false)
    metadata.pending -= 1
    if (active) busy.value = false
  }
}
</script>

<template>
  <ModalDialog :label="t('settings.remoteServers.credentials.title')" panel-class="w-lg p-5" instant @close="close">
    <form data-testid="remote-credential-form" class="space-y-4 overflow-y-auto" novalidate :aria-busy="busy" @submit.prevent="mutate('save')">
      <h3 class="font-medium">{{ t('settings.remoteServers.credentials.title') }}</h3>
      <p class="font-mono text-small break-all"><bdi dir="ltr">{{ initial.url }}</bdi></p>
      <p data-testid="remote-credential-source" class="text-small text-muted">{{ t(sourceKeys[initial.credential_source]) }}</p>
      <p class="text-small text-faint">{{ t('settings.remoteServers.credentials.hint') }}</p>
      <label class="block">
        <span class="mb-1 block text-small text-muted">{{ t('settings.remoteServers.credentials.key') }}</span>
        <input
          ref="credentialField"
          v-model="credential"
          data-testid="remote-credential-key"
          type="password"
          class="w-full rounded-md border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          autocomplete="new-password"
          autocapitalize="none"
          spellcheck="false"
          dir="ltr"
          :disabled="disabled || confirmingForget"
        />
      </label>
      <p v-if="error" role="alert" class="text-small text-danger">{{ t(error) }}</p>
      <div v-if="confirmingForget" data-testid="remote-credential-confirmation" class="space-y-3 rounded-md border border-line p-3">
        <h4 class="text-small font-medium">{{ t('settings.remoteServers.credentials.forgetTitle') }}</h4>
        <p class="text-small text-warning">{{ t('settings.remoteServers.credentials.forgetWarning') }}</p>
        <div class="flex flex-wrap justify-end gap-2">
          <button type="button" data-testid="remote-credential-forget-cancel" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover" @click="cancelForget">
            {{ t('settings.remoteServers.credentials.forgetCancel') }}
          </button>
          <button type="button" data-testid="remote-credential-forget-confirm" class="rounded-md border border-line px-3 py-1.5 text-danger hover:bg-hover disabled:opacity-50" :disabled="disabled" @click="mutate('forget')">
            {{ t('settings.remoteServers.credentials.forgetConfirm') }}
          </button>
        </div>
      </div>
      <div class="flex flex-wrap justify-end gap-2">
        <button v-if="!confirmingForget" type="button" data-testid="remote-credential-forget" class="mr-auto rounded-md border border-line px-3 py-1.5 text-danger hover:bg-hover disabled:opacity-50" :disabled="disabled" @click="askForget">
          {{ t('settings.remoteServers.credentials.forget') }}
        </button>
        <button type="button" data-testid="remote-credential-cancel" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover" @click="close">
          {{ t('settings.remoteServers.credentials.cancel') }}
        </button>
        <button type="submit" data-testid="remote-credential-submit" class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5 disabled:opacity-50" :disabled="disabled || confirmingForget || !credential.trim()">
          <Loader2 v-if="busy" class="size-3.5 animate-spin" aria-hidden="true" />
          {{ t(busy ? 'settings.remoteServers.credentials.saving' : 'settings.remoteServers.credentials.save') }}
        </button>
      </div>
    </form>
  </ModalDialog>
</template>
