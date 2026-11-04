<script setup lang="ts">
/** Fixed above the composer: transcript scrolling never hides the current work (§10.11). */
import { computed } from 'vue'
import { t } from '@/i18n'
import { producerChatTasks } from '@/lib/producer'
import { statusColor, statusLabel } from '@/lib/status'
import { useChatStore } from '@/stores/chat'
import { useTeamsStore } from '@/stores/teams'

const chat = useChatStore()
const teams = useTeamsStore()
const current = computed(() => chat.view === 'chat' ? chat.activeChat : null)
const team = computed(() => teams.teams.find(entry =>
  entry.members.some(member => member.id === current.value?.member_id)))
const groups = computed(() => current.value && team.value
  ? producerChatTasks(current.value, team.value.tasks, team.value.id)
  : { running: [], waiting: [] })
</script>

<template>
  <section v-if="current?.producer_mode" class="shrink-0 border-t border-line bg-inset/50 px-3 py-2"
    :aria-label="t('chat.producer.taskStatus')">
    <div class="grid grid-cols-2 gap-3">
      <div v-for="key in (['running', 'waiting'] as const)" :key="key" class="min-w-0">
        <h3 class="text-small font-medium">{{ t(`chat.producer.taskLists.${key}`, { count: groups[key].length }) }}</h3>
        <ul v-if="groups[key].length" class="mt-1 max-h-28 space-y-1 overflow-y-auto overscroll-contain">
          <li v-for="task in groups[key]" :key="task.id" class="text-small">
            <RouterLink :to="{ path: '/tasks', query: { task: task.id } }"
              class="flex min-w-0 items-center gap-1.5 rounded py-0.5 hover:bg-hover" :title="`${task.display_id} · ${task.title} · ${statusLabel(task.status)}`">
              <span class="size-1.5 shrink-0 rounded-full" :style="{ backgroundColor: statusColor(task.status) }" />
              <span class="shrink-0 font-mono text-faint">{{ task.display_id }}</span>
              <span class="truncate">{{ task.title }}</span>
            </RouterLink>
          </li>
        </ul>
        <p v-else class="mt-1 text-small text-faint">{{ t(`chat.producer.taskLists.${key}Empty`) }}</p>
      </div>
    </div>
  </section>
</template>
