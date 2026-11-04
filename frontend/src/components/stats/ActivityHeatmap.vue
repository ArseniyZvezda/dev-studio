<script setup lang="ts">
/**
 * When the team was working, as a grid of days (§10.4) — the shape GitHub draws for
 * commits and Claude Code for sessions: one column per week, Monday at the top, a cell
 * per day shaded by how much happened on it.
 *
 * One measure, so one hue: four steps of the accent over the inset surface, and the bare
 * surface for a day with nothing. A sequential ramp is read by comparison, so the only key
 * is the Less → More strip. The cells are the hit targets, with the gap between them
 * counted toward the nearest, so a reader aiming at a day lands on it; the keyboard walks
 * the same cells from today. Month names sit over the column their month starts in and
 * three weekday names down the side, both in text tokens. Plain SVG, like the cost chart
 * beside it — the grid is `lib/activity-heatmap.ts`, which is where its rules are tested.
 */
import { useElementSize } from '@vueuse/core'
import { computed, ref } from 'vue'

import type { ActivityDay } from '@/api/types'
import { t } from '@/i18n'
import {
  GEOMETRY,
  type HeatCell,
  describeCell,
  formatWeekday,
  heatmapWeeks,
  monthLabels,
  weeksThatFit,
  windowTotals,
} from '@/lib/activity-heatmap'
import { dayMs, formatDay } from '@/lib/cost-chart'
import { formatCost } from '@/lib/status'

const props = defineProps<{
  activity: ActivityDay[]
  /** ISO date the grid ends on. A prop rather than `new Date()` so a render is a function of its inputs. */
  today: string
}>()

const container = ref<HTMLElement | null>(null)
const svg = ref<SVGSVGElement | null>(null)
const { width: measured } = useElementSize(container)

const { cell: CELL, gap: GAP, gutter: GUTTER, pad: PAD } = GEOMETRY
const STEP = CELL + GAP
/** Room above the grid for the month names. */
const TOP = 18
const ROWS = 7
const HEIGHT = TOP + ROWS * STEP - GAP

const weeks = computed(() => weeksThatFit(measured.value || 640))
const grid = computed(() => heatmapWeeks(props.activity, props.today, weeks.value))
const months = computed(() => monthLabels(grid.value))
const totals = computed(() => windowTotals(grid.value))
/** The grid plus the pad a month name over the last column needs. */
const width = computed(() => GUTTER + weeks.value * STEP - GAP + PAD)

/**
 * The rows labelled down the side — Monday, Wednesday, Friday. Named when drawn, so the names
 * follow the interface language.
 */
const DAY_ROWS = [0, 2, 4] as const

function x(column: number): number {
  return GUTTER + column * STEP
}

function y(row: number): number {
  return TOP + row * STEP
}

/**
 * The caption: what the visible grid adds up to, or that it is still empty. One message per
 * sentence, plural on the weeks; the day count and the list are plural messages of their own,
 * handed in whole.
 */
const caption = computed(() => {
  const sums = totals.value
  if (!sums.activeDays) return t('statistics.activity.empty', { count: weeks.value })
  const parts = [
    t('statistics.activity.runs', { count: sums.runs }),
    t('statistics.activity.tasksFinished', { count: sums.finished }),
  ]
  if (sums.spent_usd > 0) parts.push(formatCost(sums.spent_usd))
  return t('statistics.activity.caption', {
    count: weeks.value,
    days: t('statistics.activity.activeDays', { count: sums.activeDays }),
    summary: parts.join(', '),
  })
})

// ---------------------------------------------------------------- hover and keyboard

interface Position {
  column: number
  row: number
}

const active = ref<Position | null>(null)
const activeCell = computed<HeatCell | null>(() =>
  active.value ? (grid.value[active.value.column]?.cells[active.value.row] ?? null) : null,
)

function cellAt(position: Position): HeatCell | null {
  return grid.value[position.column]?.cells[position.row] ?? null
}

/** The nearest cell to the pointer: the gap between two belongs to the one it precedes. */
function locate(event: PointerEvent): void {
  const element = svg.value
  if (!element) return
  const rect = element.getBoundingClientRect()
  const scale = width.value / (rect.width || 1)
  const px = (event.clientX - rect.left) * scale
  const py = (event.clientY - rect.top) * scale
  const position = {
    column: Math.floor((px - GUTTER) / STEP),
    row: Math.floor((py - TOP) / STEP),
  }
  const cell = cellAt(position)
  active.value = cell && !cell.future ? position : null
}

/** Where today sits: the last column, on its weekday row. */
function todayPosition(): Position {
  const column = grid.value.length - 1
  const row = grid.value[column]?.cells.findIndex((cell) => cell.ms === dayMs(props.today)) ?? 0
  return { column, row: Math.max(0, row) }
}

/** Arrow keys walk the grid; a step into the future or off the edge is a step not taken. */
function step(columns: number, rows: number): void {
  const from = active.value ?? todayPosition()
  const next = { column: from.column + columns, row: from.row + rows }
  const cell = cellAt(next)
  active.value = cell && !cell.future ? next : from
}

/**
 * Where the tooltip sits: above the cell, centred, and pulled in from either edge so it
 * never leaves the card.
 */
const tooltipStyle = computed(() => {
  const position = active.value
  if (!position) return {}
  const centre = x(position.column) + CELL / 2
  const left = Math.min(width.value - PAD - 96, Math.max(96, centre))
  return { left: `${left}px`, top: `${y(position.row)}px` }
})
</script>

<template>
  <div ref="container" class="relative w-full">
    <div class="overflow-x-auto">
      <svg
        ref="svg"
        :viewBox="`0 0 ${width} ${HEIGHT}`"
        :width="width"
        :height="HEIGHT"
        class="heatmap block select-none outline-none focus-visible:ring-1 focus-visible:ring-accent"
        role="img"
        :aria-label="t('statistics.activity.ariaLabel', { caption })"
        tabindex="0"
        @pointermove="locate"
        @pointerleave="active = null"
        @focus="step(0, 0)"
        @blur="active = null"
        @keydown.left.prevent="step(-1, 0)"
        @keydown.right.prevent="step(1, 0)"
        @keydown.up.prevent="step(0, -1)"
        @keydown.down.prevent="step(0, 1)"
      >
        <!-- Labels wear text tokens; the grid itself carries the only colour. -->
        <g class="fill-faint text-small">
          <text
            v-for="month in months"
            :key="month.index"
            :x="x(month.index)"
            :y="TOP - 7"
          >
            {{ month.label }}
          </text>
          <text
            v-for="row in DAY_ROWS"
            :key="row"
            :x="0"
            :y="y(row) + CELL - 2"
          >
            {{ formatWeekday(row) }}
          </text>
        </g>

        <!-- One rect per day that has happened; the future is left blank. -->
        <g v-for="(week, column) in grid" :key="week.start">
          <template v-for="(cell, row) in week.cells" :key="cell.day">
            <rect
              v-if="!cell.future"
              :x="x(column)"
              :y="y(row)"
              :width="CELL"
              :height="CELL"
              rx="2"
              :class="[
                `heat-${cell.level}`,
                active && active.column === column && active.row === row ? 'heat-active' : '',
              ]"
            />
          </template>
        </g>
      </svg>
    </div>

    <div class="mt-2 flex flex-wrap items-center justify-between gap-2 text-small text-faint">
      <p>{{ caption }}</p>
      <p class="flex items-center gap-1" aria-hidden="true">
        <span>{{ t('statistics.activity.less') }}</span>
        <svg
          v-for="level in [0, 1, 2, 3, 4]"
          :key="level"
          :width="CELL"
          :height="CELL"
          class="heatmap"
        >
          <rect :width="CELL" :height="CELL" rx="2" :class="`heat-${level}`" />
        </svg>
        <span>{{ t('statistics.activity.more') }}</span>
      </p>
    </div>

    <!-- HTML rather than SVG text, so it wraps, layers and themes like every other tooltip. -->
    <div
      v-if="activeCell"
      class="vxs-card pointer-events-none absolute z-10 w-48 rounded-md border border-card bg-elevated p-2 text-small shadow-lg"
      :style="{ ...tooltipStyle, transform: 'translate(-50%, calc(-100% - 10px))' }"
    >
      <p class="font-medium text-secondary">{{ formatDay(activeCell.ms) }}</p>
      <p class="mt-0.5 text-primary">{{ describeCell(activeCell) }}</p>
    </div>
  </div>
</template>

<style scoped>
/*
 * Four steps of the one hue over the inset surface: a sequential ramp, light to dark, and
 * anchored to the surface so it recedes correctly on both themes rather than flipping.
 * Never a literal colour — the accent is the user's, and every shade follows it.
 */
.heatmap .heat-0 {
  fill: var(--vxs-bg-inset);
}
.heatmap .heat-1 {
  fill: color-mix(in oklab, var(--vxs-accent) 32%, var(--vxs-bg-inset));
}
.heatmap .heat-2 {
  fill: color-mix(in oklab, var(--vxs-accent) 55%, var(--vxs-bg-inset));
}
.heatmap .heat-3 {
  fill: color-mix(in oklab, var(--vxs-accent) 78%, var(--vxs-bg-inset));
}
.heatmap .heat-4 {
  fill: var(--vxs-accent);
}
/* The hovered or focused day lifts with a ring in the text colour, whatever its shade. */
.heatmap .heat-active {
  stroke: var(--vxs-text-primary);
  stroke-width: 1.5;
}
</style>
