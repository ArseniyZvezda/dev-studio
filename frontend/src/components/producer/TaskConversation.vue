<script setup lang="ts">
/** Independent live task transcripts share the app's socket, never its openTask (§10.11). */
import { ArrowUpRight, MessageSquare, RotateCw, Wrench } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { api, taskAttachmentUrl } from '@/api/client'
import type { AgentMessage, TaskAttachment, TeamMember, TeamTask } from '@/api/types'
import MemberMark from '@/components/teams/MemberMark.vue'
import MarkdownText from '@/components/ui/MarkdownText.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { t } from '@/i18n'
import { toolName, toolSubject } from '@/lib/activity'
import { formatTime, statusColor, statusLabel } from '@/lib/status'
import { mergeTaskConversation, taskConversationRows } from '@/lib/task-conversation'
import { resultTypeColor, resultTypeHint, resultTypeLabel, resultTypeOf } from '@/lib/task-result'
import { taskCost } from '@/lib/task-telemetry'
import { liveLine, PROSE_KINDS } from '@/lib/transcript'
import { onEvent } from '@/ws/connection'

const props = defineProps<{ task: TeamTask; member?: TeamMember | null }>()
const messages = ref<AgentMessage[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)
const running = computed(() => props.task.is_running === true)
const rows = computed(() => taskConversationRows(messages.value))
const live = computed(() => liveLine(messages.value, 'advanced'))
const cost = computed(() => taskCost(props.task.total_cost_usd, messages.value))
let generation = 0
let alive = true
let received: AgentMessage[] = []

async function load(): Promise<void> {
  if (!alive) return
  const request = ++generation
  const { id, team_id: teamId } = props.task
  received = []
  loading.value = true
  error.value = null
  try {
    const detail = await api.task(teamId, id)
    if (!alive || request !== generation) return
    messages.value = mergeTaskConversation(detail.messages, received)
  } catch (cause) {
    if (alive && request === generation) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (alive && request === generation) {
      loading.value = false
      received = []
    }
  }
}

function receive(message: AgentMessage): void {
  if (!alive || message.task_id !== props.task.id) return
  if (loading.value) received = mergeTaskConversation(received, [message])
  messages.value = mergeTaskConversation(messages.value, [message])
}

function measureBottom(): void {
  const element = scroller.value
  if (element) atBottom.value = element.scrollHeight - element.scrollTop - element.clientHeight < 40
}

async function follow(): Promise<void> {
  if (!atBottom.value) return
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

function attachmentsOf(message: AgentMessage): TaskAttachment[] {
  const attachments = message.data?.attachments
  return Array.isArray(attachments) ? attachments.filter((entry): entry is TaskAttachment =>
    !!entry && typeof entry.name === 'string' && typeof entry.path === 'string') : []
}

function summaryColor(message: AgentMessage): string | undefined {
  const type = resultTypeOf(message)
  if (type) return resultTypeColor(type)
  if (message.kind === 'error' || message.kind === 'cancelled') return resultTypeColor('error')
}

function summaryLabel(message: AgentMessage): string {
  const type = resultTypeOf(message)
  if (type) return resultTypeLabel(type)
  return resultTypeLabel('error')
}

const unsubscribe = [
  onEvent('team.member.message', receive),
  onEvent('team.task.updated', event => {
    // A compressed transcript is replaced in storage and announced as task.updated.
    if (event.task_id === props.task.id && event.team_id === props.task.team_id) void load()
  }),
  onEvent('team.task.status_changed', event => {
    if (event.task_id === props.task.id && event.team_id === props.task.team_id) void load()
  }),
  onEvent('system.dropped', () => void load()),
  onEvent('system.hello', () => void load()),
  onEvent('project.opened', () => {
    // The app reloads on a project switch; invalidate pending responses immediately.
    alive = false
    generation++
    messages.value = []
    received = []
  }),
]

watch(() => [props.task.team_id, props.task.id], () => {
  generation++
  messages.value = []
  received = []
  atBottom.value = true
  void load()
}, { immediate: true })
watch(() => [messages.value, running.value], () => void follow())
onBeforeUnmount(() => {
  alive = false
  generation++
  unsubscribe.forEach(stop => stop())
})
</script>

<template>
  <article class="flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-line bg-surface"
    :aria-label="`${task.display_id} · ${task.title}`" data-task-conversation>
    <header class="shrink-0 border-b border-line px-3 py-3">
      <div class="mb-2 flex items-center gap-2">
        <span class="font-mono text-small text-faint">{{ task.display_id }}</span>
        <StatusBadge :status="task.status" class="min-w-0" />
        <RouterLink :to="{ path: '/tasks', query: { task: task.id } }"
          class="ml-auto grid size-7 shrink-0 place-items-center rounded-md hover:bg-hover"
          :title="t('producerPage.openTask')" :aria-label="t('producerPage.openTask')">
          <ArrowUpRight class="size-4" />
        </RouterLink>
      </div>
      <h3 class="line-clamp-2 font-medium leading-5" :title="task.title">{{ task.title }}</h3>
      <p v-if="member" class="mt-2 flex items-center gap-1.5 text-small text-muted">
        <MemberMark :member="member" variant="badge" :corner="false" />
        <span class="truncate">{{ member.name }}</span>
      </p>
    </header>

    <div v-if="error" class="shrink-0 border-b border-line bg-danger-soft px-3 py-2 text-small" role="alert">
      <p class="break-words text-danger">{{ error }}</p>
      <button class="mt-1 inline-flex items-center gap-1 rounded px-1 py-1 hover:bg-hover" :disabled="loading" @click="load">
        <RotateCw class="size-3" />{{ t('producerPage.retry') }}
      </button>
    </div>

    <div ref="scroller" class="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain px-3 py-3" @scroll.passive="measureBottom">
      <p v-if="loading && !messages.length" class="py-8 text-center text-small text-faint" role="status">{{ t('producerPage.loading') }}</p>
      <div v-else-if="!rows.length" class="flex h-full flex-col items-center justify-center gap-2 text-faint">
        <MessageSquare class="size-5" />
        <p class="text-small">{{ t('producerPage.noMessages') }}</p>
      </div>
      <template v-for="row in rows" :key="row.key">
        <p v-if="row.message.kind === 'tool_use'" class="flex min-w-0 items-baseline gap-1.5 text-small text-faint" :title="toolSubject(row.message)">
          <Wrench class="size-3 shrink-0 translate-y-0.5" />
          <span class="shrink-0 font-mono">{{ toolName(row.message) }}</span>
          <span class="truncate">{{ toolSubject(row.message) }}</span>
        </p>
        <p v-else-if="row.message.kind === 'status'" class="flex items-center gap-1.5 text-small text-faint">
          <span class="size-1.5 shrink-0 rounded-full" :style="{ backgroundColor: statusColor(String(row.message.data.to ?? task.status)) }" />
          {{ statusLabel(String(row.message.data.to ?? task.status)) }}
          <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
        </p>
        <p v-else-if="row.message.kind === 'file_observation_status' || row.message.kind === 'file_observation_error'"
          class="flex items-baseline gap-1.5 text-small"
          :class="row.message.kind === 'file_observation_error' ? 'text-warning' : 'text-faint'">
          <span class="min-w-0 break-words">{{ row.message.text }}</span>
          <span class="ml-auto shrink-0">{{ formatTime(row.message.created_at) }}</span>
        </p>
        <div v-else class="rounded-lg border px-3 py-2"
          :class="row.message.role === 'user' ? 'ml-5 border-accent-border bg-accent-soft' : 'border-line-subtle bg-elevated'"
          :style="summaryColor(row.message) ? {
            borderColor: summaryColor(row.message),
            boxShadow: `inset 3px 0 0 ${summaryColor(row.message)}`,
          } : undefined">
          <p class="mb-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-small text-faint">
            <span class="min-w-0 truncate font-medium">{{ row.message.role === 'user' ? t('taskDetail.you') : (member?.name ?? row.message.member_id) }}</span>
            <span v-if="['compressed summary', 'context_summary'].includes(row.message.kind)">{{ t('taskDetail.compression.summary') }}</span>
            <span v-if="summaryColor(row.message) && row.message.kind !== 'cancelled'" class="inline-flex items-center gap-1 font-medium"
              :style="{ color: summaryColor(row.message) }"
              :title="resultTypeOf(row.message) ? resultTypeHint(resultTypeOf(row.message)!) : undefined">
              <span class="size-1.5 rounded-full" :style="{ background: summaryColor(row.message) }" />
              {{ summaryLabel(row.message) }}
            </span>
            <span class="ml-auto">{{ formatTime(row.message.created_at) }}</span>
          </p>
          <MarkdownText v-if="PROSE_KINDS.has(row.message.kind)" :text="row.message.text" />
          <p v-else class="whitespace-pre-wrap break-words text-small">{{ row.message.text }}</p>
          <a v-for="attachment in attachmentsOf(row.message)" :key="attachment.name"
            :href="taskAttachmentUrl(task.team_id, task.id, attachment.name)" target="_blank" rel="noopener noreferrer"
            class="mt-2 block truncate text-small underline">{{ attachment.name }}</a>
        </div>
      </template>
    </div>

    <footer class="shrink-0 border-t border-line bg-inset/40 px-3 py-2">
      <p v-if="running" class="mb-2 flex min-w-0 items-center gap-2 text-small text-faint" role="status">
        <span class="vxs-chat-typing shrink-0" :aria-label="t('chat.answering')"><i /><i /><i /></span>
        <span class="truncate" :title="live.activity ?? undefined">{{ live.activity || t('chat.answeringNow') }}</span>
      </p>
      <div class="flex items-center justify-between gap-2 text-small">
        <span class="text-faint">{{ t('producerPage.taskCost') }}</span>
        <span class="font-mono tabular-nums">{{ cost }}</span>
      </div>
    </footer>
  </article>
</template>
