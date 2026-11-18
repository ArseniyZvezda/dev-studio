<script lang="ts">
import { reactive } from 'vue'
import { ApiError } from '@/api/client'
import { RemoteApiError } from '@/api/remote'
import type { MessageKey } from '@/i18n'

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

/** C7 URL change / profile deletion refusals (dev spec §16.11). Never a raw code or detail.
 * `consumers`: the census is shown again and the user may retry by hand; nothing retries.
 * `stale`: the captured revision no longer applies — close and explicitly Refresh.
 */
export function remoteLifecycleFailure(cause: unknown, intent: 'url' | 'delete'): {
  key: MessageKey; kind: 'consumers' | 'stale' | 'retry'
} {
  if (cause instanceof RemoteApiError) {
    switch (cause.code) {
      case 'profile_in_use': return { key: 'settings.remoteServers.lifecycle.inUse', kind: 'consumers' }
      case 'profile_consumers_not_drained': return { key: 'settings.remoteServers.lifecycle.notDrained', kind: 'consumers' }
      case 'consumer_lifecycle_unavailable': return { key: 'settings.remoteServers.lifecycle.notConfirmed', kind: 'consumers' }
      case 'profile_url_collision': return { key: 'settings.remoteServers.metadata.collision', kind: 'stale' }
    }
  }
  if (cause instanceof ApiError && (cause.status === 409 || cause.status === 404)) {
    return { key: 'settings.remoteServers.lifecycle.stale', kind: 'stale' }
  }
  if (cause instanceof ApiError && cause.status === 422) {
    return { key: intent === 'url' ? 'settings.remoteServers.lifecycle.invalidUrl' : 'settings.remoteServers.lifecycle.failed', kind: 'retry' }
  }
  if (cause instanceof ApiError && cause.status === 503) return { key: 'settings.remoteServers.lifecycle.unavailable', kind: 'retry' }
  return { key: 'settings.remoteServers.lifecycle.failed', kind: 'retry' }
}
</script>

<script setup lang="ts">
/** Keyless Create, name-only CAS Rename and the C7 URL change (remote-identity/studio-design
 * §5.1/§10.1, dev spec §16.11). Inputs go directly to the local API, never through Pinia.
 * Closing abandons the continuation, not its store fence: only settlement may retire that
 * request's ticket. A URL change always clears the saved key and needs an explicit review
 * of the exact old and new URL; its refusals are shown, never retried.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Loader2 } from 'lucide-vue-next'

import { createRemoteApiClient, createRemoteConsumersClient, type RemoteProfileConsumers as Census,
  type RemoteServerProfile } from '@/api/remote'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'
import RemoteProfileConsumers from './RemoteProfileConsumers.vue'

const props = defineProps<{ profile?: RemoteServerProfile; mode?: 'rename' | 'url' }>()
const emit = defineEmits<{ saved: []; close: []; refreshRequired: [] }>()
const servers = useRemoteServersStore()
const metadata = remoteProfileMetadataState(servers)
const api = createRemoteApiClient()
// Never upgrade this snapshot's CAS after a refresh, prop replacement or in-place edit.
const initial = props.profile ? {
  profile_id: props.profile.profile_id, revision: props.profile.revision, url: props.profile.url,
} : undefined
const urlMode = !!initial && props.mode === 'url'
const consumersApi = urlMode ? createRemoteConsumersClient() : null
const name = ref(props.profile?.name ?? '')
const url = ref('')
const nameField = ref<HTMLInputElement | null>(null)
const urlField = ref<HTMLInputElement | null>(null)
const busy = ref(false)
const blocked = ref(false)
const error = ref<MessageKey | null>(null)
// URL change: the exact new URL the user reviewed; only this string is ever sent.
const reviewed = ref<string | null>(null)
const census = ref<Census | null>(null)
const consumersPanel = ref<{ reload: () => Promise<void> } | null>(null)
const confirmed = computed(() => !!initial && census.value?.state === 'known'
  && census.value.profile_id === initial.profile_id && census.value.url === initial.url)
const disabled = computed(() => busy.value || blocked.value || metadata.pending > 0 || metadata.refreshRequired)
const title = computed(() => t(urlMode ? 'settings.remoteServers.changeUrl.title' : initial
  ? 'settings.remoteServers.metadata.renameTitle' : 'settings.remoteServers.metadata.addTitle'))
let active = true
let completed = false

onMounted(() => {
  void nextTick(() => { if (active) (urlMode ? urlField : nameField).value?.focus() })
})
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
  reviewed.value = null
  metadata.refreshRequired = true
  error.value = 'settings.remoteServers.metadata.stale'
  emit('refreshRequired')
}
function showFailure(cause: unknown): void {
  // Never render message/details/raw body, even when a boundary error carries them.
  if (urlMode) {
    const failure = remoteLifecycleFailure(cause, 'url')
    error.value = failure.key
    if (failure.kind === 'stale') {
      blocked.value = true // Explicit close + parent Refresh, never a fresh CAS/retry here.
      reviewed.value = null
      metadata.refreshRequired = true
      emit('refreshRequired')
    } else if (failure.kind === 'consumers') void consumersPanel.value?.reload()
    return
  }
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
/** URL step 1 → 2: freeze the exact new URL for review. Sends nothing. */
function review(): void {
  if (!active || !urlMode || disabled.value || reviewed.value !== null) return
  error.value = null
  const next = url.value.trim()
  if (!next || /[\u0000-\u001f\u007f-\u009f\s]/.test(next)) {
    error.value = 'settings.remoteServers.changeUrl.invalid'
    return
  }
  if (next === initial!.url) {
    error.value = 'settings.remoteServers.changeUrl.unchanged'
    return
  }
  reviewed.value = next
}
function back(): void {
  if (!active || busy.value) return
  reviewed.value = null
  error.value = null
}
function onSubmit(): void {
  if (urlMode) {
    if (reviewed.value === null) review()
    return // Only the explicit confirm button sends a URL change.
  }
  void submit()
}
async function submit(): Promise<void> {
  if (!active || busy.value || blocked.value) return
  if (urlMode && (reviewed.value === null || !confirmed.value)) return
  if (metadata.pending > 0 || metadata.refreshRequired) { stale(); return }
  error.value = null
  const label = name.value.trim()
  if (!urlMode && (!label || [...label].length > 120 || /[\u0000-\u001f\u007f-\u009f]/.test(name.value)
    || (!initial && !url.value.trim()))) {
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
    const result = urlMode
      ? await api.changeProfileUrl(initial!.profile_id, {
        expected_revision: initial!.revision, url: reviewed.value!, clear_credential: true,
      })
      : initial
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
    <form data-testid="remote-metadata-form" class="space-y-4 overflow-y-auto" novalidate :aria-busy="busy" @submit.prevent="onSubmit">
      <h3 class="font-medium">{{ title }}</h3>
      <template v-if="urlMode">
        <div class="space-y-1">
          <span class="block text-small text-muted">{{ t('settings.remoteServers.changeUrl.current') }}</span>
          <p data-testid="remote-url-current" class="font-mono text-small break-all"><bdi dir="ltr">{{ initial!.url }}</bdi></p>
        </div>
        <label v-if="reviewed === null" class="block">
          <span class="mb-1 block text-small text-muted">{{ t('settings.remoteServers.changeUrl.next') }}</span>
          <input
            ref="urlField"
            v-model="url"
            data-testid="remote-metadata-new-url"
            type="url"
            class="w-full rounded-md border border-line bg-canvas px-3 py-2 font-mono outline-none focus:border-accent"
            autocomplete="off"
            spellcheck="false"
            dir="ltr"
            :disabled="disabled"
          />
        </label>
        <p data-testid="remote-url-key-warning" class="text-small text-warning">{{ t('settings.remoteServers.changeUrl.keyWarning') }}</p>
        <RemoteProfileConsumers
          ref="consumersPanel"
          :profile="initial!"
          :api="consumersApi!"
          :disabled="disabled"
          @change="census = $event"
          @refresh-required="stale"
        />
        <p v-if="census && !confirmed" data-testid="remote-url-gate" class="text-small text-muted">{{ t('settings.remoteServers.lifecycle.required') }}</p>
        <div v-if="reviewed !== null" data-testid="remote-url-confirmation" class="space-y-2 rounded-md border border-line p-3">
          <h4 class="text-small font-medium">{{ t('settings.remoteServers.changeUrl.confirmTitle') }}</h4>
          <p class="text-small text-muted">{{ t('settings.remoteServers.changeUrl.from') }}</p>
          <p data-testid="remote-url-confirm-old" class="font-mono text-small break-all"><bdi dir="ltr">{{ initial!.url }}</bdi></p>
          <p class="text-small text-muted">{{ t('settings.remoteServers.changeUrl.to') }}</p>
          <p data-testid="remote-url-confirm-new" class="font-mono text-small break-all"><bdi dir="ltr">{{ reviewed }}</bdi></p>
          <p class="text-small text-warning">{{ t('settings.remoteServers.changeUrl.confirmWarning') }}</p>
        </div>
      </template>
      <template v-else>
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
      </template>
      <p v-if="error" role="alert" class="text-small text-danger">{{ t(error) }}</p>
      <div class="flex flex-wrap justify-end gap-2">
        <button type="button" data-testid="remote-metadata-cancel" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover" @click="close">
          {{ t('settings.remoteServers.metadata.cancel') }}
        </button>
        <template v-if="urlMode">
          <button v-if="reviewed === null" type="submit" data-testid="remote-url-review" class="vxs-btn-primary px-3 py-1.5 disabled:opacity-50" :disabled="disabled || !url.trim()">
            {{ t('settings.remoteServers.changeUrl.review') }}
          </button>
          <template v-else>
            <button type="button" data-testid="remote-url-back" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50" :disabled="busy" @click="back">
              {{ t('settings.remoteServers.changeUrl.back') }}
            </button>
            <button type="button" data-testid="remote-url-confirm" class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5 disabled:opacity-50" :disabled="disabled || !confirmed" @click="submit">
              <Loader2 v-if="busy" class="size-3.5 animate-spin" aria-hidden="true" />
              {{ t(busy ? 'settings.remoteServers.metadata.saving' : 'settings.remoteServers.changeUrl.confirm') }}
            </button>
          </template>
        </template>
        <button v-else type="submit" data-testid="remote-metadata-submit" class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5" :disabled="busy || blocked || metadata.pending > 0 || metadata.refreshRequired">
          <Loader2 v-if="busy" class="size-3.5 animate-spin" aria-hidden="true" />
          {{ t(busy ? 'settings.remoteServers.metadata.saving' : initial ? 'settings.remoteServers.metadata.save' : 'settings.remoteServers.metadata.create') }}
        </button>
      </div>
    </form>
  </ModalDialog>
</template>
