<script setup lang="ts">
/**
 * Per-task model and effort, overriding the assignee's for this card alone (§10.3).
 *
 * Shared by the composer and the task rail for the reason `DependencyPicker` is: both edit
 * the same two fields, and the rules about them — what "inherit" resolves to, that the
 * model list is a suggestion rather than a whitelist — belong to neither.
 *
 * Both controls default to **Same as the member**, and that is a real value rather than a
 * placeholder: null on the task means "whatever the role says", so a member retuned later
 * carries every card that never asked for something else. Choosing a level is therefore a
 * decision about *this* task, and the label spells out what the alternative currently is.
 *
 * The model list may be short one entry: a task carrying a model this build has never heard
 * of — an id typed in before, or one the roster is pinned to — is appended so the picker
 * cannot silently rewrite it on the next save. `MemberEditor` keeps its own copy of that
 * rule for the same reason and against a different value.
 */
import { computed, onMounted } from 'vue'

import type { TeamMember } from '@/api/types'
import ModelEffortSlider from '@/components/ui/ModelEffortSlider.vue'
import { t } from '@/i18n'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{
  model: string | null
  effort: string | null
  /** Who the task is assigned to, so "same as the member" can say what that means. */
  member: TeamMember | null
  /** Hidden entirely for a person, who has no session and so neither setting (§10.2). */
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:model': [string | null]; 'update:effort': [string | null] }>()

const teams = useTeamsStore()

// The rail and the composer are both opened without going through the member editor, which
// is what used to be the only thing that fetched these. The store keeps the first answer.
onMounted(() => void teams.loadOptions())

const choices = computed(() => teams.options?.suggested_models ?? [])

/** Grouped in the order the backend sent them, so aliases stay above the pinned ids. */
const modelGroups = computed(() => {
  const groups = new Map<string, typeof choices.value>()
  for (const entry of choices.value) {
    const existing = groups.get(entry.group)
    if (existing) existing.push(entry)
    else groups.set(entry.group, [entry])
  }
  return [...groups.entries()]
})

/** A model already on the task that the catalogue does not list — kept, never dropped. */
const unlistedModel = computed(() =>
  props.model && !choices.value.some((entry) => entry.id === props.model) ? props.model : null,
)

/** What inheriting resolves to right now — the whole reason the default row is readable. */
const inherited = computed(() => ({
  model: props.member?.model ?? '',
  effort: props.member?.effort ?? '',
}))

/** The blank option's words: the member's own value in brackets, when there is one. */
function sameAsMember(value: string): string {
  return value
    ? t('tasks.overrides.sameAsMemberValue', { value })
    : t('tasks.overrides.sameAsMember')
}

/** Blank is the first option, and it means the override is dropped rather than set empty. */
function pickModel(value: string): void {
  emit('update:model', value || null)
}
</script>

<template>
  <div v-if="!disabled" class="flex gap-3">
    <label class="min-w-0 flex-1">
      <span class="mb-1 block text-small text-faint">{{ t('tasks.overrides.model') }}</span>
      <select
        :value="model ?? ''"
        class="w-full rounded-md border border-line bg-inset px-2 py-1.5 outline-none focus:border-accent"
        @change="pickModel(($event.target as HTMLSelectElement).value)"
      >
        <option value="">{{ sameAsMember(inherited.model) }}</option>
        <optgroup v-for="[group, entries] in modelGroups" :key="group" :label="group">
          <option v-for="entry in entries" :key="entry.id" :value="entry.id">
            {{ entry.label }}
          </option>
        </optgroup>
        <option v-if="unlistedModel" :value="unlistedModel">{{ unlistedModel }}</option>
      </select>
    </label>
    <label class="shrink-0">
      <span class="mb-1 block text-small text-faint">{{ t('tasks.overrides.effort') }}</span>
      <ModelEffortSlider
        :model-value="effort"
        :levels="teams.options?.effort_levels"
        :label="t('tasks.overrides.effort')"
        :default-label="t('modelEffort.inherit')"
        :default-title="sameAsMember(inherited.effort)"
        @update:model-value="emit('update:effort', $event)"
      />
    </label>
  </div>
</template>
