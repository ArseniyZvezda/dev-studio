<script setup lang="ts">
/** Each card feeds the shared task panel without selecting the Tasks page's openTask (§10.11). */
import { ArrowUpRight, RotateCw } from 'lucide-vue-next'
import { onBeforeUnmount, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { AgentMessage, TaskDetail, TeamTask } from '@/api/types'
import TaskDetailPanel from '@/components/teams/TaskDetail.vue'
import { t } from '@/i18n'
import { mergeTaskConversation } from '@/lib/task-conversation'
import { onEvent } from '@/ws/connection'

const props = defineProps<{ task: TeamTask }>()
const panel = ref<TaskDetail | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
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
    panel.value = { ...detail, messages: mergeTaskConversation(detail.messages, received) }
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
  if (panel.value) panel.value.messages = mergeTaskConversation(panel.value.messages, [message])
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
    panel.value = null
    received = []
  }),
]

watch([() => props.task.team_id, () => props.task.id], () => {
  generation++
  panel.value = null
  received = []
  void load()
}, { immediate: true })
onBeforeUnmount(() => {
  alive = false
  generation++
  unsubscribe.forEach(stop => stop())
})
</script>

<template>
  <article class="flex h-full min-h-0 flex-col overflow-hidden" :aria-label="`${task.display_id} · ${task.title}`" data-task-conversation>
    <div v-if="error" class="shrink-0 border-b border-line bg-danger-soft px-3 py-2 text-small" role="alert">
      <p class="break-words text-danger">{{ error }}</p>
      <button class="mt-1 inline-flex items-center gap-1 rounded px-1 py-1 hover:bg-hover" :disabled="loading" @click="load">
        <RotateCw class="size-3" />{{ t('producerPage.retry') }}
      </button>
    </div>
    <TaskDetailPanel v-if="panel" :task="panel" embedded>
      <template #header-action>
        <RouterLink :to="{ path: '/tasks', query: { task: task.id } }"
          class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('producerPage.openTask')" :aria-label="t('producerPage.openTask')">
          <ArrowUpRight class="size-3.5" />
        </RouterLink>
      </template>
    </TaskDetailPanel>
    <p v-else-if="loading" class="p-3 text-small text-faint" role="status">{{ t('producerPage.loading') }}</p>
  </article>
</template>
