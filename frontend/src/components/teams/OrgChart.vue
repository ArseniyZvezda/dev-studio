<script setup lang="ts">
/**
 * The team as an interactive graph (spec §10.2/§10.4).
 *
 * This replaced a recursive list of rows. The list rendered the hierarchy honestly but could
 * only *show* it: every relationship — who reports to whom, who may hand work sideways to
 * whom — had to be edited in a form on the other side of the screen, one member at a time,
 * which is a poor fit for a structure whose whole content is the relationships. On a canvas
 * the connection **is** the edit: drag from a manager's bottom handle onto someone to make
 * them a report, drag from a right-hand handle onto a peer to open a `can_assign_to` route,
 * click a line to take one away. What the graph draws is exactly what `check_can_assign`
 * enforces, so the picture and the rule cannot drift apart.
 *
 * Two kinds of line, deliberately distinguishable at a glance:
 *   * solid, arrowed, top-to-bottom — the management vertical (`manager_id`);
 *   * dashed, side-to-side — a sideways route (`can_assign_to`).
 *
 * The canvas is always editable (§10.2); each completed gesture saves the roster file.
 *
 * Layout is derived until someone moves a node, and their placement is then kept
 * (`pos_x`/`pos_y` on the member, saved on drop): a team nobody has arranged carries no
 * layout at all, and "Tidy up" takes it back out again rather than pinning everyone to
 * wherever the tidy landed.
 */
import { Background, BackgroundVariant } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import {
  MarkerType,
  Panel,
  VueFlow,
  useVueFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeTypesObject,
} from '@vue-flow/core'
import { LayoutGrid, Unlink } from 'lucide-vue-next'
import { computed, markRaw, ref, watch, type Ref } from 'vue'

import MemberNode from '@/components/teams/MemberNode.vue'
import { t } from '@/i18n'
import { layoutOrgTree, type Point } from '@/lib/org-layout'
import { useTeamsStore } from '@/stores/teams'
import { useThemeStore } from '@/stores/theme'

const emit = defineEmits<{ configure: [string] }>()

const teams = useTeamsStore()
const theme = useThemeStore()
const { onConnect, onEdgeClick, onNodeClick, onNodeDragStop, onPaneClick, fitView } = useVueFlow()

// Vue Flow types custom nodes against its own `NodeProps`, which a plain SFC's inferred
// props never structurally match; the cast is the documented way to register one.
const nodeTypes = { member: markRaw(MemberNode) } as unknown as NodeTypesObject

// Cast rather than `ref<Node[]>()`: Vue Flow's generics blow the checker's instantiation
// depth when run through `UnwrapRef` (see the pipeline canvas for the same workaround).
const nodes = ref([]) as Ref<Node[]>
const selectedEdge = ref<Edge | null>(null)
const hint = ref<string | null>(null)

/** Only a change of *shape* rebuilds the canvas — counts are painted on below. */
const structure = computed(() =>
  teams.members
    .map((member) => `${member.id}:${member.manager_id ?? ''}:${member.name}:${member.kind}`)
    .join('|'),
)

function build(): void {
  const derived = layoutOrgTree(teams.orgTree)
  nodes.value = teams.members.map((member) => ({
    id: member.id,
    type: 'member',
    // A hand-placed node wins over the derived column; a member nobody has moved — one
    // just added, say — still lands somewhere sensible in the tree.
    position:
      member.pos_x !== null && member.pos_y !== null
        ? { x: member.pos_x, y: member.pos_y }
        : (derived.get(member.id) ?? { x: 0, y: 0 }),
    data: {
      member,
      active: 0,
      review: 0,
      waiting: 0,
      editing: true,
      routesToAnyone: (member.can_assign_to ?? []).includes('*'),
      onConfigure: (memberId: string) => emit('configure', memberId),
    },
  }))
  paintCounts()
}

/**
 * Task load, layered onto the existing node objects rather than rebuilt into new ones —
 * a member finishing a task must not reset drag state or selection on the whole canvas.
 */
function paintCounts(): void {
  for (const node of nodes.value) {
    const owned = teams.tasksFor(node.id)
    const member = teams.memberById.get(node.id)
    if (!node.data || !member) continue
    node.data.member = member
    node.data.active = owned.filter((task) => task.status === 'in_progress').length
    node.data.review = owned.filter((task) => task.status === 'needs_review').length
    // A person's inbox is both unreleased and released work: they claim either directly,
    // so the distinction the Pending column draws for agents means nothing here (§10.4).
    node.data.waiting =
      member.kind === 'human'
        ? owned.filter((task) => ['todo', 'pending'].includes(task.status)).length
        : 0
    node.data.routesToAnyone = (member.can_assign_to ?? []).includes('*')
  }
}

watch(structure, build, { immediate: true })
watch(() => teams.tasks, paintCounts, { deep: true })
// A member's *look* is not part of `structure`, so an icon or a colour picked in the rail
// would otherwise sit on the canvas unchanged until something else rebuilt it. Painted on
// rather than rebuilt, for the reason the counts are: a colour must not throw the viewport,
// the selection or an undropped position away.
watch(() => teams.members, paintCounts)

/**
 * Edges are derived from the roster on every change; there is no local edge state to keep
 * in step, because the roster file is the only place a relationship exists.
 *
 * A `can_assign_to` entry that the vertical already covers is skipped: the backend allows a
 * manager and a report to assign to each other regardless, so drawing a second line between
 * them would suggest a route that removing would not close.
 */
const edges = computed<Edge[]>(() => {
  const result: Edge[] = []
  for (const member of teams.members) {
    if (member.manager_id && teams.memberById.has(member.manager_id)) {
      result.push({
        id: `manages:${member.manager_id}->${member.id}`,
        source: member.manager_id,
        sourceHandle: 'reports',
        target: member.id,
        targetHandle: 'manager',
        type: 'smoothstep',
        markerEnd: MarkerType.ArrowClosed,
        data: { kind: 'manages' },
        style: { stroke: 'var(--vxs-border-strong)', strokeWidth: 2 },
      })
    }
    for (const target of member.can_assign_to ?? []) {
      if (target === '*' || !teams.memberById.has(target)) continue
      const peer = teams.memberById.get(target)!
      if (peer.manager_id === member.id || member.manager_id === peer.id) continue
      result.push({
        id: `routes:${member.id}->${target}`,
        source: member.id,
        sourceHandle: 'route-out',
        target,
        targetHandle: 'route-in',
        type: 'smoothstep',
        markerEnd: MarkerType.ArrowClosed,
        data: { kind: 'routes' },
        style: { stroke: 'var(--vxs-accent)', strokeWidth: 1.5, strokeDasharray: '5 4' },
      })
    }
  }
  return result
})

const selectedEdgeLabel = computed(() => {
  const edge = selectedEdge.value
  if (!edge) return ''
  const source = teams.memberById.get(edge.source)?.name ?? edge.source
  const target = teams.memberById.get(edge.target)?.name ?? edge.target
  return edge.data?.kind === 'manages'
    ? t('teams.chart.reportsToEdge', { target, source })
    : t('teams.chart.mayAssignEdge', { source, target })
})

onConnect(async (connection: Connection) => {
  hint.value = null
  const { source, target, sourceHandle, targetHandle } = connection
  if (!source || !target || source === target) return

  if (sourceHandle === 'reports' && targetHandle === 'manager') {
    // Offering only legal managers is the same guard the editor's dropdown applies; the
    // backend refuses a circular line either way, this just says so before the request.
    if (!teams.managerCandidates(target).some((candidate) => candidate.id === source)) {
      hint.value = t('teams.chart.circular')
      return
    }
    await teams.saveMember(target, { manager_id: source })
    return
  }

  if (sourceHandle === 'route-out' && targetHandle === 'route-in') {
    const actor = teams.memberById.get(source)
    if (!actor) return
    const routes = actor.can_assign_to ?? []
    if (routes.includes('*')) {
      hint.value = t('teams.chart.alreadyAnyone', { name: actor.name })
      return
    }
    if (routes.includes(target)) return
    await teams.saveMember(source, { can_assign_to: [...routes, target] })
    return
  }

  hint.value = t('teams.chart.cannotJoin')
})

onEdgeClick(({ edge }) => {
  selectedEdge.value = edge as Edge
  hint.value = null
})

onNodeClick(({ node }) => {
  selectedEdge.value = null
  emit('configure', node.id)
})

/**
 * A drop is the save. `nodes` is everything the gesture moved, not just the one under the
 * cursor, so a multi-node drag round-trips in one request.
 */
onNodeDragStop(({ nodes: moved }) => {
  const positions: Record<string, Point> = {}
  for (const node of moved) {
    positions[node.id] = { x: Math.round(node.position.x), y: Math.round(node.position.y) }
  }
  void teams.saveLayout(positions)
})

onPaneClick(() => {
  selectedEdge.value = null
  hint.value = null
})

async function disconnect(): Promise<void> {
  const edge = selectedEdge.value
  if (!edge) return
  selectedEdge.value = null
  if (edge.data?.kind === 'manages') {
    await teams.saveMember(edge.target, { manager_id: null })
    return
  }
  const actor = teams.memberById.get(edge.source)
  if (!actor) return
  await teams.saveMember(edge.source, {
    can_assign_to: (actor.can_assign_to ?? []).filter((entry) => entry !== edge.target),
  })
}

/** Forget every hand placement and derive the layout again — see `lib/org-layout.ts`. */
async function tidy(): Promise<void> {
  const cleared: Record<string, null> = {}
  for (const member of teams.members) {
    if (member.pos_x !== null || member.pos_y !== null) cleared[member.id] = null
  }
  await teams.saveLayout(cleared)
  build()
  void fitView({ padding: 0.2 })
}
</script>

<template>
  <div class="relative h-full min-w-0">
    <VueFlow
      v-model:nodes="nodes"
      :edges="edges"
      :node-types="nodeTypes"
      :min-zoom="0.3"
      :max-zoom="1.6"
      :default-viewport="{ zoom: 0.9 }"
      :connection-radius="30"
      fit-view-on-init
    >
      <Background v-if="theme.canvasGrid !== 'none'" :variant="theme.canvasGrid === 'lines' ? BackgroundVariant.Lines : BackgroundVariant.Dots" pattern-color="var(--vxs-canvas-dot)" :gap="18" />
      <!-- The Team page is always editable; the canvas does not offer a separate lock. -->
      <Controls :show-interactive="false" />

      <Panel position="top-left" class="flex flex-wrap items-center gap-3 text-small">
        <span class="flex items-center gap-1.5 text-faint">
          <span class="inline-block h-0.5 w-6" :style="{ background: 'var(--vxs-border-strong)' }" />
          {{ t('teams.chart.legendReportsTo') }}
        </span>
        <span class="flex items-center gap-1.5 text-faint">
          <span
            class="inline-block h-0 w-6 border-t border-dashed"
            :style="{ borderColor: 'var(--vxs-accent)' }"
          />
          {{ t('teams.chart.legendMayAssign') }}
        </span>
      </Panel>

      <Panel position="top-right">
        <button
          class="flex items-center gap-1.5 rounded-md border border-line bg-surface px-2 py-1 text-small shadow-md hover:bg-hover"
          :title="t('teams.chart.tidyTitle')"
          @click="tidy"
        >
          <LayoutGrid class="size-3.5" /> {{ t('teams.chart.tidy') }}
        </button>
      </Panel>

      <Panel v-if="selectedEdge" position="bottom-center">
        <div
          class="flex items-center gap-2 rounded-md border border-line bg-surface px-2 py-1.5 shadow-lg"
        >
          <span class="truncate">{{ selectedEdgeLabel }}</span>
          <button
            class="flex items-center gap-1.5 rounded-md border border-line px-2 py-1 text-danger hover:bg-danger-soft disabled:opacity-50"
            :disabled="teams.busy"
            @click="disconnect"
          >
            <Unlink class="size-3.5" /> {{ t('teams.chart.disconnect') }}
          </button>
        </div>
      </Panel>

      <Panel v-else-if="hint" position="bottom-center">
        <p class="rounded-md bg-warning-soft px-2 py-1.5 text-warning shadow-lg">{{ hint }}</p>
      </Panel>

      <Panel v-else position="bottom-center">
        <p class="rounded-md bg-surface px-2 py-1 text-small text-faint">
          {{ t('teams.chart.editingHelp') }}
        </p>
      </Panel>

    </VueFlow>
  </div>
</template>

<style scoped>
/* The canvas has to fill the pane for `fitView` to have anything to fit to. */
:deep(.vue-flow) {
  height: 100%;
}

/* A clicked line is the thing being acted on, so it has to be visibly the selection. */
:deep(.vue-flow__edge.selected .vue-flow__edge-path) {
  stroke: var(--vxs-accent);
  stroke-width: 3;
}

:deep(.vue-flow__edge-path) {
  cursor: pointer;
}
</style>
