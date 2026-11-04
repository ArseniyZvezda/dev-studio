/**
 * Translation between the backend's pipeline document and Vue Flow's graph model.
 *
 * The two disagree about edges: the backend describes them as
 * `{from: {node, port}, to: {node, port}}` (which is what the YAML on disk looks like,
 * and is readable in a diff), while Vue Flow wants flat
 * `{source, sourceHandle, target, targetHandle}`. Keeping the conversion in one place
 * means the canvas never has to think about the wire format and the saved YAML never
 * picks up canvas-specific fields.
 */

import type { Edge, Node } from '@vue-flow/core'

import type { NodeInstance, PipelineDefinition, PipelineEdge } from '@/api/types'

export interface GenericNodeData {
  schema: import('@/api/types').NodeTypeSchema | null
  label: string
  typeId: string
  accent: string
  /** The instance's configured values — what a custom node shows without the config panel. */
  params?: Record<string, unknown>
  /**
   * Which inputs this node shows a handle for, overriding `PortSchema.connectable`.
   * A port with no entry follows the schema.
   */
  pins?: Record<string, boolean>
  /**
   * Input ports that currently have an edge into them, painted on by the canvas.
   *
   * A pin the user folded away can still be connected — a hand-edited YAML says so, and the
   * config panel unpins by removing the edge, which is a separate transaction. Drawing the
   * union means an edge always has the handle it is anchored to, rather than Vue Flow
   * hunting for a handle that is not mounted.
   */
  connected?: string[]
  /** Set when the node holds the run open once it succeeds. */
  pauseOnComplete?: boolean
  status?: import('@/api/types').NodeStatus
  error?: string | null
  fromCache?: boolean
  /** What the node emitted on its last run — painted on beside `status` by the canvas. */
  outputs?: Record<string, unknown>
  /** How far along a running node says it is; null once it is not running. */
  progress?: import('@/api/types').NodeProgress | null
  disabled?: boolean
  [key: string]: unknown
}

export type FlowNode = Node<GenericNodeData>

/** Mirrors the id the backend derives when an edge carries none. */
export function edgeId(edge: PipelineEdge): string {
  return edge.id ?? `${edge.from.node}:${edge.from.port}->${edge.to.node}:${edge.to.port}`
}

/**
 * Vue Flow node-type key for a node instance.
 *
 * A plugin that ships its own component (`ui_component`) gets a key of its own so the
 * canvas can mount it; everything else renders through the schema-driven generic node.
 */
export function flowNodeType(
  schema: import('@/api/types').NodeTypeSchema | null | undefined,
): string {
  return schema?.ui_component_url ? `plugin:${schema.id}` : 'generic'
}

export function toFlowNodes(
  definition: PipelineDefinition,
  lookup: {
    schemaFor: (typeId: string) => import('@/api/types').NodeTypeSchema | undefined
    colorFor: (typeId: string) => string
  },
): FlowNode[] {
  const incoming = incomingPorts(definition.edges)
  return definition.nodes.map((instance) => {
    const schema = lookup.schemaFor(instance.type) ?? null
    return {
      id: instance.id,
      type: flowNodeType(schema),
      position: { x: instance.position.x, y: instance.position.y },
      data: {
        schema,
        label: instance.label ?? schema?.name ?? instance.type,
        typeId: instance.type,
        accent: lookup.colorFor(instance.type),
        disabled: instance.disabled,
        params: instance.params,
        pins: instance.pins,
        pauseOnComplete: instance.pause_on_complete,
        connected: incoming.get(instance.id) ?? [],
      },
    }
  })
}

/** Input ports with an edge into them, keyed by node id. */
export function incomingPorts(edges: PipelineEdge[]): Map<string, string[]> {
  const map = new Map<string, string[]>()
  for (const edge of edges) {
    const ports = map.get(edge.to.node)
    if (ports) ports.push(edge.to.port)
    else map.set(edge.to.node, [edge.to.port])
  }
  return map
}

/**
 * Whether a port is a pin on this node: the instance's answer, or the node type's.
 *
 * The same rule as `NodeInstance.pin_enabled` on the backend, and it has to stay the same
 * rule — the canvas decides which handles exist and the runner decides which edges mean
 * anything, and a disagreement is an edge the user can draw and the engine ignores.
 */
export function isPinned(
  port: import('@/api/types').PortSchema,
  pins: Record<string, boolean> | undefined,
): boolean {
  const override = pins?.[port.id]
  return override === undefined ? port.connectable : override
}

export function toFlowEdges(definition: PipelineDefinition): Edge[] {
  return definition.edges.map((edge) => ({
    id: edgeId(edge),
    source: edge.from.node,
    sourceHandle: edge.from.port,
    target: edge.to.node,
    targetHandle: edge.to.port,
  }))
}

/**
 * Fold canvas state back into the document. `params`, `pins`, `pause_on_complete`,
 * `disabled` and the label are tracked on the instances themselves (the config panel edits
 * those directly), so only geometry and connections need reading back off the canvas.
 */
export function toDefinition(
  base: PipelineDefinition,
  instances: NodeInstance[],
  flowNodes: FlowNode[],
  flowEdges: Edge[],
  viewport: { x: number; y: number; zoom: number },
): PipelineDefinition {
  const positions = new Map(flowNodes.map((node) => [node.id, node.position]))

  return {
    ...base,
    nodes: instances.map((instance) => ({
      ...instance,
      position: positions.get(instance.id) ?? instance.position,
    })),
    edges: flowEdges.map<PipelineEdge>((edge) => ({
      id: null,
      from: { node: edge.source, port: edge.sourceHandle ?? '' },
      to: { node: edge.target, port: edge.targetHandle ?? '' },
    })),
    viewport: { x: viewport.x, y: viewport.y, zoom: viewport.zoom },
  }
}

/** A short, collision-free id for a newly dropped node. */
export function nextNodeId(existing: NodeInstance[]): string {
  const used = new Set(existing.map((node) => node.id))
  let index = existing.length + 1
  while (used.has(`n${index}`)) index += 1
  return `n${index}`
}
