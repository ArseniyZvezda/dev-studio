<script setup lang="ts">
/**
 * One chat's transcript, as a conversation (§10.9).
 *
 * The rows come from the same fold a task's transcript uses (`lib/transcript.ts`), always at
 * Advanced: what the member said, and every tool call it made on the way there as a row of
 * its own. A chat has no level switch — the calls are how the user sees that the member
 * actually looked at the thing they asked about. Two things a task hides below Verbose are
 * drawn here regardless — an error and a stop — because in a chat they are the answer to
 * "why did the reply end".
 */
import { AlertTriangle, FilePen, Square, Wrench } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import { attachmentUrl, type UploadedAttachment } from '@/api/client'
import type { AgentChatMessage, ChatContext } from '@/api/types'
import EffortSolverResult from '@/components/chat/EffortSolverResult.vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import MarkdownText from '@/components/ui/MarkdownText.vue'
import { t } from '@/i18n'
import { toolName, toolSubject } from '@/lib/activity'
import type { MemberLook } from '@/lib/member-icons'
import { formatTime } from '@/lib/status'
import { editStat, isFileEdit, liveLine, transcriptRows, type Verbosity } from '@/lib/transcript'
import { basename, pageLabelOnScreen } from '@/lib/user-context'

const props = defineProps<{
  messages: AgentChatMessage[]
  member: MemberLook | null
  memberName: string
  /** The member is answering right now. */
  running: boolean
}>()

/** Tool calls are always shown in a chat; tool results and system frames are not. */
const VERBOSITY: Verbosity = 'advanced'

/** Lines a chat always shows, whatever the level: the reasons a reply ended early. */
const NOTE_KINDS = new Set(['error', 'cancelled', 'producer_plan', 'producer_compacted'])

const rows = computed(() => {
  const folded = new Set(transcriptRows(props.messages, VERBOSITY).map((row) => row.key))
  return props.messages.filter(
    (message) =>
      (folded.has(message.id) || NOTE_KINDS.has(message.kind)) &&
      // The runtime redacts most thinking to an empty block; an empty box says nothing.
      !(message.kind === 'thinking' && !message.text.trim()),
  )
})

/**
 * The member's current step: the tail of what has arrived since the user last spoke. The
 * turn before is finished and its tail would be stale; the user's own words are not a step.
 */
const live = computed(() => {
  let start = 0
  props.messages.forEach((message, index) => {
    if (message.role === 'user') start = index + 1
  })
  return liveLine(props.messages.slice(start), VERBOSITY)
})

/** What the user's message was sent with, read defensively — `data` is an open bag. */
function attachmentsOf(message: AgentChatMessage): UploadedAttachment[] {
  const records = message.data?.attachments
  return Array.isArray(records) ? records.filter((item): item is UploadedAttachment => !!item && typeof item.reference === 'string') : []
}

function contextOf(message: AgentChatMessage): Partial<ChatContext> | null {
  const context = message.data?.context
  return context && typeof context === 'object' ? (context as Partial<ChatContext>) : null
}

function contextChips(message: AgentChatMessage): { label: string; title: string }[] {
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

// ---- following the conversation

const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)

function measureBottom(): void {
  const element = scroller.value
  if (!element) return
  atBottom.value = element.scrollHeight - element.scrollTop - element.clientHeight < 32
}

async function scrollToLatest(): Promise<void> {
  await nextTick()
  const element = scroller.value
  if (!element) return
  element.scrollTop = element.scrollHeight
  atBottom.value = true
}

// A reply arriving while the reader is at the end keeps them there; someone who scrolled up
// to reread keeps their place, exactly as the task transcript behaves.
watch(
  () => [props.messages.length, live.value.seq, props.running] as const,
  () => {
    if (atBottom.value) void scrollToLatest()
  },
)

// Another chat opens at its end: a fresh read, not the one the user had scrolled back through.
watch(
  () => props.messages[0]?.chat_id,
  () => void scrollToLatest(),
  { immediate: true },
)

defineExpose({ scrollToLatest })
</script>

<template>
  <div
    ref="scroller"
    class="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-3"
    @scroll.passive="measureBottom"
  >
    <p v-if="!rows.length && !running" class="m-auto max-w-[18rem] text-center text-faint">
      {{ t('chat.transcript.empty', { name: memberName }) }}
    </p>

    <template v-for="message in rows" :key="message.id">
      <!-- The user, on the right: their words as typed, and what they were sent with. -->
      <div v-if="message.role === 'user'" class="ml-8 flex flex-col items-end gap-1">
        <div
          class="max-w-full rounded-xl rounded-br-sm border border-accent-border bg-accent-soft px-3 py-2"
        >
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
      </div>

      <!-- A reply, on the left, in the member's mark. -->
      <div v-else-if="message.kind === 'text'" class="mr-8 flex items-start gap-2">
        <MemberMark :member="member" variant="badge" :corner="false" class="mt-0.5 shrink-0" />
        <div class="min-w-0 flex-1">
          <div
            class="rounded-xl rounded-tl-sm border border-line-subtle bg-elevated px-3 py-2"
          >
            <MarkdownText :text="message.text" />
          </div>
          <p class="mt-0.5 px-1 text-small text-faint">{{ formatTime(message.created_at) }}</p>
        </div>
      </div>

      <EffortSolverResult v-else-if="message.kind === 'effort_solver'" :data="message.data" />

      <!-- The member thinking aloud — Advanced and above. -->
      <div
        v-else-if="message.kind === 'thinking'"
        class="mr-8 ml-9 rounded-md border border-dashed border-line-subtle px-3 py-2 text-small text-muted"
      >
        <p class="mb-1 font-medium text-faint">{{ t('chat.transcript.thinking') }}</p>
        <MarkdownText :text="message.text" />
      </div>

      <!-- A tool call reads as a line, as it does on a task: nobody is speaking. -->
      <p
        v-else-if="message.kind === 'tool_use'"
        class="ml-9 flex items-baseline gap-2 px-1 text-small text-faint"
        :title="toolSubject(message)"
      >
        <FilePen v-if="isFileEdit(message)" class="size-3 shrink-0 translate-y-0.5" />
        <Wrench v-else class="size-3 shrink-0 translate-y-0.5" />
        <span class="shrink-0 font-mono text-primary">{{ toolName(message) }}</span>
        <span class="min-w-0 truncate">{{ toolSubject(message) }}</span>
        <span v-if="editStat(message)" class="shrink-0 font-mono">
          <span class="text-success">+{{ editStat(message)!.insertions }}</span>
          <span class="ml-1 text-danger">−{{ editStat(message)!.deletions }}</span>
        </span>
      </p>

      <p
        v-else-if="message.kind === 'error'"
        class="flex items-baseline gap-2 rounded-md bg-danger-soft px-3 py-2 text-small text-danger"
      >
        <AlertTriangle class="size-3 shrink-0 translate-y-0.5" />
        <span class="min-w-0 whitespace-pre-wrap break-words">{{ message.text }}</span>
      </p>

      <p
        v-else-if="message.kind === 'cancelled'"
        class="flex items-center justify-center gap-1.5 text-small text-faint"
      >
        <Square class="size-2.5 shrink-0" />
        <span>{{ t('chat.transcript.stopped') }}</span>
        <span>· {{ formatTime(message.created_at) }}</span>
      </p>

      <details
        v-else-if="message.kind === 'producer_compacted' && message.data?.replaces_history === true"
        class="rounded-md border border-line-subtle bg-inset px-3 py-2 text-small text-muted"
      >
        <summary class="cursor-pointer font-medium">{{ t('chat.producer.compacted') }}</summary>
        <MarkdownText :text="message.text" class="mt-2" />
      </details>

      <p v-else-if="message.kind === 'producer_compacted'" class="px-3 text-center text-small text-faint">
        {{ t('chat.producer.compacted') }}
      </p>

      <!-- Everything else the level lets through: a note from the Studio, a system frame. -->
      <p v-else class="ml-9 px-1 text-small text-faint">
        <span class="font-mono">{{ message.kind }}</span>
        <span v-if="message.text" class="ml-2 break-words">{{ message.text }}</span>
      </p>
    </template>

    <!-- The member at work: the tail of the feed, restarted on every step so it blinks. -->
    <div v-if="running" class="mr-8 flex items-center gap-2">
      <MemberMark :member="member" variant="badge" :corner="false" class="shrink-0" />
      <p class="flex min-w-0 items-baseline gap-2 text-small text-faint">
        <span class="vxs-chat-typing shrink-0" :aria-label="t('chat.answering')">
          <i /><i /><i />
        </span>
        <span
          v-if="live.activity"
          :key="live.seq"
          class="vxs-task-activity min-w-0 truncate font-mono"
          :title="live.activity"
        >
          {{ live.activity }}
        </span>
      </p>
    </div>
  </div>
</template>
