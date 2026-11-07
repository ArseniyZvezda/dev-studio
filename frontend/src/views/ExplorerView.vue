<script setup lang="ts">
/**
 * The project file browser (spec §6.2).
 *
 * The page owns everything that is *one per tree* rather than one per row: the context
 * menu, the name prompt, the two confirmations, and the keyboard shortcuts. `FileTreeNode`
 * recurses, so anything mounted inside it would exist once per file in the project.
 *
 * Operations never re-read the tree. They go to `/api/explorer/*`, the file watcher sees
 * what changed, and `explorer.file_changed` re-reads only the affected directories — the
 * same path an edit made outside the Studio takes. See `stores/explorer.ts` for the three
 * things that do have to be done locally, all of which are about state no event carries.
 */
import { FilePlus2, FolderPlus, RefreshCw, X } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ConfirmDialog from '@/components/explorer/ConfirmDialog.vue'
import EntryPrompt from '@/components/explorer/EntryPrompt.vue'
import FileContextMenu, { type ExplorerAction } from '@/components/explorer/FileContextMenu.vue'
import FilePreview from '@/components/explorer/FilePreview.vue'
import FileTreeNode from '@/components/explorer/FileTreeNode.vue'
import SidePane from '@/components/layout/SidePane.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import type { EntryKind, FileEntry } from '@/api/types'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { basename, useExplorerStore } from '@/stores/explorer'
import { useSessionStore } from '@/stores/session'
import { useVcsStore } from '@/stores/vcs'

const explorer = useExplorerStore()
const session = useSessionStore()
const vcs = useVcsStore()
const route = useRoute()
const router = useRouter()

type Prompt =
  | { mode: 'create'; entryKind: EntryKind; parent: string }
  | { mode: 'rename'; path: string }

const tree = ref<HTMLElement | null>(null)
const menu = ref<{ at: { x: number; y: number }; entry: FileEntry | null } | null>(null)
const prompt = ref<Prompt | null>(null)
const deleting = ref<string[] | null>(null)

const rootEntries = computed(() => explorer.children[''] ?? [])

onMounted(async () => {
  // §9.6: the same debounced `explorer.file_changed` that refreshes the tree refreshes the
  // status, so the colours follow a write without anything asking them to.
  vcs.subscribe()
  void vcs.refresh()
  await explorer.init()
  await revealFromRoute()
})

// A search hit (§6.1) lands here as `/explorer?path=…`, from any page — including this one.
watch(
  () => route.query.path,
  () => void revealFromRoute(),
)

/**
 * Open the file the URL names, then drop the parameter.
 *
 * `reveal` expands the folders above it and loads the ones the tree has not read yet;
 * `selectPath` is what fetches the preview. The parameter is removed once it has done its
 * job, so a reload shows the tree as it was left rather than jumping again.
 */
async function revealFromRoute(): Promise<void> {
  const wanted = route.query.path
  const path = typeof wanted === 'string' ? wanted : ''
  if (!path) return
  await explorer.reveal([path])
  await explorer.selectPath(path)
  await router.replace({ path: route.path })
}

// ------------------------------------------------------------------ the menu

/**
 * What an action applies to: the whole selection when the clicked row is part of it, and
 * that row alone when it is not — which is what every file manager does, and what stops a
 * right-click somewhere else from deleting the files that happened to still be selected.
 */
function targets(entry: FileEntry | null): string[] {
  if (!entry) return [...explorer.selectedPaths]
  return explorer.isSelected(entry.path) ? [...explorer.selectedPaths] : [entry.path]
}

const menuCount = computed(() => targets(menu.value?.entry ?? null).length)

async function openMenu(entry: FileEntry | null, event: MouseEvent): Promise<void> {
  // Right-clicking outside the selection moves it, so the menu's subject is what is
  // highlighted. Selecting a folder never expands or collapses it.
  if (entry && !explorer.isSelected(entry.path)) await explorer.select(entry, 'replace')
  menu.value = { at: { x: event.clientX, y: event.clientY }, entry }
}

function closeMenu(): void {
  menu.value = null
  focusTree()
}

async function run(action: ExplorerAction): Promise<void> {
  const opened = menu.value
  const entry = opened?.entry ?? null
  const paths = targets(entry)
  // Where a creation or a paste lands: the clicked row's folder, the project root when the
  // empty area below the tree was clicked, and the selected folder when the header buttons
  // asked — which is the same menu without a pointer to place it.
  const here = opened
    ? explorer.directoryFor(entry?.path ?? null)
    : explorer.directoryFor(explorer.selectedPath)
  menu.value = null

  switch (action) {
    case 'new-file':
      prompt.value = { mode: 'create', entryKind: 'file', parent: here }
      return
    case 'new-folder':
      prompt.value = { mode: 'create', entryKind: 'directory', parent: here }
      return
    case 'rename':
      if (paths.length === 1) prompt.value = { mode: 'rename', path: paths[0] }
      return
    case 'delete':
      if (paths.length) deleting.value = paths
      return
    case 'duplicate':
      await explorer.duplicate(paths)
      break
    case 'cut':
      explorer.cut(paths)
      break
    case 'copy':
      explorer.copy(paths)
      break
    case 'paste':
      await explorer.paste(here)
      break
    case 'copy-path':
      await writeClipboard(paths, true)
      break
    case 'copy-relative-path':
      await writeClipboard(paths, false)
      break
    case 'open-in-system':
      // The empty string is the project root, which is what the menu means with no entry.
      await explorer.openInSystemFileManager(entry ? paths[0] : '')
      break
  }
  focusTree()
}

/**
 * The path as the machine spells it, or as the project does.
 *
 * The tree speaks in forward slashes whatever the platform; an absolute path is meant to
 * be pasted into a terminal or another tool, so it takes the separator the project root
 * already uses.
 */
async function writeClipboard(paths: string[], absolute: boolean): Promise<void> {
  if (!paths.length) return
  const root = (session.project?.root ?? '').replace(/[\\/]+$/, '')
  const separator = root.includes('\\') ? '\\' : '/'
  const text = paths
    .map((path) =>
      absolute && root ? `${root}${separator}${path.split('/').join(separator)}` : path,
    )
    .join('\n')
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // Clipboard access needs a secure context; over plain HTTP to another machine it is
    // simply not available, and saying so beats a button that does nothing.
    explorer.error = t('explorer.view.clipboardDenied')
  }
}

// ------------------------------------------------------------------ the keyboard

function focusTree(): void {
  tree.value?.focus()
}

async function onKeydown(event: KeyboardEvent): Promise<void> {
  if (prompt.value || deleting.value || explorer.conflict) return
  const paths = [...explorer.selectedPaths]
  const chord = event.ctrlKey || event.metaKey

  if (event.key === 'F2' && paths.length === 1) {
    event.preventDefault()
    prompt.value = { mode: 'rename', path: paths[0] }
    return
  }
  if (event.key === 'Delete' && paths.length) {
    event.preventDefault()
    deleting.value = paths
    return
  }
  if (event.key === 'Escape') {
    explorer.clearClipboard()
    return
  }
  if (!chord) return

  switch (event.key.toLowerCase()) {
    case 'a':
      event.preventDefault()
      explorer.selectAllVisible()
      break
    case 'x':
      event.preventDefault()
      explorer.cut(paths)
      break
    case 'c':
      event.preventDefault()
      explorer.copy(paths)
      break
    case 'v':
      event.preventDefault()
      await explorer.paste(explorer.directoryFor(explorer.selectedPath))
      break
  }
}

// ------------------------------------------------------------------ the dialogs

const promptTitle = computed(() => {
  const pending = prompt.value
  if (!pending) return ''
  if (pending.mode === 'rename') return t('common.rename')
  return pending.entryKind === 'directory'
    ? t('explorer.prompt.newFolder')
    : t('explorer.prompt.newFile')
})

const promptHint = computed(() => {
  const pending = prompt.value
  if (!pending) return ''
  if (pending.mode === 'rename') return pending.path
  const folder = pending.parent || session.project?.name
  return folder === undefined
    ? t('explorer.prompt.inProjectRoot')
    : t('explorer.prompt.inFolder', { folder })
})

const promptInitial = computed(() =>
  prompt.value?.mode === 'rename' ? basename(prompt.value.path) : '',
)

/** A fresh dialog per subject, so its field re-seeds rather than keeping the last name. */
const promptKey = computed(() => {
  const pending = prompt.value
  if (!pending) return ''
  return pending.mode === 'rename' ? `rename:${pending.path}` : `create:${pending.entryKind}:${pending.parent}`
})

async function submitPrompt(value: string): Promise<void> {
  const pending = prompt.value
  if (!pending) return

  if (pending.mode === 'rename') {
    if (value === basename(pending.path)) {
      prompt.value = null
      focusTree()
      return
    }
    if (!(await explorer.renameEntry(pending.path, value))) return
  } else if (!(await explorer.createEntry(pending.parent, value, pending.entryKind))) {
    // Left open with the message beneath the field: a refused name is answered by editing
    // it, not by starting again.
    return
  }
  prompt.value = null
  focusTree()
}

function cancelPrompt(): void {
  prompt.value = null
  explorer.error = null
  focusTree()
}

const deleteMessage = computed(() => {
  const paths = deleting.value ?? []
  if (paths.length !== 1) {
    return t('explorer.confirmDelete.items', { count: paths.length })
  }
  return explorer.entryFor(paths[0])?.is_dir
    ? t('explorer.confirmDelete.folder')
    : t('explorer.confirmDelete.file')
})

async function confirmDelete(): Promise<void> {
  const paths = deleting.value
  if (!paths) return
  if (await explorer.remove(paths)) deleting.value = null
  focusTree()
}

const conflictMessage = computed(() => {
  const pending = explorer.conflict
  if (!pending) return ''
  const folder = pending.destination || session.project?.name
  return folder === undefined
    ? t('explorer.conflict.replaceInRoot', { detail: pending.detail })
    : t('explorer.conflict.replaceIn', { detail: pending.detail, folder })
})

async function confirmOverwrite(): Promise<void> {
  await explorer.resolveConflict()
  focusTree()
}
</script>

<template>
  <div class="flex h-full">
    <SidePane
      :title="t('explorer.view.title')"
      storage-key="voxarium_studio.explorer.pane-width"
      :default-width="288"
    >
      <template #actions>
        <button
          class="grid size-6 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('explorer.view.newFileHere')"
          @click="run('new-file')"
        >
          <FilePlus2 class="size-3.5" />
        </button>
        <button
          class="grid size-6 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('explorer.view.newFolderHere')"
          @click="run('new-folder')"
        >
          <FolderPlus class="size-3.5" />
        </button>
        <button
          class="grid size-6 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('common.refresh')"
          @click="explorer.loadDirectory('')"
        >
          <RefreshCw class="size-3.5" />
        </button>
      </template>

      <template #top>
        <p
          v-if="explorer.error"
          class="flex items-start gap-2 bg-danger-soft px-3 py-1.5 text-danger"
        >
          <span class="min-w-0 flex-1 break-all">{{ explorer.error }}</span>
          <button class="shrink-0" :title="t('explorer.view.dismiss')" @click="explorer.error = null">
            <X class="size-3.5" />
          </button>
        </p>
      </template>

      <!-- The scroller stays here rather than inside `SidePane`, and that is not tidiness:
           it carries the tree's `ref` (every dialog gives focus back to it), its `tabindex`,
           the project root's right-click target and every keyboard shortcut. Slot content is
           compiled in this component's scope, so all of that keeps working with nothing
           forwarded — and a wrapper with `overflow-auto` in the container would silently take
           the scroll off the element those handlers are bound to.

           The empty area below the rows is the project root's own right-click target: once
           the tree is taller than the pane there is otherwise nowhere to aim at it. -->
      <div
        ref="tree"
        tabindex="0"
        class="min-h-0 flex-1 overflow-auto py-1 outline-none focus-visible:ring-1 focus-visible:ring-accent focus-visible:ring-inset"
        @contextmenu.prevent="openMenu(null, $event)"
        @keydown="onKeydown"
      >
        <FileTreeNode
          v-for="entry in rootEntries"
          :key="entry.path"
          :entry="entry"
          :depth="0"
          @context="openMenu"
        />
        <div v-if="!rootEntries.length && explorer.loadingPaths.has('')" class="px-3 py-1">
          <LoadingBars />
        </div>
        <p v-else-if="!rootEntries.length" class="px-3 py-2 text-faint">
          {{ t('explorer.view.empty') }}
        </p>
      </div>

      <template #footer>
        <p
          v-if="explorer.clipboard"
          class="border-t border-line-subtle px-3 py-1.5 text-small text-faint"
        >
          <I18nT
            :keypath="
              explorer.clipboard.mode === 'cut'
                ? 'explorer.view.clipboardCut'
                : 'explorer.view.clipboardCopied'
            "
            :params="{ count: explorer.clipboard.paths.length }"
          >
            <template #clear>
              <button class="underline" @click="explorer.clearClipboard()">{{
                t('explorer.view.clipboardClear')
              }}</button>
            </template>
          </I18nT>
        </p>

        <p
          v-if="!session.project?.config.explorer.show_data_dir"
          class="border-t border-line-subtle px-3 py-1.5 text-small text-faint"
        >
          <I18nT keypath="explorer.view.dataDirHidden">
            <template #dir><code class="font-mono">.voxarium_studio/</code></template>
          </I18nT>
        </p>
      </template>
    </SidePane>

    <!-- The preview is the page's one band (`vxs-enter`, §7); the file tree beside it is a
         pane and stays still. -->
    <section class="vxs-enter min-w-0 flex-1">
      <FilePreview :preview="explorer.preview" :loading="explorer.previewLoading" />
    </section>

    <FileContextMenu
      v-if="menu"
      :at="menu.at"
      :entry="menu.entry"
      :count="menuCount"
      :clipboard="explorer.clipboard"
      :busy="explorer.busy"
      @run="run"
      @close="closeMenu"
    />

    <EntryPrompt
      v-if="prompt"
      :key="promptKey"
      :title="promptTitle"
      :hint="promptHint"
      :initial="promptInitial"
      :confirm-label="prompt.mode === 'rename' ? t('common.rename') : t('common.create')"
      :select-stem="prompt.mode === 'rename'"
      :busy="explorer.busy"
      :error="explorer.error"
      @submit="submitPrompt"
      @cancel="cancelPrompt"
    />

    <ConfirmDialog
      v-if="deleting"
      :title="t('common.delete')"
      :message="deleteMessage"
      :items="deleting"
      :confirm-label="t('common.delete')"
      danger
      :busy="explorer.busy"
      @confirm="confirmDelete"
      @cancel="((deleting = null), focusTree())"
    />

    <!-- A 409 is a question the request type can answer, so it is asked rather than
         reported: `overwrite` goes back with the same paths and destination. -->
    <ConfirmDialog
      v-if="explorer.conflict"
      :title="t('explorer.conflict.title')"
      :message="conflictMessage"
      :items="explorer.conflict.paths"
      :confirm-label="t('explorer.conflict.replace')"
      danger
      :busy="explorer.busy"
      @confirm="confirmOverwrite"
      @cancel="((explorer.dismissConflict()), focusTree())"
    />
  </div>
</template>
