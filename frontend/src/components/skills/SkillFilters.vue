<script setup lang="ts">
/** §10.10: the catalog page and assignment picker share both filter axes. */
import { computed, watch } from 'vue'
import type { SkillEntry } from '@/api/types'
import { t } from '@/i18n'
import { skillBackendLabel, skillPickerScope, type SkillPickerScope } from '@/lib/skills'

const props = defineProps<{ skills: readonly SkillEntry[] }>()
const backend = defineModel<string>('backend', { required: true })
const scope = defineModel<SkillPickerScope>('scope', { required: true })
const backends = computed(() => ['all', 'shared', ...new Set(props.skills.flatMap(skill => skill.backends ?? []))])
const scopes = computed<SkillPickerScope[]>(() => ['all', 'studio', 'global',
  ...(props.skills.some(skill => skillPickerScope(skill) === 'project') ? ['project' as const] : []),
])
watch(backends, values => { if (!values.includes(backend.value)) backend.value = 'all' })
watch(scopes, values => { if (!values.includes(scope.value)) scope.value = 'all' })
</script>

<template>
  <div class="space-y-2">
    <div class="flex items-start gap-2.5">
      <span class="w-14 shrink-0 pt-1 text-[11px] font-medium text-faint">{{ t('skills.picker.backend') }}</span>
      <div role="group" :aria-label="t('skills.picker.backend')" class="flex flex-wrap gap-1">
        <button v-for="value in backends" :key="value" type="button" class="skill-filter"
          :aria-pressed="backend === value" @click="backend = value">{{ skillBackendLabel(value) }}</button>
      </div>
    </div>
    <div class="flex items-start gap-2.5">
      <span class="w-14 shrink-0 pt-1 text-[11px] font-medium text-faint">{{ t('skills.picker.scope') }}</span>
      <div role="group" :aria-label="t('skills.picker.scope')" class="flex flex-wrap gap-1">
        <button v-for="value in scopes" :key="value" type="button" class="skill-filter"
          :aria-pressed="scope === value" @click="scope = value">{{ t(`skills.picker.${value}`) }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.skill-filter {
  border: 1px solid transparent;
  border-radius: 5px;
  padding: 3px 7px;
  color: var(--vxs-text-muted);
  font-size: 11px;
  line-height: 1.4;
}
.skill-filter:hover { background: var(--vxs-bg-hover); color: var(--vxs-text-primary); }
.skill-filter[aria-pressed='true'] {
  background: var(--vxs-bg-active);
  border-color: var(--vxs-border-strong);
  color: var(--vxs-text-primary);
}
.skill-filter:active { transform: scale(0.99); }
@media (prefers-reduced-motion: reduce) { .skill-filter:active { transform: none; } }
</style>
