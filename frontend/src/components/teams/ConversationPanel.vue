<script setup lang="ts">
/** The transcript panel shared by the task dialog, active tasks and Producer chat (§10.4, §10.11). */
import { useResizeObserver } from '@vueuse/core'
import { ArrowDown, FilePen, FileText, GitCommitHorizontal, GraduationCap, Pencil, RefreshCw, Wrench } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import ChatUserMessage from '@/components/chat/ChatUserMessage.vue'
import EffortSolverResult from '@/components/chat/EffortSolverResult.vue'
import ProducerCompaction from '@/components/chat/ProducerCompaction.vue'
import MarkdownText from '@/components/ui/MarkdownText.vue'
import ModelEffortSlider from '@/components/ui/ModelEffortSlider.vue'
import { t } from '@/i18n'
import { toolName, toolSubject } from '@/lib/activity'
import { formatTime, statusColor, statusLabel } from '@/lib/status'
import { resultTypeColor, resultTypeHint, resultTypeLabel, resultTypeOf } from '@/lib/task-result'
import type { TranscriptMessage, Verbosity } from '@/lib/transcript'
import {
  PROSE_KINDS,
  VERBOSITY_HINTS,
  VERBOSITY_LABELS,
  VERBOSITY_LEVELS,
  commitText,
  editStat,
  editedText,
  historyReason,
  isFileEdit,
  isSkillCall,
  isVerbosity,
  liveLine,
  transcriptRows,
} from '@/lib/transcript'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{
  conversationId: string | null
  messages: TranscriptMessage[]
  emptyText: string
  embedded?: boolean
  taskStatus?: string
  producer?: { name: string; running: boolean }
}>()
const teams = useTeamsStore()
const transcript = ref<HTMLElement | null>(null)
const VERBOSITY_KEY = 'voxarium_studio.transcript.verbosity'
const remembered = typeof window === 'undefined' ? null : window.localStorage.getItem(VERBOSITY_KEY)
const verbosity = ref<Verbosity>(isVerbosity(remembered) ? remembered : 'compact')
const rows = computed(() => {
  const folded = transcriptRows(props.messages, verbosity.value)
  return props.producer ? folded.filter(({ message }) => message.kind !== 'thinking' || message.text.trim()) : folded
})
const live = computed(() => {
  let start = 0
  if (props.producer) {
    // Only the current Producer turn has live activity (§10.9).
    if (!props.producer.running) return { activity: '', seq: 0 }
    props.messages.forEach((message, index) => { if (message.role === 'user') start = index + 1 })
  }
  return liveLine(start ? props.messages.slice(start) : props.messages, verbosity.value)
})

function actorName(message: TranscriptMessage): string {
  const actor = message.data?.actor_id
  if (typeof actor === 'string') return teams.memberById.get(actor)?.name ?? actor
  return t('taskDetail.you')
}

/** What a status entry records: the transition, and why it happened when there is a why. */
function transition(message: TranscriptMessage): {
  from: string | null
  to: string
  reason: string
} {
  return {
    from: typeof message.data?.from === 'string' ? message.data.from : null,
    to: typeof message.data?.to === 'string' ? message.data.to : props.taskStatus ?? '',
    reason: typeof message.data?.reason === 'string' ? message.data.reason : '',
  }
}

const follow = ref(true)
const atBottom = ref(true)

/** Within a line of the end counts as the end — a scroll position rarely lands exactly. */
function measureBottom(): void {
  const element = transcript.value
  if (!element) return
  atBottom.value = element.scrollHeight - element.scrollTop - element.clientHeight < 32
}

async function scrollToLatest(): Promise<void> {
  await nextTick()
  const element = transcript.value
  if (!element) return
  element.scrollTop = element.scrollHeight
  atBottom.value = true
}

watch(
  [() => props.messages.length, () => live.value.seq, () => props.producer?.running],
  () => {
    if (follow.value && atBottom.value) void scrollToLatest()
  },
)

// Switching it back on is an instruction to catch up, not just to follow from here.
watch(follow, (enabled) => {
  if (enabled) void scrollToLatest()
})

/**
 * Turning the dial is a re-read of the same feed, and every row above the reader changes
 * height when it does — so someone who was at the end is put back there rather than left
 * wherever that many pixels now lands. Someone who had scrolled up keeps their place.
 */
watch(verbosity, (level) => {
  window.localStorage.setItem(VERBOSITY_KEY, level)
  if (atBottom.value) void scrollToLatest()
})
watch([() => props.conversationId, transcript], () => void scrollToLatest(), { immediate: true })
// Expanding a task's delegated list or the chat composer also resizes the viewport.
useResizeObserver(transcript, () => {
  if (follow.value && atBottom.value) void scrollToLatest()
})
</script>

<template>
  <slot v-if="$slots.alternative" name="alternative" />
  <div v-else ref="transcript" data-conversation-transcript
    class="flex-1 space-y-2 overflow-auto p-pane" :class="embedded ? 'min-h-32' : 'min-h-0'"
    @scroll.passive="measureBottom">
    <slot name="before" />
    <template v-for="row in rows" :key="row.key">
      <!-- History reads as a timeline rather than as a message: it has no author
           speaking, and giving it the same card as one would bury the transcript in
           bookkeeping. -->
      <p
        v-if="row.message.kind === 'status'"
        class="flex items-center gap-2 px-1 text-small text-faint"
      >
        <span
          class="size-1.5 shrink-0 rounded-full"
          :style="{ background: statusColor(transition(row.message).to) }"
        />
        <span v-if="transition(row.message).from">
          {{ statusLabel(transition(row.message).from!) }} →
        </span>
        <span class="font-medium text-primary">
          {{ statusLabel(transition(row.message).to) }}
        </span>
        <span v-if="!transition(row.message).from">
          · {{ t('taskDetail.history.created') }}
        </span>
        <span v-else-if="transition(row.message).reason">
          · {{ historyReason(transition(row.message).reason) }}
        </span>
        <span class="truncate">· {{ actorName(row.message) }}</span>
        <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
      </p>

      <p
        v-else-if="row.message.kind === 'edited'"
        class="flex items-center gap-2 px-1 text-small text-faint"
      >
        <Pencil class="size-3 shrink-0" />
        <span class="truncate">
          {{ editedText(row.message) }} · {{ actorName(row.message) }}
        </span>
        <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
      </p>

      <!-- The Commit button landing (§10.4): history like a status move, since it is
           the task's work leaving the working copy, and worth a line at every level. -->
      <p
        v-else-if="row.message.kind === 'commit'"
        class="flex items-center gap-2 px-1 text-small text-faint"
        :title="typeof row.message.data?.title === 'string' ? row.message.data.title : ''"
      >
        <GitCommitHorizontal class="size-3 shrink-0 text-success" />
        <span class="truncate">{{ commitText(row.message) }}</span>
        <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
      </p>

      <!-- The Studio resuming a stalled session, or declining to. Timeline rather than
           card for the same reason as the rest: nobody is speaking. -->
      <p
        v-else-if="row.message.kind === 'continued'"
        class="flex items-baseline gap-2 px-1 text-small text-faint"
      >
        <RefreshCw class="size-3 shrink-0 translate-y-0.5" />
        <span class="min-w-0">{{ row.message.text }}</span>
        <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
      </p>

      <!-- Preparation and incomplete disk evidence remain visible at every level (§10.4). -->
      <p
        v-else-if="row.message.kind === 'file_observation_status' || row.message.kind === 'file_observation_error'"
        class="flex items-baseline gap-2 px-1 text-small"
        :class="row.message.kind === 'file_observation_error' ? 'text-warning' : 'text-faint'"
      >
        <FileText class="size-3 shrink-0 translate-y-0.5" />
        <span class="min-w-0 break-words">{{ row.message.text }}</span>
        <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
      </p>

      <!-- A tool call reads as a timeline entry for the same reason history does: it
           has no author speaking, and one card per call would bury the agent's own
           words. A call that *wrote* something carries its own icon and its size —
           how much it changed is the one thing about it worth as much as its name
           (Advanced and up; Compact leaves the file and its count to the column). -->
      <p
        v-else-if="row.message.kind === 'tool_use'"
        class="flex items-baseline gap-2 px-1 text-small text-faint"
        :title="toolSubject(row.message)"
      >
        <FilePen v-if="isFileEdit(row.message)" class="size-3 shrink-0 translate-y-0.5" />
        <GraduationCap
          v-else-if="isSkillCall(row.message)"
          class="size-3 shrink-0 translate-y-0.5"
          :style="{ color: 'var(--vxs-nav-skills)' }"
        />
        <Wrench v-else class="size-3 shrink-0 translate-y-0.5" />
        <span class="shrink-0 font-mono text-primary">{{ toolName(row.message) }}</span>
        <span class="min-w-0 truncate">{{ toolSubject(row.message) }}</span>
        <span v-if="editStat(row.message)" class="shrink-0 font-mono">
          <span class="text-success">+{{ editStat(row.message)!.insertions }}</span>
          <span class="ml-1 text-danger">−{{ editStat(row.message)!.deletions }}</span>
        </span>
        <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
      </p>

      <EffortSolverResult v-else-if="producer && row.message.kind === 'effort_solver'" :data="row.message.data" />
      <ProducerCompaction v-else-if="producer && row.message.kind === 'producer_compacted'" :message="row.message" />

      <!-- A summary that came with a result type (§10.4) wears its colour: the edge and
           a chip in the result's green, blue or red, with the words left as they are.
           Level 3 of the styling rules — the colour is a token resolved per entry. -->
      <article
        v-else
        class="rounded-md border p-2"
        :class="
          row.message.role === 'user'
            ? 'border-accent-border bg-accent-soft'
            : ['cancelled', 'error'].includes(row.message.kind)
              ? 'border-danger bg-danger-soft'
              : 'border-line-subtle bg-elevated'
        "
        :style="
          resultTypeOf(row.message)
            ? {
                borderColor: resultTypeColor(resultTypeOf(row.message)!),
                boxShadow: `inset 3px 0 0 ${resultTypeColor(resultTypeOf(row.message)!)}`,
              }
            : undefined
        "
      >
        <p class="mb-1 flex items-baseline gap-2 text-small text-faint">
          <span class="font-medium">
            {{
              row.message.role === 'user'
                ? t('taskDetail.you')
                : (teams.memberById.get(row.message.member_id)?.name ??
                  producer?.name ?? row.message.member_id)
            }}
          </span>
          <span class="font-mono">{{ ['context_summary', 'compressed summary'].includes(row.message.kind) ? t('taskDetail.compression.summary') : row.message.kind }}</span>
          <span
            v-if="resultTypeOf(row.message)"
            class="inline-flex items-center gap-1 font-medium"
            :style="{ color: resultTypeColor(resultTypeOf(row.message)!) }"
            :title="resultTypeHint(resultTypeOf(row.message)!)"
          >
            <span
              class="size-1.5 rounded-full"
              :style="{ background: resultTypeColor(resultTypeOf(row.message)!) }"
            />
            {{ resultTypeLabel(resultTypeOf(row.message)!) }}
          </span>
          <span class="ml-auto">{{ formatTime(row.message.created_at) }}</span>
        </p>
        <!-- §10.3: structured model frames use the same effort display as the header. -->
        <div
          v-if="row.message.kind === 'model' && typeof row.message.data?.effort === 'string'"
          class="flex flex-wrap items-center gap-2"
        >
          <span v-if="typeof row.message.data.model === 'string'" class="font-mono text-small">
            {{ row.message.data.model }}
          </span>
          <ModelEffortSlider
            :model-value="row.message.data.effort"
            :levels="teams.options?.effort_levels"
            readonly
          />
          <span v-if="row.message.data.source === 'task'" class="text-small text-faint">
            {{ t('taskDetail.runSettings.overridden') }}
          </span>
        </div>
        <ChatUserMessage v-else-if="producer && row.message.role === 'user'" :message="row.message" />
        <p v-else-if="producer && row.message.kind === 'cancelled'" class="text-danger">{{ t('chat.transcript.stopped') }}</p>
        <MarkdownText v-else-if="PROSE_KINDS.has(row.message.kind)" :text="row.message.text" />
        <p v-else class="whitespace-pre-wrap">{{ row.message.text }}</p>
        <slot name="message-extra" :message="row.message" />
      </article>
    </template>
    <p
      v-if="live.activity || producer?.running || $slots.usage"
      class="flex items-center gap-2 px-1 pl-4 text-small text-faint"
    >
      <span v-if="producer?.running" class="vxs-chat-typing shrink-0" :aria-label="t('chat.answering')"><i /><i /><i /></span>
      <span
        v-if="live.activity"
        :key="live.seq"
        class="vxs-task-activity min-w-0 truncate font-mono"
        :title="live.activity"
      >
        {{ live.activity }}
      </span>
      <slot name="usage" />
    </p>
    <p v-if="!rows.length && !live.activity && !producer?.running && !$slots.usage" class="text-faint">{{ emptyText }}</p>
  </div>

  <!-- Outside the scroll area on purpose: "follow" is wanted exactly when the user has
       scrolled away from the end, which is where a control living at the end of the
       transcript cannot be reached. -->
  <div
    class="shrink-0 border-t border-line-subtle px-pane py-1 text-small text-faint"
  >
    <div class="flex flex-wrap items-center gap-x-4 gap-y-1">
      <!-- A dial rather than a switch: there were always three readings of a run — what
           happened, what was done, and everything — and only two settings for them. -->
      <label
        class="flex cursor-pointer items-center gap-2"
        :title="t(VERBOSITY_HINTS[verbosity])"
      >
        {{ t('taskDetail.verbosity.label') }}
        <select
          v-model="verbosity"
          class="cursor-pointer rounded-sm border border-line bg-inset px-1 py-0.5 outline-none focus:border-accent"
        >
          <option v-for="level in VERBOSITY_LEVELS" :key="level" :value="level">
            {{ t(VERBOSITY_LABELS[level]) }}
          </option>
        </select>
      </label>
      <label class="flex cursor-pointer items-center gap-2" :title="t('taskDetail.follow.hint')">
        <input v-model="follow" type="checkbox" class="accent-[var(--vxs-accent)]" />
        {{ t('taskDetail.follow.label') }}
      </label>
      <slot name="controls" />
      <button
        v-if="!atBottom"
        class="ml-auto flex items-center gap-1 rounded-sm px-1 hover:bg-hover hover:text-primary"
        :title="t('taskDetail.follow.latestHint')"
        @click="scrollToLatest()"
      >
        <ArrowDown class="size-3" /> {{ t('taskDetail.follow.latest') }}
      </button>
    </div>
  </div>
</template>
