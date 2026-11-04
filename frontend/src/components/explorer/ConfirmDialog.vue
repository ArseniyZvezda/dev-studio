<script setup lang="ts">
/**
 * A question with two answers, used for the two irreversible things the Explorer can do:
 * deleting (recursive, and there is no undo) and overwriting what a transfer collided with.
 *
 * `items` is what makes it honest — the paths are named rather than counted, because
 * "Delete 12 items?" is not something anyone can agree to.
 */
import { nextTick, onMounted, ref } from 'vue'

import { t } from '@/i18n'
import { useLeave } from '@/lib/leave'

defineProps<{
  title: string
  message: string
  items?: string[]
  confirmLabel: string
  danger?: boolean
  busy?: boolean
}>()

const emit = defineEmits<{ confirm: []; cancel: [] }>()

const overlay = ref<HTMLElement | null>(null)
const root = ref<HTMLElement | null>(null)

useLeave(() => overlay.value)

// The *dialog* takes focus, not the button: `Escape` has to reach a handler, and putting
// the caret on "Delete" would make a stray `Enter` do the irreversible thing.
onMounted(async () => {
  await nextTick()
  root.value?.focus()
})
</script>

<template>
  <Teleport to="body">
    <!-- `vxs-veil`: the page dims as the question arrives and comes back as it leaves (§7). -->
    <div
      ref="overlay"
      class="vxs-veil fixed inset-0 z-50 grid place-items-center bg-canvas/70 p-6"
      @click.self="emit('cancel')"
    >
      <div
        ref="root"
        class="w-[28rem] max-w-full rounded-lg border border-line bg-surface shadow-lg outline-none"
        role="alertdialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
        @keydown.esc.prevent="emit('cancel')"
      >
        <header class="border-b border-line px-4 py-3">
          <h2 class="font-medium" :class="danger && 'text-danger'">{{ title }}</h2>
        </header>

        <div class="p-4">
          <p>{{ message }}</p>
          <ul v-if="items?.length" class="mt-2 max-h-40 overflow-auto rounded-md bg-inset p-2">
            <li v-for="item in items" :key="item" class="truncate font-mono text-small" :title="item">
              {{ item }}
            </li>
          </ul>
        </div>

        <footer class="flex justify-end gap-2 border-t border-line px-4 py-3">
          <button
            type="button"
            class="rounded-md border border-line px-3 py-1 hover:bg-hover"
            @click="emit('cancel')"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="rounded-md px-3 py-1 font-medium text-on-accent disabled:opacity-50"
            :class="danger ? 'bg-danger' : 'vxs-btn-primary'"
            :disabled="busy"
            @click="emit('confirm')"
          >
            {{ confirmLabel }}
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>
