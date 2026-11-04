<script setup lang="ts">
/**
 * The corpus's `[[wiki link]]` graph, centred on the open document (block-editor spec §5.1).
 *
 * Named `DocLinkGraph` rather than `DocGraph` because `DocGraph` is the payload *type* and
 * the panel above imports both — a component and a type sharing one identifier in one file
 * is a trap. "Link graph" is also the honest name: this shows both directions, where
 * "backlinks" strictly means inbound only.
 *
 * Drawn as plain SVG, and the argument is `CostChart.vue`'s own, verbatim applicable: there
 * is one picture here and a library would be a dependency for a handful of `<circle>`s. Vue
 * Flow in particular is an editor canvas — a viewport, a transform store and one Vue
 * component instance per node — and fifty of those beside a live ProseMirror document is the
 * wrong direction for §9's ≤16 ms input frames. Nothing here is dragged, connected, selected
 * or persisted. SVG rather than `<canvas>` for hit-testing, `<title>` tooltips and, above
 * all, theming: `fill-accent` compiles to `var(--vxs-accent)` and follows a runtime accent
 * change with no redraw, where a canvas would need a second source of truth for colour.
 *
 * The picture is **decorative-plus-navigational**, and the bargain is stated rather than
 * assumed: the `<svg>` is one `role="img"` with a summary and no tab stop, because every
 * destination it draws is also a focusable button in the lists below — the Backlinks section
 * for the inbound half and "Links from here" for the outbound. Duplicating twelve
 * destinations as SVG tab stops with worse names than those buttons is a regression in
 * keyboard use, not an improvement.
 *
 * Nothing animates, so there is no reduced-motion branch: a settle that ran for two seconds
 * would compete with typing for the main thread, and a picture that is simply *there* when
 * it appears needs no motion branch at all.
 */
import { ChevronRight, Waypoints } from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'

import type { DocGraph, DocLink } from '@/api/types'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import { formatNumber, t } from '@/i18n'
import type { Choice } from '@/lib/appearance'
import {
  BOX,
  type Hops,
  type PlacedGraph,
  type PlacedNode,
  layoutDocGraph,
} from '@/lib/doc-graph'

const props = defineProps<{
  /** The whole corpus's link graph, or null while nobody has asked for it yet. */
  graph: DocGraph | null
  /** The open document — the picture's centre. The panel only renders with one open. */
  path: string
  /** The open document's own outgoing links — the text path for the outbound half. */
  links: DocLink[]
  loading?: boolean
  /** Why the picture is missing. Shown in the box, never as a page banner. */
  error?: string | null
}>()

const emit = defineEmits<{
  /** A node was clicked. Same destination as a Backlinks row. */
  open: [path: string]
  /**
   * The section was expanded. The store decides whether that costs a request — this
   * component knows only that somebody is now looking.
   */
  reveal: []
}>()

/**
 * Whether the section is open, and how far it reaches, remembered per browser.
 *
 * Both are reading preferences worth nothing to the project, so they live in `localStorage`
 * beside the token — the same reasoning `TaskDetail.vue` writes out for
 * `voxarium_studio.transcript.verbosity`. This is *not* the case `.claude/rules/styling.md` forbids:
 * that rule is about appearance, which travels with the project in `config.yaml`, and how
 * much of a side panel one reader wants folded away travels with nobody.
 */
const OPEN_KEY = 'voxarium_studio.docs.graph.open'
const HOPS_KEY = 'voxarium_studio.docs.graph.hops'

type HopKey = '1' | '2' | 'all'

/** A `computed`, so the labels and hints follow the interface language. */
const HOP_OPTIONS = computed<readonly Choice<HopKey>[]>(() => [
  {
    value: '1',
    label: t('docs.graph.hops.count', { count: 1 }),
    hint: t('docs.graph.hops.oneHint'),
  },
  {
    value: '2',
    label: t('docs.graph.hops.count', { count: 2 }),
    hint: t('docs.graph.hops.twoHint'),
  },
  { value: 'all', label: t('docs.graph.hops.all'), hint: t('docs.graph.hops.allHint') },
])

const isHopKey = (value: string | null): value is HopKey =>
  value === '1' || value === '2' || value === 'all'

const remembered = window.localStorage.getItem(HOPS_KEY)
const hops = ref<HopKey>(isHopKey(remembered) ? remembered : '1')
// Expanded by default: the section is the thing that was asked for, and the panel it sits in
// is itself already toggleable from the header's PanelRight button.
const expanded = ref(window.localStorage.getItem(OPEN_KEY) !== 'false')

const HOP_VALUE: Record<HopKey, Hops> = { '1': 1, '2': 2, all: Infinity }
const hopValue = computed<Hops>(() => HOP_VALUE[hops.value])

const hovered = ref<string | null>(null)

onMounted(() => {
  // The first fetch cannot race the first document open: this only mounts inside the
  // panel's `v-if="docs.open"`, so the request starts after the document has arrived.
  if (expanded.value) emit('reveal')
})

function toggle(): void {
  expanded.value = !expanded.value
  window.localStorage.setItem(OPEN_KEY, String(expanded.value))
  if (expanded.value) emit('reveal')
}

function setHops(value: HopKey): void {
  hops.value = value
  window.localStorage.setItem(HOPS_KEY, value)
}

/**
 * The settled picture — a `computed`, never a watcher writing a `ref`.
 *
 * It is a pure function of the payload, the focus, the hop depth and whether anyone is
 * looking, so a `computed` says exactly that and Vue caches it against exactly those. The
 * active-heading watcher beside this panel fires on every scroll frame; a layout recomputed
 * there would be a 5 ms hitch per frame instead of one per payload. `expanded` is a
 * dependency on purpose — a folded section never pays for a layout, which is the second half
 * of the lazy gate (the first being the fetch).
 */
const placed = computed<PlacedGraph | null>(() =>
  props.graph && props.path && expanded.value
    ? layoutDocGraph(props.graph, props.path, hopValue.value)
    : null,
)

/** True when the picture would be one dot and a sentence says more than a square would. */
const barren = computed(
  () => !!placed.value && placed.value.nodes.length <= 1 && hops.value !== 'all',
)

const summary = computed(() => {
  const graph = placed.value
  if (!graph) return t('docs.graph.summary.empty')
  const focus = graph.nodes.find((node) => node.relation === 'focus')
  const { inbound, outbound, other } = graph.counts
  // Up to three quantities in one sentence, each its own plural phrase inside it.
  const params = {
    title: focus?.title ?? props.path,
    inbound: t('docs.graph.summary.inbound', { count: inbound }),
    outbound: t('docs.graph.summary.outbound', { count: outbound }),
  }
  return other
    ? t('docs.graph.summary.withNearby', {
        ...params,
        nearby: t('docs.graph.summary.nearby', { count: other }),
      })
    : t('docs.graph.summary.text', params)
})

const showing = computed(() => {
  const graph = placed.value
  if (!graph) return ''
  const reach =
    hops.value === 'all'
      ? t('docs.graph.hops.wholeProject')
      : t('docs.graph.hops.count', { count: Number(hops.value) })
  return t('docs.graph.showing', {
    shown: formatNumber(graph.nodes.length),
    count: graph.total,
    reach,
  })
})

/**
 * Direction lives in the node's colour, not in an arrowhead: at 100 units drawn into a 232px
 * square one unit is 2.3px, so a 3px arrowhead is a blob. `inbound` shares the accent with
 * the focus deliberately — those are exactly the documents the Backlinks list below names, so
 * the picture and the list agree by colour. `outbound` is `--vxs-info`, which `statusColor()`
 * never returns, so no status word is given a second meaning here.
 */
const FILL: Record<PlacedNode['relation'], string> = {
  focus: 'fill-accent',
  inbound: 'fill-accent',
  outbound: 'fill-info',
  both: 'fill-accent stroke-info',
  other: 'fill-line-strong',
}

const RADIUS: Record<PlacedNode['relation'], number> = {
  focus: 4.5,
  inbound: 3,
  outbound: 3,
  both: 3,
  other: 1.8,
}

const OPACITY: Record<PlacedNode['relation'], number> = {
  focus: 1,
  inbound: 0.72,
  outbound: 0.72,
  both: 0.85,
  other: 0.55,
}

const radiusOf = (node: PlacedNode) =>
  RADIUS[node.relation] + (hovered.value === node.path ? 0.8 : 0)

// Opacity is a bound attribute and never a Tailwind `opacity-*` class: hover has to override
// it, and two utilities setting one property are decided by Tailwind's emission order
// (`.claude/rules/frontend-vue.md`).
const opacityOf = (node: PlacedNode) =>
  hovered.value === node.path ? 1 : OPACITY[node.relation]

function describe(node: PlacedNode): string {
  return t('docs.graph.node', {
    title: node.title,
    inbound: t('docs.graph.nodeLinksIn', { count: node.in_degree }),
    outbound: t('docs.graph.nodeLinksOut', { count: node.out_degree }),
  })
}

/**
 * Which dots get a written name. The focus and whatever is under the pointer always; the
 * first ring only when it is small enough to stay readable, and never more than seven.
 */
const LABEL_CAP = 7
const labels = computed(() => {
  const graph = placed.value
  if (!graph) return []
  const firstRing = graph.nodes.filter((node) => node.hops === 1)
  const named = graph.nodes.filter(
    (node) =>
      node.relation === 'focus' ||
      node.path === hovered.value ||
      (node.hops === 1 && firstRing.length <= 6),
  )
  return named.slice(0, LABEL_CAP).map((node) => ({
    path: node.path,
    title: node.title,
    strong: node.relation === 'focus' || node.path === hovered.value,
    // Clamped so a rim label is not cut off by the box's `overflow-hidden`; the dot itself
    // does not move. Percent works because the box is square against a 100-unit viewBox.
    left: Math.min(88, Math.max(12, node.x)),
    top: node.y + radiusOf(node),
  }))
})

/**
 * The outbound half's text path, which the panel had none of — it lists only *unresolved*
 * links. Built from the document's own `links` rather than from `graph`, because those are
 * refreshed by the save's own response while the graph may be a debounce behind.
 */
const outgoing = computed(() => {
  const seen = new Set<string>()
  return props.links
    .filter((link) => link.resolved && link.path && link.path !== props.path)
    .filter((link) => (seen.has(link.path!) ? false : seen.add(link.path!)))
    .map((link) => ({ path: link.path!, label: link.label || link.target }))
})
</script>

<template>
  <section class="border-b border-line-subtle p-3">
    <h2 class="text-small tracking-wide text-faint uppercase">
      <button
        class="flex w-full items-center gap-1.5 rounded-sm py-0.5 hover:text-muted"
        :aria-expanded="expanded"
        aria-controls="doc-link-graph"
        @click="toggle"
      >
        <Waypoints class="size-3" /> {{ t('docs.graph.title') }}
        <ChevronRight class="vxs-chevron ml-auto size-3" :class="expanded ? 'rotate-90' : ''" />
      </button>
    </h2>

    <!-- `v-show`, so the hop choice and the hover survive a fold while the `computed` still
         short-circuits on `expanded` and nothing is laid out while folded. -->
    <div v-show="expanded" id="doc-link-graph" class="mt-2">
      <div class="mb-2 flex justify-center">
        <SegmentedControl
          :options="HOP_OPTIONS"
          :model-value="hops"
          @update:model-value="setHops"
        />
      </div>

      <!-- Content, not chrome: no `vxs-pane` and no `vxs-card` hook, either of which would
           draw a card's ring and shadow inside the side panel. -->
      <div
        class="relative mx-auto aspect-square w-full max-w-72 overflow-hidden rounded-md border border-line-subtle bg-inset"
      >
        <!-- The error branch is entered only when there is nothing to draw. `graphError` is
             set by *any* failed fetch, and most of them are the debounced background refresh
             an agent's write schedules — with a perfectly good picture still in hand and
             nobody having asked for anything. Replacing it with "couldn't load" loses a
             correct answer to a transient one; the picture stays and the failure is a line
             under it (below). -->
        <p
          v-if="error && !placed"
          class="grid size-full place-items-center p-3 text-center text-faint"
        >
          <span>
            {{ t('docs.graph.loadFailed') }}
            <button class="ml-1 underline hover:text-muted" @click="emit('reveal')">
              {{ t('docs.graph.tryAgain') }}
            </button>
          </span>
        </p>

        <p
          v-else-if="!placed"
          class="grid size-full place-items-center p-3 text-center text-faint"
        >
          {{ loading ? t('docs.graph.building') : t('docs.graph.none') }}
        </p>

        <p v-else-if="barren" class="grid size-full place-items-center p-3 text-center text-faint">
          {{ t('docs.graph.barren') }}
        </p>

        <template v-else-if="placed">
          <!-- The viewBox is a constant and is never measured: a force layout has no axis, so
               scaling it is exactly correct and the panel's pixel width is irrelevant. That
               is the deliberate difference from `CostChart.vue`, which measures because a
               time axis must not be squashed — and it is why resizing the pane never
               re-shuffles the picture. -->
          <svg
            viewBox="0 0 100 100"
            preserveAspectRatio="xMidYMid meet"
            class="absolute inset-0 size-full"
            role="img"
            :aria-label="summary"
            focusable="false"
          >
            <!-- `non-scaling-stroke` so an edge is one device pixel at any panel width: a
                 hairline that thickened as the pane widened would read as emphasis. -->
            <g vector-effect="non-scaling-stroke">
              <line
                v-for="edge in placed.edges"
                :key="edge.key"
                :x1="edge.x1"
                :y1="edge.y1"
                :x2="edge.x2"
                :y2="edge.y2"
                vector-effect="non-scaling-stroke"
                :class="
                  edge.incident || edge.source === hovered || edge.target === hovered
                    ? 'stroke-line-strong'
                    : 'stroke-line'
                "
                :stroke-width="
                  edge.incident || edge.source === hovered || edge.target === hovered ? 1.5 : 1
                "
                :opacity="
                  edge.incident || edge.source === hovered || edge.target === hovered ? 1 : 0.45
                "
              />
            </g>

            <circle :cx="BOX / 2" :cy="BOX / 2" r="9" class="fill-accent" opacity="0.15" />

            <g
              v-for="node in placed.nodes"
              :key="node.path"
              :class="node.relation === 'focus' ? 'cursor-default' : 'cursor-pointer'"
              @pointerenter="hovered = node.path"
              @pointerleave="hovered = null"
              @click="node.relation !== 'focus' && emit('open', node.path)"
            >
              <!-- `transparent`, not `none`: an element filled `none` receives no pointer
                   events at all, and an `other` dot is under 4px of target. -->
              <circle :cx="node.x" :cy="node.y" r="6" fill="transparent" />
              <circle
                :cx="node.x"
                :cy="node.y"
                :r="radiusOf(node)"
                :class="FILL[node.relation]"
                :stroke-width="node.relation === 'both' ? 1 : 0"
                :opacity="opacityOf(node)"
                class="pointer-events-none"
              />
              <title>{{ describe(node) }}</title>
            </g>
          </svg>

          <!-- HTML rather than `<text>`: SVG text scales with the viewBox, so an 11px label
               would grow as the panel widened. Same call `CostChart.vue` makes for its
               tooltip — real `text-small`, real `truncate`, real tokens. -->
          <div class="pointer-events-none absolute inset-0">
            <span
              v-for="label in labels"
              :key="label.path"
              class="absolute max-w-28 -translate-x-1/2 truncate text-small"
              :class="label.strong ? 'text-primary' : 'text-muted'"
              :style="{ left: `${label.left}%`, top: `calc(${label.top}% + 2px)` }"
            >
              {{ label.title }}
            </span>
          </div>
        </template>
      </div>

      <!-- The other half of the rule above: the picture on screen is still the last good one,
           so what the reader needs is that it may have moved on — not that nothing loaded. -->
      <p v-if="error && placed" class="mt-1.5 text-small text-faint">
        {{ t('docs.graph.stale') }}
        <button class="ml-1 underline hover:text-muted" @click="emit('reveal')">
          {{ t('docs.graph.tryAgain') }}
        </button>
      </p>

      <p v-if="placed && !barren" class="mt-1.5 text-small text-faint">{{ showing }}</p>

      <!-- The key explains a picture the screen reader is not being shown, exactly as
           `ActivityHeatmap.vue`'s Less → More strip is. -->
      <div
        v-if="placed && !barren"
        class="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-small text-faint"
        aria-hidden="true"
      >
        <span class="flex items-center gap-1">
          <svg viewBox="0 0 8 8" class="size-2"><circle cx="4" cy="4" r="4" class="fill-accent" /></svg>
          {{ t('docs.graph.legend.inbound') }}
        </span>
        <span class="flex items-center gap-1">
          <svg viewBox="0 0 8 8" class="size-2"><circle cx="4" cy="4" r="4" class="fill-info" /></svg>
          {{ t('docs.graph.legend.outbound') }}
        </span>
        <span class="flex items-center gap-1">
          <svg viewBox="0 0 8 8" class="size-2">
            <circle cx="4" cy="4" r="2.5" class="fill-line-strong" />
          </svg>
          {{ t('docs.graph.legend.further') }}
        </span>
      </div>

      <div v-if="outgoing.length" class="mt-3">
        <h3 class="mb-1 text-small tracking-wide text-faint uppercase">
          {{ t('docs.graph.linksFromHere') }}
        </h3>
        <button
          v-for="link in outgoing"
          :key="link.path"
          class="block w-full truncate text-left hover:underline"
          @click="emit('open', link.path)"
        >
          {{ link.label }}
        </button>
      </div>
    </div>
  </section>
</template>
