<script setup lang="ts">
/**
 * Token and API cost totals or daily task averages over recorded UTC days (§10.4).
 *
 * One series, so one colour — the accent — and no legend: the section title names what is
 * plotted. The line is 2px, the area under it a wash of the same hue, the markers ringed in
 * the surface colour so they read where the line is steep, and the last value is the only
 * one labelled on the plot; the rest are in the keyboard-accessible tooltip.
 * Grid and axes are hairlines one step off the surface, and every word on the plot wears a
 * text token rather than the series colour.
 *
 * Hover finds the nearest day by x — a reader aims at a date, never at a 2px line — and the
 * keyboard walks the same points, so nothing here is reachable by pointer alone. Drawn as
 * plain SVG, shared by both resources and both aggregation modes.
 */
import { useElementSize } from '@vueuse/core'
import { LoaderCircle } from 'lucide-vue-next'
import { computed, ref } from 'vue'

import { formatNumber, t } from '@/i18n'
import {
  dayDomain,
  dayMs,
  dayTicks,
  formatMetricValue,
  formatDay,
  niceTicks,
  metricLinePath,
  type Metric,
  type MetricPoint,
  type StatisticsChartMode,
} from '@/lib/cost-chart'

const props = defineProps<{
  series: MetricPoint[]
  /** ISO date the x axis runs to. A prop rather than `new Date()` so a render is a function of its inputs. */
  today: string
  metric: Metric
  mode: StatisticsChartMode
  metricLabel?: string
  loading?: boolean
  unavailable?: boolean
}>()

const container = ref<HTMLElement | null>(null)
const svg = ref<SVGSVGElement | null>(null)
const { width: measured } = useElementSize(container)

const HEIGHT = 240
// Right margin holds the end label; left holds the axis ticks; bottom the day labels.
const MARGIN = { top: 16, right: 88, bottom: 28, left: 76 }

const width = computed(() => Math.max(280, Math.round(measured.value || 640)))
const plotWidth = computed(() => width.value - MARGIN.left - MARGIN.right)
const plotHeight = HEIGHT - MARGIN.top - MARGIN.bottom
const baseline = MARGIN.top + plotHeight

const domain = computed(() => dayDomain(props.series, props.today))
const yTicks = computed(() => niceTicks(Math.max(0, ...props.series.map((point) => point.value))))
const yMax = computed(() => yTicks.value[yTicks.value.length - 1] || 1)

function x(ms: number): number {
  const span = domain.value.end - domain.value.start || 1
  return MARGIN.left + ((ms - domain.value.start) / span) * plotWidth.value
}

function y(value: number): number {
  return MARGIN.top + plotHeight - (value / yMax.value) * plotHeight
}

const points = computed(() =>
  props.series.map((entry) => ({
    ...entry,
    ms: dayMs(entry.day),
    px: x(dayMs(entry.day)),
    py: y(entry.value),
  })),
)

const last = computed(() => points.value[points.value.length - 1] ?? null)

/** Daily averages stop at the last observation; cumulative totals keep their flat tail. */
const linePath = computed(() => metricLinePath(points.value, x(domain.value.end), props.mode))

const areaPath = computed(() => {
  if (!linePath.value || !points.value.length) return ''
  const end = props.mode === 'cumulative' ? x(domain.value.end) : last.value!.px
  const first = points.value[0]!.px
  return `${linePath.value} L${end.toFixed(1)} ${baseline} L${first.toFixed(1)} ${baseline} Z`
})

const xTicks = computed(() =>
  dayTicks(domain.value.start, domain.value.end, Math.max(2, Math.floor(plotWidth.value / 78))),
)

const messagePrefix = computed(() => props.metric === 'duration'
  ? 'statistics.durationChart'
  : props.mode === 'average'
  ? 'statistics.averageChart'
  : props.metric === 'tokens' ? 'statistics.tokensChart' : 'statistics.chart')
const metricLabel = computed(() => props.metricLabel ?? t(`statistics.metrics.${props.metric}`))

function formatValue(value: number): string {
  return formatMetricValue(value, props.metric)
}

function formatPlotValue(value: number): string {
  return formatMetricValue(value, props.metric, true)
}

const summary = computed(() => {
  if (!last.value) return t(`${messagePrefix.value}.summaryEmpty`, { metric: metricLabel.value })
  return t(`${messagePrefix.value}.summary`, {
    count: points.value.length,
    total: formatValue(last.value.value),
    value: formatValue(last.value.value),
    metric: metricLabel.value,
    from: formatDay(points.value[0]!.ms),
    to: formatDay(last.value.ms),
  })
})

// ---------------------------------------------------------------- hover and keyboard

const active = ref<number | null>(null)
const activePoint = computed(() => (active.value === null ? null : points.value[active.value] ?? null))

/** The nearest data day to the pointer, by x — the crosshair finds the day. */
function locate(event: PointerEvent): void {
  const element = svg.value
  if (!element || !points.value.length) return
  const rect = element.getBoundingClientRect()
  const px = ((event.clientX - rect.left) / (rect.width || 1)) * width.value
  let nearest = 0
  let distance = Infinity
  points.value.forEach((point, index) => {
    const gap = Math.abs(point.px - px)
    if (gap < distance) {
      distance = gap
      nearest = index
    }
  })
  active.value = nearest
}

function step(delta: number): void {
  if (!points.value.length) return
  const current = active.value ?? points.value.length - 1
  active.value = Math.min(points.value.length - 1, Math.max(0, current + delta))
}

/**
 * Where the tooltip sits: above the point, centred, and pulled in from either edge so it
 * never leaves the card. Values in the tooltip lead and the labels follow — the reader has
 * the day already and wants the number.
 */
const tooltipStyle = computed(() => {
  const point = activePoint.value
  if (!point) return {}
  const left = Math.min(width.value - 108, Math.max(108, point.px))
  return { left: `${left}px`, top: `${point.py}px` }
})
</script>

<template>
  <div ref="container" class="relative w-full" :aria-busy="loading">
    <div v-if="loading && !series.length" class="grid h-60 place-items-center rounded-md bg-inset/40" role="status">
      <LoaderCircle class="size-5 animate-spin text-faint" aria-hidden="true" />
      <span class="sr-only">{{ t('common.loading') }}</span>
    </div>
    <p
      v-else-if="!series.length"
      class="grid place-items-center text-faint"
      :style="{ height: `${HEIGHT}px` }"
    >
      {{ t(unavailable ? 'statistics.unavailable' : `${messagePrefix}.empty`) }}
    </p>

    <svg
      v-else
      ref="svg"
      :viewBox="`0 0 ${width} ${HEIGHT}`"
      :height="HEIGHT"
      width="100%"
      class="block cursor-crosshair select-none outline-none focus-visible:ring-1 focus-visible:ring-accent"
      role="img"
      :aria-label="summary"
      tabindex="0"
      @pointermove="locate"
      @pointerleave="active = null"
      @focus="step(0)"
      @blur="active = null"
      @keydown.left.prevent="step(-1)"
      @keydown.right.prevent="step(1)"
    >
      <!-- Grid: one hairline per y tick, the baseline included. Solid, never dashed. -->
      <g class="stroke-line-subtle" stroke-width="1">
        <line
          v-for="tick in yTicks"
          :key="tick"
          :x1="MARGIN.left"
          :x2="width - MARGIN.right"
          :y1="y(tick)"
          :y2="y(tick)"
        />
      </g>

      <!-- Axis labels wear text tokens; the numbers align on tabular figures. -->
      <g class="fill-faint text-small tabular-nums">
        <text
          v-for="tick in yTicks"
          :key="tick"
          :x="MARGIN.left - 8"
          :y="y(tick) + 3.5"
          text-anchor="end"
        >
          {{ formatPlotValue(tick) }}
        </text>
        <text
          v-for="tick in xTicks"
          :key="tick"
          :x="x(tick)"
          :y="HEIGHT - 8"
          text-anchor="middle"
        >
          {{ formatDay(tick) }}
        </text>
      </g>

      <!-- The series: a wash under a 2px line, markers ringed in the surface colour. -->
      <path :d="areaPath" class="fill-accent opacity-10" />
      <path
        :d="linePath"
        class="stroke-accent"
        fill="none"
        stroke-width="2"
        stroke-linejoin="round"
        stroke-linecap="round"
      />

      <!-- The crosshair, on the active day only. -->
      <line
        v-if="activePoint"
        :x1="activePoint.px"
        :x2="activePoint.px"
        :y1="MARGIN.top"
        :y2="baseline"
        class="stroke-line-strong"
        stroke-width="1"
      />

      <g class="fill-accent stroke-elevated" stroke-width="2">
        <circle
          v-for="(point, index) in points"
          :key="point.day"
          :cx="point.px"
          :cy="point.py"
          :r="active === index ? 5 : 4"
        />
      </g>

      <!-- The one value labelled on the plot: where the curve is now. -->
      <text
        v-if="last"
        :x="Math.min(last.px, width - MARGIN.right) + 10"
        :y="last.py + 3.5"
        class="fill-primary text-small font-medium tabular-nums"
      >
        {{ formatPlotValue(last.value) }}
      </text>
    </svg>

    <span v-if="loading && series.length" class="absolute right-2 top-2 rounded bg-elevated p-1 text-faint" role="status">
      <LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
      <span class="sr-only">{{ t('common.loading') }}</span>
    </span>

    <!-- HTML rather than SVG text, so it wraps, layers and themes like every other tooltip. -->
    <div
      v-if="activePoint"
      class="vxs-card pointer-events-none absolute z-10 w-52 -translate-x-1/2 rounded-md border border-card bg-elevated p-2 text-small shadow-lg"
      :style="{ ...tooltipStyle, transform: 'translate(-50%, calc(-100% - 12px))' }"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <p class="font-medium text-secondary">{{ formatDay(activePoint.ms) }}</p>
      <p v-if="metric === 'tokens'" class="mt-0.5 text-faint">{{ metricLabel }}</p>
      <p class="mt-1 flex items-baseline justify-between gap-2">
        <span class="font-semibold text-primary tabular-nums">
          {{ formatValue(activePoint.value) }}
        </span>
        <span class="text-faint">
          {{ t(mode === 'average' ? 'statistics.averageChart.perTask' : 'statistics.chart.runningTotal') }}
        </span>
      </p>
      <p v-if="mode === 'cumulative'" class="flex items-baseline justify-between gap-2">
        <span class="text-primary tabular-nums">+{{ formatValue(activePoint.spent) }}</span>
        <span class="text-faint">{{ t('statistics.chart.thatDay') }}</span>
      </p>
      <p class="flex items-baseline justify-between gap-2">
        <span class="text-primary tabular-nums">{{ formatNumber(activePoint.count) }}</span>
        <!-- A plural with no number in it: the figure is the span before, aligned apart. -->
        <span class="text-faint">
          {{ t(mode === 'average' ? 'statistics.averageChart.tasks' : 'statistics.chart.runs', { count: activePoint.count }) }}
        </span>
      </p>
    </div>
  </div>
</template>
