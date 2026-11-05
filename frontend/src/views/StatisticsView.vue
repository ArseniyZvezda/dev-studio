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
 * Two mirrored columns put tokens on the left and API Cost on the right, followed by
 * daily execution time. Cards render immediately; each metric owns its loading state.
 */
import { ChartLine } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { TeamStats } from '@/api/types'
import StatsMetricColumn from '@/components/stats/StatsMetricColumn.vue'
import CostChart from '@/components/stats/CostChart.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { t } from '@/i18n'
import { todayIso } from '@/lib/cost-chart'
import { useTeamsStore } from '@/stores/teams'

const teams = useTeamsStore()

const stats = ref<TeamStats | null>(null)
const loading = ref(false)
const initializing = ref(true)
const error = ref<string | null>(null)
const busy = computed(() => initializing.value || teams.loading || loading.value)
const durationSeries = computed(() => (stats.value?.average_task_duration_series ?? []).map(row => ({
  day: row.day, value: row.average_duration_ms, spent: 0, count: row.tasks,
})))

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
  const teamId = teams.team.id
  try {
    const result = await api.teamStats(teamId)
    if (teams.team?.id === teamId) stats.value = result
  } catch (cause) {
    if (teams.team?.id === teamId) error.value = cause instanceof ApiError ? cause.detail : String(cause)
  } finally {
    inFlight = false
    loading.value = false
  }
  if (again) {
    again = false
    await refresh()
  }
}

async function initialize(): Promise<void> {
  initializing.value = true
  await teams.load()
  initializing.value = false
  await refresh()
}

function retry(): void {
  if (teams.error || !teams.team) void initialize()
  else void refresh()
}

onMounted(() => {
  teams.subscribe()
  void initialize()
})

// The store replaces the tasks array on every refresh, which is exactly when the figures
// may have moved: a run landing its cost, an approval, a card archived or deleted.
watch(
  () => teams.tasks,
  () => void refresh(),
)
watch(() => teams.team?.id, () => {
  stats.value = null
  error.value = null
  void refresh()
})
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

    <div v-if="teams.error || error" class="flex items-center gap-3 bg-danger-soft px-pane py-2 text-danger" role="alert">
      <p class="min-w-0 flex-1 break-words">{{ teams.error || error }}</p>
      <button class="shrink-0 rounded px-2 py-1 text-small underline hover:bg-hover disabled:opacity-50" :disabled="busy" @click="retry">{{ t('statistics.retry') }}</button>
    </div>

    <EmptyState
      v-if="!teams.team && !busy && !teams.error"
      class="vxs-enter"
      :title="t('statistics.noTeam')"
      :hint="t('statistics.noTeamHint')"
    >
      <template #icon><ChartLine class="mx-auto mb-3 size-8 text-faint" /></template>
    </EmptyState>

    <div
      v-else
      class="@container p-pane"
    >
      <!-- §10.4: matching sections, tokens left and API Cost right; stack in narrow panes. -->
      <div class="grid grid-cols-1 gap-6 @4xl:grid-cols-2 @4xl:gap-x-0 @4xl:gap-y-5">
        <StatsMetricColumn :stats="stats" :today="today" :loading="busy" metric="tokens" class="@4xl:pr-5" />
        <StatsMetricColumn
          :stats="stats"
          :today="today"
          :loading="busy"
          metric="usd"
          class="border-t border-pane pt-6 @4xl:border-t-0 @4xl:border-l @4xl:pt-0 @4xl:pl-5"
        />
      </div>
      <section class="mt-6 rounded-lg border border-card bg-elevated p-pane shadow-card">
        <h2 class="mb-1 font-medium">{{ t('statistics.durationChart.title') }}</h2>
        <p class="mb-3 text-small text-faint">{{ t('statistics.durationChart.hint') }}</p>
        <CostChart :series="durationSeries" :today="today" metric="duration" mode="average" :loading="busy" :unavailable="!stats" />
      </section>
    </div>
  </div>
</template>
