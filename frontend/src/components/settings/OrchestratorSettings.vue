<script setup lang="ts">
/** Global capability-to-model mapping, never saved to project settings (§10.11). */
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { MemberOptions, OrchestratorModels } from '@/api/types'
import ModelSelector from '@/components/ui/ModelSelector.vue'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'
import { apiConnections } from '@/lib/api-backends'

const session = useSessionStore()
const options = ref<MemberOptions | null>(null)
const draft = ref<OrchestratorModels>({})
const saving = ref(false)
const error = ref('')
const strengths = ['1', '2', '3'] as const
const saved = computed(() => ({
  ...Object.fromEntries(apiConnections.value.map(c => [
    `openai_api:${c.id}`, { '1': c.model, '2': c.model, '3': c.model },
  ])),
  ...session.project?.config.orchestrator_models ?? {},
}))
watch(saved, value => { draft.value = JSON.parse(JSON.stringify(value)) }, { immediate: true })
const backends = computed(() => [...new Set([
  ...Object.keys(options.value?.agent_backends ?? {}).filter(id => !id.startsWith('openai_api:')),
  ...apiConnections.value.map(c => `openai_api:${c.id}`),
])])
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(saved.value))
const valid = computed(() => backends.value.length > 0 && backends.value.every(backend =>
  strengths.every(strength => {
    const value = draft.value[backend]?.[strength]?.trim()
    return !!value && value !== 'auto'
  }),
))

function choose(backend: string, strength: typeof strengths[number], model: string | null) {
  draft.value[backend] = { ...draft.value[backend] ?? { '1': '', '2': '', '3': '' }, [strength]: model?.trim() ?? '' }
}

onMounted(async () => {
  try { options.value = await api.memberOptions() }
  catch (cause) { error.value = String(cause) }
})

async function save() {
  if (saving.value || !valid.value || !dirty.value) return
  saving.value = true
  error.value = ''
  try {
    const config = await api.updateApplicationSettings({ orchestrator_models: JSON.parse(JSON.stringify(draft.value)) })
    if (session.project) session.project = { ...session.project, config }
  } catch (cause) { error.value = String(cause) }
  finally { saving.value = false }
}
</script>

<template>
  <h2 class="mb-2 font-medium">{{ t('settings.orchestrator.title') }}</h2>
  <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
    <p class="text-small text-faint">{{ t('settings.orchestrator.hint') }}</p>
    <div class="overflow-x-auto">
      <table class="w-full border-separate border-spacing-2">
        <thead><tr>
          <th scope="col" class="text-left text-small text-muted">{{ t('settings.orchestrator.strength') }}</th>
          <th v-for="backend in backends" :key="backend" scope="col" class="text-left font-mono text-small">{{ apiConnections.find(c => `openai_api:${c.id}` === backend)?.name || backend }}</th>
        </tr></thead>
        <tbody><tr v-for="strength in strengths" :key="strength">
          <th scope="row" class="text-left font-mono tabular-nums">{{ strength }}</th>
          <td v-for="backend in backends" :key="backend" class="min-w-64">
            <ModelSelector :backend="backend" :backends="[backend]" :model="draft[backend]?.[strength] ?? ''"
              :options="options" :disabled="saving" :label="t('settings.orchestrator.cell', { strength, backend })"
              @change="(_backend, model) => choose(backend, strength, model)" />
          </td>
        </tr></tbody>
      </table>
    </div>
    <button class="vxs-btn-primary" :disabled="saving || !dirty || !valid" @click="save">
      {{ t(saving ? 'common.saving' : 'common.save') }}
    </button>
    <p v-if="error" role="alert" class="text-small text-danger">{{ error }}</p>
  </div>
</template>
