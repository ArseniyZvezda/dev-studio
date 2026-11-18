<script lang="ts">
import { ApiError } from '@/api/client'
import { RemoteApiError, type RemoteConsumersClient } from '@/api/remote'
import type { MessageKey } from '@/i18n'

export type RemoteConsumersApi = RemoteConsumersClient

/** Gate L refusals the key form can receive (dev spec §16). Never renders raw codes or details. */
export function remoteConsumerErrorKey(cause: unknown): MessageKey | null {
  if (!(cause instanceof RemoteApiError)) return null
  switch (cause.code) {
    case 'consumer_lifecycle_unavailable': return 'settings.remoteServers.consumers.notConfirmed'
    case 'profile_consumers_not_drained': return 'settings.remoteServers.credentials.notDrained'
    case 'profile_in_use': return 'settings.remoteServers.credentials.inUse'
    case 'consumer_inventory_changed': return 'settings.remoteServers.consumers.inventoryChanged'
    case 'consumer_confirmation_unavailable': return 'settings.remoteServers.consumers.confirmationUnavailable'
    default: return null
  }
}

function consumerFailure(cause: unknown): MessageKey {
  // On the census itself this code means its store is unreadable, not an unconfirmed URL.
  if (cause instanceof RemoteApiError && cause.code === 'consumer_lifecycle_unavailable') {
    return 'settings.remoteServers.consumers.unavailable'
  }
  const known = remoteConsumerErrorKey(cause)
  if (known) return known
  if (cause instanceof ApiError && (cause.status === 409 || cause.status === 404)) {
    return 'settings.remoteServers.consumers.conflict'
  }
  if (cause instanceof ApiError && cause.status === 503) return 'settings.remoteServers.consumers.unavailable'
  return 'settings.remoteServers.consumers.failed'
}
</script>

<script setup lang="ts">
/** Gate L: the user, not the Studio, vouches for which projects use a server URL (dev spec §16).
 * Shows exactly what the backend knows and confirms only on an explicit click with that
 * exact URL, list and revision. Nothing is inferred from recent projects, an empty list
 * or a successful Verify, and a changed inventory always asks again.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Loader2 } from 'lucide-vue-next'

import type { RemoteProfileConsumers, RemoteServerProfile } from '@/api/remote'
import { formatDate, t } from '@/i18n'

const props = defineProps<{
  profile: Pick<RemoteServerProfile, 'profile_id' | 'revision' | 'url'>
  api: RemoteConsumersApi
  disabled?: boolean
}>()
/** `change` carries the census only while it is settled and bound to this exact profile URL;
 * null while loading, writing or failed. `refreshRequired` means the profile itself moved.
 */
const emit = defineEmits<{ change: [census: RemoteProfileConsumers | null]; refreshRequired: [] }>()

// The panel's CAS is captured once, like the form that owns it.
const initial = { profile_id: props.profile.profile_id, revision: props.profile.revision, url: props.profile.url }
const census = ref<RemoteProfileConsumers | null>(null)
const loading = ref(false)
const busy = ref<'confirm' | 'withdraw' | null>(null)
const acknowledged = ref(false)
const error = ref<MessageKey | null>(null)
const blocked = ref(false)
let active = true
let sequence = 0

const activeRoots = computed(() => new Set(census.value?.active_roots ?? []))
const controlsDisabled = computed(() => props.disabled || loading.value || !!busy.value || blocked.value)

function publish(value: RemoteProfileConsumers | null): void {
  census.value = value
  emit('change', value)
}
function moved(): void {
  blocked.value = true
  publish(null)
  emit('refreshRequired')
}
function bound(value: RemoteProfileConsumers): boolean {
  return value.profile_id === initial.profile_id && value.url === initial.url
}

/** Read-only GET. Always resets the user's acknowledgement: it applied to the list shown. */
async function load(keepError = false): Promise<void> {
  if (!active || blocked.value) return
  const request = ++sequence
  acknowledged.value = false
  loading.value = true
  if (!keepError) error.value = null
  publish(null)
  try {
    const result = await props.api.profileConsumers(initial.profile_id)
    if (!active || request !== sequence) return
    if (!bound(result)) {
      error.value = 'settings.remoteServers.consumers.conflict'
      moved()
      return
    }
    publish(result)
  } catch (cause) {
    if (!active || request !== sequence) return
    error.value = cause instanceof ApiError && cause.status === 503
      ? 'settings.remoteServers.consumers.unavailable'
      : cause instanceof ApiError && (cause.status === 404 || cause.status === 409)
        ? 'settings.remoteServers.consumers.conflict' : 'settings.remoteServers.consumers.loadError'
    if (cause instanceof ApiError && (cause.status === 404 || cause.status === 409)) moved()
  } finally {
    if (active && request === sequence) loading.value = false
  }
}

async function change(intent: 'confirm' | 'withdraw'): Promise<void> {
  const shown = census.value
  if (!active || !shown || controlsDisabled.value) return
  if (intent === 'confirm' && (shown.state !== 'unknown' || !acknowledged.value)) return
  if (intent === 'withdraw' && shown.state !== 'known') return
  const request = ++sequence
  busy.value = intent
  error.value = null
  acknowledged.value = false
  publish(null)
  try {
    const result = intent === 'confirm'
      // Exactly what the user saw; the backend refuses if it no longer matches.
      ? await props.api.confirmProfileConsumers(initial.profile_id, {
        expected_revision: initial.revision, url: shown.url, bound_roots: [...shown.bound_roots],
      })
      : await props.api.withdrawProfileConsumers(initial.profile_id, { expected_revision: initial.revision })
    if (!active || request !== sequence) return
    if (!bound(result)) {
      error.value = 'settings.remoteServers.consumers.conflict'
      moved()
      return
    }
    publish(result)
  } catch (cause) {
    if (!active || request !== sequence) return
    busy.value = null
    const key = consumerFailure(cause)
    if (cause instanceof RemoteApiError && cause.code === 'consumer_inventory_changed') {
      // Show the new list and ask again; never confirm on the user's behalf.
      error.value = key
      await load(true)
      return
    }
    error.value = key
    if (key === 'settings.remoteServers.consumers.conflict') { moved(); return }
    publish(shown)
  } finally {
    if (active && request === sequence) busy.value = null
  }
}

function confirmedTime(value: string | null): string {
  if (value === null || !Number.isFinite(Date.parse(value))) return t('settings.remoteServers.unknownTime')
  return formatDate(new Date(value), {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}
function operationsText(value: RemoteProfileConsumers): string {
  return value.operations.map(item => t(`settings.remoteServers.consumers.operation.${item}`)).join(', ')
}

onMounted(() => { void load() })
onBeforeUnmount(() => { active = false; sequence += 1 })
defineExpose({ reload: () => load() })
</script>

<template>
  <section data-testid="remote-consumers" class="space-y-2 rounded-md border border-line p-3" :aria-busy="loading || !!busy">
    <h4 class="text-small font-medium">{{ t('settings.remoteServers.consumers.title') }}</h4>
    <p class="font-mono text-small break-all"><bdi dir="ltr" data-testid="remote-consumers-url">{{ census?.url ?? initial.url }}</bdi></p>
    <p v-if="loading" role="status" class="flex items-center gap-2 text-small text-muted">
      <Loader2 class="size-3.5 animate-spin" aria-hidden="true" />
      {{ t('settings.remoteServers.consumers.loading') }}
    </p>
    <template v-if="census">
      <ul v-if="census.bound_roots.length" data-testid="remote-consumers-roots" class="space-y-1 text-small">
        <li v-for="root in census.bound_roots" :key="root" class="flex flex-wrap items-baseline gap-2">
          <bdi dir="ltr" class="font-mono break-all">{{ root }}</bdi>
          <span v-if="activeRoots.has(root)" data-testid="remote-consumers-active" class="text-warning">
            {{ t('settings.remoteServers.consumers.syncing') }}
          </span>
        </li>
      </ul>
      <p v-else data-testid="remote-consumers-empty" class="text-small text-muted">
        {{ t('settings.remoteServers.consumers.empty') }}
      </p>
      <p v-if="census.operations.length" data-testid="remote-consumers-operations" class="text-small text-muted">
        {{ t('settings.remoteServers.consumers.operations', { operations: operationsText(census) }) }}
      </p>
      <p v-if="census.active_roots.length" data-testid="remote-consumers-active-warning" class="text-small text-warning">
        {{ t('settings.remoteServers.consumers.activeWarning') }}
      </p>
      <div v-if="census.state === 'known'" class="flex flex-wrap items-center justify-between gap-2">
        <p data-testid="remote-consumers-confirmed" class="text-small text-muted">
          {{ t('settings.remoteServers.consumers.confirmedAt', { time: confirmedTime(census.confirmed_at) }) }}
        </p>
        <button type="button" data-testid="remote-consumers-withdraw" class="rounded-md border border-line px-3 py-1.5 text-small hover:bg-hover disabled:opacity-50" :disabled="controlsDisabled" @click="change('withdraw')">
          {{ t('settings.remoteServers.consumers.withdraw') }}
        </button>
      </div>
      <div v-else class="space-y-2">
        <p class="text-small text-faint">{{ t('settings.remoteServers.consumers.unknownHint') }}</p>
        <label class="flex items-start gap-2 text-small">
          <input v-model="acknowledged" data-testid="remote-consumers-acknowledge" type="checkbox" class="mt-0.5" :disabled="controlsDisabled" />
          <span>{{ t('settings.remoteServers.consumers.acknowledge', { url: census.url }) }}</span>
        </label>
        <div class="flex justify-end">
          <button type="button" data-testid="remote-consumers-confirm" class="flex items-center gap-2 rounded-md border border-line px-3 py-1.5 text-small hover:bg-hover disabled:opacity-50" :disabled="controlsDisabled || !acknowledged" @click="change('confirm')">
            {{ t('settings.remoteServers.consumers.confirm') }}
          </button>
        </div>
      </div>
    </template>
    <p v-if="busy" role="status" class="flex items-center gap-2 text-small text-muted">
      <Loader2 class="size-3.5 animate-spin" aria-hidden="true" />
      {{ t('settings.remoteServers.consumers.saving') }}
    </p>
    <p v-if="error" role="alert" data-testid="remote-consumers-error" class="text-small text-danger">{{ t(error) }}</p>
    <button v-if="error && !census && !loading && !busy && !blocked" type="button" data-testid="remote-consumers-retry" class="rounded-md border border-line px-3 py-1.5 text-small hover:bg-hover disabled:opacity-50" :disabled="props.disabled" @click="load()">
      {{ t('settings.remoteServers.consumers.retry') }}
    </button>
  </section>
</template>
