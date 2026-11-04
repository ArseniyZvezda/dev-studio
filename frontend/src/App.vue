<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { setToken } from '@/api/auth'
import AppShell from '@/components/layout/AppShell.vue'
import DesktopTitleBar from '@/components/layout/DesktopTitleBar.vue'
import RuntimeVersionError from '@/components/layout/RuntimeVersionError.vue'
import { locale, t } from '@/i18n'
import { producerNeedsReply } from '@/lib/producer'
import { useChatStore } from '@/stores/chat'
import I18nT from '@/i18n/I18nT.vue'
import { loadPluginPages } from '@/plugin-runtime/loader'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const tokenInput = ref('')
const chats = useChatStore()
const producerAttention = computed(() => producerNeedsReply(chats.chats))
// §10.11: watch every chat even while the popup is closed or another page is visible.
watch([producerAttention, locale], ([waiting]) => {
  void window.voxariumWindow?.setProducerAttention?.(waiting).catch(() => {})
}, { immediate: true })

onMounted(async () => {
  await session.load()
  // Plugin pages extend the nav and the router, so they are fetched as part of
  // startup rather than lazily when someone happens to open the plugins screen.
  if (!session.error && !session.versionMismatch) await loadPluginPages()
  window.addEventListener('focus', checkVersion)
})
const checkVersion = () => void session.refreshVersion()
onBeforeUnmount(() => window.removeEventListener('focus', checkVersion))
watch([() => session.versionMismatch, () => session.backendVersion], ([mismatch], [wasMismatch]) => {
  if (mismatch) void session.recoverVersionMismatch()
  else if (wasMismatch) window.location.reload()
})

async function submitToken() {
  if (!tokenInput.value.trim()) return
  setToken(tokenInput.value.trim())
  await session.load()
  if (!session.error && !session.versionMismatch) await loadPluginPages()
}
</script>

<template>
  <div class="flex h-full flex-col">
    <DesktopTitleBar />
    <div class="min-h-0 flex-1">
  <div v-if="session.loading" class="grid h-full place-items-center text-muted">
    <div class="flex flex-col items-center gap-3">
      <div class="size-8 animate-spin rounded-full border-2 border-line border-t-accent" />
      <p>{{ t('app.connecting') }}</p>
    </div>
  </div>

  <div v-else-if="session.needsToken" class="grid h-full place-items-center">
    <form
      class="w-80 rounded-lg border border-line bg-surface p-6 shadow-lg"
      @submit.prevent="submitToken"
    >
      <h1 class="mb-1 text-base font-semibold">{{ t('app.tokenTitle') }}</h1>
      <p class="mb-4 text-muted">
        <I18nT keypath="app.tokenBody">
          <template #flag><code class="font-mono">--token</code></template>
        </I18nT>
      </p>
      <input
        v-model="tokenInput"
        type="password"
        autocomplete="off"
        :placeholder="t('app.tokenPlaceholder')"
        class="mb-3 w-full rounded-md border border-line bg-inset px-3 py-2 outline-none focus:border-accent"
      />
      <button
        type="submit"
        class="vxs-btn-primary w-full px-3 py-2 font-medium"
      >
        {{ t('app.connect') }}
      </button>
    </form>
  </div>

  <div v-else-if="session.error" class="grid h-full place-items-center">
    <div class="max-w-md rounded-lg border border-line bg-surface p-6 text-center shadow-lg">
      <h1 class="mb-2 text-base font-semibold text-danger">{{ t('app.unreachable') }}</h1>
      <p class="mb-4 text-muted">{{ session.error }}</p>
      <button
        class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
        @click="session.load()"
      >
        {{ t('common.retry') }}
      </button>
    </div>
  </div>

  <RuntimeVersionError v-else-if="session.versionMismatch" />
  <AppShell v-else />
    </div>
  </div>
</template>
