<script setup lang="ts">
/**
 * A folded block of a transcript — thinking, or a run of tool calls (§10.4).
 *
 * Dashed rather than solid on purpose: the solid cards are someone speaking, and these are
 * the work around the speech. Collapsed at Default and open at Verbose in every transcript,
 * task and chat alike (the panel decides); the header is one button that toggles it, so the
 * whole line is the target.
 */
import { ChevronDown } from 'lucide-vue-next'

defineProps<{ open: boolean; label: string }>()
defineEmits<{ toggle: [] }>()
</script>

<template>
  <section class="rounded-md border border-dashed border-line text-small text-muted" data-transcript-fold>
    <button
      type="button"
      class="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1 text-left hover:bg-hover hover:text-primary"
      :aria-expanded="open"
      @click="$emit('toggle')"
    >
      <slot name="icon" />
      <span class="shrink-0 font-medium">{{ label }}</span>
      <span class="min-w-0 flex-1 truncate text-faint"><slot name="summary" /></span>
      <span v-if="$slots.meta" class="shrink-0 text-faint"><slot name="meta" /></span>
      <ChevronDown class="vxs-chevron size-3.5 shrink-0" :class="open ? 'rotate-180' : ''" />
    </button>
    <div v-if="open" class="border-t border-dashed border-line px-3 py-2">
      <slot />
    </div>
  </section>
</template>
