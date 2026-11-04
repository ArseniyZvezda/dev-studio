<script setup lang="ts">
/**
 * `[[a document]]` inside the editor (block-editor spec §5).
 *
 * Resolution is a *view* concern and never touches the node: what is written back is
 * `attrs.value`, the raw text between the brackets, whether or not it points anywhere. So
 * this component decides only how the link looks and what a click does.
 *
 * The click leaves as a bubbling DOM event rather than a Vue emit or a store call. A node
 * view is mounted by ProseMirror outside the page's component tree, so there is no parent to
 * emit to; the page listens on the editor's container instead.
 */
import { nodeViewProps, NodeViewWrapper } from '@tiptap/vue-3'
import { computed } from 'vue'

import { t } from '@/i18n'
import { useDocsStore } from '@/stores/docs'

// `nodeViewProps` rather than a hand-written `defineProps<…>`: an SFC's inferred props never
// structurally match Tiptap's `NodeViewProps`, the same way they do not match Vue Flow's
// `NodeProps` (see `.claude/rules/frontend-vue.md`).
const props = defineProps(nodeViewProps)

const docs = useDocsStore()

const target = computed(() => String(props.node.attrs.target ?? ''))
const label = computed(() => (props.node.attrs.label as string | null) || target.value)
const resolved = computed(() => docs.resolveLink(target.value))

function open(event: MouseEvent) {
  event.preventDefault()
  event.currentTarget?.dispatchEvent(
    new CustomEvent('wiki-link', { detail: target.value, bubbles: true }),
  )
}
</script>

<template>
  <NodeViewWrapper
    as="span"
    class="wiki-link"
    :class="[resolved ? 'wiki-link-resolved' : 'wiki-link-missing', selected && 'wiki-link-selected']"
    :title="resolved ?? t('editor.wiki.missing', { target })"
    @click="open"
    >{{ label }}</NodeViewWrapper
  >
</template>
