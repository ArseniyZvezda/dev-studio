<script setup lang="ts">
/** Project-level commit description model (§10.4), independent of the task's agent. */
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { MemberOptions } from '@/api/types'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const options = ref<MemberOptions | null>(null)
const backend = ref('claude_code')
const model = ref('haiku')
const saving = ref(false)
const error = ref('')
const custom = ref(false)
const saved = computed(() => session.project?.config.teams)
watch(saved, (value) => {
  backend.value = value?.commit_backend ?? 'claude_code'
  model.value = value?.commit_model ?? 'haiku'
  custom.value = false
}, { immediate: true })
const models = computed(() => (options.value?.suggested_models ?? [])
  .filter((entry) => !entry.backend || entry.backend === backend.value))
const isCustom = computed(() => custom.value || (model.value !== '' && !models.value.some((entry) => entry.id === model.value)))
const dirty = computed(() => backend.value !== saved.value?.commit_backend || model.value.trim() !== saved.value?.commit_model)
const backendInfo = computed(() => options.value?.agent_backends[backend.value])

onMounted(async () => {
  try { options.value = await api.memberOptions() }
  catch (cause) { error.value = String(cause) }
})

function changeBackend(event: Event) {
  backend.value = (event.target as HTMLSelectElement).value
  model.value = backend.value === 'claude_code' ? 'haiku' : ''
  custom.value = false
}
function changeModel(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  custom.value = value === '__custom__'
  if (!custom.value) model.value = value
}
async function save() {
  saving.value = true
  error.value = ''
  try {
    const config = await api.updateSettings({ teams: {
      commit_backend: backend.value, commit_model: model.value.trim(),
    } })
    if (session.project) session.project = { ...session.project, config }
  } catch (cause) { error.value = String(cause) }
  finally { saving.value = false }
}
</script>

<template>
  <div class="mt-4 border-t border-pane pt-4">
    <h3 class="font-medium">{{ t('settings.commitDescription.title') }}</h3>
    <p class="mt-1 text-small text-faint">{{ t('settings.commitDescription.hint') }}</p>
    <div class="mt-3 flex flex-wrap items-end gap-3">
      <label class="min-w-44">
        <span class="mb-1 block text-small text-muted">{{ t('settings.commitDescription.backend') }}</span>
        <select class="vxs-input w-full" :value="backend" :disabled="saving || !options" @change="changeBackend">
          <option v-if="!options?.agent_backends[backend]" :value="backend">{{ backend }}</option>
          <option v-for="(_, name) in options?.agent_backends" :key="name" :value="name">{{ name }}</option>
        </select>
      </label>
      <label class="min-w-56">
        <span class="mb-1 block text-small text-muted">{{ t('settings.commitDescription.model') }}</span>
        <select class="vxs-input w-full" :value="isCustom ? '__custom__' : model" :disabled="saving || !options" @change="changeModel">
          <option v-for="entry in models" :key="entry.id" :value="entry.id">{{ entry.label }}</option>
          <option v-if="!models.some(entry => entry.id === '')" value="">{{ t('settings.commitDescription.default') }}</option>
          <option value="__custom__">{{ t('settings.commitDescription.custom') }}</option>
        </select>
      </label>
      <label v-if="isCustom" class="min-w-56">
        <span class="mb-1 block text-small text-muted">{{ t('settings.commitDescription.modelId') }}</span>
        <input v-model="model" class="vxs-input w-full" :disabled="saving" />
      </label>
      <button class="vxs-btn-primary" :disabled="saving || !dirty || !options" @click="save">{{ t('common.save') }}</button>
    </div>
    <p v-if="backendInfo && !backendInfo.available" class="mt-2 text-small text-warning">{{ backendInfo.reason }}</p>
    <p v-if="error" class="mt-2 text-small text-danger" role="alert">{{ error }}</p>
  </div>
</template>
