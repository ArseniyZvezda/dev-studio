<script setup lang="ts">
/**
 * One row of the Explorer tree; recurses into itself for expanded directories.
 *
 * The right-click is *emitted* rather than handled, and re-emitted through the recursion,
 * so the whole tree shares one menu (`ExplorerView` mounts it). A menu per row would be a
 * menu per file in the project.
 *
 * The row itself is `TreeRow`, shared with the Documents tree — one style for both, and
 * the version control marks (§6.2, §9.6) drawn in one place. What is the Explorer's own
 * here is the state behind the row: expansion, selection, the clipboard's cut and the lazy
 * read of a directory all live in the explorer store, so they survive a re-read.
 */
import { Box, File as FileIcon, FileImage, FileText } from 'lucide-vue-next'
import { computed } from 'vue'

import type { FileEntry } from '@/api/types'
import TreeRow from '@/components/ui/TreeRow.vue'
import { t } from '@/i18n'
import { useExplorerStore } from '@/stores/explorer'

const props = defineProps<{ entry: FileEntry; depth: number }>()
const emit = defineEmits<{ context: [entry: FileEntry, event: MouseEvent] }>()

const explorer = useExplorerStore()

const isExpanded = computed(() => explorer.expanded.has(props.entry.path))
const isSelected = computed(() => explorer.isSelected(props.entry.path))
const isActive = computed(() => explorer.selectedPath === props.entry.path)
const isCut = computed(() => explorer.isCut(props.entry.path))
const childEntries = computed(() => explorer.children[props.entry.path] ?? [])
const isLoading = computed(() => explorer.loadingPaths.has(props.entry.path))

/** By what the backend sniffed the file to be; a folder's icon is the row's own. */
const icon = computed(() => {
  switch (props.entry.kind) {
    case 'image':
      return FileImage
    case 'model':
      return Box
    case 'text':
      return FileText
    default:
      return FileIcon
  }
})

async function onClick(event: MouseEvent | KeyboardEvent): Promise<void> {
  const mode = event.shiftKey ? 'range' : event.ctrlKey || event.metaKey ? 'toggle' : 'replace'
  await explorer.select(props.entry, mode)
  // Walking the tree is a plain click's job alone — building a selection over a folder must
  // not collapse it, and neither must opening its menu.
  if (mode === 'replace' && props.entry.is_dir) await explorer.toggle(props.entry.path)
}
</script>

<template>
  <TreeRow
    :name="entry.name"
    :is-dir="entry.is_dir"
    :depth="depth"
    :expanded="isExpanded"
    :selected="isSelected"
    :active="isActive"
    :dimmed="isCut"
    :loading="isLoading"
    :icon="icon"
    :vcs-path="entry.path"
    @click="onClick"
    @toggle="explorer.toggle(entry.path)"
    @context="emit('context', entry, $event)"
  />

  <template v-if="entry.is_dir && isExpanded">
    <FileTreeNode
      v-for="child in childEntries"
      :key="child.path"
      :entry="child"
      :depth="depth + 1"
      @context="(node, event) => emit('context', node, event)"
    />
    <p
      v-if="!childEntries.length && !isLoading"
      class="text-faint italic"
      :style="{ paddingLeft: `${(depth + 1) * 12 + 24}px` }"
    >
      {{ t('explorer.tree.empty') }}
    </p>
  </template>
</template>
