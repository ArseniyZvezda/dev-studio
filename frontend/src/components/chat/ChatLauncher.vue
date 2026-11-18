<script setup lang="ts">
/**
 * The round corner button every website chat has: it folds the popup out and back.
 *
 * It is the one chat control that belongs to the shell rather than to a page, so it is on
 * screen everywhere (§10.9). Beside a floating popup it sits under it, in the widget's own
 * column; where the widget is docked inside a page — the Producer workspace — it stays in
 * the corner and lifts the conversation out of its slot into a popup of its own (§10.11).
 *
 * The primary action's grey with a lit edge rather than an accent fill, its icon keeping
 * the accent, since an icon is where colour goes.
 */
import { ChevronDown, MessageCircle } from 'lucide-vue-next'

import { t } from '@/i18n'
import { useChatStore } from '@/stores/chat'

const chat = useChatStore()
</script>

<template>
  <button
    class="chat-launcher pointer-events-auto relative grid size-12 place-items-center rounded-full"
    :title="chat.open ? t('chat.minimiseChat') : t('chat.label')"
    :aria-expanded="chat.open"
    :aria-label="t('chat.label')"
    @click="chat.toggle()"
  >
    <ChevronDown v-if="chat.open" class="size-5" />
    <MessageCircle v-else class="size-5" />
    <!-- `vxs-pop`: the count lands on the button when a reply does. -->
    <span
      v-if="!chat.open && chat.unreadCount"
      class="vxs-pop absolute -top-0.5 -right-0.5 grid h-4 min-w-4 place-items-center rounded-pill bg-danger px-1 text-small font-medium text-on-accent"
    >
      {{ chat.unreadCount }}
    </span>
  </button>
</template>

<style scoped>
.chat-launcher {
  background: var(--vxs-accent-gradient);
  color: var(--vxs-accent);
  box-shadow:
    inset 0 0 0 1px var(--vxs-border-strong),
    var(--vxs-shadow-lg);
}

/* The ground and the edge each step up, as the primary button's do (`base.css`). */
.chat-launcher:hover {
  background: var(--vxs-accent-gradient-hover);
  box-shadow:
    inset 0 0 0 1px color-mix(in oklab, var(--vxs-border-strong), var(--vxs-text-primary) 12%),
    var(--vxs-shadow-lg);
}
</style>
