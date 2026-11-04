<script setup lang="ts">
/**
 * The project's running cost, as a line over the days it was spent on (§10.4).
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
 * plain SVG: there is one chart in the app, and a library would be a dependency for one
 * `<path>`.
 */
import { useElementSize } from '@vueuse/core'
import { computed, ref } from 'vue'

import type { CostPoint } from '@/api/types'
import { t } from '@/i18n'
import {
  dayDomain,
  dayMs,
  dayTicks,
  formatAxisCost,
  formatDay,
  niceTicks,
  smoothCostPath,
} from '@/lib/cost-chart'
import { formatCost } from '@/lib/status'

const props = defineProps<{
  series: CostPoint[]
  /** ISO date the x axis runs to. A prop rather than `new Date()` so a render is a function of its inputs. */
  today: string
}>()

const container = ref<HTMLElement | null>(null)
const svg = ref<SVGSVGElement | null>(null)
const { width: measured } = useElementSize(container)

const HEIGHT = 240
// Right margin holds the end label; left holds the axis ticks; bottom the day labels.
const MARGIN = { top: 16, right: 72, bottom: 28, left: 52 }

const width = computed(() => Math.max(320, Math.round(measured.value || 640)))
const plotWidth = computed(() => width.value - MARGIN.left - MARGIN.right)
const plotHeight = HEIGHT - MARGIN.top - MARGIN.bottom
const baseline = MARGIN.top + plotHeight

const domain = computed(() => dayDomain(props.series, props.today))
const yTicks = computed(() => niceTicks(props.series[props.series.length - 1]?.cumulative_usd ?? 0))
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
    py: y(entry.cumulative_usd),
  })),
)

const last = computed(() => points.value[points.value.length - 1] ?? null)

/** The curve, carried flat to today when the last spend was earlier than that. */
const linePath = computed(() => {
  if (!points.value.length) return ''
  const curve = points.value.map(({ px, py }) => ({ px, py }))
  const end = x(domain.value.end)
  if (last.value && end > last.value.px + 0.5) {
    curve.push({ px: end, py: last.value.py })
  }
  return smoothCostPath(curve)
})

const areaPath = computed(() => {
  if (!linePath.value || !points.value.length) return ''
  const end = Math.max(x(domain.value.end), last.value?.px ?? 0)
  const first = points.value[0]!.px
  return `${linePath.value} L${end.toFixed(1)} ${baseline} L${first.toFixed(1)} ${baseline} Z`
})

const xTicks = computed(() => dayTicks(domain.value.start, domain.value.end))

const summary = computed(() => {
  if (!last.value) return t('statistics.chart.summaryEmpty')
  return t('statistics.chart.summary', {
    count: points.value.length,
    total: formatCost(last.value.cumulative_usd),
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
  const left = Math.min(width.value - 96, Math.max(96, point.px))
  return { left: `${left}px`, top: `${point.py}px` }
})
</script>

<template>
  <div ref="container" class="relative w-full">
    <p
      v-if="!series.length"
      class="grid place-items-center text-faint"
      :style="{ height: `${HEIGHT}px` }"
    >
      {{ t('statistics.chart.empty') }}
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
          {{ formatAxisCost(tick) }}
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
        {{ formatCost(last.cumulative_usd) }}
      </text>
    </svg>

    <!-- HTML rather than SVG text, so it wraps, layers and themes like every other tooltip. -->
    <div
      v-if="activePoint"
      class="vxs-card pointer-events-none absolute z-10 w-44 -translate-x-1/2 rounded-md border border-card bg-elevated p-2 text-small shadow-lg"
      :style="{ ...tooltipStyle, transform: 'translate(-50%, calc(-100% - 12px))' }"
    >
      <p class="font-medium text-secondary">{{ formatDay(activePoint.ms) }}</p>
      <p class="mt-1 flex items-baseline justify-between gap-2">
        <span class="font-semibold text-primary tabular-nums">
          {{ formatCost(activePoint.cumulative_usd) }}
        </span>
        <span class="text-faint">{{ t('statistics.chart.runningTotal') }}</span>
      </p>
      <p class="flex items-baseline justify-between gap-2">
        <span class="text-primary tabular-nums">+{{ formatCost(activePoint.spent_usd) }}</span>
        <span class="text-faint">{{ t('statistics.chart.thatDay') }}</span>
      </p>
      <p class="flex items-baseline justify-between gap-2">
        <span class="text-primary tabular-nums">{{ activePoint.runs }}</span>
        <!-- A plural with no number in it: the figure is the span before, aligned apart. -->
        <span class="text-faint">{{ t('statistics.chart.runs', { count: activePoint.runs }) }}</span>
      </p>
    </div>
  </div>
</template>
