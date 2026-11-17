<script lang="ts">
import { reactive } from 'vue'

// Renderer-store ownership survives view remounts, without sharing metadata UI
// state across Pinia roots or retaining any request inputs/credentials.
const metadataStates = new WeakMap<object, { pending: number; refreshRequired: boolean }>()
export function remoteProfileMetadataState(owner: object) {
  let state = metadataStates.get(owner)
  if (!state) {
    state = reactive({ pending: 0, refreshRequired: false })
    metadataStates.set(owner, state)
  }
  return state
}
</script>

<script setup lang="ts">
/** Keyless Create and name-only CAS Rename (remote-identity/studio-design §5.1/§10.1).
 * Inputs go directly to the local API, never through Pinia. Closing abandons the
 * continuation, not its store fence: only settlement may retire that request's ticket.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Loader2 } from 'lucide-vue-next'

import { ApiError } from '@/api/client'
import { createRemoteApiClient, RemoteApiError, type RemoteServerProfile } from '@/api/remote'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t, type MessageKey } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'

const props = defineProps<{ profile?: RemoteServerProfile }>()
const emit = defineEmits<{ saved: []; close: []; refreshRequired: [] }>()
const servers = useRemoteServersStore()
const metadata = remoteProfileMetadataState(servers)
const api = createRemoteApiClient()
// Never upgrade this snapshot's CAS after a refresh, prop replacement or in-place edit.
const initial = props.profile ? {
  profile_id: props.profile.profile_id, revision: props.profile.revision, url: props.profile.url,
} : undefined
const name = ref(props.profile?.name ?? '')
const url = ref('')
const nameField = ref<HTMLInputElement | null>(null)
const busy = ref(false)
const blocked = ref(false)
const error = ref<MessageKey | null>(null)
const title = computed(() => t(initial
  ? 'settings.remoteServers.metadata.renameTitle' : 'settings.remoteServers.metadata.addTitle'))
let active = true
let completed = false

onMounted(() => { void nextTick(() => { if (active) nameField.value?.focus() }) })
onBeforeUnmount(() => {
  if (busy.value && !completed) metadata.refreshRequired = true
  active = false
})

function close(): void {
  if (!active) return
  if (busy.value && !completed) metadata.refreshRequired = true
  active = false
  emit('close')
}
function same(value: RemoteServerProfile | undefined): boolean {
  return !!initial && !!value && initial.profile_id === value.profile_id
    && initial.revision === value.revision && initial.url === value.url
}
function stale(): void {
  blocked.value = true
  metadata.refreshRequired = true
  error.value = 'settings.remoteServers.metadata.stale'
  emit('refreshRequired')
}
function showFailure(cause: unknown): void {
  // Never render message/details/raw body, even when a boundary error carries them.
  if (cause instanceof RemoteApiError && cause.code === 'profile_url_collision') {
    blocked.value = true // Collision is also a conflict: close + explicit parent Refresh.
    metadata.refreshRequired = true
    error.value = 'settings.remoteServers.metadata.collision'
    emit('refreshRequired')
  } else if (cause instanceof ApiError && cause.status === 409) {
    blocked.value = true // Explicit close + parent Refresh, never a fresh CAS/retry here.
    metadata.refreshRequired = true
    error.value = 'settings.remoteServers.metadata.conflict'
    emit('refreshRequired')
  } else if (cause instanceof ApiError && cause.status === 422) {
    error.value = 'settings.remoteServers.metadata.invalid'
  } else if (cause instanceof ApiError && cause.status === 503) {
    error.value = 'settings.remoteServers.metadata.unavailable'
  } else {
    error.value = 'settings.remoteServers.metadata.failed'
  }
}
async function submit(): Promise<void> {
  if (!active || busy.value || blocked.value) return
  if (metadata.pending > 0 || metadata.refreshRequired) { stale(); return }
  error.value = null
  const label = name.value.trim()
  if (!label || [...label].length > 120 || /[\u0000-\u001f\u007f-\u009f]/.test(name.value)
    || (!initial && !url.value.trim())) {
    error.value = 'settings.remoteServers.metadata.invalid'
    return
  }
  // Check the live binding BEFORE taking a ticket; otherwise an old form could
  // pair a new store ticket with its old expected_revision and accept the wrong DTO.
  if (!servers.loaded || servers.loading || servers.error
    || (initial ? !same(props.profile)
      || !same(servers.items.find(item => item.profile_id === initial.profile_id)) : !!props.profile)) {
    stale()
    return
  }
  const ticket = servers.beginMutation(initial?.profile_id)
  if (!ticket) { stale(); return }
  metadata.pending += 1
  busy.value = true
  try {
    const result = initial
      ? await api.renameProfile(initial.profile_id, { expected_revision: initial.revision, name: name.value })
      : await api.createProfile({ name: name.value, url: url.value })
    if (!active) return
    if (initial ? !same(props.profile) : !!props.profile) { stale(); return }
    if (servers.applyMutation(ticket, result)) {
      // A second submit must not reuse this form's snapshot, even before parent unmount.
      blocked.value = true
      completed = true // saved may synchronously unmount; this is not abandonment.
      emit('saved')
    } else stale()
  } catch (cause) {
    if (active) showFailure(cause)
  } finally {
    // Also runs after close/Escape/unmount. Never dispose the renderer-owned store.
    servers.endMutation(ticket, false)
    metadata.pending -= 1
    if (active) busy.value = false
  }
}
</script>

<template>
  <ModalDialog :label="title" panel-class="w-lg p-5" instant @close="close">
    <form data-testid="remote-metadata-form" class="space-y-4 overflow-y-auto" novalidate :aria-busy="busy" @submit.prevent="submit">
      <h3 class="font-medium">{{ title }}</h3>
      <p v-if="!initial" class="text-small text-muted">{{ t('settings.remoteServers.metadata.keylessHint') }}</p>
      <label class="block">
        <span class="mb-1 block text-small text-muted">{{ t('settings.remoteServers.metadata.name') }}</span>
        <input
          ref="nameField"
          v-model="name"
          data-testid="remote-metadata-name"
          class="w-full rounded-md border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          autocomplete="off"
          :disabled="busy || blocked"
        />
      </label>
      <label v-if="!initial" class="block">
        <span class="mb-1 block text-small text-muted">{{ t('settings.remoteServers.metadata.url') }}</span>
        <input
          v-model="url"
          data-testid="remote-metadata-url"
          type="url"
          class="w-full rounded-md border border-line bg-canvas px-3 py-2 font-mono outline-none focus:border-accent"
          autocomplete="off"
          spellcheck="false"
          dir="ltr"
          :disabled="busy || blocked"
        />
      </label>
      <p v-if="error" role="alert" class="text-small text-danger">{{ t(error) }}</p>
      <div class="flex flex-wrap justify-end gap-2">
        <button type="button" data-testid="remote-metadata-cancel" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover" @click="close">
          {{ t('settings.remoteServers.metadata.cancel') }}
        </button>
        <button type="submit" data-testid="remote-metadata-submit" class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5" :disabled="busy || blocked || metadata.pending > 0 || metadata.refreshRequired">
          <Loader2 v-if="busy" class="size-3.5 animate-spin" aria-hidden="true" />
          {{ t(busy ? 'settings.remoteServers.metadata.saving' : initial ? 'settings.remoteServers.metadata.save' : 'settings.remoteServers.metadata.create') }}
        </button>
      </div>
    </form>
  </ModalDialog>
</template>
