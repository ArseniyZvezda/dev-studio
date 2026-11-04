/**
 * The live force simulation behind Documents → Graph, the whole-corpus picture drawn on a page
 * of its own (block-editor spec §5.1.1).
 *
 * Not `lib/doc-graph.ts`, and deliberately so. That one settles a neighbourhood once, inside a
 * `computed`, because it shares a main thread with ProseMirror and must never animate. This
 * page has no editor beside it and is *about* the picture, so it runs the way Obsidian's does:
 * a simulation stepped once per frame that cools to a stop, reheats when a setting moves or a
 * node is dragged, and costs nothing once it is still. Pure arithmetic, no Vue and no DOM, so
 * what each slider does to the shape is pinned in `doc-graph-sim.spec.ts` by data.
 *
 * The integrator is d3-force's — velocity Verlet with a cooling `alpha` and a velocity decay —
 * written out here rather than imported, for `DocLinkGraph.vue`'s reason: a handful of forces over
 * a few hundred dots are not worth a dependency. Repulsion is the plain `n²` pass: a corpus is tens to
 * low hundreds of documents (49 documents give ~1.2k pairs a tick), where a quadtree would be more
 * code than time saved.
 *
 * Coordinates are world pixels around the origin; the page owns the camera.
 */

import type { DocGraph } from '@/api/types'
import { BOX, seedPosition } from '@/lib/doc-graph'

/** The five knobs above the graph. */
export interface GraphForces {
  /** How hard a link pulls its two documents together — a multiple of d3's default spring. */
  linkStrength: number
  /** How hard documents of one folder pull towards that folder's middle. */
  folderStrength: number
  /**
   * How hard two documents with no link between them push each other apart, on top of the
   * repulsion every pair has — a multiple of `UNLINKED_PUSH`.
   */
  unlinkedRepel: number
  /** A node's radius, in world pixels. */
  nodeSize: number
  /** The length a link rests at, in world pixels. */
  linkDistance: number
}

export interface ForceRange {
  min: number
  max: number
  step: number
}

export const FORCE_LIMITS: Readonly<Record<keyof GraphForces, ForceRange>> = {
  linkStrength: { min: 0, max: 2, step: 0.05 },
  folderStrength: { min: 0, max: 2, step: 0.05 },
  unlinkedRepel: { min: 0, max: 3, step: 0.05 },
  nodeSize: { min: 3, max: 24, step: 1 },
  linkDistance: { min: 20, max: 300, step: 5 },
}

export const DEFAULT_FORCES: Readonly<GraphForces> = {
  linkStrength: 1,
  folderStrength: 0,
  unlinkedRepel: 1,
  nodeSize: 8,
  linkDistance: 180,
}

/**
 * Whatever came out of `localStorage`, made into five numbers inside their ranges — a key a
 * reader edited by hand, or one from a build with different limits, falls back key by key.
 */
export function clampForces(raw: unknown): GraphForces {
  const source = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const out = { ...DEFAULT_FORCES }
  for (const key of Object.keys(FORCE_LIMITS) as (keyof GraphForces)[]) {
    const value = source[key]
    if (typeof value !== 'number' || !Number.isFinite(value)) continue
    const { min, max } = FORCE_LIMITS[key]
    out[key] = Math.min(max, Math.max(min, value))
  }
  return out
}

export interface SimNode {
  path: string
  title: string
  /** The folder the document sits in, `''` at the top of the docs folder. */
  folder: string
  x: number
  y: number
  vx: number
  vy: number
  /** Where a drag is holding the node, or `null` when the forces have it. */
  fx: number | null
  fy: number | null
  /** Links in either direction, after de-duplication — what the spring is divided by. */
  degree: number
  /** How much larger than `nodeSize` this node is drawn: 1 with no links, up to `MAX_GROWTH`. */
  scale: number
}

export interface SimLink {
  /** Unordered `a|b`, so a reciprocal pair is one spring and one line. */
  key: string
  source: number
  target: number
}

export interface Simulation {
  nodes: SimNode[]
  links: SimLink[]
  /** For each node, the indices it is linked to — what hover highlights. */
  neighbours: Set<number>[]
  forces: GraphForces
  alpha: number
  /** Where `alpha` is heading: 0 to cool to a stop, above it to keep moving under a drag. */
  alphaTarget: number
}

const ALPHA_MIN = 0.001
/** d3's: about 300 ticks from hot to still. */
const ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1 / 300)
const VELOCITY_DECAY = 0.4
/** What the folder slider multiplies — at 1 a node moves 8% of the way to its folder per hot tick. */
const FOLDER_PULL = 0.08
/** A faint pull to the origin, so a document nothing links to drifts back rather than away. */
const GRAVITY = 0.03
/** Beyond this, two documents do not push each other: a stray component should not fling the rest. */
const REPEL_RANGE = 900
/** How firmly two overlapping circles are separated per tick. */
const COLLIDE = 0.7
/**
 * What the unlinked-repulsion slider multiplies: the extra push between two documents nothing
 * links. Of the order of the base charge, so at 100% an unrelated pair pushes about twice as
 * hard as a linked one — clusters part from each other while a link still holds its pair close.
 */
const UNLINKED_PUSH = 250
/** The most linked document in the corpus is drawn this much larger than one with no links. */
export const MAX_GROWTH = 1.75

/**
 * A node's size for its link count, relative to the corpus's most linked document.
 *
 * Square-root rather than linear, because a circle is read by its area: on a corpus with one
 * hub of eighty links, linear growth would leave every ordinary document indistinguishable from
 * an orphan. The scale is relative so the slider's size stays what an unlinked document is, and
 * the hub is always exactly `MAX_GROWTH` of it whatever the corpus.
 */
export function growth(degree: number, maxDegree: number): number {
  if (maxDegree <= 0 || degree <= 0) return 1
  return 1 + (MAX_GROWTH - 1) * Math.sqrt(Math.min(degree, maxDegree) / maxDegree)
}

/** A node's drawn radius, in world pixels. */
export function radiusOf(node: SimNode, forces: GraphForces): number {
  return forces.nodeSize * node.scale
}

/** Bigger dots need more room, so repulsion grows with them. */
function charge(forces: GraphForces): number {
  return -(150 + 12 * forces.nodeSize)
}

export function folderOf(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut < 0 ? '' : path.slice(0, cut)
}

/**
 * Where a picture's documents were, apart from the simulation that put them there — what a
 * refresh carries into the next simulation, and what the page keeps between visits.
 */
export interface Placement {
  /** World position by document path. */
  positions: Record<string, [number, number]>
  /** `linkDigest` of the links those positions settled under. */
  links: string
  /** How hot the picture still was: 0 once it had come to rest. */
  alpha: number
}

/** FNV-1a over the sorted link keys: whether two pictures were shaped by the same links. */
function linkDigest(keys: string[]): string {
  let hash = 0x811c9dc5
  for (const key of [...keys].sort()) {
    for (let i = 0; i < key.length; i += 1) {
      hash ^= key.charCodeAt(i)
      hash = Math.imul(hash, 0x01000193)
    }
    hash ^= 0x0a
    hash = Math.imul(hash, 0x01000193)
  }
  return `${keys.length}:${(hash >>> 0).toString(16)}`
}

/** The simulation's picture as it stands, positions rounded to a tenth of a pixel. */
export function placementOf(sim: Simulation): Placement {
  const positions: Record<string, [number, number]> = {}
  for (const node of sim.nodes) {
    positions[node.path] = [Math.round(node.x * 10) / 10, Math.round(node.y * 10) / 10]
  }
  return { positions, links: linkDigest(sim.links.map((link) => link.key)), alpha: sim.alpha }
}

/**
 * A placement read back from storage, or `null` when it is not one. A position that is not two
 * finite numbers is dropped on its own — that document is seeded instead — so one bad entry
 * written by hand costs one node, not the picture.
 */
export function parsePlacement(raw: unknown): Placement | null {
  if (!raw || typeof raw !== 'object') return null
  const source = raw as Record<string, unknown>
  if (typeof source.links !== 'string') return null
  if (!source.positions || typeof source.positions !== 'object') return null
  const alpha =
    typeof source.alpha === 'number' && Number.isFinite(source.alpha)
      ? Math.min(1, Math.max(0, source.alpha))
      : 1
  const positions: Record<string, [number, number]> = {}
  for (const [path, value] of Object.entries(source.positions as Record<string, unknown>)) {
    if (
      Array.isArray(value) &&
      value.length === 2 &&
      value.every((part) => typeof part === 'number' && Number.isFinite(part))
    ) {
      positions[path] = [value[0], value[1]]
    }
  }
  return { positions, links: source.links, alpha }
}

/**
 * A simulation for the corpus.
 *
 * A document the previous placement already had starts where it was, so a corpus refreshed
 * under the page — an agent's write, a rename — moves the picture only as far as the change
 * pushes it, and a page opened again over the same corpus opens exactly as it was left. A new
 * document starts from its **path's** seed (`seedPosition`), for the reason that function gives:
 * the same corpus opens in the same shape every time, and testably so.
 *
 * How hot it starts is what the placement still explains: every document and the same links
 * — as hot as it was, which for a picture left at rest is still; the same documents with links
 * changed — a nudge; some documents new — enough to fit them in around the rest; nothing known
 * — the whole settle.
 */
export function createSimulation(
  graph: DocGraph,
  forces: GraphForces,
  previous?: Placement | null,
): Simulation {
  const kept = new Map(
    Object.entries(previous?.positions ?? {}).map(([path, [x, y]]) => [path, { x, y }]),
  )
  // Spread the seeds over roughly the area the settled picture will take, so the first ticks
  // pull together instead of exploding outwards.
  const spread = (forces.linkDistance * Math.sqrt(Math.max(graph.nodes.length, 1))) / (BOX / 2)

  const nodes: SimNode[] = graph.nodes.map((entry) => {
    const old = kept.get(entry.path)
    const seed = seedPosition(entry.path)
    return {
      path: entry.path,
      title: entry.title,
      folder: folderOf(entry.path),
      x: old ? old.x : (seed.x - BOX / 2) * spread,
      y: old ? old.y : (seed.y - BOX / 2) * spread,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null,
      degree: 0,
      scale: 1,
    }
  })

  const index = new Map(nodes.map((node, at) => [node.path, at]))
  const neighbours = nodes.map(() => new Set<number>())
  const seen = new Set<string>()
  const links: SimLink[] = []
  for (const edge of graph.edges) {
    const source = index.get(edge.source)
    const target = index.get(edge.target)
    if (source === undefined || target === undefined || source === target) continue
    const key =
      edge.source < edge.target ? `${edge.source}|${edge.target}` : `${edge.target}|${edge.source}`
    if (seen.has(key)) continue
    seen.add(key)
    links.push({ key, source, target })
    nodes[source].degree += 1
    nodes[target].degree += 1
    neighbours[source].add(target)
    neighbours[target].add(source)
  }

  const maxDegree = nodes.reduce((most, node) => Math.max(most, node.degree), 0)
  for (const node of nodes) node.scale = growth(node.degree, maxDegree)

  const known = nodes.filter((node) => kept.has(node.path)).length
  let alpha = 1
  if (previous && known === nodes.length) {
    alpha =
      previous.links === linkDigest(links.map((link) => link.key))
        ? previous.alpha
        : Math.max(previous.alpha, 0.3)
  } else if (previous && known > 0) {
    alpha = Math.max(previous.alpha, 0.5)
  }
  return {
    nodes,
    links,
    neighbours,
    forces: { ...forces },
    alpha,
    alphaTarget: 0,
  }
}

/** Wake the simulation up to at least `alpha` — a setting moved, or a node was let go. */
export function reheat(sim: Simulation, alpha = 0.5): void {
  sim.alpha = Math.max(sim.alpha, alpha)
}

/** Whether another tick would move anything. */
export function isRunning(sim: Simulation): boolean {
  return sim.alpha >= ALPHA_MIN || sim.alphaTarget > 0
}

/**
 * One tick. Returns whether the simulation is still moving, so the page knows when to stop
 * asking for frames.
 */
export function step(sim: Simulation): boolean {
  if (!isRunning(sim)) return false
  sim.alpha += (sim.alphaTarget - sim.alpha) * ALPHA_DECAY
  const { nodes, links, forces, alpha } = sim
  const n = nodes.length

  // Springs, d3's way: each end moves in proportion to how lightly the *other* is anchored,
  // and a spring between two hubs is weakened by their degree, or a hub would crush its
  // neighbourhood into a point.
  for (const link of links) {
    const a = nodes[link.source]
    const b = nodes[link.target]
    let dx = b.x + b.vx - a.x - a.vx
    let dy = b.y + b.vy - a.y - a.vy
    let length = Math.hypot(dx, dy)
    if (length < 1e-6) {
      dx = jiggle(link.source, link.target)
      dy = jiggle(link.target, link.source)
      length = Math.hypot(dx, dy)
    }
    const strength = forces.linkStrength / Math.max(1, Math.min(a.degree, b.degree))
    const pull = ((length - forces.linkDistance) / length) * alpha * strength
    const bias = a.degree / (a.degree + b.degree)
    b.vx -= dx * pull * bias
    b.vy -= dy * pull * bias
    a.vx += dx * pull * (1 - bias)
    a.vy += dy * pull * (1 - bias)
  }

  // Repulsion and collision share one pass over the pairs. Every pair pushes; a pair with no
  // link between them pushes harder still, by the unlinked slider.
  const push = charge(forces) * alpha
  const unlinkedPush = -UNLINKED_PUSH * forces.unlinkedRepel * alpha
  const range2 = REPEL_RANGE * REPEL_RANGE
  for (let i = 0; i < n; i += 1) {
    const a = nodes[i]
    const linked = sim.neighbours[i]
    const reach = radiusOf(a, forces) + 4
    for (let j = i + 1; j < n; j += 1) {
      const b = nodes[j]
      // Circles keep clear by their own sizes, so a hub does not sit on its neighbours.
      const apart = reach + radiusOf(b, forces)
      let dx = b.x - a.x
      let dy = b.y - a.y
      let length2 = dx * dx + dy * dy
      if (length2 < 1e-6) {
        dx = jiggle(i, j)
        dy = jiggle(j, i)
        length2 = dx * dx + dy * dy
      }
      if (length2 < range2) {
        const w = (linked.has(j) ? push : push + unlinkedPush) / Math.max(length2, 1)
        a.vx += dx * w
        a.vy += dy * w
        b.vx -= dx * w
        b.vy -= dy * w
      }
      if (length2 < apart * apart) {
        const length = Math.sqrt(length2)
        const overlap = ((apart - length) / length) * COLLIDE * 0.5
        a.vx -= dx * overlap
        a.vy -= dy * overlap
        b.vx += dx * overlap
        b.vy += dy * overlap
      }
    }
  }

  // A folder pulls towards its own middle — a centroid rather than a spring per pair, so it is
  // one pass however large the folder, and a folder of one document is left alone.
  if (forces.folderStrength > 0) {
    const centres = new Map<string, { x: number; y: number; count: number }>()
    for (const node of nodes) {
      const centre = centres.get(node.folder) ?? { x: 0, y: 0, count: 0 }
      centre.x += node.x
      centre.y += node.y
      centre.count += 1
      centres.set(node.folder, centre)
    }
    const k = forces.folderStrength * FOLDER_PULL * alpha
    for (const node of nodes) {
      const centre = centres.get(node.folder)!
      if (centre.count < 2) continue
      node.vx += (centre.x / centre.count - node.x) * k
      node.vy += (centre.y / centre.count - node.y) * k
    }
  }

  for (const node of nodes) {
    node.vx -= node.x * GRAVITY * alpha
    node.vy -= node.y * GRAVITY * alpha
    if (node.fx !== null && node.fy !== null) {
      node.x = node.fx
      node.y = node.fy
      node.vx = 0
      node.vy = 0
      continue
    }
    node.vx *= 1 - VELOCITY_DECAY
    node.vy *= 1 - VELOCITY_DECAY
    node.x += node.vx
    node.y += node.vy
  }
  return isRunning(sim)
}

/**
 * A tiny, deterministic nudge for two points on top of each other — d3 uses `Math.random()`,
 * which would make the same corpus settle differently on every visit.
 */
function jiggle(a: number, b: number): number {
  return (((a * 7919 + b * 104729) % 1000) / 1000 - 0.5) * 1e-3
}

export interface Bounds {
  minX: number
  minY: number
  maxX: number
  maxY: number
}

/** The box around every node, circles included — what "fit to screen" frames. */
export function bounds(sim: Simulation): Bounds | null {
  if (!sim.nodes.length) return null
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const node of sim.nodes) {
    const r = radiusOf(node, sim.forces)
    minX = Math.min(minX, node.x - r)
    minY = Math.min(minY, node.y - r)
    maxX = Math.max(maxX, node.x + r)
    maxY = Math.max(maxY, node.y + r)
  }
  return { minX, minY, maxX, maxY }
}
