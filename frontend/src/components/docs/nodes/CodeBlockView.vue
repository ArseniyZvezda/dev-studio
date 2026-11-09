<script setup lang="ts">
/** The copy control is view chrome: code remains the only document content (block-editor spec §3.1). */
import { NodeViewContent, nodeViewProps, NodeViewWrapper } from '@tiptap/vue-3'
import { computed } from 'vue'

import CodeCopyButton from '@/components/ui/CodeCopyButton.vue'

const props = defineProps(nodeViewProps)
const code = computed(() => props.node.textContent)
</script>

<template>
  <NodeViewWrapper class="code-copy-block">
    <pre><NodeViewContent as="code" /></pre>
    <CodeCopyButton :text="code" />
  </NodeViewWrapper>
</template>
