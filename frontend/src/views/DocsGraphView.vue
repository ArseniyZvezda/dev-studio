<script setup lang="ts">
/**
 * Documents → Graph: every document of the project and every `[[wiki link]]` between them, on a
 * page of their own, the way Obsidian draws a vault (block-editor spec §5.1.1).
 *
 * The side panel's link graph (`DocLinkGraph.vue`) is the same payload drawn the opposite way,
 * and the differences are the point of having both. That one is a still picture centred on the
 * open document, settled once beside a live editor that must keep its frames; this one is the
 * whole corpus, moving — a simulation stepped per frame that cools to a stop, with a camera that
 * pans and zooms, nodes that can be dragged, and five forces set above it. `lib/doc-graph-sim.ts`
 * owns the physics; this component owns the camera, the pointer and the drawing.
 *
 * **A node is its folder.** Each document is a circle in its parent folder's colour with that
 * folder's icon *cut out of it* — an SVG mask per icon, the icon's strokes black on a white disc,
 * so the page shows through where the icon is. A folder with no colour wears the accent, as it
 * does in the tree, and one with no icon the plain folder.
 *
 * The forces are a reading preference worth nothing to the project, so they live in
 * `localStorage` beside the side panel's hop count, not in `config.yaml` — and so do where the
 * documents last stood and where the camera looked, per project, so the page opens as it was left.
 *
 * The picture is one `role="img"` with a summary and no tab stops, on `DocLinkGraph`'s bargain:
 * every document it draws is also a focusable row in the Documents tree.
 */
import { BookOpen, Folder, Maximize, RotateCcw, SquarePen, X } from 'lucide-vue-next'
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  shallowRef,
  watch,
  type Component,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '@/api/client'
import type { DocDetail } from '@/api/types'
import DocReader from '@/components/docs/DocReader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { formatNumber, t } from '@/i18n'
import {
  DEFAULT_FORCES,
  FORCE_LIMITS,
  bounds,
  radiusOf,
  clampForces,
  createSimulation,
  parsePlacement,
  placementOf,
  reheat,
  step,
  type GraphForces,
  type Placement,
  type Simulation,
} from '@/lib/doc-graph-sim'
import { folderColorValue, folderIcon } from '@/lib/folder-style'
import { useDocsStore } from '@/stores/docs'
import { useSessionStore } from '@/stores/session'

const docs = useDocsStore()
const route = useRoute()
const router = useRouter()

const FORCES_KEY = 'voxarium_studio.docs.graph-page.forces'

function readForces(): GraphForces {
  try {
    return clampForces(JSON.parse(window.localStorage.getItem(FORCES_KEY) ?? 'null'))
  } catch {
    return { ...DEFAULT_FORCES }
  }
}

function writeForces(value: GraphForces): void {
  try {
    window.localStorage.setItem(FORCES_KEY, JSON.stringify(value))
  } catch {
    // Storage that refuses costs the memory, never the picture on screen.
  }
}

const forces = ref<GraphForces>(readForces())

/** The five sliders, in the order they sit above the graph. */
const SLIDERS = computed(() => [
  {
    key: 'linkStrength' as const,
    label: t('docs.graphPage.forces.linkStrength'),
    hint: t('docs.graphPage.forces.linkStrengthHint'),
    value: percent(forces.value.linkStrength),
  },
  {
    key: 'folderStrength' as const,
    label: t('docs.graphPage.forces.folderStrength'),
    hint: t('docs.graphPage.forces.folderStrengthHint'),
    value: percent(forces.value.folderStrength),
  },
  {
    key: 'unlinkedRepel' as const,
    label: t('docs.graphPage.forces.unlinkedRepel'),
    hint: t('docs.graphPage.forces.unlinkedRepelHint'),
    value: percent(forces.value.unlinkedRepel),
  },
  {
    key: 'nodeSize' as const,
    label: t('docs.graphPage.forces.nodeSize'),
    hint: t('docs.graphPage.forces.nodeSizeHint'),
    value: t('settings.appearance.pixels', { size: forces.value.nodeSize }),
  },
  {
    key: 'linkDistance' as const,
    label: t('docs.graphPage.forces.linkDistance'),
    hint: t('docs.graphPage.forces.linkDistanceHint'),
    value: t('settings.appearance.pixels', { size: forces.value.linkDistance }),
  },
])

function percent(value: number): string {
  return t('docs.graphPage.percent', { value: formatNumber(Math.round(value * 100)) })
}

const isDefault = computed(() =>
  (Object.keys(DEFAULT_FORCES) as (keyof GraphForces)[]).every(
    (key) => forces.value[key] === DEFAULT_FORCES[key],
  ),
)

function setForces(next: GraphForces): void {
  forces.value = next
  writeForces(next)
  const current = sim.value
  if (!current) return
  current.forces = { ...next }
  reheat(current, 0.5)
  run()
}

function setForce(key: keyof GraphForces, raw: string): void {
  setForces(clampForces({ ...forces.value, [key]: Number(raw) }))
}

// --- The simulation ------------------------------------------------------------------------

/**
 * Not reactive inside: positions change on every frame, and a deep proxy over them would make
 * every tick a few thousand tracked writes. `frame` is the one signal the template follows.
 */
const sim = shallowRef<Simulation | null>(null)
const frame = ref(0)
let raf: number | null = null

/**
 * Motion a reader asked not to see: the settle runs to its end inside one task and the picture
 * simply appears. A drag still moves, because the reader is the one moving it.
 */
const reducedMotion =
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** Ask for frames until the simulation and the camera have both come to rest. */
function run(): void {
  if (reducedMotion && sim.value && sim.value.alphaTarget === 0) {
    for (let i = 0; i < 600 && step(sim.value); i += 1);
    if (!userMoved) Object.assign(view, fitTarget() ?? view)
    frame.value += 1
    return
  }
  if (raf === null) raf = requestAnimationFrame(tick)
}

function tick(): void {
  raf = null
  const current = sim.value
  if (!current) return
  const moving = step(current)
  const gliding = !userMoved && approach(fitTarget(), 0.12)
  frame.value += 1
  if (moving || gliding) raf = requestAnimationFrame(tick)
  // At rest is when the picture is worth keeping for the next visit.
  else saveLayout()
}

// --- The camera ----------------------------------------------------------------------------

const box = ref<HTMLElement | null>(null)
const size = reactive({ width: 0, height: 0 })
/** World origin's offset from the box's centre, and the zoom. */
const view = reactive({ x: 0, y: 0, k: 1 })
const ZOOM = { min: 0.1, max: 4 }
/**
 * Whether the reader has taken the camera. Until then it keeps the whole corpus framed while it
 * settles; a pan, a zoom or a drag hands it over, and Fit hands it back.
 */
let userMoved = false

function fitTarget(): { x: number; y: number; k: number } | null {
  const current = sim.value
  const b = current && bounds(current)
  if (!b || !size.width || !size.height) return null
  const pad = 48
  const width = Math.max(b.maxX - b.minX, 1)
  const height = Math.max(b.maxY - b.minY, 1)
  const k = Math.min(
    ZOOM.max,
    Math.max(
      ZOOM.min,
      Math.min((size.width - 2 * pad) / width, (size.height - 2 * pad) / height, 1.6),
    ),
  )
  return { x: -((b.minX + b.maxX) / 2) * k, y: -((b.minY + b.maxY) / 2) * k, k }
}

/** Move the camera a share of the way to `target`; true while it is still visibly away. */
function approach(target: { x: number; y: number; k: number } | null, share: number): boolean {
  if (!target) return false
  view.x += (target.x - view.x) * share
  view.y += (target.y - view.y) * share
  view.k += (target.k - view.k) * share
  return (
    Math.abs(target.x - view.x) > 0.5 ||
    Math.abs(target.y - view.y) > 0.5 ||
    Math.abs(target.k - view.k) > 0.002
  )
}

function fit(): void {
  userMoved = false
  if (reducedMotion) Object.assign(view, fitTarget() ?? view)
  else run()
}

const worldTransform = computed(
  () => `translate(${size.width / 2 + view.x} ${size.height / 2 + view.y}) scale(${view.k})`,
)

/** A pointer's position in world coordinates. */
function toWorld(event: PointerEvent | WheelEvent): { x: number; y: number } {
  const rect = box.value!.getBoundingClientRect()
  return {
    x: (event.clientX - rect.left - size.width / 2 - view.x) / view.k,
    y: (event.clientY - rect.top - size.height / 2 - view.y) / view.k,
  }
}

function onWheel(event: WheelEvent): void {
  const rect = box.value!.getBoundingClientRect()
  const px = event.clientX - rect.left - size.width / 2
  const py = event.clientY - rect.top - size.height / 2
  const k = Math.min(ZOOM.max, Math.max(ZOOM.min, view.k * Math.exp(-event.deltaY * 0.0015)))
  // The point under the pointer stays under it.
  view.x = px - ((px - view.x) * k) / view.k
  view.y = py - ((py - view.y) * k) / view.k
  view.k = k
  userMoved = true
}

// --- Between visits ------------------------------------------------------------------------

/**
 * Where every document was and where the camera looked, kept per browser and per project, so
 * coming back to the page — from another section or after a reload — finds the picture as it
 * was left rather than settling it again from the seeds. `localStorage` for the forces' reason,
 * and keyed by the project's root as the tree's folds are, because a path names a different
 * document in another project.
 */
const LAYOUT_PREFIX = 'voxarium_studio.docs.graph-page.layout.'

interface StoredLayout {
  placement: Placement
  view: { x: number; y: number; k: number }
  userMoved: boolean
}

const session = useSessionStore()
const layoutKey = computed(() => {
  const root = session.project?.root
  return root ? `${LAYOUT_PREFIX}${root}` : null
})

function readLayout(): StoredLayout | null {
  if (!layoutKey.value) return null
  try {
    const raw = JSON.parse(window.localStorage.getItem(layoutKey.value) ?? 'null')
    const placement = parsePlacement(raw?.placement)
    const camera = raw?.view
    const finite = (value: unknown) => typeof value === 'number' && Number.isFinite(value)
    if (!placement || !finite(camera?.x) || !finite(camera?.y) || !finite(camera?.k)) return null
    return {
      placement,
      view: {
        x: camera.x,
        y: camera.y,
        k: Math.min(ZOOM.max, Math.max(ZOOM.min, camera.k)),
      },
      userMoved: raw.userMoved === true,
    }
  } catch {
    return null
  }
}

function saveLayout(): void {
  if (!sim.value || !layoutKey.value) return
  const stored: StoredLayout = {
    placement: placementOf(sim.value),
    view: { x: view.x, y: view.y, k: view.k },
    userMoved,
  }
  try {
    window.localStorage.setItem(layoutKey.value, JSON.stringify(stored))
  } catch {
    // A full or refused storage costs the next visit a settle, nothing more.
  }
}

const stored = readLayout()
if (stored) {
  Object.assign(view, stored.view)
  userMoved = stored.userMoved
}
/** Handed to the first simulation only; every later one starts from the one before it. */
let restored: Placement | null = stored?.placement ?? null

watch(
  () => docs.graph,
  (graph) => {
    if (!graph) return
    sim.value = createSimulation(
      graph,
      forces.value,
      sim.value ? placementOf(sim.value) : restored,
    )
    restored = null
    run()
  },
  { immediate: true },
)

// --- The pointer ---------------------------------------------------------------------------

const hovered = ref<number | null>(null)

/** What the pointer is doing between its press and its release. */
let gesture:
  | { kind: 'pan'; startX: number; startY: number; lastX: number; lastY: number; moved: boolean }
  | { kind: 'node'; index: number; startX: number; startY: number; moved: boolean }
  | null = null
const panning = ref(false)

function onBackgroundDown(event: PointerEvent): void {
  if (event.button !== 0) return
  gesture = {
    kind: 'pan',
    startX: event.clientX,
    startY: event.clientY,
    lastX: event.clientX,
    lastY: event.clientY,
    moved: false,
  }
  panning.value = true
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}

function onNodeDown(index: number, event: PointerEvent): void {
  if (event.button !== 0 || !sim.value) return
  event.stopPropagation()
  gesture = { kind: 'node', index, startX: event.clientX, startY: event.clientY, moved: false }
  box.value?.querySelector('svg')?.setPointerCapture(event.pointerId)
}

function onMove(event: PointerEvent): void {
  if (!gesture) return
  if (gesture.kind === 'pan') {
    view.x += event.clientX - gesture.lastX
    view.y += event.clientY - gesture.lastY
    gesture.lastX = event.clientX
    gesture.lastY = event.clientY
    // The view follows from the first pixel; only past the same wobble a node click allows
    // does the press stop counting as a click on empty space.
    if (Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) >= 4)
      gesture.moved = true
    userMoved = true
    return
  }
  const current = sim.value
  if (!current) return
  if (!gesture.moved) {
    // A few pixels of wobble is still a click.
    if (Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) < 4) return
    gesture.moved = true
    // The camera stops chasing the frame, or the node would slide away under the pointer.
    userMoved = true
    current.alphaTarget = 0.3
    reheat(current, 0.3)
  }
  const node = current.nodes[gesture.index]
  const at = toWorld(event)
  node.fx = at.x
  node.fy = at.y
  run()
}

function onUp(): void {
  const done = gesture
  gesture = null
  panning.value = false
  // A click on empty space lets go of the document being read, as Escape does.
  if (done?.kind === 'pan' && !done.moved && readingPath.value) closeReader()
  if (done?.kind !== 'node') return
  const current = sim.value
  if (!current) return
  const node = current.nodes[done.index]
  if (!done.moved) {
    read(node.path)
    return
  }
  // Let go: the forces take it back, and the picture cools from where it was left.
  node.fx = null
  node.fy = null
  current.alphaTarget = 0
  run()
}

// --- The reader ----------------------------------------------------------------------------

/**
 * A click on a node opens that document beside the graph, for reading; "Go to Documents" is
 * where it is edited. The document rides `?path=`, so a reload or a shared link reopens it, and
 * Back closes it the way it opened.
 *
 * Fetched here rather than through `docs.select`: the store's open document belongs to
 * Documents, whose editor may still have a save in flight for it when this page mounts, and
 * swapping it under that save is the race `stores/docs.ts` exists to prevent.
 */
const reading = ref<DocDetail | null>(null)
const readingError = ref<string | null>(null)

const readingPath = computed(() => {
  const value = route.query.path
  return typeof value === 'string' && value ? value : null
})

/** The listing's timestamp for the document read — it moves when an agent or Documents writes it. */
const readingStamp = computed(
  () => docs.entries.find((entry) => entry.path === readingPath.value)?.updated_at ?? null,
)

let readRequest = 0

watch(
  [readingPath, readingStamp],
  async ([path, stamp]) => {
    if (!path) {
      reading.value = null
      readingError.value = null
      return
    }
    if (reading.value?.path === path && (!stamp || reading.value.updated_at === stamp)) return
    // Only the newest request lands: clicking through nodes faster than the answers arrive
    // must not end on whichever was slowest.
    const ask = ++readRequest
    readingError.value = null
    try {
      const document = await api.doc(path)
      if (ask === readRequest) reading.value = document
    } catch (cause) {
      if (ask !== readRequest) return
      reading.value = null
      readingError.value = cause instanceof Error ? cause.message : String(cause)
    }
  },
  { immediate: true },
)

function read(path: string): void {
  if (path === readingPath.value) return
  void router.push({ name: 'docs-graph', query: { path } })
}

function closeReader(): void {
  void router.push({ name: 'docs-graph' })
}

function goToDocuments(): void {
  if (readingPath.value) void router.push({ name: 'docs', query: { path: readingPath.value } })
}

/** A `[[link]]` in the document being read opens its target here too, if there is one. */
function followLink(target: string): void {
  const path = docs.resolveLink(target)
  if (path) read(path)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || !readingPath.value || event.defaultPrevented) return
  // A dialog over the page — the search palette, a confirmation — owns its own Escape.
  if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return
  closeReader()
}

// --- What is drawn -------------------------------------------------------------------------

interface Look {
  color: string
  /** The mask's id suffix — the folder icon's stored name, or `folder` for the plain one. */
  icon: string
}

/** One look per folder, rebuilt when a folder is restyled — never per frame. */
const looks = computed(() => {
  const out = new Map<string, Look>()
  for (const node of sim.value?.nodes ?? []) {
    if (out.has(node.folder)) continue
    const style = node.folder ? docs.folderStyle(node.folder) : null
    const icon = folderIcon(style?.icon)
    out.set(node.folder, {
      color: folderColorValue(style?.color) ?? 'var(--vxs-accent)',
      icon: icon?.name ?? 'folder',
    })
  }
  return out
})

/** Every icon some node wears, each made once into a mask. */
const masks = computed(() => {
  const out = new Map<string, Component>()
  for (const look of looks.value.values()) {
    if (out.has(look.icon)) continue
    out.set(look.icon, folderIcon(look.icon)?.component ?? Folder)
  }
  return [...out].map(([name, component]) => ({ id: `doc-graph-cut-${name}`, component }))
})

/**
 * The cut-out icon's box, as a share of the radius: most of the disc, so it reads, with a rim
 * left round it. The masks are drawn for a unit disc and each node scales its own, so one mask
 * per icon serves every size a link count gives.
 */
const ICON_BOX = 1.25

/** Labels need a readable size on screen; zoomed far out, only the hovered ones keep theirs. */
const LABEL_ZOOM = 0.55

const scene = computed(() => {
  void frame.value
  const current = sim.value
  if (!current) return { nodes: [], links: [] }
  // What the picture is about: the node under the pointer, else the document open in the
  // reader — both dim everything that is not it or one link away from it.
  const selected = readingPath.value
    ? current.nodes.findIndex((node) => node.path === readingPath.value)
    : -1
  const focus = hovered.value ?? (selected >= 0 ? selected : null)
  const near = focus === null ? null : current.neighbours[focus]
  const lit = (index: number) => focus === null || index === focus || near!.has(index)
  return {
    nodes: current.nodes.map((node, index) => {
      const look = looks.value.get(node.folder)!
      const on = lit(index)
      return {
        index,
        path: node.path,
        title: node.title,
        x: node.x,
        y: node.y,
        radius: radiusOf(node, current.forces),
        color: look.color,
        mask: `url(#doc-graph-cut-${look.icon})`,
        opacity: on ? 1 : 0.2,
        focused: index === hovered.value,
        selected: node.path === readingPath.value,
        label:
          view.k >= LABEL_ZOOM || (focus !== null && on) || node.path === readingPath.value,
      }
    }),
    links: current.links.map((link) => {
      const a = current.nodes[link.source]
      const b = current.nodes[link.target]
      const incident = focus !== null && (link.source === focus || link.target === focus)
      return {
        key: link.key,
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        stroke: incident ? 'var(--vxs-text-secondary)' : 'var(--vxs-border-strong)',
        opacity: focus === null ? 0.7 : incident ? 1 : 0.12,
      }
    }),
  }
})

const counts = computed(() => {
  const current = sim.value
  return t('docs.graphPage.counts', {
    documents: t('docs.graphPage.documents', { count: current?.nodes.length ?? 0 }),
    links: t('docs.graphPage.links', { count: current?.links.length ?? 0 }),
  })
})

const summary = computed(() => {
  const current = sim.value
  return t('docs.graphPage.summary', {
    documents: t('docs.graphPage.documents', { count: current?.nodes.length ?? 0 }),
    links: t('docs.graphPage.links', { count: current?.links.length ?? 0 }),
  })
})

// --- Lifecycle -----------------------------------------------------------------------------

let observer: ResizeObserver | null = null

onMounted(() => {
  docs.subscribe()
  // The listing carries the folders' looks; the graph is fetched lazily and kept warm by the
  // store from here on — an agent's write moves the picture without a reload.
  void docs.load()
  void docs.loadGraph()
  // A reload or a closed tab unmounts nothing, so the page's own end is heard as well.
  window.addEventListener('pagehide', saveLayout)
  window.addEventListener('keydown', onKeydown)
  if (!box.value) return
  const measure = () => {
    const rect = box.value!.getBoundingClientRect()
    size.width = rect.width
    size.height = rect.height
    if (!userMoved) fit()
  }
  measure()
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(measure)
    observer.observe(box.value)
  }
})

onBeforeUnmount(() => {
  // Leaving mid-settle keeps the picture as far as it got, and its heat with it, so the next
  // visit carries on rather than starting over.
  saveLayout()
  window.removeEventListener('pagehide', saveLayout)
  window.removeEventListener('keydown', onKeydown)
  if (raf !== null) cancelAnimationFrame(raf)
  observer?.disconnect()
})
</script>

<template>
  <div class="flex h-full min-w-0 flex-col">
    <header class="vxs-pane flex shrink-0 items-center gap-3 border-b border-pane bg-surface px-pane py-2">
      <div class="min-w-0 flex-1">
        <h1 class="text-title font-medium">{{ t('docs.graphPage.title') }}</h1>
        <p class="truncate text-small text-faint">
          {{ sim ? counts : t('docs.graphPage.subtitle') }}
        </p>
      </div>
      <button
        class="flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-line px-2 text-muted hover:bg-hover hover:text-primary"
        :title="t('docs.graphPage.fit')"
        @click="fit"
      >
        <Maximize class="size-3.5" />
        <span>{{ t('docs.graphPage.fit') }}</span>
      </button>
    </header>

    <!-- The forces, above the picture they shape. -->
    <div
      class="flex shrink-0 flex-wrap items-center gap-x-6 gap-y-2 border-b border-line px-pane py-2"
      role="group"
    >
      <label
        v-for="slider in SLIDERS"
        :key="slider.key"
        class="flex items-center gap-2"
        :title="slider.hint"
      >
        <span class="text-muted">{{ slider.label }}</span>
        <input
          type="range"
          class="w-28"
          :min="FORCE_LIMITS[slider.key].min"
          :max="FORCE_LIMITS[slider.key].max"
          :step="FORCE_LIMITS[slider.key].step"
          :value="forces[slider.key]"
          @input="setForce(slider.key, ($event.target as HTMLInputElement).value)"
        />
        <span class="w-12 text-small text-faint tabular-nums">{{ slider.value }}</span>
      </label>
      <button
        class="ml-auto flex h-7 items-center gap-1.5 rounded-md border border-line px-2 text-muted hover:bg-hover hover:text-primary disabled:pointer-events-none disabled:opacity-40"
        :disabled="isDefault"
        :title="t('docs.graphPage.resetHint')"
        @click="setForces({ ...DEFAULT_FORCES })"
      >
        <RotateCcw class="size-3.5" />
        <span>{{ t('common.reset') }}</span>
      </button>
    </div>

    <div class="vxs-enter relative flex min-h-0 flex-1">
    <div ref="box" class="relative min-h-0 min-w-0 flex-1 overflow-hidden">
      <div
        v-if="docs.graphError && !docs.graph"
        class="grid size-full place-items-center p-6 text-center text-faint"
      >
        <span>
          {{ t('docs.graphPage.loadFailed') }}
          <button class="ml-1 underline hover:text-muted" @click="docs.loadGraph()">
            {{ t('docs.graph.tryAgain') }}
          </button>
        </span>
      </div>

      <div v-else-if="!docs.graph" class="p-pane">
        <LoadingBars />
      </div>

      <EmptyState
        v-else-if="!docs.graph.nodes.length"
        :title="t('docs.graphPage.emptyTitle')"
        :hint="t('docs.graphPage.emptyHint')"
      />

      <template v-else>
        <svg
          class="absolute inset-0 size-full touch-none select-none"
          :class="panning ? 'cursor-grabbing' : 'cursor-grab'"
          role="img"
          :aria-label="summary"
          focusable="false"
          @pointerdown="onBackgroundDown"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="onUp"
          @wheel.prevent="onWheel"
          @dblclick="fit"
        >
          <defs>
            <!-- One mask per folder icon: the disc white, the icon's strokes black, so the
                 circle is drawn everywhere but where the icon is. For a unit disc — the masked
                 circle sits in a group scaled to its node's radius, and the mask follows it. -->
            <mask
              v-for="mask in masks"
              :id="mask.id"
              :key="mask.id"
              maskUnits="userSpaceOnUse"
              x="-1.1"
              y="-1.1"
              width="2.2"
              height="2.2"
            >
              <circle r="1.1" fill="white" />
              <component
                :is="mask.component"
                :x="-ICON_BOX / 2"
                :y="-ICON_BOX / 2"
                :size="ICON_BOX"
                color="black"
                :stroke-width="2.5"
              />
            </mask>
          </defs>

          <g :transform="worldTransform">
            <line
              v-for="link in scene.links"
              :key="link.key"
              :x1="link.x1"
              :y1="link.y1"
              :x2="link.x2"
              :y2="link.y2"
              vector-effect="non-scaling-stroke"
              stroke-width="1"
              :style="{ stroke: link.stroke }"
              :opacity="link.opacity"
            />

            <g
              v-for="node in scene.nodes"
              :key="node.path"
              :transform="`translate(${node.x} ${node.y})`"
              :opacity="node.opacity"
              class="cursor-pointer"
              @pointerdown="onNodeDown(node.index, $event)"
              @pointerenter="hovered = node.index"
              @pointerleave="hovered = null"
            >
              <!-- `transparent`, not `none`: a shape filled `none` takes no pointer events, and
                   the icon's cut-out would otherwise be a hole in the target. -->
              <circle :r="node.radius + 4" fill="transparent" />
              <!-- A ring for the node under the pointer, and a heavier one for the document
                   open in the reader, which stays while the pointer moves on. -->
              <circle
                v-if="node.focused || node.selected"
                :r="node.radius + 3"
                fill="none"
                vector-effect="non-scaling-stroke"
                :stroke-width="node.selected ? 2.5 : 1.5"
                :style="{ stroke: node.color }"
              />
              <g :transform="`scale(${node.radius})`">
                <circle r="1" :mask="node.mask" :style="{ fill: node.color }" />
              </g>
              <text
                v-if="node.label"
                :y="node.radius + 13"
                text-anchor="middle"
                class="doc-graph-label pointer-events-none"
                :class="node.focused || node.selected ? 'doc-graph-label-focus' : ''"
              >
                {{ node.title }}
              </text>
              <title>{{ node.path }}</title>
            </g>
          </g>
        </svg>

        <p class="pointer-events-none absolute bottom-2 left-pane text-small text-faint">
          {{ t('docs.graphPage.help') }}
        </p>
      </template>
    </div>

    <!-- The document a node was clicked for, laid over the graph's right side rather than
         beside it: the graph's box keeps its size, so opening and closing documents never
         re-frames or shifts the picture the reader is navigating. -->
    <aside
      v-if="readingPath"
      class="vxs-pane absolute inset-y-0 right-0 z-10 flex w-[min(40rem,45%)] min-w-72 flex-col border-l border-pane bg-surface shadow-lg"
      :aria-label="t('docs.graphPage.reader.label')"
    >
      <header class="flex shrink-0 items-start gap-2 border-b border-pane px-4 py-2">
        <div class="min-w-0 flex-1">
          <p class="truncate font-medium">{{ reading?.title ?? readingPath }}</p>
          <p class="flex items-center gap-1.5 text-small text-faint">
            <span class="flex shrink-0 items-center gap-1" :title="t('docs.graphPage.reader.readOnlyHint')">
              <BookOpen class="size-3" />
              {{ t('docs.graphPage.reader.readOnly') }}
            </span>
            <span aria-hidden="true">·</span>
            <span class="truncate font-mono">{{ readingPath }}</span>
          </p>
        </div>
        <button
          class="vxs-btn-primary flex h-7 shrink-0 items-center gap-1.5 px-2.5"
          :title="t('docs.graphPage.reader.goToDocumentsHint')"
          @click="goToDocuments"
        >
          <SquarePen class="size-3.5" />
          {{ t('docs.graphPage.reader.goToDocuments') }}
        </button>
        <button
          class="grid size-7 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
          :title="t('common.close')"
          @click="closeReader"
        >
          <X class="size-3.5" />
        </button>
      </header>

      <!-- Keyed on the timestamp as well as the path: a write from an agent or from Documents
           is a new document to read, and a reader is built for one. -->
      <DocReader
        v-if="reading && reading.path === readingPath"
        :key="`${reading.path}@${reading.updated_at}`"
        :path="reading.path"
        :source="reading.content"
        @navigate="followLink"
      />
      <p v-else-if="readingError" class="p-4 text-faint">
        {{ t('docs.graphPage.reader.loadFailed') }}
        <span class="block text-small">{{ readingError }}</span>
      </p>
      <div v-else class="p-4">
        <LoadingBars />
      </div>
    </aside>
    </div>
  </div>
</template>

<style scoped>
/* World-space text: it grows and shrinks with the zoom, as a map's names do, and is ringed in
   the page's own ground so a line passing under it does not cut through the letters. */
.doc-graph-label {
  font-size: 11px;
  fill: var(--vxs-text-muted);
  stroke: var(--vxs-bg-canvas);
  stroke-width: 3px;
  stroke-linejoin: round;
  paint-order: stroke;
}

.doc-graph-label-focus {
  fill: var(--vxs-text-primary);
  font-weight: 600;
}
</style>
