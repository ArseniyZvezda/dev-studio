<script setup lang="ts">
/**
 * Project memory (§10.12): `voxarium_studio/memory/`, one file per lasting fact.
 *
 * Every agent run is sent the index — one `name — description` line per entry, capped — and
 * reads, adds and removes entries with `recall`, `remember` and `forget`. This page is where a
 * person sees what the team has recorded, corrects it, and decides whether agents get it at all.
 *
 * Agents write here while the page is open, so the list follows `team.memory.changed` and the
 * editor saves conditionally: an entry an agent changed since it was opened is not written over
 * without asking.
 */
import { AlertTriangle, Brain, FolderOpen, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { MemoryEntry, MemoryEntryDetail } from '@/api/types'
import ConfirmDialog from '@/components/explorer/ConfirmDialog.vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { formatNumber, t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import {
  MEMORY_CONTENT_MAX,
  MEMORY_DESCRIPTION_MAX,
  filterMemory,
  indexFill,
  isMemoryName,
  toMemoryName,
} from '@/lib/memory'
import { formatTime } from '@/lib/status'
import { useMemoryStore } from '@/stores/memory'
import { useSessionStore } from '@/stores/session'

/** `teams.memory_index_chars` bounds, as `core/config.py` declares them. */
const CAP_MIN = 500
const CAP_MAX = 100_000

const store = useMemoryStore()
const session = useSessionStore()

const error = ref<string | null>(null)
const busy = ref<string | null>(null)

const overview = computed(() => store.overview)
const entries = computed(() => overview.value?.entries ?? [])
const folder = computed(() => overview.value?.path ?? 'voxarium_studio/memory')
const search = ref('')
const filtered = computed(() => filterMemory(entries.value, search.value))

onMounted(() => {
  store.subscribe()
  void store.load()
})

function fail(cause: unknown): void {
  error.value = cause instanceof Error ? cause.message : String(cause)
}

function author(entry: MemoryEntry): string {
  return entry.author_member?.name ?? entry.author ?? t('memory.list.byUser')
}

// ---------------------------------------------------------------- sharing with agents

const capDraft = ref('')
watch(
  () => overview.value?.index_chars,
  (cap) => {
    if (cap !== undefined) capDraft.value = String(cap)
  },
  { immediate: true },
)
const capValid = computed(() => {
  const value = Number(capDraft.value)
  return Number.isInteger(value) && value >= CAP_MIN && value <= CAP_MAX
})
const fill = computed(() =>
  overview.value ? indexFill(overview.value.index.chars, overview.value.index_chars) : 0,
)
const omitted = computed(() =>
  overview.value ? overview.value.index.total - overview.value.index.shown : 0,
)

async function saveSharing(patch: { memory_enabled?: boolean; memory_index_chars?: number }): Promise<void> {
  busy.value = 'settings'
  try {
    const config = await api.updateSettings({ teams: patch })
    if (session.project) session.project = { ...session.project, config }
    await store.refresh()
    error.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

async function toggleEnabled(event: Event): Promise<void> {
  const box = event.target as HTMLInputElement
  await saveSharing({ memory_enabled: box.checked })
  // A failed save leaves the bound value unchanged, so Vue would not put the box back.
  if (overview.value) box.checked = overview.value.enabled
}

function commitCap(): void {
  if (!capValid.value) return
  const value = Number(capDraft.value)
  if (value !== overview.value?.index_chars) void saveSharing({ memory_index_chars: value })
}

// ---------------------------------------------------------------- creating

const creating = ref(false)
const draft = reactive({ name: '', description: '', content: '' })
const nameField = ref<HTMLInputElement | null>(null)
const draftName = computed(() => draft.name.trim())
const draftNameInvalid = computed(() => !!draftName.value && !isMemoryName(draftName.value))

async function startCreate(): Promise<void> {
  Object.assign(draft, { name: '', description: '', content: '' })
  creating.value = true
  error.value = null
  await nextTick()
  nameField.value?.focus()
}

/** A name typed as words is offered back as the id it would be saved under. */
function tidyName(): void {
  if (draftNameInvalid.value) {
    const suggested = toMemoryName(draft.name)
    if (suggested) draft.name = suggested
  }
}

async function create(): Promise<void> {
  tidyName()
  const name = draftName.value
  if (!isMemoryName(name)) {
    error.value = t('memory.create.nameInvalid')
    return
  }
  busy.value = '+'
  try {
    await api.saveMemoryEntry(name, {
      description: draft.description,
      content: draft.content,
      create: true,
    })
    creating.value = false
    error.value = null
    await store.refresh()
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 409) error.value = t('memory.create.taken', { name })
    else fail(cause)
  } finally {
    busy.value = null
  }
}

// ---------------------------------------------------------------- editing

/** The open entry, as read and as being typed. */
const editor = ref<{
  name: string
  detail: MemoryEntryDetail
  description: string
  content: string
} | null>(null)
const conflict = ref(false)

const editorDirty = computed(
  () =>
    !!editor.value &&
    (editor.value.description !== editor.value.detail.description ||
      editor.value.content !== editor.value.detail.content),
)

function discardEdits(): boolean {
  if (!editorDirty.value) return true
  return window.confirm(t('memory.editor.discardConfirm', { name: editor.value?.name ?? '' }))
}

function adoptDetail(detail: MemoryEntryDetail): void {
  editor.value = { name: detail.name, detail, description: detail.description, content: detail.content }
  conflict.value = false
}

async function openEditor(name: string): Promise<void> {
  if (editor.value?.name === name) return
  if (!discardEdits()) return
  busy.value = name
  try {
    adoptDetail(await api.memoryEntry(name))
    error.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

/** Back to the text as read. A conflict stays: the entry on disk is still newer than this. */
function revertEdits(): void {
  const open = editor.value
  if (!open) return
  open.description = open.detail.description
  open.content = open.detail.content
}

function closeEditor(): void {
  if (!discardEdits()) return
  editor.value = null
  conflict.value = false
}

/** `force` is "Keep mine" after a conflict: the write goes through without an expectation. */
async function save(force = false): Promise<void> {
  const open = editor.value
  if (!open) return
  const sent = { description: open.description, content: open.content }
  busy.value = open.name
  try {
    const detail = await api.saveMemoryEntry(open.name, {
      ...sent,
      expected_updated_at: force ? null : open.detail.updated_at,
    })
    // Typing may have continued while the request travelled: keep it, unsaved, against the new version.
    const current = editor.value
    if (current?.name === open.name) {
      editor.value = {
        name: open.name,
        detail,
        description: current.description === sent.description ? detail.description : current.description,
        content: current.content === sent.content ? detail.content : current.content,
      }
    }
    conflict.value = false
    error.value = null
    await store.refresh()
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 409) {
      conflict.value = true
      error.value = null
    } else {
      fail(cause)
    }
  } finally {
    busy.value = null
  }
}

async function reload(): Promise<void> {
  const open = editor.value
  if (!open) return
  try {
    const detail = await api.memoryEntry(open.name)
    if (editor.value?.name === open.name) adoptDetail(detail)
  } catch (cause) {
    fail(cause)
  }
}

function onEditorKey(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    if (editorDirty.value && busy.value === null) void save()
  }
}

// An agent's `remember` landed on the open entry: a clean editor takes it, a dirty one is told now.
watch(
  () =>
    editor.value
      ? entries.value.find((entry) => entry.name === editor.value?.name)?.updated_at
      : undefined,
  (updatedAt) => {
    const open = editor.value
    if (!open || !updatedAt || busy.value === open.name) return
    if (Date.parse(updatedAt) <= Date.parse(open.detail.updated_at)) return
    if (editorDirty.value) conflict.value = true
    else void reload()
  },
)

// …or its `forget` did.
watch(
  () => store.overview,
  (page) => {
    const open = editor.value
    if (open && page && !page.entries.some((entry) => entry.name === open.name)) {
      editor.value = null
      conflict.value = false
      error.value = t('memory.editor.deletedOnDisk', { name: open.name })
    }
  },
)

// ---------------------------------------------------------------- deleting

const deleting = ref<MemoryEntry | null>(null)

async function confirmDelete(): Promise<void> {
  const target = deleting.value
  if (!target) return
  busy.value = target.name
  try {
    store.adopt(await api.deleteMemoryEntry(target.name))
    if (editor.value?.name === target.name) {
      editor.value = null
      conflict.value = false
    }
    error.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
    deleting.value = null
  }
}

async function reveal(path: string): Promise<void> {
  try {
    await api.revealEntry(path)
  } catch (cause) {
    fail(cause)
  }
}
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="vxs-pane flex items-center gap-3 border-b border-pane bg-surface px-pane py-2">
      <div class="min-w-0 flex-1">
        <h1 class="text-title font-medium">{{ t('memory.title') }}</h1>
        <p class="truncate text-small text-faint">{{ folder }}</p>
      </div>
      <button
        class="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
        :disabled="store.loading"
        @click="store.refresh()"
      >
        <RefreshCw class="size-3.5" :class="{ 'animate-spin': store.loading }" /> {{ t('common.refresh') }}
      </button>
      <button
        class="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover"
        :title="t('memory.openFolderTitle', { folder })"
        @click="reveal(folder)"
      >
        <FolderOpen class="size-3.5" /> {{ t('memory.openFolder') }}
      </button>
    </header>

    <p v-if="error ?? store.error" class="bg-danger-soft px-pane py-1 text-danger">
      {{ error ?? store.error }}
    </p>

    <div class="max-w-3xl space-y-6 p-pane">
      <LoadingBars v-if="store.loading && !overview" />

      <template v-else-if="overview">
        <p class="vxs-enter text-muted">{{ t('memory.intro', { folder }) }}</p>

        <!-- What agents are actually sent: the switch, the cap, and the index under it. -->
        <section class="vxs-enter">
          <h2 class="mb-2 font-medium">{{ t('memory.sharing.title') }}</h2>
          <div class="space-y-3 rounded-lg border border-line bg-surface p-3">
            <label class="flex items-start gap-2">
              <input
                type="checkbox"
                class="mt-1 accent-[var(--vxs-accent)]"
                :checked="overview.enabled"
                :disabled="busy !== null"
                @change="toggleEnabled"
              />
              <span>
                <span class="font-medium">{{ t('memory.sharing.enabled') }}</span>
                <span class="block text-small text-faint">{{ t('memory.sharing.enabledHint') }}</span>
              </span>
            </label>

            <template v-if="overview.enabled">
              <label class="flex flex-wrap items-center gap-2 pl-6">
                <span class="text-muted">{{ t('memory.sharing.cap') }}</span>
                <input
                  v-model="capDraft"
                  type="number"
                  :min="CAP_MIN"
                  :max="CAP_MAX"
                  step="500"
                  class="w-28 rounded-md border bg-inset px-2 py-1 outline-none focus:border-accent disabled:opacity-50"
                  :class="capValid ? 'border-line' : 'border-danger'"
                  :disabled="busy !== null"
                  @change="commitCap"
                  @keydown.enter.prevent="commitCap"
                />
              </label>
              <p class="pl-6 text-small" :class="capValid ? 'text-faint' : 'text-danger'">
                {{
                  capValid
                    ? t('memory.sharing.capHint', { min: formatNumber(CAP_MIN), max: formatNumber(CAP_MAX) })
                    : t('memory.sharing.capInvalid', { min: formatNumber(CAP_MIN), max: formatNumber(CAP_MAX) })
                }}
              </p>

              <div class="space-y-1 pl-6">
                <div class="h-1.5 overflow-hidden rounded-full bg-inset" role="presentation">
                  <div
                    class="h-full rounded-full"
                    :class="omitted ? 'bg-warning' : 'bg-accent'"
                    :style="{ width: `${Math.round(fill * 100)}%` }"
                  />
                </div>
                <p class="text-small text-faint">
                  {{
                    t('memory.sharing.usage', {
                      chars: formatNumber(overview.index.chars),
                      cap: formatNumber(overview.index_chars),
                    })
                  }}
                  ·
                  {{ t('memory.sharing.entries', { count: overview.index.total, shown: overview.index.shown }) }}
                </p>
                <p v-if="omitted" class="flex items-start gap-1.5 text-small text-warning">
                  <AlertTriangle class="mt-0.5 size-3.5 shrink-0" />
                  {{ t('memory.sharing.truncated', { count: omitted }) }}
                </p>
              </div>

              <details class="pl-6">
                <summary class="cursor-pointer text-small text-muted">{{ t('memory.sharing.preview') }}</summary>
                <pre
                  v-if="overview.index.text"
                  class="memory-mono mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-line bg-inset p-2"
                >{{ overview.index.text }}</pre>
                <p v-else class="mt-2 text-small text-faint">{{ t('memory.sharing.previewEmpty') }}</p>
              </details>
            </template>
            <p v-else class="pl-6 text-small text-faint">{{ t('memory.sharing.off') }}</p>
          </div>
        </section>

        <section class="vxs-enter">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="font-medium">{{ t('memory.list.title') }}</h2>
            <button
              class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover"
              @click="startCreate"
            >
              <Plus class="size-3.5" /> {{ t('memory.create.button') }}
            </button>
          </div>

          <form
            v-if="creating"
            class="mb-2 space-y-3 rounded-lg border border-line bg-surface p-3"
            @submit.prevent="create"
          >
            <h3 class="font-medium">{{ t('memory.create.title') }}</h3>
            <label class="flex items-center gap-3">
              <span class="w-24 shrink-0 text-muted">{{ t('memory.create.name') }}</span>
              <input
                ref="nameField"
                v-model="draft.name"
                class="memory-mono flex-1 rounded-md border bg-inset px-2 py-1 outline-none focus:border-accent"
                :class="draftNameInvalid ? 'border-danger' : 'border-line'"
                placeholder="build-uses-ue-5-4"
                spellcheck="false"
                @blur="tidyName"
              />
            </label>
            <p class="pl-27 text-small" :class="draftNameInvalid ? 'text-danger' : 'text-faint'">
              <template v-if="draftNameInvalid">{{ t('memory.create.nameInvalid') }}</template>
              <I18nT v-else keypath="memory.create.nameHint">
                <template #path>
                  <span class="font-mono">{{ folder }}/{{ draftName || 'name' }}.md</span>
                </template>
              </I18nT>
            </p>
            <label class="flex items-start gap-3">
              <span class="w-24 shrink-0 pt-1 text-muted">{{ t('memory.create.description') }}</span>
              <textarea
                v-model="draft.description"
                rows="2"
                :maxlength="MEMORY_DESCRIPTION_MAX"
                class="flex-1 rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                :placeholder="t('memory.create.descriptionPlaceholder')"
              />
            </label>
            <p class="pl-27 text-small text-faint">
              {{ t('memory.create.descriptionHint', { max: formatNumber(MEMORY_DESCRIPTION_MAX) }) }}
            </p>
            <label class="flex items-start gap-3">
              <span class="w-24 shrink-0 pt-1 text-muted">{{ t('memory.create.content') }}</span>
              <textarea
                v-model="draft.content"
                rows="4"
                :maxlength="MEMORY_CONTENT_MAX"
                class="flex-1 rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                :placeholder="t('memory.create.contentPlaceholder')"
              />
            </label>
            <div class="flex gap-2 pl-27">
              <button
                type="submit"
                class="vxs-btn-primary px-3 py-1 disabled:opacity-50"
                :disabled="busy !== null || !draftName || !draft.description.trim()"
              >
                {{ t('common.create') }}
              </button>
              <button
                type="button"
                class="rounded-md border border-line px-3 py-1 hover:bg-hover"
                @click="creating = false"
              >
                {{ t('common.cancel') }}
              </button>
            </div>
          </form>

          <input
            v-if="entries.length"
            v-model="search"
            type="search"
            :placeholder="t('memory.search')"
            :aria-label="t('memory.search')"
            class="mb-3 w-full rounded-md border border-line bg-inset px-3 py-1.5 outline-none focus:border-accent"
          />
          <p v-if="entries.length && !filtered.length" class="py-3 text-muted">{{ t('memory.noMatches') }}</p>

          <ul class="space-y-2">
            <li
              v-for="entry in filtered"
              :key="entry.name"
              class="rounded-lg border bg-surface"
              :class="editor?.name === entry.name ? 'border-accent-border' : 'border-line'"
            >
              <div class="flex items-start gap-3 p-3">
                <Brain class="mt-0.5 size-5 shrink-0" style="color: var(--vxs-nav-memory)" />
                <div class="min-w-0 flex-1">
                  <p class="memory-mono truncate font-medium" :title="`${folder}/${entry.name}.md`">{{ entry.name }}</p>
                  <p v-if="entry.description" class="text-muted">{{ entry.description }}</p>
                  <p v-if="entry.warning" class="mt-0.5 flex items-start gap-1.5 text-small text-warning">
                    <AlertTriangle class="mt-0.5 size-3.5 shrink-0" /> {{ entry.warning }}
                  </p>
                  <p class="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-small text-faint">
                    <span>{{ formatTime(entry.updated_at) }}</span>
                    <span>·</span>
                    <span class="inline-flex items-center gap-1 text-muted">
                      <MemberMark v-if="entry.author_member" :member="entry.author_member" />{{ author(entry) }}
                    </span>
                    <template v-if="entry.chars">
                      <span>·</span>
                      <span>{{ t('memory.list.chars', { count: entry.chars }) }}</span>
                    </template>
                  </p>
                </div>
                <div class="flex shrink-0 gap-2">
                  <button
                    class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="busy === entry.name || editor?.name === entry.name"
                    :title="t('memory.list.editTitle')"
                    @click="openEditor(entry.name)"
                  >
                    <Pencil class="size-3.5" /> {{ t('common.edit') }}
                  </button>
                  <button
                    class="inline-flex items-center rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="busy === entry.name"
                    :title="t('memory.list.deleteTitle')"
                    @click="deleting = entry"
                  >
                    <Trash2 class="size-3.5" />
                  </button>
                </div>
              </div>

              <div v-if="editor?.name === entry.name" class="space-y-2 border-t border-line p-3" @keydown="onEditorKey">
                <p class="flex items-center gap-1.5 text-small text-faint">
                  <span class="font-mono">{{ folder }}/{{ entry.name }}.md</span>
                  <span v-if="editorDirty" class="text-warning">· {{ t('memory.editor.unsaved') }}</span>
                </p>
                <label class="block">
                  <span class="mb-1 flex justify-between text-small text-muted">
                    <span>{{ t('memory.create.description') }}</span>
                    <span class="text-faint">
                      {{ t('memory.editor.length', { length: formatNumber(editor.description.length), max: formatNumber(MEMORY_DESCRIPTION_MAX) }) }}
                    </span>
                  </span>
                  <textarea
                    v-model="editor.description"
                    rows="2"
                    :maxlength="MEMORY_DESCRIPTION_MAX"
                    class="w-full rounded-md border border-line bg-inset p-2 outline-none focus:border-accent"
                  />
                </label>
                <label class="block">
                  <span class="mb-1 block text-small text-muted">{{ t('memory.create.content') }}</span>
                  <textarea
                    v-model="editor.content"
                    rows="10"
                    :maxlength="MEMORY_CONTENT_MAX"
                    class="memory-mono w-full rounded-md border border-line bg-inset p-2 outline-none focus:border-accent"
                  />
                </label>

                <div
                  v-if="conflict"
                  class="flex flex-wrap items-center gap-2 rounded-md bg-warning-soft p-2 text-small text-warning"
                >
                  <AlertTriangle class="size-3.5 shrink-0" />
                  <span class="flex-1">{{ t('memory.editor.changedOnDisk') }}</span>
                  <button
                    class="rounded-md border border-line bg-surface px-2 py-0.5 text-primary hover:bg-hover"
                    @click="reload"
                  >
                    {{ t('common.reload') }}
                  </button>
                  <button
                    class="rounded-md border border-line bg-surface px-2 py-0.5 text-primary hover:bg-hover disabled:opacity-50"
                    :disabled="busy !== null"
                    @click="save(true)"
                  >
                    {{ t('memory.editor.keepMine') }}
                  </button>
                </div>

                <div class="flex items-center gap-2">
                  <button
                    class="vxs-btn-primary px-3 py-1 disabled:opacity-50"
                    :disabled="busy !== null || !editorDirty || !editor.description.trim()"
                    @click="save()"
                  >
                    {{ t('common.save') }}
                  </button>
                  <button
                    class="rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="!editorDirty"
                    @click="revertEdits"
                  >
                    {{ t('memory.editor.revert') }}
                  </button>
                  <button class="rounded-md border border-line px-3 py-1 hover:bg-hover" @click="closeEditor">
                    {{ t('common.close') }}
                  </button>
                  <span class="text-small text-faint">{{ t('memory.editor.shortcutHint') }}</span>
                </div>
              </div>
            </li>
          </ul>

          <EmptyState
            v-if="!entries.length && !creating"
            :title="t('memory.list.emptyTitle')"
            :hint="t('memory.list.emptyHint')"
          >
            <template #icon><Brain class="mx-auto mb-3 size-8 text-faint" /></template>
          </EmptyState>

          <!-- Files in the folder agents are not shown, named rather than hidden. -->
          <div v-if="overview.invalid.length" class="mt-3">
            <h3 class="mb-1 text-small font-medium text-muted">{{ t('memory.invalid.title') }}</h3>
            <ul class="space-y-1">
              <li
                v-for="item in overview.invalid"
                :key="item.file"
                class="flex items-center gap-2 rounded-md border border-line bg-inset p-2 text-small"
              >
                <span class="min-w-0 flex-1">
                  <span class="font-mono font-medium">{{ item.file }}</span>
                  <span class="text-warning"> — {{ item.error }}</span>
                </span>
                <button
                  class="inline-flex shrink-0 items-center rounded-md border border-line px-2 py-0.5 hover:bg-hover"
                  :title="t('memory.invalid.revealTitle')"
                  @click="reveal(`${folder}/${item.file}`)"
                >
                  <FolderOpen class="size-3.5" />
                </button>
              </li>
            </ul>
          </div>
        </section>

        <section class="vxs-enter">
          <h2 class="mb-2 font-medium">{{ t('memory.usage.title') }}</h2>
          <div class="space-y-2 rounded-lg border border-line bg-surface p-3 text-muted">
            <p>
              <I18nT keypath="memory.usage.body">
                <template #recall><span class="font-mono">recall</span></template>
                <template #remember><span class="font-mono">remember</span></template>
                <template #forget><span class="font-mono">forget</span></template>
              </I18nT>
            </p>
            <p class="text-small text-faint">
              <I18nT
                keypath="memory.usage.note"
                :params="{ settings: t('teams.settings.title'), section: t('agentPrompts.title') }"
              >
                <template #fragment>
                  <span class="font-medium">{{ t('agentPrompts.fragments.project_memory.label') }}</span>
                </template>
              </I18nT>
            </p>
          </div>
        </section>
      </template>
    </div>

    <ConfirmDialog
      v-if="deleting"
      :title="t('memory.delete.title', { name: deleting.name })"
      :message="t('memory.delete.message')"
      :items="[`${folder}/${deleting.name}.md`]"
      :confirm-label="t('common.delete')"
      danger
      :busy="busy !== null"
      @confirm="confirmDelete"
      @cancel="deleting = null"
    />
  </div>
</template>

<style scoped>
/* `base.css` gives fields an unlayered `font: inherit`, which outranks `font-mono`; a scoped
   class is unlayered too and more specific than the element (see SkillsView). */
.memory-mono {
  font-family: var(--vxs-font-mono);
}

textarea.memory-mono,
pre.memory-mono {
  font-size: 12px;
}
</style>
