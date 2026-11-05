<script setup lang="ts">
/**
 * Skills for the project's agents (§10.10): `voxarium_studio/skills/`, one folder per skill.
 *
 * A skill is a `SKILL.md` — a description in its frontmatter, instructions under it — and
 * whatever files those instructions refer to. It is called by its **folder's name**, which is
 * what the member editor offers and what a member's list stores.
 *
 * The page is deliberately not where a skill is given to anyone. Nothing here reaches an agent
 * by default for project skills: a member gets a skill in their settings on the Teams page, and only then is that
 * member told its name, description and where to read it. What the page does show is who has
 * each one, because that is the question to answer before editing or deleting it — and the one
 * thing the folder cannot say.
 * Package skills are read-only, identified by `source`, and available to every agent session.
 *
 * The folder is changed by copies and checkouts as often as from here, so the list follows the
 * file watcher (`stores/skills.ts`) and the editor saves conditionally: a `SKILL.md` that
 * changed on disk since it was opened is not written over without asking.
 */
import {
  AlertTriangle,
  FileText,
  FolderOpen,
  GraduationCap,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  UserMinus,
} from 'lucide-vue-next'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { SkillDetail, SkillEntry, SkillUser } from '@/api/types'
import ConfirmDialog from '@/components/explorer/ConfirmDialog.vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import ClampText from '@/components/ui/ClampText.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { describeFiles, filterSkills, skillDisplayName } from '@/lib/skills'
import { useSkillsStore } from '@/stores/skills'

const store = useSkillsStore()

const error = ref<string | null>(null)
const busy = ref<string | null>(null)

const overview = computed(() => store.overview)
const skills = computed(() => overview.value?.skills ?? [])
const search = ref('')
const filteredSkills = computed(() => filterSkills(skills.value, search.value))
const folder = computed(() => overview.value?.path ?? 'voxarium_studio/skills')

onMounted(() => {
  store.subscribe()
  void store.load()
})

function fail(cause: unknown): void {
  error.value = cause instanceof Error ? cause.message : String(cause)
}

/** "Igor", "Marina and Igor", "Sofia, Marina and Igor". */
function names(users: SkillUser[]): string {
  const all = users.map((user) => user.name)
  return all.length > 1
    ? t('skills.names', { rest: all.slice(0, -1).join(', '), last: all.at(-1) ?? '' })
    : (all[0] ?? '')
}

// ---------------------------------------------------------------- creating

const creating = ref(false)
const draft = reactive({ name: '', description: '' })
const nameField = ref<HTMLInputElement | null>(null)

/** Nothing to list and nothing to warn about — the page's empty state. */
const nothingHere = computed(
  () =>
    !skills.value.length &&
    !overview.value?.invalid.length &&
    !overview.value?.missing.length &&
    !creating.value,
)

async function startCreate(): Promise<void> {
  Object.assign(draft, { name: '', description: '' })
  creating.value = true
  error.value = null
  // `autofocus` is honoured on a page load, not on a form a click inserted.
  await nextTick()
  nameField.value?.focus()
}

async function create(): Promise<void> {
  const name = draft.name.trim()
  if (!name) {
    error.value = t('skills.create.nameRequired')
    return
  }
  busy.value = '+'
  try {
    store.adopt(await api.createSkill(name, draft.description))
    creating.value = false
    error.value = null
    // Straight into its instructions: a new skill is a heading and a placeholder paragraph.
    await openEditor(name)
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

// ---------------------------------------------------------------- editing SKILL.md

/** The skill whose `SKILL.md` is open, with the text as it was read and as it is being typed. */
const editor = ref<{ name: string; detail: SkillDetail; text: string } | null>(null)
/** Set when a save was refused because the file changed on disk after it was opened. */
const conflict = ref(false)

const editorDirty = computed(
  () => !!editor.value && editor.value.text !== editor.value.detail.content,
)

function discardEdits(): boolean {
  if (!editorDirty.value) return true
  return window.confirm(t('skills.editor.discardConfirm', { name: editor.value?.name ?? '' }))
}

async function openEditor(name: string): Promise<void> {
  if (editor.value?.name === name) return
  if (!discardEdits()) return
  busy.value = name
  try {
    const detail = await api.skill(name)
    editor.value = { name, detail, text: detail.content }
    conflict.value = false
    error.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

function closeEditor(): void {
  if (!discardEdits()) return
  editor.value = null
  conflict.value = false
}

/** `force` is "Keep mine" after a conflict: the write goes through without an expectation. */
async function save(force = false): Promise<void> {
  if (editor.value?.detail.source) return
  const open = editor.value
  if (!open) return
  const name = open.name
  // What is sent, as it was at the click: typing continues while the request travels.
  const sent = open.text
  busy.value = name
  try {
    const detail = await api.saveSkill(name, sent, force ? null : open.detail.updated_at)
    // Adopted only while the editor still shows this skill, and the text only if nothing was
    // typed meanwhile — otherwise the newer typing stays, unsaved, against the new version.
    const current = editor.value
    if (current?.name === name) {
      editor.value = { name, detail, text: current.text === sent ? detail.content : current.text }
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

/** "Reload" after a conflict, and what a clean editor does by itself when the file moves. */
async function reload(): Promise<void> {
  const open = editor.value
  if (!open) return
  try {
    const detail = await api.skill(open.name)
    if (editor.value?.name !== open.name) return
    editor.value = { name: open.name, detail, text: detail.content }
    conflict.value = false
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

/**
 * The open file moved on disk — an agent improving its skill, an editor, a checkout. A clean
 * editor simply takes the new text; one holding edits is told now rather than on its next save.
 */
watch(
  () => (editor.value ? store.byName.get(editor.value.name)?.updated_at : undefined),
  (updatedAt) => {
    const open = editor.value
    if (!open || !updatedAt || busy.value === open.name) return
    // Newer only: a listing read before this editor's own save lands with the older time, and
    // that is not the file moving.
    if (Date.parse(updatedAt) <= Date.parse(open.detail.updated_at)) return
    if (editorDirty.value) conflict.value = true
    else void reload()
  },
)

// A skill deleted from outside while its editor is open has nothing left to save into — nor
// does one whose SKILL.md went, leaving a folder that is not a skill any more.
watch(
  () => store.overview,
  (page) => {
    const open = editor.value
    if (open && page && !page.skills.some((skill) => skill.name === open.name)) {
      editor.value = null
      conflict.value = false
      error.value = t('skills.editor.deletedOnDisk', { name: open.name, folder: page.path })
    }
  },
)

// ---------------------------------------------------------------- deleting

/** What the confirmation names: the paths go with the folder, and so does every member's copy. */
const deleting = ref<{ name: string; used_by: SkillUser[]; files: string[] } | null>(null)

function askDelete(skill: SkillEntry): void {
  deleting.value = {
    name: skill.name,
    used_by: skill.used_by,
    files: [`${skill.path}/SKILL.md`, ...skill.files.map((file) => `${skill.path}/${file}`)],
  }
}

function askDeleteFolder(entry: { name: string; used_by: SkillUser[] }): void {
  deleting.value = {
    name: entry.name,
    used_by: entry.used_by,
    files: [`${folder.value}/${entry.name}/`],
  }
}

const deleteMessage = computed(() => {
  const target = deleting.value
  if (!target) return ''
  return target.used_by.length
    ? t('skills.delete.messageUsed', { names: names(target.used_by) })
    : t('skills.delete.messageUnused')
})

async function confirmDelete(): Promise<void> {
  const target = deleting.value
  if (!target) return
  busy.value = target.name
  try {
    store.adopt(await api.deleteSkill(target.name))
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

/** A skill that is gone from the folder but still on members: only the names come off. */
async function takeOff(entry: { name: string; used_by: SkillUser[] }): Promise<void> {
  const question = t('skills.missing.takeOffConfirm', {
    name: entry.name,
    names: names(entry.used_by),
  })
  if (!window.confirm(question)) return
  busy.value = entry.name
  try {
    store.adopt(await api.deleteSkill(entry.name))
    error.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

// ---------------------------------------------------------------- the folder on disk

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
        <h1 class="text-title font-medium">{{ t('skills.title') }}</h1>
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
        :title="t('skills.openFolderTitle', { folder })"
        @click="reveal(folder)"
      >
        <FolderOpen class="size-3.5" /> {{ t('skills.openFolder') }}
      </button>
    </header>

    <p v-if="error ?? store.error" class="bg-danger-soft px-pane py-1 text-danger">
      {{ error ?? store.error }}
    </p>

    <!-- The introduction and each section are the page's bands (`vxs-enter`, §7). The create
         form and the editor open on a click, and a band waits its turn — so neither has one. -->
    <div class="max-w-3xl space-y-6 p-pane">
      <LoadingBars v-if="store.loading && !overview" />

      <template v-else>
        <p class="vxs-enter text-muted">
          <I18nT keypath="skills.intro">
            <template #file><span class="font-mono">SKILL.md</span></template>
          </I18nT>
        </p>

        <section class="vxs-enter">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="font-medium">{{ t('skills.list.title') }}</h2>
            <button
              class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover"
              @click="startCreate"
            >
              <Plus class="size-3.5" /> {{ t('skills.create.button') }}
            </button>
          </div>

          <form
            v-if="creating"
            class="mb-2 space-y-3 rounded-lg border border-line bg-surface p-3"
            @submit.prevent="create"
          >
            <h3 class="font-medium">{{ t('skills.create.title') }}</h3>
            <label class="flex items-center gap-3">
              <span class="w-24 shrink-0 text-muted">{{ t('skills.create.name') }}</span>
              <input
                ref="nameField"
                v-model="draft.name"
                class="skill-mono flex-1 rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                placeholder="level-prototyping"
                spellcheck="false"
              />
            </label>
            <p class="pl-27 text-small text-faint">
              <I18nT keypath="skills.create.nameHint">
                <template #path>
                  <span class="font-mono">{{ folder }}/{{ draft.name.trim() || 'name' }}/SKILL.md</span>
                </template>
              </I18nT>
            </p>
            <label class="flex items-start gap-3">
              <span class="w-24 shrink-0 pt-1 text-muted">{{ t('skills.create.description') }}</span>
              <textarea
                v-model="draft.description"
                rows="2"
                class="flex-1 rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                :placeholder="t('skills.create.descriptionPlaceholder')"
              />
            </label>
            <p class="pl-27 text-small text-faint">{{ t('skills.create.descriptionHint') }}</p>
            <div class="flex gap-2 pl-27">
              <button
                type="submit"
                class="vxs-btn-primary px-3 py-1 disabled:opacity-50"
                :disabled="busy !== null || !draft.name.trim()"
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
            v-if="skills.length"
            v-model="search"
            type="search"
            :placeholder="t('skills.search')"
            :aria-label="t('skills.search')"
            class="mb-3 w-full rounded-md border border-line bg-inset px-3 py-1.5 outline-none focus:border-accent"
          />
          <p v-if="skills.length && !filteredSkills.length" class="py-3 text-muted">{{ t('skills.noMatches') }}</p>
          <ul class="space-y-2">
            <li
              v-for="skill in filteredSkills"
              :key="skill.name"
              class="rounded-lg border bg-surface"
              :class="editor?.name === skill.name ? 'border-accent-border' : 'border-line'"
            >
              <div class="flex items-start gap-3 p-3">
                <GraduationCap class="mt-0.5 size-5 shrink-0" style="color: var(--vxs-nav-skills)" />

                <div class="min-w-0 flex-1">
                  <p class="truncate font-medium" :title="skill.path">{{ skillDisplayName(skill) }}</p>
                  <p v-if="skill.source" class="text-small text-faint">
                    {{ skill.scope ? t(`skills.scope.${skill.scope}`) : t('skills.scope.builtin') }} · {{ skill.source }}
                    <span v-if="skill.backends?.length"> · {{ skill.backends.join(', ') }}</span>
                    <span v-else-if="skill.scope !== 'builtin' && skill.scope"> · {{ t('skills.allBackends') }}</span>
                  </p>
                  <ClampText v-if="skill.description" class="text-muted" :text="skill.description" />
                  <p v-if="skill.warning" class="mt-0.5 flex items-start gap-1.5 text-small text-warning">
                    <AlertTriangle class="mt-0.5 size-3.5 shrink-0" /> {{ skill.warning }}
                  </p>

                  <p class="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-small text-faint">
                    <span
                      class="font-mono"
                      :title="skill.files.length ? skill.files.join('\n') : t('skills.list.nothingBesides')"
                    >
                      {{ describeFiles(skill) }}
                    </span>
                    <span>·</span>
                    <span v-if="skill.automatic ?? (!!skill.source && !skill.scope)">{{ t('skills.list.availableToAll') }}</span>
                    <template v-else-if="skill.used_by.length">
                      <span>{{ t('skills.list.givenTo') }}</span>
                      <span
                        v-for="user in skill.used_by"
                        :key="user.id"
                        class="inline-flex items-center gap-1 text-muted"
                      >
                        <MemberMark :member="user" />{{ user.name }}
                      </span>
                    </template>
                    <span v-else>{{ t('skills.list.givenToNobody') }}</span>
                  </p>
                </div>

                <div class="flex shrink-0 gap-2">
                  <button
                    class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="busy === skill.name || editor?.name === skill.name"
                    :title="t(skill.source ? 'skills.list.viewTitle' : 'skills.list.editTitle')"
                    @click="openEditor(skill.name)"
                  >
                    <FileText v-if="skill.source" class="size-3.5" />
                    <Pencil v-else class="size-3.5" />
                    {{ t(skill.source ? 'skills.list.view' : 'common.edit') }}
                  </button>
                  <button
                    v-if="!skill.source"
                    class="inline-flex items-center rounded-md border border-line px-2 py-1 hover:bg-hover"
                    :title="t('skills.list.revealTitle')"
                    @click="reveal(skill.path)"
                  >
                    <FolderOpen class="size-3.5" />
                  </button>
                  <button
                    v-if="!skill.source"
                    class="inline-flex items-center rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="busy === skill.name"
                    :title="t('skills.list.deleteTitle')"
                    @click="askDelete(skill)"
                  >
                    <Trash2 class="size-3.5" />
                  </button>
                </div>
              </div>

              <!-- SKILL.md, whole: frontmatter included, because the description in it is the
                   line agents read and hiding it behind a form would hide the format too. -->
              <div v-if="editor?.name === skill.name" class="space-y-2 border-t border-line p-3">
                <p class="flex items-center gap-1.5 text-small text-faint">
                  <FileText class="size-3.5" />
                  <span class="font-mono">{{ skill.path }}/SKILL.md</span>
                  <span v-if="editorDirty" class="text-warning">· {{ t('skills.editor.unsaved') }}</span>
                </p>
                <textarea
                  v-model="editor.text"
                  :readonly="!!skill.source"
                  rows="18"
                  spellcheck="false"
                  class="skill-mono w-full rounded-md border border-line bg-inset p-2 outline-none focus:border-accent"
                  @keydown="onEditorKey"
                />

                <div
                  v-if="conflict"
                  class="flex flex-wrap items-center gap-2 rounded-md bg-warning-soft p-2 text-small text-warning"
                >
                  <AlertTriangle class="size-3.5 shrink-0" />
                  <span class="flex-1">{{ t('skills.editor.changedOnDisk') }}</span>
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
                    {{ t('skills.editor.keepMine') }}
                  </button>
                </div>

                <div class="flex items-center gap-2">
                  <button
                    v-if="!skill.source"
                    class="vxs-btn-primary px-3 py-1 disabled:opacity-50"
                    :disabled="busy !== null || !editorDirty"
                    @click="save()"
                  >
                    {{ t('common.save') }}
                  </button>
                  <button
                    v-if="!skill.source"
                    class="rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="!editorDirty"
                    @click="editor.text = editor.detail.content"
                  >
                    {{ t('skills.editor.revert') }}
                  </button>
                  <button class="rounded-md border border-line px-3 py-1 hover:bg-hover" @click="closeEditor">
                    {{ t('common.close') }}
                  </button>
                  <span v-if="!skill.source" class="text-small text-faint">{{ t('skills.editor.shortcutHint') }}</span>
                </div>
              </div>
            </li>
          </ul>

          <EmptyState
            v-if="nothingHere"
            :title="t('skills.list.emptyTitle')"
            :hint="t('skills.list.emptyHint', { folder })"
          >
            <template #icon><GraduationCap class="mx-auto mb-3 size-8 text-faint" /></template>
          </EmptyState>

          <!-- Folders the Studio cannot offer anyone, named rather than hidden: without this a
               skill copied in without its SKILL.md would simply not be in the member's list. -->
          <ul v-if="overview?.invalid.length" class="mt-2 space-y-1">
            <li
              v-for="entry in overview.invalid"
              :key="entry.name"
              class="flex items-center gap-2 rounded-md border border-line bg-inset p-2 text-small"
            >
              <span class="min-w-0 flex-1">
                <span class="font-medium">{{ entry.name }}</span>
                <span class="text-warning"> — {{ entry.error }}</span>
                <span v-if="entry.used_by.length" class="text-faint">
                  ·
                  {{
                    t('skills.invalid.givenTo', {
                      count: entry.used_by.length,
                      names: names(entry.used_by),
                    })
                  }}
                </span>
              </span>
              <button
                class="inline-flex shrink-0 items-center rounded-md border border-line px-2 py-0.5 hover:bg-hover"
                :title="t('skills.invalid.revealTitle')"
                @click="reveal(`${folder}/${entry.name}`)"
              >
                <FolderOpen class="size-3.5" />
              </button>
              <button
                class="inline-flex shrink-0 items-center rounded-md border border-line px-2 py-0.5 hover:bg-hover disabled:opacity-50"
                :disabled="busy === entry.name"
                :title="t('skills.invalid.deleteTitle')"
                @click="askDeleteFolder(entry)"
              >
                <Trash2 class="size-3.5" />
              </button>
            </li>
          </ul>

          <!-- Given to somebody, gone from the folder — renamed or deleted outside the Studio. The
               members' lists still hold the name, and nothing else on this page would say so. -->
          <ul v-if="overview?.missing.length" class="mt-2 space-y-1">
            <li
              v-for="entry in overview.missing"
              :key="entry.name"
              class="flex items-center gap-2 rounded-md border border-line bg-inset p-2 text-small"
            >
              <AlertTriangle class="size-3.5 shrink-0 text-warning" />
              <span class="min-w-0 flex-1">
                <span class="font-medium">{{ entry.name }}</span>
                <span class="text-warning">
                  — {{ t('skills.missing.notInFolder', { folder }) }}
                </span>
                <span class="text-faint">
                  ·
                  {{
                    t('skills.missing.stillGivenTo', {
                      count: entry.used_by.length,
                      names: names(entry.used_by),
                    })
                  }}
                </span>
              </span>
              <button
                class="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-line px-2 py-0.5 hover:bg-hover disabled:opacity-50"
                :disabled="busy === entry.name"
                :title="t('skills.missing.takeOffTitle', { name: entry.name })"
                @click="takeOff(entry)"
              >
                <UserMinus class="size-3.5" /> {{ t('skills.missing.takeOff') }}
              </button>
            </li>
          </ul>
        </section>

        <section class="vxs-enter">
          <h2 class="mb-2 font-medium">{{ t('skills.usage.title') }}</h2>
          <div class="space-y-2 rounded-lg border border-line bg-surface p-3 text-muted">
            <p>
              <I18nT keypath="skills.usage.body">
                <template #file><span class="font-mono">SKILL.md</span></template>
              </I18nT>
            </p>
            <p class="text-small text-faint">
              <I18nT
                keypath="skills.usage.note"
                :params="{ settings: t('teams.settings.title'), section: t('agentPrompts.title') }"
              >
                <template #dir><span class="font-mono">.claude/skills/</span></template>
                <template #fragment>
                  <span class="font-medium">{{ t('agentPrompts.fragments.member_skills.label') }}</span>
                </template>
              </I18nT>
            </p>
          </div>
        </section>
      </template>
    </div>

    <ConfirmDialog
      v-if="deleting"
      :title="t('skills.delete.title', { name: deleting.name })"
      :message="deleteMessage"
      :items="deleting.files"
      :confirm-label="t('common.delete')"
      danger
      :busy="busy !== null"
      @confirm="confirmDelete"
      @cancel="deleting = null"
    />
  </div>
</template>

<style scoped>
/* `font-mono` and a `text-[…]` size do not reach a field here: `base.css` gives `input`,
   `textarea` and `select` an unlayered `font: inherit`, which outranks every utility. A scoped
   class is unlayered too, and more specific than the element. */
.skill-mono {
  font-family: var(--vxs-font-mono);
}

textarea.skill-mono {
  font-size: 12px;
}
</style>
