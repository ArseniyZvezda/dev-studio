<script setup lang="ts">
/** Searchable assignment catalog, with independent backend and installation filters (§10.10). */
import { computed, nextTick, ref, useId, watch } from 'vue'
import { PopoverContent, PopoverPortal, PopoverRoot, PopoverTrigger } from 'reka-ui'
import { ArrowDownUp, ChevronDown, GraduationCap, Plus, Search, X } from 'lucide-vue-next'
import type { SkillEntry } from '@/api/types'
import { t } from '@/i18n'
import { filterSkillOptions, skillDisplayName, skillOptions, skillPickerScope, type SkillPickerScope } from '@/lib/skills'

const props = withDefaults(defineProps<{
  skills: readonly SkillEntry[] | null
  selected?: readonly string[]
  backend?: string
  disabled?: boolean
  loading?: boolean
  error?: string | null
  placeholder?: string
}>(), { selected: () => [], disabled: false, loading: false, error: null })
const emit = defineEmits<{ select: [name: string] }>()

const open = ref(false)
const query = ref('')
const backendFilter = ref('all')
const scopeFilter = ref<SkillPickerScope>('all')
const searchField = ref<HTMLInputElement | null>(null)
const list = ref<HTMLElement | null>(null)
const activeIndex = ref(-1)
const id = useId()
const available = computed(() => skillOptions(props.skills ?? [], props.selected, props.backend))
const backendFilters = computed(() => [
  'all', 'shared', ...new Set(available.value.flatMap((skill) => skill.backends ?? [])),
])
const scopeFilters = computed<SkillPickerScope[]>(() => [
  'all', 'studio', 'global',
  ...(available.value.some((skill) => skillPickerScope(skill) === 'project') ? ['project' as const] : []),
])
const filtered = computed(() => filterSkillOptions(available.value, query.value, backendFilter.value, scopeFilter.value))
const hasFilters = computed(() => !!query.value || backendFilter.value !== 'all' || scopeFilter.value !== 'all')
const pending = computed(() => props.loading || (props.skills === null && !props.error))
const activeId = computed(() => !pending.value && !props.error && filtered.value[activeIndex.value]
  ? `${id}-option-${activeIndex.value}` : undefined)

function backendLabel(backend: string): string {
  if (backend === 'all' || backend === 'shared') return t(`skills.picker.${backend}`)
  return ({ claude_code: 'Claude Code', codex: 'Codex', qwen: 'Qwen Code' } as Record<string, string>)[backend] ?? backend
}

function clearFilters(): void {
  query.value = ''
  backendFilter.value = 'all'
  scopeFilter.value = 'all'
}

watch(open, (value) => {
  if (value) {
    clearFilters()
    activeIndex.value = -1
  }
})
watch(() => props.disabled, (disabled) => { if (disabled) open.value = false })
watch(backendFilters, (values) => { if (!values.includes(backendFilter.value)) backendFilter.value = 'all' })
watch(scopeFilters, (values) => { if (!values.includes(scopeFilter.value)) scopeFilter.value = 'all' })
watch(filtered, () => { activeIndex.value = -1 })

function focusSearch(event: Event): void {
  event.preventDefault()
  void nextTick(() => searchField.value?.focus({ preventScroll: true }))
}

function choose(skill: SkillEntry): void {
  if (props.disabled || pending.value || props.error) return
  emit('select', skill.name)
  open.value = false
}

function onSearchKeydown(event: KeyboardEvent): void {
  if (event.isComposing) return
  if (event.key === 'Enter') {
    event.preventDefault()
    const selected = filtered.value[Math.max(0, activeIndex.value)]
    if (selected) choose(selected)
    return
  }
  if (pending.value || props.error || (event.key !== 'ArrowDown' && event.key !== 'ArrowUp')) return
  event.preventDefault()
  const length = filtered.value.length
  if (!length) return
  activeIndex.value = activeIndex.value < 0
    ? event.key === 'ArrowDown' ? 0 : length - 1
    : (activeIndex.value + (event.key === 'ArrowDown' ? 1 : -1) + length) % length
  void nextTick(() => list.value?.querySelector(`#${CSS.escape(activeId.value ?? '')}`)?.scrollIntoView({ block: 'nearest' }))
}
</script>

<template>
  <PopoverRoot v-model:open="open">
    <PopoverTrigger
      type="button"
      class="skill-picker-trigger flex w-full min-w-0 items-center gap-2 rounded-md border border-line bg-inset px-3 py-2 text-left text-small hover:border-line-strong hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
      :disabled="disabled"
    >
      <GraduationCap class="size-4 shrink-0 text-faint" aria-hidden="true" />
      <span class="min-w-0 flex-1 truncate">{{ placeholder || t('skills.picker.add') }}</span>
      <span v-if="!pending && !error" class="font-mono text-[11px] text-faint">{{ available.length }}</span>
      <ChevronDown class="size-3.5 shrink-0 text-faint" aria-hidden="true" />
    </PopoverTrigger>
    <PopoverPortal>
      <PopoverContent
        align="start"
        :side-offset="6"
        :collision-padding="12"
        class="skill-picker-panel z-[70] flex flex-col overflow-hidden rounded-xl border border-line bg-surface text-primary shadow-lg"
        :aria-label="placeholder || t('skills.picker.add')"
        @open-auto-focus="focusSearch"
      >
        <div class="flex shrink-0 items-center gap-2.5 border-b border-line-subtle px-3.5 py-3">
          <Search class="size-4 shrink-0 text-faint" aria-hidden="true" />
          <input
            ref="searchField"
            v-model="query"
            type="text"
            role="combobox"
            aria-autocomplete="list"
            :aria-expanded="open"
            :aria-controls="`${id}-list`"
            :aria-activedescendant="activeId"
            :aria-label="t('skills.search')"
            :placeholder="t('skills.search')"
            autocomplete="off"
            class="min-w-0 flex-1 bg-transparent text-small outline-none placeholder:text-faint focus-visible:outline-none"
            @keydown="onSearchKeydown"
          />
          <button v-if="query" type="button" class="grid size-5 place-items-center rounded text-faint hover:bg-hover hover:text-primary" :aria-label="t('skills.picker.clearSearch')" @click="query = ''; searchField?.focus()">
            <X class="size-3.5" aria-hidden="true" />
          </button>
        </div>

        <div class="shrink-0 space-y-2 border-b border-line-subtle bg-inset/50 px-3.5 py-3">
          <div class="flex items-start gap-2.5">
            <span class="w-14 shrink-0 pt-1 text-[11px] font-medium text-faint">{{ t('skills.picker.backend') }}</span>
            <div role="group" :aria-label="t('skills.picker.backend')" class="flex flex-wrap gap-1">
              <button v-for="backend in backendFilters" :key="backend" type="button" class="skill-filter" :aria-pressed="backendFilter === backend" @click="backendFilter = backend">{{ backendLabel(backend) }}</button>
            </div>
          </div>
          <div class="flex items-start gap-2.5">
            <span class="w-14 shrink-0 pt-1 text-[11px] font-medium text-faint">{{ t('skills.picker.scope') }}</span>
            <div role="group" :aria-label="t('skills.picker.scope')" class="flex flex-wrap gap-1">
              <button v-for="scope in scopeFilters" :key="scope" type="button" class="skill-filter" :aria-pressed="scopeFilter === scope" @click="scopeFilter = scope">{{ t(`skills.picker.${scope}`) }}</button>
            </div>
          </div>
        </div>

        <div :id="`${id}-list`" ref="list" role="listbox" :aria-label="t('skills.title')" :aria-busy="pending" class="skill-picker-results min-h-0 overflow-y-auto overscroll-contain p-1.5">
          <p v-if="pending" role="status" class="px-3 py-8 text-center text-small text-faint">{{ t('common.loading') }}</p>
          <p v-else-if="error" role="alert" class="px-3 py-5 text-small text-danger">{{ error }}</p>
          <div v-else-if="!filtered.length" class="px-4 py-7 text-center">
            <Search class="mx-auto mb-2 size-5 text-faint" aria-hidden="true" />
            <p class="text-small font-medium">{{ t(hasFilters ? 'skills.noMatches' : 'skills.picker.empty') }}</p>
            <button v-if="hasFilters" type="button" class="mt-2 text-small text-accent hover:underline" @click="clearFilters(); searchField?.focus()">{{ t('skills.picker.reset') }}</button>
          </div>
          <div
            v-for="(skill, index) in pending || error ? [] : filtered"
            :id="`${id}-option-${index}`"
            :key="skill.name"
            role="option"
            :aria-selected="activeIndex === index"
            class="skill-picker-option group flex cursor-pointer items-start gap-2.5 rounded-lg px-3 py-2.5"
            :class="activeIndex === index ? 'bg-active' : 'hover:bg-hover'"
            :title="`${skillDisplayName(skill)}\n${skill.description}\n${skill.path}`"
            @pointermove="activeIndex = index"
            @mousedown.prevent
            @click="choose(skill)"
          >
            <span class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md border border-line bg-surface"><GraduationCap class="size-4 text-muted" aria-hidden="true" /></span>
            <div class="min-w-0 flex-1">
              <div class="flex min-w-0 items-center gap-2">
                <span class="truncate text-small font-medium">{{ skillDisplayName(skill) }}</span>
                <span class="shrink-0 rounded border border-line px-1 py-px text-[10px] text-muted">{{ t(`skills.picker.${skillPickerScope(skill)}`) }}</span>
              </div>
              <p v-if="skill.description" class="mt-0.5 line-clamp-2 text-[12px] leading-relaxed text-muted">{{ skill.description }}</p>
              <p class="mt-1 truncate text-[11px] text-faint">{{ skill.backends?.length ? skill.backends.map(backendLabel).join(' · ') : t('skills.picker.shared') }}<template v-if="skill.source"> · {{ skill.source }}</template></p>
            </div>
            <Plus class="mt-1 size-3.5 shrink-0 text-faint" aria-hidden="true" />
          </div>
        </div>

        <div class="flex shrink-0 items-center justify-between gap-3 border-t border-line-subtle px-3.5 py-2 text-[11px] text-faint">
          <span role="status" aria-live="polite">{{ t('skills.picker.results', { count: filtered.length }) }}</span>
          <span class="flex items-center gap-1.5"><ArrowDownUp class="size-3" aria-hidden="true" />{{ t('skills.picker.keyboard') }}</span>
        </div>
      </PopoverContent>
    </PopoverPortal>
  </PopoverRoot>
</template>

<style scoped>
:global(.skill-picker-panel) {
  width: min(30rem, calc(100vw - 24px));
  max-height: min(35rem, var(--reka-popover-content-available-height));
  /* Reka's portal root does not inherit Vue's scope attribute. Override the general
     dialog entrance as well: opening a keyboard picker must respond immediately. */
  animation: none;
}
.skill-picker-results { max-height: 21rem; }
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
:global(.skill-picker-trigger:active:not(:disabled)), .skill-filter:active { transform: scale(0.99); }
@media (prefers-reduced-motion: reduce) {
  :global(.skill-picker-trigger:active:not(:disabled)), .skill-filter:active { transform: none; }
}
</style>
