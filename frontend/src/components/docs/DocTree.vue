<script setup lang="ts">
/**
 * Folder tree over `voxarium_studio/docs/`. Recursive, so a nested path like
 * `features/combat/dash.md` reads as the folders it actually is on disk.
 *
 * The rows are `TreeRow`, the same row the Explorer draws — one style for both trees, and
 * version control status on a document exactly as on any other file (§6.2, §9.6). The
 * status is keyed by paths in the *project's* frame, so `vcsRoot` (normally `voxarium_studio/docs`)
 * goes in front of a document's own path before the lookup; a tree given no root draws no
 * marks at all rather than looking every document up under the wrong name.
 *
 * A folder wears the colour and icon its `vxs-folder.json` gives it (block-editor spec §5.2),
 * read off the store like the fold state — and for the same reason.
 */
import { FileText } from 'lucide-vue-next'

import TreeRow from '@/components/ui/TreeRow.vue'
import { folderColorValue, folderIcon } from '@/lib/folder-style'
import { useDocsStore, type DocTreeNode } from '@/stores/docs'

const props = withDefaults(
  defineProps<{
    nodes: DocTreeNode[]
    selected: string | null
    depth?: number
    /** Where the documents folder sits in the project, for the version control lookup. */
    vcsRoot?: string | null
  }>(),
  { depth: 0, vcsRoot: null },
)
const emit = defineEmits<{ open: [path: string]; context: [node: DocTreeNode, event: MouseEvent] }>()

// Which folders are folded is the store's, not this component's: every level of the
// recursion shares one set, and it outlives an instance that a `v-if` above throws away.
const docs = useDocsStore()

function vcsPath(node: DocTreeNode): string | null {
  return props.vcsRoot === null ? null : `${props.vcsRoot}/${node.path}`
}

/** A folder opens and closes; a document opens in the editor. */
function activate(node: DocTreeNode) {
  if (node.isFolder) docs.toggleFolder(node.path)
  else emit('open', node.path)
}
</script>

<template>
  <ul class="list-none">
    <li v-for="node in props.nodes" :key="node.path">
      <TreeRow
        :name="node.isFolder ? node.name : (node.entry?.title ?? node.name)"
        :is-dir="node.isFolder"
        :depth="props.depth"
        :expanded="!docs.isCollapsed(node.path)"
        :selected="props.selected === node.path && !node.isFolder"
        :icon="FileText"
        :folder-icon="node.isFolder ? (folderIcon(docs.folderStyle(node.path)?.icon)?.component ?? null) : null"
        :icon-color="node.isFolder ? folderColorValue(docs.folderStyle(node.path)?.color) : null"
        :vcs-path="vcsPath(node)"
        :title="node.isFolder ? undefined : node.path"
        focusable
        @click="activate(node)"
        @toggle="docs.toggleFolder(node.path)"
        @context="emit('context', node, $event)"
      />

      <DocTree
        v-if="node.isFolder && !docs.isCollapsed(node.path)"
        :nodes="node.children"
        :selected="props.selected"
        :depth="props.depth + 1"
        :vcs-root="props.vcsRoot"
        @open="emit('open', $event)"
        @context="(child, event) => emit('context', child, event)"
      />
    </li>
  </ul>
</template>
