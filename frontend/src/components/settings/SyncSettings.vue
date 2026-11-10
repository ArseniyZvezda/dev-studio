<script setup lang="ts">
/**
 * Online sync for this project (dev spec §16.5).
 *
 * Three things are entered here and they do not live in the same place. The server's address
 * and the space are the project's — saved in its `config.yaml`, so a teammate who pulls the
 * project already has them. The key is the person's: it goes to their own settings directory
 * and is never shown again, which is why the field is always empty and only says whether one
 * is stored.
 *
 * Saved with an explicit button rather than as it is typed, like the launch root: a
 * half-typed address is an address, and every save reconnects.
 */
import { computed, ref, watch } from 'vue'

import type { SyncState, SyncUpdate } from '@/api/types'
import { t, type MessageKey } from '@/i18n'
import { useSyncStore } from '@/stores/sync'

const sync = useSyncStore()

const enabled = ref(false)
const url = ref('')
const space = ref('')
const token = ref('')
const error = ref('')

const status = computed(() => sync.status)

// The form follows what is saved until somebody types in it — and again after each save.
watch(
  () => [status.value?.enabled, status.value?.url, status.value?.space] as const,
  ([savedEnabled, savedUrl, savedSpace]) => {
    enabled.value = savedEnabled ?? false
    url.value = savedUrl ?? ''
    space.value = savedSpace ?? ''
  },
  { immediate: true },
)

const dirty = computed(
  () =>
    enabled.value !== (status.value?.enabled ?? false) ||
    url.value.trim() !== (status.value?.url ?? '') ||
    space.value.trim() !== (status.value?.space ?? '') ||
    token.value.trim() !== '',
)

const STATE_LABELS: Record<SyncState, MessageKey> = {
  disabled: 'sync.settings.state.disabled',
  unconfigured: 'sync.settings.state.unconfigured',
  connecting: 'sync.settings.state.connecting',
  syncing: 'sync.settings.state.syncing',
  connected: 'sync.settings.state.connected',
  offline: 'sync.settings.state.offline',
  unauthorised: 'sync.settings.state.unauthorised',
  error: 'sync.settings.state.error',
}

const state = computed<SyncState>(() => status.value?.state ?? 'disabled')
const stateTone = computed(() => {
  if (state.value === 'connected') return 'bg-success'
  if (state.value === 'unauthorised' || state.value === 'error') return 'bg-danger'
  if (state.value === 'disabled') return 'bg-line'
  return 'bg-warning'
})
const others = computed(() => sync.others.map((peer) => peer.user.name))

async function apply(patch: SyncUpdate) {
  error.value = ''
  try {
    await sync.save(patch)
    token.value = ''
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

function save() {
  const patch: SyncUpdate = {
    enabled: enabled.value,
    url: url.value.trim(),
    space: space.value.trim(),
  }
  // Sent only when one was typed: an empty field means "keep the stored key".
  if (token.value.trim()) patch.token = token.value.trim()
  void apply(patch)
}

function forgetKey() {
  void apply({ token: '' })
}
</script>

<template>
  <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
    <p class="text-small text-faint">{{ t('sync.settings.intro') }}</p>

    <label class="flex items-center gap-2">
      <input v-model="enabled" type="checkbox" class="size-4" />
      <span>{{ t('sync.settings.enabled') }}</span>
    </label>

    <label class="block">
      <span class="mb-1 block text-muted">{{ t('sync.settings.url') }}</span>
      <input
        v-model="url"
        type="url"
        autocomplete="off"
        spellcheck="false"
        placeholder="https://studio.example.com"
        class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
      />
    </label>
    <p v-if="status?.insecure" class="text-small text-warning">
      {{ t('sync.settings.insecure') }}
    </p>

    <label class="block">
      <span class="mb-1 block text-muted">{{ t('sync.settings.space') }}</span>
      <input
        v-model="space"
        autocomplete="off"
        spellcheck="false"
        placeholder="my-game"
        class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
      />
      <span class="mt-1 block text-small text-faint">{{ t('sync.settings.spaceHint') }}</span>
    </label>

    <label class="block">
      <span class="mb-1 block text-muted">{{ t('sync.settings.token') }}</span>
      <div class="flex gap-2">
        <input
          v-model="token"
          type="password"
          autocomplete="off"
          spellcheck="false"
          :placeholder="
            status?.has_token ? t('sync.settings.tokenStored') : t('sync.settings.tokenMissing')
          "
          class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
        />
        <button
          v-if="status?.has_token"
          type="button"
          class="shrink-0 rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
          :disabled="sync.saving"
          @click="forgetKey"
        >
          {{ t('sync.settings.forget') }}
        </button>
      </div>
      <span class="mt-1 block text-small text-faint">{{ t('sync.settings.tokenHint') }}</span>
    </label>

    <div class="flex items-center gap-3">
      <button
        type="button"
        class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
        :disabled="!dirty || sync.saving"
        @click="save"
      >
        {{ t('sync.settings.save') }}
      </button>
      <span class="flex min-w-0 items-center gap-2 text-small" role="status">
        <span class="size-2 shrink-0 rounded-full" :class="stateTone" />
        <span class="truncate">
          {{ t(STATE_LABELS[state]) }}
          <template v-if="state === 'connected' && status?.user">
            · {{ t('sync.settings.signedInAs', { name: status.user.name }) }}
          </template>
        </span>
      </span>
    </div>

    <p v-if="status?.message && state !== 'connected'" class="text-small text-faint break-words">
      {{ status.message }}
    </p>
    <p v-if="state === 'connected'" class="text-small text-faint">
      {{
        others.length
          ? t('sync.settings.online', { names: others.join(', ') })
          : t('sync.settings.alone')
      }}
    </p>
    <p v-if="error" class="text-small text-danger break-words">{{ error }}</p>
  </div>
</template>
