<script setup lang="ts">
/** The durable session checklist stays above the conversation (§10.11). */
import { t } from '@/i18n'

defineProps<{ items: { text: string; done: boolean }[] }>()
</script>

<template>
  <section class="flex min-h-0 min-w-0 flex-col border-b border-line" :aria-label="t('producerPage.plan')" data-producer-plan>
    <h2 class="shrink-0 px-4 py-3 font-medium">{{ t('producerPage.plan') }}</h2>
    <ul class="min-h-0 space-y-2 overflow-y-auto overscroll-contain px-4 pb-3">
      <li v-for="(item, index) in items" :key="index" class="flex items-start gap-2 text-small">
        <input type="checkbox" :checked="item.done" disabled :aria-label="item.text"
          class="mt-0.5 size-3.5 shrink-0 accent-accent disabled:opacity-100" />
        <span class="min-w-0 break-words" :class="{ 'text-muted line-through': item.done }">{{ item.text }}</span>
      </li>
    </ul>
  </section>
</template>
