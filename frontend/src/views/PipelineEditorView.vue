<script setup lang="ts">
/**
 * The pipeline workspace (spec §6.3): canvas, palette, per-node config, live status
 * and logs.
 *
 * The document (`definition`) stays the source of truth for everything the YAML cares
 * about; the canvas owns only geometry and connections, which are folded back in on
 * save. Run status is layered onto the nodes' `data` by a watcher rather than being
 * rebuilt into new node objects, so a status change never disturbs drag state or
 * selection.
 */
import { Background, BackgroundVariant } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import {
  VueFlow,
  useVueFlow,
  type Edge,
  type NodeChange,
  type NodeTypesObject,
} from '@vue-flow/core'
import { MiniMap } from '@vue-flow/minimap'
import { ArrowLeft, Ban, History, Play, Save, SkipForward } from 'lucide-vue-next'
import {
  computed,
  markRaw,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
  watch,
  type Ref,
} from 'vue'
import { useRouter } from 'vue-router'

import type { NodeInstance, NodeTypeSchema, PipelineDefinition, PipelineEdge } from '@/api/types'
import LogPanel from '@/components/pipeline/LogPanel.vue'
import RunHistory from '@/components/pipeline/RunHistory.vue'
import NodePalette from '@/components/pipeline/NodePalette.vue'
import ResizeHandle from '@/components/ui/ResizeHandle.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { t } from '@/i18n'
import {
  edgeId,
  flowNodeType,
  incomingPorts,
  nextNodeId,
  toDefinition,
  toFlowEdges,
  toFlowNodes,
  type FlowNode,
} from '@/lib/graph'
import NodeConfigPanel from '@/node-renderer/NodeConfigPanel.vue'
import GenericNode from '@/node-renderer/GenericNode.vue'
import { pluginComponent } from '@/plugin-runtime/loader'
import { provideNodeParamWriter } from '@/plugin-runtime/node-params'
import { usePipelinesStore } from '@/stores/pipelines'
import { useRunsStore } from '@/stores/runs'
import { useThemeStore } from '@/stores/theme'

const props = defineProps<{ id: string }>()

const router = useRouter()
const pipelines = usePipelinesStore()
const runs = useRunsStore()
const theme = useThemeStore()

const { onConnect, onNodeClick, onPaneClick, screenToFlowCoordinate, fitView, getViewport } =
  useVueFlow()

const definition = shallowRef<PipelineDefinition | null>(null)
const instances = ref<NodeInstance[]>([])
// Cast rather than `ref<FlowNode[]>()`: Vue Flow's `Node`/`Edge` are generic enough
// that running them through `UnwrapRef` blows the checker's instantiation depth.
const nodes = ref([]) as Ref<FlowNode[]>
const edges = ref([]) as Ref<Edge[]>
const selectedNodeId = ref<string | null>(null)
const historyOpen = ref(false)
// Rail widths live here rather than in the panels, because it is the canvas between them
// that gives the pixels up. They are session state on purpose: a width is a way of looking
// at one pipeline for a minute, not a property of the project, and appearance that outlives
// the visit belongs in `config.yaml` with the theme or nowhere at all.
const paletteWidth = ref(256)
const configWidth = ref(320)
const dirty = ref(false)
const saving = ref(false)
const loadError = ref<string | null>(null)

// Vue Flow types custom nodes against its own `NodeProps`, which a plain SFC's inferred
// props never structurally match; the cast is the documented way to register one.
//
// A plugin declaring `frontend.nodes[].component` gets its module mounted here under its
// own key (§9.3); everything else falls back to the schema-driven generic node, which is
// what keeps a plugin working with no frontend code at all.
const nodeTypes = computed(() => {
  const types: Record<string, unknown> = { generic: markRaw(GenericNode) }
  for (const schema of pipelines.nodeTypes) {
    if (!schema.ui_component_url) continue
    types[flowNodeType(schema)] = pluginComponent(
      schema.ui_component_url,
      schema.plugin_id,
      `Node component for ${schema.id}`,
    )
  }
  return types as unknown as NodeTypesObject
})

const selectedInstance = computed(
  () => instances.value.find((node) => node.id === selectedNodeId.value) ?? null,
)
const selectedSchema = computed(() =>
  selectedInstance.value ? (pipelines.nodeTypeById.get(selectedInstance.value.type) ?? null) : null,
)

const nodeLabels = computed(() =>
  Object.fromEntries(instances.value.map((node) => [node.id, node.label ?? node.type])),
)

// Annotated explicitly: inferring this from Vue Flow's deeply generic `Edge` sends the
// checker into an unbounded instantiation.
const pipelineEdges = computed<PipelineEdge[]>(() =>
  edges.value.map((edge) => ({
    id: edge.id,
    from: { node: edge.source, port: edge.sourceHandle ?? '' },
    to: { node: edge.target, port: edge.targetHandle ?? '' },
  })),
)

onMounted(async () => {
  pipelines.subscribe()
  runs.subscribe()
  // Scope adopted runs to this pipeline, so a run started from the CLI for a different
  // one does not repaint this canvas.
  runs.watchPipeline(props.id)
  await load()
})

async function load() {
  try {
    const loaded = await pipelines.load(props.id)
    definition.value = loaded
    // Copied, not aliased: the config panel edits these objects, and `definition` is what
    // a save is diffed against.
    instances.value = loaded.nodes.map((node) => ({
      ...node,
      params: { ...node.params },
      pins: { ...node.pins },
    }))
    nodes.value = toFlowNodes(loaded, {
      schemaFor: (typeId) => pipelines.nodeTypeById.get(typeId),
      colorFor: (typeId) => pipelines.colorFor(typeId),
    })
    edges.value = toFlowEdges(loaded)
    dirty.value = false
    await runs.loadHistory(props.id)
    // Show the newest run's outcome straight away, so opening a pipeline explains
    // what happened last time rather than showing a blank canvas.
    const latest = runs.history[0]
    if (latest) await runs.openRun(latest.id)
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : String(cause)
  }
}

// Paint live run state onto the existing node objects.
//
// Outputs ride along with status because for some node types the result *is* the display —
// `preview3d.view` renders the file it was handed, and this is the only place the resolved
// path exists once the run has moved on. Progress rides along for the opposite reason: it
// exists only while the node runs, and the store drops it the moment the node finishes.
watch(
  [
    () => runs.nodeStatus,
    () => runs.nodeError,
    () => runs.fromCache,
    () => runs.nodeOutputs,
    () => runs.nodeProgress,
  ],
  ([statuses, errors, cached, outputs, progress]) => {
    for (const node of nodes.value) {
      if (!node.data) continue
      node.data.status = statuses[node.id]
      node.data.error = errors[node.id] ?? null
      node.data.fromCache = cached[node.id] ?? false
      node.data.outputs = outputs[node.id]
      node.data.progress = progress[node.id] ?? null
    }
  },
  { deep: true },
)

// Keep labels, the disabled flag, params and pins in step with edits made in the config
// panel. Params are on the node so a plugin's custom component can render what the node is
// actually configured with; the generic renderer only needs the schema.
watch(
  instances,
  (list) => {
    for (const instance of list) {
      const node = nodes.value.find((candidate) => candidate.id === instance.id)
      if (!node?.data) continue
      node.data.label = instance.label ?? node.data.schema?.name ?? instance.type
      node.data.disabled = instance.disabled
      node.data.params = instance.params
      node.data.pins = instance.pins
      node.data.pauseOnComplete = instance.pause_on_complete
    }
  },
  { deep: true },
)

/**
 * Which input ports have an edge into them, painted on like everything else.
 *
 * The generic node draws a handle for anything connected whether or not it is pinned, so a
 * hand-edited pipeline still renders its edges — and Vue Flow anchors an edge to a mounted
 * handle or to nothing, which is the failure this avoids.
 */
watch(
  pipelineEdges,
  (list) => {
    const incoming = incomingPorts(list)
    for (const node of nodes.value) {
      if (!node.data) continue
      node.data.connected = incoming.get(node.id) ?? []
    }
  },
  { immediate: true },
)

onConnect((connection) => {
  if (!connection.source || !connection.target) return
  const edge: Edge = {
    id: edgeId({
      id: null,
      from: { node: connection.source, port: connection.sourceHandle ?? '' },
      to: { node: connection.target, port: connection.targetHandle ?? '' },
    }),
    source: connection.source,
    sourceHandle: connection.sourceHandle,
    target: connection.target,
    targetHandle: connection.targetHandle,
  }
  // A target port takes exactly one value, so a new connection replaces the old one.
  edges.value = [
    ...edges.value.filter(
      (existing) =>
        !(existing.target === edge.target && existing.targetHandle === edge.targetHandle),
    ),
    edge,
  ]
  dirty.value = true
})

onNodeClick(({ node }) => {
  selectedNodeId.value = node.id
})

onPaneClick(() => {
  selectedNodeId.value = null
})

function addNode(schema: NodeTypeSchema) {
  const id = nextNodeId(instances.value)
  // Drop new nodes into the middle of the current view, not at the origin.
  const position = screenToFlowCoordinate({
    x: window.innerWidth / 2,
    y: window.innerHeight / 2,
  })
  const instance: NodeInstance = {
    id,
    type: schema.id,
    label: null,
    position,
    params: {},
    pins: {},
    pause_on_complete: false,
    disabled: false,
  }
  instances.value = [...instances.value, instance]
  nodes.value = [
    ...nodes.value,
    {
      id,
      type: flowNodeType(schema),
      position,
      data: {
        schema,
        label: schema.name,
        typeId: schema.id,
        accent: pipelines.colorFor(schema.id),
        disabled: false,
        params: instance.params,
        pins: instance.pins,
        pauseOnComplete: false,
        connected: [],
      },
    },
  ]
  selectedNodeId.value = id
  dirty.value = true
}

/**
 * Vue Flow reports measurement and selection through the same channel as real edits —
 * `dimensions` fires as soon as nodes mount and `select` on every click. Treating those
 * as changes marked a freshly opened pipeline dirty, which then made every Run rewrite
 * the user's YAML for nothing.
 */
function onNodesChange(changes: NodeChange[]) {
  if (changes.some((change) => ['position', 'add', 'remove'].includes(change.type))) {
    dirty.value = true
  }
}

function updateNode(patch: Partial<NodeInstance>) {
  if (!selectedNodeId.value) return
  instances.value = instances.value.map((node) =>
    node.id === selectedNodeId.value ? { ...node, ...patch } : node,
  )
  dirty.value = true
}

/**
 * Drop the edge into a port that has just stopped being a pin.
 *
 * The panel decides *that* it should go and the canvas is what owns edges, so the panel
 * says so and this removes it. Leaving it would produce an edge the runner still honours
 * and the canvas cannot draw — the graph would do something the picture does not show.
 */
function disconnectPort(portId: string) {
  const id = selectedNodeId.value
  if (!id) return
  const before = edges.value.length
  edges.value = edges.value.filter(
    (edge) => !(edge.target === id && edge.targetHandle === portId),
  )
  if (edges.value.length !== before) dirty.value = true
}

function removeNode() {
  const id = selectedNodeId.value
  if (!id) return
  instances.value = instances.value.filter((node) => node.id !== id)
  nodes.value = nodes.value.filter((node) => node.id !== id)
  edges.value = edges.value.filter((edge) => edge.source !== id && edge.target !== id)
  selectedNodeId.value = null
  dirty.value = true
}

async function save() {
  if (!definition.value) return
  saving.value = true
  try {
    const viewport = getViewport()
    const next = toDefinition(definition.value, instances.value, nodes.value, edges.value, viewport)
    await pipelines.save(next)
    definition.value = next
    dirty.value = false
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    saving.value = false
  }
}

async function run(startNodeId: string | null = null) {
  // Persist first: the backend runs the document on disk, not the canvas in the browser.
  if (dirty.value) await save()
  await runs.start(props.id, { startNodeId })
}

async function openRun(runId: string) {
  historyOpen.value = false
  await runs.openRun(runId)
}

/**
 * Delete removes the selected node.
 *
 * Bound on the window rather than on the canvas because Vue Flow's pane is not focusable and
 * a click on a node leaves focus wherever it was, so a listener on the flow would fire for
 * almost nobody. The guard is therefore the other half of it: a `Delete` typed into the
 * palette's search box or a config field is a character, not a command.
 *
 * Vue Flow's own `delete-key-code` is switched off on the canvas for the same reason this
 * exists — it removes nodes from `nodes` alone, leaving the instance behind in `instances`,
 * and `instances` is what gets saved.
 */
function isTypingInto(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null
  if (!element) return false
  return (
    element.isContentEditable ||
    element.tagName === 'INPUT' ||
    element.tagName === 'TEXTAREA' ||
    element.tagName === 'SELECT'
  )
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Delete') return
  if (isTypingInto(event.target)) return
  if (!selectedNodeId.value) return
  event.preventDefault()
  removeNode()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

/**
 * Let a plugin's own node component edit the instance it is drawing (`runtime.setNodeParams`).
 *
 * It goes through the same path as the config panel — a new params object on the instance,
 * and `dirty` set — because a plugin writing into `data.params` directly would mutate the
 * document without the editor noticing, and the change would vanish on the next save.
 * Withdrawn on unmount, so a stale editor can never be written into.
 */
const withdrawParamWriter = provideNodeParamWriter((nodeId, patch) => {
  if (!instances.value.some((node) => node.id === nodeId)) return
  instances.value = instances.value.map((node) =>
    node.id === nodeId ? { ...node, params: { ...node.params, ...patch } } : node,
  )
  dirty.value = true
})
onBeforeUnmount(withdrawParamWriter)
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="vxs-pane flex shrink-0 items-center gap-2 border-b border-pane bg-surface px-2 py-1.5">
      <button
        class="grid size-7 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
        :title="t('pipelines.editor.back')"
        @click="router.push('/pipelines')"
      >
        <ArrowLeft class="size-4" />
      </button>

      <div class="min-w-0">
        <p class="truncate font-medium">{{ definition?.name ?? props.id }}</p>
        <p class="truncate text-small text-faint">
          {{ t('pipelines.counts.nodes', { count: instances.length }) }} ·
          {{ t('pipelines.counts.edges', { count: edges.length }) }}
          <span v-if="dirty" class="text-warning">· {{ t('pipelines.editor.unsaved') }}</span>
        </p>
      </div>

      <div class="mx-2 flex-1">
        <StatusBadge v-if="runs.currentRun" :status="runs.currentRun.status" />
      </div>

      <div class="relative">
        <button class="toolbar-button" @click="historyOpen = !historyOpen">
          <History class="size-3.5" /> {{ t('pipelines.editor.history') }}
        </button>
        <RunHistory
          v-if="historyOpen"
          :runs="runs.history"
          @select="openRun"
          @close="historyOpen = false"
        />
      </div>

      <button class="toolbar-button" :disabled="!dirty || saving" @click="save">
        <Save class="size-3.5" /> {{ saving ? t('common.saving') : t('common.save') }}
      </button>

      <button
        v-if="runs.isPaused"
        class="toolbar-button toolbar-button-primary"
        :title="
          runs.pausedNodes.length
            ? t('pipelines.editor.pausedAt', {
                nodes: runs.pausedNodes.map((id) => nodeLabels[id] ?? id).join(', '),
              })
            : undefined
        "
        @click="runs.resume()"
      >
        <SkipForward class="size-3.5" /> {{ t('pipelines.editor.continue') }}
      </button>
      <button
        v-if="runs.isRunning"
        class="toolbar-button text-danger"
        @click="runs.cancel()"
      >
        <Ban class="size-3.5" /> {{ t('pipelines.editor.cancelRun') }}
      </button>
      <button
        v-else
        class="toolbar-button toolbar-button-primary"
        :disabled="runs.starting || !instances.length"
        @click="run()"
      >
        <Play class="size-3.5" /> {{ t('pipelines.editor.run') }}
      </button>
    </header>

    <p v-if="loadError" class="shrink-0 bg-danger-soft px-pane py-1 text-danger">
      {{ loadError }}
    </p>
    <p v-if="runs.error" class="shrink-0 bg-danger-soft px-pane py-1 text-danger">
      {{ runs.error }}
    </p>
    <p
      v-for="issue in pipelines.issues"
      :key="issue.message"
      class="shrink-0 bg-warning-soft px-pane py-1 text-warning"
    >
      {{ issue.node_id ? `${issue.node_id}: ` : '' }}{{ issue.message }}
    </p>

    <div class="flex min-h-0 flex-1">
      <NodePalette :style="{ width: `${paletteWidth}px` }" @add="addNode" />
      <ResizeHandle v-model="paletteWidth" side="right" :min="180" :max="480" />

      <!-- The canvas is the page's one band (`vxs-enter`, §7); the palette and the config panel
           beside it are panes and stay still. -->
      <div class="vxs-enter relative min-w-0 flex-1">
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          :node-types="nodeTypes"
          :min-zoom="0.2"
          :max-zoom="2"
          fit-view-on-init
          :delete-key-code="null"
          @nodes-change="onNodesChange"
        >
          <Background v-if="theme.canvasGrid !== 'none'" :variant="theme.canvasGrid === 'lines' ? BackgroundVariant.Lines : BackgroundVariant.Dots" pattern-color="var(--vxs-canvas-dot)" :gap="18" />
          <Controls />
          <MiniMap pannable zoomable />
        </VueFlow>

        <button
          v-if="instances.length"
          class="absolute right-3 bottom-3 rounded-md border border-line bg-surface px-2 py-1 text-small shadow-md hover:bg-hover"
          @click="fitView()"
        >
          {{ t('pipelines.editor.fitView') }}
        </button>
      </div>

      <template v-if="selectedInstance">
        <ResizeHandle v-model="configWidth" side="left" :min="240" :max="640" />
        <aside class="shrink-0" :style="{ width: `${configWidth}px` }">
          <NodeConfigPanel
            :node="selectedInstance"
            :schema="selectedSchema"
            :edges="pipelineEdges"
            :status="runs.nodeStatus[selectedInstance.id]"
            :error="runs.nodeError[selectedInstance.id]"
            @update="updateNode"
            @remove="removeNode"
            @disconnect="disconnectPort"
            @run-from="run(selectedInstance.id)"
          />
        </aside>
      </template>
    </div>

    <LogPanel :node-labels="nodeLabels" />
  </div>
</template>

<style scoped>
.toolbar-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--vxs-border);
  border-radius: var(--vxs-radius-md);
  white-space: nowrap;
}

/* Under the pointer the fill and the edge each step up, as a secondary button's do (§7). */
.toolbar-button:hover:not(:disabled) {
  background: var(--vxs-bg-hover);
  border-color: var(--vxs-border-strong);
}

.toolbar-button:disabled {
  opacity: 0.5;
}

/* Run and Continue: the primary action's grey with a lit edge (`.vxs-btn-primary` in
   `base.css`), in the toolbar's own shape — its edge's step included. */
.toolbar-button-primary {
  background: var(--vxs-accent-gradient);
  border-color: var(--vxs-border-strong);
  color: var(--vxs-text-primary);
}

.toolbar-button-primary:hover:not(:disabled) {
  background: var(--vxs-accent-gradient-hover);
  border-color: color-mix(in oklab, var(--vxs-border-strong), var(--vxs-text-primary) 12%);
}
</style>
