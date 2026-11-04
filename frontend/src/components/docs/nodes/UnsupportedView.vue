<script setup lang="ts">
/**
 * A construct the editor does not model, shown as the Markdown it is (§3.4, §8.3).
 *
 * The text here is a *rendering* of `attrs.mdast`. What goes back to disk is that node, so
 * nothing shown — or mis-shown — in this component can damage the file. The block is an
 * atom, so the caret cannot enter it and there is nothing to edit by accident; moving and
 * deleting still work, which is all §3.4 asks for.
 */
import { nodeViewProps, NodeViewWrapper } from '@tiptap/vue-3'
import type { RootContent } from 'mdast'
import { computed } from 'vue'

import { t } from '@/i18n'
import { serializeNode } from '@/lib/md/document'

const props = defineProps(nodeViewProps)

const inline = computed(() => props.node.type.name === 'unsupportedInline')

const text = computed(() => {
  const node = props.node.attrs.mdast as RootContent | null
  if (!node) return ''
  try {
    return serializeNode(node)
  } catch {
    // A node this serializer cannot print is still safe — it is handed back untouched on
    // save — so the block degrades to a label rather than breaking the document.
    return `<${node.type}>`
  }
})
</script>

<template>
  <NodeViewWrapper
    :as="inline ? 'span' : 'div'"
    class="unsupported"
    :class="[inline ? 'unsupported-inline' : 'unsupported-block', selected && 'unsupported-selected']"
    :title="t('editor.unsupported.hint')"
  >
    <span v-if="!inline" class="unsupported-label">{{ t('editor.blocks.unsupported') }}</span>
    <code class="unsupported-source">{{ text }}</code>
  </NodeViewWrapper>
</template>
