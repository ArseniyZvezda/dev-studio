<script setup lang="ts">
/**
 * Choosing which managed project the Studio is pointed at (§5).
 *
 * Three ways in, in order of how well they work: the machine's own folder dialog, the
 * recently-opened list, and an in-app folder browser. The native dialog is the one people
 * mean by "open a project", but the page cannot raise it itself — the File System Access
 * API returns an opaque handle and never a path — so the backend opens it and this waits
 * on the answer. Anywhere that dialog cannot appear (a Studio bound to the network, a
 * machine with no GUI toolkit) the browser below is the whole feature, not a courtesy.
 *
 * A folder that is not a project yet is initialised on open. That is asked about rather
 * than done silently: the same click in the wrong row would otherwise scatter `.voxarium_studio/`
 * directories through someone's drive.
 */
import {
  AlertTriangle,
  ChevronRight,
  CornerLeftUp,
  FolderOpen,
  FolderSearch,
  HardDrive,
  Loader2,
  Plus,
  X,
} from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { DirectoryListing, PathInspection, PickerStatus } from '@/api/types'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { useSessionStore } from '@/stores/session'
import ProjectTaskCounts from './ProjectTaskCounts.vue'

const emit = defineEmits<{ close: [] }>()

const session = useSessionStore()
let refreshTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => { refreshTimer = setInterval(() => void session.refreshRecent(), 15_000) })
onBeforeUnmount(() => clearInterval(refreshTimer))


const picker = ref<PickerStatus | null>(null)
const listing = ref<DirectoryListing | null>(null)
const selected = ref<PathInspection | null>(null)
const pathDraft = ref('')
const showHidden = ref(false)

const waitingForDialog = ref(false)
const browsing = ref(false)
const error = ref<string | null>(null)

const visibleEntries = computed(() =>
  (listing.value?.entries ?? []).filter((entry) => showHidden.value || !entry.hidden),
)

const isCurrentProject = computed(
  () => !!selected.value && selected.value.path === session.project?.root,
)

/** The one thing the confirm button has to say clearly: open, or create then open. */
const willInitialise = computed(
  () => !!selected.value && selected.value.is_dir && !selected.value.is_project,
)

const canConfirm = computed(() => !!selected.value?.is_dir && !session.switching && !browsing.value && !waitingForDialog.value)

onMounted(async () => {
  void session.refreshRecent()
  try {
    picker.value = await api.pickerStatus()
  } catch {
    /* Falling back to the in-app browser is exactly what a failure here means. */
  }
  pathDraft.value = session.project?.root ?? ''
})

async function navigate(path: string) {
  browsing.value = true
  error.value = null
  try {
    listing.value = await api.browseDirectory(path)
    // Browsing *is* selecting: the folder you are looking at is the one Open acts on,
    // which is what makes an empty new folder selectable at all.
    selected.value = path
      ? {
          path: listing.value.path,
          name: listing.value.name,
          exists: true,
          is_dir: true,
          is_project: listing.value.is_project,
        }
      : null
    pathDraft.value = listing.value.path
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    browsing.value = false
  }
}

/** Ask the backend to raise the machine's folder dialog and wait for the user's answer. */
async function pickNatively() {
  waitingForDialog.value = true
  error.value = null
  try {
    const result = await api.pickDirectory(selected.value?.path ?? undefined)
    if (!result.available) {
      error.value = result.reason || t('project.open.dialogUnavailable')
      return
    }
    if (result.cancelled || !result.path || !result.inspection) return
    selected.value = result.inspection
    pathDraft.value = result.inspection.path
    // Already a project: they picked it in a file dialog, they meant it. A folder that
    // still has to be initialised drops through to the confirmation instead.
    if (result.inspection.is_project) await confirm()
    else await navigate(result.inspection.path)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    waitingForDialog.value = false
  }
}

/** Resolve whatever was typed or pasted into the path box. */
async function applyDraft() {
  const path = pathDraft.value.trim()
  if (!path) return
  error.value = null
  try {
    const inspection = await api.inspectPath(path)
    selected.value = inspection
    if (inspection.is_dir) await navigate(inspection.path)
    else error.value = t('project.open.notAFolder', { path: inspection.path })
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function confirm() {
  const target = selected.value
  if (!target?.is_dir) return
  if (target.path === session.project?.root) {
    emit('close')
    return
  }
  error.value = null
  try {
    // The page reloads on success, so there is no "done" state to render here.
    await session.openProject(target.path, true)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function openRecent(path: string) {
  try {
    selected.value = await api.inspectPath(path)
    await confirm()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}
</script>

<template>
  <div class="flex max-h-[min(640px,calc(100dvh-70px))] w-[420px] max-w-[calc(100vw-24px)] flex-col">
        <header class="flex items-center gap-2 border-b border-line px-4 py-3">
          <FolderOpen class="size-4 text-accent" />
          <h2 class="flex-1 font-medium">{{ t('project.open.title') }}</h2>
          <button
            class="rounded-md p-1 text-muted hover:bg-hover hover:text-primary disabled:opacity-40"
            :disabled="waitingForDialog || session.switching"
            :title="t('common.close')"
            @click="emit('close')"
          >
            <X class="size-4" />
          </button>
        </header>

        <div class="min-h-0 flex-1 overflow-y-auto p-4">
          <p v-if="error" class="mb-3 flex items-start gap-2 rounded-md bg-inset p-2 text-danger">
            <AlertTriangle class="mt-0.5 size-4 shrink-0" />
            <span class="break-all">{{ error }}</span>
          </p>

          <!-- The native dialog, and what to do when it cannot be offered. -->
          <div class="mb-4 flex items-center gap-3">
            <button
              v-if="picker?.available"
              class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5 font-medium disabled:opacity-60"
              :disabled="waitingForDialog || session.switching"
              @click="pickNatively"
            >
              <Loader2 v-if="waitingForDialog" class="size-4 animate-spin" />
              <FolderSearch v-else class="size-4" />
              {{
                waitingForDialog
                  ? t('project.open.waitingForDialog')
                  : t('project.open.browseComputer')
              }}
            </button>
            <p v-else-if="picker" class="text-small text-faint">
              {{ t('project.open.pickerUnavailable', { reason: picker.reason }) }}
            </p>
            <p v-if="waitingForDialog" class="text-small text-faint">
              {{ t('project.open.dialogBehind') }}
            </p>
          </div>

          <!-- Recently opened -->
          <section v-if="session.recent.length" class="mb-4 max-h-60 overflow-y-auto">
            <h3 class="mb-1 text-small uppercase tracking-wide text-faint">
              {{ t('project.open.recent') }}
            </h3>
            <ul class="divide-y divide-line-subtle rounded-md border border-line">
              <li
                v-for="entry in session.recent"
                :key="entry.root"
                class="flex items-center gap-2 px-2 py-1.5"
                :class="entry.exists ? '' : 'opacity-50'"
              >
                <button
                  class="flex min-w-0 flex-1 items-center gap-2 text-left disabled:cursor-not-allowed"
                  :disabled="!entry.exists || session.switching"
                  :title="entry.root"
                  @click="openRecent(entry.root)"
                >
                  <span class="min-w-0 flex-1">
                    <span class="block truncate font-medium">{{ entry.name }}</span>
                    <span class="block truncate font-mono text-small text-faint">{{ entry.root }}</span>
                    <ProjectTaskCounts
                      class="mt-1"
                      :counts="entry.root === session.project?.root ? session.taskCounts : entry.task_counts"
                    />
                  </span>
                  <span v-if="!entry.exists" class="shrink-0 text-small text-danger">{{
                    t('project.open.missing')
                  }}</span>
                  <span
                    v-else-if="entry.root === session.project?.root"
                    class="shrink-0 text-small text-accent"
                    >{{ t('project.open.current') }}</span
                  >
                </button>
                <button
                  class="shrink-0 rounded p-1 text-faint hover:bg-hover hover:text-primary"
                  :title="t('project.open.forget')"
                  @click="session.forgetRecent(entry.root)"
                >
                  <X class="size-3.5" />
                </button>
              </li>
            </ul>
          </section>

          <!-- In-app folder browser -->
          <details @toggle="(event) => { if ((event.target as HTMLDetailsElement).open && !listing) navigate(session.project?.root ?? '') }">
            <summary class="mb-2 cursor-pointer text-muted">{{ t('project.open.browse') }}</summary>
            <div class="mb-1 flex items-center gap-2">
              <h3 class="flex-1 text-small uppercase tracking-wide text-faint">
                {{ t('project.open.browse') }}
              </h3>
              <label class="flex cursor-pointer items-center gap-1.5 text-small text-faint">
                <input v-model="showHidden" type="checkbox" class="accent-[var(--vxs-accent)]" />
                {{ t('project.open.showHidden') }}
              </label>
            </div>

            <div class="rounded-md border border-line">
              <div class="flex items-center gap-2 border-b border-line-subtle px-2 py-1.5">
                <button
                  class="rounded p-1 text-muted hover:bg-hover hover:text-primary disabled:opacity-30"
                  :disabled="listing?.parent === null"
                  :title="t('project.open.up')"
                  @click="navigate(listing?.parent ?? '')"
                >
                  <CornerLeftUp class="size-4" />
                </button>
                <span class="truncate font-mono text-small text-muted" :title="listing?.path">
                  {{ listing?.path || listing?.name }}
                </span>
                <Loader2 v-if="browsing" class="size-3.5 animate-spin text-faint" />
              </div>

              <ul class="max-h-40 overflow-y-auto">
                <li v-for="entry in visibleEntries" :key="entry.path">
                  <button
                    class="vxs-row flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-hover"
                    @click="navigate(entry.path)"
                  >
                    <HardDrive v-if="listing?.parent === null" class="size-4 shrink-0 text-faint" />
                    <FolderOpen v-else class="size-4 shrink-0 text-faint" />
                    <span class="truncate" :class="entry.hidden ? 'text-muted' : ''">
                      {{ entry.name }}
                    </span>
                    <span v-if="entry.is_project" class="shrink-0 text-small text-accent">
                      {{ t('project.open.projectTag') }}
                    </span>
                    <ChevronRight class="ml-auto size-3.5 shrink-0 text-faint" />
                  </button>
                </li>
                <li v-if="!browsing && !visibleEntries.length" class="px-2 py-3 text-center text-faint">
                  {{ t('project.open.noSubfolders') }}
                </li>
              </ul>

              <p
                v-if="listing?.truncated"
                class="border-t border-line-subtle px-2 py-1 text-small text-faint"
              >
                {{ t('project.open.truncated') }}
              </p>
            </div>
          </details>

          <label class="mt-3 block">
            <span class="mb-1 block text-small uppercase tracking-wide text-faint">
              {{ t('project.open.folder') }}
            </span>
            <input
              v-model="pathDraft"
              spellcheck="false"
              placeholder="D:\Games\MyProject"
              class="w-full rounded-md border border-line bg-inset px-3 py-2 font-mono outline-none focus:border-accent"
              @keydown.enter.prevent="applyDraft"
              @change="applyDraft"
            />
          </label>
        </div>

        <footer class="flex items-center gap-3 border-t border-line px-4 py-3">
          <p class="min-w-0 flex-1 text-small text-faint">
            <span v-if="isCurrentProject">{{ t('project.open.alreadyOpen') }}</span>
            <span v-else-if="willInitialise" class="flex items-center gap-1.5 text-warning">
              <Plus class="size-3.5 shrink-0" />
              <span class="break-all">
                <I18nT keypath="project.open.willInitialise" :params="{ name: selected?.name ?? '' }">
                  <template #dir><code class="font-mono">.voxarium_studio/</code></template>
                </I18nT>
              </span>
            </span>
            <span v-else-if="selected?.is_project" class="break-all">
              {{ t('project.open.opensExisting', { name: selected?.name ?? '' }) }}
            </span>
          </p>
          <button
            class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-40"
            :disabled="waitingForDialog || session.switching"
            @click="emit('close')"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5 font-medium disabled:opacity-50"
            :disabled="!canConfirm"
            @click="confirm"
          >
            <Loader2 v-if="session.switching" class="size-4 animate-spin" />
            {{ willInitialise ? t('project.open.createAndOpen') : t('common.open') }}
          </button>
        </footer>
  </div>
</template>
