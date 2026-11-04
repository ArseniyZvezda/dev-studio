<script setup lang="ts">
/**
 * The `/` menu's popup (block-editor spec §4.3).
 *
 * Placed at the caret and flipped above it near the bottom of the window, which is the whole
 * reason it is `position: fixed` in a teleport rather than living inside the scroller: a
 * popup clipped by `overflow: auto` is the usual way this goes wrong.
 *
 * Keyboard handling is *not* here. The suggestion plugin owns the keys while the menu is
 * open, so arrows and `Enter` keep working with the caret still in the document — which is
 * what lets someone keep typing to narrow the list.
 */
import { computed, ref, watch } from 'vue'

import { t } from '@/i18n'
import type { SlashMenuState } from '@/lib/editor/slash-menu'
import { groupSlashItems, slashGroupLabel, slashItemLabel } from '@/lib/editor/slash-items'

const props = defineProps<{ state: SlashMenuState }>()

/** Enough room for the popup, or it goes above the caret instead. */
const ESTIMATED_HEIGHT = 320
const GAP = 6

const list = ref<HTMLElement | null>(null)

const groups = computed(() => groupSlashItems(props.state.items))

/** Flat order, so the highlighted index matches the plugin's. */
const flat = computed(() => groups.value.flatMap((entry) => entry.items))

const position = computed(() => {
  const rect = props.state.rect
  if (!rect) return null
  const below = window.innerHeight - rect.bottom
  const flip = below < ESTIMATED_HEIGHT && rect.top > below
  return {
    left: `${Math.min(rect.left, window.innerWidth - 320)}px`,
    ...(flip
      ? { bottom: `${window.innerHeight - rect.top + GAP}px` }
      : { top: `${rect.bottom + GAP}px` }),
  }
})

// Keep the highlighted row in view when the arrows walk past the fold.
watch(
  () => props.state.index,
  async (index) => {
    const container = list.value
    if (!container) return
    const option = container.querySelectorAll<HTMLElement>('[data-slash-item]')[index]
    option?.scrollIntoView({ block: 'nearest' })
  },
)
</script>

<template>
  <Teleport to="body">
    <div
      v-if="state.open && position"
      class="slash-menu"
      :style="position"
      role="listbox"
      :aria-label="t('editor.slash.label')"
    >
      <div ref="list" class="slash-menu-list">
        <template v-for="entry in groups" :key="entry.group">
          <p class="slash-menu-group">{{ slashGroupLabel(entry.group) }}</p>
          <button
            v-for="item in entry.items"
            :key="item.id"
            type="button"
            data-slash-item
            class="slash-menu-item"
            :class="flat.indexOf(item) === state.index && 'is-active'"
            role="option"
            :aria-selected="flat.indexOf(item) === state.index"
            @mousedown.prevent="state.select(item)"
          >
            <component :is="item.icon" class="slash-menu-icon" />
            <span class="slash-menu-label">{{ slashItemLabel(item) }}</span>
            <code v-if="item.hint" class="slash-menu-hint">{{ item.hint }}</code>
          </button>
        </template>
      </div>
    </div>
  </Teleport>
</template>
