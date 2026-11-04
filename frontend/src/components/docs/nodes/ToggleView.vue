<script setup lang="ts">
/**
 * `:::toggle[Title]{open}` … `:::` (block-editor spec §3.3).
 *
 * `attrs.open` is the file's default and is read exactly once, here. Whether the toggle is
 * open *right now* is component state and never becomes an attribute — §3.3 is explicit
 * that reading a document must not produce a change in version control, and a transaction
 * setting an attribute is a change.
 *
 * The body is hidden with CSS rather than unmounted: ProseMirror needs the content in the
 * document to keep positions valid, and a collapsed toggle must not swallow the blocks
 * inside it.
 */
import { NodeViewContent, nodeViewProps, NodeViewWrapper } from '@tiptap/vue-3'
import { ChevronRight } from 'lucide-vue-next'
import { computed, ref } from 'vue'

import { t } from '@/i18n'

const props = defineProps(nodeViewProps)

const open = ref(props.node.attrs.open === true)
const untitled = computed(() => props.node.firstChild?.content.size === 0)
</script>

<template>
  <NodeViewWrapper
    class="toggle"
    :class="[open ? 'is-open' : 'is-collapsed', untitled && 'is-untitled']"
    :data-placeholder="t('editor.placeholder.title')"
  >
    <button
      type="button"
      class="toggle-chevron"
      :aria-expanded="open"
      :title="t('editor.placeholder.title')"
      contenteditable="false"
      @click="open = !open"
    >
      <ChevronRight class="size-3.5" />
    </button>
    <NodeViewContent class="toggle-body" />
  </NodeViewWrapper>
</template>
