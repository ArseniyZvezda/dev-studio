<script setup lang="ts">
/**
 * A document for reading only — the graph page's side panel (block-editor spec §5.1.1).
 *
 * The same schema and node views as `BlockEditor`, so a callout, a wiki link, a picture or a
 * coloured phrase looks exactly as it does in Documents; none of its editing — no gutter, no
 * menus, no autosave, nothing that could produce Markdown. With `editable: false` ProseMirror
 * takes no input, and there is no `onUpdate` here to carry a change anywhere, so §8.2's rule
 * that a document opened and not edited is never written holds by construction.
 *
 * The parent mounts it with `:key` on the path, as Documents does its editor: another document
 * is another reader.
 */
import { EditorContent, useEditor } from '@tiptap/vue-3'
import { computed } from 'vue'

import { createExtensions } from '@/lib/editor/extensions'
import { createSlashState } from '@/lib/editor/slash-menu'
import { parseDocument } from '@/lib/md/document'
import { mdastToProseMirror } from '@/lib/md/prosemirror'
import { useDocsStore } from '@/stores/docs'

const props = defineProps<{
  /** The document shown — what its pictures are resolved against (§3.4). */
  path: string
  source: string
}>()

const emit = defineEmits<{
  /** A `[[wiki link]]` was clicked; the raw target, as Documents' editor reports it. */
  navigate: [target: string]
}>()

const docs = useDocsStore()

const editor = useEditor({
  content: mdastToProseMirror(parseDocument(props.source).root),
  editable: false,
  extensions: createExtensions({
    // The `/` menu is part of the schema's assembly and never opens without input.
    slash: createSlashState(),
    documentPath: () => props.path,
  }),
  editorProps: {
    attributes: { class: 'markdown-body block-editor', spellcheck: 'false' },
  },
})

/** The reading size and leading somebody picked in Documents, so a document reads the same here. */
const style = computed(() => ({
  '--vxs-doc-text-size': docs.fontSize === null ? undefined : `${docs.fontSize}px`,
  '--vxs-doc-line-height': docs.lineHeight === null ? undefined : String(docs.lineHeight),
}))

function onWikiLink(event: Event): void {
  emit('navigate', String((event as CustomEvent<string>).detail ?? ''))
}
</script>

<template>
  <div class="doc-reader min-h-0 flex-1 overflow-auto" :style="style" @wiki-link="onWikiLink">
    <EditorContent v-if="editor" :editor="editor" class="block-editor-column" />
  </div>
</template>

<style scoped>
/* No gutter to make room for: the column's side padding is only air here, in a panel a third
   of the page wide. */
.doc-reader :deep(.block-editor-column) {
  padding: var(--vxs-space-4) var(--vxs-space-5) var(--vxs-space-6);
}
</style>
