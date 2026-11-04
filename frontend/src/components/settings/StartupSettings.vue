<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { t } from '@/i18n'
const api = window.voxariumStartup
const supported = ref(false)
const enabled = ref(false)
const busy = ref(true)
const error = ref('')
async function refresh(value?: boolean) {
  busy.value = true
  error.value = ''
  try {
    const state = await (value === undefined ? api?.getState() : api?.setEnabled(value))
    supported.value = state?.supported ?? false
    enabled.value = state?.enabled ?? false
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
  finally { busy.value = false }
}
onMounted(() => void refresh())
</script>

<template>
  <h2 class="mb-3 font-semibold">{{ t('settings.startup.title') }}</h2>
  <label class="flex items-center gap-3">
    <input type="checkbox" :checked="enabled" :disabled="busy || !supported" class="accent-[var(--vxs-accent)]" @change="refresh(($event.target as HTMLInputElement).checked)" />
    <span>{{ t('settings.startup.label') }}</span>
  </label>
  <p class="mt-2 text-small text-muted">{{ t(supported ? 'settings.startup.description' : 'settings.startup.desktopOnly') }}</p>
  <p v-if="error" class="mt-2 text-small text-danger" role="alert">{{ error }}</p>
</template>
