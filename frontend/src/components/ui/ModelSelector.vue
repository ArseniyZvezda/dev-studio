<script setup lang="ts">
/**
 * One-line backend/model picker shared by members, tasks, chats and plugin pages (spec
 * §10.3/§10.9, §9.3 `runtime.components`). `backends` narrows the icon row to what the
 * caller can actually run — Graphify reads documents through `claude -p`, so it passes
 * `['claude_code']`.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronDown } from 'lucide-vue-next'
import type { MemberOptions } from '@/api/types'
import AgentBackendIcon from './AgentBackendIcon.vue'
import { t } from '@/i18n'
import { apiConnections, loadApiConnections } from '@/lib/api-backends'
onMounted(() => loadApiConnections())

const props = defineProps<{
  backend: string | null
  model: string | null
  options: MemberOptions | null
  inheritedBackend?: string
  inheritedModel?: string
  allowInherit?: boolean
  allowAuto?: boolean
  disabled?: boolean
  label?: string
  backends?: readonly string[]
}>()
const emit = defineEmits<{ change: [backend: string | null, model: string | null] }>()
const ALL_BACKENDS = ['claude_code', 'codex', 'qwen', 'antigravity'] as const
const backends = computed(() => {
  const allowed = props.backends ? [...props.backends] : [...new Set([
    ...ALL_BACKENDS, ...Object.keys(props.options?.agent_backends ?? {}).filter(id =>
      !id.startsWith('openai_api:') || apiConnections.value.some(c => `openai_api:${c.id}` === id)
      || !apiConnections.value.length),
    ...apiConnections.value.map(c => `openai_api:${c.id}`),
    ...(props.backend ? [props.backend] : []),
  ])]
  return allowed.length ? allowed : [...ALL_BACKENDS]
})
const selectedBackend = computed(() => props.backend || props.inheritedBackend || backends.value[0])
const models = computed(() => {
  const connection = apiConnections.value.find(c => `openai_api:${c.id}` === selectedBackend.value)
  // §10.6: shared global metadata is fresher than a mounted member-options snapshot.
  if (connection) {
    return [...new Set([connection.model, ...(connection.models ?? [])])].map(id => ({
      id, label: id, group: connection.name,
    }))
  }
  const entries = (props.options?.suggested_models ?? []).filter(
    entry => !entry.backend || entry.backend === selectedBackend.value,
  )
  return entries
})
const custom = ref(false)
watch(() => [props.backend, props.model], () => { custom.value = false })
const listed = computed(() => props.model === null || props.model === '' || (props.allowAuto && props.model === 'auto') || models.value.some(entry => entry.id === props.model))
const editingCustom = computed(() => custom.value || !listed.value)
const modelValue = computed(() => editingCustom.value ? '__custom__' : props.model === null
  ? (props.allowInherit && selectedBackend.value === props.inheritedBackend ? '__inherit__' : '') : props.model)

function backendLabel(name: string): string {
  return apiConnections.value.find(c => `openai_api:${c.id}` === name)?.name
    || props.options?.agent_backends[name]?.label || name
}

function chooseBackend(value: string): void {
  if (props.disabled) return
  if (value === selectedBackend.value) return
  custom.value = false
  if (props.allowInherit && value === props.inheritedBackend) {
    emit('change', null, null)
  } else {
    emit('change', value, '')
  }
}

function chooseModel(value: string): void {
  if (value.trim() === 'auto' && !props.allowAuto) return
  if (value === '__custom__') { custom.value = true; return }
  custom.value = false
  emit('change', props.backend, value === '__inherit__' ? null : value)
}
function showList(): void {
  custom.value = false
  emit('change', props.backend, props.allowInherit && selectedBackend.value === props.inheritedBackend
    ? null : '')
}
</script>

<template>
  <div class="flex min-w-0 items-center gap-1" :aria-label="label || t('teams.editor.model')" data-model-selector>
    <div class="flex shrink-0 items-center rounded-md border border-line bg-inset p-0.5" role="group" :aria-label="t('teams.editor.backend')">
      <button v-for="name in backends" :key="name" type="button" class="grid size-7 place-items-center rounded-sm"
        :class="selectedBackend === name ? 'bg-accent-soft text-primary' : 'text-faint hover:bg-hover hover:text-primary'"
        :title="backendLabel(name)" :aria-label="backendLabel(name)" :aria-pressed="selectedBackend === name" :disabled="disabled"
        @click.stop="chooseBackend(name)">
        <AgentBackendIcon :backend="name" class="size-4" />
      </button>
    </div>
    <input v-if="editingCustom" class="min-w-0 flex-1 font-mono rounded-md border border-line bg-inset px-2 py-1 text-primary outline-none focus:border-accent" :value="model ?? ''"
      :aria-label="t('teams.editor.modelId')" :placeholder="t('teams.editor.modelId')" :disabled="disabled"
      @change="chooseModel(($event.target as HTMLInputElement).value.trim())" />
    <button v-if="editingCustom" type="button" class="grid size-7 shrink-0 place-items-center rounded-sm border border-line hover:bg-hover"
      :title="t('teams.editor.model')" :aria-label="t('teams.editor.model')" :disabled="disabled" @click.stop="showList">
      <ChevronDown class="size-4" />
    </button>
    <select v-else class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1 text-primary outline-none focus:border-accent" :value="modelValue" :aria-label="label || t('teams.editor.model')"
      :disabled="disabled" @change="chooseModel(($event.target as HTMLSelectElement).value)">
      <option v-if="allowInherit && selectedBackend === inheritedBackend" value="__inherit__">
        {{ t('tasks.overrides.sameAsMemberValue', { value: inheritedModel || t('chat.defaultModel') }) }}
      </option>
      <option v-if="allowAuto" value="auto">{{ t('effortSolver.autoModel') }}</option>
      <option value="">{{ t('chat.defaultModel') }}</option>
      <optgroup v-for="group in [...new Set(models.map(entry => entry.group))]" :key="group" :label="group">
        <option v-for="entry in models.filter(item => item.group === group)" :key="entry.id" :value="entry.id">{{ entry.label }}</option>
      </optgroup>
      <option value="__custom__">{{ t('teams.editor.somethingElse') }}</option>
    </select>
  </div>
</template>
