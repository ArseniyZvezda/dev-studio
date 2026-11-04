<script setup lang="ts">
/**
 * The task board (spec §10.4) — everything about work in flight, on its own page.
 *
 * Split out of Teams, which now only edits the roster. The two were sharing one right
 * rail and one sidebar, which meant assigning work and configuring a member competed for
 * the same space; a board wants width, and an org chart wants a form beside it.
 *
 * Arriving with `?assign=<member id>` opens the composer for that member. The Teams page
 * used to link here from each node; that button is gone, because the roster is not where
 * work gets created. The entry point stays — "give this person something to do" is a
 * reasonable thing to land on, and nothing about it depended on the org chart.
 */
import { Archive, Play, Plus, Users } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import EmptyState from '@/components/ui/EmptyState.vue'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import TaskBoard from '@/components/teams/TaskBoard.vue'
import TaskComposerDialog from '@/components/teams/TaskComposerDialog.vue'
import TaskDetail from '@/components/teams/TaskDetail.vue'
import { t } from '@/i18n'
import { formatCost, statusLabel } from '@/lib/status'
import { useSessionStore } from '@/stores/session'
import { useTeamsStore } from '@/stores/teams'

const teams = useTeamsStore()
const session = useSessionStore()
const route = useRoute()
const router = useRouter()

/** Who the composer opens pointed at; null while it is closed. The dialog owns the form
 *  itself, and is mounted only while open, so reopening always starts from a blank one. */
const composerFor = ref<string | null>(null)

/**
 * Whether the Archived column is drawn (§10.4). Off on every visit: the archive exists so
 * finished work is *not* on the board, and remembering the switch would undo that on the
 * next reload for anyone who looked once.
 */
const showArchived = ref(false)

// The switch is rendered only while something is archived, so an archive emptied from
// its own column would otherwise leave a column on screen with no way to dismiss it.
watch(
  () => teams.archivedTasks.length,
  (count) => {
    if (!count) showArchived.value = false
  },
)

/** Surfaced from `GET /api/status` so an unusable backend is visible before a run is
 *  started rather than after it fails. */
const backendState = computed(() => {
  const backends = session.status?.agent_backends ?? {}
  return Object.entries(backends).map(([name, info]) => ({ name, ...info }))
})

onMounted(async () => {
  teams.subscribe()
  await teams.load()
  await session.refreshStatus()

  // Deep link from the org chart. Drop the query afterwards so a reload does not reopen
  // a composer the user already dismissed.
  const requested = route.query.assign
  if (typeof requested === 'string' && teams.memberById.has(requested)) {
    openComposer(requested)
    const { assign: _consumed, ...rest } = route.query
    void router.replace({ path: route.path, query: rest })
  }

  openRequestedTask()
})

/**
 * `?task=<id>` opens that task — where a notification about one leads (§6.5). Scrubbed like
 * `assign`, so a reload does not reopen a dialog the user closed. Watched as well as read on
 * mount: a notification clicked while the board is already open changes the query and mounts
 * nothing.
 */
function openRequestedTask(): void {
  const wanted = route.query.task
  if (typeof wanted !== 'string') return
  if (teams.tasks.some((task) => task.id === wanted)) void teams.selectTask(wanted)
  const { task: _opened, ...rest } = route.query
  void router.replace({ path: route.path, query: rest })
}

watch(() => route.query.task, openRequestedTask)

function openComposer(assigneeId: string) {
  composerFor.value = assigneeId
}

async function setAutostartTasks(event: Event) {
  const checkbox = event.target as HTMLInputElement
  await teams.setAutostartTasks(checkbox.checked)
  checkbox.checked = teams.team?.autostart_tasks ?? false
}
</script>

<template>
  <div class="flex h-full">
    <section class="flex min-w-0 flex-1 flex-col">
      <header class="vxs-pane flex shrink-0 flex-wrap items-center gap-2 border-b border-pane px-pane py-2">
        <span class="flex-1 truncate text-title font-medium">
          {{ t('tasks.title') }}
          <span v-if="teams.team" class="text-[length:var(--vxs-text-size)] text-faint">— {{ teams.team.name }}</span>
        </span>
        <!-- What the board has cost, over the board. Only Done counts, so the figure is
             about results rather than about work in flight — and archived tasks are in it,
             because archiving takes a card off the board and nothing off the books (§10.4). -->
        <span
          class="shrink-0 rounded-pill border border-card px-2.5 py-0.5 text-small text-faint"
          :title="t('tasks.header.doneCostHint')"
        >
          {{ statusLabel('done') }}
          <span class="ml-1 font-medium tabular-nums text-primary">
            {{ formatCost(teams.doneCost) }}
          </span>
        </span>
        <!-- The archive is hidden on purpose; this is the one way to see it. Rendered only
             once something is in it, so a board nobody has tidied carries no control for a
             column that would be empty. -->
        <button
          v-if="teams.archivedTasks.length"
          class="flex shrink-0 items-center gap-1 rounded-pill border px-2.5 py-0.5 text-small"
          :class="
            showArchived
              ? 'border-accent-border bg-accent-soft text-accent'
              : 'border-card text-faint hover:bg-hover hover:text-primary'
          "
          :title="showArchived ? t('tasks.header.hideArchived') : t('tasks.header.showArchived')"
          @click="showArchived = !showArchived"
        >
          <Archive class="size-3" />
          {{ t('tasks.archived') }}
          <span class="font-medium tabular-nums">{{ teams.archivedTasks.length }}</span>
        </button>
        <label
          class="flex shrink-0 cursor-pointer items-center gap-1.5 text-small text-secondary"
          :title="t('tasks.header.autostartHint')"
        >
          <input
            type="checkbox"
            class="accent-[var(--vxs-accent)] disabled:cursor-not-allowed"
            :checked="teams.team?.autostart_tasks ?? false"
            :disabled="!teams.team || teams.busy || teams.loading"
            @change="setAutostartTasks"
          />
          {{ t('tasks.header.autostart') }}
        </label>
        <button
          class="flex shrink-0 items-center gap-1.5 rounded border border-card px-3 py-1 hover:bg-hover disabled:opacity-50"
          :disabled="teams.busy || teams.loading || !teams.startableTasks.length"
          :title="t('tasks.header.startAllHint')"
          @click="teams.startAllTasks()"
        >
          <Play class="size-3.5" />
          {{ t('tasks.header.startAll') }}
        </button>
        <button
          class="flex shrink-0 items-center gap-1.5 vxs-btn-primary px-3 py-1 font-medium text-on-accent disabled:opacity-50"
          :disabled="!teams.members.length"
          :title="
            teams.members.length ? t('tasks.header.newTaskHint') : t('tasks.header.needsMembers')
          "
          @click="openComposer(teams.members[0]?.id ?? '')"
        >
          <Plus class="size-3.5" />
          {{ t('tasks.newTask') }}
        </button>
      </header>

      <p v-if="teams.error" class="shrink-0 bg-danger-soft px-pane py-1 text-danger">
        {{ teams.error }}
      </p>

      <div class="min-h-0 flex-1">
        <!-- The board's columns are the page's bands (`TaskBoard`); an empty board is one. -->
        <EmptyState
          v-if="!teams.tasks.length && !teams.loading"
          class="vxs-enter"
          :title="t('tasks.empty.title')"
          :hint="t('tasks.empty.hint')"
        >
          <template #icon><Users class="mx-auto mb-3 size-8 text-faint" /></template>
        </EmptyState>
        <TaskBoard
          v-else
          :tasks="teams.tasks"
          :selected-id="teams.openTask?.id ?? null"
          :show-archived="showArchived"
          @select="teams.selectTask($event)"
        />
      </div>

      <div
        v-for="backend in backendState"
        :key="backend.name"
        class="shrink-0 border-t border-line-subtle px-pane py-1.5 text-small"
        :class="backend.available ? 'text-faint' : 'text-warning'"
      >
        {{ backend.name }}: {{ backend.reason }}
      </div>
    </section>

    <TaskComposerDialog
      v-if="composerFor !== null"
      :assignee-id="composerFor"
      @close="composerFor = null"
    />

    <!-- A set height, not one that hugs the content: a transcript is what this dialog is
         mostly made of, and a panel that grew with the number of messages would move its own
         footer buttons out from under the cursor. 95vh, because a transcript is also what
         wants every line it can get — while the width stays fixed, which is why there is no
         fill-the-window mode any more: the column cannot use the extra width, so expanding
         only framed the same content in empty panel (§10.4). `max-h-full` takes the height
         back down on a short window — a *height* rather than a second `max-height`, or the
         two would fight and whichever Tailwind emitted last would win.

         74rem + 10px, not 56rem: the changed-files column takes 298px of it, and the
         transcript keeps the 896px it reads at. `max-w-full` gives that back on a narrow
         window, where the column is what gives way. -->
    <ModalDialog
      v-if="teams.openTask"
      :label="t('tasks.dialogLabel', { id: teams.openTask.display_id })"
      panel-class="h-[95vh] w-[calc(74rem+10px)]"
      @close="teams.selectTask(null)"
    >
      <TaskDetail :task="teams.openTask" @close="teams.selectTask(null)" />
    </ModalDialog>
  </div>
</template>
