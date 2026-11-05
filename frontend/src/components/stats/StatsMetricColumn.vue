<script setup lang="ts">
/** Mirrored token / API Cost statistics, with one shared layout (§10.4). */
import { computed, ref, watch } from 'vue'

import type { TeamStats } from '@/api/types'
import ActivityHeatmap from '@/components/stats/ActivityHeatmap.vue'
import CostChart from '@/components/stats/CostChart.vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import { formatNumber, t } from '@/i18n'
import { formatMetricValue, type Metric, type MetricPoint } from '@/lib/cost-chart'
import { formatTokens } from '@/lib/status'

const props = defineProps<{ stats: TeamStats; today: string; metric: Metric }>()
const isTokens = computed(() => props.metric === 'tokens')
const heading = computed(() => t(isTokens.value ? 'statistics.metrics.tokens' : 'statistics.metrics.usd'))
const format = (value: number) => formatMetricValue(value, props.metric)
type TokenKind = 'uncached' | 'cached' | 'unclassified'
const tokenKind = ref<TokenKind>('uncached')
const tokenKinds = computed<TokenKind[]>(() => props.stats.totals.spent_unclassified_tokens > 0
  ? ['uncached', 'cached', 'unclassified'] : ['uncached', 'cached'])
const tokenOptions = computed(() => tokenKinds.value.map(value => ({
  value, label: t(`tokenUsage.${value}Short`),
})))
const tokenLabel = computed(() => t(`tokenUsage.${tokenKind.value}`))
watch(tokenKinds, kinds => { if (!kinds.includes(tokenKind.value)) tokenKind.value = 'uncached' })
const activity = computed(() => isTokens.value ? props.stats.activity.map(row => ({
  ...row, spent_tokens: row[`spent_${tokenKind.value}_tokens`],
})) : props.stats.activity)

const tiles = computed(() => {
  const totals = props.stats.totals
  const value = (name: 'spent' | 'done' | 'in_flight' | 'cancelled') =>
    isTokens.value ? totals[`${name}_${tokenKind.value}_tokens`] : totals[`${name}_usd`]
  return [
    { label: t('statistics.tiles.spent'), value: format(value('spent')), note: t('statistics.tiles.spentNote') },
    {
      label: t('statistics.tiles.finished'), value: format(value('done')),
      note: totals.archived
        ? t('statistics.tiles.finishedNoteArchived', { count: totals.done, archived: totals.archived })
        : t('statistics.tiles.finishedNote', { count: totals.done }),
    },
    { label: t('statistics.tiles.inFlight'), value: format(value('in_flight')), note: t('statistics.tiles.inFlightNote') },
    {
      label: t('statistics.tiles.tasks'), value: formatNumber(totals.tasks),
      note: value('cancelled')
        ? t('statistics.tiles.cancelledNote', { cost: isTokens.value ? t('statistics.tokens', { count: value('cancelled'), value: formatTokens(value('cancelled')) }) : format(value('cancelled')) })
        : t('statistics.tiles.nothingCancelled'),
    },
  ]
})

const series = computed<MetricPoint[]>(() => isTokens.value
  ? props.stats.token_series.map(row => ({ day: row.day, value: row[`cumulative_${tokenKind.value}_tokens`], spent: row[`spent_${tokenKind.value}_tokens`], count: row.runs }))
  : props.stats.series.map(row => ({ day: row.day, value: row.cumulative_usd, spent: row.spent_usd, count: row.runs })))

const averages = computed<MetricPoint[]>(() => props.stats.average_task_series.map(row => ({
  day: row.day,
  value: isTokens.value ? row[`average_${tokenKind.value}_tokens`] : row.average_usd,
  spent: 0,
  count: row.tasks,
})))

const members = computed(() => props.stats.by_member.map(row => ({
  ...row, value: isTokens.value ? row[`spent_${tokenKind.value}_tokens`] : row.spent_usd,
})).sort((a, b) => b.value - a.value || a.name.localeCompare(b.name)))

const models = computed(() => (isTokens.value
  ? props.stats.by_model_tokens.map(row => ({ model: row.model, value: row[`spent_${tokenKind.value}_tokens`] }))
  : props.stats.by_model.map(row => ({ model: row.model, value: row.spent_usd })))
  .sort((a, b) => b.value - a.value || (a.model ?? '').localeCompare(b.model ?? '')))

function share(value: number, maximum: number): string {
  return maximum > 0 ? `${Math.min(100, (value / maximum) * 100)}%` : '0%'
}
</script>

<template>
  <section class="grid min-w-0 grid-cols-1 gap-5 @4xl:row-span-7 @4xl:grid-rows-subgrid" :aria-label="heading">
    <header>
      <h2 class="text-title font-semibold text-primary">{{ heading }}</h2>
      <template v-if="isTokens">
        <dl class="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-small">
          <div v-for="kind in tokenKinds" :key="kind" :title="t(`tokenUsage.${kind}Hint`)">
            <dt class="text-faint">{{ t(`tokenUsage.${kind}`) }}</dt>
            <dd class="mt-0.5 font-semibold text-primary tabular-nums">{{ formatTokens(stats.totals[`spent_${kind}_tokens`]) }}</dd>
          </div>
        </dl>
        <div class="mt-3 overflow-x-auto">
          <SegmentedControl v-model="tokenKind" :options="tokenOptions" :aria-label="t('tokenUsage.chartMetric')" />
        </div>
        <p class="mt-2 text-small text-faint">{{ t(`tokenUsage.${tokenKind}Hint`) }}</p>
      </template>
    </header>

    <div class="vxs-enter grid grid-cols-2 gap-3">
      <div v-for="tile in tiles" :key="tile.label" class="min-w-0 rounded-lg border border-card bg-elevated p-3 shadow-card">
        <p class="text-small text-faint">{{ tile.label }}</p>
        <p class="mt-1 break-words text-xl font-semibold text-primary">{{ tile.value }}</p>
        <p class="mt-0.5 text-small text-faint">{{ tile.note }}</p>
      </div>
    </div>

    <section class="vxs-enter flex flex-col rounded-lg border border-card bg-elevated p-pane shadow-card">
      <h3 class="mb-1 font-medium">{{ t('statistics.activity.title') }}</h3>
      <p class="mb-3 text-small text-faint">{{ t('statistics.activity.hint') }}</p>
      <ActivityHeatmap :activity="activity" :today="today" :metric="metric" class="mt-auto" />
    </section>

    <section class="vxs-enter flex flex-col rounded-lg border border-card bg-elevated p-pane shadow-card">
      <h3 class="mb-1 font-medium">{{ t(isTokens ? 'statistics.tokensChart.title' : 'statistics.chart.title') }}<span v-if="isTokens" class="text-faint"> — {{ tokenLabel }}</span></h3>
      <p class="mb-3 text-small text-faint">{{ t(isTokens ? 'statistics.tokensChart.hint' : 'statistics.chart.hint') }}</p>
      <CostChart :series="series" :today="today" :metric="metric" :metric-label="isTokens ? tokenLabel : undefined" mode="cumulative" class="mt-auto" />
    </section>

    <section class="vxs-enter flex flex-col rounded-lg border border-card bg-elevated p-pane shadow-card">
      <h3 class="mb-1 font-medium">{{ t(isTokens ? 'statistics.averageChart.tokensTitle' : 'statistics.averageChart.costTitle') }}<span v-if="isTokens" class="text-faint"> — {{ tokenLabel }}</span></h3>
      <p class="mb-3 text-small text-faint">{{ t(isTokens ? 'statistics.averageChart.tokensHint' : 'statistics.averageChart.costHint') }}</p>
      <CostChart :series="averages" :today="today" :metric="metric" :metric-label="isTokens ? tokenLabel : undefined" mode="average" class="mt-auto" />
    </section>

    <section class="vxs-enter rounded-lg border border-card bg-elevated p-pane shadow-card">
      <h3 class="mb-1 font-medium">{{ t('statistics.byMember.title') }}</h3>
      <p class="mb-3 text-small text-faint">{{ t(isTokens ? 'statistics.byMember.tokensHint' : 'statistics.byMember.hint') }}</p>
      <p v-if="!members.length" class="text-faint">{{ t('statistics.byMember.noTasks') }}</p>
      <div v-else class="space-y-3">
        <div v-for="row in members" :key="row.member_id" class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1">
          <p class="flex min-w-0 items-center gap-1.5">
            <MemberMark :member="row" />
            <span class="truncate" :title="row.name">{{ row.name }}</span>
            <span v-if="row.kind === 'human'" class="shrink-0 text-small text-faint">{{ t('statistics.byMember.person') }}</span>
          </p>
          <p class="text-right text-small font-medium text-primary tabular-nums">{{ format(row.value) }}</p>
          <div class="h-2 rounded-pill bg-inset" :title="t('statistics.byMember.finishedTitle', { done: row.done, count: row.tasks })">
            <div class="h-full rounded-pill bg-accent" :style="{ width: share(row.value, members[0]?.value ?? 0) }" />
          </div>
          <p class="text-right text-small text-faint">{{ t('statistics.byMember.doneRatio', { done: row.done, tasks: row.tasks }) }}</p>
        </div>
      </div>
    </section>

    <section class="vxs-enter rounded-lg border border-card bg-elevated p-pane shadow-card">
      <h3 class="mb-1 font-medium">{{ t('statistics.byModel.title') }}</h3>
      <p class="mb-3 text-small text-faint">{{ t(isTokens ? 'statistics.byModel.tokensHint' : 'statistics.byModel.hint') }}</p>
      <p v-if="!models.length" class="text-faint">{{ t('statistics.byModel.empty') }}</p>
      <div v-else class="space-y-3">
        <div v-for="row in models" :key="row.model ?? ''" class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1">
          <p class="truncate" :title="row.model ?? t('statistics.byModel.unknown')">{{ row.model ?? t('statistics.byModel.unknown') }}</p>
          <p class="text-right text-small font-medium text-primary tabular-nums">{{ format(row.value) }}</p>
          <div class="col-span-2 h-2 rounded-pill bg-inset">
            <div class="h-full rounded-pill bg-accent" :style="{ width: share(row.value, models[0]?.value ?? 0) }" />
          </div>
        </div>
      </div>
    </section>
  </section>
</template>
