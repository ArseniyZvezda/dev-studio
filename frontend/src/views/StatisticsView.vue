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
 * Two mirrored columns put tokens on the left and API Cost on the right.
 * Figures refresh with the board while retaining the previous render during a fetch.
 */
import { ChartLine } from 'lucide-vue-next'
import { onMounted, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { TeamStats } from '@/api/types'
import StatsMetricColumn from '@/components/stats/StatsMetricColumn.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { t } from '@/i18n'
import { todayIso } from '@/lib/cost-chart'
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
      class="@container p-pane transition-opacity"
      :class="loading ? 'opacity-60' : ''"
    >
      <!-- §10.4: matching sections, tokens left and API Cost right; stack in narrow panes. -->
      <div class="grid grid-cols-1 gap-6 @4xl:grid-cols-2 @4xl:gap-x-0 @4xl:gap-y-5">
        <StatsMetricColumn :stats="stats" :today="today" metric="tokens" class="@4xl:pr-5" />
        <StatsMetricColumn
          :stats="stats"
          :today="today"
          metric="usd"
          class="border-t border-pane pt-6 @4xl:border-t-0 @4xl:border-l @4xl:pt-0 @4xl:pl-5"
        />
      </div>
    </div>

    <div v-else-if="loading" class="p-pane"><LoadingBars /></div>
  </div>
</template>
