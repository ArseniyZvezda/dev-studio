<script setup lang="ts">
/** Mandatory startup context, shared defaults plus member additions (§10.10). */
import { FileText, GraduationCap, LockKeyhole, Plus, Search, X } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId } from 'vue'

import { api } from '@/api/client'
import type { DocEntry } from '@/api/types'
import SkillPicker from '@/components/skills/SkillPicker.vue'
import { t } from '@/i18n'
import { skillChips, skillDisplayName } from '@/lib/skills'
import { useSkillsStore } from '@/stores/skills'
import { onEvent } from '@/ws/connection'

const props = withDefaults(defineProps<{
  skills: string[]
  documents: string[]
  inheritedSkills?: string[]
  inheritedDocuments?: string[]
  backend?: string
  shared?: boolean
  disabled?: boolean
}>(), { inheritedSkills: () => [], inheritedDocuments: () => [] })
const emit = defineEmits<{
  'update:skills': [string[]]
  'update:documents': [string[]]
}>()

const skillsStore = useSkillsStore()
const combinedSkills = computed(() => [...new Set([...props.inheritedSkills, ...props.skills])])
const combinedDocuments = computed(() => [...new Set([...props.inheritedDocuments, ...props.documents])])
const chips = computed(() => skillChips(combinedSkills.value, skillsStore.skills))
const docs = ref<DocEntry[] | null>(null)
const docsLoading = ref(false)
const docsError = ref<string | null>(null)
const docsOpen = ref(false)
const query = ref('')
const searchInput = ref<HTMLInputElement | null>(null)
const docTrigger = ref<HTMLButtonElement | null>(null)
const docResults = ref<HTMLElement | null>(null)
const documentPanelId = useId()
const docsByPath = computed(() => new Map((docs.value ?? []).map(doc => [doc.path, doc])))
const availableDocuments = computed(() => {
  const selected = new Set(combinedDocuments.value)
  const terms = query.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return (docs.value ?? []).filter(doc => !selected.has(doc.path) && terms.every(term =>
    `${doc.title} ${doc.path}`.toLocaleLowerCase().includes(term),
  ))
})
let disposed = false
let generation = 0
let refreshTimer: ReturnType<typeof setTimeout> | null = null

async function loadDocuments(): Promise<void> {
  const request = ++generation
  docsLoading.value = true
  docsError.value = null
  try {
    const result = await api.docs()
    if (!disposed && request === generation) docs.value = result
  } catch (cause) {
    if (!disposed && request === generation) docsError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (!disposed && request === generation) docsLoading.value = false
  }
}

function refreshDocuments(): void {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => { refreshTimer = null; void loadDocuments() }, 300)
}

const unsubscribeDocument = onEvent('team.doc.changed', refreshDocuments)
const unsubscribeFolder = onEvent('team.doc.folder_changed', refreshDocuments)
onMounted(() => {
  skillsStore.subscribe()
  void skillsStore.load()
  void loadDocuments()
})
onBeforeUnmount(() => {
  disposed = true
  unsubscribeDocument()
  unsubscribeFolder()
  if (refreshTimer) clearTimeout(refreshTimer)
})

function addSkill(name: string): void {
  if (!props.disabled && !combinedSkills.value.includes(name)) emit('update:skills', [...props.skills, name])
}

function addDocument(path: string): void {
  if (props.disabled || combinedDocuments.value.includes(path)) return
  emit('update:documents', [...props.documents, path])
  void nextTick(() => searchInput.value?.focus())
}

async function toggleDocuments(): Promise<void> {
  docsOpen.value = !docsOpen.value
  if (!docsOpen.value) return
  query.value = ''
  void loadDocuments()
  await nextTick()
  searchInput.value?.focus()
}

function closeDocuments(event: KeyboardEvent): void {
  event.stopPropagation()
  docsOpen.value = false
  docTrigger.value?.focus()
}

function navigateDocuments(event: KeyboardEvent): void {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  if (event.target === searchInput.value && ['Home', 'End'].includes(event.key)) return
  const buttons = [...(docResults.value?.querySelectorAll<HTMLButtonElement>('button') ?? [])]
  if (!buttons.length) return
  event.preventDefault()
  const current = buttons.indexOf(event.target as HTMLButtonElement)
  const target = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : event.key === 'ArrowDown' ? (current + 1) % buttons.length
      : current <= 0 ? buttons.length - 1 : current - 1
  buttons[target]?.focus()
}
</script>

<template>
  <section class="space-y-4 border-t border-line-subtle pt-4">
    <div>
      <h3 class="text-small font-semibold tracking-wide text-muted uppercase">{{ t('teams.requirements.title') }}</h3>
      <p class="mt-1 text-small text-faint">{{ t(shared ? 'teams.requirements.sharedHint' : 'teams.requirements.memberHint') }}</p>
    </div>

    <div class="space-y-2">
      <h4 class="flex items-center gap-1.5 font-medium"><GraduationCap class="size-4 text-accent" />{{ t('teams.requirements.skills') }}</h4>
      <p class="text-small text-faint">{{ t('teams.requirements.skillsHint') }}</p>
      <ul v-if="chips.length" class="space-y-1.5">
        <li v-for="chip in chips" :key="chip.name" class="flex min-w-0 items-center gap-2 rounded-md border border-line bg-inset px-2 py-1.5">
          <div class="min-w-0 flex-1">
            <p class="truncate text-small font-medium" :class="chip.missing ? 'text-warning' : ''" :title="chip.skill?.path || chip.name">
              {{ chip.skill ? skillDisplayName(chip.skill) : chip.name }}
            </p>
            <p v-if="chip.missing" class="text-small text-warning">{{ t('teams.requirements.missingSkill') }}</p>
            <p v-else-if="chip.skill?.source" class="truncate text-small text-faint">
              {{ chip.skill.source }}<template v-if="chip.skill.scope"> · {{ t(`skills.scope.${chip.skill.scope}`) }}</template>
            </p>
            <span v-if="inheritedSkills.includes(chip.name)" class="mt-0.5 inline-flex items-center gap-1 text-small text-faint">
              <LockKeyhole class="size-3" />{{ t('teams.requirements.inherited') }}
            </span>
          </div>
          <button v-if="skills.includes(chip.name)" type="button" :disabled="disabled"
            class="grid size-6 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-danger focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
            :aria-label="t('teams.requirements.removeSkill', { name: chip.skill ? skillDisplayName(chip.skill) : chip.name })"
            @click="emit('update:skills', skills.filter(name => name !== chip.name))"><X class="size-3.5" /></button>
        </li>
      </ul>
      <SkillPicker :skills="skillsStore.skills" :selected="combinedSkills" :backend="backend" :disabled="disabled"
        :loading="skillsStore.loading" :error="skillsStore.error" :placeholder="t('teams.requirements.addSkill')" @select="addSkill" />
    </div>

    <div class="space-y-2">
      <h4 class="flex items-center gap-1.5 font-medium"><FileText class="size-4 text-accent" />{{ t('teams.requirements.documents') }}</h4>
      <p class="text-small text-faint">{{ t('teams.requirements.documentsHint') }}</p>
      <ul v-if="combinedDocuments.length" class="space-y-1.5">
        <li v-for="path in combinedDocuments" :key="path" class="flex min-w-0 items-center gap-2 rounded-md border border-line bg-inset px-2 py-1.5">
          <div class="min-w-0 flex-1">
            <p class="truncate text-small font-medium" :title="path">{{ docsByPath.get(path)?.title || path }}</p>
            <p v-if="docsByPath.get(path)?.title" class="truncate font-mono text-small text-faint" :title="path">{{ path }}</p>
            <p v-if="docs !== null && !docsError && !docsByPath.has(path)" class="text-small text-warning">{{ t('teams.requirements.missingDocument') }}</p>
            <span v-if="inheritedDocuments.includes(path)" class="mt-0.5 inline-flex items-center gap-1 text-small text-faint">
              <LockKeyhole class="size-3" />{{ t('teams.requirements.inherited') }}
            </span>
          </div>
          <button v-if="documents.includes(path)" type="button" :disabled="disabled"
            class="grid size-6 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-danger focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
            :aria-label="t('teams.requirements.removeDocument', { name: docsByPath.get(path)?.title || path })"
            @click="emit('update:documents', documents.filter(name => name !== path))"><X class="size-3.5" /></button>
        </li>
      </ul>
      <button ref="docTrigger" type="button" :disabled="disabled" :aria-expanded="docsOpen" :aria-controls="documentPanelId"
        class="flex w-full items-center gap-2 rounded-md border border-line bg-inset px-2.5 py-2 text-left text-small text-muted hover:border-accent hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
        @click="toggleDocuments"><Plus class="size-3.5" />{{ t('teams.requirements.addDocument') }}</button>
      <div v-if="docsOpen" :id="documentPanelId" class="overflow-hidden rounded-md border border-line bg-surface" @keydown.esc="closeDocuments" @keydown="navigateDocuments">
        <div class="flex items-center gap-2 border-b border-line px-2.5 py-2">
          <Search class="size-3.5 shrink-0 text-faint" />
          <input ref="searchInput" v-model="query" type="search" :placeholder="t('teams.requirements.searchDocuments')" :aria-label="t('teams.requirements.searchDocuments')" class="min-w-0 flex-1 bg-transparent text-small outline-none placeholder:text-faint" />
        </div>
        <p v-if="docsLoading" class="px-3 py-3 text-small text-faint" role="status">{{ t('common.loading') }}</p>
        <div v-else-if="docsError" class="space-y-2 p-3">
          <p class="text-small text-danger" role="alert">{{ docsError }}</p>
          <button type="button" class="rounded-md border border-line px-2 py-1 text-small hover:bg-hover" @click="loadDocuments">{{ t('common.retry') }}</button>
        </div>
        <ul v-else-if="availableDocuments.length" ref="docResults" class="max-h-52 overflow-y-auto p-1">
          <li v-for="doc in availableDocuments" :key="doc.path">
            <button type="button" :disabled="disabled" class="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-hover focus-visible:bg-accent-soft focus-visible:outline-none disabled:opacity-50" @click="addDocument(doc.path)">
              <FileText class="size-4 shrink-0 text-faint" />
              <span class="min-w-0 flex-1"><span class="block truncate text-small font-medium">{{ doc.title }}</span><span class="block truncate font-mono text-small text-faint" :title="doc.path">{{ doc.path }}</span></span>
              <Plus class="size-3.5 shrink-0 text-faint" />
            </button>
          </li>
        </ul>
        <p v-else class="px-3 py-4 text-small text-faint">{{ t(query ? 'teams.requirements.noMatchingDocuments' : 'teams.requirements.noDocuments') }}</p>
      </div>
      <p v-if="docsError && !docsOpen" class="text-small text-danger" role="alert">{{ docsError }}</p>
    </div>
  </section>
</template>
