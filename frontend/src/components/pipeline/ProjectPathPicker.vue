<script setup lang="ts">
/**
 * "Which file, or which folder?" — the picker behind every `file` / `directory` / `path`
 * port in the node config panel.
 *
 * It browses `/api/explorer/tree` one directory at a time, which is the same feed the
 * Explorer's tree runs on and the same containment rules: every path it can produce is
 * project-relative and inside the project, because that is the only thing the endpoint will
 * describe. So a path chosen here cannot be one `_resolve_under_project` will refuse — the
 * boundary is enforced on the backend either way, but the user never meets it.
 *
 * Not the Explorer's own `FileTreeNode`: that is bound to the explorer store's expansion,
 * selection and version-control state, none of which a modal wants to share or disturb.
 * One directory at a time is also the right shape for a dialog — a tree deep enough to need
 * scrolling in a 26rem box is a tree nobody can read.
 */
import { ChevronRight, CornerLeftUp, File as FileIcon, Folder } from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { FileEntry } from '@/api/types'
import { t } from '@/i18n'
import { useLeave } from '@/lib/leave'

const props = defineProps<{
  /** What may be *chosen*. `path` accepts either, which is what `core.path` is for. */
  mode: 'file' | 'directory' | 'path'
  /** Where to open, and what the field currently holds. */
  initial: string
  /** Extension hints from the port. A hint, never a gate — see `preview3d`'s own note. */
  accept?: string[]
}>()

const emit = defineEmits<{ submit: [value: string]; cancel: [] }>()

const overlay = ref<HTMLElement | null>(null)
const root = ref<HTMLElement | null>(null)
const cwd = ref('')

useLeave(() => overlay.value)
const entries = ref<FileEntry[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

/**
 * The answer, and the only state that is one.
 *
 * Everything else — the highlighted row, the button's wording — is derived from it, so the
 * field and the list cannot disagree about what Select would return. Pasting a path in is
 * then the same gesture as clicking a row, which is what makes the field worth having.
 */
const chosen = ref(props.initial.trim())

const allowsFiles = computed(() => props.mode !== 'directory')
const allowsDirectories = computed(() => props.mode !== 'file')

/** The listed row that `chosen` names, if it is one — the highlight, and nothing more. */
const selected = computed(() => entries.value.find((entry) => entry.path === chosen.value) ?? null)

/** Breadcrumb segments, root first, each carrying the path that navigates to it. */
const trail = computed(() => {
  const crumbs = [{ name: t('pipelines.pathPicker.projectRoot'), path: '' }]
  let walked = ''
  for (const segment of cwd.value.split('/').filter(Boolean)) {
    walked = walked ? `${walked}/${segment}` : segment
    crumbs.push({ name: segment, path: walked })
  }
  return crumbs
})

/** Dimmed, not hidden: `accept` is a hint about the usual case, not a rule about this one. */
function matchesAccept(entry: FileEntry): boolean {
  if (entry.is_dir || !props.accept?.length) return true
  const name = entry.name.toLowerCase()
  return props.accept.some((suffix) => name.endsWith(suffix.toLowerCase()))
}

function selectable(entry: FileEntry): boolean {
  return entry.is_dir ? allowsDirectories.value : allowsFiles.value
}

/** The project root has no path to hand back, so it is somewhere to browse, not to pick. */
const canConfirm = computed(() => chosen.value.trim().length > 0)

const confirmLabel = computed(() =>
  chosen.value && chosen.value === cwd.value
    ? t('pipelines.pathPicker.selectFolder')
    : t('pipelines.pathPicker.select'),
)

/** List one directory. Returns whether it worked, which is what the fallback walk needs. */
async function open(path: string): Promise<boolean> {
  loading.value = true
  error.value = null
  try {
    const tree = await api.tree(path, 1)
    cwd.value = tree.path
    entries.value = tree.entries
    // Navigating *is* choosing, when a folder is a legal answer: the folder you went into
    // is the one you were heading for, and it never appears as a row inside itself.
    if (allowsDirectories.value && tree.path) chosen.value = tree.path
    return true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    return false
  } finally {
    loading.value = false
  }
}

function choose(entry: FileEntry): void {
  // A folder in file-only mode is a place to go, not a thing to pick.
  if (!selectable(entry)) {
    if (entry.is_dir) void open(entry.path)
    return
  }
  chosen.value = entry.path
}

function activate(entry: FileEntry): void {
  if (entry.is_dir) void open(entry.path)
  else if (allowsFiles.value) emit('submit', entry.path)
}

function goUp(): void {
  void open(cwd.value.split('/').slice(0, -1).join('/'))
}

function submit(): void {
  if (canConfirm.value) emit('submit', chosen.value.trim())
}

onMounted(async () => {
  // Open beside whatever the field already holds — its own folder for a file, itself for a
  // folder — and walk up until something lists. A path that has since been deleted then
  // leaves the dialog somewhere usable instead of empty with no way back.
  const start = props.initial.trim().replace(/\\/g, '/').replace(/\/+$/, '')
  const wanted = chosen.value
  let candidate = start
  while (!(await open(candidate)) && candidate) {
    candidate = candidate.split('/').slice(0, -1).join('/')
  }
  // `open` claims the folder it landed on; the path that was already in the field is the
  // better answer until the user says otherwise.
  if (wanted) chosen.value = wanted
  await nextTick()
  root.value?.focus()
})
</script>

<template>
  <Teleport to="body">
    <div
      ref="overlay"
      class="vxs-veil fixed inset-0 z-50 grid place-items-center bg-canvas/70 p-6"
      @click.self="emit('cancel')"
    >
      <div
        ref="root"
        class="flex max-h-[36rem] w-[32rem] max-w-full flex-col rounded-lg border border-line bg-surface shadow-lg outline-none"
        role="dialog"
        aria-modal="true"
        :aria-label="t('pipelines.pathPicker.label')"
        tabindex="-1"
        @keydown.esc.prevent="emit('cancel')"
      >
        <header class="border-b border-line px-4 py-3">
          <h2 class="font-medium">
            {{
              mode === 'directory'
                ? t('pipelines.pathPicker.title.directory')
                : mode === 'file'
                  ? t('pipelines.pathPicker.title.file')
                  : t('pipelines.pathPicker.title.path')
            }}
          </h2>
          <nav class="mt-1 flex flex-wrap items-center gap-0.5 text-small text-faint">
            <template v-for="(crumb, index) in trail" :key="crumb.path">
              <ChevronRight v-if="index" class="size-3 shrink-0" />
              <button
                type="button"
                class="max-w-40 truncate rounded-sm px-1 hover:bg-hover"
                :class="index === trail.length - 1 && 'text-primary'"
                @click="open(crumb.path)"
              >
                {{ crumb.name }}
              </button>
            </template>
          </nav>
        </header>

        <p v-if="error" class="bg-danger-soft px-4 py-1 text-small text-danger">{{ error }}</p>

        <ul class="min-h-40 flex-1 overflow-auto p-1">
          <li v-if="cwd">
            <button
              type="button"
              class="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left hover:bg-hover"
              @click="goUp"
            >
              <CornerLeftUp class="size-3.5 shrink-0 text-faint" />
              <span class="text-muted">..</span>
            </button>
          </li>
          <!--
            Two controls per folder row, because one row means two different things: the
            folder as an *answer*, and the folder as a *place*. A single button that did
            both by click count leaves the second one undiscoverable, and a chevron that
            only selects is worse than no chevron.
          -->
          <li
            v-for="entry in entries"
            :key="entry.path"
            class="flex items-center rounded-sm"
            :class="selected?.path === entry.path ? 'bg-accent-soft' : 'hover:bg-hover'"
          >
            <button
              type="button"
              class="flex min-w-0 flex-1 items-center gap-2 px-2 py-1 text-left"
              :class="[
                selected?.path === entry.path ? 'text-primary' : '',
                matchesAccept(entry) ? '' : 'text-faint',
                selectable(entry) ? '' : 'italic',
              ]"
              :title="entry.path"
              @click="choose(entry)"
              @dblclick="activate(entry)"
            >
              <Folder v-if="entry.is_dir" class="size-3.5 shrink-0 text-faint" />
              <FileIcon v-else class="size-3.5 shrink-0 text-faint" />
              <span class="truncate">{{ entry.name }}</span>
            </button>
            <button
              v-if="entry.is_dir"
              type="button"
              class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
              :title="t('pipelines.pathPicker.openFolder', { name: entry.name })"
              @click="open(entry.path)"
            >
              <ChevronRight class="size-3.5" />
            </button>
          </li>
          <li v-if="!loading && !entries.length" class="px-2 py-3 text-small text-faint">
            {{ t('pipelines.pathPicker.emptyFolder') }}
          </li>
        </ul>

        <div class="border-t border-line px-4 py-2">
          <label class="block">
            <span class="mb-1 block text-small text-faint">{{ t('pipelines.pathPicker.path') }}</span>
            <input
              v-model="chosen"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
              :placeholder="t('pipelines.pathPicker.pathPlaceholder')"
              @keydown.enter.prevent="submit"
            />
          </label>
        </div>

        <footer class="flex justify-end gap-2 border-t border-line px-4 py-3">
          <button
            type="button"
            class="rounded-md border border-line px-3 py-1 hover:bg-hover"
            @click="emit('cancel')"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="vxs-btn-primary px-3 py-1 font-medium text-on-accent disabled:opacity-50"
            :disabled="!canConfirm"
            @click="submit"
          >
            {{ confirmLabel }}
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>
