<script setup lang="ts">
/**
 * "What should it be called?" — the one dialog behind New file, New folder and Rename.
 *
 * It stays open when the backend refuses the name, because a 400 ("that is not a valid
 * name") or a 409 ("something is already called that") is answered by editing the field
 * that is already on screen, not by starting again.
 */
import { nextTick, onMounted, ref } from 'vue'

import { t } from '@/i18n'
import { useLeave } from '@/lib/leave'

const props = defineProps<{
  title: string
  hint: string
  initial: string
  confirmLabel: string
  /** Preselect the name without its extension, which is the part a rename usually changes. */
  selectStem?: boolean
  busy?: boolean
  error?: string | null
}>()

const emit = defineEmits<{ submit: [value: string]; cancel: [] }>()

const value = ref(props.initial)
const input = ref<HTMLInputElement | null>(null)
const overlay = ref<HTMLElement | null>(null)

useLeave(() => overlay.value)

onMounted(async () => {
  await nextTick()
  const field = input.value
  if (!field) return
  field.focus()
  const dot = props.initial.lastIndexOf('.')
  const stemEnd = props.selectStem && dot > 0 ? dot : props.initial.length
  field.setSelectionRange(0, stemEnd)
})

function submit(): void {
  const next = value.value.trim()
  if (next) emit('submit', next)
}
</script>

<template>
  <Teleport to="body">
    <!-- `vxs-veil`: the page dims as the prompt arrives and comes back as it leaves (§7). -->
    <div
      ref="overlay"
      class="vxs-veil fixed inset-0 z-50 grid place-items-center bg-canvas/70 p-6"
      @click.self="emit('cancel')"
    >
      <form
        class="w-[26rem] max-w-full rounded-lg border border-line bg-surface shadow-lg"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        @submit.prevent="submit"
      >
        <header class="border-b border-line px-4 py-3">
          <h2 class="font-medium">{{ title }}</h2>
          <p class="truncate text-small text-faint" :title="hint">{{ hint }}</p>
        </header>

        <div class="p-4">
          <input
            ref="input"
            v-model="value"
            class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
            spellcheck="false"
            @keydown.esc.prevent="emit('cancel')"
          />
          <p v-if="error" class="mt-2 break-all text-danger">{{ error }}</p>
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
            type="submit"
            class="vxs-btn-primary px-3 py-1 font-medium text-on-accent disabled:opacity-50"
            :disabled="busy || !value.trim()"
          >
            {{ confirmLabel }}
          </button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>
