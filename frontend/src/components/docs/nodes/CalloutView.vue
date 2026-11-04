<script setup lang="ts">
/**
 * `:::note[Title]` … `:::` (block-editor spec §3.3).
 *
 * The title is a real child node, not an attribute, so inline markup inside it survives —
 * and an *empty* one means the directive had no label, which §3.3 says renders without a
 * header. The empty case is styled away rather than removed: the node has to stay for the
 * user to be able to type a title into it, and for a collapsed toggle to know where the
 * body starts.
 */
import { NodeViewContent, nodeViewProps, NodeViewWrapper } from '@tiptap/vue-3'
import { computed } from 'vue'

import { CALLOUT_ICONS } from '@/lib/editor/icons'

const props = defineProps(nodeViewProps)

const kind = computed(() => String(props.node.attrs.kind ?? 'note'))
const icon = computed(() => CALLOUT_ICONS[kind.value] ?? CALLOUT_ICONS.note)
const untitled = computed(() => props.node.firstChild?.content.size === 0)
</script>

<template>
  <NodeViewWrapper class="callout" :class="untitled && 'is-untitled'" :data-kind="kind">
    <component :is="icon" class="callout-icon" aria-hidden="true" />
    <NodeViewContent class="callout-body" />
  </NodeViewWrapper>
</template>
