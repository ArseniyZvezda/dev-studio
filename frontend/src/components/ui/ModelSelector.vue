<script setup lang="ts">
/** One-line backend/model picker shared by members, tasks and chats (spec §10.3/§10.9). */
import { computed, ref, watch } from 'vue'
import { ChevronDown } from 'lucide-vue-next'
import type { MemberOptions } from '@/api/types'
import AgentBackendIcon from './AgentBackendIcon.vue'
import { t } from '@/i18n'

const props = defineProps<{
  backend: string | null
  model: string | null
  options: MemberOptions | null
  inheritedBackend?: string
  inheritedModel?: string
  allowInherit?: boolean
  disabled?: boolean
  label?: string
}>()
const emit = defineEmits<{ change: [backend: string | null, model: string | null] }>()
const backends = ['claude_code', 'codex', 'qwen'] as const
const selectedBackend = computed(() => props.backend || props.inheritedBackend || 'claude_code')
const models = computed(() => (props.options?.suggested_models ?? []).filter(
  entry => !entry.backend || entry.backend === selectedBackend.value,
))
const custom = ref(false)
watch(() => [props.backend, props.model], () => { custom.value = false })
const listed = computed(() => props.model === null || props.model === '' || models.value.some(entry => entry.id === props.model))
const editingCustom = computed(() => custom.value || !listed.value)
const modelValue = computed(() => editingCustom.value ? '__custom__' : props.model === null
  ? (props.allowInherit && selectedBackend.value === props.inheritedBackend ? '__inherit__' : '') : props.model)

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
        :title="name" :aria-label="name" :aria-pressed="selectedBackend === name" :disabled="disabled"
        @click.stop="chooseBackend(name)">
        <AgentBackendIcon :backend="name" class="size-4" />
      </button>
    </div>
    <input v-if="editingCustom" class="vxs-input min-w-0 flex-1 font-mono" :value="model ?? ''"
      :aria-label="t('teams.editor.modelId')" :placeholder="t('teams.editor.modelId')" :disabled="disabled"
      @change="emit('change', backend, ($event.target as HTMLInputElement).value.trim())" />
    <button v-if="editingCustom" type="button" class="grid size-7 shrink-0 place-items-center rounded-sm border border-line hover:bg-hover"
      :title="t('teams.editor.model')" :aria-label="t('teams.editor.model')" :disabled="disabled" @click.stop="showList">
      <ChevronDown class="size-4" />
    </button>
    <select v-else class="vxs-input min-w-0 flex-1" :value="modelValue" :aria-label="label || t('teams.editor.model')"
      :disabled="disabled" @change="chooseModel(($event.target as HTMLSelectElement).value)">
      <option v-if="allowInherit && selectedBackend === inheritedBackend" value="__inherit__">
        {{ t('tasks.overrides.sameAsMemberValue', { value: inheritedModel || t('chat.defaultModel') }) }}
      </option>
      <option value="">{{ t('chat.defaultModel') }}</option>
      <optgroup v-for="group in [...new Set(models.map(entry => entry.group))]" :key="group" :label="group">
        <option v-for="entry in models.filter(item => item.group === group)" :key="entry.id" :value="entry.id">{{ entry.label }}</option>
      </optgroup>
      <option value="__custom__">{{ t('teams.editor.somethingElse') }}</option>
    </select>
  </div>
</template>
