<script setup lang="ts">
/** Project-level commit description model (§10.4), independent of the task's agent. */
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { MemberOptions } from '@/api/types'
import ModelSelector from '@/components/ui/ModelSelector.vue'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const options = ref<MemberOptions | null>(null)
const backend = ref('claude_code')
const model = ref('haiku')
const saving = ref(false)
const error = ref('')
const saved = computed(() => session.project?.config.teams)
watch(saved, (value) => {
  backend.value = value?.commit_backend ?? 'claude_code'
  model.value = value?.commit_model ?? 'haiku'
}, { immediate: true })
const dirty = computed(() => backend.value !== saved.value?.commit_backend || model.value.trim() !== saved.value?.commit_model)
const backendInfo = computed(() => options.value?.agent_backends[backend.value])

onMounted(async () => {
  try { options.value = await api.memberOptions() }
  catch (cause) { error.value = String(cause) }
})

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
      <ModelSelector class="min-w-60 flex-1" :backend="backend" :model="model" :options="options"
        :disabled="saving || !options" @change="(nextBackend, nextModel) => {
          backend = nextBackend ?? backend; model = nextModel ?? ''
        }" />
      <button class="vxs-btn-primary" :disabled="saving || !dirty || !options" @click="save">{{ t('common.save') }}</button>
    </div>
    <p v-if="backendInfo && !backendInfo.available" class="mt-2 text-small text-warning">{{ backendInfo.reason }}</p>
    <p v-if="error" class="mt-2 text-small text-danger" role="alert">{{ error }}</p>
  </div>
</template>
