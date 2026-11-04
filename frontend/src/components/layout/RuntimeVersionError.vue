<script setup lang="ts">
import { computed } from 'vue'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const busy = computed(() => ['checking', 'restarting', 'reloading'].includes(session.recoveryState))
const progress = computed(() => {
  if (session.recoveryState === 'restarting') return t('app.versionMismatch.restarting')
  if (session.recoveryState === 'reloading') return t('app.versionMismatch.reloading')
  return t('app.versionMismatch.checking')
})
const reload = () => window.location.reload()
</script>

<template>
  <div class="grid h-full place-items-center overflow-auto p-6">
    <section role="alert" aria-labelledby="version-mismatch-title" class="w-full max-w-lg rounded-lg border border-danger/40 bg-surface p-6 shadow-lg">
      <h1 id="version-mismatch-title" class="mb-3 text-base font-semibold text-danger">{{ t('app.versionMismatch.title') }}</h1>
      <p class="mb-4 text-muted">{{ t('app.versionMismatch.body') }}</p>
      <dl class="mb-4 grid grid-cols-2 gap-2 rounded-md border border-line bg-inset p-3">
        <dt>{{ t('app.versionMismatch.frontend') }}</dt><dd class="text-right font-mono">{{ session.frontendVersion }}</dd>
        <dt>{{ t('app.versionMismatch.backend') }}</dt><dd class="text-right font-mono">{{ session.backendVersion ?? t('app.versionMismatch.unknown') }}</dd>
      </dl>
      <p v-if="busy" role="status" class="text-muted">{{ progress }}</p>
      <template v-else>
        <p class="mb-4 text-danger">{{ session.recoveryError }}</p>
        <div class="flex flex-wrap gap-2">
          <button class="vxs-btn-primary px-3 py-2" @click="session.recoverVersionMismatch(true)">{{ t('common.retry') }}</button>
          <button class="rounded-md border border-line px-3 py-2 hover:bg-hover" @click="reload">{{ t('common.reload') }}</button>
        </div>
      </template>
    </section>
  </div>
</template>
