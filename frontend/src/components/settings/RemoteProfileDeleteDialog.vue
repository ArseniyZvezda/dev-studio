<script setup lang="ts">
/** C7 profile deletion (dev spec §16.11): review of the exact name and URL, then an explicit
 * confirmation. The request carries only the captured revision; a refusal is shown, never
 * retried, and bindings are never removed on the user's behalf. Closing abandons the
 * continuation, not its renderer-owned mutation fence.
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { Loader2 } from 'lucide-vue-next'

import { createRemoteApiClient, createRemoteConsumersClient, type RemoteProfileConsumers as Census,
  type RemoteServerProfile } from '@/api/remote'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t, type MessageKey } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'
import { remoteLifecycleFailure, remoteProfileMetadataState } from './RemoteProfileMetadataForm.vue'
import RemoteProfileConsumers from './RemoteProfileConsumers.vue'

const props = defineProps<{ profile: RemoteServerProfile }>()
const emit = defineEmits<{ deleted: []; close: []; refreshRequired: [] }>()
const servers = useRemoteServersStore()
const metadata = remoteProfileMetadataState(servers)
const api = createRemoteApiClient()
const consumersApi = createRemoteConsumersClient()
// The reviewed snapshot: what the user saw is what the CAS deletes, never a refreshed row.
const initial = {
  profile_id: props.profile.profile_id, revision: props.profile.revision, url: props.profile.url,
  name: props.profile.name,
}
const confirming = ref(false)
const busy = ref(false)
const blocked = ref(false)
const error = ref<MessageKey | null>(null)
const census = ref<Census | null>(null)
const consumersPanel = ref<{ reload: () => Promise<void> } | null>(null)
const confirmed = computed(() => census.value?.state === 'known' && census.value.profile_id === initial.profile_id
  && census.value.url === initial.url)
const disabled = computed(() => busy.value || blocked.value || metadata.pending > 0 || metadata.refreshRequired)
let active = true
let completed = false

function abandon(): void {
  if (busy.value && !completed) metadata.refreshRequired = true
  active = false
}
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
function stale(key: MessageKey = 'settings.remoteServers.lifecycle.stale'): void {
  blocked.value = true
  confirming.value = false
  metadata.refreshRequired = true
  error.value = key
  emit('refreshRequired')
}
function proceed(): void {
  if (!active || disabled.value) return
  error.value = null
  confirming.value = true
}
function back(): void {
  if (!active || busy.value) return
  confirming.value = false
}
async function remove(): Promise<void> {
  if (!active || busy.value || blocked.value || !confirming.value || !confirmed.value) return
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
  try {
    await api.deleteProfile(initial.profile_id, { expected_revision: initial.revision })
    // A confirmed 204 is applied even after close: the profile no longer exists.
    if (servers.applyRemoval(ticket)) {
      completed = true
      if (active) { blocked.value = true; emit('deleted') }
    } else if (active) stale()
    else metadata.refreshRequired = true
  } catch (cause) {
    if (!active) return
    const failure = remoteLifecycleFailure(cause, 'delete')
    if (failure.kind === 'stale') { stale(failure.key); return }
    error.value = failure.key
    confirming.value = false
    if (failure.kind === 'consumers') void consumersPanel.value?.reload()
  } finally {
    servers.endMutation(ticket, false)
    metadata.pending -= 1
    if (active) busy.value = false
  }
}
</script>

<template>
  <ModalDialog :label="t('settings.remoteServers.delete.title')" panel-class="w-lg p-5" instant @close="close">
    <div data-testid="remote-delete-dialog" class="space-y-4 overflow-y-auto" :aria-busy="busy">
      <h3 class="font-medium">{{ t('settings.remoteServers.delete.title') }}</h3>
      <dl class="space-y-1 text-small">
        <dt class="text-muted">{{ t('settings.remoteServers.metadata.name') }}</dt>
        <dd data-testid="remote-delete-name" class="font-medium break-words"><bdi>{{ initial.name }}</bdi></dd>
        <dt class="text-muted">{{ t('settings.remoteServers.metadata.url') }}</dt>
        <dd data-testid="remote-delete-url" class="font-mono break-all"><bdi dir="ltr">{{ initial.url }}</bdi></dd>
      </dl>
      <p class="text-small text-faint">{{ t('settings.remoteServers.delete.hint') }}</p>
      <RemoteProfileConsumers
        ref="consumersPanel"
        :profile="initial"
        :api="consumersApi"
        :disabled="disabled"
        @change="census = $event"
        @refresh-required="stale()"
      />
      <p v-if="census && !confirmed" data-testid="remote-delete-gate" class="text-small text-muted">{{ t('settings.remoteServers.lifecycle.required') }}</p>
      <div v-if="confirming" data-testid="remote-delete-confirmation" class="space-y-2 rounded-md border border-line p-3">
        <h4 class="text-small font-medium">{{ t('settings.remoteServers.delete.confirmTitle') }}</h4>
        <p class="text-small text-warning">
          {{ t('settings.remoteServers.delete.confirmWarning', { name: initial.name, url: initial.url }) }}
        </p>
      </div>
      <p v-if="error" role="alert" class="text-small text-danger">{{ t(error) }}</p>
      <div class="flex flex-wrap justify-end gap-2">
        <button type="button" data-testid="remote-delete-cancel" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover" @click="close">
          {{ t('settings.remoteServers.cancel') }}
        </button>
        <button v-if="!confirming" type="button" data-testid="remote-delete-review" class="rounded-md border border-line px-3 py-1.5 text-danger hover:bg-hover disabled:opacity-50" :disabled="disabled" @click="proceed">
          {{ t('settings.remoteServers.delete.continue') }}
        </button>
        <template v-else>
          <button type="button" data-testid="remote-delete-back" class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50" :disabled="busy" @click="back">
            {{ t('settings.remoteServers.changeUrl.back') }}
          </button>
          <button type="button" data-testid="remote-delete-confirm" class="flex items-center gap-2 rounded-md border border-line px-3 py-1.5 text-danger hover:bg-hover disabled:opacity-50" :disabled="disabled || !confirmed" @click="remove">
            <Loader2 v-if="busy" class="size-3.5 animate-spin" aria-hidden="true" />
            {{ t(busy ? 'settings.remoteServers.delete.deleting' : 'settings.remoteServers.delete.confirm') }}
          </button>
        </template>
      </div>
    </div>
  </ModalDialog>
</template>
