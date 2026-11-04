<script setup lang="ts">
/**
 * Statistics (spec §6.1, §10.4) — what the team's work has cost.
 *
 * Every task counts, archived ones included: archiving takes a card off the board and
 * nothing off the books, and a page that summed only what the board shows would tell the
 * user the project got cheaper the day they tidied it. The figures come from
 * `GET /api/teams/{id}/stats`, which builds the time series from the runs themselves
 * rather than from the tasks — a card resumed across three days is spent on three days.
 *
 * Four tiles, a heatmap of the days the team was working, one smooth chart,
 * and the same money by member and model. Everything is re-read whenever the board changes, through
 * the same `team.*` events the board follows, and the previous render is held at reduced
 * opacity while the new one is fetched — no skeleton, no layout jump. The skeleton is for the
 * first read alone, before there is anything to hold.
 */
import { ChartLine } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { TeamStats } from '@/api/types'
import ActivityHeatmap from '@/components/stats/ActivityHeatmap.vue'
import CostChart from '@/components/stats/CostChart.vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { t } from '@/i18n'
import { todayIso } from '@/lib/cost-chart'
import { formatCost } from '@/lib/status'
import { useTeamsStore } from '@/stores/teams'

const teams = useTeamsStore()

const stats = ref<TeamStats | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const today = todayIso()

/**
 * One fetch at a time. A board event can arrive while a read is in flight — a status
 * change fires two of them — and two answers landing out of order would leave the page
 * on the older one, so a request made mid-read is remembered and run once, afterwards.
 */
let inFlight = false
let again = false

async function refresh(): Promise<void> {
  if (!teams.team) {
    stats.value = null
    return
  }
  if (inFlight) {
    again = true
    return
  }
  inFlight = true
  loading.value = true
  error.value = null
  try {
    stats.value = await api.teamStats(teams.team.id)
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.detail : String(cause)
  } finally {
    inFlight = false
    loading.value = false
  }
  if (again) {
    again = false
    await refresh()
  }
}

onMounted(async () => {
  teams.subscribe()
  await teams.load()
  await refresh()
})

// The store replaces the tasks array on every refresh, which is exactly when the figures
// may have moved: a run landing its cost, an approval, a card archived or deleted.
watch(
  () => teams.tasks,
  () => void refresh(),
)

const totals = computed(() => stats.value?.totals ?? null)

/** The headline figures. Proportional digits, as a standalone number wants. */
const tiles = computed(() => {
  const figures = totals.value
  if (!figures) return []
  return [
    {
      label: t('statistics.tiles.spent'),
      value: formatCost(figures.spent_usd),
      note: t('statistics.tiles.spentNote'),
      title: t('statistics.tiles.spentTitle'),
    },
    {
      label: t('statistics.tiles.finished'),
      value: formatCost(figures.done_usd),
      note: figures.archived
        ? t('statistics.tiles.finishedNoteArchived', {
            count: figures.done,
            archived: figures.archived,
          })
        : t('statistics.tiles.finishedNote', { count: figures.done }),
      title: t('statistics.tiles.finishedTitle'),
    },
    {
      label: t('statistics.tiles.inFlight'),
      value: formatCost(figures.in_flight_usd),
      note: t('statistics.tiles.inFlightNote'),
      title: t('statistics.tiles.inFlightTitle'),
    },
    {
      label: t('statistics.tiles.tasks'),
      value: `${figures.tasks}`,
      note: figures.cancelled_usd
        ? t('statistics.tiles.cancelledNote', { cost: formatCost(figures.cancelled_usd) })
        : t('statistics.tiles.nothingCancelled'),
      title: t('statistics.tiles.tasksTitle'),
    },
  ]
})

const mostSpent = computed(() =>
  Math.max(0, ...(stats.value?.by_member.map((row) => row.spent_usd) ?? [0])),
)

const mostModelSpent = computed(() =>
  Math.max(0, ...(stats.value?.by_model.map((row) => row.spent_usd) ?? [0])),
)

function share(spent: number): string {
  return mostSpent.value ? `${Math.round((spent / mostSpent.value) * 100)}%` : '0%'
}
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="vxs-pane border-b border-pane bg-surface px-pane py-2">
      <h1 class="text-title font-medium">
        {{ t('statistics.title') }}
        <span v-if="teams.team" class="text-[length:var(--vxs-text-size)] text-faint">
          — {{ teams.team.name }}
        </span>
      </h1>
      <p class="truncate text-small text-faint">
        {{ t('statistics.subtitle') }}
      </p>
    </header>

    <p v-if="teams.error" class="bg-danger-soft px-pane py-1 text-danger">{{ teams.error }}</p>
    <p v-if="error" class="bg-danger-soft px-pane py-1 text-danger">{{ error }}</p>

    <EmptyState
      v-if="!teams.team && !teams.loading"
      class="vxs-enter"
      :title="t('statistics.noTeam')"
      :hint="t('statistics.noTeamHint')"
    >
      <template #icon><ChartLine class="mx-auto mb-3 size-8 text-faint" /></template>
    </EmptyState>

    <div
      v-else-if="stats"
      class="max-w-5xl space-y-6 p-pane transition-opacity"
      :class="loading ? 'opacity-60' : ''"
    >
      <!-- The headline numbers. A row of tiles rather than a chart: each is one value. Each
           section below is one of the page's bands (`vxs-enter`), arriving top-down. -->
      <section class="vxs-enter grid grid-cols-2 gap-3 md:grid-cols-4">
        <div
          v-for="tile in tiles"
          :key="tile.label"
          class="rounded-lg border border-card bg-elevated p-3 shadow-card"
          :title="tile.title"
        >
          <p class="text-small text-faint">{{ tile.label }}</p>
          <p class="mt-1 text-xl font-semibold text-primary">{{ tile.value }}</p>
          <!-- Wraps rather than truncates: the note is what says which tasks the figure counts,
               and cut to "295 tasks in Done, 295 ar…" it says nothing. -->
          <p class="mt-0.5 text-small text-faint">{{ tile.note }}</p>
        </div>
      </section>

      <!-- When the work happened: a day per cell, a week per column, ending today. -->
      <section class="vxs-enter rounded-lg border border-card bg-elevated p-pane shadow-card">
        <h2 class="mb-1 font-medium">{{ t('statistics.activity.title') }}</h2>
        <p class="mb-3 text-small text-faint">
          {{ t('statistics.activity.hint') }}
        </p>
        <ActivityHeatmap :activity="stats.activity" :today="today" />
      </section>

      <section class="vxs-enter rounded-lg border border-card bg-elevated p-pane shadow-card">
        <div class="mb-1 flex flex-wrap items-center gap-2">
          <h2 class="font-medium">{{ t('statistics.chart.title') }}</h2>
        </div>
        <p class="mb-3 text-small text-faint">
          {{ t('statistics.chart.hint') }}
        </p>

        <CostChart :series="stats.series" :today="today" />
      </section>

      <!-- The same money by member. Bars in one hue: the rows are people, not a scale. -->
      <section class="vxs-enter rounded-lg border border-card bg-elevated p-pane shadow-card">
        <h2 class="mb-1 font-medium">{{ t('statistics.byMember.title') }}</h2>
        <p class="mb-3 text-small text-faint">
          {{ t('statistics.byMember.hint') }}
        </p>
        <p v-if="!stats.by_member.length" class="text-faint">{{ t('statistics.byMember.noTasks') }}</p>
        <div v-else class="space-y-2">
          <div
            v-for="row in stats.by_member"
            :key="row.member_id"
            class="grid grid-cols-[minmax(0,12rem)_1fr_auto] items-center gap-3"
          >
            <p class="flex min-w-0 items-center gap-1.5">
              <MemberMark :member="row" />
              <span class="truncate">{{ row.name }}</span>
              <span v-if="row.kind === 'human'" class="shrink-0 text-small text-faint">
                {{ t('statistics.byMember.person') }}
              </span>
            </p>
            <div
              class="h-2 rounded-pill bg-inset"
              :title="t('statistics.byMember.finishedTitle', { done: row.done, count: row.tasks })"
            >
              <div
                class="h-full rounded-pill bg-accent"
                :style="{ width: share(row.spent_usd) }"
              />
            </div>
            <p class="text-right text-small tabular-nums">
              <span class="font-medium text-primary">{{ formatCost(row.spent_usd) }}</span>
              <span class="ml-2 text-faint">
                {{ t('statistics.byMember.doneRatio', { done: row.done, tasks: row.tasks }) }}
              </span>
            </p>
          </div>
        </div>
      </section>
      <section class="vxs-enter rounded-lg border border-card bg-elevated p-pane shadow-card">
        <h2 class="mb-1 font-medium">{{ t('statistics.byModel.title') }}</h2>
        <p class="mb-3 text-small text-faint">{{ t('statistics.byModel.hint') }}</p>
        <p v-if="!stats.by_model.length" class="text-faint">{{ t('statistics.byModel.empty') }}</p>
        <div v-else class="space-y-2">
          <div
            v-for="row in stats.by_model"
            :key="row.model ?? ''"
            class="grid grid-cols-[minmax(0,12rem)_1fr_auto] items-center gap-3"
          >
            <p class="truncate" :title="row.model ?? t('statistics.byModel.unknown')">
              {{ row.model ?? t('statistics.byModel.unknown') }}
            </p>
            <div class="h-2 rounded-pill bg-inset">
              <div
                class="h-full rounded-pill bg-accent"
                :style="{ width: mostModelSpent ? `${(row.spent_usd / mostModelSpent) * 100}%` : '0%' }"
              />
            </div>
            <p class="text-right text-small font-medium text-primary tabular-nums">
              {{ formatCost(row.spent_usd) }}
            </p>
          </div>
        </div>
      </section>
    </div>

    <div v-else-if="loading" class="p-pane"><LoadingBars /></div>
  </div>
</template>
