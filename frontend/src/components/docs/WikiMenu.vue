<script setup lang="ts">
/**
 * The `[[` autocomplete popup (block-editor spec §5).
 *
 * The same shape as the `/` menu and for the same reasons: fixed and teleported so the
 * scroller cannot clip it, flipped above the caret near the bottom of the window, and with
 * the keys left to the suggestion plugin so typing keeps narrowing the list.
 */
import { FilePlus2 } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import { t } from '@/i18n'
import type { WikiMenuState } from '@/lib/editor/wiki-menu'

const props = defineProps<{ state: WikiMenuState }>()

const ESTIMATED_HEIGHT = 280
const GAP = 6

const list = ref<HTMLElement | null>(null)

const position = computed(() => {
  const rect = props.state.rect
  if (!rect) return null
  const below = window.innerHeight - rect.bottom
  const flip = below < ESTIMATED_HEIGHT && rect.top > below
  return {
    left: `${Math.min(rect.left, window.innerWidth - 340)}px`,
    ...(flip
      ? { bottom: `${window.innerHeight - rect.top + GAP}px` }
      : { top: `${rect.bottom + GAP}px` }),
  }
})

watch(
  () => props.state.index,
  (index) => {
    const option = list.value?.querySelectorAll<HTMLElement>('[data-wiki-item]')[index]
    option?.scrollIntoView({ block: 'nearest' })
  },
)
</script>

<template>
  <Teleport to="body">
    <div
      v-if="state.open && position"
      class="slash-menu wiki-menu"
      :style="position"
      role="listbox"
      :aria-label="t('editor.wiki.label')"
    >
      <p class="slash-menu-group">{{ t('editor.wiki.label') }}</p>
      <div ref="list" class="slash-menu-list">
        <button
          v-for="(item, index) in state.items"
          :key="item.path"
          type="button"
          data-wiki-item
          class="slash-menu-item"
          :class="index === state.index && 'is-active'"
          role="option"
          :aria-selected="index === state.index"
          @mousedown.prevent="state.select(item)"
        >
          <span class="slash-menu-label">{{ item.title }}</span>
          <!-- Shown only when the name alone would not say which document this is (§5). -->
          <code v-if="item.hint" class="slash-menu-hint">{{ item.hint }}</code>
        </button>

        <button
          v-if="state.create"
          type="button"
          data-wiki-item
          class="slash-menu-item is-active"
          role="option"
          aria-selected="true"
          @mousedown.prevent="state.selectNew(state.create)"
        >
          <FilePlus2 class="slash-menu-icon" />
          <span class="slash-menu-label">{{ t('editor.wiki.create', { name: state.create }) }}</span>
        </button>
      </div>
    </div>
  </Teleport>
</template>
