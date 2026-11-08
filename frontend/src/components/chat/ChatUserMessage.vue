<script setup lang="ts">
import { attachmentUrl, type UploadedAttachment } from '@/api/client'
import type { ChatContext } from '@/api/types'
import { t } from '@/i18n'
import type { TranscriptMessage } from '@/lib/transcript'
import { basename, pageLabelOnScreen } from '@/lib/user-context'

defineProps<{ message: TranscriptMessage; bubble?: boolean }>()
/** What the user's message was sent with, read defensively — `data` is an open bag. */
function attachmentsOf(message: TranscriptMessage): UploadedAttachment[] {
  const records = message.data?.attachments
  return Array.isArray(records) ? records.filter((item): item is UploadedAttachment => !!item && typeof item.reference === 'string') : []
}

function contextOf(message: TranscriptMessage): Partial<ChatContext> | null {
  const context = message.data?.context
  return context && typeof context === 'object' ? (context as Partial<ChatContext>) : null
}

function contextChips(message: TranscriptMessage): { label: string; title: string }[] {
  const context = contextOf(message)
  if (!context) return []
  const chips: { label: string; title: string }[] = []
  // The page was sent in English (`user-context.ts`); the chip reads it in the language on screen.
  if (context.page) {
    chips.push({ label: pageLabelOnScreen(context.page), title: context.route || context.page })
  }
  if (context.document) {
    chips.push({ label: basename(context.document), title: context.document })
  } else if (context.file) {
    chips.push({ label: basename(context.file), title: context.file })
  }
  if (context.selection) {
    chips.push({
      label: t('chat.context.selection', { count: context.selection.length }),
      title: context.selection.slice(0, 400),
    })
  }
  return chips
}
</script>

<template>
  <div :class="bubble ? 'max-w-full rounded-xl rounded-br-sm border border-accent-border bg-accent-soft px-3 py-2' : ''">
    <p v-if="message.kind === 'producer_plan'" class="mb-1 text-small font-medium">{{ t('chat.producer.approved') }}</p>
    <p class="whitespace-pre-wrap break-words">{{ message.text }}</p>
    <a v-for="entry in attachmentsOf(message)" :key="entry.reference" :href="attachmentUrl(entry.reference)" target="_blank" rel="noopener" class="mt-2 block underline">
      <img v-if="entry.image" :src="attachmentUrl(entry.reference)" :alt="entry.name" class="max-h-48 max-w-full rounded" />
      {{ entry.name }}
    </a>
  </div>
  <p
    v-if="contextChips(message).length"
    class="flex max-w-full flex-wrap justify-end gap-1 text-small text-faint"
  >
    <span
      v-for="chip in contextChips(message)"
      :key="chip.label"
      class="max-w-[12rem] truncate rounded-pill bg-inset px-2 leading-5"
      :title="chip.title"
    >
      {{ chip.label }}
    </span>
  </p>
</template>
