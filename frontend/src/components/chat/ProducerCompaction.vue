<script setup lang="ts">
import MarkdownText from '@/components/ui/MarkdownText.vue'
import { t } from '@/i18n'
import type { TranscriptMessage } from '@/lib/transcript'

defineProps<{ message: TranscriptMessage }>()
</script>

<template>
  <details
    v-if="message.data?.replaces_history === true"
    class="rounded-md border border-line-subtle bg-inset px-3 py-2 text-small text-muted"
  >
    <summary class="cursor-pointer font-medium">{{ t('chat.producer.compacted') }}</summary>
    <MarkdownText :text="message.text" class="mt-2" />
  </details>

  <p v-else class="px-3 text-center text-small text-faint">
    {{ t('chat.producer.compacted') }}
  </p>
</template>
