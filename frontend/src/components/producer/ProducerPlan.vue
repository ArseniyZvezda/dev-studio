<script setup lang="ts">
/**
 * The durable session checklist stays above the conversation (§10.11).
 *
 * Under each item sit the tasks that have ever belonged to it — finished and cancelled ones
 * included, because the count is what makes the next task harder to justify. The dot is the
 * task's status in the board's own colours; the record arrives with the chat, so a task that
 * has left the board still shows the status it left in.
 */
import type { ProducerPlanItem, ProducerPlanItemTask } from '@/api/types'
import { t } from '@/i18n'
import { statusColor, statusLabel } from '@/lib/status'

defineProps<{ items: ProducerPlanItem[] }>()

function hint(task: ProducerPlanItemTask): string {
  return [task.display_id, task.title, statusLabel(task.status),
    task.reopens ? t('producerPage.planTaskReopened', { count: task.reopens }) : '']
    .filter(Boolean).join(' · ')
}
</script>

<template>
  <section class="flex min-h-0 min-w-0 flex-col border-b border-line" :aria-label="t('producerPage.plan')" data-producer-plan>
    <h2 class="shrink-0 px-4 py-3 font-medium">{{ t('producerPage.plan') }}</h2>
    <ul class="min-h-0 space-y-2 overflow-y-auto overscroll-contain px-4 pb-3">
      <li v-for="(item, index) in items" :key="item.id ?? index" class="flex items-start gap-2 text-small">
        <input type="checkbox" :checked="item.done" disabled :aria-label="item.text"
          class="mt-0.5 size-3.5 shrink-0 accent-accent disabled:opacity-100" />
        <div class="min-w-0">
          <span class="break-words" :class="{ 'text-muted line-through': item.done }">{{ item.text }}</span>
          <ul v-if="item.tasks?.length" class="mt-1 flex flex-wrap gap-x-3 gap-y-1"
            :aria-label="t('producerPage.planTasks', { item: item.text })" data-plan-tasks>
            <li v-for="task in item.tasks" :key="task.id">
              <RouterLink :to="{ path: '/tasks', query: { task: task.id } }" :title="hint(task)" :aria-label="hint(task)"
                class="flex items-center gap-1 rounded hover:bg-hover" data-plan-task>
                <span class="size-2 shrink-0 rounded-full" :style="{ backgroundColor: statusColor(task.status) }" />
                <span class="font-mono text-faint">{{ task.display_id }}</span>
                <span v-if="task.reopens" class="tabular-nums text-faint">↻{{ task.reopens }}</span>
              </RouterLink>
            </li>
          </ul>
        </div>
      </li>
    </ul>
  </section>
</template>
